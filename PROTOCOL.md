# Serial protocol — Interactive Ambient Light v2.2

Everything between the host (the computer) and the Feather nRF52840 Sense goes
over one USB CDC serial line. The protocol is **line oriented** ASCII: one frame
per line, terminated by `\n`, fields separated by commas.

- Baud 115200 (irrelevant over USB CDC, but both sides agree)
- ASCII; the firmware ignores `\r`, so `\r\n` also works
- Line limit: **80 bytes** (firmware buffer); longer lines are dropped whole
- Unknown commands and events are ignored, never fatal, so fields can be added later

```
host   --F / K / G / P / T / X-->  Feather
Feather  --S / E / PONG-->  host
```

**What changed in v2.2.** The firmware no longer has "moods". It knows a colour
field, a spatial wave, a breathing envelope, transients and reflexes; the host
describes the look with `F` frames at 20–30 Hz. The v2.1 commands (`L`, `M`,
`LID`, `U`, `C`, `R`) are still accepted for compatibility but **the host no
longer sends them**. Design rationale in [FIRMWARE-API.md](FIRMWARE-API.md).

---

## 1. Host → lamp

### `F,<hue>,<sat>,<val>,<grad>,<wave_n>,<wave_amp>,<wave_hz>,<breath_hz>,<breath_amp>,<open>,<ease_ms>` — the look

| Field | Range | Unit | Meaning |
|---|---|---|---|
| `hue` | 0–255 | colour wheel | base colour; 0 red, 42 yellow, 85 green, 128 cyan, 150 teal, 170 blue, 213 magenta |
| `sat` | 0–255 | — | saturation |
| `val` | 0–255 | brightness | base amplitude |
| `grad` | −128…127 | hue | hue spread across the ring; 0 is one flat colour |
| `wave_n` | 0–16 | waves | travelling bands around the ring; 0 disables |
| `wave_amp` | 0–100 | % | depth of those bands |
| `wave_hz` | 0–255 | 0.01 Hz | how fast they travel |
| `breath_hz` | 0–255 | 0.01 Hz | breathing rate |
| `breath_amp` | 0–100 | % | breathing depth |
| `open` | 0–100 | % | eyelid; without a servo it is a brightness gain |
| `ease_ms` | 0–10000 | ms | time constant to reach this frame; 0 = immediately |

```
F,150,220,180,20,2,30,40,10,40,80,600
```

- Rate: **20–30 Hz**, 25 Hz recommended. A frame is 30–45 bytes, about 0.9 kB/s
- **Missing fields keep their previous value**; out-of-range values are clamped
- A frame with no valid number does not refresh the frame clock
- `F` produces **no events**; it is safe to stream continuously
- 1.5 s without a frame (and without a fresh `M`) → ease into the fallback look (§5)

### `K,<type>[,<strength>]` — transients

| type | Action | Timing |
|---|---|---|
| 0 | blink | 120 ms close → 60 ms hold → 180 ms back to the current `open` |
| 1 | wince | 100 ms down to `min(15, open)` → 1500 ms back |
| 2 | flash | white flash, `strength` 0–255, exponential decay (τ = 0.28 s, 255 when omitted) |

- A transient is an **overlay**: it does not change the target `open` and returns
  to the stream by itself
- One at a time; a new one replaces the old
- A startle (`E,STARTLE`) uses the same flash + wince internally
- `K` refreshes the link watchdog but **not** the frame clock

```
K,0
K,1
K,2,200
```

### `G,<cap>,<gamma>,<dither>` — render configuration

