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

import sys
import tempfile
import unittest
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
        self.assertEqual(str(self._at("2026-08-11T04:59")), "2026-08-10")
        self.assertEqual(str(self._at("2026-08-11T05:00")), "2026-08-11")

    def test_premarket_never_rolls_back(self) -> None:
        # 盤前本來就在清晨執行，回推會直接寫錯日期
        self.assertEqual(str(self._at("2026-08-11T06:45", "premarket")), "2026-08-11")


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

    def test_normal_run_does_not_signal_skip(self) -> None:
        out = self.tmpdir / "gh_output"
        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}):
            self._run(report_on_disk=False)
        self.assertFalse(out.exists(), "有產出報告時不得寫 skipped，不然通知會被誤擋")


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
