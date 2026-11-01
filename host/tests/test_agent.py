from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import DeepSeekAgent, DeviceHub, StateStore  # noqa: E402
from looks import DEFAULT_LOOK  # noqa: E402

LIGHT_CALL = {"val": 90, "hue": 24, "ease": 1200, "reason": "the room went quiet"}


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments) -> None:
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


def response(calls, content=""):
    message = SimpleNamespace(content=content, tool_calls=list(calls))
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=25)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeLink:
    def __init__(self) -> None:
        self.sent: list = []

    def send_line(self, line: str) -> bool:
        self.sent.append(line)
        return True


class FakeSensors:
    def sensors(self) -> dict:
        return {"prox": 10, "near": False, "lux": 12, "energy": 3}

    def mac_context(self) -> dict:
        return {"idle_s": 2, "user_present": True, "playing": False,
                "title": "", "artist": "", "source": ""}

    def music_bus(self):
        return 0, 0, True


def make_state():
    return SimpleNamespace(
        base_look=DEFAULT_LOOK,
        light_reason="",
        last_sent="",
        last_sent_at=0.0,
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

    def test_set_light_authors_numbers_not_a_mode(self):
        pushed = []
        self.hub.on_light = pushed.append
        result = self.hub.set_light(reason="the room went quiet", val=80, hue=24)

        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], ["hue", "val"])
        self.assertEqual(len(pushed), 1, "the stream should receive the look")
        self.assertEqual(pushed[0].val, 80)
        self.assertEqual(pushed[0].hue, 24)
        self.assertEqual(pushed[0].sat, DEFAULT_LOOK.sat,
                         "an omitted field keeps its value")
        self.assertTrue(result["line"].startswith("F,24,"))

    def test_set_light_clamps_out_of_range_numbers(self):
        self.hub.on_light = lambda look: None
        result = self.hub.set_light(reason="x", hue=999, grad=-999, open=500)
        self.assertEqual(result["params"]["hue"], 255)
        self.assertEqual(result["params"]["grad"], -128)
        self.assertEqual(result["params"]["open"], 100)

    def test_set_light_twice_accumulates(self):
        self.hub.on_light = lambda look: None
        self.hub.set_light(reason="one", val=90)
        second = self.hub.set_light(reason="two", hue=200)
        self.assertEqual(second["params"]["val"], 90, "the earlier edit persists")
        self.assertEqual(second["params"]["hue"], 200)

    def test_set_light_without_a_stream_says_so(self):
        result = self.hub.set_light(reason="x", val=100)
        self.assertFalse(result["ok"])

    def test_blink_is_a_transient_not_a_look(self):
        self.assertEqual(self.hub.blink()["line"], "K,0")
        self.assertEqual(self.hub.blink("wince")["line"], "K,1")
        self.assertEqual(self.state.base_look.val, DEFAULT_LOOK.val)

    def test_set_reflex_tunes_the_body(self):
        class FakeReflex:
            def __init__(self):
                from reflex import ReflexConfig
                self.cfg = ReflexConfig()

        self.hub.reflex = FakeReflex()
        result = self.hub.set_reflex(startle_jump=10, beat_val=60, reason="jumpy")
        self.assertTrue(result["ok"])
        self.assertEqual(self.hub.reflex.cfg.startle_jump, 10)
        self.assertEqual(self.hub.reflex.cfg.beat_val, 60.0)

    def test_set_reflex_without_a_body(self):
        self.hub.reflex = None
        self.assertFalse(self.hub.set_reflex(beat_val=60)["ok"])


class TestDeepSeekAgent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.tmp.name) / "state.json"))
        self.link = FakeLink()
        self.state = make_state()
        self.hub = DeviceHub(self.link, self.state, self.store, FakeSensors())
        self.hub.on_light = lambda look: None

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_executes_tool_calls(self):
        calls = [
            FakeToolCall("a", "set_light", LIGHT_CALL),
            FakeToolCall("b", "blink", {"kind": "blink"}),
        ]
        agent = DeepSeekAgent(client_factory=lambda: FakeClient([response(calls)]))
        turn = agent.turn({"noise": {"mean": 10}}, self.hub)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.actions, ["set_light", "blink"])
        self.assertEqual(turn.reason, "the room went quiet")
        self.assertEqual(self.state.base_look.val, 90)
        self.assertIn("K,0", self.link.sent)

    def test_a_spoken_reply_comes_back_without_tools(self):
        agent = DeepSeekAgent(
            client_factory=lambda: FakeClient([response([], content="嗯，我暗一点。")])
        )
        turn = agent.turn({"said": "暗一点"}, self.hub)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.reply, "嗯，我暗一点。")
        self.assertEqual(turn.actions, [])

    def test_observe_gets_a_second_round(self):
        calls = [
            FakeToolCall("o", "observe", {"kind": "all"}),
            FakeToolCall("m", "set_light", LIGHT_CALL),
        ]
        client = FakeClient([response([calls[0]]), response([calls[1]])])
        agent = DeepSeekAgent(client_factory=lambda: client)
        turn = agent.turn({"noise": {"mean": 10}}, self.hub)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.actions, ["observe", "set_light"])
        self.assertEqual(len(client.chat.completions.calls), 2)
        second = client.chat.completions.calls[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" and "sensors" in m.get("content", "")
                            for m in second))

    def test_bad_tool_arguments_do_not_kill_the_turn(self):
        calls = [FakeToolCall("x", "set_light", "{not json")]
        agent = DeepSeekAgent(client_factory=lambda: FakeClient([response(calls)]))
        turn = agent.turn({}, self.hub)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.actions, ["set_light"])

    def test_failures_open_a_circuit(self):
        class Boom(FakeCompletions):
            def create(self, **kwargs):
                raise RuntimeError("offline")

        agent = DeepSeekAgent(
            client_factory=lambda: FakeClient([]),
        )
        agent._client = SimpleNamespace(chat=SimpleNamespace(completions=Boom([])))
        for _ in range(3):
            self.assertIsNone(agent.turn({}, self.hub))
        self.assertGreater(agent.open_until, time.monotonic())


if __name__ == "__main__":
    unittest.main(verbosity=2)
