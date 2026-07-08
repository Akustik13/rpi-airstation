"""gridui.py — сітковий рушій розкладки + бібліотека блоків.

Один і той самий код малює екрани і в застосунку (реальні дані), і в редакторах
(демо-дані). Блоки самі вписуються в свій прямокутник: авто-fit шрифтів, кліп
графіків по межах, згруповані «число+одиниця» — тож нічого не налазить і не вилазить.

Дані передаються ПЛОСКИМ словником `data` (провайдер заповнює його щокадру).
Розкладка — список блоків: {"type","col","row","w","h","p":{...}} у клітинках сітки.
"""
import math, json, os
import pygame
try:
    import config as C
except Exception:
    class C:  # запасні кольори, якщо config недоступний
        W, H = 1280, 720
        BG=(10,15,28); SIDEBAR=(13,20,36); PANEL=(21,30,50); PANEL2=(33,45,70)
        BORDER=(64,78,104); TEXT=(238,244,255); TEXT2=(200,212,232); MUTED=(132,148,174)
        WHITE=(255,255,255); GREEN=(52,211,153); YELLOW=(250,204,21); ORANGE=(251,146,60)
        RED=(248,113,113); CYAN=(34,211,238); PURPLE=(167,139,250); BLUE=(96,165,250)
        ACCENT=(96,165,250); ACCENT_D=(20,55,105)

GRID_COLS = 16
GRID_ROWS = 9
PAD = 8   # відступ між блоками
HITS = []   # тап-регіони, які реєструють блоки: [(action, pygame.Rect)]

# ── Шрифти з кешем (з курсивом) ───────────────────────────────────────────────
_FC = {}
def font(size, bold=False, italic=False):
    k = (size, bold, italic)
    if k not in _FC:
        got = None
        for name in ['dejavusans', 'freesans', 'liberationsans', 'arial', '']:
            try:
                f = pygame.font.SysFont(name, int(size), bold=bold, italic=italic)
                if f: got = f; break
            except Exception:
                pass
        _FC[k] = got or pygame.font.Font(None, int(size) + 4)
    return _FC[k]

# ── Безпечний текст ───────────────────────────────────────────────────────────
def fit_font(text, max_w, base, bold=False, italic=False, minsize=10):
    """Повертає найбільший шрифт ≤ base, за якого text вміщується в max_w."""
    sz = int(base)
    while sz > minsize:
        f = font(sz, bold, italic)
        if f.size(text)[0] <= max_w:
            return f
        sz -= 2
    return font(minsize, bold, italic)

def draw_text(surf, text, f, col, x, y, a='tl', max_w=None):
    if max_w is not None:
        f = fit_font(text, max_w, f.get_height(), False)  # приблизно; для точності краще передати size
    su = f.render(str(text), True, col); r = su.get_rect()
    setattr(r, {'tl': 'topleft', 'tr': 'topright', 'mc': 'center', 'ml': 'midleft',
                'mr': 'midright', 'bl': 'bottomleft', 'bc': 'midbottom', 'tc': 'midtop',
                'br': 'bottomright'}[a], (x, y))
    surf.blit(su, r); return r

def draw_num_unit(surf, value, unit, x, y, num_size, unit_size, col, unit_col, max_w, bold=True):
    """Малює «число + одиниця» як ЄДИНИЙ блок із гарантованим проміжком, авто-fit
    по ширині max_w. Повертає загальний rect. Одиниця фізично не може налізти."""
    s = str(value)
    uf = font(unit_size, False)
    uw = uf.size(unit)[0] + (8 if unit else 0)
    nf = fit_font(s, max(10, max_w - uw), num_size, bold)
    nr = nf.render(s, True, col); surf.blit(nr, (x, y))
    if unit:
        ur = uf.render(unit, True, unit_col)
        surf.blit(ur, (x + nr.get_width() + 8, y + nr.get_height() - uf.get_height() - 2))
        return pygame.Rect(x, y, nr.get_width() + 8 + ur.get_width(), nr.get_height())
    return pygame.Rect(x, y, nr.get_width(), nr.get_height())

# ── Сітка ─────────────────────────────────────────────────────────────────────
def cell_rect(col, row, w, h, W=None, H=None, cols=GRID_COLS, rows=GRID_ROWS, pad=PAD):
    W = W or C.W; H = H or C.H
    cw = W / cols; ch = H / rows
    x = int(col * cw) + pad
    y = int(row * ch) + pad
    ww = int(w * cw) - 2 * pad
    hh = int(h * ch) - 2 * pad
    return (x, y, max(10, ww), max(10, hh))

def pt_to_cell(px, py, W=None, H=None, cols=GRID_COLS, rows=GRID_ROWS):
    W = W or C.W; H = H or C.H
    return (max(0, min(cols - 1, int(px / (W / cols)))),
            max(0, min(rows - 1, int(py / (H / rows)))))

# ── Картка (заокруглена рамка як у застосунку) ────────────────────────────────
def card(surf, rect, title=None, accent=None, fill=True):
    accent = accent or C.BORDER
    if fill:
        pygame.draw.rect(surf, C.PANEL, rect, border_radius=14)
    pygame.draw.rect(surf, accent, rect, 1, border_radius=14)
    if title:
        f = fit_font(title, rect[2] - 36, 21, False)
        surf.blit(f.render(title, True, C.TEXT2), (rect[0] + 16, rect[1] + 12))

