"""Adapter tests for the wxautox4-open backend (no WeChat, no UIA)."""

import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.backends import wxautox4_backend as backend_mod
from collector.backends import wxautox4_desktop as desktop_mod
from collector.backends import wxautox4_media as media_mod
from collector.backends.wxautox4_backend import ImageHandle, WxAutoX4Backend, _chat_name


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


class FakeControl:
    def __init__(self, rid):
        self.rid = rid

    def GetRuntimeId(self):
        return self.rid


class FakeMessage:
    def __init__(self, msg_type="text", attr="friend", content="hi", sender="",
                 rid=(1, 2), download_writes=True):
        self.type = msg_type
        self.attr = attr
        self.content = content
        self.sender = sender
        self.time = ""
        self.control = FakeControl(rid)
        self._download_writes = download_writes
        self.download_calls = []

    def download(self, dir_path=None):
        self.download_calls.append(dir_path)
        if self._download_writes:
            Path(dir_path, "saved.png").write_bytes(PNG)
        return {"status": "成功", "message": "", "data": {"dir_path": dir_path}}


class FakeChat:
    def __init__(self, messages=None, delivered=None):
        self.messages = list(messages or [])
        self.delivered = list(delivered or [])
        self.files = []
        self.audios = []
        self.texts = []
        self.control = None

    def SendMsg(self, text, who=None, clear=True, at=None, exact=False):
        self.texts.append((text, at))
        return {"status": "成功"}

    def GetNewMessage(self):
        fresh, self.messages = self.messages, []
        return fresh

    def GetAllMessage(self):
        return list(self.messages) + list(self.delivered)

    def SendFiles(self, filepath):
        self.files.append(filepath)
        msg_type = "image" if str(filepath).lower().endswith((".gif", ".png", ".jpg", ".jpeg")) else "file"
        self.delivered.append(FakeMessage(msg_type=msg_type, attr="self", content="[文件]",
                                          rid=(9, len(self.delivered))))
        return {"status": "成功"}

    def SendAudio(self, filepath):
        self.audios.append(filepath)
        self.delivered.append(FakeMessage(msg_type="voice", attr="self", content='语音3"秒',
                                          rid=(9, len(self.delivered))))
        return {"status": "成功"}


class ReadableBox:
    """Stand-in for wxautox4's ChatBox, whose resolution is the expensive part."""

    def __init__(self, rows):
        self.rows = rows
        self.used_msg_ids = set()
        self.reads = 0

    def get_msgs(self):
        self.reads += 1
        rows = self.rows()
        self.used_msg_ids = {message.control.GetRuntimeId() for message in rows}
        return rows


class BoxChat(FakeChat):
    """Detached window that hands out a fresh ChatBox on every resolution."""

    def __init__(self, messages=None, blank=False):
        super().__init__(messages=messages)
        self.boxes = []
        self.blank = blank

    def _chatbox(self):
        chat = self

        def rows():
            return [] if chat.blank else list(chat.messages) + list(chat.delivered)

        box = ReadableBox(rows)
        chat.boxes.append(box)
        return box


class CachedChatBoxTest(unittest.TestCase):
    def test_reads_reuse_one_resolved_chatbox(self):
        chat = BoxChat([FakeMessage(content="一", rid=(1,))])
        backend, _ = make_backend({"群A": chat})
        backend._message_ids(chat)
        backend._message_ids(chat)
        backend.poll()
        self.assertEqual(len(chat.boxes), 1, "每个窗口只应解析一次 ChatBox")
        self.assertGreaterEqual(chat.boxes[0].reads, 3)

    def test_delivery_check_keeps_the_poller_cursor(self):
        chat = BoxChat([FakeMessage(content="一", rid=(1,))])
        backend, _ = make_backend({"群A": chat})
        backend.poll()
        cursor = set(chat.boxes[0].used_msg_ids)
        backend.send_sticker("群A", "a.gif", "id1")
        own = {message.control.GetRuntimeId() for message in chat.delivered}
        self.assertEqual(set(chat.boxes[0].used_msg_ids), cursor | own,
                         "送达校验只许把自己的行标记为已读")

    def test_blank_box_is_reresolved_once(self):
        chat = BoxChat([FakeMessage(content="一", rid=(1,))])
        backend, _ = make_backend({"群A": chat})
        self.assertEqual(len(backend._all_messages(chat)), 1)
        chat.blank = True
        self.assertEqual(backend._all_messages(chat), [])
        chat.blank = False
        self.assertEqual(len(backend._all_messages(chat)), 1)
        self.assertEqual(len(chat.boxes), 2, "读空后应丢弃缓存并重解析一次")

    def test_main_window_switch_drops_the_cached_box(self):
        class BoxMain(FakeWeChat):
            def __init__(self):
                super().__init__({})
                self.rows = []
                self.boxes = []
                self.switched = []
                self.core = SimpleNamespace(
                    control=SimpleNamespace(NativeWindowHandle=7),
                    get_chatbox=self._get_chatbox,
                )

            def _get_chatbox(self):
                box = ReadableBox(lambda: list(self.rows))
                self.boxes.append(box)
                return box

            def GetSubWindow(self, group):
                return None

            def ChatWith(self, group, exact=False):
                self.switched.append(group)
                return True

            def GetAllMessage(self):
                return list(self.rows)

        main = BoxMain()
        backend = WxAutoX4Backend(["群A"], wechat_factory=lambda debug=False: main)
        self.assertEqual(backend.poll(), [])
        main.rows.append(FakeMessage(content="主窗口消息", rid=(3, 1)))
        self.assertEqual([e["text"] for e in backend.poll()], ["主窗口消息"])
        self.assertEqual(len(main.boxes), 2, "每次切换会话都要重新解析 ChatBox")
        self.assertEqual(backend.poll(), [])


