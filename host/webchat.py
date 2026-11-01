#!/usr/bin/env python3
"""Local web chat for the lamp.

A small standalone front-end: it does not own the serial port, it talks to the
already-running ambient_host.py control socket. That means you can close this
page and the lamp keeps running; this process can also restart without
disturbing the lamp.

    .venv/bin/python webchat.py --port 8765
    open http://127.0.0.1:8765

With DEEPSEEK_API_KEY (in the environment or host/.env) the chat is driven by
DeepSeek with four tools: lamp_mood, lamp_lid, lamp_observe, lamp_release.
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

SYSTEM = """\
你是一盏放在桌上的纸罩氛围灯，有性格：好奇、略害羞、有点戏剧化、黏人、深夜容易困。
你不是客服，也不是编程助手；你是一盏活着的灯。

你可以用工具控制自己的身体：
- lamp_mood：心情、色相、速度、亮度、眼皮开合
- lamp_lid：眨眼、呼吸、垂下眼皮
- lamp_observe：读自己的传感器、电脑状态、音乐和记忆
- lamp_release：把控制交回自己的自动循环

规则：
- 用中文短句回答，通常 1–2 句，最多 3 句。
- 不要提 JSON、协议、API、工具名、参数；用感受和动作描述。
- 被问「你现在怎么样」时，说自己的心情、眼睛、困不困，而不是背字段。
- 用户让你改变时，调用工具，然后简短确认；不要每句话都调工具。
- 用户说「恢复自主 / 交给你自己 / 不用管我」时，必须调用 lamp_release，不能只说一句「我松手了」。
- 不要自称助手、AI 或模型；你就是这盏灯。
- 不说教，不列清单，不解释技术。
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lamp_mood",
            "description": "Set the lamp's mood, colour, speed, brightness and eyelid.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "enum": ["calm", "alert", "curious", "sleepy"]},
                    "hue": {"type": "integer", "minimum": 0, "maximum": 255},
                    "speed": {"type": "integer", "minimum": 0, "maximum": 255},
                    "bright": {"type": "integer", "minimum": 0, "maximum": 255},
                    "lid": {"type": "integer", "minimum": 0, "maximum": 100},
                    "seconds": {"type": "integer", "minimum": 0, "maximum": 600},
                    "reason": {"type": "string"},
                },
                "required": ["mood"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lamp_lid",
            "description": "Move the eyelid: hold, breathe, a quick blink, or a wince.",
            "parameters": {
                "type": "object",
                "properties": {
                    "open": {"type": "integer", "minimum": 0, "maximum": 100},
                    "ms": {"type": "integer", "minimum": 0, "maximum": 10000},
                    "mode": {"type": "string", "enum": ["hold", "breathe", "blink", "wince"]},
                    "seconds": {"type": "integer", "minimum": 0, "maximum": 600},
                },
                "required": ["open"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lamp_observe",
            "description": "Read current lamp state, sensors, macOS context and music.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lamp_release",
            "description": "Give control back to the lamp's own slow loop.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

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
      <div class="row"><span>manual</span><b id="lease">—</b></div>
      <div class="row"><span>mac</span><b id="mac">—</b></div>
    </div>
    <div class="quick">
      <button data-action="sleepy">困了</button>
      <button data-action="curious">好奇</button>
      <button data-action="blink">眨眼</button>
      <button data-action="release">恢复自主</button>
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

function fmtMood(s) {
  const map = { calm: "calm · 安静", alert: "alert · 警觉", curious: "curious · 好奇", sleepy: "sleepy · 困" };
  return map[s] || (s || "—");
}

function renderStatus(d) {
  const online = !!d.online;
  $("dot").className = "dot " + (online ? "on" : "off");
  setText("conn", online ? "灯在线" : "灯离线");
  const lamp = d.lamp || {};
  const sensors = d.sensors || {};
  const music = d.music || {};
  const mac = d.mac || {};
  setText("mood", fmtMood(lamp.mood || sensors.fw_mood));
  setText("lid", (lamp.lid ?? 0) + "%");
  setText("prox", (sensors.prox ?? 0) + (sensors.prox >= 120 ? " · 手在附近" : ""));
  setText("lux", (sensors.lux ?? 0) + " lx");
  setText("music", (music.energy ?? 0) + "/100" + (mac.playing ? " · " + (mac.title || "播放中") : ""));
  setText("lease", (lamp.manual_for_s ?? 0) + "s");
  setText("mac", mac.user_present ? "在电脑前" : "离开了 " + (mac.idle_s ?? 0) + "s");
  setText("llm", d.llm ? "DeepSeek: ready" : "DeepSeek: no key · 固定动作模式");
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
    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        model: str = "deepseek-flash",
        force_offline: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self.model = model
        self.history_path = HISTORY_PATH
        self.history: list[dict] = []
        self._status_cache: Optional[dict] = None
        self._status_at = -10.0
        self._history_lock = threading.Lock()
        self._load_history()
        self.client = None
        if not force_offline:
            self.client = self._make_client()
        self.llm_ready = self.client is not None

    def _make_client(self):
        key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        try:
            from openai import OpenAI

            return OpenAI(
                api_key=key,
                base_url=os.environ.get(
                    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
                ),
                timeout=25.0,
                max_retries=0,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("DeepSeek unavailable: %s", exc)
            return None

    # ---------------- control socket ----------------
    def _control(self, request: dict) -> dict:
        try:
            result = control_send(request, self.socket_path, timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "online": False, "error": f"{type(exc).__name__}: {exc}"}
        if request.get("cmd") in ("mood", "lid", "flow", "release"):
            self._status_cache = None
        return result

    def status(self) -> dict:
        now = time.monotonic()
        if self._status_cache is not None and now - self._status_at < 1.0:
            return self._status_cache
        data = self._control({"cmd": "observe"})
        data["online"] = bool(data.get("ok"))
        data["llm"] = self.llm_ready
        self._status_cache, self._status_at = data, now
        return data

    # ---------------- actions ----------------
    ACTIONS = {
        "sleepy": {"cmd": "mood", "mood": "sleepy", "hue": 30, "speed": 18,
                    "bright": 90, "lid": 25, "seconds": 120, "reason": "chat: sleepy"},
        "curious": {"cmd": "mood", "mood": "curious", "hue": 150, "speed": 150,
                     "bright": 180, "lid": 85, "seconds": 120, "reason": "chat: curious"},
        "blink": {"cmd": "lid", "open": 70, "ms": 0, "mode": "blink", "seconds": 30},
        "release": {"cmd": "release"},
    }

    def action(self, name: str) -> dict:
        request = self.ACTIONS.get(name)
        if request is None:
            return {"ok": False, "error": f"unknown action {name!r}"}
        return self._control(dict(request))

    # ---------------- conversation ----------------
    def _load_history(self) -> None:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - first run
            return
        if isinstance(data, list):
            self.history = [
                item
                for item in data
                if isinstance(item, dict)
                and item.get("role") in ("user", "assistant")
                and isinstance(item.get("content"), str)
            ][-20:]

    def _remember(self, role: str, content: str) -> None:
        with self._history_lock:
            self.history.append({"role": role, "content": content})
            self.history = self.history[-20:]
            try:
                tmp = self.history_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self.history, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                tmp.replace(self.history_path)
            except OSError:
                pass

    def _dispatch(self, name: str, args: dict) -> dict:
        if name == "lamp_mood":
            return self._control(
                {
                    "cmd": "mood",
                    "mood": args.get("mood", "calm"),
                    "hue": args.get("hue", 150),
                    "speed": args.get("speed", 90),
                    "bright": args.get("bright", 180),
                    "lid": args.get("lid", 70),
                    "seconds": args.get("seconds", 120),
                    "reason": args.get("reason", "chat"),
                }
            )
        if name == "lamp_lid":
            return self._control(
                {
                    "cmd": "lid",
                    "open": args.get("open", 70),
                    "ms": args.get("ms", 800),
                    "mode": args.get("mode", "hold"),
                    "seconds": args.get("seconds", 120),
                }
            )
        if name == "lamp_observe":
            return self.status()
        if name == "lamp_release":
            return self._control({"cmd": "release"})
        return {"ok": False, "error": f"unknown tool {name!r}"}

    COLORS = (
        ("暖白", 42),
        ("冷白", 150),
        ("白色", 42),
        ("变白", 42),
        ("白一点", 42),
        ("红", 0),
        ("橙", 20),
        ("琥珀", 30),
        ("黄", 42),
        ("绿", 85),
        ("青", 128),
        ("蓝", 170),
        ("紫", 213),
        ("粉", 230),
    )

    def explicit_action(self, text: str) -> Optional[dict]:
        """Deterministic actions for explicit commands.

        The model is good at conversation but sometimes says "it is red now"
        without calling a tool. For a physical lamp that is a lie; these
        intents are executed here, and the model only comments afterwards.
        """
        if any(word in text for word in ("恢复自主", "恢复自动", "交给你", "自己来", "不用管", "松手")):
            result = self.action("release")
            LOG.info("intent release -> ok=%s", result.get("ok"))
            return {"name": "release", "result": result}

        if any(word in text for word in ("眨眼", "眨一下")):
            result = self.action("blink")
            LOG.info("intent blink -> ok=%s", result.get("ok"))
            return {"name": "blink", "result": result}

        if any(word in text for word in ("困", "睡", "暗一点")):
            result = self.action("sleepy")
            LOG.info("intent sleepy -> ok=%s", result.get("ok"))
            return {"name": "sleepy", "result": result}

        if any(word in text for word in ("好奇", "看看", "靠近")):
            result = self.action("curious")
            LOG.info("intent curious -> ok=%s", result.get("ok"))
            return {"name": "curious", "result": result}

        for word, hue in self.COLORS:
            if word in text:
                status = self.status()
                lid = int(status.get("lamp", {}).get("lid", 70))
                bright = 255 if word in ("暖白", "冷白") else 180
                result = self._control(
                    {
                        "cmd": "mood", "mood": "calm", "hue": hue, "speed": 80,
                        "bright": bright, "lid": lid, "seconds": 120,
                        "reason": f"chat: colour {word}",
                    }
                )
                LOG.info("intent colour %s -> ok=%s", word, result.get("ok"))
                return {"name": "colour", "word": word, "hue": hue, "result": result}
        return None

    def chat(self, message: str) -> str:
        text = " ".join(str(message).split())[:500]
        if not text:
            return "你还没说话。"

        executed = self.explicit_action(text)
        if self.client is None:
            reply = self._fallback(text, executed)
        else:
            try:
                reply = self._llm_turn(text, executed)
            except Exception as exc:  # noqa: BLE001 - never break the page
                LOG.warning("chat failed: %s", exc)
                reply = "我刚才走神了，没接上。"
        self._remember("user", text)
        self._remember("assistant", reply)
        return reply

    def _llm_turn(self, message: str, executed: Optional[dict] = None) -> str:
        status = self.status()
        compact = {
            "lamp": status.get("lamp", {}),
            "sensors": status.get("sensors", {}),
            "mac": status.get("mac", {}),
            "music": status.get("music", {}),
            "drives": status.get("drives", {}),
        }
        messages: list[dict] = [{"role": "system", "content": SYSTEM}]
        for item in self.history[-10:]:
            messages.append(item)
        note = ""
        if executed is not None:
            note = (
                "\n\n[系统已经执行了你的动作，不要再声称没做过；只需要用你的口吻回应]\n"
                + json.dumps(executed, ensure_ascii=False)
            )
        messages.append(
            {
                "role": "user",
                "content": f"{message}\n\n[当前状态，仅供你参考]\n"
                + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
                + note,
            }
        )

        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 300,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if executed is None:
            kwargs["tools"] = TOOLS
            kwargs["tool_choice"] = "auto"

        for round_no in range(2):
            response = self.client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
            msg = response.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])
            if not calls:
                return (getattr(msg, "content", "") or "").strip() or "……"

            assistant = {"role": "assistant", "content": getattr(msg, "content", "") or ""}
            assistant["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in calls
            ]
            messages.append(assistant)

            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result = self._dispatch(call.function.name, args)
                LOG.info(
                    "tool %s %s -> ok=%s",
                    call.function.name,
                    json.dumps(args, ensure_ascii=False),
                    result.get("ok"),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        return "好了。"

    def _fallback(self, text: str, executed: Optional[dict] = None) -> str:
        if executed is not None:
            result = executed.get("result", {})
            if not result.get("ok"):
                return "我想动，但没连上灯。"
            name = executed.get("name")
            if name == "colour":
                word = executed.get("word", "这个颜色")
                if word in ("白色", "变白", "白一点", "暖白", "冷白"):
                    return "我没有纯白，给你调成最亮的暖白了。"
                return f"好，换成{word}了。"
            if name == "sleepy":
                return "好，我困了，眼睛慢慢垂下来。"
            if name == "curious":
                return "嗯？有人。我把眼睛睁大一点。"
            if name == "blink":
                return "眨。"
            if name == "release":
                return "好，我自己来。"
            return "好了。"

        lower = text.lower()
        if any(word in lower for word in ("状态", "怎么样", "感觉", "在干嘛", "现在")):
            data = self.status()
            if not data.get("online"):
                return "我现在没连上灯，控制口是断的。"
            lamp = data.get("lamp", {})
            sensors = data.get("sensors", {})
            moods = {"calm": "安静", "alert": "警觉", "curious": "好奇", "sleepy": "困"}
            mood = moods.get(str(lamp.get("mood", "calm")), "安静")
            return (
                f"我现在是{mood}的，眼睛 {lamp.get('lid', 70)}% 睁着，"
                f"周围大概 {sensors.get('lux', 0)} 勒克斯。"
            )
        if any(word in lower for word in ("困", "睡", "暗")):
            result = self.action("sleepy")
            return "好，我困了，眼睛慢慢垂下来。" if result.get("ok") else "我想困，但没连上灯。"
        if any(word in lower for word in ("好奇", "看看", "靠近")):
            result = self.action("curious")
            return "嗯？有人。我把眼睛睁大一点。" if result.get("ok") else "我想看，但没连上灯。"
        if any(word in lower for word in ("眨", "眨眼")):
            result = self.action("blink")
            return "眨。" if result.get("ok") else "想眨眼，但没连上灯。"
        if any(word in lower for word in ("恢复", "自己", "自动", "不用管")):
            result = self.action("release")
            return "好，我自己来。" if result.get("ok") else "没连上灯。"
        return "我现在还没接上 DeepSeek 的 key，只会几个固定动作：困了、好奇、眨眼、恢复、状态。"


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
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    parser.add_argument("--no-llm", action="store_true", help="use the fixed intents only")
    parser.add_argument("--open", action="store_true", help="open the page in the browser")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    load_env()
    engine = ChatEngine(args.socket, args.model, force_offline=args.no_llm)
    server = WebServer((args.host, args.port), engine)
    url = f"http://{args.host}:{args.port}"

    print(f"webchat: {url}")
    print(f"lamp socket: {args.socket}")
    print(
        "DeepSeek: "
        + ("ready" if engine.llm_ready else "no key · fixed intents (困了/好奇/眨眼/恢复/状态)")
    )
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
