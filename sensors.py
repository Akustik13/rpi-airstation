"""
sensors.py — robust Raspberry Pi sensor layer.

Works without SSH and without crashing if I2C is disabled.
Primary access is smbus2/raw I2C because it is reliable on old Raspberry Pi OS
with Python 3.7. Installer also installs vendor libraries where available.
"""

import time, struct, math, random, logging, threading
from typing import Optional

log = logging.getLogger(__name__)

# ── Registry ──────────────────────────────────────────────────────────────────
class SensorInfo:
    def __init__(self, key, name, addr, iface='I2C'):
        self.key       = key
        self.name      = name
        self.addr      = addr
        self.iface     = iface
        self.online    = False
        self.last_read = 0.0
        self.error_msg = ''

REGISTRY = {
    'scd41':  SensorInfo('scd41',  'SCD41',  '0x62'),
    'sgp41':  SensorInfo('sgp41',  'SGP41',  '0x59'),
    'sps30':  SensorInfo('sps30',  'SPS30',  '0x69'),
    'bmp280': SensorInfo('bmp280', 'BMP280', '0x76/77'),
}

STUB_MODE = False
_bus      = None
_bus_num  = 1
_i2c_error = ""

# smbus2 is NOT thread-safe. The app starts several background threads that
# can touch the I2C bus concurrently (the periodic Poller, manual "Restart
# SCD41/SPS30" buttons, and re-init from the settings menu). Without a lock,
# two threads interleaving their write/read calls produces exactly the
# symptoms reported: "Remote I/O error" and CRC mismatches on whichever
# sensor's transaction got interrupted mid-flight by another thread's command.
_i2c_lock = threading.Lock()

# ── I2C helpers ───────────────────────────────────────────────────────────────

def _get_bus(bus_num=1):
    global _bus, _bus_num, _i2c_error
    if _bus is None or _bus_num != bus_num:
        # Close previous bus if any.
        try:
            if _bus is not None:
                _bus.close()
        except Exception:
            pass
        dev = '/dev/i2c-%s' % bus_num
        try:
            import os
            if not os.path.exists(dev):
                raise IOError("%s not found. Enable I2C in raspi-config and reboot." % dev)
            from smbus2 import SMBus
            _bus = SMBus(bus_num)
            _bus_num = bus_num
            _i2c_error = ''
        except Exception as e:
            _i2c_error = str(e)
            raise
    return _bus

def _reopen_i2c_bus():
    """Force-close and reopen the SMBus handle.

    An OSError (e.g. Errno 121 Remote I/O error) from one sensor's
    transaction can leave the kernel I2C adapter's internal state slightly
    off for whichever sensor is touched next on the same bus — this matches
    the observed failure pattern where an SPS30 CRC glitch was immediately
    followed by SGP41 failing, even though SGP41's own command sequence was
    correct. Reopening the handle resets that state cleanly.
    """
    global _bus
    try:
        if _bus is not None:
            _bus.close()
    except Exception:
        pass
    _bus = None
    try:
        _get_bus(_bus_num)
    except Exception as e:
        log.error(f"I2C bus reopen failed: {e}")

def _write(addr, data):
    from smbus2 import i2c_msg
    _get_bus().i2c_rdwr(i2c_msg.write(addr, data))

def _read(addr, n):
    from smbus2 import i2c_msg
    m = i2c_msg.read(addr, n)
    _get_bus().i2c_rdwr(m)
    return bytes(m)

def _write_read(addr, cmd, n, delay=0.05):
    _write(addr, cmd)
    time.sleep(delay)
    return _read(addr, n)

def scan_i2c_bus(bus_num=1):
    found = []
    try:
        import os
        dev = '/dev/i2c-%s' % bus_num
        if not os.path.exists(dev):
            log.error('scan: %s missing. I2C disabled or wrong bus.' % dev)
            return [(-1, 'error')]
        from smbus2 import SMBus
        b = SMBus(bus_num)
        for addr in range(0x03, 0x78):
            try:
                b.read_byte(addr)
                found.append(addr)
            except Exception:
                pass
        b.close()
    except Exception as e:
        log.error('scan: %s' % e)
        return [(-1, 'error')]
    return found


# ══════════════════════════════════════════════════════════════════════════════
#  Sensirion CRC (used by SCD41, SGP41, SPS30)
# ══════════════════════════════════════════════════════════════════════════════

def _crc8(data):
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc

