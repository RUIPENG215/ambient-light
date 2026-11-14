from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensors import BeatDetector, LevelBeatDetector, build_situation  # noqa: E402


class TestBeatDetector(unittest.TestCase):
    def test_finds_low_frequency_onsets(self):
        detector = BeatDetector(samplerate=48000, blocksize=1024)
        t = np.arange(1024) / 48000.0
        for _ in range(20):
            detector.push(np.zeros(1024))

        beats = 0
        for i in range(12):
            amp = 0.6 if i in (4, 8) else 0.01
            _, beat = detector.push(amp * np.sin(2 * np.pi * 60.0 * t))
            beats += int(beat > 0)
        self.assertGreaterEqual(beats, 1)
        self.assertGreater(detector.energy, 0.0)

    def test_level_fallback_detects_a_jump(self):
        detector = LevelBeatDetector()
        for _ in range(10):
            detector.update(-60.0)
        energy, beat = detector.update(-40.0)
        self.assertGreater(beat, 0.0)
        self.assertGreater(energy, 0.0)


class TestSituation(unittest.TestCase):
    def test_shape(self):
        situation = build_situation(
            stats={"mean_level": 10, "p95_level": 20, "max_level": 30, "trend": "steady"},
            sensors={"prox": 5, "near": False, "lux": 12, "energy": 2, "mood": "calm", "lid": 70},
            mac={"idle_s": 5, "user_present": True, "playing": False, "title": "", "artist": ""},
            music=(20, 0, True),
            lamp={"light": {"hue": 24, "val": 90, "open": 60},
                  "reason": "the room went quiet", "for_s": 12,
                  "effect": {"name": "candle", "t": 3, "seconds": 45}},
            drives={"energy": 0.7, "curiosity": 0.5, "security": 0.8, "bond": 0.3},
            memory=[{"t": "10:00", "text": "hello", "salience": 0.5}],
            events=["WAKE 3s ago"],
            uptime_min=1.0,
        )
        self.assertEqual(situation["music"]["energy"], 20)
        self.assertTrue(situation["presence"]["user_present"])
        self.assertEqual(situation["lamp"]["light"]["hue"], 24)
        self.assertEqual(situation["lamp"]["light"]["val"], 90)
        self.assertEqual(situation["lamp"]["for_s"], 12)
        self.assertEqual(situation["lamp"]["reason"], "the room went quiet")
        self.assertEqual(situation["lamp"]["effect"]["name"], "candle")
        self.assertEqual(len(situation["memory"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
