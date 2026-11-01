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
        self.sent: list = []
        self.sensors = Sensors(prox=12, lux=18.0, energy=3, mood="calm", lid=70)
        self.events: list = []

    def send_mood(self, decision) -> bool:
        self.sent.append(decision.serial_line())
        return True

    def send_lid(self, *args) -> bool:
        self.sent.append(("LID",) + args)
        return True

    def recent_events(self, window_s: float = 60.0):
        return self.events


class FakeSensors:
    def __init__(self, link: FakeLink) -> None:
        self.link = link

    def mac_context(self) -> dict:
        return {
            "idle_s": 5,
            "user_present": True,
            "playing": False,
            "title": "",
            "artist": "",
            "source": "",
        }

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
        self.server = ControlServer(
            self.link, self.state, self.store, self.sensors, self.socket_path
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
        self.assertEqual(data["lamp"]["lid"], 70)

    def test_mood_takes_a_lease(self):
        response = send(
            {
                "cmd": "mood",
                "mood": "curious",
                "hue": 150,
                "speed": 90,
                "bright": 180,
                "lid": 85,
                "seconds": 30,
            },
            self.socket_path,
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["line"], "M,curious,150,90,180,85")
        self.assertEqual(self.state.mood, "curious")
        self.assertEqual(self.state.lid, 85)
        self.assertGreater(self.state.manual_until, time.monotonic())

        data = send({"cmd": "observe"}, self.socket_path)
        self.assertEqual(data["lamp"]["mood"], "curious")
        self.assertEqual(data["lamp"]["lid"], 85)

    def test_lid_sends_mood_then_lid(self):
        response = send(
            {"cmd": "lid", "open": 20, "ms": 1500, "mode": "hold", "seconds": 10},
            self.socket_path,
        )
        self.assertTrue(response["ok"])
        self.assertEqual(self.link.sent[0], "M,calm,150,90,180,20")
        self.assertEqual(self.link.sent[1][0], "LID")
        self.assertEqual(self.link.sent[1][1], 20)
        self.assertEqual(self.state.lid, 20)

    def test_release_clears_lease(self):
        send({"cmd": "mood", "mood": "alert", "seconds": 60}, self.socket_path)
        self.assertGreater(self.state.manual_until, 0)
        send({"cmd": "release"}, self.socket_path)
        self.assertEqual(self.state.manual_until, 0.0)
        self.assertIsNone(self.state.manual_last)

    def test_bad_mood_is_an_error_not_a_crash(self):
        response = send({"cmd": "mood", "mood": "angry"}, self.socket_path)
        self.assertFalse(response["ok"])
        self.assertIn("unknown mood", response["error"])
        self.assertTrue(send({"cmd": "ping"}, self.socket_path)["pong"])

    def test_flow_cycles_hue_until_release(self):
        response = send(
            {
                "cmd": "flow",
                "seconds": 1,
                "speed": 10,
                "bright": 150,
                "lid": 80,
                "hue": 0,
                "hue_step": 20,
                "interval_ms": 50,
            },
            self.socket_path,
        )
        self.assertTrue(response["ok"])
        time.sleep(0.3)
        send({"cmd": "release"}, self.socket_path)

        lines = [
            line
            for line in self.link.sent
            if isinstance(line, str) and line.startswith("M,calm,")
        ]
        self.assertGreaterEqual(len(lines), 2)
        hues = {int(line.split(",")[2]) for line in lines}
        self.assertGreater(len(hues), 1)
        self.assertEqual(self.state.manual_until, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
