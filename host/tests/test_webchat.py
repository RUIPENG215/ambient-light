from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webchat  # noqa: E402
from webchat import ChatEngine  # noqa: E402


class TestChatEngine(unittest.TestCase):
    """The page owns no model: it forwards to the host's agent."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = ChatEngine(socket_path="/tmp/does-not-matter.sock")
        self.engine.history_path = Path(self.tmp.name) / "history.json"
        self.engine.history = []
        self.sent: list[dict] = []
        self.reply = {"ok": True, "reply": "mm, a little dimmer.", "actions": ["set_light"]}
        webchat.control_send = self._fake_send

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_send(self, request, path, timeout=20.0):
        self.sent.append(dict(request))
        return dict(self.reply)

    def test_chat_forwards_to_the_host_agent(self):
        reply = self.engine.chat("a little darker")
        self.assertEqual(reply, "mm, a little dimmer.")
        self.assertEqual(self.sent[-1]["cmd"], "say")
        self.assertEqual(self.sent[-1]["text"], "a little darker")
        self.assertEqual(len(self.engine.history), 2)

    def test_chat_when_the_agent_is_off_says_so(self):
        self.reply = {"ok": False, "offline": True, "error": "no agent"}
        reply = self.engine.chat("hello")
        self.assertIn("reflexes", reply)

    def test_chat_when_the_host_is_down_says_so(self):
        def boom(*a, **k):
            raise FileNotFoundError("no socket")

        webchat.control_send = boom
        reply = self.engine.chat("are you there")
        self.assertIn("lost the thread", reply)

    def test_buttons_write_numbers(self):
        self.reply = {"ok": True, "line": "F,30,200,70,28,1,30,8,12,55,55,2500",
                      "params": {}, "changed": ["val"]}
        result = self.engine.action("darker")
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent[-1]["cmd"], "light")
        self.assertEqual(self.sent[-1]["val"], 70)

    def test_unknown_button_is_an_error(self):
        result = self.engine.action("sleepy")
        self.assertFalse(result["ok"])
        self.assertIn("unknown action", result["error"])

    def test_history_is_persisted(self):
        self.engine.chat("first line")
        again = ChatEngine(socket_path="/tmp/x.sock")
        again.history_path = self.engine.history_path
        again._load_history()
        self.assertTrue(any(m.get("text") == "first line" for m in again.history))


if __name__ == "__main__":
    unittest.main(verbosity=2)
