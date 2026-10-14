"""
報告產出的失敗攔截測試。

背景：盤後第一份報告只產出 178 bytes（模型在第一個表格中斷），
但程式回報成功，殘缺報告被 workflow commit 並發布到 Pages。
這類 bug 不會拋例外、log 也全綠，只有測試攔得住。

不呼叫 API、不連網路：fetch 與 generate_brief 都以假物件替換，
輸出目錄導到暫存資料夾，所以可以隨時重跑。

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from premarket import analyze, fetch, main as main_mod  # noqa: E402

DATE = "2026-08-06"
GOOD_BRIEF = "## 1. 今日 K 線的形態\n\n" + "測試內容。" * 200  # 遠超過 500 字元門檻


class StubPayload:
    """替代 fetch_post.PostPayload —— 主流程只用到這三個介面。"""

    missing: list = []

    def to_dict(self) -> dict:
        return {"session_date": DATE, "derived": {"加權指數收盤": 44611.6}, "missing": []}


def run_main(brief_result, tmpdir: Path) -> int:
    """在暫存輸出目錄跑一次 main()，brief_result 可為字串或要拋的例外。"""
    kwargs = (
        {"side_effect": brief_result}
        if isinstance(brief_result, BaseException)
        else {"return_value": brief_result}
    )
    with (
        patch.object(main_mod, "DOCS", tmpdir),
        patch.object(main_mod, "DATA", tmpdir / "data"),
        patch.object(main_mod.fetch, "fetch_taiex_ohlc", return_value=[{"close": 1.0}]),
        # 休市檢查另有專屬測試（TestHolidaySkip）；其餘案例一律當成有開盤，
        # 免得每個測試都要連帶模擬證交所的指數回應。
        patch.object(main_mod, "market_traded_on", return_value=True),
        patch("premarket.fetch_post.build_postmarket_payload", return_value=StubPayload()),
        patch.object(analyze, "generate_brief", **kwargs),
        patch.object(main_mod.render, "render", return_value="<html>x</html>"),
    ):
        sys.argv = ["premarket.main", "--session", "postmarket", "--date", DATE]
        return main_mod.main()


class TestBriefGuards(unittest.TestCase):
    """報告不完整時，必須 exit 非零且不留下任何報告檔案。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def report_files(self) -> list[str]:
        return sorted(p.name for p in self.tmpdir.glob("*.md")) + sorted(
            p.name for p in self.tmpdir.glob("*.html")
        )

    def test_truncated_response_fails_and_writes_nothing(self) -> None:
        code = run_main(analyze.BriefTruncated("模擬：撞到 max_tokens"), self.tmpdir)
        self.assertEqual(code, 1, "撞到 max_tokens 必須回傳非零 exit code")
        self.assertEqual(self.report_files(), [], "失敗時不得寫出報告檔案")

    def test_too_short_response_fails_and_writes_nothing(self) -> None:
        code = run_main("## 1. 今日K線\n\n| 開盤 |\n|---|", self.tmpdir)
        self.assertEqual(code, 1, "產出過短必須回傳非零 exit code")
        self.assertEqual(self.report_files(), [], "失敗時不得寫出報告檔案")

    def test_boundary_just_below_threshold(self) -> None:
        code = run_main("x" * (analyze.MIN_BRIEF_CHARS - 1), self.tmpdir)
        self.assertEqual(code, 1, "剛好低於門檻一個字元仍須視為失敗")

    def test_valid_response_succeeds_and_writes_files(self) -> None:
        code = run_main(GOOD_BRIEF, self.tmpdir)
        self.assertEqual(code, 0)
        # 盤後不得覆蓋 index.html —— 那是盤前的固定網址
        self.assertEqual(
            self.report_files(),
            [f"postmarket-{DATE}.md", "latest-postmarket.html", f"postmarket-{DATE}.html"],
        )
        self.assertFalse((self.tmpdir / "index.html").exists(), "盤後不得產生 index.html")


