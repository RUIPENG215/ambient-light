from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ambient_host import LampState  # noqa: E402
from agent import StateStore  # noqa: E402
from control import ControlServer  # noqa: E402
from feather_link import Sensors  # noqa: E402
from lampctl import send  # noqa: E402


class FakeLink:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.sensors = Sensors(prox=12, lux=18.0, energy=3, mood="host", lid=70)
        self.events: list = []

    def send_line(self, line: str) -> bool:
        self.lines.append(line)
        return True

    def recent_events(self, window_s: float = 60.0):
        return self.events


class FakeSensors:
    def __init__(self, link: FakeLink) -> None:
        self.link = link

    def mac_context(self) -> dict:
        return {"idle_s": 5, "user_present": True, "playing": False,
                "title": "", "artist": "", "source": ""}

    def music_bus(self):
        return 0, 0, True


class TestControlServer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = f"/tmp/al-test-{os.getpid()}-{id(self)}.sock"
        self.link = FakeLink()
        self.sensors = FakeSensors(self.link)
        self.store = StateStore(str(Path(self.tmp.name) / "state.json"))
        self.state = LampState()
        self.said: list[str] = []
        self.server = ControlServer(
            self.link, self.state, self.store, self.sensors, self.socket_path,
            senses=lambda: {"level": 12.0, "music": 3, "beat": 0,
                            "prox": 12, "lux": 18.0, "present": True, "idle_s": 5},
            say=lambda text: (self.said.append(text) or
                              {"ok": True, "reply": "mm.", "actions": []}),
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def test_ping_and_observe(self):
        self.assertTrue(send({"cmd": "ping"}, self.socket_path)["pong"])
        data = send({"cmd": "observe"}, self.socket_path)
        self.assertTrue(data["ok"])
        self.assertEqual(data["sensors"]["prox"], 12)
        self.assertEqual(data["senses"]["level"], 12.0)
        self.assertIn("light", data["lamp"])

    def test_light_writes_numbers_and_streams(self):
        response = send({"cmd": "light", "val": 90, "hue": 24}, self.socket_path)
        self.assertTrue(response["ok"])
        self.assertEqual(response["changed"], ["hue", "val"])

        time.sleep(0.25)
        frames = [line for line in self.link.lines if line.startswith("F,")]
        self.assertGreater(len(frames), 3)
        last = frames[-1].split(",")
        self.assertEqual(last[1], "24")
        self.assertEqual(last[3], "90")
        self.assertEqual(self.state.base_look.val, 90)

    def test_light_keeps_fields_it_was_not_given(self):
        send({"cmd": "light", "val": 90}, self.socket_path)
        response = send({"cmd": "light", "hue": 200}, self.socket_path)
        self.assertEqual(response["params"]["val"], 90)
        self.assertEqual(response["params"]["hue"], 200)

    def test_starting_the_stream_sends_the_render_config(self):
        send({"cmd": "light", "val": 100}, self.socket_path)
        time.sleep(0.2)
        self.assertIn("G,80,0,1", self.link.lines)

    def test_blink_is_a_transient(self):
        response = send({"cmd": "blink", "kind": "wince"}, self.socket_path)
        self.assertTrue(response["ok"])
        self.assertEqual(response["line"], "K,1")
        self.assertIn("K,1", self.link.lines)

    def test_say_reaches_the_agent(self):
        response = send({"cmd": "say", "text": "a little darker"}, self.socket_path)
        self.assertTrue(response["ok"])
        self.assertEqual(response["reply"], "mm.")
        self.assertEqual(self.said, ["a little darker"])

    def test_say_without_text_is_an_error(self):
        response = send({"cmd": "say", "text": "  "}, self.socket_path)
        self.assertFalse(response["ok"])
        self.assertIn("say needs", response["error"])

    def test_unknown_command_is_an_error_not_a_crash(self):
        response = send({"cmd": "mood", "mood": "calm"}, self.socket_path)
        self.assertFalse(response["ok"])
        self.assertIn("unknown command", response["error"])
        self.assertTrue(send({"cmd": "ping"}, self.socket_path)["pong"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
