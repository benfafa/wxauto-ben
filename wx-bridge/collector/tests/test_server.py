import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from concurrent.futures import Future
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.backends.fake_backend import FakeBackend  # noqa: E402
from collector.server import CollectorService, StickerJob, TtsJob  # noqa: E402
from collector.tts import normalize_tts  # noqa: E402
from collector.stickers import StickerCatalog  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef"


class CollectorServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name) / "stickers"
        directory.mkdir()
        self.image = directory / "happy.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.send_log = Path(self.temp.name) / "sent.jsonl"
        stickers = StickerCatalog({"enabled": True, "items": [
            {"id": "happy", "file": "happy.png", "keywords": ["开心"]}
        ]}, base_dir=self.temp.name)
        self.backend = FakeBackend(["测试群"], send_log=self.send_log)
        self.service = CollectorService(
            self.backend,
            TOKEN,
            allowed_groups=["测试群"],
            poll_ms=100,
            port=0,
            allow_inject=True,
            stickers=stickers,
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

    def test_sticker_and_text_use_the_same_ui_worker_and_audit_ids(self):
        threads = []
        send = self.backend.send_sticker

        def record_thread(*args):
            threads.append(threading.current_thread().name)
            return send(*args)

        with patch.object(self.backend, "send_sticker", side_effect=record_thread):
            self.assertEqual(self.http("POST", "/send", {"group": "测试群", "text": "你好"})[0], 200)
            status, payload = self.http("POST", "/send-sticker", {"group": "测试群", "stickerId": "happy"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload, {"ok": True, "stickerId": "happy"})
        self.assertEqual(threads, ["wxauto-ui"])
        sent = self.backend.sent()
        self.assertEqual(sent[0]["text"], "你好")
        self.assertEqual(sent[1]["stickerId"], "happy")
        self.assertEqual(sent[1]["file"], str(self.image.resolve()))
        records = [json.loads(line) for line in self.send_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[1]["kind"], "sticker")
        self.assertEqual(records[1]["stickerId"], "happy")
        self.assertEqual(self.service.health()["sendCount"], 2)

    def test_sticker_http_rejects_auth_targets_ids_paths_and_disabled(self):
        body = {"group": "测试群", "stickerId": "happy"}
        self.assertEqual(self.http("POST", "/send-sticker", body, token=None)[0], 401)
        self.assertEqual(self.http("POST", "/send-sticker", {**body, "group": "其他群"})[0], 403)
        self.assertEqual(self.http("POST", "/send-sticker", {**body, "stickerId": "unknown"})[0], 404)
        for invalid in [{**body, "file": str(self.image)}, {**body, "path": str(self.image)},
                        {"group": "测试群"}, {**body, "stickerId": "../happy.png"},
                        {**body, "stickerId": str(self.image)}, {**body, "stickerId": []}]:
            self.assertEqual(self.http("POST", "/send-sticker", invalid)[0], 400)
        self.service.stickers = StickerCatalog()
        status, payload = self.http("POST", "/send-sticker", body)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "stickers-disabled")
        self.assertEqual(self.backend.sent(), [])
        self.assertEqual(self.http("POST", "/send", {"group": "测试群", "text": "仍可发文字"})[0], 200)

    def test_cancelled_sticker_job_never_reaches_backend(self):
        future = Future()
        future.cancel()
        self.service._handle_send(StickerJob(group="测试群", sticker_id="happy", future=future))
        self.assertEqual(self.backend.sent(), [])
        self.assertTrue(future.cancelled())

    def test_explicit_text_failure_remains_an_http_error(self):
        for result in [False, {"ok": False}]:
            with patch.object(self.backend, "send", return_value=result):
                status, payload = self.http("POST", "/send", {"group": "测试群", "text": "失败"})
            self.assertEqual(status, 500)
            self.assertFalse(payload["ok"])
        self.assertEqual(self.service.health()["sendCount"], 0)

    def test_sticker_is_revalidated_after_enqueue_before_backend_send(self):
        put = self.service._jobs.put

        def replace_after_validation(job):
            if isinstance(job, StickerJob):
                self.image.write_bytes(b"invalid replacement")
            put(job)

        with patch.object(self.service._jobs, "put", side_effect=replace_after_validation):
            status, payload = self.http("POST", "/send-sticker", {"group": "测试群", "stickerId": "happy"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "invalid-sticker-file")
        self.assertEqual(self.backend.sent(), [])
        self.assertEqual(self.service.health()["sendCount"], 0)

    def test_sticker_failures_are_http_errors_not_success_and_worker_recovers(self):
        for result in [False, {"ok": False}, None]:
            with patch.object(self.backend, "send_sticker", return_value=result):
                status, payload = self.http("POST", "/send-sticker", {"group": "测试群", "stickerId": "happy"})
            self.assertEqual(status, 500, payload)
            self.assertFalse(payload["ok"])
        with patch.object(self.backend, "send_sticker", side_effect=RuntimeError("UI failed")):
            self.assertEqual(self.http("POST", "/send-sticker", {"group": "测试群", "stickerId": "happy"})[0], 500)
        self.assertEqual(self.service.health()["sendCount"], 0)
        self.assertEqual(self.http("POST", "/send", {"group": "测试群", "text": "之后的文字"})[0], 200)

    def test_invalid_sticker_file_is_rejected_over_http(self):
        self.image.unlink()
        status, payload = self.http("POST", "/send-sticker", {"group": "测试群", "stickerId": "happy"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "invalid-sticker-file")
        self.assertEqual(self.backend.sent(), [])

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


class TtsJobRoutingTest(unittest.TestCase):
    """TTS 走原生气泡还是文件，取决于配置和后端能力。"""

    def _run(self, *, native, supports_voice):
        called = []

        class Backend:
            def send_voice(self, group, path):
                called.append(("voice", group))
                return {"ok": True}

            def send_file(self, group, path, what="文件"):
                called.append(("file", group))
                return {"ok": True}

            def send(self, group, text, at=None):
                return {"ok": True}

        backend = Backend()
        backend.supports_voice = supports_voice
        service = CollectorService(
            backend, "0" * 32, allowed_groups=["测试群"],
            tts=normalize_tts({"enabled": True, "nativeVoice": native}))
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        path = folder / ("reply.wav" if native else "reply.mp3")
        path.write_bytes(b"x")
        job = TtsJob(group="测试群", path=path, folder=folder, size=1, future=Future())
        service._handle_send(job)
        return called, job.future

    def test_native_voice_needs_backend_support(self):
        self.assertEqual(self._run(native=True, supports_voice=True)[0], [("voice", "测试群")])
        self.assertEqual(self._run(native=True, supports_voice=False)[0], [("file", "测试群")])
        self.assertEqual(self._run(native=False, supports_voice=True)[0], [("file", "测试群")])

    def test_native_voice_reports_native_flag(self):
        called, future = self._run(native=True, supports_voice=True)
        self.assertTrue(future.result()["native"])
