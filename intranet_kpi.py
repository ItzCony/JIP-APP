"""Analytické oddělení → KPI.

Spouští převzatý skript kpi_final.py (Serhii, KPI_Final.py) nad sdílenou složkou
a nabízí výstupy ke stažení. Logika skriptu se nepřepisuje — výstupy zůstávají 1:1,
aplikace mu jen podstrčí konfiguraci (kpi.ini) a složku pro výstupy.

* Vstupy (SLK, Karty k …, Nedodávky_VO_dodavatel …, Kontaktní údaje …, SHELL na
  protažení …, ceníky Gastro) leží ve sdílené složce z proměnné `JIPKA_KPI_SLOZKA`;
  skript si v ní sám najde nejnovější soubory podle masek.
* Každý běh má vlastní složku KPI/<YYYY-MM-DD_HHMMSS>/ se souhrn_python.xlsx,
  Dodavately/…, KPI.log a stav.json. Drží se posledních POCET_BEHU běhů.
* Výpočet běží na pozadí: vedlejší vlákno pustí skript jako samostatný proces
  s nižší prioritou a jen čte jeho výstup. Event-loop, sdílený thread-pool ani
  CPU-pool aplikace to nezdrží; pandas si vezme 1–2 GB a po konci je vrátí.
  Najednou běží jeden výpočet, s aplikací končí i on (viz ukonci).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from nicegui import background_tasks, ui

SKRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kpi_final.py')
VSTUPY = os.path.abspath(os.environ['JIPKA_KPI_SLOZKA'].strip()) \
    if os.environ.get('JIPKA_KPI_SLOZKA', '').strip() else ''
VYSTUPY = os.path.abspath('KPI')        # kořen vedle aplikace (jako Bonusy)
POCET_BEHU = 6
LIMIT_S = 30 * 60                       # déle běžící skript se ukončí
_FORMAT = '%Y-%m-%d_%H%M%S'

_zamek = threading.Lock()
_stav = {'bezi': False, 'slozka': '', 'kdo': '', 'start': 0.0,
         'krok': '', 'radek': '', 'verze': 0}
_proces: subprocess.Popen | None = None     # běžící skript (kvůli ukonci)

_RE_RADEK = re.compile(r'^\S+ \S+ \S+\s+(.*)$')        # "datum čas LEVEL  zpráva"
_RE_KROK = re.compile(r'--- (Krok.*?) ---')
_RE_KONTROLA = re.compile(r'\[(OK|WARN|FAIL)\] (.*)$')
_RE_CHYBA = re.compile(r'^\S+ \S+ (ERROR|CRITICAL)\s+(.*)$')


# =========================================================
# BĚH SKRIPTU
# =========================================================
def _zmena(**kw):
    _stav.update(kw)
    _stav['verze'] += 1


def spust(kdo: str) -> bool:
    """Založí složku běhu a pustí skript ve vlákně. False = už jeden běží."""
    with _zamek:
        if _stav['bezi']:
            return False
        slozka = os.path.join(VYSTUPY, datetime.datetime.now().strftime(_FORMAT))
        os.makedirs(slozka, exist_ok=True)
        _zmena(bezi=True, slozka=slozka, kdo=kdo, start=time.time(),
               krok='Spouštím…', radek='')
    threading.Thread(target=_beh, args=(slozka, kdo), daemon=True).start()
    return True


def _upozadit(pid: int):
    """Linux: výpočet ustoupí aplikaci — o CPU (nice 10) i při nedostatku paměti
    (OOM killer obětuje raději KPI než intranet). Windows řeší creationflags."""
    try:
        os.setpriority(os.PRIO_PROCESS, pid, 10)
        with open(f'/proc/{pid}/oom_score_adj', 'w') as f:
            f.write('500')
    except (AttributeError, OSError):
        pass


def ukonci():
    """app.on_shutdown: běžící výpočet skončí s aplikací. Restart z audit konzole
    (os.execv) by ho jinak nechal běžet bez rodiče a šel by spustit druhý."""
    p = _proces
    if p is not None and p.poll() is None:
        p.kill()


def _beh(slozka: str, kdo: str):
    global _proces
    start = datetime.datetime.now()
    kod, chyba, radky = None, '', []
    try:
        ini = os.path.join(slozka, 'kpi.ini')
        with open(ini, 'w', encoding='utf-8') as f:
            f.write('[cesty]\n'
                    f'data = {VSTUPY}\n'
                    f'vystup = {os.path.join(slozka, "souhrn_python.xlsx")}\n'
                    f'dodavatele = {os.path.join(slozka, "Dodavately")}\n'
                    f'shell_seznam = {VSTUPY}\n')     # výchozí by prohledal i nadřazenou složku
        env = dict(os.environ, KPI_CONFIG=ini, KPI_LOG=os.path.join(slozka, 'KPI.log'),
                   PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        p = _proces = subprocess.Popen(
            [sys.executable, SKRIPT], cwd=slozka, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
        _upozadit(p.pid)
        vyprselo = threading.Event()
        stopky = threading.Timer(LIMIT_S, lambda: (vyprselo.set(), p.kill()))
        stopky.start()
        try:
            for radek in p.stdout:
                radek = radek.rstrip()
                radky.append(radek)
                m = _RE_KROK.search(radek)
                z = _RE_RADEK.match(radek)
                _zmena(krok=m.group(1) if m else _stav['krok'],
                       radek=(z.group(1) if z else radek)[:200])
            kod = p.wait()
        finally:
            stopky.cancel()
        if vyprselo.is_set():
            chyba = f'Výpočet trval déle než {LIMIT_S // 60} minut a byl ukončen.'
    except Exception as exc:                # noqa: BLE001 — chyba spuštění patří do stav.json
        chyba = f'Skript nešel spustit: {exc}'

    kontroly, za_autocheck = [], False
    for radek in radky:
        if '===== AUTOCHECK =====' in radek:
            za_autocheck = True
        elif za_autocheck and (m := _RE_KONTROLA.search(radek)):
            kontroly.append([m.group(1), m.group(2)])
    if not chyba and kod:
        posledni = [m.group(2) for r in radky if (m := _RE_CHYBA.match(r))]
        chyba = ('Výpočet byl ukončen zvenčí (restart aplikace / nedostatek paměti).' if kod < 0
                 else posledni[-1] if posledni else f'Skript skončil s kódem {kod}.')
    if kod == 0:
        vysledek = 'ok'
    elif any(s == 'FAIL' for s, _ in kontroly):
        vysledek = 'kontroly'               # výstupy jsou, ale kontroly hlásí FAIL
    else:
        vysledek = 'chyba'
    try:
        with open(os.path.join(slozka, 'stav.json'), 'w', encoding='utf-8') as f:
            json.dump({'kdo': kdo, 'start': start.isoformat(timespec='seconds'),
                       'konec': datetime.datetime.now().isoformat(timespec='seconds'),
                       'kod': kod, 'vysledek': vysledek, 'chyba': chyba,
                       'kontroly': kontroly}, f, ensure_ascii=False, indent=1)
    finally:
        try:
            _uklid(slozka)
        finally:
            _zmena(bezi=False)          # stav se nesmí zaseknout ani při chybě úklidu


def _uklid(aktualni: str):
    """Nech posledních POCET_BEHU běhů, starší smaž."""
    stare = [b['slozka'] for b in behy()[POCET_BEHU:] if b['slozka'] != aktualni]
    for cesta in stare:
        shutil.rmtree(cesta, ignore_errors=True)


# =========================================================
# SOUBORY
# =========================================================
def behy() -> list[dict]:
    """Složky běhů, nejnovější první, se stav.json (chybí = běží / přerušeno)."""
    if not os.path.isdir(VYSTUPY):
        return []
    vysledek = []
    for jmeno in sorted(os.listdir(VYSTUPY), reverse=True):
        cesta = os.path.join(VYSTUPY, jmeno)
        try:
            cas = datetime.datetime.strptime(jmeno, _FORMAT)
        except ValueError:
            continue
        if not os.path.isdir(cesta):
            continue
        stav = {}
        try:
            with open(os.path.join(cesta, 'stav.json'), encoding='utf-8') as f:
                stav = json.load(f)
        except (OSError, ValueError):
            pass
        vysledek.append({**stav, 'slozka': cesta, 'cas': cas,
                         'log': os.path.isfile(os.path.join(cesta, 'KPI.log'))})
    return vysledek


def vystupy(slozka: str) -> list[str]:
    """Výstupní XLSX běhu jako relativní cesty (souhrn, Dodavately/…)."""
    vysledek = []
    for koren, _slozky, jmena in os.walk(slozka):
        vysledek += [os.path.relpath(os.path.join(koren, j), slozka)
                     for j in jmena if j.lower().endswith('.xlsx')]
    return sorted(vysledek, key=lambda r: (os.sep in r, r.lower()))


def _zip(slozka: str) -> str:
    """Všechny výstupy v jednom ZIPu (vytvoří se jednou, pak se jen stahuje).
    XLSX už je zkomprimované — ukládá se bez komprese."""
    cesta = os.path.join(slozka, f'KPI_{os.path.basename(slozka)}.zip')
    if not os.path.isfile(cesta):
        docasna = cesta + '.tmp'
        with zipfile.ZipFile(docasna, 'w', zipfile.ZIP_STORED) as z:
            for rel in vystupy(slozka):
                z.write(os.path.join(slozka, rel), rel)
        os.replace(docasna, cesta)
    return cesta


def _vstupni_soubory() -> list[dict]:
    soubory = []
    for jmeno in sorted(os.listdir(VSTUPY), key=str.lower):
        cesta = os.path.join(VSTUPY, jmeno)
        if os.path.isfile(cesta) and not jmeno.startswith('~$'):
            st = os.stat(cesta)
            soubory.append({'nazev': jmeno, 'velikost': f'{st.st_size / 1e6:,.1f} MB'.replace(',', ' '),
                            'zmeneno': datetime.datetime.fromtimestamp(st.st_mtime)
                            .strftime('%d.%m.%Y %H:%M')})
    return soubory


# =========================================================
# UI
# =========================================================
_STITKY = {
    'ok':       ('Hotovo', 'green'),
    'kontroly': ('Hotovo, kontroly hlásí FAIL', 'orange'),
    'chyba':    ('Selhalo', 'red'),
    None:       ('Přerušeno', 'grey'),
}
_IKONY = {'OK': ('check_circle', 'green'), 'WARN': ('warning', 'orange'),
          'FAIL': ('cancel', 'red')}


def _vykresli_async(refreshable, nazev: str):
    """První vykreslení async refreshable ze synchronního kódu. Kontejner vznikne
    hned (pořadí prvků drží), obsah doplní úloha — volání vrací jen korutinu."""
    background_tasks.create(refreshable(), name=nazev)


def vykresli(user_name: str):
    """Obsah sekce KPI (nadpis a tlačítko Zpět kreslí Analytické oddělení)."""
    videno = {'verze': _stav['verze'], 'bezi': _stav['bezi']}

    async def _spustit():
        if not VSTUPY or not await asyncio.to_thread(os.path.isdir, VSTUPY):
            ui.notify('Sdílená složka se vstupy není dostupná.', type='negative')
            return
        try:
            spusteno = await asyncio.to_thread(spust, user_name)
        except OSError as exc:
            ui.notify(f'Nelze založit složku pro výstupy: {exc}', type='negative')
            return
        if not spusteno:
            ui.notify('Výpočet už běží.', type='warning')
            return
        ui.notify('Výpočet spuštěn — trvá zhruba 3–5 minut.', type='info')

    async def _stahnout_zip(slozka):
        try:
            cesta = await asyncio.to_thread(_zip, slozka)
        except OSError as exc:
            ui.notify(f'ZIP se nepodařilo vytvořit: {exc}', type='negative')
            return
        ui.download.file(cesta, os.path.basename(cesta))

    @ui.refreshable
    async def _vstupy_panel():
        if not VSTUPY:
            ui.label('Sdílená složka není nastavená — na serveru chybí proměnná '
                     'JIPKA_KPI_SLOZKA.').classes('text-red-600')
            return
        ui.label(VSTUPY).classes('font-mono text-sm text-gray-500')
        try:        # síťová složka může viset — mimo event-loop
            soubory = await asyncio.to_thread(_vstupni_soubory)
        except OSError as exc:
            ui.label(f'Složka není dostupná: {exc}').classes('text-red-600')
            return
        if not soubory:
            ui.label('Složka je prázdná.').classes('text-gray-400')
            return
        ui.table(columns=[
            {'name': 'nazev', 'label': 'Soubor', 'field': 'nazev', 'align': 'left'},
            {'name': 'velikost', 'label': 'Velikost', 'field': 'velikost', 'align': 'right'},
            {'name': 'zmeneno', 'label': 'Změněno', 'field': 'zmeneno', 'align': 'left'},
        ], rows=soubory, row_key='nazev').props('dense flat').classes('w-full')

    @ui.refreshable
    def _beh_panel():
        with ui.row().classes('w-full items-center gap-4 py-2'):
            if not _stav['bezi']:
                ui.button('Spustit výpočet', icon='play_arrow', on_click=_spustit) \
                    .props('color=primary no-caps')
                ui.label('Skript vezme nejnovější soubory ze sdílené složky. '
                         'Výpočet trvá zhruba 3–5 minut.').classes('text-sm text-gray-500')
                return
            ui.spinner(size='lg')
            with ui.column().classes('gap-0'):
                minut, sekund = divmod(int(time.time() - _stav['start']), 60)
                ui.label(f'{_stav["krok"]}  •  {minut}:{sekund:02d}  •  spustil {_stav["kdo"]}') \
                    .classes('font-semibold')
                ui.label(_stav['radek']).classes('text-sm text-gray-500 font-mono')

    @ui.refreshable
    async def _vysledky_panel():
        seznam = [b for b in await asyncio.to_thread(behy)
                  if not (_stav['bezi'] and b['slozka'] == _stav['slozka'])]
        if not seznam:
            ui.label('Zatím žádný výpočet.').classes('text-gray-400')
            return
        for i, beh in enumerate(seznam):
            soubory = await asyncio.to_thread(vystupy, beh['slozka'])
            text, barva = _STITKY.get(beh.get('vysledek'), _STITKY[None])
            with ui.card().classes('w-full'):
                with ui.row().classes('w-full items-center gap-3'):
                    ui.label(f'{beh["cas"]:%d.%m.%Y %H:%M}').classes('text-lg font-semibold')
                    ui.badge(text, color=barva)
                    if beh.get('kdo'):
                        ui.label(f'spustil {beh["kdo"]}').classes('text-sm text-gray-500')
                    ui.space()
                    if soubory:
                        ui.button('Stáhnout vše (ZIP)', icon='folder_zip',
                                  on_click=lambda _e, s=beh['slozka']: _stahnout_zip(s)) \
                            .props('color=green no-caps dense')
                    log = os.path.join(beh['slozka'], 'KPI.log')
                    if beh['log']:              # zjištěno v behy() mimo event-loop
                        ui.button('Log', icon='article',
                                  on_click=lambda _e, c=log, n=f'KPI_{os.path.basename(beh["slozka"])}.log':
                                      ui.download.file(c, n)) \
                            .props('outline no-caps dense')
                if beh.get('chyba'):
                    ui.label(beh['chyba']).classes('text-red-600 text-sm')
                with ui.row().classes('w-full gap-2 flex-wrap'):
                    for rel in soubory:
                        ui.button(rel.replace(os.sep, ' / '), icon='download',
                                  on_click=lambda _e, c=os.path.join(beh['slozka'], rel):
                                      ui.download.file(c, os.path.basename(c))) \
                            .props('color=green outline no-caps dense')
                kontroly = beh.get('kontroly') or []
                if kontroly:
                    pocty = {s: sum(1 for k, _ in kontroly if k == s) for s in _IKONY}
                    with ui.expansion(f'Kontroly — OK {pocty["OK"]} · WARN {pocty["WARN"]} '
                                      f'· FAIL {pocty["FAIL"]}', icon='fact_check',
                                      value=i == 0 and (pocty['WARN'] or pocty['FAIL'])) \
                            .classes('w-full'):
                        for status, popis in kontroly:
                            ikona, barva = _IKONY[status]
                            with ui.row().classes('items-center gap-2 no-wrap'):
                                ui.icon(ikona, color=barva, size='18px')
                                ui.label(popis).classes('text-sm')

    with ui.card().classes('w-full'):
        with ui.row().classes('w-full items-center'):
            ui.label('Vstupní data (sdílená složka)').classes('text-lg font-semibold')
            ui.space()
            ui.button(icon='refresh', on_click=_vstupy_panel.refresh) \
                .props('flat round dense').tooltip('Znovu načíst obsah složky')
        with ui.expansion('Soubory', value=False).classes('w-full'):
            _vykresli_async(_vstupy_panel, 'kpi-vstupy')
    _beh_panel()
    ui.label('Výsledky').classes('text-lg font-semibold mt-4')
    with ui.column().classes('w-full gap-3'):
        _vykresli_async(_vysledky_panel, 'kpi-vysledky')

    def _tik():
        if _stav['bezi']:
            _beh_panel.refresh()                # i kvůli běžícím stopkám
        if _stav['verze'] == videno['verze']:
            return
        prepnuto = videno['bezi'] != _stav['bezi']
        videno.update(verze=_stav['verze'], bezi=_stav['bezi'])
        _beh_panel.refresh()
        if prepnuto:
            _vysledky_panel.refresh()

    ui.timer(1.0, _tik)
