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
                          ├──► base_look  (fourteen numbers: a target, not a frame)
  lampctl light ──────────┘                 │
                                            ▼
                            LookEngine, 25 Hz  ◄── reflex.py reads the sensors
                                            │     each frame: ease the target,
                                            │     bend it with the reflex, format
                                            ▼
                                  F frames / K transients  (serial)
                                            ▼
                                  firmware 2.2.2, 50 fps
```

Read it as five things, not three:

- **agent.py** and **lampctl** both write the same target (`base_look`). The agent
  owns it; `lampctl light` is a debug writer.
- **effects.py** is a performance layer: a named function of time that replaces
  the base for a few seconds, fades out, and leaves nothing behind.
- **reflex.py** is not a thread and never sends a frame. It is a function the
  engine calls once per frame, which returns the bent frame plus any transient
  commands.
- **LookEngine** (`looks.py`) is the only code that writes to the serial port
  after boot: it eases the target, applies the drift and the effect, runs the
  reflex, and sends one `F` frame — plus `G` and `C,0,0,0,0` once at start.
- **firmware** is the only thing that turns numbers into light.

- **No agent, still alive.** No key, no network or `--no-llm`: the reflex keeps
  running and the lamp keeps answering sound and hands.
- **No host, still alive.** If the host dies the firmware eases into its own
  amber breathing 1.5 s later.
- **One mind.** The web page owns no model; it forwards what you type to the one
  agent in the host.

## The app

Double-click **Ambient Lamp.app** (in this folder). It starts everything in one
process and puts the lamp's face — a small glowing ring that mirrors the paper
lamp — in the bottom-right corner of the screen, always on top.

- What the lamp says appears as a speech bubble next to the ring.
- Click the ring to open the panel: **Talk** (chat), **Colour** (mix two
  colours, rainbow, flow, stars; save a look under a name), **Play** (try its
  gestures and performances), **Setup** (the DeepSeek key, what it senses).
  `–` sends it back to the corner, `×` quits and stops the lamp host.
- The app is a thin launcher around this folder: it asks Terminal to run
  `host/run_app.command` (which runs `host/lamp_app.py` with `host/.venv`), so
  a small Terminal window stays open while the lamp runs. An unsigned app is
  not allowed to read `~/Documents` or use the microphone by itself; Terminal
  already is. Keep the app inside the project folder. The corner window needs
  `pywebview` (`.venv/bin/python3 -m pip install pywebview`); without it the
  page opens in a browser.
- State, chat history and logs stay in `host/` (the launcher's own log is
  `host/logs/app.log`), the same files the command-line way uses.
- From a terminal the same thing is `cd host && ./lamp.sh app`.

### What makes it an agent rather than a remote control

- **It hears intent.** "我有点困了" is not a brightness command; it decides what a
  sleepy person needs and says so. The recent conversation is passed along, so
  it can refer to what was said before.
- **It has body language.** `nod`, `shake`, `happy`, `shy`, `sulk`, `surprise`,
  `wave` are one-to-two-second movements of the ring ([effects.py](host/effects.py),
  `GESTURES`). While the model is thinking, a band circles the ring (`think`).
- **It always answers in words** when spoken to (`say`), and speaks first only
  when something happened: switched on, you came back, music started, it got
  late, it was ignored for 45 minutes.
- **It remembers.** Facts (`remember`), looks you named (`save_look` /
  `recall_look`, shared with the Colour tab), reminders (`remind`).
- **It has moods** that last hours, not minutes: the drives settle toward a
  resting value over about two hours and being talked to raises `bond`.

## Layout

| Path | What it is |
|---|---|
| [firmware/ambient_light/ambient_light.ino](firmware/ambient_light/ambient_light.ino) | Firmware: F-frame renderer, transients, blades, fallback look, serial |
| [firmware/servo_test/servo_test.ino](firmware/servo_test/servo_test.ino) | Minimal one-servo test: no LEDs, no sensor, no host |
| [firmware/syntax_check/run_checks.sh](firmware/syntax_check/run_checks.sh) | Syntax and behaviour checks without the Arduino toolchain |
| [host/ambient_host.py](host/ambient_host.py) | Host main: fast loop / music / light stream / agent |
| [host/agent.py](host/agent.py) | The one mind: DeepSeek tools, drives, memory |
| [host/looks.py](host/looks.py) | `Look` (fourteen numbers) and the 25 Hz frame engine |
| [host/effects.py](host/effects.py) | Named performances: candle / heartbeat / ocean / aurora |
| [host/reflex.py](host/reflex.py) | Sensor reflex layer — no network, never offline |
| [host/control.py](host/control.py) | Local socket: `light` / `blink` / `say` / `observe` |
| [host/lampctl.py](host/lampctl.py) | CLI: change the light, talk to it, read state |
| [host/lamp_app.py](host/lamp_app.py) | The app: host + page + corner window in one process |
| [host/ui.html](host/ui.html) | The face (ring) and the panel: talk, colour, play, setup |
| [host/webchat.py](host/webchat.py) | Serves `ui.html` and its API; owns no model |
| [host/paths.py](host/paths.py) | Where state, logs and the key live (project folder or Application Support) |
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
| `5V` | External 5 V / 3 A supply (**do not** power 32 LEDs from the Feather) |
| `GND` | External supply GND **and** Feather GND (common ground) |
| `DIN` | Feather `D6`, through 330 Ω |
| `5V`/`GND` | 1000 µF electrolytic near the strip |

32 LEDs at full white draw about 1.9 A. The host caps the channel value
(120/255 by default, about 0.9 A at full white, and far less in normal use), so
a 2 A supply is enough while the servos are parked. The three servos would share
that 5 V rail (see [Blades](#blades-power-and-calibration)), which is why it
becomes 3 A once they are enabled. Driving 5 V data from 3.3 V is marginal — add
a 74AHCT125 if you see flicker.

### The shade

- 10 cm ring, tracing-paper cylinder, base. Add a diffuser layer above the LEDs
  (milk acrylic or two sheets of paper) or you will see 32 hot spots.
- **Blades are parked.** The firmware can drive three blade servos, but
  `ENABLE_SERVO` is **0** by default: the servos that arrived are 360°
  continuous-rotation and cannot hold an angle. Until 180° positional servos
  are fitted, openness is the original **brightness modulation** (`open` 0–100
  → gain 0.02–1.0), so blinking still reads with no hardware attached.
- With `ENABLE_SERVO 1`: three 180° positional servos inside the ring, shafts
  radial at 120°, each turning an opaque blade across its third. `open` 0–100
  maps onto a narrow arc (1450 → 1650 µs ≈ 18°).
- Blades are card or acetate on a servo horn; keep them matte black on the LED
  side so they do not bounce light back through the paper.

### Sensors

- APDS9960 is onboard (I2C, no wiring). If you see `E,SENSOR_FAIL` at boot,
  check that the board is the Sense variant.
- A VL53L0X (ToF) is bought but **not wired**.
- Servo signals on **D11, D9, D10** (blades 1, 2, 3), powered from the same
  external 5 V rail as the strip, with a common ground; see below.

## Flash the firmware

1. Arduino IDE → install the **Adafruit nRF52** board package.
2. Library manager → **Adafruit NeoPixel**, **Adafruit APDS9960**.
3. Board → *Adafruit Feather nRF52840 Sense*, open the `.ino`, upload.
4. Serial monitor at 115200, line ending **New Line**. You can type:

```
T                         # red/green/blue/white self-test (blocks 1.2 s)
P                         # expect PONG
G,80,1,1                  # channel ceiling 80, gamma 2.2, dither on
F,150,220,180,20,2,30,40,10,40,80,600,0,0,0   # a look, all fourteen numbers
F,30,200,90,0,1,90,20,0,0,100,0,100,0,35      # rainbow + comet + stars
K,0                       # blink
K,2,255                   # white flash
B                         # report the three blade calibrations
B,2,1500,1700             # blade 2: 1500 us closed, 1700 us open
```

A desktop serial monitor cannot send a stream; the lamp will just ease toward
whatever single frame you typed. Use the host for real driving.

### Blades: power and calibration

> **Status: parked.** `ENABLE_SERVO` is **0** by default. The three-servo code,
> the `B` command and the boot report are complete and tested; enable them
> (`#define ENABLE_SERVO 1` or compile with `-DENABLE_SERVO=1`) once 180°
> positional servos are fitted. Until then the lamp runs on the brightness
> eyelid and none of this section is needed.

