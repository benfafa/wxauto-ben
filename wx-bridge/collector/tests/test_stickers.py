import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.stickers import MAX_STICKER_BYTES, StickerCatalog, StickerError, auto_sticker_items

PNG = b"\x89PNG\r\n\x1a\n"
IMAGE_BYTES = {".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff",
               ".png": PNG, ".gif": b"GIF89a"}
# Kept in sync with test/stickers.test.js to prove Node and Python agree.
AUTO_FILES = ["a.b.PNG", "happy day.jpg", "sad-life.GIF", "开心_庆祝.png"]
AUTO_IDS = {
    "a.b.PNG": "auto-8c0d8630c877",
    "happy day.jpg": "auto-f4f24ff4a3a1",
    "sad-life.GIF": "auto-8697d6432e49",
    "开心_庆祝.png": "auto-08b754933842",
}
AUTO_KEYWORDS = {
    "a.b.PNG": ["a", "b"],
    "happy day.jpg": ["happy", "day"],
    "sad-life.GIF": ["sad", "life"],
    "开心_庆祝.png": ["开心", "庆祝"],
}


def write_image(path, name):
    path.write_bytes(IMAGE_BYTES[Path(name).suffix.lower()] + b"fixture")


class StickerCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.directory = self.base / "stickers"
        self.directory.mkdir()
        (self.directory / "happy.png").write_bytes(PNG + b"test fixture")
        self.config = {"enabled": True, "items": [{"id": "happy", "file": "happy.png", "keywords": ["开心"]}]}

    def catalog(self, config=None):
        return StickerCatalog(self.config if config is None else config, base_dir=self.base)

    def test_config_file_relative_paths_and_disabled_defaults(self):
        config_file = self.base / "bridge.json"
        config_file.write_text(json.dumps({"stickers": self.config}, ensure_ascii=False), encoding="utf-8-sig")
        catalog = StickerCatalog.from_config_file(config_file)
        self.assertEqual(catalog.validate("happy"), (self.directory / "happy.png").resolve())
        for catalog in [StickerCatalog(base_dir=self.base / "missing"),
                        StickerCatalog.from_config_file(self.base / "absent.json")]:
            with self.assertRaises(StickerError) as caught:
                catalog.validate("happy")
            self.assertEqual(caught.exception.code, "stickers-disabled")
        config_file.write_text('{"stickers": null}', encoding="utf-8")
        with self.assertRaises(ValueError):
            StickerCatalog.from_config_file(config_file)

    def test_rejects_paths_and_schema_even_when_disabled(self):
        paths = ["../happy.png", "sub/../../happy.png", "sub\\..\\happy.png", "/happy.png",
                 "C:/happy.png", "C:happy.png", "\\happy.png", "\\\\host\\share\\happy.png",
                 "happy.png:stream", "happy.png\x00", "a/./happy.png", "a//happy.png", "a./happy.png",
                 "happy.png ", "happy.webp"]
        for file in paths:
            with self.subTest(file=file), self.assertRaises(ValueError):
                config = copy.deepcopy(self.config)
                config["enabled"] = False
                config["items"][0]["file"] = file
                self.catalog(config)
        for patch in [{"enabled": "false"}, {"autoLoad": "true"}, {"autoLoad": 1},
                      {"cooldownMs": True}, {"cooldownMs": -1},
                      {"cooldownMs": 1.5}, {"cooldownMs": "100"}, {"directory": "../stickers"},
                      {"directory": "C:/stickers"}, {"items": {}}, {"extra": True},
                      {"items": self.config["items"] * 2}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.catalog({**self.config, **patch})
        for patch in [{"id": "../happy"}, {"keywords": []}, {"keywords": [""]}, {"keywords": [1]}, {"path": "evil"}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.catalog({**self.config, "items": [{**self.config["items"][0], **patch}]})

    def test_empty_enabled_library_needs_no_directory_and_description_is_optional(self):
        catalog = StickerCatalog({"enabled": True, "items": []}, base_dir=self.base / "absent")
        with self.assertRaises(StickerError):
            catalog.validate("happy")
        config = {**self.config, "cooldownMs": 60000, "items": [
            {**self.config["items"][0], "description": "庆祝、祝贺"}]}
        self.assertEqual(self.catalog(config).validate("happy"), (self.directory / "happy.png").resolve())
        for description in [False, None, "x" * 501]:
            with self.subTest(description=description), self.assertRaises(ValueError):
                self.catalog({**config, "items": [{**config["items"][0], "description": description}]})

    def test_supported_magic_and_size_boundary(self):
        for suffix, magic in [("jpg", b"\xff\xd8\xff"), ("jpeg", b"\xff\xd8\xff"),
                              ("PNG", PNG), ("gif", b"GIF87a"), ("gif", b"GIF89a")]:
            with self.subTest(suffix=suffix, magic=magic):
                file = self.directory / f"sample.{suffix}"
                file.write_bytes(magic + b"fixture")
                config = {**self.config, "items": [{"id": "one", "file": file.name, "keywords": ["one"]}]}
                catalog = self.catalog(config)
                self.assertEqual(catalog.validate("one"), file.resolve())
                file.write_bytes(b"not an image")
                with self.assertRaises(StickerError):
                    catalog.validate("one")
        file = self.directory / "happy.png"
        file.write_bytes(PNG + b"x" * (MAX_STICKER_BYTES - len(PNG)))
        catalog = self.catalog()
        catalog.validate("happy")
        with file.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaises(StickerError):
            catalog.validate("happy")

    def test_revalidates_missing_nonregular_and_changed_files(self):
        catalog = self.catalog()
        file = self.directory / "happy.png"
        for content in [b"", b"GIF89a", b"not a PNG"]:
            file.write_bytes(content)
            with self.assertRaises(StickerError):
                catalog.validate("happy")
        file.unlink()
        with self.assertRaises(StickerError):
            catalog.validate("happy")
        file.mkdir()
        with self.assertRaises(StickerError):
            catalog.validate("happy")
        for value in ["unknown", "../happy.png", str(file), None, [], {}]:
            with self.subTest(value=value), self.assertRaises(StickerError):
                catalog.validate(value)

    def test_resolved_path_escape_and_root_change_without_symlink_privileges(self):
        catalog = self.catalog()
        resolve = Path.resolve
        file = self.directory / "happy.png"
        for changed, destination in [(file, self.base / "outside.png"),
                                     (file, self.base / "stickers-sibling" / "happy.png"),
                                     (self.directory, self.base / "replacement")]:
            def redirected(value, *args, **kwargs):
                return destination if value == changed else resolve(value, *args, **kwargs)

            with self.subTest(changed=changed, destination=destination):
                with patch.object(Path, "resolve", redirected), self.assertRaises(StickerError):
                    catalog.validate("happy")

    def test_resolved_extension_must_match_magic_too(self):
        catalog = self.catalog()
        resolve = Path.resolve
        target = self.directory / "renamed.gif"
        target.write_bytes(PNG)

        def redirected(value, *args, **kwargs):
            return target if value == self.directory / "happy.png" else resolve(value, *args, **kwargs)

        with patch.object(Path, "resolve", redirected), self.assertRaises(StickerError):
            catalog.validate("happy")

    def test_symlink_escape_and_replacement_are_rejected(self):
        outside = self.base / "outside.png"
        outside.write_bytes(PNG)
        link = self.directory / "link.png"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable without changing permissions: {error}")
        config = {**self.config, "items": [{"id": "link", "file": "link.png", "keywords": ["link"]}]}
        with self.assertRaises(StickerError):
            self.catalog(config)
        link.unlink()
        link.symlink_to(self.directory / "happy.png")
        catalog = self.catalog(config)
        catalog.validate("link")
        link.unlink()
        link.symlink_to(outside)
        with self.assertRaises(StickerError):
            catalog.validate("link")

    def test_auto_load_scans_one_level_sorted_and_matches_node_ids(self):
        directory = self.base / "auto"
        directory.mkdir()
        for name in AUTO_FILES:
            write_image(directory / name, name)
        (directory / "nested.png").mkdir()
        (directory / "notes.txt").write_text("not an image", encoding="utf-8")
        try:
            (directory / "link.png").symlink_to(directory / "a.b.PNG")
        except OSError:
            pass  # Symlink creation is not available; the remaining assertions still cover skips.

        items = auto_sticker_items(directory)
        self.assertEqual([item["file"].name for item in items], AUTO_FILES)
        self.assertEqual([item["id"] for item in items], [AUTO_IDS[name] for name in AUTO_FILES])
        for item in items:
            self.assertEqual(item["keywords"], AUTO_KEYWORDS[item["file"].name])
            self.assertEqual(item["description"], Path(item["file"].name).stem)

        catalog = StickerCatalog({"enabled": True, "directory": "auto"}, base_dir=self.base)
        for item in items:
            self.assertEqual(catalog.validate(item["id"]), (directory / item["file"]).resolve())

    def test_auto_load_keyword_splitting_dedupes_truncates_and_caps_at_ten(self):
        long = "x" * 60
        name = f"one_one_{long}_three_four_five_six_seven_eight_nine_ten_eleven.png"
        directory = self.base / "auto"
        directory.mkdir()
        write_image(directory / name, name)
        [item] = auto_sticker_items(directory)
        self.assertEqual(item["keywords"], ["one", "x" * 50, "three", "four", "five",
                                            "six", "seven", "eight", "nine", "ten"])
        self.assertEqual(item["description"], name[:-4])

    def test_auto_load_rules_match_node_and_explicit_items_win(self):
        directory = self.base / "auto"
        directory.mkdir()
        for name in AUTO_FILES:
            write_image(directory / name, name)
        explicit = {"id": "custom", "file": "a.b.PNG", "keywords": ["自定义"]}
        for auto_load in (True, False):
            with self.subTest(auto_load=auto_load):
                catalog = StickerCatalog({"enabled": True, "directory": "auto",
                                          "autoLoad": auto_load, "items": [explicit]},
                                         base_dir=self.base)
                self.assertEqual(catalog.validate("custom"), (directory / "a.b.PNG").resolve())
                with self.assertRaises(StickerError) as caught:
                    catalog.validate(AUTO_IDS["a.b.PNG"])
                self.assertEqual(caught.exception.code, "unknown-sticker")

        catalog = StickerCatalog({"enabled": True, "directory": "auto", "autoLoad": False},
                                 base_dir=self.base)
        with self.assertRaises(StickerError) as caught:
            catalog.validate(AUTO_IDS["a.b.PNG"])
        self.assertEqual(caught.exception.code, "unknown-sticker")

        missing = StickerCatalog({"enabled": True, "directory": "missing"}, base_dir=self.base)
        with self.assertRaises(StickerError):
            missing.validate("auto-000000000000")
        with self.assertRaises(ValueError):
            StickerCatalog({"autoLoad": "true"}, base_dir=self.base)

    def test_auto_load_keeps_at_most_200_images_after_sorting(self):
        names = [f"img-{index:03d}.png" for index in range(205)]
        directory = self.base / "auto"
        directory.mkdir()
        for name in names:
            write_image(directory / name, name)
        items = auto_sticker_items(directory)
        self.assertEqual(len(items), 200)
        self.assertEqual(items[0]["file"].name, "img-000.png")
        self.assertEqual(items[199]["file"].name, "img-199.png")

    def test_directory_symlink_escape_is_rejected(self):
        outside = self.base / "stickers-sibling"
        outside.mkdir()
        (outside / "happy.png").write_bytes(PNG)
        link = self.directory / "sub"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable without changing permissions: {error}")
        config = {**self.config, "items": [{"id": "link", "file": "sub/happy.png", "keywords": ["link"]}]}
        with self.assertRaises(StickerError):
            self.catalog(config)


if __name__ == "__main__":
    unittest.main()


# Kept in sync with test/stickers.test.js: the same index JSON must produce the
# same ID -> file mapping on both sides.
INDEX_FILES = ["001.gif", "002.png", "003.png", "004.gif", "005.png", "006.png"]
INDEX_BODY = {
    "001.gif": {"file": "001.gif", "safe": True, "caption": "熊猫头端碗坏笑，边吃边看热闹",
                "keywords": ["熊猫头", "看热闹"], "mood": "狡黠"},
    "002.png": {"file": "002.png", "safe": True, "caption": "流泪猫猫头，委屈到不行",
                "keywords": ["委屈", "哭"], "mood": "难过"},
    "003.png": {"file": "003.png", "safe": False, "caption": "涉政梗图", "keywords": ["敏感"], "mood": "无语"},
    "004.gif": {"file": "004.gif", "safe": True, "blocked": True, "caption": "服务商审核拒绝",
                "keywords": ["待人工确认"], "mood": "不可用"},
    "005.png": {"file": "005.png", "safe": True, "caption": "   ", "keywords": ["空描述"], "mood": "中性"},
    "006.png": {"file": "006.png", "caption": "没有 safe 字段", "keywords": ["缺字段"]},
    "007.png": {"file": "evil/../007.png", "safe": True, "caption": "越界路径", "keywords": ["越界"]},
    "ghost.png": {"file": "ghost.png", "safe": True, "caption": "索引里有但磁盘上没有", "keywords": ["缺失"]},
    "002.gif": {"file": "002.gif", "safe": True, "caption": "与 002.png 撞名的动图", "keywords": ["撞名"]},
}


def index_sha(value, length):
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


class StickerIndexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.directory = self.base / "stickers"
        self.directory.mkdir()
        for name in INDEX_FILES:
            write_image(self.directory / name, name)
        self.index_file = self.base / "sticker-index.json"

    def write_index(self, body=None):
        self.index_file.write_text(json.dumps(INDEX_BODY if body is None else body), encoding="utf-8")

    def catalog(self, **overrides):
        config = {"enabled": True, "directory": "stickers", "indexFile": "sticker-index.json"}
        config.update(overrides)
        return StickerCatalog(config, base_dir=self.base)

    def test_index_ids_match_node_and_skip_unsafe_or_missing_entries(self):
        self.write_index()
        catalog = self.catalog()
        self.assertEqual(sorted(catalog._files.items()),
                         sorted([("001", Path("001.gif")),
                                 (f"002-{index_sha('002.png', 6)}", Path("002.png"))]))
        self.assertEqual(catalog.index_file, self.index_file)

    def test_explicit_items_keep_priority_without_renumbering(self):
        self.write_index()
        catalog = self.catalog(items=[{"id": "001", "file": "001.gif", "keywords": ["自定义"]}])
        self.assertEqual(sorted(catalog._files), ["001", f"002-{index_sha('002.png', 6)}"])
        self.assertEqual(catalog._files["001"], Path("001.gif"))

    def test_disabled_or_missing_index_behaves_like_node(self):
        self.write_index()
        self.assertEqual(self.catalog(enabled=False)._files, {})
        self.assertEqual(self.catalog(indexFile="nope.json", autoLoad=False)._files, {})
        fallback = self.catalog(indexFile="nope.json")
        self.assertEqual(len(fallback._files), len(INDEX_FILES))
        with self.assertRaises(ValueError):
            self.catalog(indexFile=7)
        with self.assertRaises(ValueError):
            self.catalog(indexFile="../outside.json")

    def test_index_entries_are_validated_on_send_not_at_startup(self):
        body = {"broken.png": {"file": "broken.png", "safe": True, "caption": "坏文件", "keywords": ["坏"]}}
        self.write_index(body)
        (self.directory / "broken.png").write_bytes(b"not an image at all")
        catalog = self.catalog()
        self.assertIn("broken", catalog._files)
        with self.assertRaises(StickerError):
            catalog.validate("broken")

    def test_index_rejects_a_malformed_file(self):
        self.index_file.write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.catalog()
