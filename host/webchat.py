#!/usr/bin/env python3
"""Local web chat for the lamp.

A small standalone front-end: it does not own the serial port, it talks to the
already-running ambient_host.py control socket. That means you can close this
page and the lamp keeps running; this process can also restart without
disturbing the lamp.

    .venv/bin/python webchat.py --port 8765
    open http://127.0.0.1:8765

With DEEPSEEK_API_KEY (in the environment or host/.env) the chat is driven by
DeepSeek with four tools: lamp_light, lamp_blink, lamp_observe, lamp_release.
Without a key it falls back to a few fixed intents so the page is still usable.
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

from lampctl import send as control_send

LOG = logging.getLogger("webchat")
HERE = Path(__file__).resolve().parent
HISTORY_PATH = HERE / "webchat_history.json"
DEFAULT_SOCKET = os.environ.get("AMBIENT_SOCK", "/tmp/ambient-lamp.sock")

HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ambient Lamp</title>
<style>
  :root {
    --bg: #15120e;
    --panel: #201b15;
    --panel-2: #2a231b;
    --line: #3a3025;
    --text: #f2e8d8;
    --dim: #a99a86;
    --accent: #ffb45e;
    --accent-2: #ff8a5c;
    --lamp: #35291d;
    --ok: #8fce9b;
    --bad: #e0776a;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
    color: var(--text);
    background: radial-gradient(80% 70% at 30% 0%, #2b2116 0%, var(--bg) 60%);
  }
  .app {
    height: 100vh;
    overflow: hidden;
    display: grid;
    grid-template-columns: 310px 1fr;
    max-width: 1180px;
    margin: 0 auto;
  }
  aside {
    overflow-y: auto;
    border-right: 1px solid var(--line);
    padding: 26px 22px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    background: linear-gradient(180deg, rgba(255,180,94,.06), transparent 45%);
  }
  .brand { font-size: 20px; letter-spacing: .02em; font-weight: 650; }
  .sub { color: var(--dim); font-size: 12px; margin-top: 4px; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; }
  .dot.on { background: var(--ok); box-shadow: 0 0 10px rgba(143,206,155,.6); }
  .dot.off { background: var(--bad); }
  .status { display: flex; flex-direction: column; gap: 9px; }
  .row { display: flex; justify-content: space-between; align-items: baseline; font-size: 13px; }
  .row span { color: var(--dim); }
  .row b { font-weight: 550; font-variant-numeric: tabular-nums; }
  .quick { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: auto; }
  button {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 9px 10px;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
    transition: border-color .15s, transform .05s;
  }
  button:hover { border-color: var(--accent); }
  button:active { transform: translateY(1px); }
  .foot { color: var(--dim); font-size: 11px; line-height: 1.5; }
  main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .chatlog {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 30px 34px 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .msg {
    max-width: min(78%, 620px);
    padding: 10px 14px;
    border-radius: 14px;
    line-height: 1.55;
    font-size: 14px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .msg.user { align-self: flex-end; background: var(--accent); color: #24170a; border-bottom-right-radius: 4px; }
  .msg.lamp { align-self: flex-start; background: var(--lamp); border: 1px solid var(--line); border-bottom-left-radius: 4px; }
  .msg.sys { align-self: center; color: var(--dim); font-size: 12px; background: transparent; border: 1px dashed var(--line); }
  .composer {
    border-top: 1px solid var(--line);
    padding: 14px 22px 18px;
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }
  textarea {
    flex: 1;
    resize: none;
    min-height: 44px;
    max-height: 140px;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 11px 13px;
    font: inherit;
    font-size: 14px;
    line-height: 1.4;
    outline: none;
  }
  textarea:focus { border-color: var(--accent); }
  #send {
    background: var(--accent);
    color: #24170a;
    border: 0;
    font-weight: 650;
    padding: 11px 18px;
    border-radius: 12px;
  }
  #send:disabled { opacity: .5; cursor: default; }
  .hint { color: var(--dim); font-size: 11px; padding: 0 22px 10px; }
  @media (max-width: 760px) {
    .app { grid-template-columns: 1fr; height: auto; min-height: 100vh; overflow: visible; }
    .chatlog { max-height: 60vh; }
    aside { border-right: 0; border-bottom: 1px solid var(--line); padding: 18px; gap: 14px; }
    .quick { margin-top: 0; }
    .chatlog { padding: 18px; min-height: 55vh; }
    .msg { max-width: 88%; }
  }
</style>
</head>
<body>
<div class="app">
  <aside>
    <div>
      <div class="brand">Ambient Lamp</div>
      <div class="sub"><span id="dot" class="dot off"></span><span id="conn">connecting…</span></div>
    </div>
    <div class="status">
      <div class="row"><span>mood</span><b id="mood">—</b></div>
      <div class="row"><span>eyelid</span><b id="lid">—</b></div>
      <div class="row"><span>proximity</span><b id="prox">—</b></div>
      <div class="row"><span>light</span><b id="lux">—</b></div>
      <div class="row"><span>music</span><b id="music">—</b></div>
      <div class="row"><span>stream</span><b id="lease">—</b></div>
      <div class="row"><span>mac</span><b id="mac">—</b></div>
    </div>
    <div class="quick">
      <button data-action="darker">暗一点</button>
      <button data-action="brighter">亮一点</button>
      <button data-action="flowing">流动一点</button>
      <button data-action="blink">眨一下</button>
    </div>
    <div class="foot" id="llm">DeepSeek: checking…</div>
  </aside>
  <main>
    <div class="chatlog" id="log"></div>
    <div class="hint">Enter 发送，Shift+Enter 换行。它只会用光和眼睛回答。</div>
    <div class="composer">
      <textarea id="input" rows="1" placeholder="和它说点什么…"></textarea>
      <button id="send">发送</button>
    </div>
  </main>
</div>
<script>
const $ = (id) => document.getElementById(id);
const log = $("log");
const input = $("input");
const sendBtn = $("send");

function add(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

async function api(path, opts) {
  const response = await fetch(path, opts);
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

function setText(id, value) {
  $(id).textContent = value;
}

function renderStatus(d) {
  const online = !!d.online;
  $("dot").className = "dot " + (online ? "on" : "off");
  setText("conn", online ? "灯在线" : "灯离线");
  const lamp = d.lamp || {};
  const sensors = d.sensors || {};
  const music = d.music || {};
  const mac = d.mac || {};
  const light = lamp.light || {};
  setText("mood", "hue " + (light.hue ?? 0) + " · val " + (light.val ?? 0));
  setText("lid", (light.open ?? 0) + "%");
  setText("prox", (sensors.prox ?? 0) + (sensors.prox >= 120 ? " · 手在附近" : ""));
  setText("lux", (sensors.lux ?? 0) + " lx");
  setText("music", (music.energy ?? 0) + "/100" + (mac.playing ? " · " + (mac.title || "播放中") : ""));
  setText("lease", lamp.stream || "off");
  setText("mac", mac.user_present ? "在电脑前" : "离开了 " + (mac.idle_s ?? 0) + "s");
  setText("llm", d.llm ? "agent: ready" : "agent: off · reflex only");
}

async function refresh() {
  try {
    renderStatus(await api("/api/status"));
  } catch (err) {
    $("dot").className = "dot off";
    setText("conn", "webchat 离线");
  }
}

async function loadHistory() {
  try {
    const data = await api("/api/history");
    for (const item of data.messages || []) {
      add(item.role === "user" ? "user" : "lamp", item.content);
    }
  } catch (err) { /* first run */ }
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  add("user", text);
  sendBtn.disabled = true;
  const thinking = add("sys", "灯在感受…");
  try {
    const data = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text })
    });
    thinking.remove();
    add("lamp", data.reply || "……");
  } catch (err) {
    thinking.remove();
    add("lamp", "我刚才走神了。");
  } finally {
    sendBtn.disabled = false;
    input.focus();
    refresh();
  }
}

async function action(name) {
  try {
    const data = await api("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name })
    });
    add("lamp", data.ok ? "好。" : "没成功：" + (data.error || "灯没连上"));
  } catch (err) {
    add("lamp", "没成功：灯没连上。");
  }
  refresh();
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});
document.querySelectorAll("[data-action]").forEach((btn) => {
  btn.addEventListener("click", () => action(btn.dataset.action));
});

loadHistory();
refresh();
setInterval(refresh, 2000);
input.focus();
</script>
</body>
</html>
"""


