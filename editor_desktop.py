#!/usr/bin/env python3
"""editor_desktop.py — десктопний grid-редактор екранів метеостанції з мишею.

Запуск:  python3 editor_desktop.py [layouts/wx4_grid.json]

Ліворуч — прев'ю пристрою (1280×720) з живими демо-даними й сіткою.
Праворуч — палітра блоків і властивості вибраного блока.
Миша: клац по палітрі — додати блок; клац по блоку — вибрати; тягнути тіло —
пересунути (прилипання до сітки); тягнути кут — змінити розмір; Delete — видалити.
Кнопки: New / Load / Save. Усе пише той самий JSON, що читає станція.
"""
import sys, os, glob, subprocess
import pygame
import gridui as G
try:
    import config as C
except Exception:
    C = G.C

DEV_W, DEV_H = 1280, 720
PANEL_W = 320
WIN_W, WIN_H = DEV_W + PANEL_W, DEV_H + 60

def main():
    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption('AirStation — Grid Editor')
    clock = pygame.time.Clock()
    dev = pygame.Surface((DEV_W, DEV_H))
    data = G.demo_data()

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path and os.path.exists(path):
        layout = G.load_layout(path)
    else:
        layout = {'name': 'custom', 'cols': 16, 'rows': 9, 'blocks': []}
    cols = layout.get('cols', 16); rows = layout.get('rows', 16)

    sel = [None]           # індекс вибраного блока
    drag = [None]          # ('move'|'resize', start_mouse, start_block)
    palette = list(G.BLOCKS.items())
    metric_keys = ['co2', 'voc_index', 'nox_index', 'eco2', 'aqi', 'iaq', 'pm2_5', 'pm10', 'temperature', 'humidity', 'pressure']

    def dev_to_cell(mx, my):
        return G.pt_to_cell(mx, my, DEV_W, DEV_H, cols, rows)

    def block_rect(b):
        return pygame.Rect(G.cell_rect(b['col'], b['row'], b['w'], b['h'], DEV_W, DEV_H, cols, rows))

    def save():
        name = layout.get('name', 'custom')
        os.makedirs('layouts', exist_ok=True)
        p = path or f'layouts/{name}.json'
        G.save_layout(layout, p)
        return p

    def _git_root():
        d = os.path.dirname(os.path.abspath(__file__))
        while True:
            if os.path.isdir(os.path.join(d, '.git')):
                return d
            nd = os.path.dirname(d)
            if nd == d:
                return None
            d = nd

    def git_push():
        """Зберігає дизайн і пушить папку layouts у GitHub. Повертає статус-рядок."""
        save()
        root = _git_root()
        if not root:
            return 'Не знайдено .git — це не клон репозиторію'

        def run(args):
            return subprocess.run(['git'] + args, cwd=root, capture_output=True,
                                  text=True, timeout=60)
        try:
            run(['add', 'layouts'])
            c = run(['commit', '-m', 'designs: update layouts'])
            # commit може повернути "nothing to commit" — це не помилка
            p = run(['push'])
            if p.returncode == 0:
                return 'Залито на GitHub ✓'
            err = (p.stderr or p.stdout or '').strip().splitlines()
            return 'Помилка push: ' + (err[-1] if err else 'невідома')
        except FileNotFoundError:
            return 'git не встановлено або не в PATH'
        except subprocess.TimeoutExpired:
            return 'Таймаут (перевір інтернет/авторизацію)'
        except Exception as e:
            return 'Помилка: ' + str(e)[:60]

    font = pygame.font.SysFont('dejavusans', 18)
    fontb = pygame.font.SysFont('dejavusans', 20, bold=True)
    small = pygame.font.SysFont('dejavusans', 15)

    def txt(s, f, col, x, y, a='tl'):
        su = f.render(s, True, col); r = su.get_rect()
        setattr(r, {'tl': 'topleft', 'mc': 'center', 'ml': 'midleft', 'tr': 'topright'}[a], (x, y))
        screen.blit(su, r); return r

    palette_rects = []
    prop_rects = []
    tool_rects = {}

    running = True
    msg = ['']
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_DELETE and sel[0] is not None:
                    layout['blocks'].pop(sel[0]); sel[0] = None
                elif ev.key == pygame.K_s and (ev.mod & pygame.KMOD_CTRL):
                    msg[0] = 'Saved: ' + save()
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                mx, my = ev.pos
                # палітра
                hit_pal = False
                for (tkey, rect) in palette_rects:
                    if rect.collidepoint(mx, my):
                        b = {'type': tkey, 'col': 0, 'row': 0, 'w': 4, 'h': 2, 'p': {}}
                        if tkey == 'metric': b['p'] = {'key': 'co2', 'icon': True}
                        if tkey == 'gauge': b['p'] = {'key': 'pressure'}
                        layout['blocks'].append(b); sel[0] = len(layout['blocks']) - 1
                        hit_pal = True; break
                if hit_pal: continue
                # властивості
                hit_prop = False
                for (act, rect) in prop_rects:
                    if rect.collidepoint(mx, my):
                        b = layout['blocks'][sel[0]]
                        if act[0] == 'metric':
                            b.setdefault('p', {})['key'] = act[1]
                        elif act == 'icon':
                            b.setdefault('p', {})['icon'] = not b['p'].get('icon', False)
                        elif act == 'del':
                            layout['blocks'].pop(sel[0]); sel[0] = None
                        hit_prop = True; break
                if hit_prop: continue
                # тулбар
                for (name, rect) in tool_rects.items():
                    if rect.collidepoint(mx, my):
                        if name == 'save': msg[0] = 'Saved: ' + save()
                        elif name == 'new': layout['blocks'].clear(); sel[0] = None
                        elif name == 'grid': layout['_showgrid'] = not layout.get('_showgrid', True)
                        elif name == 'github': msg[0] = git_push()
                        break
                else:
                    # канва пристрою
                    if mx < DEV_W and my < DEV_H:
                        # перевіряємо ручку ресайзу вибраного
                        if sel[0] is not None:
                            br = block_rect(layout['blocks'][sel[0]])
                            handle = pygame.Rect(br.right - 18, br.bottom - 18, 22, 22)
                            if handle.collidepoint(mx, my):
                                drag[0] = ('resize', (mx, my), dict(layout['blocks'][sel[0]])); continue
                        # вибір блока (згори донизу)
                        found = None
                        for i in range(len(layout['blocks']) - 1, -1, -1):
                            if block_rect(layout['blocks'][i]).collidepoint(mx, my):
                                found = i; break
                        sel[0] = found
                        if found is not None:
                            drag[0] = ('move', (mx, my), dict(layout['blocks'][found]))
            elif ev.type == pygame.MOUSEBUTTONUP:
                drag[0] = None
            elif ev.type == pygame.MOUSEMOTION and drag[0] is not None and sel[0] is not None:
                mode, (sx, sy), sb = drag[0]
                mx, my = ev.pos
                cwid = DEV_W / cols; chei = DEV_H / rows
                dcol = round((mx - sx) / cwid); drow = round((my - sy) / chei)
                b = layout['blocks'][sel[0]]
                if mode == 'move':
                    b['col'] = max(0, min(cols - b['w'], sb['col'] + dcol))
                    b['row'] = max(0, min(rows - b['h'], sb['row'] + drow))
                else:
                    b['w'] = max(1, min(cols - b['col'], sb['w'] + dcol))
                    b['h'] = max(1, min(rows - b['row'], sb['h'] + drow))

        # ── малюємо прев'ю пристрою ──
        G.render_layout(dev, layout, data, DEV_W, DEV_H)
        if layout.get('_showgrid', True):
            for c in range(cols + 1):
                x = int(c * DEV_W / cols); pygame.draw.line(dev, (40, 50, 70), (x, 0), (x, DEV_H), 1)
            for rr in range(rows + 1):
                y = int(rr * DEV_H / rows); pygame.draw.line(dev, (40, 50, 70), (0, y), (DEV_W, y), 1)
        if sel[0] is not None:
            br = block_rect(layout['blocks'][sel[0]])
            pygame.draw.rect(dev, C.ACCENT, br, 2, border_radius=8)
            pygame.draw.rect(dev, C.ACCENT, (br.right - 18, br.bottom - 18, 16, 16), border_radius=4)
        screen.fill((6, 10, 18))
        screen.blit(dev, (0, 0))

        # ── права панель: палітра + властивості ──
        px = DEV_W + 12
        txt('БЛОКИ', fontb, C.WHITE, px, 10)
        palette_rects = []
        yy = 40
        for tkey, (label, _fn) in palette:
            rect = pygame.Rect(px, yy, PANEL_W - 24, 32)
            pygame.draw.rect(screen, C.PANEL2, rect, border_radius=7)
            pygame.draw.rect(screen, C.BORDER, rect, 1, border_radius=7)
            txt(label, font, C.TEXT2, px + 10, yy + 6)
            txt('+', fontb, C.GREEN, rect.right - 22, yy + 4)
            palette_rects.append((tkey, rect)); yy += 38

        txt('ВЛАСТИВОСТІ', fontb, C.WHITE, px, yy + 8); yy += 40
        prop_rects = []
        if sel[0] is not None:
            b = layout['blocks'][sel[0]]
            txt(f"{G.BLOCKS[b['type']][0]}  [{b['col']},{b['row']}]  {b['w']}×{b['h']}", small, C.MUTED, px, yy); yy += 26
            if b['type'] in ('metric', 'gauge'):
                txt('показник:', small, C.MUTED, px, yy); yy += 22
                for i, mk in enumerate(metric_keys):
                    rect = pygame.Rect(px + (i % 3) * 98, yy + (i // 3) * 30, 92, 26)
                    curp = b.get('p', {}).get('key') == mk
                    pygame.draw.rect(screen, C.ACCENT_D if curp else C.PANEL2, rect, border_radius=6)
                    pygame.draw.rect(screen, C.ACCENT if curp else C.BORDER, rect, 1, border_radius=6)
                    txt(G.META.get(mk, (mk,))[0][:7], small, C.WHITE if curp else C.TEXT2, rect.x + 6, rect.y + 5)
                    prop_rects.append((('metric', mk), rect))
                yy += ((len(metric_keys) + 2) // 3) * 30 + 8
                if b['type'] == 'metric':
                    rect = pygame.Rect(px, yy, 150, 28)
                    pygame.draw.rect(screen, C.PANEL2, rect, border_radius=6); pygame.draw.rect(screen, C.BORDER, rect, 1, border_radius=6)
                    txt(('☑' if b.get('p', {}).get('icon') else '☐') + ' іконка', small, C.TEXT2, rect.x + 8, rect.y + 6)
                    prop_rects.append(('icon', rect)); yy += 36
            drect = pygame.Rect(px, yy, 150, 30)
            pygame.draw.rect(screen, (60, 20, 24), drect, border_radius=6); pygame.draw.rect(screen, C.RED, drect, 1, border_radius=6)
            txt('Видалити блок', small, C.RED, drect.x + 10, drect.y + 7); prop_rects.append(('del', drect))
        else:
            txt('клацни блок на екрані', small, C.MUTED, px, yy)

        # ── нижній тулбар ──
        tool_rects = {}
        ty = DEV_H + 12
        for i, (name, label, col) in enumerate([('new', 'Новий', C.ORANGE), ('grid', 'Сітка', C.CYAN), ('save', 'Зберегти', C.GREEN), ('github', '⬆ GitHub', C.BLUE)]):
            rect = pygame.Rect(20 + i * 160, ty, 150, 40)
            pygame.draw.rect(screen, tuple(int(x * 0.3) for x in col), rect, border_radius=8)
            pygame.draw.rect(screen, col, rect, 1, border_radius=8)
            txt(label, fontb, col, rect.centerx - fontb.size(label)[0] // 2, rect.y + 8)
            tool_rects[name] = rect
        txt(msg[0], font, C.YELLOW, 690, ty + 10)
        txt('Ctrl+S — зберегти · Delete — видалити · тягни кут — розмір', small, C.MUTED, 690, ty + 30)

        pygame.display.flip()
        clock.tick(30)
    pygame.quit()

if __name__ == '__main__':
    main()
