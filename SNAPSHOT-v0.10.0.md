# Snapshot v0.10.0 — 2026-11-01 23:10 (+08)

The first version that is **one reflex layer and one agent**. Firmware 2.2.0
plus a rewritten host. The previous snapshot is `SNAPSHOT-v0.9.1.md` (firmware
2.1.0, four moods, a state machine).

## The idea

> **The reflex lives on the computer (25 Hz, no network). The thinking is one
> agent (optional).**

```
sensors (sound / music / proximity / lux / idle)
   │
   ▼
reflex.py        25 Hz, bends eleven numbers
   │  F frames
   ▼
firmware 2.2.0   50 fps; falls back to amber breathing 1.5 s after the stream stops
   ▲
   │  the same eleven numbers
agent.py         DeepSeek, ~25 s per turn
```

Three independent layers, and any one of them can die without turning the lamp
into a brick:

| Situation | Who owns it | The lamp |
|---|---|---|
| key and network | agent writes, reflex reacts | complete |
| no key / offline / `--no-llm` | reflex only | still breathes, still follows sound and hands |
| host died | firmware fallback | amber breathing after 1.5 s |

## Firmware 2.2.0

- Knows **colour field / spatial wave / breathing envelope / transients /
  reflexes**. Does not know moods.
- `F` parameter frame: `hue sat val grad wave_n wave_amp wave_hz breath_hz
  breath_amp open ease`
- `K` transients: blink, wince, white flash
- `G`: channel ceiling, gamma, temporal dither
- fallback look and a slow doze; `FRAME_LOST` / `FRAME_OK` events
- 8-field telemetry: `S,prox,als,lux,energy,origin,open,flags`
- Still parses `M` / `LID` / `L` / `U` / `C` / `R`, **the host no longer sends
  them**; removal is v2.3 and needs a reflash

## Host

| File | Lines | Role |
|---|---|---|
| `host/ambient_host.py` | 600 | fast loop / music / light stream / agent loop |
| `host/agent.py` | 652 | the one mind: six tools, drives, memory, rate limit |
| `host/reflex.py` | 214 | sound→breath, beat→brightness, hand→open, lux→floor, idle→dim, jump→startle |
| `host/looks.py` | 284 | `Look` (eleven floats), the 25 Hz engine, host-side easing |
| `host/control.py` | 267 | local socket: `light` / `blink` / `say` / `observe` |
| `host/lampctl.py` | ~200 | CLI: change the light, talk to it, read state |
| `host/webchat.py` | 561 | page: status and chat (owns no model) |
| `host/lamp.sh` | — | one command to start, stop, tail and talk |

Agent tools: `set_light` (eleven continuous values, no enums), `blink`,
`set_reflex` (tune its own sensitivity), `feel`, `remember`, `observe`.

## Removed

`brain.py` (Decision / LocalBrain / build_context / fingerprint),
`stream_look.py`, the named-look catalogue, the `flow` command, the web page's
own DeepSeek and prompt, the mind lease and `release`, `state 2.json`,
`.DS_Store`.

Host code 5434 → 3807 lines (−30%); whole repo 6626 → 4999 (−25%).

## Verified

- firmware checks: **70 pass / 0 fail**, `-Wall -Wextra` clean
- host tests: **76 pass**
- real board: firmware 2.2.0 flashed and running; brightness, gamma, dither,
  fallback ease, startle, beat, proximity, lux and idle each looked at on the
  physical lamp
- end-to-end dry run: 251 frames in 10 s; on the wire only `G` ×1, `C,0,0,0,0`
  ×1, `F` at 25 Hz, and `K` transients
- autonomous decision on the real lamp: with nobody talking to it, the agent
  chose "late night, quiet room" → `hue 22 / val 95 / wave_hz 4 / ease 6000`

## Unverified / known issues

- **The web chat's reply path was never confirmed on hardware.** `tool_choice`
  was `required`, which suppressed prose; fixed in v0.10.1.
- `say` needs the agent in the host (an API key). Without one the page says it
  only has reflexes.
- The firmware's `M`/`LID`/`L`/`U`/`C`/`R` are dead code until v2.3.
- The onboard PDM microphone has no signal, so hearing depends on the computer.
  BlackHole is not installed and the beat falls back to the microphone.
- ToF is bought, not wired.
- `tests/smoke.sh` needs a real terminal (pty).

## Run

```bash
cd ambient-light/host
./lamp.sh start
./lamp.sh status
./lamp.sh say "a little darker"
./lamp.sh stop
```

Firmware checks, no board needed:

```bash
cd ambient-light/firmware/syntax_check && ./run_checks.sh
```

## Not in the snapshot

`host/.env` (API key), `.venv/`, `state.json`, `webchat_history.json`, `logs/`,
`__pycache__/`.

## Next

1. Confirm the web chat replies, then tag `v1.0.0`
2. Firmware v2.3: remove `M`/`LID`/`L`/`U`/`C`/`R` and the preset table
3. BlackHole, so the beat is measured instead of guessed
4. Wire the VL53L0X for a real distance
5. Polish the page copy; `lamp.sh` already removes the extra terminals
