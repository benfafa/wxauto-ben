import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector import tts as mod

MP3 = b"ID3\x04\x00" + b"\x00" * 64


class TtsConfigTest(unittest.TestCase):
    def test_defaults_and_bounds(self):
        cfg = mod.normalize_tts()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["provider"], "minimax")
        self.assertEqual(cfg["maxChars"], 300)
        for bad in ({"provider": "azure"}, {"maxChars": 0}, {"maxBytes": 10}, {"timeoutMs": 100},
                    {"speed": 3}, {"pitch": 13}, {"enabled": "yes"}, {"typo": 1}, []):
            with self.assertRaises(ValueError):
                mod.normalize_tts(bad)

    def test_clean_text_strips_controls_and_cuts_at_punctuation(self):
        self.assertEqual(mod.clean_text("  你好\n\n世界  ", 50), "你好 世界")
        self.assertEqual(mod.clean_text("a\x00b\x07c", 50), "a b c")
        long = "第一句话。" * 10
        cut = mod.clean_text(long, 15)
        self.assertLessEqual(len(cut), 15)
        self.assertTrue(cut.endswith("。"))

    def test_from_file(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "config.json"
            self.assertFalse(mod.tts_from_file(file)["enabled"])
            file.write_text(json.dumps({"tts": {"enabled": True, "voice": "x"}}), encoding="utf-8")
            cfg = mod.tts_from_file(file)
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["voice"], "x")
            file.write_text(json.dumps({"tts": None}), encoding="utf-8")
            with self.assertRaises(ValueError):
                mod.tts_from_file(file)


class TtsSynthesizeTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".key", encoding="utf-8")
        handle.write("sk-local-test")
        handle.close()
        self.key_file = handle.name
        self.addCleanup(lambda: Path(self.key_file).unlink(missing_ok=True))

    def _cfg(self, **patch):
        return mod.normalize_tts({"enabled": True, "keyFile": self.key_file, **patch})

    @staticmethod
    def _response(body):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False
        return Response(body.encode("utf-8"))

    def test_minimax_synthesize_writes_mp3_and_cleans_up(self):
        body = json.dumps({"base_resp": {"status_code": 0}, "data": {"audio": MP3.hex()}})
        with patch("collector.tts.urllib.request.urlopen", return_value=self._response(body)):
            path, folder = mod.synthesize(self._cfg(), "哼，才不是为了你呢。")
        try:
            self.assertEqual(path.read_bytes(), MP3)
            self.assertEqual(path.parent, folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_rejects_non_audio_payload(self):
        body = json.dumps({"base_resp": {"status_code": 0}, "data": {"audio": b"not-mp3".hex()}})
        with patch("collector.tts.urllib.request.urlopen", return_value=self._response(body)):
            with self.assertRaises(mod.TtsError):
                mod.synthesize(self._cfg(), "你好")

    def test_rejects_oversized_audio(self):
        big = b"ID3" + b"\x00" * 4096
        body = json.dumps({"base_resp": {"status_code": 0}, "data": {"audio": big.hex()}})
        with patch("collector.tts.urllib.request.urlopen", return_value=self._response(body)):
            with self.assertRaises(mod.TtsError):
                mod.synthesize(self._cfg(maxBytes=1024), "你好")

    def test_surfaces_service_error(self):
        body = json.dumps({"base_resp": {"status_code": 2054, "status_msg": "voice id not exist"}})
        with patch("collector.tts.urllib.request.urlopen", return_value=self._response(body)):
            with self.assertRaises(mod.TtsError) as caught:
                mod.synthesize(self._cfg(), "你好")
        self.assertIn("2054", str(caught.exception))

    def test_missing_key_file(self):
        cfg = mod.normalize_tts({"enabled": True, "keyFile": str(Path(self.key_file).with_suffix(".missing"))})
        with self.assertRaises(mod.TtsError):
            mod.synthesize(cfg, "你好")

    def test_disabled_and_empty_text(self):
        with self.assertRaises(mod.TtsError):
            mod.synthesize(mod.normalize_tts({"enabled": False}), "你好")
        with self.assertRaises(mod.TtsError):
            mod.synthesize(self._cfg(), "   ")