The three servos are the only part of the lamp that can brown out the board, so
wire them carefully first:

| Servo | Signal | Power |
|---|---|---|
| blade 1 | `D11` | external 5 V + |
| blade 2 | `D9` | external 5 V + |
| blade 3 | `D10` | external 5 V + |
| all | — | GND common with the Feather, 1000 µF across 5 V/GND |

An SG90 pulls ~700 mA when stalled; three plus 32 LEDs need **5 V / 3 A** (a
2 A supply will sag and reset the nRF52). Do not use the Feather's 3V/5V pin.

**Use positional (180°) servos, not 360° continuous-rotation ones.** A 360°
servo has no angle: 1500 µs stops it, and the pulse width sets *speed and
direction*, so a blade wired to one will simply spin forever. The product name
"360°" means "unlimited rotation", not "180° but more". SG90 180° and MG90S 180°
both work; MG90S is worth it for the metal gears.

**Test one servo before the whole lamp.** Flash
[`firmware/servo_test/servo_test.ino`](firmware/servo_test/servo_test.ino) on its
own — it contains only `Servo.h`, no NeoPixel and no sensor, so a working sweep
there proves the servo, its power and its signal pin:

```
servo on D11 attach=0
us=1500
c=closed  o=open  z=stop  +/-=nudge  s=sweep  p=print
a positional servo holds an angle; a 360 one keeps turning
```

