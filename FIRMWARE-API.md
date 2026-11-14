# Firmware contract — v2.2.2

What the board promises, why it is shaped this way, and what is still in there
from v2.1. Wire-level detail lives in [PROTOCOL.md](PROTOCOL.md); this document
is about the *model* behind the bytes.

## The one idea

The firmware does not know what "calm" or "curious" means. It knows a colour
field, a spatial wave, a breathing envelope, white sparkles, transients and
reflexes. The host describes a look with fourteen numbers, 20–30 times a
second, and the board draws it at 50 fps.

**Meaning lives on the computer. Physics lives on the board.**

## What the firmware is responsible for

| # | Responsibility | Why it cannot move to the host |
|---|---|---|
| 1 | 50 fps render, gamma, dither | the host sends 25 frames/s; something has to interpolate them without visible steps |
| 2 | transients: blink / wince / flash | a 120 ms blink is three frames at 25 Hz; it would read as a stutter |
| 3 | the board's own startle (when `L` is sent) | 20 ms | 
| 4 | fallback look + slow doze | if the host dies, USB is unplugged, or the laptop sleeps, the lamp must not become a black brick |
| 5 | telemetry and the link watchdog | only the board can notice that it stopped hearing from the host |
| 6 | three blade servos | the end points are mechanical and per board, and the blades must still move when the host is silent (fallback, blink, doze) |

Everything else — what looks good, what reacts to what — is host-side.

## Commands

Full field tables in PROTOCOL.md. In short:

```
F,<hue>,<sat>,<val>,<grad>,<wave_n>,<wave_amp>,<wave_hz>,<breath_hz>,<breath_amp>,<open>,<ease_ms>,<hue_cycles>,<shape>,<sparkle>
K,<type>[,<strength>]     0 blink, 1 wince, 2 flash
G,<cap>,<gamma>,<dither>  render configuration
B[,<servo>,<closed_us>,<open_us>]   blade end points / report
P  T  X
```

The v2.1 commands `L`, `M`, `LID`, `U`, `C`, `R` are still parsed. The host no
longer sends them; they are scheduled for removal in v2.3.

## Render model

Per frame, with `dt` between 16 and 40 ms:

```
1) ease every parameter toward its target
     tau = max(ease_ms / 3000, 0.02) s, or snap when ease_ms == 0

2) advance phases
     breath_phase += 2π * (breath_hz / 100) * dt
     wave_phase   += 2π * (wave_hz   / 100) * dt      // signed: negative reverses

3) breathing envelope (pressing down from the top, so nothing clips)
     env = 1 - (breath_amp / 100) * (0.5 - 0.5 * cos(breath_phase))

4) per pixel i of 32
     x    = i / 31
     h_i  = hue + grad * (x - 0.5) * 2 + (hue_cycles / 100) * 256 * x
     s    = sin(2π * wave_n * x - wave_phase)
     shp  = 2 * ((1 + s) / 2) ^ (1 + 7 * shape / 100) - 1   // 0 = sine, 100 = comet
     w    = 1 + (wave_amp / 100) * shp
     v    = (val / 255) * env * w * open_gain * music_gain
     v   += flash * 0.9
     v   += beat_pulse * 0.18 * (0.25 + 0.75 * open_gain) * (coupling_beat / 100)
     v   += spark[i] * 0.9 * open_gain                 // white glint, through the eyelid
     v    = clamp(v, 0, 1)

5) output
     lin = gamma(v)                     // gamma 0: identity, 1: x^2.2, 2: x^2.8
     pwm = lin * cap                    // 0..cap
     if dither and pwm >= 8: pwm += ordered_dither[i][frame]   // 1/16 LSB
     setPixel(i, HSV(h_i, sat * (1 - 0.8 * flash) * (1 - 0.85 * spark[i]), pwm))
```

`spark[i]` decays with τ = 0.12 s; each frame, with probability
`sparkle/100 * 3 * dt` per pixel, it is re-ignited to 0.55–1.0. That is the only
randomness in the renderer, and it is local to the board — the host never sends
pixels.

`open_gain(open) = 0.02 + 0.98 * open/100`, so a closed eye still lets a little
light through. With `ENABLE_SERVO 1` the same `open` turns three blades and the
gain stays 1.0 — the mechanical occlusion is the eye, so the LEDs must not dim
as well or closing the blades would go doubly dark. `ENABLE_SERVO` is **0 by
default in v2.2.1**: the delivered servos turned out to be 360° continuous
rotation, which cannot hold an angle. The blade code stays compiled out until
180° positional servos are fitted.

## The basis, v2.2.2: three new dimensions

