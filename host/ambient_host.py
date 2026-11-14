#!/usr/bin/env python3
"""Interactive Ambient Light — host.

Two layers, in this order:

  reflex   (per frame)  sensors -> numbers. No network, never offline.
  agent    (on demand)  one DeepSeek mind, writes the numbers the reflex bends.

The reflex has no thread: LookEngine calls it once per frame, after easing the
agent's target and before formatting the F frame. The engine is the only code
that writes to the serial port after boot.

The lamp is a body (firmware), a nervous system (reflex.py) and a mind
(agent.py). The mind is optional: with no key, no network, or `--no-llm`, the
reflex keeps running and the lamp keeps breathing and reacting. The board also
has its own last-resort look if this process dies.

Loops:
  fast   20 Hz   microphone -> state.level          (no serial write)
  music  20 Hz   system audio -> state.music/beat   (no serial write)
  look   25 Hz   LookEngine -> "F" frames + "K" transients
  mind   ~25 s   situation -> one agent turn -> set_light / feel / remember
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from agent import DeepSeekAgent, DeviceHub, StateStore
from audio_meter import AudioMeter
from control import ControlServer
from feather_link import FeatherLink
from looks import DEFAULT_LOOK, Look
from reflex import Reflex
from sensors import BeatDetector, MacContext, SensorHub, SystemAudio, build_situation

LOG = logging.getLogger("host")


# ---------------------------------------------------------------- state
@dataclass
class LampState:
    level: float = 0.0
    music: int = 0
    beat: int = 0
    bass: int = 0        # 0-100 kick envelope from the system-audio tap
    treble: int = 0      # 0-100 hi-hat / cymbal envelope
    present: bool = True
    base_look: Look = field(default_factory=lambda: DEFAULT_LOOK)
    light_reason: str = ""
    idle_s: float = 0.0
    mac: dict = field(default_factory=dict)
    mac_at: float = 0.0
    history: Deque[str] = field(default_factory=lambda: deque(maxlen=8))
    started_at: float = field(default_factory=time.monotonic)
    last_consult: float = 0.0
    last_agent_at: float = 0.0
    last_attempt_at: float = 0.0
    next_heartbeat_at: float = 0.0
    fingerprint: tuple = ()
    skip_logged_at: float = 0.0
    # a level pinned at 100 means every modulation is saturated
    pegged_since: float = 0.0
    pegged_warned: bool = False
    # for the status line and observe
    last_sent: str = ""
    last_sent_at: float = 0.0
    # a look set by hand in the panel is kept until this time (monotonic)
    hold_until: float = 0.0


def sensor_signature(state: LampState, sensors: dict, mac: dict, music: tuple) -> tuple:
    """A coarse fingerprint: did something happen that is worth a thought?

    Deliberately blunt. The reflex answers every small change within a frame;
    the mind is only woken when the room changes *state*: somebody arrived or
    left, music started or stopped, the room went from quiet to loud. Finer
    buckets made it re-decide every 25 s, which looked like a lamp changing
    colour by itself.
    """
    level = float(state.level)
    loud = 0 if level < 25.0 else (1 if level < 70.0 else 2)
    present = float(mac.get("idle_s", 0) or 0) < 120.0
    # "playing" from the now-playing query, or simply sound on the system tap
    playing = bool(mac.get("playing")) or int(music[0]) >= 12
    near = bool(sensors.get("near")) or int(sensors.get("prox", 0)) >= 120
    return (loud, present, playing, near)


def describe_change(before: tuple, after: tuple) -> str:
    """Name what happened between two signatures, most interesting first."""
    if len(before) != 4 or len(after) != 4:
        return "woke_up"
    if before[1] != after[1]:
        return "person_returned" if after[1] else "person_left"
    if before[2] != after[2]:
        return "music_started" if after[2] else "music_stopped"
    if before[3] != after[3] and after[3]:
        return "hand_near"
    if before[0] != after[0]:
        return "room_loud" if after[0] > before[0] else "room_quiet"
    return ""


# How close a look has to be to DEFAULT_LOOK to count as "already resting".
# Loose on purpose: the point is to stop re-homing every few seconds, not to
# demand an exact match.
REST_TOLERANCE = {
    "hue": 12, "sat": 25, "val": 25, "grad": 12,
    "wave_n": 2, "wave_amp": 20, "wave_hz": 6,
    "breath_hz": 6, "breath_amp": 20, "open": 20,
    "hue_cycles": 5, "shape": 10, "sparkle": 10,
}


def near_rest(look: Look) -> bool:
    """True when `look` is visually close to the amber rest look."""
    return all(
        abs(getattr(look, name) - getattr(DEFAULT_LOOK, name)) <= tolerance
        for name, tolerance in REST_TOLERANCE.items()
    )


# ---------------------------------------------------------------- loops
def fast_loop(stop: threading.Event, meter: AudioMeter, state: LampState, hz: float) -> None:
    """Microphone to a number. The reflex does the rest; nothing goes to serial."""
    period = 1.0 / max(1.0, hz)
    next_t = time.monotonic()
    while not stop.is_set():
        if meter.ready:
            level = int(round(meter.level))
            state.level = float(level)

            now = time.monotonic()
            if level >= 98:
                if state.pegged_since == 0.0:
                    state.pegged_since = now
                elif now - state.pegged_since > 15.0 and not state.pegged_warned:
                    state.pegged_warned = True
                    base = meter.baseline_db
                    over = (meter.db - base) if base is not None else 0.0
                    guess = int(max(20.0, min(60.0, over * 0.8)))
                    LOG.warning(
                        "level pegged at 100 for %.0f s (%.0f dB over baseline); "
                        "try --span-db %d",
                        now - state.pegged_since, over, guess,
                    )
            else:
                state.pegged_since = 0.0
                state.pegged_warned = False

        next_t += period
        delay = next_t - time.monotonic()
        if delay > 0:
            stop.wait(delay)
        else:
            next_t = time.monotonic()


def music_loop(stop: threading.Event, sensors: SensorHub, state: LampState) -> None:
    """System audio (or the microphone) to energy/beat. Also no serial write."""
    period = 0.02     # 50 Hz: a kick should reach the next 25 Hz frame
    next_t = time.monotonic()
    while not stop.is_set():
        energy, beat, present = sensors.music_bus()
        state.music, state.beat, state.present = int(energy), int(beat), present
        system = getattr(sensors, "system", None)
        if system is not None and system.available:
            state.bass = int(round(100.0 * system.detector.bass))
            state.treble = int(round(100.0 * system.detector.treble))
        else:
            state.bass = state.treble = 0
        try:
            # The Mac's idle time is the presence sensor (cached for 1 s inside
            # MacContext), so the reflex can greet whoever comes back.
            state.idle_s = float(sensors.mac.idle_seconds())
        except Exception:  # noqa: BLE001 - presence is best effort
            pass

        next_t += period
        delay = next_t - time.monotonic()
        if delay > 0:
            stop.wait(delay)
        else:
            next_t = time.monotonic()


# ---------------------------------------------------------------- the host
class Host:
    """Owns the agent loop, the reflex, and the bridge between them."""

    def __init__(self, args, meter, link, sensors, store, state, brain) -> None:
        self.args = args
        self.meter = meter
        self.link = link
        self.sensors = sensors
        self.store = store
        self.state = state
        self.brain = brain
        self.reflex = None if args.no_reflex else Reflex()
        self.control: Optional[ControlServer] = None
        self.hub: Optional[DeviceHub] = None
        self.stop = threading.Event()
        self.events: "queue.Queue" = queue.Queue(maxsize=64)
        self._turn_lock = threading.Lock()   # one turn at a time, chat or timer
        self._last_decay = time.monotonic()
        self._last_save = 0.0
        self._quiet_since = time.monotonic()  # last sound / hand / chat
        # the conversation, so a reply can refer to what was said before
        self.chat: Deque[tuple] = deque(maxlen=16)
        self._last_chat_at = time.monotonic()
        self._ignored_told = False
        self._late_told_day = -1
        # things the lamp said, for the on-screen face to show as bubbles
        self._outbox: list[dict] = []
        self._outbox_id = 0
        self._outbox_lock = threading.Lock()
        self._reminders: list[dict] = []

    # ---- wires ----
    def attach_control(self, control: ControlServer) -> None:
        self.control = control
        try:
            # Always streaming: the reflex needs a base to bend.
            control.engine.start("lamp", self.state.base_look)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("cannot start the light stream: %s", exc)

    def senses(self) -> dict:
        raw = self.link.sensors
        # LookEngine calls this once per frame (25 Hz), which makes it the
        # cheapest place to notice that the room is no longer quiet.
        if (self.state.level >= 12.0 or self.state.beat > 20
                or int(raw.prox) >= 120):
            self._quiet_since = time.monotonic()
        # Without a system-audio tap, "music" is the microphone measured
        # against its own last 3 s, so room hiss alone swings it to 50. It is
        # not a loudness; only the calibrated level may drive the light then.
        system = getattr(self.sensors, "system", None)
        music = self.state.music if (system is not None and system.available) else 0
        return {
            "level": self.state.level,
            "music": music,
            "bass": self.state.bass,
            "treble": self.state.treble,
            "beat": self.state.beat,
            "prox": raw.prox,
            "lux": raw.lux,
            "present": self.state.present,
            "idle_s": self.state.idle_s,
        }

    def _already_resting(self) -> bool:
        return near_rest(self.state.base_look)

    def on_event(self, msg) -> None:  # noqa: ANN001
        if getattr(msg, "name", "") not in ("STARTLE", "WAKE", "LINK_LOST", "LINK_OK"):
            return
        try:
            self.events.put_nowait(msg)
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait(msg)
            except queue.Empty:
                pass

    # ---- the mind ----
    # ---- the lamp's voice and body, as the screen sees them ----
    def utter(self, text: str, kind: str = "proactive") -> None:
        text = " ".join(str(text).split())[:400]
        if not text:
            return
        with self._outbox_lock:
            self._outbox_id += 1
            self._outbox.append({"id": self._outbox_id, "text": text, "kind": kind,
                                 "at": int(time.time())})
            del self._outbox[:-30]

    def feed(self, since: int = 0) -> dict:
        """Cheap, pollable: the frame on the wire and anything said lately."""
        engine = self.control.engine if self.control is not None else None
        status = engine.status() if engine is not None else {}
        with self._outbox_lock:
            said = [u for u in self._outbox if u["id"] > int(since or 0)]
            last = self._outbox_id
        return {
            "ok": True,
            "line": status.get("line", ""),
            "cap": status.get("cap", 120),
            "effect": status.get("effect"),
            "gesture": status.get("gesture"),
            "utterances": said,
            "last_id": last,
            "agent": self.brain is not None,
            "link": bool(self.link.connected) or bool(getattr(self.link, "dry_run", False)),
            "away": float(self.state.idle_s) >= 10.0,
            "music": int(self.senses().get("music", 0)) >= 12,
        }

    def gesture(self, name: str, seconds: Optional[float] = None) -> None:
        if self.control is not None:
            self.control.engine.set_gesture(name, seconds)

    def remind(self, minutes: float, text: str) -> None:
        due = time.time() + minutes * 60.0
        entry = {"due": due, "text": text}
        self._reminders.append(entry)

        def fire() -> None:
            if self.stop.is_set():
                return
            if entry in self._reminders:
                self._reminders.remove(entry)
            LOG.info("reminder: %s", text)
            try:
                self.gesture("wave")
                if self.control is not None:
                    self.control.engine.set_effect("heartbeat", 6.0, 1.0)
            except Exception:  # noqa: BLE001
                pass
            self.utter(text, "reminder")
            self.chat.append(("assistant", text))

        timer = threading.Timer(minutes * 60.0, fire)
        timer.daemon = True
        timer.start()

    def looks(self) -> dict:
        return {"ok": True, "looks": dict(self.store.looks)}

    def set_api_key(self, key: str) -> dict:
        """Give the lamp a mind (or a new one) without restarting."""
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "error": "empty key"}
        os.environ["DEEPSEEK_API_KEY"] = key
        brain = make_brain(self.args)
        if brain is None:
            return {"ok": False, "error": "could not start the agent (see the log)"}
        self.brain = brain
        return {"ok": True, "agent": brain.model}

    def _situation(self, mac: dict, music: tuple, said: str = "",
                   event: str = "") -> dict:
        stats = self.meter.stats(10.0)
        situation = build_situation(
            stats=stats,
            sensors=self.sensors.sensors(),
            mac=mac,
            music=music,
            lamp={
                "light": self.state.base_look.fields(),
                "reason": self.state.light_reason,
                "for_s": int(time.monotonic() - self.state.last_sent_at),
                "effect": (self.control.engine.status().get("effect")
                           if self.control is not None else None),
            },
            drives=self.store.drives,
            memory=self.store.memory,
            events=[
                f"{ev.name} {int(time.monotonic() - ev.at)}s ago"
                for ev in self.link.recent_events(60.0)
            ],
            uptime_min=(time.monotonic() - self.state.started_at) / 60.0,
            last_reason=self.state.light_reason,
        )
        situation["saved_looks"] = list(self.store.looks)
        situation["minutes_since_we_talked"] = int(
            (time.monotonic() - self._last_chat_at) / 60.0)
        if self._reminders:
            situation["reminders"] = [
                {"in_min": max(0, int((r["due"] - time.time()) / 60.0)), "text": r["text"]}
                for r in self._reminders[-4:]
            ]
        if said:
            situation["said"] = said
        elif event:
            situation["event"] = event
        return situation

    def _run_turn(self, said: str = "", event: str = "") -> Optional[object]:
        """One agent turn. Safe to call from the mind loop or from a chat."""
        if self.brain is None or self.hub is None:
            return None
        with self._turn_lock:
            mac = self.state.mac or self.sensors.mac_context()
            music = self.sensors.music_bus()
            situation = self._situation(mac, music, said=said, event=event)
            self.state.last_attempt_at = time.monotonic()
            if said:
                # The second or so the model takes is not dead air: a band
                # circles the ring, which reads as "it is thinking about it".
                try:
                    self.gesture("think")
                except Exception:  # noqa: BLE001
                    pass
            try:
                turn = self.brain.turn(situation, self.hub, list(self.chat))
            finally:
                if said and self.control is not None:
                    self.control.engine.stop_gesture("think")
            self.state.last_consult = time.monotonic()
            if turn is None:
                status = self.brain.status()
                if time.monotonic() - self.state.skip_logged_at > 60.0:
                    self.state.skip_logged_at = time.monotonic()
                    LOG.info("holding (%s)",
                             status.get("skip") or status.get("error") or "no decision")
                return None
            self.state.last_agent_at = time.monotonic()
            light = self.state.base_look
            LOG.info(
                "mind -> %s%s  hue=%d val=%d%s  %dms",
                "+".join(turn.actions) or "silent",
                f'  "{turn.reply}"' if turn.reply else "",
                int(light.hue), int(light.val),
                f"  ({turn.reason})" if turn.reason else "",
                turn.latency_ms,
            )
            if ("set_light" in turn.actions and "blink" not in turn.actions
                    and "gesture" not in turn.actions):
                # A change with no visible cause reads as a lamp drifting by
                # itself; the blink marks it as a reaction.
                try:
                    self.hub.blink("blink")
                except Exception:  # noqa: BLE001
                    pass
            if said:
                self.chat.append(("user", said))
            if turn.reply:
                self.state.history.append(turn.reply)
                self.chat.append(("assistant", turn.reply))
                self.utter(turn.reply, "reply" if said else "proactive")
            return turn

    def say(self, text: str) -> dict:
        """The person spoke. One turn, immediately, with a reply."""
        text = " ".join(str(text).split())[:500]
        if not text:
            return {"ok": False, "error": "empty message"}
        self._quiet_since = time.monotonic()
        self._last_chat_at = time.monotonic()
        self._ignored_told = False
        self.store.feel({"bond": 0.04, "energy": 0.02})     # being talked to matters
        if self.brain is None:
            return {
                "ok": False,
                "offline": True,
                "error": "no agent (no API key); the lamp is running on reflexes",
            }
        turn = self._run_turn(said=text)
        if turn is None:
            return {"ok": False, "error": "the mind did not answer"}
        return {
            "ok": True,
            "reply": turn.reply,
            "actions": list(turn.actions),
            "light": self.state.base_look.fields(),
        }

    # ---- the periodic mind ----
    def mind_loop(self) -> None:
        args = self.args
        interval = args.slow_min
        while not self.stop.is_set():
            try:
                item = self.events.get(timeout=interval)
            except queue.Empty:
                item = ""
            if item is None:
                return
            event_name = getattr(item, "name", "") if item else ""
            while True:
                try:
                    extra = self.events.get_nowait()
                except queue.Empty:
                    break
                if extra is None:
                    return
                event_name = getattr(extra, "name", "") or event_name

            now = time.monotonic()
            self.store.decay(now - self._last_decay)
            self._last_decay = now
            if now - self._last_save > 60.0:
                self.store.save()
                self._last_save = now

            if now - self.state.mac_at >= 8.0:
                self.state.mac = self.sensors.mac_context()
                self.state.mac_at = now
                self.state.idle_s = float(self.state.mac.get("idle_s", 0) or 0)

            # Quiet for long enough -> home to the amber resting look. This is
            # a floor under the agent's behaviour, not a second mind: it only
            # runs when nothing has made a sound, a hand or a change for a while.
            rest_after = getattr(args, "rest_after", 0.0)
            if rest_after > 0 and self.hub is not None:
                quiet_for = now - max(self._quiet_since, self.state.last_sent_at)
                if (quiet_for >= rest_after and now >= self.state.hold_until
                        and not self._already_resting()):
                    self.hub.set_light(reason="it has been quiet for a while",
                                       **DEFAULT_LOOK.fields())
                    LOG.info("rest: %.0f s of quiet, back to the amber look", quiet_for)

            if self.brain is None or not self.meter.ready:
                interval = 3.0
                continue

            music = self.sensors.music_bus()
            system = getattr(self.sensors, "system", None)
            if system is None or not system.available:
                # mic-derived "music" is not a loudness (see Host.senses)
                music = (0, music[1], music[2])
            sensors = self.sensors.sensors()
            signature = sensor_signature(self.state, sensors, self.state.mac, music)
            changed = signature != self.state.fingerprint
            event_due = event_name in ("STARTLE", "WAKE", "LINK_LOST")
            heartbeat_due = now >= self.state.next_heartbeat_at
            min_due = now - self.state.last_attempt_at >= args.llm_min_interval

            # Two things only a clock can notice.
            present = signature[1]
            clock = time.localtime()
            late = (clock.tm_hour >= 23 or clock.tm_hour < 4) and present
            ignored = (present and not self._ignored_told
                       and now - self._last_chat_at > 45 * 60.0)

            why = ""
            if changed and self.state.fingerprint:
                why = describe_change(self.state.fingerprint, signature)
            elif changed:
                why = "woke_up"
            elif late and self._late_told_day != clock.tm_yday:
                why = "late_night"
            elif ignored:
                why = "ignored"
            elif heartbeat_due:
                why = "heartbeat"

            if not min_due or not (event_due or why):
                interval = min(args.slow_max, interval + 1.0)
                continue
            if why == "late_night":
                self._late_told_day = clock.tm_yday
            elif why == "ignored":
                self._ignored_told = True

            turn = self._run_turn(event=why or "heartbeat")
            if turn is not None:
                self.state.fingerprint = signature
                self.state.next_heartbeat_at = now + args.llm_heartbeat
            interval = args.slow_min if changed else min(args.slow_max, interval + 1.0)

    # ---- status ----
    def status(self) -> str:
        st = self.state
        light = st.base_look
        level = int(round(st.level))
        bar = "#" * max(0, min(10, level // 10))
        bar = bar + "." * (10 - len(bar))
        name = self.control.engine.status()["name"] if (
            self.control is not None and self.control.engine.running) else "off"
        text = (
            f"{time.strftime('%H:%M:%S')}  lvl {level:3d} {bar}  "
            f"{self.meter.db:6.1f} dBFS  hue={int(light.hue):3d} val={int(light.val):3d} "
            f"open={int(light.open):3d}  prox={self.link.sensors.prox:3d}  "
            f"lux={self.link.sensors.lux:5.0f}  mus={st.music:3d}  "
            f"link={'up' if self.link.connected else '--'}  stream={name}"
        )
        if self.brain is not None:
            text += (f"  llm={self.brain.limiter.used}/{self.brain.limiter.max_per_hour}"
                     f"  {self.brain.last_latency_ms}ms")
        else:
            text += "  llm=off (reflex only)"
        return text


# ---------------------------------------------------------------- setup
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive Ambient Light host (reflex layer + one agent)"
    )
    p.add_argument("--port", default=os.environ.get("AMBIENT_PORT", "auto"),
                   help="serial port, or 'auto'")
    p.add_argument("--baud", type=int, default=115200)

    p.add_argument("--no-llm", action="store_true",
                   help="no agent at all: reflexes only")
    p.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    p.add_argument("--api-base", default=os.environ.get("DEEPSEEK_BASE_URL",
                                                        "https://api.deepseek.com"))
    p.add_argument("--max-calls-per-hour", type=int, default=60)
    p.add_argument("--llm-min-interval", type=float, default=60.0)
    p.add_argument("--llm-heartbeat", type=float, default=600.0)
    p.add_argument("--llm-timeout", type=float, default=8.0)
    p.add_argument("--state-file", default="state.json")

    p.add_argument("--look-hz", type=float, default=25.0)
    p.add_argument("--cap", type=int, default=120,
                   help="channel ceiling sent via G (120 at 32 LEDs full white ≈ 0.9 A)")
    p.add_argument("--gamma", type=int, default=0, choices=(0, 1, 2))
    p.add_argument("--no-dither", action="store_true")
    p.add_argument("--no-reflex", action="store_true",
                   help="do not run the host-side sensor reflex layer")

    p.add_argument("--fast-hz", type=float, default=20.0)
    p.add_argument("--slow-min", type=float, default=3.0)
    p.add_argument("--slow-max", type=float, default=10.0)
    p.add_argument("--rest-after", type=float, default=180.0,
                   help="seconds of quiet before easing back to the amber rest "
                        "look (0 disables)")

    p.add_argument("--calibrate", type=float, default=3.0, help="baseline seconds")
    p.add_argument("--span-db", type=float, default=18.0,
                   help="dB above baseline that counts as 100")
    p.add_argument("--samplerate", type=int, default=48000)
    p.add_argument("--blocksize", type=int, default=1024)
    p.add_argument("--device", default=None, help="input device index or name")
    p.add_argument("--sim-audio", action="store_true", help="synthetic audio, no microphone")
    p.add_argument("--no-music", action="store_true",
                   help="do not open BlackHole; beat falls back to the microphone")

    p.add_argument("--control-socket", default=os.environ.get(
        "AMBIENT_SOCK", "/tmp/ambient-lamp.sock"))
    p.add_argument("--no-control", action="store_true",
                   help="do not open the lampctl/webchat control socket")

    p.add_argument("--dry-run", action="store_true", help="print serial commands")
    p.add_argument("--duration", type=float, default=0.0, help="exit after N seconds")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--log-file", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("serial").setLevel(logging.INFO)


def list_devices() -> None:
    print("Audio input devices:")
    try:
        import sounddevice as sd

        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                mark = "  <- BlackHole" if "blackhole" in str(dev["name"]).lower() else ""
                print(f"  [{idx}] {dev['name']}{mark}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (cannot query audio: {exc})")

    print("\nSerial ports:")
    try:
        from serial.tools import list_ports

        for p in list_ports.comports():
            print(f"  {p.device}  {p.description or 'n/a'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (cannot query serial: {exc})")


def make_brain(args: argparse.Namespace) -> Optional[DeepSeekAgent]:
    if args.no_llm:
        LOG.info("agent: off — reflexes only")
        return None
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        LOG.warning("no DEEPSEEK_API_KEY: agent off, reflexes only "
                    "(source host/.env to enable it)")
        return None
    try:
        import openai  # noqa: F401
    except ImportError:
        LOG.warning("openai SDK missing: agent off, reflexes only "
                    "(pip install -r requirements.txt)")
        return None
    LOG.info("agent: DeepSeek %s (timeout %.1fs, %d calls/hour max)",
             args.model, args.llm_timeout, args.max_calls_per_hour)
    return DeepSeekAgent(
        api_key=api_key,
        model=args.model,
        base_url=args.api_base,
        timeout_s=args.llm_timeout,
        max_calls_per_hour=args.max_calls_per_hour,
    )


# ---------------------------------------------------------------- main
def main(argv: Optional[list[str]] = None, embedded: bool = False,
         on_ready=None) -> int:
    """Run the host. `embedded` = inside the app, on a thread: no signal
    handlers, and `on_ready(host)` hands the caller something to stop."""
    args = parse_args(argv)
    setup_logging(args)

    if args.list_devices:
        list_devices()
        return 0

    meter = AudioMeter(
        samplerate=args.samplerate,
        blocksize=args.blocksize,
        calibration_s=args.calibrate,
        span_db=args.span_db,
        device=args.device,
        simulated=args.sim_audio,
    )
    try:
        meter.start()
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 2

    baseline = getattr(meter, "baseline_db", None)
    if baseline is not None and baseline > -30.0:
        LOG.warning("baseline %.1f dBFS is loud: the calibration window had sound "
                    "in it, so levels will read low. Restart in a quiet room.", baseline)

    link = FeatherLink(port=args.port, baud=args.baud, dry_run=args.dry_run)
    link.start()

    mac = MacContext()
    detector = BeatDetector(args.samplerate, args.blocksize)
    system = SystemAudio(detector, args.samplerate, args.blocksize)
    if not args.no_music:
        system.start()

    sensors = SensorHub(meter, link, mac, system)
    store = StateStore(args.state_file)
    state = LampState()
    brain = make_brain(args)
    host = Host(args, meter, link, sensors, store, state, brain)
    link.on_event = host.on_event

    dev_hub = DeviceHub(link, state, store, sensors)
    dev_hub.reflex = host.reflex
    host.hub = dev_hub

    def push_light(look: Look) -> None:
        if host.control is None:
            return
        if host.control.engine.running:
            host.control.engine.set("agent", look)
        else:
            host.control.engine.start("agent", look)

    dev_hub.on_light = push_light

    def push_effect(kind: str, seconds: float, intensity: float) -> None:
        if host.control is None:
            raise RuntimeError("no light stream (control socket off)")
        host.control.engine.set_effect(kind, seconds or None, intensity)

    dev_hub.on_effect = push_effect
    dev_hub.on_gesture = host.gesture
    dev_hub.on_remind = host.remind

    if not args.no_control:
        try:
            control = ControlServer(
                link, state, store, sensors, args.control_socket,
                look_hz=args.look_hz,
                look_cap=args.cap,
                look_gamma=args.gamma,
                look_dither=0 if args.no_dither else 1,
                reflex=host.reflex,
                senses=host.senses,
                say=host.say,
                extra={
                    "feed": lambda req: host.feed(req.get("since", 0)),
                    "looks": lambda req: host.looks(),
                    "set_key": lambda req: host.set_api_key(req.get("key", "")),
                    "save_look": lambda req: dev_hub.save_look(req.get("name", "")),
                    "recall_look": lambda req: dev_hub.recall_look(req.get("name", "")),
                    "forget_look": lambda req: (
                        {"ok": store.looks.pop(str(req.get("name", "")), None) is not None,
                         "looks": dict(store.looks)}),
                },
            )
            control.start()
            host.attach_control(control)
        except Exception as exc:  # noqa: BLE001 - control is optional
            LOG.warning("control socket disabled: %s", exc)
            host.control = None

    def on_signal(signum, frame):  # noqa: ANN001
        LOG.info("signal %s, shutting down", signum)
        host.stop.set()

    if not embedded:
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
    if on_ready is not None:
        on_ready(host)

    threads = [
        threading.Thread(target=fast_loop, args=(host.stop, meter, state, args.fast_hz),
                         name="fast", daemon=True),
        threading.Thread(target=music_loop, args=(host.stop, sensors, state),
                         name="music", daemon=True),
        threading.Thread(target=host.mind_loop, name="mind", daemon=True),
    ]
    for t in threads:
        t.start()

    LOG.info(
        "reflex %s | stream %.0f Hz cap %d | agent %s | slow %.0f..%.0f s | "
        "port %s | span %.1f dB | music %s",
        "off" if host.reflex is None else "on",
        args.look_hz, args.cap,
        brain.model if brain else "off",
        args.slow_min, args.slow_max, args.port, args.span_db,
        system.device_name if system.available else "mic only",
    )
    if args.sim_audio:
        LOG.info("simulated audio: no microphone will be opened")

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while not host.stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            wait = 10.0 if deadline is None else min(10.0, max(0.0, deadline - time.monotonic()))
            if host.stop.wait(wait):
                break
            if embedded:
                LOG.info("%s", host.status())
            else:
                print(host.status(), flush=True)
    finally:
        host.stop.set()
        try:
            host.events.put_nowait(None)
        except queue.Full:
            pass
        for t in threads:
            t.join(timeout=2.0)
        if host.control is not None:
            host.control.stop()
        if getattr(host, "dark_on_exit", False):
            # Quitting the app should switch the lamp off, not hand it to the
            # firmware's amber fallback. A channel ceiling of 0 keeps the LEDs
            # dark until the next start sends its own G again.
            try:
                link.send_line("G,0,0,1")
                time.sleep(0.15)
                LOG.info("lamp switched off (G,0)")
            except Exception:  # noqa: BLE001
                pass
        link.close()
        system.stop()
        meter.stop()
        store.save()
        LOG.info("bye")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
