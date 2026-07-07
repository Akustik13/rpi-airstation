#!/bin/bash
set -e
export DISPLAY=${DISPLAY:-:0.0}
export XAUTHORITY=${XAUTHORITY:-/home/pi/.Xauthority}

# Stop auto service so AirStation does not catch touches during calibration.
sudo systemctl stop airstation.service 2>/dev/null || true
pkill -f 'python3 .*run.py' 2>/dev/null || true
sleep 1

# Make cursor visible if possible.
xsetroot -cursor_name left_ptr 2>/dev/null || true

if ! command -v xinput_calibrator >/dev/null 2>&1; then
  echo "xinput_calibrator not found. Trying apt install..."
  sudo apt-get update
  sudo apt-get install -y xinput-calibrator
fi

mkdir -p /home/pi/airstation_logs
OUT=/home/pi/airstation_logs/touch_calibration_$(date +%Y%m%d_%H%M%S).txt

echo "Touch the red crosses with stylus. Output will be saved to $OUT"
xinput_calibrator | tee "$OUT"

echo ""
echo "Now copy Calibration block from $OUT to:"
echo "  /etc/X11/xorg.conf.d/99-calibration.conf"
echo "Then reboot: sudo reboot"
read -p "Press Enter to restart AirStation..." dummy
sudo systemctl start airstation.service 2>/dev/null || true
