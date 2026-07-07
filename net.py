"""net.py — інтернет для метеостанції: перевірка з'єднання + підтяг даних
з Open-Meteo (безкоштовно, без ключа): вітер, УФ-індекс, прогноз на тиждень.

Все у фоновому потоці з коротким таймаутом; якщо мережі немає — ONLINE=False,
дані лишаються None, а UI показує «—» або офлайн-оцінку. Логіку сенсорів не чіпає.
"""
import threading, time, json, socket
from urllib.request import urlopen, Request

ONLINE = [False]
_LOCK = threading.Lock()
DATA = {'wind_kmh': None, 'wind_dir': None, 'uv': None, 'daily': [], 'kp': None, 'ts': 0.0}
_started = [False]

def is_online():
    return ONLINE[0]

def get():
    with _LOCK:
        return dict(DATA)

def _check_socket():
    for host in (('1.1.1.1', 53), ('8.8.8.8', 53)):
        try:
            s = socket.create_connection(host, timeout=3); s.close(); return True
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

def _fetch_kp():
    """Останній планетарний Kp-індекс з NOAA SWPC (безкоштовно, без ключа)."""
    url = 'https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json'
    with urlopen(Request(url, headers=_UA), timeout=6) as r:
        rows = json.load(r)
    # перший рядок — заголовок; беремо останнє значення Kp
    if isinstance(rows, list) and len(rows) > 1:
        try:
            return float(rows[-1][1])
        except Exception:
            return None
    return None

def _worker(get_latlon):
    while True:
        on = _check_socket()
        ONLINE[0] = on
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
            except Exception:
                pass
            try:
                kp = _fetch_kp()
                with _LOCK:
                    DATA['kp'] = kp
            except Exception:
                pass
        time.sleep(900 if on else 30)

def start(get_latlon):
    if _started[0]:
        return
    _started[0] = True
    threading.Thread(target=_worker, args=(get_latlon,), daemon=True).start()
