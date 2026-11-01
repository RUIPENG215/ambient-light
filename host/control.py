"""Local control socket for the running lamp host.

The host owns the serial port. Other processes (DSH's bash tool, `lampctl.py`,
a chat agent) talk to it over a Unix socket with newline-delimited JSON.

Request:
    {"cmd": "mood", "mood": "curious", "hue": 150, "lid": 85, "seconds": 60}
    {"cmd": "lid", "open": 20, "ms": 2000, "mode": "hold"}
    {"cmd": "observe"}
    {"cmd": "release"}

Response: one JSON object, always with "ok".

A mood/lid command also takes a short "mind lease": for N seconds the host's
own slow loop stops writing moods, so the chat agent and the local policy do
not fight over the lamp. `release` ends it immediately.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Optional

from brain import Decision

DEFAULT_SOCKET = os.environ.get("AMBIENT_SOCK", "/tmp/ambient-lamp.sock")
MODE_IDS = {"hold": 0, "breathe": 1, "blink": 2, "wince": 3}


class ControlError(Exception):
    pass


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ControlError("request must be a JSON object")
                response = self.server.control.handle(request)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - always answer
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()


class _ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: str, control: "ControlServer") -> None:
        self.control = control
        super().__init__(address, _ControlHandler)


class ControlServer:
    """Threaded Unix-socket server; all mutations happen on the host process."""

    def __init__(
        self,
        link,
        state,
        store,
        sensors,
        socket_path: str = DEFAULT_SOCKET,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.link = link
        self.state = state
        self.store = store
        self.sensors = sensors
        self.socket_path = str(socket_path)
        self.log = logger or logging.getLogger("control")
        self._server: Optional[_ControlServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._flow_stop = threading.Event()
        self._flow_thread: Optional[threading.Thread] = None

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        path = Path(self.socket_path)
        if path.exists():
            if self._is_alive(path):
                raise RuntimeError(f"control socket already in use: {path}")
            try:
                path.unlink()
            except OSError:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        server = _ControlServer(str(path), self)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="control", daemon=True
        )
        self._thread.start()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.log.info("control socket ready: %s", path)

    def stop(self) -> None:
        self.stop_flow()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            Path(self.socket_path).unlink()
        except OSError:
            pass

    def _is_alive(self, path: Path) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                probe.connect(str(path))
                probe.sendall(b'{"cmd":"ping"}\n')
                return bool(probe.recv(64))
        except OSError:
            return False

    # ---------------- dispatch ----------------
    def handle(self, request: dict) -> dict:
        cmd = str(request.get("cmd", "")).strip().lower()
        if cmd == "ping":
            return {"ok": True, "pong": True, "pid": os.getpid()}
        if cmd == "observe":
            return {"ok": True, **self.observe()}
        if cmd == "mood":
            return self.set_mood(request)
        if cmd == "lid":
            return self.set_lid(request)
        if cmd == "flow":
            return self.start_flow(request)
        if cmd == "release":
            return self.release()
        raise ControlError(f"unknown command {cmd!r}")

    # ---------------- commands ----------------
    def set_mood(self, request: dict) -> dict:
        self.stop_flow()
        seconds = max(0, min(600, int(request.get("seconds", 60))))
        decision = Decision(
            str(request.get("mood", "calm")),
            int(request.get("hue", 150)),
            int(request.get("speed", 90)),
            int(request.get("lid", 70)),
            int(request.get("bright", 180)),
            str(request.get("reason", "chat")),
            45,
            "chat",
        )
        with self._lock:
            ok = self.link.send_mood(decision)
            now = time.monotonic()
            if decision.mood != getattr(self.state, "mood", ""):
                try:
                    self.state.history.append(
                        f"{self.state.mood} {int(now - self.state.mood_since)}s"
                    )
                except AttributeError:
                    pass
                self.state.mood = decision.mood
                self.state.mood_since = now
            self.state.lid = decision.louver
            self.state.last = decision
            self.state.last_sent = decision.serial_line()
            self.state.last_sent_at = now
            self.state.last_decision_at = now
            self.state.manual_until = now + seconds
            self.state.manual_last = decision
            self.state.manual_sent_at = now
            self.store.last_mood = decision.mood
            self.store.save()
        return {
            "ok": ok,
            "line": decision.serial_line(),
            "mood": decision.mood,
            "lid": decision.louver,
            "manual_for_s": seconds,
        }

    def set_lid(self, request: dict) -> dict:
        self.stop_flow()
        seconds = max(0, min(600, int(request.get("seconds", 60))))
        open_pct = max(0, min(100, int(request.get("open", 70))))
        ms = max(0, min(60000, int(request.get("ms", 800))))
        mode = str(request.get("mode", "hold")).lower()
        mode_id = MODE_IDS.get(mode, 0)
        amp = float(request.get("amp", 4.0))
        period = float(request.get("period", 6.0))

        with self._lock:
            base = self._base_decision(open_pct)
            # Establish host mode first, then the eyelid command. The M line
            # carries the same lid, so a later keep-alive cannot reopen it.
            ok_m = self.link.send_mood(base)
            ok_l = self.link.send_lid(open_pct, ms, mode_id, amp, period)
            now = time.monotonic()
            self.state.lid = open_pct
            self.state.last = base
            self.state.last_sent = base.serial_line()
            self.state.last_sent_at = now
            self.state.last_decision_at = now
            self.state.manual_until = now + seconds
            self.state.manual_last = base
            self.state.manual_sent_at = now
            self.store.save()
        return {
            "ok": bool(ok_m and ok_l),
            "line": base.serial_line(),
            "lid": open_pct,
            "mode": mode,
            "manual_for_s": seconds,
        }

    def _base_decision(self, lid: int) -> Decision:
        base = getattr(self.state, "manual_last", None) or getattr(self.state, "last", None)
        if base is None:
            return Decision("calm", 150, 90, lid, 180, "chat lid", 45, "chat")
        return Decision(
            base.mood,
            base.hue,
            base.speed,
            lid,
            base.brightness,
            "chat lid",
            45,
            "chat",
        )

    def start_flow(self, request: dict) -> dict:
        """Slow calm breathing with a slowly cycling hue (a host-side loop)."""
        self.stop_flow()
        seconds = max(1, min(600, int(request.get("seconds", 30))))
        speed = max(0, min(255, int(request.get("speed", 10))))
        bright = max(0, min(255, int(request.get("bright", 150))))
        lid = max(0, min(100, int(request.get("lid", 80))))
        hue = int(request.get("hue", 150)) % 256
        step = max(1, min(32, int(request.get("hue_step", 3))))
        interval = max(0.05, min(2.0, float(request.get("interval_ms", 300)) / 1000.0))

        with self._lock:
            now = time.monotonic()
            if self.state.mood != "calm":
                self.state.history.append(f"{self.state.mood} {int(now - self.state.mood_since)}s")
                self.state.mood = "calm"
                self.state.mood_since = now
            self.state.lid = lid
            self.state.manual_until = now + seconds
            self.store.last_mood = "calm"
            self.store.save()

        stop = threading.Event()
        self._flow_stop = stop

        def run() -> None:
            current = hue
            end = time.monotonic() + seconds
            while not stop.is_set() and time.monotonic() < end:
                decision = Decision(
                    "calm", current, speed, lid, bright, "flow", 45, "chat"
                )
                with self._lock:
                    ok = self.link.send_mood(decision)
                    tick = time.monotonic()
                    self.state.lid = lid
                    self.state.last = decision
                    self.state.last_sent = decision.serial_line()
                    self.state.last_sent_at = tick
                    self.state.last_decision_at = tick
                    self.state.manual_until = tick + max(3.0, min(30.0, float(seconds)))
                    self.state.manual_last = decision
                    self.state.manual_sent_at = tick
                if not ok:
                    break
                current = (current + step) % 256
                stop.wait(interval)
            with self._lock:
                self.state.manual_until = time.monotonic() + 3.0

        self._flow_thread = threading.Thread(target=run, name="flow", daemon=True)
        self._flow_thread.start()
        return {
            "ok": True,
            "flow": True,
            "seconds": seconds,
            "speed": speed,
            "bright": bright,
            "lid": lid,
            "hue_step": step,
            "interval_ms": int(interval * 1000),
        }

    def stop_flow(self) -> None:
        stop = self._flow_stop
        if stop is not None:
            stop.set()
        thread = self._flow_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._flow_thread = None

    def release(self) -> dict:
        self.stop_flow()
        with self._lock:
            self.state.manual_until = 0.0
            self.state.manual_last = None
        return {"ok": True, "released": True}

    # ---------------- read ----------------
    def observe(self) -> dict:
        now = time.monotonic()
        raw = self.sensors.link.sensors
        mac = self.sensors.mac_context()
        music = self.sensors.music_bus()
        manual_for = max(0.0, float(getattr(self.state, "manual_until", 0.0)) - now)
        events = [
            {"name": ev.name, "detail": ev.detail, "ago_s": int(now - ev.at)}
            for ev in self.sensors.link.recent_events(60.0)
        ]
        return {
            "lamp": {
                "mood": self.state.mood,
                "lid": self.state.lid,
                "since_s": int(now - self.state.mood_since),
                "manual_for_s": int(manual_for),
                "last_line": self.state.last_sent,
            },
            "sensors": {
                "prox": int(raw.prox),
                "lux": round(float(raw.lux)),
                "energy": int(raw.energy),
                "mood": raw.mood,
                "lid": int(raw.lid),
            },
            "mac": mac,
            "music": {"energy": music[0], "beat": music[1], "user_present": music[2]},
            "drives": {k: round(float(v), 2) for k, v in self.store.drives.items()},
            "memory": list(self.store.memory)[-6:],
            "events": events[-6:],
        }
