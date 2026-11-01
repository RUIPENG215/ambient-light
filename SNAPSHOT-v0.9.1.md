# Snapshot v0.9.1 — 2026-10-31 12:02 (+08)

This is the first end-to-end version of the interactive ambient lamp.

## What works

- **Firmware 2.1.0** (`firmware/ambient_light/ambient_light.ino`)
  - 32 × WS2812B on D6, APDS9960 proximity/lux
  - mood state machine: calm / alert / curious / sleepy
  - reflex layer: STARTLE, proximity wake, 45 s sleep
  - light-modulated eyelid: `LID` hold / breathe / blink / wince
  - music modulation bus `U`, coupling `C`, reflex thresholds `R`
  - host TTL 180 s; lid and colour also release after TTL
- **Host** (`host/`)
  - 20 Hz `L`, 20 Hz `U`, 3–10 s slow loop
  - DeepSeek agent with 7 tools, drives + memory in `state.json`
  - Mac sensors: microphone, optional BlackHole music, idle / now playing
  - local rule fallback (`--no-llm`) with sustained-only alert and 8 s dwell
  - control socket (`/tmp/ambient-lamp.sock`), `lampctl.py` CLI
  - `flow` command: slow calm breathing with cycling hue
  - local web chat (`webchat.py`): status panel + chat + four quick actions
- **DeepSeek**
  - `deepseek-flash`, thinking disabled, forced/normal tool calls
  - web chat loads `host/.env`; host needs `source .env`
  - no key → fixed intents still work in the web page

## Verified

- firmware self-check: **28 pass / 0 fail**
- host tests: **43 pass**
- pty end-to-end smoke: **pass**
- real board: flash, `T`, `P`, `M`, `LID`, `L` startle verified manually
- web chat: `DeepSeek: ready`, status, chat, colour change, release verified
- control socket: mood / lid / observe / flow / release verified
- firmware stub covers both APDS9960 API styles, including the
  `colorDataReady/getColorData/calculateLux` block edited at 11:03

## Run

```bash
cd host
source .venv/bin/activate
python3 ambient_host.py --port auto --no-llm --no-music   # stable demo
python3 webchat.py --port 8765 --open                     # chat UI
```

With DeepSeek in the host's ambient loop:

```bash
set -a; source .env; set +a
python3 ambient_host.py --port auto --no-music
```

## Not included in the snapshot

- `host/.env` (API key)
- `.venv/`
- `state.json` (runtime drives/memory)
- `webchat_history.json` (chat log)
- `__pycache__/`, `.DS_Store`

## Known limits / next

- Paper shade + base + diffuser not yet assembled; APDS must face the room.
- VL53L0X purchased but not wired into firmware yet.
- SG90 eyelid is reserved (`ENABLE_SERVO 0`); current eyelid is light modulation.
- BlackHole not installed; music beat falls back to microphone.
- Firmware has only been host-compiled with stubs, not built with the real nRF52 toolchain in this snapshot.
- Next: physical assembly, reset `state.json` before the demo, backup video, then MCP / `lampd` + `agentd` split.
