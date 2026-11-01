from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feather_link import FeatherLink  # noqa: E402


class SocketSerial:
    """Minimal pyserial look-alike over one end of a socketpair."""

    def __init__(self, sock: socket.socket, name: str = "socketpair") -> None:
        self.sock = sock
        self.port = name
        self._buf = b""
        self.sock.settimeout(0.2)

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except (BlockingIOError, TimeoutError):
                return b""
            if not chunk:
                raise ConnectionError("peer closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line + b"\n"

    def write(self, data: bytes) -> int:
        self.sock.sendall(data)
        return len(data)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def wait_until(pred, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class TestFeatherLink(unittest.TestCase):
    def setUp(self) -> None:
        self.a, self.b = socket.socketpair()
        self.link = FeatherLink(serial_factory=lambda: SocketSerial(self.a))
        self.link.start()

    def tearDown(self) -> None:
        self.link.close()
        try:
            self.b.close()
        except OSError:
            pass

    def test_parses_telemetry_and_events(self):
        self.b.sendall(b"E,BOOT,2.0.0\n")
        self.b.sendall(b"S,12,345,8,43,curious\n")

        self.assertTrue(wait_until(lambda: self.link.sensors.prox == 12))
        sensors = self.link.sensors
        self.assertEqual(sensors.als, 345)
        self.assertEqual(sensors.energy, 43)
        self.assertEqual(sensors.mood, "curious")
        self.assertTrue(self.link.connected)

        self.assertTrue(wait_until(lambda: any(e.name == "BOOT" for e in self.link.events)))
        self.assertEqual(self.link.recent_events(30.0)[0].name, "BOOT")

    def test_writes_frames(self):
        self.assertTrue(wait_until(lambda: self.link.connected))
        self.assertTrue(self.link.send_line("G,80,0,1"))
        self.assertTrue(self.link.send_line("F,30,200,120,28,1,30,8,12,55,55,3000"))

        self.b.settimeout(1.0)
        data = self.b.recv(4096).decode("ascii")
        self.assertIn("G,80,0,1\n", data)
        self.assertIn("F,30,200,120,28,1,30,8,12,55,55,3000\n", data)

    def test_survives_garbage(self):
        self.b.sendall(b"garbage\nS,1,2\n\n")
        self.b.sendall(b"S,7,100,3,12,calm\n")
        self.assertTrue(wait_until(lambda: self.link.sensors.prox == 7))

    def test_reconnects_after_peer_close(self):
        self.assertTrue(wait_until(lambda: self.link.connected))
        self.b.close()
        self.assertTrue(wait_until(lambda: not self.link.connected, timeout=2.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