def _grad_color(t):
    stops = [(0.0, C.GREEN), (0.45, C.YELLOW), (0.72, C.ORANGE), (1.0, (140, 24, 24))]
    t = max(0, min(1, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]; t1, c1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / max(t1 - t0, 1e-6)
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return stops[-1][1]

def grad_bar(surf, rect, frac):
    x, y, w, h = rect
    for i in range(int(w)):
        pygame.draw.line(surf, _grad_color(i / max(w - 1, 1)), (x + i, y), (x + i, y + h))
    pygame.draw.rect(surf, C.BORDER, rect, 1, border_radius=6)
    mx = x + int(w * max(0, min(1, frac)))
    pygame.draw.polygon(surf, C.PURPLE, [(mx, y + h + 2), (mx - 7, y + h + 14), (mx + 7, y + h + 14)])

# ── Дрібні іконки ─────────────────────────────────────────────────────────────
def ic_thermo(surf, cx, cy, col=C.CYAN):
    pygame.draw.rect(surf, col, (cx - 5, cy - 22, 10, 30), border_radius=5, width=3)
    pygame.draw.circle(surf, col, (cx, cy + 14), 10); pygame.draw.circle(surf, C.RED, (cx, cy + 14), 5)
    pygame.draw.line(surf, C.RED, (cx, cy + 14), (cx, cy - 8), 4)
def ic_drop(surf, cx, cy, col=C.BLUE):
    pygame.draw.polygon(surf, col, [(cx, cy - 22), (cx + 15, cy + 8), (cx - 15, cy + 8)])
    pygame.draw.circle(surf, col, (cx, cy + 8), 15); pygame.draw.circle(surf, (150, 200, 255), (cx - 4, cy + 6), 4)
def ic_gauge(surf, cx, cy, col=C.PURPLE):
    pygame.draw.arc(surf, col, (cx - 20, cy - 16, 40, 40), math.radians(20), math.radians(160), 4)
    pygame.draw.line(surf, C.WHITE, (cx, cy + 4), (cx + 10, cy - 10), 3); pygame.draw.circle(surf, C.WHITE, (cx, cy + 4), 3)
def ic_co2(surf, cx, cy, col=C.GREEN):
    pygame.draw.circle(surf, col, (cx - 10, cy), 11); pygame.draw.circle(surf, col, (cx + 6, cy - 6), 14)
    pygame.draw.circle(surf, col, (cx + 16, cy), 10); pygame.draw.rect(surf, col, (cx - 14, cy, 34, 12), border_radius=6)
    surf.blit(font(12, True).render('CO₂', True, (10, 20, 15)), (cx - 12, cy - 6))
def moon_disc(surf, cx, cy, r, illum, wax, lit=(232, 232, 214)):
    pygame.draw.circle(surf, (18, 24, 40), (cx, cy), r + 3); pygame.draw.circle(surf, (60, 66, 84), (cx, cy), r)
    for yy in range(-r, r + 1):
        hw = int(math.sqrt(max(0, r * r - yy * yy))); tx = int(hw * (2 * illum - 1))
        x0, x1 = (tx, hw) if wax else (-hw, -tx)
        if x1 > x0: pygame.draw.line(surf, lit, (cx + x0, cy + yy), (cx + x1, cy + yy))
    pygame.draw.circle(surf, (90, 96, 120), (cx, cy), r, 1)
def sun_icon(surf, cx, cy, r, col=C.YELLOW):
    for a in range(0, 360, 45):
        rad = math.radians(a)
        pygame.draw.line(surf, col, (cx + int(math.cos(rad) * r * 1.3), cy + int(math.sin(rad) * r * 1.3)),
                         (cx + int(math.cos(rad) * r * 1.75), cy + int(math.sin(rad) * r * 1.75)), 3)
    pygame.draw.circle(surf, col, (cx, cy), r)
def sunrise_icon(surf, cx, cy, up=True, col=C.YELLOW):
    for a in range(200, 341, 35):
        rad = math.radians(a)
        pygame.draw.line(surf, col, (cx + int(math.cos(rad) * 16), cy + int(math.sin(rad) * 16)),
                         (cx + int(math.cos(rad) * 24), cy + int(math.sin(rad) * 24)), 3)
    pygame.draw.circle(surf, col, (cx, cy), 12)
    pygame.draw.line(surf, C.TEXT2, (cx - 26, cy + 8), (cx + 26, cy + 8), 3)
    ay = cy - 22 if up else cy + 22; d = -1 if up else 1
    pygame.draw.line(surf, col, (cx, cy - 2 * d), (cx, ay), 3)
    pygame.draw.polygon(surf, col, [(cx, ay), (cx - 5, ay + 6 * d), (cx + 5, ay + 6 * d)])
def wx_icon(surf, cx, cy, s, code):
    sun = C.YELLOW; cloud = (200, 212, 232)
    if code in ('sun', 'partly'):
        r = int(s * 0.5); ox, oy = (cx - int(s * 0.25), cy - int(s * 0.25)) if code == 'partly' else (cx, cy)
        sun_icon(surf, ox, oy, r, sun)
    if code in ('partly', 'cloud', 'rain', 'storm'):
        cyy = cy + int(s * 0.15)
        pygame.draw.circle(surf, cloud, (cx - int(s * 0.35), cyy), int(s * 0.32))
        pygame.draw.circle(surf, cloud, (cx + int(s * 0.15), cyy - int(s * 0.12)), int(s * 0.40))
        pygame.draw.circle(surf, cloud, (cx + int(s * 0.5), cyy), int(s * 0.30))
        pygame.draw.rect(surf, cloud, (cx - int(s * 0.6), cyy, int(s * 1.2), int(s * 0.35)), border_radius=8)
    if code in ('rain', 'storm'):
        for dx in (-0.35, 0.0, 0.35):
            x0 = cx + int(s * dx); pygame.draw.line(surf, C.BLUE, (x0, cy + int(s * 0.6)), (x0 - 6, cy + int(s * 0.85)), 3)
    if code == 'storm':
        pygame.draw.polygon(surf, sun, [(cx, cy + int(s * 0.55)), (cx - 10, cy + int(s * 0.85)), (cx, cy + int(s * 0.8)), (cx - 6, cy + int(s * 1.05))])

# ── Метадані показників ───────────────────────────────────────────────────────
META = {
    'co2':         ('CO₂', 'ppm', C.GREEN, (800, 1200)),
    'voc_index':   ('VOC Index', 'idx', C.PURPLE, (100, 200)),
    'nox_index':   ('NOx Index', 'idx', C.ORANGE, (100, 200)),
    'eco2':        ('eCO₂*', 'ppm', C.CYAN, (800, 1200)),
    'aqi':         ('AQI', '', C.BLUE, (50, 100)),
    'iaq':         ('IAQ*', '', C.YELLOW, (100, 200)),
    'pm2_5':       ('PM2.5', 'µg/m³', C.GREEN, (15, 25)),
    'pm10':        ('PM10', 'µg/m³', C.ORANGE, (45, 75)),
    'temperature': ('Температура', '°C', C.CYAN, None),
    'humidity':    ('Вологість', '%', C.BLUE, None),
    'pressure':    ('Тиск', 'hPa', C.PURPLE, None),
}
def _status(key, v):
    th = META.get(key, (None, None, None, None))[3]
    if v is None or th is None:
        return '', C.TEXT2
    g, w = th
    if v <= g: return 'Добре', C.GREEN
    if v <= w: return 'Помірно', C.YELLOW
    return 'Погано', C.RED

def _fmt(v, dig=0):
    if v is None: return '—'
    try: return f'{float(v):.{dig}f}'
    except Exception: return str(v)

# ══════════════════════════ БЛОКИ ════════════════════════════════════════════
# Кожен блок: draw(surf, rect, p, data). p — параметри, data — плоский словник.

def _b_clock(surf, rect, p, data):
    x, y, w, h = rect
    hm = data.get('time_hm', '19:04'); ss = data.get('time_s', '00')
    show_s = p.get('seconds', True)
    reserve = int(w * 0.22) if show_s else 0
    nf = fit_font(hm, w - reserve - 12, h * 0.9, True)
    nr = nf.render(hm, True, C.WHITE)
    surf.blit(nr, (x + (w - reserve - nr.get_width()) // 2, y + (h - nr.get_height()) // 2))
    if show_s:
        sf = font(max(16, int(nf.get_height() * 0.38)), True)
        sr = sf.render(ss, True, C.ACCENT)
        surf.blit(sr, (x + (w - reserve) // 2 + nr.get_width() // 2 + 8,
                       y + (h + nr.get_height()) // 2 - sr.get_height() - 4))

def _b_datetime(surf, rect, p, data):
    x, y, w, h = rect
    draw_text(surf, data.get('weekday', 'Четвер') + ', ' + data.get('date', '02.07.2026'),
              fit_font(data.get('weekday', '') + ', ' + data.get('date', ''), w - 16, min(h * 0.7, 30)),
              C.TEXT2, x + w // 2, y + h // 2, 'mc')

def _b_location(surf, rect, p, data):
    x, y, w, h = rect
    cx = x + 16
    pygame.draw.circle(surf, C.RED, (cx, y + h // 2 - 4), 7)
    pygame.draw.polygon(surf, C.RED, [(cx - 7, y + h // 2 - 1), (cx + 7, y + h // 2 - 1), (cx, y + h // 2 + 10)])
    draw_text(surf, data.get('loc', 'Munich'), fit_font(data.get('loc', ''), w - 44, min(h * 0.8, 26), True),
              C.WHITE, cx + 18, y + h // 2, 'ml')

def _b_metric(surf, rect, p, data):
    key = p.get('key', 'co2')
    lab, unit, col, th = META.get(key, (key, '', C.BLUE, None))
    card(surf, rect, lab, col)
    x, y, w, h = rect
    v = data.get(key)
    if key == 'temperature': unit = data.get('temp_unit', '°C')
    if key == 'pressure': unit = data.get('press_unit', 'hPa')
    st, sc = _status(key, v)
    if st:
        draw_text(surf, st, fit_font(st, w * 0.4, 20), sc, x + w - 14, y + 12, 'tr')
    dig = 1 if key in ('temperature', 'pressure', 'pm2_5', 'pm10') else 0
    if p.get('icon', False):
        ic = {'temperature': ic_thermo, 'humidity': ic_drop, 'pressure': ic_gauge, 'co2': ic_co2}.get(key)
        if ic:
            ic(surf, x + 34, y + h // 2 + 6, col)
            draw_num_unit(surf, _fmt(v, dig), unit, x + 64, y + h // 2 - 24, h * 0.5, 20, C.WHITE, C.TEXT2, w - 80)
            return
    draw_num_unit(surf, _fmt(v, dig), unit, x + 18, y + int(h * 0.34), h * 0.44, 22, C.WHITE, C.TEXT2, w - 36)
    # міні градієнт-шкала якщо є пороги
    if th and h > 90:
        gmax = th[1] * 1.5
        grad_bar(surf, (x + 18, y + h - 30, w - 36, 14), (v or 0) / gmax)

def _b_aqi_bar(surf, rect, p, data):
    card(surf, rect, 'Якість повітря', C.GREEN)
    x, y, w, h = rect
    v = data.get('aqi'); st, sc = _status('aqi', v)
    draw_text(surf, f'AQI {_fmt(v)}', font(min(int(h*0.32),30), True), C.WHITE, x + 18, y + int(h*0.28), 'tl')
    draw_text(surf, st, font(20), sc, x + w - 16, y + int(h*0.30), 'tr')
    grad_bar(surf, (x + 18, y + h - 34, w - 36, 18), (v or 0) / 300.)

def _b_pressure_graph(surf, rect, p, data):
    card(surf, rect, 'Тиск за 24 години', C.BLUE)
    x, y, w, h = rect
    prev = surf.get_clip(); surf.set_clip(pygame.Rect(rect))
    gx, gy, gw, gh = x + 56, y + 44, w - 74, h - (96 if p.get('trend', True) else 70)
    rows = data.get('pressure_series', [])
    vals = [v for _, v in rows]
    if vals:
        lo, hi = min(vals) - 2, max(vals) + 2
    else:
        lo, hi = 996, 1012
    if hi - lo < 1: hi = lo + 1
    for i in range(5):
        yy = gy + gh - int(gh * i / 4)
        draw_text(surf, f'{lo+(hi-lo)*i/4:.0f}', font(13), C.MUTED, gx - 6, yy, 'mr')
        pygame.draw.line(surf, (40, 52, 74), (gx, yy), (gx + gw, yy), 1)
    if len(rows) > 1:
        t0 = rows[0][0]; t1 = rows[-1][0]; span = max(t1 - t0, 1)
        pts = [(gx + int((t - t0) / span * gw), gy + gh - int(gh * (v - lo) / (hi - lo))) for t, v in rows]
        pygame.draw.lines(surf, C.BLUE, False, pts, 3)
    if p.get('trend', True) and rows:
        ty = y + h - 44; nb = 8; bw = (gw - 7 * 8) // nb
        t0 = rows[0][0]
        draw_text(surf, 'Тренд — кожен стовпець = 3 год', font(13), C.MUTED, gx, ty - 18, 'tl')
        for b in range(nb):
            seg = [v for t, v in rows if t0 + b*3*3600 <= t < t0 + (b+1)*3*3600]
            if len(seg) < 2: continue
            d = seg[-1] - seg[0]; col = C.GREEN if d >= 0 else C.RED
            bh = int(min(26, abs(d) * 10) + 3); bx = gx + b * (bw + 8)
            pygame.draw.rect(surf, col, (bx, ty - bh if d >= 0 else ty, bw, bh), border_radius=3)
    surf.set_clip(prev)

def _b_gauge(surf, rect, p, data):
    key = p.get('key', 'pressure')
    card(surf, rect, META.get(key, ('',))[0] + ' — шкала', C.PURPLE)
    x, y, w, h = rect
    v = data.get(key)
    th = META.get(key, (None, None, None, None))[3]
    cx = x + w // 2; cy = y + h - int(h * 0.18); R = int(min(w / 2 - 24, h - 70)); R = max(40, R)
    def pt(fr, rad):
        a = math.radians(180 + 180 * max(0, min(1, fr))); return (cx + int(math.cos(a) * rad), cy + int(math.sin(a) * rad))
    if key == 'pressure':
        lo, hi = 980, 1030
        for i in range(60):
            t = i / 60; c = tuple(int(C.GREEN[j] + (C.BLUE[j] - C.GREEN[j]) * t) for j in range(3))
            pygame.draw.line(surf, c, pt(t, R), pt((i + 1) / 60, R), 8)
        for mv in (980, 1000, 1020, 1030):
            draw_text(surf, str(mv), font(12), C.MUTED, *pt((mv - lo) / (hi - lo), R + 14), 'mc')
    else:
        lo, hi = 0, (th[1] * 1.6 if th else 100)
        gf = (th[0] - lo) / (hi - lo) if th else .5; wf = (th[1] - lo) / (hi - lo) if th else .8
        for i in range(60):
            t = i / 60; col = C.GREEN if t < gf else (C.YELLOW if t < wf else C.RED)
            pygame.draw.line(surf, col, pt(t, R), pt((i + 1) / 60, R), 8)
    try: fr = (float(v) - lo) / (hi - lo)
    except Exception: fr = 0
    e = pt(max(0, min(1, fr)), R - 12)
    pygame.draw.line(surf, C.WHITE, (cx, cy), e, 4); pygame.draw.circle(surf, C.WHITE, (cx, cy), 6)
    unit = data.get('press_unit', 'hPa') if key == 'pressure' else META.get(key, ('', ''))[1]
    draw_text(surf, _fmt(v, 1 if key == 'pressure' else 0), font(min(int(R*0.5), 30), True), C.WHITE, cx, cy - int(R * 0.34), 'mc')

def _b_forecast(surf, rect, p, data):
    card(surf, rect, 'Прогноз', C.PURPLE)
    x, y, w, h = rect
    wx_icon(surf, x + 56, y + h // 2 + 6, min(int(h*0.4), 48), data.get('forecast_icon', 'cloud'))
    txt = data.get('forecast_text', 'Прогноз недоступний')
    words = txt.split(' '); lines = ['']
    fnt = font(min(int(h*0.16), 21))
    for wd in words:
        if fnt.size(lines[-1] + ' ' + wd)[0] > w - 130:
            lines.append(wd)
        else:
            lines[-1] = (lines[-1] + ' ' + wd).strip()
    for i, ln in enumerate(lines[:3]):
        draw_text(surf, ln, fnt, C.TEXT2, x + 118, y + h // 2 - 20 + i * 26, 'ml')

def _b_moon(surf, rect, p, data):
    x, y, w, h = rect
    r = min(w, h) // 2 - 18
    moon_disc(surf, x + w // 2, y + r + 10, r, data.get('moon_illum', 0.5), data.get('moon_wax', True))
    draw_text(surf, data.get('moon_name', ''), fit_font(data.get('moon_name', ''), w - 8, 18), C.TEXT2,
              x + w // 2, y + h - 16, 'bc')

def _b_suntimes(surf, rect, p, data):
    x, y, w, h = rect
    sunrise_icon(surf, x + 26, y + h // 3, True, C.YELLOW)
    draw_text(surf, data.get('sunrise', '--:--'), font(min(int(h*0.28), 22), True), C.YELLOW, x + 54, y + h // 3, 'ml')
    sunrise_icon(surf, x + 26, y + 2 * h // 3, False, C.ORANGE)
    draw_text(surf, data.get('sunset', '--:--'), font(min(int(h*0.28), 22), True), C.ORANGE, x + 54, y + 2 * h // 3, 'ml')

def _b_uv(surf, rect, p, data):
    card(surf, rect, 'Індекс УФ', C.ORANGE)
    x, y, w, h = rect
    for lo, hi, col in [(0, 2, C.GREEN), (3, 5, C.YELLOW), (6, 7, C.ORANGE), (8, 10, C.RED), (11, 13, C.PURPLE)]:
        bx = x + 18; bw = w - 36
        pygame.draw.rect(surf, col, (bx + int(bw*lo/13), y + h - 28, int(bw*(hi+1)/13)-int(bw*lo/13)-3, 14), border_radius=4)
    uv = data.get('uv')
    if uv is not None:
        px = x + 18 + int((w - 36) * (uv + 0.5) / 13)
        pygame.draw.polygon(surf, C.WHITE, [(px, y + h - 34), (px - 6, y + h - 44), (px + 6, y + h - 44)])
    draw_text(surf, ('—' if uv is None else str(uv)) + ('~' if data.get('uv_est') else ''),
              font(min(int(h*0.4), 46), True), C.WHITE, x + 18, y + int(h*0.30), 'tl')

def _b_outdoor(surf, rect, p, data):
    card(surf, rect, 'Надворі · BLE', C.BORDER)
    x, y, w, h = rect
    pygame.draw.circle(surf, C.MUTED, (x + w - 28, y + 22), 7)
    ic_thermo(surf, x + 34, y + h // 2 + 4, C.MUTED)
    draw_num_unit(surf, data.get('out_temp', '13.3'), '°C', x + 64, y + h // 2 - 22, h * 0.45, 18, C.MUTED, C.MUTED, w * 0.5)
    draw_text(surf, 'заглушка BLE', font(14), C.MUTED, x + 16, y + h - 22, 'tl')

def _b_kp_storms(surf, rect, p, data):
    card(surf, rect, 'Магнітні бурі — Kp за добу', C.PURPLE)
    x, y, w, h = rect
    prev = surf.get_clip(); surf.set_clip(pygame.Rect(rect))
    import time as _t
    # кнопка «оновити» + статус (правий верх)
    rb = pygame.Rect(x + w - 44, y + 10, 34, 30)
    pygame.draw.rect(surf, C.PANEL2, rb, border_radius=8); pygame.draw.rect(surf, C.ACCENT, rb, 1, border_radius=8)
    draw_text(surf, '⟳', font(22), C.ACCENT, rb.centerx, rb.centery, 'mc')
    HITS.append(('astro_refresh', rb))
    if data.get('loading'):
        draw_text(surf, 'завантаження…', font(13), C.YELLOW, x + w - 54, y + 24, 'mr')
    elif data.get('kp_ts'):
        draw_text(surf, 'оновлено ' + _t.strftime('%H:%M', _t.localtime(data['kp_ts'])), font(13), C.MUTED, x + w - 54, y + 24, 'mr')
    hist = data.get('kp_hist', [])[-8:]
    gx, gy, gw, gh = x + 20, y + 46, w - 40, int(h * 0.42)
    for i in range(4):
        yy = gy + gh - int(gh * i / 3)
        pygame.draw.line(surf, (60, 55, 90), (gx, yy), (gx + gw, yy), 1)
        draw_text(surf, str(i * 3), font(12), C.MUTED, gx - 6, yy, 'mr')
    if hist:
        bw = gw / max(len(hist), 1)
        for i, item in enumerate(hist):
            try:
                ts, kp = item
            except Exception:
                kp = item; ts = None
            bh = int(gh * max(0.03, min(1.0, kp / 9.0)))
            bx = gx + int(i * bw)
            pygame.draw.rect(surf, _kp_col(kp), (bx + 2, gy + gh - bh, int(bw) - 4, bh), border_radius=3)
            if ts:
                draw_text(surf, _t.strftime('%H', _t.localtime(ts)), font(12), C.MUTED, bx + int(bw / 2), gy + gh + 4, 'tc')
    else:
        draw_text(surf, 'дані з’являться при інтернеті', font(15), C.MUTED, gx + gw // 2, gy + gh // 2, 'mc')
    kp = data.get('kp')
    draw_text(surf, f"Зараз: Kp {'—' if kp is None else f'{kp:.0f}'} · {data.get('kp_label','')}",
              font(min(int(h*0.11),19), True), _kp_col(kp), x + 20, y + gy - y + gh + 26, 'tl')
    # аврора
    au = data.get('aurora', {})
    draw_text(surf, au.get('text', ''), font(15), C.GREEN if au.get('possible') else C.TEXT2,
              x + 20, y + gy - y + gh + 52, 'tl')
    # 3-денний прогноз
    days = data.get('kp_days', [])
    if days and h > 150:
        draw_text(surf, 'Прогноз:', font(15), C.MUTED, x + 20, y + h - 26, 'ml')
        dx = x + 110
        for d in days[:3]:
            lab = _weekday_short(d.get('date', '')); mx = d.get('max', 0)
            draw_text(surf, f"{lab} {mx:.0f}", font(16, True), _kp_col(mx), dx, y + h - 26, 'ml')
            dx += 92
    surf.set_clip(prev)

def _weekday_short(datestr):
    try:
        import datetime as _dt
        wd = _dt.datetime.strptime(datestr, '%Y-%m-%d').weekday()
        return ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд'][wd]
    except Exception:
        return '—'

def _b_retro(surf, rect, p, data):
    card(surf, rect, 'Ретроградні планети', C.CYAN)
    x, y, w, h = rect
    prev = surf.get_clip(); surf.set_clip(pygame.Rect(rect))
    retro = data.get('retro_detail', [])
    if not retro:
        draw_text(surf, 'Усі планети в прямому русі ✦', font(20), C.GREEN, x + w // 2, y + h // 2, 'mc')
        surf.set_clip(prev); return
    rowh = min(46, (h - 52) // max(len(retro), 1))
    yy = y + 48
    for d in retro:
        row_rect = pygame.Rect(x + 6, yy, w - 12, rowh)
        HITS.append(('planet:' + d['planet'], row_rect))
        draw_text(surf, d.get('glyph', '•'), font(min(rowh, 30)), C.PURPLE, x + 24, yy + rowh // 2, 'mc')
        draw_text(surf, d['planet'], font(min(int(rowh*0.5), 20), True), C.WHITE, x + 50, yy + rowh // 2 - 8, 'ml')
        draw_text(surf, f"{d['start']}–{d['end']} · ще {d['days_left']} дн ({d['phase']})",
                  font(13), C.TEXT2, x + 50, yy + rowh // 2 + 12, 'ml')
        # прогрес-смуга періоду
        bx = x + w - 180; bw = 160
        pygame.draw.rect(surf, C.PANEL2, (bx, yy + rowh // 2 - 6, bw, 12), border_radius=6)
        pygame.draw.rect(surf, C.PURPLE, (bx, yy + rowh // 2 - 6, int(bw * d['progress']), 12), border_radius=6)
        draw_text(surf, 'ⓘ', font(16), C.CYAN, x + w - 16, yy + rowh // 2, 'mr')
        yy += rowh
    draw_text(surf, 'тап по планеті — деталі', font(12), C.MUTED, x + 16, y + h - 14, 'ml')
    surf.set_clip(prev)

def _b_astro_moon(surf, rect, p, data):
    x, y, w, h = rect
    r = max(40, int(min(w, h) * 0.30))          # менший місяць
    cx, cy = x + w // 2, y + r + 22
    # тепле світіння
    glow = pygame.Surface((r * 4, r * 4), pygame.SRCALPHA)
    for i in range(r * 2, 0, -6):
        a = int(26 * (i / (r * 2)))
        pygame.draw.circle(glow, (245, 220, 130, a), (r * 2, r * 2), i)
    surf.blit(glow, (cx - r * 2, cy - r * 2))
    moon_disc(surf, cx, cy, r, data.get('moon_illum', 0.5), data.get('moon_wax', True), lit=(250, 226, 120))
    draw_text(surf, data.get('moon_name', ''), fit_font(data.get('moon_name', ''), w - 12, 24, True),
              (245, 235, 200), cx, cy + r + 22, 'mc')
    draw_text(surf, f"Освітлення {int(data.get('moon_illum',0)*100)}%", font(15), (200, 190, 160),
              cx, cy + r + 48, 'mc')

def _b_astro_forecast(surf, rect, p, data):
    card(surf, rect, 'Прогноз дня ✨', C.PURPLE)
    x, y, w, h = rect
    prev = surf.get_clip(); surf.set_clip(pygame.Rect(rect))
    txt = data.get('astro_forecast', '') or data.get('advice', '')
    words = txt.split(' '); lines = ['']; af = font(min(int(h * 0.09), 18))
    for wd in words:
        if af.size(lines[-1] + ' ' + wd)[0] > w - 40:
            lines.append(wd)
        else:
            lines[-1] = (lines[-1] + ' ' + wd).strip()
    yy = y + 48
    for ln in lines[:max(1, (h - 70) // 24)]:
        draw_text(surf, ln, af, (222, 226, 245), x + 20, yy, 'tl'); yy += 24
    draw_text(surf, '* прикмети та традиції — для настрою, не медична порада',
              font(12), C.MUTED, x + 20, y + h - 20, 'tl')
    surf.set_clip(prev)

def _b_label(surf, rect, p, data):
    x, y, w, h = rect
    draw_text(surf, p.get('text', 'Текст'), fit_font(p.get('text', ''), w - 12, min(h*0.7, 28), p.get('bold', True)),
              C.TEXT2, x + w // 2, y + h // 2, 'mc')

def _kp_color(k):
    if k is None: return C.MUTED
    if k < 4: return C.GREEN
    if k < 5: return C.YELLOW
    if k < 7: return C.ORANGE
    return C.RED

def _b_astro(surf, rect, p, data):
    card(surf, rect, 'Астро', C.PURPLE)
    x, y, w, h = rect
    prev = surf.get_clip(); surf.set_clip(pygame.Rect(rect))
    # місяць ліворуч
    mr = min(int(h * 0.28), 46)
    moon_disc(surf, x + 24 + mr, y + 52 + mr, mr, data.get('moon_illum', 0.5), data.get('moon_wax', True))
    draw_text(surf, data.get('moon_name', ''), fit_font(data.get('moon_name', ''), 2 * mr + 20, 16),
              C.TEXT2, x + 24 + mr, y + 52 + 2 * mr + 12, 'mc')
    # права частина
    rx = x + 60 + 2 * mr
    rw = x + w - rx - 16
    # Kp-шкала магнітних бур
    kp = data.get('kp')
    draw_text(surf, 'Магнітні бурі (Kp)', font(15), C.MUTED, rx, y + 44, 'tl')
    bar = (rx, y + 68, rw, 16)
    for i in range(int(rw)):
        kk = i / max(rw - 1, 1) * 9
        pygame.draw.line(surf, _kp_color(kk), (rx + i, bar[1]), (rx + i, bar[1] + bar[3]))
    pygame.draw.rect(surf, C.BORDER, bar, 1, border_radius=5)
    for gk in (5, 6, 7, 8):
        gx = rx + int(rw * gk / 9)
        pygame.draw.line(surf, (12, 18, 30), (gx, bar[1]), (gx, bar[1] + bar[3]), 1)
    if kp is not None:
        mx = rx + int(rw * max(0, min(1, kp / 9.0)))
        pygame.draw.polygon(surf, C.WHITE, [(mx, bar[1] - 3), (mx - 6, bar[1] - 12), (mx + 6, bar[1] - 12)])
    klab = data.get('kp_label', '—')
    kcol = _kp_color(kp)
    draw_text(surf, f"Kp {'—' if kp is None else f'{kp:.0f}'}  ·  {klab}", font(min(int(h*0.14),20), True),
              kcol, rx, y + 92, 'tl')
    # ретроград
    retro = data.get('retro', [])
    rtxt = 'Ретроград: ' + (', '.join(retro) if retro else 'немає')
    draw_text(surf, rtxt, fit_font(rtxt, rw, 18), C.TEXT2, rx, y + 122, 'tl')
    # порада дня
    adv = data.get('advice', '')
    if adv and h > 150:
        words = adv.split(' '); lines = ['']
        af = font(16)
        for wd in words:
            if af.size(lines[-1] + ' ' + wd)[0] > w - 48:
                lines.append(wd)
            else:
                lines[-1] = (lines[-1] + ' ' + wd).strip()
        draw_text(surf, '💡 Порада дня:', font(15), C.YELLOW, x + 24, y + h - 24 - 22 * min(len(lines), 2), 'tl')
        for i, ln in enumerate(lines[:2]):
            draw_text(surf, ln, af, C.TEXT2, x + 24, y + h - 22 * (min(len(lines), 2) - i) - 2, 'tl')
    surf.set_clip(prev)

BLOCKS = {
    'clock': ('Годинник', _b_clock),
    'datetime': ('Дата', _b_datetime),
    'location': ('Локація', _b_location),
    'metric': ('Показник', _b_metric),
    'aqi_bar': ('Якість повітря', _b_aqi_bar),
    'pressure_graph': ('Графік тиску', _b_pressure_graph),
    'gauge': ('Аналог. шкала', _b_gauge),
    'forecast': ('Прогноз', _b_forecast),
    'moon': ('Місяць', _b_moon),
    'suntimes': ('Схід/захід', _b_suntimes),
    'uv': ('УФ-індекс', _b_uv),
    'outdoor': ('Надворі (BLE)', _b_outdoor),
    'astro': ('Астро', _b_astro),
    'kp_storms': ('Магнітні бурі', _b_kp_storms),
    'retro': ('Ретроградні планети', _b_retro),
    'astro_moon': ('Місяць (космос)', _b_astro_moon),
    'astro_forecast': ('Прогноз дня', _b_astro_forecast),
    'label': ('Напис', _b_label),
}

def draw_block(surf, block, data, W=None, H=None, cols=GRID_COLS, rows=GRID_ROWS):
    r = cell_rect(block['col'], block['row'], block['w'], block['h'], W, H, cols, rows)
    fn = BLOCKS.get(block['type'])
    if fn:
        try:
            fn[1](surf, r, block.get('p', {}), data)
        except Exception as e:
            pygame.draw.rect(surf, C.RED, r, 1, border_radius=8)
            draw_text(surf, 'err:' + block['type'], font(14), C.RED, r[0] + 6, r[1] + 6, 'tl')
    return r

def render_layout(surf, layout, data, W=None, H=None):
    W = W or C.W; H = H or C.H
    cols = layout.get('cols', GRID_COLS); rows = layout.get('rows', GRID_ROWS)
    HITS.clear()
    if layout.get('bg') == 'cosmic':
        _draw_cosmic_bg(surf, W, H)
    else:
        surf.fill(C.BG)
    for b in layout.get('blocks', []):
        draw_block(surf, b, data, W, H, cols, rows)

import random as _rnd
def _draw_cosmic_bg(surf, W, H):
    # градієнт індиго → майже чорний
    for y in range(0, H, 2):
        t = y / H
        c = (int(16 + 8 * (1 - t)), int(12 + 6 * (1 - t)), int(34 + 20 * (1 - t)))
        pygame.draw.line(surf, c, (0, y), (W, y)); pygame.draw.line(surf, c, (0, y + 1), (W, y + 1))
    # м'яке світіння-туманність
    for (cx, cy, rr, col) in [(int(W * 0.18), int(H * 0.25), 220, (40, 30, 80)),
                              (int(W * 0.8), int(H * 0.7), 260, (30, 45, 90))]:
        glow = pygame.Surface((rr * 2, rr * 2), pygame.SRCALPHA)
        for i in range(rr, 0, -8):
            a = int(22 * (i / rr))
            pygame.draw.circle(glow, (col[0], col[1], col[2], a), (rr, rr), i)
        surf.blit(glow, (cx - rr, cy - rr))
    # зорі (детерміновані, не мерехтять)
    st = _rnd.Random(42)
    for _ in range(140):
        x = st.randint(0, W); y = st.randint(0, H); b = st.randint(60, 255); s = st.choice([1, 1, 1, 2])
        surf.fill((b, b, min(255, b + 20)), (x, y, s, s))

def _kp_col(kp):
    if kp is None: return C.MUTED
    if kp < 4: return C.GREEN
    if kp < 5: return C.YELLOW
    if kp < 7: return C.ORANGE
    return C.RED

# ── Збереження / завантаження ─────────────────────────────────────────────────
def save_layout(layout, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)

def load_layout(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

# ── Демо-дані для редакторів ──────────────────────────────────────────────────
def demo_data():
    import time
    now = time.time()
    return {
        'time_hm': '19:04', 'time_s': '32', 'date': '02.07.2026', 'weekday': 'Четвер', 'loc': 'Munich, Bavaria',
        'temperature': 27.2, 'temp_unit': '°C', 'humidity': 53, 'pressure': 1002.4, 'press_unit': 'hPa',
        'co2': 1250, 'voc_index': 112, 'nox_index': 2, 'eco2': 496, 'aqi': 53, 'iaq': 130, 'pm2_5': 4.2, 'pm10': 60,
        'sunrise': '05:17', 'sunset': '21:17', 'moon_illum': 0.94, 'moon_wax': False, 'moon_name': 'Спадний',
        'forecast_icon': 'rain', 'forecast_text': 'Тиск падає — очікуються опади', 'forecast_rate': -2.0,
        'wind': 12, 'uv': 4, 'uv_est': True, 'online': False, 'out_temp': '13.3',
        'kp': 5.3, 'kp_label': 'буря G1', 'retro': ['Меркурій', 'Сатурн'],
        'kp_hist': [(now - (7 - i) * 10800, [2, 3, 4, 5, 6, 5, 4, 5][i]) for i in range(8)],
        'kp_days': [{'date': '2026-07-08', 'max': 5}, {'date': '2026-07-09', 'max': 4}, {'date': '2026-07-10', 'max': 6}],
        'aurora': {'need': 8, 'possible': False, 'text': 'Аврора — за Kp ≥ 8 (зараз 5)'},
        'kp_ts': now, 'loading': False,
        'retro_detail': [
            {'planet': 'Меркурій', 'glyph': '☿', 'start': '29.06', 'end': '23.07', 'days_left': 8, 'total': 24, 'progress': 0.66, 'phase': 'середина', 'area': 'спілкування', 'advice': ''},
            {'planet': 'Сатурн', 'glyph': '♄', 'start': '13.07', 'end': '28.11', 'days_left': 136, 'total': 138, 'progress': 0.01, 'phase': 'початок', 'area': 'дисципліна', 'advice': ''},
            {'planet': 'Нептун', 'glyph': '♆', 'start': '04.07', 'end': '10.12', 'days_left': 148, 'total': 159, 'progress': 0.06, 'phase': 'початок', 'area': 'мрії', 'advice': ''}],
        'astro_forecast': 'Геомагнітна буря G1 — бережіть режим сну. Ретроград: Меркурій, Сатурн — перевіряйте деталі й не поспішайте з рішеннями.',
        'advice': 'Провітрюйте приміщення — свіже повітря бадьорить і покращує концентрацію.',
        'pressure_series': [(now - (96 - i) * 900, 1008 - i * 0.06 + math.sin(i / 6) * 1.2) for i in range(96)],
    }
