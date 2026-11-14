#!/usr/bin/env python3
"""The lamp's face and panel, served locally.

One page (`ui.html`) with two shapes: a small glowing ring that mirrors the
paper lamp and shows what it says as speech bubbles, and — when opened — a
panel to talk to it, mix colours, save named looks and try its gestures.

It owns no model and no serial port. Everything goes through the host's control
socket, so there is still exactly one mind and one writer:

    .venv/bin/python webchat.py --port 8765 --open     # in a browser
    .venv/bin/python lamp_app.py                        # as the corner pet

`lamp_app.py` puts the same page in a frameless always-on-top window.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import paths
from lampctl import send as control_send

LOG = logging.getLogger("webchat")
HERE = Path(__file__).resolve().parent
HISTORY_PATH = paths.data_dir() / "webchat_history.json"
DEFAULT_SOCKET = os.environ.get("AMBIENT_SOCK", "/tmp/ambient-lamp.sock")
UI_PATH = HERE / "ui.html"

LIGHT_FIELDS = ("hue", "sat", "val", "grad", "wave_n", "wave_amp", "wave_hz",
                "breath_hz", "breath_amp", "open", "ease",
                "hue_cycles", "shape", "sparkle")


def load_ui() -> bytes:
    try:
        return UI_PATH.read_bytes()
    except OSError:
        return b"<!doctype html><title>Ambient Lamp</title><p>ui.html is missing."


def load_env() -> None:
    paths.load_env()


class ChatEngine:
    """A window onto the one agent living in ambient_host. No model here.

    The page owns no mind of its own: it forwards what you type to the host's
    agent (`say`) and shows the reply. That is why there is exactly one lamp
    personality and one place where the light is decided.
    """

    # Quick actions are parameter writes or effects, not modes.
    ACTIONS = {
        "darker": {"cmd": "light", "val": 70, "ease": 2500, "reason": "page: darker"},
        "brighter": {"cmd": "light", "val": 175, "ease": 2500, "reason": "page: brighter"},
        "flowing": {"cmd": "light", "grad": 30, "wave_amp": 40, "wave_hz": 14,
                    "ease": 3000, "reason": "page: flowing"},
        "blink": {"cmd": "blink", "kind": "blink"},
        "candle": {"cmd": "effect", "name": "candle", "seconds": 60},
        "aurora": {"cmd": "effect", "name": "aurora", "seconds": 180},
    }

    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self.socket_path = str(socket_path)
        self.history_path = HISTORY_PATH
        self.history: list[dict] = []
        self._status_cache: Optional[dict] = None
        self._status_at = 0.0
        self._load_history()

    # ---- control socket ----
    def _control(self, request: dict, timeout: float = 20.0, quiet: bool = False) -> dict:
        try:
            result = control_send(request, self.socket_path, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - the page must stay alive
            result = {"ok": False, "online": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        if not result.get("ok") and not quiet:
            LOG.warning("control %s failed: %s", request.get("cmd"),
                        result.get("error") or result)
        if request.get("cmd") != "feed":
            self._status_cache = None
        return result

    def status(self) -> dict:
        now = time.monotonic()
        if self._status_cache is not None and now - self._status_at < 1.0:
            return self._status_cache
        data = self._control({"cmd": "observe"}, timeout=4.0, quiet=True)
        if data.get("ok"):
            data["online"] = True
            data["llm"] = True          # the host answered, so it is running
        else:
            data = {"online": False, "llm": False, "lamp": {}, "sensors": {},
                    "mac": {}, "music": {}, "error": data.get("error", "")}
        self._status_cache = data
        self._status_at = now
        return data

    def feed(self, since: int = 0) -> dict:
        """The frame on the wire and anything the lamp said: polled ~12 Hz."""
        return self._control({"cmd": "feed", "since": int(since)}, timeout=1.5, quiet=True)

    # ---- conversation ----
    def chat(self, message: str) -> str:
        text = " ".join(str(message).split())[:500]
        if not text:
            return "You have not said anything yet."
        result = self._control({"cmd": "say", "text": text}, timeout=30.0)
        if result.get("ok"):
            reply = str(result.get("reply") or "").strip()
            if not reply:
                actions = result.get("actions") or []
                reply = "(it moved the light, and said nothing)" if actions else "…"
        elif result.get("offline"):
            reply = ("I only have reflexes right now — no mind connected (no API key). "
                     "I am still breathing, and I still react to sound and to a hand.")
        else:
            reply = "I lost the thread, sorry."
            LOG.info("chat failed: %s", result.get("error"))
        self._remember("user", text)
        self._remember("assistant", reply)
        return reply

    # ---- light, looks, movement ----
    def light(self, data: dict) -> dict:
        request: dict = {"cmd": "light", "reason": str(data.get("reason", "panel"))[:80]}
        for key in LIGHT_FIELDS:
            if data.get(key) is not None:
                try:
                    request[key] = float(data[key])
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{key} must be a number"}
        if data.get("hold") is not None:
            request["hold"] = data["hold"]
        return self._control(request, timeout=4.0)

    def action(self, name: str) -> dict:
        request = self.ACTIONS.get(str(name))
        if request is None:
            return {"ok": False, "error": f"unknown action {name!r}"}
        return self._control(dict(request), timeout=8.0)

    def gesture(self, data: dict) -> dict:
        request = {"cmd": "gesture", "name": str(data.get("name", ""))}
        if data.get("seconds"):
            request["seconds"] = data["seconds"]
        return self._control(request, timeout=4.0)

    def effect(self, data: dict) -> dict:
        if data.get("stop"):
            return self._control({"cmd": "stop-effect"}, timeout=4.0)
        request = {"cmd": "effect", "name": str(data.get("name", ""))}
        for key in ("seconds", "intensity"):
            if data.get(key) is not None:
                request[key] = data[key]
        return self._control(request, timeout=4.0)

    def looks(self, op: str = "looks", name: str = "") -> dict:
        if op not in ("looks", "save_look", "recall_look", "forget_look"):
            return {"ok": False, "error": "unknown look operation"}
        return self._control({"cmd": op, "name": str(name)[:40]}, timeout=4.0)

    # ---- the mind's key ----
    def config(self) -> dict:
        feed = self.feed(0)
        return {
            "ok": True,
            "has_key": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "agent": bool(feed.get("agent")),
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        }

    def set_key(self, key: str) -> dict:
        key = str(key or "").strip()
        if len(key) < 8:
            return {"ok": False, "error": "that does not look like a key"}
        result = self._control({"cmd": "set_key", "key": key}, timeout=8.0)
        if result.get("ok"):
            try:
                paths.save_env_value("DEEPSEEK_API_KEY", key)   # for the next launch
            except OSError as exc:
                result["warning"] = f"could not save the key: {exc}"
        return result

    # ---- history ----
    def _load_history(self) -> None:
        try:
            raw = json.loads(Path(self.history_path).read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.history = [m for m in raw if isinstance(m, dict)][-40:]
        except (OSError, ValueError):
            self.history = []

    def _remember(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text, "at": int(time.time())})
        self.history = self.history[-40:]
        try:
            tmp = Path(str(self.history_path) + ".tmp")
            tmp.write_text(json.dumps(self.history, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.history_path)
        except OSError:
            pass


class WebHandler(BaseHTTPRequestHandler):
    server_version = "AmbientWeb/2.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                         # the page went away mid-poll

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:  # noqa: N802
        engine: ChatEngine = self.server.engine  # type: ignore[attr-defined]
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", load_ui())
        elif path == "/api/feed":
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._json(engine.feed(since))
        elif path == "/api/status":
            self._json(engine.status())
        elif path == "/api/history":
            self._json({"messages": engine.history})
        elif path == "/api/looks":
            self._json(engine.looks())
        elif path == "/api/config":
            self._json(engine.config())
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        engine: ChatEngine = self.server.engine  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length > 1_000_000:
            self._json({"ok": False, "error": "request too large"}, 413)
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "invalid json"}, 400)
            return
        if not isinstance(data, dict):
            self._json({"ok": False, "error": "expected an object"}, 400)
            return

        if path == "/api/chat":
            message = str(data.get("message", ""))[:500]
            if not message.strip():
                self._json({"ok": False, "error": "empty message"}, 400)
                return
            try:
                reply = engine.chat(message)
            except Exception as exc:  # noqa: BLE001 - keep the page alive
                LOG.exception("chat handler failed")
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._json({"ok": True, "reply": reply})
            return

        routes = {
            "/api/light": lambda: engine.light(data),
            "/api/action": lambda: engine.action(str(data.get("name", ""))),
            "/api/gesture": lambda: engine.gesture(data),
            "/api/effect": lambda: engine.effect(data),
            "/api/looks/save": lambda: engine.looks("save_look", data.get("name", "")),
            "/api/looks/recall": lambda: engine.looks("recall_look", data.get("name", "")),
            "/api/looks/forget": lambda: engine.looks("forget_look", data.get("name", "")),
            "/api/config": lambda: engine.set_key(data.get("key", "")),
        }
        handler = routes.get(path)
        if handler is None:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            self._json(handler())
        except Exception as exc:  # noqa: BLE001 - keep the page alive
            LOG.exception("%s failed", path)
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine: ChatEngine) -> None:
        self.engine = engine
        super().__init__(address, WebHandler)


def serve_in_thread(port: int = 8765, socket_path: str = DEFAULT_SOCKET,
                    host: str = "127.0.0.1") -> WebServer:
    """Start the page server on a daemon thread and return it (for the app)."""
    server = WebServer((host, port), ChatEngine(socket_path))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="The lamp's face and panel, served locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--open", action="store_true", help="open the page in the browser")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    load_env()
    engine = ChatEngine(args.socket)
    server = WebServer((args.host, args.port), engine)
    url = f"http://{args.host}:{args.port}"

    print(f"webchat: {url}")
    print(f"lamp socket: {args.socket}")
    print("agent: the one in ambient_host.py (this page owns no model)")
    print("Ctrl+C to stop. The lamp keeps running if you close this page.")

    if args.open:
        import webbrowser

        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
