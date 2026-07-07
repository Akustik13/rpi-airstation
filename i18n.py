"""i18n.py — двомовність UI: українська (uk) та англійська (en).

Мова зберігається в налаштуваннях (db, ключ 'lang') і перемикається
на екрані Налаштування → Головні. Використання: from i18n import T, set_lang.
"""

LANG = ['uk']   # поточна мова, мутабельний контейнер

def set_lang(code):
    LANG[0] = 'en' if code == 'en' else 'uk'

def get_lang():
    return LANG[0]

_TR = {
    # ── Навігація / шапки ────────────────────────────────────────────────
    'nav_home':      ('Головна', 'Home'),
    'nav_graphs':    ('Графіки', 'Graphs'),
    'nav_data':      ('Дані', 'Data'),
    'nav_settings':  ('Налаштування', 'Settings'),
    'nav_about':     ('Про пристрій', 'About'),
    'nav_exit':      ('Вийти', 'Exit'),
    'home_title':    ('ГОЛОВНА', 'HOME'),
    'online':        ('Онлайн', 'Online'),
    'offline':       ('Офлайн', 'Offline'),
    'back':          ('← Назад', '← Back'),

    # ── Картки головного екрана ──────────────────────────────────────────
    'temp':          ('Температура', 'Temperature'),
    'humidity':      ('Вологість', 'Humidity'),
    'pressure':      ('Тиск', 'Pressure'),
    'pm_card':       ('Частинки (µg/m³)', 'Particles (µg/m³)'),
    'status_good':   ('Добре', 'Good'),
    'status_mod':    ('Помірно', 'Moderate'),
    'status_bad':    ('Погано', 'Poor'),
    'status_vent':   ('Провітрити', 'Ventilate'),
    'stable':        ('→ стабільно', '→ stable'),
    'last_update':   ('Останнє оновлення:', 'Last update:'),
    'poll_int':      ('Опитування:', 'Polling:'),
    'sec':           ('сек', 's'),
    'main_graph':    ('Графік:', 'Main chart:'),
    'hour_short':    ('год', 'h'),
    'not_enough':    ('Ще недостатньо даних', 'Not enough data yet'),

    # ── Назви каналів / заголовки графіків ───────────────────────────────
    'lbl_co2':         ('CO₂', 'CO₂'),
    'lbl_voc_index':   ('VOC Index', 'VOC Index'),
    'lbl_nox_index':   ('NOx Index', 'NOx Index'),
    'lbl_pm2_5':       ('PM2.5', 'PM2.5'),
    'lbl_pm10':        ('PM10', 'PM10'),
    'lbl_temperature': ('Температура', 'Temperature'),
    'lbl_humidity':    ('Вологість', 'Humidity'),
    'lbl_pressure':    ('Тиск', 'Pressure'),
    'gt_co2':          ('Графік: CO₂ (ppm)', 'Chart: CO₂ (ppm)'),
    'gt_voc_index':    ('Графік: VOC Index', 'Chart: VOC Index'),
    'gt_nox_index':    ('Графік: NOx Index', 'Chart: NOx Index'),
    'gt_pm2_5':        ('Графік: PM2.5 (µg/m³)', 'Chart: PM2.5 (µg/m³)'),
    'gt_pm10':         ('Графік: PM10 (µg/m³)', 'Chart: PM10 (µg/m³)'),
    'gt_temperature':  ('Графік: температура (°C)', 'Chart: temperature (°C)'),
    'gt_humidity':     ('Графік: вологість (%)', 'Chart: humidity (%)'),
    'gt_pressure':     ('Графік: тиск (hPa)', 'Chart: pressure (hPa)'),

    # ── Екран "Графіки" ──────────────────────────────────────────────────
    'graphs_title':  ('ГРАФІКИ', 'GRAPHS'),
    'graphs_sub':    ('Вибери, який графік побудувати', 'Choose which chart to open'),
    'graphs_hint':   ('Натисни картку — відкриється повний графік.', 'Tap a card to open the full chart.'),

    # ── Екран "Дані" ─────────────────────────────────────────────────────
    'data_title':    ('ДАНІ', 'DATA'),
    'data_sub':      ('Поточні сирі значення з датчиків і статус підключення',
                      'Current raw sensor readings and connection status'),
    'd_bmp':         ('BMP280 / барометр', 'BMP280 / barometer'),
    'd_scd':         ('SCD41 / CO₂', 'SCD41 / CO₂'),
    'd_sgp':         ('SGP41 / гази', 'SGP41 / gases'),
    'd_sps':         ('SPS30 / частинки', 'SPS30 / particles'),
    'r_temp_bmp':    ('Температура BMP', 'BMP temperature'),
    'r_temp_scd':    ('Температура SCD', 'SCD temperature'),
    'r_hum_scd':     ('Вологість SCD', 'SCD humidity'),

    # ── Екран "Про пристрій" ─────────────────────────────────────────────
    'about_title':   ('ПРО ПРИСТРІЙ', 'ABOUT'),
    'about_sub':     ('Сенсори, показники та коротка довідка',
                      'Sensors, metrics and a short reference'),

    # ── Налаштування ─────────────────────────────────────────────────────
    'menu_title':    ('НАЛАШТУВАННЯ', 'SETTINGS'),
    'menu_sub':      ('Вкладки: головні / сенсори / час', 'Tabs: general / sensors / time'),
    'tab_general':   ('Головні', 'General'),
    'tab_sensors':   ('Сенсори', 'Sensors'),
    'tab_time':      ('Час', 'Time'),
    'card_poll':     ('Опитування та шина I²C', 'Polling & I²C bus'),
    'card_window':   ('Вікно графіка', 'Chart window'),
    'card_maingraph':('Графік на головному екрані', 'Chart on the home screen'),
    'card_tempsrc':  ('Джерело температури', 'Temperature source'),
    'card_lang':     ('Мова / Language', 'Мова / Language'),
    'save':          ('Зберегти', 'Save'),
    'saved':         ('Збережено', 'Saved'),
    'maingraph_hint':('Це змінює тільки великий графік на головному екрані.',
                      'This only changes the big chart on the home screen.'),
    'sens_status':   ('Статус сенсорів', 'Sensor status'),
    'col_sensor':    ('Сенсор', 'Sensor'),
    'col_addr':      ('Адреса', 'Addr'),
    'col_status':    ('Статус', 'Status'),
    'col_last':      ('Останнє чит.', 'Last read'),
    'col_error':     ('Помилка', 'Error'),
    'time_card':     ('Час', 'Time'),
    'time_hint':     ('Network: системний час Raspberry Pi / NTP. Manual: задай вручну нижче.',
                      'Network: Raspberry Pi system time / NTP. Manual: set it below.'),
    'apply_time':    ('Застосувати час', 'Apply manual time'),

    # ── Вкладка "Вигляд" ─────────────────────────────────────────────────
    'tab_display':      ('Вигляд', 'Display'),
    'card_autohide':    ('Автоприховування панелі', 'Auto-hide panels'),
    'card_hidesec':     ('Сховати через (сек)', 'Hide after (sec)'),
    'card_header':      ('Верхній напис', 'Top header'),
    'card_mgstyle':     ('Стиль графіка на головному', 'Home chart style'),
    'card_metricstyle': ('CO₂ / VOC / NOx у боксах', 'CO₂ / VOC / NOx boxes'),
    'opt_on':           ('Увімк', 'On'),
    'opt_off':          ('Вимк', 'Off'),
    'opt_show':         ('Показувати', 'Show'),
    'opt_hide':         ('Ховати', 'Hide'),
    'style_bars':       ('Стовпчики', 'Bars'),
    'style_gauge':      ('Аналоговий', 'Gauge'),
    'style_gradient':   ('Градієнт-шкала', 'Gradient bar'),
    'display_hint':     ('Панель і напис ховаються самі. Торкнись екрана або проведи зліва направо, щоб показати.',
                         'Panels auto-hide. Tap the screen or swipe left→right to show them.'),
    'tap_to_show':      ('Торкнись, щоб показати панель', 'Tap to show the panel'),

    # ── Вкладка "Калібр." (калібровка + одиниці + тиск) ──────────────────
    'tab_calib':        ('Калібр.', 'Calib.'),
    'card_calib':       ('Калібровка (зсув до показів)', 'Calibration (offset to readings)'),
    'card_units':       ('Одиниці', 'Units'),
    'card_presmode':    ('Тиск', 'Pressure'),
    'cal_temp':         ('Температура', 'Temperature'),
    'cal_hum':          ('Вологість', 'Humidity'),
    'cal_pres':         ('Тиск', 'Pressure'),
    'cal_co2':          ('CO₂', 'CO₂'),
    'cal_voc':          ('VOC', 'VOC'),
    'cal_nox':          ('NOx', 'NOx'),
    'unit_temp':        ('Температура', 'Temperature'),
    'unit_pres':        ('Тиск', 'Pressure'),
    'pres_abs':         ('Станційний', 'Station'),
    'pres_sea':         ('До рівня моря', 'Sea level'),
    'altitude':         ('Висота (м)', 'Altitude (m)'),

    # ── Режим екрана + бокси ─────────────────────────────────────────────
    'card_screen':      ('Режим екрана', 'Screen mode'),
    'screen_air':       ('Станція повітря', 'Air station'),
    'screen_wx':        ('Метеостанція', 'Weather station'),
    'card_boxes':       ('Показники у боксах', 'Box metrics'),
    'boxes_hint':       ('Обери три показники для верхніх боксів головного екрана.',
                         'Pick three metrics for the top boxes on the home screen.'),

    # ── Метео / погодна станція ──────────────────────────────────────────
    'wx_indoor':        ('У приміщенні', 'Indoor'),
    'wx_outdoor':       ('Надворі', 'Outdoor'),
    'wx_ble_wait':      ('Датчик BLE не підключено', 'BLE sensor not connected'),
    'wx_forecast':      ('Прогноз', 'Forecast'),
    'wx_sunrise':       ('Схід', 'Sunrise'),
    'wx_sunset':        ('Захід', 'Sunset'),
    'wx_moon':          ('Місяць', 'Moon'),
    'wx_pressure':      ('Тиск', 'Pressure'),
    'wx_co2':           ('CO₂', 'CO₂'),

    # ── Похідні показники ────────────────────────────────────────────────
    'lbl_eco2':         ('eCO₂*', 'eCO₂*'),
    'lbl_aqi':          ('AQI', 'AQI'),
    'lbl_iaq':          ('IAQ*', 'IAQ*'),
    'lbl_pm2_5':        ('PM2.5', 'PM2.5'),
    'lbl_pm10':         ('PM10', 'PM10'),
    'lbl_temperature':  ('Темп.', 'Temp.'),
    'lbl_humidity':     ('Волог.', 'Humidity'),

    # ── v19 меню ─────────────────────────────────────────────────────────
    'tab_boxes':     ('Бокси', 'Boxes'),
    'card_design':   ('Дизайн за замовчуванням', 'Default screen'),
    'card_netmark':  ('Позначати дані з інтернету', 'Mark internet data'),
    'card_outdoor':  ('Панель «Надворі»', 'Outdoor panel'),
    'out_ble':       ('BLE (заглушка)', 'BLE (stub)'),
    'out_co2':       ('CO₂ замість BLE', 'CO₂ instead'),
    'out_off':       ('Порожньо', 'Empty'),
    'card_location': ('Локація', 'Location'),
    'choose':        ('Обрати ▾', 'Choose ▾'),
    'design_hint':   ('Перелистуй екрани свайпом ліворуч/праворуч. Панель — тап біля лівого краю.',
                      'Swipe left/right to switch screens. Tap the left edge for the panel.'),
    'tab_screens':   ('Екрани', 'Screens'),
    'car_title':     ('Екрани в каруселі перелистування', 'Carousel screens'),
    'car_hint':      ('Галочка — показувати при свайпі. ▲▼ — порядок. Кастомні (grid) — з редактора.',
                      'Check to include in swipe. ▲▼ — order. Custom (grid) come from the editor.'),
    'car_builtin':   ('вбудований', 'built-in'),
    'car_custom':    ('кастомний', 'custom'),
    'upd_title':     ('Оновлення з GitHub', 'GitHub update'),
    'upd_check':     ('Перевірити', 'Check'),
    'upd_now':       ('Оновити', 'Update'),
    'upd_layouts':   ('Оновити дизайни', 'Update designs'),
    'upd_version':   ('Версія', 'Version'),
    'upd_checking':  ('Перевірка…', 'Checking…'),
    'upd_uptodate':  ('Актуальна версія', 'Up to date'),
    'upd_avail':     ('Доступно оновлення', 'Update available'),
    'upd_offline':   ('Немає інтернету', 'No internet'),
}

