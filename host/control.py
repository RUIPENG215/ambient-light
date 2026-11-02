"""Local control socket for the running lamp host.

The host owns the serial port and the one agent. Other processes (`lampctl.py`,
`webchat.py`, DSH's bash tool) talk to it over a Unix socket with
newline-delimited JSON.

Request:
    {"cmd": "light", "val": 90, "ease": 2500}
    {"cmd": "blink", "kind": "blink"}
    {"cmd": "say", "text": "a little darker"}
    {"cmd": "observe"}
    {"cmd": "ping"}

Response: one JSON object, always with "ok".

There are no leases any more. The agent owns the light; `light` writes the same
numbers the agent writes, which is what a debug tool or the web page does — it
does not fight a second mind, because there is no second mind.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from looks import FIELD_ORDER, LookEngine

DEFAULT_SOCKET = os.environ.get("AMBIENT_SOCK", "/tmp/ambient-lamp.sock")


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
        look_hz: float = 25.0,
        look_cap: int = 80,
        look_gamma: int = 0,
        look_dither: int = 1,
        reflex=None,
        senses=None,
        say=None,
    ) -> None:
        self.link = link
        self.state = state
        self.store = store
        self.sensors = sensors
        self.socket_path = str(socket_path)
        self.log = logger or logging.getLogger("control")
        self.senses = senses
        self.say_fn = say
        self._server: Optional[_ControlServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.engine = LookEngine(
            link,
            hz=look_hz,
            cap=look_cap,
            gamma=look_gamma,
            dither=look_dither,
            logger=self.log,
            reflex=reflex,
            senses=senses,
        )

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
        self.engine.stop()
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
        if cmd == "light":
            return self.set_light(request)
        if cmd == "blink":
            return self.blink(request)
        if cmd == "say":
            return self.say(request)
        raise ControlError(f"unknown command {cmd!r}")

    # ---------------- commands ----------------
    def set_light(self, request: dict) -> dict:
        """Write a few of the 11 numbers; the rest keep their value."""
        clean = {
            key: float(request[key])
            for key in FIELD_ORDER
            if key in request and request[key] is not None
        }
        base = getattr(self.state, "base_look", None) or self.engine._look
        if base is None:
            raise ControlError("no base look yet")
        look = replace(base, **clean) if clean else base

        with self._lock:
            self.state.base_look = look
            self.state.light_reason = str(request.get("reason", "control light"))[:120]
            if self.engine.running:
                self.engine.set("control", look)
            else:
                self.engine.start("control", look)
            self.state.last_sent = look.line()
            self.state.last_sent_at = time.monotonic()
            self.store.save()

        return {"ok": True, "line": look.line(), "params": look.fields(),
                "changed": sorted(clean)}

    def blink(self, request: dict) -> dict:
        kind = str(request.get("kind", "blink")).lower()
        cmd = "K,1" if kind == "wince" else "K,0"
        try:
            ok = bool(self.link.send_line(cmd))
        except Exception:  # noqa: BLE001
            ok = False
        return {"ok": ok, "line": cmd}

    def say(self, request: dict) -> dict:
        text = str(request.get("text") or request.get("message") or "").strip()
        if not text:
            raise ControlError("say needs some text")
        if self.say_fn is None:
            return {"ok": False, "error": "no agent attached"}
        return self.say_fn(text)

    # ---------------- read ----------------
    def _senses(self) -> Optional[dict]:
        try:
            if callable(self.senses):
                from reflex import as_senses

                snap = as_senses(self.senses())
                return {
                    "level": round(snap.level, 1),
                    "music": snap.music,
                    "beat": snap.beat,
                    "prox": snap.prox,
                    "lux": round(snap.lux, 1),
                    "idle_s": int(snap.idle_s),
                }
        except Exception:  # noqa: BLE001 - observe must never fail
            pass
        return None

    def observe(self) -> dict:
        raw = self.sensors.link.sensors
        mac = self.sensors.mac_context()
        music = self.sensors.music_bus()
        look = self.engine.status()
        light = look.get("params") or {}
        return {
            "lamp": {
                "light": light,
                "reason": getattr(self.state, "light_reason", ""),
                "stream": look["name"] if look["running"] else "",
                "frames": look["frames"],
                "hz": look["hz"],
                "line": look["line"],
            },
            "sensors": {
                "prox": int(raw.prox),
                "lux": round(float(raw.lux)),
                "energy": int(raw.energy),
                "origin": raw.mood,
                "open": int(raw.lid),
            },
            "senses": self._senses(),
            "reflex": look.get("reflex"),
            "mac": mac,
            "music": {"energy": music[0], "beat": music[1], "user_present": music[2]},
            "drives": {k: round(float(v), 2) for k, v in self.store.drives.items()},
            "memory": list(self.store.memory)[-6:],
            "events": [
                {"name": ev.name, "detail": ev.detail, "ago_s": int(time.monotonic() - ev.at)}
                for ev in self.sensors.link.recent_events(60.0)
            ][-6:],
        }
