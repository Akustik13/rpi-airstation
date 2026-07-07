"""wx.py — чиста логіка метеостанції без залежностей від pygame:
одиниці, компенсація тиску, похідні індекси (AQI / IAQ / eCO₂),
фаза місяця, схід/захід сонця, прогноз за трендом тиску.

Модуль самодостатній (лише math/datetime), тож працює на Pi Zero
навіть без astral. Логіку сенсорів не чіпає.
"""
import math
from datetime import datetime, timedelta

# ─────────────────────────── Одиниці ──────────────────────────────────────────

def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0

def temp_convert(c, unit):
    if c is None:
        return None
    return c_to_f(c) if unit == 'f' else c

def temp_unit_label(unit):
    return '°F' if unit == 'f' else '°C'

def hpa_to_mmhg(h):
    return h * 0.7500616827

def pressure_convert(hpa, unit):
    if hpa is None:
        return None
    if unit == 'mmhg':
        return hpa_to_mmhg(hpa)
    if unit == 'kpa':
        return hpa / 10.0
    return hpa

def pressure_unit_label(unit):
    return {'mmhg': 'мм рт.ст.', 'kpa': 'kPa'}.get(unit, 'hPa')

def pressure_digits(unit):
    return 0 if unit == 'mmhg' else 1

def sea_level_pressure(station_hpa, altitude_m, temp_c=15.0):
    """Приведення станційного тиску до рівня моря (барометрична формула)."""
    if station_hpa is None:
        return None
    try:
        T = (temp_c if temp_c is not None else 15.0) + 273.15
        return station_hpa * math.pow(1.0 - (0.0065 * altitude_m) / (T + 0.0065 * altitude_m), -5.257)
    except Exception:
        return station_hpa

# ─────────────────────────── Похідні індекси ──────────────────────────────────

def _piecewise(cp, bp):
    """Лінійна інтерполяція за таблицею контрольних точок EPA."""
    if cp is None:
        return None
    for clow, chigh, ilow, ihigh in bp:
        if cp <= chigh:
            cp = max(cp, clow)
            return int(round((ihigh - ilow) / (chigh - clow) * (cp - clow) + ilow))
    return 500