_WEEKDAYS = {
    'uk': ['Понеділок','Вівторок','Середа','Четвер','П’ятниця','Субота','Неділя'],
    'en': ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'],
}

_ABOUT_LINES = {
    'uk': [
        ('Сенсори', 'h'),
        ('BMP280/BMP390: температура та атмосферний тиск.', 't'),
        ('SCD41: справжній CO₂, температура, вологість.', 't'),
        ('SGP41: VOC Index та NOx Index (індекси Sensirion, не ppb).', 't'),
        ('SPS30: частинки PM1.0, PM2.5, PM4.0, PM10 у µg/m³.', 't'),
        ('', 't'),
        ('Що показує головний екран', 'h'),
        ('CO₂: вентиляція. Понад 800 ppm — бажано провітрити.', 't'),
        ('VOC Index: леткі органічні сполуки — запахи, хімія, кухня.', 't'),
        ('NOx Index: оксиди азоту — газова плита, вулиця, дим.', 't'),
        ('PM2.5/PM10: пил та дрібні частинки.', 't'),
        ('Насічки на графіках: жовта — межа норми, червона — високий рівень.', 't'),
        ('', 't'),
        ('Похідні показники (можна ввімкнути в боксах)', 'h'),
        ('AQI: індекс якості повітря US EPA за PM2.5/PM10. 0–50 добре, 51–100 помірно, >100 шкідливо.', 't'),
        ('IAQ*: складений indoor-індекс із CO₂, VOC та PM2.5. Це оцінка, а не сертифікований сенсор.', 't'),
        ('eCO₂*: оцінка еквівалентного CO₂ за VOC-навантаженням. Реальний CO₂ дає SCD41.', 't'),
        ('Зірочка* означає розрахункову величину, а не прямий вимір.', 't'),
        ('', 't'),
        ('Прогноз погоди', 'h'),
        ('Рахується за трендом тиску за ~3 год: різке падіння → буря, зростання → ясно.', 't'),
        ('', 't'),
        ('Блок «Астро»', 'h'),
        ('Магнітні бурі — Kp-індекс з NOAA (0–9, буря від Kp5). Фаза місяця — офлайн.', 't'),
        ('Ретроград планет — за вшитою таблицею (наближено). Порада дня — за умовами.', 't'),
        ('', 't'),
        ('Екрани та жести', 'h'),
        ('Свайп ліворуч/праворуч — перемикання дизайнів (карусель).', 't'),
        ('Тап біля лівого краю — показати/сховати бокову панель.', 't'),
        ('Кастомні екрани створюються у grid-редакторі й додаються в карусель.', 't'),
        ('Позначка-глобус біля значення означає дані з інтернету.', 't'),
    ],
    'en': [
        ('Sensors', 'h'),
        ('BMP280/BMP390: temperature and barometric pressure.', 't'),
        ('SCD41: true CO₂, temperature, humidity.', 't'),
        ('SGP41: VOC Index and NOx Index (Sensirion indices, not ppb).', 't'),
        ('SPS30: particulate matter PM1.0, PM2.5, PM4.0, PM10 in µg/m³.', 't'),
        ('', 't'),
        ('What the home screen shows', 'h'),
        ('CO₂: ventilation. Above 800 ppm — consider airing the room.', 't'),
        ('VOC Index: volatile organic compounds — odours, chemicals, cooking.', 't'),
        ('NOx Index: nitrogen oxides — gas stove, street air, smoke.', 't'),
        ('PM2.5/PM10: dust and fine particles.', 't'),
        ('Chart notches: yellow — normal limit, red — high level.', 't'),
        ('', 't'),
        ('Derived metrics (optional in boxes)', 'h'),
        ('AQI: US EPA air-quality index from PM2.5/PM10. 0–50 good, 51–100 moderate, >100 unhealthy.', 't'),
        ('IAQ*: composite indoor index from CO₂, VOC and PM2.5. An estimate, not a certified sensor.', 't'),
        ('eCO₂*: equivalent-CO₂ estimate from VOC load. Real CO₂ comes from the SCD41.', 't'),
        ('An asterisk* marks a computed value, not a direct measurement.', 't'),
        ('', 't'),
        ('Weather forecast', 'h'),
        ('Based on the ~3h pressure trend: a sharp drop → storm, a rise → clear.', 't'),
        ('', 't'),
        ('Astro block', 'h'),
        ('Geomagnetic storms — NOAA Kp index (0–9, storm from Kp5). Moon phase — offline.', 't'),
        ('Planet retrograde — built-in table (approximate). Advice — condition-based.', 't'),
        ('', 't'),
        ('Screens & gestures', 'h'),
        ('Swipe left/right to switch designs (carousel).', 't'),
        ('Tap near the left edge to show/hide the side panel.', 't'),
        ('Custom screens are built in the grid editor and added to the carousel.', 't'),
        ('A small globe next to a value means the data comes from the internet.', 't'),
    ],
}


def T(key):
    pair = _TR.get(key)
    if pair is None:
        return key
    return pair[1] if LANG[0] == 'en' else pair[0]

def weekdays():
    return _WEEKDAYS['en' if LANG[0] == 'en' else 'uk']

def about_lines():
    return _ABOUT_LINES['en' if LANG[0] == 'en' else 'uk']
