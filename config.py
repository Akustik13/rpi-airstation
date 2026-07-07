"""config.py — constants, thresholds, colour zones."""

# Display
W, H = 1280, 720
FPS  = 5

# Colours (pygame RGB) — глибокий синьо-графітовий фон, світліший читабельний текст
BG       = ( 10,  15, 28)
SIDEBAR  = ( 13,  20, 36)   # панель навігації, трохи світліша за фон
PANEL    = ( 21,  30, 50)
PANEL2   = ( 33,  45, 70)
BORDER   = ( 64,  78, 104)
TEXT     = (238, 244, 255)
TEXT2    = (200, 212, 232)  # яскравіше — головна скарга була на читабельність
MUTED    = (132, 148, 174)
WHITE    = (255, 255, 255)

GREEN    = ( 52, 211, 153)
GREEN_D  = (  8,  60,  46)
YELLOW   = (250, 204,  21)
YELLOW_D = ( 92,  70,   8)
ORANGE   = (251, 146,  60)
ORANGE_D = ( 90,  45,  10)
RED      = (248, 113, 113)
RED_D    = ( 96,  22,  22)
CYAN     = ( 34, 211, 238)
PURPLE   = (167, 139, 250)
BLUE     = ( 96, 165, 250)

ACCENT   = ( 96, 165, 250)
ACCENT_D = ( 20,  55, 105)  # заливка активних кнопок/вкладок

# Layout splits
LEFT_W   = 220   # 7 inch landscape sidebar
DIVIDER  = 2
RIGHT_X  = LEFT_W + DIVIDER
RIGHT_W  = W - RIGHT_X

# Bar chart sizing
BAR_COLS = 7           # CO2, VOC, NOx, PM1, PM2.5, PM4, PM10
BAR_GAP  = 4
BAR_W    = (RIGHT_W - BAR_GAP*(BAR_COLS+1)) // BAR_COLS  # ~34px
BAR_TOP  = 52          # leave room for legend/title/value
BAR_BOT  = H - 44      # leave room for channel name + unit
BAR_H    = BAR_BOT - BAR_TOP

# Sensor keys → display metadata
CHANNELS = {
    'co2': {
        'label': 'CO₂', 'unit': 'ppm',
        'color': (  6,182,212),
        'max':   2000,
        'zones': [(600,'Good',GREEN),(1000,'Mod.',YELLOW),(1500,'Poor',ORANGE),(2000,'Bad',RED)],
        'group': 'air',
    },
    'voc_index': {
        'label': 'VOC', 'unit': 'idx',
        'color': (139, 92,246),
        'max':   500,
        'zones': [(100,'Good',GREEN),(200,'Mod.',YELLOW),(300,'Poor',ORANGE),(500,'Bad',RED)],
        'group': 'air',
    },
    'nox_index': {
        'label': 'NOx', 'unit': 'idx',
        'color': (249,115, 22),
        'max':   500,
        'zones': [( 50,'Good',GREEN),(150,'Mod.',YELLOW),(300,'Poor',ORANGE),(500,'Bad',RED)],
        'group': 'air',
    },
    'pm1_0': {
        'label': 'PM1', 'unit': 'µg',
        'color': ( 96,165,250),
        'max':   100,
        'zones': [(10,'Good',GREEN),(20,'Mod.',YELLOW),(35,'Poor',ORANGE),(100,'Bad',RED)],
        'group': 'pm',
    },
    'pm2_5': {
        'label': 'PM2.5','unit': 'µg',
        'color': ( 52,211,153),
        'max':   150,
        'zones': [(15,'Good',GREEN),(25,'Mod.',YELLOW),(50,'Poor',ORANGE),(150,'Bad',RED)],
        'group': 'pm',
    },
    'pm4_0': {
        'label': 'PM4', 'unit': 'µg',
        'color': (167,139,250),
        'max':   200,
        'zones': [(30,'Good',GREEN),(55,'Mod.',YELLOW),(80,'Poor',ORANGE),(200,'Bad',RED)],
        'group': 'pm',
    },
    'pm10': {
        'label': 'PM10','unit': 'µg',
        'color': (251,191, 36),
        'max':   300,
        'zones': [(45,'Good',GREEN),(75,'Mod.',YELLOW),(100,'Poor',ORANGE),(300,'Bad',RED)],
        'group': 'pm',
    },
}
BAR_KEYS = list(CHANNELS.keys())  # ordered: CO2,VOC,NOx,PM1,PM2.5,PM4,PM10

# Left panel channels — only ONE displayed temperature, source selected in Menu
# Internal sensor values temp_scd/temp_bmp are still stored and can be used by mapping.
LEFT_CHANNELS = [
    ('temperature', '°C',  'Temperature', ORANGE),
    ('humidity',    '%',   'Humidity',    CYAN),
    ('pressure',    'hPa', 'Pressure',    PURPLE),
]

# Default source mapping for display fields. Can be changed in Menu.
DEFAULT_SOURCE_MAP = {
    'temperature': 'bmp280',   # 'bmp280' or 'scd41'
    'humidity':    'scd41',
    'pressure':    'bmp280',
    'co2':         'scd41',
}


POLL_INTERVAL = 5   # seconds

def zone_color(key, val):
    """Return fill colour for a bar based on value thresholds."""
    ch = CHANNELS.get(key)
    if not ch: return GREEN
    for limit, label, col in ch['zones']:
        if val <= limit: return col
    return RED

def lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a + (b-a)*t) for a,b in zip(c1,c2))

def gradient_color(key, val):
    """Smooth gradient green→yellow→orange→red."""
    ch = CHANNELS.get(key)
    if not ch or not ch['zones']: return GREEN
    zones = ch['zones']
    prev_lim, prev_col = 0, GREEN
    for limit, label, col in zones:
        if val <= limit:
            t = (val - prev_lim) / max(limit - prev_lim, 1)
            return lerp_color(prev_col, col, t)
        prev_lim, prev_col = limit, col
    return RED

FIX_VERSION = "v15_pm_bars_bottom_anchor_zone_colors_threshold_notches"