class WindowSelfHealingTest(unittest.TestCase):
    def test_fallback_adopts_a_detached_window_that_appears_later(self):
        chat = FakeChat([FakeMessage(content="独立窗口消息", rid=(5, 1))])
        backend, fake = make_backend({"群A": None})
        self.assertIn("群A", backend.fallback)
        fake.chats["群A"] = chat
        backend.fallback_retry_seconds = 0.0
        texts = [event["text"] for event in backend.poll()]
        self.assertEqual(texts, ["独立窗口消息"])
        self.assertEqual(backend.fallback, set(), "独立窗口出现后必须脱离主窗口兜底")
        self.assertIs(backend.chats["群A"], chat)

    def test_fallback_retry_is_rate_limited(self):
        backend, fake = make_backend({"群A": None})
        backend.fallback_retry_seconds = 600.0
        backend._last_refresh["群A"] = time.monotonic()
        lookups = []
        original = fake.GetSubWindow

        def counting(group):
            lookups.append(group)
            return original(group)

        fake.GetSubWindow = counting
        backend.poll()
        self.assertEqual(lookups, [])

    def test_minimized_window_is_restored_without_stealing_focus(self):
        calls = []
        gui = SimpleNamespace(
            IsIconic=lambda hwnd: True,
            ShowWindow=lambda hwnd, cmd: calls.append((hwnd, cmd)),
        )
        con = SimpleNamespace(SW_SHOWNOACTIVATE=4)
        chat = FakeChat()
        chat.control = SimpleNamespace(NativeWindowHandle=4242)
        backend, _ = make_backend({"群A": chat})
        with patch.dict(sys.modules, {"win32gui": gui, "win32con": con}):
            self.assertTrue(backend._restore_window(chat))
        self.assertEqual(calls, [(4242, 4)])

    def test_visible_window_is_left_alone(self):
        calls = []
        gui = SimpleNamespace(
            IsIconic=lambda hwnd: False,
            ShowWindow=lambda hwnd, cmd: calls.append((hwnd, cmd)),
        )
        con = SimpleNamespace(SW_SHOWNOACTIVATE=4)
        chat = FakeChat()
        chat.control = SimpleNamespace(NativeWindowHandle=4242)
        backend, _ = make_backend({"群A": chat})
        with patch.dict(sys.modules, {"win32gui": gui, "win32con": con}):
            self.assertFalse(backend._restore_window(chat))
        self.assertEqual(calls, [])

    def test_poll_restores_before_reading(self):
        chat = FakeChat([FakeMessage(content="一", rid=(1,))])
        backend, _ = make_backend({"群A": chat})
        restored = []
        with patch.object(WxAutoX4Backend, "_restore_window",
                          side_effect=lambda target: restored.append(target) or False):
            backend.poll()
        self.assertIn(chat, restored)


class DetachedWindowLossTest(unittest.TestCase):
    """微信可以只重建窗口不换进程：死句柄读得到空列表却仍报健康。"""

    def test_two_misses_demote_to_main_window_fallback(self):
        chat = FakeChat([FakeMessage(content="一", rid=(1,))])

        class Flaky(FakeWeChat):
            def __init__(self, chats):
                super().__init__(chats)
                self.lost = False

            def GetSubWindow(self, group):
                return None if self.lost else self.chats.get(group)

        fake = Flaky({"群A": chat})
        backend = WxAutoX4Backend(["群A"], wechat_factory=lambda debug=False: fake)
        self.assertEqual(backend.fallback, set())

        fake.lost = True
        backend._last_refresh["群A"] = 0.0
        backend.poll()
        self.assertEqual(backend.fallback, set(), "一次查不到窗口不该就降级")

        backend._last_refresh["群A"] = 0.0
        backend.poll()
        self.assertIn("群A", backend.fallback)
        self.assertIs(backend.chats["群A"], backend.wx)
        self.assertNotIn("群A", backend._seeded, "降级后要重新播种，避免回放旧消息")