Send `c`, `o`, or `s`. If nothing moves with this sketch, the problem is wiring
or power, not the lamp firmware. `attach=255` means the Servo library could not
claim a PWM channel — try another pin. If `c`/`o` make it rotate forever, it is a
360° type: a positional servo moves a few degrees and then holds. `z` (1500) is
the stop command for a 360° one. Power off and turn the horn by hand as well: a
positional servo has a hard stop near 180°, a 360° one turns freely.

With `ENABLE_SERVO 1`, the main firmware reports the same thing at boot:

```
E,SERVO_OK,1,11
E,SERVO_OK,2,9
E,SERVO_OK,3,10
```

`E,SERVO_FAIL,<n>` instead means that blade's `attach()` failed. Those lines are
the first thing to look at when a blade is silent.

Calibrate one blade at a time, with the host stopped (otherwise the `F` stream
overrides the position). Set `closed_us` until the blade just covers its sector
and `open_us` until it just clears the LEDs, then check with:

```
LID,0,0,0           # hold all blades closed for 3 min (a single F expires in 1.5 s)
LID,100,0,0         # hold them open
B                   # E,BLADE,1,... / 2 / 3
```

If a blade moves the wrong way, swap its two values. Keep the swing small: if
`open_us - closed_us` is above ~400 µs you are fighting the horn, not the blade.
The defaults assume all three horns point the same way; on a real build they
rarely do.

The servos and the NeoPixels both use the nRF52's PWM peripherals. The firmware
attaches the servos before the first LED frame, so they end up on different PWM
modules; if the strip still flickers while the blades move (or the servos twitch
while the LEDs change), move the servos to a PCA9685 over I2C — that removes the
contention and gives you a 6-servo board for later.

After changing the firmware, run the checks on the computer (no board needed):

```bash
cd firmware/syntax_check
./run_checks.sh
```