def _sensirion_read(addr, cmd, n_words, delay=0.05):
    """Send command (if non-empty), read n_words×3 bytes, verify CRC, return data bytes."""
    try:
        if cmd:
            _write(addr, cmd)
            time.sleep(delay)
        raw = _read(addr, n_words * 3)
        result = bytearray()
        for i in range(n_words):
            d = raw[i*3:i*3+2]
            crc = raw[i*3+2]
            if _crc8(d) != crc:
                log.warning(f"0x{addr:02X} CRC word {i}: got {crc:#04x} exp {_crc8(d):#04x}")
                return None
            result.extend(d)
        return bytes(result)
    except Exception as e:
        log.error(f"sensirion_read 0x{addr:02X}: {e}")
        return None

def _sensirion_write(addr, cmd, data_words=None):
    if data_words is None:
        data_words = []
    """Send command + data words each with CRC."""
    payload = list(cmd)
    for i in range(0, len(data_words), 2):
        word = data_words[i:i+2]
        payload.extend(word)
        payload.append(_crc8(bytes(word)))
    _write(addr, payload)


# ══════════════════════════════════════════════════════════════════════════════
#  SCD41  (0x62) — CO2, Temperature, Humidity
# ══════════════════════════════════════════════════════════════════════════════
SCD41 = 0x62
_scd41_last_reinit = 0.0
_scd41_fail_count = 0
_scd41_min_reinit_interval = 15.0

def _scd41_recover(reason=''):
    """Try to recover a stuck SCD41 without rebooting Raspberry Pi.
    Handles common [Errno 121] Remote I/O error after sensor hangs.
    Uses stop, soft reset, re-init, then start periodic measurement again.
    Rate limited so it does not block every read cycle.
    """
    global _scd41_last_reinit, _scd41_fail_count
    now = time.time()
    if now - _scd41_last_reinit < _scd41_min_reinit_interval:
        return False
    _scd41_last_reinit = now
    log.warning('SCD41 recovery start: %s' % reason)
    try:
        try:
            _write(SCD41, [0x3F, 0x86])  # stop periodic measurement
            time.sleep(0.6)
        except Exception:
            pass
        try:
            _write(SCD41, [0x36, 0xF6])  # wake up, useful if sensor entered sleep
            time.sleep(0.05)
        except Exception:
            pass
        try:
            _write(SCD41, [0x36, 0x46])  # reinit from EEPROM, not factory reset
            time.sleep(0.05)
        except Exception:
            pass
        ok = _init_scd41(_bus_num)
        if ok:
            _scd41_fail_count = 0
            log.info('SCD41 recovery OK')
        return ok
    except Exception as e:
        REGISTRY['scd41'].error_msg = str(e)
        log.warning('SCD41 recovery failed: %s' % e)
        return False

def _init_scd41(bus_num=1) -> bool:
    try:
        _get_bus(bus_num)
        # Stop periodic measurement — Section 3.6.3: must wait 500ms after
        try: _write(SCD41, [0x3F, 0x86]); time.sleep(0.6)
        except: pass

        # Reinit — Section 3.10.5: reloads settings from EEPROM, needs 30ms
        try: _write(SCD41, [0x36, 0x46]); time.sleep(0.05)
        except: pass

        # Read serial number to confirm alive — Section 3.10.2
        # send command, wait 1ms (50ms for slow bus), read 9 bytes
        _write(SCD41, [0x36, 0x82])
        time.sleep(0.05)
        raw = _read(SCD41, 9)
        if len(raw) < 9:
            raise RuntimeError("Serial read too short")
        # Verify CRCs
        for i in range(3):
            if _crc8(raw[i*3:i*3+2]) != raw[i*3+2]:
                raise RuntimeError(f"Serial CRC word {i}")
        w0 = struct.unpack('>H', raw[0:2])[0]
        w1 = struct.unpack('>H', raw[3:5])[0]
        w2 = struct.unpack('>H', raw[6:8])[0]
        log.info(f"SCD41 serial: {w0:04X}-{w1:04X}-{w2:04X}")

        # Start periodic measurement — Section 3.6.1: interval 5 seconds
        _write(SCD41, [0x21, 0xB1])
        time.sleep(0.1)

        REGISTRY['scd41'].online = True
        log.info("SCD41 OK")
        return True
    except Exception as e:
        REGISTRY['scd41'].error_msg = str(e)
        log.warning(f"SCD41 init: {e}")
        return False