class DuplicateRowTest(unittest.TestCase):
    """UIA 回收行会换 runtime id，同一条消息不能被再回一次。"""

    def test_same_text_under_a_new_runtime_id_is_not_new(self):
        backend, _ = make_backend({"群A": FakeChat()})
        text = "@才不是鲸鱼娘 今晚吃了一尾肥鱼"
        self.assertTrue(backend._remember("群A", FakeMessage(content=text, rid=(1, 1))))
        self.assertFalse(backend._remember("群A", FakeMessage(content=text, rid=(1, 99))))

    def test_different_texts_stay_distinct(self):
        backend, _ = make_backend({"群A": FakeChat()})
        self.assertTrue(backend._remember("群A", FakeMessage(content="甲说的", rid=(1, 1))))
        self.assertTrue(backend._remember("群A", FakeMessage(content="乙说的", rid=(1, 2))))

    def test_groups_do_not_share_the_dedupe_window(self):
        backend, _ = make_backend({"群A": FakeChat(), "群B": FakeChat()})
        text = "同一句话"
        self.assertTrue(backend._remember("群A", FakeMessage(content=text, rid=(1, 1))))
        self.assertTrue(backend._remember("群B", FakeMessage(content=text, rid=(2, 1))))

    def test_placeholder_rows_are_not_deduped_by_text(self):
        backend, _ = make_backend({"群A": FakeChat()})
        first = FakeMessage(msg_type="image", content="[图片]", rid=(3, 1))
        second = FakeMessage(msg_type="image", content="[图片]", rid=(3, 2))
        self.assertTrue(backend._remember("群A", first))
        self.assertTrue(backend._remember("群A", second))

    def test_a_genuine_repeat_after_the_window_gets_through(self):
        backend, _ = make_backend({"群A": FakeChat()})
        backend.duplicate_window_seconds = 0.05
        text = "好的"
        self.assertTrue(backend._remember("群A", FakeMessage(content=text, rid=(4, 1))))
        time.sleep(0.12)
        self.assertTrue(backend._remember("群A", FakeMessage(content=text, rid=(4, 2))))

    def test_the_dedupe_can_be_turned_off(self):
        backend, _ = make_backend({"群A": FakeChat()})
        backend.duplicate_window_seconds = 0.0
        text = "关掉窗口"
        self.assertTrue(backend._remember("群A", FakeMessage(content=text, rid=(5, 1))))
        self.assertTrue(backend._remember("群A", FakeMessage(content=text, rid=(5, 2))))


class FileDialogCaptionTest(unittest.TestCase):
    """文件选择框的标题跟着系统语言和 shell 版本变，不能只认死几个词。"""

    def test_localized_captions_are_accepted(self):
        for title in ("打开", "Open", "打开文件", "选择文件", "Select File", "Open Files", " 打开 "):
            self.assertTrue(desktop_mod.is_file_dialog_title(title), title)

    def test_other_dialogs_are_not_mistaken_for_it(self):
        for title in ("", "微信", "另存为", "确认", "Save as"):
            self.assertFalse(desktop_mod.is_file_dialog_title(title), title)


class AtFailsChat(FakeChat):
    def SendMsg(self, text, who=None, clear=True, at=None, exact=False):
        if at:
            return {"status": "失败", "message": "@部分失败"}
        return super().SendMsg(text, at=None)


class FakeWeChat:
    def __init__(self, chats):
        self.chats = chats
        self.nickname = "F"
        self.sent = []

    def GetSubWindow(self, group):
        return self.chats.get(group)

    def SendMsg(self, text, who=None, at=None, exact=False):
        self.sent.append((text, who, at, exact))
        return {"status": "成功"}

    def SendFiles(self, filepath, who=None, exact=False):
        self.sent.append((filepath, who))
        return {"status": "成功"}

    def SendAudio(self, filepath, who=None, exact=False):
        return {"status": "成功"}


def make_backend(chats, **kwargs):
    fake = FakeWeChat(chats)

    def factory(debug=False):
        return fake

    backend = WxAutoX4Backend(list(chats), wechat_factory=factory, **kwargs)
    backend.delivery_wait_seconds = 1.0
    return backend, fake


class ChatInfoTest(unittest.TestCase):
    def test_accepts_method_and_dict_shapes(self):
        class Method:
            def chat_info(self):
                return {"chat_name": "群A"}

        class Dict:
            chat_info = {"chat_name": "群B"}

        class Broken:
            def chat_info(self):
                raise RuntimeError("nope")

        class Missing:
            pass

        self.assertEqual(_chat_name(Method(), "兜底"), "群A")
        self.assertEqual(_chat_name(Dict(), "兜底"), "群B")
        self.assertEqual(_chat_name(Broken(), "兜底"), "兜底")
        self.assertEqual(_chat_name(Missing(), "兜底"), "兜底")


class ImageHandleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_returns_the_file_the_client_wrote(self):
        message = FakeMessage(msg_type="image", content="[图片]")
        path = ImageHandle(message, self.dir).download(dir_path=str(self.dir))
        self.assertTrue(Path(path).is_file())
        self.assertEqual(Path(path).parent, self.dir)
        self.assertTrue(path.endswith(".png"))

    def test_picks_the_newest_image_when_the_folder_is_not_empty(self):
        (self.dir / "old.png").write_bytes(PNG)
        old = time.time() - 3600
        import os
        os.utime(self.dir / "old.png", (old, old))
        message = FakeMessage(msg_type="image")
        path = ImageHandle(message, self.dir).download(dir_path=str(self.dir))
        self.assertEqual(Path(path).name, "saved.png")

    def test_fails_when_nothing_is_written(self):
        message = FakeMessage(msg_type="image", download_writes=False)
        with self.assertRaises(RuntimeError):
            ImageHandle(message, self.dir).download(dir_path=str(self.dir))


class PollTest(unittest.TestCase):
    def test_aggregates_each_chat_and_skips_self_and_time_rows(self):
        chat_a = FakeChat()
        chat_b = FakeChat()
        backend, _ = make_backend({"群A": chat_a, "群B": chat_b})
        chat_a.messages.extend([
            FakeMessage(content="hello", rid=(1, 1)),
            FakeMessage(attr="self", content="mine", rid=(1, 2)),
            FakeMessage(msg_type="time", attr="system", content="12:00", rid=(1, 3)),
        ])
        chat_b.messages.append(FakeMessage(content="另一个群", rid=(2, 1)))
        events = backend.poll()
        self.assertEqual([event["text"] for event in events], ["hello", "另一个群"])
        self.assertEqual({event["group"] for event in events}, {"群A", "群B"})
        self.assertEqual([event["kind"] for event in events], ["text", "text"])

    def test_does_not_replay_the_same_row_twice(self):
        chat = FakeChat()
        backend, _ = make_backend({"群A": chat})
        chat.messages.append(FakeMessage(content="只发一次", rid=(5, 5)))
        self.assertEqual(len(backend.poll()), 1)
        chat.messages.append(FakeMessage(content="只发一次", rid=(5, 5)))
        self.assertEqual(backend.poll(), [])
        # UIA 把行回收后再放回来会换控件 id，同一条文字不能被当成新消息再回一次。
        chat.messages.append(FakeMessage(content="只发一次", rid=(5, 6)))
        self.assertEqual(backend.poll(), [])
        # 换了内容就还是新消息。
        chat.messages.append(FakeMessage(content="换个说法", rid=(5, 7)))
        self.assertEqual(len(backend.poll()), 1)

    def test_startup_backlog_is_not_forwarded(self):
        old = FakeMessage(content="启动前的消息", rid=(7, 1))
        chat = FakeChat([old])
        backend, _ = make_backend({"群A": chat})
        self.assertEqual(backend.poll(), [])
        chat.messages.append(FakeMessage(content="新的", rid=(7, 2)))
        self.assertEqual([event["text"] for event in backend.poll()], ["新的"])

    def test_image_and_sticker_attach_adapters_only_with_vision(self):
        chat = FakeChat()
        backend, _ = make_backend({"群A": chat})
        chat.messages.extend([
            FakeMessage(msg_type="image", content="[图片]", rid=(3, 1)),
            FakeMessage(msg_type="emotion", content="[动画表情]", rid=(3, 2)),
        ])
        events = backend.poll()
        self.assertNotIn("_imageMessage", events[0])
        self.assertEqual(events[0]["kind"], "image")
        self.assertEqual(events[1]["kind"], "sticker")

        chat.messages.extend([
            FakeMessage(msg_type="image", content="[图片]", rid=(3, 3)),
            FakeMessage(msg_type="emotion", content="[动画表情]", rid=(3, 4)),
        ])
        backend.vision_enabled = True
        events = backend.poll()
        self.assertIsInstance(events[0]["_imageMessage"], ImageHandle)
        # 表情走截图，截图失败时只是没有附图，不能丢消息。
        self.assertNotIn("_imageMessage", events[1])

    def test_failed_chat_is_reported_and_reopened(self):
        class FlakyChat(FakeChat):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def GetAllMessage(self):
                self.calls += 1
                raise RuntimeError("window gone")

        chat = FlakyChat()
        backend, fake = make_backend({"群A": chat})
        self.assertEqual(backend.poll(), [])
        self.assertIn("群A", backend.health()["errors"])
        self.assertNotIn("群A", backend.chats)
        # 下一轮重新 GetSubWindow 并恢复监听。
        fake.chats["群A"] = FakeChat([FakeMessage(content="回来了", rid=(8, 1))])
        self.assertEqual([event["text"] for event in backend.poll()], ["回来了"])
        self.assertEqual(backend.health()["errors"], {})

    def test_visible_rows_are_read_even_when_library_cursor_dropped_them(self):
        class StaleCursorChat(FakeChat):
            def GetNewMessage(self):
                return []

        chat = StaleCursorChat()
        backend, _ = make_backend({"群A": chat})
        chat.messages.append(FakeMessage(content="被游标吞掉的消息", rid=(21, 1)))
        self.assertEqual([event["text"] for event in backend.poll()], ["被游标吞掉的消息"])

    def test_stale_detached_window_is_refreshed(self):
        class StaleWindowChat(FakeChat):
            def GetAllMessage(self):
                return []

        chat = StaleWindowChat()
        backend, fake = make_backend({"群A": chat})
        fake.chats["群A"] = FakeChat([FakeMessage(content="窗口恢复了", rid=(31, 1))])
        self.assertEqual([event["text"] for event in backend.poll()], ["窗口恢复了"])