class TestTruncationDetection(unittest.TestCase):
    """analyze 層：stop_reason 為 max_tokens 時要拋 BriefTruncated。"""

    class _Block:
        type = "text"
        text = "被截斷的開頭"

    class _Usage:
        input_tokens = 16118
        output_tokens = 16000

    def _fake_client(self, stop_reason: str):
        outer = self

        class FakeMessages:
            def create(self, **_kw):
                class Resp:
                    content = [outer._Block()]
                    usage = outer._Usage()
                Resp.stop_reason = stop_reason
                return Resp()

        class FakeClient:
            def __init__(self, **_kw):
                self.messages = FakeMessages()

        return FakeClient

    def test_max_tokens_raises(self) -> None:
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-not-used"}),
            patch.object(analyze.anthropic, "Anthropic", self._fake_client("max_tokens")),
        ):
            with self.assertRaises(analyze.BriefTruncated):
                analyze.generate_brief({"missing": []}, session="postmarket")

    def test_end_turn_returns_text(self) -> None:
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-not-used"}),
            patch.object(analyze.anthropic, "Anthropic", self._fake_client("end_turn")),
        ):
            self.assertEqual(
                analyze.generate_brief({"missing": []}, session="postmarket"), "被截斷的開頭"
            )


class TestApiErrorClassification(unittest.TestCase):
    """
    API 失敗要分成「要人動手」與「等下次排程」兩類。

    背景：金鑰會定期到期（2026-09-05 那把就是），失效時原本只會噴一坨
    AuthenticationError 的 traceback，要往上滾才看得懂該做什麼。
    而限流／過載長得很像，處理方式卻完全相反 —— 前者要換 secret，
    後者放著等備援排程重跑就好。
    """

    def _raising_client(self, exc: BaseException):
        class FakeMessages:
            def create(self, **_kw):
                raise exc

        class FakeClient:
            def __init__(self, **_kw):
                self.messages = FakeMessages()

        return FakeClient

    def _call(self, exc: BaseException):
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-not-used"}),
            patch.object(analyze.anthropic, "Anthropic", self._raising_client(exc)),
        ):
            return analyze.generate_brief({"missing": []}, session="postmarket")

    class _FakeHTTPResponse:
        """
        anthropic 例外建構子會碰到的欄位就這三個，不需要真的 HTTP 物件。

        刻意不 import httpx：SDK 1.x 起改用 httpx2 當底層，測試若直接相依
        HTTP 套件，會隨著 SDK 換底層而在 CI 整個掛掉 —— 2026-09-04 就是這樣,
        一行 import 連垮四次排程（盤後盤前的主排程與備援各一）。
        """

        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.request = None
            self.headers: dict[str, str] = {}

    def _status_error(self, cls, status: int):
        return cls("boom", response=self._FakeHTTPResponse(status), body=None)

    def test_expired_key_becomes_actionable_message(self) -> None:
        with self.assertRaises(analyze.APIKeyInvalid) as ctx:
            self._call(self._status_error(analyze.anthropic.AuthenticationError, 401))
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception), "訊息要指名該去換哪個 secret")

    def test_permission_denied_is_also_a_key_problem(self) -> None:
        with self.assertRaises(analyze.APIKeyInvalid):
            self._call(self._status_error(analyze.anthropic.PermissionDeniedError, 403))

    def test_rate_limit_is_transient(self) -> None:
        with self.assertRaises(analyze.APIUnavailable):
            self._call(self._status_error(analyze.anthropic.RateLimitError, 429))

    def test_overloaded_is_transient(self) -> None:
        with self.assertRaises(analyze.APIUnavailable):
            self._call(self._status_error(analyze.anthropic.OverloadedError, 529))

    def test_connection_error_is_transient(self) -> None:
        with self.assertRaises(analyze.APIUnavailable):
            self._call(analyze.anthropic.APIConnectionError(request=None))

    def test_unclassified_error_keeps_its_traceback(self) -> None:
        # 400／404 代表模型名或參數被改壞了，包裝成人話反而蓋掉線索
        with self.assertRaises(analyze.anthropic.BadRequestError):
            self._call(self._status_error(analyze.anthropic.BadRequestError, 400))

    def test_main_exits_1_without_writing_files(self) -> None:
        """主流程要把這些當成失敗，且不留下半份報告。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            code = run_main(
                analyze.APIKeyInvalid("金鑰過期，請更新 ANTHROPIC_API_KEY"), tmpdir
            )
            self.assertEqual(code, 1)
            self.assertEqual(sorted(p.name for p in tmpdir.glob("*.md")), [])


class TestTargetDate(unittest.TestCase):
    """排程延遲跨過午夜時，盤後的目標日期必須回推。"""

    def _at(self, iso: str, session: str = "postmarket"):
        from datetime import datetime as dt

        return main_mod._target_date(dt.fromisoformat(iso), session)

    def test_postmarket_before_midnight_keeps_date(self) -> None:
        self.assertEqual(str(self._at("2026-08-10T23:53")), "2026-08-10")

    def test_postmarket_after_midnight_rolls_back(self) -> None:
        # 21:30 的排程延遲三小時 -> 00:30，仍應產出 8/10 的報告
        self.assertEqual(str(self._at("2026-08-11T00:30")), "2026-08-10")

    def test_postmarket_boundary_hour(self) -> None:
        # 界線是場次時間 21:00：盤後排程只在 21:23／22:17 觸發，
        # 早於 21 點執行的一定是被延遲的那一次，屬於前一天的場次。
        self.assertEqual(str(self._at("2026-08-10T20:59")), "2026-08-09")
        self.assertEqual(str(self._at("2026-08-10T21:00")), "2026-08-10")

    def test_morning_delay_rolls_back(self) -> None:
        """
        2026-10-13 的真實案例：排程 21:23，實際在隔天 05:45 與 06:07 才執行。

        舊的界線是 05:00，這兩次剛好跨過去，於是目標日期變成 10-13 ——
        當天還沒開盤，九個資料源全空，撞上「超過半數失敗」而中止。
        """
        self.assertEqual(str(self._at("2026-10-13T05:45")), "2026-10-12")
        self.assertEqual(str(self._at("2026-10-13T06:07")), "2026-10-12")

    def test_premarket_never_rolls_back(self) -> None:
        # 盤前本來就在清晨執行，回推會直接寫錯日期
        self.assertEqual(str(self._at("2026-08-11T06:45", "premarket")), "2026-08-11")
        # 盤前即使被延遲到中午才跑，日期仍是當天
        self.assertEqual(str(self._at("2026-10-13T11:30", "premarket")), "2026-10-13")


class TestHolidaySkip(unittest.TestCase):
    """
    國定假日要安靜結束，不能以失敗收場。

    2026-10-09 是國慶補假休市，但 is_trading_day() 只濾週末，於是整份照跑、
    九個資料源全空、撞上「超過半數失敗」而失敗（#86／#87），每逢假日就寄兩封
    失敗通知。休市不是故障。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, session: str, traded: bool):
        """回傳 (exit code, 有沒有真的去抓資料)。"""
        with (
            patch.object(main_mod, "DOCS", self.tmpdir),
            patch.object(main_mod, "DATA", self.tmpdir / "data"),
            patch.object(main_mod, "market_traded_on", return_value=traded),
            patch.object(main_mod.fetch, "fetch_taiex_ohlc", return_value=[{"close": 1.0}]),
            patch(
                "premarket.fetch_post.build_postmarket_payload", return_value=StubPayload()
            ) as post_fetch,
            patch.object(analyze, "generate_brief", return_value=GOOD_BRIEF),
            patch.object(main_mod.render, "render", return_value="<html>x</html>"),
        ):
            sys.argv = ["premarket.main", "--session", session, "--date", DATE]
            return main_mod.main(), post_fetch.called

    def test_holiday_exits_zero_without_fetching(self) -> None:
        code, fetched = self._run("postmarket", traded=False)
        self.assertEqual(code, 0, "休市是正常情況，不該回傳非零 exit code")
        self.assertFalse(fetched, "休市就不必白跑兩分鐘的抓取")
        self.assertEqual(sorted(p.name for p in self.tmpdir.glob("*.md")), [])

    def test_trading_day_proceeds(self) -> None:
        code, fetched = self._run("postmarket", traded=True)
        self.assertEqual(code, 0)
        self.assertTrue(fetched, "有開盤就要照常抓資料")

    def test_unknown_proceeds(self) -> None:
        """
        問不出來（None）要照常跑，絕不能當成休市。

        證交所回 HTTP 200 加一頁 HTML 時 fetch 也是回 None。把它當休市的話,
        端點一掛就會安靜跳過真正的交易日 —— 比失敗更糟,因為備援那次會做出
        同樣的誤判，而且沒有人會收到通知。
        """
        code, fetched = self._run("postmarket", traded=None)
        self.assertEqual(code, 0)
        self.assertTrue(fetched, "狀態不明時要照常抓，讓既有的缺漏保護去判斷")

    def test_premarket_does_not_consult_the_check(self) -> None:
        """盤前的目標日期是『今天、開盤前』，本來就查不到指數，不能拿來判斷休市。"""
        with (
            patch.object(main_mod, "DOCS", self.tmpdir),
            patch.object(main_mod, "DATA", self.tmpdir / "data"),
            patch.object(main_mod, "market_traded_on", return_value=False) as chk,
            patch.object(main_mod.fetch, "fetch_taiex_ohlc", return_value=[{"close": 1.0}]),
            patch.object(main_mod.fetch, "build_payload", return_value=StubPayload()),
            patch.object(main_mod, "load_history", return_value=[]),
            patch.object(analyze, "generate_brief", return_value=GOOD_BRIEF),
            patch.object(main_mod.render, "render", return_value="<html>x</html>"),
        ):
            sys.argv = ["premarket.main", "--date", DATE]
            code = main_mod.main()
        self.assertEqual(code, 0)
        chk.assert_not_called()


