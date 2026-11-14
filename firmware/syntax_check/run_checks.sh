#!/usr/bin/env bash
# Host-side checks for the .ino — no Arduino toolchain required.
#   1. syntax/type check with -Wall -Wextra, blades off (the default) and on
#   2. syntax check of the standalone servo test sketch
#   3. behaviour test of firmware v2.2.1 with a fake clock + serial + servos
set -euo pipefail

cd "$(dirname "$0")"

CXX="${CXX:-g++}"
FLAGS=(-std=gnu++17 -Wall -Wextra -I stubs)

echo "== syntax check (blades off, default) =="
"$CXX" "${FLAGS[@]}" -fsyntax-only -x c++ ../ambient_light/ambient_light.ino
echo "ok"

echo
echo "== syntax check (blades on) =="
"$CXX" "${FLAGS[@]}" -DENABLE_SERVO=1 -fsyntax-only -x c++ ../ambient_light/ambient_light.ino
echo "ok"

echo
echo "== servo_test syntax check =="
"$CXX" "${FLAGS[@]}" -fsyntax-only -x c++ ../servo_test/servo_test.ino
echo "ok"

echo
echo "== state machine behaviour (blades on) =="
"$CXX" "${FLAGS[@]}" -DENABLE_SERVO=1 -o /tmp/ambient_light_state_test test_state.cpp
/tmp/ambient_light_state_test
