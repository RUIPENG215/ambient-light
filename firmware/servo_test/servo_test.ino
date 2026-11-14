/*
 * Minimal single-servo test — no NeoPixel, no APDS9960, no host, no protocol.
 *
 * Flash this on its own to prove the servo, its power and its signal pin
 * before blaming the lamp firmware. It includes only Servo.h, so a working
 * sweep here means the hardware is fine and the problem is in the main sketch
 * (pin map, calibration, or a PWM peripheral that could not be claimed).
 *
 * Wiring
 *   signal (orange/yellow) -> SERVO_PIN, D11 by default
 *   red                    -> external 5 V
 *   brown/black            -> GND, common with the Feather
 *
 * Serial monitor at 115200, line ending "New Line":
 *   c        go to CLOSED_US
 *   o        go to OPEN_US
 *   z        go to 1500 us (centre; STOPS a continuous-rotation servo)
 *   + / -    nudge 10 us
 *   1500     go to an absolute pulse width (1000..2000)
 *   s        sweep closed -> open -> closed once (run it unloaded first)
 *   p        print the current pulse width
 *
 * If the horn turns forever instead of holding an angle, this is a **360°
 * continuous-rotation** servo (or a positional one with a dead feedback pot),
 * not a 180° positional servo. On those 1500 us = stop, >1500 and <1500 are
 * the two directions at a speed proportional to the offset; they have no
 * "angle". The blades need a positional servo — see the README.
 */

#include <Servo.h>

const int SERVO_PIN = 11;      // D11 for blade 1; use 9 or 10 for blades 2/3
const int CLOSED_US = 1450;
const int OPEN_US   = 1650;

Servo blade;
int us = 1500;
char buf[16];
uint8_t len = 0;

void show() {
  Serial.print("us=");
  Serial.println(us);
}

void moveTo(int target) {
  if (target < 1000) target = 1000;
  if (target > 2000) target = 2000;
  us = target;
  blade.writeMicroseconds(us);
  show();
}

void sweep() {
  for (int u = CLOSED_US; u <= OPEN_US; u += 10) {
    blade.writeMicroseconds(u);
    delay(40);
  }
  for (int u = OPEN_US; u >= CLOSED_US; u -= 10) {
    blade.writeMicroseconds(u);
    delay(40);
  }
  moveTo(us);
}

void handle(char *line) {
  if (!strcmp(line, "c")) {
    moveTo(CLOSED_US);
  } else if (!strcmp(line, "o")) {
    moveTo(OPEN_US);
  } else if (!strcmp(line, "z")) {
    moveTo(1500);                 // centre; the stop point of a 360 servo
  } else if (!strcmp(line, "+")) {
    moveTo(us + 10);
  } else if (!strcmp(line, "-")) {
    moveTo(us - 10);
  } else if (!strcmp(line, "s")) {
    sweep();
  } else if (!strcmp(line, "p")) {
    show();
  } else {
    int v = atoi(line);
    if (v >= 1000 && v <= 2000) {
      moveTo(v);
    } else {
      Serial.println("1000..2000 only");
    }
  }
}

void setup() {
  Serial.begin(115200);
  int idx = blade.attach(SERVO_PIN, 1000, 2000);
  Serial.print("servo on D");
  Serial.print(SERVO_PIN);
  Serial.print(" attach=");
  Serial.println(idx);            // 255 = INVALID_SERVO, no PWM channel
  delay(300);
  moveTo(us);
  Serial.println("c=closed  o=open  z=stop  +/-=nudge  s=sweep  p=print");
  Serial.println("a positional servo holds an angle; a 360 one keeps turning");
}

void loop() {
  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n' || c == '\r') {
      if (len > 0) {
        buf[len] = '\0';
        handle(buf);
        len = 0;
      }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = (char)c;
    } else {
      len = 0;   // overflow: drop the line
    }
  }
}