It does a `-Wall -Wextra` syntax check and drives `setup()/loop()` with a fake
clock and a fake serial port: startle, decay, fallback, frame timeout, dither,
gamma, cap, transients, coupling disabled, three blades following `open`, `B`
calibration, the v2.2.2 fields (rainbow turns, comet shape, sparkle, reverse
flow), 88 checks.

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
reflex on | stream 25 Hz cap 120 | agent deepseek-flash | slow 3..10 s | port auto | span 30.0 dB
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
| `--llm-min-interval` | 60 | minimum seconds between autonomous turns |
| `--llm-heartbeat` | 600 | how often it thinks when nothing changes |
| `--max-calls-per-hour` | 60 | hard sliding-window cap |
| `--llm-timeout` | 8 | per-call timeout (s) |
| `--look-hz` | 25 | parameter stream rate |
| `--cap` | 120 | channel ceiling sent via `G` (0.9 A at full white) |
| `--gamma` | 0 | 0 linear / 1 = 2.2 / 2 = 2.8 |
| `--no-reflex` | off | disable the sensor reflex layer |
| `--rest-after` | 180 | seconds of quiet before easing back to the amber rest look (0 disables) |
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

# the v2.2.2 basis
python3 lampctl.py light --hue-cycles 100 --grad 0      # one rainbow around the ring
python3 lampctl.py light --wave-n 1 --wave-amp 90 --shape 100 --wave-hz 30 --val 40  # comet
python3 lampctl.py light --hue 170 --val 50 --sparkle 35 --hue-cycles 0  # star field
python3 lampctl.py light --wave-hz -20                  # the flow reverses

