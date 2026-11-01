"""Sensor hub for the lamp.

Three sources, one situation:

  * the microphone (AudioMeter, already in audio_meter.py) -> room noise
  * the system output (BlackHole, optional)               -> music energy / beat
  * macOS context (idle, now playing)                     -> "is the human here"

Nothing here talks to the lamp. `SensorHub` only reads; `agent.py` decides.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import threading
import time
from collections import deque
from typing import Deque, Optional

import numpy as np


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return proc.stdout or ""
    except Exception:  # noqa: BLE001 - a missing CLI is normal
        return ""


class MacContext:
    """Best-effort macOS context. Every call degrades to a safe default."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mac")
        self._idle = 0.0
        self._idle_at = -10.0
        self._np = {"playing": False, "title": "", "artist": "", "source": ""}
        self._np_at = -10.0

    def idle_seconds(self) -> float:
        now = time.monotonic()
        if now - self._idle_at < 1.0:
            return self._idle
        out = _run(["ioreg", "-c", "IOHIDSystem"], 1.0)
        for line in out.splitlines():
            if "HIDIdleTime" in line and "=" in line:
                raw = line.split("=", 1)[1].strip().strip('"')
                try:
                    self._idle = int(raw) / 1e9
                except ValueError:
                    pass
                break
        self._idle_at = now
        return self._idle

    def user_present(self) -> bool:
        return self.idle_seconds() < 120.0

    def now_playing(self) -> dict:
        now = time.monotonic()
        if now - self._np_at < 1.5:
            return self._np
        info = self._nowplaying_cli() or self._nowplaying_applescript()
        if info is None:
            info = {"playing": False, "title": "", "artist": "", "source": ""}
        self._np = info
        self._np_at = now
        return info

    def _nowplaying_cli(self) -> Optional[dict]:
        out = _run(["nowplaying-cli", "get-raw"], 1.5)
        if not out.strip():
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None
        title = str(data.get("kMRMediaRemoteNowPlayingInfoTitle") or "")
        artist = str(data.get("kMRMediaRemoteNowPlayingInfoArtist") or "")
        rate = float(data.get("kMRMediaRemoteNowPlayingInfoPlaybackRate") or 0.0)
        if not title and not artist:
            return None
        return {"playing": rate > 0, "title": title, "artist": artist, "source": "nowplaying"}

    def _nowplaying_applescript(self) -> Optional[dict]:
        script = """
        set out to ""
        tell application "System Events"
          if exists process "Music" then
            tell application "Music"
              if player state is playing then set out to "Music|" & name of current track & "|" & artist of current track
            end tell
          end if
          if out is "" and exists process "Spotify" then
            tell application "Spotify"
              if player state is playing then set out to "Spotify|" & name of current track & "|" & artist of current track
            end tell
          end if
        end tell
        return out
        """
        out = _run(["osascript", "-e", script], 2.0).strip()
        if not out:
            return None
        parts = out.split("|")
        if len(parts) < 3:
            return None
        return {
            "playing": True,
            "title": parts[1],
            "artist": parts[2],
            "source": parts[0].lower(),
        }


class LevelBeatDetector:
    """Fallback when there is no BlackHole: onsets from the mic dB stream."""

    def __init__(self) -> None:
        self._hist: Deque[tuple[float, float]] = deque(maxlen=60)
        self._last_beat = 0.0
        self.energy = 0.0
        self.beat = 0.0

    def update(self, db: float) -> tuple[float, float]:
        now = time.monotonic()
        self._hist.append((now, db))
        if len(self._hist) < 8:
            return 0.0, 0.0
        vals = [v for _, v in self._hist]
        base = float(np.percentile(vals, 20))
        energy = max(0.0, min(1.0, (db - base) / 24.0))
        old = None
        for t, v in reversed(self._hist):
            if now - t >= 0.15:
                old = v
                break
        if old is None and len(self._hist) >= 8:
            old = self._hist[0][1]
        beat = 0.0
        if old is not None and db - old >= 6.0 and now - self._last_beat > 0.18:
            beat = 1.0
            self._last_beat = now
        self.energy, self.beat = energy, beat
        return energy, beat


class BeatDetector:
    """Spectral-flux onset detector for system audio (20–150 Hz).

    One frame in, one (energy, beat) out. No librosa/aubio dependency.
    """

    def __init__(self, samplerate: int = 48000, blocksize: int = 1024) -> None:
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._window = np.hanning(blocksize)
        freqs = np.fft.rfftfreq(blocksize, 1.0 / samplerate)
        self._low = (freqs >= 25.0) & (freqs <= 150.0)
        self._prev: Optional[np.ndarray] = None
        self._flux: Deque[float] = deque(maxlen=80)
        self._rms: Deque[float] = deque(maxlen=200)
        self._last_beat = 0.0
        self._lock = threading.Lock()
        self.energy = 0.0
        self.beat = 0.0

    def push(self, samples: np.ndarray) -> tuple[float, float]:
        x = np.asarray(samples, dtype=np.float64).reshape(-1)
        if x.size < self.blocksize:
            x = np.pad(x, (0, self.blocksize - x.size))
        elif x.size > self.blocksize:
            x = x[-self.blocksize :]

        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        db = 20.0 * math.log10(max(rms, 1e-9))

        with self._lock:
            self._rms.append(db)
            floor = float(np.percentile(self._rms, 20)) if len(self._rms) >= 10 else db
            energy = max(0.0, min(1.0, (db - floor) / 26.0))

            spec = np.abs(np.fft.rfft(x * self._window))[self._low]
            if self._prev is not None and spec.shape == self._prev.shape:
                flux = float(np.sum(np.maximum(0.0, spec - self._prev)))
            else:
                flux = 0.0
            self._prev = spec
            self._flux.append(flux)

            beat = 0.0
            if len(self._flux) >= 12:
                arr = np.asarray(self._flux)
                threshold = float(arr.mean() + 1.3 * arr.std())
                now = time.monotonic()
                if flux > threshold and flux > 1e-6 and now - self._last_beat > 0.12:
                    beat = 1.0
                    self._last_beat = now
            self.energy, self.beat = energy, beat
            return energy, beat


