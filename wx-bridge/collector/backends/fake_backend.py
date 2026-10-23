"""无微信环境下的采集端后端：用于 macOS/Linux 上的桥接联调与回归。"""

import json
import queue
import threading
import time
from pathlib import Path


class FakeImageMessage:
    """Synthetic local fixture; never accepts an input path or URL."""
    def download(self, dir_path=None):
        from PIL import Image
        file = Path(dir_path) / "fake.png"
        Image.new("RGB", (16, 16), (30, 90, 150)).save(file)
        return file


class FakeBackend:
    name = "fake"

    def __init__(self, groups, send_log=None):
        self.groups = list(groups)
        self.vision_enabled = False
        self._queue = queue.Queue()
        self._sent = []
        self._lock = threading.Lock()
        self.send_log = Path(send_log) if send_log else None

    def inject(self, message):
        self._queue.put(dict(message))

    def health(self):
        return {
            "selfNickname": "FakeBot",
            "groups": list(self.groups),
        }

    def poll(self):
        messages = []
        while True:
            try:
                message = self._queue.get_nowait()
                if message.get("kind") == "image" and self.vision_enabled:
                    message.setdefault("_imageMessage", FakeImageMessage())
                else:
                    message.pop("_imageMessage", None)
                messages.append(message)
            except queue.Empty:
                break
        return messages

    def send(self, group, text, at=None):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        record = {
            "group": group,
            "text": text,
            "at": at,
            "time": time.time(),
        }
        self._record(record)
        return record

    def send_file(self, group, filepath, *, what="文件"):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        self._record({
            "kind": "file",
            "group": group,
            "file": str(filepath),
            "time": time.time(),
        })
        return {"ok": True, "delivered": True}

    def send_sticker(self, group, filepath, sticker_id):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        self._record({
            "kind": "sticker",
            "group": group,
            "stickerId": sticker_id,
            "file": str(filepath),
            "time": time.time(),
        })
        return {"ok": True, "stickerId": sticker_id}

    def _record(self, record):
        with self._lock:
            self._sent.append(record)
            if self.send_log:
                self.send_log.parent.mkdir(parents=True, exist_ok=True)
                with self.send_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def sent(self):
        with self._lock:
            return list(self._sent)