class SendTest(unittest.TestCase):
    def test_text_uses_the_detached_chat_when_available(self):
        chat = FakeChat()
        backend, fake = make_backend({"群A": chat})
        self.assertEqual(backend.send("群A", "hi", at="张三"), {"ok": True})
        self.assertEqual(chat.texts, [("hi", "张三")])
        self.assertEqual(fake.sent, [])

    def test_rejects_unknown_group(self):
        backend, _ = make_backend({"群A": FakeChat()})
        with self.assertRaises(ValueError):
            backend.send("群B", "hi")

    def test_at_failure_sends_plain_text(self):
        chat = AtFailsChat()
        backend, _ = make_backend({"群A": chat})
        self.assertEqual(backend.send("群A", "hi", at="未知"), {"ok": True})
        self.assertEqual(chat.texts, [("hi", None)])

    def test_file_send_waits_for_delivery(self):
        chat = FakeChat()
        backend, _ = make_backend({"群A": chat})
        result = backend.send_sticker("群A", "a.gif", "id1")
        self.assertEqual(result, {"ok": True, "delivered": True, "stickerId": "id1"})
        self.assertEqual(chat.files, ["a.gif"])

    def test_voice_send_uses_native_audio_api(self):
        chat = FakeChat()
        backend, _ = make_backend({"群A": chat})
        result = backend.send_voice("群A", "voice.wav")
        self.assertEqual(result, {"ok": True, "delivered": True, "voice": True})
        self.assertEqual(chat.audios, ["voice.wav"])

    def test_delivery_timeout_is_an_error(self):
        class SilentChat(FakeChat):
            def GetAllMessage(self):
                return []

            def SendFiles(self, filepath):
                self.files.append(filepath)
                return {"status": "成功"}

        backend, _ = make_backend({"群A": SilentChat()})
        backend.delivery_wait_seconds = 0.3
        with self.assertRaises(RuntimeError):
            backend.send_file("群A", "x.png")

    def test_failed_response_raises(self):
        class BadChat(FakeChat):
            def SendFiles(self, filepath):
                return {"status": "失败", "message": "nope"}

        backend, _ = make_backend({"群A": BadChat()})
        with self.assertRaises(RuntimeError):
            backend.send_file("群A", "x.png")


class FallbackSendBox:
    def __init__(self, chat, who="群A"):
        self.chat = chat
        self.who = who
        self.input_field = object()
        self.msg_list = object()
        self.texts = []
        self.files = []
        self.ats = []
        self.at_fails = False
        self.clears = 0

    def send_text(self, text, clear=True):
        self.texts.append(text)
        return {"status": "成功"}

    def send_file(self, filepath):
        self.files.append(filepath)
        msg_type = "image" if str(filepath).lower().endswith((".gif", ".png", ".jpg", ".jpeg")) else "file"
        self.chat.pending.append(FakeMessage(
            msg_type=msg_type, attr="self", content="[文件]", rid=(4, 100 + len(self.files)),
        ))
        return {"status": "成功"}

    def input_at(self, at):
        self.ats.append(at)
        return {"status": "失败", "message": "@部分失败"} if self.at_fails else {"status": "成功"}

    def clear_edit(self):
        self.clears += 1
        return True