class TestErrorAnnotation(unittest.TestCase):
    """
    致命錯誤要同時寫成 GitHub annotation。

    2026-10 金鑰過期那次，「API 金鑰無效或已過期…請更新 ANTHROPIC_API_KEY」
    從第一天就印在日誌裡，但日誌要登入才看得到，run 頁面上只有一句
    「Process completed with exit code 1」—— 於是連續失敗九個交易日才被發現。
    """

    def _emit(self, msg: str, in_actions: bool):
        env = {"GITHUB_ACTIONS": "true"} if in_actions else {}
        buf = io.StringIO()
        with patch.dict("os.environ", env, clear=not in_actions):
            with redirect_stdout(buf):
                main_mod.emit_error(msg)
        return buf.getvalue()

    def test_writes_annotation_in_actions(self) -> None:
        out = self._emit("API 金鑰無效或已過期，請更新 ANTHROPIC_API_KEY。", True)
        self.assertIn("::error::", out)
        self.assertIn("ANTHROPIC_API_KEY", out)

    def test_collapses_newlines(self) -> None:
        # annotation 不能跨行，換行會讓訊息在 run 頁面上被截斷
        out = self._emit("第一行\n第二行\n第三行", True)
        self.assertEqual(out.count("::error::"), 1)
        self.assertNotIn("\n第", out)

    def test_silent_outside_actions(self) -> None:
        # 本機執行不該噴出 ::error:: 這種只有 CI 看得懂的字串
        self.assertEqual(self._emit("本機跑的錯誤", False), "")

    def test_unexpected_exception_still_annotates(self) -> None:
        """
        未分類的例外也要在 run 頁面留下一行。

        2026-10-14 的「API key is not scoped to a workspace」是 400，刻意不被
        BriefUnavailable 收編（traceback 對這類設定錯誤最有價值），結果 run 頁面上
        又只剩一句 exit code 1。
        """
        buf = io.StringIO()
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            with patch.object(main_mod, "main", side_effect=RuntimeError("壞掉了")):
                with redirect_stdout(buf):
                    code = main_mod.cli()
        self.assertEqual(code, 1)
        self.assertIn("::error::RuntimeError: 壞掉了", buf.getvalue())


