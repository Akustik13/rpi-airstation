"""net.py — інтернет для метеостанції: перевірка з'єднання + підтяг даних
з Open-Meteo (безкоштовно, без ключа): вітер, УФ-індекс, прогноз на тиждень.

Все у фоновому потоці з коротким таймаутом; якщо мережі немає — ONLINE=False,
дані лишаються None, а UI показує «—» або офлайн-оцінку. Логіку сенсорів не чіпає.
"""
import threading, time, json, socket
from urllib.request import urlopen, Request

ONLINE = [False]
_LOCK = threading.Lock()
DATA = {'wind_kmh': None, 'wind_dir': None, 'uv': None, 'daily': [], 'kp': None,
        'kp_hist': [], 'kp_days': [], 'ts': 0.0, 'kp_ts': 0.0, 'loading': False}
_started = [False]
_wake = threading.Event()

def is_online():
    return ONLINE[0]

def get():
    with _LOCK:
        return dict(DATA)

def force_refresh():
    """Примусово розбудити фоновий потік для негайного стягування даних."""
    with _LOCK:
        DATA['loading'] = True
    _wake.set()

def _check_socket():
    # Порт 443 (HTTPS) — практично завжди відкритий за наявності інтернету.
    # (порт 53 часто блокують, тож ним не користуємось.)
    for host in (('1.1.1.1', 443), ('8.8.8.8', 443), ('services.swpc.noaa.gov', 443)):
        try:
            s = socket.create_connection(host, timeout=4); s.close(); return True
        except Exception:
            continue
    return False

def _fetch(lat, lon):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=wind_speed_10m,wind_direction_10m,uv_index"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,uv_index_max"
           "&timezone=auto&forecast_days=7&wind_speed_unit=kmh")
    req = Request(url, headers={'User-Agent': 'airstation/1.0'})
    with urlopen(req, timeout=6) as r:
        return json.load(r)

def _row_kp(row):
    """Витягує (time_str, kp) з рядка NOAA — байдуже, це dict чи list."""
    try:
        if isinstance(row, dict):
            t = row.get('time_tag') or row.get('time')
            kp = row.get('Kp', row.get('kp', row.get('kp_index')))
            return t, (None if kp is None else float(kp))
        if isinstance(row, (list, tuple)):
            return row[0], float(row[1])   # рядок-заголовок дасть ValueError → пропуститься
    except Exception:
        return None, None
    return None, None

def _fetch_kp():
    """Останній планетарний Kp-індекс з NOAA SWPC (безкоштовно, без ключа)."""
    url = 'https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json'
    with urlopen(Request(url, headers=_UA), timeout=6) as r:
        rows = json.load(r)
    for row in reversed(rows):
        _t, kp = _row_kp(row)
        if kp is not None:
            return kp
    return None

def _parse_ts(s):
    import calendar
    if not s:
        return None
    s = s.split('.')[0].replace('T', ' ')
    try:
        return calendar.timegm(time.strptime(s, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return None

def _fetch_kp_hist():
    """Останні ~8 значень Kp по 3 год (доба): список (ts, kp)."""
    url = 'https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json'
    with urlopen(Request(url, headers=_UA), timeout=6) as r:
        rows = json.load(r)
    out = []
    for row in rows:
        t, kp = _row_kp(row)
        if kp is None:
            continue
        ts = _parse_ts(t)
        if ts is not None:
            out.append((ts, kp))
    return out[-8:]

def _fetch_kp_forecast():
    """3-денний прогноз Kp з NOAA: список днів [{'date':'YYYY-MM-DD','max':kp}]."""
    url = 'https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json'
    with urlopen(Request(url, headers=_UA), timeout=6) as r:
        rows = json.load(r)
    today = time.strftime('%Y-%m-%d', time.gmtime())
    perday = {}
    for row in rows:
        t, kp = _row_kp(row)
        if kp is None or not t:
            continue
        day = t.split('T')[0].split(' ')[0]
        if day < today:
            continue
        perday[day] = max(perday.get(day, 0), kp)
    days = sorted(perday.keys())
    return [{'date': d, 'max': perday[d]} for d in days[:3]]

def _worker(get_latlon):
    while True:
        on = _check_socket()
        ONLINE[0] = on
        got_any = False
        if on:
            try:
                lat, lon = get_latlon()
                j = _fetch(lat, lon)
                cur = j.get('current', {}) or {}
                d = j.get('daily', {}) or {}
                codes = d.get('weather_code', []) or []
                hi = d.get('temperature_2m_max', []) or []
                lo = d.get('temperature_2m_min', []) or []
                uvm = d.get('uv_index_max', []) or []
                daily = []
                for i in range(min(7, len(codes))):
                    daily.append({'code': codes[i],
                                  'hi': hi[i] if i < len(hi) else None,
                                  'lo': lo[i] if i < len(lo) else None,
                                  'uv': uvm[i] if i < len(uvm) else None})
                with _LOCK:
                    DATA.update({'wind_kmh': cur.get('wind_speed_10m'),
                                 'wind_dir': cur.get('wind_direction_10m'),
                                 'uv': cur.get('uv_index'),
                                 'daily': daily, 'ts': time.time()})
                got_any = True
            except Exception:
                pass
            try:
                hist = _fetch_kp_hist()
                days = _fetch_kp_forecast()
                kp = hist[-1][1] if hist else _fetch_kp()
                with _LOCK:
                    DATA['kp'] = kp; DATA['kp_hist'] = hist; DATA['kp_days'] = days; DATA['kp_ts'] = time.time()
                got_any = True
            except Exception:
                pass
        # успішний фетч = точно онлайн (навіть якщо проба сокета збрехала)
        if got_any:
            ONLINE[0] = True
        with _LOCK:
            DATA['loading'] = False
        # переривчастий сон: прокидаємось раніше за force_refresh()
        _wake.clear()
        _wake.wait(timeout=(900 if on else 30))

def start(get_latlon):
    if _started[0]:
        return
    _started[0] = True
    threading.Thread(target=_worker, args=(get_latlon,), daemon=True).start()
