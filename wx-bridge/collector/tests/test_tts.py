import array
import io
import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector import tts as mod

WAV_RATE = 24000


def _wav_bytes(seconds=0.2, rate=WAV_RATE):
    frames = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes([0, 1]) * frames)
    return buffer.getvalue()


WAV = _wav_bytes()

MP3 = b"ID3\x04\x00" + b"\x00" * 64


class TtsConfigTest(unittest.TestCase):
    def test_defaults_and_bounds(self):
        cfg = mod.normalize_tts()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["provider"], "minimax")
        self.assertEqual(cfg["model"], "speech-2.8-hd")
        self.assertEqual(cfg["voice"], "Chinese (Mandarin)_BashfulGirl")
        self.assertEqual(cfg["emotion"], "happy")
        self.assertEqual(cfg["vol"], 5.0)
        self.assertEqual(cfg["maxChars"], 300)
        self.assertFalse(cfg["nativeVoice"])
        for bad in ({"provider": "azure"}, {"maxChars": 0}, {"maxBytes": 10}, {"timeoutMs": 100},
                    {"speed": 3}, {"pitch": 13}, {"vol": 0}, {"emotion": "bored"},
                    {"edgeVoice": 1}, {"enabled": "yes"}, {"nativeVoice": "yes"},
                    {"typo": 1}, []):
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

    def test_native_voice_requests_wav_and_keeps_pcm(self):
        body = json.dumps({"base_resp": {"status_code": 0}, "data": {"audio": WAV.hex()}})
        calls = []

        def fake_urlopen(request, *args, **kwargs):
            calls.append(json.loads(request.data.decode("utf-8")))
            return self._response(body)

        with patch("collector.tts.urllib.request.urlopen", side_effect=fake_urlopen):
            path, folder = mod.synthesize(self._cfg(nativeVoice=True), "说句话")
        try:
            self.assertEqual(calls[0]["model"], "speech-2.8-hd")
            self.assertEqual(calls[0]["voice_setting"]["voice_id"], "Chinese (Mandarin)_BashfulGirl")
            self.assertEqual(calls[0]["voice_setting"]["emotion"], "happy")
            self.assertEqual(calls[0]["voice_setting"]["vol"], 5.0)
            self.assertEqual(calls[0]["output_format"], "hex")
            self.assertEqual(calls[0]["audio_setting"]["format"], "wav")
            self.assertEqual(calls[0]["audio_setting"]["sample_rate"], 24000)
            self.assertEqual(path.suffix, ".wav")
            self.assertEqual(path.read_bytes(), WAV)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_native_voice_rejects_a_non_wav_payload(self):
        body = json.dumps({"base_resp": {"status_code": 0}, "data": {"audio": MP3.hex()}})
        with patch("collector.tts.urllib.request.urlopen", return_value=self._response(body)):
            with self.assertRaises(mod.TtsError):
                mod.synthesize(self._cfg(nativeVoice=True), "你好")

    @staticmethod
    def _fake_miniaudio(amplitude=12000):
        class Decoded:
            def __init__(self, samples):
                self.samples = samples
                self.num_frames = len(samples)

        def decode(data, output_format=None, nchannels=1, sample_rate=24000):
            values = array.array("h", [0] * (sample_rate // 2))
            values[0] = amplitude
            values[1] = -amplitude
            return Decoded(values)

        return SimpleNamespace(decode=decode, SampleFormat=SimpleNamespace(SIGNED16="s16"))

    def test_edge_provider_closes_its_temp_file(self):
        class Communicate:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                Path(path).write_bytes(MP3)

        cfg = mod.normalize_tts({"provider": "edge", "edgeVoice": "zh-CN-XiaoxiaoNeural"})
        with patch.dict(sys.modules, {"edge_tts": SimpleNamespace(Communicate=Communicate)}):
            # An open mkstemp handle makes the cleanup raise on Windows.
            self.assertEqual(mod._synth_edge(cfg, "你好", 5), MP3)

    def test_edge_native_voice_decodes_to_wav(self):
        with patch("collector.tts._synth_edge", return_value=MP3), \
                patch.dict(sys.modules, {"miniaudio": self._fake_miniaudio()}):
            path, folder = mod.synthesize(self._cfg(provider="edge", nativeVoice=True), "说句话")
        try:
            data = path.read_bytes()
            self.assertEqual(data[:4], b"RIFF")
            with wave.open(io.BytesIO(data)) as handle:
                self.assertEqual(handle.getframerate(), 24000)
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_edge_native_voice_caps_a_clipping_peak(self):
        with patch.dict(sys.modules, {"miniaudio": self._fake_miniaudio(amplitude=32767)}):
            data = mod._mp3_to_wav(MP3)
        with wave.open(io.BytesIO(data)) as handle:
            frames = array.array("h")
            frames.frombytes(handle.readframes(handle.getnframes()))
        self.assertLessEqual(max(abs(value) for value in frames), mod.PEAK_CEILING)

    def test_edge_native_voice_reports_a_missing_decoder(self):
        with patch("collector.tts._synth_edge", return_value=MP3), \
                patch.dict(sys.modules, {"miniaudio": None}):
            with self.assertRaises(mod.TtsError) as caught:
                mod.synthesize(self._cfg(provider="edge", nativeVoice=True), "你好")
        self.assertIn("miniaudio", str(caught.exception))

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
