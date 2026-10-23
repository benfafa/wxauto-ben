import json
import sys
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.backends.fake_backend import FakeBackend  # noqa: E402
from collector.server import CollectorService  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef"


class CollectorServerTest(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend(["测试群"])
        self.service = CollectorService(
            self.backend,
            TOKEN,
            allowed_groups=["测试群"],
            poll_ms=100,
            port=0,
            allow_inject=True,
        ).start()
        self.base = f"http://127.0.0.1:{self.service.port}"

    def tearDown(self):
        self.service.stop()

    def http(self, method, path, body=None, token=TOKEN):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base + path, data=data, method=method)
        if token is not None:
            request.add_header("authorization", f"Bearer {token}")
        if data is not None:
            request.add_header("content-type", "application/json")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def wait_for_event(self, since=0):
        deadline = time.time() + 5
        while time.time() < deadline:
            status, payload = self.http("GET", f"/poll?since={since}&waitMs=500")
            self.assertEqual(status, 200)
            if payload["events"]:
                return payload
            time.sleep(0.05)
        self.fail("no event arrived within 5s")

    def test_health_and_auth(self):
        status, payload = self.http("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "fake")
        self.assertEqual(payload["selfNickname"], "FakeBot")
        self.assertEqual(payload["groups"], ["测试群"])

        status, payload = self.http("GET", "/health", token=None)
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

        status, payload = self.http("GET", "/health", token="wrong-token")
        self.assertEqual(status, 401)

    def test_inject_is_picked_up_and_cursor_advances(self):
        before = self.http("GET", "/cursor")[1]
        status, payload = self.http("POST", "/inject", {
            "group": "测试群",
            "sender": "张三",
            "text": "@小助手 在吗",
            "id": "msg-1",
        })
        self.assertEqual(status, 200, payload)

        polled = self.wait_for_event(since=before["cursor"])
        self.assertEqual(len(polled["events"]), 1)
        event = polled["events"][0]
        self.assertEqual(event["group"], "测试群")
        self.assertEqual(event["sender"], "张三")
        self.assertEqual(event["text"], "@小助手 在吗")
        self.assertEqual(event["id"], "msg-1")
        self.assertGreater(event["seq"], before["cursor"])

        status, again = self.http("GET", f"/poll?since={polled['cursor']}&waitMs=200")
        self.assertEqual(status, 200)
        self.assertEqual(again["events"], [])
        self.assertEqual(again["cursor"], polled["cursor"])

    def test_send_goes_through_the_ui_worker(self):
        status, payload = self.http("POST", "/send", {
            "group": "测试群",
            "text": "你好",
            "at": "张三",
        })
        self.assertEqual(status, 200, payload)
        sent = self.backend.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["group"], "测试群")
        self.assertEqual(sent[0]["text"], "你好")
        self.assertEqual(sent[0]["at"], "张三")

    def test_send_rejects_bad_targets_and_payloads(self):
        status, _ = self.http("POST", "/send", {"group": "别的群", "text": "hi"})
        self.assertEqual(status, 403)
        status, _ = self.http("POST", "/send", {"group": "测试群", "text": "   "})
        self.assertEqual(status, 400)
        status, _ = self.http("POST", "/send", {"group": "测试群", "text": "x" * 4001})
        self.assertEqual(status, 400)

    def test_inject_rejects_non_whitelisted_group(self):
        status, _ = self.http("POST", "/inject", {
            "group": "别的群",
            "sender": "张三",
            "text": "@小助手 在吗",
        })
        self.assertEqual(status, 403)

    def test_long_poll_waits_when_empty(self):
        started = time.monotonic()
        status, payload = self.http("GET", "/poll?since=0&waitMs=600")
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertEqual(payload["events"], [])
        self.assertGreaterEqual(elapsed, 0.5)


class InjectDisabledTest(unittest.TestCase):
    def test_inject_is_404_when_disabled(self):
        backend = FakeBackend(["测试群"])
        service = CollectorService(backend, TOKEN, allowed_groups=["测试群"], port=0).start()
        try:
            request = Request(
                f"http://127.0.0.1:{service.port}/inject",
                data=json.dumps({"group": "测试群", "text": "x"}).encode(),
                method="POST",
            )
            request.add_header("authorization", f"Bearer {TOKEN}")
            request.add_header("content-type", "application/json")
            try:
                urlopen(request, timeout=5)
                self.fail("expected 404")
            except HTTPError as error:
                try:
                    self.assertEqual(error.code, 404)
                finally:
                    error.close()
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