def read_scd41():
    if REGISTRY['scd41'].online:
        try:
            # Step 1: check data ready (Section 3.9.2)
            # Least significant 11 bits of word[0] = 0 → not ready
            _write(SCD41, [0xE4, 0xB8])
            time.sleep(0.05)          # 10kHz bus needs more time
            raw_dr = _read(SCD41, 3)  # 1 word + CRC
            if len(raw_dr) < 3:
                return {}
            dr_val = struct.unpack('>H', raw_dr[0:2])[0]
            if (dr_val & 0x07FF) == 0:
                return {}             # data not ready yet

            # Step 2: send read_measurement command 0xEC05
            _write(SCD41, [0xEC, 0x05])
            time.sleep(0.05)          # datasheet: 1ms min, more for 10kHz

            # Step 3: read 9 bytes = 3 words × (2 data + 1 CRC)
            raw = _read(SCD41, 9)
            if len(raw) < 9:
                return {}

            # Verify CRC for each word
            for i in range(3):
                d2 = raw[i*3:i*3+2]
                crc_rx = raw[i*3+2]
                if _crc8(d2) != crc_rx:
                    log.warning(f"SCD41 read CRC word {i}")
                    return {}

            co2_raw = struct.unpack('>H', raw[0:2])[0]
            t_raw   = struct.unpack('>H', raw[3:5])[0]
            h_raw   = struct.unpack('>H', raw[6:8])[0]

            # Datasheet Section 3.6.2 conversions
            co2  = co2_raw
            temp = round(-45 + 175 * t_raw / 65535, 1)
            hum  = round(100 * h_raw / 65535, 1)

            REGISTRY['scd41'].last_read = time.time()
            return {'co2': co2, 'temp_scd': temp, 'hum_scd': hum}

        except Exception as e:
            global _scd41_fail_count
            _scd41_fail_count += 1
            REGISTRY['scd41'].error_msg = str(e)
            log.error(f"SCD41 read: {e}")
            REGISTRY['scd41'].online = False
            _scd41_recover(str(e))
    else:
        # Offline: periodically try to bring the sensor back.
        _scd41_recover('offline')
    if STUB_MODE:
        t = time.time()
        return {
            'co2':      int(600 + 200*abs(math.sin(t/1800)) + random.gauss(0,5)),
            'temp_scd': round(22.0 + 1.5*math.sin(t/7200) + random.gauss(0,.1), 1),
            'hum_scd':  round(50 + 10*math.sin(t/3600) + random.gauss(0,.3), 1),
        }
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  SGP41  (0x59) — VOC Index, NOx Index
# ══════════════════════════════════════════════════════════════════════════════
SGP41 = 0x59
_sgp41_conditioning = True
_sgp41_cond_end     = 0.0

# Official Sensirion gas-index algorithm (stateful adaptive filter).
# Raw SRAW_VOC/SRAW_NOX values are NOT proportional to an "index" — they need
# baseline tracking + adaptive lowpass + sigmoid scaling, which only this
# library implements correctly. A naive linear formula on raw SRAW values
# barely reacts to real air changes because the sensor's baseline sits in a
# narrow band and small deltas get crushed by a fixed offset/divisor.
_voc_algo = None
_nox_algo = None
try:
    from sensirion_gas_index_algorithm.voc_algorithm import VocAlgorithm
    from sensirion_gas_index_algorithm.nox_algorithm import NoxAlgorithm
    _voc_algo = VocAlgorithm()
    _nox_algo = NoxAlgorithm()
    _HAS_GAS_INDEX = True
except Exception as _e:
    _HAS_GAS_INDEX = False
    log.warning(
        "sensirion-gas-index-algorithm not installed — VOC/NOx index will be "
        "a crude approximation. Install with: "
        "pip3 install sensirion-gas-index-algorithm  (%s)" % _e)

# SGP41 humidity / temperature compensation.
# Sensirion commands 0x2612 and 0x2619 accept two 16-bit words:
#   RH ticks:   RH[%] * 65535 / 100      default 0x8000 = 50 %RH
#   Temp ticks: (T[°C] + 45) * 65535/175 default 0x6666 = 25 °C
# SCD41 is preferred as the compensation source because it measures humidity.
def _sgp41_comp_words(env=None):
    env = env or {}
    try:
        rh = env.get('hum_scd')
        if rh is None:
            rh = env.get('humidity')
        temp = env.get('temp_scd')
        if temp is None:
            temp = env.get('temperature')
        if rh is None or temp is None:
            raise ValueError('missing T/RH')
        rh = max(0.0, min(100.0, float(rh)))
        temp = max(-45.0, min(130.0, float(temp)))
        rh_ticks = int(round(rh * 65535.0 / 100.0))
        t_ticks  = int(round((temp + 45.0) * 65535.0 / 175.0))
    except Exception:
        rh_ticks = 0x8000   # 50 %RH
        t_ticks  = 0x6666   # 25 °C
    return [(rh_ticks >> 8) & 0xFF, rh_ticks & 0xFF,
            (t_ticks  >> 8) & 0xFF, t_ticks  & 0xFF]

