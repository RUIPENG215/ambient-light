#!/usr/bin/env python3
"""Talk to a running ambient_host.py over its Unix control socket.

Examples:
    python3 lampctl.py observe
    python3 lampctl.py mood curious --hue 150 --lid 85 --seconds 120
    python3 lampctl.py lid 20 --ms 2000
    python3 lampctl.py release

The socket path defaults to $AMBIENT_SOCK or /tmp/ambient-lamp.sock.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

DEFAULT_SOCKET = os.environ.get("AMBIENT_SOCK", "/tmp/ambient-lamp.sock")
MOODS = ("calm", "alert", "curious", "sleepy")
MODES = ("hold", "breathe", "blink", "wince")


def send(request: dict[str, Any], path: str, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        chunks = b""
        while not chunks.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks += chunk
    if not chunks:
        raise RuntimeError("empty response from control socket")
    return json.loads(chunks.decode("utf-8"))


def print_observe(data: dict) -> None:
    lamp = data.get("lamp", {})
    sensors = data.get("sensors", {})
    mac = data.get("mac", {})
    music = data.get("music", {})
    drives = data.get("drives", {})
    print(
        f"lamp    mood={lamp.get('mood', '?'):<8} lid={lamp.get('lid', 0):3d} "
        f"since={lamp.get('since_s', 0)}s manual={lamp.get('manual_for_s', 0)}s"
    )
    print(
        f"sensors prox={sensors.get('prox', 0):3d} lux={sensors.get('lux', 0):4d} "
        f"energy={sensors.get('energy', 0):3d} fw_mood={sensors.get('mood', '?')} "
        f"fw_lid={sensors.get('lid', 0)}"
    )
    print(
        f"mac     idle={mac.get('idle_s', 0)}s present={mac.get('user_present')} "
        f"playing={mac.get('playing')} title={mac.get('title', '')!r}"
    )
    print(f"music   energy={music.get('energy', 0)} beat={music.get('beat', 0)}")
    print(
        "drives  " + " ".join(f"{key}={value}" for key, value in drives.items())
    )
    for entry in data.get("memory", [])[-3:]:
        print(f"memory  {entry.get('t', '')}  {entry.get('text', '')}")
    for event in data.get("events", [])[-4:]:
        print(
            f"event   {event.get('name', '')} {event.get('ago_s', 0)}s ago"
            f" {event.get('detail', '')}".rstrip()
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Control a running ambient lamp host")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--json", action="store_true", help="print the raw response")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("observe", help="read lamp state, sensors and macOS context")
    sub.add_parser("ping", help="check that the host control socket is alive")

    mood = sub.add_parser("mood", help="set mood and take a short mind lease")
    mood.add_argument("mood", choices=MOODS)
    mood.add_argument("--hue", type=int, default=150)
    mood.add_argument("--speed", type=int, default=90)
    mood.add_argument("--bright", type=int, default=180)
    mood.add_argument("--lid", type=int, default=70)
    mood.add_argument("--reason", default="chat")
    mood.add_argument("--seconds", type=int, default=60)

    lid = sub.add_parser("lid", help="move the eyelid and take a short mind lease")
    lid.add_argument("open", type=int)
    lid.add_argument("--ms", type=int, default=800)
    lid.add_argument("--mode", choices=MODES, default="hold")
    lid.add_argument("--amp", type=float, default=4.0)
    lid.add_argument("--period", type=float, default=6.0)
    lid.add_argument("--seconds", type=int, default=60)

    flow = sub.add_parser(
        "flow", help="slow calm breathing with slowly cycling colours"
    )
    flow.add_argument("--seconds", type=int, default=30)
    flow.add_argument("--speed", type=int, default=10)
    flow.add_argument("--bright", type=int, default=150)
    flow.add_argument("--lid", type=int, default=80)
    flow.add_argument("--hue", type=int, default=150)
    flow.add_argument("--hue-step", type=int, default=3)
    flow.add_argument("--interval-ms", type=int, default=300)

    sub.add_parser("release", help="give control back to the host's own slow loop")

    args = parser.parse_args(argv)

    if args.command == "observe":
        request = {"cmd": "observe"}
    elif args.command == "ping":
        request = {"cmd": "ping"}
    elif args.command == "release":
        request = {"cmd": "release"}
    elif args.command == "flow":
        request = {
            "cmd": "flow",
            "seconds": args.seconds,
            "speed": args.speed,
            "bright": args.bright,
            "lid": args.lid,
            "hue": args.hue,
            "hue_step": args.hue_step,
            "interval_ms": args.interval_ms,
        }
    elif args.command == "mood":
        request = {
            "cmd": "mood",
            "mood": args.mood,
            "hue": args.hue,
            "speed": args.speed,
            "bright": args.bright,
            "lid": args.lid,
            "reason": args.reason,
            "seconds": args.seconds,
        }
    else:
        request = {
            "cmd": "lid",
            "open": args.open,
            "ms": args.ms,
            "mode": args.mode,
            "amp": args.amp,
            "period": args.period,
            "seconds": args.seconds,
        }

    try:
        response = send(request, args.socket)
    except FileNotFoundError:
        print(
            f"no control socket at {args.socket}; is ambient_host.py running?",
            file=sys.stderr,
        )
        return 2
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print(f"control socket error: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"bad response from host: {exc}", file=sys.stderr)
        return 2

    if args.json or not response.get("ok"):
        print(json.dumps(response, ensure_ascii=False, indent=1))
        return 0 if response.get("ok") else 1

    if args.command == "observe":
        print_observe(response)
    elif args.command == "ping":
        print(f"pong pid={response.get('pid', '?')}")
    elif args.command == "release":
        print("released; the host's own slow loop resumes")
    elif args.command == "flow":
        print(
            f"flow: calm breathing, hue cycling every {response.get('interval_ms')} ms, "
            f"for {response.get('seconds')} s"
        )
    elif args.command == "mood":
        print(
            f"ok {response.get('line', '')}  (manual for {response.get('manual_for_s', 0)}s)"
        )
    else:
        print(
            f"ok lid={response.get('lid')} mode={response.get('mode')}  "
            f"(manual for {response.get('manual_for_s', 0)}s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
