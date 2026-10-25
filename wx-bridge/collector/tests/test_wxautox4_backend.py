"""Adapter tests for the wxautox4-open backend (no WeChat, no UIA)."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
        self.control = None

    def GetNewMessage(self):
        fresh, self.messages = self.messages, []
        return fresh

    def GetAllMessage(self):
        return list(self.messages) + list(self.delivered)

    def SendFiles(self, filepath):
        self.files.append(filepath)
        self.delivered.append(FakeMessage(attr="self", content="[文件]", rid=(9, len(self.delivered))))
        return {"status": "成功"}

    def SendAudio(self, filepath):
        self.audios.append(filepath)
        self.delivered.append(FakeMessage(msg_type="voice", attr="self", content='语音3"秒',
                                          rid=(9, len(self.delivered))))
        return {"status": "成功"}


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
        message = FakeMessage(content="只发一次", rid=(5, 5))
        chat = FakeChat()
        backend, _ = make_backend({"群A": chat})
        chat.messages.append(message)
        self.assertEqual(len(backend.poll()), 1)
        chat.messages.append(FakeMessage(content="只发一次", rid=(5, 5)))
        self.assertEqual(backend.poll(), [])
        # 内容相同但控件不同（真的又发了一条）仍然要转发。
        chat.messages.append(FakeMessage(content="只发一次", rid=(5, 6)))
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

            def GetNewMessage(self):
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


class SendTest(unittest.TestCase):
    def test_text_uses_exact_group_target(self):
        backend, fake = make_backend({"群A": FakeChat()})
        self.assertEqual(backend.send("群A", "hi", at="张三"), {"ok": True})
        self.assertEqual(fake.sent, [("hi", "群A", "张三", True)])

    def test_rejects_unknown_group(self):
        backend, _ = make_backend({"群A": FakeChat()})
        with self.assertRaises(ValueError):
            backend.send("群B", "hi")

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


class FallbackTest(unittest.TestCase):
    def test_uses_main_window_when_detached_window_is_unavailable(self):
        class NoSub(FakeWeChat):
            def __init__(self):
                super().__init__({})
                self.switched = []
                self.pending = []

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
        self.assertEqual(backend.health()["errors"], {})
        fake.pending.append(FakeMessage(content="主窗口消息", rid=(4, 4)))
        self.assertEqual([event["text"] for event in backend.poll()], ["主窗口消息"])
        self.assertEqual(fake.switched[-1], "群A")
        # 兜底模式下发送也要走主窗口的 who 参数。
        self.assertEqual(backend.send("群A", "hi"), {"ok": True})
        self.assertEqual(fake.sent[-1], ("hi", "群A", None, True))


if __name__ == "__main__":
    unittest.main()
