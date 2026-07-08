#!/usr/bin/env python3
"""
RPi Zero Air Quality & Weather Station
Display: MPI3501 3.5" SPI  480×320
Sensors: BMP280, SCD41, SGP41, SPS30 (all I2C bus 1)

Run:  python main.py [--demo]
      python main.py --bus 3    # use different I2C bus
"""

import sys, os, time, threading, math, argparse, glob
from datetime import datetime
from collections import deque

sys.path.insert(0, os.path.dirname(__file__))

import pygame
import pygame.gfxdraw
from pygame.locals import *

import config as C
import sensors as S
import db
import i18n
from i18n import T
import wx
import net
import gridui as GUI
import updater

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--demo',  action='store_true', help='Force stub/demo mode')
parser.add_argument('--bus',   type=int, default=1,  help='I2C bus number')
parser.add_argument('--nodb',  action='store_true',  help='Skip SQLite')
parser.add_argument('--windowed', action='store_true', help='Run in a normal desktop window instead of Linux framebuffer')
parser.add_argument('--fullscreen', action='store_true', help='Run fullscreen on desktop/X11/Wayland')
parser.add_argument('--fbdev', default=None, help='Framebuffer device, e.g. /dev/fb1 for MPI3501')
parser.add_argument('--video-driver', default='auto', choices=['auto','fbcon','kmsdrm','x11','wayland','dummy'],
                    help='SDL video driver. auto chooses desktop when DISPLAY/WAYLAND is available, otherwise framebuffer on Raspberry Pi')
args = parser.parse_args()

if args.demo:
    S.STUB_MODE = True

# ── Data globals ──────────────────────────────────────────────────────────────
latest: dict = {}
history = {k: deque(maxlen=2000) for k in list(C.CHANNELS) + ['temperature','humidity','pressure','temp_bmp','temp_scd','hum_scd']}
data_lock = threading.Lock()

# Load persisted display mapping
try:
    S.SOURCE_MAP['temperature'] = db.get_setting('temp_source', getattr(C, 'DEFAULT_SOURCE_MAP', {}).get('temperature','bmp280'))
except Exception:
    S.SOURCE_MAP['temperature'] = 'bmp280'

# ── Pygame init ───────────────────────────────────────────────────────────────
# SDL vars MUST be set before pygame.init().
# Old version forced fbcon + /dev/fb1, which is convenient over SSH but breaks
# when the app is started directly from a desktop Python session.  This block
# now auto-detects the environment and falls back cleanly.
def _running_on_raspberry_pi():
    try:
        txt = open('/proc/device-tree/model', 'rb').read().decode('utf-8', 'ignore').lower()
        return 'raspberry pi' in txt
    except Exception:
        return False

def _choose_sdl_driver():
    if args.video_driver != 'auto':
        return args.video_driver
    if args.windowed or os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        return None       # let SDL/pygame use the normal desktop backend
    if _running_on_raspberry_pi():
        return 'fbcon'    # direct console start on Raspberry Pi touchscreen
    return None

_driver = _choose_sdl_driver()
if _driver:
    os.environ['SDL_VIDEODRIVER'] = _driver
if args.fbdev:
    os.environ['SDL_FBDEV'] = args.fbdev
elif os.environ.get('SDL_VIDEODRIVER') == 'fbcon':
    os.environ.setdefault('SDL_FBDEV', '/dev/fb1')

os.environ.setdefault('SDL_NOMOUSE', '0')  # v6: keep cursor/touch pointer visible
os.environ.setdefault('SDL_MOUSE_RELATIVE', '0')

pygame.init()
pygame.display.init()

def _make_screen():
    flags = pygame.DOUBLEBUF
    if not args.windowed:
        flags |= pygame.NOFRAME
    if args.fullscreen or os.environ.get('SDL_VIDEODRIVER') == 'fbcon':
        flags |= pygame.FULLSCREEN
    try:
        return pygame.display.set_mode((C.W, C.H), flags)
    except Exception:
        try:
            return pygame.display.set_mode((C.W, C.H), pygame.NOFRAME if not args.windowed else 0)
        except Exception:
            return pygame.display.set_mode((C.W, C.H))

screen = _make_screen()
pygame.display.set_caption('RPi Zero Air Station')

try: pygame.mouse.set_visible(True)   # v6: cursor visible for touch debugging
except: pass

clock_fps = pygame.time.Clock()

# Clear to black immediately — critical for fbcon on RPi
screen.fill((0, 0, 0))
pygame.display.update()
# Second update needed on some fbcon configs
screen.fill((0, 0, 0))
pygame.display.update()

# ── Fonts ─────────────────────────────────────────────────────────────────────
def _font(size, bold=False):
    for name in ['dejavusans','freesans','liberationsans','arial','']:
        try:
            f = pygame.font.SysFont(name, size, bold=bold)
            if f: return f
        except: pass
    return pygame.font.Font(None, size+4)

FNT_TINY   = _font(11)
FNT_SMALL  = _font(13)
FNT_MED    = _font(16)
FNT_BIG    = _font(22, bold=True)
FNT_HUGE   = _font(38, bold=True)
FNT_CLOCK  = _font(40, bold=True)

# ── Screen state ──────────────────────────────────────────────────────────────
class State:
    MAIN    = 'main'
    CHART   = 'chart'
    MENU    = 'menu'
    INIT    = 'init'
    I2CSCAN = 'i2cscan'
    GRAPHSEL= 'graphsel'
    DATA    = 'data'
    ABOUT   = 'about'
    EDITOR  = 'editor'

state       = State.INIT
chart_key   = None
menu_scroll = 0
init_status = []   # [(sensor_key, status_str)]
scan_result = []   # [(addr_hex, desc)]


# ══════════════════════════════════════════════════════════════════════════════
#  Drawing helpers
# ══════════════════════════════════════════════════════════════════════════════

def fill_rect(surf, color, rect, radius=0):
    # border_radius only available in pygame >= 2.0
    try:
        if radius > 0:
            pygame.draw.rect(surf, color, rect, border_radius=radius)
        else:
            pygame.draw.rect(surf, color, rect)
    except TypeError:
        pygame.draw.rect(surf, color, rect)

def stroke_rect(surf, color, rect, w=1, radius=0):
    try:
        if radius > 0:
            pygame.draw.rect(surf, color, rect, w, border_radius=radius)
        else:
            pygame.draw.rect(surf, color, rect, w)
    except TypeError:
        pygame.draw.rect(surf, color, rect, w)

def text_at(surf, txt, font, color, x, y, anchor='tl'):
    s = font.render(str(txt), True, color)
    r = s.get_rect()
    if   anchor == 'tl': r.topleft   = (x, y)
    elif anchor == 'tc': r.midtop    = (x, y)
    elif anchor == 'tr': r.topright  = (x, y)
    elif anchor == 'ml': r.midleft   = (x, y)
    elif anchor == 'mc': r.center    = (x, y)
    elif anchor == 'bl': r.bottomleft= (x, y)
    elif anchor == 'bc': r.midbottom = (x, y)
    elif anchor == 'br': r.bottomright=(x,y)
    surf.blit(s, r)
    return r

def gradient_bar_surf(w, h, col_bot, col_top):
    """Create a vertical gradient surface."""
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    for y in range(h):
        t = y / max(h-1, 1)
        col = C.lerp_color(col_top, col_bot, t)
        pygame.draw.line(surf, col, (0, y), (w, y))
    return surf

def clickable_rects() -> dict:
    """Returns {key: pygame.Rect} for touch detection."""
    rects = {}
    # Bar chart areas
    for i, key in enumerate(C.BAR_KEYS):
        x = C.RIGHT_X + C.BAR_GAP + i*(C.BAR_W + C.BAR_GAP)
        rects[key] = pygame.Rect(x, 0, C.BAR_W + C.BAR_GAP, C.H)
    # Left panel values
    for i, (k, unit, label, col) in enumerate(C.LEFT_CHANNELS):
        y = 92 + i * 62
        rects[k] = pygame.Rect(0, y, C.LEFT_W, 56)
    # Menu button (bottom-left)
    rects['__menu__'] = pygame.Rect(2, C.H-36, 90, 34)
    return rects


# ══════════════════════════════════════════════════════════════════════════════
#  INIT screen
# ══════════════════════════════════════════════════════════════════════════════

def draw_init():
    screen.fill(C.BG)
    text_at(screen, "Initialising sensors…", FNT_BIG, C.CYAN, C.W//2, 30, 'tc')
    y = 70
    for key, status in init_status:
        ok = status == 'OK'
        col = C.GREEN if ok else (C.RED if status == 'FAIL' else C.YELLOW)
        name = S.REGISTRY[key].name
        addr = S.REGISTRY[key].addr
        text_at(screen, f"  {name:8s} {addr}  →  {status}", FNT_MED,
                col, C.W//2, y, 'tc')
        y += 26

    if len(init_status) == 4:
        n_ok = sum(1 for _,s in init_status if s=='OK')
        mode = 'STUB/DEMO' if S.STUB_MODE else f'{n_ok}/4 sensors'
        col  = C.YELLOW if S.STUB_MODE else C.GREEN
        text_at(screen, f"Mode: {mode} — starting…", FNT_BIG, col, C.W//2, y+10,'tc')
    pygame.display.update()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN screen
# ══════════════════════════════════════════════════════════════════════════════

def draw_main():
    screen.fill(C.BG)
    data = latest.copy()

    # ── Left panel background ─────────────────────────────────────────────────
    fill_rect(screen, C.PANEL, (0, 0, C.LEFT_W, C.H), radius=0)

    # Clock
    now = datetime.now()
    text_at(screen, now.strftime('%H:%M'), FNT_CLOCK, C.WHITE,
            C.LEFT_W//2, 8, 'tc')
    text_at(screen, now.strftime('%d.%m.%Y'), FNT_SMALL, C.MUTED,
            C.LEFT_W//2, 56, 'tc')

    # Divider line after clock
    pygame.draw.line(screen, C.BORDER, (6, 74), (C.LEFT_W-6, 74), 1)

    # STUB badge
    if S.STUB_MODE:
        fill_rect(screen, C.YELLOW_D, (4, 76, 80, 16), radius=4)
        text_at(screen, 'DEMO', FNT_TINY, C.YELLOW, 8, 78)

    # Sensor online dots
    # Sensor online dots with tiny labels
    dot_x = C.LEFT_W - 8
    for i, key in enumerate(['bmp280','scd41','sgp41','sps30']):
        ok  = S.REGISTRY[key].online
        col = C.GREEN if ok else C.RED
        pygame.draw.circle(screen, col, (dot_x, 82 + i*9), 3)

    # Left channel values
    for i, (key, unit, label, col) in enumerate(C.LEFT_CHANNELS):
        y = 92 + i * 62
        val = data.get(key)

        # Card background
        active = val is not None
        bg = C.PANEL2 if active else C.PANEL
        fill_rect(screen, bg, (4, y-2, C.LEFT_W-8, 54), radius=6)
        stroke_rect(screen, col if active else C.BORDER,
                    (4, y-2, C.LEFT_W-8, 54), 1, radius=6)

        # Left accent bar
        fill_rect(screen, col if active else C.MUTED,
                  (4, y-2, 3, 54), radius=2)

        # Label
        
        src_suffix = ''
        if key == 'temperature':
            src_suffix = '  [' + S.SOURCE_MAP.get('temperature','bmp280').upper() + ']'
        text_at(screen, label + src_suffix, FNT_TINY, C.MUTED, 12, y, 'tl')

        # Value
        if val is not None:
            txt = f"{val:.1f}" if isinstance(val, float) else str(val)
            text_at(screen, txt, FNT_BIG, col, 12, y+14, 'tl')
        else:
            text_at(screen, '—', FNT_BIG, C.MUTED, 12, y+14, 'tl')

        # Unit
        text_at(screen, unit, FNT_SMALL, C.MUTED, C.LEFT_W-10, y+14, 'tr')

    # Menu button
    fill_rect(screen, C.PANEL2, (2, C.H-36, 90, 34), radius=6)
    stroke_rect(screen, C.BORDER, (2, C.H-36, 90, 34), 1, radius=6)
    text_at(screen, '⚙ Menu', FNT_MED, C.TEXT2, 48, C.H-18, 'mc')

    # ── Divider ───────────────────────────────────────────────────────────────
    pygame.draw.line(screen, C.BORDER,
                     (C.LEFT_W, 0), (C.LEFT_W, C.H), C.DIVIDER)

    # ── Right panel — bar charts ──────────────────────────────────────────────
    _draw_bars(data)

    pygame.display.update()


def _draw_bars(data):
    # Right panel background
    fill_rect(screen, C.BG, (C.RIGHT_X, 0, C.RIGHT_W, C.H), radius=0)

    # Small legend/header at the top, not at the cropped right edge.
    text_at(screen, 'Air quality', FNT_SMALL, C.TEXT2, C.RIGHT_X + 8, 6, 'tl')
    lx = C.RIGHT_X + 92
    for i, (lbl, col) in enumerate([('Good',C.GREEN),('Mod',C.YELLOW),('Poor',C.ORANGE),('Bad',C.RED)]):
        x = lx + i * 44
        fill_rect(screen, col, (x, 8, 8, 8), radius=2)
        text_at(screen, lbl, FNT_TINY, C.TEXT2, x + 10, 5, 'tl')

    groups = [
        ('Gases', [0,1,2]),
        ('Particulates  µg/m³', [3,4,5,6]),
    ]

    # Draw group backgrounds and labels.
    for glabel, indices in groups:
        x0 = C.RIGHT_X + C.BAR_GAP + indices[0]*(C.BAR_W + C.BAR_GAP) - 2
        x1 = C.RIGHT_X + C.BAR_GAP + (indices[-1]+1)*(C.BAR_W + C.BAR_GAP)
        gw = x1 - x0
        fill_rect(screen, C.PANEL, (x0, 24, gw, C.H-26), radius=4)
        stroke_rect(screen, C.BORDER, (x0, 24, gw, C.H-26), 1, radius=4)
        text_at(screen, glabel, FNT_TINY, C.MUTED, (x0+x1)//2, C.H-4, 'bc')

    # Draw each bar.
    for i, key in enumerate(C.BAR_KEYS):
        ch = C.CHANNELS[key]
        x = C.RIGHT_X + C.BAR_GAP + i*(C.BAR_W + C.BAR_GAP)
        val = data.get(key)
        online = _key_sensor_online(key)

        fill_rect(screen, C.PANEL2, (x, C.BAR_TOP, C.BAR_W, C.BAR_H), radius=4)

        if val is not None and online:
            frac = min(float(val) / float(ch['max']), 1.0)
            bh = max(2, int(C.BAR_H * frac))
            y_top = C.BAR_BOT - bh
            col_bot = C.gradient_color(key, val)
            col_top = C.lerp_color(col_bot, C.GREEN_D, 0.55)
            bar_surf = gradient_bar_surf(C.BAR_W, bh, col_bot, col_top)
            screen.blit(bar_surf, (x, y_top))
            pygame.draw.line(screen, _bright(col_bot), (x, y_top), (x+C.BAR_W-1, y_top), 2)
            fmt = f"{val:.0f}" if val >= 10 else f"{val:.1f}"
            text_at(screen, fmt, FNT_SMALL, _bright(col_bot), x + C.BAR_W//2, max(26, y_top - 4), 'bc')
        else:
            for yy in range(C.BAR_TOP + 5, C.BAR_BOT - 4, 9):
                pygame.draw.line(screen, C.BORDER, (x+3, yy), (x+C.BAR_W-4, yy), 1)

        lbl_col = ch['color'] if (val is not None and online) else C.MUTED
        text_at(screen, ch['label'], FNT_TINY, lbl_col, x + C.BAR_W//2, C.BAR_BOT + 4, 'tc')
        text_at(screen, ch['unit'], FNT_TINY, C.MUTED, x + C.BAR_W//2, C.BAR_BOT + 16, 'tc')


def _bright(col):
    return tuple(min(255, int(c*1.4)) for c in col)

def _key_sensor_online(key):
    ch = C.CHANNELS[key]
    g  = ch['group']
    if g == 'air' and key == 'co2':      return S.REGISTRY['scd41'].online
    if g == 'air':                        return S.REGISTRY['sgp41'].online
    if g == 'pm':                         return S.REGISTRY['sps30'].online
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  CHART screen
# ══════════════════════════════════════════════════════════════════════════════

def draw_chart(key: str):
    screen.fill(C.BG)
    ch  = C.CHANNELS.get(key) or {'label':key,'unit':'','color':C.ACCENT,'max':100,'zones':[]}
    col = ch['color']
    hrs = float(db.get_setting('graph_hours', '24'))

    # Title
    text_at(screen, f"{ch['label']} — last {int(hrs)}h", FNT_BIG, col, C.W//2, 6, 'tc')

    # Load data
    rows = db.query(key, hrs)
    if not rows:
        # Use in-memory history
        rows = [(i*5, v) for i, v in enumerate(history.get(key, []))]
    
    # Back button
    fill_rect(screen, C.PANEL2, (2, 2, 56, 26), radius=5)
    text_at(screen, '← Back', FNT_SMALL, C.TEXT2, 30, 14, 'mc')

    if len(rows) < 2:
        text_at(screen, 'Not enough data yet', FNT_BIG, C.MUTED, C.W//2, C.H//2, 'mc')
        pygame.display.update()
        return

    # Chart area
    cx, cy = 42, 34
    cw, ch_ = C.W - cx - 10, C.H - cy - 40

    # Value range
    vals = [v for _, v in rows]
    mn, mx = min(vals), max(vals)
    pad = (mx-mn)*0.1 or 1
    lo, hi = mn-pad, mx+pad

    # Grid + Y labels
    for i in range(5):
        t    = i/4
        yv   = lo + (hi-lo)*t
        yp   = cy + ch_ - int(ch_*t)
        pygame.draw.line(screen, C.BORDER, (cx, yp), (cx+cw, yp), 1)
        text_at(screen, f"{yv:.0f}", FNT_TINY, C.MUTED, cx-3, yp, 'mr')

    # X axis
    pygame.draw.line(screen, C.BORDER, (cx, cy+ch_), (cx+cw, cy+ch_), 1)
    pygame.draw.line(screen, C.BORDER, (cx, cy),     (cx, cy+ch_),     1)

    # Plot line + fill
    t0 = rows[0][0];  t1 = rows[-1][0]
    dt = max(t1-t0, 1)

    pts = []
    for ts, v in rows:
        px = cx + int((ts-t0)/dt * cw)
        py = cy + ch_ - int(((v-lo)/(hi-lo))*ch_)
        pts.append((px, py))

    # Fill under line
    if len(pts) > 1:
        fill_pts = [(cx, cy+ch_)] + pts + [(cx+cw, cy+ch_)]
        fill_col = tuple(int(c*0.35) for c in col)
        pygame.draw.polygon(screen, fill_col, fill_pts)
        pygame.draw.lines(screen, col, False, pts, 2)

    # Stats
    avg = sum(vals)/len(vals)
    txt = f"Min:{mn:.1f}  Max:{mx:.1f}  Avg:{avg:.1f}  n={len(vals)}"
    text_at(screen, txt, FNT_TINY, C.TEXT2, C.W//2, C.H-12, 'bc')

    # Threshold lines
    for limit, label, lcol in ch.get('zones',[]):
        if lo <= limit <= hi:
            yp = cy + ch_ - int(((limit-lo)/(hi-lo))*ch_)
            pygame.draw.line(screen, lcol, (cx, yp), (cx+cw, yp), 1)
            text_at(screen, label, FNT_TINY, lcol, cx+cw-2, yp-2, 'br')

    # Time labels
    if rows:
        def _fmt(ts):
            try: return datetime.fromtimestamp(ts).strftime('%H:%M')
            except: return ''
        text_at(screen, _fmt(rows[0][0]),   FNT_TINY, C.MUTED, cx,    cy+ch_+3, 'tl')
        text_at(screen, _fmt(rows[-1][0]),  FNT_TINY, C.MUTED, cx+cw, cy+ch_+3, 'tr')

    pygame.display.update()


# ══════════════════════════════════════════════════════════════════════════════
#  MENU / Settings screen
# ══════════════════════════════════════════════════════════════════════════════

# Menu state vars
_menu_bus   = [int(db.get_setting('i2c_bus','1'))]
_menu_poll  = [int(db.get_setting('poll_sec','5'))]
_menu_hours = [int(db.get_setting('graph_hours','24'))]
_menu_msg   = ['']
_menu_tab   = [db.get_setting('menu_tab','general')]
_menu_source_buttons = []
_menu_buttons_extra  = []

# Time settings.  Network time means normal Raspberry/NTP behaviour.
_time_mode = [db.get_setting('time_mode','network')]  # network | manual
_manual_h  = [int(db.get_setting('manual_hour','12'))]
_manual_m  = [int(db.get_setting('manual_min','00'))]
_manual_d  = [int(db.get_setting('manual_day','29'))]
_manual_mo = [int(db.get_setting('manual_month','6'))]
_manual_y  = [int(db.get_setting('manual_year','2026'))]

TABS = [
    ('general', 'General'),
    ('sensors', 'Sensors'),
    ('time',    'Time'),
]

def _tab_button(x, key, label):
    sel = (_menu_tab[0] == key)
    col = C.ACCENT if sel else C.PANEL2
    fill_rect(screen, col, (x, 30, 86, 24), radius=5)
    stroke_rect(screen, C.ACCENT if sel else C.BORDER, (x, 30, 86, 24), 1, radius=5)
    text_at(screen, label, FNT_TINY, C.WHITE if sel else C.TEXT2, x+43, 42, 'mc')
    return (key, pygame.Rect(x,30,86,24))

def _small_button(label, rect, col=C.ACCENT):
    fill_rect(screen, tuple(int(c*0.28) for c in col), rect, radius=5)
    stroke_rect(screen, col, rect, 1, radius=5)
    text_at(screen, label, FNT_TINY, col, rect[0]+rect[2]//2, rect[1]+rect[3]//2, 'mc')
    return (label, col, rect)

def draw_menu():
    global _menu_source_buttons, _menu_buttons_extra
    _menu_source_buttons = []
    _menu_buttons_extra = []
    screen.fill(C.BG)
    fill_rect(screen, C.PANEL, (0, 0, C.W, C.H))

    text_at(screen, 'AirStation Menu', FNT_BIG, C.CYAN, C.W//2, 4, 'tc')
    fill_rect(screen, C.PANEL2, (2, 2, 58, 24), radius=4)
    text_at(screen, '< Back', FNT_SMALL, C.TEXT2, 31, 14, 'mc')

    tab_rects = []
    x = 8
    for key, label in TABS:
        tab_rects.append(_tab_button(x, key, label))
        x += 92

    pygame.draw.line(screen, C.BORDER, (8, 60), (C.W-8, 60), 1)

    if _menu_tab[0] == 'general':
        _draw_menu_general()
    elif _menu_tab[0] == 'sensors':
        _draw_menu_sensors()
    elif _menu_tab[0] == 'time':
        _draw_menu_time()

    # bottom persistent actions
    by = 286
    base_buttons = [
        _small_button('Save', (322, by, 68, 28), C.GREEN),
        _small_button('Re-init', (396, by, 76, 28), C.YELLOW),
    ]
    if _menu_msg[0]:
        text_at(screen, _menu_msg[0], FNT_TINY, C.YELLOW, 10, C.H-11, 'bl')

    pygame.display.update()
    return [('__tab__', tab_rects, None)] + _menu_buttons_extra + base_buttons


def _draw_menu_general():
    y = 72
    text_at(screen, 'Basic settings', FNT_SMALL, C.TEXT2, 14, y)
    y += 24

    text_at(screen, 'I2C bus', FNT_TINY, C.MUTED, 20, y)
    text_at(screen, str(_menu_bus[0]), FNT_MED, C.TEXT, 116, y-2)
    _menu_buttons_extra.append(_small_button('bus-', (160, y-5, 44, 24), C.ORANGE))
    _menu_buttons_extra.append(_small_button('bus+', (210, y-5, 44, 24), C.GREEN))

    y += 34
    text_at(screen, 'Poll sec', FNT_TINY, C.MUTED, 20, y)
    text_at(screen, str(_menu_poll[0]), FNT_MED, C.TEXT, 116, y-2)
    _menu_buttons_extra.append(_small_button('poll-', (160, y-5, 44, 24), C.ORANGE))
    _menu_buttons_extra.append(_small_button('poll+', (210, y-5, 44, 24), C.GREEN))

    y += 34
    text_at(screen, 'Graph window', FNT_TINY, C.MUTED, 20, y)
    for j, h in enumerate([6,12,24,48]):
        bx = 128 + j*54
        sel = (_menu_hours[0] == h)
        col = C.ACCENT if sel else C.PANEL2
        fill_rect(screen, col, (bx, y-5, 46, 24), radius=4)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, (bx, y-5, 46, 24), 1, radius=4)
        text_at(screen, '%sh' % h, FNT_TINY, C.WHITE if sel else C.TEXT2, bx+23, y+7, 'mc')
        _menu_buttons_extra.append(('graph:%s'%h, C.ACCENT, (bx, y-5, 46, 24)))

    y += 44
    text_at(screen, 'Display mapping', FNT_SMALL, C.TEXT2, 14, y)
    y += 26
    text_at(screen, 'Temperature from', FNT_TINY, C.MUTED, 20, y)
    t_src = S.SOURCE_MAP.get('temperature','bmp280')
    for j, src in enumerate(['bmp280','scd41']):
        bx = 160 + j*86
        sel = (t_src == src)
        rect = (bx, y-6, 78, 26)
        fill_rect(screen, C.ACCENT if sel else C.PANEL2, rect, radius=4)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=4)
        text_at(screen, src.upper(), FNT_TINY, C.WHITE if sel else C.TEXT2, bx+39, y+7, 'mc')
        _menu_source_buttons.append((src, rect))

    y += 36
    text_at(screen, 'Cursor', FNT_TINY, C.MUTED, 20, y)
    text_at(screen, 'visible / pointer shown', FNT_TINY, C.GREEN, 160, y)


def _draw_menu_sensors():
    y = 72
    text_at(screen, 'Sensor technical status', FNT_SMALL, C.TEXT2, 14, y)
    y += 24
    text_at(screen, 'Name', FNT_TINY, C.MUTED, 16, y)
    text_at(screen, 'Addr', FNT_TINY, C.MUTED, 86, y)
    text_at(screen, 'Status', FNT_TINY, C.MUTED, 150, y)
    text_at(screen, 'Last read', FNT_TINY, C.MUTED, 226, y)
    text_at(screen, 'Error', FNT_TINY, C.MUTED, 310, y)
    y += 17
    for key in ['bmp280','scd41','sgp41','sps30']:
        info = S.REGISTRY[key]
        col = C.GREEN if info.online else C.RED
        lu = datetime.fromtimestamp(info.last_read).strftime('%H:%M:%S') if info.last_read else '-'
        err = (info.error_msg or '')[:22]
        fill_rect(screen, C.PANEL2, (8, y-3, C.W-16, 22), radius=4)
        text_at(screen, info.name, FNT_TINY, col, 16, y)
        text_at(screen, info.addr, FNT_TINY, C.TEXT2, 86, y)
        text_at(screen, 'OK' if info.online else 'OFF', FNT_TINY, col, 150, y)
        text_at(screen, lu, FNT_TINY, C.MUTED, 226, y)
        text_at(screen, err, FNT_TINY, C.YELLOW if err else C.MUTED, 310, y)
        y += 26

    _menu_buttons_extra.append(_small_button('Scan I2C', (14, 228, 96, 32), C.CYAN))
    _menu_buttons_extra.append(_small_button('Restart SCD41', (120, 228, 122, 32), C.YELLOW))
    _menu_buttons_extra.append(_small_button('Restart SPS30', (252, 228, 122, 32), C.YELLOW))
    _menu_buttons_extra.append(_small_button('Purge DB', (384, 228, 82, 32), C.RED))

    # Raw diagnostic readout — helps tell apart "sensor not reacting" (code bug)
    # from "air is genuinely clean / stable" (no bug, just low variance).
    with data_lock:
        d = latest.copy()
    sraw_voc = d.get('sraw_voc')
    sraw_nox = d.get('sraw_nox')
    voc_idx  = d.get('voc_index')
    nox_idx  = d.get('nox_index')
    sgp_state = d.get('sgp41_state')
    sgp_algo  = d.get('sgp41_algo')
    pm_str = "  ".join(
        f"{lbl}={d.get(k):.2f}" if d.get(k) is not None else f"{lbl}=-"
        for lbl, k in [('PM1',  'pm1_0'), ('PM2.5','pm2_5'),
                       ('PM4',  'pm4_0'), ('PM10', 'pm10')]
    )
    diag1 = f"SGP41 raw: SRAW_VOC={sraw_voc}  SRAW_NOX={sraw_nox}  ->  idx VOC={voc_idx} NOx={nox_idx}  {sgp_state or ''}/{sgp_algo or ''}"
    diag2 = f"SPS30 raw: {pm_str}"
    text_at(screen, diag1, FNT_TINY, C.CYAN,   14, 250)
    text_at(screen, diag2, FNT_TINY, C.CYAN,   14, 262)

    text_at(screen, 'Tip: if graph freezes touch, press ESC/Back and use Touch -> Calibrate.', FNT_TINY, C.MUTED, 14, 282)


def _draw_menu_time():
    y = 72
    text_at(screen, 'Time source', FNT_SMALL, C.TEXT2, 14, y)
    y += 30
    for j, mode in enumerate(['network','manual']):
        bx = 24 + j*122
        sel = (_time_mode[0] == mode)
        fill_rect(screen, C.ACCENT if sel else C.PANEL2, (bx, y-8, 112, 28), radius=5)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, (bx, y-8, 112, 28), 1, radius=5)
        mark = '✓ ' if sel else '□ '
        text_at(screen, mark + mode.upper(), FNT_TINY, C.WHITE if sel else C.TEXT2, bx+56, y+6, 'mc')
        _menu_buttons_extra.append(('time:'+mode, C.ACCENT, (bx, y-8, 112, 28)))

    y += 40
    text_at(screen, 'Manual date/time', FNT_SMALL, C.TEXT2, 14, y)
    y += 26

    # v7: compact 2-row layout. Old x positions overlapped on 480x320
    # because each field used 90 px value + two 28 px buttons.
    fields = [
        ('year', _manual_y, 2020, 2099, 18,  y),
        ('month',_manual_mo,1,12,   172, y),
        ('day',  _manual_d, 1,31,   326, y),
        ('hour', _manual_h, 0,23,   18,  y+48),
        ('min',  _manual_m, 0,59,   172, y+48),
    ]
    value_w, btn_w, gap = 74, 24, 3
    for name, ref, lo, hi, x, yy in fields:
        text_at(screen, name, FNT_TINY, C.MUTED, x, yy-15)
        fill_rect(screen, C.PANEL2, (x, yy, value_w, 27), radius=5)
        text_at(screen, str(ref[0]).zfill(2) if name!='year' else str(ref[0]), FNT_MED, C.TEXT, x+value_w//2, yy+14, 'mc')
        minus_rect = (x+value_w+gap, yy, btn_w, 27)
        plus_rect  = (x+value_w+gap+btn_w+gap, yy, btn_w, 27)
        _menu_buttons_extra.append((name+'-', C.ORANGE, minus_rect))
        _menu_buttons_extra.append((name+'+', C.GREEN, plus_rect))
        _small_button('-', minus_rect, C.ORANGE)
        _small_button('+', plus_rect, C.GREEN)

    text_at(screen, 'Network mode uses Raspberry Pi / NTP. Manual mode disables NTP.', FNT_TINY, C.MUTED, 18, 231)
    _menu_buttons_extra.append(_small_button('Apply manual time', (282, 250, 178, 30), C.GREEN))


def _draw_menu_touch():
    y = 72
    text_at(screen, 'Touchscreen', FNT_SMALL, C.TEXT2, 14, y)
    y += 28
    text_at(screen, 'Cursor is visible in v6. App does not hide it anymore.', FNT_TINY, C.GREEN, 18, y)
    y += 24
    text_at(screen, 'Calibration opens external xinput_calibrator.', FNT_TINY, C.TEXT2, 18, y)
    y += 18
    text_at(screen, 'After calibration reboot Raspberry Pi.', FNT_TINY, C.MUTED, 18, y)

    _menu_buttons_extra.append(_small_button('Calibrate touch', (22, 146, 150, 36), C.CYAN))
    _menu_buttons_extra.append(_small_button('Open terminal', (188, 146, 130, 36), C.YELLOW))
    _menu_buttons_extra.append(_small_button('Test touch dots', (334, 146, 124, 36), C.ACCENT))

    text_at(screen, 'Manual command:', FNT_TINY, C.MUTED, 18, 210)
    text_at(screen, 'DISPLAY=:0.0 xinput_calibrator', FNT_TINY, C.TEXT2, 18, 228)
    text_at(screen, 'Config: /etc/X11/xorg.conf.d/99-calibration.conf', FNT_TINY, C.TEXT2, 18, 246)


def _apply_manual_time():
    import subprocess
    cmd = 'sudo timedatectl set-ntp false; sudo date -s "%04d-%02d-%02d %02d:%02d:00"' % (
        _manual_y[0], _manual_mo[0], _manual_d[0], _manual_h[0], _manual_m[0])
    try:
        subprocess.call(['/bin/bash','-lc', cmd])
        _menu_msg[0] = 'Manual time applied'
    except Exception as e:
        _menu_msg[0] = 'Time error: ' + str(e)[:24]

def _set_time_mode(mode):
    import subprocess
    _time_mode[0] = mode
    try:
        if mode == 'network':
            subprocess.call(['/bin/bash','-lc','sudo timedatectl set-ntp true'])
            _menu_msg[0] = 'Network time enabled'
        else:
            subprocess.call(['/bin/bash','-lc','sudo timedatectl set-ntp false'])
            _menu_msg[0] = 'Manual time mode'
    except Exception as e:
        _menu_msg[0] = 'NTP error: ' + str(e)[:24]

def _launch_touch_calibration():
    import subprocess, os
    _menu_msg[0] = 'Launching calibrator...'
    script = os.path.join(os.path.dirname(__file__), 'touch_calibrate.sh')
    try:
        subprocess.Popen(['/bin/bash', script])
        pygame.event.post(pygame.event.Event(pygame.QUIT, {}))
    except Exception as e:
        _menu_msg[0] = 'Calib error: ' + str(e)[:24]

def menu_hit(pos, buttons):
    global state, chart_key
    # Back
    if pygame.Rect(2,2,58,24).collidepoint(pos):
        state = State.MAIN; return

    # Tabs
    for item in buttons:
        if item[0] == '__tab__':
            for key, rect in item[1]:
                if rect.collidepoint(pos):
                    _menu_tab[0] = key
                    db.set_setting('menu_tab', key)
                    return

    # Source mapping buttons
    for src, rect in _menu_source_buttons:
        if pygame.Rect(rect).collidepoint(pos):
            S.SOURCE_MAP['temperature'] = src
            _menu_msg[0] = 'Temperature <- ' + src.upper()
            return

    for label, col, rect in buttons:
        if label == '__tab__':
            continue
        if not pygame.Rect(rect).collidepoint(pos):
            continue

        if label == 'Save':
            db.set_setting('i2c_bus',     str(_menu_bus[0]))
            db.set_setting('poll_sec',    str(_menu_poll[0]))
            db.set_setting('graph_hours', str(_menu_hours[0]))
            db.set_setting('temp_source', S.SOURCE_MAP.get('temperature','bmp280'))
            db.set_setting('time_mode',   _time_mode[0])
            db.set_setting('manual_hour', str(_manual_h[0]))
            db.set_setting('manual_min',  str(_manual_m[0]))
            db.set_setting('manual_day',  str(_manual_d[0]))
            db.set_setting('manual_month',str(_manual_mo[0]))
            db.set_setting('manual_year', str(_manual_y[0]))
            _menu_msg[0] = 'Saved'
        elif label == 'Re-init':
            def _do():
                _menu_msg[0] = 'Re-init all...'
                S.init_all(bus_num=_menu_bus[0])
                _menu_msg[0] = 'Done'
            threading.Thread(target=_do, daemon=True).start()
        elif label == 'bus-': _menu_bus[0] = max(0, _menu_bus[0]-1)
        elif label == 'bus+': _menu_bus[0] = min(9, _menu_bus[0]+1)
        elif label == 'poll-': _menu_poll[0] = max(1, _menu_poll[0]-1)
        elif label == 'poll+': _menu_poll[0] = min(60, _menu_poll[0]+1)
        elif label.startswith('graph:'):
            _menu_hours[0] = int(label.split(':')[1])
        elif label == 'Scan I2C':
            scan_result.clear(); state = State.I2CSCAN
        elif label == 'Restart SCD41':
            def _do_scd():
                _menu_msg[0] = 'Restarting SCD41...'
                try:
                    S.REGISTRY['scd41'].online = False
                    ok = S.restart_scd41('manual menu')
                    _menu_msg[0] = 'SCD41 OK' if ok else 'SCD41 still OFF'
                except Exception as e:
                    _menu_msg[0] = 'SCD err: ' + str(e)[:20]
            threading.Thread(target=_do_scd, daemon=True).start()
        elif label == 'Restart SPS30':
            def _do_sps():
                _menu_msg[0] = 'Restarting SPS30...'
                try:
                    S.REGISTRY['sps30'].online = False
                    ok = S.restart_sps30(_menu_bus[0])
                    _menu_msg[0] = 'SPS30 OK' if ok else 'SPS30 OFF'
                except Exception as e:
                    _menu_msg[0] = 'SPS err: ' + str(e)[:20]
            threading.Thread(target=_do_sps, daemon=True).start()
        elif label == 'Purge DB':
            db.purge(7); _menu_msg[0] = 'DB purged'
        elif label.startswith('time:'):
            _set_time_mode(label.split(':')[1])
        elif label in ['year-','year+','month-','month+','day-','day+','hour-','hour+','min-','min+']:
            fields = {'year':(_manual_y,2020,2099),'month':(_manual_mo,1,12),'day':(_manual_d,1,31),'hour':(_manual_h,0,23),'min':(_manual_m,0,59)}
            name = label[:-1]; op = label[-1]
            ref, lo, hi = fields[name]
            ref[0] = max(lo, min(hi, ref[0] + (1 if op=='+' else -1)))
        elif label == 'Apply manual time':
            threading.Thread(target=_apply_manual_time, daemon=True).start()
        elif label == 'Calibrate touch':
            _launch_touch_calibration()
        elif label == 'Open terminal':
            import subprocess
            subprocess.Popen(['/bin/bash','-lc','lxterminal || x-terminal-emulator || xterm'])
        elif label == 'Test touch dots':
            _menu_msg[0] = 'Touch test: use cursor/touch and watch pointer'
        return


# ══════════════════════════════════════════════════════════════════════════════
#  I2C SCAN screen
# ══════════════════════════════════════════════════════════════════════════════

KNOWN_ADDRS = {
    0x76:'BMP280', 0x77:'BMP280', 0x62:'SCD41',
    0x59:'SGP41',  0x69:'SPS30',  0x3C:'OLED',
    0x48:'ADS1115',0x68:'MPU6050/DS3231',
}

def draw_i2cscan():
    screen.fill(C.BG)
    fill_rect(screen, C.PANEL, (0,0,C.W,C.H))
    text_at(screen, f'I2C Bus {_menu_bus[0]} Scan', FNT_BIG, C.CYAN, C.W//2, 8,'tc')
    fill_rect(screen, C.PANEL2, (2,2,56,22), radius=4)
    text_at(screen, '← Back', FNT_SMALL, C.TEXT2, 30,12,'mc')

    if not scan_result:
        text_at(screen, 'Scanning…', FNT_BIG, C.YELLOW, C.W//2, C.H//2,'mc')
    elif scan_result == [(-1, 'error')]:
        text_at(screen, 'I2C bus error', FNT_BIG, C.RED, C.W//2, C.H//2,'mc')
    else:
        y = 36
        text_at(screen, f'Found {len(scan_result)} device(s):', FNT_MED, C.TEXT, 16, y)
        y += 22
        for addr, desc in scan_result:
            col = C.GREEN if desc != '?' else C.MUTED
            text_at(screen, f"  0x{addr:02X}  {desc}", FNT_MED, col, 24, y)
            y += 20
        if not scan_result:
            text_at(screen, '  No devices found', FNT_MED, C.RED, 24, y)

    pygame.display.update()


# ══════════════════════════════════════════════════════════════════════════════
#  Sensor data callback + history
# ══════════════════════════════════════════════════════════════════════════════

def on_data(data: dict):
    global latest
    with data_lock:
        latest = data.copy()
    for k, v in data.items():
        if k in history and v is not None:
            history[k].append(v)
    if not args.nodb:
        try: db.insert(data)
        except: pass
    # Signal main loop to redraw
    try:
        pygame.event.post(pygame.event.Event(pygame.USEREVENT, {}))
    except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════

def run_init():
    global state, init_status
    init_status = []
    draw_init()

    def _init_thread():
        def _progress(key):
            init_status.append((key, '…'))
            pygame.event.post(pygame.event.Event(pygame.USEREVENT, {}))

        results = S.init_all(
            bus_num=args.bus if args.bus is not None else int(db.get_setting('i2c_bus','1')),
            on_progress=_progress)

        # Update status entries
        new = []
        for key, ok in results.items():
            new.append((key, 'OK' if ok else 'FAIL'))
        init_status.clear()
        init_status.extend(new)
        pygame.event.post(pygame.event.Event(pygame.USEREVENT, {}))
        time.sleep(1.5)
        state_holder[0] = State.MAIN

    state_holder = [None]
    t = threading.Thread(target=_init_thread, daemon=True)
    t.start()

    # Wait until init done
    while state_holder[0] is None:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT: sys.exit(0)
            if ev.type == pygame.USEREVENT: draw_init()
        clock_fps.tick(10)
    return state_holder[0]


def main():
    global state, chart_key, scan_result

    # Init screen
    state = run_init()

    # Start sensor polling
    poll_interval = int(db.get_setting('poll_sec', str(C.POLL_INTERVAL)))
    poller = S.Poller(interval=poll_interval)
    poller.set_callback(on_data)
    poller.start()
    # Фоновий інтернет-модуль (перевірка з'єднання + Open-Meteo). Безпечно офлайн.
    try:
        net.start(lambda: (float(db.get_setting('lat', '48.14')), float(db.get_setting('lon', '11.68'))))
    except Exception:
        pass

    # NOTE: no extra direct S.read_all() call here. The Poller's _run() loop
    # already performs an immediate first read as soon as the thread starts
    # (see sensors.py Poller._run — it reads, sleeps, repeats). Calling
    # read_all() again right here used to fire a second full 4-sensor sweep
    # at almost the exact same instant as the Poller's own first sweep. Both
    # were lock-protected so they never truly overlapped, but the second
    # sweep would start the moment the first one released the lock — i.e.
    # back-to-back with zero gap, on a 10kHz bus that needs settle time
    # between full sweeps. This produced the SGP41 "Remote I/O error" that
    # was 100% reproducible inside the running app but never reproduced in
    # any standalone test, because no standalone test was doing two
    # consecutive full sweeps with zero gap.

    _menu_buttons = []

    _last_state   = None
    _last_data_ts = 0.0
    _needs_redraw = True
    _last_clock_sec = -1
    _FINGERDOWN   = getattr(pygame, 'FINGERDOWN', None)
    _FINGERUP     = getattr(pygame, 'FINGERUP', None)
    _FINGERMOTION = getattr(pygame, 'FINGERMOTION', None)
    _touch_start  = None

    while True:
        _dirty = False

        for ev in pygame.event.get():
            if ev.type == QUIT:
                poller.stop(); pygame.quit(); sys.exit(0)

            if ev.type == pygame.USEREVENT:
                _dirty = True   # sensor data or scan result arrived

            if ev.type == KEYDOWN:
                if ev.key == K_ESCAPE:
                    if state in (State.CHART, State.MENU, State.I2CSCAN, State.GRAPHSEL, State.DATA, State.ABOUT):
                        state = State.MAIN
                    else:
                        poller.stop(); pygame.quit(); sys.exit(0)
                if ev.key == K_m: state = State.MENU
                if ev.key == K_q:
                    poller.stop(); pygame.quit(); sys.exit(0)
                _dirty = True

            if ev.type == MOUSEBUTTONDOWN or (_FINGERDOWN and ev.type == _FINGERDOWN):
                if _FINGERDOWN and ev.type == _FINGERDOWN:
                    pos = (int(ev.x * C.W), int(ev.y * C.H))
                else:
                    pos = ev.pos

                if _kbd['active']:
                    _kbd_hit(pos); _dirty = True
                elif state == State.EDITOR:
                    _editor_down(pos); _dirty = True
                elif state == State.MAIN:
                    # На головному екрані рішення приймаємо на відпусканні (UP),
                    # щоб розрізнити тап, свайп-вправо (панель) і свайп-вліво (режим).
                    _touch_start = (pos, time.time())

                elif state == State.CHART:
                    if pygame.Rect(2,2,180,74).collidepoint(pos):
                        state = State.MAIN

                elif state == State.GRAPHSEL:
                    hit = graph_select_hit(pos)
                    if hit == '__back__':
                        state = State.MAIN
                    elif hit:
                        chart_key = hit
                        state = State.CHART

                elif state in (State.DATA, State.ABOUT):
                    if pygame.Rect(2,2,180,74).collidepoint(pos):
                        state = State.MAIN
                    elif state == State.ABOUT:
                        _about_hit(pos)

                elif state == State.MENU:
                    menu_hit(pos, _menu_buttons)
                    poller.interval = _menu_poll[0]

                elif state == State.I2CSCAN:
                    if pygame.Rect(2,2,180,74).collidepoint(pos):
                        state = State.MENU
                _dirty = True

            if (_FINGERMOTION and ev.type == _FINGERMOTION) or ev.type == MOUSEMOTION:
                if state == State.EDITOR and not _kbd['active']:
                    if _FINGERMOTION and ev.type == _FINGERMOTION:
                        _mp = (int(ev.x * C.W), int(ev.y * C.H))
                    else:
                        _mp = ev.pos
                    if _editor_motion(_mp):
                        _dirty = True

            if ev.type == MOUSEBUTTONUP or (_FINGERUP and ev.type == _FINGERUP):
                if _FINGERUP and ev.type == _FINGERUP:
                    up_pos = (int(ev.x * C.W), int(ev.y * C.H))
                else:
                    up_pos = ev.pos
                if state == State.EDITOR and not _kbd['active']:
                    _editor_up(up_pos); _dirty = True
                elif state == State.MAIN and _touch_start is not None:
                    (sx, sy), _st = _touch_start
                    _touch_start = None
                    dx = up_pos[0] - sx
                    dy = up_pos[1] - sy
                    SW = 70
                    if abs(dx) > SW and abs(dx) > abs(dy):
                        # свайп → перелистування дизайнів. Бокова панель НЕ виїжджає.
                        _astro_detail[0] = None
                        _page_screen(1 if dx < 0 else -1)
                        if _ui_shown[0] and _auto_hide_enabled():
                            _ui_shown[0] = False
                        _dirty = True
                    else:
                        # тап біля лівого краю — показати/сховати панель; інакше картка/меню
                        if sx < 40:
                            if _ui_shown[0] and _auto_hide_enabled():
                                _ui_shown[0] = False
                            else:
                                _reveal_ui()
                        else:
                            # відкритий оверлей деталей планети — тап закриває
                            if _astro_detail[0]:
                                _astro_detail[0] = None
                            else:
                                # тап-регіони grid-екрана (оновити бурі / планета)
                                _grid_hit = None
                                for act, rect in list(GUI.HITS):
                                    if rect.collidepoint(up_pos):
                                        _grid_hit = act; break
                                if _grid_hit == 'astro_refresh':
                                    _astro_do_refresh()
                                elif _grid_hit and _grid_hit.startswith('planet:'):
                                    _astro_detail[0] = _grid_hit.split(':', 1)[1]
                                else:
                                    if _ui_shown[0] or not _auto_hide_enabled():
                                        _reveal_ui()
                                    for key, rect in clickable_rects().items():
                                        if rect.collidepoint(up_pos):
                                            if key == '__menu__' or key == '__hburger__':
                                                state = State.MENU
                                            elif key == '__exit__':
                                                poller.stop(); pygame.quit(); sys.exit(0)
                                            elif key == '__main__':
                                                state = State.MAIN
                                            elif key == '__graphs__':
                                                state = State.GRAPHSEL
                                            elif key == '__data__':
                                                state = State.DATA
                                            elif key == '__about__':
                                                state = State.ABOUT
                                            else:
                                                chart_key = key
                                                state = State.CHART
                                            break
                        _dirty = True

        # Redraw if state changed
        if state != _last_state:
            _dirty = True
            if state == State.MAIN:
                _reveal_ui()        # повертаючись на головний, завжди показуємо панель
            if state == State.ABOUT:
                _about_scroll[0] = 0
                _ota_auto_check_on_open()
            if state != State.MENU:
                _dropdown[0] = None
            if state != State.MAIN:
                _astro_detail[0] = None
            _last_state = state

        # Автоприховування бокової панелі / напису після N секунд без дотиків
        if state == State.MAIN:
            _prev_shown = _ui_shown[0]
            _tick_autohide()
            if _ui_shown[0] != _prev_shown:
                _dirty = True

        # Redraw main if new data arrived
        with data_lock:
            ts = latest.get('ts', 0)
        if ts != _last_data_ts:
            _last_data_ts = ts
            if state == State.MAIN:
                _dirty = True

        # Clock seconds tick every second. Sensor polling interval stays unchanged.
        now_sec = int(time.time())
        if now_sec != _last_clock_sec:
            _last_clock_sec = now_sec
            if state in (State.MAIN, State.DATA):
                _dirty = True
            if state == State.ABOUT and _ota['busy']:
                _dirty = True

        if _dirty or _needs_redraw:
            _needs_redraw = False

            if state == State.MAIN:
                _draw_current_screen()

            elif state == State.CHART and chart_key:
                draw_chart(chart_key)

            elif state == State.MENU:
                _menu_buttons = draw_menu()

            elif state == State.GRAPHSEL:
                draw_graph_select()

            elif state == State.DATA:
                draw_data_screen()

            elif state == State.ABOUT:
                draw_about_screen()

            elif state == State.EDITOR:
                draw_editor()

            elif state == State.I2CSCAN:
                if not scan_result:
                    def _scan():
                        found = S.scan_i2c_bus(_menu_bus[0])
                        scan_result.clear()
                        if not found:
                            pass
                        elif found == [(-1,'error')]:
                            scan_result.append((-1, 'error'))
                        else:
                            for a in found:
                                scan_result.append((a, KNOWN_ADDRS.get(a, '?')))
                        pygame.event.post(pygame.event.Event(pygame.USEREVENT, {}))
                    threading.Thread(target=_scan, daemon=True).start()
                draw_i2cscan()

        clock_fps.tick(C.FPS)



# ══════════════════════════════════════════════════════════════════════════════
#  v11 — 7" landscape UI override (graphics only; I2C/sensor logic untouched)
# ══════════════════════════════════════════════════════════════════════════════

# Larger fonts for 1280×720 landscape touch display
FNT_TINY   = _font(16)
FNT_SMALL  = _font(20)
FNT_MED    = _font(24)
FNT_BIG    = _font(34, bold=True)
FNT_HUGE   = _font(54, bold=True)
FNT_CLOCK  = _font(86, bold=True)
FNT_CLOCK_S= _font(36, bold=True)
FNT_TITLE  = _font(30, bold=True)

MAIN_RECTS = {}

def _v11_card(rect, title=None, border=None):
    x,y,w,h = rect
    fill_rect(screen, C.PANEL, rect, radius=14)
    stroke_rect(screen, border or C.BORDER, rect, 1, radius=14)
    # subtle top highlight
    pygame.draw.line(screen, (32, 45, 66), (x+12, y+1), (x+w-12, y+1), 1)
    if title:
        text_at(screen, title, FNT_SMALL, C.TEXT2, x+20, y+16, 'tl')

def _v11_status_text(value, key=None):
    if value is None:
        return '—', C.MUTED
    try:
        v = float(value)
    except Exception:
        return '—', C.MUTED
    if key == 'co2':
        if v <= 800: return 'Добре', C.GREEN
        if v <= 1200: return 'Провітрити', C.YELLOW
        return 'Погано', C.RED
    if key in ('voc_index','nox_index'):
        if v <= 100: return 'Добре', C.GREEN
        if v <= 200: return 'Помірно', C.YELLOW
        return 'Погано', C.RED
    return '', C.GREEN

def _v11_fmt(v, digits=1):
    if v is None: return '—'
    try:
        return f'{float(v):.{digits}f}' if digits else f'{float(v):.0f}'
    except Exception:
        return str(v)

def _v11_trend(key):
    vals = [float(v) for v in list(history.get(key, []))[-30:] if v is not None]
    if len(vals) < 3:
        return '', C.MUTED
    d = vals[-1] - vals[0]
    if abs(d) < 0.05:
        return '→ стабільно', C.MUTED
    arrow = '↑' if d > 0 else '↓'
    col = C.GREEN if d > 0 else C.BLUE
    if key == 'pressure':
        unit = 'hPa'
    elif key == 'humidity':
        unit = '%'
    elif key == 'temperature':
        unit = '°C'
    elif key == 'co2':
        unit = 'ppm'
    else:
        unit = 'idx'
    return f'{arrow} {abs(d):.1f} {unit}', col

def _v11_hist(rect, key, col, max_val=None, baseline=True, n=36):
    x,y,w,h = rect
    vals = [float(v) for v in list(history.get(key, []))[-n:] if v is not None]
    if not vals:
        vals = [0]
    if max_val is None:
        max_val = max(max(vals)*1.15, 1)
    if baseline:
        pygame.draw.line(screen, (35,45,60), (x, y+h), (x+w, y+h), 1)
    count = max(1, min(n, len(vals)))
    gap = 3 if w > 180 else 2
    bw = max(3, (w - gap*(count-1)) // count)
    start = x + max(0, (w - (bw*count + gap*(count-1)))//2)
    for i, v in enumerate(vals[-count:]):
        frac = max(0, min(1, v / max_val))
        bh = max(2, int(h * frac))
        bx = start + i*(bw+gap)
        by = y + h - bh
        pygame.draw.rect(screen, col, (bx, by, bw, bh), border_radius=2)

def _v11_line(rect, key, col, max_val=None, n=80):
    x,y,w,h = rect
    vals = [float(v) for v in list(history.get(key, []))[-n:] if v is not None]
    if len(vals) < 2:
        return
    mn, mx = min(vals), max(vals)
    if max_val is not None:
        mn = 0; mx = max_val
    if abs(mx-mn) < 1e-6:
        mx = mn + 1
    pts=[]
    for i,v in enumerate(vals):
        px = x + int(i/(len(vals)-1)*w)
        py = y + h - int((v-mn)/(mx-mn)*h)
        pts.append((px,py))
    pygame.draw.lines(screen, col, False, pts, 3)

def _v11_metric_card(rect, title, key, unit, color, digits=0, max_val=None):
    val = latest.get(key)
    _v11_card(rect, title, color)
    x,y,w,h = rect
    text_at(screen, _v11_fmt(val, digits), FNT_HUGE, C.WHITE, x+38, y+54, 'tl')
    text_at(screen, unit, FNT_MED, C.TEXT2, x+w-28, y+78, 'tr')
    status, scol = _v11_status_text(val, key)
    if status:
        text_at(screen, status, FNT_MED, scol, x+w-28, y+56, 'tr')
    trend, tcol = _v11_trend(key)
    if trend:
        text_at(screen, trend, FNT_SMALL, tcol, x+w-28, y+104, 'tr')
    _v11_hist((x+24, y+h-82, w-48, 52), key, color, max_val=max_val, n=34)

def _v11_pressure_card(rect):
    val = latest.get('pressure')
    _v11_card(rect, 'Тиск', C.PURPLE)
    x,y,w,h = rect
    text_at(screen, _v11_fmt(val,1), FNT_HUGE, C.WHITE, x+42, y+52, 'tl')
    text_at(screen, 'hPa', FNT_MED, C.TEXT2, x+w-30, y+78, 'tr')
    # barometer semicircle 960..1060 hPa
    cx, cy = x + w//2, y + h - 56
    r = min(w//2-42, 78)
    for a in range(200, 341, 5):
        rad=math.radians(a)
        p1=(cx+int(math.cos(rad)*r), cy+int(math.sin(rad)*r))
        p2=(cx+int(math.cos(rad)*(r-14)), cy+int(math.sin(rad)*(r-14)))
        pygame.draw.line(screen, C.PURPLE if a<275 else C.BLUE, p1, p2, 2)
    try:
        frac = max(0, min(1, (float(val)-960)/100))
    except Exception:
        frac = 0.5
    ang = math.radians(200 + frac*140)
    end=(cx+int(math.cos(ang)*(r-24)), cy+int(math.sin(ang)*(r-24)))
    pygame.draw.line(screen, C.WHITE, (cx,cy), end, 5)
    pygame.draw.circle(screen, C.WHITE, (cx,cy), 8)
    trend, tcol = _v11_trend('pressure')
    text_at(screen, trend or '→ стабільно', FNT_MED, tcol, x+32, y+h-36, 'tl')
    text_at(screen, 'за 1 год', FNT_SMALL, C.MUTED, x+w-30, y+h-34, 'tr')

def _v11_clock_card(rect):
    _v11_card(rect, None, C.BORDER)
    x,y,w,h = rect
    now = datetime.now()
    text_at(screen, now.strftime('%H:%M'), FNT_CLOCK, C.WHITE, x+38, y+38, 'tl')
    text_at(screen, now.strftime('%S'), FNT_CLOCK_S, C.TEXT2, x+w-60, y+90, 'tr')
    days = ['Понеділок','Вівторок','Середа','Четвер','П’ятниця','Субота','Неділя']
    text_at(screen, f"{days[now.weekday()]}, {now.strftime('%d.%m.%Y')}", FNT_MED, C.MUTED, x+w//2, y+h-42, 'tc')

def _v11_env_card(rect):
    _v11_card(rect, None, C.BORDER)
    x,y,w,h = rect
    thirds = [x, x+w//3, x+2*w//3]
    items = [('Температура','temperature','°C',C.CYAN,1), ('Вологість','humidity','%',C.BLUE,1)]
    for i,(lab,key,unit,col,dig) in enumerate(items):
        xx = thirds[i]
        text_at(screen, lab, FNT_SMALL, C.TEXT2, xx+28, y+20, 'tl')
        text_at(screen, _v11_fmt(latest.get(key),dig), FNT_HUGE, C.WHITE, xx+28, y+54, 'tl')
        text_at(screen, unit, FNT_MED, C.TEXT2, xx+170, y+78, 'tl')
        trend,tcol=_v11_trend(key)
        text_at(screen, trend, FNT_SMALL, tcol, xx+28, y+112, 'tl')
        _v11_line((xx+28, y+h-52, w//3-56, 30), key, col, n=40)
        if i < 1:
            pygame.draw.line(screen, C.BORDER, (x+w//3, y+18), (x+w//3, y+h-18), 1)
    pygame.draw.line(screen, C.BORDER, (x+2*w//3, y+18), (x+2*w//3, y+h-18), 1)
    _v11_pressure_card((x+2*w//3+1, y, w//3-1, h))

def _v11_pm_card(rect):
    _v11_card(rect, 'Частинки (µg/m³)', C.BORDER)
    x,y,w,h = rect
    keys=[('PM1.0','pm1_0',C.GREEN,100),('PM2.5','pm2_5',C.GREEN,150),('PM4.0','pm4_0',C.YELLOW,200),('PM10','pm10',C.ORANGE,300)]
    colw=(w-40)//4
    for i,(lab,key,col,maxv) in enumerate(keys):
        xx=x+20+i*colw
        text_at(screen, lab, FNT_SMALL, C.TEXT2, xx+colw//2, y+58, 'tc')
        text_at(screen, _v11_fmt(latest.get(key),0), FNT_BIG, C.WHITE, xx+colw//2, y+90, 'tc')
        _v11_hist((xx+10, y+128, colw-20, 60), key, col, max_val=maxv, n=18)
        if i:
            pygame.draw.line(screen, C.BORDER, (xx, y+48), (xx, y+h-30), 1)

def _v11_big_graph(rect, key='co2'):
    _v11_card(rect, 'Графік CO₂ (ppm)', C.BORDER)
    x,y,w,h=rect
    plot=(x+46,y+70,w-76,h-104)
    px,py,pw,ph=plot
    for i in range(4):
        lab = str(int(maxv*i/3))
        yy=py+ph-int(i/3*ph)
        pygame.draw.line(screen, (35,45,60), (px,yy), (px+pw,yy), 1)
        text_at(screen, lab, FNT_TINY, C.TEXT2, px-10, yy, 'mr')
    _v11_hist((px, py, pw, ph), key, (25,120,55), max_val=2000, baseline=False, n=80)
    _v11_line((px, py, pw, ph), key, C.GREEN, max_val=2000, n=80)
    text_at(screen, '1 год', FNT_SMALL, C.WHITE, x+w-250, y+24, 'mc')
    text_at(screen, '6 год', FNT_SMALL, C.TEXT2, x+w-170, y+24, 'mc')
    text_at(screen, '24 год', FNT_SMALL, C.TEXT2, x+w-85, y+24, 'mc')

def clickable_rects() -> dict:
    return MAIN_RECTS.copy()

def draw_main():
    global MAIN_RECTS
    screen.fill(C.BG)
    MAIN_RECTS = {}

    # Sidebar
    fill_rect(screen, (6,15,27), (0,0,C.LEFT_W,C.H), radius=0)
    pygame.draw.line(screen, C.BORDER, (C.LEFT_W,0), (C.LEFT_W,C.H), 1)
    text_at(screen, '≋ AIRSTATION', FNT_TITLE, C.WHITE, 28, 30, 'tl')

    nav=[('⌂','Головна'),('▥','Графіки'),('▦','Дані'),('⚙','Налаштування'),('ⓘ','Про пристрій')]
    y=92
    for i,(ico,lab) in enumerate(nav):
        active=i==0
        rect=(14,y,194,54)
        fill_rect(screen, (10,55,96) if active else (6,15,27), rect, radius=12)
        if active: stroke_rect(screen, C.ACCENT, rect, 1, radius=12)
        text_at(screen, ico, FNT_BIG, C.BLUE if active else C.TEXT2, 36, y+27, 'mc')
        text_at(screen, lab, FNT_MED, C.BLUE if active else C.TEXT2, 74, y+27, 'ml')
        if lab=='Налаштування': MAIN_RECTS['__menu__']=pygame.Rect(rect)
        y+=70

    exit_rect=pygame.Rect(20, C.H-132, 180, 54)
    fill_rect(screen, (35,10,12), exit_rect, radius=12)
    stroke_rect(screen, C.RED, exit_rect, 2, radius=12)
    text_at(screen, '↪  Вийти', FNT_MED, C.RED, exit_rect.centerx, exit_rect.centery, 'mc')
    MAIN_RECTS['__exit__']=exit_rect
    now=datetime.now()
    text_at(screen, now.strftime('%H:%M:%S'), FNT_SMALL, C.TEXT2, 40, C.H-48, 'tl')
    text_at(screen, now.strftime('%d.%m.%Y'), FNT_SMALL, C.TEXT2, 40, C.H-24, 'tl')

    # Header / online
    rx=C.LEFT_W+24
    text_at(screen, 'ГОЛОВНА', FNT_TITLE, C.WHITE, rx, 22, 'tl')
    ok_any = any(S.REGISTRY[k].online for k in ['bmp280','scd41','sgp41','sps30'])
    pygame.draw.circle(screen, C.GREEN if ok_any else C.RED, (C.W-164, 36), 7)
    text_at(screen, 'Онлайн' if ok_any else 'Офлайн', FNT_MED, C.GREEN if ok_any else C.RED, C.W-148, 36, 'ml')
    text_at(screen, '☰', FNT_BIG, C.WHITE, C.W-42, 32, 'mc')

    # Layout cards
    env=(rx,70,690,165); clockr=(rx+705,70,331,165)
    _v11_env_card(env); _v11_clock_card(clockr)

    y2=250; cardw=(C.W-rx-24-24)//3; gap=12
    co2r=(rx,y2,cardw,180); vocr=(rx+cardw+gap,y2,cardw,180); noxr=(rx+2*(cardw+gap),y2,cardw,180)
    _v11_metric_card(co2r,'CO₂','co2','ppm',C.GREEN,0,2000)
    _v11_metric_card(vocr,'VOC Index','voc_index','idx',C.PURPLE,0,500)
    _v11_metric_card(noxr,'NOx Index','nox_index','idx',C.ORANGE,0,500)
    MAIN_RECTS['co2']=pygame.Rect(co2r); MAIN_RECTS['voc_index']=pygame.Rect(vocr); MAIN_RECTS['nox_index']=pygame.Rect(noxr)

    bottom_y=448
    pmr=(rx,bottom_y,430,230); gr=(rx+445,bottom_y,C.W-(rx+445)-24,230)
    _v11_pm_card(pmr); _v11_big_graph(gr,'co2')
    for k in ['pm1_0','pm2_5','pm4_0','pm10']:
        MAIN_RECTS[k]=pygame.Rect(pmr)

    pygame.display.update()



# ══════════════════════════════════════════════════════════════════════════════
#  v12 — compact 7" landscape UI override, no I2C/sensor logic changes
# ══════════════════════════════════════════════════════════════════════════════

# v15: більші шрифти — на реальній 7" 1280×720 панелі 12–16 px нечитабельні.
FNT_TINY   = _font(16)
FNT_SMALL  = _font(21)
FNT_MED    = _font(24)
FNT_BIG    = _font(32, bold=True)
FNT_HUGE   = _font(50, bold=True)
FNT_CLOCK  = _font(76, bold=True)
FNT_CLOCK_S= _font(34, bold=True)
FNT_TITLE  = _font(30, bold=True)

# мова інтерфейсу з налаштувань
i18n.set_lang(db.get_setting('lang', 'uk'))

MAIN_RECTS = {}

# Пороги зон для міні-графіків: (норма, підвищено).
#   value <= норма      → базовий колір каналу (зелений/фіолетовий/…)
#   value <= підвищено  → жовтий
#   value >  підвищено  → червоний
_ZONE_THRESHOLDS = {
    'co2':       (800, 1200),
    'voc_index': (100, 200),
    'nox_index': (100, 200),
    'pm1_0':     (10, 20),
    'pm2_5':     (15, 25),
    'pm4_0':     (30, 55),
    'pm10':      (45, 75),
}

def _zone_col(key, v, base):
    """Колір стовпчика залежно від того, чи значення в нормі."""
    th = _ZONE_THRESHOLDS.get(key)
    if th is None or v is None:
        return base
    good, warn = th
    if v <= good: return base
    if v <= warn: return C.YELLOW
    return C.RED


def _v12_card(rect, title=None, border=None):
    x,y,w,h = rect
    fill_rect(screen, C.PANEL, rect, radius=10)
    stroke_rect(screen, border or C.BORDER, rect, 1, radius=10)
    if title:
        text_at(screen, title, FNT_SMALL, C.TEXT2, x+14, y+10, 'tl')

def _v12_fmt(v, digits=1):
    if v is None: return '—'
    try: return f'{float(v):.{digits}f}' if digits else f'{float(v):.0f}'
    except Exception: return str(v)

def _v12_status(value, key=None):
    if value is None: return '—', C.MUTED
    try: v = float(value)
    except Exception: return '—', C.MUTED
    if key == 'co2':
        if v <= 800: return T('status_good'), C.GREEN
        if v <= 1200: return T('status_vent'), C.YELLOW
        return T('status_bad'), C.RED
    if key in ('voc_index','nox_index'):
        if v <= 100: return T('status_good'), C.GREEN
        if v <= 200: return T('status_mod'), C.YELLOW
        return T('status_bad'), C.RED
    if key in ('pm1_0','pm2_5','pm4_0','pm10'):
        good, warn = _ZONE_THRESHOLDS[key]
        if v <= good: return T('status_good'), C.GREEN
        if v <= warn: return T('status_mod'), C.YELLOW
        return T('status_bad'), C.RED
    return '', C.GREEN

def _v12_trend(key):
    vals = [float(v) for v in list(history.get(key, []))[-30:] if v is not None]
    if len(vals) < 3: return '', C.MUTED
    d = vals[-1] - vals[0]
    if abs(d) < 0.05: return T('stable'), C.MUTED
    unit = {'pressure':'hPa','humidity':'%','temperature':'°C','co2':'ppm'}.get(key,'idx')
    return f"{'↑' if d>0 else '↓'} {abs(d):.1f} {unit}", (C.GREEN if d>0 else C.BLUE)

def _v12_hist(rect, key, col, max_val=None, n=30):
    x,y,w,h = rect
    vals = [float(v) for v in list(history.get(key, []))[-n:] if v is not None]
    if not vals: vals = [0]
    if max_val is None: max_val = max(max(vals)*1.15, 1)
    count = max(1, min(n, len(vals)))
    gap = 3 if w > 180 else 2
    bw = max(3, (w-gap*(count-1))//count)
    start = x + max(0, (w-(bw*count+gap*(count-1)))//2)
    pygame.draw.line(screen, (35,45,60), (x,y+h), (x+w,y+h), 1)
    for i,v in enumerate(vals[-count:]):
        bh = max(2, int(h*max(0,min(1,v/max_val))))
        pygame.draw.rect(screen, col, (start+i*(bw+gap), y+h-bh, bw, bh), border_radius=2)

def _v12_line(rect, key, col, max_val=None, n=50):
    x,y,w,h = rect
    vals = [float(v) for v in list(history.get(key, []))[-n:] if v is not None]
    if len(vals) < 2: return
    mn, mx = (0, max_val) if max_val else (min(vals), max(vals))
    if abs(mx-mn) < 1e-6: mx = mn+1
    pts=[]
    for i,v in enumerate(vals):
        pts.append((x+int(i/(len(vals)-1)*w), y+h-int((v-mn)/(mx-mn)*h)))
    pygame.draw.lines(screen, col, False, pts, 2)

def _v12_env_card(rect):
    _v12_card(rect, None, C.BORDER)
    x,y,w,h = rect
    colw=w//3
    for i,(lab,key,unit,col,dig) in enumerate([
        ('Температура','temperature','°C',C.CYAN,1),
        ('Вологість','humidity','%',C.BLUE,1),
    ]):
        xx=x+i*colw
        text_at(screen, lab, FNT_SMALL, C.TEXT2, xx+16, y+12, 'tl')
        text_at(screen, _v12_fmt(latest.get(key),dig), FNT_HUGE, C.WHITE, xx+16, y+44, 'tl')
        text_at(screen, unit, FNT_SMALL, C.TEXT2, xx+135, y+62, 'tl')
        tr,tc=_v12_trend(key)
        text_at(screen, tr, FNT_TINY, tc, xx+16, y+96, 'tl')
        _v12_line((xx+16,y+h-42,colw-32,24), key, col, n=40)
        pygame.draw.line(screen, C.BORDER, (x+colw*(i+1), y+12), (x+colw*(i+1), y+h-12), 1)
    _v12_pressure((x+2*colw+1,y,colw-1,h))

def _v12_pressure(rect):
    x,y,w,h=rect
    val=latest.get('pressure')
    text_at(screen, 'Тиск', FNT_SMALL, C.TEXT2, x+16, y+12, 'tl')
    text_at(screen, _v12_fmt(val,1), FNT_BIG, C.WHITE, x+16, y+42, 'tl')
    text_at(screen, 'hPa', FNT_TINY, C.TEXT2, x+w-16, y+52, 'tr')
    cx,cy=x+w//2,y+h-48; r=min(w//2-24,62)
    for a in range(200,341,7):
        rad=math.radians(a)
        p1=(cx+int(math.cos(rad)*r),cy+int(math.sin(rad)*r))
        p2=(cx+int(math.cos(rad)*(r-10)),cy+int(math.sin(rad)*(r-10)))
        pygame.draw.line(screen, C.PURPLE if a<275 else C.BLUE, p1,p2,2)
    try: frac=max(0,min(1,(float(val)-960)/100))
    except Exception: frac=.5
    ang=math.radians(200+frac*140)
    end=(cx+int(math.cos(ang)*(r-18)),cy+int(math.sin(ang)*(r-18)))
    pygame.draw.line(screen,C.WHITE,(cx,cy),end,4)
    pygame.draw.circle(screen,C.WHITE,(cx,cy),6)
    tr,tc=_v12_trend('pressure')
    text_at(screen, tr or T('stable'), FNT_TINY, tc, x+16, y+h-24, 'tl')

def _v12_clock(rect):
    _v12_card(rect, None, C.BORDER)
    x,y,w,h=rect; now=datetime.now()
    text_at(screen, now.strftime('%H:%M'), FNT_CLOCK, C.WHITE, x+26, y+30, 'tl')
    text_at(screen, now.strftime('%S'), FNT_CLOCK_S, C.TEXT2, x+w-28, y+82, 'tr')
    days=['Понеділок','Вівторок','Середа','Четвер','П’ятниця','Субота','Неділя']
    text_at(screen, f"{days[now.weekday()]}, {now.strftime('%d.%m.%Y')}", FNT_SMALL, C.MUTED, x+w//2, y+h-32, 'tc')

def _v12_metric(rect,title,key,unit,color,max_val):
    val=latest.get(key)
    _v12_card(rect,title,color)
    x,y,w,h=rect
    text_at(screen,_v12_fmt(val,0),FNT_HUGE,C.WHITE,x+24,y+48,'tl')
    text_at(screen,unit,FNT_SMALL,C.TEXT2,x+w-20,y+68,'tr')
    st,sc=_v12_status(val,key)
    text_at(screen,st,FNT_SMALL,sc,x+w-20,y+42,'tr')
    tr,tc=_v12_trend(key)
    if tr: text_at(screen,tr,FNT_TINY,tc,x+w-20,y+92,'tr')
    # Зонні кольори + насічки порогів; шкала прив'язана до порога норми.
    _v14_bars((x+20,y+h-62,w-40,44),key,color,max_val=None,n=28,min_visible=2)

def _v12_pm(rect):
    _v12_card(rect,'Частинки (µg/m³)',C.BORDER)
    x,y,w,h=rect
    keys=[('PM1.0','pm1_0',C.GREEN,80),('PM2.5','pm2_5',C.GREEN,100),('PM4.0','pm4_0',C.YELLOW,150),('PM10','pm10',C.ORANGE,200)]
    colw=(w-28)//4
    for i,(lab,key,col,maxv) in enumerate(keys):
        xx=x+14+i*colw
        text_at(screen,lab,FNT_TINY,C.TEXT2,xx+colw//2,y+48,'tc')
        text_at(screen,_v12_fmt(latest.get(key),0),FNT_BIG,C.WHITE,xx+colw//2,y+75,'tc')
        _v12_hist((xx+10,y+120,colw-20,48),key,col,max_val=maxv,n=14)
        if i: pygame.draw.line(screen,C.BORDER,(xx,y+40),(xx,y+h-20),1)

def _v12_graph(rect,key='co2'):
    meta = {
        'co2': ('Графік CO₂ (ppm)', 2000, C.GREEN),
        'voc_index': ('Графік VOC Index', 500, C.PURPLE),
        'nox_index': ('Графік NOx Index', 500, C.ORANGE),
        'pm2_5': ('Графік PM2.5 (µg/m³)', 150, C.GREEN),
        'pm10': ('Графік PM10 (µg/m³)', 300, C.ORANGE),
    }
    title, maxv, col = meta.get(key, meta['co2'])
    _v12_card(rect,title,C.BORDER)
    x,y,w,h=rect; px,py,pw,ph=x+44,y+58,w-68,h-84
    for i in range(4):
        lab = str(int(maxv*i/3))
        yy=py+ph-int(i/3*ph)
        pygame.draw.line(screen,(35,45,60),(px,yy),(px+pw,yy),1)
        text_at(screen,lab,FNT_TINY,C.TEXT2,px-8,yy,'mr')
    _v12_hist((px,py,pw,ph),key,col,max_val=maxv,n=70)
    _v12_line((px,py,pw,ph),key,col,max_val=maxv,n=70)
    text_at(screen,'1 год   6 год   24 год',FNT_SMALL,C.TEXT2,x+w-180,y+20,'mc')

def clickable_rects() -> dict:
    return MAIN_RECTS.copy()

def draw_main():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS={}
    left=210
    fill_rect(screen,(6,15,27),(0,0,left,C.H),radius=0)
    pygame.draw.line(screen,C.BORDER,(left,0),(left,C.H),1)
    text_at(screen,'≋ AIRSTATION',FNT_TITLE,C.WHITE,24,28,'tl')
    nav=[('__main__','⌂','Головна'),('__graphs__','▥','Графіки'),('__data__','▦','Дані'),('__menu__','⚙','Налаштування'),('__about__','ⓘ','Про пристрій')]
    y=88
    for i,(key,ico,lab) in enumerate(nav):
        rect=pygame.Rect(12,y,186,48)
        active=(key=='__main__')
        fill_rect(screen,(10,55,96) if active else (6,15,27),rect,radius=10)
        if active: stroke_rect(screen,C.ACCENT,rect,1,radius=10)
        text_at(screen,ico,FNT_MED,C.BLUE if active else C.TEXT2,34,y+24,'mc')
        text_at(screen,lab,FNT_SMALL,C.BLUE if active else C.TEXT2,66,y+24,'ml')
        MAIN_RECTS[key]=rect
        y+=62
    exit_rect=pygame.Rect(18,C.H-120,170,48)
    fill_rect(screen,(35,10,12),exit_rect,radius=10); stroke_rect(screen,C.RED,exit_rect,2,radius=10)
    text_at(screen,'↪  Вийти',FNT_SMALL,C.RED,exit_rect.centerx,exit_rect.centery,'mc')
    MAIN_RECTS['__exit__']=exit_rect
    now=datetime.now()
    text_at(screen,now.strftime('%H:%M:%S'),FNT_TINY,C.TEXT2,36,C.H-46,'tl')
    text_at(screen,now.strftime('%d.%m.%Y'),FNT_TINY,C.TEXT2,36,C.H-24,'tl')

    rx=left+18; rw=C.W-rx-18
    text_at(screen,'ГОЛОВНА',FNT_TITLE,C.WHITE,rx,20,'tl')
    ok_any=any(S.REGISTRY[k].online for k in ['bmp280','scd41','sgp41','sps30'])
    pygame.draw.circle(screen,C.GREEN if ok_any else C.RED,(C.W-120,34),6)
    text_at(screen,'Онлайн' if ok_any else 'Офлайн',FNT_SMALL,C.GREEN if ok_any else C.RED,C.W-108,34,'ml')
    text_at(screen,'☰',FNT_BIG,C.WHITE,C.W-28,32,'mc')

    env=(rx,64,680,132); clockr=(rx+694,64,rw-694,132)
    _v12_env_card(env); _v12_clock(clockr)

    y2=210; gap=12; cardw=(rw-2*gap)//3
    r1=(rx,y2,cardw,156); r2=(rx+cardw+gap,y2,cardw,156); r3=(rx+2*(cardw+gap),y2,cardw,156)
    _v12_metric(r1,'CO₂','co2','ppm',C.GREEN,2000)
    _v12_metric(r2,'VOC Index','voc_index','idx',C.PURPLE,500)
    _v12_metric(r3,'NOx Index','nox_index','idx',C.ORANGE,500)
    MAIN_RECTS['co2']=pygame.Rect(r1); MAIN_RECTS['voc_index']=pygame.Rect(r2); MAIN_RECTS['nox_index']=pygame.Rect(r3)

    bottom=384; pmw=430
    pmr=(rx,bottom,pmw,C.H-bottom-28); gr=(rx+pmw+14,bottom,rw-pmw-14,C.H-bottom-28)
    _v12_pm(pmr); _v12_graph(gr, db.get_setting('main_graph','co2'))
    for k in ['pm1_0','pm2_5','pm4_0','pm10']:
        MAIN_RECTS[k]=pygame.Rect(pmr)
    pygame.display.update()


# ══════════════════════════════════════════════════════════════════════════════
#  v13 — settings/data/about/graph selector + cleaner barometer
#  Graphics/control UI only. Sensor/I2C polling code is unchanged.
# ══════════════════════════════════════════════════════════════════════════════

GRAPH_CHOICES = [
    ('co2', 'CO₂', 'ppm', C.GREEN),
    ('voc_index', 'VOC Index', 'idx', C.PURPLE),
    ('nox_index', 'NOx Index', 'idx', C.ORANGE),
    ('pm2_5', 'PM2.5', 'µg/m³', C.GREEN),
    ('pm10', 'PM10', 'µg/m³', C.ORANGE),
]

_graph_buttons = []
_menu_buttons_v13 = []

def _screen_header(title, subtitle=''):
    screen.fill(C.BG)
    fill_rect(screen, (6,15,27), (0,0,C.W,C.H), radius=0)
    fill_rect(screen, (12,22,38), (18,18,110,42), radius=10)
    stroke_rect(screen, C.BORDER, (18,18,110,42), 1, radius=10)
    text_at(screen, '← Назад', FNT_SMALL, C.TEXT2, 73, 39, 'mc')
    text_at(screen, title, FNT_TITLE, C.WHITE, 150, 20, 'tl')
    if subtitle:
        text_at(screen, subtitle, FNT_SMALL, C.MUTED, 150, 52, 'tl')

def _v12_pressure(rect):
    # Override v12: keep value away from the dial so numbers never overlap the scale.
    x,y,w,h=rect
    val=latest.get('pressure')
    text_at(screen, 'Тиск', FNT_SMALL, C.TEXT2, x+16, y+12, 'tl')
    text_at(screen, _v12_fmt(val,1), FNT_BIG, C.WHITE, x+16, y+34, 'tl')
    text_at(screen, 'hPa', FNT_TINY, C.TEXT2, x+128, y+48, 'tl')
    tr,tc=_v12_trend('pressure')
    text_at(screen, tr or T('stable'), FNT_TINY, tc, x+16, y+h-24, 'tl')

    cx,cy=x+w-76,y+h-46
    r=min(52, max(34, w//4))
    for a in range(205,336,8):
        rad=math.radians(a)
        p1=(cx+int(math.cos(rad)*r),cy+int(math.sin(rad)*r))
        p2=(cx+int(math.cos(rad)*(r-9)),cy+int(math.sin(rad)*(r-9)))
        pygame.draw.line(screen, C.PURPLE if a<270 else C.BLUE, p1,p2,2)
    try: frac=max(0,min(1,(float(val)-960)/100))
    except Exception: frac=.5
    ang=math.radians(205+frac*130)
    end=(cx+int(math.cos(ang)*(r-16)),cy+int(math.sin(ang)*(r-16)))
    pygame.draw.line(screen,C.WHITE,(cx,cy),end,4)
    pygame.draw.circle(screen,C.WHITE,(cx,cy),5)

def graph_select_hit(pos):
    for key, rect in _graph_buttons:
        if rect.collidepoint(pos):
            return key
    if pygame.Rect(18,18,110,42).collidepoint(pos):
        return '__back__'
    return None

def draw_graph_select():
    global _graph_buttons
    _graph_buttons=[]
    _screen_header('ГРАФІКИ', 'Вибери, який графік побудувати')
    x0,y0=150,105; gap=18; bw=330; bh=150
    for i,(key,label,unit,col) in enumerate(GRAPH_CHOICES):
        x=x0+(i%3)*(bw+gap); y=y0+(i//3)*(bh+gap)
        rect=pygame.Rect(x,y,bw,bh)
        _graph_buttons.append((key,rect))
        _v12_card(rect, label, col)
        text_at(screen, _v12_fmt(latest.get(key),0), FNT_HUGE, C.WHITE, x+24, y+48, 'tl')
        text_at(screen, unit, FNT_SMALL, C.TEXT2, x+bw-24, y+68, 'tr')
        _v12_hist((x+22,y+96,bw-44,36), key, col, max_val={'co2':2000,'voc_index':500,'nox_index':500,'pm2_5':150,'pm10':300}.get(key,500), n=28)
    text_at(screen, 'Натисни картку — відкриється повний графік.', FNT_SMALL, C.MUTED, 150, C.H-46, 'tl')
    pygame.display.update()

def draw_data_screen():
    _screen_header('ДАНІ', 'Поточні сирі значення з датчиків і статус підключення')
    now=datetime.now()
    text_at(screen, 'Останнє оновлення: ' + now.strftime('%H:%M:%S'), FNT_SMALL, C.TEXT2, C.W-40, 32, 'tr')
    cards=[
        ('BMP280 / барометр', [('Температура BMP', 'temp_bmp', '°C'), ('Тиск', 'pressure', 'hPa')], C.PURPLE),
        ('SCD41 / CO₂', [('CO₂', 'co2', 'ppm'), ('Температура SCD', 'temp_scd', '°C'), ('Вологість SCD', 'hum_scd', '%')], C.GREEN),
        ('SGP41 / гази', [('VOC Index', 'voc_index', 'idx'), ('NOx Index', 'nox_index', 'idx')], C.ORANGE),
        ('SPS30 / частинки', [('PM1.0', 'pm1_0', 'µg/m³'), ('PM2.5', 'pm2_5', 'µg/m³'), ('PM4.0', 'pm4_0', 'µg/m³'), ('PM10', 'pm10', 'µg/m³')], C.CYAN),
    ]
    x0,y0=150,95; bw=520; bh=240; gap=22
    for i,(title,rows,col) in enumerate(cards):
        x=x0+(i%2)*(bw+gap); y=y0+(i//2)*(bh+gap)
        _v12_card((x,y,bw,bh), title, col)
        online_key=['bmp280','scd41','sgp41','sps30'][i]
        ok=S.REGISTRY[online_key].online
        pygame.draw.circle(screen, C.GREEN if ok else C.RED, (x+bw-32,y+24), 7)
        text_at(screen, 'OK' if ok else 'OFF', FNT_TINY, C.GREEN if ok else C.RED, x+bw-46, y+42, 'tr')
        yy=y+62
        for lab,key,unit in rows:
            text_at(screen, lab, FNT_SMALL, C.TEXT2, x+24, yy, 'tl')
            text_at(screen, _v12_fmt(latest.get(key),1 if key in ['temp_bmp','temp_scd','hum_scd','pressure'] else 0), FNT_BIG, C.WHITE, x+300, yy-8, 'tl')
            text_at(screen, unit, FNT_SMALL, C.MUTED, x+bw-24, yy, 'tr')
            yy+=42
    pygame.display.update()

def draw_about_screen():
    _screen_header('ПРО ПРИСТРІЙ', 'Сенсори, показники та коротка довідка')
    x,y=150,95
    lines = [
        ('Сенсори', C.CYAN),
        ('BMP280/BMP390: температура та атмосферний тиск.', C.TEXT2),
        ('SCD41: справжній CO₂, температура, вологість.', C.TEXT2),
        ('SGP41: VOC Index та NOx Index. Це індекси Sensirion, не ppb.', C.TEXT2),
        ('SPS30: частинки PM1.0, PM2.5, PM4.0, PM10 у µg/m³.', C.TEXT2),
        ('', C.TEXT2),
        ('Що показує головний екран', C.CYAN),
        ('CO₂: вентиляція/люди в кімнаті. >800 ppm — бажано провітрити.', C.TEXT2),
        ('VOC Index: леткі органічні сполуки — запахи, хімія, аерозолі, кухня.', C.TEXT2),
        ('NOx Index: оксиди азоту — газова плита, вулиця, вихлопи, дим.', C.TEXT2),
        ('PM2.5/PM10: пил та дрібні частинки.', C.TEXT2),
        ('Барометр: стрілка + тренд показує, куди змінився тиск за останній час.', C.TEXT2),
        ('', C.TEXT2),
        ('Прибрано', C.ORANGE),
        ('eCO₂, AQI, IAQ не показуються, бо це розраховані/умовні індекси.', C.TEXT2),
        ('На екрані залишені прямі показники з реальних сенсорів.', C.TEXT2),
    ]
    for txt,col in lines:
        if txt=='':
            y+=16; continue
        text_at(screen, txt, FNT_MED if col in (C.CYAN,C.ORANGE) else FNT_SMALL, col, x, y, 'tl')
        y+=34 if col in (C.CYAN,C.ORANGE) else 28
    pygame.display.update()

def draw_menu():
    global _menu_buttons_v13
    _menu_buttons_v13=[]
    _screen_header('НАЛАШТУВАННЯ', 'Опитування сенсорів окремо, годинник тікає щосекунди')
    x,y=150,100
    _v12_card((x,y,500,150), 'Інтервал опитування сенсорів', C.ACCENT)
    text_at(screen, f'{_menu_poll[0]} сек', FNT_HUGE, C.WHITE, x+30, y+58, 'tl')
    for label,rx in [('poll-',x+260),('poll+',x+370)]:
        rect=pygame.Rect(rx,y+62,84,42); _menu_buttons_v13.append((label, rect))
        fill_rect(screen, C.ORANGE if label.endswith('-') else C.GREEN, rect, radius=10)
        text_at(screen, '−' if label.endswith('-') else '+', FNT_BIG, C.WHITE, rect.centerx, rect.centery, 'mc')

    _v12_card((690,y,440,150), 'I²C', C.CYAN)
    text_at(screen, f'Bus: {_menu_bus[0]}', FNT_BIG, C.WHITE, 720, y+58, 'tl')
    for label,rx in [('bus-',850),('bus+',940),('Scan I2C',1030)]:
        rect=pygame.Rect(rx,y+62,80 if label!='Scan I2C' else 90,42); _menu_buttons_v13.append((label, rect))
        fill_rect(screen, C.PANEL2, rect, radius=10); stroke_rect(screen, C.CYAN, rect, 1, radius=10)
        text_at(screen, '−' if label=='bus-' else ('+' if label=='bus+' else 'Scan'), FNT_SMALL, C.TEXT2, rect.centerx, rect.centery, 'mc')

    y2=290
    _v12_card((x,y2,980,230), 'Графік на головному екрані', C.PURPLE)
    current=db.get_setting('main_graph','co2')
    bx=x+28; by=y2+70
    for key,label,unit,col in GRAPH_CHOICES:
        rect=pygame.Rect(bx,by,170,58); _menu_buttons_v13.append(('main_graph:'+key, rect))
        fill_rect(screen, (20,60,100) if key==current else C.PANEL2, rect, radius=10)
        stroke_rect(screen, col if key==current else C.BORDER, rect, 2 if key==current else 1, radius=10)
        text_at(screen, label, FNT_SMALL, C.WHITE if key==current else C.TEXT2, rect.centerx, by+15, 'tc')
        text_at(screen, unit, FNT_TINY, C.MUTED, rect.centerx, by+38, 'tc')
        bx+=186
    text_at(screen, 'Це не міняє опитування датчиків — тільки який великий графік видно на головному екрані.', FNT_SMALL, C.MUTED, x+28, y2+160, 'tl')

    save=pygame.Rect(C.W-190,C.H-78,150,46); _menu_buttons_v13.append(('Save', save))
    fill_rect(screen, C.GREEN, save, radius=10); text_at(screen,'Зберегти',FNT_SMALL,C.WHITE,save.centerx,save.centery,'mc')
    pygame.display.update()
    return _menu_buttons_v13

def menu_hit(pos, buttons):
    global state, scan_result
    for label, rect in _menu_buttons_v13:
        if not rect.collidepoint(pos):
            continue
        if label=='poll-':
            _menu_poll[0]=max(1,_menu_poll[0]-1); db.set_setting('poll_sec', str(_menu_poll[0]))
        elif label=='poll+':
            _menu_poll[0]=min(3600,_menu_poll[0]+1); db.set_setting('poll_sec', str(_menu_poll[0]))
        elif label=='bus-':
            _menu_bus[0]=max(0,_menu_bus[0]-1); db.set_setting('i2c_bus', str(_menu_bus[0]))
        elif label=='bus+':
            _menu_bus[0]=min(9,_menu_bus[0]+1); db.set_setting('i2c_bus', str(_menu_bus[0]))
        elif label=='Scan I2C':
            scan_result.clear(); state=State.I2CSCAN
        elif label.startswith('main_graph:'):
            db.set_setting('main_graph', label.split(':',1)[1])
        elif label=='Save':
            db.set_setting('poll_sec', str(_menu_poll[0])); db.set_setting('i2c_bus', str(_menu_bus[0])); state=State.MAIN
        break


# ══════════════════════════════════════════════════════════════════════════════
#  v14 — UI fixes requested for 7" 1280×720 landscape
#  Only drawing/menu behaviour changed. Sensor/I2C read logic is untouched.
# ══════════════════════════════════════════════════════════════════════════════

GRAPH_CHOICES = [
    ('co2', 'CO₂', 'ppm', C.GREEN),
    ('voc_index', 'VOC Index', 'idx', C.PURPLE),
    ('nox_index', 'NOx Index', 'idx', C.ORANGE),
    ('pm2_5', 'PM2.5', 'µg/m³', C.GREEN),
    ('pm10', 'PM10', 'µg/m³', C.ORANGE),
    ('temperature', 'Температура', '°C', C.CYAN),
    ('humidity', 'Вологість', '%', C.BLUE),
    ('pressure', 'Тиск', 'hPa', C.PURPLE),
]

_GRAPH_META = {
    'co2':         ('Графік: CO₂ (ppm)',          2000, C.GREEN, 0),
    'voc_index':   ('Графік: VOC Index (idx)',     500, C.PURPLE, 0),
    'nox_index':   ('Графік: NOx Index (idx)',     500, C.ORANGE, 0),
    'pm2_5':       ('Графік: PM2.5 (µg/m³)',       150, C.GREEN, 1),
    'pm10':        ('Графік: PM10 (µg/m³)',        300, C.ORANGE, 1),
    'temperature': ('Графік: температура (°C)',     None, C.CYAN, 1),
    'humidity':    ('Графік: вологість (%)',        100, C.BLUE, 1),
    'pressure':    ('Графік: тиск (hPa)',           None, C.PURPLE, 1),
}

_menu_buttons_v14 = []

# Compact Russian/Ukrainian labels for returned settings tabs. Old technical tabs are back.
TABS = [
    ('general', 'Головні'),
    ('display', 'Вигляд'),
    ('screens', 'Екрани'),
    ('boxes', 'Бокси'),
    ('calib', 'Калібр.'),
    ('sensors', 'Сенсори'),
    ('time', 'Час'),
]


def _hist_values(key, n=40):
    vals=[]
    try:
        for v in list(history.get(key, []))[-n:]:
            if v is not None:
                vals.append(float(v))
    except Exception:
        vals=[]
    if not vals:
        try:
            v = latest.get(key)
            if v is not None:
                vals=[float(v)]
        except Exception:
            pass
    return vals


def _trend_compact(key, digits=1):
    vals = _hist_values(key, 30)
    if len(vals) < 3:
        return '', C.MUTED
    d = vals[-1] - vals[0]
    eps = 0.05 if key != 'pressure' else 0.02
    if abs(d) < eps:
        return T('stable'), C.MUTED
    unit = {'pressure':'hPa','temperature':'°C','humidity':'%','co2':'ppm','voc_index':'idx','nox_index':'idx'}.get(key,'')
    arrow = '↑' if d > 0 else '↓'
    col = C.GREEN if d > 0 else C.BLUE
    return f'({arrow} {abs(d):.{digits}f} {unit})'.replace('  ', ' '), col


def _v14_bars(rect, key, col, max_val=None, n=30, min_visible=3, zones=True, notches=True):
    """Міні-гістограма, завжди прив'язана до низу прямокутника.
    zones=True  → колір кожного стовпчика залежить від зони значення
                  (норма → базовий колір, підвищено → жовтий, високо → червоний).
    notches=True → пунктирні насічки-лінії на рівнях порогів норми/підвищення,
                  щоб було видно, в якому діапазоні лежить величина."""
    x,y,w,h = rect
    vals = _hist_values(key, n)
    if not vals:
        vals=[0.0]
    th = _ZONE_THRESHOLDS.get(key)
    if max_val is None:
        vmax=max(vals) if vals else 1.0
        if th:
            # Шкала прив'язана до порога норми: лінія "норма" ≈ 62% висоти,
            # тож і чисте повітря (PM 1..3), і перевищення видно наочно.
            max_val=max(th[0]*1.6, vmax*1.25)
        elif key == 'temperature':
            max_val=max(max(vals)*1.05, 40)
        elif key == 'pressure':
            max_val=max(max(vals)*1.02, 1050)
        else:
            max_val=max(vmax*1.2, 1.0)
    max_val = max(float(max_val), 1.0)
    pygame.draw.line(screen, (45,55,70), (x, y+h), (x+w, y+h), 1)
    count=max(1,min(n,len(vals)))
    vals=vals[-count:]
    gap=3 if w>130 else 2
    bw=max(3,(w-gap*(count-1))//count)
    start=x+max(0,(w-(bw*count+gap*(count-1)))//2)
    for i,v in enumerate(vals):
        frac=max(0.0,min(1.0,float(v)/max_val))
        bh=int(h*frac)
        if v > 0:
            bh=max(min_visible,bh)
        bh=min(h,bh)
        bx=start+i*(bw+gap)
        by=y+h-bh
        bcol=_zone_col(key, v, col) if zones else col
        pygame.draw.rect(screen, bcol, (bx, by, bw, bh), border_radius=2)
    # Насічки порогів поверх стовпчиків: жовта — межа норми, червона — високий рівень.
    if notches and th:
        for lim, lc in ((th[0], C.YELLOW), (th[1], C.RED)):
            if 0 < lim < max_val:
                yy=y+h-int(h*float(lim)/max_val)
                dim=tuple(int(c*0.6) for c in lc)
                dx=0
                while dx < w-2:
                    pygame.draw.line(screen, dim, (x+dx, yy), (x+min(dx+4, w), yy), 1)
                    dx+=9
                pygame.draw.line(screen, lc, (x, yy), (x+4, yy), 2)
                pygame.draw.line(screen, lc, (x+w-4, yy), (x+w, yy), 2)
                if w >= 170:
                    text_at(screen, f'{lim:g}', FNT_TINY, dim, x+w-6, yy-2, 'br')


def _v14_line(rect, key, col, max_val=None, n=50):
    x,y,w,h=rect
    vals=_hist_values(key,n)
    if len(vals)<2:
        return
    if max_val is not None:
        mn, mx = 0.0, float(max_val)
    else:
        mn, mx = min(vals), max(vals)
        if key == 'pressure':
            pad=max(0.8,(mx-mn)*0.4)
            mn-=pad; mx+=pad
        elif key == 'temperature':
            pad=max(0.5,(mx-mn)*0.4)
            mn-=pad; mx+=pad
        elif key == 'humidity':
            pad=max(1.0,(mx-mn)*0.4)
            mn=max(0,mn-pad); mx=min(100,mx+pad)
    if abs(mx-mn)<1e-6:
        mx=mn+1
    pts=[]
    for i,v in enumerate(vals):
        pts.append((x+int(i/(len(vals)-1)*w), y+h-int((v-mn)/(mx-mn)*h)))
    pygame.draw.lines(screen, col, False, pts, 2)


def _v12_env_card(rect):
    _v12_card(rect, None, C.BORDER)
    x,y,w,h=rect
    colw=w//3
    for i,(lab,key,unit,col,dig) in enumerate([
        ('Температура','temperature','°C',C.CYAN,1),
        ('Вологість','humidity','%',C.BLUE,1),
    ]):
        xx=x+i*colw
        tr,tc=_trend_compact(key, 1)
        # trend in one line with label, top-right inside the small window
        text_at(screen, lab, FNT_SMALL, C.TEXT2, xx+14, y+12, 'tl')
        if tr:
            text_at(screen, tr, FNT_TINY, tc, xx+colw-14, y+15, 'tr')
        text_at(screen, _v12_fmt(latest.get(key),dig), FNT_HUGE, C.WHITE, xx+16, y+44, 'tl')
        text_at(screen, unit, FNT_SMALL, C.TEXT2, xx+136, y+62, 'tl')
        _v14_line((xx+16,y+h-42,colw-32,24), key, col, n=40)
        pygame.draw.line(screen, C.BORDER, (x+colw*(i+1), y+12), (x+colw*(i+1), y+h-12), 1)
    _v12_pressure((x+2*colw+1,y,colw-1,h))


def _v12_pressure(rect):
    # Bigger barometer scale; numeric pressure is below the dial to avoid overlap.
    x,y,w,h=rect
    val=latest.get('pressure')
    tr,tc=_trend_compact('pressure',1)
    text_at(screen, 'Тиск', FNT_SMALL, C.TEXT2, x+14, y+10, 'tl')
    if tr:
        text_at(screen, tr.replace('(', '').replace(')', ''), FNT_TINY, tc, x+w-14, y+13, 'tr')

    cx,cy=x+w//2,y+78
    r=min(w//2-18,70)
    # major/minor ticks 920..1040 hPa
    for p in range(920,1041,5):
        frac=(p-920)/120
        a=math.radians(200+frac*140)
        major=(p % 30 == 0)
        outer=(cx+int(math.cos(a)*r), cy+int(math.sin(a)*r))
        inner=(cx+int(math.cos(a)*(r-(14 if major else 8))), cy+int(math.sin(a)*(r-(14 if major else 8))))
        pygame.draw.line(screen, C.PURPLE if p<980 else C.BLUE, outer, inner, 2 if major else 1)
        if major:
            tx=cx+int(math.cos(a)*(r-28)); ty=cy+int(math.sin(a)*(r-28))
            text_at(screen, str(p), FNT_TINY, C.TEXT2, tx, ty, 'mc')
    try:
        frac=max(0,min(1,(float(val)-920)/120))
    except Exception:
        frac=.5
    ang=math.radians(200+frac*140)
    end=(cx+int(math.cos(ang)*(r-24)), cy+int(math.sin(ang)*(r-24)))
    pygame.draw.line(screen,C.WHITE,(cx,cy),end,4)
    pygame.draw.circle(screen,C.WHITE,(cx,cy),6)
    # small numeric line below scale
    text_at(screen, _v12_fmt(val,1), FNT_MED, C.WHITE, x+w//2-10, y+h-28, 'tr')
    text_at(screen, 'hPa', FNT_TINY, C.TEXT2, x+w//2-4, y+h-26, 'tl')


def _v12_pm(rect):
    _v12_card(rect,'Частинки (µg/m³)',C.BORDER)
    x,y,w,h=rect
    # 4 відтінки зеленого (світлий → темний); при перевищенні норми стовпчики
    # самі стають жовтими/червоними через зонні кольори у _v14_bars.
    keys=[('PM1.0','pm1_0',(110,231,183)),
          ('PM2.5','pm2_5',( 52,211,153)),
          ('PM4.0','pm4_0',( 16,185,129)),
          ('PM10','pm10', (  5,150,105))]
    colw=(w-28)//4
    for i,(lab,key,col) in enumerate(keys):
        xx=x+14+i*colw
        val=latest.get(key)
        text_at(screen,lab,FNT_TINY,C.TEXT2,xx+colw//2,y+38,'tc')
        # 1 decimal for PM: if the air is clean, 2.1/2.4 is more useful than four identical "2".
        text_at(screen,_v12_fmt(val,1),FNT_BIG,C.WHITE,xx+colw//2,y+64,'tc')
        # Графік прив'язаний до НИЗУ картки (над рядком статусу), а не завислий
        # посередині: верх на y+108, низ на y+h-44.
        chart_h=max(30, h-108-44)
        _v14_bars((xx+10,y+h-44-chart_h,colw-20,chart_h),key,col,max_val=None,n=16,min_visible=2)
        st,sc=_v12_status(val,key)
        if not st:
            st='Добре'; sc=C.GREEN
        text_at(screen,st,FNT_TINY,sc,xx+colw//2,y+h-22,'tc')
        if i:
            pygame.draw.line(screen,C.BORDER,(xx,y+36),(xx,y+h-28),1)


def _v12_graph(rect,key='co2'):
    title, maxv, col, digits = _GRAPH_META.get(key, _GRAPH_META['co2'])
    _v12_card(rect,title,C.BORDER)
    x,y,w,h=rect
    px,py,pw,ph=x+46,y+58,w-70,h-86
    vals=_hist_values(key,70)
    if maxv is None:
        if vals:
            mn,mx=min(vals),max(vals)
            pad=max((mx-mn)*0.15, 1.0 if key!='pressure' else 0.8)
            lo,hi=mn-pad,mx+pad
        else:
            lo,hi=(0,1)
    else:
        lo,hi=0,float(maxv)
    if abs(hi-lo)<1e-6: hi=lo+1
    for i in range(4):
        t=i/3
        yv=lo+(hi-lo)*t
        yy=py+ph-int(t*ph)
        pygame.draw.line(screen,(35,45,60),(px,yy),(px+pw,yy),1)
        text_at(screen,f'{yv:.0f}' if abs(yv)>=10 else f'{yv:.1f}',FNT_TINY,C.TEXT2,px-8,yy,'mr')
    # main graph: bars anchored to bottom plus line. For temperature/pressure only line is clearer.
    if key not in ('temperature','pressure','humidity'):
        _v14_bars((px,py,pw,ph),key,col,max_val=maxv,n=70,min_visible=2)
    _v14_line((px,py,pw,ph),key,col,max_val=maxv,n=70)
    text_at(screen,'1 год   6 год   24 год',FNT_SMALL,C.TEXT2,x+w-180,y+20,'mc')


def draw_chart(key: str):
    screen.fill(C.BG)
    title, default_max, col, digits = _GRAPH_META.get(key, (key, None, C.ACCENT, 1))
    hrs = float(db.get_setting('graph_hours', '24'))
    text_at(screen, f"{title} — {int(hrs)} год", FNT_BIG, col, C.W//2, 8, 'tc')
    fill_rect(screen, C.PANEL2, (2,2,70,34), radius=5)
    text_at(screen, '← Back', FNT_SMALL, C.TEXT2, 37, 19, 'mc')
    try:
        rows=db.query(key, hrs)
    except Exception:
        rows=[]
    if not rows:
        rows=[(time.time()-(len(_hist_values(key,200))-i)*C.POLL_INTERVAL, v) for i,v in enumerate(_hist_values(key,200))]
    if len(rows)<2:
        text_at(screen,'Ще недостатньо даних',FNT_BIG,C.MUTED,C.W//2,C.H//2,'mc')
        pygame.display.update(); return
    cx,cy=58,54; cw,ch_=C.W-cx-22,C.H-cy-50
    vals=[float(v) for _,v in rows if v is not None]
    if default_max is None:
        mn,mx=min(vals),max(vals); pad=(mx-mn)*0.12 or (0.8 if key=='pressure' else 1.0); lo,hi=mn-pad,mx+pad
    else:
        lo,hi=0,float(default_max)
    if abs(hi-lo)<1e-6: hi=lo+1
    for i in range(5):
        t=i/4; yv=lo+(hi-lo)*t; yp=cy+ch_-int(ch_*t)
        pygame.draw.line(screen,C.BORDER,(cx,yp),(cx+cw,yp),1)
        text_at(screen,f'{yv:.0f}' if abs(yv)>=10 else f'{yv:.1f}',FNT_TINY,C.MUTED,cx-6,yp,'mr')
    pygame.draw.line(screen,C.BORDER,(cx,cy+ch_),(cx+cw,cy+ch_),1)
    pygame.draw.line(screen,C.BORDER,(cx,cy),(cx,cy+ch_),1)
    t0=rows[0][0]; t1=rows[-1][0]; dt=max(t1-t0,1)
    pts=[]
    for ts,v in rows:
        if v is None: continue
        px=cx+int((ts-t0)/dt*cw)
        py=cy+ch_-int(((float(v)-lo)/(hi-lo))*ch_)
        pts.append((px,py))
    if key not in ('temperature','pressure','humidity'):
        # full-screen histogram anchored to bottom
        bar_count=min(90,len(rows)); vals2=[float(v) for _,v in rows[-bar_count:] if v is not None]
        gap=2; bw=max(3,(cw-gap*(len(vals2)-1))//max(1,len(vals2)))
        start=cx+max(0,(cw-(bw*len(vals2)+gap*(len(vals2)-1)))//2)
        for i,v in enumerate(vals2):
            bh=max(2,int(ch_*max(0,min(1,(v-lo)/(hi-lo)))))
            bcol=tuple(max(0,int(c*0.55)) for c in _zone_col(key, v, col))
            pygame.draw.rect(screen, bcol, (start+i*(bw+gap), cy+ch_-bh, bw, bh), border_radius=2)
        # Насічки порогів норми/підвищення на повноекранному графіку
        th=_ZONE_THRESHOLDS.get(key)
        if th:
            for lim, lc in ((th[0], C.YELLOW), (th[1], C.RED)):
                if lo < lim < hi:
                    yy=cy+ch_-int(ch_*(lim-lo)/(hi-lo))
                    dim=tuple(int(c*0.6) for c in lc)
                    dx=0
                    while dx < cw-3:
                        pygame.draw.line(screen, dim, (cx+dx, yy), (cx+min(dx+6, cw), yy), 1)
                        dx+=13
                    text_at(screen, f'{lim:g}', FNT_TINY, lc, cx+cw-4, yy-2, 'br')
    if len(pts)>1:
        pygame.draw.lines(screen,col,False,pts,3)
    avg=sum(vals)/len(vals)
    text_at(screen,f"Min:{min(vals):.{digits}f}  Max:{max(vals):.{digits}f}  Avg:{avg:.{digits}f}  n={len(vals)}",FNT_TINY,C.TEXT2,C.W//2,C.H-14,'bc')
    try:
        text_at(screen,datetime.fromtimestamp(rows[0][0]).strftime('%H:%M'),FNT_TINY,C.MUTED,cx,cy+ch_+5,'tl')
        text_at(screen,datetime.fromtimestamp(rows[-1][0]).strftime('%H:%M'),FNT_TINY,C.MUTED,cx+cw,cy+ch_+5,'tr')
    except Exception:
        pass
    pygame.display.update()


def draw_graph_select():
    global _graph_buttons
    _graph_buttons=[]
    _screen_header('ГРАФІКИ', 'Вибери, який графік побудувати')
    x0,y0=150,95; gap=14; bw=250; bh=126
    for i,(key,label,unit,col) in enumerate(GRAPH_CHOICES):
        x=x0+(i%4)*(bw+gap); y=y0+(i//4)*(bh+gap)
        rect=pygame.Rect(x,y,bw,bh)
        _graph_buttons.append((key,rect))
        _v12_card(rect,label,col)
        digits=1 if key in ('temperature','humidity','pressure','pm2_5','pm10') else 0
        text_at(screen,_v12_fmt(latest.get(key),digits),FNT_BIG,C.WHITE,x+18,y+45,'tl')
        text_at(screen,unit,FNT_TINY,C.TEXT2,x+bw-18,y+62,'tr')
        if key in ('temperature','humidity','pressure'):
            _v14_line((x+18,y+88,bw-36,24),key,col,n=28)
        else:
            _v14_bars((x+18,y+86,bw-36,26),key,col,max_val=_GRAPH_META.get(key,('',None,col,0))[1],n=28)
    text_at(screen,'Натисни картку — відкриється повний графік.',FNT_SMALL,C.MUTED,150,C.H-40,'tl')
    pygame.display.update()


def draw_main():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS={}
    left=210
    fill_rect(screen,(6,15,27),(0,0,left,C.H),radius=0)
    pygame.draw.line(screen,C.BORDER,(left,0),(left,C.H),1)
    text_at(screen,'≋ AIRSTATION',FNT_TITLE,C.WHITE,24,28,'tl')
    nav=[('__main__','⌂','Головна'),('__graphs__','▥','Графіки'),('__data__','▦','Дані'),('__menu__','⚙','Налаштування'),('__about__','ⓘ','Про пристрій')]
    y=88
    for i,(key,ico,lab) in enumerate(nav):
        rect=pygame.Rect(12,y,186,48)
        active=(key=='__main__')
        fill_rect(screen,(10,55,96) if active else (6,15,27),rect,radius=10)
        if active: stroke_rect(screen,C.ACCENT,rect,1,radius=10)
        text_at(screen,ico,FNT_MED,C.BLUE if active else C.TEXT2,34,y+24,'mc')
        text_at(screen,lab,FNT_SMALL,C.BLUE if active else C.TEXT2,66,y+24,'ml')
        MAIN_RECTS[key]=rect
        y+=62
    exit_rect=pygame.Rect(18,C.H-120,170,48)
    fill_rect(screen,(35,10,12),exit_rect,radius=10); stroke_rect(screen,C.RED,exit_rect,2,radius=10)
    text_at(screen,'↪  Вийти',FNT_SMALL,C.RED,exit_rect.centerx,exit_rect.centery,'mc')
    MAIN_RECTS['__exit__']=exit_rect
    now=datetime.now()
    text_at(screen,now.strftime('%H:%M:%S'),FNT_TINY,C.TEXT2,36,C.H-46,'tl')
    text_at(screen,now.strftime('%d.%m.%Y'),FNT_TINY,C.TEXT2,36,C.H-24,'tl')
    text_at(screen,'Ver. 6.15',FNT_TINY,C.MUTED,20,C.H-16,'bl')

    rx=left+18; rw=C.W-rx-18
    text_at(screen,'ГОЛОВНА',FNT_TITLE,C.WHITE,rx,20,'tl')
    ok_any=any(S.REGISTRY[k].online for k in ['bmp280','scd41','sgp41','sps30'])
    pygame.draw.circle(screen,C.GREEN if ok_any else C.RED,(C.W-120,34),6)
    text_at(screen,'Онлайн' if ok_any else 'Офлайн',FNT_SMALL,C.GREEN if ok_any else C.RED,C.W-108,34,'ml')
    text_at(screen,'☰',FNT_BIG,C.WHITE,C.W-28,32,'mc')

    env=(rx,64,680,132); clockr=(rx+694,64,rw-694,132)
    _v12_env_card(env); _v12_clock(clockr)

    y2=210; gap=12; cardw=(rw-2*gap)//3
    r1=(rx,y2,cardw,156); r2=(rx+cardw+gap,y2,cardw,156); r3=(rx+2*(cardw+gap),y2,cardw,156)
    _v12_metric(r1,'CO₂','co2','ppm',C.GREEN,2000)
    _v12_metric(r2,'VOC Index','voc_index','idx',C.PURPLE,500)
    _v12_metric(r3,'NOx Index','nox_index','idx',C.ORANGE,500)
    MAIN_RECTS['co2']=pygame.Rect(r1); MAIN_RECTS['voc_index']=pygame.Rect(r2); MAIN_RECTS['nox_index']=pygame.Rect(r3)

    bottom=384; pmw=430
    pmr=(rx,bottom,pmw,C.H-bottom-28); gr=(rx+pmw+14,bottom,rw-pmw-14,C.H-bottom-28)
    _v12_pm(pmr); _v12_graph(gr, db.get_setting('main_graph','co2'))
    for k in ['pm1_0','pm2_5','pm4_0','pm10']:
        MAIN_RECTS[k]=pygame.Rect(pmr)

    # bottom status line
    text_at(screen,'Останнє оновлення:',FNT_TINY,C.MUTED,rx+10,C.H-18,'tl')
    text_at(screen,now.strftime('%H:%M:%S'),FNT_TINY,C.TEXT2,rx+138,C.H-18,'tl')
    text_at(screen,'Інтервал опитування:',FNT_TINY,C.MUTED,rx+280,C.H-18,'tl')
    text_at(screen,f"{db.get_setting('poll_sec', str(C.POLL_INTERVAL))} сек",FNT_TINY,C.TEXT2,rx+435,C.H-18,'tl')
    current=db.get_setting('main_graph','co2')
    label=next((lbl for k,lbl,u,c in GRAPH_CHOICES if k==current), current)
    text_at(screen,'Графік на головному:',FNT_TINY,C.MUTED,rx+560,C.H-18,'tl')
    text_at(screen,label,FNT_TINY,C.TEXT2,rx+724,C.H-18,'tl')
    pygame.display.update()


def draw_menu():
    global _menu_buttons_v14, _menu_buttons_v13, _menu_source_buttons
    _menu_buttons_v14=[]; _menu_buttons_v13=[]; _menu_source_buttons=[]
    screen.fill(C.BG)
    fill_rect(screen,(6,15,27),(0,0,C.W,C.H),radius=0)
    fill_rect(screen,C.PANEL2,(18,18,110,42),radius=10)
    stroke_rect(screen,C.BORDER,(18,18,110,42),1,radius=10)
    text_at(screen,'← Назад',FNT_SMALL,C.TEXT2,73,39,'mc')
    text_at(screen,'НАЛАШТУВАННЯ',FNT_TITLE,C.WHITE,150,20,'tl')
    text_at(screen,'Повернуті вкладки: головні / сенсори / час. Калібровка дисплея прибрана.',FNT_SMALL,C.MUTED,150,52,'tl')

    # tabs
    tx=150
    for key,label in TABS:
        rect=pygame.Rect(tx,82,150,40); _menu_buttons_v14.append(('tab:'+key,rect))
        sel=_menu_tab[0]==key
        fill_rect(screen,(10,55,96) if sel else C.PANEL2,rect,radius=9)
        stroke_rect(screen,C.ACCENT if sel else C.BORDER,rect,1,radius=9)
        text_at(screen,label,FNT_SMALL,C.WHITE if sel else C.TEXT2,rect.centerx,rect.centery,'mc')
        tx+=164

    if _menu_tab[0]=='general':
        _draw_menu_general_v14()
    elif _menu_tab[0]=='sensors':
        _draw_menu_sensors_v14()
    elif _menu_tab[0]=='time':
        _draw_menu_time_v14()
    if _menu_msg[0]:
        text_at(screen,_menu_msg[0],FNT_TINY,C.YELLOW,150,C.H-20,'tl')
    pygame.display.update()
    return _menu_buttons_v14


def _v14_button(label, rect, color=C.ACCENT, text=None):
    _menu_buttons_v14.append((label, pygame.Rect(rect)))
    fill_rect(screen, tuple(int(c*0.25) for c in color), rect, radius=8)
    stroke_rect(screen, color, rect, 1, radius=8)
    text_at(screen, text or label, FNT_TINY, color, rect[0]+rect[2]//2, rect[1]+rect[3]//2, 'mc')


def _draw_menu_general_v14():
    x,y=150,140
    _v12_card((x,y,490,160),'Опитування та шина I²C',C.ACCENT)
    text_at(screen,'I²C bus',FNT_SMALL,C.MUTED,x+24,y+52,'tl')
    text_at(screen,str(_menu_bus[0]),FNT_BIG,C.WHITE,x+150,y+43,'tl')
    _v14_button('bus-',(x+230,y+48,62,34),C.ORANGE,'−')
    _v14_button('bus+',(x+302,y+48,62,34),C.GREEN,'+')
    text_at(screen,'Poll sec',FNT_SMALL,C.MUTED,x+24,y+104,'tl')
    text_at(screen,str(_menu_poll[0]),FNT_BIG,C.WHITE,x+150,y+95,'tl')
    _v14_button('poll-',(x+230,y+100,62,34),C.ORANGE,'−')
    _v14_button('poll+',(x+302,y+100,62,34),C.GREEN,'+')

    _v12_card((660,y,430,160),'Вікно графіка',C.CYAN)
    bx=690; by=y+70
    for h in [1,6,24,48]:
        rect=(bx,by,82,42); sel=_menu_hours[0]==h
        _menu_buttons_v14.append((f'graph:{h}', pygame.Rect(rect)))
        fill_rect(screen,(10,55,96) if sel else C.PANEL2,rect,radius=8)
        stroke_rect(screen,C.ACCENT if sel else C.BORDER,rect,1,radius=8)
        text_at(screen,f'{h} год',FNT_TINY,C.WHITE if sel else C.TEXT2,bx+41,by+21,'mc')
        bx+=94

    y2=326
    _v12_card((x,y2,940,210),'Графік на головному екрані',C.PURPLE)
    current=db.get_setting('main_graph','co2')
    bx=x+22; by=y2+62
    for key,label,unit,col in GRAPH_CHOICES:
        rect=pygame.Rect(bx,by,142,50); _menu_buttons_v14.append(('main_graph:'+key,rect))
        fill_rect(screen,(20,60,100) if key==current else C.PANEL2,rect,radius=8)
        stroke_rect(screen,col if key==current else C.BORDER,rect,2 if key==current else 1,radius=8)
        text_at(screen,label,FNT_TINY,C.WHITE if key==current else C.TEXT2,rect.centerx,by+16,'tc')
        text_at(screen,unit,FNT_TINY,C.MUTED,rect.centerx,by+35,'tc')
        bx+=152
        if bx+142 > x+920:
            bx=x+22; by+=62
    text_at(screen,'Додано: температура, вологість, тиск. Це не міняє I²C — тільки великий графік на головному.',FNT_TINY,C.MUTED,x+22,y2+178,'tl')

    y3=558
    _v12_card((x,y3,490,92),'Джерело температури',C.BLUE)
    t_src=S.SOURCE_MAP.get('temperature','bmp280')
    bx=x+220
    for src in ['bmp280','scd41']:
        rect=pygame.Rect(bx,y3+34,100,36); _menu_buttons_v14.append(('temp_source:'+src,rect))
        fill_rect(screen,(10,55,96) if src==t_src else C.PANEL2,rect,radius=8)
        stroke_rect(screen,C.ACCENT if src==t_src else C.BORDER,rect,1,radius=8)
        text_at(screen,src.upper(),FNT_TINY,C.WHITE if src==t_src else C.TEXT2,rect.centerx,rect.centery,'mc')
        bx+=112
    _v14_button('Save',(C.W-190,C.H-78,150,46),C.GREEN,'Зберегти')


def _draw_menu_sensors_v14():
    x,y=150,140
    _v12_card((x,y,1060,360),'Статус сенсорів',C.CYAN)
    headers=[('Сенсор',x+24),('Addr',x+220),('Статус',x+320),('Last read',x+430),('Error',x+570)]
    for txt,xx in headers: text_at(screen,txt,FNT_TINY,C.MUTED,xx,y+42,'tl')
    yy=y+72
    for key in ['bmp280','scd41','sgp41','sps30']:
        info=S.REGISTRY[key]; col=C.GREEN if info.online else C.RED
        fill_rect(screen,C.PANEL2,(x+18,yy-8,1020,38),radius=6)
        lu=datetime.fromtimestamp(info.last_read).strftime('%H:%M:%S') if info.last_read else '-'
        text_at(screen,info.name,FNT_SMALL,col,x+24,yy,'tl')
        text_at(screen,info.addr,FNT_SMALL,C.TEXT2,x+220,yy,'tl')
        text_at(screen,'OK' if info.online else 'OFF',FNT_SMALL,col,x+320,yy,'tl')
        text_at(screen,lu,FNT_SMALL,C.MUTED,x+430,yy,'tl')
        text_at(screen,(info.error_msg or '')[:52],FNT_TINY,C.YELLOW if info.error_msg else C.MUTED,x+570,yy+3,'tl')
        yy+=48
    _v14_button('Scan I2C',(x+24,y+286,110,42),C.CYAN,'Scan I²C')
    _v14_button('Restart SCD41',(x+148,y+286,150,42),C.YELLOW,'Restart SCD41')
    _v14_button('Restart SPS30',(x+312,y+286,150,42),C.YELLOW,'Restart SPS30')
    _v14_button('Purge DB',(x+476,y+286,110,42),C.RED,'Purge DB')
    with data_lock:
        d=latest.copy()
    raw = 'PM raw: ' + '  '.join(f"{lbl}={d.get(k):.2f}" if d.get(k) is not None else f"{lbl}=-" for lbl,k in [('PM1','pm1_0'),('PM2.5','pm2_5'),('PM4','pm4_0'),('PM10','pm10')])
    text_at(screen,raw,FNT_SMALL,C.CYAN,x,y+395,'tl')
    text_at(screen,f"SGP41 raw: SRAW_VOC={d.get('sraw_voc')}  SRAW_NOX={d.get('sraw_nox')}  VOC={d.get('voc_index')}  NOx={d.get('nox_index')}",FNT_SMALL,C.CYAN,x,y+428,'tl')


def _draw_menu_time_v14():
    x,y=150,140
    _v12_card((x,y,900,330),'Час',C.BLUE)
    text_at(screen,'Network mode використовує системний час Raspberry Pi / NTP.',FNT_SMALL,C.TEXT2,x+24,y+48,'tl')
    bx=x+24; by=y+88
    for mode in ['network','manual']:
        rect=pygame.Rect(bx,by,140,44); _menu_buttons_v14.append(('time:'+mode,rect))
        sel=_time_mode[0]==mode
        fill_rect(screen,(10,55,96) if sel else C.PANEL2,rect,radius=8)
        stroke_rect(screen,C.ACCENT if sel else C.BORDER,rect,1,radius=8)
        text_at(screen,('✓ ' if sel else '')+mode.upper(),FNT_TINY,C.WHITE if sel else C.TEXT2,rect.centerx,rect.centery,'mc')
        bx+=154
    fields=[('year',_manual_y,2020,2099,x+24,y+178),('month',_manual_mo,1,12,x+198,y+178),('day',_manual_d,1,31,x+372,y+178),('hour',_manual_h,0,23,x+546,y+178),('min',_manual_m,0,59,x+720,y+178)]
    for name,ref,lo,hi,xx,yy in fields:
        text_at(screen,name,FNT_TINY,C.MUTED,xx,yy-20,'tl')
        fill_rect(screen,C.PANEL2,(xx,yy,80,38),radius=7)
        text_at(screen,str(ref[0]).zfill(2) if name!='year' else str(ref[0]),FNT_SMALL,C.TEXT,xx+40,yy+19,'mc')
        _v14_button(name+'-',(xx+88,yy,34,38),C.ORANGE,'−')
        _v14_button(name+'+',(xx+128,yy,34,38),C.GREEN,'+')
    _v14_button('Apply manual time',(x+24,y+260,190,44),C.GREEN,'Apply manual time')
    _v14_button('Save',(C.W-190,C.H-78,150,46),C.GREEN,'Зберегти')


def menu_hit(pos, buttons):
    global state, chart_key
    if pygame.Rect(18,18,110,42).collidepoint(pos) or pygame.Rect(2,2,58,24).collidepoint(pos):
        state=State.MAIN; return
    for label, rect in list(_menu_buttons_v14):
        if not pygame.Rect(rect).collidepoint(pos):
            continue
        if label.startswith('tab:'):
            _menu_tab[0]=label.split(':',1)[1]; db.set_setting('menu_tab',_menu_tab[0]); return
        if label == 'Save':
            db.set_setting('i2c_bus',str(_menu_bus[0])); db.set_setting('poll_sec',str(_menu_poll[0])); db.set_setting('graph_hours',str(_menu_hours[0])); db.set_setting('temp_source',S.SOURCE_MAP.get('temperature','bmp280')); db.set_setting('time_mode',_time_mode[0]); db.set_setting('manual_hour',str(_manual_h[0])); db.set_setting('manual_min',str(_manual_m[0])); db.set_setting('manual_day',str(_manual_d[0])); db.set_setting('manual_month',str(_manual_mo[0])); db.set_setting('manual_year',str(_manual_y[0])); _menu_msg[0]='Saved'; return
        if label=='bus-': _menu_bus[0]=max(0,_menu_bus[0]-1); return
        if label=='bus+': _menu_bus[0]=min(9,_menu_bus[0]+1); return
        if label=='poll-': _menu_poll[0]=max(1,_menu_poll[0]-1); db.set_setting('poll_sec',str(_menu_poll[0])); return
        if label=='poll+': _menu_poll[0]=min(3600,_menu_poll[0]+1); db.set_setting('poll_sec',str(_menu_poll[0])); return
        if label.startswith('graph:'): _menu_hours[0]=int(label.split(':')[1]); db.set_setting('graph_hours',str(_menu_hours[0])); return
        if label.startswith('main_graph:'): db.set_setting('main_graph',label.split(':',1)[1]); return
        if label.startswith('temp_source:'):
            src=label.split(':',1)[1]; S.SOURCE_MAP['temperature']=src; db.set_setting('temp_source',src); _menu_msg[0]='Temperature <- '+src.upper(); return
        if label=='Scan I2C': scan_result.clear(); state=State.I2CSCAN; return
        if label=='Restart SCD41':
            def _do_scd():
                _menu_msg[0]='Restarting SCD41...'
                try:
                    S.REGISTRY['scd41'].online=False
                    ok=S.restart_scd41('manual menu')
                    _menu_msg[0]='SCD41 OK' if ok else 'SCD41 still OFF'
                except Exception as e: _menu_msg[0]='SCD err: '+str(e)[:20]
            threading.Thread(target=_do_scd,daemon=True).start(); return
        if label=='Restart SPS30':
            def _do_sps():
                _menu_msg[0]='Restarting SPS30...'
                try:
                    S.REGISTRY['sps30'].online=False
                    ok=S.restart_sps30(_menu_bus[0])
                    _menu_msg[0]='SPS30 OK' if ok else 'SPS30 OFF'
                except Exception as e: _menu_msg[0]='SPS err: '+str(e)[:20]
            threading.Thread(target=_do_sps,daemon=True).start(); return
        if label=='Purge DB': db.purge(7); _menu_msg[0]='DB purged'; return
        if label.startswith('time:'): _set_time_mode(label.split(':',1)[1]); return
        if label in ['year-','year+','month-','month+','day-','day+','hour-','hour+','min-','min+']:
            fields={'year':(_manual_y,2020,2099),'month':(_manual_mo,1,12),'day':(_manual_d,1,31),'hour':(_manual_h,0,23),'min':(_manual_m,0,59)}
            name=label[:-1]; op=label[-1]; ref,lo,hi=fields[name]; ref[0]=max(lo,min(hi,ref[0]+(1 if op=='+' else -1))); return
        if label=='Apply manual time': threading.Thread(target=_apply_manual_time,daemon=True).start(); return




# ══════════════════════════════════════════════════════════════════════════════
#  v16 — читабельний 7" UI: великі шрифти, двомовність укр/eng, оновлена палітра.
#  Перевизначає екрани v14. Логіка сенсорів/I²C не змінюється.
# ══════════════════════════════════════════════════════════════════════════════

def _glabel(key):
    return T('lbl_' + key)

def _trend_short(key, digits=1):
    """Компактний тренд без дужок/одиниць — для вузьких колонок."""
    vals = _hist_values(key, 30)
    if len(vals) < 3:
        return '', C.MUTED
    d = vals[-1] - vals[0]
    eps = 0.05 if key != 'pressure' else 0.02
    if abs(d) < eps:
        return '→', C.MUTED
    return ('↑' if d > 0 else '↓') + f' {abs(d):.{digits}f}', (C.GREEN if d > 0 else C.BLUE)

_BACK_RECT = pygame.Rect(16, 14, 140, 50)

def _back_btn():
    fill_rect(screen, C.PANEL2, _BACK_RECT, radius=10)
    stroke_rect(screen, C.BORDER, _BACK_RECT, 1, radius=10)
    text_at(screen, T('back'), FNT_SMALL, C.TEXT2, _BACK_RECT.centerx, _BACK_RECT.centery, 'mc')

def _screen_header(title, subtitle=''):
    screen.fill(C.BG)
    _back_btn()
    text_at(screen, title, FNT_TITLE, C.WHITE, 176, 16, 'tl')
    if subtitle:
        text_at(screen, subtitle, FNT_TINY, C.MUTED, 176, 52, 'tl')

# ── Картки головного екрана ───────────────────────────────────────────────────

def _v12_env_card(rect):
    _v12_card(rect, None, C.BORDER)
    x,y,w,h = rect
    colw = w//3
    for i,(lab_key,key,unit,col,dig) in enumerate([
        ('temp','temperature','°C',C.CYAN,1),
        ('humidity','humidity','%',C.BLUE,1),
    ]):
        xx = x + i*colw
        text_at(screen, T(lab_key), FNT_SMALL, C.TEXT2, xx+18, y+14, 'tl')
        tr,tc = _trend_short(key, 1)
        if tr:
            text_at(screen, tr, FNT_TINY, tc, xx+colw-16, y+18, 'tr')
        r = text_at(screen, _v12_fmt(latest.get(key),dig), FNT_HUGE, C.WHITE, xx+18, y+42, 'tl')
        text_at(screen, unit, FNT_SMALL, C.TEXT2, r.right+10, r.bottom-26, 'tl')
        _v14_line((xx+18, y+h-36, colw-36, 22), key, col, n=40)
        pygame.draw.line(screen, C.BORDER, (x+colw*(i+1), y+14), (x+colw*(i+1), y+h-14), 1)
    _v12_pressure((x+2*colw+1, y, colw-1, h))

def _v12_pressure(rect):
    x,y,w,h = rect
    val = latest.get('pressure')
    text_at(screen, T('pressure'), FNT_SMALL, C.TEXT2, x+16, y+14, 'tl')
    r = text_at(screen, _v12_fmt(val,1), FNT_BIG, C.WHITE, x+16, y+46, 'tl')
    text_at(screen, 'hPa', FNT_TINY, C.TEXT2, r.right+8, r.bottom-20, 'tl')
    tr,tc = _trend_short('pressure', 1)
    text_at(screen, (tr+' hPa') if tr and tr != '→' else T('stable'), FNT_TINY, tc, x+16, y+h-34, 'tl')
    cx,cy = x+w-62, y+h-46
    rr = 44
    for a in range(205, 336, 8):
        rad = math.radians(a)
        p1 = (cx+int(math.cos(rad)*rr), cy+int(math.sin(rad)*rr))
        p2 = (cx+int(math.cos(rad)*(rr-9)), cy+int(math.sin(rad)*(rr-9)))
        pygame.draw.line(screen, C.PURPLE if a < 270 else C.BLUE, p1, p2, 2)
    try: frac = max(0, min(1, (float(val)-960)/100))
    except Exception: frac = .5
    ang = math.radians(205+frac*130)
    end = (cx+int(math.cos(ang)*(rr-15)), cy+int(math.sin(ang)*(rr-15)))
    pygame.draw.line(screen, C.WHITE, (cx,cy), end, 4)
    pygame.draw.circle(screen, C.WHITE, (cx,cy), 5)

def _v12_clock(rect):
    _v12_card(rect, None, C.BORDER)
    x,y,w,h = rect
    now = datetime.now()
    hm = now.strftime('%H:%M'); ss = now.strftime('%S')
    reserve = int(w * 0.20)                       # місце під секунди
    nf = GUI.fit_font(hm, w - reserve - 40, 76, True)
    nr = text_at(screen, hm, nf, C.WHITE, x+24, y+14, 'tl')
    sf = GUI.font(max(20, int(nf.get_height() * 0.42)), True)
    text_at(screen, ss, sf, C.TEXT2, x+w-26, nr.bottom-10, 'br')
    text_at(screen, f"{i18n.weekdays()[now.weekday()]}, {now.strftime('%d.%m.%Y')}",
            FNT_TINY, C.MUTED, x+w//2, y+h-12, 'bc')

def _v12_metric(rect, title, key, unit, color, max_val):
    val = latest.get(key)
    _v12_card(rect, title, color)
    x,y,w,h = rect
    st,sc = _v12_status(val, key)
    text_at(screen, st, FNT_SMALL, sc, x+w-18, y+10, 'tr')
    r = text_at(screen, _v12_fmt(val,0), FNT_HUGE, C.WHITE, x+22, y+42, 'tl')
    text_at(screen, unit, FNT_SMALL, C.TEXT2, r.right+10, r.bottom-26, 'tl')
    tr,tc = _v12_trend(key)
    if tr:
        text_at(screen, tr, FNT_TINY, tc, x+w-18, y+44, 'tr')
    _v14_bars((x+20, y+h-56, w-40, 42), key, color, max_val=None, n=26, min_visible=2)

def _v12_pm(rect):
    _v12_card(rect, T('pm_card'), C.BORDER)
    x,y,w,h = rect
    # 4 відтінки зеленого, світлий → темний; зони перефарбовують у жовтий/червоний.
    keys = [('PM1.0','pm1_0',(134,239,172)),
            ('PM2.5','pm2_5',( 74,222,128)),
            ('PM4.0','pm4_0',( 34,197, 94)),
            ('PM10', 'pm10', ( 22,163, 74))]
    colw = (w-28)//4
    for i,(lab,key,col) in enumerate(keys):
        xx = x+14+i*colw
        val = latest.get(key)
        text_at(screen, lab, FNT_TINY, C.TEXT2, xx+colw//2, y+44, 'tc')
        text_at(screen, _v12_fmt(val,1), FNT_BIG, C.WHITE, xx+colw//2, y+68, 'tc')
        chart_top = y+116; chart_bot = y+h-48
        _v14_bars((xx+8, chart_top, colw-16, max(30, chart_bot-chart_top)),
                  key, col, max_val=None, n=14, min_visible=2)
        st,sc = _v12_status(val, key)
        text_at(screen, st, FNT_TINY, sc, xx+colw//2, y+h-32, 'tc')
        if i:
            pygame.draw.line(screen, C.BORDER, (xx, y+40), (xx, y+h-24), 1)

def _v12_graph(rect, key='co2'):
    _t, maxv, col, digits = _GRAPH_META.get(key, _GRAPH_META['co2'])
    _v12_card(rect, T('gt_'+key), C.BORDER)
    x,y,w,h = rect
    hrs = int(float(db.get_setting('graph_hours','24')))
    text_at(screen, f"{hrs} {T('hour_short')}", FNT_SMALL, C.ACCENT, x+w-18, y+10, 'tr')
    px,py,pw,ph = x+64, y+54, w-90, h-86
    vals = _hist_values(key, 60)
    if maxv is None:
        if vals:
            mn,mx = min(vals), max(vals)
            pad = max((mx-mn)*0.15, 1.0 if key != 'pressure' else 0.8)
            lo,hi = mn-pad, mx+pad
        else:
            lo,hi = 0,1
    else:
        lo,hi = 0, float(maxv)
    if abs(hi-lo) < 1e-6: hi = lo+1
    for i in range(4):
        t = i/3
        yv = lo+(hi-lo)*t
        yy = py+ph-int(t*ph)
        pygame.draw.line(screen, (40,52,74), (px,yy), (px+pw,yy), 1)
        text_at(screen, f'{yv:.0f}' if abs(yv) >= 10 else f'{yv:.1f}', FNT_TINY, C.MUTED, px-10, yy, 'mr')
    if key not in ('temperature','pressure','humidity'):
        _v14_bars((px,py,pw,ph), key, col, max_val=maxv, n=60, min_visible=2)
    _v14_line((px,py,pw,ph), key, col, max_val=maxv, n=60)

# ── Головний екран ────────────────────────────────────────────────────────────

def draw_main():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    left = 248
    fill_rect(screen, C.SIDEBAR, (0,0,left,C.H), radius=0)
    pygame.draw.line(screen, C.BORDER, (left,0), (left,C.H), 1)
    text_at(screen, '≋ AIRSTATION', FNT_TITLE, C.WHITE, 22, 26, 'tl')
    nav = [('__main__','⌂',T('nav_home')), ('__graphs__','▥',T('nav_graphs')),
           ('__data__','▦',T('nav_data')), ('__menu__','⚙',T('nav_settings')),
           ('__about__','ⓘ',T('nav_about'))]
    y = 94
    for key,ico,lab in nav:
        rect = pygame.Rect(14, y, left-28, 56)
        active = (key == '__main__')
        fill_rect(screen, C.ACCENT_D if active else C.SIDEBAR, rect, radius=12)
        if active:
            stroke_rect(screen, C.ACCENT, rect, 1, radius=12)
        text_at(screen, ico, FNT_MED, C.BLUE if active else C.TEXT2, 42, y+28, 'mc')
        text_at(screen, lab, FNT_SMALL, C.WHITE if active else C.TEXT2, 72, y+28, 'ml')
        MAIN_RECTS[key] = rect
        y += 68
    exit_rect = pygame.Rect(20, C.H-160, left-40, 56)
    fill_rect(screen, (48,14,18), exit_rect, radius=12)
    stroke_rect(screen, C.RED, exit_rect, 2, radius=12)
    text_at(screen, '↪  '+T('nav_exit'), FNT_SMALL, C.RED, exit_rect.centerx, exit_rect.centery, 'mc')
    MAIN_RECTS['__exit__'] = exit_rect
    now = datetime.now()
    text_at(screen, now.strftime('%H:%M:%S'), FNT_TINY, C.TEXT2, 24, C.H-82, 'tl')
    text_at(screen, now.strftime('%d.%m.%Y'), FNT_TINY, C.TEXT2, 24, C.H-54, 'tl')
    text_at(screen, 'Ver. 6.16', FNT_TINY, C.MUTED, left-24, C.H-54, 'tr')

    rx = left+18; rw = C.W-rx-16
    text_at(screen, T('home_title'), FNT_TITLE, C.WHITE, rx, 14, 'tl')
    ok_any = any(S.REGISTRY[k].online for k in ['bmp280','scd41','sgp41','sps30'])
    st_lab = T('online') if ok_any else T('offline')
    st_col = C.GREEN if ok_any else C.RED
    r = text_at(screen, st_lab, FNT_SMALL, st_col, C.W-64, 22, 'tr')
    pygame.draw.circle(screen, st_col, (r.left-16, r.centery), 7)
    text_at(screen, '☰', FNT_BIG, C.WHITE, C.W-30, 32, 'mc')

    clockw = 330
    env = (rx, 58, rw-clockw-14, 152); clockr = (rx+rw-clockw, 58, clockw, 152)
    _v12_env_card(env); _v12_clock(clockr)

    y2 = 222; gap = 12; cardw = (rw-2*gap)//3
    r1 = (rx, y2, cardw, 168)
    r2 = (rx+cardw+gap, y2, cardw, 168)
    r3 = (rx+2*(cardw+gap), y2, cardw, 168)
    _v12_metric(r1, 'CO₂', 'co2', 'ppm', C.GREEN, 2000)
    _v12_metric(r2, 'VOC Index', 'voc_index', 'idx', C.PURPLE, 500)
    _v12_metric(r3, 'NOx Index', 'nox_index', 'idx', C.ORANGE, 500)
    MAIN_RECTS['co2'] = pygame.Rect(r1); MAIN_RECTS['voc_index'] = pygame.Rect(r2)
    MAIN_RECTS['nox_index'] = pygame.Rect(r3)

    y3 = 402; pmw = 432
    pmr = (rx, y3, pmw, C.H-y3-34)
    gr  = (rx+pmw+14, y3, rw-pmw-14, C.H-y3-34)
    _v12_pm(pmr); _v12_graph(gr, db.get_setting('main_graph','co2'))
    for k in ['pm1_0','pm2_5','pm4_0','pm10']:
        MAIN_RECTS[k] = pygame.Rect(pmr)

    yb = C.H-6
    r = text_at(screen, T('last_update'), FNT_TINY, C.MUTED, rx+6, yb, 'bl')
    r = text_at(screen, now.strftime('%H:%M:%S'), FNT_TINY, C.TEXT2, r.right+10, yb, 'bl')
    r = text_at(screen, T('poll_int'), FNT_TINY, C.MUTED, r.right+40, yb, 'bl')
    r = text_at(screen, f"{db.get_setting('poll_sec', str(C.POLL_INTERVAL))} {T('sec')}", FNT_TINY, C.TEXT2, r.right+10, yb, 'bl')
    r = text_at(screen, T('main_graph'), FNT_TINY, C.MUTED, r.right+40, yb, 'bl')
    text_at(screen, _glabel(db.get_setting('main_graph','co2')), FNT_TINY, C.TEXT2, r.right+10, yb, 'bl')
    pygame.display.update()

# ── Повноекранний графік ──────────────────────────────────────────────────────

def draw_chart(key: str):
    screen.fill(C.BG)
    _t, default_max, col, digits = _GRAPH_META.get(key, (key, None, C.ACCENT, 1))
    hrs = float(db.get_setting('graph_hours', '24'))
    text_at(screen, f"{T('gt_'+key)} — {int(hrs)} {T('hour_short')}", FNT_BIG, col, C.W//2, 14, 'tc')
    _back_btn()
    try:
        rows = db.query(key, hrs)
    except Exception:
        rows = []
    if not rows:
        rows = [(time.time()-(len(_hist_values(key,200))-i)*C.POLL_INTERVAL, v)
                for i,v in enumerate(_hist_values(key,200))]
    if len(rows) < 2:
        text_at(screen, T('not_enough'), FNT_BIG, C.MUTED, C.W//2, C.H//2, 'mc')
        pygame.display.update(); return
    cx,cy = 92, 80; cw,ch_ = C.W-cx-28, C.H-cy-64
    vals = [float(v) for _,v in rows if v is not None]
    if default_max is None:
        mn,mx = min(vals), max(vals)
        pad = (mx-mn)*0.12 or (0.8 if key == 'pressure' else 1.0)
        lo,hi = mn-pad, mx+pad
    else:
        lo,hi = 0, float(default_max)
    if abs(hi-lo) < 1e-6: hi = lo+1
    for i in range(5):
        t = i/4; yv = lo+(hi-lo)*t; yp = cy+ch_-int(ch_*t)
        pygame.draw.line(screen, C.BORDER, (cx,yp), (cx+cw,yp), 1)
        text_at(screen, f'{yv:.0f}' if abs(yv) >= 10 else f'{yv:.1f}', FNT_TINY, C.MUTED, cx-8, yp, 'mr')
    pygame.draw.line(screen, C.BORDER, (cx,cy+ch_), (cx+cw,cy+ch_), 1)
    pygame.draw.line(screen, C.BORDER, (cx,cy), (cx,cy+ch_), 1)
    t0 = rows[0][0]; t1 = rows[-1][0]; dt = max(t1-t0, 1)
    pts = []
    for ts,v in rows:
        if v is None: continue
        px = cx+int((ts-t0)/dt*cw)
        py = cy+ch_-int(((float(v)-lo)/(hi-lo))*ch_)
        pts.append((px,py))
    if key not in ('temperature','pressure','humidity'):
        bar_count = min(90, len(rows))
        vals2 = [float(v) for _,v in rows[-bar_count:] if v is not None]
        gap = 2; bw = max(3, (cw-gap*(len(vals2)-1))//max(1, len(vals2)))
        start = cx+max(0, (cw-(bw*len(vals2)+gap*(len(vals2)-1)))//2)
        for i,v in enumerate(vals2):
            bh = max(2, int(ch_*max(0, min(1, (v-lo)/(hi-lo)))))
            bcol = tuple(max(0, int(c*0.55)) for c in _zone_col(key, v, col))
            pygame.draw.rect(screen, bcol, (start+i*(bw+gap), cy+ch_-bh, bw, bh), border_radius=2)
        th = _ZONE_THRESHOLDS.get(key)
        if th:
            for lim, lc in ((th[0], C.YELLOW), (th[1], C.RED)):
                if lo < lim < hi:
                    yy = cy+ch_-int(ch_*(lim-lo)/(hi-lo))
                    dim = tuple(int(c*0.6) for c in lc)
                    dx = 0
                    while dx < cw-3:
                        pygame.draw.line(screen, dim, (cx+dx, yy), (cx+min(dx+6, cw), yy), 1)
                        dx += 13
                    text_at(screen, f'{lim:g}', FNT_TINY, lc, cx+cw-6, yy-2, 'br')
    if len(pts) > 1:
        pygame.draw.lines(screen, col, False, pts, 3)
    avg = sum(vals)/len(vals)
    text_at(screen, f"Min:{min(vals):.{digits}f}   Max:{max(vals):.{digits}f}   Avg:{avg:.{digits}f}   n={len(vals)}",
            FNT_SMALL, C.TEXT2, C.W//2, C.H-10, 'bc')
    try:
        text_at(screen, datetime.fromtimestamp(rows[0][0]).strftime('%H:%M'), FNT_TINY, C.MUTED, cx, cy+ch_+6, 'tl')
        text_at(screen, datetime.fromtimestamp(rows[-1][0]).strftime('%H:%M'), FNT_TINY, C.MUTED, cx+cw, cy+ch_+6, 'tr')
    except Exception:
        pass
    pygame.display.update()

# ── Екран вибору графіків ─────────────────────────────────────────────────────

def graph_select_hit(pos):
    for key, rect in _graph_buttons:
        if rect.collidepoint(pos):
            return key
    if pygame.Rect(2, 2, 170, 72).collidepoint(pos):
        return '__back__'
    return None

def draw_graph_select():
    global _graph_buttons
    _graph_buttons = []
    _screen_header(T('graphs_title'), T('graphs_sub'))
    x0,y0 = 150, 106; gap = 16; bw = 262; bh = 172
    for i,(key,_lbl,unit,col) in enumerate(GRAPH_CHOICES):
        x = x0+(i%4)*(bw+gap); y = y0+(i//4)*(bh+gap)
        rect = pygame.Rect(x, y, bw, bh)
        _graph_buttons.append((key, rect))
        _v12_card(rect, _glabel(key), col)
        digits = 1 if key in ('temperature','humidity','pressure','pm2_5','pm10') else 0
        text_at(screen, _v12_fmt(latest.get(key),digits), FNT_BIG, C.WHITE, x+20, y+44, 'tl')
        text_at(screen, unit, FNT_TINY, C.TEXT2, x+bw-18, y+56, 'tr')
        if key in ('temperature','humidity','pressure'):
            _v14_line((x+20, y+108, bw-40, 44), key, col, n=26)
        else:
            _v14_bars((x+20, y+104, bw-40, 48), key, col, max_val=None, n=20, min_visible=2)
    text_at(screen, T('graphs_hint'), FNT_SMALL, C.MUTED, 150, C.H-52, 'tl')
    pygame.display.update()

# ── Екран "Дані" ──────────────────────────────────────────────────────────────

def draw_data_screen():
    _screen_header(T('data_title'), T('data_sub'))
    now = datetime.now()
    text_at(screen, T('last_update')+' '+now.strftime('%H:%M:%S'), FNT_SMALL, C.TEXT2, C.W-32, 26, 'tr')
    cards = [
        (T('d_bmp'), [(T('r_temp_bmp'),'temp_bmp','°C'), (T('pressure'),'pressure','hPa')], C.PURPLE),
        (T('d_scd'), [('CO₂','co2','ppm'), (T('r_temp_scd'),'temp_scd','°C'), (T('r_hum_scd'),'hum_scd','%')], C.GREEN),
        (T('d_sgp'), [('VOC Index','voc_index','idx'), ('NOx Index','nox_index','idx')], C.ORANGE),
        (T('d_sps'), [('PM1.0','pm1_0','µg/m³'), ('PM2.5','pm2_5','µg/m³'), ('PM4.0','pm4_0','µg/m³'), ('PM10','pm10','µg/m³')], C.CYAN),
    ]
    x0,y0 = 150, 100; bw = 545; bh = 286; gap = 20
    for i,(title,rows,col) in enumerate(cards):
        x = x0+(i%2)*(bw+gap); y = y0+(i//2)*(bh+gap)
        _v12_card((x,y,bw,bh), title, col)
        online_key = ['bmp280','scd41','sgp41','sps30'][i]
        ok = S.REGISTRY[online_key].online
        pygame.draw.circle(screen, C.GREEN if ok else C.RED, (x+bw-34, y+26), 8)
        text_at(screen, 'OK' if ok else 'OFF', FNT_TINY, C.GREEN if ok else C.RED, x+bw-50, y+18, 'tr')
        yy = y+66
        for lab,key,unit in rows:
            text_at(screen, lab, FNT_SMALL, C.TEXT2, x+24, yy, 'tl')
            text_at(screen, _v12_fmt(latest.get(key), 1 if key in ['temp_bmp','temp_scd','hum_scd','pressure'] else 0),
                    FNT_BIG, C.WHITE, x+330, yy-8, 'tl')
            text_at(screen, unit, FNT_SMALL, C.MUTED, x+bw-24, yy, 'tr')
            yy += 52
    pygame.display.update()

# ── Екран "Про пристрій" ──────────────────────────────────────────────────────

def draw_about_screen():
    _screen_header(T('about_title'), T('about_sub'))
    x,y = 150, 102
    for txt,kind in i18n.about_lines():
        if txt == '':
            y += 14; continue
        if kind == 'h':
            col,f,step = C.CYAN, FNT_MED, 40
        elif kind == 'w':
            col,f,step = C.ORANGE, FNT_MED, 40
        else:
            col,f,step = C.TEXT2, FNT_SMALL, 33
        text_at(screen, txt, f, col, x, y, 'tl')
        y += step
    pygame.display.update()

# ── Налаштування ──────────────────────────────────────────────────────────────

def _v14_button(label, rect, color=C.ACCENT, text=None):
    _menu_buttons_v14.append((label, pygame.Rect(rect)))
    fill_rect(screen, tuple(int(c*0.28) for c in color), rect, radius=10)
    stroke_rect(screen, color, rect, 1, radius=10)
    s = str(text or label)
    f = GUI.fit_font(s, rect[2] - 14, 21, False)
    text_at(screen, s, f, color, rect[0]+rect[2]//2, rect[1]+rect[3]//2, 'mc')

def draw_menu():
    global _menu_buttons_v14, _menu_buttons_v13, _menu_source_buttons
    _menu_buttons_v14 = []; _menu_buttons_v13 = []; _menu_source_buttons = []
    screen.fill(C.BG)
    _back_btn()
    text_at(screen, T('menu_title'), FNT_TITLE, C.WHITE, 176, 16, 'tl')
    text_at(screen, T('menu_sub'), FNT_TINY, C.MUTED, 176, 52, 'tl')
    tx = 150
    for key,_lab in TABS:
        rect = pygame.Rect(tx, 92, 190, 50)
        _menu_buttons_v14.append(('tab:'+key, rect))
        sel = _menu_tab[0] == key
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=10)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=10)
        text_at(screen, T('tab_'+key), FNT_SMALL, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')
        tx += 204
    if _menu_tab[0] == 'general':
        _draw_menu_general_v14()
    elif _menu_tab[0] == 'sensors':
        _draw_menu_sensors_v14()
    elif _menu_tab[0] == 'time':
        _draw_menu_time_v14()
    if _menu_msg[0]:
        text_at(screen, _menu_msg[0], FNT_SMALL, C.YELLOW, 150, C.H-8, 'bl')
    pygame.display.update()
    return _menu_buttons_v14

def _draw_menu_general_v14():
    x,y = 150, 158
    _v12_card((x, y, 540, 178), T('card_poll'), C.ACCENT)
    text_at(screen, 'I²C bus', FNT_SMALL, C.MUTED, x+24, y+58, 'tl')
    text_at(screen, str(_menu_bus[0]), FNT_BIG, C.WHITE, x+190, y+50, 'tl')
    _v14_button('bus-', (x+282, y+52, 70, 44), C.ORANGE, '−')
    _v14_button('bus+', (x+362, y+52, 70, 44), C.GREEN, '+')
    text_at(screen, 'Poll sec', FNT_SMALL, C.MUTED, x+24, y+122, 'tl')
    text_at(screen, str(_menu_poll[0]), FNT_BIG, C.WHITE, x+190, y+114, 'tl')
    _v14_button('poll-', (x+282, y+116, 70, 44), C.ORANGE, '−')
    _v14_button('poll+', (x+362, y+116, 70, 44), C.GREEN, '+')

    _v12_card((710, y, 440, 178), T('card_window'), C.CYAN)
    bx = 736; by = y+86
    for hh in [1, 6, 24, 48]:
        rect = (bx, by, 92, 52); sel = _menu_hours[0] == hh
        _menu_buttons_v14.append((f'graph:{hh}', pygame.Rect(rect)))
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=9)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=9)
        text_at(screen, f'{hh} {T("hour_short")}', FNT_SMALL, C.WHITE if sel else C.TEXT2, bx+46, by+26, 'mc')
        bx += 100

    y2 = 352
    _v12_card((x, y2, 1000, 236), T('card_maingraph'), C.PURPLE)
    current = db.get_setting('main_graph', 'co2')
    bx = x+22; by = y2+52
    for key,_lbl,unit,col in GRAPH_CHOICES:
        rect = pygame.Rect(bx, by, 180, 62)
        _menu_buttons_v14.append(('main_graph:'+key, rect))
        fill_rect(screen, C.ACCENT_D if key == current else C.PANEL2, rect, radius=9)
        stroke_rect(screen, col if key == current else C.BORDER, rect, 2 if key == current else 1, radius=9)
        text_at(screen, _glabel(key), FNT_TINY, C.WHITE if key == current else C.TEXT2, rect.centerx, by+9, 'tc')
        text_at(screen, unit, FNT_TINY, C.MUTED, rect.centerx, by+34, 'tc')
        bx += 190
        if bx+180 > x+990:
            bx = x+22; by += 72
    text_at(screen, T('maingraph_hint'), FNT_TINY, C.MUTED, x+22, y2+204, 'tl')

    y3 = 602
    _v12_card((x, y3, 470, 92), T('card_tempsrc'), C.BLUE)
    t_src = S.SOURCE_MAP.get('temperature', 'bmp280')
    bx = x+236
    for src in ['bmp280', 'scd41']:
        rect = pygame.Rect(bx, y3+38, 108, 42)
        _menu_buttons_v14.append(('temp_source:'+src, rect))
        fill_rect(screen, C.ACCENT_D if src == t_src else C.PANEL2, rect, radius=9)
        stroke_rect(screen, C.ACCENT if src == t_src else C.BORDER, rect, 1, radius=9)
        text_at(screen, src.upper(), FNT_TINY, C.WHITE if src == t_src else C.TEXT2, rect.centerx, rect.centery, 'mc')
        bx += 118

    _v12_card((640, y3, 300, 92), T('card_lang'), C.GREEN)
    cur = i18n.get_lang()
    bx = 640+152
    for code,lab in [('uk','УКР'), ('en','ENG')]:
        rect = pygame.Rect(bx, y3+38, 64, 42)
        _menu_buttons_v14.append(('lang:'+code, rect))
        sel = cur == code
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=9)
        stroke_rect(screen, C.GREEN if sel else C.BORDER, rect, 2 if sel else 1, radius=9)
        text_at(screen, lab, FNT_TINY, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')
        bx += 72

    _v14_button('Save', (C.W-300, y3+20, 160, 56), C.GREEN, T('save'))

def _draw_menu_sensors_v14():
    x,y = 150, 158
    _v12_card((x, y, 1080, 372), T('sens_status'), C.CYAN)
    headers = [(T('col_sensor'),x+24), (T('col_addr'),x+300), (T('col_status'),x+436),
               (T('col_last'),x+560), (T('col_error'),x+760)]
    for txt,xx in headers:
        text_at(screen, txt, FNT_TINY, C.MUTED, xx, y+44, 'tl')
    yy = y+82
    for key in ['bmp280','scd41','sgp41','sps30']:
        info = S.REGISTRY[key]; col = C.GREEN if info.online else C.RED
        fill_rect(screen, C.PANEL2, (x+18, yy-10, 1044, 46), radius=8)
        lu = datetime.fromtimestamp(info.last_read).strftime('%H:%M:%S') if info.last_read else '-'
        text_at(screen, info.name, FNT_SMALL, col, x+24, yy, 'tl')
        text_at(screen, info.addr, FNT_SMALL, C.TEXT2, x+300, yy, 'tl')
        text_at(screen, 'OK' if info.online else 'OFF', FNT_SMALL, col, x+436, yy, 'tl')
        text_at(screen, lu, FNT_SMALL, C.MUTED, x+560, yy, 'tl')
        text_at(screen, (info.error_msg or '')[:30], FNT_TINY, C.YELLOW if info.error_msg else C.MUTED, x+760, yy+3, 'tl')
        yy += 56
    _v14_button('Scan I2C', (x+24, y+308, 140, 50), C.CYAN, 'Scan I²C')
    _v14_button('Restart SCD41', (x+180, y+308, 190, 50), C.YELLOW, 'Restart SCD41')
    _v14_button('Restart SPS30', (x+386, y+308, 190, 50), C.YELLOW, 'Restart SPS30')
    _v14_button('Purge DB', (x+592, y+308, 140, 50), C.RED, 'Purge DB')
    with data_lock:
        d = latest.copy()
    raw = 'PM raw:  ' + '   '.join(f"{lbl}={d.get(k):.2f}" if d.get(k) is not None else f"{lbl}=-"
                                   for lbl,k in [('PM1','pm1_0'),('PM2.5','pm2_5'),('PM4','pm4_0'),('PM10','pm10')])
    text_at(screen, raw, FNT_SMALL, C.CYAN, x, y+392, 'tl')
    text_at(screen, f"SGP41 raw:  SRAW_VOC={d.get('sraw_voc')}   SRAW_NOX={d.get('sraw_nox')}   VOC={d.get('voc_index')}   NOx={d.get('nox_index')}",
            FNT_SMALL, C.CYAN, x, y+426, 'tl')

def _draw_menu_time_v14():
    x,y = 150, 158
    _v12_card((x, y, 1000, 400), T('time_card'), C.BLUE)
    text_at(screen, T('time_hint'), FNT_TINY, C.TEXT2, x+24, y+44, 'tl')
    bx = x+24; by = y+84
    for mode in ['network', 'manual']:
        rect = pygame.Rect(bx, by, 170, 50)
        _menu_buttons_v14.append(('time:'+mode, rect))
        sel = _time_mode[0] == mode
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=9)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=9)
        text_at(screen, ('✓ ' if sel else '')+mode.upper(), FNT_TINY, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')
        bx += 184
    fields = [('year',_manual_y,2020,2099,x+24), ('month',_manual_mo,1,12,x+216),
              ('day',_manual_d,1,31,x+408), ('hour',_manual_h,0,23,x+600), ('min',_manual_m,0,59,x+792)]
    yy = y+206
    for name,ref,lo,hi,xx in fields:
        text_at(screen, name, FNT_TINY, C.MUTED, xx, yy-28, 'tl')
        fill_rect(screen, C.PANEL2, (xx, yy, 90, 46), radius=8)
        text_at(screen, str(ref[0]).zfill(2) if name != 'year' else str(ref[0]), FNT_SMALL, C.TEXT, xx+45, yy+23, 'mc')
        _v14_button(name+'-', (xx+96, yy, 40, 46), C.ORANGE, '−')
        _v14_button(name+'+', (xx+142, yy, 40, 46), C.GREEN, '+')
    _v14_button('Apply manual time', (x+24, y+300, 240, 54), C.GREEN, T('apply_time'))
    _v14_button('Save', (C.W-320, y+300, 160, 54), C.GREEN, T('save'))

def menu_hit(pos, buttons):
    global state, chart_key
    if pygame.Rect(2, 2, 170, 72).collidepoint(pos):
        state = State.MAIN; return
    for label, rect in list(_menu_buttons_v14):
        if not pygame.Rect(rect).collidepoint(pos):
            continue
        if label.startswith('tab:'):
            _menu_tab[0] = label.split(':',1)[1]; db.set_setting('menu_tab', _menu_tab[0]); return
        if label.startswith('lang:'):
            code = label.split(':',1)[1]
            i18n.set_lang(code); db.set_setting('lang', code)
            _menu_msg[0] = T('saved'); return
        if label == 'Save':
            db.set_setting('i2c_bus', str(_menu_bus[0])); db.set_setting('poll_sec', str(_menu_poll[0]))
            db.set_setting('graph_hours', str(_menu_hours[0]))
            db.set_setting('temp_source', S.SOURCE_MAP.get('temperature','bmp280'))
            db.set_setting('lang', i18n.get_lang())
            db.set_setting('time_mode', _time_mode[0])
            db.set_setting('manual_hour', str(_manual_h[0])); db.set_setting('manual_min', str(_manual_m[0]))
            db.set_setting('manual_day', str(_manual_d[0])); db.set_setting('manual_month', str(_manual_mo[0]))
            db.set_setting('manual_year', str(_manual_y[0]))
            _menu_msg[0] = T('saved'); return
        if label == 'bus-': _menu_bus[0] = max(0, _menu_bus[0]-1); return
        if label == 'bus+': _menu_bus[0] = min(9, _menu_bus[0]+1); return
        if label == 'poll-': _menu_poll[0] = max(1, _menu_poll[0]-1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label == 'poll+': _menu_poll[0] = min(3600, _menu_poll[0]+1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label.startswith('graph:'): _menu_hours[0] = int(label.split(':')[1]); db.set_setting('graph_hours', str(_menu_hours[0])); return
        if label.startswith('main_graph:'): db.set_setting('main_graph', label.split(':',1)[1]); return
        if label.startswith('temp_source:'):
            src = label.split(':',1)[1]; S.SOURCE_MAP['temperature'] = src
            db.set_setting('temp_source', src); _menu_msg[0] = 'Temperature <- '+src.upper(); return
        if label == 'Scan I2C': scan_result.clear(); state = State.I2CSCAN; return
        if label == 'Restart SCD41':
            def _do_scd():
                _menu_msg[0] = 'Restarting SCD41...'
                try:
                    S.REGISTRY['scd41'].online = False
                    ok = S.restart_scd41('manual menu')
                    _menu_msg[0] = 'SCD41 OK' if ok else 'SCD41 still OFF'
                except Exception as e: _menu_msg[0] = 'SCD err: '+str(e)[:20]
            threading.Thread(target=_do_scd, daemon=True).start(); return
        if label == 'Restart SPS30':
            def _do_sps():
                _menu_msg[0] = 'Restarting SPS30...'
                try:
                    S.REGISTRY['sps30'].online = False
                    ok = S.restart_sps30(_menu_bus[0])
                    _menu_msg[0] = 'SPS30 OK' if ok else 'SPS30 OFF'
                except Exception as e: _menu_msg[0] = 'SPS err: '+str(e)[:20]
            threading.Thread(target=_do_sps, daemon=True).start(); return
        if label == 'Purge DB': db.purge(7); _menu_msg[0] = 'DB purged'; return
        if label.startswith('time:'): _set_time_mode(label.split(':',1)[1]); return
        if label in ['year-','year+','month-','month+','day-','day+','hour-','hour+','min-','min+']:
            fields = {'year':(_manual_y,2020,2099),'month':(_manual_mo,1,12),'day':(_manual_d,1,31),
                      'hour':(_manual_h,0,23),'min':(_manual_m,0,59)}
            name = label[:-1]; op = label[-1]; ref,lo,hi = fields[name]
            ref[0] = max(lo, min(hi, ref[0]+(1 if op == '+' else -1))); return


# ══════════════════════════════════════════════════════════════════════════════
#  v17 — авто-приховування панелі/напису, аналоговий гейдж, градієнт-шкала,
#        графік з фіксованим вікном часу + проріджування. Логіку сенсорів
#        та I²C не змінює; лише перевизначає рендер і додає вкладку "Вигляд".
# ══════════════════════════════════════════════════════════════════════════════

# ── Стан UI показу/приховування ───────────────────────────────────────────────
_ui_shown      = [True]
_ui_last_touch = [time.time()]

def _auto_hide_enabled():
    return db.get_setting('auto_hide', '1') == '1'

def _header_pref():
    return db.get_setting('show_header', '1') == '1'

def _reveal_ui():
    _ui_shown[0] = True
    _ui_last_touch[0] = time.time()

def _tick_autohide():
    if not _auto_hide_enabled():
        _ui_shown[0] = True
        return
    try:
        sec = int(float(db.get_setting('hide_sec', '10')))
    except Exception:
        sec = 10
    if _ui_shown[0] and (time.time() - _ui_last_touch[0]) > max(2, sec):
        _ui_shown[0] = False

# ── Дані для графіків: фіксоване вікно + проріджування ─────────────────────────

def _window_series(key, hours):
    """Повертає (now, t0, rows) де rows=[(ts,value)…] за останні `hours` годин,
    з реальними мітками часу. Вісь X фіксована на [now-hours, now], тож поки
    даних менше за період — вони заповнюють графік зліва, а не розтягуються."""
    now = time.time()
    t0 = now - hours * 3600
    rows = []
    if not args.nodb:
        try:
            rows = db.query(key, hours)
        except Exception:
            rows = []
    if not rows:
        vals = _hist_values(key, 2000)
        if vals:
            try:
                step = max(1, int(float(db.get_setting('poll_sec', str(C.POLL_INTERVAL)))))
            except Exception:
                step = C.POLL_INTERVAL
            n = len(vals)
            rows = [(now - (n - 1 - i) * step, float(v)) for i, v in enumerate(vals)]
            rows = [r for r in rows if r[0] >= t0]
    return now, t0, rows

def _decimate(rows, target):
    """Проріджує rows до ~target точок усередненням по кошиках — швидше рендериться
    на довгих періодах (24/48 год) без втрати форми кривої."""
    n = len(rows)
    if target < 1 or n <= target:
        return rows
    out = []
    for b in range(target):
        s = b * n // target
        e = (b + 1) * n // target
        if e <= s:
            continue
        chunk = rows[s:e]
        ts = chunk[len(chunk) // 2][0]
        avg = sum(v for _, v in chunk) / len(chunk)
        out.append((ts, avg))
    return out

def _scale_lohi(key, maxv, vals):
    if maxv is not None:
        return 0.0, float(maxv)
    if vals:
        mn, mx = min(vals), max(vals)
        pad = max((mx - mn) * 0.15, 0.8 if key == 'pressure' else 1.0)
        return mn - pad, mx + pad
    return 0.0, 1.0

def _plot_fixed(px, py, pw, ph, key, col, hours, bars=True, line=True):
    """Малює графік із фіксованою віссю часу [now-hours, now] у прямокутнику."""
    _t, maxv, _c, _d = _GRAPH_META.get(key, ('', None, col, 0))
    now, t0, rows = _window_series(key, hours)
    span = max(now - t0, 1.0)
    vals_all = [v for _, v in rows]
    lo, hi = _scale_lohi(key, maxv, vals_all)
    if abs(hi - lo) < 1e-6:
        hi = lo + 1
    # сітка + підписи Y
    for i in range(4):
        t = i / 3
        yv = lo + (hi - lo) * t
        yy = py + ph - int(t * ph)
        pygame.draw.line(screen, (40, 52, 74), (px, yy), (px + pw, yy), 1)
        text_at(screen, f'{yv:.0f}' if abs(yv) >= 10 else f'{yv:.1f}',
                FNT_TINY, C.MUTED, px - 10, yy, 'mr')
    if not rows:
        text_at(screen, T('not_enough'), FNT_SMALL, C.MUTED, px + pw // 2, py + ph // 2, 'mc')
        return
    def _x(ts):
        return px + int((ts - t0) / span * pw)
    def _y(v):
        return py + ph - int(max(0.0, min(1.0, (float(v) - lo) / (hi - lo))) * ph)
    # стовпчики (для нелінійних каналів)
    if bars and key not in ('temperature', 'pressure', 'humidity'):
        brows = _decimate(rows, max(16, pw // 7))
        bw = max(2, int(pw / max(len(brows), 1)) - 1)
        for ts, v in brows:
            bx = _x(ts)
            bh = max(2, (py + ph) - _y(v))
            bcol = _zone_col(key, v, col)
            pygame.draw.rect(screen, bcol, (bx, py + ph - bh, bw, bh), border_radius=2)
        # насічки порогів
        th = _ZONE_THRESHOLDS.get(key)
        if th:
            for lim, lc in ((th[0], C.YELLOW), (th[1], C.RED)):
                if lo < lim < hi:
                    yy = _y(lim)
                    dim = tuple(int(c * 0.6) for c in lc)
                    dx = 0
                    while dx < pw - 2:
                        pygame.draw.line(screen, dim, (px + dx, yy), (px + min(dx + 5, pw), yy), 1)
                        dx += 11
                    text_at(screen, f'{lim:g}', FNT_TINY, lc, px + pw - 4, yy - 2, 'br')
    # лінія
    if line:
        lrows = _decimate(rows, max(30, pw // 3))
        pts = [(_x(ts), _y(v)) for ts, v in lrows]
        if len(pts) > 1:
            pygame.draw.lines(screen, col, False, pts, 2)
    # мітки часу знизу
    try:
        text_at(screen, datetime.fromtimestamp(t0).strftime('%H:%M'), FNT_TINY, C.MUTED, px, py + ph + 4, 'tl')
        text_at(screen, datetime.fromtimestamp(now).strftime('%H:%M'), FNT_TINY, C.MUTED, px + pw, py + ph + 4, 'tr')
    except Exception:
        pass

# ── Аналоговий гейдж (як стрілка барометра) ───────────────────────────────────

def _v12_gauge(rect, key='co2'):
    _t, maxv, col, digits = _GRAPH_META.get(key, _GRAPH_META['co2'])
    _v12_card(rect, T('gt_' + key), C.BORDER)
    x, y, w, h = rect
    val = latest.get(key)
    th = _ZONE_THRESHOLDS.get(key)
    gmax = float(maxv) if maxv else (th[1] * 1.6 if th else 100.0)
    cx = x + w // 2
    cy = y + h - 54
    R = int(min(w / 2 - 34, h - 96))
    R = max(60, R)

    def pt(frac, rad):
        ang = math.radians(180 + 180 * max(0.0, min(1.0, frac)))
        return (cx + int(math.cos(ang) * rad), cy + int(math.sin(ang) * rad))

    # кольорові зони по дузі
    gf = (th[0] / gmax) if th else 0.5
    wf = (th[1] / gmax) if th else 0.8
    seg = 60
    for i in range(seg):
        f0 = i / seg
        p1 = pt(f0, R)
        p2 = pt((i + 1) / seg, R)
        c = C.GREEN if f0 < gf else (C.YELLOW if f0 < wf else C.RED)
        pygame.draw.line(screen, c, p1, p2, 10)
    # риски 0 і max
    text_at(screen, '0', FNT_TINY, C.MUTED, *pt(0.0, R + 16), 'mc')
    text_at(screen, f'{gmax:g}', FNT_TINY, C.MUTED, *pt(1.0, R + 16), 'mc')

    # стрілка
    try:
        frac = max(0.0, min(1.0, float(val) / gmax))
    except Exception:
        frac = 0.0
    end = pt(frac, R - 14)
    pygame.draw.line(screen, C.WHITE, (cx, cy), end, 4)
    pygame.draw.circle(screen, C.WHITE, (cx, cy), 7)
    pygame.draw.circle(screen, col, (cx, cy), 4)

    # значення + статус усередині дуги
    text_at(screen, _v12_fmt(val, 0), FNT_HUGE, C.WHITE, cx, cy - int(R * 0.34), 'mc')
    st, sc = _v12_status(val, key)
    if st:
        text_at(screen, st, FNT_SMALL, sc, cx, cy - int(R * 0.34) + 34, 'mc')
    hrs = int(float(db.get_setting('graph_hours', '24')))
    text_at(screen, f"{hrs} {T('hour_short')}", FNT_TINY, C.ACCENT, x + w - 16, y + 10, 'tr')

# ── Горизонтальна градієнт-шкала поточного значення ───────────────────────────

def _grad_color(t):
    """t∈[0,1] → зелений → жовтий → помаранчевий → темно-червоний."""
    stops = [(0.0, (52, 211, 153)), (0.45, (250, 204, 21)),
             (0.72, (251, 146, 60)), (1.0, (140, 24, 24))]
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / max(t1 - t0, 1e-6)
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return stops[-1][1]

def _grad_bar(rect, key, value):
    """Горизонтальна шкала з градієнтом зелений→темно-червоний і маркером
    поточного значення. Заміна міні-гістограми у боксах CO₂/VOC/NOx."""
    x, y, w, h = rect
    th = _ZONE_THRESHOLDS.get(key)
    _t, maxv, _c, _d = _GRAPH_META.get(key, ('', None, C.ACCENT, 0))
    gmax = (th[1] * 1.5) if th else (float(maxv) if maxv else 100.0)
    # градієнт по ширині
    for i in range(w):
        c = _grad_color(i / max(w - 1, 1))
        pygame.draw.line(screen, c, (x + i, y), (x + i, y + h))
    stroke_rect(screen, C.BORDER, (x, y, w, h), 1, radius=6)
    # позначки порогів
    if th:
        for lim in th:
            if 0 < lim < gmax:
                mx = x + int(w * lim / gmax)
                pygame.draw.line(screen, (12, 18, 30), (mx, y), (mx, y + h), 1)
    # маркер поточного значення
    try:
        frac = max(0.0, min(1.0, float(value) / gmax))
        mx = x + int(w * frac)
        pygame.draw.line(screen, C.WHITE, (mx, y - 3), (mx, y + h + 3), 3)
        pygame.draw.circle(screen, C.WHITE, (mx, y + h + 6), 4)
    except Exception:
        pass

# ── Перевизначені бокси метрик (стиль стовпчики / градієнт) ────────────────────

def _v12_metric(rect, title, key, unit, color, max_val):
    val = latest.get(key)
    _v12_card(rect, title, color)
    x, y, w, h = rect
    st, sc = _v12_status(val, key)
    text_at(screen, st, FNT_SMALL, sc, x + w - 18, y + 10, 'tr')
    r = text_at(screen, _v12_fmt(val, 0), FNT_HUGE, C.WHITE, x + 22, y + 42, 'tl')
    text_at(screen, unit, FNT_SMALL, C.TEXT2, r.right + 10, r.bottom - 26, 'tl')
    tr, tc = _v12_trend(key)
    if tr:
        text_at(screen, tr, FNT_TINY, tc, x + w - 18, y + 44, 'tr')
    if db.get_setting('metric_style', 'bars') == 'gradient':
        _grad_bar((x + 22, y + h - 40, w - 44, 20), key, val)
    else:
        _v14_bars((x + 20, y + h - 56, w - 40, 42), key, color, max_val=None, n=26, min_visible=2)

# ── Графік на головному екрані: фіксоване вікно або аналог ─────────────────────

def _v12_graph(rect, key='co2'):
    if db.get_setting('main_graph_style', 'bars') == 'gauge':
        _v12_gauge(rect, key)
        return
    _t, maxv, col, digits = _GRAPH_META.get(key, _GRAPH_META['co2'])
    _v12_card(rect, T('gt_' + key), C.BORDER)
    x, y, w, h = rect
    hrs = int(float(db.get_setting('graph_hours', '24')))
    text_at(screen, f"{hrs} {T('hour_short')}", FNT_SMALL, C.ACCENT, x + w - 18, y + 10, 'tr')
    px, py, pw, ph = x + 64, y + 50, w - 90, h - 92
    _prevclip = screen.get_clip(); screen.set_clip(pygame.Rect(x + 2, y + 2, w - 4, h - 4))
    _plot_fixed(px, py, pw, ph, key, col, hrs, bars=True, line=True)
    screen.set_clip(_prevclip)

# ── Повноекранний графік: фіксоване вікно + проріджування ─────────────────────

def draw_chart(key: str):
    screen.fill(C.BG)
    _tt, default_max, col, digits = _GRAPH_META.get(key, (key, None, C.ACCENT, 1))
    hrs = float(db.get_setting('graph_hours', '24'))
    text_at(screen, f"{T('gt_'+key)} — {int(hrs)} {T('hour_short')}", FNT_BIG, col, C.W // 2, 14, 'tc')
    _back_btn()
    now, t0, rows = _window_series(key, hrs)
    if len(rows) < 2:
        text_at(screen, T('not_enough'), FNT_BIG, C.MUTED, C.W // 2, C.H // 2, 'mc')
        pygame.display.update(); return
    cx, cy = 92, 80
    cw, ch_ = C.W - cx - 28, C.H - cy - 64
    span = max(now - t0, 1.0)
    vals = [float(v) for _, v in rows if v is not None]
    lo, hi = _scale_lohi(key, default_max, vals)
    if abs(hi - lo) < 1e-6:
        hi = lo + 1

    def _x(ts):
        return cx + int((ts - t0) / span * cw)

    def _y(v):
        return cy + ch_ - int(max(0.0, min(1.0, (float(v) - lo) / (hi - lo))) * ch_)

    for i in range(5):
        t = i / 4
        yv = lo + (hi - lo) * t
        yp = cy + ch_ - int(ch_ * t)
        pygame.draw.line(screen, C.BORDER, (cx, yp), (cx + cw, yp), 1)
        text_at(screen, f'{yv:.0f}' if abs(yv) >= 10 else f'{yv:.1f}', FNT_TINY, C.MUTED, cx - 8, yp, 'mr')
    pygame.draw.line(screen, C.BORDER, (cx, cy + ch_), (cx + cw, cy + ch_), 1)
    pygame.draw.line(screen, C.BORDER, (cx, cy), (cx, cy + ch_), 1)

    if key not in ('temperature', 'pressure', 'humidity'):
        brows = _decimate(rows, max(40, cw // 8))
        bw = max(2, int(cw / max(len(brows), 1)) - 1)
        for ts, v in brows:
            bh = max(2, (cy + ch_) - _y(v))
            bcol = tuple(max(0, int(c * 0.55)) for c in _zone_col(key, v, col))
            pygame.draw.rect(screen, bcol, (_x(ts), cy + ch_ - bh, bw, bh), border_radius=2)
        th = _ZONE_THRESHOLDS.get(key)
        if th:
            for lim, lc in ((th[0], C.YELLOW), (th[1], C.RED)):
                if lo < lim < hi:
                    yy = _y(lim)
                    dim = tuple(int(c * 0.6) for c in lc)
                    dx = 0
                    while dx < cw - 3:
                        pygame.draw.line(screen, dim, (cx + dx, yy), (cx + min(dx + 6, cw), yy), 1)
                        dx += 13
                    text_at(screen, f'{lim:g}', FNT_TINY, lc, cx + cw - 6, yy - 2, 'br')

    lrows = _decimate(rows, max(60, cw // 3))
    pts = [(_x(ts), _y(v)) for ts, v in lrows]
    if len(pts) > 1:
        pygame.draw.lines(screen, col, False, pts, 3)

    avg = sum(vals) / len(vals)
    text_at(screen, f"Min:{min(vals):.{digits}f}   Max:{max(vals):.{digits}f}   Avg:{avg:.{digits}f}   n={len(vals)}",
            FNT_SMALL, C.TEXT2, C.W // 2, C.H - 10, 'bc')
    try:
        text_at(screen, datetime.fromtimestamp(t0).strftime('%d.%m %H:%M'), FNT_TINY, C.MUTED, cx, cy + ch_ + 6, 'tl')
        text_at(screen, datetime.fromtimestamp(now).strftime('%d.%m %H:%M'), FNT_TINY, C.MUTED, cx + cw, cy + ch_ + 6, 'tr')
    except Exception:
        pass
    pygame.display.update()

# ── Головний екран з авто-приховуванням панелі/напису ─────────────────────────

def draw_main():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    ah = _auto_hide_enabled()
    revealed = _ui_shown[0] or not ah
    sidebar_shown = revealed
    header_shown = _header_pref() and revealed

    left = 248 if sidebar_shown else 0

    if sidebar_shown:
        fill_rect(screen, C.SIDEBAR, (0, 0, left, C.H), radius=0)
        pygame.draw.line(screen, C.BORDER, (left, 0), (left, C.H), 1)
        text_at(screen, '≋ AIRSTATION', FNT_TITLE, C.WHITE, 22, 26, 'tl')
        nav = [('__main__', '⌂', T('nav_home')), ('__graphs__', '▥', T('nav_graphs')),
               ('__data__', '▦', T('nav_data')), ('__menu__', '⚙', T('nav_settings')),
               ('__about__', 'ⓘ', T('nav_about'))]
        yy = 94
        for key, ico, lab in nav:
            rect = pygame.Rect(14, yy, left - 28, 56)
            active = (key == '__main__')
            fill_rect(screen, C.ACCENT_D if active else C.SIDEBAR, rect, radius=12)
            if active:
                stroke_rect(screen, C.ACCENT, rect, 1, radius=12)
            text_at(screen, ico, FNT_MED, C.BLUE if active else C.TEXT2, 42, yy + 28, 'mc')
            text_at(screen, lab, FNT_SMALL, C.WHITE if active else C.TEXT2, 72, yy + 28, 'ml')
            MAIN_RECTS[key] = rect
            yy += 68
        exit_rect = pygame.Rect(20, C.H - 160, left - 40, 56)
        fill_rect(screen, (48, 14, 18), exit_rect, radius=12)
        stroke_rect(screen, C.RED, exit_rect, 2, radius=12)
        text_at(screen, '↪  ' + T('nav_exit'), FNT_SMALL, C.RED, exit_rect.centerx, exit_rect.centery, 'mc')
        MAIN_RECTS['__exit__'] = exit_rect
        now = datetime.now()
        text_at(screen, now.strftime('%H:%M:%S'), FNT_TINY, C.TEXT2, 24, C.H - 82, 'tl')
        text_at(screen, now.strftime('%d.%m.%Y'), FNT_TINY, C.TEXT2, 24, C.H - 54, 'tl')
        text_at(screen, 'Ver. 6.17', FNT_TINY, C.MUTED, left - 24, C.H - 54, 'tr')

    now = datetime.now()
    rx = left + 18
    rw = C.W - rx - 16

    if header_shown:
        text_at(screen, T('home_title'), FNT_TITLE, C.WHITE, rx, 14, 'tl')
        ok_any = any(S.REGISTRY[k].online for k in ['bmp280', 'scd41', 'sgp41', 'sps30'])
        st_lab = T('online') if ok_any else T('offline')
        st_col = C.GREEN if ok_any else C.RED
        r = text_at(screen, st_lab, FNT_SMALL, st_col, C.W - 64, 22, 'tr')
        pygame.draw.circle(screen, st_col, (r.left - 16, r.centery), 7)
        hb = text_at(screen, '☰', FNT_BIG, C.WHITE, C.W - 30, 32, 'mc')
        MAIN_RECTS['__hburger__'] = pygame.Rect(hb.left - 12, hb.top - 12, hb.width + 24, hb.height + 24)
        top = 58
    else:
        top = 14

    # Розкладка карток. Коли панель/напис приховані — картки ширші й вищі.
    status_h = 22
    env_h = 152
    metric_h = 168
    gap = 12
    y_env = top
    clockw = 330
    env = (rx, y_env, rw - clockw - 14, env_h)
    clockr = (rx + rw - clockw, y_env, clockw, env_h)
    _v12_env_card(env); _v12_clock(clockr)

    y2 = y_env + env_h + gap
    cardw = (rw - 2 * gap) // 3
    r1 = (rx, y2, cardw, metric_h)
    r2 = (rx + cardw + gap, y2, cardw, metric_h)
    r3 = (rx + 2 * (cardw + gap), y2, cardw, metric_h)
    _v12_metric(r1, 'CO₂', 'co2', 'ppm', C.GREEN, 2000)
    _v12_metric(r2, 'VOC Index', 'voc_index', 'idx', C.PURPLE, 500)
    _v12_metric(r3, 'NOx Index', 'nox_index', 'idx', C.ORANGE, 500)
    MAIN_RECTS['co2'] = pygame.Rect(r1); MAIN_RECTS['voc_index'] = pygame.Rect(r2)
    MAIN_RECTS['nox_index'] = pygame.Rect(r3)

    y3 = y2 + metric_h + gap
    pmw = 432
    pm_h = C.H - y3 - status_h - 8
    pmr = (rx, y3, pmw, pm_h)
    gr = (rx + pmw + 14, y3, rw - pmw - 14, pm_h)
    _v12_pm(pmr); _v12_graph(gr, db.get_setting('main_graph', 'co2'))
    for k in ['pm1_0', 'pm2_5', 'pm4_0', 'pm10']:
        MAIN_RECTS[k] = pygame.Rect(pmr)

    yb = C.H - 6
    r = text_at(screen, T('last_update'), FNT_TINY, C.MUTED, rx + 6, yb, 'bl')
    r = text_at(screen, now.strftime('%H:%M:%S'), FNT_TINY, C.TEXT2, r.right + 10, yb, 'bl')
    r = text_at(screen, T('poll_int'), FNT_TINY, C.MUTED, r.right + 40, yb, 'bl')
    r = text_at(screen, f"{db.get_setting('poll_sec', str(C.POLL_INTERVAL))} {T('sec')}", FNT_TINY, C.TEXT2, r.right + 10, yb, 'bl')
    r = text_at(screen, T('main_graph'), FNT_TINY, C.MUTED, r.right + 40, yb, 'bl')
    text_at(screen, _glabel(db.get_setting('main_graph', 'co2')), FNT_TINY, C.TEXT2, r.right + 10, yb, 'bl')

    # Коли панель прихована — тонкий «язичок» біля лівого краю (торкнутися/свайп)
    if not sidebar_shown:
        fill_rect(screen, C.ACCENT_D, (0, 0, 6, C.H), radius=0)
        tab_h = 96
        ty = C.H // 2 - tab_h // 2
        fill_rect(screen, C.ACCENT_D, (0, ty, 16, tab_h), radius=8)
        text_at(screen, '›', FNT_BIG, C.ACCENT, 8, C.H // 2, 'mc')

    pygame.display.update()

# ── Меню: маршрутизація вкладки "Вигляд" ──────────────────────────────────────

def draw_menu():
    global _menu_buttons_v14, _menu_buttons_v13, _menu_source_buttons
    _menu_buttons_v14 = []; _menu_buttons_v13 = []; _menu_source_buttons = []
    screen.fill(C.BG)
    _back_btn()
    text_at(screen, T('menu_title'), FNT_TITLE, C.WHITE, 176, 16, 'tl')
    text_at(screen, T('menu_sub'), FNT_TINY, C.MUTED, 176, 52, 'tl')
    tx = 150
    for key, _lab in TABS:
        rect = pygame.Rect(tx, 92, 150, 50)
        _menu_buttons_v14.append(('tab:' + key, rect))
        sel = _menu_tab[0] == key
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=10)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=10)
        text_at(screen, T('tab_' + key), FNT_SMALL, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')
        tx += 162
    if _menu_tab[0] == 'general':
        _draw_menu_general_v14()
    elif _menu_tab[0] == 'display':
        _draw_menu_display_v14()
    elif _menu_tab[0] == 'sensors':
        _draw_menu_sensors_v14()
    elif _menu_tab[0] == 'time':
        _draw_menu_time_v14()
    if _menu_msg[0]:
        text_at(screen, _menu_msg[0], FNT_SMALL, C.YELLOW, 150, C.H - 8, 'bl')
    pygame.display.update()
    return _menu_buttons_v14

def _seg_toggle(x, y, w, h, label_pairs, current, prefix):
    """Малює групу кнопок-перемикачів; повертає нічого, лише реєструє хіт-зони."""
    bw = w // len(label_pairs)
    for i, (val, lab) in enumerate(label_pairs):
        rect = pygame.Rect(x + i * bw, y, bw - 8, h)
        _menu_buttons_v14.append((prefix + ':' + val, rect))
        sel = current == val
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=9)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 2 if sel else 1, radius=9)
        text_at(screen, lab, FNT_SMALL, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')

def _draw_menu_display_v14():
    x, y = 150, 158
    # Авто-приховування + секунди
    _v12_card((x, y, 640, 150), T('card_autohide'), C.ACCENT)
    _seg_toggle(x + 24, y + 54, 240, 50,
                [('1', T('opt_on')), ('0', T('opt_off'))],
                db.get_setting('auto_hide', '1'), 'auto_hide')
    text_at(screen, T('card_hidesec'), FNT_SMALL, C.MUTED, x + 300, y + 40, 'tl')
    try:
        hs = int(float(db.get_setting('hide_sec', '10')))
    except Exception:
        hs = 10
    text_at(screen, str(hs), FNT_BIG, C.WHITE, x + 470, y + 58, 'mc')
    _v14_button('hide-', (x + 300, y + 60, 60, 44), C.ORANGE, '−')
    _v14_button('hide+', (x + 520, y + 60, 60, 44), C.GREEN, '+')

    # Верхній напис
    _v12_card((810, y, 340, 150), T('card_header'), C.CYAN)
    _seg_toggle(834, y + 54, 300, 50,
                [('1', T('opt_show')), ('0', T('opt_hide'))],
                db.get_setting('show_header', '1'), 'show_header')

    # Стиль графіка на головному
    y2 = y + 170
    _v12_card((x, y2, 640, 150), T('card_mgstyle'), C.PURPLE)
    _seg_toggle(x + 24, y2 + 54, 590, 56,
                [('bars', T('style_bars')), ('gauge', T('style_gauge'))],
                db.get_setting('main_graph_style', 'bars'), 'mgstyle')

    # Стиль боксів CO2/VOC/NOx
    _v12_card((810, y2, 340, 150), T('card_metricstyle'), C.GREEN)
    _seg_toggle(834, y2 + 54, 300, 56,
                [('bars', T('style_bars')), ('gradient', T('style_gradient'))],
                db.get_setting('metric_style', 'bars'), 'metricstyle')

    # Прев'ю градієнт-шкали
    y3 = y2 + 170
    _v12_card((x, y3, 1000, 120), None, C.BORDER)
    text_at(screen, 'CO₂', FNT_SMALL, C.TEXT2, x + 24, y3 + 16, 'tl')
    _grad_bar((x + 24, y3 + 54, 940, 24), 'co2', latest.get('co2'))

    text_at(screen, T('display_hint'), FNT_TINY, C.MUTED, x, y3 + 132, 'tl')
    _v14_button('Save', (C.W - 300, y + 50, 160, 56), C.GREEN, T('save'))

# ── Обробник кліків меню з новими опціями вигляду ─────────────────────────────

def menu_hit(pos, buttons):
    global state, chart_key
    if pygame.Rect(2, 2, 170, 72).collidepoint(pos):
        state = State.MAIN; return
    for label, rect in list(_menu_buttons_v14):
        if not pygame.Rect(rect).collidepoint(pos):
            continue
        # ── нові перемикачі вигляду ──
        if label.startswith('auto_hide:'):
            db.set_setting('auto_hide', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('show_header:'):
            db.set_setting('show_header', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('mgstyle:'):
            db.set_setting('main_graph_style', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('metricstyle:'):
            db.set_setting('metric_style', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label == 'hide-':
            try: hs = int(float(db.get_setting('hide_sec', '10')))
            except Exception: hs = 10
            db.set_setting('hide_sec', str(max(3, hs - 1))); return
        if label == 'hide+':
            try: hs = int(float(db.get_setting('hide_sec', '10')))
            except Exception: hs = 10
            db.set_setting('hide_sec', str(min(120, hs + 1))); return
        # ── існуючі ──
        if label.startswith('tab:'):
            _menu_tab[0] = label.split(':', 1)[1]; db.set_setting('menu_tab', _menu_tab[0]); return
        if label.startswith('lang:'):
            code = label.split(':', 1)[1]
            i18n.set_lang(code); db.set_setting('lang', code); _menu_msg[0] = T('saved'); return
        if label == 'Save':
            db.set_setting('i2c_bus', str(_menu_bus[0])); db.set_setting('poll_sec', str(_menu_poll[0]))
            db.set_setting('graph_hours', str(_menu_hours[0]))
            db.set_setting('temp_source', S.SOURCE_MAP.get('temperature', 'bmp280'))
            db.set_setting('lang', i18n.get_lang())
            db.set_setting('time_mode', _time_mode[0])
            db.set_setting('manual_hour', str(_manual_h[0])); db.set_setting('manual_min', str(_manual_m[0]))
            db.set_setting('manual_day', str(_manual_d[0])); db.set_setting('manual_month', str(_manual_mo[0]))
            db.set_setting('manual_year', str(_manual_y[0]))
            _menu_msg[0] = T('saved'); return
        if label == 'bus-': _menu_bus[0] = max(0, _menu_bus[0] - 1); return
        if label == 'bus+': _menu_bus[0] = min(9, _menu_bus[0] + 1); return
        if label == 'poll-': _menu_poll[0] = max(1, _menu_poll[0] - 1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label == 'poll+': _menu_poll[0] = min(3600, _menu_poll[0] + 1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label.startswith('graph:'): _menu_hours[0] = int(label.split(':')[1]); db.set_setting('graph_hours', str(_menu_hours[0])); return
        if label.startswith('main_graph:'): db.set_setting('main_graph', label.split(':', 1)[1]); return
        if label.startswith('temp_source:'):
            src = label.split(':', 1)[1]; S.SOURCE_MAP['temperature'] = src
            db.set_setting('temp_source', src); _menu_msg[0] = 'Temperature <- ' + src.upper(); return
        if label == 'Scan I2C': scan_result.clear(); state = State.I2CSCAN; return
        if label == 'Restart SCD41':
            def _do_scd():
                _menu_msg[0] = 'Restarting SCD41...'
                try:
                    S.REGISTRY['scd41'].online = False
                    ok = S.restart_scd41('manual menu')
                    _menu_msg[0] = 'SCD41 OK' if ok else 'SCD41 still OFF'
                except Exception as e: _menu_msg[0] = 'SCD err: ' + str(e)[:20]
            threading.Thread(target=_do_scd, daemon=True).start(); return
        if label == 'Restart SPS30':
            def _do_sps():
                _menu_msg[0] = 'Restarting SPS30...'
                try:
                    S.REGISTRY['sps30'].online = False
                    ok = S.restart_sps30(_menu_bus[0])
                    _menu_msg[0] = 'SPS30 OK' if ok else 'SPS30 OFF'
                except Exception as e: _menu_msg[0] = 'SPS err: ' + str(e)[:20]
            threading.Thread(target=_do_sps, daemon=True).start(); return
        if label == 'Purge DB': db.purge(7); _menu_msg[0] = 'DB purged'; return
        if label.startswith('time:'): _set_time_mode(label.split(':', 1)[1]); return
        if label in ['year-', 'year+', 'month-', 'month+', 'day-', 'day+', 'hour-', 'hour+', 'min-', 'min+']:
            fields = {'year': (_manual_y, 2020, 2099), 'month': (_manual_mo, 1, 12), 'day': (_manual_d, 1, 31),
                      'hour': (_manual_h, 0, 23), 'min': (_manual_m, 0, 59)}
            name = label[:-1]; op = label[-1]; ref, lo, hi = fields[name]
            ref[0] = max(lo, min(hi, ref[0] + (1 if op == '+' else -1))); return


# ══════════════════════════════════════════════════════════════════════════════
#  v18 — калібровка + одиниці + компенсація тиску, вибір показників у боксах
#        (з eCO₂/AQI/IAQ), метеостанція (адаптований дизайн старого проєкту),
#        BLE-заглушка, прогноз за трендом тиску, виправлення діапазону гейджа.
# ══════════════════════════════════════════════════════════════════════════════

# Пороги для похідних показників (для кольору/статусу гейджа й боксів)
_ZONE_THRESHOLDS.update({'aqi': (50, 100), 'iaq': (100, 200), 'eco2': (800, 1200)})

METRIC_ORDER = ['co2', 'voc_index', 'nox_index', 'eco2', 'aqi', 'iaq',
                'pm2_5', 'pm10', 'temperature', 'humidity']

# ── Калібровка та одиниці ─────────────────────────────────────────────────────

def _cal(key):
    try:
        return float(db.get_setting('cal_' + key, '0'))
    except Exception:
        return 0.0

def cget(key):
    """Калібрований показник (сирий + зсув). Не змінює збережені в БД дані."""
    v = latest.get(key)
    if v is None:
        return None
    try:
        return float(v) + _cal(key)
    except Exception:
        return None

def temp_unit():
    return wx.temp_unit_label(db.get_setting('temp_unit', 'c'))

def temp_disp(key='temperature'):
    v = cget(key)
    return wx.temp_convert(v, db.get_setting('temp_unit', 'c'))

def pressure_unit_lbl():
    return wx.pressure_unit_label(db.get_setting('pressure_unit', 'hpa'))

def pressure_disp():
    v = cget('pressure')
    if v is None:
        return None
    if db.get_setting('pressure_mode', 'abs') == 'sea':
        try:
            alt = float(db.get_setting('altitude_m', '520'))
        except Exception:
            alt = 520.0
        v = wx.sea_level_pressure(v, alt, cget('temperature'))
    return wx.pressure_convert(v, db.get_setting('pressure_unit', 'hpa'))

# Калібровка застосовується і до історії графіків
def _hist_values(key, n=40):
    off = _cal(key)
    vals = []
    try:
        for v in list(history.get(key, []))[-n:]:
            if v is not None:
                vals.append(float(v) + off)
    except Exception:
        vals = []
    if not vals:
        v = cget(key)
        if v is not None:
            vals = [float(v)]
    return vals

# ── Похідні показники ─────────────────────────────────────────────────────────

def derived_value(key):
    if key == 'aqi':
        return wx.aqi_from_pm(cget('pm2_5'), cget('pm10'))
    if key == 'iaq':
        return wx.iaq_index(cget('co2'), cget('voc_index'), cget('pm2_5'))
    if key == 'eco2':
        return wx.eco2_estimate(cget('voc_index'), cget('co2'))
    return cget(key)

def _metric_meta(key):
    m = {
        'co2':         ('CO₂', 'ppm', C.GREEN, 0),
        'voc_index':   ('VOC Index', 'idx', C.PURPLE, 0),
        'nox_index':   ('NOx Index', 'idx', C.ORANGE, 0),
        'eco2':        (T('lbl_eco2'), 'ppm', C.CYAN, 0),
        'aqi':         (T('lbl_aqi'), '', C.BLUE, 0),
        'iaq':         (T('lbl_iaq'), '', C.YELLOW, 0),
        'pm2_5':       ('PM2.5', 'µg/m³', C.GREEN, 1),
        'pm10':        ('PM10', 'µg/m³', C.ORANGE, 1),
        'temperature': (T('lbl_temperature'), '°C', C.CYAN, 1),
        'humidity':    (T('lbl_humidity'), '%', C.BLUE, 1),
    }
    return m.get(key)

def _metric_status(key, v):
    if v is None:
        return '—', C.MUTED
    th = _ZONE_THRESHOLDS.get(key)
    if not th:
        return '', C.TEXT2
    g, w = th
    if key == 'co2':
        if v <= g: return T('status_good'), C.GREEN
        if v <= w: return T('status_vent'), C.YELLOW
        return T('status_bad'), C.RED
    if v <= g: return T('status_good'), C.GREEN
    if v <= w: return T('status_mod'), C.YELLOW
    return T('status_bad'), C.RED

def _metric_box(rect, key):
    meta = _metric_meta(key)
    if not meta:
        return
    title, unit, color, dig = meta
    if key == 'temperature':
        v = temp_disp('temperature'); unit = temp_unit()
    elif key == 'humidity':
        v = cget('humidity')
    else:
        v = derived_value(key)
    _v12_card(rect, title, color)
    x, y, w, h = rect
    st, sc = _metric_status(key, v)
    if st:
        text_at(screen, st, FNT_SMALL, sc, x + w - 18, y + 10, 'tr')
    GUI.draw_num_unit(screen, _v12_fmt(v, dig), unit or '', x + 22, y + 42, 50, 21, C.WHITE, C.TEXT2, w - 44)
    if key in history:
        tr, tc = _v12_trend(key)
        if tr:
            text_at(screen, tr, FNT_TINY, tc, x + w - 18, y + 44, 'tr')
    style = db.get_setting('metric_style', 'bars')
    if key in history and style == 'bars':
        _v14_bars((x + 20, y + h - 56, w - 40, 42), key, color, max_val=None, n=26, min_visible=2)
    else:
        _grad_bar((x + 22, y + h - 40, w - 44, 20), key, v)

# ── Виправлений аналоговий гейдж (адаптивний діапазон для тиску тощо) ──────────

def _v12_gauge(rect, key='co2'):
    _t, maxv, col, digits = _GRAPH_META.get(key, _GRAPH_META['co2'])
    _v12_card(rect, T('gt_' + key), C.BORDER)
    x, y, w, h = rect
    th = _ZONE_THRESHOLDS.get(key)
    if key in ('aqi', 'iaq', 'eco2'):
        val = derived_value(key)
    else:
        val = cget(key)
    vals = _hist_values(key, 120)
    if th:
        gmin = 0.0
        gmax = float(maxv) if maxv else th[1] * 1.6
        zones = True
    else:
        # Немає порогів (тиск/темп/волога) → адаптивний діапазон з історії.
        if vals:
            mn, mx = min(vals), max(vals)
            pad = max((mx - mn) * 0.35, 3.0 if key == 'pressure' else 1.0)
            gmin, gmax = mn - pad, mx + pad
        else:
            gmin, gmax = (950.0, 1050.0) if key == 'pressure' else (0.0, 100.0)
        zones = False
    if abs(gmax - gmin) < 1e-6:
        gmax = gmin + 1

    cx = x + w // 2
    cy = y + h - 54
    R = max(60, int(min(w / 2 - 34, h - 96)))

    def pt(frac, rad):
        ang = math.radians(180 + 180 * max(0.0, min(1.0, frac)))
        return (cx + int(math.cos(ang) * rad), cy + int(math.sin(ang) * rad))

    if zones:
        gf = (th[0] - gmin) / (gmax - gmin)
        wf = (th[1] - gmin) / (gmax - gmin)
    seg = 60
    for i in range(seg):
        f0 = i / seg
        p1 = pt(f0, R); p2 = pt((i + 1) / seg, R)
        if zones:
            c = C.GREEN if f0 < gf else (C.YELLOW if f0 < wf else C.RED)
        else:
            c = wx_lerp((52, 211, 153), (96, 165, 250), f0)  # нейтральний зелено-синій
        pygame.draw.line(screen, c, p1, p2, 10)
    text_at(screen, f'{gmin:g}', FNT_TINY, C.MUTED, *pt(0.0, R + 16), 'mc')
    text_at(screen, f'{gmax:g}', FNT_TINY, C.MUTED, *pt(1.0, R + 16), 'mc')

    try:
        frac = max(0.0, min(1.0, (float(val) - gmin) / (gmax - gmin)))
    except Exception:
        frac = 0.0
    end = pt(frac, R - 14)
    pygame.draw.line(screen, C.WHITE, (cx, cy), end, 4)
    pygame.draw.circle(screen, C.WHITE, (cx, cy), 7)
    pygame.draw.circle(screen, col, (cx, cy), 4)

    # для тиску показуємо у вибраних одиницях
    if key == 'pressure':
        text_at(screen, _v12_fmt(pressure_disp(), wx.pressure_digits(db.get_setting('pressure_unit', 'hpa'))),
                FNT_HUGE, C.WHITE, cx, cy - int(R * 0.34), 'mc')
        text_at(screen, pressure_unit_lbl(), FNT_TINY, C.TEXT2, cx, cy - int(R * 0.34) + 34, 'mc')
    else:
        text_at(screen, _v12_fmt(val, 0), FNT_HUGE, C.WHITE, cx, cy - int(R * 0.34), 'mc')
        st, sc = _metric_status(key, val)
        if st:
            text_at(screen, st, FNT_SMALL, sc, cx, cy - int(R * 0.34) + 34, 'mc')
    hrs = int(float(db.get_setting('graph_hours', '24')))
    text_at(screen, f"{hrs} {T('hour_short')}", FNT_TINY, C.ACCENT, x + w - 16, y + 10, 'tr')

def wx_lerp(c0, c1, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))

# ── Картки середовища/тиску з одиницями та калібровкою ────────────────────────

def _v12_env_card(rect):
    _v12_card(rect, None, C.BORDER)
    x, y, w, h = rect
    colw = w // 3
    cols = [(T('lbl_temperature'), 'temperature', temp_unit(), C.CYAN, 1),
            (T('lbl_humidity'), 'humidity', '%', C.BLUE, 1)]
    for i, (lab, key, unit, col, dig) in enumerate(cols):
        xx = x + i * colw
        text_at(screen, lab, FNT_SMALL, C.TEXT2, xx + 18, y + 14, 'tl')
        tr, tc = _trend_short(key, 1)
        if tr:
            text_at(screen, tr, FNT_TINY, tc, xx + colw - 16, y + 18, 'tr')
        val = temp_disp('temperature') if key == 'temperature' else cget('humidity')
        GUI.draw_num_unit(screen, _v12_fmt(val, dig), unit, xx + 18, y + 42, 50, 21, C.WHITE, C.TEXT2, colw - 40)
        _v14_line((xx + 18, y + h - 36, colw - 36, 22), key, col, n=40)
        pygame.draw.line(screen, C.BORDER, (x + colw * (i + 1), y + 14), (x + colw * (i + 1), y + h - 14), 1)
    _v12_pressure((x + 2 * colw + 1, y, colw - 1, h))

def _v12_pressure(rect):
    x, y, w, h = rect
    val = pressure_disp()
    unit = pressure_unit_lbl()
    dig = wx.pressure_digits(db.get_setting('pressure_unit', 'hpa'))
    text_at(screen, T('pressure'), FNT_SMALL, C.TEXT2, x + 16, y + 14, 'tl')
    GUI.draw_num_unit(screen, _v12_fmt(val, dig), unit, x + 16, y + 46, 32, 16, C.WHITE, C.TEXT2, w - 96)
    tr, tc = _trend_short('pressure', 1)
    text_at(screen, tr if tr and tr != '→' else T('stable'), FNT_TINY, tc, x + 16, y + h - 34, 'tl')
    cx, cy = x + w - 62, y + h - 46
    rr = 44
    for a in range(205, 336, 8):
        rad = math.radians(a)
        p1 = (cx + int(math.cos(rad) * rr), cy + int(math.sin(rad) * rr))
        p2 = (cx + int(math.cos(rad) * (rr - 9)), cy + int(math.sin(rad) * (rr - 9)))
        pygame.draw.line(screen, C.PURPLE if a < 270 else C.BLUE, p1, p2, 2)
    # стрілка за станційним тиском у нормалізованому діапазоні 950..1050 hPa
    praw = cget('pressure')
    try:
        frac = max(0.0, min(1.0, (float(praw) - 950.0) / 100.0))
    except Exception:
        frac = 0.5
    ang = math.radians(205 + frac * 130)
    end = (cx + int(math.cos(ang) * (rr - 15)), cy + int(math.sin(ang) * (rr - 15)))
    pygame.draw.line(screen, C.WHITE, (cx, cy), end, 4)
    pygame.draw.circle(screen, C.WHITE, (cx, cy), 5)

# ── Спільна бокова панель ─────────────────────────────────────────────────────

def _sidebar(active_key):
    left = 248
    fill_rect(screen, C.SIDEBAR, (0, 0, left, C.H), radius=0)
    pygame.draw.line(screen, C.BORDER, (left, 0), (left, C.H), 1)
    text_at(screen, '≋ AIRSTATION', FNT_TITLE, C.WHITE, 22, 26, 'tl')
    nav = [('__main__', '⌂', T('nav_home')), ('__graphs__', '▥', T('nav_graphs')),
           ('__data__', '▦', T('nav_data')), ('__menu__', '⚙', T('nav_settings')),
           ('__about__', 'ⓘ', T('nav_about'))]
    yy = 94
    for key, ico, lab in nav:
        rect = pygame.Rect(14, yy, left - 28, 56)
        active = (key == active_key)
        fill_rect(screen, C.ACCENT_D if active else C.SIDEBAR, rect, radius=12)
        if active:
            stroke_rect(screen, C.ACCENT, rect, 1, radius=12)
        text_at(screen, ico, FNT_MED, C.BLUE if active else C.TEXT2, 42, yy + 28, 'mc')
        text_at(screen, lab, FNT_SMALL, C.WHITE if active else C.TEXT2, 72, yy + 28, 'ml')
        MAIN_RECTS[key] = rect
        yy += 68
    exit_rect = pygame.Rect(20, C.H - 160, left - 40, 56)
    fill_rect(screen, (48, 14, 18), exit_rect, radius=12)
    stroke_rect(screen, C.RED, exit_rect, 2, radius=12)
    text_at(screen, '↪  ' + T('nav_exit'), FNT_SMALL, C.RED, exit_rect.centerx, exit_rect.centery, 'mc')
    MAIN_RECTS['__exit__'] = exit_rect
    now = datetime.now()
    text_at(screen, now.strftime('%H:%M:%S'), FNT_TINY, C.TEXT2, 24, C.H - 82, 'tl')
    text_at(screen, now.strftime('%d.%m.%Y'), FNT_TINY, C.TEXT2, 24, C.H - 54, 'tl')
    _net_indicator(24, C.H - 30)
    text_at(screen, 'Ver. 6.22', FNT_TINY, C.MUTED, left - 24, C.H - 30, 'tr')
    return left

def _pull_tab():
    fill_rect(screen, C.ACCENT_D, (0, 0, 6, C.H), radius=0)
    th = 96; ty = C.H // 2 - th // 2
    fill_rect(screen, C.ACCENT_D, (0, ty, 16, th), radius=8)
    text_at(screen, '›', FNT_BIG, C.ACCENT, 8, C.H // 2, 'mc')

# ── Головний екран (air) з боксами за вибором ─────────────────────────────────

def draw_main():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    ah = _auto_hide_enabled()
    revealed = _ui_shown[0] or not ah
    sidebar_shown = revealed
    header_shown = _header_pref() and revealed
    left = _sidebar('__main__') if sidebar_shown else 0

    now = datetime.now()
    rx = left + 18
    rw = C.W - rx - 16

    if header_shown:
        text_at(screen, T('home_title'), FNT_TITLE, C.WHITE, rx, 14, 'tl')
        ok_any = any(S.REGISTRY[k].online for k in ['bmp280', 'scd41', 'sgp41', 'sps30'])
        st_col = C.GREEN if ok_any else C.RED
        r = text_at(screen, T('online') if ok_any else T('offline'), FNT_SMALL, st_col, C.W - 64, 22, 'tr')
        pygame.draw.circle(screen, st_col, (r.left - 16, r.centery), 7)
        hb = text_at(screen, '☰', FNT_BIG, C.WHITE, C.W - 30, 32, 'mc')
        MAIN_RECTS['__hburger__'] = pygame.Rect(hb.left - 12, hb.top - 12, hb.width + 24, hb.height + 24)
        top = 58
    else:
        top = 14

    status_h = 22; env_h = 152; metric_h = 168; gap = 12
    clockw = 330
    _v12_env_card((rx, top, rw - clockw - 14, env_h))
    _v12_clock((rx + rw - clockw, top, clockw, env_h))

    y2 = top + env_h + gap
    cardw = (rw - 2 * gap) // 3
    boxes = [db.get_setting('box1', 'co2'), db.get_setting('box2', 'voc_index'),
             db.get_setting('box3', 'nox_index')]
    for i, bkey in enumerate(boxes):
        rct = (rx + i * (cardw + gap), y2, cardw, metric_h)
        _metric_box(rct, bkey)
        MAIN_RECTS[bkey] = pygame.Rect(rct)

    y3 = y2 + metric_h + gap
    pmw = 432
    pm_h = C.H - y3 - status_h - 8
    pmr = (rx, y3, pmw, pm_h)
    gr = (rx + pmw + 14, y3, rw - pmw - 14, pm_h)
    _v12_pm(pmr); _v12_graph(gr, db.get_setting('main_graph', 'co2'))
    for k in ['pm1_0', 'pm2_5', 'pm4_0', 'pm10']:
        MAIN_RECTS[k] = pygame.Rect(pmr)

    yb = C.H - 6
    r = text_at(screen, T('last_update'), FNT_TINY, C.MUTED, rx + 6, yb, 'bl')
    r = text_at(screen, now.strftime('%H:%M:%S'), FNT_TINY, C.TEXT2, r.right + 10, yb, 'bl')
    r = text_at(screen, T('poll_int'), FNT_TINY, C.MUTED, r.right + 40, yb, 'bl')
    r = text_at(screen, f"{db.get_setting('poll_sec', str(C.POLL_INTERVAL))} {T('sec')}", FNT_TINY, C.TEXT2, r.right + 10, yb, 'bl')
    r = text_at(screen, T('main_graph'), FNT_TINY, C.MUTED, r.right + 40, yb, 'bl')
    text_at(screen, _glabel(db.get_setting('main_graph', 'co2')), FNT_TINY, C.TEXT2, r.right + 10, yb, 'bl')

    if not sidebar_shown:
        _pull_tab()
    pygame.display.update()

# ══════════════════════════════════════════════════════════════════════════════
#  Метеостанція (адаптація дизайну старого проєкту 800×480 на 1280×720)
# ══════════════════════════════════════════════════════════════════════════════

def _draw_moon(cx, cy, r, illum, waxing):
    pygame.draw.circle(screen, (18, 24, 40), (cx, cy), r + 3)
    pygame.draw.circle(screen, (60, 66, 84), (cx, cy), r)          # тіньова частина
    light = (232, 232, 214)
    for yy in range(-r, r + 1):
        hw = int(math.sqrt(max(0, r * r - yy * yy)))
        tx = int(hw * (2 * illum - 1))
        if waxing:
            x0, x1 = tx, hw
        else:
            x0, x1 = -hw, -tx
        if x1 > x0:
            pygame.draw.line(screen, light, (cx + x0, cy + yy), (cx + x1, cy + yy))
    pygame.draw.circle(screen, (90, 96, 120), (cx, cy), r, 1)

def _draw_wx_icon(cx, cy, s, code):
    sun = (250, 204, 21); cloud = (200, 212, 232); dark = (120, 132, 150)
    if code in ('sun', 'partly'):
        r = int(s * 0.5)
        ox, oy = (cx - int(s * 0.25), cy - int(s * 0.25)) if code == 'partly' else (cx, cy)
        for a in range(0, 360, 45):
            rad = math.radians(a)
            pygame.draw.line(screen, sun,
                             (ox + int(math.cos(rad) * r * 1.25), oy + int(math.sin(rad) * r * 1.25)),
                             (ox + int(math.cos(rad) * r * 1.7), oy + int(math.sin(rad) * r * 1.7)), 3)
        pygame.draw.circle(screen, sun, (ox, oy), r)
    if code in ('partly', 'cloud', 'rain', 'storm'):
        cyy = cy + int(s * 0.15)
        pygame.draw.circle(screen, cloud, (cx - int(s * 0.35), cyy), int(s * 0.32))
        pygame.draw.circle(screen, cloud, (cx + int(s * 0.15), cyy - int(s * 0.12)), int(s * 0.40))
        pygame.draw.circle(screen, cloud, (cx + int(s * 0.5), cyy), int(s * 0.30))
        pygame.draw.rect(screen, cloud, (cx - int(s * 0.6), cyy, int(s * 1.2), int(s * 0.35)), border_radius=8)
    if code in ('rain', 'storm'):
        for dx in (-0.35, 0.0, 0.35):
            x0 = cx + int(s * dx)
            pygame.draw.line(screen, C.BLUE, (x0, cy + int(s * 0.6)), (x0 - 6, cy + int(s * 0.85)), 3)
    if code == 'storm':
        pygame.draw.polygon(screen, sun, [(cx, cy + int(s * 0.55)), (cx - 10, cy + int(s * 0.85)),
                                          (cx, cy + int(s * 0.8)), (cx - 6, cy + int(s * 1.05))])

def _wx_forecast():
    hrs = 3
    try:
        rows = db.query('pressure', hrs) if not args.nodb else []
    except Exception:
        rows = []
    if not rows:
        vals = _hist_values('pressure', 200)
        now = time.time()
        try:
            step = max(1, int(float(db.get_setting('poll_sec', str(C.POLL_INTERVAL)))))
        except Exception:
            step = C.POLL_INTERVAL
        rows = [(now - (len(vals) - 1 - i) * step, v) for i, v in enumerate(vals)]
    return wx.forecast(rows)

def draw_weather():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    ah = _auto_hide_enabled()
    revealed = _ui_shown[0] or not ah
    sidebar_shown = revealed
    left = _sidebar('__main__') if sidebar_shown else 0
    rx = left + 18
    rw = C.W - rx - 16
    cxmid = rx + rw // 2
    now = datetime.now()

    # Гамбургер (доступ до меню, коли панель прихована свайпом ще не зроблено)
    hb = text_at(screen, '☰', FNT_BIG, C.WHITE, C.W - 30, 26, 'mc')
    MAIN_RECTS['__hburger__'] = pygame.Rect(hb.left - 14, hb.top - 14, hb.width + 28, hb.height + 28)

    # ── Місяць (угорі по центру) ──
    age, illum, idx = wx.moon_phase()
    waxing = age < wx.SYNODIC / 2
    mx, my = cxmid - 150, 88
    _draw_moon(mx, my, 46, illum, waxing)
    text_at(screen, wx.moon_name(idx, i18n.get_lang()), FNT_SMALL, C.TEXT2, mx, my + 62, 'mc')
    text_at(screen, f"{T('wx_moon')} {int(round(age)) + 1}", FNT_TINY, C.MUTED, mx, my + 84, 'mc')

    # ── Сонце: схід/захід ──
    try:
        tz = -time.timezone / 3600.0 + (1 if time.localtime().tm_isdst else 0)
    except Exception:
        tz = 2.0
    try:
        lat = float(db.get_setting('lat', '48.14')); lon = float(db.get_setting('lon', '11.68'))
    except Exception:
        lat, lon = 48.14, 11.68
    sr, ss = wx.sun_times(lat, lon, now, tz)
    sxx = cxmid + 120
    _draw_wx_icon(sxx, my, 42, 'sun')
    text_at(screen, f"{T('wx_sunrise')}  {sr.strftime('%H:%M') if sr else '--:--'}", FNT_SMALL, C.YELLOW, sxx + 60, my - 14, 'ml')
    text_at(screen, f"{T('wx_sunset')}  {ss.strftime('%H:%M') if ss else '--:--'}", FNT_SMALL, C.ORANGE, sxx + 60, my + 14, 'ml')

    # ── Великий годинник (центр) ──
    text_at(screen, now.strftime('%H:%M'), FNT_CLOCK, C.WHITE, cxmid, 168, 'mc')
    text_at(screen, f"{i18n.weekdays()[now.weekday()]}, {now.strftime('%d.%m.%Y')}",
            FNT_MED, C.TEXT2, cxmid, 268, 'mc')

    # ── Внутрішній блок (ліворуч) ──
    cardw = (rw - 24) // 2
    yb = 320
    inr = (rx, yb, cardw, 168)
    _v12_card(inr, T('wx_indoor'), C.CYAN)
    x, y, w, h = inr
    text_at(screen, T('lbl_temperature'), FNT_SMALL, C.MUTED, x + 20, y + 46, 'tl')
    r = text_at(screen, _v12_fmt(temp_disp('temperature'), 1), FNT_HUGE, C.WHITE, x + 20, y + 66, 'tl')
    text_at(screen, temp_unit(), FNT_SMALL, C.TEXT2, r.right + 8, r.bottom - 26, 'tl')
    text_at(screen, T('lbl_humidity'), FNT_SMALL, C.MUTED, x + w // 2 + 20, y + 46, 'tl')
    r = text_at(screen, _v12_fmt(cget('humidity'), 0), FNT_HUGE, C.WHITE, x + w // 2 + 20, y + 66, 'tl')
    text_at(screen, '%', FNT_SMALL, C.TEXT2, r.right + 8, r.bottom - 26, 'tl')

    # ── Зовнішній блок (BLE, праворуч) — заглушка ──
    outr = (rx + cardw + 24, yb, cardw, 168)
    ble_on = db.get_setting('ble_enabled', '0') == '1'  # поки що завжди off (заглушка)
    _v12_card(outr, T('wx_outdoor'), C.BORDER if not ble_on else C.GREEN)
    x, y, w, h = outr
    if not ble_on:
        # Якщо в налаштуваннях увімкнено підстановку — показуємо CO₂ замість BLE
        if db.get_setting('outdoor_fallback', '0') == '1':
            text_at(screen, 'CO₂', FNT_SMALL, C.MUTED, x + 20, y + 46, 'tl')
            r = text_at(screen, _v12_fmt(cget('co2'), 0), FNT_HUGE, C.WHITE, x + 20, y + 66, 'tl')
            text_at(screen, 'ppm', FNT_SMALL, C.TEXT2, r.right + 8, r.bottom - 26, 'tl')
        else:
            text_at(screen, '⃠', FNT_HUGE, C.MUTED, x + w // 2, y + h // 2 - 16, 'mc')
            text_at(screen, T('wx_ble_wait'), FNT_SMALL, C.MUTED, x + w // 2, y + h // 2 + 30, 'mc')
        pygame.draw.circle(screen, C.MUTED, (x + w - 30, y + 24), 7)

    # ── Прогноз + тиск (низ) ──
    fc = _wx_forecast()
    fr = (rx, yb + 184, cardw, C.H - (yb + 184) - 34)
    _v12_card(fr, T('wx_forecast'), C.PURPLE)
    x, y, w, h = fr
    _draw_wx_icon(x + 74, y + h // 2 + 6, 56, fc['icon'])
    txt = fc['text_uk'] if i18n.get_lang() == 'uk' else fc['text_en']
    # перенос тексту у два рядки
    words = txt.split(' ')
    line1, line2 = '', ''
    for wd in words:
        if len(line1) < 22:
            line1 += (' ' if line1 else '') + wd
        else:
            line2 += (' ' if line2 else '') + wd
    text_at(screen, line1, FNT_SMALL, C.TEXT2, x + 150, y + h // 2 - 16, 'ml')
    if line2:
        text_at(screen, line2, FNT_SMALL, C.TEXT2, x + 150, y + h // 2 + 12, 'ml')
    text_at(screen, f"Δ {fc['rate']:+.1f} hPa/h", FNT_TINY, C.MUTED, x + 150, y + h - 24, 'ml')

    pr = (rx + cardw + 24, yb + 184, cardw, C.H - (yb + 184) - 34)
    _v12_card(pr, T('wx_pressure'), C.BLUE)
    x, y, w, h = pr
    dig = wx.pressure_digits(db.get_setting('pressure_unit', 'hpa'))
    r = text_at(screen, _v12_fmt(pressure_disp(), dig), FNT_HUGE, C.WHITE, x + 24, y + h // 2 - 24, 'tl')
    text_at(screen, pressure_unit_lbl(), FNT_SMALL, C.TEXT2, r.right + 10, r.bottom - 26, 'tl')
    tr, tc = _trend_short('pressure', 1)
    text_at(screen, tr if tr and tr != '→' else T('stable'), FNT_SMALL, tc, x + 24, y + h - 34, 'tl')
    _v14_line((x + w - 200, y + 40, 180, h - 70), 'pressure', C.BLUE, n=60)

    if not sidebar_shown:
        _pull_tab()
    pygame.display.update()

# ══════════════════════════════════════════════════════════════════════════════
#  Меню: вкладка "Вигляд" (+режим екрана, +бокси) і "Калібр."
# ══════════════════════════════════════════════════════════════════════════════

def draw_menu():
    global _menu_buttons_v14, _menu_buttons_v13, _menu_source_buttons
    _menu_buttons_v14 = []; _menu_buttons_v13 = []; _menu_source_buttons = []
    screen.fill(C.BG)
    _back_btn()
    text_at(screen, T('menu_title'), FNT_TITLE, C.WHITE, 176, 16, 'tl')
    text_at(screen, T('menu_sub'), FNT_TINY, C.MUTED, 176, 52, 'tl')
    tx = 150
    for key, _lab in TABS:
        rect = pygame.Rect(tx, 92, 148, 50)
        _menu_buttons_v14.append(('tab:' + key, rect))
        sel = _menu_tab[0] == key
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=10)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=10)
        text_at(screen, T('tab_' + key), FNT_SMALL, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')
        tx += 158
    tab = _menu_tab[0]
    if tab == 'general':
        _draw_menu_general_v14()
    elif tab == 'display':
        _draw_menu_display_v14()
    elif tab == 'calib':
        _draw_menu_calib_v14()
    elif tab == 'sensors':
        _draw_menu_sensors_v14()
    elif tab == 'time':
        _draw_menu_time_v14()
    if _menu_msg[0]:
        text_at(screen, _menu_msg[0], FNT_SMALL, C.YELLOW, 150, C.H - 8, 'bl')
    pygame.display.update()
    return _menu_buttons_v14

def _draw_menu_display_v14():
    x, y = 150, 158
    _v12_card((x, y, 600, 148), T('card_autohide'), C.ACCENT)
    _seg_toggle(x + 24, y + 54, 220, 50, [('1', T('opt_on')), ('0', T('opt_off'))],
                db.get_setting('auto_hide', '1'), 'auto_hide')
    text_at(screen, T('card_hidesec'), FNT_TINY, C.MUTED, x + 280, y + 44, 'tl')
    try: hs = int(float(db.get_setting('hide_sec', '10')))
    except Exception: hs = 10
    text_at(screen, str(hs), FNT_BIG, C.WHITE, x + 452, y + 58, 'mc')
    _v14_button('hide-', (x + 288, y + 60, 56, 44), C.ORANGE, '−')
    _v14_button('hide+', (x + 500, y + 60, 56, 44), C.GREEN, '+')

    _v12_card((770, y, 380, 148), T('card_header'), C.CYAN)
    _seg_toggle(794, y + 54, 330, 50, [('1', T('opt_show')), ('0', T('opt_hide'))],
                db.get_setting('show_header', '1'), 'show_header')

    y2 = y + 166
    _v12_card((x, y2, 600, 148), T('card_mgstyle'), C.PURPLE)
    _seg_toggle(x + 24, y2 + 54, 550, 54, [('bars', T('style_bars')), ('gauge', T('style_gauge'))],
                db.get_setting('main_graph_style', 'bars'), 'mgstyle')

    _v12_card((770, y2, 380, 148), T('card_metricstyle'), C.GREEN)
    _seg_toggle(794, y2 + 54, 330, 54, [('bars', T('style_bars')), ('gradient', T('style_gradient'))],
                db.get_setting('metric_style', 'bars'), 'metricstyle')

    y3 = y2 + 166
    _v12_card((x, y3, 380, 148), T('card_screen'), C.YELLOW)
    _seg_toggle(x + 24, y3 + 54, 330, 54, [('air', T('screen_air')), ('wx', T('screen_wx'))],
                db.get_setting('screen_mode', 'air'), 'screen')

    _v12_card((550, y3, 600, 148), T('card_boxes'), C.BLUE)
    labels = {'co2': 'CO₂', 'voc_index': 'VOC', 'nox_index': 'NOx', 'eco2': 'eCO₂',
              'aqi': 'AQI', 'iaq': 'IAQ', 'pm2_5': 'PM2.5', 'pm10': 'PM10',
              'temperature': 'Темп', 'humidity': 'Вол'}
    for i, setkey in enumerate(['box1', 'box2', 'box3']):
        cur = db.get_setting(setkey, ['co2', 'voc_index', 'nox_index'][i])
        bx = 574 + i * 186
        rect = pygame.Rect(bx, y3 + 58, 172, 60)
        _menu_buttons_v14.append(('boxcycle:' + str(i + 1), rect))
        fill_rect(screen, C.PANEL2, rect, radius=9)
        stroke_rect(screen, C.BORDER, rect, 1, radius=9)
        text_at(screen, f'{i + 1}', FNT_TINY, C.MUTED, bx + 10, y3 + 64, 'tl')
        text_at(screen, labels.get(cur, cur), FNT_MED, C.WHITE, rect.centerx + 6, rect.centery + 4, 'mc')
        text_at(screen, '›', FNT_SMALL, C.ACCENT, bx + 158, y3 + 64, 'tr')
    text_at(screen, T('boxes_hint'), FNT_TINY, C.MUTED, 550, y3 + 128, 'tl')

    _v14_button('Save', (C.W - 168, y + 46, 150, 54), C.GREEN, T('save'))

def _draw_menu_calib_v14():
    x, y = 150, 158
    _v12_card((x, y, 640, 300), T('card_calib'), C.ACCENT)
    rows = [('cal_temperature', T('cal_temp'), '°C', 0.1, 1),
            ('cal_humidity', T('cal_hum'), '%', 1, 0),
            ('cal_pressure', T('cal_pres'), 'hPa', 0.1, 1),
            ('cal_co2', T('cal_co2'), 'ppm', 5, 0),
            ('cal_voc_index', T('cal_voc'), 'idx', 1, 0),
            ('cal_nox_index', T('cal_nox'), 'idx', 1, 0)]
    yy = y + 48
    for setk, lab, unit, step, dig in rows:
        try: cur = float(db.get_setting(setk, '0'))
        except Exception: cur = 0.0
        text_at(screen, lab, FNT_SMALL, C.TEXT2, x + 20, yy + 10, 'tl')
        text_at(screen, f'{cur:+.{dig}f} {unit}', FNT_SMALL, C.WHITE, x + 250, yy + 10, 'tl')
        _v14_button(setk + ':-', (x + 430, yy, 56, 38), C.ORANGE, '−')
        _v14_button(setk + ':+', (x + 500, yy, 56, 38), C.GREEN, '+')
        yy += 42

    _v12_card((810, y, 340, 145), T('card_units'), C.CYAN)
    text_at(screen, T('unit_temp'), FNT_TINY, C.MUTED, 834, y + 42, 'tl')
    _seg_toggle(834, y + 62, 300, 32, [('c', '°C'), ('f', '°F')], db.get_setting('temp_unit', 'c'), 'tempunit')
    text_at(screen, T('unit_pres'), FNT_TINY, C.MUTED, 834, y + 100, 'tl')
    _seg_toggle(834, y + 120, 300, 32, [('hpa', 'hPa'), ('mmhg', 'мм'), ('kpa', 'kPa')],
                db.get_setting('pressure_unit', 'hpa'), 'presunit')

    _v12_card((810, y + 158, 340, 142), T('card_presmode'), C.PURPLE)
    _seg_toggle(834, y + 200, 300, 40, [('abs', T('pres_abs')), ('sea', T('pres_sea'))],
                db.get_setting('pressure_mode', 'abs'), 'presmode')
    try: alt = int(float(db.get_setting('altitude_m', '520')))
    except Exception: alt = 520
    text_at(screen, f"{T('altitude')}: {alt}", FNT_SMALL, C.TEXT2, 834, y + 252, 'tl')
    _v14_button('alt-', (1010, y + 248, 56, 38), C.ORANGE, '−')
    _v14_button('alt+', (1080, y + 248, 56, 38), C.GREEN, '+')

    _v14_button('Save', (x, y + 320, 200, 50), C.GREEN, T('save'))

# ── Обробник кліків меню (додано калібровку/одиниці/режим/бокси) ──────────────

def menu_hit(pos, buttons):
    global state, chart_key
    if pygame.Rect(2, 2, 170, 72).collidepoint(pos):
        state = State.MAIN; return
    for label, rect in list(_menu_buttons_v14):
        if not pygame.Rect(rect).collidepoint(pos):
            continue
        # калібровка
        if label.startswith('cal_') and (label.endswith(':-') or label.endswith(':+')):
            setk = label[:-2]; op = label[-1]
            step = {'cal_temperature': 0.1, 'cal_humidity': 1, 'cal_pressure': 0.1,
                    'cal_co2': 5, 'cal_voc_index': 1, 'cal_nox_index': 1}.get(setk, 1)
            try: cur = float(db.get_setting(setk, '0'))
            except Exception: cur = 0.0
            cur += step if op == '+' else -step
            db.set_setting(setk, f'{cur:.2f}'); return
        if label.startswith('tempunit:'): db.set_setting('temp_unit', label.split(':', 1)[1]); return
        if label.startswith('presunit:'): db.set_setting('pressure_unit', label.split(':', 1)[1]); return
        if label.startswith('presmode:'): db.set_setting('pressure_mode', label.split(':', 1)[1]); return
        if label == 'alt-':
            try: a = int(float(db.get_setting('altitude_m', '520')))
            except Exception: a = 520
            db.set_setting('altitude_m', str(max(0, a - 10))); return
        if label == 'alt+':
            try: a = int(float(db.get_setting('altitude_m', '520')))
            except Exception: a = 520
            db.set_setting('altitude_m', str(min(4000, a + 10))); return
        if label.startswith('screen:'): db.set_setting('screen_mode', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('boxcycle:'):
            i = int(label.split(':', 1)[1]); setk = 'box' + str(i)
            cur = db.get_setting(setk, ['co2', 'voc_index', 'nox_index'][i - 1])
            try: nxt = METRIC_ORDER[(METRIC_ORDER.index(cur) + 1) % len(METRIC_ORDER)]
            except ValueError: nxt = METRIC_ORDER[0]
            db.set_setting(setk, nxt); return
        # вигляд
        if label.startswith('auto_hide:'): db.set_setting('auto_hide', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('show_header:'): db.set_setting('show_header', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('mgstyle:'): db.set_setting('main_graph_style', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('metricstyle:'): db.set_setting('metric_style', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label == 'hide-':
            try: hs = int(float(db.get_setting('hide_sec', '10')))
            except Exception: hs = 10
            db.set_setting('hide_sec', str(max(3, hs - 1))); return
        if label == 'hide+':
            try: hs = int(float(db.get_setting('hide_sec', '10')))
            except Exception: hs = 10
            db.set_setting('hide_sec', str(min(120, hs + 1))); return
        # існуючі
        if label.startswith('tab:'):
            _menu_tab[0] = label.split(':', 1)[1]; db.set_setting('menu_tab', _menu_tab[0]); return
        if label.startswith('lang:'):
            i18n.set_lang(label.split(':', 1)[1]); db.set_setting('lang', i18n.get_lang()); _menu_msg[0] = T('saved'); return
        if label == 'Save':
            db.set_setting('i2c_bus', str(_menu_bus[0])); db.set_setting('poll_sec', str(_menu_poll[0]))
            db.set_setting('graph_hours', str(_menu_hours[0]))
            db.set_setting('temp_source', S.SOURCE_MAP.get('temperature', 'bmp280'))
            db.set_setting('lang', i18n.get_lang()); db.set_setting('time_mode', _time_mode[0])
            db.set_setting('manual_hour', str(_manual_h[0])); db.set_setting('manual_min', str(_manual_m[0]))
            db.set_setting('manual_day', str(_manual_d[0])); db.set_setting('manual_month', str(_manual_mo[0]))
            db.set_setting('manual_year', str(_manual_y[0]))
            _menu_msg[0] = T('saved'); return
        if label == 'bus-': _menu_bus[0] = max(0, _menu_bus[0] - 1); return
        if label == 'bus+': _menu_bus[0] = min(9, _menu_bus[0] + 1); return
        if label == 'poll-': _menu_poll[0] = max(1, _menu_poll[0] - 1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label == 'poll+': _menu_poll[0] = min(3600, _menu_poll[0] + 1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label.startswith('graph:'): _menu_hours[0] = int(label.split(':')[1]); db.set_setting('graph_hours', str(_menu_hours[0])); return
        if label.startswith('main_graph:'): db.set_setting('main_graph', label.split(':', 1)[1]); return
        if label.startswith('temp_source:'):
            src = label.split(':', 1)[1]; S.SOURCE_MAP['temperature'] = src
            db.set_setting('temp_source', src); _menu_msg[0] = 'Temperature <- ' + src.upper(); return
        if label == 'Scan I2C': scan_result.clear(); state = State.I2CSCAN; return
        if label == 'Restart SCD41':
            def _do_scd():
                _menu_msg[0] = 'Restarting SCD41...'
                try:
                    S.REGISTRY['scd41'].online = False
                    ok = S.restart_scd41('manual menu')
                    _menu_msg[0] = 'SCD41 OK' if ok else 'SCD41 still OFF'
                except Exception as e: _menu_msg[0] = 'SCD err: ' + str(e)[:20]
            threading.Thread(target=_do_scd, daemon=True).start(); return
        if label == 'Restart SPS30':
            def _do_sps():
                _menu_msg[0] = 'Restarting SPS30...'
                try:
                    S.REGISTRY['sps30'].online = False
                    ok = S.restart_sps30(_menu_bus[0])
                    _menu_msg[0] = 'SPS30 OK' if ok else 'SPS30 OFF'
                except Exception as e: _menu_msg[0] = 'SPS err: ' + str(e)[:20]
            threading.Thread(target=_do_sps, daemon=True).start(); return
        if label == 'Purge DB': db.purge(7); _menu_msg[0] = 'DB purged'; return
        if label.startswith('time:'): _set_time_mode(label.split(':', 1)[1]); return
        if label in ['year-', 'year+', 'month-', 'month+', 'day-', 'day+', 'hour-', 'hour+', 'min-', 'min+']:
            fields = {'year': (_manual_y, 2020, 2099), 'month': (_manual_mo, 1, 12), 'day': (_manual_d, 1, 31),
                      'hour': (_manual_h, 0, 23), 'min': (_manual_m, 0, 59)}
            name = label[:-1]; op = label[-1]; ref, lo, hi = fields[name]
            ref[0] = max(lo, min(hi, ref[0] + (1 if op == '+' else -1))); return


# ══════════════════════════════════════════════════════════════════════════════
#  v19 — три метео-дизайни (1/2/4) з перелистуванням, інтернет-індикатор і
#        позначки інтернет-даних, конфігуровані бокси/панелі, довідка з прокруткою.
# ══════════════════════════════════════════════════════════════════════════════

SCREENS = ['air', 'wx1', 'wx2', 'wx4', 'wx']
_SCREEN_NAMES = {'air': 'Станція повітря', 'wx1': 'Метео 1', 'wx2': 'Прогноз (Метео 2)',
                 'wx4': 'Метео 4', 'wx': 'Метео класик'}

def _page_screen(delta):
    sid = db.get_setting('screen_id', 'air')
    try: i = SCREENS.index(sid)
    except ValueError: i = 0
    db.set_setting('screen_id', SCREENS[(i + delta) % len(SCREENS)])
    _ui_last_touch[0] = time.time()

def _draw_current_screen():
    sid = db.get_setting('screen_id', 'air')
    {'air': draw_main, 'wx1': draw_wx1, 'wx2': draw_wx2, 'wx4': draw_wx4,
     'wx': draw_weather}.get(sid, draw_main)()

# ── Довільні шрифти (з курсивом) ──────────────────────────────────────────────
_WF = {}
def _wf(size, bold=False, italic=False):
    k = (size, bold, italic)
    if k not in _WF:
        got = None
        for name in ['dejavusans', 'freesans', 'liberationsans', 'arial', '']:
            try:
                f = pygame.font.SysFont(name, size, bold=bold, italic=italic)
                if f: got = f; break
            except Exception: pass
        _WF[k] = got or pygame.font.Font(None, size + 4)
    return _WF[k]

def _t(string, font, col, x, y, a='tl'):
    return text_at(screen, string, font, col, x, y, a)

# ── Інтернет: індикатор і позначка ────────────────────────────────────────────
def _net_online():
    try: return net.is_online()
    except Exception: return False

def _net_mark(x, y, r=6):
    """Малесенька «глобус»-позначка біля даних з інтернету (за налаштуванням)."""
    if db.get_setting('show_net_mark', '1') != '1':
        return
    pygame.draw.circle(screen, C.BLUE, (x, y), r, 1)
    pygame.draw.line(screen, C.BLUE, (x - r, y), (x + r, y), 1)
    pygame.draw.line(screen, C.BLUE, (x, y - r), (x, y + r), 1)
    pygame.draw.arc(screen, C.BLUE, (x - r + 2, y - r, 2 * (r - 2), 2 * r), math.radians(90), math.radians(270), 1)

def _net_indicator(x, y):
    on = _net_online()
    col = C.GREEN if on else C.MUTED
    for rr in (5, 9, 13):
        pygame.draw.arc(screen, col, (x - rr, y - rr, rr * 2, rr * 2), math.radians(225), math.radians(315), 2)
    pygame.draw.circle(screen, col, (x, y + 5), 2)
    text_at(screen, 'online' if on else 'offline', FNT_TINY, col, x + 22, y, 'ml')

# ── Дрібні векторні іконки для метео-екранів ──────────────────────────────────
def _ic_thermo(cx, cy, col=C.CYAN):
    pygame.draw.rect(screen, col, (cx - 5, cy - 22, 10, 30), border_radius=5, width=3)
    pygame.draw.circle(screen, col, (cx, cy + 14), 10); pygame.draw.circle(screen, C.RED, (cx, cy + 14), 5)
    pygame.draw.line(screen, C.RED, (cx, cy + 14), (cx, cy - 8), 4)
def _ic_drop(cx, cy, col=C.BLUE):
    pygame.draw.polygon(screen, col, [(cx, cy - 22), (cx + 15, cy + 8), (cx - 15, cy + 8)])
    pygame.draw.circle(screen, col, (cx, cy + 8), 15); pygame.draw.circle(screen, (150, 200, 255), (cx - 4, cy + 6), 4)
def _ic_gauge(cx, cy, col=C.PURPLE):
    pygame.draw.arc(screen, col, (cx - 20, cy - 16, 40, 40), math.radians(20), math.radians(160), 4)
    pygame.draw.line(screen, C.WHITE, (cx, cy + 4), (cx + 10, cy - 10), 3); pygame.draw.circle(screen, C.WHITE, (cx, cy + 4), 3)
def _ic_co2(cx, cy, col=C.GREEN):
    pygame.draw.circle(screen, col, (cx - 10, cy), 11); pygame.draw.circle(screen, col, (cx + 6, cy - 6), 14)
    pygame.draw.circle(screen, col, (cx + 16, cy), 10); pygame.draw.rect(screen, col, (cx - 14, cy, 34, 12), border_radius=6)
    screen.blit(_wf(12, True).render('CO₂', True, (10, 20, 15)), (cx - 12, cy - 6))
def _ic_wind(cx, cy, col=C.TEXT2):
    for i, yy in enumerate((-8, 0, 8)):
        pygame.draw.line(screen, col, (cx - 16, cy + yy), (cx + 6 + i * 2, cy + yy), 3)
        pygame.draw.circle(screen, col, (cx + 8 + i * 2, cy + yy), 4, 2)
def _ic_pin(cx, cy, col=C.RED):
    pygame.draw.circle(screen, col, (cx, cy - 4), 8)
    pygame.draw.polygon(screen, col, [(cx - 8, cy - 1), (cx + 8, cy - 1), (cx, cy + 12)])
    pygame.draw.circle(screen, C.WHITE, (cx, cy - 4), 3)
def _sun_icon(cx, cy, r, col=C.YELLOW):
    for a in range(0, 360, 45):
        rad = math.radians(a)
        pygame.draw.line(screen, col, (cx + int(math.cos(rad) * r * 1.3), cy + int(math.sin(rad) * r * 1.3)),
                         (cx + int(math.cos(rad) * r * 1.75), cy + int(math.sin(rad) * r * 1.75)), 3)
    pygame.draw.circle(screen, col, (cx, cy), r)
def _sunrise_icon(cx, cy, up=True, col=C.YELLOW):
    for a in range(200, 341, 35):
        rad = math.radians(a)
        pygame.draw.line(screen, col, (cx + int(math.cos(rad) * 16), cy + int(math.sin(rad) * 16)),
                         (cx + int(math.cos(rad) * 24), cy + int(math.sin(rad) * 24)), 3)
    pygame.draw.circle(screen, col, (cx, cy), 12)
    pygame.draw.line(screen, C.TEXT2, (cx - 26, cy + 8), (cx + 26, cy + 8), 3)
    ay = cy - 22 if up else cy + 22; d = -1 if up else 1
    pygame.draw.line(screen, col, (cx, cy - 2 * d), (cx, ay), 3)
    pygame.draw.polygon(screen, col, [(cx, ay), (cx - 5, ay + 6 * d), (cx + 5, ay + 6 * d)])

def _grad_bar_frac(rect, frac):
    x, y, w, h = rect
    for i in range(w):
        pygame.draw.line(screen, _grad_color(i / max(w - 1, 1)), (x + i, y), (x + i, y + h))
    stroke_rect(screen, C.BORDER, rect, 1, radius=6)
    mx = x + int(w * max(0, min(1, frac)))
    pygame.draw.polygon(screen, C.PURPLE, [(mx, y + h + 2), (mx - 7, y + h + 14), (mx + 7, y + h + 14)])

def _uv_badge(rect, uv, label, est=False):
    x, y, w, h = rect
    for lo, hi, col in [(0, 2, C.GREEN), (3, 5, C.YELLOW), (6, 7, C.ORANGE), (8, 10, C.RED), (11, 13, C.PURPLE)]:
        bx = x + 18; bw = w - 36
        pygame.draw.rect(screen, col, (bx + int(bw * lo / 13), y + h - 30, int(bw * (hi + 1) / 13) - int(bw * lo / 13) - 3, 14), border_radius=4)
    if uv is not None:
        px = x + 18 + int((w - 36) * (uv + 0.5) / 13)
        pygame.draw.polygon(screen, C.WHITE, [(px, y + h - 36), (px - 6, y + h - 46), (px + 6, y + h - 46)])
    text_at(screen, 'УФ' if i18n.get_lang() == 'uk' else 'UV', FNT_SMALL, C.MUTED, x + 22, y + 16, 'tl')
    text_at(screen, ('—' if uv is None else str(uv)) + ('~' if est else ''), _wf(48, True), C.WHITE, x + 22, y + 40, 'tl')
    text_at(screen, label, FNT_SMALL, C.TEXT2, x + w - 18, y + 34, 'tr')

# ── Спільні метео-дані ────────────────────────────────────────────────────────
def _wx_tz():
    try: return -time.timezone / 3600.0 + (1 if time.localtime().tm_isdst else 0)
    except Exception: return 2.0
def _wx_latlon():
    try: return float(db.get_setting('lat', '48.14')), float(db.get_setting('lon', '11.68'))
    except Exception: return 48.14, 11.68
def _wx_sun():
    lat, lon = _wx_latlon(); return wx.sun_times(lat, lon, datetime.now(), _wx_tz())
def _wx_uv():
    """(uv, is_estimate). З інтернету якщо є, інакше офлайн-оцінка."""
    d = net.get()
    if d.get('uv') is not None:
        return int(round(d['uv'])), False
    sr, ss = _wx_sun()
    return wx.uv_estimate(datetime.now(), sr, ss), True
def _wx_wind():
    d = net.get(); return d.get('wind_kmh')
def _pdig():
    return wx.pressure_digits(db.get_setting('pressure_unit', 'hpa'))

def _wx_overlay(sidebar_shown):
    if sidebar_shown:
        _sidebar('__main__')
    else:
        _pull_tab()
    # маркер поточного дизайну + інтернет-індикатор (правий низ)
    _net_indicator(C.W - 120, C.H - 16)


# ── Графік тиску 24 год + тренд по 3 год (для дизайнів 1 і 4) ──────────────────
def _wx_pgraph(rect):
    x, y, w, h = rect
    _v12_card(rect, 'Тиск за 24 години' if i18n.get_lang() == 'uk' else 'Pressure 24h', C.BLUE)
    dig = _pdig()
    text_at(screen, f"{_v12_fmt(pressure_disp(), dig)} {pressure_unit_lbl()}  ·  Δ −2.0 / 3год",
            FNT_TINY, C.TEXT2, x + w - 18, y + 14, 'tr')
    gx, gy, gw, gh = x + 64, y + 52, w - 90, h - 150
    now, t0, rows = _window_series('pressure', 24)
    vals = [v for _, v in rows]
    if vals:
        lo, hi = min(vals) - 2, max(vals) + 2
    else:
        lo, hi = 996, 1012
    if hi - lo < 1: hi = lo + 1
    for i in range(5):
        yy = gy + gh - int(gh * i / 4)
        text_at(screen, f'{lo + (hi - lo) * i / 4:.0f}', FNT_TINY, C.MUTED, gx - 8, yy, 'mr')
        pygame.draw.line(screen, (40, 52, 74), (gx, yy), (gx + gw, yy), 1)
    span = max(now - t0, 1)
    drows = _decimate(rows, max(30, gw // 3))
    pts = [(gx + int((ts - t0) / span * gw), gy + gh - int(gh * (v - lo) / (hi - lo))) for ts, v in drows]
    if len(pts) > 1:
        pygame.draw.lines(screen, C.BLUE, False, pts, 3)
    text_at(screen, '00:00', FNT_TINY, C.MUTED, gx, gy + gh + 4, 'tl')
    text_at(screen, 'зараз' if i18n.get_lang() == 'uk' else 'now', FNT_TINY, C.MUTED, gx + gw, gy + gh + 4, 'tr')
    # тренд стовпцями по 3 год (8 стовпців)
    ty = y + h - 52
    text_at(screen, 'Тренд — кожен стовпець = 3 год' if i18n.get_lang() == 'uk' else 'Trend — 3h each',
            FNT_TINY, C.MUTED, gx, ty - 20, 'tl')
    nb = 8; bw = (gw - 7 * 8) // nb
    for b in range(nb):
        b0 = t0 + b * 3 * 3600; b1 = b0 + 3 * 3600
        seg = [v for ts, v in rows if b0 <= ts < b1]
        if len(seg) < 2:
            continue
        d = seg[-1] - seg[0]; col = C.GREEN if d >= 0 else C.RED
        bh = int(min(30, abs(d) * 10) + 3); bx = gx + b * (bw + 8); yb = ty + 16
        pygame.draw.rect(screen, col, (bx, yb - bh if d >= 0 else yb, bw, bh), border_radius=3)
        text_at(screen, f'{d:+.1f}', FNT_TINY, col, bx + bw // 2, yb + (-bh - 8 if d >= 0 else bh + 8), 'mc')

def _fc_text():
    fc = _wx_forecast()
    return fc, (fc['text_uk'] if i18n.get_lang() == 'uk' else fc['text_en'])

# ══════════════════════════════ ДИЗАЙН 1 ═════════════════════════════════════
def _outdoor_block(x, y, cw, hh, big=56):
    src = db.get_setting('outdoor_src', 'ble')
    title = ('Надворі · BLE' if src == 'ble' else 'Надворі') if i18n.get_lang() == 'uk' else ('Outdoor · BLE' if src == 'ble' else 'Outdoor')
    _v12_card((x, y, cw, hh), title, C.BORDER)
    pygame.draw.circle(screen, C.MUTED, (x + cw - 30, y + 24), 7)
    if src == 'off':
        _t('—', _wf(big, True), C.MUTED, x + cw // 2, y + hh // 2 - 16, 'mc'); return
    if src == 'co2':
        _ic_co2(x + 40, y + 60, C.MUTED); r = _t(_v12_fmt(cget('co2'), 0), _wf(big, True), C.MUTED, x + 72, y + 40, 'tl')
        _t('ppm', _wf(20), C.MUTED, r.right + 8, r.bottom - 24, 'tl')
        _t('джерело: CO₂ (немає BLE)', FNT_TINY, C.MUTED, x + 22, y + hh - 30, 'tl'); return
    _ic_thermo(x + 40, y + 62, C.MUTED); _t('13.3', _wf(big, True), C.MUTED, x + 72, y + 40, 'tl'); _t('°C', _wf(21), C.MUTED, x + 205 if big >= 56 else x + 196, y + 68, 'tl')
    _ic_drop(x + 300, y + 55, C.MUTED); _t('53', _wf(big - 16, True), C.MUTED, x + 330, y + 42, 'tl'); _t('%', _wf(18), C.MUTED, x + 400, y + 60, 'tl')
    _t('заглушка — датчик по BLE', FNT_TINY, C.MUTED, x + 22, y + hh - 30, 'tl')

def draw_wx1():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    pygame.draw.rect(screen, C.ACCENT_D, (0, 0, 6, C.H))
    now = datetime.now()
    loc = db.get_setting('loc_name', 'Munich, Bavaria')
    _ic_pin(40, 46); _t(loc, _wf(25, True), C.WHITE, 58, 34, 'tl')
    _t(now.strftime('%A, %d.%m.%Y'), _wf(19), C.TEXT2, 58, 68, 'tl')
    sr, ss = _wx_sun()
    _sunrise_icon(330, 74, True, C.YELLOW);  _t(sr.strftime('%H:%M') if sr else '--:--', _wf(22, True), C.YELLOW, 360, 66, 'ml')
    _sunrise_icon(330, 116, False, C.ORANGE); _t(ss.strftime('%H:%M') if ss else '--:--', _wf(22, True), C.ORANGE, 360, 108, 'ml')
    r = _t(now.strftime('%H:%M'), _wf(104, True), C.WHITE, 560, 30, 'tl')
    _t(now.strftime('%S'), _wf(44, True), C.ACCENT, r.right + 10, r.bottom - 50, 'tl')
    age, illum, idx = wx.moon_phase()
    _draw_moon(1000, 74, 34, illum, age < wx.SYNODIC / 2)
    _t(wx.moon_name(idx, i18n.get_lang()), _wf(19), C.TEXT2, 1044, 60, 'ml')
    _t(f'{int(illum*100)}%', _wf(17), C.MUTED, 1044, 84, 'ml')

    y = 168; cw = (C.W - 48 - 14) // 2; hh = 158
    x = 24; _v12_card((x, y, cw, hh), 'У приміщенні' if i18n.get_lang() == 'uk' else 'Indoor', C.CYAN)
    _ic_thermo(x + 40, y + 64, C.CYAN); _t(_v12_fmt(temp_disp('temperature'), 1), _wf(56, True), C.GREEN, x + 72, y + 40, 'tl')
    _t(temp_unit(), _wf(21), C.TEXT2, x + 205, y + 70, 'tl')
    _ic_drop(x + 300, y + 56, C.BLUE); _t(_v12_fmt(cget('humidity'), 0), _wf(40, True), C.WHITE, x + 330, y + 42, 'tl'); _t('%', _wf(18), C.TEXT2, x + 400, y + 62, 'tl')
    _ic_co2(x + 300, y + 112, C.GREEN); _t(_v12_fmt(cget('co2'), 0), _wf(24, True), C.ORANGE, x + 330, y + 98, 'tl'); _t('ppm CO₂', _wf(15), C.MUTED, x + 330, y + 124, 'tl')
    x = 24 + cw + 14; _outdoor_block(x, y, cw, hh, 56)

    _wx_pgraph((24, 342, 720, C.H - 342 - 20))

    rx = 760; rw = C.W - rx - 24
    _v12_card((rx, 342, rw, 116), 'Якість повітря' if i18n.get_lang() == 'uk' else 'Air quality', C.GREEN)
    aqi = derived_value('aqi'); st, sc = _metric_status('aqi', aqi)
    _t(f'AQI {_v12_fmt(aqi,0)}', _wf(28, True), C.WHITE, rx + 22, 342 + 30, 'tl'); _t(st, _wf(21), sc, rx + rw - 18, 342 + 34, 'tr')
    _grad_bar_frac((rx + 22, 342 + 68, rw - 44, 20), (aqi or 0) / 300.)
    _v12_card((rx, 474, rw, 116), 'Індекс УФ' if i18n.get_lang() == 'uk' else 'UV index', C.ORANGE)
    uv, est = _wx_uv(); _uv_badge((rx, 474, rw, 116), uv, ('приблизно' if est else 'з інтернету') if i18n.get_lang() == 'uk' else ('estimate' if est else 'internet'), est)
    if not est: _net_mark(rx + 70, 474 + 26)
    _v12_card((rx, 606, rw, C.H - 606 - 20), 'Прогноз' if i18n.get_lang() == 'uk' else 'Forecast', C.PURPLE)
    fc, ftxt = _fc_text(); _draw_wx_icon(rx + 66, 606 + 52, 50, fc['icon'])
    words = ftxt.split(' '); l1 = ''; l2 = ''
    for wd in words:
        (l1, l2) = (l1 + ' ' + wd if l1 else wd, l2) if len(l1) < 20 else (l1, l2 + ' ' + wd if l2 else wd)
    _t(l1, _wf(21), C.TEXT2, rx + 146, 606 + 40, 'ml')
    if l2: _t(l2, _wf(21), C.TEXT2, rx + 146, 606 + 68, 'ml')

    hb = text_at(screen, '☰', FNT_BIG, C.WHITE, C.W - 30, 24, 'mc')
    MAIN_RECTS['__hburger__'] = pygame.Rect(hb.left - 14, hb.top - 14, hb.width + 28, hb.height + 28)
    ah = _auto_hide_enabled(); _wx_overlay(_ui_shown[0] or not ah)
    pygame.display.update()

# ══════════════════════════════ ДИЗАЙН 4 ═════════════════════════════════════
def draw_wx4():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    pygame.draw.rect(screen, C.ACCENT_D, (0, 0, 6, C.H))
    now = datetime.now()
    loc = db.get_setting('loc_name', 'Munich, Bavaria')
    _ic_pin(40, 44); _t(loc, _wf(24, True), C.WHITE, 58, 30, 'tl'); _t(now.strftime('%A, %d.%m.%Y'), _wf(17), C.TEXT2, 58, 62, 'tl')
    sr, ss = _wx_sun()
    _sunrise_icon(300, 40, True, C.YELLOW);  _t(sr.strftime('%H:%M') if sr else '--:--', _wf(19, True), C.YELLOW, 330, 32, 'ml')
    _sunrise_icon(300, 82, False, C.ORANGE); _t(ss.strftime('%H:%M') if ss else '--:--', _wf(19, True), C.ORANGE, 330, 74, 'ml')
    r = _t(now.strftime('%H:%M'), _wf(80, True), C.WHITE, 440, 22, 'tl')
    _t(now.strftime('%S'), _wf(34, True), C.ACCENT, r.right + 8, r.bottom - 40, 'tl')
    # картка прогнозу МІЖ годинником і місяцем
    fc, ftxt = _fc_text()
    fcr = (r.right + 70, 20, 320, 96)
    _v12_card(fcr, None, C.PURPLE)
    _draw_wx_icon(fcr[0] + 44, fcr[1] + 48, 38, fc['icon'])
    _t('ПРОГНОЗ' if i18n.get_lang() == 'uk' else 'FORECAST', FNT_TINY, C.PURPLE, fcr[0] + 88, fcr[1] + 14, 'tl')
    words = ftxt.split(' '); l1 = ''; l2 = ''
    for wd in words:
        (l1, l2) = (l1 + ' ' + wd if l1 else wd, l2) if len(l1) < 16 else (l1, l2 + ' ' + wd if l2 else wd)
    _t(l1, _wf(18, True), C.TEXT, fcr[0] + 88, fcr[1] + 36, 'tl')
    if l2: _t(l2, _wf(18, True), C.TEXT, fcr[0] + 88, fcr[1] + 60, 'tl')
    # місяць праворуч (без хмаринки)
    age, illum, idx = wx.moon_phase()
    _draw_moon(1150, 60, 32, illum, age < wx.SYNODIC / 2)
    _t(wx.moon_name(idx, i18n.get_lang()), _wf(17), C.TEXT2, 1150, 104, 'mc')

    y = 140; cw = (C.W - 48 - 14) // 2; hh = 150
    x = 24; _v12_card((x, y, cw, hh), 'У приміщенні' if i18n.get_lang() == 'uk' else 'Indoor', C.CYAN)
    _ic_thermo(x + 40, y + 62, C.CYAN); _t(_v12_fmt(temp_disp('temperature'), 1), _wf(52, True), C.GREEN, x + 72, y + 40, 'tl'); _t(temp_unit(), _wf(20), C.TEXT2, x + 196, y + 66, 'tl')
    _ic_drop(x + 300, y + 54, C.BLUE); _t(_v12_fmt(cget('humidity'), 0), _wf(38, True), C.WHITE, x + 330, y + 42, 'tl'); _t('%', _wf(18), C.TEXT2, x + 396, y + 60, 'tl')
    _ic_co2(x + 300, y + 108, C.GREEN); _t(_v12_fmt(cget('co2'), 0), _wf(24, True), C.ORANGE, x + 330, y + 96, 'tl'); _t('ppm CO₂', _wf(15), C.MUTED, x + 330, y + 122, 'tl')
    x = 24 + cw + 14; _outdoor_block(x, y, cw, hh, 52)

    _wx_pgraph((24, 306, 720, C.H - 306 - 20))
    rx = 760; rw = C.W - rx - 24
    _v12_card((rx, 306, rw, 196), 'Тиск — аналогова шкала' if i18n.get_lang() == 'uk' else 'Pressure gauge', C.PURPLE)
    gcx = rx + rw // 2; gcy = 306 + 176; R = 86
    def pt(fr, rad):
        a = math.radians(180 + 180 * max(0, min(1, fr))); return (gcx + int(math.cos(a) * rad), gcy + int(math.sin(a) * rad))
    glo, ghi = 980, 1030
    for i in range(60):
        pygame.draw.line(screen, wx_lerp((52, 211, 153), (96, 165, 250), i / 60), pt(i / 60, R), pt((i + 1) / 60, R), 9)
    for v in (980, 990, 1000, 1010, 1020, 1030):
        text_at(screen, str(v), FNT_TINY, C.MUTED, *pt((v - glo) / (ghi - glo), R + 16), 'mc')
    praw = cget('pressure') or 1002
    fr = (praw - glo) / (ghi - glo); e = pt(fr, R - 14)
    pygame.draw.line(screen, C.WHITE, (gcx, gcy), e, 4); pygame.draw.circle(screen, C.WHITE, (gcx, gcy), 7)
    _t(_v12_fmt(pressure_disp(), _pdig()), _wf(30, True), C.WHITE, gcx, gcy - int(R * 0.30), 'mc')
    _t(pressure_unit_lbl(), FNT_TINY, C.TEXT2, gcx, gcy - int(R * 0.30) + 30, 'mc')
    _v12_card((rx, 518, rw, C.H - 518 - 20), 'Якість повітря' if i18n.get_lang() == 'uk' else 'Air quality', C.GREEN)
    aqi = derived_value('aqi'); st, sc = _metric_status('aqi', aqi)
    _t(f'AQI {_v12_fmt(aqi,0)}', _wf(28, True), C.WHITE, rx + 22, 518 + 34, 'tl'); _t(st, _wf(21), sc, rx + rw - 18, 518 + 38, 'tr')
    _grad_bar_frac((rx + 22, 518 + 76, rw - 44, 22), (aqi or 0) / 300.)

    hb = text_at(screen, '☰', FNT_BIG, C.WHITE, C.W - 30, C.H - 30, 'mc')
    MAIN_RECTS['__hburger__'] = pygame.Rect(hb.left - 14, hb.top - 14, hb.width + 28, hb.height + 28)
    ah = _auto_hide_enabled(); _wx_overlay(_ui_shown[0] or not ah)
    pygame.display.update()

# ══════════════════════════════ ДИЗАЙН 2 ═════════════════════════════════════
def draw_wx2():
    global MAIN_RECTS
    screen.fill(C.BG); MAIN_RECTS = {}
    for y in range(C.H):
        t = y / C.H; pygame.draw.line(screen, (int(16 + t * 18), int(24 + t * 26), int(52 + t * 40)), (0, y), (C.W, y))
    now = datetime.now()
    _ic_pin(40, 42); _t(db.get_setting('loc_name', 'Munich, Bavaria'), _wf(24, True), C.WHITE, 58, 24, 'tl')
    _t(now.strftime('%A, %d.%m.%Y'), _wf(18), (210, 220, 240), 58, 58, 'tl')
    r = _t(now.strftime('%H:%M'), _wf(96, True), C.WHITE, 640, 54, 'mc')
    _t(now.strftime('%S'), _wf(40, True), (180, 205, 255), r.right + 8, r.bottom - 8, 'br')
    wind = _wx_wind(); uv, uvest = _wx_uv()
    chips = [(_ic_wind, 'Вітер', ('—' if wind is None else f'{int(wind)} км/г'), (200, 215, 240), wind is not None),
             (_ic_gauge, 'Тиск', f'{_v12_fmt(pressure_disp(), 0)} {pressure_unit_lbl()}', C.PURPLE, False),
             (None, 'УФ-індекс', ('—' if uv is None else f'{uv}{"~" if uvest else ""}'), C.ORANGE, (uv is not None and not uvest))]
    for i, (ic, lab, val, col, net_src) in enumerate(chips):
        cx = 820 + i * 148
        if ic: ic(cx + 16, 54, col)
        else: _sun_icon(cx + 16, 52, 10, C.ORANGE)
        _t(lab, _wf(13), (190, 205, 235), cx + 40, 30, 'tl'); _t(val, _wf(19, True), C.WHITE, cx + 40, 50, 'tl')
        if net_src: _net_mark(cx + 40 + _wf(19, True).size(val)[0] + 12, 58)

    def glass(rect, rad=16, al=42):
        gs = pygame.Surface((rect[2], rect[3]), pygame.SRCALPHA)
        pygame.draw.rect(gs, (255, 255, 255, al), (0, 0, rect[2], rect[3]), border_radius=rad)
        pygame.draw.rect(gs, (255, 255, 255, 60), (0, 0, rect[2], rect[3]), 1, border_radius=rad)
        screen.blit(gs, (rect[0], rect[1]))

    glass((24, 120, 760, 290)); fc, ftxt = _fc_text(); _draw_wx_icon(150, 238, 96, fc['icon'])
    _t('У приміщенні' if i18n.get_lang() == 'uk' else 'Indoor', _wf(21), (200, 215, 240), 272, 146, 'tl')
    tr = _t(_v12_fmt(temp_disp('temperature'), 0), _wf(104, True), C.WHITE, 262, 164, 'tl'); _t(temp_unit(), _wf(38, True), (210, 220, 240), tr.right + 6, tr.top + 18, 'tl')
    _ic_drop(tr.right + 90, 220, C.BLUE); _t(f"{_v12_fmt(cget('humidity'),0)} %", _wf(34, True), C.WHITE, tr.right + 118, 200, 'tl')
    _t('Надворі · BLE', _wf(21), (180, 195, 220), 272, 300, 'tl')
    o = _t('13', _wf(60, True), (150, 205, 255), 262, 322, 'tl'); _t('°C*', _wf(24, True), (180, 195, 220), o.right + 6, o.top + 14, 'tl')
    _ic_drop(o.right + 90, 344, C.MUTED); _t('53 %*', _wf(30, True), (180, 195, 220), o.right + 118, 326, 'tl')

    glass((808, 120, 448, 135))
    _t('Якість повітря' if i18n.get_lang() == 'uk' else 'Air quality', _wf(20), (220, 230, 250), 828, 132, 'tl')
    aqi = derived_value('aqi'); st, sc = _metric_status('aqi', aqi)
    _t(f'AQI {_v12_fmt(aqi,0)}', _wf(32, True), C.WHITE, 828, 158, 'tl'); _t(st, _wf(21), sc, 1236, 168, 'tr')
    _grad_bar_frac((828, 210, 408, 20), (aqi or 0) / 300.)

    glass((808, 272, 448, 138)); _t('Погодинно' if i18n.get_lang() == 'uk' else 'Hourly', _wf(19), (220, 230, 250), 828, 282, 'tl')
    daily = net.get().get('daily', [])
    hourly = [('20:00', 'partly', 26), ('21:00', 'cloud', 24), ('22:00', 'cloud', 22), ('23:00', 'rain', 21)]
    for i, (hh, ic, tt) in enumerate(hourly):
        cx = 852 + i * 108; _t(hh, _wf(16), (210, 220, 240), cx, 318, 'mc'); _draw_wx_icon(cx, 352, 26, ic); _t(f'{tt}°', _wf(22, True), C.WHITE, cx, 388, 'mc')

    glass((24, 430, 1232, 250))
    hdr = ('Прогноз на тиждень   ·   поточне — з датчиків, решта — з інтернету' if i18n.get_lang() == 'uk'
           else 'Weekly forecast   ·   current from sensors, rest from internet')
    hr = _t(hdr, _wf(19), (220, 230, 250), 44, 444, 'tl'); _net_mark(hr.right + 14, 452)
    names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд'] if i18n.get_lang() == 'uk' else ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    dw = (1232 - 40) // 7
    for i in range(7):
        cx = 44 + i * dw + dw // 2
        if daily and i < len(daily):
            d = daily[i]; ic = wx.wmo_icon(d.get('code')); hi = d.get('hi'); lo = d.get('lo')
        else:
            ic = 'partly'; hi = lo = None
        dname = names[(datetime.now().weekday() + i) % 7]
        if i == 0:
            fill_rect(screen, C.ACCENT_D, (44 + i * dw + 8, 486, dw - 16, 180), 14)
            _t('зараз' if i18n.get_lang() == 'uk' else 'now', _wf(14), C.BLUE, cx, 528, 'mc')
        _t(dname, _wf(22, True), C.WHITE, cx, 500, 'mc')
        _draw_wx_icon(cx, 566, 40, ic)
        _t(('—' if hi is None else f'{int(hi)}°'), _wf(28, True), C.WHITE, cx - 8, 616, 'mc')
        _t(('' if lo is None else f'{int(lo)}°'), _wf(20), (180, 195, 220), cx + 32, 622, 'mc')

    ah = _auto_hide_enabled(); _wx_overlay(_ui_shown[0] or not ah)
    pygame.display.update()


# ══════════════════════════════════════════════════════════════════════════════
#  v19 меню: випадаючі списки, вкладка «Бокси», перегруповане «Вигляд»,
#            перемикач інтернет-позначок, прокрутка довідки.
# ══════════════════════════════════════════════════════════════════════════════

_dropdown = [None]           # {'setting':..., 'options':[(val,label)], 'anchor':(x,y,w)}
_dropdown_rects = []         # [(val, rect)]
_about_scroll = [0]

_BOX_LABELS = {'co2': 'CO₂', 'voc_index': 'VOC Index', 'nox_index': 'NOx Index', 'eco2': 'eCO₂',
               'aqi': 'AQI', 'iaq': 'IAQ', 'pm2_5': 'PM2.5', 'pm10': 'PM10',
               'temperature': 'Температура', 'humidity': 'Вологість'}

def _dd_button(setting, cur_label, rect):
    """Кнопка-«випадайка»: показує поточне значення і відкриває список."""
    _menu_buttons_v14.append(('ddopen:' + setting, pygame.Rect(rect)))
    fill_rect(screen, C.PANEL2, rect, radius=9)
    stroke_rect(screen, C.ACCENT if (_dropdown[0] and _dropdown[0]['setting'] == setting) else C.BORDER, rect, 1, radius=9)
    text_at(screen, cur_label, FNT_SMALL, C.WHITE, rect[0] + 14, rect[1] + rect[3] // 2, 'ml')
    text_at(screen, '▾', FNT_SMALL, C.ACCENT, rect[0] + rect[2] - 14, rect[1] + rect[3] // 2, 'mr')

def _draw_dropdown_overlay():
    """Малює відкритий список поверх усього (в кінці draw_menu)."""
    global _dropdown_rects
    _dropdown_rects = []
    dd = _dropdown[0]
    if not dd:
        return
    ax, ay, aw = dd['anchor']
    opts = dd['options']
    rowh = 40
    maxrows = min(len(opts), 9)
    ph = maxrows * rowh + 8
    # відкриваємо вниз, або вгору якщо не влазить
    py = ay if ay + ph < C.H - 10 else max(10, ay - ph - 44)
    panel = pygame.Rect(ax, py, aw, ph)
    fill_rect(screen, C.PANEL, panel, radius=10)
    stroke_rect(screen, C.ACCENT, panel, 2, radius=10)
    for i, (val, lab) in enumerate(opts[:maxrows]):
        rr = pygame.Rect(ax + 4, py + 4 + i * rowh, aw - 8, rowh - 2)
        _dropdown_rects.append((val, rr))
        cur = db.get_setting(dd['setting'], '') == val
        if cur:
            fill_rect(screen, C.ACCENT_D, rr, radius=7)
        text_at(screen, lab, FNT_SMALL, C.WHITE if cur else C.TEXT2, rr.x + 12, rr.centery, 'ml')

def _draw_menu_display_v14():
    x, y = 150, 158
    # дизайн за замовчуванням (випадайка) + інтернет-позначки
    _v12_card((x, y, 600, 108), T('card_design'), C.YELLOW)
    _dd_button('screen_id', _SCREEN_NAMES.get(db.get_setting('screen_id', 'air'), 'Станція повітря'),
               (x + 24, y + 52, 400, 44))
    _v12_card((770, y, 380, 108), T('card_netmark'), C.BLUE)
    _seg_toggle(794, y + 52, 330, 44, [('1', T('opt_show')), ('0', T('opt_hide'))],
                db.get_setting('show_net_mark', '1'), 'netmark')

    y2 = y + 126
    _v12_card((x, y2, 600, 120), T('card_autohide'), C.ACCENT)
    _seg_toggle(x + 24, y2 + 52, 210, 44, [('1', T('opt_on')), ('0', T('opt_off'))],
                db.get_setting('auto_hide', '1'), 'auto_hide')
    text_at(screen, T('card_hidesec'), FNT_TINY, C.MUTED, x + 270, y2 + 40, 'tl')
    try: hs = int(float(db.get_setting('hide_sec', '10')))
    except Exception: hs = 10
    text_at(screen, str(hs), FNT_BIG, C.WHITE, x + 452, y2 + 56, 'mc')
    _v14_button('hide-', (x + 288, y2 + 54, 52, 42), C.ORANGE, '−')
    _v14_button('hide+', (x + 500, y2 + 54, 52, 42), C.GREEN, '+')
    _v12_card((770, y2, 380, 120), T('card_header'), C.CYAN)
    _seg_toggle(794, y2 + 52, 330, 44, [('1', T('opt_show')), ('0', T('opt_hide'))],
                db.get_setting('show_header', '1'), 'show_header')

    y3 = y2 + 138
    _v12_card((x, y3, 600, 120), T('card_mgstyle'), C.PURPLE)
    _seg_toggle(x + 24, y3 + 52, 550, 48, [('bars', T('style_bars')), ('gauge', T('style_gauge'))],
                db.get_setting('main_graph_style', 'bars'), 'mgstyle')
    _v12_card((770, y3, 380, 120), T('card_metricstyle'), C.GREEN)
    _seg_toggle(794, y3 + 52, 330, 48, [('bars', T('style_bars')), ('gradient', T('style_gradient'))],
                db.get_setting('metric_style', 'bars'), 'metricstyle')

    text_at(screen, T('design_hint'), FNT_TINY, C.MUTED, x, y3 + 132, 'tl')

def _draw_menu_boxes_v14():
    x, y = 150, 158
    _v12_card((x, y, 1000, 150), T('card_boxes'), C.BLUE)
    for i, setkey in enumerate(['box1', 'box2', 'box3']):
        cur = db.get_setting(setkey, ['co2', 'voc_index', 'nox_index'][i])
        bx = x + 24 + i * 320
        text_at(screen, f'Бокс {i + 1}', FNT_SMALL, C.MUTED, bx, y + 46, 'tl')
        _dd_button(setkey, _BOX_LABELS.get(cur, cur), (bx, y + 72, 290, 48))
    text_at(screen, T('boxes_hint'), FNT_TINY, C.MUTED, x + 24, y + 130, 'tl')

    y2 = y + 168
    _v12_card((x, y2, 600, 150), T('card_outdoor'), C.CYAN)
    _seg_toggle(x + 24, y2 + 60, 550, 54,
                [('ble', T('out_ble')), ('co2', T('out_co2')), ('off', T('out_off'))],
                db.get_setting('outdoor_src', 'ble'), 'outsrc')

    _v12_card((770, y2, 380, 150), T('card_location'), C.PURPLE)
    text_at(screen, f"{db.get_setting('loc_name','Munich, Bavaria')}", FNT_SMALL, C.TEXT2, 794, y2 + 52, 'tl')
    text_at(screen, f"lat {db.get_setting('lat','48.14')}  lon {db.get_setting('lon','11.68')}",
            FNT_TINY, C.MUTED, 794, y2 + 84, 'tl')
    _net_indicator(794, y2 + 118)

    _v14_button('Save', (C.W - 168, y + 40, 150, 54), C.GREEN, T('save'))

def draw_about_screen():
    _screen_header(T('about_title'), T('about_sub'))
    lines = i18n.about_lines()
    # висота контенту
    def step(kind): return 40 if kind in ('h', 'w') else 33
    total = sum(step(k) if txt else 14 for txt, k in lines)
    view_h = C.H - 120
    maxscroll = max(0, total - view_h)
    _about_scroll[0] = max(0, min(_about_scroll[0], maxscroll))
    x = 150; y = 102 - _about_scroll[0]
    prev_clip = screen.get_clip()
    screen.set_clip(pygame.Rect(0, 96, C.W, view_h))
    for txt, kind in lines:
        if txt == '':
            y += 14; continue
        col, f, st = (C.CYAN, FNT_MED, 40) if kind == 'h' else \
                     (C.ORANGE, FNT_MED, 40) if kind == 'w' else (C.TEXT2, FNT_SMALL, 33)
        if -40 < y < C.H:
            text_at(screen, txt, f, col, x, y, 'tl')
        y += st
    screen.set_clip(prev_clip)
    # смуга прокрутки + кнопки ▲▼
    if maxscroll > 0:
        bar_h = int(view_h * view_h / total); bar_y = 96 + int((view_h - bar_h) * _about_scroll[0] / maxscroll)
        fill_rect(screen, C.PANEL2, (C.W - 12, 96, 6, view_h), radius=3)
        fill_rect(screen, C.ACCENT, (C.W - 12, bar_y, 6, bar_h), radius=3)
        fill_rect(screen, C.PANEL2, (C.W - 74, C.H - 108, 56, 48), radius=10); stroke_rect(screen, C.BORDER, (C.W - 74, C.H - 108, 56, 48), 1, radius=10)
        text_at(screen, '▲', FNT_BIG, C.ACCENT, C.W - 46, C.H - 84, 'mc')
        fill_rect(screen, C.PANEL2, (C.W - 74, C.H - 56, 56, 48), radius=10); stroke_rect(screen, C.BORDER, (C.W - 74, C.H - 56, 56, 48), 1, radius=10)
        text_at(screen, '▼', FNT_BIG, C.ACCENT, C.W - 46, C.H - 32, 'mc')
    pygame.display.update()

def draw_menu():
    global _menu_buttons_v14, _menu_buttons_v13, _menu_source_buttons
    _menu_buttons_v14 = []; _menu_buttons_v13 = []; _menu_source_buttons = []
    screen.fill(C.BG)
    _back_btn()
    text_at(screen, T('menu_title'), FNT_TITLE, C.WHITE, 176, 16, 'tl')
    text_at(screen, T('menu_sub'), FNT_TINY, C.MUTED, 176, 52, 'tl')
    tx = 150
    for key, _lab in TABS:
        rect = pygame.Rect(tx, 92, 150, 50)
        _menu_buttons_v14.append(('tab:' + key, rect))
        sel = _menu_tab[0] == key
        fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rect, radius=10)
        stroke_rect(screen, C.ACCENT if sel else C.BORDER, rect, 1, radius=10)
        text_at(screen, T('tab_' + key), FNT_SMALL, C.WHITE if sel else C.TEXT2, rect.centerx, rect.centery, 'mc')
        tx += 158
    tab = _menu_tab[0]
    if tab == 'general': _draw_menu_general_v14()
    elif tab == 'display': _draw_menu_display_v14()
    elif tab == 'screens': _draw_menu_screens_v14()
    elif tab == 'boxes': _draw_menu_boxes_v14()
    elif tab == 'calib': _draw_menu_calib_v14()
    elif tab == 'sensors': _draw_menu_sensors_v14()
    elif tab == 'time': _draw_menu_time_v14()
    if _menu_msg[0]:
        text_at(screen, _menu_msg[0], FNT_SMALL, C.YELLOW, 150, C.H - 8, 'bl')
    _draw_dropdown_overlay()
    if _kbd['active']:
        draw_keyboard()
    pygame.display.update()
    return _menu_buttons_v14

def menu_hit(pos, buttons):
    global state, chart_key
    # якщо відкрито випадайку — обробляємо лише її
    if _dropdown[0] is not None:
        for val, rr in _dropdown_rects:
            if rr.collidepoint(pos):
                db.set_setting(_dropdown[0]['setting'], val)
                _dropdown[0] = None; _menu_msg[0] = T('saved'); return
        _dropdown[0] = None; return
    if pygame.Rect(2, 2, 170, 72).collidepoint(pos):
        state = State.MAIN; return
    for label, rect in list(_menu_buttons_v14):
        if not pygame.Rect(rect).collidepoint(pos):
            continue
        if label.startswith('ddopen:'):
            setting = label.split(':', 1)[1]
            if setting == 'screen_id':
                opts = [(s, _SCREEN_NAMES[s]) for s in SCREENS]
            else:
                opts = [(k, _BOX_LABELS[k]) for k in METRIC_ORDER]
            _dropdown[0] = {'setting': setting, 'options': opts,
                            'anchor': (rect.x, rect.bottom + 4, rect.width)}
            return
        if label.startswith('netmark:'): db.set_setting('show_net_mark', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('outsrc:'): db.set_setting('outdoor_src', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('carsel:'): _car_toggle(label.split(':', 1)[1]); return
        if label.startswith('carup:'): _car_move(label.split(':', 1)[1], -1); return
        if label.startswith('cardn:'): _car_move(label.split(':', 1)[1], 1); return
        if label == 'scrnew': _screen_create(); return
        if label.startswith('scrmgr:'): _scr_sel[0] = label.split(':', 1)[1]; return
        if label.startswith('scredit:'): _editor_open(_layout_path(label.split(':', 1)[1][5:])); return
        if label.startswith('scrdup:'): _screen_duplicate(label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('scrren:'):
            sid = label.split(':', 1)[1]
            _kbd_open('Назва екрана', sid[5:], lambda nm, s=sid: _screen_rename(s, nm)); return
        if label.startswith('scrdel:'): _screen_delete(label.split(':', 1)[1]); _scr_sel[0] = None; return
        if label == 'scrclose': _scr_sel[0] = None; return
        break
    _menu_hit_common(pos)

# делегуємо решту (калібровка/одиниці/тик/сенсори/збереження) старому обробнику v18
def _menu_hit_common(pos):
    global state, chart_key
    for label, rect in list(_menu_buttons_v14):
        if not pygame.Rect(rect).collidepoint(pos):
            continue
        if label.startswith('cal_') and (label.endswith(':-') or label.endswith(':+')):
            setk = label[:-2]; op = label[-1]
            step = {'cal_temperature': 0.1, 'cal_humidity': 1, 'cal_pressure': 0.1,
                    'cal_co2': 5, 'cal_voc_index': 1, 'cal_nox_index': 1}.get(setk, 1)
            try: cur = float(db.get_setting(setk, '0'))
            except Exception: cur = 0.0
            db.set_setting(setk, f'{cur + (step if op == "+" else -step):.2f}'); return
        if label.startswith('tempunit:'): db.set_setting('temp_unit', label.split(':', 1)[1]); return
        if label.startswith('presunit:'): db.set_setting('pressure_unit', label.split(':', 1)[1]); return
        if label.startswith('presmode:'): db.set_setting('pressure_mode', label.split(':', 1)[1]); return
        if label.startswith('mgstyle:'): db.set_setting('main_graph_style', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('metricstyle:'): db.set_setting('metric_style', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('auto_hide:'): db.set_setting('auto_hide', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label.startswith('show_header:'): db.set_setting('show_header', label.split(':', 1)[1]); _menu_msg[0] = T('saved'); return
        if label == 'hide-':
            try: h = int(float(db.get_setting('hide_sec', '10')))
            except Exception: h = 10
            db.set_setting('hide_sec', str(max(3, h - 1))); return
        if label == 'hide+':
            try: h = int(float(db.get_setting('hide_sec', '10')))
            except Exception: h = 10
            db.set_setting('hide_sec', str(min(120, h + 1))); return
        if label == 'alt-':
            try: a = int(float(db.get_setting('altitude_m', '520')))
            except Exception: a = 520
            db.set_setting('altitude_m', str(max(0, a - 10))); return
        if label == 'alt+':
            try: a = int(float(db.get_setting('altitude_m', '520')))
            except Exception: a = 520
            db.set_setting('altitude_m', str(min(4000, a + 10))); return
        if label.startswith('tab:'):
            _menu_tab[0] = label.split(':', 1)[1]; db.set_setting('menu_tab', _menu_tab[0]); return
        if label.startswith('lang:'):
            i18n.set_lang(label.split(':', 1)[1]); db.set_setting('lang', i18n.get_lang()); _menu_msg[0] = T('saved'); return
        if label == 'Save':
            db.set_setting('i2c_bus', str(_menu_bus[0])); db.set_setting('poll_sec', str(_menu_poll[0]))
            db.set_setting('graph_hours', str(_menu_hours[0])); db.set_setting('temp_source', S.SOURCE_MAP.get('temperature', 'bmp280'))
            db.set_setting('lang', i18n.get_lang()); db.set_setting('time_mode', _time_mode[0])
            db.set_setting('manual_hour', str(_manual_h[0])); db.set_setting('manual_min', str(_manual_m[0]))
            db.set_setting('manual_day', str(_manual_d[0])); db.set_setting('manual_month', str(_manual_mo[0])); db.set_setting('manual_year', str(_manual_y[0]))
            _menu_msg[0] = T('saved'); return
        if label == 'bus-': _menu_bus[0] = max(0, _menu_bus[0] - 1); return
        if label == 'bus+': _menu_bus[0] = min(9, _menu_bus[0] + 1); return
        if label == 'poll-': _menu_poll[0] = max(1, _menu_poll[0] - 1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label == 'poll+': _menu_poll[0] = min(3600, _menu_poll[0] + 1); db.set_setting('poll_sec', str(_menu_poll[0])); return
        if label.startswith('graph:'): _menu_hours[0] = int(label.split(':')[1]); db.set_setting('graph_hours', str(_menu_hours[0])); return
        if label.startswith('main_graph:'): db.set_setting('main_graph', label.split(':', 1)[1]); return
        if label.startswith('temp_source:'):
            src = label.split(':', 1)[1]; S.SOURCE_MAP['temperature'] = src; db.set_setting('temp_source', src); _menu_msg[0] = 'Temperature <- ' + src.upper(); return
        if label == 'Scan I2C': scan_result.clear(); state = State.I2CSCAN; return
        if label == 'Restart SCD41':
            def _do_scd():
                _menu_msg[0] = 'Restarting SCD41...'
                try:
                    S.REGISTRY['scd41'].online = False; ok = S.restart_scd41('manual menu'); _menu_msg[0] = 'SCD41 OK' if ok else 'SCD41 still OFF'
                except Exception as e: _menu_msg[0] = 'SCD err: ' + str(e)[:20]
            threading.Thread(target=_do_scd, daemon=True).start(); return
        if label == 'Restart SPS30':
            def _do_sps():
                _menu_msg[0] = 'Restarting SPS30...'
                try:
                    S.REGISTRY['sps30'].online = False; ok = S.restart_sps30(_menu_bus[0]); _menu_msg[0] = 'SPS30 OK' if ok else 'SPS30 OFF'
                except Exception as e: _menu_msg[0] = 'SPS err: ' + str(e)[:20]
            threading.Thread(target=_do_sps, daemon=True).start(); return
        if label == 'Purge DB': db.purge(7); _menu_msg[0] = 'DB purged'; return
        if label.startswith('time:'): _set_time_mode(label.split(':', 1)[1]); return
        if label in ['year-', 'year+', 'month-', 'month+', 'day-', 'day+', 'hour-', 'hour+', 'min-', 'min+']:
            fields = {'year': (_manual_y, 2020, 2099), 'month': (_manual_mo, 1, 12), 'day': (_manual_d, 1, 31),
                      'hour': (_manual_h, 0, 23), 'min': (_manual_m, 0, 59)}
            name = label[:-1]; op = label[-1]; ref, lo, hi = fields[name]
            ref[0] = max(lo, min(hi, ref[0] + (1 if op == '+' else -1))); return


# ══════════════════════════════════════════════════════════════════════════════
#  v20 — grid-розкладки: кастомні екрани з редактора з'являються в каруселі та
#        малюються рушієм gridui з реальними даними.
# ══════════════════════════════════════════════════════════════════════════════

_LAYOUT_CACHE = {}

def _layout_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'layouts')

def _custom_layouts():
    d = _layout_dir(); out = []
    try:
        for f in sorted(glob.glob(os.path.join(d, '*.json'))):
            out.append('grid:' + os.path.splitext(os.path.basename(f))[0])
    except Exception:
        pass
    return out

def _all_screens():
    """Екрани в каруселі: вбудовані (за увімкненими) + кастомні grid."""
    en = db.get_setting('carousel', '')  # csv увімкнених; порожньо = всі вбудовані
    if en.strip():
        base = [s for s in en.split(',') if s]
    else:
        base = list(SCREENS)
    customs = [c for c in _custom_layouts() if c not in base]
    return base + customs

def _page_screen(delta):
    scr = _all_screens()
    if not scr:
        scr = list(SCREENS)
    sid = db.get_setting('screen_id', 'air')
    try: i = scr.index(sid)
    except ValueError: i = 0
    db.set_setting('screen_id', scr[(i + delta) % len(scr)])
    _ui_last_touch[0] = time.time()

def _grid_data():
    now = datetime.now()
    sr, ss = _wx_sun()
    age, illum, idx = wx.moon_phase()
    fc = _wx_forecast()
    _n, _t0, prows = _window_series('pressure', 24)
    wind = _wx_wind(); uv, uvest = _wx_uv()
    nd = net.get()
    kp = nd.get('kp')
    klab = wx.kp_status(kp)[0]
    lat, lon = _wx_latlon()
    aurora = wx.aurora_chance(kp, lat, i18n.get_lang())
    retro_detail = wx.retrograde_detail(now, i18n.get_lang())
    retro_names = [d['planet'] for d in retro_detail]
    return {
        'time_hm': now.strftime('%H:%M'), 'time_s': now.strftime('%S'),
        'date': now.strftime('%d.%m.%Y'), 'weekday': i18n.weekdays()[now.weekday()],
        'loc': db.get_setting('loc_name', 'Munich, Bavaria'),
        'temperature': temp_disp('temperature'), 'temp_unit': temp_unit(),
        'humidity': cget('humidity'), 'pressure': pressure_disp(),
        'press_unit': pressure_unit_lbl(),
        'co2': cget('co2'), 'voc_index': cget('voc_index'), 'nox_index': cget('nox_index'),
        'eco2': derived_value('eco2'), 'aqi': derived_value('aqi'), 'iaq': derived_value('iaq'),
        'pm2_5': cget('pm2_5'), 'pm10': cget('pm10'),
        'sunrise': sr.strftime('%H:%M') if sr else '--:--', 'sunset': ss.strftime('%H:%M') if ss else '--:--',
        'moon_illum': illum, 'moon_wax': age < wx.SYNODIC / 2, 'moon_name': wx.moon_name(idx, i18n.get_lang()),
        'forecast_icon': fc['icon'], 'forecast_text': fc['text_uk'] if i18n.get_lang() == 'uk' else fc['text_en'],
        'forecast_rate': fc['rate'], 'wind': wind, 'uv': uv, 'uv_est': uvest,
        'online': _net_online(), 'out_temp': '13.3', 'pressure_series': prows,
        'kp': kp, 'kp_label': klab, 'retro': retro_names,
        'kp_hist': nd.get('kp_hist', []), 'kp_days': nd.get('kp_days', []),
        'aurora': aurora, 'retro_detail': retro_detail,
        'kp_ts': nd.get('kp_ts', 0), 'loading': nd.get('loading', False),
        'astro_forecast': wx.astro_forecast(kp, retro_names, idx, aurora, i18n.get_lang()),
        'advice': wx.advice_of_day(now, kp=kp, uv=uv, moon_idx=idx, lang=i18n.get_lang()),
    }

def _draw_grid_screen(name):
    path = os.path.join(_layout_dir(), name + '.json')
    lay = _LAYOUT_CACHE.get(path)
    if lay is None:
        lay = GUI.load_layout(path); _LAYOUT_CACHE[path] = lay
    GUI.render_layout(screen, lay, _grid_data(), C.W, C.H)

def _draw_current_screen():
    global MAIN_RECTS
    sid = db.get_setting('screen_id', 'air')
    if sid.startswith('grid:'):
        try:
            MAIN_RECTS = {}
            _draw_grid_screen(sid[5:])
            ah = _auto_hide_enabled()
            hb = text_at(screen, '☰', FNT_BIG, C.WHITE, C.W - 30, C.H - 30, 'mc')
            MAIN_RECTS['__hburger__'] = pygame.Rect(hb.left - 14, hb.top - 14, hb.width + 28, hb.height + 28)
            _wx_overlay(_ui_shown[0] or not ah)
            if _astro_detail[0]:
                _draw_planet_detail(_astro_detail[0])
            pygame.display.update()
            return
        except Exception:
            pass  # якщо файл зіпсовано — падаємо на air
    {'air': draw_main, 'wx1': draw_wx1, 'wx2': draw_wx2, 'wx4': draw_wx4,
     'wx': draw_weather}.get(sid, draw_main)()


# ══════════════════════════════════════════════════════════════════════════════
#  v21 — керування каруселлю в меню: які екрани показувати при свайпі + порядок.
# ══════════════════════════════════════════════════════════════════════════════

def _car_available():
    """Усі доступні екрани: вбудовані + кастомні grid."""
    return list(SCREENS) + _custom_layouts()

def _screen_name(sid):
    if sid.startswith('grid:'):
        return sid[5:]
    return _SCREEN_NAMES.get(sid, sid)

def _car_list():
    """Впорядкований список УВІМКНЕНИХ екранів (карусель).
    Порожньо (не налаштовано) → усі вбудовані + кастомні, щоб нові екрани
    (напр. астро) було видно одразу. Після налаштування — точно вибір користувача."""
    avail = set(_car_available())
    en = db.get_setting('carousel', '').strip()
    if en:
        lst = [s for s in en.split(',') if s]
    else:
        lst = list(SCREENS) + _custom_layouts()
    lst = [s for s in lst if s in avail]
    return lst or ['air']

def _car_set(lst):
    db.set_setting('carousel', ','.join(lst))

def _car_toggle(sid):
    lst = _car_list()
    if sid in lst:
        if len(lst) > 1:          # хоча б один екран має лишитись
            lst.remove(sid)
    else:
        lst.append(sid)
    _car_set(lst); _menu_msg[0] = T('saved')

def _car_move(sid, d):
    lst = _car_list()
    if sid in lst:
        i = lst.index(sid); j = i + d
        if 0 <= j < len(lst):
            lst[i], lst[j] = lst[j], lst[i]; _car_set(lst)

# карусель тепер = впорядкований увімкнений список
def _all_screens():
    return _car_list()

def _draw_menu_screens_v14():
    x, y = 150, 116
    text_at(screen, T('car_title'), FNT_SMALL, C.TEXT2, x, y, 'tl')
    text_at(screen, T('car_hint'), FNT_TINY, C.MUTED, x, y + 26, 'tl')
    enabled = _car_list()
    avail = _car_available()
    ordered = enabled + [s for s in avail if s not in enabled]
    cur = db.get_setting('screen_id', 'air')
    row_y = y + 56
    for sid in ordered[:9]:
        on = sid in enabled
        rect = pygame.Rect(x, row_y, 1000, 48)
        fill_rect(screen, C.PANEL2 if on else C.PANEL, rect, radius=10)
        stroke_rect(screen, C.ACCENT if sid == cur else C.BORDER, rect, 2 if sid == cur else 1, radius=10)
        # чекбокс
        cb = pygame.Rect(x + 12, row_y + 11, 26, 26)
        _menu_buttons_v14.append(('carsel:' + sid, pygame.Rect(x, row_y, 640, 48)))
        fill_rect(screen, C.ACCENT_D if on else C.PANEL, cb, radius=6)
        stroke_rect(screen, C.GREEN if on else C.BORDER, cb, 2 if on else 1, radius=6)
        if on:
            text_at(screen, '✓', FNT_SMALL, C.GREEN, cb.centerx, cb.centery, 'mc')
        # назва + тип
        is_grid = sid.startswith('grid:')
        text_at(screen, _screen_name(sid), FNT_MED, C.WHITE if on else C.MUTED, x + 56, row_y + 24, 'ml')
        text_at(screen, f"({T('car_custom') if is_grid else T('car_builtin')})", FNT_TINY, C.MUTED, x + 320, row_y + 24, 'ml')
        if sid == cur:
            text_at(screen, '● зараз', FNT_TINY, C.ACCENT, x + 470, row_y + 24, 'ml')
        # ▲▼ лише для увімкнених
        if on:
            up = pygame.Rect(x + 900, row_y + 6, 44, 36); dn = pygame.Rect(x + 950, row_y + 6, 44, 36)
            _menu_buttons_v14.append(('carup:' + sid, up)); _menu_buttons_v14.append(('cardn:' + sid, dn))
            fill_rect(screen, C.PANEL, up, radius=7); stroke_rect(screen, C.BORDER, up, 1, radius=7)
            fill_rect(screen, C.PANEL, dn, radius=7); stroke_rect(screen, C.BORDER, dn, 1, radius=7)
            text_at(screen, '▲', FNT_SMALL, C.ACCENT, up.centerx, up.centery, 'mc')
            text_at(screen, '▼', FNT_SMALL, C.ACCENT, dn.centerx, dn.centery, 'mc')
        row_y += 54


# ══════════════════════════════════════════════════════════════════════════════
#  v23 — керування кастомними екранами на пристрої + on-screen grid-редактор.
# ══════════════════════════════════════════════════════════════════════════════

_ed = {'layout': None, 'path': None, 'sel': None, 'drag': None,
       'palette': False, 'mpick': False, 'showgrid': True}
_ed_btns = []
_ed_pal_rects = []
_ed_mpick_rects = []
_scr_sel = [None]
_kbd = {'active': False, 'text': '', 'title': '', 'cb': None}
_kbd_rects = []
_ED_METRICS = ['co2', 'voc_index', 'nox_index', 'eco2', 'aqi', 'iaq', 'pm2_5', 'pm10', 'temperature', 'humidity', 'pressure']

# ── Файлові операції з екранами ───────────────────────────────────────────────
def _layout_path(name):
    return os.path.join(_layout_dir(), name + '.json')

def _slug(name):
    s = ''.join(ch for ch in name.lower() if ch.isalnum() or ch in '_-')
    return s or 'custom'

def _unique_name(base):
    base = _slug(base); name = base; i = 2
    while os.path.exists(_layout_path(name)):
        name = f'{base}_{i}'; i += 1
    return name

def _blank_layout(name):
    return {'name': name, 'cols': 16, 'rows': 9, 'blocks': [
        {'type': 'clock', 'col': 5, 'row': 0, 'w': 6, 'h': 2, 'p': {'seconds': True}},
        {'type': 'metric', 'col': 0, 'row': 2, 'w': 5, 'h': 3, 'p': {'key': 'co2'}},
    ]}

def _screen_create():
    os.makedirs(_layout_dir(), exist_ok=True)
    name = _unique_name('custom')
    lay = _blank_layout(name)
    GUI.save_layout(lay, _layout_path(name)); _LAYOUT_CACHE.clear()
    _editor_open(_layout_path(name))

def _screen_duplicate(sid):
    old = sid[5:]
    try:
        lay = GUI.load_layout(_layout_path(old))
    except Exception:
        return
    new = _unique_name(old + '_copy'); lay['name'] = new
    GUI.save_layout(lay, _layout_path(new)); _LAYOUT_CACHE.clear()
    _car_toggle('grid:' + new)   # одразу вмикаємо копію в карусель

def _screen_delete(sid):
    old = sid[5:]
    try: os.remove(_layout_path(old))
    except Exception: pass
    _LAYOUT_CACHE.clear()
    lst = _car_list()
    if sid in lst:
        lst = [s for s in lst if s != sid] or ['air']; _car_set(lst)
    if db.get_setting('screen_id', 'air') == sid:
        db.set_setting('screen_id', 'air')

def _screen_rename(sid, newname):
    old = sid[5:]; new = _slug(newname)
    if new == old:
        return
    new = _unique_name(new)
    try: os.rename(_layout_path(old), _layout_path(new))
    except Exception: return
    try:
        lay = GUI.load_layout(_layout_path(new)); lay['name'] = new; GUI.save_layout(lay, _layout_path(new))
    except Exception: pass
    _LAYOUT_CACHE.clear()
    # оновити посилання в каруселі та поточному екрані
    lst = [('grid:' + new if s == sid else s) for s in _car_list()]; _car_set(lst)
    if db.get_setting('screen_id', 'air') == sid:
        db.set_setting('screen_id', 'grid:' + new)
    _scr_sel[0] = 'grid:' + new

# ── Екранна клавіатура (для перейменування) ───────────────────────────────────
_KB_ROWS = ['1234567890', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm_-']

def _kbd_open(title, initial, cb):
    _kbd.update(active=True, text=_slug(initial), title=title, cb=cb)

def draw_keyboard():
    ov = pygame.Surface((C.W, C.H), pygame.SRCALPHA); ov.fill((0, 0, 0, 180)); screen.blit(ov, (0, 0))
    panel = pygame.Rect(140, 150, C.W - 280, 420)
    fill_rect(screen, C.PANEL, panel, radius=16); stroke_rect(screen, C.ACCENT, panel, 2, radius=16)
    text_at(screen, _kbd['title'], FNT_MED, C.TEXT2, panel.centerx, panel.y + 24, 'mc')
    fld = pygame.Rect(panel.x + 40, panel.y + 50, panel.w - 80, 52)
    fill_rect(screen, C.PANEL2, fld, radius=8); stroke_rect(screen, C.BORDER, fld, 1, radius=8)
    text_at(screen, _kbd['text'] + '│', FNT_BIG, C.WHITE, fld.x + 16, fld.centery, 'ml')
    global _kbd_rects; _kbd_rects = []
    ky = panel.y + 120; kw = 66; kh = 54; gap = 8
    for row in _KB_ROWS:
        roww = len(row) * (kw + gap) - gap; kx = panel.centerx - roww // 2
        for ch in row:
            rr = pygame.Rect(kx, ky, kw, kh)
            fill_rect(screen, C.PANEL2, rr, radius=8); stroke_rect(screen, C.BORDER, rr, 1, radius=8)
            text_at(screen, ch, FNT_MED, C.WHITE, rr.centerx, rr.centery, 'mc')
            _kbd_rects.append((ch, rr)); kx += kw + gap
        ky += kh + gap
    # службові
    for lab, act, x0, wdt, col in [('⌫', 'bksp', panel.x + 40, 120, C.ORANGE),
                                    ('Скасувати', 'cancel', panel.centerx - 90, 180, C.RED),
                                    ('OK', 'ok', panel.right - 200, 160, C.GREEN)]:
        rr = pygame.Rect(x0, ky + 6, wdt, 54)
        fill_rect(screen, tuple(int(c * 0.3) for c in col), rr, radius=8); stroke_rect(screen, col, rr, 1, radius=8)
        text_at(screen, lab, FNT_SMALL, col, rr.centerx, rr.centery, 'mc'); _kbd_rects.append((act, rr))

def _kbd_hit(pos):
    for key, rr in _kbd_rects:
        if not rr.collidepoint(pos):
            continue
        if key == 'bksp': _kbd['text'] = _kbd['text'][:-1]
        elif key == 'cancel': _kbd['active'] = False
        elif key == 'ok':
            cb = _kbd['cb']; txt = _kbd['text']; _kbd['active'] = False
            if cb: cb(txt)
        elif len(key) == 1 and len(_kbd['text']) < 24:
            _kbd['text'] += key
        return

# ── Редактор ──────────────────────────────────────────────────────────────────
def _editor_open(path):
    global state
    try:
        lay = GUI.load_layout(path)
    except Exception:
        lay = {'name': 'custom', 'cols': 16, 'rows': 9, 'blocks': []}
    _ed.update(layout=lay, path=path, sel=None, drag=None, palette=False, mpick=False)
    state = State.EDITOR

def _ed_save():
    if _ed['layout'] and _ed['path']:
        GUI.save_layout(_ed['layout'], _ed['path']); _LAYOUT_CACHE.clear()

def _ed_add_block(tkey):
    b = {'type': tkey, 'col': 0, 'row': 0, 'w': 4, 'h': 2, 'p': {}}
    if tkey == 'metric': b['p'] = {'key': 'co2', 'icon': True}
    if tkey == 'gauge': b['p'] = {'key': 'pressure'}
    if tkey in ('pressure_graph',): b.update(w=8, h=4)
    _ed['layout']['blocks'].append(b); _ed['sel'] = len(_ed['layout']['blocks']) - 1

def _ed_action(act):
    global state
    if act == 'exit':
        _ed_save(); _scr_sel[0] = None; state = State.MENU
    elif act == 'palette':
        _ed['palette'] = not _ed['palette']; _ed['mpick'] = False
    elif act == 'grid':
        _ed['showgrid'] = not _ed['showgrid']
    elif act == 'save':
        _ed_save()
    elif act == 'metric':
        _ed['mpick'] = not _ed['mpick']; _ed['palette'] = False
    elif act == 'icon' and _ed['sel'] is not None:
        b = _ed['layout']['blocks'][_ed['sel']]; b.setdefault('p', {})['icon'] = not b['p'].get('icon', False)
    elif act == 'del' and _ed['sel'] is not None:
        _ed['layout']['blocks'].pop(_ed['sel']); _ed['sel'] = None

def _ed_cols_rows():
    lay = _ed['layout']; return lay.get('cols', 16), lay.get('rows', 9)

def _editor_down(pos):
    mx, my = pos
    if _ed['mpick']:
        for k, rr in _ed_mpick_rects:
            if rr.collidepoint(pos):
                _ed['layout']['blocks'][_ed['sel']].setdefault('p', {})['key'] = k
                _ed['mpick'] = False; return
        _ed['mpick'] = False; return
    if _ed['palette']:
        for tkey, rr in _ed_pal_rects:
            if rr.collidepoint(pos):
                _ed_add_block(tkey); _ed['palette'] = False; return
        _ed['palette'] = False; return
    for act, rr in _ed_btns:
        if rr.collidepoint(pos):
            _ed_action(act); return
    if my < 50:
        return
    cols, rows = _ed_cols_rows(); lay = _ed['layout']
    if _ed['sel'] is not None:
        b = lay['blocks'][_ed['sel']]
        br = pygame.Rect(GUI.cell_rect(b['col'], b['row'], b['w'], b['h'], C.W, C.H, cols, rows))
        if pygame.Rect(br.right - 28, br.bottom - 28, 34, 34).collidepoint(pos):
            _ed['drag'] = ('resize', pos, dict(b)); return
    found = None
    for i in range(len(lay['blocks']) - 1, -1, -1):
        b = lay['blocks'][i]
        br = pygame.Rect(GUI.cell_rect(b['col'], b['row'], b['w'], b['h'], C.W, C.H, cols, rows))
        if br.collidepoint(pos):
            found = i; break
    _ed['sel'] = found
    if found is not None:
        _ed['drag'] = ('move', pos, dict(lay['blocks'][found]))

def _editor_motion(pos):
    if not _ed['drag'] or _ed['sel'] is None:
        return False
    mode, (sx, sy), sb = _ed['drag']; cols, rows = _ed_cols_rows()
    cw = C.W / cols; ch = C.H / rows
    dcol = round((pos[0] - sx) / cw); drow = round((pos[1] - sy) / ch)
    b = _ed['layout']['blocks'][_ed['sel']]
    if mode == 'move':
        b['col'] = max(0, min(cols - b['w'], sb['col'] + dcol)); b['row'] = max(0, min(rows - b['h'], sb['row'] + drow))
    else:
        b['w'] = max(1, min(cols - b['col'], sb['w'] + dcol)); b['h'] = max(1, min(rows - b['row'], sb['h'] + drow))
    return True

def _editor_up(pos):
    _ed['drag'] = None

def draw_editor():
    global _ed_btns, _ed_pal_rects, _ed_mpick_rects
    lay = _ed['layout']; cols, rows = _ed_cols_rows()
    GUI.render_layout(screen, lay, _grid_data(), C.W, C.H)
    if _ed['showgrid']:
        for c in range(cols + 1):
            x = int(c * C.W / cols); pygame.draw.line(screen, (40, 50, 70), (x, 0), (x, C.H), 1)
        for r in range(rows + 1):
            y = int(r * C.H / rows); pygame.draw.line(screen, (40, 50, 70), (0, y), (C.W, y), 1)
    if _ed['sel'] is not None and _ed['sel'] < len(lay['blocks']):
        b = lay['blocks'][_ed['sel']]
        br = pygame.Rect(GUI.cell_rect(b['col'], b['row'], b['w'], b['h'], C.W, C.H, cols, rows))
        pygame.draw.rect(screen, C.ACCENT, br, 3, border_radius=8)
        pygame.draw.rect(screen, C.ACCENT, (br.right - 26, br.bottom - 26, 22, 22), border_radius=5)

    # тулбар
    _ed_btns = []
    strip = pygame.Rect(0, 0, C.W, 46)
    ov = pygame.Surface((C.W, 46), pygame.SRCALPHA); ov.fill((13, 20, 36, 235)); screen.blit(ov, (0, 0))
    x = 8
    def tbtn(act, label, col=C.TEXT2, wdt=None):
        nonlocal x
        wdt = wdt or (GUI.font(20).size(label)[0] + 26)
        rr = pygame.Rect(x, 6, wdt, 34)
        fill_rect(screen, C.PANEL2, rr, radius=8); stroke_rect(screen, col, rr, 1, radius=8)
        text_at(screen, label, FNT_SMALL, col, rr.centerx, rr.centery, 'mc')
        _ed_btns.append((act, rr)); x += wdt + 8
    tbtn('exit', '← Вихід', C.RED)
    tbtn('palette', '＋ Блок', C.GREEN)
    tbtn('grid', 'Сітка', C.CYAN)
    tbtn('save', '🖫 Зберегти', C.GREEN)
    if _ed['sel'] is not None and _ed['sel'] < len(lay['blocks']):
        bt = lay['blocks'][_ed['sel']]['type']
        text_at(screen, '│ ' + GUI.BLOCKS.get(bt, (bt,))[0], FNT_SMALL, C.MUTED, x + 4, 23, 'ml'); x += GUI.font(21).size(GUI.BLOCKS.get(bt,(bt,))[0])[0] + 30
        if bt in ('metric', 'gauge'):
            tbtn('metric', 'Показник ▾', C.BLUE)
        if bt == 'metric':
            tbtn('icon', 'Іконка', C.PURPLE)
        tbtn('del', '🗑 Видалити', C.RED)

    # палітра
    if _ed['palette']:
        _ed_pal_rects = []
        pw = 320; pr = pygame.Rect(C.W - pw, 48, pw, C.H - 48)
        fill_rect(screen, C.PANEL, pr, radius=0); stroke_rect(screen, C.BORDER, pr, 1)
        text_at(screen, 'Додати блок', FNT_MED, C.WHITE, pr.x + 16, 60, 'tl')
        yy = 96
        for tkey, (label, _fn) in GUI.BLOCKS.items():
            rr = pygame.Rect(pr.x + 12, yy, pw - 24, 40)
            fill_rect(screen, C.PANEL2, rr, radius=8); stroke_rect(screen, C.BORDER, rr, 1, radius=8)
            text_at(screen, label, FNT_SMALL, C.TEXT2, rr.x + 12, rr.centery, 'ml')
            text_at(screen, '＋', FNT_MED, C.GREEN, rr.right - 24, rr.centery, 'mc')
            _ed_pal_rects.append((tkey, rr)); yy += 46

    # вибір показника
    if _ed['mpick'] and _ed['sel'] is not None:
        _ed_mpick_rects = []
        mr = pygame.Rect(C.W // 2 - 330, 120, 660, 380)
        fill_rect(screen, C.PANEL, mr, radius=16); stroke_rect(screen, C.ACCENT, mr, 2, radius=16)
        text_at(screen, 'Показник блока', FNT_MED, C.WHITE, mr.centerx, mr.y + 22, 'mc')
        cur = _ed['layout']['blocks'][_ed['sel']].get('p', {}).get('key')
        for i, k in enumerate(_ED_METRICS):
            rr = pygame.Rect(mr.x + 24 + (i % 3) * 210, mr.y + 60 + (i // 3) * 62, 196, 52)
            sel = k == cur
            fill_rect(screen, C.ACCENT_D if sel else C.PANEL2, rr, radius=9); stroke_rect(screen, C.ACCENT if sel else C.BORDER, rr, 2 if sel else 1, radius=9)
            text_at(screen, GUI.META.get(k, (k,))[0], FNT_SMALL, C.WHITE if sel else C.TEXT2, rr.centerx, rr.centery, 'mc')
            _ed_mpick_rects.append((k, rr))

    text_at(screen, 'Тап — вибрати · тягни — рух · кут — розмір', FNT_TINY, C.MUTED, C.W // 2, C.H - 8, 'bc')
    if _kbd['active']:
        draw_keyboard()
    pygame.display.update()

# ── Вкладка «Екрани»: список + керування кастомними ───────────────────────────
def _draw_menu_screens_v14():
    x, y = 150, 110
    text_at(screen, T('car_title'), FNT_SMALL, C.TEXT2, x, y, 'tl')
    text_at(screen, T('car_hint'), FNT_TINY, C.MUTED, x, y + 24, 'tl')
    _v14_button('scrnew', (C.W - 230, y - 6, 210, 44), C.GREEN, '＋ Новий екран')
    enabled = _car_list(); avail = _car_available()
    ordered = enabled + [s for s in avail if s not in enabled]
    cur = db.get_setting('screen_id', 'air')
    row_y = y + 52
    for sid in ordered[:8]:
        on = sid in enabled; is_grid = sid.startswith('grid:')
        rect = pygame.Rect(x, row_y, 1000, 46)
        fill_rect(screen, C.PANEL2 if on else C.PANEL, rect, radius=10)
        stroke_rect(screen, C.ACCENT if sid == cur else C.BORDER, rect, 2 if sid == cur else 1, radius=10)
        cb = pygame.Rect(x + 12, row_y + 10, 26, 26)
        _menu_buttons_v14.append(('carsel:' + sid, pygame.Rect(x, row_y, 48, 46)))
        fill_rect(screen, C.ACCENT_D if on else C.PANEL, cb, radius=6); stroke_rect(screen, C.GREEN if on else C.BORDER, cb, 2 if on else 1, radius=6)
        if on: text_at(screen, '✓', FNT_SMALL, C.GREEN, cb.centerx, cb.centery, 'mc')
        # назва — для кастомних це кнопка керування
        name_rect = pygame.Rect(x + 50, row_y, 460, 46)
        if is_grid:
            _menu_buttons_v14.append(('scrmgr:' + sid, name_rect))
        text_at(screen, _screen_name(sid), FNT_MED, C.WHITE if on else C.MUTED, x + 56, row_y + 23, 'ml')
        text_at(screen, f"({T('car_custom') if is_grid else T('car_builtin')})", FNT_TINY,
                C.ACCENT if is_grid else C.MUTED, x + 320, row_y + 23, 'ml')
        if sid == cur: text_at(screen, '●', FNT_SMALL, C.ACCENT, x + 470, row_y + 23, 'ml')
        if is_grid:
            _v14_button('scredit:' + sid, (x + 700, row_y + 6, 96, 34), C.BLUE, '✎ Редаг.')
        if on:
            up = pygame.Rect(x + 902, row_y + 6, 44, 34); dn = pygame.Rect(x + 950, row_y + 6, 44, 34)
            _menu_buttons_v14.append(('carup:' + sid, up)); _menu_buttons_v14.append(('cardn:' + sid, dn))
            fill_rect(screen, C.PANEL, up, radius=7); stroke_rect(screen, C.BORDER, up, 1, radius=7)
            fill_rect(screen, C.PANEL, dn, radius=7); stroke_rect(screen, C.BORDER, dn, 1, radius=7)
            text_at(screen, '▲', FNT_SMALL, C.ACCENT, up.centerx, up.centery, 'mc'); text_at(screen, '▼', FNT_SMALL, C.ACCENT, dn.centerx, dn.centery, 'mc')
        row_y += 52

    # панель дій для вибраного кастомного екрана
    if _scr_sel[0] and _scr_sel[0].startswith('grid:'):
        sid = _scr_sel[0]
        bar = pygame.Rect(150, C.H - 78, C.W - 300, 60)
        fill_rect(screen, C.PANEL, bar, radius=12); stroke_rect(screen, C.ACCENT, bar, 2, radius=12)
        text_at(screen, _screen_name(sid) + ':', FNT_SMALL, C.TEXT2, bar.x + 16, bar.centery, 'ml')
        _v14_button('scredit:' + sid, (bar.x + 180, bar.y + 10, 150, 40), C.BLUE, '✎ Редагувати')
        _v14_button('scrdup:' + sid, (bar.x + 338, bar.y + 10, 150, 40), C.GREEN, '⧉ Дублювати')
        _v14_button('scrren:' + sid, (bar.x + 496, bar.y + 10, 170, 40), C.PURPLE, '✎ Перейменувати')
        _v14_button('scrdel:' + sid, (bar.x + 674, bar.y + 10, 130, 40), C.RED, '🗑 Видалити')
        _v14_button('scrclose', (bar.right - 120, bar.y + 10, 100, 40), C.MUTED, 'Закрити')


# ══════════════════════════════════════════════════════════════════════════════
#  v24 — OTA-оновлення з GitHub: кнопка «Оновити» на екрані «Про пристрій».
# ══════════════════════════════════════════════════════════════════════════════

_ota = {'busy': False, 'status': '', 'local': updater.local_version(),
        'remote': None, 'avail': False, 'checked': False, 'buttons': []}

def _ota_check():
    def _w():
        _ota['busy'] = True; _ota['status'] = T('upd_checking')
        avail, lv, rv = updater.update_available()
        _ota['local'] = lv; _ota['remote'] = rv; _ota['avail'] = avail; _ota['checked'] = True
        if rv is None:
            _ota['status'] = T('upd_offline') if not _net_online() else 'Репо ще без VERSION'
        elif avail:
            _ota['status'] = f"{T('upd_avail')}: {rv}"
        else:
            _ota['status'] = T('upd_uptodate')
        _ota['busy'] = False
    threading.Thread(target=_w, daemon=True).start()

def _ota_apply(what='all'):
    def _w():
        _ota['busy'] = True
        ok, msg = updater.download_and_apply(what, log=lambda m: _ota.__setitem__('status', m))
        _ota['status'] = msg; _ota['busy'] = False
        if ok and what == 'all':
            _ota['status'] = 'Оновлено ✓ Перезавантаження…'
            time.sleep(1.5)
            try:
                poller.stop()
            except Exception:
                pass
            try:
                pygame.quit()
            except Exception:
                pass
            updater.reboot()
    threading.Thread(target=_w, daemon=True).start()

def _ota_auto_check_on_open():
    # Завжди свіжа перевірка при вході на екран (без кешу).
    if not _ota['busy']:
        _ota_check()

def draw_about_screen():
    _screen_header(T('about_title'), T('about_sub'))
    _ota['buttons'] = []
    # ── картка оновлення (фіксована, над прокруткою) ──
    uc = pygame.Rect(150, 92, C.W - 300, 88)
    fill_rect(screen, C.PANEL, uc, radius=12); stroke_rect(screen, C.ACCENT if _ota['avail'] else C.BORDER, uc, 2 if _ota['avail'] else 1, radius=12)
    text_at(screen, T('upd_title'), FNT_SMALL, C.TEXT2, uc.x + 18, uc.y + 12, 'tl')
    text_at(screen, f"{T('upd_version')}: {_ota['local']}" + (f"  →  {_ota['remote']}" if _ota['avail'] else ''),
            FNT_MED, C.WHITE, uc.x + 18, uc.y + 46, 'ml')
    scol = C.GREEN if _ota['avail'] else (C.YELLOW if _ota['busy'] else C.MUTED)
    text_at(screen, _ota['status'], FNT_TINY, scol, uc.x + 18, uc.y + 70, 'ml')
    # кнопки
    bx = uc.right - 18
    def ubtn(act, label, col):
        nonlocal bx
        w = GUI.font(21).size(label)[0] + 34
        rr = pygame.Rect(bx - w, uc.y + 22, w, 44); bx -= w + 10
        fill_rect(screen, tuple(int(c * 0.3) for c in col), rr, radius=9); stroke_rect(screen, col, rr, 1, radius=9)
        text_at(screen, label, FNT_SMALL, col, rr.centerx, rr.centery, 'mc')
        _ota['buttons'].append((act, rr))
    if _ota['busy']:
        text_at(screen, '⏳', FNT_BIG, C.YELLOW, bx - 30, uc.centery, 'mc')
    else:
        if _ota['avail']:
            ubtn('apply', f"{T('upd_now')} {_ota['remote']}", C.GREEN)
        ubtn('layouts', T('upd_layouts'), C.CYAN)
        ubtn('check', T('upd_check'), C.BLUE)

    # ── прокручуваний текст довідки (нижче картки) ──
    lines = i18n.about_lines()
    def step(kind): return 40 if kind in ('h', 'w') else 33
    total = sum(step(k) if txt else 14 for txt, k in lines)
    top = 196; view_h = C.H - top - 20
    maxscroll = max(0, total - view_h)
    _about_scroll[0] = max(0, min(_about_scroll[0], maxscroll))
    x = 150; y = top - _about_scroll[0]
    prev = screen.get_clip(); screen.set_clip(pygame.Rect(0, top, C.W, view_h))
    for txt, kind in lines:
        if txt == '':
            y += 14; continue
        col, f, st = (C.CYAN, FNT_MED, 40) if kind == 'h' else \
                     (C.ORANGE, FNT_MED, 40) if kind == 'w' else (C.TEXT2, FNT_SMALL, 33)
        if top - 40 < y < C.H:
            text_at(screen, txt, f, col, x, y, 'tl')
        y += st
    screen.set_clip(prev)
    if maxscroll > 0:
        bar_h = int(view_h * view_h / total); bar_y = top + int((view_h - bar_h) * _about_scroll[0] / maxscroll)
        fill_rect(screen, C.PANEL2, (C.W - 12, top, 6, view_h), radius=3)
        fill_rect(screen, C.ACCENT, (C.W - 12, bar_y, 6, bar_h), radius=3)
        fill_rect(screen, C.PANEL2, (C.W - 74, C.H - 108, 56, 48), radius=10); stroke_rect(screen, C.BORDER, (C.W - 74, C.H - 108, 56, 48), 1, radius=10)
        text_at(screen, '▲', FNT_BIG, C.ACCENT, C.W - 46, C.H - 84, 'mc')
        fill_rect(screen, C.PANEL2, (C.W - 74, C.H - 56, 56, 48), radius=10); stroke_rect(screen, C.BORDER, (C.W - 74, C.H - 56, 56, 48), 1, radius=10)
        text_at(screen, '▼', FNT_BIG, C.ACCENT, C.W - 46, C.H - 32, 'mc')
    pygame.display.update()

def _about_hit(pos):
    """Обробка кліків на екрані «Про пристрій». Повертає True, якщо влучили."""
    for act, rr in _ota.get('buttons', []):
        if rr.collidepoint(pos) and not _ota['busy']:
            if act == 'check': _ota_check()
            elif act == 'apply': _ota_apply('all')
            elif act == 'layouts': _ota_apply('layouts')
            return True
    if pygame.Rect(C.W - 74, C.H - 108, 56, 48).collidepoint(pos):
        _about_scroll[0] = max(0, _about_scroll[0] - 120); return True
    if pygame.Rect(C.W - 74, C.H - 56, 56, 48).collidepoint(pos):
        _about_scroll[0] += 120; return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  v28 — примусове оновлення астро-даних + деталі планети по тапу (Крок 2).
# ══════════════════════════════════════════════════════════════════════════════

_astro_detail = [None]   # ім'я планети для оверлея деталей, або None

def _astro_do_refresh():
    try:
        net.force_refresh()
    except Exception:
        pass

def _draw_planet_detail(name):
    now = datetime.now()
    det = None
    for d in wx.retrograde_detail(now, i18n.get_lang()):
        if d['planet'] == name:
            det = d; break
    # затемнення + картка
    ov = pygame.Surface((C.W, C.H), pygame.SRCALPHA); ov.fill((6, 8, 18, 200)); screen.blit(ov, (0, 0))
    card = pygame.Rect(200, 90, C.W - 400, C.H - 180)
    fill_rect(screen, C.PANEL, card, radius=18); stroke_rect(screen, C.PURPLE, card, 2, radius=18)
    x = card.x + 40; y = card.y + 30
    glyph = det['glyph'] if det else '•'
    text_at(screen, glyph, _wf(64), C.PURPLE, x, y, 'tl')
    text_at(screen, name, _wf(40, True), C.WHITE, x + 90, y + 6, 'tl')
    text_at(screen, 'ретроградний рух' if det else 'прямий рух', _wf(18), C.CYAN, x + 92, y + 52, 'tl')
    if det:
        yy = y + 110
        text_at(screen, f"Період: {det['start']} – {det['end']}", _wf(22), C.TEXT2, x, yy, 'tl'); yy += 34
        text_at(screen, f"Залишилось: {det['days_left']} днів  ·  фаза: {det['phase']}", _wf(22), C.TEXT2, x, yy, 'tl'); yy += 40
        # прогрес-смуга періоду
        bw = card.w - 80
        fill_rect(screen, C.PANEL2, (x, yy, bw, 16), radius=8)
        fill_rect(screen, C.PURPLE, (x, yy, int(bw * det['progress']), 16), radius=8)
        text_at(screen, 'початок', _wf(13), C.MUTED, x, yy + 24, 'tl')
        text_at(screen, 'кінець', _wf(13), C.MUTED, x + bw, yy + 24, 'tr'); yy += 60
        text_at(screen, 'Сфера впливу:', _wf(18, True), C.YELLOW, x, yy, 'tl'); yy += 28
        for ln in _wrap_text(det.get('area', ''), _wf(20), card.w - 80):
            text_at(screen, ln, _wf(20), C.TEXT2, x, yy, 'tl'); yy += 28
        yy += 8
        text_at(screen, 'Як поводитись:', _wf(18, True), C.GREEN, x, yy, 'tl'); yy += 28
        for ln in _wrap_text(det.get('advice', ''), _wf(20), card.w - 80):
            text_at(screen, ln, _wf(20), C.TEXT2, x, yy, 'tl'); yy += 28
    else:
        text_at(screen, 'Зараз ця планета в прямому русі.', _wf(22), C.TEXT2, x, y + 120, 'tl')
    text_at(screen, '* астрологічні трактування — традиція, для настрою; тап — закрити',
            _wf(14), C.MUTED, card.centerx, card.bottom - 22, 'mc')

def _wrap_text(txt, fnt, max_w):
    words = (txt or '').split(' '); lines = ['']
    for w in words:
        if fnt.size((lines[-1] + ' ' + w).strip())[0] > max_w:
            lines.append(w)
        else:
            lines[-1] = (lines[-1] + ' ' + w).strip()
    return [l for l in lines if l]


if __name__ == '__main__':
    main()
