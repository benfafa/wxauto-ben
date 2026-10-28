"""TTS：把一段文字合成成 mp3，交给采集端当文件发出去。

只允许采集端内部调用：文字来自 bridge 的模型回复，HTTP 上永远只有文字和群名，
文件路径不出现在请求里。合成在 HTTP 线程里做（网络 + 编码），发送仍然排到
唯一那条 UI 线程上，两者不混。
"""

import array
import io
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path

DEFAULTS = {
    "enabled": False,
    "provider": "minimax",
    "keyFile": "",
    # 猫娘风 TTS：MiniMax Speech 2.8 HD + BashfulGirl。
    "model": "speech-2.8-hd",
    "voice": "Chinese (Mandarin)_BashfulGirl",
    "edgeVoice": "zh-CN-XiaoxiaoNeural",
    "speed": 1.0,
    "pitch": 0,
    "vol": 5.0,
    "emotion": "happy",
    "maxChars": 300,
    "maxBytes": 5 * 1024 * 1024,
    "timeoutMs": 60000,
    # 发微信原生语音气泡（而不是 mp3 文件）：需要能出 WAV 的后端 + 虚拟声卡。
    "nativeVoice": False,
}

PROVIDERS = ("minimax", "edge")
EMOTIONS = ("happy", "sad", "angry", "fearful", "disgusted", "surprised", "neutral")
MINIMAX_URL = "https://api.minimaxi.com/v1/t2a_v2"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class TtsError(RuntimeError):
    pass


def normalize_tts(config=None):
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("tts: invalid configuration fields")
    result = {**DEFAULTS, **config}
    if not isinstance(result["enabled"], bool):
        raise ValueError("tts.enabled must be boolean")
    if not isinstance(result["nativeVoice"], bool):
        raise ValueError("tts.nativeVoice must be boolean")
    if result["provider"] not in PROVIDERS:
        raise ValueError(f"tts.provider must be one of {', '.join(PROVIDERS)}")
    for name, minimum, maximum in (("maxChars", 1, 2000), ("maxBytes", 1024, 20 * 1024 * 1024),
                                   ("timeoutMs", 5000, 120000), ("pitch", -12, 12)):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"tts.{name}: invalid integer")
    speed = result["speed"]
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= speed <= 2:
        raise ValueError("tts.speed must be between 0.5 and 2")
    volume = result["vol"]
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0.1 <= volume <= 10:
        raise ValueError("tts.vol must be between 0.1 and 10")
    if not isinstance(result["emotion"], str) or result["emotion"] not in ("", *EMOTIONS):
        raise ValueError(f"tts.emotion must be one of {', '.join(EMOTIONS)}")
    for name in ("keyFile", "model", "voice", "edgeVoice"):
        if not isinstance(result[name], str):
            raise ValueError(f"tts.{name} must be a string")
    return result


def tts_from_file(file):
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return normalize_tts()
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    if "tts" in data and data["tts"] is None:
        raise ValueError("tts must be an object")
    return normalize_tts(data.get("tts", {}))


