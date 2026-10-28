"""Outbound file and voice media for the wxautox4 backend.

File sending prefers the clipboard but falls back to WeChat's native file
picker when the clipboard is locked.  Voice sending drives WeChat's
press-and-talk UI with real mouse input while playing a WAV into a virtual
audio cable.
"""

import threading
import time
import wave
from pathlib import Path

from .wxautox4_desktop import is_com_event_error


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif")
VOICE_PANEL_TIMEOUT = 3.0
VOICE_SEND_TIMEOUT = 6.0
VOICE_MAX_SECONDS = 60.0
CANCEL_NAMES = ("取消", "Cancel")


class FileMixin:
    """Send files/images, with a clipboard-free fallback."""

    @staticmethod
    def _file_kinds(filepath):
        if Path(filepath).suffix.lower() in IMAGE_SUFFIXES:
            return {"image", "emotion"}
        return {"file", "other"}

    @staticmethod
    def _is_clipboard_error(result):
        text = str(result).lower()
        return "剪贴板" in text or "clipboard" in text

    def send_file(self, group, filepath, *, what="文件"):
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        target = self._chat_target(group)
        if target is self.wx:
            box = self._switch_main_chat(group)
            if box is None:
                raise RuntimeError(f"切换会话失败：{group}")
        else:
            box = None
        before = self._message_ids(target)
        kinds = self._file_kinds(filepath)
        result = box.send_file(filepath) if box is not None else target.SendFiles(filepath)
        if not self._send_ok(result) and self._is_clipboard_error(result):
            if box is None:
                try:
                    box = target._chatbox()
                except Exception:
                    box = None
            if box is None:
                raise RuntimeError("无法打开剪贴板，也拿不到聊天输入框")
            result = self._send_file_dialog(box, target, filepath)
        self._check_send_result(result)
        self._mark_new_kind(target, before, kinds, group=group)
        if not self._wait_for_new(target, before, kinds):
            raise RuntimeError(f"{what}未确认送达：微信里没有出现新的发送记录")
        return {"ok": True, "delivered": True}

    def send_sticker(self, group, filepath, sticker_id):
        result = self.send_file(group, filepath, what="图片")
        return {**result, "stickerId": sticker_id}


