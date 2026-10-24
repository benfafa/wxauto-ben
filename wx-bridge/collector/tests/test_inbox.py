import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.inbox import Inbox, normalize_inbox

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class FakeImageMessage:
    def __init__(self, data, name="img.png", escape=False):
        self.data = data
        self.name = name
        self.escape = escape

    def download(self, dir_path=None):
        folder = Path(dir_path)
        folder.mkdir(parents=True, exist_ok=True)
        if self.escape:
            return str(folder.parent / "outside.png")
        path = folder / self.name
        path.write_bytes(self.data)
        return str(path)


class InboxConfigTest(unittest.TestCase):
    def test_defaults_and_bounds(self):
        cfg = normalize_inbox()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["maxPerDay"], 50)
        for bad in ({"maxBytes": 10}, {"maxPerDay": 0}, {"maxTotalMb": 1},
                    {"enabled": "yes"}, {"directory": "../x"}, {"directory": "C:/x"},
                    {"typo": 1}, []):
            with self.assertRaises(ValueError, msg=json.dumps(bad)):
                normalize_inbox(bad)


class InboxSaveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def inbox(self, **patch):
        return Inbox({"enabled": True, "directory": "inbox", **patch}, base_dir=self.base)

    def test_saves_and_dedupes(self):
        box = self.inbox()
        entry = box.save(FakeImageMessage(PNG), "群A", "张三")
        self.assertIsNotNone(entry)
        self.assertTrue(entry["file"].endswith(".png"))
        self.assertTrue((self.base / "inbox" / entry["file"]).is_file())
        self.assertEqual(box.status()["today"], 1)
        # 同一张图再来一次：命中 sha256，不再落一份
        self.assertIsNone(box.save(FakeImageMessage(PNG), "群A", "李四"))
        self.assertEqual(box.status()["count"], 1)
        # 索引落盘、重启后还能去重
        again = Inbox({"enabled": True, "directory": "inbox"}, base_dir=self.base)
        self.assertIsNone(again.save(FakeImageMessage(PNG), "群A", "王五"))
        self.assertEqual(again.status()["count"], 1)

    def test_rejects_non_image_and_oversize_and_escape(self):
        box = self.inbox(maxBytes=1024)
        self.assertIsNone(box.save(FakeImageMessage(b"not an image"), "群A", "张三"))
        self.assertIsNone(box.save(FakeImageMessage(b"\x89PNG\r\n\x1a\n" + b"x" * 4096), "群A", "张三"))
        self.assertIsNone(box.save(FakeImageMessage(PNG, escape=True), "群A", "张三"))
        self.assertEqual(box.status()["count"], 0)

    def test_jpg_keeps_extension_and_daily_cap(self):
        box = self.inbox(maxPerDay=2)
        self.assertTrue(box.save(FakeImageMessage(JPG, name="a.jpg"), "群A", "张三")["file"].endswith(".jpg"))
        self.assertTrue(box.save(FakeImageMessage(JPG + b"x"), "群A", "张三"))
        self.assertIsNone(box.save(FakeImageMessage(JPG + b"xx"), "群A", "张三"))
        self.assertEqual(box.status()["today"], 2)
        self.assertEqual(box.status()["maxPerDay"], 2)

    def test_disabled_is_a_noop(self):
        box = Inbox({"enabled": False}, base_dir=self.base)
        self.assertIsNone(box.save(FakeImageMessage(PNG), "群A", "张三"))
        self.assertFalse((self.base / "inbox").exists())
