"""Named looks — the host-side vocabulary of the lamp.

The firmware does not know what "warm" or "curious" means any more; it only
evaluates a parameter frame at 50 fps. This module is the one place that holds
the vocabulary, so tuning the light never means reflashing anything.

A `Look` is 11 numbers plus a little optional motion:

    hue sat val grad wave_n wave_amp wave_hz breath_hz breath_amp open ease

`LookEngine` streams it at ~25 Hz. The firmware interpolates between frames, so
the host can be as sloppy as it likes (Python, GC, a sleeping laptop) and the
lamp still moves smoothly.

    python3 lampctl.py look ember --seconds 300
    python3 lampctl.py looks
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional

from reflex import as_senses

# Wire order of the F command, see PROTOCOL.md.
FIELD_ORDER = (
    "hue", "sat", "val", "grad",
    "wave_n", "wave_amp", "wave_hz",
    "breath_hz", "breath_amp", "open", "ease",
)

FIELD_RANGE = {
    "hue": (0, 255),
    "sat": (0, 255),
    "val": (0, 255),
    "grad": (-128, 127),
    "wave_n": (0, 16),
    "wave_amp": (0, 100),
    "wave_hz": (0, 255),
    "breath_hz": (0, 255),
    "breath_amp": (0, 100),
    "open": (0, 100),
    "ease": (0, 10000),
}


def _clamp(name: str, value: float) -> float:
    lo, hi = FIELD_RANGE[name]
    return max(lo, min(hi, float(value)))


@dataclass(frozen=True)
class Look:
    """One frame of the lamp's appearance, plus optional slow drift."""

    hue: float = 30.0
    sat: float = 200.0
    val: float = 120.0
    grad: float = 6.0
    wave_n: float = 1.0
    wave_amp: float = 6.0
    wave_hz: float = 3.0
    breath_hz: float = 12.0     # 0.01 Hz units: 12 -> 0.12 Hz, an 8 s breath
    breath_amp: float = 55.0
    open: float = 55.0
    ease: float = 3000.0

    # motion, applied by the engine rather than the firmware
    hue_drift: float = 0.0      # hue units per second
    val_drift: float = 0.0
    breath_drift: float = 0.0

    # ---- wire ----
    def fields(self) -> dict[str, int]:
        return {name: int(round(_clamp(name, getattr(self, name)))) for name in FIELD_ORDER}

    def line(self) -> str:
        values = self.fields()
        return "F," + ",".join(str(values[name]) for name in FIELD_ORDER)

    # ---- motion ----
    def evolved(self, elapsed_s: float) -> "Look":
        if not (self.hue_drift or self.val_drift or self.breath_drift):
            return self
        return replace(
            self,
            hue=(self.hue + self.hue_drift * elapsed_s) % 256.0,
            val=_clamp("val", self.val + self.val_drift * elapsed_s),
            breath_hz=_clamp("breath_hz", self.breath_hz + self.breath_drift * elapsed_s),
        )

    def as_dict(self) -> dict:
        out = self.fields()
        out["hue_drift"] = self.hue_drift
        out["val_drift"] = self.val_drift
        out["breath_drift"] = self.breath_drift
        return out


# ---------------------------------------------------------------- the body
# One lamp, one look. The agent writes these numbers; the reflex bends them.
DEFAULT_LOOK = Look(
    hue=30, sat=200, val=120, grad=28, wave_n=1, wave_amp=30, wave_hz=8,
    breath_hz=12, breath_amp=55, open=55, ease=3000,
)


def ease_toward(current: Look, target: Look, dt: float) -> Look:
    """Move `current` toward `target` using target.ease (same curve as firmware)."""
    if target.ease <= 0.0:
        return target
    tau = max(0.02, target.ease / 3000.0)
    a = 1.0 - math.exp(-max(0.0, dt) / tau)
    moved = {
        name: getattr(current, name) + (getattr(target, name) - getattr(current, name)) * a
        for name in FIELD_ORDER
        if name != "ease"
    }
    return replace(target, **moved)


