from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ambient_host import LampState, apply_decision  # noqa: E402
from audio_meter import AudioMeter  # noqa: E402
from brain import (  # noqa: E402
    Decision,
    LLMBrain,
    RateLimiter,
    build_context,
    fingerprint,
)
from feather_link import parse_line  # noqa: E402


class TestProtocol(unittest.TestCase):
    def test_sensor_line(self):
        msg = parse_line("S,12,345,8,43,curious")
        self.assertIsNotNone(msg)
        self.assertEqual(msg.kind, "sensor")
        self.assertEqual(msg.sensor.prox, 12)
        self.assertEqual(msg.sensor.als, 345)
        self.assertEqual(msg.sensor.lux, 8.0)
        self.assertEqual(msg.sensor.energy, 43)
        self.assertEqual(msg.sensor.mood, "curious")

    def test_event_with_detail(self):
        msg = parse_line("E,WAKE,prox")
        self.assertEqual(msg.kind, "event")
        self.assertEqual(msg.name, "WAKE")
        self.assertEqual(msg.detail, "prox")

    def test_pong_and_garbage(self):
        self.assertEqual(parse_line("PONG").kind, "pong")
        self.assertIsNone(parse_line("hello"))
        self.assertIsNone(parse_line("S,1,2"))
        self.assertIsNone(parse_line("S,a,b,c,d,calm"))


class TestDecision(unittest.TestCase):
    def test_clamps_and_validates(self):
        d = Decision.from_tool_input(
            {
                "mood": "ALERT",
                "hue": 999,
                "speed": -5,
                "louver": 150,
                "brightness": 300,
                "reason": "too\nloud   outside",
                "duration_s": 1,
            }
        )
        self.assertEqual(d.mood, "alert")
        self.assertEqual((d.hue, d.speed, d.louver, d.brightness), (255, 0, 100, 255))
        self.assertEqual(d.duration_s, 5)
        self.assertEqual(d.reason, "too loud outside")

    def test_unknown_mood(self):
        with self.assertRaises(ValueError):
            Decision("angry")

    def test_serial_line(self):
        d = Decision("alert", 8, 230, 90, 235)
        self.assertEqual(d.serial_line(), "M,alert,8,230,235,90")


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
        # median ignores the single -900 outlier
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
        m._push(-40.0)          # 20 dB above baseline -> level 100
        stats = m.stats(60.0)
        self.assertEqual(stats["frames"], 6)
        self.assertIn(stats["trend"], ("rising", "falling", "steady"))
        self.assertEqual(stats["max_level"], 100)


class FakeMessages:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        block = SimpleNamespace(type="tool_use", name="set_mood", input=self.payload)
        usage = SimpleNamespace(input_tokens=700, output_tokens=80)
        return SimpleNamespace(content=[block], usage=usage)


class FakeClient:
    def __init__(self, payload=None, exc=None):
        self.messages = FakeMessages(payload=payload, exc=exc)


class TestLLMBrain(unittest.TestCase):
    def test_success_forces_tool(self):
        payload = {
            "mood": "curious",
            "hue": 150,
            "speed": 150,
            "louver": 70,
            "reason": "hand near",
        }
        fake = FakeClient(payload=payload)
        brain = LLMBrain(min_interval_s=0.0, client_factory=lambda: fake)
        decision = brain.decide({"noise": {"mean": 10}})

        self.assertIsNotNone(decision)
        self.assertEqual(decision.mood, "curious")
        self.assertEqual(decision.source, "llm")
        self.assertEqual(brain.limiter.used, 1)
        self.assertEqual(brain.tokens_in, 700)

        kwargs = fake.messages.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["tool_choice"], {"type": "tool", "name": "set_mood"})
        self.assertEqual(kwargs["tools"][0]["name"], "set_mood")

    def test_timeout_returns_none_and_counts(self):
        fake = FakeClient(exc=TimeoutError("timed out"))
        brain = LLMBrain(min_interval_s=0.0, client_factory=lambda: fake)
        self.assertIsNone(brain.decide({}))
        self.assertEqual(brain.failures, 1)
        self.assertEqual(brain.limiter.used, 1)

    def test_circuit_breaker(self):
        fake = FakeClient(exc=RuntimeError("offline"))
        brain = LLMBrain(min_interval_s=0.0, client_factory=lambda: fake)
        for _ in range(3):
            brain.decide({})
        self.assertGreater(brain.open_until, 0.0)
        self.assertFalse(brain.gate()[0])

    def test_hourly_cap(self):
        payload = {"mood": "calm", "hue": 150, "speed": 80, "louver": 30}
        fake = FakeClient(payload=payload)
        brain = LLMBrain(min_interval_s=0.0, max_calls_per_hour=1, client_factory=lambda: fake)
        self.assertIsNotNone(brain.decide({}))
        self.assertIsNone(brain.decide({}))
        self.assertEqual(brain.last_skip, "hourly cap")


class TestContext(unittest.TestCase):
    def test_context_and_fingerprint(self):
        stats = {"mean_level": 42, "p95_level": 60, "max_level": 88, "trend": "rising"}
        ctx = build_context(
            stats,
            prox=210,
            lux=12,
            mood="calm",
            mood_since_s=30,
            history=["calm 10s", "alert 4s"],
            baseline_db=-52.3,
        )
        self.assertEqual(ctx["noise"]["mean"], 42)
        self.assertTrue(ctx["prox"]["near"])
        self.assertEqual(ctx["lamp"]["since_s"], 30)
        self.assertEqual(len(ctx["history"]), 2)
        self.assertEqual(ctx["noise"]["baseline_dbfs"], -52.3)
        self.assertEqual(len(fingerprint(ctx)), 4)


class FakeLink:
    def __init__(self):
        self.sent = []

    def send_mood(self, decision):
        self.sent.append(decision.serial_line())
        return True


class TestApplyDecision(unittest.TestCase):
    def test_dedupes_within_30s_and_records_history(self):
        state = LampState()
        link = FakeLink()
        d = Decision("alert", 8, 230, 90, 235, "loud", 30)

        self.assertTrue(apply_decision(state, link, d))
        self.assertEqual(state.mood, "alert")
        self.assertFalse(apply_decision(state, link, d))
        self.assertEqual(len(link.sent), 1)

        quiet = Decision("calm", 150, 80, 30, 180, "quiet", 45)
        self.assertTrue(apply_decision(state, link, quiet))
        self.assertEqual(state.mood, "calm")
        self.assertEqual(len(state.history), 2)   # calm -> alert -> calm
        self.assertTrue(state.history[-1].startswith("alert "))

    def test_resent_after_30s_keeps_firmware_ttl_alive(self):
        state = LampState()
        link = FakeLink()
        d = Decision("calm", 150, 80, 30, 180)
        apply_decision(state, link, d)
        state.last_sent_at = time.monotonic() - 31.0
        self.assertTrue(apply_decision(state, link, d))
        self.assertEqual(len(link.sent), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
