// Minimal host-side stand-ins for Arduino APIs.
// Purpose: syntax-check and unit-test the .ino without the nRF52 core.
// These are NOT a simulator and must never be flashed.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <string>
#include <deque>

#define constrain(x, lo, hi) ((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x)))
#define F(x) x

extern uint32_t gMillis;
extern std::string gSerialOut;
extern std::deque<int> gSerialIn;

inline uint32_t millis() { return gMillis; }
inline void delay(uint32_t ms) { gMillis += ms; }
inline void pinMode(int, int) {}
inline long random(long) { return 0; }
inline long random(long, long) { return 0; }

inline long map(long x, long in_min, long in_max, long out_min, long out_max) {
  return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min;
}

struct SerialStub {
  void begin(long) {}
  int available() { return (int)gSerialIn.size(); }
  int read() {
    if (gSerialIn.empty()) return -1;
    int c = gSerialIn.front();
    gSerialIn.pop_front();
    return c;
  }
  void print(const char *s) { gSerialOut += s; }
  void print(char c) { gSerialOut += c; }
  void print(int v) { gSerialOut += std::to_string(v); }
  void print(unsigned int v) { gSerialOut += std::to_string(v); }
  void print(long v) { gSerialOut += std::to_string(v); }
  void print(unsigned long v) { gSerialOut += std::to_string(v); }
  void print(float v) { gSerialOut += std::to_string(v); }
  void print(double v) { gSerialOut += std::to_string(v); }
  void println(const char *s) { gSerialOut += s; gSerialOut += '\n'; }
  void println(char c) { gSerialOut += c; gSerialOut += '\n'; }
  void println(int v) { gSerialOut += std::to_string(v); gSerialOut += '\n'; }
  void println(unsigned int v) { gSerialOut += std::to_string(v); gSerialOut += '\n'; }
  void println(long v) { gSerialOut += std::to_string(v); gSerialOut += '\n'; }
  void println(unsigned long v) { gSerialOut += std::to_string(v); gSerialOut += '\n'; }
  void println(float v) { gSerialOut += std::to_string(v); gSerialOut += '\n'; }
  void println(double v) { gSerialOut += std::to_string(v); gSerialOut += '\n'; }
};

extern SerialStub Serial;