v2.2 replaced moods with a parameter space, but that space was still **one
linear hue ramp, one sine wave, one global envelope**. It could not make a
rainbow, a comet or a star field — which is why the lamp read as variations of
one look. v2.2.2 appends three continuous dimensions and signs `wave_hz`:

| Field | What it adds | Try |
|---|---|---|
| `hue_cycles` 0–400 | full hue turns around the ring (0.01 turns) | 100 = one rainbow, 200 = two, 50 = half |
| `shape` 0–100 | sharpens the sine into a narrow band | 80–100 with `wave_amp` 90 and `val` 40 = comet |
| `sparkle` 0–100 | random white glints, τ = 0.12 s | 5–15 fireflies, 30–50 stars, 70+ embers |
| `wave_hz` −255…255 | direction | −20 flows the other way |

They are **appended to `F`**, so an 11-field host is bit-for-bit unchanged and
an older board ignores them. The renderer is still one pass, one random source,
and no pixels on the wire.

What is still out of reach: two independent colours per pixel (a real palette),
sharp spatial masks, text, or per-pixel effects that are not white glints.
Those need either a palette field (`hue2 + mix`, still parametric) or a
low-resolution framebuffer (the raster experiment). v2.2.2 deliberately stops
before both — the point was to widen the instrument, not to replace it.

## Blades: three servos, one parameter

> **Parked by default.** Set `ENABLE_SERVO 1` (or `-DENABLE_SERVO=1`) to use
> this. Everything below describes the enabled build.

Three 180° positional servos sit inside the ring, their shafts radial and 120°
apart. Each turns an opaque blade across its third of the ring. Only `open`
drives them; the host does not know they exist, which is why the wire format did
not change. A 360° continuous-rotation servo must not be used: 1500 µs stops it
and the pulse sets speed/direction, so the blade would spin forever.

Pins are **D11, D9, D10** (blades 1–3). D5 is avoided: it is the SPI SS pin and
its header contact has been unreliable on this build. D11 is a plain GPIO
(P0.06), free of Wire (D22/D23), SPI (D24–D26), PDM (D34/D35) and the on-board
LEDs (D4/D8/D13).

```
open 0   -> servoClosedUs[i]      blade covers its sector
open 100 -> servoOpenUs[i]        blade clear of the LEDs
us(i)    = closed + (open/100) * (open_of_i - closed_of_i)   // clamped 1000..2000
```

- **Small arc on purpose.** 1450 → 1650 µs is about 18° on an SG90, enough to
  uncover a third of a 10 cm ring. The blade does not need to travel 90°.
- **Per-servo end points.** No two horns are mounted the same way; `B` sets them
  at runtime, and swapping closed/open inverts a blade.
- **Deadband.** A settled blade is not rewritten for sub-2 µs changes, so a
  constant `open` does not make three servos buzz.
- **50 Hz, not 25 Hz.** `updateBlades()` runs in the render loop; the host's
  frames only move the target. A `K` blink therefore reaches the blades in
  120 ms even though frames arrive every 40 ms.
- **Boot position.** `setup()` parks the blades at `FB_OPEN` (55), so the lamp
  does not boot with its eyes shut.
- **PWM before pixels.** The servos are attached before the first
  `pixels.show()`. The NeoPixel driver takes the first idle PWM peripheral, so
  claiming one for the blades first keeps the two on separate modules.

At boot the firmware reports each attach: `E,SERVO_OK,<n>,<pin>` or
`E,SERVO_FAIL,<n>`. If a blade is silent, that line is the first thing to read;
`firmware/servo_test/servo_test.ino` is the second (it uses only `Servo.h`).

Power: an SG90 can pull ~700 mA stalled. Three of them plus 32 LEDs exceed a
2 A supply; use 5 V / 3 A or more, a common ground, and a 1000 µF capacitor
near the servos. Never power them from the Feather's 3V/5V pin.

## Brightness: why the cap is applied *after* gamma

`val` is a linear amplitude. `cap` is a hardware ceiling applied in linear space:

```
pwm = gamma(v) * cap
```

Putting `cap` inside the gamma — `gamma(v * cap/255)` — would make a full-bright
frame `pow(0.235, 2.2) ≈ 4%`, dimmer than v2.1. That mistake existed in the first
draft of this document and was caught on hardware.

## Dither: why there is a floor

WS2812 is 8-bit per channel. At `cap 80` and an openness of 55, a resting lamp
sits around channel 10–20; one code step is 5–10% of the light, which is exactly
the banding that makes a slow fade look steppy. Temporal dithering spreads that
quantisation over frames.

But below a channel value of about 8, one code step **doubles** the light, and
dithering turns a dim glow into a 50 Hz flicker. So `DITHER_MIN = 8`: smooth in
the middle, quantised at the bottom.

