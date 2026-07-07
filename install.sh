#!/bin/bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${SUDO_USER:-$(whoami)}"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
PYTHON="/usr/bin/python3"
DESKTOP_DIR="$APP_HOME/Desktop"
AUTOSTART_DIR="$APP_HOME/.config/autostart"

if [ "$APP_USER" = "root" ]; then
  APP_USER="pi"
  APP_HOME="/home/pi"
fi

echo "Installing AirStation dependencies..."
echo "App dir:  $APP_DIR"
echo "App user: $APP_USER"
echo "Home:     $APP_HOME"

sudo apt-get update
sudo apt-get install -y \
  python3-pip \
  python3-pygame \
  python3-smbus \
  i2c-tools \
  lxterminal \
  unclutter \
  python3-venv

# Install Python packages for this user. --break-system-packages is needed on Raspberry Pi OS Bookworm/Trixie.
sudo -u "$APP_USER" $PYTHON -m pip install --user --break-system-packages -r "$APP_DIR/requirements.txt" || \
sudo -u "$APP_USER" $PYTHON -m pip install --user -r "$APP_DIR/requirements.txt"

# Enable I2C if raspi-config is available.
sudo raspi-config nonint do_i2c 0 2>/dev/null || true

chmod +x "$APP_DIR/run.py" "$APP_DIR/start_airstation_fullscreen.sh" 2>/dev/null || true

# Desktop autostart is used because the app is graphical and must start inside the logged-in desktop session.
sudo -u "$APP_USER" mkdir -p "$DESKTOP_DIR" "$AUTOSTART_DIR"
cat > /tmp/AirStation.desktop <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=AirStation
Comment=Start AirStation fullscreen
Exec=$PYTHON $APP_DIR/run.py --fullscreen
Path=$APP_DIR
Icon=utilities-terminal
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
EOF
sudo cp /tmp/AirStation.desktop "$DESKTOP_DIR/AirStation.desktop"
sudo cp /tmp/AirStation.desktop "$AUTOSTART_DIR/AirStation.desktop"
sudo chown "$APP_USER:$APP_USER" "$DESKTOP_DIR/AirStation.desktop" "$AUTOSTART_DIR/AirStation.desktop"
sudo chmod +x "$DESKTOP_DIR/AirStation.desktop" "$AUTOSTART_DIR/AirStation.desktop"

# Keep a systemd service file for manual diagnostics, but do not rely on it for GUI autostart.
sudo tee /etc/systemd/system/airstation.service >/dev/null <<EOF2
[Unit]
Description=AirStation fullscreen app
After=graphical.target display-manager.service
Wants=graphical.target display-manager.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=DISPLAY=:0
Environment=XAUTHORITY=$APP_HOME/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=SDL_VIDEO_WINDOW_POS=0,0
ExecStart=$PYTHON $APP_DIR/run.py --fullscreen
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
EOF2

sudo systemctl daemon-reload
# Avoid double start: graphical autostart starts the app after desktop login.
sudo systemctl disable airstation.service >/dev/null 2>&1 || true
sudo systemctl stop airstation.service >/dev/null 2>&1 || true

echo "Done. Desktop shortcut and desktop autostart created."
echo "Manual start: $PYTHON $APP_DIR/run.py --fullscreen"
echo "Reboot recommended: sudo reboot"