class FallbackTest(unittest.TestCase):
    def test_uses_main_window_when_detached_window_is_unavailable(self):
        class NoSub(FakeWeChat):
            def __init__(self):
                super().__init__({})
                self.switched = []
                self.pending = []
                self.box = FallbackSendBox(self)
                self.core = SimpleNamespace(
                    control=SimpleNamespace(NativeWindowHandle=1),
                    get_chatbox=lambda: self.box,
                )

            def GetSubWindow(self, group):
                return None

            def ChatWith(self, group, exact=False):
                self.switched.append(group)
                return True

            def GetAllMessage(self):
                return list(self.pending)

            def GetNewMessage(self):
                fresh, self.pending = self.pending, []
                return fresh

        fake = NoSub()
        backend = WxAutoX4Backend(["群A"], wechat_factory=lambda debug=False: fake)
        self.assertIn("群A", backend.fallback)
        self.assertEqual(backend.health()["listening"], ["群A"])
        self.assertEqual(backend.health()["fallback"], ["群A"])
        self.assertEqual(backend.health()["errors"], {})
        # 第一轮切进群时只补种子，不把窗口里的旧消息当新消息转发。
        fake.pending.append(FakeMessage(content="主窗口旧消息", rid=(4, 4)))
        self.assertEqual(backend.poll(), [])
        fake.pending.append(FakeMessage(content="主窗口消息", rid=(4, 5)))
        self.assertEqual([event["text"] for event in backend.poll()], ["主窗口消息"])
        self.assertEqual(fake.switched[-1], "群A")
        # 兜底模式不再走会静默切错的 wx.SendMsg/SendFiles，而是先可靠切群，
        # 再直接操作当前会话的输入框。
        with patch.object(WxAutoX4Backend, "_activate_window", return_value=True):
            self.assertEqual(backend.send("群A", "hi", at="张三"), {"ok": True})
            self.assertEqual(fake.box.ats, ["张三"])
            self.assertEqual(fake.box.texts, ["hi"])
            self.assertEqual(fake.sent, [])
            # 未知发送者 @ 失败时不能把整条回复丢掉。
            fake.box.at_fails = True
            self.assertEqual(backend.send("群A", "hello", at="未知"), {"ok": True})
            self.assertEqual(fake.box.ats[-1], "未知")
            self.assertEqual(fake.box.texts[-1], "hello")
            self.assertGreaterEqual(fake.box.clears, 1)
            result = backend.send_file("群A", "a.gif", what="图片")
        self.assertEqual(result, {"ok": True, "delivered": True})
        self.assertEqual(fake.box.files, ["a.gif"])


class ComRetryTest(unittest.TestCase):
    def test_rebuilds_once_on_com_event_error(self):
        class FlakyChat(FakeChat):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def SendAudio(self, filepath):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("COMError: (-2147220991, '事件无法调用任何订户')")
                return super().SendAudio(filepath)

        first = FlakyChat()
        second = FakeChat()
        chats = {"群A": first}
        created = []

        def factory(debug=False):
            created.append(1)
            return FakeWeChat(chats)

        backend = WxAutoX4Backend(["群A"], wechat_factory=factory)
        backend.delivery_wait_seconds = 0.5
        chats["群A"] = second          # 重建后 GetSubWindow 拿到新窗口
        result = backend.send_voice("群A", "v.wav")
        self.assertTrue(result["voice"])
        self.assertEqual(second.audios, ["v.wav"])
        self.assertEqual(len(created), 2)  # 初始一次 + 重建一次

    def test_other_errors_are_not_retried(self):
        class BrokenChat(FakeChat):
            def SendAudio(self, filepath):
                raise RuntimeError("boom")

        backend, _ = make_backend({"群A": BrokenChat()})
        with self.assertRaises(RuntimeError):
            backend.send_voice("群A", "v.wav")


class VoiceControl:
    def __init__(self, name="", rect=(0, 0, 10, 10)):
        self.Name = name
        self.ClassName = "mmui::XButton"
        left, top, right, bottom = rect
        self.BoundingRectangle = SimpleNamespace(left=left, top=top, right=right, bottom=bottom)

    def GetChildren(self):
        return []


class FakeVoiceView:
    def __init__(self):
        self.cancel = VoiceControl("取消")

    def GetChildren(self):
        return [self.cancel]


class FakeVoiceBox:
    """Stand-in for ChatBox's press-and-talk controls."""

    def __init__(self, chat, who="群A"):
        self.chat = chat
        self.who = who
        self.input_field = object()
        self.msg_list = object()
        self.in_voice = False
        self.pending = False
        self.button = VoiceControl("发语音")
        self.view = FakeVoiceView()
        self.area = VoiceControl()
        self.send = VoiceControl("发送语音")

    def voice_button(self, timeout=0.0):
        return None if self.in_voice else self.button

    def voice_record_view(self, timeout=0.0):
        return self.view if self.in_voice else None

    def record_area(self, view):
        return self.area

    def voice_send_button(self, view):
        return self.send if self.pending else None

    def _leave_voice_mode(self):
        self.in_voice = False


class FakeCable:
    def check(self):
        return True, ""

    def play(self, filepath):
        return 1.0


class VoiceChat(FakeChat):
    def __init__(self):
        super().__init__()
        self.box = FakeVoiceBox(self)

    def _chatbox(self):
        return self.box