python3 lampctl.py blink
python3 lampctl.py say "a little darker, slower"         # talk to the same agent
```

`light` writes the same fourteen numbers the agent writes, so it is a **debug
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
- the six buttons write parameters or start effects, they do not switch modes:
  dimmer / brighter / more flow / blink / candle / aurora
- status refreshes every 2 s; history goes to `webchat_history.json`
- binds `127.0.0.1` only — do not expose it

## What the agent decides

Each turn it receives a JSON `situation`: noise mean/peak/trend, music energy,
beat and track, whether someone is at the computer, idle seconds, room lux, **the
fourteen numbers it last wrote**, four drives, the last six memories and events.
When you speak, the situation gains a `said` field and that turn must answer.

| Tool | What it does |
|---|---|
| `set_light` | writes the fourteen numbers: hue / sat / val / grad / wave_n / wave_amp / wave_hz / breath_hz / breath_amp / open / ease / hue_cycles / shape / sparkle. No enums; omitted fields keep their value |
| `blink` | blink or wince — a transient, not a new appearance |
| `play_effect` | perform `candle` / `heartbeat` / `ocean` / `aurora` for a few seconds; it fades out by itself and leaves the base look alone |
| `set_reflex` | tune its own body: how easily a sound startles it, how much a hand matters, how much it dims when you leave, how much music shows |
| `feel` | nudge drives (energy / curiosity / security / bond), deltas only |
| `remember` | store something worth remembering tomorrow |
| `observe` | read sensors, macOS context, music — at most once per turn |

**It is not choosing from a menu.** `set_light` takes fourteen continuous values,
so "a little darker", "make it flow" and "too harsh" are each one or two
numbers. The reflex layer then rides on top: the agent decides *what it looks
like*, the reflex decides *how it answers the room*.

The three new values are how it escapes "one lamp with a moving band":

| Field | Value | Looks like |
|---|---|---|
| `hue_cycles` | 100 | one full rainbow around the ring (200 = two) |
| `shape` | 80–100 with `wave_amp` 90 | a narrow comet/scan band instead of a sine |
| `sparkle` | 10 / 35 / 70 | fireflies / a star field / embers |
| `wave_hz` | negative | the flow reverses |

## The reflex layer

[reflex.py](host/reflex.py) is one call per frame inside the 25 Hz engine. It has
no thread of its own, never sends a frame, and never touches the network — which
is why it survives a dead API.

| Sensor | Changes | Feels like |
|---|---|---|
| Silence | a clear breath (about 4.6 s, down to half brightness; 2.5 s when loud; almost dark only when nobody is there); the band at 15% and nearly parked | an untouched lamp only breathes, so every other motion has a cause |
| Room mic / system audio | white sparks (`sparkle`, louder = denser); breath and band wake within 0.15 s and settle over 2.5 s; a little brightness | a voice makes it spark and come alive |
| Music (system audio via BlackHole) | the flow nearly stops; `val` drops to 12% and is thrown up by the bass envelope on every kick, `open` going to 100 with it and the hardest kicks bleaching to white; no sparks; the ring becomes one rainbow that a strong kick turns by a notch | the whole lamp dances instead of spinning |
| Beat without music (a clap rhythm) | a ~200 ms pulse on `val`, a nudge to the band, a small lift of `open` | it moves with the tap |
| A sudden level jump | `K,2,255`, then a comet laps the ring for 1.6 s | a clap makes it flash and sends a light running |
| Mac idle 10 s → back | `K,0` blink, then a 3 s warm swell (`val`, `open`, hue) | it notices you coming back |
| Mac idle time | from 10 s it dims (up to 45%), breathes slower and turns blue through red and violet, full by 45 s; a touch brings amber back in about 1.5 s | an empty desk has its own colour |
| Proximity (board) | `open`, brightness, hue toward warm | silent while the board sits inside the shade |
| Room light (board) | a brightness floor | brighter by day, dimmer at night |

Design rules:

- **Still by default.** Presence and sound are sensed on the Mac (microphone,
  idle time); the lamp only moves when one of them changes, and the agent is
  only woken when the room changes state (arrive/leave, music on/off,
  quiet/loud). When it does change the light, the host adds a blink.
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

### Quiet returns to amber

The agent is allowed to leave the lamp on the colour of a moment, so the host
has a floor under it: after `--rest-after` seconds (default **180**) without a
sound above 12, a beat above 20, a hand at the sensor, or a change to the light,
the base eases back to the amber resting look (`DEFAULT_LOOK`, 3 s ease). It is
not a second mind — it only writes the same 14 numbers, and any noise, hand or
chat resets the clock. `--rest-after 0` turns it off, and `lampctl light` also
resets the clock, so a manual test is not yanked away immediately.

## Effects

The fourteen numbers are the *base*. [effects.py](host/effects.py) performs them:
each effect is a pure function of time that replaces the base temporarily, fades
in and out with an envelope, and then gets out of the way. The order inside the
25 Hz loop is:

```
ease the agent's target -> apply the active effect -> the reflex bends it -> wire
```

Because the effect runs **before** the reflex, sound and hands still ride on top,
and the base look is never modified. An effect is a verb, not a mode: there is no
state left behind.

| Effect | Default length | Looks like | When it fits |
|---|---|---|---|
| `candle` | 45 s | warm, irregular flicker; never loops | reading, a room that should feel alive |
| `heartbeat` | 8 s | two thumps per 1.1 s | a message, a kick, someone back at the desk |
| `ocean` | 90 s | slow teal bands, 25 s swell | long quiet work |
| `aurora` | 180 s | a wide gradient drifting through hue | minutes of nothing happening |

```bash
./lampctl.py effects                          # list them
./lampctl.py effect candle                    # its own length
./lampctl.py effect ocean --seconds 300       # longer
./lampctl.py effect heartbeat --intensity 0.6 # half strength
./lampctl.py stop-effect
./lampctl.py observe                          # shows "effect candle 12.3s / 45s"
```

The agent gets the same vocabulary as `play_effect(kind, seconds, intensity,
reason)`, and the situation tells it what is currently playing, so it does not
stack three effects on top of each other.

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
| A blade does not move | boot log `E,SERVO_OK/FAIL`, then `servo_test` | flash `servo_test` alone; moves there → main pin map/calibration, not moving → power/ground/signal |
| A blade spins forever, never holds an angle | the servo type | it is a 360° continuous-rotation servo (1500 µs = stop); use a 180° positional servo |
| `E,SERVO_FAIL,<n>` | Servo library could not claim a PWM channel | move that blade to another free pin; if it persists, use a PCA9685 |
| Servo buzzes or gets warm at an end point | mechanical stop | back `closed_us`/`open_us` off by 20–40 µs |
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
