from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looks import DEFAULT_LOOK  # noqa: E402
from reflex import Reflex, ReflexConfig, Senses, as_senses  # noqa: E402

BASE = DEFAULT_LOOK
DT = 0.04  # 25 Hz


def settle(reflex: Reflex, senses: Senses, frames: int = 60) -> None:
    for i in range(frames):
        reflex.frame(BASE, senses, DT, i * DT)


class TestAudio(unittest.TestCase):
    def test_a_loud_room_breathes_deeper_than_a_quiet_one(self):
        quiet, loud = Reflex(), Reflex()
        settle(quiet, Senses(level=5))
        settle(loud, Senses(level=90))
        q, _ = quiet.frame(BASE, Senses(level=5), DT, 3.0)
        l, _ = loud.frame(BASE, Senses(level=90), DT, 3.0)
        self.assertGreaterEqual(l.breath_amp, q.breath_amp)
        self.assertGreater(l.wave_amp, q.wave_amp + 10)
        self.assertGreater(l.val, q.val)

    def test_breath_stays_inside_the_wire_range(self):
        r = Reflex()
        look, _ = r.frame(BASE, Senses(level=100), DT, 0.0)
        self.assertLessEqual(look.breath_amp, 100.0)
        self.assertLessEqual(look.val, 255.0)


class TestBeat(unittest.TestCase):
    def test_a_beat_lifts_val_and_then_decays(self):
        r = Reflex()
        hit, _ = r.frame(BASE, Senses(beat=100), DT, 0.0)
        after1, _ = r.frame(BASE, Senses(beat=0), DT, 0.04)
        after2, _ = r.frame(BASE, Senses(beat=0), DT, 0.08)
        self.assertGreater(hit.val, BASE.val + 20)
        self.assertLess(after1.val, hit.val)
        self.assertLess(after2.val, after1.val)

    def test_a_weak_onset_is_ignored(self):
        r = Reflex()
        look, _ = r.frame(BASE, Senses(beat=10), DT, 0.0)
        self.assertAlmostEqual(look.val, BASE.val, delta=1.0)


