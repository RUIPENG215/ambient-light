"""Microphone -> noise level 0..100, relative to a calibrated room baseline.

Kept close to the original noise_to_feather.py:
  * 3 s calibration at start-up
  * level = (db - baseline) / SPAN_DB * 100, clipped to 0..100
  * exponential smoothing

Differences that matter when this runs for hours:
  * median instead of mean for the baseline (one cough during calibration
    no longer poisons the whole session)
  * smoothing is time-based, so it does not change when you change blocksize
  * a ring buffer of recent frames feeds the slow loop's summary
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np


@dataclass(frozen=True)
class Frame:
    t: float
    db: float


class AudioMeter:
    """Thread-safe sound level meter.

    `level` is the smoothed 0..100 value the fast loop sends to the lamp.
    `stats()` summarises the last N seconds for the slow loop / the LLM.
    """

    def __init__(
        self,
        samplerate: int = 48000,
        blocksize: int = 1024,
        calibration_s: float = 3.0,
        span_db: float = 18.0,
        floor_db: float = -90.0,
        attack_tau: float = 0.12,
        release_tau: float = 0.35,
        device: Optional[int | str] = None,
        simulated: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.calibration_s = max(0.0, calibration_s)
        self.span_db = max(1.0, span_db)
        self.floor_db = floor_db
        self.attack_tau = max(0.01, attack_tau)
        self.release_tau = max(0.01, release_tau)
        self.device = device
        self.simulated = simulated
        self.log = logger or logging.getLogger("audio")

        self._lock = threading.Lock()
        self._frames: Deque[Frame] = deque(maxlen=int(120 * samplerate / blocksize))
        self._calib: list[float] = []
        self._calib_start = time.monotonic()
        self._baseline: Optional[float] = None
        self._level = 0.0
        self._db = floor_db
        self._ready = False
        self._overflows = 0

        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        self._calib_start = time.monotonic()
        if self.simulated:
            self._thread = threading.Thread(target=self._sim_loop, name="audio-sim", daemon=True)
            self._thread.start()
            return

        import sounddevice as sd  # imported lazily so --sim-audio works without PortAudio

        try:
            self._stream = sd.InputStream(
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - surface a useful message
            raise RuntimeError(
                "cannot open the microphone. On macOS give Terminal/IDE microphone "
                "permission (System Settings > Privacy & Security > Microphone) and "
                "check `python3 ambient_host.py --list-devices`."
            ) from exc

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ---------------- audio path ----------------
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            self._overflows += 1
        x = indata[:, 0].astype(np.float64, copy=False)
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        db = 20.0 * math.log10(max(rms, 1e-9))
        self._push(max(db, self.floor_db))

    def _push(self, db: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._db = db
            self._frames.append(Frame(now, db))

            if not self._ready:
                self._calib.append(db)
                if (
                    now - self._calib_start >= self.calibration_s
                    and len(self._calib) >= 5
                ):
                    self._baseline = float(np.median(self._calib))
                    self._ready = True
                    self._level = 0.0
                    self.log.info(
                        "baseline %.1f dBFS from %d frames", self._baseline, len(self._calib)
                    )
                return

            target = self._level_from_db(db)
            dt = self.blocksize / float(self.samplerate)
            tau = self.attack_tau if target > self._level else self.release_tau
            alpha = 1.0 - math.exp(-dt / tau)
            self._level += (target - self._level) * alpha
            if self._level < 0.05:
                self._level = 0.0

    def _level_from_db(self, db: float) -> float:
        if self._baseline is None:
            return 0.0
        return max(0.0, min(100.0, (db - self._baseline) / self.span_db * 100.0))

    # ---------------- properties ----------------
    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def level(self) -> float:
        with self._lock:
            return self._level if self._ready else 0.0

    @property
    def db(self) -> float:
        with self._lock:
            return self._db

    @property
    def baseline_db(self) -> Optional[float]:
        with self._lock:
            return self._baseline

    @property
    def overflows(self) -> int:
        return self._overflows

    # ---------------- summary for the slow loop ----------------
    def stats(self, window_s: float = 10.0) -> dict:
        now = time.monotonic()
        with self._lock:
            frames = [f for f in self._frames if now - f.t <= window_s]
            baseline = self._baseline
            level = self._level

        if not frames or baseline is None:
            return {
                "mean_db": self.floor_db,
                "max_db": self.floor_db,
                "p95_db": self.floor_db,
                "mean_level": 0,
                "p95_level": 0,
                "max_level": 0,
                "trend": "steady",
                "frames": 0,
            }

        dbs = np.array([f.db for f in frames], dtype=np.float64)
        levels = np.clip((dbs - baseline) / self.span_db * 100.0, 0.0, 100.0)

        trend = "steady"
        half = len(dbs) // 2
        if half >= 2:
            delta = float(dbs[half:].mean() - dbs[:half].mean())
            if delta > 2.0:
                trend = "rising"
            elif delta < -2.0:
                trend = "falling"

        return {
            "mean_db": round(float(dbs.mean()), 1),
            "max_db": round(float(dbs.max()), 1),
            "p95_db": round(float(np.percentile(dbs, 95)), 1),
            "mean_level": int(round(float(levels.mean()))),
            "p95_level": int(round(float(np.percentile(levels, 95)))),
            "max_level": int(round(float(levels.max()))),
            "trend": trend,
            "frames": len(frames),
            "level_now": int(round(level)),
        }

    # ---------------- simulated source for demos / CI ----------------
    def _sim_loop(self) -> None:
        rng = random.Random(7)
        burst_until = 0.0
        next_burst = time.monotonic() + 4.0
        while not self._stop.is_set():
            now = time.monotonic()
            db = -56.0 + 2.0 * math.sin(now / 9.0) + rng.gauss(0.0, 1.2)
            if now >= next_burst:
                burst_until = now + rng.uniform(0.8, 3.0)
                next_burst = now + rng.uniform(5.0, 11.0)
            if now < burst_until:
                db += rng.uniform(12.0, 28.0)
            self._push(db)
            self._stop.wait(0.02)
