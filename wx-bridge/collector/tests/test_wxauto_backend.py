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

    def __init__(self):
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
        self.assertEqual(events[1]["kind"], "media")

    def test_send_forwards_group_text_and_at(self):
        self.backend.send("群A", "你好", "张三")
        self.assertEqual(FakeWeChat.last.sent, [{"who": "群A", "msg": "你好", "at": "张三"}])
        with self.assertRaises(ValueError):
            self.backend.send("别的群", "你好")

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


if __name__ == "__main__":
    unittest.main()
