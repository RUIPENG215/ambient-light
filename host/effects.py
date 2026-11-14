"""Performance layer: named, time-based effects over the 14-number look.

The agent writes a base look; this module *performs* it for a few seconds or a
few minutes, then gets out of the way. An effect is a pure function

    (base Look, t seconds, k intensity) -> Look

so it needs no protocol change and no state on the board. The order inside the
25 Hz engine is:

    ease the agent's target -> apply the effect -> the reflex bends it -> wire

That order matters: the effect is a temporary *base*, so sound and hands still
ride on top of it, and the effect can never outlive its envelope.

The four effects are deliberately legible rather than parameter dumps:

    candle     warm, irregular flicker; a room with something alive in it
    heartbeat  two thumps per cycle; a message, a kick, a moment
    ocean      slow teal bands drifting; long quiet work
    aurora     a slow colour drift; minutes, not seconds
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

# looks.py imports this module; keep the type import out of the runtime cycle.
if TYPE_CHECKING:  # pragma: no cover
    from looks import Look


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def _lerp_hue(a: float, b: float, k: float) -> float:
    """Shortest way round the colour wheel."""
    delta = ((b - a + 128.0) % 256.0) - 128.0
    return (a + delta * k) % 256.0


def _blend(base, palette: dict, k: float):
    """Move `base` toward a palette by k (0 = base, 1 = palette)."""
    moved = {}
    for name, target in palette.items():
        current = getattr(base, name)
        moved[name] = _lerp_hue(current, target, k) if name == "hue" else _lerp(current, target, k)
    return replace(base, **moved)


def _envelope(t: float, duration: float, attack: float = 0.4, release: float = 0.7) -> float:
    """0 at the edges, 1 in the middle; keeps every effect from snapping."""
    if t < 0.0 or t > duration:
        return 0.0
    a = 1.0 if attack <= 0.0 else min(1.0, t / attack)
    r = 1.0 if release <= 0.0 else min(1.0, (duration - t) / release)
    return _clamp(min(a, r), 0.0, 1.0)


# ---------------------------------------------------------------- palettes
# What each effect becomes at full intensity. Effects blend toward these, so
# they read the same whatever the agent's current base happens to be.
CANDLE = {
    "hue": 24.0, "sat": 210.0, "val": 110.0, "grad": 4.0,
    "wave_n": 0.0, "wave_amp": 0.0, "wave_hz": 0.0,
    "breath_hz": 9.0, "breath_amp": 25.0, "open": 50.0,
}
HEART = {
    "hue": 8.0, "sat": 220.0, "val": 80.0, "grad": 0.0,
    "wave_n": 0.0, "wave_amp": 0.0, "wave_hz": 0.0,
    "breath_hz": 6.0, "breath_amp": 10.0, "open": 50.0,
}
OCEAN = {
    "hue": 150.0, "sat": 200.0, "val": 100.0, "grad": 30.0,
    "wave_n": 3.0, "wave_amp": 45.0, "wave_hz": 6.0,
    "breath_hz": 8.0, "breath_amp": 40.0, "open": 65.0,
}
AURORA = {
    "hue": 160.0, "sat": 190.0, "val": 110.0, "grad": 55.0,
    "wave_n": 2.0, "wave_amp": 35.0, "wave_hz": 4.0,
    "breath_hz": 10.0, "breath_amp": 50.0, "open": 75.0,
}


# ---------------------------------------------------------------- effects
def candle(base, t: float, k: float):
    """Irregular warm flicker. Three incommensurate sines, so it never loops."""
    if k <= 0.0001:
        return base
    n = (0.55 * math.sin(t * 7.3)
         + 0.30 * math.sin(t * 11.7 + 1.3)
         + 0.15 * math.sin(t * 3.1 + 0.7))
    n = _clamp(n, -1.0, 1.0)
    look = _blend(base, CANDLE, k)
    return replace(
        look,
        hue=(look.hue + 3.0 * k * n) % 256.0,
        val=_clamp(look.val * (1.0 + 0.12 * k * n) + 8.0 * k * n, 0.0, 255.0),
        breath_hz=_clamp(look.breath_hz * (1.0 + 0.10 * k * n), 0.0, 255.0),
        ease=60.0,
    )


def heartbeat(base, t: float, k: float):
    """Two thumps per 1.1 s cycle: a strong one and a smaller echo."""
    if k <= 0.0001:
        return base
    p = t % 1.1
    thump = (math.exp(-((p - 0.0) / 0.055) ** 2)
             + 0.65 * math.exp(-((p - 0.20) / 0.060) ** 2))
    look = _blend(base, HEART, k)
    return replace(
        look,
        val=_clamp(look.val + 60.0 * k * thump, 0.0, 255.0),
        open=_clamp(look.open + 10.0 * k * thump, 0.0, 100.0),
        ease=40.0,
    )


def ocean(base, t: float, k: float):
    """Slow teal bands; the wave speed breathes with a 25 s swell."""
    if k <= 0.0001:
        return base
    slow = 0.5 + 0.5 * math.sin(t * 0.25)
    look = _blend(base, OCEAN, k)
    return replace(
        look,
        hue=(look.hue + 6.0 * math.sin(t * 0.13)) % 256.0,
        wave_hz=_clamp(look.wave_hz * (0.70 + 0.60 * slow), 0.0, 255.0),
        wave_amp=_clamp(look.wave_amp * (0.85 + 0.30 * slow), 0.0, 100.0),
        val=_clamp(look.val * (0.92 + 0.08 * math.sin(t * 0.9)), 0.0, 255.0),
        ease=100.0,
    )


def aurora(base, t: float, k: float):
    """A slow colour drift with a wide gradient; minutes, not seconds."""
    if k <= 0.0001:
        return base
    look = _blend(base, AURORA, k)
    return replace(
        look,
        hue=(look.hue + 40.0 * math.sin(t * 0.18)) % 256.0,
        grad=_clamp(look.grad + 20.0 * math.sin(t * 0.11), -128.0, 127.0),
        val=_clamp(look.val * (0.95 + 0.05 * math.sin(t * 0.07)), 0.0, 255.0),
        ease=100.0,
    )


EFFECTS: dict[str, dict[str, Any]] = {
    "candle": {
        "fn": candle, "seconds": 45.0,
        "description": "warm irregular flicker; a room with something alive in it",
    },
    "heartbeat": {
        "fn": heartbeat, "seconds": 8.0,
        "description": "two thumps per cycle; a message, a kick, a moment",
    },
    "ocean": {
        "fn": ocean, "seconds": 90.0,
        "description": "slow teal bands drifting; long quiet work",
    },
    "aurora": {
        "fn": aurora, "seconds": 180.0,
        "description": "a slow colour drift; minutes, not seconds",
    },
}


# ---------------------------------------------------------------- gestures
# Body language. An effect is a mood that lasts a minute; a gesture is a second
# or two of movement that means something: yes, no, "let me think", delight.
# They are what makes a change of light read as *behaviour* instead of as a
# parameter being set. Same signature as an effect: (base, t, k) -> Look.
def _bump(t: float, at: float, width: float) -> float:
    return math.exp(-((t - at) / width) ** 2)


def think(base, t: float, k: float):
    """A single band circling slowly: something is going on inside."""
    return replace(
        _blend(base, {"wave_n": 1.0, "wave_amp": 95.0, "shape": 85.0,
                      "wave_hz": 55.0, "breath_amp": 0.0, "sparkle": 0.0,
                      "val": max(60.0, base.val * 0.8)}, k),
        ease=80.0,
    )


def nod(base, t: float, k: float):
    """Two slow blinks: yes."""
    dip = min(1.0, _bump(t, 0.30, 0.11) + _bump(t, 0.80, 0.11)) * k
    return replace(base, open=base.open * (1.0 - 0.85 * dip),
                   val=base.val * (1.0 - 0.6 * dip), ease=40.0)


def shake(base, t: float, k: float):
    """The band swings left, right, left: no."""
    look = _blend(base, {"wave_n": 1.0, "wave_amp": 90.0, "shape": 70.0,
                         "breath_amp": 0.0, "sparkle": 0.0}, k)
    swing = 130.0 * math.sin(2.0 * math.pi * t / 0.55)
    return replace(look, wave_hz=_lerp(base.wave_hz, swing, k), ease=40.0)


def happy(base, t: float, k: float):
    """Brightens, opens wide and lets every colour run round once."""
    look = _blend(base, {"open": 96.0, "sparkle": 40.0, "wave_hz": 45.0,
                         "hue_cycles": 100.0, "grad": 0.0}, k)
    return replace(look, val=_clamp(base.val + 75.0 * k, 0.0, 255.0),
                   hue=(base.hue + 90.0 * t * k) % 256.0, ease=70.0)


def shy(base, t: float, k: float):
    """Looks away: dims, half closes, blushes."""
    look = _blend(base, {"open": 24.0, "hue": 238.0, "sat": 150.0,
                         "wave_amp": 0.0, "sparkle": 0.0}, 0.75 * k)
    return replace(look, val=base.val * (1.0 - 0.5 * k), ease=120.0)


def sulk(base, t: float, k: float):
    """Turns cold and small for a few seconds."""
    look = _blend(base, {"open": 28.0, "hue": 165.0, "sat": 120.0,
                         "wave_amp": 0.0, "breath_amp": 10.0, "sparkle": 0.0}, k)
    return replace(look, val=base.val * (1.0 - 0.55 * k), ease=150.0)


def surprise(base, t: float, k: float):
    """Eyes wide, nearly white, for a moment."""
    look = _blend(base, {"open": 100.0, "sat": base.sat * 0.3}, k)
    return replace(look, val=_clamp(base.val + 110.0 * k, 0.0, 255.0), ease=30.0)


def wave(base, t: float, k: float):
    """One quick lap of light: hello / goodbye / look at me."""
    look = _blend(base, {"wave_n": 1.0, "wave_amp": 100.0, "shape": 90.0,
                         "wave_hz": 105.0, "breath_amp": 0.0, "sparkle": 0.0}, k)
    return replace(look, val=_clamp(max(base.val, 140.0), 0.0, 255.0), ease=50.0)


GESTURES: dict[str, dict[str, Any]] = {
    "think": {"fn": think, "seconds": 20.0, "attack": 0.25, "release": 0.35,
              "description": "a band circling slowly; held while it thinks"},
    "nod": {"fn": nod, "seconds": 1.3, "attack": 0.05, "release": 0.10,
            "description": "two slow blinks: yes, agreed, got it"},
    "shake": {"fn": shake, "seconds": 1.7, "attack": 0.12, "release": 0.25,
              "description": "the band swings side to side: no, I would rather not"},
    "happy": {"fn": happy, "seconds": 2.4, "attack": 0.20, "release": 0.80,
              "description": "brightens and lets the colours run: delight"},
    "shy": {"fn": shy, "seconds": 2.6, "attack": 0.30, "release": 0.90,
            "description": "dims, half closes, blushes: embarrassed, touched"},
    "sulk": {"fn": sulk, "seconds": 4.5, "attack": 0.50, "release": 1.20,
             "description": "cold and small for a few seconds: hurt, ignored"},
    "surprise": {"fn": surprise, "seconds": 1.0, "attack": 0.04, "release": 0.50,
                 "description": "eyes wide and pale for a moment: oh!"},
    "wave": {"fn": wave, "seconds": 1.5, "attack": 0.08, "release": 0.45,
             "description": "one quick lap of light: hello, goodbye, look here"},
}


class EffectPlayer:
    """One active effect at a time, thread-safe across control and look threads."""

    def __init__(self, registry: Optional[dict] = None) -> None:
        self.registry = EFFECTS if registry is None else registry
        self._lock = threading.Lock()
        self._name = ""
        self._started = 0.0
        self._duration = 0.0
        self._intensity = 0.0

    def start(self, name: str, seconds: Optional[float] = None,
              intensity: float = 1.0, now: Optional[float] = None) -> None:
        name = str(name).strip().lower()
        if name not in self.registry:
            raise ValueError(f"unknown effect {name!r}")
        if seconds is None:
            seconds = float(self.registry[name]["seconds"])
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            self._name = name
            self._started = now
            self._duration = _clamp(float(seconds), 0.3, 3600.0)
            self._intensity = _clamp(float(intensity), 0.0, 1.0)

    def stop(self, name: Optional[str] = None, now: Optional[float] = None) -> None:
        """Stop at once, or — given a name — let that one fade out gracefully."""
        with self._lock:
            if name is not None:
                if self._name != name:
                    return
                now = time.monotonic() if now is None else float(now)
                release = float(self.registry[name].get("release", 0.7))
                self._duration = min(self._duration, (now - self._started) + release)
                return
            self._name = ""
            self._duration = 0.0
            self._intensity = 0.0

    def active(self, now: Optional[float] = None) -> bool:
        return self.status(now) is not None

    def apply(self, look, now: float):
        with self._lock:
            name = self._name
            if not name:
                return look
            t = float(now) - self._started
            if t > self._duration:
                self._name = ""
                return look
            duration = self._duration
            intensity = self._intensity
        spec = self.registry[name]
        k = intensity * _envelope(t, duration, float(spec.get("attack", 0.4)),
                                  float(spec.get("release", 0.7)))
        if k <= 0.001:
            return look
        return spec["fn"](look, t, k)

    def status(self, now: Optional[float] = None) -> Optional[dict]:
        with self._lock:
            name = self._name
            if not name:
                return None
            now = time.monotonic() if now is None else float(now)
            t = now - self._started
            if t > self._duration:
                self._name = ""
                return None
            return {
                "name": name,
                "t": round(t, 1),
                "seconds": round(self._duration, 1),
                "intensity": round(self._intensity, 2),
            }
