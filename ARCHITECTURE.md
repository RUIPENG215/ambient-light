# Architecture

What the thing is made of, what runs where, and what happens between a sound in
the room and a change on the LEDs. Everything here describes the code as it is
in this snapshot; the wire format is in [PROTOCOL.md](PROTOCOL.md) and the
firmware's internal model in [FIRMWARE-API.md](FIRMWARE-API.md).

## In one paragraph

A paper lamp with 32 NeoPixels. A Feather nRF52840 Sense renders whatever fourteen
numbers it is told, at 50 fps, and keeps its own amber fallback. A Python host on
a Mac turns sensors into those fourteen numbers 25 times a second (the **reflex**),
and a single DeepSeek agent occasionally rewrites the base they are derived from
(the **mind**). The agent is optional; the reflex is not.

## Hardware

| Part | Detail | Notes |
|---|---|---|
| MCU | Adafruit Feather nRF52840 Sense — Cortex-M4F, 1 MB flash, 256 KB RAM | 82,640 B flash / 9,340 B RAM used |
| LEDs | 32 × WS2812B, 800 kHz, GRB, data on **D6** | through 330 Ω |
| Power | external **5 V / 2 A** (3 A once servos are enabled), common ground with the Feather | 32 LEDs ≈ 0.9 A capped at 120; SG90 stall ≈ 700 mA each |
| Buffer | 1000 µF across 5 V/GND at the strip and at the servos | |
| Sensor | **APDS9960**, onboard, I2C | proximity + RGB/ALS |
| Microphone | PDM, onboard | **no signal on this board** — hearing is done by the Mac |
| Blades | 3 × 180° positional servos inside the ring, shafts radial 120° apart, on **D11/D9/D10** | **parked: `ENABLE_SERVO 0`**; 360° servos cannot hold an angle. When enabled: ~18° arc, per-servo end points, `B` command, `E,SERVO_OK/FAIL` at boot |
| Range | VL53L0X (ToF) bought | **not wired** |
| Enclosure | 10 cm ring, tracing paper cylinder, base, diffuser layer | without the diffuser you see 32 hot spots |

```
   Mac ──USB CDC──► Feather nRF52840 Sense ──D6 (330 Ω)──► 32 × WS2812B
                        │  I2C                                │ 5 V / GND
                        ├── APDS9960 (prox + lux)             ├── 5 V 3 A + 1000 µF
                        └── D11/D9/D10 ──► 3 × SG90 blades   └── 5 V (servo rail)
```

The USB cable is the only connection. The board never sees audio or the network.

## Runtime map

Two processes, one wire.

```
┌─ ambient_host.py (one process, five threads + a socket) ─────────────────┐
│                                                                          │
│  fast    20 Hz   AudioMeter.level  ──────────────► state.level           │
│  music   20 Hz   music_bus() (BlackHole or mic) ──► state.music/beat     │
│  look    25 Hz   LookEngine: base + effect + reflex ──► "F" / "K" ──┐    │
│  mind    3–10 s  one agent turn ──► base_look                    │      │
│  control         unix socket /tmp/ambient-lamp.sock               │      │
└───────────────────────────────────────────────────────────────────┼──────┘
                                                                    │ USB
┌─ webchat.py (separate process) ──────────────┐                    ▼
│  http://127.0.0.1:8765 → the same socket     │            firmware 2.2.2
│  owns no model                               │            50 fps render
└──────────────────────────────────────────────┘                    │
                                                                    ▼
                                                        32 LEDs + 3 blades
```

Only **one** thread writes to the serial port: the `look` thread. Everything else
talks to it through `state` or through the control socket.

## The five pieces

### 1. `agent.py` — the mind

- One DeepSeek client, one set of tools, one memory. Rate-limited to 60 calls an
  hour, a 25 s minimum between autonomous turns, a 120 s heartbeat, and a circuit
  breaker after three consecutive failures.
- It is **not** in the 25 Hz path. A turn takes 800–1600 ms and ends by writing a
  *target*, then it leaves.
