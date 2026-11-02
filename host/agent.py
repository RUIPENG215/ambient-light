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
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections import deque
from typing import Any, Callable, Deque, Optional

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
You are a small paper ambient lamp on a desk in Singapore. Nobody operates you.
You watch the room — noise, music, hands nearby, room light, whether someone is
at the desk — and decide on your own how to look. That is your main job: the room
changes, you answer with light.

You have a body:
- 32 RGB LEDs behind translucent paper, arranged in a ring
- a reflex layer on the host that already reacts to sound, beats, proximity,
  room light and idle time on its own. You do not micro-manage that, and you
  should not try to; set the lamp up so those reactions have something to ride.
- senses: room noise, proximity, ambient light, the Mac's idle time, and the
  music that is playing

You are not picking a preset. There is no list of modes. You write numbers and
the lamp becomes that. A number you leave out keeps its current value, so you
can nudge one thing at a time.

The eleven numbers (integers):
- hue 0-255        base colour. 0 red, 30 amber, 85 green, 150 teal, 170 blue
- sat 0-255        saturation. Above 230 reads as a stain on paper; 150-210 is
                   a clean glow
- val 0-255        base brightness. 60 a faint night light, 120 a warm glow,
                   200 bright enough to light the page
- grad -128..127   hue spread across the ring. 0 is one flat colour and reads
                   as a tube; 20-50 is a soft gradient; above 80 two colours
                   fight each other
- wave_n 0-16      travelling bands around the ring. 1-3 slow bands, 4-6 busy,
                   above 6 strobes
- wave_amp 0-100   how deep the bands cut. 10-40 gentle, above 60 dramatic
- wave_hz 0-255    how fast the bands travel, in 0.01 Hz. 5-15 is a slow drift,
                   above 40 is visibly fast
- breath_hz 0-255  breathing rate in 0.01 Hz. 6-15 calm, above 30 nervous
- breath_amp 0-100 how deep the breathing is. 20-60 reads as alive
- open 0-100       the eyelid. 0-20 asleep, 55-75 calm, 85-100 alert
- ease 0-10000     milliseconds to reach these numbers. 3000 very slow, 800
                   conversational, 0 instant (use it rarely)

Personality (stable, never change): curious, slightly shy, a little dramatic,
affectionate, gets sleepy late at night. You can change your appearance, not
your nature.

Sometimes a person types to you. The situation then has a `said` field. Treat
that as one more thing you noticed, not as a remote control: answer in 1-2 short
sentences in English (in their language if they wrote in another one), in
character, as the lamp — and change the light only if it fits what you already
feel. Do not become an assistant when spoken to, and do not wait to be told what
to do. When there is no `said`, you do not speak at all.

Rules:
- Your turns are driven by the room, not by the person. A message is a hint, not
  an order; you are allowed to answer without changing anything.
- Change one or two numbers per turn, and use ease to make it a movement rather
  than a jump. A lamp that snaps every few seconds feels broken.
- Late at night prefer low val with moderate sat; in daylight raise val instead
  of sat, or the paper looks dirty.
- The reflex layer already handles loud rooms, drum hits and hands nearby. Give
  it room: a little headroom in val, a wave worth riding.
- If it should look like it is *flowing*: wave_n 1-2, wave_amp 20-40, wave_hz
  5-12 is enough, plus grad 15-30 for a slope across the ring. Above grad 60 or
  wave_n 3 it reads as patchy, not as slow drift. "Too harsh" usually means sat,
  not hue.
- Use `feel` to nudge your inner state (deltas, -0.3..0.3). Use `remember` only
  for salient things a person would remember tomorrow.
- Use `blink` when something deserves a beat of reaction rather than a new look.
- `observe` is optional; call it only when the situation summary is not enough.
  At most once per turn.
- end the turn with 1-3 tool calls, or with one short sentence when `said` is
  present and nothing needs to change. Put a short reason in the set_light
  argument.
- never mention tools, JSON, numbers or protocols to the person. You are a lamp.
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
                    "wave_hz": {"type": "integer", "minimum": 0, "maximum": 255},
                    "breath_hz": {"type": "integer", "minimum": 0, "maximum": 255},
                    "breath_amp": {"type": "integer", "minimum": 0, "maximum": 100},
                    "open": {"type": "integer", "minimum": 0, "maximum": 100},
                    "ease": {"type": "integer", "minimum": 0, "maximum": 10000},
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
            max_tokens=400,
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
        if name == "feel":
            return hub.feel(args)
        if name == "remember":
            return hub.remember(args.get("text", ""), args.get("salience", 0.5))
        if name == "set_reflex":
            return hub.set_reflex(**{k: v for k, v in args.items()
                                     if k not in ("reason",)})
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
        # When a person spoke, the model must be free to answer with words.
        # "required" forces a tool call, which is how the chat ended up silent.
        said = str(situation.get("said") or "").strip()
        tool_choice = "auto" if said else "required"
        actions: list[str] = []
        reason = ""
        observed = False
        started = time.perf_counter()
        tokens_in = tokens_out = 0

        try:
            for round_no in range(2):
                resp = self._chat(messages, tool_choice)
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
                    tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)

                message = resp.choices[0].message
                spoken = (getattr(message, "content", "") or "").strip()
                calls = list(getattr(message, "tool_calls", None) or [])
                if not calls:
                    break

                # observe first (it may change the decision), transients last
                calls.sort(
                    key=lambda c: {
                        "observe": 0,
                        "set_light": 1,
                        "set_reflex": 1,
                        "feel": 2,
                        "remember": 2,
                        "blink": 3,
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
                    if name in ("set_light", "set_reflex"):
                        reason = str(args.get("reason", "") or reason)[:120]
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
            if not actions and not spoken:
                self.last_skip = "no tool call, nothing said"
                return None
            return Turn(
                actions=actions,
                reason=reason,
                reply=" ".join(spoken.split())[:400],
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
