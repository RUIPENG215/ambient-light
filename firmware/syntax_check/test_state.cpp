// Host-side behaviour test for firmware v2.2.
// Drives setup()/loop() with a fake clock and a fake serial port.
// Build: see run_checks.sh
#include "stubs/Arduino.h"
#include "stubs/Wire.h"
#include <cstdio>
#include <cmath>
#include <string>
#include <vector>

uint32_t gMillis = 0;
std::string gSerialOut;
std::deque<int> gSerialIn;
uint16_t gProx = 0;

SerialStub Serial;
TwoWire Wire;

#include "../ambient_light/ambient_light.ino"

static int gFailures = 0;
static std::string gAllOut;   // everything ever printed, for "never happens" checks

static void check(bool ok, const char *what) {
  printf("%s  %s\n", ok ? "PASS" : "FAIL", what);
  if (!ok) gFailures++;
}

static void feed(const char *s) {
  while (*s) gSerialIn.push_back((unsigned char)*s++);
}

static bool saw(const char *needle) {
  return gSerialOut.find(needle) != std::string::npos;
}

static int countOf(const std::string &needle) {
  int n = 0;
  size_t pos = gSerialOut.find(needle);
  while (pos != std::string::npos) {
    n++;
    pos = gSerialOut.find(needle, pos + needle.size());
  }
  return n;
}

static void clearOut() {
  gAllOut += gSerialOut;
  gSerialOut.clear();
}

static void stepFor(uint32_t ms) {
  uint32_t end = gMillis + ms;
  while (gMillis < end) {
    gMillis += 20;
    loop();
  }
}

static std::string lastSLine() {
  size_t pos = gSerialOut.rfind("S,");
  while (pos != std::string::npos) {
    if (pos == 0 || gSerialOut[pos - 1] == '\n') {
      size_t end = gSerialOut.find('\n', pos);
      return gSerialOut.substr(pos, end == std::string::npos ? std::string::npos
                                                            : end - pos);
    }
    if (pos == 0) break;
    pos = gSerialOut.rfind("S,", pos - 1);
  }
  return "";
}

static std::vector<std::string> split(const std::string &line) {
  std::vector<std::string> out;
  size_t start = 0;
  while (true) {
    size_t comma = line.find(',', start);
    if (comma == std::string::npos) {
      out.push_back(line.substr(start));
      break;
    }
    out.push_back(line.substr(start, comma - start));
    start = comma + 1;
  }
  return out;
}

static bool near(float a, float b, float tol) { return std::fabs(a - b) <= tol; }