def _sgp41_display_estimate(sraw_voc=None, sraw_nox=None):
    """Return display-friendly fallback indexes from valid SRAW values.

    The official Sensirion gas-index algorithm intentionally returns 0 during
    its initial blackout/warm-up period. That looked like "sensor works but no
    data" on the UI. SRAW values are real sensor data, so while the adaptive
    algorithm is warming up (or if the package is missing) we show a conservative
    estimate instead of a hard 0. After the algorithm starts producing >0 values,
    those official values are used.
    """
    out = {}
    if sraw_voc is not None:
        # Clean-air baseline is usually around 32768; VOC index normal baseline
        # is around 100. This is only for display during algorithm warm-up.
        out['voc_index'] = max(0, min(500, int(round(100 + (int(sraw_voc) - 32768) / 256.0))))
    if sraw_nox is not None:
        # NOx index clean baseline is near 1. Keep it low unless SRAW moves up.
        out['nox_index'] = max(0, min(500, int(round(1 + (int(sraw_nox) - 32768) / 512.0))))
    return out

def _sgp41_process_indexes(sraw_voc, sraw_nox=None):
    est = _sgp41_display_estimate(sraw_voc, sraw_nox)
    src = 'fallback'
    voc_idx = est.get('voc_index')
    nox_idx = est.get('nox_index')

    if _HAS_GAS_INDEX:
        try:
            v = int(_voc_algo.process(int(sraw_voc)))
            # v == 0 is normal during initial blackout; keep showing estimate.
            if v > 0:
                voc_idx = v
                src = 'official'
        except Exception as e:
            log.warning(f"SGP41 VOC algorithm failed: {e}")
        if sraw_nox is not None:
            try:
                n = int(_nox_algo.process(int(sraw_nox)))
                if n > 0:
                    nox_idx = n
                    src = 'official'
            except Exception as e:
                log.warning(f"SGP41 NOx algorithm failed: {e}")

    return voc_idx, nox_idx, src

def _init_sgp41(bus_num=1) -> bool:
    global _sgp41_conditioning, _sgp41_cond_end, _sgp41_fail_count
    try:
        _get_bus(bus_num)
        # Read serial to confirm alive
        d = _sensirion_read(SGP41, [0x36, 0x82], 3, delay=0.05)
        if d is None:
            raise RuntimeError("No response to serial read")
        log.info(f"SGP41 serial: {d.hex()}")
        # Start conditioning (10s warm-up)
        _sensirion_write(SGP41, [0x26, 0x12], _sgp41_comp_words())
        time.sleep(0.05)
        _sgp41_conditioning = True
        _sgp41_cond_end = time.time() + 10.0
        _sgp41_fail_count = 0
        REGISTRY['sgp41'].online = True
        log.info("SGP41 OK (conditioning 10s)")
        return True
    except Exception as e:
        REGISTRY['sgp41'].error_msg = str(e)
        log.warning(f"SGP41 init: {e}")
        return False

_sgp41_fail_count = 0

