"""Where the lamp keeps its things.

Run from the project folder (the developer way) everything stays next to the
code, as before. Run as the double-click app, state, history, logs and the API
key live in ~/Library/Application Support/Ambient Lamp, so the app can be moved
and the project folder stays clean.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "Ambient Lamp"


def data_dir() -> Path:
    override = os.environ.get("AMBIENT_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif os.environ.get("AMBIENT_APP") == "1":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        return HERE
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_file() -> Path:
    return data_dir() / ".env"


def load_env() -> None:
    """Read KEY=value lines into os.environ without overriding what is set."""
    for path in (env_file(), HERE / ".env"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_env_value(key: str, value: str) -> Path:
    """Set one KEY=value in the data dir's .env, keeping the other lines."""
    path = env_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ[key] = value
    return path
