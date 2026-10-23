"""无微信环境下的采集端后端：用于 macOS/Linux 上的桥接联调与回归。"""

import json
import queue
import threading
import time
from pathlib import Path


class FakeBackend:
    name = "fake"

    def __init__(self, groups, send_log=None):
        self.groups = list(groups)
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
                messages.append(self._queue.get_nowait())
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
        with self._lock:
            self._sent.append(record)
            if self.send_log:
                self.send_log.parent.mkdir(parents=True, exist_ok=True)
                with self.send_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def sent(self):
        with self._lock:
            return list(self._sent)