def read_sgp41(env=None):
    global _sgp41_conditioning, _sgp41_fail_count
    if REGISTRY['sgp41'].online:
        try:
            comp_words = _sgp41_comp_words(env)

            if _sgp41_conditioning:
                if time.time() < _sgp41_cond_end:
                    # Still conditioning — must call the conditioning command
                    # repeatedly per datasheet, and it DOES return SRAW_VOC
                    # which should be fed to the VOC algorithm so the
                    # baseline starts adapting immediately instead of being
                    # thrown away.
                    try:
                        _sensirion_write(SGP41, [0x26, 0x12], comp_words)
                        time.sleep(0.06)
                        raw = _read(SGP41, 3)   # only VOC word during conditioning
                        if len(raw) >= 3 and _crc8(raw[0:2]) == raw[2]:
                            sraw_voc = struct.unpack('>H', raw[0:2])[0]
                            voc_idx, _, src = _sgp41_process_indexes(sraw_voc, None)
                            REGISTRY['sgp41'].last_read = time.time()
                            REGISTRY['sgp41'].error_msg = ''
                            _sgp41_fail_count = 0
                            log.debug(f"SGP41 conditioning… SRAW_VOC={sraw_voc} VOC={voc_idx}({src}) "
                                      f"({_sgp41_cond_end - time.time():.1f}s left)")
                            return {
                                'voc_index': voc_idx,
                                'sraw_voc': sraw_voc,
                                'sgp41_state': 'conditioning',
                                'sgp41_algo': src,
                            }
                        else:
                            log.warning("SGP41 conditioning: bad/short read "
                                        f"raw={raw.hex() if raw else None}")
                    except OSError as e:
                        # Real bus-level error during conditioning — reopen
                        # the bus handle so it doesn't poison subsequent
                        # sensor reads (this is what caused the cascading
                        # SPS30 -> SGP41 failures seen in the logs).
                        log.error(f"SGP41 conditioning I2C error: {e} — reopening bus")
                        REGISTRY['sgp41'].error_msg = f"conditioning: {e}"
                        _reopen_i2c_bus()
                    except Exception as e:
                        log.error(f"SGP41 conditioning write/read failed: {e}")
                        REGISTRY['sgp41'].error_msg = f"conditioning: {e}"
                    return {}
                else:
                    _sgp41_conditioning = False
                    log.info("SGP41 conditioning done — starting normal reads")

            # Measure raw signals: cmd 0x2619 — returns SRAW_VOC then SRAW_NOX,
            # each as 2 data bytes + 1 CRC byte = 6 bytes total.
            _sensirion_write(SGP41, [0x26, 0x19], comp_words)
            time.sleep(0.06)  # SGP41 needs ~50ms to measure

            raw = _read(SGP41, 6)
            if len(raw) < 6:
                log.warning(f"SGP41 measure: short read ({len(raw)} bytes)")
                return {}

            w_voc, c_voc = raw[0:2], raw[2]
            w_nox, c_nox = raw[3:5], raw[5]
            if _crc8(w_voc) != c_voc or _crc8(w_nox) != c_nox:
                log.warning(f"SGP41 read: CRC mismatch raw={raw.hex()}")
                return {}

            sraw_voc = struct.unpack('>H', w_voc)[0]
            sraw_nox = struct.unpack('>H', w_nox)[0]
            REGISTRY['sgp41'].last_read = time.time()
            REGISTRY['sgp41'].error_msg = ''   # clear any stale error on success
            _sgp41_fail_count = 0              # reset consecutive-failure counter

            voc_idx, nox_idx, src = _sgp41_process_indexes(sraw_voc, sraw_nox)
            log.debug(f"SGP41 SRAW_VOC={sraw_voc} SRAW_NOX={sraw_nox} -> VOC={voc_idx} NOx={nox_idx} ({src})")

            return {
                'voc_index': voc_idx, 'nox_index': nox_idx,
                'sraw_voc': sraw_voc, 'sraw_nox': sraw_nox,
                'sgp41_state': 'normal', 'sgp41_algo': src,
            }
        except OSError as e:
            # Errno 121 (Remote I/O error) and similar bus-level errors are
            # often transient — caused by a glitch on a previous sensor's
            # transaction (e.g. SPS30) rather than SGP41 itself being broken.
            # Reopen the bus handle and only mark the sensor offline after
            # several consecutive failures, instead of permanently disabling
            # it on the very first hiccup. This was why SGP41 "never worked
            # again" in the logs — one transient error latched online=False
            # forever, requiring a manual restart from the menu.
            _sgp41_fail_count += 1
            log.error(f"SGP41 read I2C error ({_sgp41_fail_count}/5): {e} — reopening bus")
            REGISTRY['sgp41'].error_msg = str(e)
            _reopen_i2c_bus()
            if _sgp41_fail_count >= 5:
                REGISTRY['sgp41'].online = False
                log.error("SGP41 marked offline after 5 consecutive I2C errors")
        except Exception as e:
            log.error(f"SGP41 read: {e}")
            REGISTRY['sgp41'].error_msg = str(e)
            _sgp41_fail_count += 1
            if _sgp41_fail_count >= 5:
                REGISTRY['sgp41'].online = False
    if STUB_MODE:
        t = time.time()
        return {
            'voc_index': int(80 + 60*abs(math.sin(t/2400)) + random.gauss(0,2)),
            'nox_index': int(2  +  8*abs(math.sin(t/3000)) + random.gauss(0,.2)),
        }
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  SPS30  (0x69) — PM1.0 PM2.5 PM4.0 PM10
# ══════════════════════════════════════════════════════════════════════════════
SPS30 = 0x69
_sps30_started = False

