"""Náklady veletrh — jednoduchá tabulka nákladů firemního veletrhu JIP 2027.

Sloupce: název nákladu, domluvený náklad, Náklad - Final, rozdíl (domluvený − Final, kladný = ušetřeno),
poznámka; dole součty. Nic víc.
Tabulka jako v Excelu (AG Grid). Každý zápis do buňky jde do logu (kdy, kdo, kam, bylo → je); ukáže ho očičko u řádku.
Výběr, Ctrl+C a Ctrl+V jako v Excelu (intranet_ui_utils.KOPIE_JS); vložený blok se zapíše celý, nebo vůbec.
Zapisuje, přidává a maže řádky: naklady_veletrh_zadavatel / _schvalovatel / _spravce
('vse' = všechna práva); naklady_veletrh_ctenar jen čte.
Staré tabulky naklady_veletrh* (kategorie, faktury, přílohy, plány) zůstaly v DB jen jako archiv.
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import io
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from nicegui import ui

import intranet_data
import intranet_logger
from intranet_ui_utils import KOPIE_CLASS, KOPIE_COLDEF, KOPIE_JS, KOPIE_VKLADANI_CLASS

PRAVO_ZADAVATEL = 'naklady_veletrh_zadavatel'
PRAVO_SCHVALOVATEL = 'naklady_veletrh_schvalovatel'
PRAVO_SPRAVCE = 'naklady_veletrh_spravce'
PRAVO_CTENAR = 'naklady_veletrh_ctenar'
_PISE = frozenset((PRAVO_ZADAVATEL, PRAVO_SCHVALOVATEL, PRAVO_SPRAVCE, 'vse'))
_LOG = 'Náklady veletrh'
# zapisovatelné sloupce (= sloupce v DB) → popisek; rozdíl se jen dopočítává
_SLOUPCE = {'nazev': 'Název nákladu', 'domluveno': 'Domluvený náklad', 'odsouhlaseno': 'Náklad - Final',
            'poznamka': 'Poznámka'}
_CASTKY = ('domluveno', 'odsouhlaseno')
_TEXTY = {'nazev': 255, 'poznamka': 1000}       # textové sloupce → max. délka (VARCHAR)
_PRAZDNY = {'nazev': '', 'domluveno': None, 'odsouhlaseno': None, 'poznamka': ''}
_FMT_KC = ('p => typeof p.value === "number" ? p.value.toLocaleString("cs-CZ", '
           '{minimumFractionDigits: 2, maximumFractionDigits: 2}) + " Kč" : (p.value ?? "")')
_CSS = ('.nv-grid .ag-cell{border-right:1px solid #e2e2e2}'
        '.nv-grid .nv-cislo{background:#f3f3f3;color:#666;text-align:center}'
        '.nv-grid .ag-row-pinned{font-weight:700}'
        '.nv-grid .nv-plus{color:#15803d}.nv-grid .nv-minus{color:#b91c1c}'      # rozdíl: ušetřeno / přes
        '.nv-grid .ag-cell-wrap-text{line-height:20px;padding-top:3px;padding-bottom:3px}'   # hustší zalomená poznámka
        # bez díry nad součtem (auto-height dává tělu min. 150 px): AG Grid 36 / AG Grid 34 (NiceGUI 3.x)
        '.nv-grid .ag-grid-scrolling-rows.ag-layout-auto-height{min-height:0}'
        '.nv-grid .ag-layout-auto-height .ag-center-cols-viewport,'
        '.nv-grid .ag-layout-auto-height .ag-center-cols-container{min-height:0}')
# Sdíleně do hlavičky všech stránek už při importu: modul se dokresluje na pozadí a NiceGUI 3.x
# ui.add_css zavolané mezi odesláním stránky a připojením websocketu tiše zahodí.
ui.add_css(_CSS, shared=True)


def _ikona(nazev, tip) -> str:
    """JS cellRenderer: ikona v buňce, jen když má řádek uložené id (data[field] == true)."""
    return (f'p => p.value ? \'<i class="material-icons" title="{tip}" '
            f'style="font-size:18px;line-height:inherit;color:#757575;cursor:pointer">{nazev}</i>\' : ""')


def _kc(d) -> float | None:
    """Decimal → číslo pro grid (zobrazení); zdrojem pravdy zůstává Decimal na serveru."""
    return None if d is None else float(d)


def _zapnuto() -> bool:
    return bool(intranet_data.nacti_nastaveni_intranetu().get('naklady_veletrh_zapnuty', True))


def ma_pristup_k_modulu(prava) -> bool:
    return bool(set(prava or ()) & (_PISE | {PRAVO_CTENAR})) and _zapnuto()


# =========================================================
# ČÁSTKY
# =========================================================
def _castka(text) -> Decimal | None:
    """'12 500,50 Kč' → Decimal('12500.50'); prázdné → None; nesmysl → ValueError."""
    t = ''.join(str(text or '').split()).replace(',', '.').lower().removesuffix('kč')
    if not t:
        return None
    try:
        d = Decimal(t).quantize(Decimal('0.01'), ROUND_HALF_UP)
        if abs(d) < 10 ** 13:       # DECIMAL(15,2); NaN tady vyhodí InvalidOperation
            return d
    except InvalidOperation:
        pass
    raise ValueError(text)


def _text(d) -> str:
    """Decimal → '12 500' / '12 500,50'; None → ''."""
    if d is None:
        return ''
    return f'{d:,.2f}'.replace(',', ' ').replace('.', ',').removesuffix(',00')


def _zobraz(k, v) -> str:
    """Hodnota buňky jako text (do pole i do logu)."""
    return _text(v) if k in _CASTKY else (v or '')


def _rozdil(r) -> Decimal | None:
    """Domluvený − Final (kladný = ušetřeno); jen když jsou vyplněné obě částky."""
    if r['domluveno'] is None or r['odsouhlaseno'] is None:
        return None
    return r['domluveno'] - r['odsouhlaseno']


def _soucet(hodnoty) -> Decimal:
    """Součet sloupce; prázdné buňky (None) se nepočítají."""
    return sum((h for h in hodnoty if h is not None), Decimal(0))


def _tsv(text) -> list[list[str]]:
    """Schránka (Excel i grid) → řádky buněk; Excel buňku s novým řádkem obalí uvozovkami."""
    return [r or [''] for r in csv.reader(io.StringIO(str(text or ''), newline=''), dialect='excel-tab')]


# =========================================================
# DATABÁZE (blokující — volat přes asyncio.to_thread)
# =========================================================
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_polozky (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nazev VARCHAR(255) NOT NULL DEFAULT '',
        domluveno DECIMAL(15,2) NULL,
        odsouhlaseno DECIMAL(15,2) NULL,
        poznamka VARCHAR(1000) NOT NULL DEFAULT ''
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_polozky_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        polozka_id INT NOT NULL,
        user_id INT NULL,
        kdo VARCHAR(255),
        kam VARCHAR(40) NOT NULL,
        bylo TEXT,
        je TEXT,
        cas DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_polozka (polozka_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
)
_DB_HOTOVO = False


@contextmanager
def _transakce():
    """Kurzor v jedné transakci: commit na konci, rollback při výjimce."""
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def inicializace_db():
    global _DB_HOTOVO
    if _DB_HOTOVO:
        return
    with _transakce() as cur:
        for prikaz in _SCHEMA:
            cur.execute(prikaz)
        # tabulka z verze bez poznámky
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'naklady_veletrh_polozky' AND COLUMN_NAME = 'poznamka'")
        if not cur.fetchone()['n']:
            cur.execute("ALTER TABLE naklady_veletrh_polozky ADD COLUMN poznamka VARCHAR(1000) NOT NULL DEFAULT ''")
    _DB_HOTOVO = True


def _loguj(cur, pid, user_id, kdo, kam, bylo=None, je=None):
    cur.execute('INSERT INTO naklady_veletrh_polozky_log (polozka_id, user_id, kdo, kam, bylo, je) '
                'VALUES (%s, %s, %s, %s, %s, %s)', (pid, user_id, kdo, kam, bylo or None, je or None))


def _nacti() -> list[dict]:
    with _transakce() as cur:
        cur.execute('SELECT id, nazev, domluveno, odsouhlaseno, poznamka FROM naklady_veletrh_polozky ORDER BY id')
        return cur.fetchall()


def _novy(cur, user_id, kdo) -> int:
    cur.execute('INSERT INTO naklady_veletrh_polozky () VALUES ()')
    pid = cur.lastrowid
    _loguj(cur, pid, user_id, kdo, 'Nový řádek')
    return pid


def _zapis(cur, pid, k, hodnota, user_id, kdo) -> bool:
    """Zapíše buňku a změnu do logu. False = v DB už ta hodnota je. LookupError = řádek smazán."""
    kam = _SLOUPCE[k]       # zároveň hlídá, že k je skutečný sloupec (jde do SQL)
    cur.execute(f'SELECT {k} AS v FROM naklady_veletrh_polozky WHERE id = %s FOR UPDATE', (pid,))
    radek = cur.fetchone()
    if radek is None:
        raise LookupError('Řádek mezitím někdo smazal.')
    if radek['v'] == hodnota:
        return False
    cur.execute(f'UPDATE naklady_veletrh_polozky SET {k} = %s WHERE id = %s', (hodnota, pid))
    _loguj(cur, pid, user_id, kdo, kam, _zobraz(k, radek['v']), _zobraz(k, hodnota))
    return True


def _pridej(user_id, kdo) -> int:
    with _transakce() as cur:
        return _novy(cur, user_id, kdo)


def _zmen(pid, k, hodnota, user_id, kdo) -> bool:
    with _transakce() as cur:
        return _zapis(cur, pid, k, hodnota, user_id, kdo)


def _vloz(zmeny, user_id, kdo) -> int:
    """Ctrl+V v jedné transakci — celé, nebo nic.
    zmeny = [(id | None = nový řádek, {sloupec: hodnota})] → počet změněných buněk."""
    n = 0
    with _transakce() as cur:
        for pid, hodnoty in zmeny:
            pid = pid or _novy(cur, user_id, kdo)
            n += sum(_zapis(cur, pid, k, v, user_id, kdo) for k, v in hodnoty.items())
    return n


def _smaz(pid, user_id, kdo):
    """Smaže řádek; jeho log v DB zůstává (i se smazaným obsahem)."""
    with _transakce() as cur:
        cur.execute('SELECT nazev, domluveno, odsouhlaseno, poznamka FROM naklady_veletrh_polozky '
                    'WHERE id = %s FOR UPDATE', (pid,))
        r = cur.fetchone()
        if r is None:
            return
        cur.execute('DELETE FROM naklady_veletrh_polozky WHERE id = %s', (pid,))
        _loguj(cur, pid, user_id, kdo, 'Smazaný řádek', ' | '.join(_zobraz(k, r[k]) for k in _SLOUPCE))


def _historie(pid) -> list[dict]:
    with _transakce() as cur:
        cur.execute('SELECT cas, kdo, kam, bylo, je FROM naklady_veletrh_polozky_log '
                    'WHERE polozka_id = %s ORDER BY id DESC', (pid,))
        return cur.fetchall()


def _xlsx(radky) -> bytes:
    """Tabulka jako v aplikaci: stejné sloupce a dole řádek Součet."""
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Náklady veletrh'
    ws.append([_SLOUPCE['nazev'], _SLOUPCE['domluveno'], _SLOUPCE['odsouhlaseno'], 'Rozdíl', _SLOUPCE['poznamka']])
    for r in radky:
        ws.append([r['nazev'], r['domluveno'], r['odsouhlaseno'], _rozdil(r), r['poznamka'] or None])
        for c in ws[ws.max_row]:
            if isinstance(c.value, str):
                c.data_type = 's'       # text „=…“ nesmí v Excelu skončit jako vzorec
    ws.append(['Součet', _soucet(r['domluveno'] for r in radky), _soucet(r['odsouhlaseno'] for r in radky),
               _soucet(map(_rozdil, radky))])
    for c in ws[1] + ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=4):
        for c in row:
            c.number_format = '#,##0.00 "Kč"' if c.column < 4 else '#,##0.00 "Kč";[Red]-#,##0.00 "Kč"'
    ws.column_dimensions['A'].width = 45
    for sl in 'BCD':
        ws.column_dimensions[sl].width = 22
    ws.column_dimensions['E'].width = 60
    ws.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =========================================================
# UI
# =========================================================
async def vykresli(user_id, user_name, vsechna_prava):
    pise = bool(set(vsechna_prava or ()) & _PISE)
    try:
        await asyncio.to_thread(inicializace_db)
        s = {'radky': await asyncio.to_thread(_nacti), 'k2id': {}, 'n': 0}
    except Exception as e:
        ui.label(f'Náklady veletrh se nepodařilo načíst: {e}').classes('text-red-600')
        return
    host = ui.element('div')        # dialogy mimo tabulku
    zamek = asyncio.Lock()          # zápisy jednoho uživatele po sobě (prázdný řádek nesmí vzniknout 2×)

    # Řádky gridu mají stabilní klíč 'k' (getRowId): str(id), nový řádek 'n<číslo>' dokud nedostane id.
    def do_gridu(r, k):
        return {'k': k, **{c: (r[c] or '') if c in _TEXTY else _kc(r[c]) for c in _SLOUPCE},
                'rozdil': _kc(_rozdil(r)), 'log': True, 'smazat': True}

    def prazdny(k=None):
        if k is None:
            s['n'] += 1
            k = f"n{s['n']}"
        return {**do_gridu(_PRAZDNY, k), 'log': False, 'smazat': False}

    def radky_gridu():
        return [do_gridu(r, str(r['id'])) for r in s['radky']] + ([prazdny()] if pise else [])

    def soucet_radek():
        rr = s['radky']
        return [{'k': 'soucet', 'nazev': 'Součet', **{c: _kc(_soucet(r[c] for r in rr)) for c in _CASTKY},
                 'rozdil': _kc(_soucet(map(_rozdil, rr)))}]

    def radek(k):
        pid = s['k2id'].get(k) or (int(k) if k.isdigit() else None)
        return next((r for r in s['radky'] if r['id'] == pid), None)

    def tx(**t):
        grid.run_grid_method('applyTransaction', t)

    def soucty():
        grid.run_grid_method('setGridOption', 'pinnedBottomRowData', soucet_radek())

    async def obnov():
        try:
            s['radky'] = await asyncio.to_thread(_nacti)
        except Exception as e:
            ui.notify(f'Data se nepodařilo načíst: {e}', type='negative')
            return
        s['k2id'].clear()
        grid.run_grid_method('setGridOption', 'rowData', radky_gridu())
        soucty()

    async def zmena(e):
        a = e.args
        k, g = a.get('colId'), a.get('data') or {}
        if not pise or k not in _SLOUPCE or a.get('rowPinned') or not g.get('k'):
            return
        async with zamek:
            r = radek(g['k'])
            stara = (r or _PRAZDNY)[k]
            try:
                nova = str(a.get('newValue') or '').strip()[:_TEXTY[k]] if k in _TEXTY else _castka(a.get('newValue'))
            except ValueError:
                ui.notify('Částku zadejte jako číslo, např. 12 500 nebo 12 500,50.', type='warning')
                nova = stara
            # ponytail: poslední zápis vyhrává; souběh dvou lidí na stejné buňce ukáže log, zámky až bude potřeba
            if nova != stara:
                try:
                    if r is None:       # psaní do prázdného řádku dole = nový náklad
                        pid = await asyncio.to_thread(_pridej, user_id, user_name)
                        r = {'id': pid, **_PRAZDNY}
                        s['radky'].append(r)
                        s['k2id'][g['k']] = pid
                        tx(add=[prazdny()])
                    if await asyncio.to_thread(_zmen, r['id'], k, nova, user_id, user_name):
                        nazev = nova if k == 'nazev' else r['nazev']
                        intranet_logger.log_activity(
                            user_name, _LOG, f"{nazev or 'bez názvu'} — {_SLOUPCE[k]}: "
                                             f"{_zobraz(k, stara) or '—'} → {_zobraz(k, nova) or '—'}")
                    r[k] = nova
                except LookupError as ex:
                    ui.notify(str(ex), type='warning')
                    await obnov()
                    return
                except Exception as ex:
                    ui.notify(f'Uložení se nepodařilo: {ex}', type='negative')
            # buňka = to, co platí na serveru (formát částky, vrácení chybného zápisu)
            tx(update=[do_gridu(r, g['k']) if r else prazdny(g['k'])])
            soucty()

    async def vloz(e):
        """Ctrl+V (intranet_ui_utils.KOPIE_JS): blok ze schránky od levého horního rohu výběru, jedna
        buňka vyplní celý výběr; co přeteče pod poslední řádek, přidá jako nové náklady."""
        a = e.args if isinstance(e.args, dict) else {}
        klice, cols, blok = a.get('radky') or [], a.get('sloupce') or [], _tsv(a.get('text'))
        if not pise or not klice or not cols or not blok:
            return
        if len(blok) == 1 and len(blok[0]) == 1:        # jako Excel
            blok = [blok[0] * min(int(a.get('sirka') or 1), len(cols))] * min(int(a.get('vyska') or 1), len(klice))
        if len(blok) > 500:
            ui.notify('Najednou jde vložit nejvýš 500 řádků.', type='warning')
            return
        async with zamek:
            zmeny = []      # [(id | None = nový náklad, {sloupec: hodnota})]; mimo zapisovatelné sloupce nic
            for i, bunky in enumerate(blok):
                r = radek(str(klice[i])) if i < len(klice) else None
                try:
                    h = {k: ' '.join(v.split())[:_TEXTY[k]] if k in _TEXTY else _castka(v)
                         for k, v in zip(cols, bunky) if k in _SLOUPCE}
                except ValueError as ex:
                    ui.notify(f'„{ex}“ není částka, nic se nevložilo. Částku zadejte jako číslo, '
                              'např. 12 500 nebo 12 500,50.', type='warning')
                    return
                if r or any(v not in ('', None) for v in h.values()):     # prázdné nové řádky nezakládá
                    zmeny.append((r and r['id'], h))
            if not any(h for _, h in zmeny):
                return
            try:
                n = await asyncio.to_thread(_vloz, zmeny, user_id, user_name)
            except LookupError as ex:
                ui.notify(str(ex), type='warning')
            except Exception as ex:
                ui.notify(f'Vložení se nepodařilo: {ex}', type='negative')
            else:
                novych = sum(pid is None for pid, _ in zmeny)
                zprava = f'Vloženo buněk: {n}' + (f', nových řádků: {novych}' if novych else '')
                ui.notify(zprava, position='bottom', timeout=1500)
                intranet_logger.log_activity(user_name, _LOG, f'Ctrl+V — {zprava}')
            await obnov()

    async def klik(e):
        a = e.args
        if a.get('colId') not in ('log', 'smazat') or a.get('rowPinned'):
            return
        r = radek((a.get('data') or {}).get('k') or '')
        if r is None:
            return
        if a['colId'] == 'log':
            await ukaz_log(r)
        elif pise:
            await smaz(r)

    async def smaz(r):
        with host, ui.dialog() as dlg, ui.card():
            ui.label(f"Smazat řádek „{r['nazev'] or 'bez názvu'}“?").classes('font-semibold')
            with ui.row().classes('w-full justify-end'):
                ui.button('Zrušit', on_click=lambda: dlg.submit(False)).props('flat')
                ui.button('Smazat', color='red', on_click=lambda: dlg.submit(True))
        ok = await dlg
        dlg.delete()
        if not ok:
            return
        async with zamek:
            try:
                await asyncio.to_thread(_smaz, r['id'], user_id, user_name)
            except Exception as e:
                ui.notify(f'Řádek se nepodařilo smazat: {e}', type='negative')
                return
            intranet_logger.log_activity(user_name, _LOG, f"Smazán řádek {r['nazev'] or 'bez názvu'} "
                                                          f"({_text(r['domluveno']) or '—'} / {_text(r['odsouhlaseno']) or '—'})")
            await obnov()

    async def ukaz_log(r):
        try:
            zaznamy = await asyncio.to_thread(_historie, r['id'])
        except Exception as e:
            ui.notify(f'Historii se nepodařilo načíst: {e}', type='negative')
            return
        sloupce = [{'name': c, 'label': c.capitalize(), 'field': c, 'align': 'left'}
                   for c in ('kdy', 'kdo', 'kam', 'bylo', 'je')]
        rows = [{'id': i, 'kdy': z['cas'].strftime('%d.%m.%Y %H:%M'), 'kdo': z['kdo'] or '', 'kam': z['kam'],
                 'bylo': z['bylo'] or '', 'je': z['je'] or ''} for i, z in enumerate(zaznamy)]
        with host, ui.dialog() as dlg, ui.card().classes('w-full max-w-3xl'):
            ui.label(f"Historie: {r['nazev'] or 'bez názvu'}").classes('text-lg font-semibold')
            ui.table(columns=sloupce, rows=rows, row_key='id', pagination=0) \
                .props('flat bordered dense separator=cell hide-bottom' if rows
                       else 'flat bordered dense no-data-label="Zatím žádné zápisy"') \
                .classes('w-full')
            ui.button('Zavřít', on_click=dlg.close).props('flat').classes('self-end')
        await dlg
        dlg.delete()

    async def export():
        try:
            radky = await asyncio.to_thread(_nacti)     # čerstvá data, ne to, co visí na obrazovce
            obsah = await asyncio.to_thread(_xlsx, radky)
        except Exception as e:
            ui.notify(f'Export se nepodařil: {e}', type='negative')
            return
        ui.download.content(obsah, f'Naklady_veletrh_{datetime.date.today():%Y%m%d}.xlsx')
        intranet_logger.log_activity(user_name, _LOG, 'Export do Excelu')

    ed = {':editable': 'p => !p.node.rowPinned'} if pise else {'editable': False}
    kc = {'width': 140, 'type': 'rightAligned', ':valueFormatter': _FMT_KC, 'cellDataType': False}
    castka = {**kc, 'cellEditor': 'agTextCellEditor', **ed}    # text → server parsuje „12 500,50“
    sloupce = [
        {'colId': 'cislo', 'headerName': '', 'width': 48, 'cellClass': 'nv-cislo', 'suppressNavigable': True,
         ':valueGetter': 'p => p.node.rowPinned ? "" : p.node.rowIndex + 1'},
        {'field': 'nazev', 'headerName': _SLOUPCE['nazev'], 'flex': 1, 'minWidth': 140, **ed},
        {'field': 'domluveno', 'headerName': _SLOUPCE['domluveno'], **castka},
        {'field': 'odsouhlaseno', 'headerName': _SLOUPCE['odsouhlaseno'], **castka},
        {'field': 'rozdil', 'headerName': 'Rozdíl', 'headerTooltip': 'Domluvený náklad − Náklad - Final '
                                                                      '(kladný = ušetřeno, záporný = přes domluvu)',
         'cellClassRules': {**KOPIE_COLDEF['cellClassRules'], ':nv-plus': 'p => p.value > 0',
                            ':nv-minus': 'p => p.value < 0'}, **kc},
        {'field': 'poznamka', 'headerName': _SLOUPCE['poznamka'], 'flex': 1, 'minWidth': 140,
         'wrapText': True, 'autoHeight': True, **ed},
        {'field': 'log', 'headerName': '', 'width': 44, ':cellRenderer': _ikona('visibility', 'Kdo co kam zapsal'),
         ':valueFormatter': '() => ""'},       # Ctrl+C: prázdná buňka, ne „true“
    ]
    if pise:
        sloupce.append({'field': 'smazat', 'headerName': '', 'width': 44,
                        ':cellRenderer': _ikona('delete', 'Smazat řádek'), ':valueFormatter': '() => ""'})

    with ui.card().classes('w-full p-4 gap-3 shadow-sm'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Náklady veletrh').classes('text-xl font-bold')
            ui.button('Export', icon='download', on_click=export).props('flat color=primary')
        # auto_size_columns=False: NiceGUI 3.x jinak přidá autoSizeStrategy, AG Grid 34 ji
        # s flex sloupcem odmítne a vykreslí prázdnou tabulku (bez hlaviček i řádků)
        ui.run_javascript(KOPIE_JS)
        grid = ui.aggrid({
            'columnDefs': sloupce,
            'rowData': radky_gridu(),
            'pinnedBottomRowData': soucet_radek(),
            ':getRowId': 'p => p.data.k',
            'domLayout': 'autoHeight',
            'stopEditingWhenCellsLoseFocus': True,
            'enterNavigatesVertically': True,
            'enterNavigatesVerticallyAfterEdit': True,
            'undoRedoCellEditing': True,
            'suppressNoRowsOverlay': True,     # prázdnou tabulku řekne řádek Součet 0 Kč
            'defaultColDef': {'sortable': False, 'resizable': True, 'suppressMovable': True, **KOPIE_COLDEF},
            ':onRowDataUpdated': 'p => p.api.refreshCells({columns: ["cislo"]})',   # čísla řádků po smazání
        }, theme='balham', auto_size_columns=False).style('height: auto') \
            .classes(f'w-full nv-grid {KOPIE_CLASS}' + (f' {KOPIE_VKLADANI_CLASS}' if pise else ''))
        grid.on('cellValueChanged', zmena)
        grid.on('cellClicked', klik)
        grid.on('vlozeni', vloz)


if __name__ == '__main__':      # kontrola parseru a formátu částek
    assert _castka(' 12 500,50 Kč') == Decimal('12500.50')
    assert _tsv('Stánek\t12 500,50\r\nDoprava\t\r\n') == [['Stánek', '12 500,50'], ['Doprava', '']]
    assert _tsv('"dva\nřádky"\t1\n\nx') == [['dva\nřádky', '1'], [''], ['x']] and _tsv(None) == []
    assert _castka('12 500') == Decimal('12500.00')
    assert _castka('') is None and _castka(None) is None
    for spatne in ('abc', '1,2,3', 'nan', 'inf', '1e13'):
        try:
            _castka(spatne)
        except ValueError:
            pass
        else:
            raise AssertionError(spatne)
    assert _text(Decimal('12500.00')) == '12 500' and _text(Decimal('12500.5')) == '12 500,50'
    assert _text(None) == '' and _castka(_text(Decimal('-1234567.89'))) == Decimal('-1234567.89')
    assert _soucet([Decimal('1.5'), None, Decimal(2)]) == Decimal('3.5') and _soucet([]) == 0
    assert _rozdil({'domluveno': Decimal(100), 'odsouhlaseno': Decimal(120)}) == Decimal(-20)
    assert _rozdil({'domluveno': Decimal(100), 'odsouhlaseno': None}) is None
    import openpyxl
    radky = [{'nazev': 'Stánek', 'domluveno': Decimal('12500.50'), 'odsouhlaseno': None, 'poznamka': ''},
             {'nazev': '=1+1', 'domluveno': Decimal(100), 'odsouhlaseno': Decimal(90), 'poznamka': '=2+2 zbytek'},
             {'nazev': 'Doprava', 'domluveno': Decimal(50), 'odsouhlaseno': Decimal(80), 'poznamka': 'navíc'}]
    ws = openpyxl.load_workbook(io.BytesIO(_xlsx(radky))).active
    assert [c.value for c in ws[1]] == ['Název nákladu', 'Domluvený náklad', 'Náklad - Final', 'Rozdíl', 'Poznámka']
    assert [c.value for c in ws[2]] == ['Stánek', 12500.5, None, None, None]
    assert ws['A3'].value == '=1+1' and ws['A3'].data_type == 's'
    assert ws['E3'].value == '=2+2 zbytek' and ws['E3'].data_type == 's'
    assert [c.value for c in ws[4]][:4] == ['Doprava', 50, 80, -30]
    assert [c.value for c in ws[5]] == ['Součet', 12650.5, 170, -20, None]
    print('OK')
