#!/usr/bin/env bash
# Host-side checks for the .ino — no Arduino toolchain required.
#   1. syntax/type check with -Wall -Wextra
#   2. behaviour test of the mood state machine with a fake clock + serial
set -euo pipefail

cd "$(dirname "$0")"

CXX="${CXX:-g++}"
FLAGS=(-std=gnu++17 -Wall -Wextra -I stubs)

echo "== syntax check =="
"$CXX" "${FLAGS[@]}" -fsyntax-only -x c++ ../ambient_light/ambient_light.ino
echo "ok"

echo
echo "== state machine behaviour =="
"$CXX" "${FLAGS[@]}" -o /tmp/ambient_light_state_test test_state.cpp
/tmp/ambient_light_state_test