def _init_sps30(bus_num=1) -> bool:
    global _sps30_started
    try:
        _get_bus(bus_num)
        # Wake up — on a 10kHz bus (used here because of long wiring) the
        # sensor's I2C peripheral needs noticeably more settle time after
        # wake than the ~5ms typical at 100kHz. Short fixed sleeps here were
        # the most likely cause of the "CRC word 0: got 0xff" garbage seen
        # right at startup — 0xff/0xff is the classic symptom of a read that
        # started before the slave was actually ready to drive the bus.
        try: _write(SPS30, [0x11, 0x13]); time.sleep(0.3)
        except: pass
        # Stop measurement (if one was left running from a previous session)
        try: _write(SPS30, [0x01, 0x04]); time.sleep(0.2)
        except: pass
        # Start measurement, float output: cmd=0x0010, arg=0x0300+CRC
        arg = [0x03, 0x00]
        _write(SPS30, [0x00, 0x10, 0x03, 0x00, _crc8(bytes(arg))])
        # Datasheet: fan needs to spin up before the first sample is valid.
        # Give it real time instead of declaring "online" the instant the
        # start command was accepted — the first data-ready poll otherwise
        # lands while the sensor is still mid-spin-up.
        time.sleep(0.5)
        _sps30_started = True
        REGISTRY['sps30'].online = True
        log.info("SPS30 OK")
        return True
    except Exception as e:
        REGISTRY['sps30'].error_msg = str(e)
        log.warning(f"SPS30 init: {e}")
        return False

def read_sps30():
    global _sps30_started
    if REGISTRY['sps30'].online and _sps30_started:
        try:
            # Check data-ready: cmd 0x0202. Delay bumped for the 10kHz bus —
            # the sensor needs more time to prepare its response at this
            # clock speed than the previous 10ms allowed.
            dr = _sensirion_read(SPS30, [0x02, 0x02], 1, delay=0.05)
            if dr is None:
                return {}
            if struct.unpack('>H', dr)[0] & 0x0001 == 0:
                return {}   # no new data yet

            # Read values: cmd 0x0300 → 10 floats = 20 words = 60 bytes
            d = _sensirion_read(SPS30, [0x03, 0x00], 20, delay=0.05)
            if d and len(d) >= 40:
                floats = struct.unpack('>10f', d[:40])
                keys   = ['pm1_0','pm2_5','pm4_0','pm10',
                          'pm0_5_n','pm1_0_n','pm2_5_n','pm4_0_n','pm10_n','typ_ps']
                REGISTRY['sps30'].last_read = time.time()
                REGISTRY['sps30'].error_msg = ''
                return {k: round(float(v), 2) for k,v in zip(keys, floats)}
        except OSError as e:
            # Errno 121 (Remote I/O error) is a real bus-level NACK/timeout,
            # not just bad data. Reopening the SMBus handle here resets the
            # kernel I2C adapter's internal state — without this, a failed
            # transaction on SPS30 can leave the bus in a state that then
            # makes the *next* sensor touched (SGP41 in the logs) also fail,
            # even though SGP41 itself never did anything wrong.
            log.error(f"SPS30 read I2C error: {e} — reopening bus")
            REGISTRY['sps30'].error_msg = str(e)
            _reopen_i2c_bus()
        except Exception as e:
            log.error(f"SPS30 read: {e}")
            REGISTRY['sps30'].error_msg = str(e)
            # Try restart
            try:
                _write(SPS30, [0x00, 0x10, 0x03, 0x00, _crc8(bytes([0x03,0x00]))])
            except: pass
    if STUB_MODE:
        t = time.time()
        b = 4 + 3*abs(math.sin(t/3600))
        return {
            'pm1_0':   round(b*0.4  + random.gauss(0,.1), 1),
            'pm2_5':   round(b*0.7  + random.gauss(0,.2), 1),
            'pm4_0':   round(b*0.9  + random.gauss(0,.2), 1),
            'pm10':    round(b       + random.gauss(0,.3), 1),
            'pm0_5_n': round(280    + random.gauss(0,10), 0),
            'pm1_0_n': round(180    + random.gauss(0,8),  0),
            'pm2_5_n': round(90     + random.gauss(0,5),  0),
            'pm4_0_n': round(45     + random.gauss(0,3),  0),
            'pm10_n':  round(18     + random.gauss(0,2),  0),
            'typ_ps':  round(0.5    + random.gauss(0,.02),2),
        }
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  BMP280  (0x76 or 0x77) — Temperature, Pressure
# ══════════════════════════════════════════════════════════════════════════════
_bmp_addr   = None
_bmp_cal    = {}

