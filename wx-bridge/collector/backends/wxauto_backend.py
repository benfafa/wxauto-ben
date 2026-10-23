"""wxauto 3.9（Windows / 微信 3.9.11.17）后端。

约束：
- wxauto 是 UI Automation，不是协议库。所有调用必须在**同一个 UI 线程**里串行执行
  （见 server.py 的 CollectorService），否则 comtypes 控件会互相踩。
- wxauto 3.9 没有结构化的 at 列表，只能拿到消息可见文本；@ 判断由 bridge 用别名匹配。
- 每个被监听的群需要一个独立的 ChatWnd 窗口，窗口被关掉后需要重新 AddListenChat。
"""

import re
import sys
import threading
import time
import uuid
from pathlib import Path


MEDIA_RE = re.compile(
    r"^\[(图片|文件|语音|视频|链接|表情|位置|动画表情|名片|聊天记录|合并转发|转账|红包|音乐|小程序|视频号|直播|游戏|其他)\]"
)


class WxAutoBackend:
    name = "wxauto"

    def __init__(self, groups):
        repo_root = Path(__file__).resolve().parents[3]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from wxauto import WeChat  # 仅 Windows 可导入

        self.groups = list(groups)
        self.wx = WeChat()
        self.self_nickname = str(getattr(self.wx, "nickname", "") or "")
        self.chats = {}
        self.errors = {}
        self._last_attempt = {}
        self._state_lock = threading.Lock()
        for group in self.groups:
            self._ensure_chat(group, force=True)

    def _ensure_chat(self, group, force=False):
        now = time.monotonic()
        if not force and now - self._last_attempt.get(group, 0) < 30:
            return self.chats.get(group)
        self._last_attempt[group] = now
        try:
            self.wx.AddListenChat(who=group)
            current = dict(self.wx.GetAllListenChat())
            with self._state_lock:
                self.chats.update(current)
            chat = current.get(group)
            if chat is None:
                with self._state_lock:
                    self.errors[group] = "AddListenChat 后没有找到监听窗口"
                return None
            exists = getattr(getattr(chat, "UIAAPI", None), "Exists", None)
            if callable(exists) and not exists(0.5):
                with self._state_lock:
                    self.chats.pop(group, None)
                    self.errors[group] = "AddListenChat 后聊天窗口不存在"
                return None
            with self._state_lock:
                self.errors.pop(group, None)
            return chat
        except Exception as error:
            with self._state_lock:
                self.errors[group] = f"{type(error).__name__}: {error}"
            return None

    def health(self):
        with self._state_lock:
            listening = sorted(self.chats.keys())
            errors = dict(self.errors)
        return {
            "selfNickname": self.self_nickname,
            "groups": list(self.groups),
            "listening": listening,
            "errors": errors,
        }

    def poll(self):
        messages = []
        for group in self.groups:
            chat = self.chats.get(group)
            if chat is None:
                chat = self._ensure_chat(group)
                if chat is None:
                    continue
            try:
                new_messages = chat.GetNewMessage()
            except Exception as error:
                with self._state_lock:
                    self.errors[group] = f"GetNewMessage: {type(error).__name__}: {error}"
                    # 窗口可能被用户关掉/微信重绘；清掉引用，下次轮询重开。
                    if not getattr(getattr(chat, "UIAAPI", None), "Exists", lambda *_: False)(0.1):
                        self.chats.pop(group, None)
                continue
            with self._state_lock:
                self.errors.pop(group, None)
            for message in new_messages or []:
                converted = self._convert(group, message)
                if converted:
                    messages.append(converted)
        return messages

    def _convert(self, group, message):
        msg_type = str(getattr(message, "type", "") or "")
        if msg_type in ("self", "sys", "time", "recall", ""):
            return None
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        if not content:
            return None
        sender = getattr(message, "sender", "") or getattr(message, "sender_remark", "") or "未知"
        sender = str(sender)
        msg_id = str(getattr(message, "id", "") or "")
        if not msg_id:
            msg_id = f"wxauto-{uuid.uuid4().hex}"
        media = MEDIA_RE.match(content)
        return {
            "id": msg_id,
            "group": group,
            "sender": sender,
            "text": content,
            "kind": "media" if media else "text",
            "rawType": msg_type,
        }

    def send(self, group, text, at=None):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        # WeChat.SendMsg 会优先复用同名 ChatWnd；窗口不存在时会自行打开聊天。
        self.wx.SendMsg(text, who=group, at=at or None)
        return {"ok": True}
