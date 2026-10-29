"""wxautox4-open backend (WeChat 4.x) for the wx-bridge collector.

wxautox4-open and wxauto4 share API names but differ in three places:

1. ``chat_info`` is a method (wxauto4 exposes a dict);
2. ``download()`` returns a WxResponse (the collector needs the saved path);
3. ``GetAllMessage()`` only reads the open conversation.

The backend itself only handles sessions, polling and delivery verification.
Raw Win32/UI work lives in :mod:`wxautox4_desktop`; outbound files/voice live
in :mod:`wxautox4_media`.
"""

import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from PIL import ImageStat

from .wxautox4_desktop import DesktopMixin, patch_wxautox4_clipboard
from .wxautox4_media import FileMixin, IMAGE_SUFFIXES, VoiceMixin


MEDIA_RE = re.compile(
    r"^\[(图片|文件|语音|视频|链接|表情|位置|动画表情|名片|聊天记录|合并转发|转账|红包|音乐|小程序|视频号|直播|游戏|其他)\]"
)

SKIP_ATTRS = ("self", "system", "sys", "time", "recall")
AVATAR_SIZE = 40
AVATAR_MIN_SCORE = 12.0


def _avatar_score(crop):
    hsv = crop.convert("HSV")
    saturation = ImageStat.Stat(hsv.split()[1]).mean[0]
    deviation = ImageStat.Stat(crop.convert("L")).stddev[0]
    return saturation * 2 + deviation


def _avatar_hash(crop):
    gray = crop.convert("L").resize((8, 8))
    pixels = list(gray.getdata())
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if value >= average else "0" for value in pixels)
    return format(int(bits, 2), "016x")[:8]


def _best_avatar_crop(image, left, top, width, side):
    """Find the most avatar-like square on one side of a message row."""
    if side == "left":
        xs = (left + 12, left + 16, left + 20)
    else:
        xs = (left + width - 64, left + width - 60, left + width - 56)
    best = None
    for x in xs:
        for y in (top, top + 4, top + 8, top + 12):
            if x < 0 or y < 0 or x + AVATAR_SIZE > image.width or y + AVATAR_SIZE > image.height:
                continue
            crop = image.crop((x, y, x + AVATAR_SIZE, y + AVATAR_SIZE))
            score = _avatar_score(crop)
            if best is None or score > best[0]:
                best = (score, crop)
    return best


def _attr(message):
    return str(getattr(message, "attr", "") or "").lower()


def _chat_name(message, default):
    """Read the conversation name from either API shape."""
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
    """Row identity from UIA. Volatile: a recycled row comes back with a new id."""
    control = getattr(message, "control", None)
    runtime = ()
    try:
        runtime = tuple(control.GetRuntimeId()) if control is not None else ()
    except Exception:
        runtime = ()
    content = str(getattr(message, "content", "") or "")
    return (runtime, content, str(getattr(message, "time", "") or ""))


def _message_type(message):
    return str(getattr(message, "type", "") or "").lower()


DEDUP_TYPES = ("text", "quote")


def _content_key(message):
    """Identity that survives UIA recycling a row, for rows that carry text.

    A row's runtime id changes when WeChat scrolls it out and back — which our
    own reply reliably triggers — so the trigger message would look new again and
    get answered twice. WeChat exposes no per-row timestamp here (``time`` is
    None), so the visible text is the only stable handle. Placeholder rows
    (image/sticker/file) are excluded: their "content" is not distinguishing.
    """
    if _message_type(message) not in DEDUP_TYPES:
        return None
    content = str(getattr(message, "content", "") or "").strip()
    return content or None


def _message_identity(message):
    identity = getattr(message, "runtime_id", None) or getattr(message, "id", None)
    if identity:
        return str(identity)
    control = getattr(message, "control", None)
    try:
        return str(tuple(control.GetRuntimeId())) if control is not None else ""
    except Exception:
        return ""


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


def _load_wechat(debug=False):
    from wxautox4 import WeChat

    # ``ads`` is a wxauto4 argument; wxautox4-open swallows it via **kwargs.
    return WeChat(ads=False, debug=debug, resize=False)