class TestMusic(unittest.TestCase):
    def test_headphones_music_still_moves_the_lamp(self):
        """The microphone is quiet, the system-audio tap is not."""
        quiet, playing = Reflex(), Reflex()
        settle(quiet, Senses(level=0, music=0))
        settle(playing, Senses(level=0, music=80))
        q, _ = quiet.frame(BASE, Senses(level=0, music=0), DT, 3.0)
        p, _ = playing.frame(BASE, Senses(level=0, music=80), DT, 3.0)
        self.assertGreater(p.hue_cycles, 80, "music opens the ring into a rainbow")
        self.assertLess(abs(p.grad), 3, "and the seamed gradient folds away")
        self.assertEqual(q.hue_cycles, 0)
        kick, _ = playing.frame(BASE, Senses(level=0, music=80, bass=100), DT, 3.04)
        self.assertGreater(kick.val, q.val + 40)

    def test_music_makes_the_whole_ring_dance_not_spin(self):
        r = Reflex()
        settle(r, Senses(music=90, level=40))
        gap, _ = r.frame(BASE, Senses(music=90, level=40, bass=0), DT, 3.0)
        hit, _ = r.frame(BASE, Senses(music=90, level=40, bass=100), DT, 3.04)
        self.assertLess(gap.val, BASE.val * 0.6, "dim between kicks")
        self.assertGreater(hit.val, gap.val + 120, "thrown up by the kick")
        self.assertLess(abs(hit.wave_hz), abs(BASE.wave_hz) * 1.2, "the flow does not speed up")
        self.assertLess(hit.wave_amp, BASE.wave_amp * 0.4, "and mostly stops")
        self.assertLessEqual(hit.ease, 40.0)

    def test_a_strong_kick_turns_the_colours_one_notch(self):
        r = Reflex()
        settle(r, Senses(music=70))
        before, _ = r.frame(BASE, Senses(music=70), DT, 3.0)
        r.frame(BASE, Senses(music=70, bass=100), DT, 3.04)
        r.frame(BASE, Senses(music=70, bass=90), DT, 3.08)
        after = before
        for i in range(3, 14):
            after, _ = r.frame(BASE, Senses(music=70), DT, 3.0 + i * DT)
        turned = (after.hue - before.hue) % 256
        self.assertAlmostEqual(turned, 20.0, delta=3.0, msg="one notch per kick, not per frame")

    def test_no_sparks_while_music_plays(self):
        """The microphone hears the speakers; that must not read as a voice."""
        r = Reflex()
        settle(r, Senses(music=70, level=80), frames=250)   # the rainbow is fully in
        look, _ = r.frame(BASE, Senses(music=70, level=80, treble=100), DT, 10.0)
        self.assertLess(look.sparkle, 1.0)
        room = Reflex()
        settle(room, Senses(music=0, level=80))
        talk, _ = room.frame(BASE, Senses(music=0, level=80), DT, 3.0)
        self.assertGreater(talk.sparkle, 30, "a voice still makes sparks")

    def test_only_the_hardest_kicks_go_white(self):
        r = Reflex()
        settle(r, Senses(music=90), frames=250)
        medium, _ = r.frame(BASE, Senses(music=90, bass=65), DT, 10.0)
        hard, _ = r.frame(BASE, Senses(music=90, bass=100), DT, 10.04)
        self.assertAlmostEqual(medium.sat, BASE.sat, delta=1.0, msg="a normal kick keeps its colour")
        self.assertLess(hard.sat, BASE.sat * 0.2, "the hardest one is white")
        quiet = Reflex()
        settle(quiet, Senses())
        look, _ = quiet.frame(BASE, Senses(bass=100), DT, 3.0)
        self.assertAlmostEqual(look.sat, BASE.sat, delta=1.0, msg="never without music")

    def test_the_eye_opens_with_the_kick(self):
        r = Reflex()
        settle(r, Senses(music=90))
        gap, _ = r.frame(BASE, Senses(music=90, bass=0), DT, 3.0)
        hit, _ = r.frame(BASE, Senses(music=90, bass=100), DT, 3.04)
        self.assertLess(gap.val, BASE.val * 0.25)
        self.assertGreater(hit.val, 200)
        self.assertGreater(hit.open, 95)

    def test_room_and_music_are_not_counted_twice(self):
        both = Reflex()
        settle(both, Senses(level=80, music=80))
        loud, _ = both.frame(BASE, Senses(level=80, music=80), DT, 3.0)

        tap = Reflex()
        settle(tap, Senses(level=0, music=80))
        solo, _ = tap.frame(BASE, Senses(level=0, music=80), DT, 3.0)
        self.assertAlmostEqual(loud.val, solo.val, delta=0.5)
        self.assertAlmostEqual(loud.breath_amp, solo.breath_amp, delta=0.5)

    def test_a_loud_room_alone_is_not_music(self):
        room = Reflex()
        settle(room, Senses(level=80, music=0))
        look, _ = room.frame(BASE, Senses(level=80, music=0), DT, 3.0)
        self.assertEqual(look.hue_cycles, 0)
        self.assertGreater(look.val, BASE.val, "it brightens, it does not dance")

    def test_music_opens_the_eye_a_little(self):
        quiet, playing = Reflex(), Reflex()
        settle(quiet, Senses(music=0))
        settle(playing, Senses(music=100))
        q, _ = quiet.frame(BASE, Senses(music=0), DT, 3.0)
        p, _ = playing.frame(BASE, Senses(music=100), DT, 3.0)
        self.assertGreater(p.open, q.open + 2.0)

    def test_a_louder_room_breathes_faster(self):
        quiet, loud = Reflex(), Reflex()
        settle(quiet, Senses(level=0))
        settle(loud, Senses(level=100))
        q, _ = quiet.frame(BASE, Senses(level=0), DT, 3.0)
        l, _ = loud.frame(BASE, Senses(level=100), DT, 3.0)
        self.assertGreater(l.breath_hz, q.breath_hz * 1.3)

    def test_music_sets_the_light_in_motion(self):
        quiet, loud = Reflex(), Reflex()
        settle(quiet, Senses(level=0))
        settle(loud, Senses(level=100))
        q, _ = quiet.frame(BASE, Senses(level=0), DT, 3.0)
        l, _ = loud.frame(BASE, Senses(level=100), DT, 3.0)
        self.assertGreater(l.grad, q.grad + 5, "colour should spread further")
        self.assertGreater(l.wave_amp, q.wave_amp + 10, "the wave should deepen")
        self.assertGreater(l.wave_hz, q.wave_hz * 1.3, "and travel faster")

    def test_a_kick_also_lifts_the_eye(self):
        r = Reflex()
        rest, _ = r.frame(BASE, Senses(beat=0), DT, 0.0)
        kick, _ = r.frame(BASE, Senses(beat=100), DT, 0.04)
        self.assertGreater(kick.open, rest.open + 2)

    def test_a_bass_drop_never_startles(self):
        r = Reflex()
        settle(r, Senses(level=5, music=0))
        for i in range(10):
            _, cmds = r.frame(BASE, Senses(level=5, music=100), DT, 3.0 + i * DT)
            self.assertEqual(cmds, [], "music must never trigger a startle")
        self.assertEqual(r.startles, 0)


