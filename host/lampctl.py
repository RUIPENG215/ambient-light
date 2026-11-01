#!/usr/bin/env python3
"""Talk to a running ambient_host.py over its Unix control socket.

    python3 lampctl.py observe
    python3 lampctl.py light --val 90 --ease 2500
    python3 lampctl.py light --grad 30 --wave-amp 40 --wave-hz 14
    python3 lampctl.py say "暗一点，慢一点"
    python3 lampctl.py blink

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

LIGHT_FIELDS = ("hue", "sat", "val", "grad", "wave_n", "wave_amp", "wave_hz",
                "breath_hz", "breath_amp", "open", "ease")


def send(request: dict[str, Any], path: str, timeout: float = 20.0) -> dict:
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
    light = lamp.get("light") or {}
    if light:
        print("light   " + " ".join(f"{k}={light[k]}" for k in LIGHT_FIELDS if k in light))
    print(
        f"stream  {lamp.get('stream') or 'off':<8} {lamp.get('frames', 0)} frames "
        f"@ {lamp.get('hz', 0)} Hz"
        + (f"  ({lamp['reason']})" if lamp.get("reason") else "")
    )
    sensors = data.get("sensors", {})
    print(
        f"sensors prox={sensors.get('prox', 0):3d} lux={sensors.get('lux', 0):5.1f} "
        f"open={sensors.get('open', 0):3d} fw={sensors.get('origin', '?')}"
    )
    senses = data.get("senses") or {}
    if senses:
        print(
            f"senses  level={senses.get('level', 0):5.1f} music={senses.get('music', 0):3d} "
            f"beat={senses.get('beat', 0):3d} prox={senses.get('prox', 0):3d} "
            f"lux={senses.get('lux', 0):5.1f} idle={senses.get('idle_s', 0)}s"
        )
    reflex = data.get("reflex") or {}
    if reflex:
        print(
            f"reflex  beat={reflex.get('beat_env')} prox={reflex.get('prox_env')} "
            f"lux={reflex.get('lux_env')} startles={reflex.get('startles')}"
        )
    mac = data.get("mac", {})
    print(
        f"mac     idle={mac.get('idle_s', 0)}s present={mac.get('user_present')} "
        f"playing={mac.get('playing')} title={mac.get('title', '')!r}"
    )
    drives = data.get("drives", {})
    if drives:
        print("drives  " + " ".join(f"{k}={v}" for k, v in drives.items()))
    for entry in data.get("memory", [])[-3:]:
        print(f"memory  {entry.get('t', '')}  {entry.get('text', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Control a running ambient lamp host")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--json", action="store_true", help="print the raw response")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("observe", help="read the light, the sensors and the reflex")
    sub.add_parser("ping", help="check that the host control socket is alive")

    light = sub.add_parser("light", help="write a few light numbers, keep the rest")
    for _name in LIGHT_FIELDS:
        light.add_argument(f"--{_name.replace('_', '-')}", dest=_name, type=int, default=None)
    light.add_argument("--reason", default="lampctl")

    blink = sub.add_parser("blink", help="a transient: blink or wince")
    blink.add_argument("kind", nargs="?", choices=("blink", "wince"), default="blink")

    say = sub.add_parser("say", help="talk to the agent and get its answer")
    say.add_argument("text", nargs="+")

    args = parser.parse_args(argv)

    if args.command == "observe":
        request: dict[str, Any] = {"cmd": "observe"}
    elif args.command == "ping":
        request = {"cmd": "ping"}
    elif args.command == "blink":
        request = {"cmd": "blink", "kind": args.kind}
    elif args.command == "say":
        request = {"cmd": "say", "text": " ".join(args.text)}
    else:
        request = {"cmd": "light", "reason": args.reason}
        for key in LIGHT_FIELDS:
            value = getattr(args, key)
            if value is not None:
                request[key] = value

    try:
        response = send(request, args.socket)
    except FileNotFoundError:
        print(f"no control socket at {args.socket}; is ambient_host.py running?",
              file=sys.stderr)
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
    elif args.command == "blink":
        print(f"ok {response.get('line')}")
    elif args.command == "say":
        print(response.get("reply", ""))
        actions = response.get("actions") or []
        if actions:
            print(f"  ({', '.join(actions)})")
    else:
        changed = ",".join(response.get("changed") or ["nothing"])
        print(f"ok changed={changed}\n   {response.get('line')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