class FakeResponse:
    """證交所忙碌時的樣子：HTTP 200，但 body 是一頁 HTML。"""

    def __init__(self, body: str, json_ok: bool = False) -> None:
        self.text = body
        self.encoding = "utf-8"
        self._json_ok = json_ok

    def raise_for_status(self) -> None:
        pass  # 200，狀態碼攔不到

    def json(self):
        if not self._json_ok:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return {"stat": "很抱歉，沒有符合條件的資料!"}


class TestNonJsonRetries(unittest.TestCase):
    """
    HTTP 200 + 非 JSON 必須重試。

    背景：2026-08-11 盤前 12 次請求全拿到 HTML，但重試只掛在 raise_for_status()
    上，一次都沒退避重試就放棄，整份報告因為找不到前一交易日而中止。
    """

    def _run(self, response, fn=lambda: fetch._get_json("https://www.twse.com.tw/x")):
        """跑一次請求，回傳 (結果, 實際送出的請求次數)。節流與退避都跳過，測試才不用等。"""
        calls = []

        def fake_request(method, url, **kw):
            calls.append(url)
            return response

        with (
            patch.object(fetch.requests, "request", side_effect=fake_request),
            patch.object(fetch, "_throttle"),
            patch.object(fetch.time, "sleep"),
        ):
            return fn(), len(calls)

    def test_html_body_is_retried_then_gives_up(self) -> None:
        result, n = self._run(FakeResponse("<html>系統忙碌</html>"))
        self.assertIsNone(result, "重試用盡後仍要回 None，讓呼叫端當成這項沒資料")
        self.assertEqual(n, fetch.RETRIES, "非 JSON 的 200 必須用滿重試次數")

    def test_valid_json_is_not_retried(self) -> None:
        # 「查無資料」是合法 JSON，由各 fetcher 判讀 stat，不該浪費重試次數
        result, n = self._run(FakeResponse("{}", json_ok=True))
        self.assertEqual(n, 1)
        self.assertNotEqual(result, None)

    def test_text_and_csv_paths_also_retry(self) -> None:
        def boom(r):
            raise ValueError("解析失敗")

        _, n = self._run(
            FakeResponse("x"), fn=lambda: fetch._fetch("GET", "https://www.twse.com.tw/x", boom)
        )
        self.assertEqual(n, fetch.RETRIES)


