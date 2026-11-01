from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import RateLimiter  # noqa: E402
from ambient_host import LampState, sensor_signature  # noqa: E402
from audio_meter import AudioMeter  # noqa: E402
from feather_link import parse_line  # noqa: E402


class TestProtocol(unittest.TestCase):
    def test_sensor_line(self):
        msg = parse_line("S,12,345,8,43,host,70,0")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.kind, "sensor")
        self.assertEqual(msg.sensor.prox, 12)
        self.assertEqual(msg.sensor.als, 345)
        self.assertEqual(msg.sensor.lux, 8.0)
        self.assertEqual(msg.sensor.energy, 43)
        self.assertEqual(msg.sensor.mood, "host")
        self.assertEqual(msg.sensor.lid, 70)

    def test_old_seven_field_telemetry_still_parses(self):
        msg = parse_line("S,12,345,8,43,fallback,25")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.sensor.lid, 25)

    def test_event_with_detail(self):
        msg = parse_line("E,BOOT,2.2.0")
        self.assertEqual(msg.kind, "event")
        self.assertEqual(msg.name, "BOOT")
        self.assertEqual(msg.detail, "2.2.0")

    def test_pong_and_garbage(self):
        self.assertEqual(parse_line("PONG").kind, "pong")
        self.assertIsNone(parse_line(""))
        self.assertIsNone(parse_line("what is this"))


class TestRateLimiter(unittest.TestCase):
    def test_sliding_window(self):
        now = [0.0]
        rl = RateLimiter(2, clock=lambda: now[0])
        self.assertTrue(rl.allow())
        rl.record()
        self.assertTrue(rl.allow())
        rl.record()
        self.assertFalse(rl.allow())
        now[0] = 3599.0
        self.assertFalse(rl.allow())
        now[0] = 3600.0
        self.assertTrue(rl.allow())


class TestAudioMeter(unittest.TestCase):
    def test_median_calibration(self):
        m = AudioMeter(calibration_s=0.0, span_db=20.0, simulated=True)
        for db in (-50.0, -50.0, -52.0, -48.0, -900.0):
            m._push(db)
        self.assertTrue(m.ready)
        self.assertAlmostEqual(m.baseline_db, -50.0, places=1)

    def test_level_mapping(self):
        m = AudioMeter(simulated=True)
        m._baseline = -50.0
        self.assertEqual(m._level_from_db(-60.0), 0.0)
        self.assertAlmostEqual(m._level_from_db(-41.0), 50.0, places=1)
        self.assertEqual(m._level_from_db(-20.0), 100.0)

    def test_stats_shape(self):
        m = AudioMeter(calibration_s=0.0, simulated=True)
        for _ in range(5):
            m._push(-60.0)
        m._push(-40.0)
        stats = m.stats(60.0)
        self.assertEqual(stats["frames"], 6)
        self.assertIn(stats["trend"], ("rising", "falling", "steady"))
        self.assertEqual(stats["max_level"], 100)


class TestStateShape(unittest.TestCase):
    def test_a_fresh_lamp_has_one_body(self):
        state = LampState()
        light = state.base_look.fields()
        self.assertEqual(light["hue"], 30)
        self.assertGreater(light["val"], 0)
        self.assertEqual(state.light_reason, "")

    def test_the_signature_moves_with_the_room(self):
        state = LampState(level=10.0)
        a = sensor_signature(state, {"prox": 10, "lux": 6}, {"idle_s": 5}, (0, 0, True))
        b = sensor_signature(state, {"prox": 200, "lux": 6}, {"idle_s": 5}, (0, 0, True))
        self.assertNotEqual(a, b, "a hand in front must count as a change")
        state.level = 95.0
        c = sensor_signature(state, {"prox": 10, "lux": 6}, {"idle_s": 5}, (0, 0, True))
        self.assertNotEqual(a, c, "a loud room must count as a change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
