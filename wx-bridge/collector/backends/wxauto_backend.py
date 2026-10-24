"""wxauto 3.9（Windows / 微信 3.9.11.17）后端。

约束：
- wxauto 是 UI Automation，不是协议库。所有调用必须在**同一个 UI 线程**里串行执行
  （见 server.py 的 CollectorService），否则 comtypes 控件会互相踩。
- wxauto 3.9 没有结构化的 at 列表，只能拿到消息可见文本；@ 判断由 bridge 用别名匹配。
- 每个被监听的群需要一个独立的 ChatWnd 窗口，窗口被关掉后需要重新 AddListenChat。
"""

import os
import re
import queue
import sys
import threading
import time
import uuid
from pathlib import Path


MEDIA_RE = re.compile(
    r"^\[(图片|文件|语音|视频|链接|表情|位置|动画表情|名片|聊天记录|合并转发|转账|红包|音乐|小程序|视频号|直播|游戏|其他)\]"
)


SKIP_ATTRS = ("self", "system", "sys", "time", "recall")


def _attr(message):
    """wxauto4 keeps the sender class in `attr` and the content kind in `type`
    (a sticker I sent reads type='emotion', attr='self'); the archived wxauto
    build put the sender class in `type` instead. Accept both."""
    return str(getattr(message, "attr", "") or "").lower()


def _is_self(message):
    return _attr(message) == "self" or str(getattr(message, "type", "") or "").lower() == "self"


