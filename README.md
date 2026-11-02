# Interactive Ambient Light

A paper ambient lamp with 32 NeoPixels that breathes, gets startled, notices you
and gets sleepy. The interesting part is where the thinking happens: the lamp's
body is firmware, its nervous system is a reflex layer on the computer, and its
mind is a DeepSeek agent that is allowed to be offline.

**Two layers, in this order:**

> The reflex lives on the computer (25 Hz, no network). The thinking is one
> agent (optional).

```
  you type ──► agent.py ──┐
                          ├──► base_look  (eleven numbers: a target, not a frame)
  lampctl light ──────────┘                 │
                                            ▼
                            LookEngine, 25 Hz  ◄── reflex.py reads the sensors
                                            │     each frame: ease the target,
                                            │     bend it with the reflex, format
                                            ▼
                                  F frames / K transients  (serial)
                                            ▼
                                  firmware 2.2.0, 50 fps
```

Read it as four things, not three:

- **agent.py** and **lampctl** both write the same target (`base_look`). The agent
  owns it; `lampctl light` is a debug writer.
- **reflex.py** is not a thread and never sends a frame. It is a function the
  engine calls once per frame, which returns the bent frame plus any transient
  commands.
- **LookEngine** (`looks.py`) is the only code that writes to the serial port
  after boot: it eases the target, applies the drift, runs the reflex, and sends
  one `F` frame — plus `G` and `C,0,0,0,0` once at start.
- **firmware** is the only thing that turns numbers into light.

- **No agent, still alive.** No key, no network or `--no-llm`: the reflex keeps
  running and the lamp keeps answering sound and hands.
- **No host, still alive.** If the host dies the firmware eases into its own
  amber breathing 1.5 s later.
- **One mind.** The web page owns no model; it forwards what you type to the one
  agent in the host.

## Layout

| Path | What it is |
|---|---|
| [firmware/ambient_light/ambient_light.ino](firmware/ambient_light/ambient_light.ino) | Firmware: F-frame renderer, transients, fallback look, serial |
| [firmware/syntax_check/run_checks.sh](firmware/syntax_check/run_checks.sh) | Syntax and behaviour checks without the Arduino toolchain |
| [host/ambient_host.py](host/ambient_host.py) | Host main: fast loop / music / light stream / agent |
| [host/agent.py](host/agent.py) | The one mind: DeepSeek tools, drives, memory |
| [host/looks.py](host/looks.py) | `Look` (eleven numbers) and the 25 Hz frame engine |
| [host/reflex.py](host/reflex.py) | Sensor reflex layer — no network, never offline |
| [host/control.py](host/control.py) | Local socket: `light` / `blink` / `say` / `observe` |
| [host/lampctl.py](host/lampctl.py) | CLI: change the light, talk to it, read state |
| [host/webchat.py](host/webchat.py) | Local page: status and chat (owns no model) |
| [host/sensors.py](host/sensors.py) | Beat detection, BlackHole audio, macOS context |
| [host/audio_meter.py](host/audio_meter.py) | Microphone → calibration → 0–100 level |
| [host/feather_link.py](host/feather_link.py) | Serial protocol, telemetry, reconnect |
| [host/sim_feather.py](host/sim_feather.py) | Fake Feather on a pty, for testing without hardware |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Hardware and software as it runs now: processes, timing, ownership |
| [PROTOCOL.md](PROTOCOL.md) | The serial protocol, v2.2 |
| [FIRMWARE-API.md](FIRMWARE-API.md) | The firmware contract: frames, render math, timing |

## Hardware

### The strip

| Strip | Connect to |
|---|---|
| `5V` | External 5 V / 2 A supply (**do not** power 32 LEDs from the Feather) |
| `GND` | External supply GND **and** Feather GND (common ground) |
| `DIN` | Feather `D6`, through 330 Ω |
| `5V`/`GND` | 1000 µF electrolytic near the strip |

32 LEDs at full white draw about 1.9 A. The firmware caps the channel value
(80/255 by default, about 0.6 A), so USB power survives, but an external supply
is steadier. Driving 5 V data from 3.3 V is marginal — add a 74AHCT125 if you
see flicker.

### The shade

- 10 cm ring, tracing-paper cylinder, base. Add a diffuser layer above the LEDs
  (milk acrylic or two sheets of paper) or you will see 32 hot spots.
- Openness is rendered as **brightness modulation** (`open` 0–100 maps to
  0.02–1.0), so blinking and squinting work without mechanics.
- An SG90 is reserved: set `ENABLE_SERVO 1` and the same `open` parameter drives
  the servo. No protocol change.

### Sensors

