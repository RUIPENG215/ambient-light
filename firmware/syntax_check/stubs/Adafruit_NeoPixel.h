#pragma once
#include <Arduino.h>

class Adafruit_NeoPixel {
 public:
  Adafruit_NeoPixel(uint16_t, int16_t, uint16_t) {}
  void begin() {}
  void show() {}
  void clear() {}
  void setBrightness(uint8_t) {}
  void setPixelColor(uint16_t, uint32_t) {}
  uint32_t getPixelColor(uint16_t) const { return 0; }
  void fill(uint32_t) {}
  static uint32_t Color(uint8_t r, uint8_t g, uint8_t b) {
    return ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
  }
  static uint32_t ColorHSV(uint16_t, uint8_t = 255, uint8_t = 255) { return 0; }
  static uint32_t gamma32(uint32_t x) { return x; }
};

#define NEO_GRB 0x52
#define NEO_KHZ800 0x0000
