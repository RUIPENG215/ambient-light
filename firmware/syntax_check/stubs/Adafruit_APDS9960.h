#pragma once
#include <Arduino.h>
#include <Wire.h>

extern uint16_t gProx;

// Minimal stand-in covering both APDS9960 API styles seen in the wild:
//   * colorAvailable()/readAmbientLight()/readRedLight()...
//   * colorDataReady()/getColorData()/calculateLux()  (Adafruit 1.x)
class Adafruit_APDS9960 {
 public:
  bool begin(uint8_t = 0x39, TwoWire * = &Wire) { return true; }
  bool enableProximity(bool = true) { return true; }
  bool enableColor(bool = true) { return true; }

  uint16_t readProximity() { return gProx; }

  bool colorAvailable() { return true; }
  uint16_t readAmbientLight() { return 120; }
  uint16_t readRedLight() { return 10; }
  uint16_t readGreenLight() { return 20; }
  uint16_t readBlueLight() { return 5; }

  bool colorDataReady() { return true; }
  void getColorData(uint16_t *r, uint16_t *g, uint16_t *b, uint16_t *c) {
    if (r) *r = 10;
    if (g) *g = 20;
    if (b) *b = 5;
    if (c) *c = 120;
  }
  float calculateLux(uint16_t r, uint16_t g, uint16_t b) {
    return 0.3f * (float)r + 0.6f * (float)g + 0.1f * (float)b;
  }
};
