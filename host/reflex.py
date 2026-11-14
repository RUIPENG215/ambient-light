"""Reflex layer — sensors to light, on the Mac, without the agent.

The board renders and the agent decides, but neither needs to be alive for the
lamp to react: this module reads the microphone, the system audio, the proximity
sensor, the room light and the idle time, and bends whatever the agent (or the
last manual look) asked for. It runs inside the 25 Hz frame loop, so a hand in
front of the sensor is felt in 40–120 ms, not in DeepSeek's 1–2 s.

Everything here is continuous. There are no modes: each sensor contributes a
number, and the numbers are added to the base look.

The rule that makes it read as *interactive*: a quiet, untouched lamp is almost
still. Motion is the answer to an input, so whoever made the sound or touched
the keyboard can see that they caused it.

    here    somebody at the Mac, quiet -> the base look's own slow flow
            round the ring, and almost no breathing
    music   system audio playing -> the flow stops and the whole ring dances:
            nearly dark between kicks, wide open on each one, white on the
            hardest ones (it follows
            the bass envelope, not a beat counter), no sparks, strong kicks
            turn a rainbow round the ring a notch
    sound   room mic and system audio, whichever is louder -> white sparks
            (louder = denser), and the breath and the band wake up
    beat    a kick drum lifts val for ~200 ms
    clap    an edge in the level fires a flash, then a comet runs the ring
    arrive  the Mac was idle and somebody touches it -> blink + a warm swell
    idle    nobody at the Mac for 10 s -> dims, slows and turns blue (by way
            of red and violet), asleep by 45 s; a touch brings amber back
    prox    (board sensor; it sits inside the shade, so normally silent)
    lux     a bright room lifts the floor, a dark room lets it sit low

The reflex is deliberately *additive*, not authoritative: the agent still owns
the base look. That keeps "who owns the lamp" answerable in one sentence.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:                    # looks imports us; keep this out of the cycle
    from looks import Look


@dataclass(frozen=True)
class Senses:
    """One snapshot of everything the lamp can feel. Zero is a valid state."""

    level: float = 0.0          # 0-100 microphone level (fast loop)
    music: int = 0              # 0-100 system audio (or mic fallback) energy
    beat: int = 0               # 0-100 beat onset pulse
    bass: int = 0               # 0-100 kick envelope (system audio only)
    treble: int = 0             # 0-100 hi-hat envelope (system audio only)
    prox: int = 0               # 0-255 APDS9960 proximity
    lux: float = 0.0            # estimated room light
    present: bool = True        # a person is at the computer
    idle_s: float = 0.0         # seconds since the last input


@dataclass
class ReflexConfig:
    audio_breath: float = 0.9    # how much sound deepens the breath
    audio_val: float = 0.55      # brightness per level unit
    audio_tempo: float = 0.3     # breath rate swing: 0.7x quiet .. 1.3x loud
    beat_val: float = 55.0       # val units at beat 100
    beat_wave_hz: float = 30.0   # a kick nudges the band (not while music plays)
    beat_hue_step: float = 20.0  # a strong kick turns the colours this far
    kick_level: float = 60.0     # bass envelope that counts as a strong kick
    kick_gap_s: float = 0.22     # at most one colour notch per this long
    music_floor: float = 0.12    # between kicks: this fraction of base val
    music_punch: float = 240.0   # val added at a full kick
    dance_open: float = 100.0    # and the eye opens this far with it, which
                                 # roughly doubles the swing on its own
    music_treble: float = 0.0    # sparkle at a full hi-hat (off)
    white_level: float = 78.0    # bass envelope above this starts to bleach
    white_depth: float = 0.92    # how much saturation the hardest kick removes
    white_val: float = 35.0      # and a little extra brightness with it
    music_wave: float = 0.15     # how much of the flow is left while it dances
    music_ease: float = 40.0     # crisper than the usual 100 ms
    music_on_level: float = 12.0 # system-audio level that counts as music
    music_cycles: float = 100.0  # hue_cycles while music plays: one rainbow
    music_spark: float = 0.0     # sparks left while music plays: none (the
                                 # microphone hears the speakers, not the room)
    beat_decay_s: float = 0.18
    music_open: float = 4.0      # open points when the music is loud
    beat_open: float = 6.0       # the eye opens a little on each kick
    audio_wave_amp: float = 22.0 # extra spatial contrast when it is loud
    audio_wave_hz: float = 1.2   # the travelling wave speeds up with volume
    audio_grad: float = 14.0     # and the colour spreads further round the ring
    prox_open: float = 24.0      # open points at full proximity
    prox_val: float = 18.0
    prox_hue: float = 10.0       # hue points toward warm
    prox_near: int = 120
    prox_tau_s: float = 0.25     # smoothing, so a hand does not twitch it
    lux_floor: float = 0.72      # brightness multiplier in the dark
    lux_gain: float = 0.38       # extra brightness in a bright room
    idle_dim: float = 0.45       # how much of val the idle can take away
    away_hue: float = 170.0      # the colour of an empty desk: blue
    away_breath: float = 0.6     # asleep it breathes this much slower
    away_return_s: float = 0.6   # how fast the colour comes back on a touch
    breath_rate: float = 2.6     # resting breath: 12 -> ~4.6 s per breath,
                                 # about a person's; loud -> ~2.5 s
    idle_after_s: float = 10.0   # start dimming after this long without input
    idle_full_s: float = 45.0    # and reach the full dim by here
    startle_jump: float = 26.0   # rise over the slow average
    startle_step: float = 10.0   # and a real edge: this much in one frame
    startle_level: float = 45.0  # and an absolute floor
    startle_cool_s: float = 2.0
    ease: float = 100.0          # the host already smooths; keep this short
    # --- stillness: what is left of the base motion when nothing is happening
    still_breath: float = 1.0    # fraction of breath_amp in a silent room
    breath_gain: float = 0.1     # somebody here: the flow is the motion, the
                                 # whole lamp does not pulse
    breath_gain_away: float = 1.7  # nobody here: it dips to almost dark, so
                                 # awake and asleep cannot be confused
    still_wave: float = 1.0      # fraction of wave_amp in a silent room
    still_wave_hz: float = 1.0   # fraction of wave_hz in a silent room
    away_wave: float = 0.85      # how much of the flow stops when nobody is there
    wake_level: float = 15.0     # level where the motion starts to wake
    wake_span: float = 35.0      # and this much further it is fully awake
    wake_attack_s: float = 0.15
    wake_release_s: float = 2.5  # it settles slowly, so the cause stays visible
    # --- sound -> sparks: the one thing a voice visibly does
    audio_sparkle: float = 60.0  # sparkle points at level 100
    sparkle_floor: float = 20.0  # below this the room counts as silent
    # --- clap -> a comet runs round the ring after the flash
    comet_s: float = 1.6
    comet_hz: float = 100.0      # 0.01 Hz: one lap per second
    comet_val: float = 150.0     # the comet is at least this bright
    # --- arrival: the Mac was idle, then somebody touched it
    away_after_s: float = 10.0   # idle this long counts as "gone"
    greet_val: float = 45.0
    greet_open: float = 20.0
    greet_hue: float = 8.0       # toward warm
    greet_decay_s: float = 3.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Reflex:
    """Turns Senses into a modulation of a base Look."""

    def __init__(self, config: Optional[ReflexConfig] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.cfg = config or ReflexConfig()
        self.log = logger or logging.getLogger("reflex")
        self._beat_env = 0.0
        self._prox_env = 0.0
        self._lux_env = -1.0
        self._slow = 0.0
        self._prev_level = 0.0
        self._last_startle = -1e9
        self.startles = 0
        self._act = 0.0              # 0 still .. 1 awake
        self._greet_env = 0.0
        self._away = False
        self._away_env = 0.0         # 0 here .. 1 long gone
        self._music_env = 0.0        # 0 silence .. 1 music playing
        self._prev_bass = 0.0
        self._last_kick = -1e9
        self._hue_turn = 0.0         # where the kicks have turned the colours
        self._hue_shown = 0.0
        self._comet_at = -1e9
        self.greetings = 0
        # set by the engine each frame: an effect is playing, do not still it
        self.performing = False

    # ---- one frame ----
    def frame(self, base: "Look", senses: Senses, dt: float,
              now: float = 0.0) -> "tuple[Look, list[str]]":
        """Return the look to draw this frame and any transient commands."""
        dt = _clamp(dt, 0.001, 0.5)
        cfg = self.cfg
        cmds: list[str] = []

        # --- arrival: the Mac's idle time is the presence sensor. Gone for a
        # while, then a key or the trackpad -> blink and a warm swell.
        idle_s = max(0.0, float(senses.idle_s))
        if idle_s >= cfg.away_after_s:
            self._away = True
        elif self._away and idle_s < 3.0:
            self._away = False
            self._greet_env = 1.0
            self.greetings += 1
            cmds.append("K,0")       # blink: it noticed you
            self.log.debug("greeting: back after being away")
        self._greet_env *= math.exp(-dt / max(0.05, cfg.greet_decay_s))
        if self._greet_env < 0.005:
            self._greet_env = 0.0

        # --- sound
        # The microphone hears the room; the system-audio tap hears the music
        # cleanly, which matters when headphones mean the microphone is quiet.
        # max() rather than sum, so a loud room playing loud music is not
        # counted twice.
        level = _clamp(senses.level, 0.0, 100.0)
        music = _clamp(float(senses.music), 0.0, 100.0)
        alive = max(level, music)
        loudness = alive / 100.0

        # Music: the system-audio tap is playing. Slow in, slower out, so a
        # gap between two tracks does not fold the rainbow away.
        playing = music >= cfg.music_on_level
        self._music_env += ((1.0 if playing else 0.0) - self._music_env) * (
            1.0 - math.exp(-dt / (0.8 if playing else 3.0)))
        if self._music_env < 0.005:
            self._music_env = 0.0

        # Stillness is the baseline. `drive` is 0 in a silent, untouched room
        # and 1 when something is happening; it wakes fast and settles slowly.
        target = _clamp((alive - cfg.wake_level) / max(1.0, cfg.wake_span), 0.0, 1.0)
        tau = cfg.wake_attack_s if target > self._act else cfg.wake_release_s
        self._act += (target - self._act) * (1.0 - math.exp(-dt / max(0.02, tau)))
        drive = 1.0 if self.performing else max(self._act, self._greet_env)

        depth = cfg.still_breath + (1.0 - cfg.still_breath) * drive
        # self._away_env is last frame's value; one frame late is invisible
        gain = cfg.breath_gain + (cfg.breath_gain_away - cfg.breath_gain) * self._away_env
        depth *= gain * (1.0 + 0.55 * cfg.audio_breath * loudness)
        breath_amp = _clamp(base.breath_amp * depth, 0.0, 100.0)
        # louder -> breathes faster, not only deeper
        tempo = 1.0 + cfg.audio_tempo * (loudness - 0.5) * 2.0
        breath_hz = base.breath_hz * tempo * cfg.breath_rate
        val = base.val + cfg.audio_val * alive
        # motion: the band is nearly parked when quiet, and wakes with sound
        # While music plays the flow must not speed up: a fast band reads as
        # "spinning", not as rhythm. `talk` is the loudness that is not music.
        talk = loudness * (1.0 - self._music_env)
        grad = _clamp(base.grad + cfg.audio_grad * talk, -128.0, 127.0)
        wave_amp = _clamp(
            base.wave_amp * (cfg.still_wave + (1.0 - cfg.still_wave) * drive)
            + cfg.audio_wave_amp * talk, 0.0, 100.0)
        # signed: a negative wave_hz (reverse flow) must survive the reflex
        wave_hz = _clamp(
            base.wave_hz * (cfg.still_wave_hz + (1.0 - cfg.still_wave_hz) * drive)
            * (1.0 + cfg.audio_wave_hz * talk), -255.0, 255.0)
        # sparks: the legible answer to a voice. Louder -> denser.
        spark_k = _clamp((alive - cfg.sparkle_floor)
                         / max(1.0, 100.0 - cfg.sparkle_floor), 0.0, 1.0)
        sparkle = base.sparkle + cfg.audio_sparkle * spark_k * (
            1.0 - (1.0 - cfg.music_spark) * self._music_env)
        sparkle = _clamp(sparkle, 0.0, 100.0)

        # --- startle: a rise over the slow average, once, then a cooldown.
        # Deliberately driven by the microphone only: a bass drop must never
        # make the lamp jump. This mirrors v2.1's "U never triggers STARTLE".

        self._slow += (level - self._slow) * (1.0 - math.exp(-dt / 0.6))
        rising = level - self._slow
        step = level - self._prev_level          # a slow swell has no edge
        self._prev_level = level
        if (rising >= cfg.startle_jump
                and step >= cfg.startle_step
                and level >= cfg.startle_level
                and now - self._last_startle >= cfg.startle_cool_s):
            self._last_startle = now
            self.startles += 1
            self._comet_at = now
            cmds.append("K,2,255")   # flash; the comet below follows it
            self.log.debug("startle: level=%.0f rising=%.0f", level, rising)

        # --- beat: fast attack, exponential release
        if senses.beat > 20:
            self._beat_env = max(self._beat_env, senses.beat / 100.0)
        self._beat_env *= math.exp(-dt / max(0.02, cfg.beat_decay_s))
        # Outside music (a clap rhythm, a tap on the desk) the onset is all
        # there is; while music plays the bass envelope below does the job.
        solo = 1.0 - self._music_env
        val += cfg.beat_val * self._beat_env * solo
        wave_hz += ((-1.0 if base.wave_hz < 0 else 1.0)
                    * cfg.beat_wave_hz * self._beat_env * solo)
        # a strong kick turns the colours one notch
        bass = _clamp(float(senses.bass), 0.0, 100.0)
        if (bass >= cfg.kick_level and self._prev_bass < cfg.kick_level
                and now - self._last_kick >= cfg.kick_gap_s):
            self._last_kick = now
            self._hue_turn += cfg.beat_hue_step
        self._prev_bass = bass
        self._hue_shown += (self._hue_turn - self._hue_shown) * (
            1.0 - math.exp(-dt / 0.08))

        # --- proximity: smoothed, so a hand passing by does not twitch
        near = _clamp(senses.prox / 255.0, 0.0, 1.0)
        if senses.prox >= cfg.prox_near:
            near = max(near, cfg.prox_near / 255.0)
        self._prox_env += (near - self._prox_env) * (1.0 - math.exp(-dt / cfg.prox_tau_s))
        open_pct = base.open + cfg.prox_open * self._prox_env
        open_pct += cfg.music_open * (music / 100.0)
        open_pct += cfg.beat_open * self._beat_env
        val += cfg.prox_val * self._prox_env
        hue = base.hue - cfg.prox_hue * self._prox_env
        open_pct += cfg.greet_open * self._greet_env
        val += cfg.greet_val * self._greet_env
        hue -= cfg.greet_hue * self._greet_env

        # --- room light: a bright room lifts the lamp, a dark room lets it sit
        lux = max(0.0, float(senses.lux))
        if self._lux_env < 0.0:
            self._lux_env = lux
        self._lux_env += (lux - self._lux_env) * (1.0 - math.exp(-dt / 2.0))
        if self._lux_env > 0.5:
            day = _clamp(math.log10(1.0 + self._lux_env) / 2.0, 0.0, 1.0)
            val *= cfg.lux_floor + cfg.lux_gain * day

        # --- idle: slowly get quieter when nobody is there
        idle_k = _clamp((senses.idle_s - cfg.idle_after_s)
                        / max(1.0, cfg.idle_full_s - cfg.idle_after_s), 0.0, 1.0)
        # Leaving is slow by nature (idle_k ramps); coming back is one touch,
        # so the return is smoothed here or the colour would snap.
        if idle_k >= self._away_env:
            self._away_env = idle_k
        else:
            self._away_env += (idle_k - self._away_env) * (
                1.0 - math.exp(-dt / max(0.05, cfg.away_return_s)))
            if self._away_env < 0.005:
                self._away_env = 0.0
        gone = self._away_env
        wave_amp *= 1.0 - cfg.away_wave * gone      # asleep it breathes, not flows
        val *= 1.0 - cfg.idle_dim * gone
        open_pct *= 1.0 - 0.5 * cfg.idle_dim * gone
        # An empty desk has its own colour. The short way round the wheel from
        # amber to blue runs through red and violet, like dusk.
        delta = ((cfg.away_hue - hue + 128.0) % 256.0) - 128.0
        hue = hue + delta * gone
        breath_hz *= 1.0 - (1.0 - cfg.away_breath) * gone
        breath_hz = _clamp(breath_hz, 0.0, 255.0)

        # The reflex keeps the host smoothing short, but never makes a look
        # slower than it asked for: an effect can request ease=40 for a flicker.
        ease = cfg.ease
        if 0.0 <= base.ease < ease:
            ease = base.ease

        # --- music look: one seamless rainbow round the ring (the linear
        # gradient has a seam, so it is folded away), turned by the kicks.
        m = self._music_env * (1.0 - gone)
        hue_cycles = base.hue_cycles + (cfg.music_cycles - base.hue_cycles) * m
        grad = grad * (1.0 - m)
        hue = hue + self._hue_shown * m
        # The dance itself: the whole ring sits low and is thrown up by the
        # bass. Squaring the envelope keeps the gaps dark and the hits sharp.
        punch = (bass / 100.0) ** 1.5
        dance = base.val * cfg.music_floor + cfg.music_punch * punch
        val = val + (dance * (val / max(1.0, base.val + cfg.audio_val * alive)) - val) * m
        open_pct += (cfg.dance_open - open_pct) * punch * m
        # The hardest kicks go white: the colour drains out at the top of the
        # hit and returns as it falls, so an accent stands out from the rest.
        white = _clamp((bass - cfg.white_level)
                       / max(1.0, 100.0 - cfg.white_level), 0.0, 1.0) * m
        sat = base.sat * (1.0 - cfg.white_depth * white)
        val += cfg.white_val * white
        wave_amp *= 1.0 - (1.0 - cfg.music_wave) * m
        breath_amp *= 1.0 - m
        sparkle += cfg.music_treble * (_clamp(float(senses.treble), 0.0, 100.0) / 100.0) ** 2 * m
        if m > 0.5:
            ease = min(ease, cfg.music_ease)

        # --- comet: after a clap, one bright band laps the ring and fades.
        wave_n = base.wave_n
        shape = base.shape
        t = now - self._comet_at
        if 0.0 <= t < cfg.comet_s:
            k = min(1.0, t / 0.08, (cfg.comet_s - t) / 0.5)
            direction = -1.0 if base.wave_hz < 0 else 1.0
            wave_n = wave_n + (1.0 - wave_n) * k
            wave_amp = wave_amp + (100.0 - wave_amp) * k
            shape = shape + (92.0 - shape) * k
            wave_hz = wave_hz + (direction * cfg.comet_hz - wave_hz) * k
            breath_amp = breath_amp * (1.0 - k)
            sparkle = sparkle * (1.0 - k)     # let the comet read cleanly
            val = val + (max(val, cfg.comet_val) - val) * k
            ease = min(ease, 60.0)

        frame = replace(
            base,
            hue=hue % 256.0,
            sat=_clamp(sat, 0.0, 255.0),
            val=_clamp(val, 0.0, 255.0),
            breath_amp=_clamp(breath_amp, 0.0, 100.0),
            breath_hz=breath_hz,
            grad=grad,
            wave_n=wave_n,
            hue_cycles=_clamp(hue_cycles, 0.0, 400.0),
            wave_amp=_clamp(wave_amp, 0.0, 100.0),
            wave_hz=_clamp(wave_hz, -255.0, 255.0),
            shape=_clamp(shape, 0.0, 100.0),
            sparkle=_clamp(sparkle, 0.0, 100.0),
            open=_clamp(open_pct, 0.0, 100.0),
            ease=ease,
        )
        return frame, cmds

    # ---- state for logs / observe ----
    def snapshot(self) -> dict[str, Any]:
        return {
            "beat_env": round(self._beat_env, 3),
            "prox_env": round(self._prox_env, 3),
            "lux_env": round(self._lux_env, 1),
            "slow_level": round(self._slow, 1),
            "startles": self.startles,
            "awake": round(self._act, 2),
            "away": round(self._away_env, 2),
            "music_env": round(self._music_env, 2),
            "greet_env": round(self._greet_env, 2),
            "greetings": self.greetings,
        }


def as_senses(raw: Any) -> Senses:
    """Accept a dict (from the host) or a Senses, defaulting missing fields."""
    if isinstance(raw, Senses):
        return raw
    if not isinstance(raw, dict):
        return Senses()
    return Senses(
        level=float(raw.get("level", 0.0) or 0.0),
        music=int(raw.get("music", 0) or 0),
        beat=int(raw.get("beat", 0) or 0),
        bass=int(raw.get("bass", 0) or 0),
        treble=int(raw.get("treble", 0) or 0),
        prox=int(raw.get("prox", 0) or 0),
        lux=float(raw.get("lux", 0.0) or 0.0),
        present=bool(raw.get("present", True)),
        idle_s=float(raw.get("idle_s", 0.0) or 0.0),
    )