- APDS9960 is onboard (I2C, no wiring). If you see `E,SENSOR_FAIL` at boot,
  check that the board is the Sense variant.
- A VL53L0X (ToF) is bought but **not wired**.
- Servo pin `D5`, disabled by default; give it its own 5 V supply.

## Flash the firmware

1. Arduino IDE → install the **Adafruit nRF52** board package.
2. Library manager → **Adafruit NeoPixel**, **Adafruit APDS9960**.
3. Board → *Adafruit Feather nRF52840 Sense*, open the `.ino`, upload.
4. Serial monitor at 115200, line ending **New Line**. You can type:

```
T                         # red/green/blue/white self-test (blocks 1.2 s)
P                         # expect PONG
G,80,1,1                  # channel ceiling 80, gamma 2.2, dither on
F,150,220,180,20,2,30,40,10,40,80,600   # a look, as eleven numbers
K,0                       # blink
K,2,255                   # white flash
```

A desktop serial monitor cannot send a stream; the lamp will just ease toward
whatever single frame you typed. Use the host for real driving.

After changing the firmware, run the checks on the computer (no board needed):

```bash
cd firmware/syntax_check
./run_checks.sh
```

It does a `-Wall -Wextra` syntax check and drives `setup()/loop()` with a fake
clock and a fake serial port: startle, decay, fallback, frame timeout, dither,
gamma, cap, transients, coupling disabled, 70 checks.

### After flashing, look at it by hand

```bash
cd host && source .venv/bin/activate
python3 ambient_host.py --port auto --span-db 30     # in one terminal
python3 lampctl.py observe                           # in another
python3 lampctl.py light --val 200 --grad 35 --wave-hz 20
python3 lampctl.py light --val 90 --ease 3000        # glide down slowly
python3 lampctl.py blink
```

## Install the host

```bash
cd host
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and put your DEEPSEEK_API_KEY in it
```

The `openai` package talks to DeepSeek's OpenAI-compatible endpoint. Without a
key (or without the package) the agent is simply off and the lamp runs on
reflexes.

### Music (optional, recommended)

```bash
brew install blackhole-2ch
sudo killall coreaudiod
# Audio MIDI Setup → + → Create Multi-Output Device
#   tick "MacBook Speakers" + "BlackHole 2ch", both at 48 kHz
# System Settings → Sound → Output → Multi-Output Device
python3 ambient_host.py --list-devices   # BlackHole should appear
```

Without BlackHole the beat falls back to the microphone, which works for
clapping but cannot hear a kick drum a laptop speaker never reproduced.

## Run it

**One command for everything** (host + page, logs in `host/logs/`):

```bash
cd host
./lamp.sh start
#   host      started (pid 12345)  agent: DeepSeek
#   page      http://127.0.0.1:8765

./lamp.sh status     # what is running, and what the light is being fed
./lamp.sh logs       # follow both logs
./lamp.sh say "a little darker"
./lamp.sh stop
```

Closing the terminal does not stop it (`nohup`); use `./lamp.sh stop`.

By hand:

```bash
cd host
source .venv/bin/activate
set -a; source .env; set +a          # without this there is no agent
python3 ambient_host.py --port auto --span-db 30
```

The first log line says which layer is in charge:

```
reflex on | stream 25 Hz cap 80 | agent deepseek-flash | slow 3..10 s | port auto | span 30.0 dB
```

A status line every 10 s:

```
22:57:23  lvl   8 ..........  -54.4 dBFS  hue= 30 val= 90 open= 55  prox=  0  lux=    0  mus=  5  link=up  stream=lamp  llm=3/60  812ms
```

### Three degradations, all still alive

| Situation | Log says | Lamp |
|---|---|---|
| Key and network | `agent deepseek-flash` | agent writes the look, reflex reacts |
| No key / offline / `--no-llm` | `agent off` | reflex only: it still breathes, follows sound and hands |
| Host died | firmware `FRAME_LOST` after 1.5 s | its own amber breathing |

```bash
python3 ambient_host.py --port auto --no-llm      # reflexes only
```

### Without hardware

```bash
python3 sim_feather.py --pty
# PTY=/dev/ttys012
python3 ambient_host.py --port /dev/ttys012 --no-llm --sim-audio
```

### Dry run

```bash
python3 ambient_host.py --no-llm --sim-audio --dry-run --duration 10
```

Prints `[dry] G,...`, `[dry] C,0,0,0,0`, `[dry] F,...`, `[dry] K,...`.

### Options