def _bmp280_read_calibration(addr):
    b = _get_bus()
    cal = {}
    # T: 0x88..0x8D
    raw = b.read_i2c_block_data(addr, 0x88, 6)
    cal['T1'] = struct.unpack_from('<H', bytes(raw), 0)[0]
    cal['T2'] = struct.unpack_from('<h', bytes(raw), 2)[0]
    cal['T3'] = struct.unpack_from('<h', bytes(raw), 4)[0]
    # P: 0x8E..0x9F
    raw = b.read_i2c_block_data(addr, 0x8E, 18)
    cal['P1'] = struct.unpack_from('<H', bytes(raw), 0)[0]
    for i, key in enumerate(['P2','P3','P4','P5','P6','P7','P8','P9']):
        cal[key] = struct.unpack_from('<h', bytes(raw), 2+i*2)[0]
    return cal

def _bmp280_compensate(adc_T, adc_P, cal):
    # Temperature
    var1 = (adc_T/16384.0 - cal['T1']/1024.0) * cal['T2']
    var2 = (adc_T/131072.0 - cal['T1']/8192.0)**2 * cal['T3']
    t_fine = var1 + var2
    T = t_fine / 5120.0

    # Pressure
    var1 = t_fine/2.0 - 64000.0
    var2 = var1 * var1 * cal['P6'] / 32768.0
    var2 = var2 + var1 * cal['P5'] * 2.0
    var2 = var2/4.0 + cal['P4'] * 65536.0
    var1 = (cal['P3']*var1*var1/524288.0 + cal['P2']*var1) / 524288.0
    var1 = (1.0 + var1/32768.0) * cal['P1']
    if var1 == 0: return T, 0
    p = 1048576.0 - adc_P
    p = ((p - var2/4096.0) * 6250.0) / var1
    var1 = cal['P9'] * p * p / 2147483648.0
    var2 = p * cal['P8'] / 32768.0
    p = p + (var1 + var2 + cal['P7']) / 16.0
    return round(T, 1), round(p/100.0, 1)

def _init_bmp280(bus_num=1) -> bool:
    global _bmp_addr, _bmp_cal
    _get_bus(bus_num)
    for addr in [0x76, 0x77]:
        try:
            chip_id = _get_bus().read_byte_data(addr, 0xD0)
            if chip_id not in [0x58, 0x60]:  # 0x58=BMP280, 0x60=BME280
                log.warning(f"BMP280 0x{addr:02X}: unexpected chip_id {chip_id:#04x}")
            # Force normal mode, osrs_t=2, osrs_p=5, normal mode
            _get_bus().write_byte_data(addr, 0xF4, 0xB7)
            time.sleep(0.1)
            _bmp_cal  = _bmp280_read_calibration(addr)
            _bmp_addr = addr
            REGISTRY['bmp280'].online = True
            REGISTRY['bmp280'].addr   = f'0x{addr:02X}'
            log.info(f"BMP280 found at 0x{addr:02X} chip_id={chip_id:#04x}")
            return True
        except Exception as e:
            log.debug(f"BMP280 0x{addr:02X}: {e}")
    REGISTRY['bmp280'].error_msg = 'Not found on 0x76 or 0x77'
    log.warning("BMP280 not found")
    return False

def read_bmp280():
    if REGISTRY['bmp280'].online and _bmp_addr:
        try:
            raw = _get_bus().read_i2c_block_data(_bmp_addr, 0xF7, 6)
            adc_P = (raw[0]<<12 | raw[1]<<4 | raw[2]>>4)
            adc_T = (raw[3]<<12 | raw[4]<<4 | raw[5]>>4)
            T, P = _bmp280_compensate(adc_T, adc_P, _bmp_cal)
            REGISTRY['bmp280'].last_read = time.time()
            return {'temp_bmp': T, 'pressure': P}
        except Exception as e:
            log.error(f"BMP280 read: {e}")
            REGISTRY['bmp280'].online = False
    if STUB_MODE:
        t = time.time()
        return {
            'temp_bmp': round(21.5 + 2*math.sin(t/3600) + random.gauss(0,.05), 1),
            'pressure': round(1013.25 + 3*math.sin(t/7200) + random.gauss(0,.05), 1),
        }
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  Source mapping  (configurable: which sensor feeds each display slot)
# ══════════════════════════════════════════════════════════════════════════════
# Default mapping — can be changed in settings
SOURCE_MAP = {
    'temperature': 'bmp280',   # 'scd41' or 'bmp280'
    'humidity':    'scd41',   # 'scd41' only
    'pressure':    'bmp280',  # 'bmp280' only
    'co2':         'scd41',
}

def get_temperature(data):
    src = SOURCE_MAP.get('temperature', 'scd41')
    if src == 'bmp280': return data.get('temp_bmp')
    if src == 'scd41':  return data.get('temp_scd')
    # Fallback: try both
    return data.get('temp_scd') or data.get('temp_bmp')

