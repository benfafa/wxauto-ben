import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.backends.wxauto_backend import WxAutoBackend  # noqa: E402


class FakeUIA:
    def Exists(self, timeout=0):
        return True


class FakeChat:
    def __init__(self, who):
        self.who = who
        self.UIAAPI = FakeUIA()
        self.messages = []
        self.fail_next = False

    def GetNewMessage(self):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("transient UIA failure")
        messages = self.messages
        self.messages = []
        return messages


class FakeMessage:
    def __init__(self, msg_type="friend", sender="张三", content="@小助手 你好", msg_id="m1"):
        self.type = msg_type
        self.sender = sender
        self.content = content
        self.id = msg_id


class FakeWeChat:
    last = None

    def __init__(self, *args, **kwargs):
        FakeWeChat.last = self
        self.nickname = "机器人账号"
        self.listen = {}
        self.sent = []
        self.windows = {}

    def AddListenChat(self, who):
        chat = FakeChat(who)
        self.listen[who] = chat
        self.windows[who] = chat

    def GetAllListenChat(self):
        return self.listen

    def SendMsg(self, msg, who=None, at=None):
        self.sent.append({"who": who, "msg": msg, "at": at})
        return getattr(self, "send_result", None)

    def SendFiles(self, filepath, who=None):
        self.sent.append({"who": who, "file": filepath})
        return getattr(self, "send_result", None)


