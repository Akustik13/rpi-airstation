#!/bin/bash
cd /home/pi/AirStation
export DISPLAY=:0
export XAUTHORITY=/home/pi/.Xauthority
export SDL_VIDEO_WINDOW_POS=0,0
exec python3 run.py --fullscreen
