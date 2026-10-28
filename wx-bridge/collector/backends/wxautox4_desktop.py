"""Raw Windows desktop primitives for the wxautox4 backend.

Everything that talks directly to Win32 (window activation, occlusion-safe
mouse clicks, the native file picker and pywin32 clipboard compatibility)
lives here.  The backend and media mixins stay focused on WeChat semantics.

WeChat 4.1.15 ignores UIA Invoke on several chat controls, so a few operations
have to use the same real input a person would.
"""

import logging
import os
import struct
import time


FILE_DIALOG_TITLES = {"选择文件", "打开", "Open", "Select File"}
# The picker is a standard #32770, but its caption follows the shell version and
# locale ("打开"、"打开文件"、"Open"), so also accept any dialog naming the action.
FILE_DIALOG_HINTS = ("选择文件", "打开", "open", "select file")
FILENAME_EDIT_ID = 1148
OPEN_BUTTON_ID = 1
BM_CLICK = 0x00F5


def is_file_dialog_title(title):
    value = str(title or "").strip()
    if value in FILE_DIALOG_TITLES:
        return True
    lowered = value.lower()
    return any(hint in lowered for hint in FILE_DIALOG_HINTS)


def build_hdrop_payload(paths):
    """Build a CF_HDROP blob (DROPFILES header + UTF-16LE paths)."""
    parts = b""
    for path in paths:
        parts += os.path.abspath(path).encode("utf-16-le") + b"\x00\x00"
    header = struct.pack("<Iiiii", 20, 0, 0, 0, 1)  # pFiles, pt, fNC, fWide
    return header + parts + b"\x00\x00"


def patch_wxautox4_clipboard():
    """Fix wxautox4-open's file clipboard helpers on current pywin32.

    The bundled helper calls ``win32gui.OpenClipboard`` and
    ``wintypes.DROPFILES()``; neither exists in current pywin32, so every
    SendFiles/sticker send fails with "无法打开剪贴板".  Patch the module-level
    helpers once, in this process only.
    """
    try:
        from wxautox4.uia import uiautomation as uia
    except Exception:
        return
    if getattr(uia, "_wx_bridge_hdrop_patch", False):
        return
    logging.info("wxautox4 clipboard helpers patched (win32clipboard)")

    def set_clipboard_files(paths):
        resolved = [os.path.abspath(path) for path in paths]
        if not resolved:
            raise ValueError("至少需要一个文件")
        for path in resolved:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
        import win32clipboard
        import win32con

        payload = build_hdrop_payload(resolved)
        last_error = None
        for _ in range(20):
            try:
                win32clipboard.OpenClipboard()
                last_error = None
                break
            except Exception as error:
                last_error = error
                time.sleep(0.05)
        if last_error is not None:
            raise RuntimeError(f"无法打开剪贴板：{last_error}")
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, payload)
        finally:
            win32clipboard.CloseClipboard()
        return resolved

    uia._build_hdrop = build_hdrop_payload
    uia.SetClipboardFiles = set_clipboard_files
    uia._wx_bridge_hdrop_patch = True


def is_com_event_error(error):
    """UIA event subscription lost (0x80040201) after a window rebuild."""
    text = str(error)
    return ("-2147220991" in text
            or "事件无法调用任何订户" in text
            or "unable to invoke" in text.lower())