| Option | Default | Effect |
|---|---|---|
| `--no-llm` | off | no agent, reflexes only |
| `--model` | `deepseek-flash` | DeepSeek model |
| `--api-base` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `--llm-min-interval` | 25 | minimum seconds between autonomous turns |
| `--llm-heartbeat` | 120 | how often it thinks when nothing changes |
| `--max-calls-per-hour` | 60 | hard sliding-window cap |
| `--llm-timeout` | 8 | per-call timeout (s) |
| `--look-hz` | 25 | parameter stream rate |
| `--cap` | 80 | channel ceiling sent via `G` |
| `--gamma` | 0 | 0 linear / 1 = 2.2 / 2 = 2.8 |
| `--no-reflex` | off | disable the sensor reflex layer |
| `--span-db` | 18 | dB over baseline that counts as 100 (use 30 for music) |
| `--calibrate` | 3 | baseline calibration seconds |
| `--sim-audio` | off | synthetic audio, no microphone permission |
| `--no-music` | off | do not open BlackHole |
| `--dry-run` | off | print serial lines instead of writing them |
| `--state-file` | `state.json` | drives and memory |
| `--control-socket` | `/tmp/ambient-lamp.sock` | local control socket |
| `--list-devices` | — | list audio devices and serial ports |

## Control it from another process

The host opens a Unix socket at `/tmp/ambient-lamp.sock`. Leave the host running
and use another terminal:

```bash
cd host
python3 lampctl.py observe                              # what is being fed to the lamp
python3 lampctl.py light --val 90 --ease 2500           # change one or two numbers
python3 lampctl.py light --grad 30 --wave-amp 40 --wave-hz 14
python3 lampctl.py blink
python3 lampctl.py say "a little darker, slower"         # talk to the same agent
```

`light` writes the same eleven numbers the agent writes, so it is a **debug
tool**, not a second control logic: the agent sees `lamp.light` on its next turn
and can keep going from there. `--json` prints the raw response.

## The web page

A window, not a second brain. It forwards what you type to the one agent and
shows the answer. Close the page and the lamp keeps running; restart it and the
lamp's personality does not change.

```bash
cd host && source .venv/bin/activate
python3 webchat.py --port 8765 --open
# http://127.0.0.1:8765
```

- chat = `say` on the host, so the host needs a key to answer; without one it
  says "I only have reflexes right now"
- the four buttons write parameters, they do not switch modes: dimmer /
  brighter / more flow / blink
- status refreshes every 2 s; history goes to `webchat_history.json`
- binds `127.0.0.1` only — do not expose it

## What the agent decides

Each turn it receives a JSON `situation`: noise mean/peak/trend, music energy,
beat and track, whether someone is at the computer, idle seconds, room lux, **the
eleven numbers it last wrote**, four drives, the last six memories and events.
When you speak, the situation gains a `said` field and that turn must answer.

| Tool | What it does |
|---|---|
| `set_light` | writes the eleven numbers: hue / sat / val / grad / wave_n / wave_amp / wave_hz / breath_hz / breath_amp / open / ease. No enums; omitted fields keep their value |
| `blink` | blink or wince — a transient, not a new appearance |
| `set_reflex` | tune its own body: how easily a sound startles it, how much a hand matters, how much it dims when you leave, how much music shows |
| `feel` | nudge drives (energy / curiosity / security / bond), deltas only |
| `remember` | store something worth remembering tomorrow |
| `observe` | read sensors, macOS context, music — at most once per turn |

**It is not choosing from a menu.** `set_light` takes eleven continuous values,
so "a little darker", "make it flow" and "too harsh" are each one or two
numbers. The reflex layer then rides on top: the agent decides *what it looks
like*, the reflex decides *how it answers the room*.

## The reflex layer

[reflex.py](host/reflex.py) is one call per frame inside the 25 Hz engine. It has
no thread of its own, never sends a frame, and never touches the network — which
is why it survives a dead API.

| Sensor | Changes | Feels like |
|---|---|---|
| Room mic / system audio | breath depth (≈60% quiet → ≈150% loud), a little brightness, breath *speed* | a quiet room breathes deeply; music makes it move |
| Beat | a ~200 ms pulse on `val`, plus a small lift of `open` | it moves with the kick |
| Proximity | `open`, brightness, hue toward warm | it notices a hand |
| Room light | a brightness floor | brighter by day, dimmer at night |
| Idle time | slowly dims (up to 35%) | it gets sleepy when you leave |
| A sudden level jump | `K,2,255` + `K,1` | a clap makes it flash and recoil |

Design rules:

- **Additive, not authoritative.** The reflex bends the agent's base, never
  replaces it, so "who owns the lamp" stays answerable.
- **The board's own coupling is off.** The stream sends `C,0,0,0,0` once, or the
  firmware would add a second layer of music gain.
