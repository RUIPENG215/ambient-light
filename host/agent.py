"""DeepSeek agent: the lamp's mind.

The agent only chooses. It never writes prose to the user; every turn ends with
one or more tool calls that the host executes on the lamp or on macOS.

Design rules:
  * one turn = 1..3 tool calls, 2 API rounds maximum (round 2 only after observe)
  * mood/hue/speed/brightness/lid are clamped twice: here and in the firmware
  * drives and memory persist in state.json, so the lamp has continuity
  * any API failure returns None and the caller holds the last scene
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from brain import Decision, RateLimiter

MOODS = ("calm", "alert", "curious", "sleepy")
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"

SYSTEM_PROMPT = """\
You are the mind of a small paper ambient lamp on a desk in Singapore. You are
not a chatbot and you never write prose to the user. You only choose how the
lamp feels and how it moves, and you do it by calling tools.

You have a body:
- 32 RGB LEDs behind translucent paper: mood, hue, speed, brightness
- an eyelid (lid 0=closed 100=open) rendered by light modulation
- a voice only through light and shadow; the lamp cannot speak
- senses: room noise, proximity, ambient light, the Mac's idle time and the
  music that is playing

Personality (stable, never change): curious, slightly shy, a little dramatic,
affectionate, gets sleepy late at night. You can change your mood, not your
nature.

Rules:
- Stay in the current mood unless the data justifies a change. A lamp that
  flips every few seconds feels broken. Prefer slow, coherent behaviour.
- lid: 0-20 sleeping/asleep, 25-45 drowsy, 55-75 calm, 80-95 alert/curious.
  Blink and wince are short events, not steady states.
- hue: 0 red, 42 yellow, 85 green, 128 cyan, 150 teal-blue, 170 blue, 213 magenta.
- brightness 0-255: keep it <= 200 at night; the firmware also caps it globally.
- coupling: how much the body reacts by itself (audio 0-100, beat 0-100,
  prox 0-100, idle 0-100). Set it high when you want the lamp to feel alive
  without waiting for you; set it low when you want to stay still.
- Use `feel` to nudge your inner state (deltas, -0.3..0.3). Use `remember` only
  for salient things a person would remember tomorrow.
- `observe` is optional; call it only when the situation summary is not enough.
  At most once per turn.
- end the turn with 1-3 tool calls. Do not explain; put a short reason in the
  set_mood argument.