class DesktopMixin:
    """Window, mouse and file-picker operations shared by the send paths."""

    @staticmethod
    def _window_handle(target):
        core = getattr(target, "core", target)
        control = getattr(core, "control", None)
        try:
            return int(getattr(control, "NativeWindowHandle", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _activate_window(self, target):
        """Raise the conversation window above other applications."""
        hwnd = self._window_handle(target)
        if not hwnd:
            return False
        try:
            import win32con
            import win32gui
        except Exception:
            return False
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() != hwnd:
            # Windows refuses foreground changes from a background process.
            # Raising the window still makes the click land on WeChat, and the
            # click itself focuses it.
            try:
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
        core = getattr(target, "core", target)
        for control in (getattr(core, "SessionBox", None), getattr(core, "control", None)):
            if control is not None and self._control_clickable(control, hwnd):
                return True
        return win32gui.GetForegroundWindow() == hwnd

    def _restore_window(self, target):
        """Un-minimize a WeChat window without taking focus.

        WeChat 4.x stops rendering the message list while minimized, so UIA
        reads zero rows and the collector looks healthy while hearing nothing.
        """
        hwnd = self._window_handle(target)
        if not hwnd:
            return False
        try:
            import win32con
            import win32gui
        except Exception:
            return False
        try:
            if not win32gui.IsIconic(hwnd):
                return False
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        except Exception:
            return False
        return True

    def _control_clickable(self, control, hwnd):
        try:
            rect = control.BoundingRectangle
            return self._point_in_window(
                hwnd, (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2,
            )
        except Exception:
            return False

    @staticmethod
    def _point_in_window(hwnd, x, y):
        """Never click through to whatever happens to be on top of WeChat."""
        try:
            import win32con
            import win32gui
        except Exception:
            return False
        point = win32gui.WindowFromPoint((x, y))
        if not point:
            return False
        return win32gui.GetAncestor(point, win32con.GA_ROOT) == hwnd

    def _physical_click_at(self, x, y, hwnd):
        if not self._point_in_window(hwnd, x, y):
            raise RuntimeError("微信窗口被其他窗口遮挡，无法点击微信控件")
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        time.sleep(0.1)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

    def _physical_click(self, control, hwnd):
        rect = control.BoundingRectangle
        self._physical_click_at(
            (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2, hwnd,
        )

    @staticmethod
    def _chatbox_ready(box):
        return (box is not None
                and getattr(box, "input_field", None) is not None
                and getattr(box, "msg_list", None) is not None)

    def _current_chat_name(self):
        """Title of the open conversation, or ``None`` if the probe fails."""
        try:
            from wxautox4.ui.base import find_control

            control = find_control(
                self.wx.core.control, id_suffix="current_chat_name_label", timeout=0.8,
            )
        except Exception:
            return None
        return "" if control is None else str(getattr(control, "Name", "") or "")

    def _click_session(self, group):
        """Click a conversation row at its real screen position.

        ``wxautox4`` uses ``Click(simulateMove=False)``, which clicks wherever
        the pointer is; on this client that silently leaves the chat empty.
        """
        hwnd = self._window_handle(self.wx)
        try:
            sessions = self.wx.GetSession() or []
        except Exception:
            return False
        for session in sessions:
            if str(getattr(session, "nickname", "") or "") != group:
                continue
            try:
                rect = session.control.BoundingRectangle
                x = (rect.left + rect.right) // 2
                y = (rect.top + rect.bottom) // 2
            except Exception:
                return False
            if not self._point_in_window(hwnd, x, y):
                raise RuntimeError("微信窗口被其他窗口遮挡，无法切换会话")
            self._physical_click_at(x, y, hwnd)
            return True
        return False

    def _switch_main_chat(self, group, timeout=6.0):
        self._activate_window(self.wx)
        deadline = time.monotonic() + max(0.0, timeout)
        clicks = 0
        while time.monotonic() < deadline:
            name = self._current_chat_name()
            if name is not None and name != group and clicks < 2:
                if not self._click_session(group):
                    try:
                        if not self.wx.ChatWith(group, exact=True):
                            return None
                    except Exception:
                        return None
                clicks += 1
                time.sleep(0.8)
                continue
            try:
                box = self.wx.core.get_chatbox()
            except Exception:
                box = None
            if self._chatbox_ready(box) and str(getattr(box, "who", "") or "") == group:
                self._remember_box(self.wx, box)
                return box
            if name is not None and name != group and clicks >= 2:
                return None
            time.sleep(0.2)
        return None

    @staticmethod
    def _wait_for(predicate, timeout, interval=0.2):
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                result = predicate()
            except Exception:
                result = None
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    @staticmethod
    def _dialog_filename_edit(dialog):
        import win32gui

        edits = []

        def callback(hwnd, _):
            try:
                if win32gui.GetClassName(hwnd) == "Edit" and win32gui.GetDlgCtrlID(hwnd) == FILENAME_EDIT_ID:
                    edits.append(hwnd)
            except Exception:
                pass
            return True

        win32gui.EnumChildWindows(dialog, callback, None)
        if edits:
            return edits[0]
        combo = win32gui.FindWindowEx(dialog, 0, "ComboBoxEx32", None)
        combo = win32gui.FindWindowEx(combo, 0, "ComboBox", None) if combo else 0
        return win32gui.FindWindowEx(combo, 0, "Edit", None) if combo else 0

    @staticmethod
    def _find_file_dialog(owner_hwnd, timeout=5.0):
        import win32gui
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(owner_hwnd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = []

            def callback(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    if win32gui.GetClassName(hwnd) != "#32770":
                        return True
                    if not is_file_dialog_title(win32gui.GetWindowText(hwnd)):
                        return True
                    _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if window_pid == pid:
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(callback, None)
            if found:
                return found[0]
            time.sleep(0.2)
        return None

    def _send_file_dialog(self, box, target, filepath):
        """Send a file through WeChat's own file picker (no clipboard)."""
        import win32con
        import win32gui
        from pathlib import Path
        from wxautox4.ui.base import find_control

        hwnd = self._window_handle(target)
        if not hwnd:
            raise RuntimeError("找不到微信窗口")
        if not self._activate_window(target):
            raise RuntimeError("微信窗口无法到前台")
        try:
            box.clear_edit()
        except Exception:
            pass
        button = find_control(box.control, class_name="mmui::XButton", name="发送文件", timeout=1.5)
        if button is None:
            button = find_control(box.control, regex_name=r"^发送文件", timeout=1.0)
        if button is None:
            raise RuntimeError("找不到「发送文件」按钮")
        self._physical_click(button, hwnd)

        dialog = self._find_file_dialog(hwnd)
        if dialog is None:
            raise RuntimeError("没有打开文件选择框")
        try:
            edit = self._dialog_filename_edit(dialog)
            if edit is None:
                raise RuntimeError("文件选择框里找不到文件名输入框")
            win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, str(Path(filepath).resolve()))
            time.sleep(0.2)
            open_button = win32gui.GetDlgItem(dialog, OPEN_BUTTON_ID)
            if open_button:
                win32gui.SendMessage(open_button, BM_CLICK, 0, 0)
            else:
                win32gui.SendMessage(edit, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
                win32gui.SendMessage(edit, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and win32gui.IsWindow(dialog):
                time.sleep(0.1)
            if win32gui.IsWindow(dialog):
                raise RuntimeError("文件选择框没有关闭")
        except Exception:
            try:
                if win32gui.IsWindow(dialog):
                    win32gui.SendMessage(dialog, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
            raise
        time.sleep(0.3)
        try:
            box.input_field.SetFocus()
        except Exception:
            pass
        box.input_field.SendKeys("{Enter}")
        return {"status": "成功"}
