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


def sensor_signature(state: LampState, sensors: dict, mac: dict, music: tuple) -> tuple:
    """A cheap fingerprint: has anything the mind cares about moved?"""
    prox = int(sensors.get("prox", 0)) // 40
    lux = int(sensors.get("lux", 0)) // 8
    level = int(state.level) // 20
    energy = int(music[0]) // 25
    idle = int(mac.get("idle_s", 0)) // 60
    playing = bool(mac.get("playing"))
    near = bool(sensors.get("near"))
    return (prox, lux, level, energy, idle, playing, near)


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
    period = 0.05
    next_t = time.monotonic()
    while not stop.is_set():
        energy, beat, present = sensors.music_bus()
        state.music, state.beat, state.present = int(energy), int(beat), present

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
        return {
            "level": self.state.level,
            "music": self.state.music,
            "beat": self.state.beat,
            "prox": raw.prox,
            "lux": raw.lux,
            "present": self.state.present,
            "idle_s": self.state.idle_s,
        }

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
    def _situation(self, mac: dict, music: tuple, said: str = "") -> dict:
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
        if said:
            situation["said"] = said
        return situation

    def _run_turn(self, said: str = "") -> Optional[object]:
        """One agent turn. Safe to call from the mind loop or from a chat."""
        if self.brain is None or self.hub is None:
            return None
        with self._turn_lock:
            mac = self.state.mac or self.sensors.mac_context()
            music = self.sensors.music_bus()
            situation = self._situation(mac, music, said=said)
            self.state.last_attempt_at = time.monotonic()
            turn = self.brain.turn(situation, self.hub)
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
            if turn.reply:
                self.state.history.append(turn.reply)
            return turn

    def say(self, text: str) -> dict:
        """The person spoke. One turn, immediately, with a reply."""
        text = " ".join(str(text).split())[:500]
        if not text:
            return {"ok": False, "error": "empty message"}
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

            if self.brain is None or not self.meter.ready:
                interval = 3.0
                continue

            music = self.sensors.music_bus()
            sensors = self.sensors.sensors()
            signature = sensor_signature(self.state, sensors, self.state.mac, music)
            changed = signature != self.state.fingerprint
            event_due = event_name in ("STARTLE", "WAKE", "LINK_LOST")
            heartbeat_due = now >= self.state.next_heartbeat_at
            min_due = now - self.state.last_attempt_at >= args.llm_min_interval

            if not min_due or not (event_due or heartbeat_due or changed):
                interval = min(args.slow_max, interval + 1.0)
                continue

            turn = self._run_turn()
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
    p.add_argument("--llm-min-interval", type=float, default=25.0)
    p.add_argument("--llm-heartbeat", type=float, default=120.0)
    p.add_argument("--llm-timeout", type=float, default=8.0)
    p.add_argument("--state-file", default="state.json")

    p.add_argument("--look-hz", type=float, default=25.0)
    p.add_argument("--cap", type=int, default=80, help="channel ceiling sent via G")
    p.add_argument("--gamma", type=int, default=0, choices=(0, 1, 2))
    p.add_argument("--no-dither", action="store_true")
    p.add_argument("--no-reflex", action="store_true",
                   help="do not run the host-side sensor reflex layer")

    p.add_argument("--fast-hz", type=float, default=20.0)
    p.add_argument("--slow-min", type=float, default=3.0)
    p.add_argument("--slow-max", type=float, default=10.0)

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
def main(argv: Optional[list[str]] = None) -> int:
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
            )
            control.start()
            host.attach_control(control)
        except Exception as exc:  # noqa: BLE001 - control is optional
            LOG.warning("control socket disabled: %s", exc)
            host.control = None

    def on_signal(signum, frame):  # noqa: ANN001
        LOG.info("signal %s, shutting down", signum)
        host.stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

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
        link.close()
        system.stop()
        meter.stop()
        store.save()
        LOG.info("bye")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
