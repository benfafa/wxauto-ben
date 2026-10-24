import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.inbox import Inbox
from collector.labeler import StickerLabeler, parse_label
from collector.stickers import StickerCatalog

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeImageMessage:
    def __init__(self, data):
        self.data = data

    def download(self, dir_path=None):
        path = Path(dir_path) / "shot.png"
        path.write_bytes(self.data)
        return str(path)


class ParseLabelTest(unittest.TestCase):
    def test_accepts_clean_json_and_strips_thinking(self):
        raw = '<think>想一下</think>{"caption":"猫猫坏笑","keywords":["猫","坏笑"],"mood":"得意","safe":true}'
        self.assertEqual(parse_label(raw)["caption"], "猫猫坏笑")
        self.assertEqual(parse_label('{"caption":"x","keywords":["a"],"mood":"","safe":true}')["mood"], "a")

    def test_rejects_bad_shapes(self):
        for raw in ("不是 JSON", "{}", '{"caption":"","keywords":["a"],"mood":"x","safe":true}',
                    '{"caption":"x","keywords":[],"mood":"x","safe":true}',
                    '{"caption":"x","keywords":["a"],"mood":"x","safe":"yes"}'):
            self.assertIsNone(parse_label(raw), raw)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.box = Inbox({"enabled": True, "directory": "inbox"}, base_dir=self.base)
        self.catalog = StickerCatalog({"enabled": False, "directory": "stickers"}, base_dir=self.base)
        self.catalog.directory.mkdir()
        self.index_file = self.base / "sticker-index-extra.json"
        self.labeler = StickerLabeler(self.box, self.catalog, index_file=self.index_file, enabled=True)

    def test_publishes_safe_sticker_into_catalog_and_index(self):
        entry = self.box.save(FakeImageMessage(PNG), "群A", "张三", kind="sticker")
        label = {"caption": "猫猫坏笑", "keywords": ["猫", "坏笑"], "mood": "得意", "safe": True}
        self.assertTrue(self.labeler._publish(entry, label))
        self.assertTrue((self.catalog.directory / entry["file"]).is_file())
        index = json.loads(self.index_file.read_text(encoding="utf-8"))
        self.assertEqual(index[entry["file"]]["caption"], "猫猫坏笑")
        self.assertTrue(index[entry["file"]]["safe"])

    def test_unsafe_or_unrelated_entries_are_not_published(self):
        entry = self.box.save(FakeImageMessage(PNG), "群A", "张三", kind="sticker")
        self.assertFalse(self.labeler._publish(entry, {"caption": "x", "keywords": ["a"], "mood": "x", "safe": False}))

    def test_only_unlabeled_stickers_are_queued(self):
        photo = self.box.save(FakeImageMessage(b"\x89PNG\r\n\x1a\nphoto"), "群A", "张三", kind="photo")
        sticker = self.box.save(FakeImageMessage(PNG), "群A", "张三", kind="sticker")
        self.assertEqual([e["file"] for e in self.box.sticker_entries()], [sticker["file"]])
        self.box.record_label(sticker["sha256"], {"caption": "已打标", "keywords": ["a"], "mood": "x", "safe": True})
        self.assertEqual(self.box.sticker_entries(), [])
        self.assertNotIn(photo["file"], [e["file"] for e in self.box.sticker_entries()])