def physical_voice_env(chat, *, box, deliver=True, close_on_send=True, pending_after_hold=True):
    """Patch the desktop/audio bits and simulate the user's clicks."""

    def click(control, hwnd):
        if control is box.button:
            box.in_voice = True
        elif control is box.send:
            box.pending = False
            if close_on_send:
                box.in_voice = False
            if deliver:
                chat.delivered.append(FakeMessage(
                    msg_type="voice", attr="friend", content='语音1"秒',
                    rid=(9, len(chat.delivered) + 1),
                ))
        elif control is box.view.cancel:
            box.in_voice = False

    def hold(area, hwnd, cable, filepath, duration, errors):
        box.pending = pending_after_hold

    return [
        patch.object(WxAutoX4Backend, "_activate_window", return_value=True),
        patch.object(WxAutoX4Backend, "_window_handle", return_value=1),
        patch.object(WxAutoX4Backend, "_current_chat_name",
                     side_effect=lambda: getattr(chat, "current", box.who)),
        patch.object(WxAutoX4Backend, "_voice_cable", return_value=FakeCable()),
        patch.object(WxAutoX4Backend, "_wav_duration", return_value=1.0),
        patch.object(WxAutoX4Backend, "_hold_record_area", side_effect=hold),
        patch.object(WxAutoX4Backend, "_physical_click", side_effect=click),
    ]


class PhysicalVoiceTest(unittest.TestCase):
    def test_uses_press_and_talk_instead_of_the_library_api(self):
        chat = VoiceChat()
        backend, _ = make_backend({"群A": chat})
        patches = physical_voice_env(chat, box=chat.box)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        result = backend.send_voice("群A", "v.wav")
        self.assertEqual(result, {"ok": True, "delivered": True, "voice": True})
        self.assertEqual(chat.audios, [])  # SendAudio is the fallback, not the normal path
        self.assertEqual(len(chat.delivered), 1)

    def test_non_self_voice_row_still_confirms_delivery(self):
        chat = VoiceChat()
        backend, _ = make_backend({"群A": chat})
        chat.delivered.append(FakeMessage(msg_type="voice", attr="friend", content='语音2"秒', rid=(1, 1)))
        before = backend._signature(chat, {"voice"})
        self.assertEqual(before, ["(1, 1)"])
        chat.delivered.append(FakeMessage(msg_type="voice", attr="friend", content='语音1"秒', rid=(1, 2)))
        after = backend._signature(chat, {"voice"})
        self.assertIn("(1, 2)", after)
        self.assertNotIn("(1, 2)", before)

    def test_timeout_leaves_voice_mode_and_raises(self):
        chat = VoiceChat()
        backend, _ = make_backend({"群A": chat})
        backend.delivery_wait_seconds = 0.2
        patches = physical_voice_env(chat, box=chat.box, deliver=False, close_on_send=False)
        patches.append(patch.object(media_mod, "VOICE_SEND_TIMEOUT", 0.3))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.assertRaises(RuntimeError):
            backend.send_voice("群A", "v.wav")
        self.assertFalse(chat.box.in_voice)


class FallbackVoiceTest(unittest.TestCase):
    def _main_window(self, box):
        class Main:
            def __init__(self):
                self.nickname = "F"
                self.box = box
                self.messages = []
                self.delivered = self.messages
                self.chat_withs = []
                self.audio_calls = []
                self.current = ""
                self.core = SimpleNamespace(
                    control=SimpleNamespace(NativeWindowHandle=1),
                    get_chatbox=lambda: self.box,
                )

            def GetSubWindow(self, group):
                return None

            def GetNewMessage(self):
                return []

            def GetAllMessage(self):
                return list(self.messages)

            def ChatWith(self, group, exact=False):
                self.chat_withs.append((group, exact))
                self.box.who = group
                self.current = group
                return True

            def SendAudio(self, filepath, who=None, exact=False):
                self.audio_calls.append((filepath, who, exact))
                self.messages.append(FakeMessage(
                    msg_type="voice", attr="friend", content='语音1"秒',
                    rid=(2, len(self.messages) + 1),
                ))
                return {"status": "成功"}

        return Main()

    def _backend(self, main):
        backend = WxAutoX4Backend(["群A"], wechat_factory=lambda debug=False: main)
        backend.delivery_wait_seconds = 0.5
        return backend

    def test_physical_path_switches_the_main_window_first(self):
        main = self._main_window(FakeVoiceBox(None, who="群A"))
        backend = self._backend(main)
        with patch.object(WxAutoX4Backend, "_activate_window", return_value=True):
            for item in physical_voice_env(main, box=main.box):
                item.start()
                self.addCleanup(item.stop)
            result = backend.send_voice("群A", "v.wav")
        self.assertEqual(result["voice"], True)
        self.assertEqual(main.chat_withs, [("群A", True)])
        self.assertEqual(main.audio_calls, [])

    def test_switch_main_chat_clicks_the_session_row(self):
        class Session:
            nickname = "群A"
            control = VoiceControl(rect=(100, 100, 200, 140))

        main = self._main_window(FakeVoiceBox(None, who="群A"))
        main.current = ""
        main.GetSession = lambda: [Session()]
        backend = self._backend(main)
        clicks = []

        def click_at(x, y, hwnd):
            clicks.append((x, y, hwnd))
            main.current = "群A"

        with patch.object(WxAutoX4Backend, "_activate_window", return_value=True), \
             patch.object(WxAutoX4Backend, "_point_in_window", return_value=True), \
             patch.object(WxAutoX4Backend, "_physical_click_at", side_effect=click_at), \
             patch.object(WxAutoX4Backend, "_current_chat_name", side_effect=lambda: main.current):
            box = backend._switch_main_chat("群A")
        self.assertIs(box, main.box)
        self.assertEqual(clicks, [(150, 120, 1)])

    def test_fallback_send_audio_gets_who_and_exact(self):
        bare = SimpleNamespace(who="群A", input_field=object(), msg_list=object())
        main = self._main_window(bare)
        backend = self._backend(main)
        with patch.object(WxAutoX4Backend, "_activate_window", return_value=True), \
             patch.object(WxAutoX4Backend, "_current_chat_name", side_effect=lambda: main.current):
            result = backend.send_voice("群A", "v.wav")
        self.assertEqual(result, {"ok": True, "delivered": True, "voice": True})
        self.assertEqual(main.audio_calls, [("v.wav", "群A", True)])



