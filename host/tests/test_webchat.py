from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webchat import ChatEngine  # noqa: E402


class TestChatEngineFallback(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = ChatEngine(force_offline=True)
        self.engine.history_path = Path(self.tmp.name) / "history.json"
        self.engine.history = []
        self.actions: list[str] = []
        self.engine.action = lambda name: (self.actions.append(name) or {"ok": True})
        self.engine.status = lambda: {
            "online": True,
            "lamp": {"mood": "curious", "lid": 85},
            "sensors": {"lux": 12, "prox": 0},
            "mac": {"user_present": True, "idle_s": 3, "playing": False},
            "music": {"energy": 0, "beat": 0},
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_status_intent(self):
        reply = self.engine.chat("你现在怎么样")
        self.assertIn("好奇", reply)
        self.assertIn("85", reply)

    def test_sleepy_intent_calls_action(self):
        reply = self.engine.chat("困了")
        self.assertEqual(self.actions, ["sleepy"])
        self.assertIn("困", reply)

    def test_release_intent(self):
        reply = self.engine.chat("恢复自主")
        self.assertEqual(self.actions, ["release"])
        self.assertIn("自己", reply)

    def test_unknown_without_key(self):
        reply = self.engine.chat("你好呀")
        self.assertIn("key", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