def get_humidity(data):
    return data.get('hum_scd')

def get_pressure(data):
    return data.get('pressure')


# ══════════════════════════════════════════════════════════════════════════════
#  Init all
# ══════════════════════════════════════════════════════════════════════════════

def init_all(bus_num=1, on_progress=None):
    global STUB_MODE, _bus, _bus_num, _i2c_error
    with _i2c_lock:
        _bus = None   # reset bus for new bus_num
        _bus_num = bus_num
        results = {}

        # Fast and clear diagnostic: if /dev/i2c-1 is missing, all sensor
        # drivers would print the same confusing error. Do it once and
        # switch to demo.
        try:
            _get_bus(bus_num)
        except Exception as e:
            _i2c_error = str(e)
            for k in REGISTRY:
                REGISTRY[k].online = False
                REGISTRY[k].error_msg = _i2c_error
                results[k] = False
            STUB_MODE = True
            log.warning('I2C unavailable: %s' % _i2c_error)
            return results
        order = [
            ('scd41',  _init_scd41),
            ('sgp41',  _init_sgp41),
            ('sps30',  _init_sps30),
            ('bmp280', _init_bmp280),
        ]
        for key, fn in order:
            if on_progress: on_progress(key)
            try:   results[key] = fn(bus_num)
            except Exception as e:
                results[key] = False
                log.error(f"{key}: {e}")
            time.sleep(0.03)  # let the 10kHz bus settle before next address

        STUB_MODE = not any(results.values())
        if STUB_MODE:
            log.warning("STUB mode — no sensors")
            for k in REGISTRY: REGISTRY[k].online = True
        else:
            ok   = [k for k,v in results.items() if v]
            fail = [k for k,v in results.items() if not v]
            log.info(f"Online: {ok}")
            if fail: log.warning(f"Offline: {fail}")
        return results


# ── Lock-protected single-sensor restart helpers ──────────────────────────────
# Used by the menu's "Restart SCD41" / "Restart SPS30" buttons. These run in
# their own background thread on a button press, so without the same
# _i2c_lock used by read_all()/init_all(), a restart can interleave with an
# in-flight Poller read and produce the "Remote I/O error" / CRC mismatch
# pattern seen in the logs.
def restart_scd41(reason='manual'):
    with _i2c_lock:
        return _scd41_recover(reason)

def restart_sps30(bus_num=1):
    with _i2c_lock:
        return _init_sps30(bus_num)


def apply_source_map(data):
    """Add display fields according to SOURCE_MAP.
    Raw fields stay available: temp_scd, temp_bmp, hum_scd, pressure.
    Display uses: temperature, humidity, pressure.
    """
    data['temperature'] = get_temperature(data)
    data['humidity'] = get_humidity(data)
    data['pressure'] = get_pressure(data)
    return data

def read_all():
    # Locked because the Poller thread and UI-triggered actions (Restart
    # SCD41/SPS30 buttons, settings re-init) can call into the I2C layer
    # concurrently. smbus2 is not thread-safe — without this lock two
    # interleaved transactions produce exactly the symptoms seen in the
    # logs: "Remote I/O error" and CRC mismatches on whichever sensor's
    # write/read got cut into by the other thread.
    #
    # The small time.sleep() calls between sensors below were added after
    # isolated testing showed SGP41 is 100% reliable (80/80 reads) when
    # nothing else touches the bus, but fails repeatedly as part of this
    # back-to-back sweep. On the 10kHz bus this Pi runs (slowed down for
    # long wiring), switching the I2C address from one sensor to the next
    # with zero gap doesn't give the bus enough time to fully settle —
    # the next START condition can land before the previous slave has
    # truly released SDA/SCL, which produces exactly the "Remote I/O
    # error" / CRC-mismatch symptoms seen, on whichever sensor happens to
    # be addressed right after another.
    with _i2c_lock:
        data = {'ts': time.time()}
        data.update(read_scd41());  time.sleep(0.03)
        data.update(read_sgp41(data)); time.sleep(0.03)
        data.update(read_sps30());  time.sleep(0.03)
        data.update(read_bmp280())
        apply_source_map(data)
        return data


class Poller:
    def __init__(self, interval=5.0):
        self.interval = interval
        self._cb      = None
        self._stop    = threading.Event()

    def set_callback(self, cb): self._cb = cb

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self): self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                data = read_all()
                if self._cb: self._cb(data)
            except Exception as e:
                log.error(f"Poller: {e}")
            self._stop.wait(self.interval)