class AvatarBox:
    def __init__(self, image):
        self.image = image

    def _capture_client(self):
        return self.image, 0, 0


class AvatarChat(FakeChat):
    def __init__(self, image):
        super().__init__()
        self.box = AvatarBox(image)

    def _chatbox(self):
        return self.box


class AvatarSenderTest(unittest.TestCase):
    @staticmethod
    def _message():
        control = SimpleNamespace(BoundingRectangle=SimpleNamespace(left=0, top=0, right=200, bottom=100))
        return SimpleNamespace(control=control)

    @staticmethod
    def _avatar(first, second):
        image = Image.new("RGB", (40, 40), first)
        image.paste(second, (0, 0, 20, 20))
        return image

    def test_avatar_hash_identifies_friend_and_self(self):
        friend = Image.new("RGB", (200, 100), "white")
        friend.paste(self._avatar((255, 0, 0), (0, 0, 0)), (16, 4))
        backend, _ = make_backend({"群A": AvatarChat(friend)})
        key1, self1 = backend._sender_from_avatar("群A", self._message())
        key2, self2 = backend._sender_from_avatar("群A", self._message())
        self.assertFalse(self1)
        self.assertTrue(key1.startswith("用户#"))
        self.assertEqual(key1, key2)

        other = Image.new("RGB", (200, 100), "white")
        other.paste(self._avatar((0, 255, 0), (0, 0, 0)), (16, 4))
        backend2, _ = make_backend({"群A": AvatarChat(other)})
        key3, self3 = backend2._sender_from_avatar("群A", self._message())
        self.assertFalse(self3)
        self.assertNotEqual(key1, key3)

        own = Image.new("RGB", (200, 100), "white")
        own.paste(self._avatar((0, 0, 255), (0, 0, 0)), (140, 4))
        backend3, _ = make_backend({"群A": AvatarChat(own)})
        key4, self4 = backend3._sender_from_avatar("群A", self._message())
        self.assertTrue(self4)
        self.assertIsNone(key4)


class CursorPreservingChat(FakeChat):
    def __init__(self):
        super().__init__()
        self.box = SimpleNamespace(used_msg_ids=set())

    def _chatbox(self):
        return self.box

    def GetAllMessage(self):
        messages = super().GetAllMessage()
        for message in messages:
            self.box.used_msg_ids.add(tuple(message.control.GetRuntimeId()))
        return messages


class MessageCursorTest(unittest.TestCase):
    def test_delivery_reads_do_not_consume_incoming_rows(self):
        chat = CursorPreservingChat()
        chat.delivered.append(FakeMessage(content="新消息", rid=(7, 1)))
        chat.box.used_msg_ids.add((1, 1))
        backend, _ = make_backend({"群A": chat})
        before = set(chat.box.used_msg_ids)
        backend._message_ids(chat)
        self.assertEqual(chat.box.used_msg_ids, before)
        backend._signature(chat, {"voice"})
        self.assertEqual(chat.box.used_msg_ids, before)


class ClipboardPayloadTest(unittest.TestCase):
    def test_hdrop_header_and_wide_paths(self):
        path = r"C:\temp\要 点b脸.png"
        payload = desktop_mod.build_hdrop_payload([path])
        self.assertEqual(struct.unpack("<I", payload[:4])[0], 20)
        self.assertEqual(struct.unpack("<I", payload[16:20])[0], 1)
        decoded = payload[20:].decode("utf-16-le")
        self.assertTrue(decoded.startswith(path))
        self.assertTrue(decoded.endswith("\x00\x00"))


if __name__ == "__main__":
    unittest.main()
