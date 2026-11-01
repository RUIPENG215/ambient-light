from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import DeepSeekAgent, DeviceHub, StateStore  # noqa: E402

MOOD_CALL = {
    "mood": "alert",
    "hue": 8,
    "speed": 230,
    "brightness": 200,
    "lid": 95,
    "reason": "sudden bang",
}


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict | str) -> None:
        self.id = call_id
        self.function = FakeFunction(
            name, arguments if isinstance(arguments, str) else json.dumps(arguments)
        )


class FakeCompletions:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise RuntimeError("no scripted response left")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def response(calls):
    message = SimpleNamespace(content="", tool_calls=list(calls))
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=25)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeLink:
    def __init__(self) -> None:
        self.sent: list = []

    def send_mood(self, decision) -> bool:
        self.sent.append(decision.serial_line())
        return True

    def send_lid(self, *args) -> bool:
        self.sent.append(("LID",) + args)
        return True

    def send_coupling(self, **kwargs) -> bool:
        self.sent.append(("C", kwargs))
        return True

    def send_reflex(self, *args) -> bool:
        self.sent.append(("R",) + args)
        return True


class FakeSensors:
    def sensors(self) -> dict:
        return {"prox": 10, "near": False, "lux": 12, "energy": 3, "mood": "calm", "lid": 70}

    def mac_context(self) -> dict:
        return {
            "idle_s": 2,
            "user_present": True,
            "playing": False,
            "title": "",
            "artist": "",
            "source": "",
        }

    def music_bus(self):
        return 0, 0, True


def make_state():
    return SimpleNamespace(
        mood="calm",
        mood_since=time.monotonic(),
        lid=70,
        history=deque(maxlen=8),
        last=None,
        last_sent="",
        last_sent_at=0.0,
        last_decision_at=0.0,
    )


class TestDeviceHub(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.tmp.name) / "state.json"))
        self.link = FakeLink()
        self.state = make_state()
        self.hub = DeviceHub(self.link, self.state, self.store, FakeSensors())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_set_mood_and_lid(self):
        result = self.hub.set_mood("alert", 8, 230, 200, 95, "bang")
        self.assertTrue(result["ok"])
        self.assertEqual(self.link.sent[-1], "M,alert,8,230,200,95")
        self.assertEqual(self.state.mood, "alert")
        self.assertEqual(self.state.lid, 95)

        self.hub.set_lid(40, 900, "hold")
        self.assertEqual(self.link.sent[-1][0], "LID")
        self.assertEqual(self.link.sent[-1][1], 40)
        self.assertEqual(self.state.lid, 40)

    def test_invalid_mood_raises_before_sending(self):
        with self.assertRaises(ValueError):
            self.hub.set_mood("angry", 0, 0, 0, 0)
        self.assertEqual(self.link.sent, [])


class TestDeepSeekAgent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.tmp.name) / "state.json"))
        self.link = FakeLink()
        self.state = make_state()
        self.hub = DeviceHub(self.link, self.state, self.store, FakeSensors())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_executes_tool_calls(self):
        calls = [
            FakeToolCall("a", "set_mood", MOOD_CALL),
            FakeToolCall("b", "set_lid", {"open": 40, "ms": 900, "mode": "hold"}),
        ]
        agent = DeepSeekAgent(client_factory=lambda: FakeClient([response(calls)]))
        turn = agent.turn({"noise": {"mean": 10}}, self.hub)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.actions, ["set_mood", "set_lid"])
        self.assertEqual(turn.reason, "sudden bang")
        self.assertIn("M,alert,8,230,200,95", self.link.sent)
        self.assertEqual(self.state.mood, "alert")

    def test_observe_gets_a_second_round(self):
        calls = [
            FakeToolCall("o", "observe", {"kind": "all"}),
            FakeToolCall("m", "set_mood", MOOD_CALL),
        ]
        client = FakeClient([response([calls[0]]), response([calls[1]])])
        agent = DeepSeekAgent(client_factory=lambda: client)
        turn = agent.turn({"noise": {"mean": 10}}, self.hub)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.actions, ["observe", "set_mood"])
        self.assertEqual(len(client.chat.completions.calls), 2)
        second_messages = client.chat.completions.calls[1]["messages"]
        self.assertTrue(
            any(m.get("role") == "tool" and "sensors" in m.get("content", "")
                for m in second_messages)
        )

    def test_bad_arguments_do_not_kill_the_turn(self):
        calls = [FakeToolCall("x", "set_mood", {"mood": "angry"})]
        agent = DeepSeekAgent(client_factory=lambda: FakeClient([response(calls)]))
        turn = agent.turn({}, self.hub)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.actions, ["set_mood"])
        self.assertEqual(self.link.sent, [])

    def test_api_failure_returns_none(self):
        class Boom:
            def create(self, **kwargs):
                raise RuntimeError("offline")

        class BoomClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=Boom())

        agent = DeepSeekAgent(client_factory=lambda: BoomClient())
        self.assertIsNone(agent.turn({}, self.hub))
        self.assertEqual(agent.failures, 1)

    def test_hourly_cap(self):
        calls = [FakeToolCall("a", "set_mood", MOOD_CALL)]
        agent = DeepSeekAgent(
            max_calls_per_hour=1,
            client_factory=lambda: FakeClient([response(calls), response(calls)]),
        )
        self.assertIsNotNone(agent.turn({}, self.hub))
        self.assertIsNone(agent.turn({}, self.hub))
        self.assertEqual(agent.last_skip, "hourly cap")

    def test_force_tool_choice_and_thinking_off(self):
        calls = [FakeToolCall("a", "set_mood", MOOD_CALL)]
        client = FakeClient([response(calls)])
        agent = DeepSeekAgent(client_factory=lambda: client)
        agent.turn({}, self.hub)
        kwargs = client.chat.completions.calls[0]
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(kwargs["model"], "deepseek-flash")


class TestStateStore(unittest.TestCase):
    def test_feel_clamps_and_memory_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(str(path))
            store.feel({"energy": -5.0, "bond": 0.1})
            self.assertEqual(store.drives["energy"], 0.0)
            store.remember("someone knocked twice", 0.8)
            store.save()

            again = StateStore(str(path))
            self.assertEqual(len(again.memory), 1)
            self.assertIn("knocked", again.memory[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
