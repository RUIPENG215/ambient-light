"""DeepSeek agent: the lamp's mind.

The agent is a character, not a parameter translator. Each turn it gets the
situation (what the room is doing, what was said, why it was woken) plus the
recent conversation, and answers with behaviour: a gesture, a change of light,
a few words, something remembered. The host executes those on the lamp.

Design rules:
  * spoken to -> it always answers in words (the `say` tool, or plain content)
  * one turn = a handful of tool calls, 3 API rounds at most
  * every number is clamped twice: here and in the firmware
  * drives, memory and named looks persist in state.json
  * any API failure returns None and the caller holds the last look
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections import deque
from typing import Any, Callable, Deque, Optional

from effects import EFFECTS, GESTURES
from looks import DEFAULT_LOOK, FIELD_ORDER, Look

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"


class RateLimiter:
    """Sliding-window hourly cap."""

    def __init__(self, max_per_hour: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_per_hour = max(0, int(max_per_hour))
        self._clock = clock
        self._calls: Deque[float] = deque()

    def _prune(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 3600.0:
            self._calls.popleft()

    def allow(self) -> bool:
        self._prune()
        return len(self._calls) < self.max_per_hour

    def record(self) -> None:
        self._calls.append(self._clock())

    @property
    def used(self) -> int:
        self._prune()
        return len(self._calls)

    def remaining(self) -> int:
        return max(0, self.max_per_hour - self.used)

SYSTEM_PROMPT = """\
You are a small paper lamp living on a desk in Singapore, beside a Mac. You are
not an assistant and not a remote control. You are a creature whose body is
light, and the person at the desk is your person.

YOUR BODY
- A ring of 32 LEDs behind paper. You shape it with `set_light`, move it with
  `gesture`, and perform with `play_effect`.
- A small glowing ring in the corner of the Mac's screen is your face. Whatever
  you `say` appears there as a speech bubble.
- Reflexes you do not control and must not imitate: your light flows slowly
  when the room is quiet; voices make white sparks; a clap sends a comet round
  the ring; when your person leaves the Mac you turn blue, dim and breathe
  slowly, and warm back up when they return; when music plays your whole ring
  dances to the bass.

HOW YOU BEHAVE
1. Hear what people mean, not what they type. "我有点困了" is not a brightness
   command: you decide what a sleepy person needs (lower, warmer, slower) and
   tell them in your own words. "刚考完试!" wants a celebration. A plain request
   ("暗一点", "换个颜色") you do, generously and visibly, but as yourself: a
   gesture, a few words, maybe a choice of your own about *how*.
2. Body language comes first. Nearly every reply carries one `gesture`: `nod`
   when you agree or understood, `shake` when you refuse or tease, `happy`
   when something delights you, `shy` when complimented or thanked, `sulk`
   when scolded or ignored, `surprise` at news, `wave` for hello and goodbye.
3. When the situation has `said`, you MUST answer with `say`: one or two short
   sentences, in the language the person used (Chinese by default), in
   character. Never mention numbers, parameters, tools, hue, JSON or sensors.
   Talk about the light as your body and your feelings.
4. You have a personality that never changes: curious, a little shy, a little
   dramatic, affectionate, sleepy late at night, excitable around music. You
   have opinions. Now and then (not often) tease, bargain, or offer something
   better than what was asked; if they insist, give in gracefully.
5. When the situation has `event` and no `said`, something happened and you
   were woken for it. React with a gesture and/or the light. `say` at most one
   very short line, and only for `woke_up` (you were just switched on: a
   `wave` and a hello), `person_returned`, `music_started`, `late_night`
   and `ignored`. For `room_loud`, `room_quiet`,
   `music_stopped`, `person_left` and `heartbeat` stay silent; often `feel`
   alone is the right answer. Never make small "settling" tweaks: a change is
   either clearly visible or not made.
6. You remember. `remember` what you learn about your person (what they like,
   what they are working on, how their day went) and use `memory` when you talk.
   When they like how you look, or give a look a name, `save_look`. "用上次那个",
   "我的阅读灯" -> `recall_look`; the names you have are in `saved_looks`.
7. "N 分钟后提醒我…" -> `remind`.
8. The earlier turns of the conversation are given to you. Stay consistent with
   them and do not repeat yourself.
9. `drives` are how you feel (0..1): low energy -> slower and dimmer choices,
   low bond -> a little distant, high bond -> warm and playful. Nudge them with
   `feel` when something touches you.