class TestBackupScheduleGuard(unittest.TestCase):
    """
    備援排程的守衛：報告已存在就不能再花一次 API 費用。

    背景：2026-08-27 盤前的 schedule 事件被 GitHub 整個丟掉，連 run 記錄都沒有，
    那天開天窗。對策是同一天排兩次 —— 但正常日子的第二次必須是零成本，
    否則等於每天多付一份報告的錢，還會多廣播一則重複的 LINE 通知。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, extra_args=(), report_on_disk: bool = True):
        """回傳 (exit code, generate_brief 的 mock)，好驗證 API 到底有沒有被呼叫。"""
        if report_on_disk:
            (self.tmpdir / f"postmarket-{DATE}.md").write_text("舊報告", encoding="utf-8")
        with (
            patch.object(main_mod, "DOCS", self.tmpdir),
            patch.object(main_mod, "DATA", self.tmpdir / "data"),
            patch.object(main_mod.fetch, "fetch_taiex_ohlc", return_value=[{"close": 1.0}]),
            patch.object(main_mod, "market_traded_on", return_value=True),
            patch("premarket.fetch_post.build_postmarket_payload", return_value=StubPayload()),
            patch.object(analyze, "generate_brief", return_value=GOOD_BRIEF) as brief,
            patch.object(main_mod.render, "render", return_value="<html>x</html>"),
        ):
            sys.argv = ["premarket.main", "--session", "postmarket", "--date", DATE, *extra_args]
            return main_mod.main(), brief

    def test_existing_report_skips_api_call(self) -> None:
        code, brief = self._run()
        self.assertEqual(code, 0, "略過不是失敗，exit code 要是 0")
        brief.assert_not_called()  # 這行就是「不會多花錢」的保證

    def test_force_regenerates(self) -> None:
        code, brief = self._run(extra_args=["--force"])
        self.assertEqual(code, 0)
        brief.assert_called_once()

    def test_no_report_runs_normally(self) -> None:
        code, brief = self._run(report_on_disk=False)
        self.assertEqual(code, 0)
        brief.assert_called_once()

    def test_dry_run_not_blocked(self) -> None:
        # dry-run 不呼叫 API 也不寫報告，沒有重複產出的問題，不該被守衛擋下
        code, brief = self._run(extra_args=["--dry-run"])
        self.assertEqual(code, 0)
        brief.assert_not_called()
        self.assertTrue((self.tmpdir / "data" / f"postpayload-{DATE}.json").exists())

    def test_skip_signals_workflow_to_mute_line(self) -> None:
        """略過時要寫 skipped=true 給 workflow，否則 LINE 會重複廣播。"""
        out = self.tmpdir / "gh_output"
        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}):
            code, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("skipped=true", out.read_text(encoding="utf-8"))

    def test_success_reports_the_report_date(self) -> None:
        """
        產出成功時要把「報告日期」交給 workflow。

        LINE 通知原本寫的是執行當天，補跑舊日期會發出一則標著今天、
        內容卻是別天的通知 —— 一次補七天就是七則都寫同一天。
        """
        out = self.tmpdir / "gh_output"
        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}):
            code, brief = self._run(report_on_disk=False)
        self.assertEqual(code, 0)
        brief.assert_called_once()
        self.assertIn(f"report_date={DATE}", out.read_text(encoding="utf-8"))

    def test_normal_run_does_not_signal_skip(self) -> None:
        out = self.tmpdir / "gh_output"
        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}):
            self._run(report_on_disk=False)
        # 成功路徑本來就會寫 report_date，所以檢查內容而不是檔案存不存在
        written = out.read_text(encoding="utf-8") if out.exists() else ""
        self.assertNotIn("skipped", written, "有產出報告時不得寫 skipped，不然通知會被誤擋")


class TestSlowDownloadDeadline(unittest.TestCase):
    """
    大檔下載要有總時限。

    背景：證交所那張 7MB 的 ISIN 表在 runner 上下載八分半還沒結束，把整個 job
    撞上 10 分鐘上限而被取消（2026-10-14）。requests 的 TIMEOUT 是「單次讀取」
    逾時，涓流式的回應每次都在 20 秒內送來一點，所以永遠觸發不了。
    """

    class _Streaming:
        def __init__(self, chunks):
            self._chunks = chunks
            self.encoding = "utf-8"

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            return iter(self._chunks)

    def _get(self, chunks, clock):
        with (
            patch.object(fetch.requests, "request", return_value=self._Streaming(chunks)),
            patch.object(fetch, "_throttle"),
            patch.object(fetch.time, "sleep"),
            patch.object(fetch.time, "monotonic", side_effect=clock),
        ):
            return fetch._get_text("https://isin.twse.com.tw/x", max_seconds=90)

    def test_slow_stream_gives_up(self) -> None:
        # 時鐘每次呼叫跳 60 秒：第二塊就超過 90 秒的預算
        clock = iter(range(0, 100_000, 60)).__next__
        self.assertIsNone(self._get([b"a", b"b", b"c"], clock), "超時要回 None，交給呼叫端決定")

    def test_fast_stream_succeeds(self) -> None:
        clock = iter(range(0, 100_000, 1)).__next__
        self.assertEqual(self._get([b"ab", b"cd"], clock), "abcd")


class TestEtfCacheFallback(unittest.TestCase):
    """重抓 ETF 名單失敗時，沿用過期的快取，不要拖垮整份報告。"""

    def setUp(self) -> None:
        from premarket import fetch_post

        self.fp = fetch_post
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "etf-codes.json"
        self.addCleanup(self._tmp.cleanup)

    def _write_cache(self, days_old: int, codes=("0050", "0056")) -> None:
        from datetime import datetime as dt, timedelta as td

        self.cache.write_text(
            json.dumps(
                {
                    "fetched_at": (dt.now() - td(days=days_old)).isoformat(timespec="seconds"),
                    "codes": list(codes),
                }
            ),
            encoding="utf-8",
        )

    def _run(self, days_old: int, refresh_result):
        self._write_cache(days_old)
        with (
            patch.object(self.fp, "ETF_CACHE", self.cache),
            patch.object(self.fp, "_get_text", return_value=refresh_result) as got,
        ):
            return self.fp.fetch_etf_codes(), got.called

    def test_fresh_cache_skips_network(self) -> None:
        codes, called = self._run(1, None)
        self.assertEqual(codes, {"0050", "0056"})
        self.assertFalse(called, "沒過期就不該連網")

    def test_expired_cache_survives_failed_refresh(self) -> None:
        # 這就是 2026-10-14 的情境：快取過期 13 天，重抓拿不到東西
        codes, called = self._run(13, None)
        self.assertTrue(called)
        self.assertEqual(codes, {"0050", "0056"}, "重抓失敗要沿用舊名單，不能回 None")

    def test_no_cache_and_failed_refresh_returns_none(self) -> None:
        with (
            patch.object(self.fp, "ETF_CACHE", self.cache),
            patch.object(self.fp, "_get_text", return_value=None),
        ):
            self.assertIsNone(self.fp.fetch_etf_codes(), "連舊名單都沒有時才回 None")


class TestPrevTradeDateFallback(unittest.TestCase):
    """問不到加權指數時，改用歷史 payload 的日期回推前一交易日。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, *names: str) -> None:
        for n in names:
            (self.data / n).write_text("{}", encoding="utf-8")

    def _prev(self, today: str):
        from datetime import date as _date

        with patch.object(main_mod, "DATA", self.data):
            return main_mod.prev_trade_date_from_history(_date.fromisoformat(today))

    def test_picks_latest_before_today(self) -> None:
        self._write(
            "payload-2026-08-07.json",
            "payload-2026-08-10.json",
            "postpayload-2026-08-10.json",
            "etf-codes.json",  # 不是 payload，不能被當成日期
        )
        self.assertEqual(str(self._prev("2026-08-11")), "2026-08-10")

    def test_ignores_today_and_future(self) -> None:
        # 盤前自己那份 payload 是後寫的，但補跑舊日期時可能已經存在
        self._write("payload-2026-08-10.json", "payload-2026-08-11.json")
        self.assertEqual(str(self._prev("2026-08-11")), "2026-08-10")

    def test_no_history_returns_none(self) -> None:
        self.assertIsNone(self._prev("2026-08-11"), "沒有歷史檔案時要回 None，讓主流程中止")

    def test_main_aborts_when_both_sources_fail(self) -> None:
        with (
            patch.object(main_mod, "DATA", self.data),
            # DOCS 也要導到暫存目錄：真實的 docs/ 已經有 DATE 那天的報告，
            # 會先被「報告已存在」的守衛攔下，測不到這裡要驗的中止路徑。
            patch.object(main_mod, "DOCS", self.data),
            patch.object(main_mod.fetch, "fetch_taiex_ohlc", return_value=None),
            patch.object(main_mod, "market_traded_on", return_value=True),
        ):
            sys.argv = ["premarket.main", "--session", "postmarket", "--date", DATE]
            self.assertEqual(main_mod.main(), 1)


