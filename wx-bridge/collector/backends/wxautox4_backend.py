"""wxautox4-open 后端（微信 4.x）与 wxauto4 后端的适配层。

wxautox4-open 与 wxauto4 的公开 API 同名，但有三处语义不同，直接替换会在运行期
炸掉或者静默丢消息：

1. ``chat_info`` 是**方法**，wxauto4 是字典；
2. ``download()`` 返回 ``WxResponse``（里面只有一个目录），ImageCache 需要的是
   保存后的**文件路径**；
3. ``GetAllMessage()`` 只读当前会话，而采集端需要把多个群的消息汇总到一起。

另外语音（``SendAudio``）是 wxauto4 没有的能力：它会在语音模式下按住录制条，
把 WAV 播进虚拟声卡，再生成微信原生语音气泡。
"""

import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

MEDIA_RE = re.compile(
    r"^\[(图片|文件|语音|视频|链接|表情|位置|动画表情|名片|聊天记录|合并转发|转账|红包|音乐|小程序|视频号|直播|游戏|其他)\]"
)

SKIP_ATTRS = ("self", "system", "sys", "time", "recall")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif")


def _attr(message):
    return str(getattr(message, "attr", "") or "").lower()


def _chat_name(message, default):
    """Read the conversation name from either API shape.

    wxautox4-open exposes ``chat_info()`` as a method while wxauto4 exposes a
    plain dict.  A missing or raising implementation must not kill the poll.
    """
    info = getattr(message, "chat_info", None)
    if callable(info):
        try:
            info = info()
        except Exception:
            info = None
    if isinstance(info, dict):
        for key in ("chat_name", "name", "chat"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return default


def _runtime_key(message):
    """Identity that stays stable while the row exists but differs for repeats."""
    control = getattr(message, "control", None)
    runtime = ()
    try:
        runtime = tuple(control.GetRuntimeId()) if control is not None else ()
    except Exception:
        runtime = ()
    content = str(getattr(message, "content", "") or "")
    return (runtime, content, str(getattr(message, "time", "") or ""))


def _ts(message):
    """Message time is decorative here; the collector stamps arrival itself."""
    return 0.0


class ImageHandle:
    """Adapt ``download() -> WxResponse`` to ``download() -> file path``.

    ``ImageCache`` validates the returned path (inside the request directory,
    regular file, image magic), so the adapter only has to find which file the
    client wrote after "另存为".
    """

    def __init__(self, message, fallback_dir):
        self._message = message
        self._fallback_dir = Path(fallback_dir)

    def download(self, dir_path=None):
        folder = Path(dir_path) if dir_path else self._fallback_dir
        folder.mkdir(parents=True, exist_ok=True)
        before = {entry.name for entry in folder.iterdir()}
        started = time.time()
        result = self._message.download(dir_path=str(folder))
        if result is False:
            raise RuntimeError("图片保存失败")
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(str(result.get("message") or "图片保存失败"))
        candidates = [
            entry for entry in folder.iterdir()
            if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
            and (entry.name not in before or entry.stat().st_mtime >= started - 2)
        ]
        if not candidates:
            raise RuntimeError("图片保存后没有在目录里找到文件")
        newest = max(candidates, key=lambda entry: entry.stat().st_mtime)
        return str(newest)


def _is_com_event_error(error):
    """UIA 事件订阅失效：窗口重建后旧订阅还在，COM 抛 0x80040201。"""
    text = str(error)
    return "-2147220991" in text or "事件无法调用任何订户" in text         or "unable to invoke" in text.lower()


def _load_wechat(debug=False):
    from wxautox4 import WeChat

    # ``ads`` 是 wxauto4 的参数，wxautox4-open 用 **kwargs 吞掉，保持调用一致。
    return WeChat(ads=False, debug=debug, resize=False)


class WxAutoX4Backend:
    """Collector backend backed by wxautox4-open."""

    name = "wxautox4"
    supports_voice = True

    def __init__(self, groups, *, wechat_factory=None):
        self.groups = list(groups)
        self.mode = "wxautox4"
        self.vision_enabled = False
        self.chats = {}
        self.errors = {}
        self.fallback = set()
        self.self_nickname = ""
        self.delivery_wait_seconds = float(os.environ.get("WX_BRIDGE_SEND_VERIFY_SECONDS", "12"))
        self.debug = os.environ.get("WX_BRIDGE_WXAUTO_DEBUG") == "1"
        self._seen = {group: [] for group in self.groups}
        self._seen_limit = 500
        self._state_lock = threading.Lock()
        self._image_dir = Path(tempfile.gettempdir()) / "wxautox4-images"

        factory = wechat_factory or _load_wechat
        self._factory = factory
        self.wx = factory(debug=self.debug)
        self.self_nickname = str(getattr(self.wx, "nickname", "") or "")
        if not self.self_nickname:
            try:
                info = self.wx.GetMyInfo() or {}
                self.self_nickname = str(info.get("nickname") or info.get("name") or "")
            except Exception:
                self.self_nickname = ""
        for group in self.groups:
            self._ensure_chat(group)
        # 启动时已经在窗口里的旧消息不算新消息。
        for group in self.groups:
            chat = self.chats.get(group)
            if chat is None:
                continue
            try:
                for message in (chat.GetAllMessage() or []):
                    self._remember(group, message)
            except Exception:
                pass

    # -- chat windows --------------------------------------------------------
    def _ensure_chat(self, group):
        try:
            chat = self.wx.GetSubWindow(group)
        except Exception:
            chat = None
        if chat is not None:
            with self._state_lock:
                self.chats[group] = chat
                self.fallback.discard(group)
                self.errors.pop(group, None)
            return chat
        # 有些会话（例如「文件传输助手」）不能拆成独立窗口，就在主窗口里
        # 切过去读写；功能一致，只是每次要多一次会话切换。
        with self._state_lock:
            self.chats[group] = self.wx
            self.fallback.add(group)
            self.errors.pop(group, None)
        return self.wx

    def health(self):
        with self._state_lock:
            listening = sorted(self.chats.keys())
            errors = dict(self.errors)
        return {
            "selfNickname": self.self_nickname,
            "groups": list(self.groups),
            "listening": listening,
            "errors": errors,
            "supportsVoice": self.supports_voice,
        }

    # -- inbound -------------------------------------------------------------
    def _remember(self, group, message):
        key = _runtime_key(message)
        seen = self._seen.setdefault(group, [])
        if key in seen:
            return False
        seen.append(key)
        if len(seen) > self._seen_limit:
            del seen[: len(seen) - self._seen_limit]
        return True

    def poll(self):
        messages = []
        for group in self.groups:
            chat = self.chats.get(group) or self._ensure_chat(group)
            if chat is None:
                continue
            try:
                if group in self.fallback:
                    if not self.wx.ChatWith(group, exact=True):
                        raise RuntimeError("切换会话失败")
                    fresh = self.wx.GetNewMessage()
                else:
                    fresh = chat.GetNewMessage()
            except Exception as error:
                with self._state_lock:
                    self.errors[group] = f"GetNewMessage: {type(error).__name__}: {error}"
                    # 子窗口可能被关掉/被微信重绘，下一轮重新开。
                    self.chats.pop(group, None)
                continue
            with self._state_lock:
                self.errors.pop(group, None)
            for message in fresh or []:
                if not self._remember(group, message):
                    continue
                converted = self._convert(group, message)
                if converted:
                    messages.append(converted)
        return messages

    def _convert(self, group, message):
        msg_type = str(getattr(message, "type", "") or "").lower()
        if _attr(message) in SKIP_ATTRS or msg_type in ("self", "sys", "system", "time", "recall", ""):
            return None
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        if not content:
            if msg_type == "image":
                content = "[图片]"
            else:
                return None
        # 群聊昵称不在 UIA 树里，wxautox4-open 只能给空；bridge 会显示为「未知」。
        sender = str(getattr(message, "sender", "") or getattr(message, "sender_remark", "") or "未知")
        msg_id = "wxautox4-" + uuid.uuid4().hex
        media = MEDIA_RE.match(content)
        is_image = msg_type == "image" or bool(media and media.group(1) == "图片")
        is_sticker = msg_type == "emotion"
        captured = None
        if is_sticker and self.vision_enabled:
            from collector.capture import capture_message
            captured = capture_message(message)
        attach = None
        if is_sticker:
            attach = captured
        elif is_image and self.vision_enabled:
            attach = ImageHandle(message, self._image_dir)
        return {
            "id": msg_id,
            "group": _chat_name(message, group),
            "sender": sender,
            "text": content,
            "kind": "image" if is_image else ("sticker" if is_sticker else ("media" if media else "text")),
            "rawType": msg_type,
            **({"_imageMessage": attach} if attach is not None else {}),
        }

    # -- outbound ------------------------------------------------------------
    def _check_send_result(self, result):
        if result is None:
            return
        if isinstance(result, dict):
            if result.get("ok") is False or result.get("status") not in (None, "成功", "success"):
                raise RuntimeError(f"wxautox4 send failed: {result!r}")
            return
        if result is not True:
            raise RuntimeError(f"wxautox4 send failed: {result!r}")

    def _self_signature(self, target):
        try:
            messages = target.GetAllMessage() or []
        except Exception:
            return None
        ids = sorted(
            str(getattr(message, "runtime_id", "") or getattr(message, "id", "") or "")
            for message in messages
            if _attr(message) == "self"
        )
        return ids if any(ids) else [str(len(ids))]

    def _wait_until_delivered(self, target, before, timeout=None):
        timeout = self.delivery_wait_seconds if timeout is None else timeout
        if before is None or timeout <= 0:
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
        result = self.wx.SendMsg(text, who=group, at=at or None, exact=True)
        self._check_send_result(result)
        return {"ok": True}

    def _chat_target(self, group):
        chat = self.chats.get(group)
        if chat is None:
            chat = self._ensure_chat(group)
        return chat or self.wx

    def send_file(self, group, filepath, *, what="文件"):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        target = self._chat_target(group)
        before = self._self_signature(target)
        if target is self.wx:
            result = self.wx.SendFiles(filepath, who=group, exact=True)
        else:
            result = target.SendFiles(filepath)
        self._check_send_result(result)
        if not self._wait_until_delivered(target, before):
            raise RuntimeError(f"{what}未确认送达：微信里没有出现新的发送记录")
        return {"ok": True, "delivered": True}

    def send_sticker(self, group, filepath, sticker_id):
        result = self.send_file(group, filepath, what="图片")
        return {**result, "stickerId": sticker_id}

    def _rebuild(self):
        """Drop cached UIA handles and re-attach after a COM event failure."""
        with self._state_lock:
            self.chats.clear()
            self.fallback.clear()
        try:
            self.wx = self._factory(debug=self.debug)
        except Exception:
            return
        for group in self.groups:
            self._ensure_chat(group)

    def _send_voice_once(self, group, filepath):
        target = self._chat_target(group)
        before = self._self_signature(target)
        if hasattr(target, "SendAudio"):
            result = target.SendAudio(filepath)
        else:
            result = self.wx.SendAudio(filepath, who=group, exact=True)
        self._check_send_result(result)
        if not self._wait_until_delivered(target, before):
            raise RuntimeError("语音未确认送达：微信里没有出现新的语音气泡")
        return {"ok": True, "delivered": True, "voice": True}

    def send_voice(self, group, filepath):
        """Send a native voice bubble, rebuilding the UIA session once on COM errors."""
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        try:
            return self._send_voice_once(group, filepath)
        except Exception as error:
            if not _is_com_event_error(error):
                raise
            self.errors[group] = "UIA 事件订阅失效，已重建窗口重试"
            self._rebuild()
            return self._send_voice_once(group, filepath)