int main() {
  setup();
  check(saw("E,BOOT,2.2.0"), "boot event carries 2.2.0");
  check(!saw("E,SENSOR_FAIL"), "APDS9960 initialises");
  clearOut();

  // ---------------- quiet / fallback ----------------
  feed("L,10\n");
  stepFor(1000);
  check(!saw("E,STARTLE"), "quiet input does not startle");
  check(saw("S,"), "telemetry frames are emitted");

  std::vector<std::string> t = split(lastSLine());
  check(t.size() == 8, "telemetry has 8 fields");
  check(t.size() > 5 && t[5] == "fallback", "no host -> origin is fallback");
  check(origin == ORIGIN_FALLBACK, "no host -> fallback owns the look");
  check(renderOut[0] >= 5,
        "fallback is a visible glow, not a near-black flicker");
  clearOut();

  // ---------------- startle reflex ----------------
  feed("L,95\n");
  stepFor(60);
  check(saw("E,STARTLE"), "sudden loud jump startles");
  check(flash > 0.5f, "startle raises the flash envelope");
  check(renderOut[0] > 0, "startle is visible in the render output");
  stepFor(1000);
  check(flash < 0.05f, "flash decays away");
  check(!saw("E,MOOD"), "no MOOD event exists any more");
  clearOut();

  // ---------------- F frame ----------------
  feed("F,150,220,180,20,2,30,40,10,40,80,600\n");
  stepFor(40);
  check(near(lookTarget[P_HUE], 150, 0.01f) &&
        near(lookTarget[P_SAT], 220, 0.01f) &&
        near(lookTarget[P_VAL], 180, 0.01f),
        "F parses hue/sat/val");
  check(near(lookTarget[P_GRAD], 20, 0.01f) &&
        near(lookTarget[P_WAVE_N], 2, 0.01f) &&
        near(lookTarget[P_BREATH_HZ], 10, 0.01f) &&
        near(lookTarget[P_OPEN], 80, 0.01f) &&
        near(lookTarget[P_EASE], 600, 0.01f),
        "F parses the rest of the frame");
  check(origin == ORIGIN_HOST, "a fresh F hands the look to the host");
  stepFor(300);
  t = split(lastSLine());
  check(t.size() > 5 && t[5] == "host", "telemetry reports origin host");

  // a frame with only one field keeps everything else
  feed("F,60\n");
  stepFor(40);
  check(near(lookTarget[P_HUE], 60, 0.01f) &&
        near(lookTarget[P_SAT], 220, 0.01f),
        "missing F fields keep their previous value");

  // out of range values are clamped, not rejected
  feed("F,300,999,999,-999,99,999,999,999,999,999,99999\n");
  stepFor(40);
  check(near(lookTarget[P_HUE], 255, 0.01f) &&
        near(lookTarget[P_VAL], 255, 0.01f) &&
        near(lookTarget[P_GRAD], -128, 0.01f) &&
        near(lookTarget[P_OPEN], 100, 0.01f) &&
        near(lookTarget[P_EASE], 10000, 0.01f),
        "out of range F fields are clamped");

  // a garbage frame must not count as a live frame
  uint32_t streamAt = lastStreamAt;
  feed("F,abc\n");
  stepFor(40);
  check(lastStreamAt == streamAt, "malformed F does not refresh the stream");
  clearOut();

  // ---------------- streaming is quiet ----------------
  for (int i = 0; i < 75; i++) {           // 75 * 40 ms = 3 s at 25 Hz
    feed("F,150,220,180,20,2,30,40,10,40,80,600\n");
    stepFor(40);
  }
  check(!saw("E,FRAME_LOST"), "a live stream never reports FRAME_LOST");
  check(!saw("E,CMD"), "F frames do not spam command events");
  clearOut();

  // ---------------- frame timeout ----------------
  stepFor(2000);
  check(countOf("E,FRAME_LOST") == 1, "stopping F emits FRAME_LOST once");
  check(origin == ORIGIN_FALLBACK, "FRAME_LOST hands the look back");
  check(frameLost, "frameLost flag is set");
  clearOut();

  stepFor(6000);
  check(near(lookCur[P_VAL], FB_VAL, 20.0f),
        "the fallback look eases in, it does not snap");

  // ---------------- stream resumes ----------------
  feed("F,150,220,180,20,2,30,40,10,40,80,600\n");
  stepFor(40);
  check(saw("E,FRAME_OK"), "a returning stream emits FRAME_OK");
  check(origin == ORIGIN_HOST, "FRAME_OK hands the look to the host");
  clearOut();

  // ---------------- gamma / cap / dither ----------------
  feed("G,60,0,0\n");
  feed("F,0,0,255,0,0,0,0,0,0,100,0\n");
  stepFor(200);
  check(renderOut[0] == 60, "linear gamma at full val hits the cap exactly");
  check(gDither == false, "G turns dithering off");
  clearOut();

  feed("G,0,0,0\n");
  stepFor(120);
  check(renderOut[0] == 0 && renderOut[31] == 0, "cap 0 renders nothing");
  clearOut();

  feed("G,60,0,0\n");
  feed("F,0,0,128,0,0,0,0,0,0,100,0\n");
  stepFor(200);
  uint8_t linear = renderOut[0];
  feed("G,60,1,0\n");
  stepFor(200);
  check(renderOut[0] < linear, "gamma 2.2 is darker than linear at mid val");
  check(renderOut[0] > 0, "gamma 2.2 keeps a non-zero mid tone");
  clearOut();

  feed("G,60,1,1\n");
  stepFor(120);
  check(gDither, "G turns dithering back on");
  check(renderOut[0] <= gCap, "dithering never exceeds the cap");

  // A 1-LSB toggle on a nearly dark channel is a 100% brightness jump, which
  // reads as flicker. It must stay quantised down there.
  feed("G,60,0,1\n");
  feed("F,30,200,16,0,0,0,0,0,0,100,0\n");
  stepFor(200);
  int dimA = renderOut[0];
  stepFor(20);
  int dimB = renderOut[0];
  stepFor(20);
  int dimC = renderOut[0];
  check(dimA == dimB && dimB == dimC,
        "dither is gated off where one step is a doubling");

  // ... and it must still smooth things where one step is small.
  feed("F,30,200,220,0,0,0,0,0,0,100,0\n");
  stepFor(200);
  int first = renderOut[0];
  bool varies = false;
  for (int i = 0; i < 8; i++) {
    stepFor(20);
    if (renderOut[0] != first) varies = true;
  }
  check(varies, "dither still smooths the mid range");
  clearOut();

  // ---------------- M preset shorthand ----------------
  feed("M,calm,140,90,200,70\n");
  stepFor(400);
  check(saw("E,CMD,calm"), "legacy M is still accepted");
  check(!saw("M,calm,140,90,200,70"), "host command is not echoed");
  check(near(lookTarget[P_OPEN], 70, 0.01f), "M carries the open target");
  check(near(lookTarget[P_VAL], 196.0f * 200.0f / 255.0f, 0.5f),
        "M maps brightness through the calm preset");
  check(origin == ORIGIN_HOST, "M keeps an old host in charge");
  clearOut();

  feed("M,sleepy,140,15,60,20\n");
  stepFor(120);
  check(near(lookTarget[P_HUE], 30, 0.01f), "sleepy preset keeps the amber hue");
  check(near(lookTarget[P_VAL], 120.0f * 60.0f / 255.0f, 0.5f),
        "sleepy preset maps brightness");
  clearOut();

  feed("M,bogus,1,2,3,4\n");
  stepFor(120);
  check(!saw("E,CMD"), "unknown preset is ignored");

  // ---------------- legacy LID alias ----------------
  feed("LID,20,300,0\n");
  stepFor(600);
  check(near(lookTarget[P_OPEN], 20, 0.01f), "LID hold sets the open target");
  check(lidPos < 25.0f, "LID hold closes the eye");
  clearOut();

  feed("LID,60,300,2\n");
  stepFor(120);
  check(lidPos < 20.0f, "LID blink closes quickly");
  stepFor(400);
  check(lidPos > 40.0f, "LID blink returns toward open");
  clearOut();

  // ---------------- K transients ----------------
  feed("K,1\n");
  stepFor(120);
  check(lidPos <= 16.0f, "K wince dips the eye");
  stepFor(1700);
  check(lidPos > 50.0f, "K wince returns on its own");
  clearOut();

  feed("K,2,255\n");
  stepFor(40);
  check(flash > 0.7f, "K flash raises the envelope");
  stepFor(1000);
  check(flash < 0.05f, "K flash decays");
  clearOut();

  // ---------------- music / coupling / reflex ----------------
  feed("C,90,80,70,60\n");
  stepFor(40);
  check(couplingAudio == 90 && couplingBeat == 80 &&
        couplingProx == 70 && couplingIdle == 60,
        "coupling gains are parsed");

  feed("U,80,100,1\n");
  stepFor(40);
  check(beatPulse > 0.5f, "music beat sets a pulse");
  stepFor(600);
  check(beatPulse < 0.5f, "beat pulse decays");

  feed("R,90,130,0\n");
  stepFor(40);
  check(startleLevel == 90 && proxNear == 130 && !reflexEnabled,
        "reflex thresholds are parsed");

  feed("L,5\n");
  stepFor(500);
  feed("L,95\n");
  stepFor(60);
  check(!saw("E,STARTLE"), "disabled reflex does not startle");
  clearOut();

  // ---------------- proximity ----------------
  feed("R,90,130,1\n");
  feed("F,150,220,180,20,2,30,40,10,40,80,600\n");
  gProx = 200;
  stepFor(400);
  check(!saw("E,WAKE"), "proximity is only telemetry while the host drives");
  gProx = 0;
  clearOut();

  // ---------------- link loss ----------------
  stepFor(13000);
  check(saw("E,LINK_LOST"), "link watchdog fires after 12 s");
  check(origin == ORIGIN_FALLBACK, "link loss hands the look back");
  clearOut();

  // ---------------- fallback drowsiness ----------------
  stepFor(46000);
  check(sleepScale(gMillis) < 0.6f, "the fallback look dims when nothing happens");
  t = split(lastSLine());
  check(t.size() == 8 && ((atoi(t[7].c_str()) & 0x02) != 0),
        "telemetry flags drowsy in fallback");

  gProx = 200;
  stepFor(300);
  check(saw("E,WAKE,prox"), "fallback lamp still wakes to a hand");
  gProx = 0;
  clearOut();

  // ---------------- reset ----------------
  feed("X\n");
  stepFor(100);
  check(saw("E,RESET"), "X resets to fallback");
  check(origin == ORIGIN_FALLBACK, "X hands the look back");
  clearOut();

  feed("F,150,220,80,0,1,10,10,20,30,60,0\n");
  stepFor(40);
  check(origin == ORIGIN_HOST, "F after X takes over again");
  check(saw("E,FRAME_OK"), "recovering from a frame loss reports FRAME_OK");

  // ---------------- robustness ----------------
  feed("PPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPP"
       "PPPPPPPPPPPPPPPPPPPPPPPP\n");
  feed("P\n");
  stepFor(40);
  check(saw("PONG"), "an overlong line is dropped without breaking the parser");
  clearOut();

  // ---------------- design guarantees ----------------
  check(gAllOut.find("E,MOOD") == std::string::npos,
        "the whole session never mentions a mood");

  printf("\n%s (%d failure%s)\n", gFailures ? "FAILED" : "OK",
         gFailures, gFailures == 1 ? "" : "s");
  return gFailures ? 1 : 0;
}
