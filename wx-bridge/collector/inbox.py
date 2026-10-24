"""群图片收件箱：只收普通图片，落进 pending 目录，不自动进可发送的曲库。

边界很清楚：
- 微信「表情」（type=emotion）在 4.x 里不落普通文件，缓存是加密 .dat，免费 API
  也没有下载方法，所以这里不收表情，只收 type=image 的普通图片。
- 收进来只是素材：去重（sha256）、单张大小、每日条数、总量都有上限；真正进曲库
  还要本地打标 + 人工确认，避免把别人的私人照片当成表情再发出去。
"""

import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

DEFAULTS = {
    "enabled": False,
    "directory": "inbox",
    "maxBytes": 5 * 1024 * 1024,
    "maxPerDay": 50,
    "maxTotalMb": 500,
}

MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)
SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def normalize_inbox(config=None):
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("inbox: invalid configuration fields")
    result = {**DEFAULTS, **config}
    if not isinstance(result["enabled"], bool):
        raise ValueError("inbox.enabled must be boolean")
    for key, minimum, maximum in (("maxBytes", 1024, 20 * 1024 * 1024),
                                  ("maxPerDay", 1, 2000),
                                  ("maxTotalMb", 10, 10000)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"inbox.{key}: invalid integer")
    directory = result["directory"]
    if not isinstance(directory, str) or not directory or ":" in directory \
            or directory.startswith(("/", "\\")) or ".." in directory.replace("\\", "/").split("/"):
        raise ValueError("inbox.directory 必须是安全的相对路径")
    return result


def inbox_from_file(file):
    """读配置并构造 Inbox；目录以配置文件所在位置为基准（和 stickers 一致）。"""
    path = Path(file).absolute()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return Inbox(base_dir=path.parent)
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    if "inbox" in data and data["inbox"] is None:
        raise ValueError("inbox must be an object")
    return Inbox(data.get("inbox", {}), base_dir=path.parent)


def _extension(data):
    for magic, ext in MAGIC:
        if data.startswith(magic):
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


class Inbox:
    def __init__(self, config=None, *, base_dir=None):
        self.cfg = normalize_inbox(config)
        self.directory = Path(base_dir or Path.cwd()).absolute() / self.cfg["directory"]
        self.index_file = self.directory / "index.json"
        self.index = self._load()

    def _load(self):
        try:
            value = json.loads(self.index_file.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _persist(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_file)

    def status(self):
        today = time.strftime("%Y-%m-%d")
        entries = [entry for entry in self.index.values() if isinstance(entry, dict)]
        return {
            "enabled": self.cfg["enabled"],
            "count": len(entries),
            "today": sum(1 for entry in entries if str(entry.get("savedAt", ""))[:10] == today),
            "bytes": sum(int(entry.get("bytes") or 0) for entry in entries),
            "maxPerDay": self.cfg["maxPerDay"],
            "maxTotalMb": self.cfg["maxTotalMb"],
        }

    def _quota_left(self):
        status = self.status()
        if status["today"] >= self.cfg["maxPerDay"]:
            return False
        return status["bytes"] < self.cfg["maxTotalMb"] * 1024 * 1024

    def save(self, message, group, sender):
        """把一条普通图片消息存进收件箱。返回索引条目，跳过时返回 None。"""
        if not self.cfg["enabled"] or message is None:
            return None
        if not callable(getattr(message, "download", None)):
            return None
        if not self._quota_left():
            return None
        folder = Path(tempfile.mkdtemp(prefix="wx-inbox-"))
        try:
            result = message.download(dir_path=str(folder))
            if not isinstance(result, (str, Path)):
                return None
            file = Path(result)
            if not file.is_absolute() or file.parent != folder or not file.is_file():
                return None
            info = file.lstat()
            if info.st_size <= 0 or info.st_size > self.cfg["maxBytes"]:
                return None
            data = file.read_bytes()
            ext = _extension(data)
            if ext is None:
                return None
            digest = hashlib.sha256(data).hexdigest()
            if digest in self.index:
                return None
            name = digest[:16] + ext
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / name).write_bytes(data)
            entry = {
                "file": name,
                "bytes": len(data),
                "sha256": digest,
                "group": str(group),
                "sender": str(sender or ""),
                "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.index[digest] = entry
            self._persist()
            return entry
        finally:
            shutil.rmtree(folder, ignore_errors=True)
