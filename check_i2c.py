#!/usr/bin/env python3
import os
import sys

KNOWN = {
    0x62: 'SCD41 CO2/T/RH',
    0x59: 'SGP41 VOC/NOx',
    0x69: 'SPS30 PM sensor',
    0x76: 'BMP280/BME280 pressure',
    0x77: 'BMP280/BME280 pressure',
}

def main():
    bus_num = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    dev = '/dev/i2c-%d' % bus_num
    print('I2C device:', dev)
    if not os.path.exists(dev):
        print('ERROR: %s not found.' % dev)
        print('Enable I2C: sudo raspi-config  -> Interface Options -> I2C -> Enable')
        print('Then reboot: sudo reboot')
        return 2
    try:
        from smbus2 import SMBus
    except Exception as e:
        print('ERROR: smbus2 not installed:', e)
        print('Install: pip3 install smbus2')
        return 3
    found = []
    with SMBus(bus_num) as bus:
        for addr in range(0x03, 0x78):
            try:
                bus.read_byte(addr)
                found.append(addr)
            except Exception:
                pass
    if not found:
        print('No I2C devices found. Check wiring, power, SDA/SCL, pull-ups, bus number.')
        return 1
    print('Found:')
    for a in found:
        print('  0x%02X  %s' % (a, KNOWN.get(a, '?')))
    print('\nExpected for full station: 0x62, 0x59, 0x69, 0x76 or 0x77')
    return 0

if __name__ == '__main__':
    sys.exit(main())
