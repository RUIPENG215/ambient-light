#pragma once
#include <Arduino.h>

class Servo {
 public:
  void attach(int, int = 1000, int = 2000) {}
  void writeMicroseconds(int) {}
};