def load_env() -> None:
    path = HERE / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ChatEngine:
    """A window onto the one agent living in ambient_host. No model here.

    The page owns no mind of its own: it forwards what you type to the host's
    agent (`say`) and shows the reply. That is why there is exactly one lamp
    personality and one place where the light is decided.
    """

    # Quick buttons are parameter writes, not modes.
    ACTIONS = {
        "darker": {"cmd": "light", "val": 70, "ease": 2500, "reason": "page: darker"},
        "brighter": {"cmd": "light", "val": 175, "ease": 2500, "reason": "page: brighter"},
        "flowing": {"cmd": "light", "grad": 30, "wave_amp": 40, "wave_hz": 14,
                    "ease": 3000, "reason": "page: flowing"},
        "blink": {"cmd": "blink", "kind": "blink"},
    }

    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self.socket_path = str(socket_path)
        self.history_path = HISTORY_PATH
        self.history: list[dict] = []
        self._status_cache: Optional[dict] = None
        self._status_at = 0.0
        self._load_history()

    # ---- control socket ----
    def _control(self, request: dict, timeout: float = 20.0) -> dict:
        try:
            result = control_send(request, self.socket_path, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - the page must stay alive
            result = {"ok": False, "online": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        if not result.get("ok"):
            LOG.warning("control %s failed: %s", request.get("cmd"),
                        result.get("error") or result)
        self._status_cache = None
        return result

    def status(self) -> dict:
        now = time.monotonic()
        if self._status_cache is not None and now - self._status_at < 1.0:
            return self._status_cache
        data = self._control({"cmd": "observe"}, timeout=4.0)
        if data.get("ok"):
            data["online"] = True
            data["llm"] = True          # the host answered, so it is running
        else:
            data = {"online": False, "llm": False, "lamp": {}, "sensors": {},
                    "mac": {}, "music": {}, "error": data.get("error", "")}
        self._status_cache = data
        self._status_at = now
        return data

    # ---- conversation ----
    def chat(self, message: str) -> str:
        text = " ".join(str(message).split())[:500]
        if not text:
            return "你还没说话。"
        result = self._control({"cmd": "say", "text": text}, timeout=25.0)
        if result.get("ok"):
            reply = str(result.get("reply") or "").strip()
            if not reply:
                actions = result.get("actions") or []
                reply = "（它动了一下灯，没说话。）" if actions else "……"
        elif result.get("offline"):
            reply = ("我现在只有反射——没有连上大脑（缺 API key）。"
                     "灯还在呼吸，也会对声音和靠近有反应。")
        else:
            reply = "我刚才走神了，没接上。"
            LOG.info("chat failed: %s", result.get("error"))
        self._remember("user", text)
        self._remember("assistant", reply)
        return reply

    # ---- buttons ----
    def action(self, name: str) -> dict:
        request = self.ACTIONS.get(str(name))
        if request is None:
            return {"ok": False, "error": f"unknown action {name!r}"}
        return self._control(dict(request), timeout=8.0)

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
    server_version = "AmbientWeb/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:  # noqa: N802
        engine: ChatEngine = self.server.engine  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif path == "/api/status":
            self._json(engine.status())
        elif path == "/api/history":
            self._json({"messages": engine.history})
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

        if path == "/api/action":
            name = str(data.get("name", ""))
            self._json(engine.action(name))
            return

        self._json({"ok": False, "error": "not found"}, 404)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine: ChatEngine) -> None:
        self.engine = engine
        super().__init__(address, WebHandler)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local web chat for the ambient lamp")
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
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
