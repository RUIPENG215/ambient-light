#!/usr/bin/env python3
"""Ambient Lamp, as one thing you start.

One process: the host (reflex + agent + serial), the local page server, and a
small frameless always-on-top window in the corner of the screen showing the
lamp's face. Click the ring to open the panel; "–" sends it back to the corner.

    .venv/bin/python lamp_app.py              # what the .app runs
    .venv/bin/python lamp_app.py --browser    # no window, open a browser tab

If a host is already running (started with ./lamp.sh), this only opens the
window onto it instead of starting a second one.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paths  # noqa: E402

PET_SIZE = (300, 250)        # ring bottom-right, room for a speech bubble
PANEL_SIZE = (390, 640)
MARGIN = 18
SOCKET = os.environ.get("AMBIENT_SOCK", "/tmp/ambient-lamp.sock")

LOG = logging.getLogger("app")


def host_alive() -> bool:
    try:
        from lampctl import send

        return bool(send({"cmd": "ping"}, SOCKET, timeout=0.5).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def start_host(extra: list[str], data: Path) -> dict:
    """Run ambient_host.main on a thread; returns a box that gets the Host."""
    import ambient_host

    box: dict = {}
    argv = ["--span-db", "30", "--state-file", str(data / "state.json"),
            "--log-file", str(data / "logs" / "host.log")] + extra

    def run() -> None:
        try:
            def ready(host) -> None:
                host.dark_on_exit = True       # quitting the app turns the lamp off
                box["host"] = host

            box["code"] = ambient_host.main(argv, embedded=True, on_ready=ready)
        except SystemExit as exc:
            box["code"] = exc.code
        except Exception:  # noqa: BLE001
            LOG.exception("the host stopped")
            box["code"] = 1

    thread = threading.Thread(target=run, name="host", daemon=True)
    thread.start()
    box["thread"] = thread
    return box


def free_port(preferred: int) -> int:
    import socket

    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    return preferred


class Shell:
    """What the page may ask the window to do."""

    def __init__(self) -> None:
        self._window = None      # underscore: pywebview must not expose these to the page
        self._on_quit = None

    def _place(self, size: tuple) -> None:
        import webview

        window = self._window
        if window is None:
            return
        width, height = size
        try:
            screen = webview.screens[0]
            x = screen.x + screen.width - width - MARGIN
            y = screen.y + screen.height - height - MARGIN - 70     # clear the Dock
        except Exception:  # noqa: BLE001
            x, y = 900, 400
        window.resize(width, height)
        window.move(int(x), int(y))

    def mode(self, name: str) -> bool:
        panel = name == "panel"
        try:
            # An always-on-top window also sits on top of the input method's
            # candidate list, which hides it while typing Chinese. The panel
            # is an ordinary window; only the ring in the corner floats.
            self._window.on_top = not panel
        except Exception:  # noqa: BLE001
            pass
        self._place(PANEL_SIZE if panel else PET_SIZE)
        return True

    def quit(self) -> bool:
        """Answer the page at once, then shut down on another thread.

        Stopping the host and destroying the window from inside this call
        deadlocks: the window waits for the call to return while the call
        waits for the window. So: hide, stop the host (which switches the
        lamp off), and end the process outright.
        """
        def later() -> None:
            try:
                if self._window is not None:
                    self._window.hide()
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._on_quit is not None:
                    self._on_quit()
            finally:
                os._exit(0)

        threading.Thread(target=later, name="quit", daemon=True).start()
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ambient Lamp")
    parser.add_argument("--browser", action="store_true",
                        help="open a browser tab instead of the corner window")
    parser.add_argument("--port", type=int, default=8765)
    args, host_args = parser.parse_known_args(argv)

    data = paths.data_dir()
    (data / "logs").mkdir(parents=True, exist_ok=True)
    paths.load_env()

    box: dict = {}
    if host_alive():
        print("a host is already running; opening the window onto it")
        logging.basicConfig(level=logging.INFO)
    else:
        box = start_host(host_args, data)
        deadline = time.monotonic() + 12.0
        while "host" not in box and "code" not in box and time.monotonic() < deadline:
            time.sleep(0.1)
        if "code" in box and box.get("code") not in (0, None):
            print("the host could not start; see", data / "logs" / "host.log")

    import webchat

    port = free_port(args.port)
    server = webchat.serve_in_thread(port, SOCKET)
    url = f"http://127.0.0.1:{port}/"

    def stop_host() -> None:
        host = box.get("host")
        if host is not None:
            host.stop.set()
            thread = box.get("thread")
            if thread is not None:
                thread.join(timeout=10.0)   # long enough for the lamp-off command

    try:
        if args.browser:
            raise ImportError("browser requested")
        import webview
    except ImportError:
        import webbrowser

        webbrowser.open(url)
        print(f"Ambient Lamp at {url}  (Ctrl+C to quit)")
        try:
            while box.get("thread") is None or box["thread"].is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        stop_host()
        server.shutdown()
        return 0

    shell = Shell()
    shell._on_quit = stop_host

    # Closing the Terminal window (SIGHUP) or a kill should also end cleanly,
    # so the lamp goes dark instead of falling back to amber.
    import signal

    def on_signal(signum, frame):  # noqa: ANN001
        stop_host()
        os._exit(0)

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass
    window = webview.create_window(
        "Ambient Lamp", url, js_api=shell,
        width=PET_SIZE[0], height=PET_SIZE[1],
        frameless=True, on_top=True, transparent=True,
        easy_drag=False, resizable=False, shadow=False,
    )
    shell._window = window
    webview.start(lambda: shell.mode("pet"))
    stop_host()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
