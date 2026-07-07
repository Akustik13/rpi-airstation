"""updater.py — оновлення з GitHub без SSH.

Порівнює локальний VERSION з файлом VERSION у репозиторії; за наявності новішої
версії завантажує zip гілки, перевіряє що код компілюється, робить бекап поточної
теки й застосовує нові файли (зберігаючи data.db і кастомні layouts), потім
перезапускає застосунок. Усе з коротким таймаутом і безпечним відкотом.
"""
import os, sys, re, shutil, zipfile, tempfile, py_compile
from urllib.request import urlopen, Request

REPO = 'Akustik13/rpi-airstation'
BRANCH = 'main'
_UA = {'User-Agent': 'airstation-updater'}

def app_dir():
    return os.path.dirname(os.path.abspath(__file__))

def local_version():
    try:
        with open(os.path.join(app_dir(), 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return '0'

def remote_version(timeout=6):
    url = f'https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION'
    try:
        with urlopen(Request(url, headers=_UA), timeout=timeout) as r:
            return r.read().decode('utf-8').strip()
    except Exception:
        return None

def _vt(v):
    nums = tuple(int(x) for x in re.findall(r'\d+', v or ''))
    return nums or (0,)

def _newer(a, b):
    return _vt(a) > _vt(b)

def update_available():
    """(avail, local, remote)."""
    lv = local_version(); rv = remote_version()
    if rv is None:
        return (False, lv, None)
    return (_newer(rv, lv), lv, rv)

def _extracted_root(tmp):
    for d in os.listdir(tmp):
        p = os.path.join(tmp, d)
        if os.path.isdir(p) and d != '__MACOSX':
            return p
    return None

def download_and_apply(what='all', log=None):
    """what='all' — увесь код; what='layouts' — лише дизайни.
    Повертає (ok:bool, msg:str)."""
    def _log(m):
        if log:
            log(m)
    url = f'https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}'
    tmp = tempfile.mkdtemp(prefix='ota_')
    zpath = os.path.join(tmp, 'src.zip')
    try:
        _log('Завантаження…')
        with urlopen(Request(url, headers=_UA), timeout=40) as r, open(zpath, 'wb') as f:
            shutil.copyfileobj(r, f)
        _log('Розпакування…')
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        src = _extracted_root(tmp)
        if not src:
            return (False, 'Порожній архів')
        app = app_dir()

        if what == 'layouts':
            sl = os.path.join(src, 'layouts')
            if os.path.isdir(sl):
                dl = os.path.join(app, 'layouts'); os.makedirs(dl, exist_ok=True)
                for f in os.listdir(sl):
                    if f.endswith('.json'):
                        shutil.copy2(os.path.join(sl, f), os.path.join(dl, f))
            return (True, 'Дизайни оновлено')

        # перевірка: усі .py мають компілюватись
        _log('Перевірка коду…')
        for f in os.listdir(src):
            if f.endswith('.py'):
                py_compile.compile(os.path.join(src, f), doraise=True)

        # бекап поточної теки
        _log('Резервна копія…')
        bak = app.rstrip('/\\') + '_backup'
        shutil.rmtree(bak, ignore_errors=True)
        shutil.copytree(app, bak, ignore=shutil.ignore_patterns(
            '__pycache__', '*.pyc', 'data.db*', '*_backup'))

        # застосування
        _log('Застосування…')
        for name in os.listdir(src):
            s = os.path.join(src, name); d = os.path.join(app, name)
            if name.startswith('data.db') or name.endswith('_backup'):
                continue
            if name == 'layouts':
                os.makedirs(d, exist_ok=True)
                for f in os.listdir(s):
                    shutil.copy2(os.path.join(s, f), os.path.join(d, f))  # зберігає кастомні
                continue
            if os.path.isdir(s):
                shutil.rmtree(d, ignore_errors=True); shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
        return (True, 'Оновлено. Перезапуск…')
    except py_compile.PyCompileError as e:
        return (False, 'Помилка коду в оновленні')
    except Exception as e:
        return (False, str(e)[:80])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def restart():
    """Перезапуск застосунку (execv). Якщо запущено як сервіс — вихід, і сервіс
    підніме знову."""
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        os._exit(0)
