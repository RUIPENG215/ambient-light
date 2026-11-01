#!/usr/bin/env python3
"""A fake Feather on a pseudo-terminal.

Lets you exercise the whole host (serial link, fast loop, slow loop, protocol)
before the board is flashed, and lets CI run without hardware.

    python3 sim_feather.py --pty
    # prints: PTY=/dev/ttys012
    python3 ambient_host.py --port /dev/ttys012 --no-llm --sim-audio
"""

from __future__ import annotations

import argparse
import math
import os
import pty
import random
import select
import sys
import time
import tty


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pty", action="store_true", help="create a pty and print its path (always on)")
    ap.add_argument("--duration", type=float, default=0.0, help="exit after N seconds")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    master, slave = pty.openpty()
    for fd in (master, slave):
        try:
            tty.setraw(fd)
        except Exception:  # noqa: BLE001
            pass
    path = os.ttyname(slave)
    os.set_blocking(master, False)

    print(f"PTY={path}", flush=True)
    print(f"[sim] fake Feather on {path}", file=sys.stderr, flush=True)

    level = 0
    mood = "calm"
    hue, speed, bright, louver = 150, 90, 180, 30
    rx_lines = 0
    rx_l = 0
    buf = b""
    started = time.monotonic()
    next_report = 0.0
    next_tick = 0.0

    try:
        while True:
            now = time.monotonic()
            if args.duration and now - started >= args.duration:
                break

            readable, _, _ = select.select([master], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except (BlockingIOError, OSError):
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("ascii", errors="replace").strip()
                    if not line:
                        continue
                    rx_lines += 1
                    parts = line.split(",")
                    if parts[0] == "L" and len(parts) >= 2:
                        rx_l += 1
                        try:
                            level = max(0, min(100, int(parts[1])))
                        except ValueError:
                            pass
                    elif parts[0] == "M" and len(parts) >= 6:
                        try:
                            mood = parts[1]
                            hue = int(parts[2])
                            speed = int(parts[3])
                            bright = int(parts[4])
                            louver = int(parts[5])
                        except ValueError:
                            continue
                        if not args.quiet:
                            print(
                                f"[sim] RX M {mood} hue={hue} speed={speed} "
                                f"bright={bright} louver={louver}",
                                file=sys.stderr,
                                flush=True,
                            )
                    elif parts[0] == "P":
                        os.write(master, b"PONG\n")
                    elif parts[0] == "T":
                        pass

            now = time.monotonic()
            if now >= next_report:
                next_report = now + 0.25
                phase = now % 25.0
                prox = 230 if phase < 3.0 else random.randint(0, 25)
                als = int(200 + 100 * math.sin(now / 11.0))
                lux = max(0.0, 40 + 30 * math.sin(now / 17.0))
                frame = f"S,{prox},{als},{int(lux)},{level},{mood}\n"
                try:
                    os.write(master, frame.encode("ascii"))
                except OSError:
                    break

            if now >= next_tick:
                next_tick = now + 2.0
                if not args.quiet:
                    print(
                        f"[sim] STAT rx_l={rx_l} rx_lines={rx_lines} "
                        f"level={level} mood={mood}",
                        file=sys.stderr,
                        flush=True,
                    )
    except KeyboardInterrupt:
        pass
    finally:
        print(
            f"[sim] STAT rx_l={rx_l} rx_lines={rx_lines} level={level} mood={mood}",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.close(master)
            os.close(slave)
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
