"""db.py — SQLite storage, 5-second samples, auto-purge after 7 days."""
import sqlite3, time, threading
from pathlib import Path

DB_PATH = Path(__file__).parent / 'data.db'
_lock   = threading.Lock()
_conn   = None

def _get():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL,
            temp_bmp   REAL, pressure  REAL,
            co2        REAL, temp_scd  REAL, hum_scd REAL,
            voc_index  REAL, nox_index REAL,
            pm1_0 REAL, pm2_5 REAL, pm4_0 REAL, pm10 REAL,
            pm0_5_n REAL, pm1_0_n REAL, pm2_5_n REAL,
            pm4_0_n REAL, pm10_n  REAL, typ_ps  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        # v5 schema migration for display-mapped values
        for col in ['temperature REAL', 'humidity REAL']:
            try:
                _conn.execute('ALTER TABLE readings ADD COLUMN ' + col)
            except Exception:
                pass
        _conn.commit()
    return _conn

def insert(data):
    c = _get()
    cols = ['ts','temperature','humidity','temp_bmp','pressure','co2','temp_scd','hum_scd',
            'voc_index','nox_index','pm1_0','pm2_5','pm4_0','pm10',
            'pm0_5_n','pm1_0_n','pm2_5_n','pm4_0_n','pm10_n','typ_ps']
    vals = [data.get(c) for c in cols]
    ph   = ','.join(['?']*len(cols))
    with _lock:
        c.execute(f"INSERT INTO readings ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()

def query(key, hours=24):
    c = _get()
    since = time.time() - hours*3600
    rows = c.execute(
        f"SELECT ts,{key} FROM readings WHERE ts>=? AND {key} IS NOT NULL ORDER BY ts",
        (since,)).fetchall()
    return [(r['ts'], r[key]) for r in rows]

def get_setting(k, default=''): 
    c = _get()
    r = c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
    return r['value'] if r else default

def set_setting(k, v):
    with _lock:
        _get().execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,v))
        _get().commit()

def purge(keep_days=7):
    with _lock:
        _get().execute("DELETE FROM readings WHERE ts<?", (time.time()-keep_days*86400,))
        _get().commit()
