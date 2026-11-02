/*
 * Interactive Ambient Light — firmware v2.2
 * Board : Adafruit Feather nRF52840 Sense
 * LEDs  : 32 x WS2812B, data pin 6, GRB, 800 kHz
 * Sensor: onboard APDS9960 (proximity + RGB/ALS)
 *
 * v2.2 moves the "modes" out of the board. The firmware no longer knows what
 * calm / alert / curious / sleepy mean; it only knows a colour field, a spatial
 * wave, a breathing envelope, transients and reflexes. The host streams the
 * look as a parameter frame (F) at 20-30 Hz. - M is kept as a preset shorthand so
 * an old host keeps working. See FIRMWARE-API.md for the contract.
 *
 * Host -> board
 *   F,<hue>,<sat>,<val>,<grad>,<wave_n>,<wave_amp>,<wave_hz>,
 *     <breath_hz>,<breath_amp>,<open>,<ease_ms>    look frame, 20-30 Hz
 *   K,<type>[,<strength>]                         0 blink 1 wince 2 flash
 *   G,<cap>,<gamma>,<dither>                      render config
 *   L,<0-100>                                     noise level, 20 Hz
 *   M,<preset>,<hue>,<speed>,<bright>,<lid>       legacy preset shorthand
 *   LID,<open>,<ms>,<mode>[,<amp>,<period>]       legacy eyelid alias
 *   U,<music>,<beat>,<present>                    music bus, modulation only
 *   C,<audio>,<beat>,<prox>,<idle>                coupling gains 0..100
 *   R,<startle>,<prox>,<enabled>                  reflex thresholds
 *   P  ping -> PONG      T  LED self-test      X  back to fallback
 *
 * Board -> host
 *   S,<prox>,<als>,<lux>,<energy>,<origin>,<open>,<flags>   telemetry, 4 Hz
 *   E,<event>[,<detail>]   BOOT STARTLE WAKE CMD FRAME_LOST FRAME_OK
 *                          LINK_LOST LINK_OK SENSOR_FAIL RESET
 *
 * Layering (high -> low, they compose, they do not fight):
 *   startle flash / transients > K > U music > F frame > fallback look
 * The board keeps working with no host attached.
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_NeoPixel.h>
#include <Adafruit_APDS9960.h>

// ================= hardware =================
#define LED_PIN   6
#define LED_COUNT 32

const bool  BAR_MODE = false;   // false: whole ring; true: level bar
const float SMOOTH   = 0.25f;   // audio level smoothing

// ================= timing / behaviour =================
#define FW_VERSION            "2.2.0"

#define FRAME_MS              20      // 50 fps render
#define SENSOR_MS             100     // APDS9960 10 Hz
#define REPORT_MS             250     // telemetry 4 Hz

#define STARTLE_LEVEL         45      // absolute level that may startle
#define STARTLE_JUMP          25      // relative jump over the smoothed value
#define ACTIVITY_LEVEL        12      // above this counts as "something happened"
#define PROX_NEAR             120     // 0..255, a hand close by is 150+
#define SLEEP_AFTER_MS        45000UL // fallback dims over this window
#define LINK_TIMEOUT_MS       12000UL // no bytes at all -> LINK_LOST
#define FRAME_TTL_MS          1500UL  // no F frame -> the host stopped talking
#define PRESET_TTL_MS         180000UL// no M -> the legacy preset expired
#define STARTLE_HOLD_MS       2000UL  // startle flagged for this long

// ---- render defaults (G command overrides them) ----
#define DEFAULT_CAP           60      // channel ceiling, 40 was v2.1's BRIGHTNESS
#define DEFAULT_GAMMA         0       // 0 linear, 1 = 2.2, 2 = 2.8
#define DEFAULT_DITHER        1
#define DITHER_MIN            8       // below this a 1-LSB toggle is visible flicker

// ---- transients (values inherited from v2.1) ----
#define BLINK_CLOSE_MS        120
#define BLINK_HOLD_MS         60
#define BLINK_OPEN_MS         180
#define WINCE_CLOSE_MS        100
#define WINCE_RETURN_MS       1500
#define WINCE_LOW             15.0f
#define FLASH_TAU_S           0.28f

// ---- fallback look: what the lamp is when nobody is talking to it ----
#define FB_HUE                30.0f
#define FB_SAT                200.0f
#define FB_VAL                120.0f
#define FB_GRAD               6.0f
#define FB_WAVE_N             1.0f
#define FB_WAVE_AMP           5.0f
#define FB_WAVE_HZ            3.0f
#define FB_BREATH_HZ          12.0f
#define FB_BREATH_AMP         55.0f
#define FB_OPEN               55.0f
#define FB_EASE_MS            3000.0f
#define FB_SLEEP_DIM          0.45f   // how much of val/open the quiet dims

// Servo (eyelid / louver). Off by default: open is rendered by modulation.
// Flip ENABLE_SERVO to 1 and the same open parameter drives the servo.
#define ENABLE_SERVO          0
#define PIN_SERVO             5
#define LID_SERVO_CLOSED_US   1000
#define LID_SERVO_OPEN_US     2000

#if ENABLE_SERVO
  #include <Servo.h>
  Servo louverServo;
#endif

Adafruit_NeoPixel pixels(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
Adafruit_APDS9960  apds;
bool sensorOk = false;

const float TWO_PI_F = 6.2831853f;

// ================= look parameters =================
// Field order is the wire order of the F command.
enum Param : uint8_t {
  P_HUE = 0, P_SAT, P_VAL, P_GRAD,
  P_WAVE_N, P_WAVE_AMP, P_WAVE_HZ,
  P_BREATH_HZ, P_BREATH_AMP, P_OPEN, P_EASE,
  P_COUNT
};

float lookTarget[P_COUNT] = {
  FB_HUE, FB_SAT, FB_VAL, FB_GRAD, FB_WAVE_N, FB_WAVE_AMP, FB_WAVE_HZ,
  FB_BREATH_HZ, FB_BREATH_AMP, FB_OPEN, FB_EASE_MS
};
float lookCur[P_COUNT] = {
  FB_HUE, FB_SAT, FB_VAL, FB_GRAD, FB_WAVE_N, FB_WAVE_AMP, FB_WAVE_HZ,
  FB_BREATH_HZ, FB_BREATH_AMP, FB_OPEN, FB_EASE_MS
};

// order: hue, sat, val, grad, wave_n, wave_amp, wave_hz, breath_hz, breath_amp, open, ease
// (only used to clamp what arrives on the wire)
static const float PARAM_LO[P_COUNT] = {
  0.0f, 0.0f, 0.0f, -128.0f, 0.0f, 0.0f, 0.0f,
  0.0f, 0.0f, 0.0f, 0.0f
};
static const float PARAM_HI[P_COUNT] = {
  255.0f, 255.0f, 255.0f, 127.0f, 16.0f, 100.0f, 255.0f,
  255.0f, 100.0f, 100.0f, 10000.0f
};

// ================= render config =================
uint8_t gCap    = DEFAULT_CAP;
uint8_t gGamma  = DEFAULT_GAMMA;
bool    gDither = DEFAULT_DITHER;
uint8_t renderOut[LED_COUNT];        // last 8-bit value per LED (also handy on a scope)

// ================= animation state =================
float    breathPhase = 0.0f;
float    wavePhase   = 0.0f;
float    flash       = 0.0f;         // 0..1 white flash
int      level = 0;                  // raw 0..100
float    shown = 0.0f;               // smoothed level
int      barLit = 0;
uint16_t frameNo = 0;

// ================= openness (the old "eyelid") =================
float lidPos    = FB_OPEN;           // effective 0..100, used for rendering
float gOpenGain = 1.0f;
uint8_t transientMode = 0;           // 0 none, 1 blink, 2 wince
uint32_t transientStart = 0;
float    transientReturn = FB_OPEN;

// ================= music / coupling =================
uint8_t  musicLevel = 0;
float    beatPulse = 0.0f;
float    gMusicGain = 1.0f;
uint32_t lastMusicAt = 0;
uint8_t  couplingAudio = 60;
uint8_t  couplingBeat = 60;
uint8_t  couplingProx = 50;
uint8_t  couplingIdle = 50;

// ================= reflex thresholds =================
uint8_t  startleLevel = STARTLE_LEVEL;
uint8_t  proxNear = PROX_NEAR;
bool     reflexEnabled = true;

// ================= who owns the look =================
enum Origin : uint8_t { ORIGIN_FALLBACK = 0, ORIGIN_HOST = 1 };
Origin   origin = ORIGIN_FALLBACK;
bool     streamSeen = false;
uint32_t lastStreamAt = 0;
bool     presetValid = false;
uint32_t lastPresetAt = 0;
bool     frameLost = false;
bool     linkLost = false;

// ================= sensors =================
int      prox = 0;
uint16_t als = 0;
float    lux = 0.0f;

// ================= clocks =================
uint32_t lastFrame = 0;
uint32_t lastSensor = 0;
uint32_t lastReport = 0;
uint32_t lastActivityAt = 0;
uint32_t lastStartleAt = 0;
uint32_t lastHostAt = 0;
bool     hostSeen = false;

// ================= serial =================
char    rxBuf[80];
uint8_t rxLen = 0;

// ================= presets (the M shorthand) =================
// These are only a lookup table for a legacy command; nothing in the renderer
// knows their names.
struct Preset {
  float breathHz, breathAmp, waveN, waveAmp, waveHz, grad, sat, val;
};
#define PRESET_COUNT 4
static const char *PRESET_NAME[PRESET_COUNT] = { "calm", "alert", "curious", "sleepy" };
static const Preset PRESETS[PRESET_COUNT] = {
  { 24.0f, 71.0f, 1.0f,  4.0f, 10.0f,  8.0f, 235.0f, 196.0f },  // calm
  {120.0f, 45.0f, 3.0f, 35.0f, 60.0f, 16.0f, 245.0f, 230.0f },  // alert
  { 10.0f, 10.0f, 1.0f, 70.0f, 30.0f, 15.0f, 220.0f, 200.0f },  // curious
  {  8.0f, 40.0f, 1.0f,  4.0f,  2.0f,  4.0f, 230.0f, 120.0f }   // sleepy
};

// ordered dither, 0..15 (1/16 of a code step, spread over 4x4 pixels/frames)
static const uint8_t DITHER4[4][4] = {
  { 0,  8,  2, 10},
  {12,  4, 14,  6},
  { 3, 11,  1,  9},
  {15,  7, 13,  5}
};

// ================= forward declarations =================
void sendEvent(const char *name);
void sendEvent(const char *name, const char *detail);
int  presetIndex(const char *name);
void applyPreset(uint8_t idx, float hue, float bright, float lid, float easeMs);
void installFallback();
void requestTransient(uint8_t mode);
void startle(uint32_t now);
void handleLevel(int value, uint32_t now);
float openGain(float open);
float sleepScale(uint32_t now);
float updateTransient(uint32_t now, float baseOpen);
float gammaEncode(float x);
void renderFrame(uint32_t now, float sleep);
void serviceSensors(uint32_t now);
void readSerial(uint32_t now);
void handleLine(char *line, uint32_t now);
void reportTelemetry(uint32_t now);
void checkLink(uint32_t now);
void selfTest();
void update(float dt, uint32_t now);

// ================= helpers =================
bool parseArg(char *tok, float *out, float lo, float hi) {
  if (tok == nullptr || *tok == '\0') return false;
  char *end = nullptr;
  float v = strtof(tok, &end);
  if (end == tok) return false;
  if (v < lo) v = lo;
  if (v > hi) v = hi;
  *out = v;
  return true;
}

float openGain(float open) {
  if (open <= 0.0f) return 0.02f;
  if (open >= 100.0f) return 1.0f;
  return 0.02f + 0.98f * (open / 100.0f);
}

int presetIndex(const char *name) {
  for (uint8_t i = 0; i < PRESET_COUNT; i++) {
    if (!strcmp(name, PRESET_NAME[i])) return (int)i;
  }
  return -1;
}

void applyPreset(uint8_t idx, float hue, float bright, float lid, float easeMs) {
  const Preset &p = PRESETS[idx];
  lookTarget[P_HUE]        = hue;
  lookTarget[P_SAT]        = p.sat;
  lookTarget[P_VAL]        = p.val * (bright / 255.0f);
  lookTarget[P_GRAD]       = p.grad;
  lookTarget[P_WAVE_N]     = p.waveN;
  lookTarget[P_WAVE_AMP]   = p.waveAmp;
  lookTarget[P_WAVE_HZ]    = p.waveHz;
  lookTarget[P_BREATH_HZ]  = p.breathHz;
  lookTarget[P_BREATH_AMP] = p.breathAmp;
  lookTarget[P_OPEN]       = lid;
  lookTarget[P_EASE]       = easeMs;
}

void installFallback() {
  lookTarget[P_HUE]        = FB_HUE;
  lookTarget[P_SAT]        = FB_SAT;
  lookTarget[P_VAL]        = FB_VAL;
  lookTarget[P_GRAD]       = FB_GRAD;
  lookTarget[P_WAVE_N]     = FB_WAVE_N;
  lookTarget[P_WAVE_AMP]   = FB_WAVE_AMP;
  lookTarget[P_WAVE_HZ]    = FB_WAVE_HZ;
  lookTarget[P_BREATH_HZ]  = FB_BREATH_HZ;
  lookTarget[P_BREATH_AMP] = FB_BREATH_AMP;
  lookTarget[P_OPEN]       = FB_OPEN;
  lookTarget[P_EASE]       = FB_EASE_MS;
}

// The lamp dims itself when nothing has happened for a while. In v2.1 this was
// a mood (sleepy); here it is just a scale on val/open, and it only applies
// while the host is not driving the look.
float sleepScale(uint32_t now) {
  if (origin != ORIGIN_FALLBACK) return 1.0f;
  float k = (float)(uint32_t)(now - lastActivityAt) / (float)SLEEP_AFTER_MS;
  if (k > 1.0f) k = 1.0f;
  return 1.0f - FB_SLEEP_DIM * k;
}

// ================= reflexive layer =================
void requestTransient(uint8_t mode) {
  if (transientMode != 1 && transientMode != 2) transientReturn = lidPos;
  transientMode = mode;
  transientStart = millis();
}

void startle(uint32_t now) {
  lastStartleAt = now;
  flash = 1.0f;
  requestTransient(2);       // wince
  sendEvent("STARTLE");
}

void handleLevel(int value, uint32_t now) {
  level = constrain(value, 0, 100);
  if (level >= ACTIVITY_LEVEL) lastActivityAt = now;

  // A sudden jump -> startle. Music never comes through here, so a bass drop cannot startle.
  if (reflexEnabled && level >= (int)startleLevel &&
      level - (int)shown >= STARTLE_JUMP &&
      (uint32_t)(now - lastStartleAt) > STARTLE_HOLD_MS) {
    startle(now);
  }
}

// ================= transient / openness =================
float updateTransient(uint32_t now, float baseOpen) {
  if (transientMode != 1 && transientMode != 2) return baseOpen;

  uint32_t e = (uint32_t)(now - transientStart);
  if (transientMode == 1) {                       // blink
    if (e < BLINK_CLOSE_MS) {
      return transientReturn * (1.0f - (float)e / (float)BLINK_CLOSE_MS);
    }
    if (e < BLINK_CLOSE_MS + BLINK_HOLD_MS) return 0.0f;
    if (e < BLINK_CLOSE_MS + BLINK_HOLD_MS + BLINK_OPEN_MS) {
      return transientReturn *
             ((float)(e - BLINK_CLOSE_MS - BLINK_HOLD_MS) / (float)BLINK_OPEN_MS);
    }
    transientMode = 0;
    return baseOpen;
  }

  float low = fminf(WINCE_LOW, transientReturn);   // wince, returns on its own
  if (e < WINCE_CLOSE_MS) {
    return transientReturn - (transientReturn - low) * ((float)e / (float)WINCE_CLOSE_MS);
  }
  if (e < WINCE_CLOSE_MS + WINCE_RETURN_MS) {
    return low + (transientReturn - low) *
                 ((float)(e - WINCE_CLOSE_MS) / (float)WINCE_RETURN_MS);
  }
  transientMode = 0;
  return baseOpen;
}

// ================= render =================
float gammaEncode(float x) {
  if (x <= 0.0f) return 0.0f;
  if (x >= 1.0f) return 1.0f;
  if (gGamma == 1) return powf(x, 2.2f);
  if (gGamma == 2) return powf(x, 2.8f);
  return x;
}

void renderFrame(uint32_t now, float sleep) {
  float amp = lookCur[P_BREATH_AMP] / 100.0f;
  float env = 1.0f - amp * (0.5f - 0.5f * cosf(breathPhase));
  int   distIdx = (int)((now / FRAME_MS) & 3);

  for (uint16_t i = 0; i < LED_COUNT; i++) {
    float x = (float)i / (float)(LED_COUNT - 1);
    float h = lookCur[P_HUE] + lookCur[P_GRAD] * (x - 0.5f) * 2.0f;
    float w = 1.0f + (lookCur[P_WAVE_AMP] / 100.0f) *
                     sinf(TWO_PI_F * lookCur[P_WAVE_N] * x - wavePhase);

    float v = (lookCur[P_VAL] / 255.0f) * env * w * sleep;
    v *= gOpenGain * gMusicGain;
    v += flash * 0.9f;
    v += beatPulse * 0.18f * (0.25f + 0.75f * gOpenGain) * (couplingBeat / 100.0f);
    if (v < 0.0f) v = 0.0f;
    if (v > 1.0f) v = 1.0f;

    float sat = lookCur[P_SAT] * (1.0f - 0.8f * flash);
    if (sat < 0.0f) sat = 0.0f;
    if (sat > 255.0f) sat = 255.0f;
    int hh = ((int)lroundf(h) % 256 + 256) % 256;

    // perceptual -> linear, then the hardware ceiling, then dither.
    float lin = gammaEncode(v) * (gCap / 255.0f);
    uint16_t v12 = (uint16_t)(lin * 255.0f * 16.0f + 0.5f);
    uint16_t out = v12 >> 4;
    // Below DITHER_MIN one code step is a doubling of the light, so the
    // "smoothing" would show up as a 50 Hz flicker. Stay quantised down there.
    if (gDither && out >= DITHER_MIN) {
      out = (v12 + DITHER4[i & 3][(distIdx + (int)(i >> 1)) & 3]) >> 4;
    }
    if (out > gCap) out = gCap;

    renderOut[i] = (uint8_t)out;
    if (BAR_MODE && i >= barLit) out = 0;

    pixels.setPixelColor(i, pixels.ColorHSV((uint16_t)(hh * 257), (uint8_t)sat,
                                            (uint8_t)out));
  }
}

// ================= sensors =================
void serviceSensors(uint32_t now) {
  if (!sensorOk) return;
  if ((uint32_t)(now - lastSensor) < SENSOR_MS) return;
  lastSensor = now;

  int p = apds.readProximity();
  prox = constrain(p, 0, 255);

  if (apds.colorDataReady()) {
    uint16_t r, g, b, c;
    apds.getColorData(&r, &g, &b, &c);
    als = c;
    lux = apds.calculateLux(r, g, b);
  }

  // While the host drives the look, proximity is just telemetry: the host
  // decides what "somebody is near" means. In fallback the lamp still wakes.
  if (prox >= (int)proxNear) {
    bool drowsy = (uint32_t)(now - lastActivityAt) > SLEEP_AFTER_MS;
    lastActivityAt = now;
    if (origin == ORIGIN_FALLBACK && drowsy) {
      requestTransient(2);
      sendEvent("WAKE", "prox");
    }
  }
}

// ================= serial =================
void sendEvent(const char *name) {
  Serial.print("E,");
  Serial.println(name);
}

void sendEvent(const char *name, const char *detail) {
  Serial.print("E,");
  Serial.print(name);
  Serial.print(',');
  Serial.println(detail);
}

void readSerial(uint32_t now) {
  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rxBuf[rxLen] = '\0';
        handleLine(rxBuf, now);
        rxLen = 0;
      }
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = (char)c;
    } else {
      rxLen = 0;   // overflow: drop the whole line
    }
  }
}

void handleLine(char *line, uint32_t now) {
  lastHostAt = now;
  hostSeen = true;
  if (linkLost) {
    linkLost = false;
    sendEvent("LINK_OK");
  }

  char *cmd = strtok(line, ",");
  if (cmd == NULL) return;

  // ---------------- look frame ----------------
  if (!strcmp(cmd, "F")) {
    bool any = false;
    for (uint8_t p = 0; p < P_COUNT; p++) {
      float v;
      if (parseArg(strtok(NULL, ","), &v, PARAM_LO[p], PARAM_HI[p])) {
        lookTarget[p] = v;
        any = true;
      }
    }
    if (any) {
      streamSeen = true;
      lastStreamAt = now;
    }
    return;
  }

  // ---------------- transient ----------------
  if (!strcmp(cmd, "K")) {
    float type, strength = 255.0f;
    if (!parseArg(strtok(NULL, ","), &type, 0.0f, 2.0f)) return;
    parseArg(strtok(NULL, ","), &strength, 0.0f, 255.0f);
    if ((int)lroundf(type) == 2) {
      flash = strength / 255.0f;
    } else {
      requestTransient((int)lroundf(type) == 1 ? 2 : 1);
    }
    return;
  }

  // ---------------- render config ----------------
  if (!strcmp(cmd, "G")) {
    float cap, gamma, dither;
    if (parseArg(strtok(NULL, ","), &cap, 0.0f, 255.0f)) gCap = (uint8_t)cap;
    if (parseArg(strtok(NULL, ","), &gamma, 0.0f, 2.0f)) gGamma = (uint8_t)gamma;
    if (parseArg(strtok(NULL, ","), &dither, 0.0f, 1.0f)) gDither = dither > 0.5f;
    return;
  }

  // ---------------- legacy preset shorthand ----------------
  if (!strcmp(cmd, "M")) {
    char *m = strtok(NULL, ",");
    if (m == NULL) return;
    int idx = presetIndex(m);
    if (idx < 0) return;

    float hue = lookTarget[P_HUE];
    float speed = 90.0f, bright = 200.0f, lid = FB_OPEN;
    parseArg(strtok(NULL, ","), &hue, 0.0f, 255.0f);
    char *a2 = strtok(NULL, ",");
    char *a3 = strtok(NULL, ",");
    char *a4 = strtok(NULL, ",");
    parseArg(a2, &speed, 0.0f, 255.0f);
    parseArg(a3, &bright, 0.0f, 255.0f);
    parseArg(a4, &lid, 0.0f, 100.0f);
    (void)speed;   // the preset table already fixes the breathing rate

    if (idx == 3) hue = 30.0f;             // sleepy keeps the amber
    applyPreset((uint8_t)idx, hue, bright, lid, 800.0f);
    presetValid = true;
    lastPresetAt = now;
    sendEvent("CMD", PRESET_NAME[idx]);
    return;
  }

  // ---------------- legacy eyelid alias ----------------
  if (!strcmp(cmd, "LID")) {
    float open, ms = 800.0f, mode = 0.0f;
    if (!parseArg(strtok(NULL, ","), &open, 0.0f, 100.0f)) return;
    parseArg(strtok(NULL, ","), &ms, 0.0f, 60000.0f);
    parseArg(strtok(NULL, ","), &mode, 0.0f, 3.0f);

    // v2.1 set the lid target for every mode, so a blink returns to the
    // openness that came with it. Keep that.
    lookTarget[P_OPEN] = open;
    lookTarget[P_EASE] = ms;
    if ((int)lroundf(mode) == 2 || (int)lroundf(mode) == 3) {
      transientReturn = open;
      transientMode = (int)lroundf(mode) == 2 ? 1 : 2;
      transientStart = now;
    }
    presetValid = true;
    lastPresetAt = now;
    return;
  }

  // ---------------- music / coupling / reflex ----------------
  if (!strcmp(cmd, "U")) {
    float a1, a2;
    if (parseArg(strtok(NULL, ","), &a1, 0.0f, 100.0f)) musicLevel = (uint8_t)a1;
    if (parseArg(strtok(NULL, ","), &a2, 0.0f, 100.0f)) {
      if (a2 > 20.0f) beatPulse = fmaxf(beatPulse, a2 / 100.0f);
    }
    lastMusicAt = now;
    return;
  }

  if (!strcmp(cmd, "C")) {
    float v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 100.0f)) couplingAudio = (uint8_t)v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 100.0f)) couplingBeat = (uint8_t)v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 100.0f)) couplingProx = (uint8_t)v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 100.0f)) couplingIdle = (uint8_t)v;
    return;
  }

  if (!strcmp(cmd, "R")) {
    float v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 100.0f)) startleLevel = (uint8_t)v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 255.0f)) proxNear = (uint8_t)v;
    if (parseArg(strtok(NULL, ","), &v, 0.0f, 1.0f)) reflexEnabled = v > 0.5f;
    return;
  }

  if (!strcmp(cmd, "L")) {
    float v;
    if (!parseArg(strtok(NULL, ","), &v, 0.0f, 100.0f)) return;
    handleLevel((int)v, now);
    return;
  }

  if (!strcmp(cmd, "P")) {
    Serial.println("PONG");
    return;
  }

  if (!strcmp(cmd, "T")) {
    selfTest();
    return;
  }

  if (!strcmp(cmd, "X")) {
    streamSeen = false;
    presetValid = false;
    transientMode = 0;
    origin = ORIGIN_FALLBACK;
    installFallback();
    lookTarget[P_EASE] = 1500.0f;
    sendEvent("RESET");
    return;
  }
}

void reportTelemetry(uint32_t now) {
  if ((uint32_t)(now - lastReport) < REPORT_MS) return;
  lastReport = now;

  bool drowsy = origin == ORIGIN_FALLBACK &&
                (uint32_t)(now - lastActivityAt) > SLEEP_AFTER_MS;
  uint8_t flags = 0;
  if ((uint32_t)(now - lastStartleAt) < STARTLE_HOLD_MS) flags |= 0x01;
  if (drowsy) flags |= 0x02;
  if (frameLost) flags |= 0x04;

  Serial.print("S,");
  Serial.print(prox);
  Serial.print(',');
  Serial.print(als);
  Serial.print(',');
  Serial.print((int)(lux + 0.5f));
  Serial.print(',');
  Serial.print((int)(shown + 0.5f));
  Serial.print(',');
  Serial.print(origin == ORIGIN_HOST ? "host" : "fallback");
  Serial.print(',');
  Serial.print((int)(lidPos + 0.5f));
  Serial.print(',');
  Serial.println((int)flags);
}

void checkLink(uint32_t now) {
  if (hostSeen && !linkLost && (uint32_t)(now - lastHostAt) > LINK_TIMEOUT_MS) {
    linkLost = true;
    streamSeen = false;      // the host is gone, not just quiet
    presetValid = false;
    sendEvent("LINK_LOST");
  }
}

// ================= self test =================
void selfTest() {
  const uint32_t colors[4] = { 0xFF0000, 0x00FF00, 0x0000FF, 0xFFFFFF };
  pixels.setBrightness(gCap);
  for (uint8_t c = 0; c < 4; c++) {
    for (uint16_t i = 0; i < LED_COUNT; i++) pixels.setPixelColor(i, colors[c]);
    pixels.show();
    delay(300);
  }
  pixels.clear();
  pixels.show();
  pixels.setBrightness(255);
}

// ================= main update =================
void update(float dt, uint32_t now) {
  // ---- who owns the look ----
  bool streamAlive = streamSeen && (uint32_t)(now - lastStreamAt) < FRAME_TTL_MS;
  bool presetAlive = presetValid && (uint32_t)(now - lastPresetAt) < PRESET_TTL_MS;

  if (!streamAlive && !presetAlive) {
    if (origin != ORIGIN_FALLBACK) {
      origin = ORIGIN_FALLBACK;
      installFallback();
      sendEvent("FRAME_LOST");
      frameLost = true;
    }
  } else if (origin == ORIGIN_FALLBACK) {
    origin = ORIGIN_HOST;
    if (frameLost) {
      frameLost = false;
      sendEvent("FRAME_OK");
    }
  }

  // ---- ease towards the target ----
  float ease = lookTarget[P_EASE];
  if (ease <= 0.0f) {
    for (uint8_t p = 0; p < P_COUNT; p++) lookCur[p] = lookTarget[p];
  } else {
    float tau = ease / 3000.0f;
    if (tau < 0.02f) tau = 0.02f;
    float a = 1.0f - expf(-dt / tau);
    for (uint8_t p = 0; p < P_COUNT; p++) {
      if (p == P_EASE) continue;
      lookCur[p] += (lookTarget[p] - lookCur[p]) * a;
    }
  }

  // ---- phases ----
  breathPhase += TWO_PI_F * (lookCur[P_BREATH_HZ] / 100.0f) * dt;
  if (breathPhase > TWO_PI_F) breathPhase -= TWO_PI_F;
  wavePhase += TWO_PI_F * (lookCur[P_WAVE_HZ] / 100.0f) * dt;
  if (wavePhase > TWO_PI_F) wavePhase -= TWO_PI_F;

  shown += ((float)level - shown) * SMOOTH;

  flash *= expf(-dt / FLASH_TAU_S);
  if (flash < 0.01f) flash = 0.0f;

  if (musicLevel == 0 || (uint32_t)(now - lastMusicAt) > 1500) {
    gMusicGain = 1.0f;
  } else {
    float m = musicLevel / 100.0f;
    gMusicGain = 1.0f + 0.25f * (couplingAudio / 100.0f) * (m - 0.5f) * 2.0f;
  }
  beatPulse *= expf(-dt / 0.20f);
  if (beatPulse < 0.01f) beatPulse = 0.0f;

  barLit = (int)ceilf(shown / 100.0f * LED_COUNT);
  if (barLit > LED_COUNT) barLit = LED_COUNT;
  if (barLit < 1) barLit = 1;

  // ---- openness ----
  float sleep = sleepScale(now);
  lidPos = updateTransient(now, constrain(lookCur[P_OPEN] * sleep, 0.0f, 100.0f));
  gOpenGain = openGain(lidPos);

#if ENABLE_SERVO
  int us = (int)(LID_SERVO_CLOSED_US +
                 (LID_SERVO_OPEN_US - LID_SERVO_CLOSED_US) * (lidPos / 100.0f));
  louverServo.writeMicroseconds(us);
#endif

  renderFrame(now, sleep);
  pixels.show();
  frameNo++;

  reportTelemetry(now);
  checkLink(now);
}

void setup() {
  Serial.begin(115200);

  pixels.begin();
  pixels.setBrightness(255);      // the cap lives in software now (G command)
  pixels.clear();
  pixels.show();

  Wire.begin();
  sensorOk = apds.begin();
  if (sensorOk) {
    apds.enableProximity(true);
    apds.enableColor(true);
  } else {
    sendEvent("SENSOR_FAIL", "apds9960");
  }

#if ENABLE_SERVO
  louverServo.attach(PIN_SERVO, LID_SERVO_CLOSED_US, LID_SERVO_OPEN_US);
  louverServo.writeMicroseconds(
      (int)(LID_SERVO_CLOSED_US +
            (LID_SERVO_OPEN_US - LID_SERVO_CLOSED_US) * (FB_OPEN / 100.0f)));
#endif

  // keep the warm-orange boot colour, hold it 400 ms, then start the render loop
  pixels.fill(pixels.Color(255, 70, 10));
  pixels.show();
  delay(400);
  pixels.clear();

  uint32_t now = millis();
  lastFrame = now;
  lastSensor = now;
  lastReport = now;
  lastActivityAt = now;
  lastStartleAt = now - 5000UL;
  lidPos = FB_OPEN;
  gOpenGain = openGain(lidPos);
  installFallback();

  sendEvent("BOOT", FW_VERSION);
}

void loop() {
  uint32_t now = millis();

  readSerial(now);
  serviceSensors(now);

  if ((uint32_t)(now - lastFrame) >= FRAME_MS) {
    float dt = (float)(now - lastFrame) * 0.001f;
    lastFrame = now;
    update(dt, now);
  }
}
