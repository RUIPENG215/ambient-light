from __future__ import annotations

import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looks import DEFAULT_LOOK, FIELD_ORDER, Look, LookEngine  # noqa: E402


class FakeLink:
    def __init__(self) -> None:
        self.lines: list[tuple[float, str]] = []
        self.fail = False

    def send_line(self, line: str) -> bool:
        if self.fail:
            return False
        self.lines.append((time.monotonic(), line))
        return True

    def frames(self) -> list[str]:
        return [line for _, line in self.lines if line.startswith("F,")]


class TestLookWire(unittest.TestCase):
    def test_line_has_every_field_in_order(self):
        line = DEFAULT_LOOK.line()
        parts = line.split(",")
        self.assertEqual(parts[0], "F")
        self.assertEqual(len(parts) - 1, len(FIELD_ORDER))
        self.assertTrue(all(p.lstrip("-").isdigit() for p in parts[1:]))

    def test_values_are_clamped_not_rejected(self):
        wild = Look(hue=999, sat=-5, val=300, grad=-999, wave_n=99,
                    open=500, ease=99999)
        fields = wild.fields()
        self.assertEqual(fields["hue"], 255)
        self.assertEqual(fields["sat"], 0)
        self.assertEqual(fields["val"], 255)
        self.assertEqual(fields["grad"], -128)
        self.assertEqual(fields["wave_n"], 16)
        self.assertEqual(fields["open"], 100)
        self.assertEqual(fields["ease"], 10000)

    def test_motion_only_touches_the_fields_it_should(self):
        look = replace(replace(DEFAULT_LOOK, hue=100, hue_drift=6.0), hue=250.0, hue_drift=10.0)
        moved = look.evolved(3.0)
        self.assertEqual(int(moved.hue), 24)          # 250 + 30, wrapped
        self.assertEqual(moved.val, look.val)
        self.assertEqual(look.evolved(0.0), look)

    def test_the_default_body_is_usable(self):
        self.assertLessEqual(DEFAULT_LOOK.val, 255)
        self.assertGreater(DEFAULT_LOOK.breath_hz, 0, "it should breathe")
        self.assertGreater(DEFAULT_LOOK.grad, 0, "and it should not be flat")


class TestLookEngine(unittest.TestCase):
    def test_streams_at_roughly_the_requested_rate(self):
        link = FakeLink()
        engine = LookEngine(link, hz=25.0)
        engine.start("ember", DEFAULT_LOOK)
        time.sleep(0.4)
        engine.stop()

        self.assertFalse(engine.running)
        self.assertTrue(link.lines[0][1].startswith("G,"), "G is sent once first")
        frames = link.frames()
        self.assertGreater(len(frames), 3)
        span = link.lines[-1][0] - link.lines[0][0]
        self.assertGreater(len(frames) / span, 15.0)

    def test_retarget_without_dropping_the_stream(self):
        link = FakeLink()
        engine = LookEngine(link, hz=25.0)
        engine.start("ember", DEFAULT_LOOK)
        time.sleep(0.15)
        engine.set("tide", replace(replace(DEFAULT_LOOK, hue=150, val=95), ease=100))
        time.sleep(0.2)
        engine.stop()

        hues = {int(line.split(",")[1]) for line in link.frames()}
        self.assertIn(30, hues, "it started as ember")
        self.assertGreaterEqual(max(hues), 140, "and reached tide")
        self.assertEqual(engine.status()["name"], "tide")
        g_lines = [line for _, line in link.lines if line.startswith("G,")]
        self.assertEqual(len(g_lines), 1, "config is sent once, not per look")

    def test_a_dead_serial_port_does_not_kill_the_thread(self):
        link = FakeLink()
        link.fail = True
        engine = LookEngine(link, hz=25.0)
        engine.start("ember", DEFAULT_LOOK)
        time.sleep(0.2)
        engine.stop()
        self.assertEqual(engine.frames, 0)

    def test_reflex_disables_the_board_coupling(self):
        link = FakeLink()
        from reflex import Reflex

        engine = LookEngine(link, hz=25.0, reflex=Reflex())
        engine.start("ember", DEFAULT_LOOK)
        time.sleep(0.15)
        engine.stop()
        sent = [line for _, line in link.lines]
        self.assertEqual(sent[0], "G,80,0,1")
        self.assertEqual(sent[1], "C,0,0,0,0",
                         "the board must not add its own coupling on top")

    def test_the_agent_ease_is_honoured_in_the_host(self):
        """A 3 s glide must not arrive in 200 ms."""
        link = FakeLink()
        engine = LookEngine(link, hz=25.0)
        engine.start("ember", replace(DEFAULT_LOOK, val=120, ease=0))
        time.sleep(0.1)
        engine.set("slow", replace(DEFAULT_LOOK, val=240, ease=3000))
        time.sleep(0.2)
        engine.stop()

        vals = [int(line.split(",")[3]) for line in link.frames()]
        self.assertLess(max(vals), 200, "3000 ms ease must still be climbing")
        self.assertGreater(max(vals), 120, "but it must have started moving")

    def test_stop_is_idempotent(self):
        engine = LookEngine(FakeLink())
        engine.stop()
        engine.stop()
        self.assertFalse(engine.running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
