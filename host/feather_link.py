"""Serial link to the Feather: line protocol, reconnect, telemetry parsing.

v2.1 adds the LID (eyelid) channel, the U music bus, C coupling gains and R
reflex thresholds. All of them are optional: an old board ignores unknown
lines, and a new board runs fine with only L/M.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

import serial
from serial.tools import list_ports


@dataclass(frozen=True)
class Sensors:
    prox: int = 0
    als: int = 0
    lux: float = 0.0
    energy: int = 0
    mood: str = "calm"
    lid: int = 0
    at: float = 0.0


@dataclass(frozen=True)
class Message:
    kind: str  # "sensor" | "event" | "pong"
    sensor: Optional[Sensors] = None
    name: str = ""
    detail: str = ""
    at: float = 0.0


def parse_line(line: str) -> Optional[Message]:
    """Parse one line from the Feather. Unknown / malformed lines -> None."""
    line = line.strip()
    if not line:
        return None
    if line == "PONG":
        return Message(kind="pong")

    parts = line.split(",")
    tag = parts[0]

    if tag == "S" and len(parts) >= 6:
        try:
            return Message(
                kind="sensor",
                sensor=Sensors(
                    prox=int(parts[1]),
                    als=int(parts[2]),
                    lux=float(parts[3]),
                    energy=int(parts[4]),
                    mood=parts[5],
                    lid=int(parts[6]) if len(parts) >= 7 else 0,
                    at=time.monotonic(),
                ),
            )
        except ValueError:
            return None

    if tag == "E" and len(parts) >= 2:
        return Message(
            kind="event",
            name=parts[1],
            detail=",".join(parts[2:]),
            at=time.monotonic(),
        )

    return None


def find_serial_port() -> Optional[str]:
    """Best-effort auto-detect of the Feather's USB CDC port."""
    candidates: list[tuple[int, str]] = []
    for p in list_ports.comports():
        text = " ".join(
            str(x) for x in (p.device, p.description, p.manufacturer, p.product, p.hwid)
        ).lower()
        device = str(p.device)
        if "adafruit" in text:
            candidates.append((0, device))
        elif "nrf" in text or "feather" in text:
            candidates.append((1, device))
        elif "usbmodem" in device or "usbserial" in device:
            candidates.append((2, device))
    candidates.sort()
    return candidates[0][1] if candidates else None


class FeatherLink:
    """Owns the serial port and a reader thread.

    Writes are non-blocking with respect to the caller (a short timeout is
    enough at 115200 baud). If the board is unplugged the reader retries every
    two seconds and the host keeps running.
    """

    def __init__(
        self,
        port: str = "auto",
        baud: int = 115200,
        dry_run: bool = False,
        logger: Optional[logging.Logger] = None,
        on_event: Optional[Callable[[Message], None]] = None,
        serial_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self.port = port
        self._serial_factory = serial_factory
        self.baud = baud
        self.dry_run = dry_run
        self.log = logger or logging.getLogger("serial")
        self.on_event = on_event

        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._sensors = Sensors()
        self.events: Deque[Message] = deque(maxlen=64)
        self.rx_count = 0
        self.tx_count = 0
        self.connected = False
        self.last_rx_at = 0.0

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        if self.dry_run:
            self.log.info("dry-run: serial commands will be printed, not sent")
            return
        self._thread = threading.Thread(target=self._reader, name="serial-reader", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._drop()

    def _drop(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None
        self.connected = False

    def _open(self) -> serial.Serial:
        if self._serial_factory is not None:
            return self._serial_factory()  # type: ignore[return-value]
        port = self.port
        if port == "auto":
            port = find_serial_port() or ""
            if not port:
                raise serial.SerialException("no USB serial port found")
        return serial.Serial(port, self.baud, timeout=0.2, write_timeout=0.5)

    # ---------------- reader ----------------
    def _reader(self) -> None:
        while not self._stop.is_set():
            if self._ser is None:
                try:
                    ser = self._open()
                    with self._lock:
                        self._ser = ser
                    self.connected = True
                    self.last_rx_at = time.monotonic()
                    self.log.info(
                        "serial connected: %s @ %d", getattr(ser, "port", "?"), self.baud
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("serial open failed: %s", exc)
                    self._stop.wait(2.0)
                    continue

            try:
                raw = self._ser.readline() if self._ser is not None else b""
            except Exception as exc:  # noqa: BLE001
                self.log.warning("serial read failed: %s", exc)
                self._drop()
                self._stop.wait(1.0)
                continue

            if not raw:
                continue

            self.rx_count += 1
            self.last_rx_at = time.monotonic()
            line = raw.decode("ascii", errors="replace").strip()
            msg = parse_line(line)
            if msg is None:
                self.log.debug("ignored serial line: %r", line)
                continue

            if msg.kind == "sensor" and msg.sensor is not None:
                self._sensors = msg.sensor
            elif msg.kind == "event":
                self.events.append(msg)
                self.log.debug("event: %s %s", msg.name, msg.detail)
                if self.on_event is not None:
                    try:
                        self.on_event(msg)
                    except Exception:  # noqa: BLE001
                        self.log.exception("on_event callback failed")
            elif msg.kind == "pong":
                self.log.debug("PONG")

    # ---------------- writer ----------------
    def send_line(self, line: str) -> bool:
        if self.dry_run:
            print(f"[dry] {line}")
            return True
        ser = self._ser
        if ser is None:
            return False
        try:
            with self._lock:
                ser.write((line + "\n").encode("ascii"))
            self.tx_count += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("serial write failed: %s", exc)
            self._drop()
            return False

    def send_level(self, level: int) -> bool:
        return self.send_line(f"L,{max(0, min(100, int(level)))}")

    def send_mood(self, decision) -> bool:  # noqa: ANN001 - avoids circular import
        return self.send_line(decision.serial_line())

    def send_lid(
        self,
        open_pct: int,
        ms: int = 800,
        mode: int = 0,
        amp: float = 4.0,
        period: float = 6.0,
    ) -> bool:
        """mode: 0 hold, 1 breathe, 2 blink, 3 wince."""
        open_pct = max(0, min(100, int(open_pct)))
        ms = max(0, min(60000, int(ms)))
        mode = max(0, min(3, int(mode)))
        if mode == 1:
            return self.send_line(f"LID,{open_pct},{ms},{mode},{amp:.1f},{period:.1f}")
        return self.send_line(f"LID,{open_pct},{ms},{mode}")

    def send_music(self, level: int, beat: int = 0, present: int = 0) -> bool:
        return self.send_line(
            f"U,{max(0, min(100, int(level)))},"
            f"{max(0, min(100, int(beat)))},{1 if present else 0}"
        )

    def send_coupling(self, audio: int, beat: int, prox: int, idle: int) -> bool:
        return self.send_line(
            f"C,{max(0, min(100, int(audio)))},{max(0, min(100, int(beat)))},"
            f"{max(0, min(100, int(prox)))},{max(0, min(100, int(idle)))}"
        )

    def send_reflex(self, startle: int, prox_near: int, enabled: bool = True) -> bool:
        return self.send_line(
            f"R,{max(0, min(100, int(startle)))},{max(0, min(255, int(prox_near)))},"
            f"{1 if enabled else 0}"
        )

    def ping(self) -> bool:
        return self.send_line("P")

    # ---------------- state ----------------
    @property
    def sensors(self) -> Sensors:
        return self._sensors

    def recent_events(self, window_s: float = 30.0) -> list[Message]:
        now = time.monotonic()
        return [ev for ev in self.events if now - ev.at <= window_s]