_PM25_BP = [(0.0, 12.0, 0, 50), (12.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
            (55.5, 150.4, 151, 200), (150.5, 250.4, 201, 300), (250.5, 500.4, 301, 500)]
_PM10_BP = [(0, 54, 0, 50), (55, 154, 51, 100), (155, 254, 101, 150),
            (255, 354, 151, 200), (355, 424, 201, 300), (425, 604, 301, 500)]

def aqi_from_pm(pm25, pm10):
    """US EPA AQI: максимум із під-індексів PM2.5 та PM10. Це стандартний,
    цілком коректний індекс якості повітря за пилом."""
    vals = []
    a = _piecewise(pm25, _PM25_BP)
    b = _piecewise(pm10, _PM10_BP)
    if a is not None: vals.append(a)
    if b is not None: vals.append(b)
    return max(vals) if vals else None

def iaq_index(co2, voc_index, pm25):
    """Складений indoor-індекс 0..500 (більше = гірше): усереднення внесків
    CO₂, VOC Index і PM2.5. Це наближена оцінка, а не показник сертифікованого
    IAQ-сенсора."""
    parts = []
    if co2 is not None:
        parts.append(max(0.0, min(500.0, (co2 - 400.0) / 1600.0 * 500.0)))
    if voc_index is not None:
        parts.append(max(0.0, min(500.0, float(voc_index))))
    if pm25 is not None:
        parts.append(max(0.0, min(500.0, pm25 / 55.4 * 150.0)))
    if not parts:
        return None
    return int(round(sum(parts) / len(parts)))

def eco2_estimate(voc_index, co2=None):
    """Оцінка еквівалентного CO₂ за VOC-навантаженням (як у SGP-сенсорів).
    Це РОЗРАХУНКОВА оцінка, а не прямий вимір; реальний CO₂ дає SCD41."""
    if voc_index is None:
        return co2
    return int(round(400 + max(0.0, float(voc_index) - 100.0) * 8.0))

# ─────────────────────────── Фаза місяця ──────────────────────────────────────

_MOON_NAMES = ['New Moon', 'Waxing crescent', 'First quarter', 'Waxing gibbous',
               'Full Moon', 'Waning gibbous', 'Last quarter', 'Waning crescent']
_MOON_NAMES_UK = ['Новий місяць', 'Молодий', 'Перша чверть', 'Зростаючий',
                  'Повня', 'Спадний', 'Остання чверть', 'Старий']
SYNODIC = 29.530588853

def moon_phase(dt=None):
    """Повертає (age_days, illumination 0..1, index 0..7). Без залежностей."""
    if dt is None:
        dt = datetime.utcnow()
    ref = datetime(2000, 1, 6, 18, 14, 0)  # відома новина місяця
    days = (dt - ref).total_seconds() / 86400.0
    age = days % SYNODIC
    illum = (1.0 - math.cos(2.0 * math.pi * age / SYNODIC)) / 2.0
    idx = int((age / SYNODIC) * 8.0 + 0.5) % 8
    return age, illum, idx

def moon_name(idx, lang='uk'):
    return (_MOON_NAMES_UK if lang == 'uk' else _MOON_NAMES)[idx % 8]

# ─────────────────────────── Схід / захід сонця ───────────────────────────────

def sun_times(lat, lon, dt=None, tz_offset_hours=0.0):
    """NOAA-алгоритм. Повертає (sunrise, sunset) як datetime у ЛОКАЛЬНОМУ часі
    (за tz_offset_hours), або (None, None) для полярного дня/ночі."""
    if dt is None:
        dt = datetime.utcnow()
    N = dt.timetuple().tm_yday

    def _event(is_rise):
        zenith = 90.833
        lngHour = lon / 15.0
        t = N + ((6 if is_rise else 18) - lngHour) / 24.0
        M = (0.9856 * t) - 3.289
        L = M + (1.916 * math.sin(math.radians(M))) + (0.020 * math.sin(math.radians(2 * M))) + 282.634
        L %= 360
        RA = math.degrees(math.atan(0.91764 * math.tan(math.radians(L)))) % 360
        Lq = (math.floor(L / 90.0)) * 90.0
        RAq = (math.floor(RA / 90.0)) * 90.0
        RA = (RA + (Lq - RAq)) / 15.0
        sinDec = 0.39782 * math.sin(math.radians(L))
        cosDec = math.cos(math.asin(sinDec))
        cosH = (math.cos(math.radians(zenith)) - (sinDec * math.sin(math.radians(lat)))) / \
               (cosDec * math.cos(math.radians(lat)))
        if cosH > 1 or cosH < -1:
            return None
        H = (360 - math.degrees(math.acos(cosH))) if is_rise else math.degrees(math.acos(cosH))
        H /= 15.0
        T = H + RA - (0.06571 * t) - 6.622
        UT = (T - lngHour) % 24
        local = UT + tz_offset_hours
        local %= 24
        hh = int(local)
        mm = int(round((local - hh) * 60))
        if mm == 60:
            hh = (hh + 1) % 24; mm = 0
        return dt.replace(hour=hh, minute=mm, second=0, microsecond=0)

    try:
        return _event(True), _event(False)
    except Exception:
        return None, None

# ─────────────────────────── Прогноз за трендом тиску ─────────────────────────
#
# Класифікація за швидкістю зміни тиску (hPa/год) на останніх ~3 год + рівень.
# Це виправлена версія старого pogodaPres: там були пропущені діапазони
# (−5..−3 і 3..5 hPa нічого не показували) і крихка логіка min/max за індексами.

FORECAST = {
    'storm':     {'icon': 'storm',  'uk': 'Різке падіння — можлива буря/дощ',   'en': 'Sharp drop — storm/rain likely'},
    'rain':      {'icon': 'rain',   'uk': 'Тиск падає — очікується опади',       'en': 'Falling — rain expected'},
    'steady':    {'icon': 'cloud',  'uk': 'Тиск стабільний — без різких змін',   'en': 'Steady — no sharp change'},
    'improving': {'icon': 'partly', 'uk': 'Тиск росте — поліпшення погоди',      'en': 'Rising — improving'},
    'sunny':     {'icon': 'sun',    'uk': 'Швидке зростання — ясно і сухо',      'en': 'Rising fast — clear & dry'},
    'unknown':   {'icon': 'cloud',  'uk': 'Недостатньо даних для прогнозу',      'en': 'Not enough data yet'},
}

def forecast(series):
    """series = [(ts_seconds, hpa), …] за останні ~3 год (будь-який порядок за ts).
    Повертає dict: {code, icon, rate_hpa_per_hr, text_uk, text_en}."""
    pts = sorted((p for p in series if p[1] is not None), key=lambda p: p[0])
    if len(pts) < 2:
        f = FORECAST['unknown']; return {'code': 'unknown', 'icon': f['icon'], 'rate': 0.0,
                                         'text_uk': f['uk'], 'text_en': f['en']}
    t0, p0 = pts[0]
    t1, p1 = pts[-1]
    dt_hr = max((t1 - t0) / 3600.0, 1.0 / 60.0)
    rate = (p1 - p0) / dt_hr  # hPa/год, знак = напрям
    # база класифікації за швидкістю
    if rate <= -1.6:
        code = 'storm'
    elif rate <= -0.5:
        code = 'rain'
    elif rate < 0.5:
        code = 'steady'
    elif rate < 1.6:
        code = 'improving'
    else:
        code = 'sunny'
    # корекція за абсолютним рівнем (антициклон/циклон)
    if code == 'steady':
        if p1 >= 1022:
            code = 'improving'
        elif p1 <= 1000:
            code = 'rain'
    f = FORECAST[code]
    return {'code': code, 'icon': f['icon'], 'rate': round(rate, 2),
            'text_uk': f['uk'], 'text_en': f['en']}


# ─────────────────────────── Оцінка УФ (офлайн) ───────────────────────────────

def uv_estimate(dt, sunrise, sunset, season_peak=7.0):
    """Груба оцінка УФ-індексу без інтернету: синусоїда з піком у сонячний полудень,
    масштабована сезонним максимумом. Позначається як приблизна (*)."""
    if sunrise is None or sunset is None:
        return None
    try:
        srm = sunrise.hour * 60 + sunrise.minute
        ssm = sunset.hour * 60 + sunset.minute
        nowm = dt.hour * 60 + dt.minute
        if nowm <= srm or nowm >= ssm or ssm <= srm:
            return 0
        frac = (nowm - srm) / (ssm - srm)          # 0..1 частка дня
        val = math.sin(math.pi * frac) * season_peak
        return max(0, int(round(val)))
    except Exception:
        return None

# ─────────────────────────── Погодні коди WMO → іконка ────────────────────────

def wmo_icon(code):
    """WMO weather code (Open-Meteo) → наш код іконки sun/partly/cloud/rain/storm."""
    if code is None:
        return 'cloud'
    c = int(code)
    if c == 0:
        return 'sun'
    if c in (1, 2):
        return 'partly'
    if c in (3, 45, 48):
        return 'cloud'
    if c in (95, 96, 99):
        return 'storm'
    if c >= 51:
        return 'rain'
    return 'partly'


# ─────────────────────────── Астро: магнітні бурі (Kp) ────────────────────────

def kp_status(kp):
    """Kp-індекс (0..9) → (мітка, G-рівень, частка_0..1). Геомагнітні бурі
    за шкалою NOAA: Kp5=G1 … Kp9=G5."""
    if kp is None:
        return ('—', '', 0.0)
    try:
        k = float(kp)
    except Exception:
        return ('—', '', 0.0)
    frac = max(0.0, min(1.0, k / 9.0))
    if k < 4:
        return ('спокійно', '', frac)
    if k < 5:
        return ('неспокійно', '', frac)
    g = {5: 'G1', 6: 'G2', 7: 'G3', 8: 'G4'}.get(int(k), 'G5')
    return ('буря ' + g, g, frac)

# ─────────────────────────── Астро: ретроградність (таблиця 2026) ─────────────
# Наближені періоди на 2026 рік як (місяць,день)-діапазони. Легко редагувати.
# Це НЕ обчислення ефемерид, а вшита таблиця (як і домовлялись).
_RETRO_2026 = {
    'Меркурій': [((2, 26), (3, 20)), ((6, 29), (7, 23)), ((10, 24), (11, 13))],
    'Венера':   [((10, 3), (11, 13))],
    'Марс':     [],
    'Юпітер':   [((1, 1), (2, 4)), ((11, 15), (12, 31))],
    'Сатурн':   [((7, 13), (11, 28))],
}
_RETRO_EN = {'Меркурій': 'Mercury', 'Венера': 'Venus', 'Марс': 'Mars', 'Юпітер': 'Jupiter', 'Сатурн': 'Saturn'}

def retrograde(dt=None, lang='uk'):
    """Список планет у ретрограді на дату (за вшитою таблицею 2026)."""
    if dt is None:
        dt = datetime.now()
    md = (dt.month, dt.day)
    out = []
    for planet, ranges in _RETRO_2026.items():
        for (a, b) in ranges:
            if a <= md <= b:
                out.append(planet if lang == 'uk' else _RETRO_EN.get(planet, planet))
                break
    return out

# ─────────────────────────── Астро: порада дня ────────────────────────────────
_ADVICE_UK = [
    'Провітрюйте приміщення — свіже повітря бадьорить.',
    'Пийте більше води протягом дня.',
    'Зробіть коротку прогулянку на денному світлі.',
    'Пориньте у справу без поспіху — день сприяє зосередженню.',
    'Лягайте спати трохи раніше — відпочинок важливий.',
    'Розтягніться і подихайте глибоко кілька хвилин.',
    'Гарний день, щоб навести лад на робочому столі.',
]

def advice_of_day(dt=None, kp=None, uv=None, moon_idx=None, lang='uk'):
    if dt is None:
        dt = datetime.now()
    st = kp_status(kp)
    if st[1]:  # є геомагнітна буря
        return ('Магнітна буря — чутливим людям варто берегтися, менше стресу і кофеїну.'
                if lang == 'uk' else 'Geomagnetic storm — sensitive people take care, less stress and caffeine.')
    if uv is not None and uv >= 6:
        return ('Високий УФ — сонцезахист і головний убір, уникайте полуденного сонця.'
                if lang == 'uk' else 'High UV — use sunscreen and a hat, avoid midday sun.')
    if moon_idx == 4:
        return ('Повня — можливий неспокійний сон, не переїдайте на ніч.'
                if lang == 'uk' else 'Full moon — sleep may be restless, avoid heavy late meals.')
    return _ADVICE_UK[dt.timetuple().tm_yday % len(_ADVICE_UK)]