class SystemAudio:
    """Optional BlackHole input. Silent no-op when the driver is missing."""

    def __init__(
        self,
        detector: BeatDetector,
        samplerate: int = 48000,
        blocksize: int = 1024,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.detector = detector
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.log = logger or logging.getLogger("music")
        self.available = False
        self.device_name = ""
        self._stream = None

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self.log.info("system audio unavailable: %s", exc)
            return False
        try:
            devices = sd.query_devices()
        except Exception as exc:  # noqa: BLE001
            self.log.info("cannot query audio devices: %s", exc)
            return False

        index = None
        for i, dev in enumerate(devices):
            name = str(dev.get("name", ""))
            if dev.get("max_input_channels", 0) > 0 and "blackhole" in name.lower():
                index = i
                self.device_name = name
                break
        if index is None:
            self.log.info("BlackHole not found; music beat falls back to the microphone")
            return False

        try:
            self._stream = sd.InputStream(
                device=index,
                channels=2,
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            self.available = True
            self.log.info("system audio on %s", self.device_name)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("cannot open system audio: %s", exc)
            return False

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        x = indata[:, 0] if getattr(indata, "ndim", 1) == 2 else indata
        self.detector.push(x)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        self.available = False


class SensorHub:
    """Fuses mic, system audio and macOS context into one snapshot."""

    def __init__(
        self,
        meter,
        link,
        mac: MacContext,
        system: Optional[SystemAudio],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.meter = meter
        self.link = link
        self.mac = mac
        self.system = system
        self.log = logger or logging.getLogger("sensors")
        self._mic_beat = LevelBeatDetector()
        self._music = (0, 0, False)
        self._music_at = -10.0

    def music_bus(self) -> tuple[int, int, bool]:
        """Returns (energy 0..100, beat 0..100, user_present)."""
        now = time.monotonic()
        if now - self._music_at < 0.05:
            return self._music
        if self.system is not None and self.system.available:
            energy = self.system.detector.energy
            beat = self.system.detector.beat
        else:
            energy, beat = self._mic_beat.update(self.meter.db)
        present = self.mac.user_present()
        self._music = (int(round(energy * 100)), int(round(beat * 100)), present)
        self._music_at = now
        return self._music

    def mac_context(self) -> dict:
        np_info = self.mac.now_playing()
        idle = self.mac.idle_seconds()
        return {
            "idle_s": int(idle),
            "user_present": idle < 120.0,
            "playing": bool(np_info.get("playing")),
            "title": str(np_info.get("title", ""))[:80],
            "artist": str(np_info.get("artist", ""))[:80],
            "source": str(np_info.get("source", "")),
        }

    def sensors(self) -> dict:
        s = self.link.sensors
        return {
            "prox": int(s.prox),
            "near": int(s.prox) >= 120,
            "lux": round(float(s.lux)),
            "energy": int(s.energy),
            "mood": s.mood,
            "lid": int(s.lid),
        }


def build_situation(
    *,
    stats: dict,
    sensors: dict,
    mac: dict,
    music: tuple[int, int, bool],
    lamp: dict,
    drives: dict,
    memory: list,
    events: list,
    uptime_min: float,
    last_reason: str = "",
) -> dict:
    """The JSON the model sees once per turn. Keep it small and stable."""
    now = time.localtime()
    music_energy, music_beat, present = music
    return {
        "time": time.strftime("%H:%M", now),
        "hour": now.tm_hour,
        "uptime_min": int(uptime_min),
        "noise": {
            "mean": int(stats.get("mean_level", 0)),
            "p95": int(stats.get("p95_level", 0)),
            "max": int(stats.get("max_level", 0)),
            "trend": stats.get("trend", "steady"),
        },
        "music": {
            "playing": bool(mac.get("playing")),
            "title": mac.get("title", ""),
            "artist": mac.get("artist", ""),
            "energy": int(music_energy),
            "beat": int(music_beat),
        },
        "presence": {
            "prox": int(sensors.get("prox", 0)),
            "near": bool(sensors.get("near", False)),
            "user_present": bool(present),
            "idle_s": int(mac.get("idle_s", 0)),
        },
        "light_lux": int(sensors.get("lux", 0)),
        "lamp": {
            "mood": lamp.get("mood", "calm"),
            "since_s": int(lamp.get("since_s", 0)),
            "lid": int(lamp.get("lid", 70)),
            "coupling": lamp.get("coupling", {}),
        },
        "drives": {k: round(float(v), 2) for k, v in drives.items()},
        "memory": list(memory)[-6:],
        "events": list(events)[-6:],
        "last_reason": str(last_reason)[:120],
    }