class VoiceMixin:
    """Send native WeChat voice bubbles via press-and-talk."""

    def _voice_chatbox(self, group, target):
        if not self._activate_window(target):
            return None
        if target is self.wx:
            return self._switch_main_chat(group)
        get_box = getattr(target, "_chatbox", None)
        if not callable(get_box):
            return None
        deadline = time.monotonic() + 3.0
        while True:
            try:
                box = get_box()
            except Exception:
                box = None
            if self._chatbox_ready(box) and str(getattr(box, "who", "") or "") == group:
                self._remember_box(target, box)
                return box
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    @staticmethod
    def _wav_duration(filepath):
        try:
            with wave.open(str(filepath), "rb") as handle:
                if handle.getsampwidth() != 2:
                    return None
                rate = handle.getframerate()
                if rate <= 0:
                    return None
                return handle.getnframes() / float(rate)
        except (wave.Error, OSError):
            return None

    def _cancel_voice_mode(self, box, hwnd):
        get_view = getattr(box, "voice_record_view", None)
        if not callable(get_view):
            return
        try:
            view = get_view(timeout=0.2)
        except Exception:
            return
        if view is None:
            return
        cancel = None
        try:
            for child in view.GetChildren():
                if (getattr(child, "Name", "") or "") in CANCEL_NAMES:
                    cancel = child
                    break
                for grandchild in child.GetChildren():
                    if (getattr(grandchild, "Name", "") or "") in CANCEL_NAMES:
                        cancel = grandchild
                        break
                if cancel is not None:
                    break
        except Exception:
            pass
        if cancel is not None:
            try:
                self._physical_click(cancel, hwnd)
            except Exception:
                return
        else:
            leave = getattr(box, "_leave_voice_mode", None)
            if callable(leave):
                try:
                    leave()
                except Exception:
                    pass
        self._wait_for(lambda: True if get_view(timeout=0.1) is None else None, 2.0)

    @staticmethod
    def _voice_cable():
        from wxautox4.param import WxParam
        from wxautox4.utils.tools import VBCable

        return VBCable(keyword=(WxParam.AUDIO_PARAM or {}).get("device_keyword"))

    @staticmethod
    def _play_wav(cable, filepath, errors):
        try:
            cable.play(filepath)
        except Exception as error:
            errors["error"] = error

    def _hold_record_area(self, area, hwnd, cable, filepath, duration, errors):
        rect = area.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        if not self._point_in_window(hwnd, x, y):
            raise RuntimeError("微信窗口被其他窗口遮挡，无法按住录音条")
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        time.sleep(0.1)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        try:
            time.sleep(0.2)
            player = threading.Thread(
                target=self._play_wav, args=(cable, filepath, errors),
                daemon=True, name="wxautox4-voice",
            )
            player.start()
            deadline = time.monotonic() + duration + 0.6
            while time.monotonic() < deadline and not errors:
                time.sleep(0.1)
            player.join(timeout=5)
        finally:
            user32.mouse_event(0x0004, 0, 0, 0, 0)

    def _wait_for_pending_send(self, record_view, send_button):
        deadline = time.monotonic() + VOICE_SEND_TIMEOUT
        while time.monotonic() < deadline:
            view = record_view(timeout=0.2)
            if view is None:
                return None
            button = send_button(view)
            if button is not None:
                return button
            time.sleep(0.2)
        return None

    def _send_voice_physical(self, group, target, filepath):
        """Press-and-talk with real mouse input.

        Returns ``None`` when the chatbox/audio stack is unavailable so the
        caller can fall back to ``SendAudio``.  Once recording starts,
        failures are raised after leaving voice mode.
        """
        box = self._voice_chatbox(group, target)
        if box is None:
            return None
        hwnd = self._window_handle(target)
        if not hwnd:
            return None
        helpers = [getattr(box, name, None) for name in
                   ("voice_button", "voice_record_view", "record_area", "voice_send_button")]
        if not all(callable(helper) for helper in helpers):
            return None
        voice_button, record_view, record_area, send_button = helpers
        try:
            cable = self._voice_cable()
        except Exception:
            return None
        ok, reason = cable.check()
        if not ok:
            raise RuntimeError(reason)
        duration = self._wav_duration(filepath)
        if duration is None:
            raise RuntimeError("语音文件不是可用的 16-bit PCM WAV")
        if duration > VOICE_MAX_SECONDS:
            raise RuntimeError(f"语音超过 {int(VOICE_MAX_SECONDS)} 秒，微信无法发送")

        self._cancel_voice_mode(box, hwnd)
        before = self._message_ids(target)
        delivered = False
        try:
            button = voice_button(timeout=2.0)
            if button is None:
                return None
            self._physical_click(button, hwnd)
            view = self._wait_for(lambda: record_view(timeout=0.2), VOICE_PANEL_TIMEOUT)
            if view is None:
                # The click can be swallowed right after a chat switch.
                self._physical_click(button, hwnd)
                view = self._wait_for(lambda: record_view(timeout=0.2), VOICE_PANEL_TIMEOUT)
            if view is None:
                return None
            area = record_area(view)
            if area is None:
                return None
            errors = {}
            self._hold_record_area(area, hwnd, cable, filepath, duration, errors)
            if errors:
                raise RuntimeError(f"语音播放失败：{errors['error']}")
            pending = self._wait_for_pending_send(record_view, send_button)
            if pending is not None:
                self._physical_click(pending, hwnd)
            if not self._wait_for_new(target, before, {"voice"}):
                raise RuntimeError("语音未确认送达：微信里没有出现新的语音气泡")
            self._mark_new_kind(target, before, {"voice"}, group=group)
            delivered = True
            return {"ok": True, "delivered": True, "voice": True}
        finally:
            if not delivered:
                self._cancel_voice_mode(box, hwnd)

    def _send_voice_once(self, group, filepath):
        target = self._chat_target(group)
        result = self._send_voice_physical(group, target, filepath)
        if result is not None:
            return result
        if target is self.wx:
            # wxauto4's main-window API needs who=; calling SendAudio(filepath)
            # would record into whatever conversation happens to be open.
            if self._switch_main_chat(group) is None:
                raise RuntimeError(f"切换会话失败：{group}")
        before = self._message_ids(target)
        if target is self.wx:
            result = self.wx.SendAudio(filepath, who=group, exact=True)
        elif hasattr(target, "SendAudio"):
            result = target.SendAudio(filepath)
        else:
            result = self.wx.SendAudio(filepath, who=group, exact=True)
        self._check_send_result(result)
        if not self._wait_for_new(target, before, {"voice"}):
            raise RuntimeError("语音未确认送达：微信里没有出现新的语音气泡")
        self._mark_new_kind(target, before, {"voice"}, group=group)
        return {"ok": True, "delivered": True, "voice": True}

    def send_voice(self, group, filepath):
        """Send a native voice bubble, rebuilding the UIA session once on COM errors."""
        if group not in self.groups:
            raise ValueError(f"group not allowed: {group}")
        try:
            return self._send_voice_once(group, filepath)
        except Exception as error:
            if not is_com_event_error(error):
                raise
            self.errors[group] = "UIA 事件订阅失效，已重建窗口重试"
            self._rebuild()
            return self._send_voice_once(group, filepath)
