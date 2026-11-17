"""tools/line_notify.py：額度預警、額度用完時讓 run 失敗、其他錯誤的分類。

    python -m unittest tests.test_line_notify

不連網：用一個假的 session 依網址回應。回應內容照 LINE Messaging API 文件的格式。
"""

import contextlib
import io
import unittest

from tools import line_notify as ln


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, text: str | None = None):
        self.status_code = status
        self._body = body or {}
        self.text = text if text is not None else str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, quota=None, consumption=None, broadcast=None, quota_error=False):
        self.headers = {}
        self.quota = quota if quota is not None else FakeResponse(200, {"type": "limited", "value": 200})
        self.consumption = consumption if consumption is not None else FakeResponse(200, {"totalUsage": 42})
        self.broadcast = broadcast if broadcast is not None else FakeResponse(200, {})
        self.quota_error = quota_error
        self.sent: list[str] = []

    def get(self, url, timeout=None):
        if self.quota_error:
            raise ConnectionError("boom")
        return self.consumption if url.endswith("/consumption") else self.quota

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/message/broadcast"), url
        self.sent.append(json["messages"][0]["text"])
        if isinstance(self.broadcast, Exception):
            raise self.broadcast
        return self.broadcast


def run(session):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ln.send("token", "台股盤後籌碼 11/17 已更新", session)
    return code, out.getvalue()


class QuotaNoteTest(unittest.TestCase):
    def test_plenty_left_means_no_note(self):
        s = FakeSession()
        code, log = run(s)
        self.assertEqual(code, 0)
        self.assertEqual(s.sent, ["台股盤後籌碼 11/17 已更新"])
        self.assertIn("已用 42／200 則", log)
        self.assertEqual(s.headers["Authorization"], "Bearer token")

    def test_note_appears_when_under_twenty_percent(self):
        s = FakeSession(consumption=FakeResponse(200, {"totalUsage": 165}))
        self.assertEqual(run(s)[0], 0)
        self.assertIn("⚠️ LINE 本月免費額度剩 35／200 則", s.sent[0])
        self.assertTrue(s.sent[0].startswith("台股盤後籌碼 11/17 已更新\n\n"))

    def test_exactly_twenty_percent_left_already_warns(self):
        s = FakeSession(consumption=FakeResponse(200, {"totalUsage": 160}))
        run(s)
        self.assertIn("剩 40／200 則", s.sent[0])

    def test_unlimited_plan_never_warns(self):
        s = FakeSession(quota=FakeResponse(200, {"type": "none"}))
        self.assertEqual(run(s)[0], 0)
        self.assertNotIn("⚠️", s.sent[0])

    def test_quota_lookup_failure_does_not_block_the_message(self):
        s = FakeSession(quota_error=True)
        code, log = run(s)
        self.assertEqual(code, 0)
        self.assertEqual(len(s.sent), 1)
        self.assertIn("查不到 LINE 額度", log)


class ErrorTest(unittest.TestCase):
    def test_monthly_limit_fails_the_run(self):
        s = FakeSession(consumption=FakeResponse(200, {"totalUsage": 200}),
                        broadcast=FakeResponse(429, {"message": "You have reached your monthly limit."}))
        code, log = run(s)
        self.assertEqual(code, 1)
        self.assertIn("::error::LINE 本月訊息額度已用完（上限 200 則）", log)

    def test_plain_rate_limit_is_only_a_warning(self):
        s = FakeSession(broadcast=FakeResponse(429, {"message": "The API rate limit has been exceeded. Try again later."}))
        code, log = run(s)
        self.assertEqual(code, 0)
        self.assertIn("::warning::", log)
        self.assertNotIn("::error::", log)

    def test_bad_token_fails_the_run(self):
        for status in (401, 403):
            code, log = run(FakeSession(broadcast=FakeResponse(status, {"message": "Authentication failed"})))
            self.assertEqual(code, 1, status)
            self.assertIn("::error::LINE 權杖無效", log)

    def test_server_error_is_only_a_warning(self):
        code, log = run(FakeSession(broadcast=FakeResponse(500, {"message": "oops"})))
        self.assertEqual(code, 0)
        self.assertIn("::warning::LINE 廣播失敗（HTTP 500）", log)

    def test_connection_error_is_only_a_warning(self):
        import requests
        code, log = run(FakeSession(broadcast=requests.ConnectionError("reset")))
        self.assertEqual(code, 0)
        self.assertIn("::warning::LINE 廣播連線失敗", log)


class MainTest(unittest.TestCase):
    def test_missing_token_or_message_is_skipped_not_failed(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in ("LINE_TOKEN", "LINE_MESSAGE")}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ln.main(), 0)
                os.environ["LINE_TOKEN"] = "t"
                os.environ["LINE_MESSAGE"] = "   "
                self.assertEqual(ln.main(), 0)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