class TestProximity(unittest.TestCase):
    def test_a_hand_opens_the_eye_and_warms_the_hue(self):
        r = Reflex()
        settle(r, Senses(prox=200))
        look, _ = r.frame(BASE, Senses(prox=200), DT, 3.0)
        self.assertGreater(look.open, BASE.open + 10)
        self.assertLess(look.hue, BASE.hue)

    def test_an_empty_room_leaves_it_alone(self):
        r = Reflex()
        settle(r, Senses(prox=0))
        look, _ = r.frame(BASE, Senses(prox=0), DT, 3.0)
        self.assertAlmostEqual(look.open, BASE.open, delta=0.5)
        self.assertAlmostEqual(look.hue, BASE.hue, delta=0.5)


class TestRoomLight(unittest.TestCase):
    def test_a_bright_room_is_brighter_than_a_dark_one(self):
        def run(lux: float) -> float:
            r = Reflex()
            look = None
            for i in range(120):
                look, _ = r.frame(BASE, Senses(lux=lux), DT, i * DT)
            return look.val

        self.assertGreater(run(300.0), run(0.0))

    def test_no_sensor_reading_does_not_dim_the_lamp(self):
        r = Reflex()
        settle(r, Senses(lux=0.0))
        look, _ = r.frame(BASE, Senses(lux=0.0), DT, 3.0)
        self.assertAlmostEqual(look.val, BASE.val, delta=2.0)


class TestIdle(unittest.TestCase):
    def test_nobody_at_the_desk_dims_it(self):
        busy, idle = Reflex(), Reflex()
        settle(busy, Senses(idle_s=0))
        settle(idle, Senses(idle_s=400))
        b, _ = busy.frame(BASE, Senses(idle_s=0), DT, 3.0)
        i, _ = idle.frame(BASE, Senses(idle_s=400), DT, 3.0)
        self.assertLess(i.val, b.val)
        self.assertLess(i.open, b.open)


class TestStartle(unittest.TestCase):
    def test_a_sudden_jump_fires_once_then_cools_down(self):
        r = Reflex()
        settle(r, Senses(level=5))
        _, cmds = r.frame(BASE, Senses(level=90), DT, 3.0)
        self.assertEqual(cmds, ["K,2,255"])

        # still loud a frame later: no second flash
        _, cmds = r.frame(BASE, Senses(level=90), DT, 3.04)
        self.assertEqual(cmds, [])

        # after the cooldown a new jump starts a new one
        settle(r, Senses(level=5), frames=10)
        _, cmds = r.frame(BASE, Senses(level=95), DT, 6.0)
        self.assertEqual(cmds, ["K,2,255"])
        self.assertEqual(r.startles, 2)

    def test_a_clap_sends_a_comet_round_the_ring(self):
        r = Reflex()
        settle(r, Senses(level=5))
        r.frame(BASE, Senses(level=90), DT, 3.0)
        look = None
        for i in range(1, 10):
            look, _ = r.frame(BASE, Senses(level=5), DT, 3.0 + i * DT)
        self.assertGreater(look.shape, 80, "a narrow band")
        self.assertGreater(look.wave_amp, 90)
        self.assertGreater(look.wave_hz, 80, "about one lap per second")
        for i in range(10, 80):
            look, _ = r.frame(BASE, Senses(level=5), DT, 3.0 + i * DT)
        self.assertLess(look.shape, 1.0, "and it is gone afterwards")

    def test_a_gradual_swell_is_not_a_startle(self):
        r = Reflex()
        for i in range(60):
            _, cmds = r.frame(BASE, Senses(level=4.0 * i), DT, i * DT)
            self.assertEqual(cmds, [], "a ramp must not startle")

    def test_quiet_is_not_a_startle(self):
        r = Reflex()
        _, cmds = r.frame(BASE, Senses(level=30), DT, 0.0)
        self.assertEqual(cmds, [])