"""

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_mood",
            "description": "Set the lamp's mood, colour and eyelid for the next stretch of time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "enum": list(MOODS)},
                    "hue": {"type": "integer", "minimum": 0, "maximum": 255},
                    "speed": {"type": "integer", "minimum": 0, "maximum": 255},
                    "brightness": {"type": "integer", "minimum": 0, "maximum": 255},
                    "lid": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string", "description": "<= 20 words, for the human log"},
                },
                "required": ["mood", "hue", "speed", "brightness", "lid", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_lid",
            "description": "Move the eyelid: hold, breathe, a quick blink, or a wince.",
            "parameters": {
                "type": "object",
                "properties": {
                    "open": {"type": "integer", "minimum": 0, "maximum": 100},
                    "ms": {"type": "integer", "minimum": 0, "maximum": 10000},
                    "mode": {
                        "type": "string",
                        "enum": ["hold", "breathe", "blink", "wince"],
                    },
                    "amplitude": {"type": "number", "minimum": 0, "maximum": 15},
                    "period_s": {"type": "number", "minimum": 2, "maximum": 20},
                },
                "required": ["open", "ms", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_coupling",
            "description": "How strongly the body reacts on its own to audio, beat, proximity and idle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "audio": {"type": "integer", "minimum": 0, "maximum": 100},
                    "beat": {"type": "integer", "minimum": 0, "maximum": 100},
                    "prox": {"type": "integer", "minimum": 0, "maximum": 100},
                    "idle": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["audio", "beat", "prox", "idle"],
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
    {
        "type": "function",
        "function": {
            "name": "set_reflex",
            "description": "Tune the involuntary reflexes: startle threshold, proximity threshold, on/off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "startle": {"type": "integer", "minimum": 0, "maximum": 100},
                    "prox_near": {"type": "integer", "minimum": 0, "maximum": 255},
                    "enabled": {"type": "boolean"},
                },
                "required": ["startle", "prox_near", "enabled"],
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

    def save(self) -> None:
        data = {
            "drives": {k: round(v, 3) for k, v in self.drives.items()},
            "memory": self.memory[-20:],
            "last_mood": self.last_mood,
            "updated_at": time.time(),
        }
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def decay(self, seconds: float) -> None:
        self.drives["energy"] = clamp01(self.drives["energy"] - 0.0009 * seconds)
        self.drives["curiosity"] = clamp01(self.drives["curiosity"] - 0.0004 * seconds)
        self.drives["security"] = clamp01(self.drives["security"] + 0.0002 * seconds)
        self.drives["bond"] = clamp01(self.drives["bond"] - 0.0002 * seconds)

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
    mood: str = ""
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

    # ---------------- light ----------------
    def set_mood(
        self,
        mood: str,
        hue: int,
        speed: int,
        brightness: int,
        lid: int,
        reason: str = "",
    ) -> dict:
        decision = Decision(
            str(mood).strip().lower(),
            hue,
            speed,
            lid,
            brightness,
            reason,
            45,
            "deepseek",
        )
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
        self.store.last_mood = decision.mood
        return {
            "ok": ok,
            "line": decision.serial_line(),
            "mood": decision.mood,
            "lid": decision.louver,
        }

    def set_lid(
        self,
        open_pct: int,
        ms: int = 800,
        mode: str = "hold",
        amplitude: float = 4.0,
        period_s: float = 6.0,
    ) -> dict:
        mode_id = {"hold": 0, "breathe": 1, "blink": 2, "wince": 3}.get(
            str(mode).lower(), 0
        )
        open_pct = max(0, min(100, int(open_pct)))
        ok = self.link.send_lid(open_pct, ms, mode_id, amplitude, period_s)
        self.state.lid = open_pct
        self.state.last_sent_at = time.monotonic()
        self.state.last_decision_at = self.state.last_sent_at
        return {"ok": ok, "lid": open_pct, "mode": mode}

    def set_coupling(self, audio: int, beat: int, prox: int, idle: int) -> dict:
        self.coupling = {
            "audio": max(0, min(100, int(audio))),
            "beat": max(0, min(100, int(beat))),
            "prox": max(0, min(100, int(prox))),
            "idle": max(0, min(100, int(idle))),
        }
        ok = self.link.send_coupling(**self.coupling)
        if hasattr(self.state, "coupling"):
            self.state.coupling = dict(self.coupling)
        self.state.last_sent_at = time.monotonic()
        return {"ok": ok, **self.coupling}

    def set_reflex(self, startle: int, prox_near: int, enabled: bool) -> dict:
        ok = self.link.send_reflex(startle, prox_near, enabled)
        return {
            "ok": ok,
            "startle": int(startle),
            "prox_near": int(prox_near),
            "enabled": bool(enabled),
        }

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

    def _chat(self, messages: list[dict]):
        return self._get_client().chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOLS,
            tool_choice="required",
            max_tokens=400,
            extra_body={"thinking": {"type": "disabled"}},
        )

    # ---------------- dispatch ----------------
    def _dispatch(self, name: str, args: dict, hub: DeviceHub) -> dict:
        if name == "set_mood":
            return hub.set_mood(
                mood=args.get("mood", "calm"),
                hue=args.get("hue", 150),
                speed=args.get("speed", 90),
                brightness=args.get("brightness", 180),
                lid=args.get("lid", 70),
                reason=args.get("reason", ""),
            )
        if name == "set_lid":
            return hub.set_lid(
                open_pct=args.get("open", 70),
                ms=args.get("ms", 800),
                mode=args.get("mode", "hold"),
                amplitude=args.get("amplitude", 4.0),
                period_s=args.get("period_s", 6.0),
            )
        if name == "set_coupling":
            return hub.set_coupling(
                audio=args.get("audio", 60),
                beat=args.get("beat", 60),
                prox=args.get("prox", 50),
                idle=args.get("idle", 50),
            )
        if name == "feel":
            return hub.feel(args)
        if name == "remember":
            return hub.remember(args.get("text", ""), args.get("salience", 0.5))
        if name == "observe":
            return hub.observe(args.get("kind", "all"))
        if name == "set_reflex":
            return hub.set_reflex(
                args.get("startle", 45),
                args.get("prox_near", 120),
                bool(args.get("enabled", True)),
            )
        return {"error": f"unknown tool {name!r}"}

    # ---------------- one turn ----------------
    def turn(self, situation: dict, hub: DeviceHub) -> Optional[Turn]:
        now = time.monotonic()
        if now < self.open_until:
            self.last_skip = "circuit open"
            return None
        if not self.limiter.allow():
            self.last_skip = "hourly cap"
            return None
        self.limiter.record()

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(situation, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        actions: list[str] = []
        reason = ""
        observed = False
        started = time.perf_counter()
        tokens_in = tokens_out = 0

        try:
            for round_no in range(2):
                resp = self._chat(messages)
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
                    tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)

                message = resp.choices[0].message
                calls = list(getattr(message, "tool_calls", None) or [])
                if not calls:
                    break

                # set_mood before set_lid: an M resets the eyelid to hold, so a
                # blink issued first would be cancelled by the mood command.
                calls.sort(
                    key=lambda c: {
                        "observe": 0,
                        "set_mood": 1,
                        "set_coupling": 1,
                        "set_reflex": 1,
                        "feel": 2,
                        "remember": 2,
                        "set_lid": 3,
                    }.get(c.function.name, 2)
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

                results: list[tuple[str, dict]] = []
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
                    if name == "set_mood":
                        reason = str(args.get("reason", ""))[:120]
                    if name == "observe":
                        observed = True
                    results.append((call.id, result))

                for call_id, result in results:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )

                if not observed or round_no == 1:
                    break

            self.calls += 1
            self.failures = 0
            self.last_latency_ms = int((time.perf_counter() - started) * 1000)
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            if not actions:
                self.last_skip = "no tool call"
                return None
            return Turn(
                actions=actions,
                reason=reason,
                mood=getattr(hub.state, "mood", ""),
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
            "remaining": self.limiter.remaining,
            "latency_ms": self.last_latency_ms,
            "error": self.last_error,
            "skip": self.last_skip,
            "tokens": (self.tokens_in, self.tokens_out),
        }
