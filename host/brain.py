"""Shared decision/state models and the legacy Claude decider.

`Decision`, `RateLimiter`, `build_context`, `fingerprint` and `LocalBrain` are
used by the v2.1 host. `LLMBrain` is the old Claude implementation, kept for
reference and tests; the live mind is `agent.DeepSeekAgent`.

`Decision.louver` keeps the old attribute name for compatibility; on the wire
the sixth field is now the eyelid (`lid`), not a louver.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

MOODS = ("calm", "alert", "curious", "sleepy")

MODEL_DEFAULT = "claude-haiku-5-5"

SYSTEM_PROMPT = """\
You are the mind of a small ambient lamp on a desk in Singapore. You are not a
chatbot: you never write prose to the user, you only choose how the lamp feels.

Each turn you receive a JSON summary of the last few seconds: noise relative to
the room's calibrated baseline, the proximity sensor, ambient light, the local
time, what the lamp is doing now, and a short history.

Choose one mood for the next stretch of time:
- calm    steady breathing; the default when nothing is happening
- alert   something sudden or loud; bright, fast, red/orange
- curious someone or something is close, or an interesting sound; sweeping teal
- sleepy  quiet for a long time, or very late; dim amber, very slow

Rules:
- Stay in the current mood unless the data justifies a change. A lamp that
  flips every update feels broken. Prefer calm at night, alert for sudden
  sounds, curious for nearby presence, sleepy after long quiet.