class TestStillness(unittest.TestCase):
    def test_a_silent_room_is_almost_still(self):
        r = Reflex()
        settle(r, Senses())
        look, _ = r.frame(BASE, Senses(), DT, 3.0)
        self.assertLess(look.breath_amp, 12, "the whole lamp does not pulse")
        self.assertAlmostEqual(look.wave_amp, BASE.wave_amp, delta=1.0, msg="it flows")
        self.assertAlmostEqual(look.wave_hz, BASE.wave_hz, delta=1.0)
        self.assertEqual(look.sparkle, 0.0)

    def test_a_voice_makes_sparks_and_louder_makes_more(self):
        soft, loud = Reflex(), Reflex()
        settle(soft, Senses(level=35))
        settle(loud, Senses(level=80))
        s, _ = soft.frame(BASE, Senses(level=35), DT, 3.0)
        l, _ = loud.frame(BASE, Senses(level=80), DT, 3.0)
        self.assertGreater(s.sparkle, 8)
        self.assertGreater(l.sparkle, s.sparkle + 15)

    def test_an_effect_is_not_stilled(self):
        r = Reflex()
        r.performing = True
        settle(r, Senses())
        look, _ = r.frame(BASE, Senses(), DT, 3.0)
        self.assertAlmostEqual(look.wave_amp, BASE.wave_amp, delta=0.5)

    def test_a_reverse_flow_survives_the_reflex(self):
        r = Reflex()
        look, _ = r.frame(replace(BASE, wave_hz=-20.0), Senses(level=60), DT, 0.0)
        self.assertLess(look.wave_hz, 0.0)


class TestArrival(unittest.TestCase):
    def test_coming_back_to_the_mac_is_greeted_once(self):
        r = Reflex()
        settle(r, Senses(idle_s=90))
        look, cmds = r.frame(BASE, Senses(idle_s=1), DT, 3.0)
        self.assertEqual(cmds, ["K,0"])
        self.assertGreater(look.open, BASE.open, "it opens up at once")
        _, cmds = r.frame(BASE, Senses(idle_s=1), DT, 3.04)
        self.assertEqual(cmds, [], "one greeting per return")
        self.assertEqual(r.greetings, 1)

    def test_an_empty_desk_turns_blue_and_a_touch_brings_amber_back(self):
        r = Reflex()
        settle(r, Senses(idle_s=90))
        gone, _ = r.frame(BASE, Senses(idle_s=90), DT, 3.0)
        self.assertAlmostEqual(gone.hue, 170.0, delta=3.0)
        self.assertGreater(gone.breath_amp, 85, "asleep it dips to almost dark")
        self.assertLess(gone.wave_amp, BASE.wave_amp * 0.3, "and stops flowing")
        self.assertLess(gone.breath_hz, BASE.breath_hz * 2.6 * 0.7)
        first, _ = r.frame(BASE, Senses(idle_s=0.5), DT, 3.04)
        self.assertGreater(first.hue, 150.0, "the colour does not snap back")
        back = first
        for i in range(2, 100):
            back, _ = r.frame(BASE, Senses(idle_s=0.5), DT, 3.0 + i * DT)
        self.assertLess(abs(back.hue - BASE.hue), 10.0)

    def test_a_resting_breath_is_about_a_persons(self):
        r = Reflex()
        settle(r, Senses())
        look, _ = r.frame(BASE, Senses(), DT, 3.0)
        period = 100.0 / look.breath_hz
        self.assertGreater(period, 3.5)
        self.assertLess(period, 6.0)

    def test_someone_who_never_left_is_not_greeted(self):
        r = Reflex()
        settle(r, Senses(idle_s=6))
        _, cmds = r.frame(BASE, Senses(idle_s=0), DT, 3.0)
        self.assertEqual(cmds, [])


class TestConfigAndAdapter(unittest.TestCase):
    def test_ease_is_applied_to_reflex_frames(self):
        r = Reflex(ReflexConfig(ease=120.0))
        look, _ = r.frame(BASE, Senses(), DT, 0.0)
        self.assertEqual(look.ease, 120.0)

    def test_a_faster_look_is_not_slowed_down_by_the_reflex(self):
        r = Reflex(ReflexConfig(ease=120.0))
        look, _ = r.frame(replace(BASE, ease=40.0), Senses(), DT, 0.0)
        self.assertEqual(look.ease, 40.0, "an effect can ask for a crisper attack")

    def test_as_senses_accepts_a_host_dict(self):
        senses = as_senses({"level": 42, "beat": 80, "prox": 150,
                            "lux": 12.0, "idle_s": 30})
        self.assertEqual(senses.level, 42.0)
        self.assertEqual(senses.beat, 80)
        self.assertEqual(senses.prox, 150)

    def test_as_senses_survives_none_and_junk(self):
        self.assertEqual(as_senses(None).level, 0.0)
        self.assertEqual(as_senses(object()).prox, 0)

    def test_a_broken_sensor_value_does_not_raise(self):
        r = Reflex()
        look, cmds = r.frame(BASE, Senses(level=-50, prox=-1, lux=-9,
                                          idle_s=-5, beat=999), DT, 0.0)
        self.assertGreaterEqual(look.val, 0.0)
        self.assertLessEqual(look.val, 255.0)
        self.assertIsInstance(cmds, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
