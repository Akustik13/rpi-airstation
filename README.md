# RPi Zero AirStation v6

Changes:
- Cursor is visible; app no longer hides pointer.
- New tabbed menu: General, Sensors, Time, Touch.
- Touch calibration item launches `xinput_calibrator` outside the app.
- Sensor tech page: address, status, last read, last error.
- Time settings: network/NTP mode or manual date/time.
- Temperature source mapping remains selectable: BMP280 or SCD41.
- Manual restart buttons for SCD41/SPS30 and re-init all.

Recommended install path:

```bash
rm -rf /home/pi/AirStation
cp -r rpi_zero_airstation_v6 /home/pi/AirStation
cd /home/pi/AirStation
bash install.sh
python3 run.py --fullscreen
```

Touch calibration:

Menu -> Touch -> Calibrate touch

The app stops itself, launches `xinput_calibrator`, saves output into:

```text
/home/pi/airstation_logs/touch_calibration_YYYYMMDD_HHMMSS.txt
```

Copy the `Calibration` block into:

```text
/etc/X11/xorg.conf.d/99-calibration.conf
```

Then reboot.

The Elegoo 3.5 TFT manual says calibration is done with `DISPLAY=:0.0 xinput_calibrator`, then the calibration data is written to `/etc/X11/xorg.conf.d/99-calibration.conf` and Raspberry Pi is rebooted.
