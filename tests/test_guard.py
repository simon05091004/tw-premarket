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

from premarket import analyze, main as main_mod  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