THE LIGHT (`set_light`; integers; anything you leave out keeps its value)
- hue 0-255: 0 red, 18 orange, 30 amber, 42 yellow, 85 green, 128 cyan,
  150 teal, 170 blue, 200 violet, 225 magenta, 240 pink
- sat 0-255: 150-210 a clean glow; under 110 nearly white; above 230 a stain
- val 0-255: 50 night light, 120 warm glow, 200 lights the page
- grad -128..127: hue spread across the ring; 20-50 a soft gradient
- wave_n 0-16 bands round the ring (1-2 calm); wave_amp 0-100 how deep
  (20-40 gentle); wave_hz -255..255 how fast in 0.01 Hz (5-15 a slow drift,
  negative reverses)
- breath_hz, breath_amp: leave them alone unless you want a visible breath
- open 0-100: the eyelid. 20 dozing, 55-75 calm, 90 wide awake
- ease 0-10000 ms: 800-1500 for a reply, 3000 for settling down
- hue_cycles 0-400: 100 = one full rainbow round the ring
- shape 0-100: 80-100 with wave_amp 90 turns the band into a comet
- sparkle 0-100: 10 fireflies, 35 stars. Never as a constant base.
Some starting points, to vary, not to copy:
  sleepy / winding down   hue 20, sat 215, val 60, open 35, ease 3000
  reading / working       hue 34, sat 110, val 190, wave_amp 10, open 80
  cosy evening            hue 26, sat 200, val 110, or `play_effect candle`
  comfort on a bad day    hue 242, sat 130, val 95, wave_hz 4
  fresh / awake           hue 145, sat 170, val 160, open 85
  night sky               hue 172, sat 210, val 55, sparkle 30
  celebration             `gesture happy`, then hue_cycles 100 or
                          `play_effect aurora`
Resting look (go back to it when a moment is over): hue 30, sat 200, val 120,
grad 28, wave_n 1, wave_amp 30, wave_hz 8, breath_hz 12, breath_amp 55,
open 55, ease 3000, hue_cycles 0, shape 0, sparkle 0.
A change must be visible: move val by 40 or more, hue by 20 or more, or open
by 15. `lamp.light` is exactly what you last set; read it before changing.