- hue: 0=red, 42=yellow, 85=green, 128=cyan, 170=blue, 213=magenta.
- speed: 0=very slow, 255=fast.
- brightness: 0-255; keep it <= 200 at night.
- louver: 0=closed, 100=open; open when alert or curious, close when sleepy.
- reason: at most 20 words, for the human's debug log.
- Always call set_mood exactly once.
"""

SET_MOOD_TOOL = {
    "name": "set_mood",
    "description": (
        "Set the lamp's mood and its animation parameters for the next stretch "
        "of time. Call exactly once per update."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "enum": list(MOODS)},
            "hue": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "description": "0=red, 42=yellow, 85=green, 128=cyan, 170=blue",
            },
            "speed": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "description": "animation speed, 0=very slow",
            },
            "louver": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "paper louver: 0=closed, 100=open",
            },
            "brightness": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "description": "optional, default 180",
            },
            "reason": {"type": "string", "description": "<= 20 words, for the log"},
            "duration_s": {
                "type": "integer",
                "minimum": 5,
                "maximum": 300,
                "description": "how long this mood should roughly hold",
            },
        },
        "required": ["mood", "hue", "speed", "louver"],
    },
}


def _clampi(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


@dataclass
class Decision:
    mood: str
    hue: int = 150
    speed: int = 90
    louver: int = 30
    brightness: int = 180
    reason: str = ""
    duration_s: int = 45
    source: str = "llm"

    def __post_init__(self) -> None:
        self.mood = str(self.mood).strip().lower()
        if self.mood not in MOODS:
            raise ValueError(f"unknown mood: {self.mood!r}")
        self.hue = _clampi(self.hue, 0, 255, 150)
        self.speed = _clampi(self.speed, 0, 255, 90)
        self.louver = _clampi(self.louver, 0, 100, 30)
        self.brightness = _clampi(self.brightness, 0, 255, 180)
        self.duration_s = _clampi(self.duration_s, 5, 300, 45)
        self.reason = " ".join(str(self.reason).split())[:100]

    @classmethod
    def from_tool_input(cls, data, source: str = "llm") -> "Decision":
        if not isinstance(data, dict):
            raise ValueError("tool input is not an object")
        return cls(
            mood=data.get("mood", "calm"),
            hue=data.get("hue", 150),
            speed=data.get("speed", 90),
            louver=data.get("louver", 30),
            brightness=data.get("brightness", 180),
            reason=data.get("reason", ""),
            duration_s=data.get("duration_s", 45),
            source=source,
        )

    def serial_line(self) -> str:
        return (
            f"M,{self.mood},{self.hue},{self.speed},"
            f"{self.brightness},{self.louver}"
        )


class RateLimiter:
    """Sliding-window hourly cap."""

    def __init__(self, max_per_hour: int, clock: Callable[[], float] = time.monotonic) -> None:
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

    @property
    def remaining(self) -> int:
        return max(0, self.max_per_hour - self.used)


class LLMBrain:
    """Calls Claude with a forced set_mood tool call."""

    is_llm = True

    def __init__(
        self,
        model: str = MODEL_DEFAULT,
        api_key: Optional[str] = None,
        timeout_s: float = 8.0,
        max_calls_per_hour: int = 40,
        min_interval_s: float = 25.0,
        client_factory: Optional[Callable[[], object]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self.limiter = RateLimiter(max_calls_per_hour)
        self.log = logger or logging.getLogger("brain")

        self._client_factory = client_factory
        self._client = None

        self.last_attempt = 0.0
        self.last_latency_ms = 0
        self.last_error = ""
        self.last_skip = ""
        self.failures = 0
        self.open_until = 0.0
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import anthropic

                self._client = anthropic.Anthropic(
                    api_key=self.api_key,
                    timeout=self.timeout_s,
                    max_retries=0,
                )
        return self._client

    def gate(self, now: Optional[float] = None) -> tuple[bool, str]:
        now = time.monotonic() if now is None else now
        if now < self.open_until:
            return False, "circuit open"
        if now - self.last_attempt < self.min_interval_s:
            return False, "min interval"
        if not self.limiter.allow():
            return False, "hourly cap"
        return True, ""

    def decide(self, context: dict) -> Optional[Decision]:
        now = time.monotonic()
        ok, why = self.gate(now)
        if not ok:
            self.last_skip = why
            return None

        self.last_attempt = now
        self.limiter.record()
        started = time.perf_counter()
        try:
            payload = self._call(context)
            decision = Decision.from_tool_input(payload, source="llm")
        except Exception as exc:  # noqa: BLE001 - any API/parse problem holds state
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self.log.warning("LLM call failed (%d in a row): %s", self.failures, self.last_error)
            if self.failures >= 3:
                self.open_until = now + 300.0
                self.log.warning("circuit opened for 300 s after repeated failures")
            return None

        self.calls += 1
        self.failures = 0
        self.last_latency_ms = int((time.perf_counter() - started) * 1000)
        return decision

    def _call(self, context: dict) -> dict:
        client = self._get_client()
        resp = client.messages.create(
            model=self.model,
            max_tokens=250,
            system=SYSTEM_PROMPT,
            tools=[SET_MOOD_TOOL],
            tool_choice={"type": "tool", "name": "set_mood"},
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                }
            ],
        )

        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.tokens_in += int(getattr(usage, "input_tokens", 0) or 0)
            self.tokens_out += int(getattr(usage, "output_tokens", 0) or 0)

        for block in getattr(resp, "content", []) or []:
            if (
                getattr(block, "type", "") == "tool_use"
                and getattr(block, "name", "") == "set_mood"
            ):
                return dict(getattr(block, "input", {}) or {})

        raise RuntimeError("model returned no set_mood tool_use block")

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


class LocalBrain:
    """No-network fallback: enough personality to demo without an API key."""

    is_llm = False

    def __init__(self, dwell_s: float = 8.0, logger: Optional[logging.Logger] = None) -> None:
        self.dwell_s = dwell_s
        self.log = logger or logging.getLogger("brain")
        self._last_mood: Optional[str] = None
        self._last_at = 0.0
        self.calls = 0

    def decide(self, context: dict) -> Optional[Decision]:
        now = time.monotonic()
        noise = context["noise"]
        prox = int(context["prox"]["now"])
        lux = float(context["light_lux"])
        hour = int(context["hour"])
        since = int(context["lamp"]["since_s"])

        # Only sustained loudness becomes alert. A single clap is the
        # firmware's job (STARTLE + wince); if the host also turned every
        # transient into a 30 s alert the lamp would flash non-stop.
        if noise["mean"] >= 50 or (noise["p95"] >= 75 and noise["mean"] >= 30):
            decision = Decision("alert", 8, 230, 95, 235, "local: loud", 30, "local")
        elif prox >= 120:
            decision = Decision("curious", 150, 150, 85, 210, "local: presence", 25, "local")
        elif noise["mean"] <= 6 and since >= 45 and (hour >= 22 or lux <= 25):
            decision = Decision("sleepy", 30, 18, 25, 90, "local: quiet", 90, "local")
        else:
            night = hour >= 21 or lux <= 25
            decision = Decision(
                "calm",
                38 if night else 150,
                80,
                70,
                180,
                "local: baseline",
                45,
                "local",
            )

        # Local fallback needs a minimum dwell for ANY mood change, not just
        # for repeating the same one. Otherwise a noisy window can flip
        # calm<->alert every few seconds; the firmware's own reflex already
        # handles the first 20 ms of a startle.
        if (
            self._last_mood is not None
            and decision.mood != self._last_mood
            and since < self.dwell_s
        ):
            return None
        if decision.mood == self._last_mood and now - self._last_at < self.dwell_s:
            return None
        self._last_mood = decision.mood
        self._last_at = now
        self.calls += 1
        return decision

    def status(self) -> dict:
        return {"model": "local", "calls": self.calls, "remaining": "-", "latency_ms": 0,
                "error": "", "skip": "", "tokens": (0, 0)}


def build_context(
    stats: dict,
    *,
    prox: int = 0,
    lux: float = 0.0,
    mood: str = "calm",
    mood_since_s: float = 0.0,
    louver: int = 30,
    history=(),
    hour: Optional[int] = None,
    uptime_min: float = 0.0,
    last_reason: str = "",
    baseline_db: Optional[float] = None,
) -> dict:
    now = time.localtime()
    return {
        "time": time.strftime("%H:%M", now),
        "hour": now.tm_hour if hour is None else int(hour),
        "uptime_min": int(uptime_min),
        "noise": {
            "mean": int(stats.get("mean_level", 0)),
            "p95": int(stats.get("p95_level", 0)),
            "max": int(stats.get("max_level", 0)),
            "trend": stats.get("trend", "steady"),
            "baseline_dbfs": round(baseline_db, 1) if baseline_db is not None else None,
        },
        "prox": {"now": int(prox), "near": int(prox) >= 120},
        "light_lux": round(float(lux)),
        "lamp": {
            "mood": mood,
            "since_s": int(mood_since_s),
            "louver": int(louver),
        },
        "history": list(history)[-6:],
        "last_reason": str(last_reason)[:100],
    }


def fingerprint(context: dict) -> tuple:
    """Coarse signature used to decide whether a new LLM call is worth it."""
    noise = context["noise"]
    return (
        int(noise.get("mean", 0)) // 8,
        str(noise.get("trend", "steady")),
        int(context["prox"]["now"]) // 64,
        int(context.get("light_lux", 0)) // 25,
    )