This was the first real bug found on hardware: with gamma 2.2 as the default the
fallback lamp sat at channel 1–3 and visibly flickered. Both the gamma default
and the dither floor came out of that.

## Timing and TTL

| Timer | Value | Behaviour |
|---|---|---|
| render | 20 ms | 50 fps |
| sensor | 100 ms | APDS9960 |
| telemetry | 250 ms | 4 Hz |
| **frame timeout** | **1500 ms** | no `F` (and no fresh `M`) → `FRAME_LOST`, 3 s ease into the fallback |
| link watchdog | 12000 ms | no bytes at all → `LINK_LOST` |
| legacy preset | 180000 ms | an `M`-only host keeps ownership for 3 minutes |

Two clocks because there are two questions: *"is the host still talking?"*
(1.5 s) and *"is the host dead?"* (12 s). If a frame stops arriving the lamp
eases out; it never freezes on the last frame. An `M`-only host keeps the wider
window, because it sends one line every 30 s and would otherwise be judged dead
constantly.

## Layers, not a priority ladder

v2.1 resolved one winner per moment: startle > proximity > host > autonomy. With
a continuous stream that question disappears. v2.2 composes:

| Layer | When |
|---|---|
| fallback look | always the base |
| host `F` | frame younger than 1.5 s, overrides the base |
| transients `K` | until they finish, overriding `open` and adding flash |
| startle | `L` jump → flash + wince; only reachable when `L` is sent |

Proximity is deliberately dumb in v2.2: in fallback it wakes a dozing lamp
(`E,WAKE`), and while the host streams it is only telemetry — the reflex layer on
the computer decides what a hand means.

## Fallback look

What the lamp is when nobody is talking to it:

```
hue 30, sat 200, val 120, grad 28, wave_n 1, wave_amp 30, wave_hz 8,
breath_hz 12, breath_amp 55, open 55, ease 3000 ms
```

The doze is two curves over quiet time, not a state:

```
k = min(1, quiet / 45 s)
val  *= 1 - 0.45 k        // 120 → 66
open *= 1 - 0.45 k        // 55 → 30
```

## Telemetry and events

```
S,<prox>,<als>,<lux>,<energy>,<origin>,<open>,<flags>
```

`origin` is `host` or `fallback`; `flags` bit0 startle active, bit1 dozing,
bit2 frame timeout. Events: `BOOT CMD BLADE SERVO_OK SERVO_FAIL STARTLE WAKE
FRAME_LOST FRAME_OK LINK_LOST LINK_OK SENSOR_FAIL RESET`. `E,MOOD` is gone.

## Verification

```bash
cd firmware/syntax_check && ./run_checks.sh
```

`-Wall -Wextra -fsyntax-only`, then a behaviour test that drives `setup()` and
`loop()` with a fake clock and a fake serial port: **88 checks**, including frame
parsing and clamping, a malformed frame not refreshing the clock, the 1.5 s
timeout easing out, `G` parsing, gamma being monotonic, the dither floor, dither
still working in the mid range, `K` timing, coupling disabled, the three blades
following `open` over a small arc with a deadband, `B` calibration, the boot
servo report, the v2.2.2 fields (clamping, a sharp wave vs a sine, negative
`wave_hz`, spark ignition and decay, an 11-field frame leaving them alone), and
the design guarantee that the firmware never mentions a mood.

Not verified: the real nRF52 toolchain beyond compiling (82,640 bytes of flash,
9,340 bytes of RAM) — there is no unit test on the board itself.

## Still in the file, unused

`M` + the preset table, `LID`, `L`, `U`, `C`, `R`, `couplingProx`,
`couplingIdle`, `gMusicGain`, `beatPulse`, `BAR_MODE`. They cost flash and
attention, and the host sends none of them. Removing them is firmware v2.3 and
needs a reflash; it is deferred until after the current build has been played
with enough to be sure nothing is missing.

## Lessons worth keeping

1. **A parameter space beats a menu.** Four moods meant four hard-coded shapes;
   a continuous space plus a host-side vocabulary meant the light could be
   tuned without reflashing anything.
2. **Check the bottom of the brightness range on hardware, not on paper.** Every
   multiply looked reasonable; the product landed on channel 2.
3. **A basis is still a choice.** The first parameter space was one hue ramp, one
   sine and one envelope — neutral about meaning, but narrow about physics. Adding
   three continuous dimensions (v2.2.2) bought a rainbow, a comet and a star field
   without giving up interpolation or putting pixels on the wire.
4. **Send numbers, not pixels.** A dropped parameter frame is absorbed by the
   interpolation; a dropped pixel frame is a visible jump.
5. **Decide who owns the light, in one sentence.** "The agent decides what it
   looks like, the reflex decides how it answers the room" is that sentence; every
   design question since has been answered by it.
