"""
Telegram 推播的測試。

重點不在「訊息長得漂亮」，而在三件會讓人默默收不到通知的事：
  1. 章節抓錯或抓不到 —— 報告格式是 prompts/ 規定的，改了這邊要出聲。
  2. 回應碼沒檢查 —— LINE 那次 401 連綠勾九天，不能重演。
  3. 權杖外洩 —— Telegram 把 token 放在 URL 裡，印錯一行就等於公開。

抓段落的部分直接餵 docs/ 下的真實報告，不自己編造格式。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 和 test_guard.py 同樣的理由：測試會走到 emit_error／emit_warning，
# 不該在 run 頁面上掛出一堆假的 annotation 把真錯誤埋掉。
os.environ.pop("GITHUB_ACTIONS", None)

from premarket import notify_telegram as tg  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
TOKEN = "123456:AAFAKEtokenNOTreal"
CHAT = "87654321"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}


def _recent(stem: str, n: int = 5) -> list[Path]:
    return sorted(DOCS.glob(f"{stem}-*.md"))[-n:]


class TestSectionSplit(unittest.TestCase):
    """章節切割。編號會隨章節數變動（盤前 8 節、盤後 10 節），不能靠編號定位。"""

    MD = (
        "# 台股盤前分析 2026-11-04\n\n"
        "## 2. 夜盤與期現價差\n\n"
        "**開盤推估以夜盤台指期收盤為主要依據。**\n\n"
        "## 7. 今日開盤推估與盤面預期\n\n"
        "夜盤小幅走升 0.93%。\n\n"
        "## 8. 一句話總結\n\n"
        "訊號分歧，追價前先確認量能。\n\n"
        "> 本報告由程式自動彙整公開數據生成，非投資建議。\n"
    )

    def test_heading_number_is_stripped(self):
        headings = [h for h, _ in tg.split_sections(self.MD)]
        self.assertIn("一句話總結", headings)
        self.assertIn("今日開盤推估與盤面預期", headings)

    def test_keyword_matches_heading_not_body(self):
        """「開盤推估」這四個字也出現在第 2 節的內文裡。

        抓錯段落比抓不到更難發現 —— 訊息看起來正常，內容卻是別節的。
        """
        found = tg.find_section(tg.split_sections(self.MD), "開盤推估")
        assert found is not None
        heading, body = found
        self.assertEqual(heading, "今日開盤推估與盤面預期")
        self.assertIn("0.93%", body)

    def test_missing_section_returns_none(self):
        self.assertIsNone(tg.find_section(tg.split_sections(self.MD), "明日觀察"))

    def test_disclaimer_is_not_part_of_the_message(self):
        """免責聲明每份都有，推播帶上它只是佔掉手機一個畫面。"""
        found = tg.find_section(tg.split_sections(self.MD), "一句話總結")
        assert found is not None
        lines = tg.clean_body(found[1])
        self.assertEqual(lines, ["訊號分歧，追價前先確認量能。"])

    def test_table_rows_are_dropped(self):
        body = "說明一行。\n\n| 代號 | 名稱 |\n|---|---|\n| 2330 | 台積電 |"
        self.assertEqual(tg.clean_body(body), ["說明一行。"])

    def test_markdown_emphasis_is_stripped(self):
        self.assertEqual(tg.clean_body("**粗體**與`程式碼`"), ["粗體與程式碼"])


class TestTrim(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(tg.trim("短短一句。", 100), "短短一句。")

    def test_cuts_at_sentence_end(self):
        text = "第一句話在這裡結束。第二句話比較長一點也結束了。第三句被丟掉。"
        out = tg.trim(text, 30)
        self.assertTrue(out.endswith("。"), out)
        self.assertNotIn("第三句", out)

    def test_falls_back_to_ellipsis_when_sentence_end_is_too_early(self):
        """只有開頭有句點時切在那裡會只剩兩個字 —— 寧可用刪節號。"""
        text = "好。" + "一" * 200
        out = tg.trim(text, 50)
        self.assertTrue(out.endswith("…"), out)
        self.assertEqual(len(out), 50)


class TestBullets(unittest.TestCase):
    """條列整條保留或整條不要 —— 砍半的條列看起來像資料掉了。"""

    def test_all_bullets_kept_when_within_budget(self):
        lines = ["- 第一項。", "- 第二項。"]
        out = tg.summarize(lines, 100)
        self.assertEqual(out, "• 第一項。\n• 第二項。")
        self.assertNotIn("另", out)

    def test_overflow_bullets_are_counted_not_cut(self):
        lines = [f"- 第{i}項" + "長" * 40 + "。" for i in range(5)]
        out = tg.summarize(lines, 100)
        self.assertIn("…另 3 項見完整報告", out)
        self.assertEqual(out.count("•"), 2)

    def test_single_oversized_bullet_is_trimmed(self):
        out = tg.summarize(["- " + "長" * 500 + "。"], 100)
        self.assertLessEqual(len(out), 110)

    def test_paragraph_is_joined_not_bulleted(self):
        out = tg.summarize(["第一行。", "第二行。"], 100)
        self.assertEqual(out, "第一行。 第二行。")


class TestRealReports(unittest.TestCase):
    """拿 docs/ 下最近幾份真實報告驗抓段落 —— 格式規範寫在 prompts/，不是我編的。"""

    def _check(self, session: str, paths: list[Path]):
        self.assertTrue(paths, f"docs/ 下找不到 {session} 報告")
        sess = tg.SESSIONS[session]
        for p in paths:
            iso = p.stem[len(sess.stem) + 1:]
            with self.subTest(report=p.name):
                text, missing = tg.build_report_message(sess, iso, p.read_text(encoding="utf-8"))
                self.assertEqual(missing, [], f"{p.name} 抓不到章節")
                self.assertIn("一句話總結", text)
                self.assertIn(sess.detail_key, text)
                # 手機通知欄要一個畫面看得完
                self.assertLess(len(text), 1000, f"{p.name} 訊息太長")
                self.assertNotIn("本報告由程式", text)   # 免責聲明
                self.assertNotIn("**", text)             # Markdown 殘留
                self.assertNotIn("|", text)              # 表格殘留

    def test_premarket_reports(self):
        self._check("premarket", _recent("premarket"))

    def test_postmarket_reports(self):
        self._check("postmarket", _recent("postmarket"))

    def test_weekday_label(self):
        self.assertEqual(tg.when_label("2026-11-04"), "11/04（三）")

    def test_unparseable_date_is_passed_through(self):
        self.assertEqual(tg.when_label("latest"), "latest")


class TestMessageBuilding(unittest.TestCase):
    def test_renamed_sections_are_reported_but_message_still_sends(self):
        """章節名稱被改掉時：訊息照送（連結仍然是對的），但要留 warning。"""
        md = "# 台股盤前分析 2026-11-04\n\n## 1. 今天的感想\n\n隨便寫寫。\n"
        text, missing = tg.build_report_message(tg.SESSIONS["premarket"], "2026-11-04", md)
        self.assertEqual(missing, ["一句話總結", "開盤推估"])
        self.assertIn("摘要段落抓不到", text)
        self.assertIn("11/04", text)

    def test_empty_section_counts_as_missing(self):
        md = "## 8. 一句話總結\n\n> 只有免責聲明。\n"
        _, missing = tg.build_report_message(tg.SESSIONS["premarket"], "2026-11-04", md)
        self.assertIn("一句話總結", missing)

    def test_html_special_characters_are_escaped(self):
        """parse_mode=HTML 之下沒轉義的 < 會讓 Telegram 回 400，整則不送。"""
        md = "## 8. 一句話總結\n\n外資 <賣超> 1 & 2 的 5MA<10MA。\n"
        text, _ = tg.build_report_message(tg.SESSIONS["premarket"], "2026-11-04", md)
        self.assertIn("&lt;賣超&gt;", text)
        self.assertIn("1 &amp; 2", text)

    def test_failure_message_carries_the_reason(self):
        text = tg.build_failure_message(tg.SESSIONS["postmarket"], "分析產出失敗", "2026-11-04")
        self.assertIn("台股盤後籌碼", text)
        self.assertIn("11/04", text)
        self.assertIn("分析產出失敗", text)
        self.assertIn("沒有發布", text)

    def test_failure_message_without_reason_still_says_something(self):
        text = tg.build_failure_message(tg.SESSIONS["premarket"], "")
        self.assertIn("原因不明", text)


class TestReportUrl(unittest.TestCase):
    def test_pages_url_trailing_slash(self):
        with patch.dict("os.environ", {"PAGES_URL": "https://x.github.io/y/"}, clear=True):
            self.assertEqual(
                tg.report_url(tg.SESSIONS["premarket"], "2026-11-04"),
                "https://x.github.io/y/premarket-2026-11-04.html",
            )

    def test_falls_back_to_repo_markdown(self):
        """沒設 PAGES_URL（fork 的人常常沒開 Pages）時，連到 repo 裡的 .md 仍然看得到內容。"""
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "a/b"}, clear=True):
            self.assertEqual(
                tg.report_url(tg.SESSIONS["postmarket"], "2026-11-04"),
                "https://github.com/a/b/blob/main/docs/postmarket-2026-11-04.md",
            )

    def test_no_url_available(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(tg.report_url(tg.SESSIONS["premarket"], "2026-11-04"), "")


class TestLatestReport(unittest.TestCase):
    """手動觸發時 gen 會因「報告已存在」而略過，沒有 report_date 可用。"""

    def test_picks_newest_and_ignores_the_other_session(self):
        with tempfile.TemporaryDirectory() as d:
            docs = Path(d)
            for name in (
                "premarket-2026-10-30.md",
                "premarket-2026-11-04.md",
                "postmarket-2026-11-05.md",   # 另一種報告，不該被選中
                "premarket-latest.md",        # 不是日期
            ):
                (docs / name).write_text("x", encoding="utf-8")
            found = tg.latest_report(tg.SESSIONS["premarket"], docs)
            assert found is not None
            self.assertEqual(found[0], "2026-11-04")

    def test_empty_docs(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(tg.latest_report(tg.SESSIONS["premarket"], Path(d)))


class TestDeliver(unittest.TestCase):
    """回應碼一定要檢查。LINE 那次 401 連綠勾九天，就是因為 curl 沒看回應。"""

    def _deliver(self, code: int, body: str = "{}"):
        buf = io.StringIO()
        with patch.object(tg, "_http_post", return_value=(code, body)) as post:
            with redirect_stdout(buf):
                rc = tg.deliver("訊息", ("看完整報告", "https://x/y.html"), TOKEN, CHAT)
        return rc, buf.getvalue(), post

    def test_success(self):
        rc, out, _ = self._deliver(200, '{"ok":true}')
        self.assertEqual(rc, 0)
        self.assertIn("已送出", out)

    def test_bad_token_is_a_loud_error(self):
        """404 = 權杖打錯（token 在 URL 裡）。重試不會好，要有人去改 secret。"""
        rc, out, _ = self._deliver(404, '{"ok":false,"description":"Not Found"}')
        self.assertEqual(rc, 1)
        self.assertIn("Not Found", out)
        self.assertIn("TELEGRAM_BOT_TOKEN", out)

    def test_bad_chat_id_is_a_loud_error(self):
        rc, out, _ = self._deliver(400, '{"ok":false,"description":"chat not found"}')
        self.assertEqual(rc, 1)
        self.assertIn("chat not found", out)

    def test_server_error_is_only_a_warning(self):
        """5xx 是 Telegram 自己的問題，報告已經發布了，不值得把 run 變紅。"""
        rc, out, _ = self._deliver(503, "upstream down")
        self.assertEqual(rc, 0)
        self.assertIn("暫時性", out)

    def test_connection_failure_is_only_a_warning(self):
        rc, _, _ = self._deliver(0, "URLError: timed out")
        self.assertEqual(rc, 0)

    def test_token_never_appears_in_output(self):
        """Telegram 把權杖放在 URL 裡。錯誤訊息印出 URL 就等於把它公開在 run 頁面上。"""
        rc, out, post = self._deliver(404, '{"ok":false,"description":"Not Found"}')
        self.assertEqual(rc, 1)
        self.assertIn(TOKEN, post.call_args.args[0])   # 真的有打對端點
        self.assertNotIn(TOKEN, out)

    def test_button_becomes_an_inline_keyboard(self):
        with patch.object(tg, "_http_post", return_value=(200, "{}")) as post:
            with redirect_stdout(io.StringIO()):
                tg.send("訊息", token=TOKEN, chat_id=CHAT, button=("看完整報告", "https://x/y"))
        payload = post.call_args.args[1]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(
            payload["reply_markup"],
            {"inline_keyboard": [[{"text": "看完整報告", "url": "https://x/y"}]]},
        )

    def test_button_omitted_when_there_is_no_url(self):
        with patch.object(tg, "_http_post", return_value=(200, "{}")) as post:
            with redirect_stdout(io.StringIO()):
                tg.send("訊息", token=TOKEN, chat_id=CHAT, button=("看完整報告", ""))
        self.assertNotIn("reply_markup", post.call_args.args[1])

    def test_description_falls_back_to_raw_body(self):
        self.assertIn("502 Bad Gateway", tg.describe("<html>502 Bad Gateway</html>"))


class TestCli(unittest.TestCase):
    def _run(self, argv: list[str], env: dict, response=(200, '{"ok":true}')):
        buf = io.StringIO()
        with patch.dict("os.environ", env, clear=True):
            with patch.object(tg, "_http_post", return_value=response) as post:
                with redirect_stdout(buf):
                    rc = tg.main(argv)
        return rc, buf.getvalue(), post

    def test_missing_secret_skips_quietly(self):
        """fork 的人沒設 secret 就安靜略過，不要讓他的 workflow 變紅。"""
        rc, out, post = self._run(["--session", "premarket"], {})
        self.assertEqual(rc, 0)
        post.assert_not_called()
        self.assertIn("略過", out)

    def test_sends_the_requested_date(self):
        rc, _, post = self._run(
            ["--session", "premarket", "--date", "2026-11-04"],
            {**ENV, "PAGES_URL": "https://x.github.io/y"},
        )
        self.assertEqual(rc, 0)
        payload = post.call_args.args[1]
        self.assertIn("11/04", payload["text"])
        self.assertEqual(
            payload["reply_markup"]["inline_keyboard"][0][0]["url"],
            "https://x.github.io/y/premarket-2026-11-04.html",
        )

    def test_empty_date_resends_the_latest(self):
        """手動觸發（gen 略過）時沒有 report_date，改送最新一份就好，不用再花 API 費用。"""
        rc, out, post = self._run(["--session", "postmarket", "--date", ""], ENV)
        self.assertEqual(rc, 0)
        self.assertIn("最新一份", out)
        self.assertIn("台股盤後籌碼", post.call_args.args[1]["text"])

    def test_requested_date_missing_sends_a_warning_and_fails(self):
        """指定日期卻找不到檔案 = 程式或 workflow 壞了。不要默默改送別天的報告。"""
        with tempfile.TemporaryDirectory() as d:
            docs = Path(d)
            (docs / "premarket-2026-01-02.md").write_text("## 8. 一句話總結\n\n舊的。\n", "utf-8")
            with patch.object(tg, "DOCS", docs):
                rc, out, post = self._run(["--date", "2026-11-04"], ENV)
        self.assertEqual(rc, 1)
        self.assertIn("找不到", out)
        text = post.call_args.args[1]["text"]
        self.assertIn("沒有發布", text)
        self.assertNotIn("舊的", text)

    def test_failure_mode_links_to_the_run(self):
        rc, _, post = self._run(
            ["--session", "premarket", "--failure", "--reason", "執行被中止（可能是超時）",
             "--run-url", "https://github.com/a/b/actions/runs/9"],
            ENV,
        )
        self.assertEqual(rc, 0)
        payload = post.call_args.args[1]
        self.assertIn("執行被中止", payload["text"])
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button, {"text": "看執行紀錄", "url": "https://github.com/a/b/actions/runs/9"})

    def test_renamed_section_warns_in_the_run_page(self):
        with tempfile.TemporaryDirectory() as d:
            docs = Path(d)
            (docs / "premarket-2026-11-04.md").write_text("## 1. 感想\n\n隨便。\n", "utf-8")
            with patch.object(tg, "DOCS", docs):
                with patch.dict("os.environ", {**ENV, "GITHUB_ACTIONS": "true"}, clear=True):
                    buf = io.StringIO()
                    with patch.object(tg, "_http_post", return_value=(200, "{}")):
                        with redirect_stdout(buf):
                            rc = tg.main(["--date", "2026-11-04"])
        self.assertEqual(rc, 0)       # 摘要缺一段不算失敗，報告本身是好的
        self.assertIn("::warning::", buf.getvalue())
        self.assertIn("抓不到", buf.getvalue())

    def test_dry_run_sends_nothing(self):
        rc, out, post = self._run(["--date", "2026-11-04", "--dry-run"], {})
        self.assertEqual(rc, 0)
        post.assert_not_called()
        self.assertIn("台股盤前分析", out)


if __name__ == "__main__":
    unittest.main()
