#!/usr/bin/env python3
"""Interactive Ambient Light — host.

Three loops:

  fast   (20 Hz)      microphone -> "L,<0-100>" -> lamp. No network.
  music  (20 Hz)      system audio -> "U,<energy>,<beat>,<present>". No network.
  slow   (3..10 s)    situation -> DeepSeek tool calls -> "M/LID/C/R".

The agent owns the lamp's behaviour when the API is healthy. When it is not,
the slow loop simply sends nothing: the firmware keeps the last scene and,
after its 180 s host TTL, falls back to autonomy. No local model is involved;
`--no-llm` uses a small rule-based policy for demos and tests.
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
from brain import Decision, LocalBrain, build_context, fingerprint
from control import ControlServer
from feather_link import FeatherLink
from sensors import BeatDetector, MacContext, SensorHub, SystemAudio, build_situation

LOG = logging.getLogger("host")


# ---------------------------------------------------------------- state
@dataclass
class LampState:
    level: float = 0.0
    mood: str = "calm"
    mood_since: float = field(default_factory=time.monotonic)
    lid: int = 70
    coupling: dict = field(default_factory=lambda: {"audio": 60, "beat": 60, "prox": 50, "idle": 50})
    last: Optional[Decision] = None
    last_sent: str = ""
    last_sent_at: float = 0.0
    last_decision_at: float = 0.0
    last_consult: float = 0.0
    last_agent_at: float = 0.0
    last_attempt_at: float = 0.0
    fingerprint: tuple = ()
    history: Deque[str] = field(default_factory=lambda: deque(maxlen=8))
    started_at: float = field(default_factory=time.monotonic)
    skip_logged_at: float = 0.0
    music: int = 0
    beat: int = 0
    present: bool = True
    # A chat/script command takes a short lease; while it is active the
    # host's own slow loop does not write moods.
    manual_until: float = 0.0
    manual_last: Optional[Decision] = None
    manual_sent_at: float = 0.0


def apply_decision(state: LampState, link: FeatherLink, decision: Decision) -> bool:
    now = time.monotonic()
    line = decision.serial_line()

    # Re-send at most every 30 s even if nothing changed: the firmware drops
    # back to autonomy after 3 minutes without a command.
    if line == state.last_sent and now - state.last_sent_at < 30.0:
        return False

    if decision.mood != state.mood:
        state.history.append(f"{state.mood} {int(now - state.mood_since)}s")
        state.mood = decision.mood
        state.mood_since = now

    state.lid = decision.louver
    state.last = decision
    state.last_sent = line
    state.last_sent_at = now
    state.last_decision_at = now

    ok = link.send_mood(decision)
    LOG.info(
        "mood -> %-7s hue=%3d speed=%3d bright=%3d lid=%3d  %s%s",
        decision.mood,
        decision.hue,
        decision.speed,
        decision.brightness,
        decision.louver,
        decision.reason or "-",
        "" if ok else "  [serial down]",
    )
    return True


# ---------------------------------------------------------------- loops
def fast_loop(
    stop: threading.Event,
    meter: AudioMeter,
    link: FeatherLink,
    state: LampState,
    hz: float,
) -> None:
    period = 1.0 / max(1.0, hz)
    next_t = time.monotonic()
    while not stop.is_set():
        if meter.ready:
            level = int(round(meter.level))
            state.level = float(level)
            link.send_level(level)

        next_t += period
        delay = next_t - time.monotonic()
        if delay > 0:
            stop.wait(delay)
        else:
            next_t = time.monotonic()


def music_loop(
    stop: threading.Event,
    sensors: SensorHub,
    link: FeatherLink,
    state: LampState,
) -> None:
    """Sends the modulation bus. In dry-run, 5 Hz is enough to read the log."""
    period = 0.2 if link.dry_run else 0.05
    next_t = time.monotonic()
    while not stop.is_set():
        energy, beat, present = sensors.music_bus()
        state.music, state.beat, state.present = energy, beat, present
        link.send_music(energy, beat, present)

        next_t += period
        delay = next_t - time.monotonic()
        if delay > 0:
            stop.wait(delay)
        else:
            next_t = time.monotonic()


def slow_loop(
    stop: threading.Event,
    args: argparse.Namespace,
    sensors: SensorHub,
    dev: DeviceHub,
    state: LampState,
    brain,
    store: StateStore,
    events_q: "queue.Queue",
) -> None:
    interval = args.slow_min
    last_save = time.monotonic()
    last_decay = time.monotonic()

    while not stop.is_set():
        try:
            item = events_q.get(timeout=interval)
        except queue.Empty:
            item = ""
        if item is None:
            break
        event_name = getattr(item, "name", "") if item else ""
        while True:
            try:
                extra = events_q.get_nowait()
            except queue.Empty:
                break
            if extra is None:
                return
            event_name = getattr(extra, "name", "") or event_name

        if not sensors.meter.ready:
            interval = 2.0
            continue

        now = time.monotonic()
        store.decay(now - last_decay)
        last_decay = now
        if now - last_save > 60.0:
            store.save()
            last_save = now

        # A command from lampctl / DSH holds the lamp for a few seconds.
        # Keep the M TTL alive, but do not let the local policy or agent
        # overwrite the chat's decision.
        if now < state.manual_until:
            if state.manual_last is not None and now - state.manual_sent_at >= 30.0:
                sensors.link.send_mood(state.manual_last)
                state.manual_sent_at = now
                state.last_sent_at = now
            state.last_consult = now
            interval = 2.0
            continue

        window = max(6.0, min(20.0, now - state.last_consult))
        stats = sensors.meter.stats(window)
        raw = sensors.link.sensors
        mac = sensors.mac_context()
        music = sensors.music_bus()

        # The local policy still speaks the old context shape.
        context = build_context(
            stats,
            prox=raw.prox,
            lux=raw.lux,
            mood=state.mood,
            mood_since_s=now - state.mood_since,
            louver=state.lid,
            history=state.history,
            uptime_min=(now - state.started_at) / 60.0,
            last_reason=state.last.reason if state.last else "",
            baseline_db=sensors.meter.baseline_db,
        )
        fp = fingerprint(context)
        changed = fp != state.fingerprint
        event_due = event_name in ("STARTLE", "WAKE", "TOUCH", "APPROACH", "LINK_LOST")
        heartbeat_due = now - state.last_agent_at >= args.llm_heartbeat
        min_due = now - state.last_attempt_at >= args.llm_min_interval

        if brain.is_llm:
            if not event_due and not min_due:
                interval = min(args.slow_max, interval + 1.0)
                continue
            if not (event_due or heartbeat_due or changed or state.last is None):
                interval = min(args.slow_max, interval + 1.0)
                continue

        situation = build_situation(
            stats=stats,
            sensors=sensors.sensors(),
            mac=mac,
            music=music,
            lamp={
                "mood": state.mood,
                "since_s": now - state.mood_since,
                "lid": state.lid,
                "coupling": state.coupling,
            },
            drives=store.drives,
            memory=store.memory,
            events=[
                f"{ev.name} {int(now - ev.at)}s ago"
                for ev in sensors.link.recent_events(60.0)
            ],
            uptime_min=(now - state.started_at) / 60.0,
            last_reason=state.last.reason if state.last else "",
        )

        if brain.is_llm:
            state.last_attempt_at = now
            turn = brain.turn(situation, dev)
            state.last_consult = now
            if turn is None:
                if now - state.skip_logged_at > 60.0:
                    state.skip_logged_at = now
                    status = brain.status()
                    LOG.info(
                        "holding state (%s)",
                        status.get("skip") or status.get("error") or "no decision",
                    )
            else:
                state.last_agent_at = now
                state.fingerprint = fp
                LOG.info(
                    "turn -> %-7s lid=%3d  %s  %s  %dms",
                    state.mood,
                    state.lid,
                    "+".join(turn.actions),
                    turn.reason or "-",
                    turn.latency_ms,
                )
                store.save()
        else:
            decision = brain.decide(context)
            state.last_consult = now
            if decision is None:
                if now - state.skip_logged_at > 60.0:
                    state.skip_logged_at = now
                    LOG.info("holding state (%s)", brain.status().get("skip") or "no decision")
            else:
                apply_decision(state, sensors.link, decision)
                state.fingerprint = fp
                state.last_agent_at = now

        interval = args.slow_min if (changed or event_due) else min(
            args.slow_max, interval + 1.0
        )


# ---------------------------------------------------------------- ui
def status_line(meter: AudioMeter, link: FeatherLink, state: LampState, brain) -> str:
    level = int(round(state.level))
    filled = max(0, min(10, level // 10))
    bar = "#" * filled + "." * (10 - filled)
    text = (
        f"{time.strftime('%H:%M:%S')}  lvl {level:3d} {bar}  "
        f"{meter.db:6.1f} dBFS  mood={state.mood:<7} lid={state.lid:3d} "
        f"prox={link.sensors.prox:3d}  lux={link.sensors.lux:5.0f}  "
        f"mus={state.music:3d}  link={'up' if link.connected else '--'}"
    )
    if brain.is_llm:
        text += (
            f"  llm={brain.limiter.used}/{brain.limiter.max_per_hour}"
            f"  {brain.last_latency_ms}ms"
        )
    return text


# ---------------------------------------------------------------- setup
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive Ambient Light host (fast reflex + DeepSeek mind)"
    )
    p.add_argument("--port", default=os.environ.get("AMBIENT_PORT", "auto"),
                   help="serial port, or 'auto'")
    p.add_argument("--baud", type=int, default=115200)

    p.add_argument("--no-llm", action="store_true", help="use the local rule policy only")
    p.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    p.add_argument("--api-base", default=os.environ.get("DEEPSEEK_BASE_URL",
                                                        "https://api.deepseek.com"))
    p.add_argument("--max-calls-per-hour", type=int, default=60)
    p.add_argument("--llm-min-interval", type=float, default=25.0)
    p.add_argument("--llm-heartbeat", type=float, default=120.0)
    p.add_argument("--llm-timeout", type=float, default=8.0)
    p.add_argument("--state-file", default="state.json")

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
                   help="do not open the lampctl/DSH control socket")

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
        print(f"  (sounddevice unavailable: {exc})")

    print("\nSerial ports:")
    from serial.tools import list_ports

    for p in list_ports.comports():
        print(f"  {p.device}  {p.description}")


def make_brain(args: argparse.Namespace):
    use_llm = not args.no_llm and os.environ.get("USE_LLM", "1").lower() not in (
        "0", "false", "no", "off",
    )
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if use_llm and not api_key:
        LOG.warning("USE_LLM is on but DEEPSEEK_API_KEY is missing; using the local policy")
        use_llm = False
    if use_llm:
        try:
            import openai  # noqa: F401
        except ImportError:
            LOG.warning(
                "openai SDK not installed; using the local policy "
                "(pip install -r requirements.txt)"
            )
            use_llm = False

    if use_llm:
        LOG.info(
            "brain: DeepSeek %s (timeout %.1fs, %d calls/hour max)",
            args.model,
            args.llm_timeout,
            args.max_calls_per_hour,
        )
        return DeepSeekAgent(
            api_key=api_key,
            model=args.model,
            base_url=args.api_base,
            timeout_s=args.llm_timeout,
            max_calls_per_hour=args.max_calls_per_hour,
        )
    LOG.info("brain: local rule policy (no network)")
    return LocalBrain()


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

    events_q: "queue.Queue" = queue.Queue(maxsize=64)

    trigger_events = {"STARTLE", "WAKE", "TOUCH", "APPROACH", "LINK_LOST"}

    def on_event(msg) -> None:  # noqa: ANN001
        # Only external stimuli wake the slow loop. E,MOOD/CMD/BOOT are the
        # lamp's own announcements; feeding them back would make the policy
        # re-evaluate on every mood change and flap calm<->alert.
        if getattr(msg, "name", "") not in trigger_events:
            return
        try:
            events_q.put_nowait(msg)
        except queue.Full:
            try:
                events_q.get_nowait()
                events_q.put_nowait(msg)
            except queue.Empty:
                pass

    link = FeatherLink(
        port=args.port, baud=args.baud, dry_run=args.dry_run, on_event=on_event
    )
    link.start()

    mac = MacContext()
    detector = BeatDetector(args.samplerate, args.blocksize)
    system = SystemAudio(detector, args.samplerate, args.blocksize)
    if not args.no_music:
        system.start()

    sensor_hub = SensorHub(meter, link, mac, system)
    store = StateStore(args.state_file)
    state = LampState()
    dev_hub = DeviceHub(link, state, store, sensor_hub)
    brain = make_brain(args)
    stop = threading.Event()

    control = None
    if not args.no_control:
        try:
            control = ControlServer(
                link, state, store, sensor_hub, args.control_socket
            )
            control.start()
        except Exception as exc:  # noqa: BLE001 - control is optional
            LOG.warning("control socket disabled: %s", exc)
            control = None

    def on_sigint(signum, frame):  # noqa: ANN001
        LOG.info("signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    threads = [
        threading.Thread(
            target=fast_loop,
            args=(stop, meter, link, state, args.fast_hz),
            name="fast",
            daemon=True,
        ),
        threading.Thread(
            target=music_loop,
            args=(stop, sensor_hub, link, state),
            name="music",
            daemon=True,
        ),
        threading.Thread(
            target=slow_loop,
            args=(stop, args, sensor_hub, dev_hub, state, brain, store, events_q),
            name="slow",
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()

    LOG.info(
        "fast %.0f Hz | slow %.0f..%.0f s | port %s | span %.1f dB | music %s",
        args.fast_hz,
        args.slow_min,
        args.slow_max,
        args.port,
        args.span_db,
        system.device_name if system.available else "mic only",
    )
    if args.sim_audio:
        LOG.info("simulated audio: no microphone will be opened")

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            wait = 10.0 if deadline is None else min(10.0, max(0.0, deadline - time.monotonic()))
            if stop.wait(wait):
                break
            print(status_line(meter, link, state, brain), flush=True)
    finally:
        stop.set()
        try:
            events_q.put_nowait(None)
        except queue.Full:
            pass
        for t in threads:
            t.join(timeout=2.0)
        if control is not None:
            control.stop()
        if link.connected or args.dry_run:
            link.send_mood(Decision("sleepy", 30, 15, 10, 60, "goodnight", 45, "host"))
            link.send_lid(10, 3000, 0)
            time.sleep(0.15)
        link.close()
        system.stop()
        meter.stop()
        store.save()
        LOG.info("bye")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