if __name__ == "__main__":
    unittest.main()


class TestShortWatchlist(unittest.TestCase):
    """篩選邏輯：命中判定與「不可先賣後買排在後面」的排序。"""

    def _quotes(self, close, open_, high, vol):
        return {"name": "測試股", "open": open_, "high": high, "low": min(open_, close),
                "close": close, "volume": vol, "turnover": vol * close}

    def _run(self, today_bar, hist_close, hist_vol, foreign_net, eligible):
        from premarket import shortlist as sl

        dates = [f"2026-07-{d:02d}" for d in range(20, 30)]
        quotes = {
            ds: {"9999": self._quotes(hist_close, hist_close, hist_close, hist_vol)}
            for ds in dates[:-1]
        }
        quotes[dates[-1]] = {"9999": today_bar}
        with (
            patch.object(sl, "fetch_daily_quotes", side_effect=lambda d: quotes[d.isoformat()]),
            patch.object(sl, "fetch_foreign_net", return_value={"9999": foreign_net}),
            patch.object(sl, "fetch_daytrade_eligibility", return_value={"9999": eligible}),
            patch.object(sl, "fetch_short_margin", return_value={}),
        ):
            return sl.build_short_watchlist(dates)

    def test_volume_spike_with_black_candle_hits(self) -> None:
        bar = self._quotes(close=95.0, open_=100.0, high=101.0, vol=3_000_000)
        r = self._run(bar, 100.0, 1_000_000, foreign_net=-5000, eligible=True)
        hits = r["清單"][0]["命中條件"]
        self.assertIn("量價背離", hits)   # 量比 3.0 且收黑
        self.assertIn("籌碼轉弱", hits)   # 外資連兩日賣超

    def test_quiet_volume_does_not_hit(self) -> None:
        bar = self._quotes(close=99.0, open_=100.0, high=100.0, vol=900_000)
        r = self._run(bar, 100.0, 1_000_000, foreign_net=8000, eligible=True)
        self.assertEqual(r["清單"], [], "量能與籌碼都正常時不應進清單")

    def test_non_shortable_sorted_last(self) -> None:
        from premarket import shortlist as sl

        rows = [
            {"可當沖先賣後買": False, "命中數": 3, "量比": 9.0},
            {"可當沖先賣後買": True, "命中數": 1, "量比": 1.6},
        ]
        rows.sort(key=lambda x: (not x["可當沖先賣後買"], -x["命中數"], -(x["量比"] or 0)))
        self.assertTrue(rows[0]["可當沖先賣後買"], "不可先賣後買者必須排在後面")