class WxAutoBackend:
    name = "wxauto"

    def __init__(self, groups):
        repo_root = Path(__file__).resolve().parents[3]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        self.groups = list(groups)
        self.mode = "legacy"
        self.vision_enabled = False
        self._callback_messages = queue.Queue()
        self._seen_v4_ids = set()
        # wxauto4 supports Weixin 4.x. Keep the legacy path for the archived
        # wxauto 3.9 tests and installations that do not have wxauto4.
        if "wxauto" not in sys.modules:
            try:
                from wxauto4 import WeChat
                self.mode = "wxauto4"
            except ImportError:
                from wxauto import WeChat
        else:
            from wxauto import WeChat

        # 排障时设 WX_BRIDGE_WXAUTO_DEBUG=1 让 wxauto 把它自己的动作打进日志。
        self.debug = os.environ.get("WX_BRIDGE_WXAUTO_DEBUG") == "1"
        # 发图后等待「自己这边出现新消息」的秒数，用来把静默失败变成可见错误。
        self.delivery_wait_seconds = float(os.environ.get("WX_BRIDGE_SEND_VERIFY_SECONDS", "12"))
        if self.mode == "wxauto4":
            self.wx = WeChat(ads=False, debug=self.debug)
        else:
            self.wx = WeChat(debug=self.debug)
        self.self_nickname = str(getattr(self.wx, "nickname", "") or "")
        self.chats = {}
        self.errors = {}
        self._last_attempt = {}
        self._state_lock = threading.Lock()
        if self.mode == "wxauto4":
            for group in self.groups:
                self._ensure_chat_v4(group)
            # Ignore messages already visible when the collector starts; only
            # forward messages that arrive after startup.
            try:
                self._seen_v4_ids.update(
                    str(getattr(message, "id", "") or "")
                    for message in (self.wx.GetAllMessage() or [])
                    if getattr(message, "id", "")
                )
            except Exception:
                pass
        else:
            for group in self.groups:
                self._ensure_chat(group, force=True)

    def _on_v4_message(self, group, message, chat=None):
        # wxauto4's callback chat object can be a detached window without a
        # stable nickname; the registration site is the authoritative group.
        group = str(group or "").strip()
        if group in self.groups:
            self._callback_messages.put((group, message))

    def _ensure_chat_v4(self, group):
        try:
            chat = self.wx.GetSubWindow(group)
            with self._state_lock:
                self.chats[group] = chat
                self.errors.pop(group, None)
            return chat
        except Exception as error:
            with self._state_lock:
                self.errors[group] = f"AddListenChat: {type(error).__name__}: {error}"
            return None

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
        if self.mode == "wxauto4":
            messages = []
            try:
                new_messages = self.wx.GetAllMessage()
            except Exception as error:
                with self._state_lock:
                    for group in self.groups:
                        self.errors[group] = f"GetAllMessage: {type(error).__name__}: {error}"
                return messages
            allowed = set(self.groups)
            for message in new_messages or []:
                chat_info = getattr(message, "chat_info", {}) or {}
                group = str(chat_info.get("chat_name", "") or "").strip()
                if group not in allowed:
                    continue
                msg_id = str(getattr(message, "id", "") or "")
                if msg_id and msg_id in self._seen_v4_ids:
                    continue
                if msg_id:
                    self._seen_v4_ids.add(msg_id)
                converted = self._convert(group, message)
                if converted:
                    messages.append(converted)
            return messages

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
        if _attr(message) in SKIP_ATTRS or msg_type in ("self", "sys", "time", "recall", ""):
            return None
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        if not content:
            if msg_type == "image":
                content = "[图片]"
            else:
                return None
        sender = getattr(message, "sender", "") or getattr(message, "sender_remark", "") or "未知"
        sender = str(sender)
        msg_id = str(getattr(message, "id", "") or "")
        if not msg_id:
            msg_id = f"wxauto-{uuid.uuid4().hex}"
        media = MEDIA_RE.match(content)
        is_image = msg_type == "image" or bool(media and media.group(1) == "图片")
        is_sticker = msg_type == "emotion"
        can_look = getattr(self, "vision_enabled", False) and self.mode == "wxauto4"
        captured = None
        if is_sticker and can_look:
            # 表情没有下载接口，改为截消息气泡；失败就只留文字。
            from collector.capture import capture_message
            captured = capture_message(message)
        attach = captured if is_sticker else (message if is_image and can_look else None)
        return {
            "id": msg_id,
            "group": group,
            "sender": sender,
            "text": content,
            "kind": "image" if is_image else ("sticker" if is_sticker else ("media" if media else "text")),
            "rawType": msg_type,
            **({"_imageMessage": attach} if attach is not None else {}),
        }

    def _check_send_result(self, result):
        # Inspected wxauto4 41.1.7: WxResponse is a dict; only status=成功
        # makes is_success / bool(response) true. Never mask a failed response.
        if self.mode == "wxauto4":
            if not isinstance(result, dict) or result.get("status") != "成功" or result.get("ok") is False:
                raise RuntimeError(f"wxauto4 send failed: {result!r}")
        elif result is not None and result is not True:
            # Legacy SendMsg / detached-window SendFiles can return None on success.
            raise RuntimeError(f"wxauto send failed: {result!r}")

    def _chat_target(self, group):
        """优先用该群自己的子窗口发图：主窗口的 who 参数在子窗口存在时会被忽略。"""
        chat = self.chats.get(group)
        if chat is not None and callable(getattr(chat, "SendFiles", None)):
            return chat
        return self.wx

    def _self_signature(self, target):
        """当前窗口里「自己发出的消息」指纹，用来确认发送是否真的落地。"""
        try:
            messages = target.GetAllMessage() or []
        except Exception:
            return None
        ids = sorted(
            str(getattr(message, "id", "") or "")
            for message in messages
            if _is_self(message)
        )
        return ids if any(ids) else [str(len(ids))]

    def _wait_until_delivered(self, target, before, timeout=None):
        timeout = self.delivery_wait_seconds if timeout is None else timeout
        if before is None or timeout <= 0:
            # 读不到消息列表时不误判，只按接口返回判定。
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            after = self._self_signature(target)
            if after is not None and after != before:
                return True
            time.sleep(0.5)
        return False

    def send(self, group, text, at=None):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        if self.mode == "wxauto4":
            result = self.wx.SendMsg(text, who=group, at=at or None, exact=True)
        else:
            # WeChat.SendMsg 会优先复用同名 ChatWnd；窗口不存在时会自行打开聊天。
            result = self.wx.SendMsg(text, who=group, at=at or None)
        self._check_send_result(result)
        return {"ok": True}

    def send_file(self, group, filepath, *, what="文件"):
        """通用文件发送（表情图、TTS 语音都走这里）：发完必须确认微信里真的多了一条。"""
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        # SendFiles 是文件接口，不是微信原生的表情/语音气泡接口。
        target = self._chat_target(group)
        before = self._self_signature(target)
        try:
            if target is self.wx:
                result = self.wx.SendFiles(filepath, who=group, exact=True) if self.mode == "wxauto4" \
                    else self.wx.SendFiles(filepath, who=group)
            else:
                # 子窗口的 who 参数无效，直接发到这个群自己的窗口。
                result = target.SendFiles(filepath)
            self._check_send_result(result)
        except Exception as error:
            if target is self.wx:
                raise
            # 子窗口可能已被用户关掉，退回主窗口再试一次。
            with self._state_lock:
                self.chats.pop(group, None)
            self.errors[group] = f"SendFiles 子窗口失败，已改用主窗口：{type(error).__name__}: {error}"
            target = self.wx
            before = self._self_signature(target)
            result = self.wx.SendFiles(filepath, who=group, exact=True) if self.mode == "wxauto4" \
                else self.wx.SendFiles(filepath, who=group)
            self._check_send_result(result)
        if not self._wait_until_delivered(target, before):
            raise RuntimeError(f"{what}未确认送达：微信里没有出现新的发送记录")
        return {"ok": True, "delivered": True}

    def send_sticker(self, group, filepath, sticker_id):
        result = self.send_file(group, filepath, what="图片")
        return {**result, "stickerId": sticker_id}