# ---------------------------------------------------------------- engine
class LookEngine:
    """Streams one look at ~25 Hz on its own thread.

    Independent of the LLM and of the audio paths on purpose: if Python stalls,
    the firmware keeps interpolating, and if the engine dies the board eases
    into its own fallback after 1.5 s.
    """

    def __init__(
        self,
        link,
        hz: float = 25.0,
        cap: int = 80,
        gamma: int = 0,
        dither: int = 1,
        logger: Optional[logging.Logger] = None,
        reflex=None,
        senses=None,
    ) -> None:
        self.link = link
        self.reflex = reflex          # reflex.Reflex or None
        self.senses = senses          # callable -> dict|Senses, or None
        self.hz = max(5.0, min(50.0, float(hz)))
        self.cap = max(0, min(255, int(cap)))
        self.gamma = max(0, min(2, int(gamma)))
        self.dither = 1 if dither else 0
        self.log = logger or logging.getLogger("looks")
        self.name = ""
        self.frames = 0
        self.last_line = ""
        self.started_at = 0.0
        self._look: Optional[Look] = None
        self._shown: Optional[Look] = None   # where the light actually is
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ----
    def start(self, name: str, look: Look) -> None:
        self.stop()
        with self._lock:
            self.name = name
            self._look = look
            self.started_at = time.monotonic()
            self.frames = 0
            self._shown = look
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="look", daemon=True)
        self._thread.start()

    def set(self, name: str, look: Look) -> None:
        """Change the target without restarting the thread (no dropped frames)."""
        with self._lock:
            self.name = name
            self._look = look
            self.started_at = time.monotonic()
            self.frames = 0      # keep "frames for N s" honest across switches

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        with self._lock:
            self._look = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._look is not None

    # ---- thread ----
    def _run(self) -> None:
        period = 1.0 / self.hz
        stop = self._stop
        self._send_config()
        next_t = time.monotonic()
        last_t = time.monotonic()
        while not stop.is_set():
            with self._lock:
                look = self._look
                shown = self._shown or look
                started = self.started_at
                name = self.name
            if look is None:
                break

            now = time.monotonic()
            dt = now - last_t
            last_t = now

            # glide in the host, in the agent's own time constant
            eased = ease_toward(shown, look, dt)
            with self._lock:
                self._shown = eased
            frame = eased.evolved(now - started)
            transients: list[str] = []
            if self.reflex is not None and self.senses is not None:
                try:
                    senses = as_senses(self.senses())
                    frame, transients = self.reflex.frame(frame, senses, dt, now)
                except Exception as exc:  # noqa: BLE001 - never kill the stream
                    self.log.debug("reflex failed: %s", exc)

            line = frame.line()
            ok = self._send(line)
            for cmd in transients:
                self._send(cmd)
            if ok:
                self.frames += 1
                self.last_line = line
            else:
                self.log.debug("look %s: serial write failed", name)

            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            else:
                next_t = time.monotonic()   # fell behind: resync, do not catch up

    def _send(self, line: str) -> bool:
        try:
            return bool(self.link.send_line(line))
        except Exception as exc:  # noqa: BLE001 - never kill the thread
            self.log.debug("look send failed: %s", exc)
            return False

    def _send_config(self) -> None:
        try:
            self.link.send_line(f"G,{self.cap},{self.gamma},{self.dither}")
            if self.reflex is not None:
                # The reflex layer owns audio/beat coupling and startle now, so
                # the board must not add its own on top of ours.
                self.link.send_line("C,0,0,0,0")
        except Exception as exc:  # noqa: BLE001
            self.log.debug("look config failed: %s", exc)

    # ---- read ----
    def status(self) -> dict:
        with self._lock:
            look = self._look
            name = self.name
            started = self.started_at
        return {
            "name": name,
            "running": self.running,
            "frames": self.frames,
            "hz": self.hz,
            "cap": self.cap,
            "gamma": self.gamma,
            "dither": bool(self.dither),
            "for_s": int(time.monotonic() - started) if started else 0,
            "line": self.last_line,
            "params": look.as_dict() if look else {},
            "reflex": self.reflex.snapshot() if self.reflex is not None else None,
        }
