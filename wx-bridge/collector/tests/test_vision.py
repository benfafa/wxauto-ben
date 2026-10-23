import base64
import io
import json
import stat
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from collector.vision import ImageCache, normalize_vision, vision_from_file, check_path, MAX_REFERENCES
from collector.backends.fake_backend import FakeBackend, FakeImageMessage
from collector.backends.wxauto_backend import WxAutoBackend
from collector.server import CollectorService


class ImageCacheTest(unittest.TestCase):
    def setUp(self):
        self.cache = ImageCache({"enabled": True})
        self.addCleanup(self.cache.close)

    def fetch(self, message):
        image_id = self.cache.register("A", message)
        return self.cache.get("A", image_id)

    def test_config_defaults_and_invalid_boundaries(self):
        self.assertEqual(normalize_vision(), {"enabled": False, "contextWindowMs": 120000, "maxImages": 2, "maxBytes": 5242880})
        for config in [[], False, {"enabled": 1}, {"maxImages": "2"}, {"maxImages": True},
                       {"maxImages": 0}, {"maxImages": 5}, {"maxImages": 1.5}, {"maxBytes": 1023},
                       {"maxBytes": 5242881}, {"contextWindowMs": 999}, {"contextWindowMs": 600001}, {"url": "http://example.com"}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                normalize_vision(config)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "config.json"
            file.write_text(json.dumps({"vision": None}, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                vision_from_file(file)

    def test_disabled_has_no_references_downloads_or_cache_directory(self):
        cache = ImageCache()
        self.assertIsNone(cache.register("A", FakeImageMessage()))
        self.assertEqual(len(cache.references), 0)
        self.assertIsNone(cache.root)
        with self.assertRaises(ValueError):
            cache.get("A", "a" * 32)

    def test_random_ids_group_isolation_ttl_and_count_bound(self):
        image_id = self.cache.register("A", FakeImageMessage())
        self.assertRegex(image_id, r"^[a-f0-9]{32}$")
        with self.assertRaises(ValueError):
            self.cache.get("B", image_id)
        for _ in range(MAX_REFERENCES + 1):
            self.cache.register("A", FakeImageMessage())
        self.assertEqual(len(self.cache.references), MAX_REFERENCES)
        self.assertNotIn(image_id, self.cache.references)
        with patch("collector.vision.time.monotonic", return_value=time.monotonic() + 121):
            self.cache.prune()
        self.assertFalse(self.cache.references)

    def test_on_demand_path_and_str_results_reencoded_and_immediately_cleaned(self):
        for as_string in [False, True]:
            calls = []
            def download(dir_path=None):
                calls.append(dir_path)
                file = Path(dir_path) / "input.png"
                Image.new("RGBA", (2000, 10), (10, 20, 30, 200)).save(file)
                return str(file) if as_string else file
            image_id = self.cache.register("A", types.SimpleNamespace(download=download))
            self.assertEqual(calls, [])
            output = self.cache.get("A", image_id)
            self.assertEqual(set(output), {"mediaType", "data", "name"})
            self.assertEqual(output["mediaType"], "image/png")
            decoded = Image.open(io.BytesIO(base64.b64decode(output["data"])))
            self.assertLessEqual(max(decoded.size), 1600)
            self.assertEqual(decoded.info, {})
            self.assertFalse(Path(calls[0]).exists())
            self.assertEqual(list(self.cache.root.iterdir()), [])

    def test_download_failures_invalid_returns_and_legacy_are_rejected(self):
        for value in [None, True, {}, "https://example.com/image.png", "relative.png"]:
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                self.fetch(types.SimpleNamespace(download=lambda dir_path=None: value))
        with self.assertRaises(ValueError):
            self.fetch(object())
        with self.assertRaises(RuntimeError):
            self.fetch(types.SimpleNamespace(download=lambda dir_path=None: (_ for _ in ()).throw(RuntimeError("failed"))))

    def test_arbitrary_file_is_never_read_or_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            outside = Path(folder) / "secret.png"
            outside.write_bytes(b"secret")
            with self.assertRaises(ValueError):
                self.fetch(types.SimpleNamespace(download=lambda dir_path=None: outside))
            self.assertEqual(outside.read_bytes(), b"secret")

    def test_fake_magic_oversize_missing_and_nonregular_files(self):
        for mode in ["fake", "truncated", "large", "missing", "directory"]:
            def download(dir_path=None):
                file = Path(dir_path) / "input.png"
                if mode == "directory":
                    return Path(dir_path)
                if mode != "missing":
                    data = b"not png" if mode == "fake" else b"\x89PNG\r\n\x1a\n"
                    if mode == "large":
                        data += b"x" * 5242880
                    file.write_bytes(data)
                return file
            with self.subTest(mode=mode), self.assertRaises((ValueError, OSError)):
                self.fetch(types.SimpleNamespace(download=download))
            self.assertEqual(list(self.cache.root.iterdir()), [])

    def test_all_verified_formats_decode_and_output_has_no_metadata(self):
        for fmt in ["PNG", "JPEG", "GIF", "WEBP"]:
            with self.subTest(fmt=fmt):
                data = io.BytesIO()
                Image.new("RGB", (20, 20), (40, 60, 80)).save(data, format=fmt)
                result = self.cache._decode(data.getvalue())
                self.assertIn(result["mediaType"], ["image/png", "image/jpeg"])
                with Image.open(io.BytesIO(base64.b64decode(result["data"]))) as image:
                    self.assertEqual(image.size, (20, 20))
                    self.assertNotIn("exif", image.info)

    def test_nested_failed_download_is_cleaned_and_cleanup_failure_retries_on_poll(self):
        def download(dir_path=None):
            nested = Path(dir_path) / "nested"
            nested.mkdir()
            file = nested / "input.png"
            file.write_bytes(b"not an image")
            return file
        with self.assertRaises(ValueError):
            self.fetch(types.SimpleNamespace(download=download))
        self.assertEqual(list(self.cache.root.iterdir()), [])
        with patch.object(self.cache, "_clean_request", side_effect=PermissionError("temporarily locked")):
            self.fetch(FakeImageMessage())
        self.assertEqual(len(self.cache._owned_requests), 1)
        self.cache.prune()
        self.assertFalse(self.cache._owned_requests)
        self.assertEqual(list(self.cache.root.iterdir()), [])

    def test_hard_links_are_rejected_without_reading_external_files(self):
        original = check_path
        def checked(path, *, directory=False):
            info = original(path, directory=directory)
            if directory:
                return info
            return types.SimpleNamespace(st_nlink=2)
        with patch("collector.vision.check_path", side_effect=checked):
            with self.assertRaisesRegex(ValueError, "hard links"):
                self.fetch(FakeImageMessage())

    def test_pixel_bomb_and_max_side_checked_before_load(self):
        for size in [(8193, 1), (5000, 4000)]:
            original = types.SimpleNamespace(format="PNG", size=size)
            class Opened:
                def __enter__(self):
                    return original
                def __exit__(self, *_):
                    pass
            with patch("PIL.Image.open", return_value=Opened()):
                with self.assertRaisesRegex(ValueError, "pixel limit"):
                    self.cache._decode(b"\x89PNG\r\n\x1a\n")

    def test_symlink_and_windows_reparse_rejected_without_creating_links(self):
        # This test never asks Windows for symlink privileges.
        for info in [types.SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0),
                     types.SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)]:
            with patch.object(Path, "lstat", return_value=info):
                with self.assertRaisesRegex(ValueError, "link/reparse"):
                    check_path(Path(tempfile.gettempdir()) / "untrusted.png")

    def test_wxauto_conversion_registers_only_enabled_v4_objects_without_download(self):
        # No WeChat constructor or module import is involved.
        backend = object.__new__(WxAutoBackend)
        message = types.SimpleNamespace(type="image", content="[图片]", sender="a", id="1",
                                        download=lambda **_: self.fail("poll must not download"))
        for enabled, mode in [(False, "wxauto4"), (True, "legacy"), (True, "wxauto4")]:
            backend.vision_enabled, backend.mode = enabled, mode
            event = backend._convert("A", message)
            self.assertEqual(event["kind"], "image")
            self.assertEqual("_imageMessage" in event, enabled and mode == "wxauto4")
            if "_imageMessage" in event:
                self.assertIs(event["_imageMessage"], message)


class ImageHttpTest(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend(["A", "B"])
        self.service = CollectorService(self.backend, "test-token", allowed_groups=["A", "B"],
                                        poll_ms=100, vision={"enabled": True}).start()
        self.addCleanup(self.service.stop)
        self.base = f"http://127.0.0.1:{self.service.port}"

    def http(self, method, path, body=None, token="test-token"):
        request = Request(self.base + path, method=method,
                          data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        if token:
            request.add_header("authorization", "Bearer " + token)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def event(self, message=None):
        cursor = self.service.cursor()["cursor"]
        self.backend.inject({"group": "A", "sender": "user", "text": "[图片]", "kind": "image",
                             **({"_imageMessage": message} if message else {})})
        result = self.service.poll(cursor, 2000)
        self.assertEqual(len(result["events"]), 1)
        return result["events"][0]

    def test_authenticated_post_only_metadata_poll_and_serial_ui_download(self):
        calls = []
        def download(dir_path=None):
            calls.append(threading.current_thread().name)
            return FakeImageMessage().download(dir_path)
        event = self.event(types.SimpleNamespace(download=download))
        self.assertEqual(calls, [])
        self.assertNotIn("_imageMessage", event)
        self.assertNotIn("data", event)
        body = {"group": "A", "imageId": event["imageId"]}
        for token in [None, "wrong"]:
            self.assertEqual(self.http("POST", "/image", body, token=token)[0], 401)
        self.assertEqual(self.http("GET", "/image")[0], 404)
        self.assertEqual(self.http("POST", "/image", {**body, "group": "outside"})[0], 403)
        self.assertEqual(self.http("POST", "/image", {**body, "group": "B"})[0], 400)
        for invalid in [{**body, "path": "C:/secret.png"}, {**body, "imageId": "../secret"},
                        {**body, "imageId": "https://example.com"}, {**body, "imageId": []}]:
            self.assertEqual(self.http("POST", "/image", invalid)[0], 400)
        self.assertEqual(calls, [])
        status, image = self.http("POST", "/image", body)
        self.assertEqual(status, 200, image)
        self.assertEqual(image["mediaType"], "image/jpeg")
        self.assertEqual(calls, ["wxauto-ui"])
        self.assertEqual(self.service.health()["sendCount"], 0)
        self.assertFalse(self.backend.sent())
        self.assertEqual(list(self.service.images.root.iterdir()), [])

    def test_download_error_is_redacted_and_worker_keeps_text_path(self):
        def download(dir_path=None):
            raise RuntimeError("SECRET_BASE64 /private/path")
        event = self.event(types.SimpleNamespace(download=download))
        with self.assertLogs(level="WARNING") as logs:
            status, payload = self.http("POST", "/image", {"group": "A", "imageId": event["imageId"]})
        self.assertEqual(status, 400)
        self.assertNotIn("SECRET", json.dumps(payload) + str(logs.output))
        self.assertEqual(self.http("POST", "/send", {"group": "A", "text": "still works"})[0], 200)

    def test_default_disabled_service_never_registers_or_downloads(self):
        backend = FakeBackend(["A"])
        service = CollectorService(backend, "test-token", poll_ms=100).start()
        try:
            backend.inject({"group": "A", "sender": "a", "text": "[图片]", "kind": "image"})
            event = service.poll(0, 2000)["events"][0]
            self.assertNotIn("imageId", event)
            self.assertFalse(service.images.references)
            self.assertIsNone(service.images.root)
            with self.assertRaisesRegex(Exception, "图片采集未启用"):
                service.image("A", "a" * 32)
        finally:
            service.stop()
