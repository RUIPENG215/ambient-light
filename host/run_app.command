#!/bin/bash
# Starts Ambient Lamp from Terminal. Double-clicking Ambient Lamp.app runs this.
# (macOS does not let an unsigned app read ~/Documents or use the microphone on
# its own; Terminal already has both permissions, so the app goes through it.)
cd "$(dirname "$0")" || exit 1
mkdir -p logs
clear
echo "Ambient Lamp is running."
echo "The glowing ring is in the bottom-right corner of the screen."
echo "Quit with the x in its panel, or close this window."
echo
.venv/bin/python3 lamp_app.py 2>&1 | tee -a logs/app.log
echo
echo "Ambient Lamp has stopped. You can close this window."