| Field | Range | Default | Meaning |
|---|---|---|---|
| `cap` | 0–255 | 60 | channel ceiling (replaces v2.1's `BRIGHTNESS 40`) |
| `gamma` | 0/1/2 | 0 | 0 linear / 1 = 2.2 / 2 = 2.8 |
| `dither` | 0/1 | 1 | temporal dither, only above a channel value of 8 |

`gamma` defaults to 0 on purpose: after `val`, the breathing envelope and the
openness gain, channel values often sit around 10–30, and a 2.2 curve pushes
them to 1–3, where one code step is a doubling and the lamp visibly flickers.
Gamma is an experiment, not the default.

Power at 32 LEDs full white: `cap` 40 → 0.30 A, 60 → 0.45 A, 80 → 0.60 A. Above
60 needs the external supply. Not persisted; send it once per connection.

### `L,<level>` — room noise (v2.1, still accepted)

| Field | Range | Meaning |
|---|---|---|
| `level` | 0–100 | 0 = baseline, 100 = `SPAN_DB` (18 dB by default) above it |

Used to trigger the firmware's own startle. **The host no longer sends it** — the
reflex layer on the computer owns that.

### `M,<preset>,<hue>,<speed>,<bright>,<lid>` — v2.1 preset shorthand

Kept so an old host keeps working. It maps to a table of `F` parameters and
answers `E,CMD,<preset>`. Fields: preset is `calm` / `alert` / `curious` /
`sleepy`; `bright` scales `val`; `lid` becomes `open`; `speed` is ignored.
**The host no longer sends it.**

### `LID,<open>,<ms>,<mode>[,<amp>,<period>]` — v2.1 eyelid

`mode` 0 hold, 1 breathe, 2 blink, 3 wince. Now an alias for `open` + `K`.
Deprecated; **the host no longer sends it.**

### `U,<music>,<beat>,<present>` — v2.1 music bus

Modulation only; never triggers a startle. Superseded by `C,0,0,0,0` plus the
host reflex layer.

### `C,<audio>,<beat>,<prox>,<idle>` — v2.1 coupling

The host sends exactly one of these, `C,0,0,0,0`, to tell the firmware not to add
its own coupling on top of the reflex layer. `prox` and `idle` were never read.

### `R,<startle>,<prox>,<enabled>` — v2.1 reflex thresholds

No longer sent: the startle lives in the host reflex layer now.

### `P` / `T` / `X`

- `P` — heartbeat, the firmware answers `PONG`
- `T` — red/green/blue/white self-test, **blocks ~1.2 s**. Do not use while
  streaming; about 14 kB arrives during the block and frames are lost
- `X` — drop the host look, back to the fallback

---

## 2. Lamp → host

### `S,<prox>,<als>,<lux>,<energy>,<origin>,<open>,<flags>` — telemetry

| Field | Range | Meaning |
|---|---|---|
| `prox` | 0–255 | APDS9960 proximity; a hand close by is 150+ |
| `als` | 0–65535 | raw ambient light (clear channel) |
| `lux` | 0–65535 | estimated illuminance |
| `energy` | 0–100 | smoothed noise level as the firmware sees it |
| `origin` | `host` / `fallback` | who is driving the look |
| `open` | 0–100 | current openness |
| `flags` | 0–7 | bit0 startle active, bit1 fallback dozing, bit2 frame timeout |

- Rate 4 Hz (`REPORT_MS = 250`)
- A v2.1 host parses with `len(parts) >= 6` and simply receives `"host"` or
  `"fallback"` where it used to see a mood name; `flags` is appended at the end

```
S,12,345,8,43,host,70,0
```

### `E,<event>[,<detail>]` — events

| Event | Detail | Meaning |
|---|---|---|
| `BOOT` | firmware version | e.g. `E,BOOT,2.2.0` |
| `CMD` | preset | a legacy `M` was accepted |
| `STARTLE` | — | the firmware's own startle (only when it receives `L`) |
| `WAKE` | `prox` | fallback mode, dozing, woken by a hand |
| `FRAME_LOST` | — | 1.5 s without `F`/`M`, easing into the fallback |
| `FRAME_OK` | — | the host took over again (once, after a loss) |
| `LINK_LOST` | — | 12 s without any bytes |
| `LINK_OK` | — | link recovered |
| `SENSOR_FAIL` | `apds9960` | I2C init failed; telemetry stays 0 |
| `RESET` | — | `X` received |

`E,MOOD` no longer exists.

---

## 3. A typical session

```
lamp:  E,BOOT,2.2.0
host:  G,80,0,1
host:  C,0,0,0,0
host:  F,30,200,120,28,1,30,8,12,55,55,3000
lamp:  S,8,120,6,4,host,55,0
host:  F,30,200,186,34,1,41,12,14,62,58,120      (music started)
host:  K,0                                        (the agent blinks)
...
(host stops)
lamp:  E,FRAME_LOST
lamp:  S,6,90,3,2,fallback,55,4
(45 s later)
lamp:  S,6,90,3,1,fallback,30,2
host:  F,...
lamp:  E,FRAME_OK
```

---

## 4. Who owns the picture

v2.1 had a priority ladder (STARTLE > PROX > HOST > AUTONOMY) where one winner
took everything. v2.2 **composes layers**:

| Layer | When |
|---|---|
| fallback look | always the baseline |
| host `F` | a frame arrived within 1.5 s, overriding the baseline |
| transients (`K`) | after a trigger, until they finish |
| startle | in the host reflex layer, sent as `K,2` + `K,1` |

They do not fight, so "the host is streaming while a startle happens" composes
instead of one cancelling the other.

The fallback doze is not a state machine: it is two curves over the quiet time
(`val` and `open` each drop up to 45%), so the behaviour is continuous.

---

## 5. Link and failure handling

| Situation | Behaviour |
|---|---|
| 1.5 s without `F`, and no fresh `M` | `E,FRAME_LOST`, 3 s ease into the fallback |
| host sends again | `E,FRAME_OK`, once |
| 12 s without any byte | `E,LINK_LOST`, host considered gone |
| 180 s without `M` | the legacy preset expires (only affects `M`-only hosts) |
| USB unplugged | the host retries every 2 s; the lamp keeps running its fallback |
| line too long / not a number | dropped, no crash |
| unknown event | the host only logs it |

The fallback **eases out**, it does not freeze: killing the host with `Ctrl-C`
makes the lamp relax into amber breathing instead of hanging half-lit.

---

## 6. Bandwidth

| Direction | Rate | Bytes/frame | Total |
|---|---|---|---|
| `F` | 25 Hz | 30–45 | ~0.9 kB/s |
| `S` | 4 Hz | ~32 | ~130 B/s |
| `K` / `G` | events | 10–40 | negligible |

Around 1.1 kB/s total, far below the ~11.5 kB/s of 115200 baud. The only thing
that can overrun the line is `T`, which blocks for 1.2 s.

---

## 7. Version

- Current firmware: `2.2.0`
- Compatibility: a v2.1 host that only sends `L` and `M` still works
- `S` field 5 changed from a mood name to `origin`; the host knows its own mood
  anyway, so it does not need to read it back
- New fields are only ever appended at the end; hosts parse with `len(parts) >=`