- Tools: `set_light` (fourteen continuous values, no enums), `blink`,
  `set_reflex` (tune the reflex's own constants), `feel`, `remember`, `observe`.
- When the situation carries a `said` field it must also answer in words, in the
  language the person used.
- Persists `state.json`: four drives (energy, curiosity, security, bond) plus
  memories.

### 2. `looks.py` — the only thing that writes frames

- `Look` is fourteen floats with clamping and optional drift (`hue_drift`, …).
  The last three (`hue_cycles`, `shape`, `sparkle`) are the v2.2.2 basis: full
  hue turns, a sharpened wave, and random white glints.
- `LookEngine` is a thread at 25 Hz. Each tick:
  1. ease the current value toward the target, with the target's `ease_ms`
     (same curve as the firmware: τ = ease_ms / 3000 s);
  2. apply the look's own drift;
  3. apply the active **effect** (`effects.py`: candle / heartbeat / ocean /
     aurora), a temporary base with its own fade in/out;
  4. call `reflex.frame(...)` once — this is where sensors enter;
  5. format one `F` line and send it; send any `K` transients after it.
- On start it sends `G,<cap>,<gamma>,<dither>` and `C,0,0,0,0` exactly once.
- If a serial write fails it counts nothing and keeps going; the firmware's frame
  watchdog handles the outage.

### 3. `effects.py` — the performance layer

Four named functions of time over the same 14 numbers. An effect is started by
`lampctl effect`, the web page or the agent (`play_effect`), replaces the base
for its duration, and leaves nothing behind. The reflex still rides on top
because the effect is applied before it. The agent sees `lamp.effect` in its
situation, so it knows what is already playing.

### 4. `reflex.py` — sensors into light

Not a thread and never a sender: a pure-ish transform called once per frame.

| Input | Effect | Constant |
|---|---|---|
| `max(room mic, system audio)` | breath depth ≈0.6×…1.5×, breath *rate* 0.5×…1.5×, `val` +0–55 | `audio_breath`, `audio_val`, `audio_tempo` |
| `beat` | a decaying pulse on `val` (+0–34) and a small lift of `open` (+6) | `beat_val`, `beat_decay_s`, `beat_open` |
| `prox` | `open` +24, `val` +18, hue −10 (warmer), smoothed τ 0.25 s | `prox_open/val/hue` |
| `lux` | brightness floor ×0.72…1.10, smoothed τ 2 s | `lux_floor`, `lux_gain` |
| `idle_s` | up to −55% `val`, −27.5% `open` from 45 s to 4 min | `idle_dim`, `idle_after_s` |
| level edge | `K,2,255` + `K,1` once, 2 s cooldown | `startle_jump`, `startle_step` |

Order matters: additions first (sound, beat, proximity), then multiplications
(lux, idle), so leaving the desk dims the beat as well. Every smoothing is
`1 − exp(−dt/τ)`, so a slow frame does not change the feel.

### 5. `firmware` — the body

- Renders the frame it holds at 50 fps, interpolating with τ = `ease_ms`/3000.
- Plays transients on their own timeline: a blink is 120/60/180 ms, which is
  3 frames at 25 Hz and cannot be authored from the host.
- Keeps a fallback look and a slow doze; 1.5 s without a frame eases into it.
- Reports telemetry at 4 Hz and owns the link watchdog.
- Knows colour field, spatial wave, envelope, transients. Does not know moods.
- With `ENABLE_SERVO 1` (off by default): turns `open` into three servo pulses
  on its own 50 Hz clock, so a `K` blink reaches the blades in 120 ms even though
  host frames only arrive every 40 ms. The blades occlude; the LEDs are not
  dimmed as well (`SERVO_DIM_LIGHT 0`).

## End to end

### You clap

```
air → Mac mic → AudioMeter (attack τ 0.12 s) → state.level (20 Hz)
     → next engine tick (≤40 ms) → reflex sees a 10+ step over a 26+ rise
     → sends "K,2,255" and "K,1" on the same tick
     → firmware sets flash = 1.0 and starts the wince timeline
     → next render frame (≤20 ms) is white, the eye closes
     → 280 ms exponential decay back into whatever the stream says
```

≈200–240 ms from the clap to the flash. The board's own startle used to be
faster (~140 ms) because it skipped two hops; it is now unused and the host owns
the reflex, which was the point of moving it.

### Music plays

```
BlackHole (or mic fallback) → spectral flux → state.beat / state.music (20 Hz)
  → reflex beat envelope rises to the onset, decays with τ 0.18 s
  → every F frame carries the raised val; louder passages also deepen and speed
    the breath and widen the gradient
  → firmware eases to it (τ 33 ms) and draws
```

The kick is felt on the frame after the onset: ≈40 ms engine + ≈33 ms easing.

### You type

```
browser → webchat POST /api/chat → control socket {"cmd":"say"} → Host.say()
  → one agent turn with `said` in the situation (tool_choice becomes "auto")
  → the turn may write set_light and/or answer in prose
  → the reply goes back along the same path; the light change is already in
    base_look, so the next frame carries it
```

The chat waits up to 25 s. While it waits, the 25 Hz stream never stops.

## Timing budget

| Stage | Rate / constant | Note |
|---|---|---|
| firmware render | 20 ms | 50 fps |
| firmware interpolation | τ = ease_ms/3000 | 100 ms → τ 33 ms |
| host frame | 40 ms | 25 Hz |
| host easing | τ = ease_ms/3000 | done in `looks.py` |
| microphone attack | 120 ms | `AudioMeter.attack_tau` |
| fast loop | 50 ms | 20 Hz |
| telemetry | 250 ms | 4 Hz, firmware → host |
| frame timeout | 1500 ms | host considered quiet |
| link timeout | 12 000 ms | host considered dead |
| agent turn | 800–1600 ms | 25 s minimum interval |

The two biggest contributors to perceived latency are the microphone attack and
the frame rate, not the network.

## Who owns what

| Thing | Owner | Written by |
|---|---|---|
| `state.base_look` | the agent | `set_light`, `lampctl light` as a debug writer, or the quiet-rest floor |
| the frame on the wire | `LookEngine` | nobody else |
| the active effect | `EffectPlayer` | `lampctl effect`, the page, or the agent's `play_effect`; it expires by itself and never persists |
| `state.json` (drives, memory) | the agent | `feel` / `remember` |
| the look when the stream stops | firmware | its own fallback constants |
| reflex constants | the agent at runtime | `set_reflex`, or `ReflexConfig` in code |

Three writers into one target, one framer, one renderer. The third writer is
deliberately minimal: after `--rest-after` seconds (default 180) without a
sound, a beat, a hand or a light change, `Host.mind_loop` writes
`DEFAULT_LOOK` again so the lamp cannot stay stuck on a colour whose moment has
passed. It is a floor under the agent, not a second mind.

## On the wire

| Direction | Frame | Rate | Bytes |
|---|---|---|---|
| host → lamp | `F,…` | 25 Hz | ~58 |
| host → lamp | `K,…` | on events | 4–8 |
| host → lamp | `G,…` and `C,0,0,0,0` | once per engine start | ~20 |
| lamp → host | `S,…` | 4 Hz | ~25 |
| lamp → host | `E,…` | on events | ~10 |

About 1.6 kB/s, against ~11.5 kB/s of headroom. The only thing that can overflow
the line is `T` (self-test), which blocks the loop for 1.2 s.

## When something is missing

| Missing | The lamp | Log |
|---|---|---|
| no API key / no network / `--no-llm` | reflex only; still breathes, follows sound and hands | `agent off` |
| microphone permission | level stays 0; beat still comes from system audio if present | meter never becomes ready |
| BlackHole | beat falls back to the microphone; the kick is weak | `music mic only` |
| the host process | `FRAME_LOST` after 1.5 s, amber breathing, wakes on a hand | — |
| USB unplugged | host retries every 2 s; the lamp runs autonomous | `link=--` |
| the agent (rate cap, circuit open) | the last target stays; the reflex keeps bending it | `holding (…)` |
| the reflex (`--no-reflex`) | the agent's numbers go out untouched | `reflex off` |

## Deliberately not in the system

- No modes. No enum anywhere between "what the sensor says" and "what the LED
  does"; the only enumeration left is the firmware's legacy `M` preset table,
  which the host no longer uses.
- No audio leaves the computer, only numbers.
- No second mind: the web page owns no model and the host has no fallback policy
  beyond the reflex.
- No pixels on the wire: fourteen numbers per frame, not 96 bytes of RGB, because
  a dropped parameter frame is absorbed by the interpolation. Sparkle is the one
  random thing, and it runs on the board.

## Size

```
firmware     1000 lines     88 self-checks
host          4293 lines    98 unit tests
host tests    1227 lines
docs          4 documents + this one
```
