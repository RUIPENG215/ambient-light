from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from effects import EFFECTS, EffectPlayer, aurora, candle, heartbeat, ocean  # noqa: E402
from looks import DEFAULT_LOOK, FIELD_RANGE  # noqa: E402


def in_range(look) -> bool:
    return all(
        FIELD_RANGE[name][0] <= getattr(look, name) <= FIELD_RANGE[name][1]
        for name in FIELD_RANGE
    )


class TestEffects(unittest.TestCase):
    def test_registry_has_the_four_legible_ones(self):
        self.assertEqual(set(EFFECTS), {"candle", "heartbeat", "ocean", "aurora"})

    def test_candle_flickers_within_range(self):
        a = candle(DEFAULT_LOOK, 1.00, 1.0)
        b = candle(DEFAULT_LOOK, 1.25, 1.0)
        self.assertTrue(in_range(a))
        self.assertGreater(abs(a.val - b.val) + abs(a.hue - b.hue), 0.5)

    def test_heartbeat_has_a_thump_and_a_gap(self):
        thump = heartbeat(DEFAULT_LOOK, 0.20, 1.0)
        gap = heartbeat(DEFAULT_LOOK, 0.60, 1.0)
        self.assertGreater(thump.val, gap.val + 20.0)
        self.assertTrue(in_range(thump))

    def test_ocean_moves_toward_teal(self):
        look = ocean(DEFAULT_LOOK, 1.0, 1.0)
        self.assertGreater(look.hue, 120.0)
        self.assertLess(look.hue, 190.0)
        self.assertGreater(look.wave_n, 2.0)
        self.assertTrue(in_range(look))

    def test_aurora_drifts_over_seconds(self):
        a = aurora(DEFAULT_LOOK, 0.0, 1.0)
        b = aurora(DEFAULT_LOOK, 8.7, 1.0)
        self.assertGreater(abs(a.hue - b.hue), 20.0)
        self.assertTrue(in_range(a) and in_range(b))

    def test_zero_intensity_is_the_base(self):
        self.assertEqual(candle(DEFAULT_LOOK, 3.0, 0.0), DEFAULT_LOOK)


class TestPlayer(unittest.TestCase):
    def test_fades_in_and_out(self):
        player = EffectPlayer()
        player.start("candle", seconds=10.0, intensity=1.0, now=0.0)
        at_start = player.apply(DEFAULT_LOOK, 0.0)
        middle = player.apply(DEFAULT_LOOK, 5.0)
        self.assertEqual(at_start, DEFAULT_LOOK, "the first frame is the base")
        self.assertNotEqual(middle, DEFAULT_LOOK)

    def test_expires_on_its_own(self):
        player = EffectPlayer()
        player.start("heartbeat", seconds=4.0, intensity=1.0, now=0.0)
        self.assertIsNotNone(player.status(now=2.0))
        self.assertEqual(player.apply(DEFAULT_LOOK, 4.5), DEFAULT_LOOK)
        self.assertIsNone(player.status(now=4.5))

    def test_unknown_effect_is_rejected(self):
        with self.assertRaises(ValueError):
            EffectPlayer().start("disco")

    def test_stop_clears_it(self):
        player = EffectPlayer()
        player.start("ocean", seconds=60.0, now=0.0)
        player.stop()
        self.assertIsNone(player.status(now=1.0))
        self.assertEqual(player.apply(DEFAULT_LOOK, 1.0), DEFAULT_LOOK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
