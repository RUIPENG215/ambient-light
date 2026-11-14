#pragma once
#include <Arduino.h>

// Same sentinel as the real library.
#define INVALID_SERVO 255

// Test double for the Adafruit nRF52 Servo library. It records what the
// firmware asked for, so test_state.cpp can assert on the three blades.
class Servo {
 public:
  int  pin      = -1;
  int  minUs    = 1000;
  int  maxUs    = 2000;
  int  us       = 0;      // last commanded pulse width
  int  writes   = 0;      // how many times writeMicroseconds() was called
  bool attached = false;

  int attach(int p) { return attach(p, 1000, 2000); }

  int attach(int p, int lo, int hi) {
    pin = p;
    minUs = lo;
    maxUs = hi;
    attached = true;
    return 0;             // a valid channel index, never INVALID_SERVO
  }

  void writeMicroseconds(int value) {
    us = value;
    writes++;
  }

  void write(int degrees) {
    us = minUs + (maxUs - minUs) * degrees / 180;
    writes++;
  }

  int read() const { return us; }
};