def clean_text(text, max_chars):
    """控制字符会破坏 TTS，空白压缩后按 maxChars 截断，尽量切在标点处。"""
    cleaned = CONTROL_RE.sub(" ", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[:max_chars]
    cut = max(head.rfind(mark) for mark in "。！？!?；;，,、")
    return (head[:cut + 1] if cut >= max_chars // 2 else head).strip()


def _ssl_context():
    """这台服务器的 Python 默认 CA 存储认不了 api.minimaxi.com 的链（curl/node 都正常），
    显式用 certifi 的根证书；没装 certifi 时退回系统默认。"""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _read_key(path):
    if not path:
        raise TtsError("tts.keyFile 未配置")
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise TtsError(f"读取 TTS Key 失败：{error.strerror or error}") from error
    if not key:
        raise TtsError("TTS Key 为空")
    return key


def _check_audio(data, max_bytes, want_wav=False):
    is_wav = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    okay = is_wav or (data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0))
    if not okay:
        raise TtsError("合成结果不是 mp3/wav")
    if want_wav:
        if not is_wav:
            raise TtsError("TTS 没有返回 WAV，无法发原生语音")
        try:
            with wave.open(io.BytesIO(data)) as handle:
                if handle.getsampwidth() != 2:
                    raise TtsError("WAV 不是 16-bit PCM，无法发原生语音")
                if handle.getframerate() < 8000:
                    raise TtsError("WAV 采样率过低")
        except wave.Error as error:
            raise TtsError(f"WAV 解析失败：{error}") from error
    if not 0 < len(data) <= max_bytes:
        raise TtsError(f"合成音频超出 {max_bytes} 字节上限")
    return data


def _synth_minimax(cfg, text, timeout):
    key = _read_key(cfg["keyFile"])
    want_wav = bool(cfg.get("nativeVoice"))
    voice_setting = {
        "voice_id": cfg["voice"],
        "speed": cfg["speed"],
        "vol": cfg["vol"],
        "pitch": cfg["pitch"],
    }
    if cfg["emotion"]:
        voice_setting["emotion"] = cfg["emotion"]
    payload = {
        "model": cfg["model"],
        "text": text,
        "stream": False,
        "voice_setting": voice_setting,
        "audio_setting": {"sample_rate": 24000, "bitrate": 128000,
                          "format": "wav" if want_wav else "mp3", "channel": 1},
        "output_format": "hex",
    }
    request = urllib.request.Request(
        MINIMAX_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        for line in error:
            line.decode("utf-8", "ignore")
        raise TtsError(f"TTS 服务返回 HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TtsError(f"TTS 请求失败：{error}") from error
    status = (body.get("base_resp") or {}).get("status_code")
    if status not in (0, None):
        message = str((body.get("base_resp") or {}).get("status_msg") or "")[:80]
        raise TtsError(f"TTS 服务拒绝：{status} {message}")
    audio = (body.get("data") or {}).get("audio") or ""
    if not isinstance(audio, str) or not audio:
        raise TtsError("TTS 服务没有返回音频")
    try:
        return bytes.fromhex(audio)
    except ValueError as error:
        raise TtsError("TTS 返回的音频不是合法的十六进制") from error


def _synth_edge(cfg, text, timeout):
    try:
        import asyncio
        import edge_tts
    except ImportError as error:  # pragma: no cover - 可选依赖
        raise TtsError("edge-tts 未安装（pip install edge-tts）") from error
    fd, name = tempfile.mkstemp(prefix="tts-", suffix=".mp3")
    # mkstemp hands back an open handle; Windows refuses to delete the file
    # while it is still held, so close it before edge_tts writes the file.
    os.close(fd)
    path = Path(name)
    try:
        asyncio.run(edge_tts.Communicate(text, cfg.get("edgeVoice") or cfg["voice"]).save(str(path)))
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


WAV_RATE = 24000
# Above this the VB-CABLE → WeChat recorder chain starts clipping, which is what
# sounds like a harsh, noisy microphone.
PEAK_CEILING = 27852


def _mp3_to_wav(data, rate=WAV_RATE):
    """Edge 只回 mp3，而微信原生气泡要的是能直接播进虚拟声卡的 WAV。"""
    try:
        import miniaudio
    except ImportError as error:
        raise TtsError("edge 原生语音需要 miniaudio（pip install miniaudio）") from error
    try:
        decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=rate)
    except Exception as error:
        raise TtsError(f"edge 音频解码失败：{type(error).__name__}") from error
    samples = array.array("h")
    samples.frombytes(decoded.samples.tobytes())
    peak = max((abs(value) for value in samples), default=0)
    if peak > PEAK_CEILING:
        factor = PEAK_CEILING / peak
        samples = array.array("h", (int(value * factor) for value in samples))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def synthesize(cfg, text):
    """合成并返回 (path, 临时目录)；调用方负责删除整个目录。"""
    if not cfg["enabled"]:
        raise TtsError("TTS 未启用")
    spoken = clean_text(text, cfg["maxChars"])
    if not spoken:
        raise TtsError("可朗读的文字为空")
    timeout = cfg["timeoutMs"] / 1000
    data = _synth_minimax(cfg, spoken, timeout) if cfg["provider"] == "minimax" \
        else _synth_edge(cfg, spoken, timeout)
    if cfg["nativeVoice"] and cfg["provider"] == "edge":
        data = _mp3_to_wav(data)
    _check_audio(data, cfg["maxBytes"], want_wav=cfg["nativeVoice"])
    folder = Path(tempfile.mkdtemp(prefix="wx-bridge-tts-"))
    path = folder / ("reply.wav" if cfg["nativeVoice"] else "reply.mp3")
    path.write_bytes(data)
    return path, folder
