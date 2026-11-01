"""Reflex layer — sensors to light, on the Mac, without the agent.

The board renders and the agent decides, but neither needs to be alive for the
lamp to react: this module reads the microphone, the system audio, the proximity
sensor, the room light and the idle time, and bends whatever the agent (or the
last manual look) asked for. It runs inside the 25 Hz frame loop, so a hand in
front of the sensor is felt in 40–120 ms, not in DeepSeek's 1–2 s.

Everything here is continuous. There are no modes: each sensor contributes a
number, and the numbers are added to the base look.

    sound  room mic and system audio, whichever is louder -> breath depth,
           breath speed and brightness
    beat   a kick drum lifts val for ~200 ms
    prox   a hand raises open and brightness and warms the hue
    lux    a bright room lifts the floor, a dark room lets it sit low
    idle   nobody at the desk for minutes -> slowly dims
    startle an edge in the level fires a flash + wince once, not twice

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
    prox: int = 0               # 0-255 APDS9960 proximity
    lux: float = 0.0            # estimated room light
    present: bool = True        # a person is at the computer
    idle_s: float = 0.0         # seconds since the last input


@dataclass
class ReflexConfig:
    audio_breath: float = 0.9    # how much sound deepens the breath
    audio_val: float = 0.55      # brightness per level unit
    audio_tempo: float = 0.5     # breath rate swing: 0.5x quiet .. 1.5x loud
    beat_val: float = 34.0       # val units at beat 100
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
    idle_dim: float = 0.35       # how much of val the idle can take away
    idle_after_s: float = 60.0
    idle_full_s: float = 300.0
    startle_jump: float = 26.0   # rise over the slow average
    startle_step: float = 10.0   # and a real edge: this much in one frame
    startle_level: float = 45.0  # and an absolute floor
    startle_cool_s: float = 2.0
    ease: float = 100.0          # the host already smooths; keep this short


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

    # ---- one frame ----
    def frame(self, base: "Look", senses: Senses, dt: float,
              now: float = 0.0) -> "tuple[Look, list[str]]":
        """Return the look to draw this frame and any transient commands."""
        dt = _clamp(dt, 0.001, 0.5)
        cfg = self.cfg
        cmds: list[str] = []

        # --- sound: breath depth and a little brightness
        # The microphone hears the room; the system-audio tap hears the music
        # cleanly, which matters when headphones mean the microphone is quiet.
        # max() rather than sum, so a loud room playing loud music is not
        # counted twice.
        level = _clamp(senses.level, 0.0, 100.0)
        music = _clamp(float(senses.music), 0.0, 100.0)
        alive = max(level, music)
        depth = (1.0 - 0.45 * cfg.audio_breath
                 + cfg.audio_breath * (alive / 100.0))
        breath_amp = _clamp(base.breath_amp * depth, 0.0, 100.0)
        # louder -> breathes faster, not only deeper
        tempo = 1.0 + cfg.audio_tempo * ((alive / 100.0) - 0.5) * 2.0
        breath_hz = _clamp(base.breath_hz * tempo, 0.0, 255.0)
        val = base.val + cfg.audio_val * alive
        # motion: louder -> wider colour spread, stronger and faster travel
        loudness = alive / 100.0
        grad = _clamp(base.grad + cfg.audio_grad * loudness, -128.0, 127.0)
        wave_amp = _clamp(base.wave_amp + cfg.audio_wave_amp * loudness, 0.0, 100.0)
        wave_hz = _clamp(base.wave_hz * (1.0 + cfg.audio_wave_hz * loudness), 0.0, 255.0)

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
            cmds.append("K,2,255")   # flash
            cmds.append("K,1")       # wince
            self.log.debug("startle: level=%.0f rising=%.0f", level, rising)

        # --- beat: fast attack, exponential release
        if senses.beat > 20:
            self._beat_env = max(self._beat_env, senses.beat / 100.0)
        self._beat_env *= math.exp(-dt / max(0.02, cfg.beat_decay_s))
        val += cfg.beat_val * self._beat_env

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
        val *= 1.0 - cfg.idle_dim * idle_k
        open_pct *= 1.0 - 0.5 * cfg.idle_dim * idle_k

        frame = replace(
            base,
            hue=hue % 256.0,
            val=_clamp(val, 0.0, 255.0),
            breath_amp=_clamp(breath_amp, 0.0, 100.0),
            breath_hz=breath_hz,
            grad=grad,
            wave_amp=wave_amp,
            wave_hz=wave_hz,
            open=_clamp(open_pct, 0.0, 100.0),
            ease=cfg.ease,
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
        prox=int(raw.get("prox", 0) or 0),
        lux=float(raw.get("lux", 0.0) or 0.0),
        present=bool(raw.get("present", True)),
        idle_s=float(raw.get("idle_s", 0.0) or 0.0),
    )