class WxAutoX4Backend(DesktopMixin, FileMixin, VoiceMixin):
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
        patch_wxautox4_clipboard()
        self._seen = {group: [] for group in self.groups}
        self._recent = {}
        # A row that scrolls out and back comes with a fresh UIA runtime id;
        # identical visible text inside this window is the same message.
        self.duplicate_window_seconds = float(os.environ.get("WX_BRIDGE_DUPLICATE_WINDOW_SECONDS", "60"))
        self._seeded = set()
        self._last_refresh = {group: 0.0 for group in self.groups}
        self._minimized_warned = {}
        self._missing_window = {}
        # GetSubWindow on a missing window is itself slow, so retrying the
        # detached window must stay rare.
        self.fallback_retry_seconds = 120.0
        self._seen_limit = 500
        self._state_lock = threading.Lock()
        # Resolving a ChatBox costs ~1.6s of UIA while reading rows from a live
        # one costs 0.02s, so every read path shares this per-window cache.
        self._boxes = {}
        self._box_rows = {}
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
        # Old rows already in the window are not new messages.  Fallback chats
        # are seeded after the first real switch (see _seed_fallback).
        for group in self.groups:
            chat = self.chats.get(group)
            if chat is None or chat is self.wx:
                continue
            try:
                for message in (self._all_messages(chat) or []):
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
        # Some conversations (e.g. 文件传输助手) cannot detach; switch the main
        # window to them for every read rather than failing.
        with self._state_lock:
            self.chats[group] = self.wx
            self.fallback.add(group)
            self.errors.pop(group, None)
        return self.wx

    def _missed_window(self, group):
        # Two consecutive misses: one flaky UIA lookup is not proof the window
        # is gone, but the third one means we are reading a dead handle.
        misses = self._missing_window.get(group, 0) + 1
        self._missing_window[group] = misses
        return misses >= 2

    def _demote_to_fallback(self, group):
        """Stop reading a detached window that no longer exists.

        WeChat can rebuild its UI in place (restart without a new process): the
        dead handle keeps answering with nothing while health still says ok.
        """
        logging.warning("群「%s」的独立窗口已消失，改用主窗口轮询；窗口恢复后会自动接管", group)
        self._missing_window.pop(group, None)
        with self._state_lock:
            self.chats[group] = self.wx
            self.fallback.add(group)
        # Re-seed on the first switch so the main window's already visible rows
        # are not replayed as new messages.
        self._seeded.discard(group)
        self._drop_box(self.wx)

    def _warn_minimized(self, group):
        now = time.monotonic()
        if now - self._minimized_warned.get(group, 0.0) < 60.0:
            return
        self._minimized_warned[group] = now
        logging.warning("微信窗口「%s」被最小化，已自动还原：最小化时 UIA 读不到任何消息", group)

    def health(self):
        with self._state_lock:
            listening = sorted(self.chats.keys())
            errors = dict(self.errors)
        return {
            "selfNickname": self.self_nickname,
            "groups": list(self.groups),
            "listening": listening,
            "errors": errors,
            "fallback": sorted(self.fallback),
            "supportsVoice": self.supports_voice,
        }

    # -- inbound -------------------------------------------------------------
    def _remember(self, group, message):
        key = _runtime_key(message)
        seen = self._seen.setdefault(group, [])
        if key in seen:
            return False
        content_key = _content_key(message)
        if content_key is not None and self._is_repeat(group, content_key):
            self._note_seen(group, key)
            return False
        return self._note_seen(group, key)

    def _is_repeat(self, group, content_key):
        """Same visible text already forwarded? UIA gave the row a new id."""
        window = self.duplicate_window_seconds
        if window <= 0:
            return False
        now = time.monotonic()
        recent = self._recent.setdefault(group, [])
        recent[:] = [item for item in recent if now - item[0] <= window]
        if any(item[1] == content_key for item in recent):
            return True
        recent.append((now, content_key))
        return False

    def _note_seen(self, group, key):
        seen = self._seen.setdefault(group, [])
        seen.append(key)
        if len(seen) > self._seen_limit:
            del seen[: len(seen) - self._seen_limit]
        return True

    def _seed_fallback(self, group):
        """Mark the group's already-visible rows as seen after switching to it."""
        if group in self._seeded:
            return
        try:
            messages = self._all_messages(self.wx) or []
        except Exception:
            return
        for message in messages:
            self._remember(group, message)
        self._seeded.add(group)

    def poll(self):
        messages = []
        for group in self.groups:
            chat = self.chats.get(group) or self._ensure_chat(group)
            if chat is None:
                continue
            now = time.monotonic()
            if group in self.fallback:
                # Fallback reads mean switching the main window every cycle, which
                # rebuilds the UIA tree and re-reports rows.  A detached window can
                # appear long after startup (WeChat restores it, or the user opens
                # it), so keep offering it the chance to take over.
                if now - self._last_refresh.get(group, 0.0) >= self.fallback_retry_seconds:
                    self._last_refresh[group] = now
                    chat = self._ensure_chat(group)
            elif now - self._last_refresh.get(group, 0.0) >= 5.0:
                # The detached window's UIA tree is rebuilt after screenshots
                # and sends; a cached ChatBox then reads an empty old list.
                # Re-attach periodically so a repaint cannot silence the poll.
                self._last_refresh[group] = now
                try:
                    fresh_chat = self.wx.GetSubWindow(group)
                except Exception:
                    fresh_chat = None
                if fresh_chat is not None:
                    with self._state_lock:
                        self.chats[group] = fresh_chat
                        self.errors.pop(group, None)
                    chat = fresh_chat
                    self._missing_window.pop(group, None)
                elif self._missed_window(group):
                    self._demote_to_fallback(group)
                    continue
            if self._restore_window(chat):
                self._warn_minimized(group)
            try:
                # Read visible rows and dedupe with our own _seen list.
                # GetNewMessage() filters through ChatBox.used_msg_ids; delivery
                # checks can advance it and silently drop the next message.
                if group in self.fallback:
                    if not self.wx.ChatWith(group, exact=True):
                        raise RuntimeError("切换会话失败")
                    # The main window now shows another chat: its cached box is
                    # bound to the previous one.
                    self._drop_box(self.wx)
                    self._seed_fallback(group)
                    fresh = self._all_messages(self.wx)
                else:
                    fresh = self._all_messages(chat)
                if fresh is None:
                    raise RuntimeError("读取会话消息失败")
            except Exception as error:
                with self._state_lock:
                    self.errors[group] = f"GetNewMessage: {type(error).__name__}: {error}"
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

    def _sender_from_avatar(self, group, message):
        """Return ``(sender_key, is_self)`` from the row avatar.

        Group nicknames are not exposed by UIA, but every row paints a stable
        avatar.  A hash of the left avatar gives us a per-user key without OCR.
        """
        target = self.chats.get(group) or self.wx
        box = self._read_box(target)
        if box is None:
            return None, None
        try:
            image, origin_x, origin_y = box._capture_client()
        except Exception:
            return None, None
        if image is None:
            return None, None
        try:
            rect = message.control.BoundingRectangle
        except Exception:
            return None, None
        left = rect.left - origin_x
        top = rect.top - origin_y
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if left < 0 or top < 0 or width < 120 or height < AVATAR_SIZE:
            return None, None
        left_best = _best_avatar_crop(image, left, top, width, "left")
        right_best = _best_avatar_crop(image, left, top, width, "right")
        left_score = left_best[0] if left_best else 0.0
        right_score = right_best[0] if right_best else 0.0
        # Only skip as self when the right avatar is clearly dominant.  A dark
        # friend avatar next to a colourful bubble must not lose the message.
        if right_score >= 20 and right_score > left_score * 2 and left_score < 8:
            return None, True
        if left_score >= AVATAR_MIN_SCORE:
            return "用户#" + _avatar_hash(left_best[1]), False
        return None, None

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
        sender_key = None
        if msg_type in ("text", "quote"):
            sender_key, is_self = self._sender_from_avatar(group, message)
            if is_self:
                return None
        # Group nicknames are not in the UIA tree; bridge shows them as 未知.
        sender = sender_key or str(
            getattr(message, "sender", "") or getattr(message, "sender_remark", "") or "未知"
        )
        msg_id = "wxautox4-" + uuid.uuid4().hex
        media = MEDIA_RE.match(content)
        is_image = msg_type == "image" or bool(media and media.group(1) == "图片")
        is_sticker = msg_type == "emotion"
        attach = None
        if is_sticker and self.vision_enabled:
            from collector.capture import capture_message
            attach = capture_message(message)
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

    # -- outbound delivery ---------------------------------------------------
    @staticmethod
    def _send_ok(result):
        if result is None:
            return True
        if isinstance(result, dict):
            return (result.get("ok") is not False
                    and result.get("status") in (None, "成功", "success"))
        return result is True

    def _check_send_result(self, result):
        if not self._send_ok(result):
            raise RuntimeError(f"wxautox4 send failed: {result!r}")

    def _chatbox_for(self, target):
        try:
            return self.wx.core.get_chatbox() if target is self.wx else target._chatbox()
        except Exception:
            return None

    def _remember_box(self, target, box):
        if box is None:
            return
        key = self._window_handle(target) or id(target)
        # Keep the window object alive next to its box: a detached Chat can be
        # replaced by the periodic re-attach without costing another resolve.
        self._boxes[key] = (target, box)
        self._box_rows.pop(key, None)

    def _read_box(self, target):
        key = self._window_handle(target) or id(target)
        cached = self._boxes.get(key)
        if cached is not None:
            return cached[1]
        box = self._chatbox_for(target)
        self._remember_box(target, box)
        return box

    def _drop_box(self, target):
        key = self._window_handle(target) or id(target)
        self._boxes.pop(key, None)
        self._box_rows.pop(key, None)

    def _all_messages(self, target):
        """Read visible rows without advancing the poller's used-id cursor.

        ChatBox.get_msgs() marks every row as used, so a delivery check during
        a send would silently swallow messages that arrived in the meantime.
        """
        key = self._window_handle(target) or id(target)
        for _attempt in (0, 1):
            box = self._read_box(target)
            if not callable(getattr(box, "get_msgs", None)):
                try:
                    return target.GetAllMessage() or []
                except Exception:
                    return None
            used = set(getattr(box, "used_msg_ids", ()))
            try:
                messages = box.get_msgs() or []
            except Exception:
                messages = None
            finally:
                try:
                    box.used_msg_ids = used
                except Exception:
                    pass
            if messages is None:
                self._drop_box(target)
                continue
            had_rows = self._box_rows.get(key)
            self._box_rows[key] = len(messages)
            # WeChat rebuilds the tree after a repaint or a send; a box that read
            # rows before and now reads none is stale, not an emptied chat.
            if not messages and had_rows:
                self._drop_box(target)
                continue
            return messages
        return None

    def _message_ids(self, target):
        messages = self._all_messages(target) or []
        return {identity for identity in (_message_identity(m) for m in messages) if identity}

    def _signature(self, target, kinds):
        messages = self._all_messages(target)
        if messages is None:
            return None
        matched = [message for message in messages if _message_type(message) in kinds]
        identities = [identity for identity in (_message_identity(message) for message in matched) if identity]
        return identities or [f"count:{len(matched)}"]

    def _wait_for_new(self, target, before, kinds, timeout=None):
        timeout = self.delivery_wait_seconds if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            for message in self._all_messages(target) or []:
                if _message_type(message) not in kinds:
                    continue
                identity = _message_identity(message)
                if identity and identity not in before:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def _mark_new_kind(self, target, before, kinds, content=None, group=None):
        """Mark our own just-sent rows as seen so the poller ignores them."""
        box = self._read_box(target)
        if box is None:
            return
        needle = content.strip() if content is not None else None
        for message in self._all_messages(target) or []:
            if _message_type(message) not in kinds:
                continue
            if needle is not None and str(getattr(message, "content", "") or "").strip() != needle:
                continue
            identity = _message_identity(message)
            if not identity or identity in before:
                continue
            control = getattr(message, "control", None)
            try:
                box.used_msg_ids.add(tuple(control.GetRuntimeId()))
            except Exception:
                pass
            if group is not None:
                self._remember(group, message)

    def send(self, group, text, at=None):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        target = self._chat_target(group)
        if target is self.wx:
            box = self._switch_main_chat(group)
            if box is None:
                raise RuntimeError(f"切换会话失败：{group}")
        else:
            box = None
        if target is self.wx:
            if at:
                try:
                    box.clear_edit()
                except Exception:
                    pass
                if self._send_ok(box.input_at(at)):
                    # clear=True would wipe the mention chip just inserted.
                    result = box.send_text(text, clear=False)
                else:
                    try:
                        box.clear_edit()
                    except Exception:
                        pass
                    result = box.send_text(text)
            else:
                result = box.send_text(text)
        else:
            result = target.SendMsg(text, at=at or None)
            if at and not self._send_ok(result):
                # The member may be unknown/renamed; a plain reply beats none.
                result = target.SendMsg(text)
        self._check_send_result(result)
        # No reread here: the next poll's avatar-side check skips our own text
        # rows, and avoiding two GetAllMessage passes keeps replies snappy.
        return {"ok": True}

    def _chat_target(self, group):
        chat = self.chats.get(group)
        if chat is None:
            chat = self._ensure_chat(group)
        return chat or self.wx

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
