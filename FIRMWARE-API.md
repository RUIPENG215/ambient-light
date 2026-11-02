# Firmware contract — v2.2.0

What the board promises, why it is shaped this way, and what is still in there
from v2.1. Wire-level detail lives in [PROTOCOL.md](PROTOCOL.md); this document
is about the *model* behind the bytes.

## The one idea

The firmware does not know what "calm" or "curious" means. It knows a colour
field, a spatial wave, a breathing envelope, transients and reflexes. The host
describes a look with eleven numbers, 20–30 times a second, and the board draws
it at 50 fps.

**Meaning lives on the computer. Physics lives on the board.**

## What the firmware is responsible for

| # | Responsibility | Why it cannot move to the host |
|---|---|---|
| 1 | 50 fps render, gamma, dither | the host sends 25 frames/s; something has to interpolate them without visible steps |
| 2 | transients: blink / wince / flash | a 120 ms blink is three frames at 25 Hz; it would read as a stutter |
| 3 | the board's own startle (when `L` is sent) | 20 ms | 
| 4 | fallback look + slow doze | if the host dies, USB is unplugged, or the laptop sleeps, the lamp must not become a black brick |
| 5 | telemetry and the link watchdog | only the board can notice that it stopped hearing from the host |

Everything else — what looks good, what reacts to what — is host-side.

## Commands

Full field tables in PROTOCOL.md. In short:

```
F,<hue>,<sat>,<val>,<grad>,<wave_n>,<wave_amp>,<wave_hz>,<breath_hz>,<breath_amp>,<open>,<ease_ms>
K,<type>[,<strength>]     0 blink, 1 wince, 2 flash
G,<cap>,<gamma>,<dither>  render configuration
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
     wave_phase   += 2π * (wave_hz   / 100) * dt

3) breathing envelope (pressing down from the top, so nothing clips)
     env = 1 - (breath_amp / 100) * (0.5 - 0.5 * cos(breath_phase))

4) per pixel i of 32
     x    = i / 31
     h_i  = hue + grad * (x - 0.5) * 2
     w    = 1 + (wave_amp / 100) * sin(2π * wave_n * x - wave_phase)
     v    = (val / 255) * env * w * open_gain * music_gain
     v   += flash * 0.9
     v   += beat_pulse * 0.18 * (0.25 + 0.75 * open_gain) * (coupling_beat / 100)
     v    = clamp(v, 0, 1)

5) output
     lin = gamma(v)                     // gamma 0: identity, 1: x^2.2, 2: x^2.8
     pwm = lin * cap                    // 0..cap
     if dither and pwm >= 8: pwm += ordered_dither[i][frame]   // 1/16 LSB
     setPixel(i, HSV(h_i, sat * (1 - 0.8 * flash), pwm))
```

`open_gain(open) = 0.02 + 0.98 * open/100`, so a closed eye still lets a little
light through. With `ENABLE_SERVO 1` the same `open` drives a servo and the gain
stays 1.0.

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
bit2 frame timeout. Events: `BOOT CMD STARTLE WAKE FRAME_LOST FRAME_OK LINK_LOST
LINK_OK SENSOR_FAIL RESET`. `E,MOOD` is gone.

## Verification

```bash
cd firmware/syntax_check && ./run_checks.sh
```

`-Wall -Wextra -fsyntax-only`, then a behaviour test that drives `setup()` and
`loop()` with a fake clock and a fake serial port: **70 checks**, including frame
parsing and clamping, a malformed frame not refreshing the clock, the 1.5 s
timeout easing out, `G` parsing, gamma being monotonic, the dither floor, dither
still working in the mid range, `K` timing, coupling disabled, and the design
guarantee that the firmware never mentions a mood.

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
   eleven numbers and a host-side vocabulary meant the light could be tuned
   without reflashing anything.
2. **Check the bottom of the brightness range on hardware, not on paper.** Every
   multiply looked reasonable; the product landed on channel 2.
3. **Send numbers, not pixels.** A dropped parameter frame is absorbed by the
   interpolation; a dropped pixel frame is a visible jump.
4. **Decide who owns the light, in one sentence.** "The agent decides what it
   looks like, the reflex decides how it answers the room" is that sentence; every
   design question since has been answered by it.