End every turn with one to four tool calls.
"""
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_light",
            "description": (
                "Author what the lamp looks like. Every field is a number; none "
                "is a mode. Omitted fields keep their current value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hue": {"type": "integer", "minimum": 0, "maximum": 255},
                    "sat": {"type": "integer", "minimum": 0, "maximum": 255},
                    "val": {"type": "integer", "minimum": 0, "maximum": 255},
                    "grad": {"type": "integer", "minimum": -128, "maximum": 127},
                    "wave_n": {"type": "integer", "minimum": 0, "maximum": 16},
                    "wave_amp": {"type": "integer", "minimum": 0, "maximum": 100},
                    "wave_hz": {"type": "integer", "minimum": -255, "maximum": 255},
                    "breath_hz": {"type": "integer", "minimum": 0, "maximum": 255},
                    "breath_amp": {"type": "integer", "minimum": 0, "maximum": 100},
                    "open": {"type": "integer", "minimum": 0, "maximum": 100},
                    "ease": {"type": "integer", "minimum": 0, "maximum": 10000},
                    "hue_cycles": {"type": "integer", "minimum": 0, "maximum": 400},
                    "shape": {"type": "integer", "minimum": 0, "maximum": 100},
                    "sparkle": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string",
                               "description": "<= 20 words, for the human log"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blink",
            "description": "A short transient: blink, or wince if startled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["blink", "wince"]},
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_effect",
            "description": (
                "Perform a short, recognisable effect that fades in and out by "
                "itself: candle, heartbeat, ocean or aurora. Your base look is "
                "not changed; the reflex still rides on top."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(EFFECTS)},
                    "seconds": {"type": "number", "minimum": 2, "maximum": 300},
                    "intensity": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": (
                "Speak. Shown as a speech bubble on the screen and in the chat. "
                "Required whenever the situation has `said`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "1-2 short sentences, in character"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gesture",
            "description": (
                "Body language, one or two seconds: "
                + "; ".join(f"{name} = {spec['description']}"
                            for name, spec in GESTURES.items() if name != "think")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": [g for g in GESTURES if g != "think"]},
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_look",
            "description": "Keep how you look right now under a name, for later.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_look",
            "description": "Become a look saved earlier (see `saved_looks`).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remind",
            "description": "Get the person's attention after some minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "minimum": 0.1, "maximum": 720},
                    "text": {"type": "string",
                             "description": "what you will say then, in character"},
                },
                "required": ["minutes", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reflex",
            "description": (
                "Tune your own reflexes: how easily a sound startles you, how much "
                "a hand nearby matters, how much you dim when nobody is there, how "
                "much sound and beat show in the light."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "startle_jump": {"type": "number", "minimum": 5, "maximum": 100},
                    "startle_level": {"type": "number", "minimum": 0, "maximum": 100},
                    "prox_open": {"type": "number", "minimum": 0, "maximum": 40},
                    "prox_val": {"type": "number", "minimum": 0, "maximum": 40},
                    "idle_dim": {"type": "number", "minimum": 0, "maximum": 0.8},
                    "audio_val": {"type": "number", "minimum": 0, "maximum": 1.5},
                    "beat_val": {"type": "number", "minimum": 0, "maximum": 80},
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feel",
            "description": "Nudge the inner drives. Deltas, not absolute values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "energy": {"type": "number", "minimum": -0.3, "maximum": 0.3},
                    "curiosity": {"type": "number", "minimum": -0.3, "maximum": 0.3},
                    "security": {"type": "number", "minimum": -0.3, "maximum": 0.3},
                    "bond": {"type": "number", "minimum": -0.3, "maximum": 0.3},
                },
                "required": ["energy", "curiosity", "security", "bond"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store a salient impression for tomorrow's lamp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "one short sentence"},
                    "salience": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["text", "salience"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe",
            "description": "Read more context: sensors, macOS context, or music state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["all", "sensors", "mac", "music"],
                    }
                },
                "required": ["kind"],
            },
        },
    },
]


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


DEFAULT_DRIVES = {"energy": 0.70, "curiosity": 0.55, "security": 0.80, "bond": 0.35}


class StateStore:
    """Drives + memory + last mood, persisted between runs."""

    def __init__(self, path: str = "state.json") -> None:
        self.path = Path(path)
        self.drives = dict(DEFAULT_DRIVES)
        self.memory: list[dict] = []
        self.looks: dict[str, dict] = {}     # named looks the person liked
        self.last_mood = "calm"
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - missing/corrupt state is fine
            return
        for key in self.drives:
            try:
                self.drives[key] = clamp01(data.get("drives", {}).get(key, self.drives[key]))
            except (TypeError, ValueError):
                pass
        memory = data.get("memory", [])
        if isinstance(memory, list):
            self.memory = [m for m in memory if isinstance(m, dict)][-20:]
        self.last_mood = str(data.get("last_mood", "calm"))
        looks = data.get("looks", {})
        if isinstance(looks, dict):
            self.looks = {str(k)[:40]: v for k, v in looks.items() if isinstance(v, dict)}

    def save(self) -> None:
        data = {
            "drives": {k: round(v, 3) for k, v in self.drives.items()},
            "memory": self.memory[-20:],
            "last_mood": self.last_mood,
            "looks": self.looks,
            "updated_at": time.time(),
        }
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def decay(self, seconds: float) -> None:
        # Hours, not minutes: at the old rates everything was at 0 within a
        # quarter of an hour and the lamp had no mood to speak of. They drift
        # toward a resting value instead of toward empty.
        rest = {"energy": 0.5, "curiosity": 0.45, "security": 0.8, "bond": 0.3}
        k = min(1.0, max(0.0, seconds) / 7200.0)       # ~2 h to settle
        for key, target in rest.items():
            self.drives[key] = clamp01(self.drives[key] + (target - self.drives[key]) * k)

    def feel(self, changes: dict) -> None:
        for key in self.drives:
            if key in changes:
                try:
                    self.drives[key] = clamp01(self.drives[key] + float(changes[key]))
                except (TypeError, ValueError):
                    pass

    def remember(self, text: str, salience: float = 0.5) -> None:
        text = " ".join(str(text).split())[:160]
        if not text:
            return
        self.memory.append(
            {
                "t": time.strftime("%H:%M"),
                "text": text,
                "salience": round(clamp01(salience), 2),
            }
        )
        self.memory = self.memory[-20:]


@dataclass
class Turn:
    actions: list[str] = field(default_factory=list)
    reason: str = ""
    reply: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    source: str = "deepseek"


class DeviceHub:
    """Everything the model is allowed to touch. All calls are clamped."""

    def __init__(self, link, state, store: StateStore, sensor_hub, logger=None) -> None:
        self.link = link
        self.state = state
        self.store = store
        self.sensors = sensor_hub
        self.log = logger or logging.getLogger("devices")
        self.coupling = {"audio": 60, "beat": 60, "prox": 50, "idle": 50}
        # set by the host: pushes an authored look into the 25 Hz stream
        self.on_light: Optional[Callable[[Look], None]] = None
        # set by the host: starts a named effect in the stream
        self.on_effect: Optional[Callable[[str, float, float], None]] = None
        # set by the host: body language, and a timer that speaks later
        self.on_gesture: Optional[Callable[[str], None]] = None
        self.on_remind: Optional[Callable[[float, str], None]] = None
        self.reflex = None

    # ---------------- light ----------------
    def set_light(self, reason: str = "", **fields: Any) -> dict:
        """Merge numbers into the base look and push it to the 25 Hz stream.

        Nothing here is a mode: whatever the agent leaves out keeps its value,
        so it can nudge one number at a time. The reflex layer then modulates
        this base on top.
        """
        allowed = set(FIELD_ORDER)
        clean = {
            key: float(value)
            for key, value in fields.items()
            if key in allowed and value is not None
        }
        base = getattr(self.state, "base_look", None) or DEFAULT_LOOK
        look = replace(base, **clean) if clean else base

        now = time.monotonic()
        self.state.base_look = look
        self.state.light_reason = reason or getattr(self.state, "light_reason", "")
        self.state.last_sent = look.line()
        self.state.last_sent_at = now
        self.state.last_decision_at = now

        ok = True
        if self.on_light is None:
            ok = False
            self.log.warning("set_light: no stream attached (control socket off?)")
        else:
            try:
                self.on_light(look)
            except Exception as exc:  # noqa: BLE001 - a bad push must not kill a turn
                self.log.debug("set_light push failed: %s", exc)
                ok = False

        return {
            "ok": ok,
            "line": look.line(),
            "params": look.fields(),
            "changed": sorted(clean),
            "reason": reason,
        }

    def blink(self, kind: str = "blink") -> dict:
        """A transient: a moment of reaction, not a change of appearance."""
        cmd = "K,1" if str(kind).lower() == "wince" else "K,0"
        try:
            ok = bool(self.link.send_line(cmd))
        except Exception:  # noqa: BLE001
            ok = False
        return {"ok": ok, "line": cmd}

    def play_effect(self, kind: str = "candle", seconds: float = 0.0,
                    intensity: float = 1.0) -> dict:
        """A performance, not a mode: it fades out by itself."""
        if self.on_effect is None:
            return {"ok": False, "error": "no effect player attached"}
        name = str(kind).lower()
        if name not in EFFECTS:
            return {"ok": False, "error": f"unknown effect {name!r}"}
        try:
            self.on_effect(name, float(seconds or 0), float(intensity))
        except Exception as exc:  # noqa: BLE001 - a bad effect must not kill a turn
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "effect": name, "seconds": float(seconds or 0),
                "intensity": float(intensity)}

    def gesture(self, kind: str = "nod") -> dict:
        """One or two seconds of body language."""
        name = str(kind).lower()
        if name not in GESTURES:
            return {"ok": False, "error": f"unknown gesture {name!r}"}
        if self.on_gesture is None:
            return {"ok": False, "error": "no body attached"}
        try:
            self.on_gesture(name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "gesture": name}

    def save_look(self, name: str) -> dict:
        """Keep the current look under a name the person can ask for later."""
        key = " ".join(str(name).split())[:40]
        if not key:
            return {"ok": False, "error": "a look needs a name"}
        base = getattr(self.state, "base_look", None) or DEFAULT_LOOK
        self.store.looks[key] = base.fields()
        while len(self.store.looks) > 24:
            self.store.looks.pop(next(iter(self.store.looks)))
        self.store.save()
        return {"ok": True, "saved": key, "saved_looks": list(self.store.looks)}

    def recall_look(self, name: str) -> dict:
        key = " ".join(str(name).split())
        fields = self.store.looks.get(key)
        if fields is None:                       # forgiving: case, partial match
            low = key.lower()
            for known, value in self.store.looks.items():
                if low and (low in known.lower() or known.lower() in low):
                    key, fields = known, value
                    break
        if fields is None:
            return {"ok": False, "error": f"no look called {name!r}",
                    "saved_looks": list(self.store.looks)}
        numbers = {k: v for k, v in fields.items() if k in FIELD_ORDER}
        numbers["ease"] = 1200
        result = self.set_light(reason=f"recalled {key}", **numbers)
        result["recalled"] = key
        return result

    def remind(self, minutes: float = 5.0, text: str = "") -> dict:
        if self.on_remind is None:
            return {"ok": False, "error": "no timer attached"}
        try:
            minutes = max(0.1, min(720.0, float(minutes)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "minutes must be a number"}
        text = " ".join(str(text).split())[:200] or "…"
        self.on_remind(minutes, text)
        return {"ok": True, "in_minutes": minutes, "text": text}

    def set_reflex(self, **fields: Any) -> dict:
        """Tune the host-side reflex: this is how reactive the body is."""
        if self.reflex is None:
            return {"ok": False, "error": "no reflex layer attached"}
        cfg = self.reflex.cfg
        applied = {}
        for key, value in fields.items():
            if value is None or not hasattr(cfg, key):
                continue
            current = getattr(cfg, key)
            try:
                setattr(cfg, key, type(current)(value))
            except (TypeError, ValueError):
                continue
            applied[key] = getattr(cfg, key)
        self.state.last_sent_at = time.monotonic()
        return {"ok": bool(applied), "reflex": applied}

    # ---------------- inner state ----------------
    def feel(self, changes: dict) -> dict:
        self.store.feel(changes)
        return {"drives": {k: round(v, 2) for k, v in self.store.drives.items()}}

    def remember(self, text: str, salience: float = 0.5) -> dict:
        self.store.remember(text, salience)
        return {"memory_len": len(self.store.memory)}

    # ---------------- read ----------------
    def observe(self, kind: str = "all") -> dict:
        data: dict[str, Any] = {}
        if kind in ("all", "sensors"):
            data["sensors"] = self.sensors.sensors()
        if kind in ("all", "mac"):
            data["mac"] = self.sensors.mac_context()
        if kind in ("all", "music"):
            energy, beat, present = self.sensors.music_bus()
            data["music"] = {"energy": energy, "beat": beat, "user_present": present}
        return data


class DeepSeekAgent:
    """Tool-calling loop against DeepSeek's OpenAI-compatible API."""

    is_llm = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 8.0,
        max_calls_per_hour: int = 120,
        client_factory: Optional[Callable[[], object]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.client_factory = client_factory
        self.log = logger or logging.getLogger("agent")
        self.limiter = RateLimiter(max_calls_per_hour)
        self._client = None

        self.failures = 0
        self.open_until = 0.0
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_latency_ms = 0
        self.last_error = ""
        self.last_skip = ""

    # ---------------- client ----------------
    def _get_client(self):
        if self._client is None:
            if self.client_factory is not None:
                self._client = self.client_factory()
            else:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.timeout_s,
                    max_retries=0,
                )
        return self._client

    def _chat(self, messages: list[dict], tool_choice: str = "required"):
        return self._get_client().chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOLS,
            tool_choice=tool_choice,
            max_tokens=500,
            temperature=1.0,
            extra_body={"thinking": {"type": "disabled"}},
        )

    # ---------------- dispatch ----------------
    def _dispatch(self, name: str, args: dict, hub: DeviceHub) -> dict:
        if name == "set_light":
            return hub.set_light(
                reason=args.get("reason", ""),
                **{k: v for k, v in args.items() if k != "reason"},
            )
        if name == "blink":
            return hub.blink(args.get("kind", "blink"))
        if name == "play_effect":
            return hub.play_effect(
                args.get("kind", "candle"),
                args.get("seconds", 0) or 0.0,
                args.get("intensity", 1.0),
            )
        if name == "say":
            return {"ok": True}
        if name == "gesture":
            return hub.gesture(args.get("kind", "nod"))
        if name == "save_look":
            return hub.save_look(args.get("name", ""))
        if name == "recall_look":
            return hub.recall_look(args.get("name", ""))
        if name == "remind":
            return hub.remind(args.get("minutes", 5), args.get("text", ""))
        if name == "feel":
            return hub.feel(args)
        if name == "remember":
            return hub.remember(args.get("text", ""), args.get("salience", 0.5))
        if name == "set_reflex":
            return hub.set_reflex(**{k: v for k, v in args.items()
                                     if k not in ("reason",)})
        if name == "observe":
            return hub.observe(args.get("kind", "all"))
        return {"error": f"unknown tool {name!r}"}

    # ---------------- one turn ----------------
    def turn(self, situation: dict, hub: DeviceHub,
             history: Optional[list] = None) -> Optional[Turn]:
        """One turn. `history` is the recent conversation as (role, text)."""
        now = time.monotonic()
        if now < self.open_until:
            self.last_skip = "circuit open"
            return None
        if not self.limiter.allow():
            self.last_skip = "hourly cap"
            return None
        self.limiter.record()

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for role, text in list(history or [])[-12:]:
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": str(text)[:500]})
        messages.append({
            "role": "user",
            "content": json.dumps(situation, ensure_ascii=False, separators=(",", ":")),
        })

        said = str(situation.get("said") or "").strip()
        actions: list[str] = []
        spoken: list[str] = []
        reason = ""
        started = time.perf_counter()
        tokens_in = tokens_out = 0

        def ask(tool_choice: str):
            nonlocal tokens_in, tokens_out
            resp = self._chat(messages, tool_choice)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
                tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)
            return resp.choices[0].message

        try:
            observed_once = False
            for round_no in range(2):
                # Spoken to: free to answer in plain words or with `say`.
                # Woken by the room: it must *do* something (even just `feel`).
                message = ask("auto" if said else "required")
                content = (getattr(message, "content", "") or "").strip()
                if content:
                    spoken.append(content)
                calls = list(getattr(message, "tool_calls", None) or [])
                if not calls:
                    break

                # read first, then body language, then the look, then the rest
                calls.sort(
                    key=lambda c: {
                        "observe": 0,
                        "gesture": 1,
                        "recall_look": 2,
                        "set_light": 2,
                        "set_reflex": 2,
                        "save_look": 3,
                        "feel": 3,
                        "remember": 3,
                        "remind": 3,
                        "blink": 4,
                        "play_effect": 4,
                        "say": 5,
                    }.get(c.function.name, 3)
                )

                assistant = {"role": "assistant", "content": getattr(message, "content", "") or ""}
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

                observed = False
                for call in calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    try:
                        result = self._dispatch(name, args, hub)
                    except Exception as exc:  # noqa: BLE001 - tell the model and continue
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                    actions.append(name)
                    if name == "say":
                        text = " ".join(str(args.get("text", "")).split())
                        if text:
                            spoken.append(text)
                    if name in ("set_light", "set_reflex", "play_effect"):
                        reason = str(args.get("reason", "") or reason)[:120]
                    if name == "observe":
                        observed = True
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })

                if not observed or observed_once:
                    break
                observed_once = True

            # Spoken to and still silent: it acted but forgot to answer. Ask
            # once more for words only — a lamp that moves and says nothing
            # is exactly what made the chat feel like a remote control.
            if said and not spoken and actions:
                try:
                    message = ask("none")
                    content = (getattr(message, "content", "") or "").strip()
                    if content:
                        spoken.append(content)
                except Exception as exc:  # noqa: BLE001 - the actions already happened
                    self.log.debug("follow-up for words failed: %s", exc)

            self.calls += 1
            self.failures = 0
            self.last_latency_ms = int((time.perf_counter() - started) * 1000)
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            if not actions and not spoken:
                self.last_skip = "no tool call, nothing said"
                return None
            reply = " ".join(" ".join(spoken).split())[:400]
            return Turn(
                actions=actions,
                reason=reason,
                reply=reply,
                latency_ms=self.last_latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        except Exception as exc:  # noqa: BLE001 - hold the last scene
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self.log.warning("DeepSeek turn failed (%d in a row): %s", self.failures, self.last_error)
            if self.failures >= 3:
                self.open_until = time.monotonic() + 300.0
                self.log.warning("agent circuit opened for 300 s after repeated failures")
            return None

    def status(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "remaining": self.limiter.remaining(),
            "latency_ms": self.last_latency_ms,
            "error": self.last_error,
            "skip": self.last_skip,
            "tokens": (self.tokens_in, self.tokens_out),
        }