class WxAutoBackendTest(unittest.TestCase):
    def setUp(self):
        module = types.ModuleType("wxauto")
        module.WeChat = FakeWeChat
        self._previous = sys.modules.get("wxauto")
        sys.modules["wxauto"] = module
        self.backend = WxAutoBackend(["群A"])

    def tearDown(self):
        if self._previous is None:
            sys.modules.pop("wxauto", None)
        else:
            sys.modules["wxauto"] = self._previous


    def test_own_emotion_and_time_rows_are_filtered_by_attr(self):
        chat = self.backend.chats["群A"]
        chat.messages = [
            V4Message(msg_type="emotion", attr="self", content="动画表情", msg_id="e1"),
            V4Message(msg_type="text", attr="friend", sender="李四", content="你好", msg_id="e2"),
            V4Message(msg_type="time", attr="system", content="23:10", msg_id="e3"),
        ]
        events = self.backend.poll()
        self.assertEqual([event["id"] for event in events], ["e2"])
    def test_message_conversion_and_self_filter(self):
        chat = self.backend.chats["群A"]
        chat.messages = [
            FakeMessage(),
            FakeMessage(msg_type="self", sender="Self", content="自己的回复", msg_id="m2"),
            FakeMessage(msg_type="sys", sender="SYS", content="系统消息", msg_id="m3"),
            FakeMessage(content="[图片]", msg_id="m4"),
        ]
        events = self.backend.poll()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["group"], "群A")
        self.assertEqual(events[0]["sender"], "张三")
        self.assertEqual(events[0]["text"], "@小助手 你好")
        self.assertEqual(events[0]["kind"], "text")
        self.assertEqual(events[1]["kind"], "image")
        self.assertNotIn("_imageMessage", events[1])

    def test_send_forwards_group_text_and_at(self):
        self.backend.send("群A", "你好", "张三")
        self.assertEqual(FakeWeChat.last.sent, [{"who": "群A", "msg": "你好", "at": "张三"}])
        with self.assertRaises(ValueError):
            self.backend.send("别的群", "你好")

    def test_legacy_sticker_has_no_exact_and_false_is_failure(self):
        self.assertEqual(self.backend.send_sticker("群A", "happy.png", "happy"),
                         {"ok": True, "stickerId": "happy", "delivered": True})
        self.assertEqual(FakeWeChat.last.sent[-1], {"who": "群A", "file": "happy.png"})
        FakeWeChat.last.send_result = True
        self.backend.send_sticker("群A", "happy.png", "happy")
        FakeWeChat.last.send_result = False
        with self.assertRaises(RuntimeError):
            self.backend.send_sticker("群A", "happy.png", "happy")
        with self.assertRaises(RuntimeError):
            self.backend.send("群A", "text")
        with self.assertRaises(ValueError):
            self.backend.send_sticker("其他群", "happy.png", "happy")

    def test_wxauto4_exact_and_response_failures_for_text_and_stickers(self):
        calls = []
        response = {"status": "成功", "message": None, "data": None}

        def send_files(filepath, who=None, exact=False):
            calls.append((filepath, who, exact))
            return response

        def send_msg(text, who=None, at=None, exact=False):
            calls.append((text, who, at, exact))
            return response

        self.backend.mode = "wxauto4"
        self.backend.wx = types.SimpleNamespace(SendFiles=send_files, SendMsg=send_msg)
        self.backend.send_sticker("群A", "happy.png", "happy")
        self.backend.send("群A", "text", "张三")
        self.assertEqual(calls, [("happy.png", "群A", True), ("text", "群A", "张三", True)])
        for response in [False, None, {}, {"ok": False}, {"status": "失败"}, {"status": "错误"}]:
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    self.backend.send_sticker("群A", "happy.png", "happy")
                with self.assertRaises(RuntimeError):
                    self.backend.send("群A", "text")

    def test_installed_wxresponse_without_importing_or_constructing_wechat(self):
        import importlib.metadata
        import importlib.util
        try:
            file = importlib.metadata.distribution("wxauto4").locate_file("wxauto4/param.py")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("wxauto4 is not installed; response shapes covered by the stub test")
        spec = importlib.util.spec_from_file_location("isolated_wxauto4_param", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.backend.mode = "wxauto4"
        self.backend._check_send_result(module.WxResponse.success())
        for response in [module.WxResponse.failure("failed"), module.WxResponse.error("error")]:
            self.assertFalse(response.is_success)
            with self.assertRaises(RuntimeError):
                self.backend._check_send_result(response)

    def test_transient_poll_error_is_recorded_then_cleared(self):
        chat = self.backend.chats["群A"]
        chat.fail_next = True
        self.assertEqual(self.backend.poll(), [])
        self.assertIn("GetNewMessage", self.backend.health()["errors"]["群A"])

        chat.messages = [FakeMessage(msg_id="m9")]
        events = self.backend.poll()
        self.assertEqual(len(events), 1)
        self.assertNotIn("群A", self.backend.health()["errors"])

    def test_health_reports_nickname_and_windows(self):
        health = self.backend.health()
        self.assertEqual(health["selfNickname"], "机器人账号")
        self.assertEqual(health["listening"], ["群A"])


class SubWindowFake:
    """群子窗口：SendFiles 不接受 who，读消息用它自己的窗口。"""

    def __init__(self, messages=None, raises=False):
        self.calls = []
        self.messages = list(messages or [])
        self.raises = raises

    def SendFiles(self, filepath):
        self.calls.append(filepath)
        if self.raises:
            raise RuntimeError("子窗口已关闭")
        return {"status": "成功", "message": None, "data": None}

    def GetAllMessage(self):
        return [FakeMessage(msg_type="self", content=f"m{i}", msg_id=f"s{i}") for i in range(len(self.messages))]


class V4Message:
    """wxauto4 41.1.7 的真实形状：attr 是「谁发的」，type 是「什么类型」。"""

    def __init__(self, msg_type="text", attr="friend", sender="张三", content="", msg_id="m"):
        self.type = msg_type
        self.attr = attr
        self.sender = sender
        self.content = content
        self.id = msg_id


class GrowingSubWindow:
    def __init__(self, self_attr=True):
        self.calls = []
        self.self_attr = self_attr
        self.messages = [V4Message(msg_type="emotion", attr="self", content="动画表情", msg_id="s0")]

    def SendFiles(self, filepath):
        self.calls.append(filepath)
        self.messages.append(V4Message(msg_type="emotion", attr=self.self_attr and "self" or "friend",
                                       content="动画表情", msg_id="s%d" % len(self.messages)))
        return {"status": "成功", "message": None, "data": None}

    def GetAllMessage(self):
        return list(self.messages)

class StickerDeliveryTest(unittest.TestCase):
    def setUp(self):
        module = types.ModuleType("wxauto")
        module.WeChat = FakeWeChat
        self._previous = sys.modules.get("wxauto")
        sys.modules["wxauto"] = module
        self.backend = WxAutoBackend(["群A"])
        self.backend.mode = "wxauto4"
        self.backend.delivery_wait_seconds = 0
        self.main_calls = []

        def send_files(filepath, who=None, exact=False):
            self.main_calls.append((filepath, who, exact))
            return {"status": "成功", "message": None, "data": None}

        self.backend.wx = types.SimpleNamespace(SendFiles=send_files, SendMsg=lambda **kw: None)

    def tearDown(self):
        if self._previous is None:
            sys.modules.pop("wxauto", None)
        else:
            sys.modules["wxauto"] = self._previous

    def test_prefers_the_group_subwindow(self):
        sub = SubWindowFake(messages=["a"])
        self.backend.chats["群A"] = sub
        self.backend.send_sticker("群A", "happy.png", "happy")
        self.assertEqual(sub.calls, ["happy.png"])
        self.assertEqual(self.main_calls, [])

    def test_unconfirmed_delivery_is_an_error(self):
        sub = SubWindowFake()
        sub.GetAllMessage = lambda: []          # 发送前后都没有新消息
        self.backend.chats["群A"] = sub
        self.backend.delivery_wait_seconds = 0.2
        with self.assertRaises(RuntimeError):
            self.backend.send_sticker("群A", "happy.png", "happy")

    def test_wxauto4_attr_shape_confirms_delivery(self):
        sub = GrowingSubWindow()
        self.backend.chats["群A"] = sub
        self.backend.delivery_wait_seconds = 0.5
        result = self.backend.send_sticker("群A", "001.gif", "001")
        self.assertEqual(sub.calls, ["001.gif"])
        self.assertTrue(result["delivered"])

    def test_someone_elses_new_message_does_not_confirm_my_send(self):
        sub = GrowingSubWindow(self_attr=False)
        self.backend.chats["群A"] = sub
        self.backend.delivery_wait_seconds = 0.3
        with self.assertRaises(RuntimeError):
            self.backend.send_sticker("群A", "001.gif", "001")

    def test_falls_back_to_main_window_when_subwindow_is_gone(self):
        self.backend.chats["群A"] = SubWindowFake(raises=True)
        self.backend.send_sticker("群A", "happy.png", "happy")
        self.assertEqual(self.main_calls, [("happy.png", "群A", True)])
        self.assertNotIn("群A", self.backend.chats)
        self.assertIn("SendFiles", self.backend.health()["errors"]["群A"])


if __name__ == "__main__":
    unittest.main()