- **A startle needs an edge**: 10 level units in one frame, plus 26 over a slow
  average, plus 45 absolute, plus a 2 s cooldown. A crowd getting louder does not
  startle it.
- **Music never startles**: the startle reads the microphone only.
- **Loudness is `max(room, music)`, not a sum**, so a loud room playing loud
  music is not counted twice.

All the numbers live in `ReflexConfig`.

## Protocol

Full spec in [PROTOCOL.md](PROTOCOL.md). The short version:

```
host → lamp   F,150,220,180,20,2,30,40,10,40,80,600   25 Hz, the look
host → lamp   K,0 / K,1 / K,2,255                     blink / wince / flash
host → lamp   G,80,1,1                                cap / gamma / dither
host → lamp   C,0,0,0,0                               once: do not add your own coupling
lamp → host   S,12,345,8,43,host,70,0                 telemetry: prox, als, lux, energy, origin, open, flags
lamp → host   E,FRAME_LOST / E,FRAME_OK / E,STARTLE / E,WAKE
```

The firmware still accepts the v2.1 commands (`L`, `M`, `LID`, `U`, `C`, `R`).
The host no longer sends any of them; they are kept until firmware v2.3.

## Cost

DeepSeek `deepseek-flash`: about $0.15 / $0.30 per million input tokens
(off-peak/peak) and $0.60 / $1.20 per million output. A turn is roughly
2–3k input + 200 output, so about **$0.0005–0.001**. At the default 60 calls per
hour that is about **$0.03/hour**.

## Troubleshooting

| Symptom | Look at | Do |
|---|---|---|
| Lamp completely dark | send `T` | self-test works → check 5 V, ground, data pin; nothing → check power and upload |
| Wrong colours, flicker | data line | 330 Ω series, level shifter, shorter wire, common ground |
| No serial port | `--list-devices` | close the Arduino serial monitor, double-tap reset |
| Microphone will not open | System Settings → Privacy → Microphone | give the terminal permission; test with `--sim-audio` |
| Baseline looks wrong | `baseline … dBFS` at startup | restart in a quiet room (a loud baseline makes it deaf) |
| Lamp does not follow sound | status line `lvl` | flat → no audio; moving but the lamp is still → `link=--` |
| Lamp ignores music | `lampctl observe`, `senses level/music` | `level` under 10 → the baseline was calibrated with music playing; restart quietly |
| Level pinned at 100 | log warning | raise `--span-db` (18 → 30 for music) |
| DeepSeek 401/404 | key and model name | `echo $DEEPSEEK_API_KEY`; `--model deepseek-flash` |
| Always "holding state" | log `skip` / `error` | hourly cap or circuit open; adjust `--max-calls-per-hour` |
| No replies in the page | `host/logs/host.log`, the `mind ->` line | no key, or the turn produced neither words nor a tool call |
| Lamp does not sleep | `lampctl observe` → `light.val` | the agent keeps it bright; say "darker" or `lampctl light --val 70` |
| Serial does not recover | log | the host retries every 2 s; the lamp goes autonomous after 1.5 s, by design |

## Demo script (~3 minutes)

1. Start in a quiet room: amber breathing, slowly rotating band.
2. Clap: the firmware flashes and winces; within a second the agent decides
   something and the log shows why.
3. Put a hand near: `open` and brightness rise, the hue warms.
4. Play music: breath deepens and speeds up, the beat lifts the eye and the
   brightness.
5. Walk away for a minute: it dims, drives drift, it remembers something.
6. Come back: it opens up again.
7. Kill the host: `FRAME_LOST` after 1.5 s, the lamp eases into its own amber
   breathing. Start the host again: `FRAME_OK`.

## Known limits

- The onboard PDM microphone has no signal, so hearing depends on the computer.
  The laptop sleeping means the lamp goes deaf.
- Only numbers leave the computer (level, trend, proximity, lux, track title).
  No audio is ever sent.
- BlackHole is not installed yet: the beat falls back to the microphone, and a
  laptop speaker never reproduces the kick drum it would need.
- ToF is bought but not wired; proximity is still the APDS9960's rough value.
- Firmware v2.3 (removing `M`/`LID`/`L`/`U`/`C`/`R` and the preset table) needs a
  reflash and has not been done.

## Next

1. Confirm the web chat replies on the real lamp, then tag `v1.0.0`.
2. Firmware v2.3: delete the legacy commands and the preset table.
3. BlackHole, so the beat is measured instead of guessed.
4. Wire the VL53L0X and give proximity a real distance.
5. Multi-lamp: broadcast `F`/`K` to several ports.
