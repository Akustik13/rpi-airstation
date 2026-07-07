#!/usr/bin/env python3
"""Local launcher: starts the air-station UI directly from Python.

Examples:
  python3 run.py                 # Raspberry Pi / real sensors
  python3 run.py --demo          # PC test/demo
  python3 run.py --windowed --demo
  python3 run.py --fbdev /dev/fb1
"""
from main import main

if __name__ == '__main__':
    main()
