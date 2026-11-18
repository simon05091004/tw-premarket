"""
外匯 Telegram 出錯通知的測試。

重點是兩件事：
  1. 不能漏 —— 頁面停止更新、某家銀行一直沒抓到、LINE 提醒沒送出，都要推得出去。
  2. 不能吵 —— 外匯 15 分鐘跑一次，同一段故障只能推一次，否則第三則之後就沒人看了。

workflow 和程式之間靠 step id、環境變數名稱、output 名稱、狀態檔名接起來，
任何一邊改名另一邊都不會報錯，只會默默不通知。最後一組測試專門守這個。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 測試會走到 emit_error／emit_warning，不該在 run 頁面上掛出假的 annotation。
os.environ.pop("GITHUB_ACTIONS", None)

from premarket import notify_fx as nf  # noqa: E402

NOW = datetime(2026, 11, 18, 10, 22, tzinfo=nf.TPE)
LATER = datetime(2026, 11, 18, 10, 37, tzinfo=nf.TPE)
NEXT_DAY = datetime(2026, 11, 19, 9, 7, tzinfo=nf.TPE)
EMPTY = {"down_since": None, "missing": {}}
ESUN = {"esun": "玉山銀行"}


def run_rounds(rounds, state=None):
    """依序跑好幾輪，回每一輪的訊息段落。"""
    state = state or dict(EMPTY)
    out = []
    for now, kwargs in rounds:
        parts, _, state = nf.decide(state, now, **kwargs)
        out.append(parts)
    return out, state


class TestHardFailure(unittest.TestCase):
    def test_all_green_is_not_a_failure(self):
        self.assertIsNone(nf.hard_failure("success", "success", "success", "success"))

    def test_timeout(self):
        self.assertIn("timeout", nf.hard_failure("cancelled", "success", "success", ""))

    def test_tests_failed(self):
        self.assertIn("測試沒過", nf.hard_failure("failure", "failure", "skipped", "skipped"))

    def test_setup_failed_before_tests(self):
        self.assertIn("準備執行環境失敗", nf.hard_failure("failure", "skipped", "skipped", "skipped"))

    def test_fetch_failed(self):
        self.assertIn("全都沒抓到", nf.hard_failure("failure", "success", "failure", "skipped"))

    def test_push_failed(self):
        self.assertIn("推回 repo 失敗", nf.hard_failure("failure", "success", "success", "failure"))

    def test_line_failure_alone_is_not_a_page_outage(self):
        # LINE 那步失敗會讓 job 變紅，但牌告照樣更新了。
        self.assertIsNone(nf.hard_failure("failure", "success", "success", "success"))


class TestParseMissing(unittest.TestCase):
    def test_no_output_means_not_fetched(self):
        self.assertIsNone(nf.parse_missing(""))

    def test_empty_object_means_everything_fetched(self):
        self.assertEqual(nf.parse_missing("{}"), {})

    def test_banks(self):
        self.assertEqual(nf.parse_missing('{"esun": "玉山銀行"}'), ESUN)

    def test_garbage_is_treated_as_not_fetched(self):
        self.assertIsNone(nf.parse_missing("esun"))
        self.assertIsNone(nf.parse_missing("[1, 2]"))


class TestOutage(unittest.TestCase):
    """整輪失敗：剛壞掉推一次，中間不推，恢復推一次。"""

    def test_first_failure_alerts_then_stays_quiet_then_recovers(self):
        out, state = run_rounds([
            (NOW, dict(hard="測試沒過", missing=None)),
            (LATER, dict(hard="測試沒過", missing=None)),
            (NEXT_DAY, dict(hard=None, missing={})),
        ])
        self.assertEqual(len(out[0]), 1)
        self.assertIn("外匯牌告停止更新", out[0][0])
        self.assertIn("測試沒過", out[0][0])
        self.assertIn("不會重複通知", out[0][0])
        self.assertEqual(out[1], [])
        self.assertEqual(len(out[2]), 1)
        self.assertIn("恢復更新", out[2][0])
        self.assertIn("11/18 10:22", out[2][0])   # 跨天要寫日期
        self.assertIsNone(state["down_since"])

    def test_test_alert_during_an_outage_does_not_claim_recovery(self):
        # 測試通知不抓牌告（missing 是 None），不能拿來宣告「已正常更新」。
        state = {"down_since": "2026-11-18 10:22", "missing": {}}
        parts, _, new = nf.decide(state, LATER, hard=None, missing=None, test=True)
        self.assertFalse(any("恢復" in p for p in parts))
        self.assertEqual(new["down_since"], "2026-11-18 10:22")

    def test_same_day_recovery_shows_only_the_time(self):
        state = {"down_since": "2026-11-18 10:22", "missing": {}}
        parts, bad, _ = nf.decide(state, LATER, hard=None, missing={})
        self.assertIn("從 10:22 起中斷", parts[0])
        self.assertFalse(bad)

    def test_outage_keeps_the_missing_bank_count(self):
        state = {"down_since": None, "missing": {"esun": {"name": "玉山銀行", "since": "x", "count": 1, "alerted": False}}}
        _, _, new = nf.decide(state, NOW, hard="推回 repo 失敗", missing=None)
        self.assertEqual(new["missing"]["esun"]["count"], 1)


class TestMissingBank(unittest.TestCase):
    """某一家沒抓到：連續兩輪才推，之後不再推，恢復推一次。"""

    def test_single_blip_is_silent_both_ways(self):
        out, state = run_rounds([
            (NOW, dict(hard=None, missing=ESUN)),
            (LATER, dict(hard=None, missing={})),
        ])
        self.assertEqual(out, [[], []])
        self.assertEqual(state["missing"], {})

    def test_streak_alerts_once_and_recovers_once(self):
        out, state = run_rounds([
            (NOW, dict(hard=None, missing=ESUN)),
            (LATER, dict(hard=None, missing=ESUN)),
            (LATER, dict(hard=None, missing=ESUN)),
            (NEXT_DAY, dict(hard=None, missing={})),
        ])
        self.assertEqual(out[0], [])
        self.assertEqual(len(out[1]), 1)
        self.assertIn("玉山銀行的牌告連續 2 輪沒抓到", out[1][0])
        self.assertIn("從 10:22 起", out[1][0])
        self.assertIn("README 第十一節", out[1][0])
        self.assertEqual(out[2], [])
        self.assertEqual(len(out[3]), 1)
        self.assertIn("玉山銀行的牌告恢復了", out[3][0])
        self.assertEqual(state["missing"], {})

    def test_banks_are_tracked_separately(self):
        both = {"esun": "玉山銀行", "taishin": "台新銀行"}
        out, state = run_rounds([
            (NOW, dict(hard=None, missing=ESUN)),
            (LATER, dict(hard=None, missing=both)),
        ])
        self.assertEqual(len(out[1]), 1)
        self.assertIn("玉山銀行", out[1][0])
        self.assertEqual(state["missing"]["taishin"]["count"], 1)

    def test_not_fetched_round_does_not_reset_the_count(self):
        out, state = run_rounds([
            (NOW, dict(hard=None, missing=ESUN)),
            (LATER, dict(hard=None, missing=None, test=True)),
        ])
        self.assertEqual(state["missing"]["esun"]["count"], 1)


class TestLineFailure(unittest.TestCase):
    def test_forwards_the_alert_that_never_arrived(self):
        parts, bad, _ = nf.decide(
            dict(EMPTY), NOW, hard=None, missing={}, line_failed=True,
            alert_text="外匯提醒 11/18 10:22\n・跨行 <+3bp> & 機會",
        )
        self.assertTrue(bad)
        self.assertIn("外匯提醒沒送到 LINE", parts[0])
        self.assertIn("・跨行 &lt;+3bp&gt; &amp; 機會", parts[0])   # HTML 要跳脫

    def test_is_sent_every_time_not_deduplicated(self):
        out, _ = run_rounds([
            (NOW, dict(hard=None, missing={}, line_failed=True, alert_text="a")),
            (LATER, dict(hard=None, missing={}, line_failed=True, alert_text="b")),
        ])
        self.assertEqual([len(p) for p in out], [1, 1])


class TestTestAlert(unittest.TestCase):
    def test_test_alert_is_marked_and_not_counted_as_an_error(self):
        parts, bad, new = nf.decide(dict(EMPTY), NOW, hard=None, missing=None, test=True)
        self.assertEqual(len(parts), 1)
        self.assertIn("【測試】", parts[0])
        self.assertFalse(bad)
        self.assertEqual(new, EMPTY)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.out = Path(self.tmp.name) / "out.txt"
        self.out.write_text("")
        self.env = {
            "TELEGRAM_BOT_TOKEN": "123456:AAFAKEtokenNOTreal",
            "TELEGRAM_CHAT_ID": "87654321",
            "FX_NOTIFY_STATE": str(self.state),
            "GITHUB_OUTPUT": str(self.out),
            "RUN_URL": "https://github.com/x/y/actions/runs/1",
            "PAGES_URL": "https://x.github.io/y/",
            "JOB_STATUS": "success", "TESTS": "success", "FX": "success",
            "PUSH": "success", "LINE": "skipped", "MISSING": "{}",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, send_result=(200, '{"ok":true}'), **env):
        sent = []

        def fake_send(text, **kw):
            sent.append((text, kw))
            return send_result

        buf = io.StringIO()
        with patch.dict(os.environ, {**self.env, **env}, clear=False), \
                patch.object(nf, "send", fake_send), contextlib.redirect_stdout(buf):
            code = nf.main([], now=NOW)
        return code, sent, buf.getvalue()

    def test_missing_secret_skips_quietly(self):
        code, sent, out = self._run(TELEGRAM_BOT_TOKEN="")
        self.assertEqual((code, sent), (0, []))
        self.assertIn("略過", out)
        self.assertFalse(self.state.exists())

    def test_normal_round_sends_and_writes_nothing(self):
        code, sent, _ = self._run()
        self.assertEqual((code, sent), (0, []))
        self.assertFalse(self.state.exists())
        self.assertEqual(self.out.read_text(), "")

    def test_outage_is_sent_with_a_link_to_the_run_and_remembered(self):
        code, sent, _ = self._run(JOB_STATUS="failure", FX="failure", PUSH="skipped", MISSING="")
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        text, kw = sent[0]
        self.assertTrue(text.startswith("💱 <b>外匯牌告追蹤</b> 11/18 10:22"))
        self.assertEqual(kw["button"], ("看執行紀錄", "https://github.com/x/y/actions/runs/1"))
        self.assertEqual(json.loads(self.state.read_text())["down_since"], "2026-11-18 10:22")
        self.assertIn("state_changed=true", self.out.read_text())

        # 下一輪還是壞的：不再推。
        code, sent, _ = self._run(JOB_STATUS="failure", FX="failure", PUSH="skipped", MISSING="")
        self.assertEqual(sent, [])

    def test_recovery_links_to_the_page(self):
        self.state.write_text(json.dumps({"down_since": "2026-11-18 09:07", "missing": {}}))
        code, sent, _ = self._run()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["button"], ("看外匯頁面", "https://x.github.io/y/fx.html"))
        self.assertIsNone(json.loads(self.state.read_text())["down_since"])

    def test_bad_chat_id_fails_loudly_and_forgets_nothing(self):
        # 沒送出去就不能記成「已通知」，否則修好設定之後這段故障永遠不會再推。
        code, sent, out = self._run(
            send_result=(400, '{"ok":false,"description":"Bad Request: chat not found"}'),
            JOB_STATUS="failure", FX="failure", MISSING="",
        )
        self.assertEqual(code, 1)
        self.assertIn("chat not found", out)
        self.assertFalse(self.state.exists())

    def test_telegram_outage_retries_next_round_without_turning_red(self):
        code, _, out = self._run(send_result=(502, "Bad Gateway"), JOB_STATUS="failure", FX="failure", MISSING="")
        self.assertEqual(code, 0)
        self.assertIn("下一輪會再試", out)
        self.assertFalse(self.state.exists())

    def test_token_never_appears_in_output(self):
        _, _, out = self._run(send_result=(401, '{"ok":false,"description":"Unauthorized"}'),
                              JOB_STATUS="failure", FX="failure", MISSING="")
        self.assertNotIn("AAFAKEtokenNOTreal", out)

    def test_corrupt_state_file_starts_fresh(self):
        self.state.write_text("{not json")
        code, sent, _ = self._run(JOB_STATUS="failure", FX="failure", MISSING="")
        self.assertEqual((code, len(sent)), (0, 1))


class TestWiring(unittest.TestCase):
    """workflow ↔ 程式的接點。名稱對不上不會報錯，只會默默不通知。"""

    @classmethod
    def setUpClass(cls):
        cls.yml = (ROOT / ".github/workflows/fx.yml").read_text(encoding="utf-8")

    def test_step_ids_the_notifier_reads(self):
        for sid in ("tests", "fx", "push", "line", "notify"):
            self.assertIn(f"id: {sid}\n", self.yml, f"fx.yml 少了 id: {sid}")

    def test_env_names_match(self):
        for name, expr in {
            "JOB_STATUS": "job.status",
            "TESTS": "steps.tests.outcome",
            "FX": "steps.fx.outcome",
            "PUSH": "steps.push.outcome",
            "LINE": "steps.line.outcome",
            "MISSING": "steps.fx.outputs.missing_sources",
            "ALERT_TEXT": "steps.fx.outputs.alert_text",
            "TEST_ALERT": "inputs.test_alert",
        }.items():
            self.assertIn(f"{name}: ${{{{ {expr} }}}}", self.yml)

    def test_fx_writes_the_output_the_workflow_reads(self):
        from premarket import fx

        out = Path(tempfile.mkstemp()[1])
        try:
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}), \
                    patch.object(fx, "load_config", lambda: {"sources": ["esun", "taishin"]}), \
                    patch.dict(fx.FETCHERS, {"esun": lambda: None, "taishin": lambda: None}), \
                    patch.object(fx, "fetch_market", lambda: {"USDTWD": {"mid": 32.0}}), \
                    patch.object(fx, "append_history", lambda *a, **k: []), \
                    patch.object(fx, "derive", lambda *a, **k: {"quotes": [], "cycles": []}), \
                    patch.object(fx, "check_alerts", lambda *a, **k: []), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertLogs(level="INFO"):
                self.assertEqual(fx.run(dry_run=True), 0)
            lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.startswith("missing_sources=")]
        finally:
            out.unlink()
        self.assertEqual(len(lines), 1)
        # 只列銀行，不列國際報價代號；名稱要能直接放進訊息。
        self.assertEqual(
            nf.parse_missing(lines[0].split("=", 1)[1]),
            {"esun": "玉山銀行", "taishin": "台新銀行"},
        )

    def test_state_file_is_cached_and_ignored(self):
        name = nf.STATE_FILE.name
        self.assertGreaterEqual(self.yml.count(f"path: {name}"), 2)   # restore 與 save
        self.assertIn(name, (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_notifier_needs_only_the_standard_library(self):
        # 環境裝不起來那輪也要送得出去：那時只有系統的 python3。
        code = (
            "import sys, premarket.notify_fx; "
            "print([m for m in ('requests','pandas','yfinance','anthropic') if m in sys.modules])"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "[]", r.stderr)


if __name__ == "__main__":
    unittest.main()
