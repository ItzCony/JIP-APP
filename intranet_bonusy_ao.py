# -*- coding: utf-8 -*-
"""Modul Bonusy AO — 14 pobočkových dlaždic, v každé sekce „Podkladová tabulka“.

Struktura podkladové tabulky je 1:1 podle vzoru
„vzorová podkladová tabulka.xlsx“ (list Podklad_*): 47 sloupců od
„IČO - zadané“ po „nastavení Sklad6“.
"""

import asyncio
import datetime
import inspect
import io
import os
import re
import unicodedata

from nicegui import app, ui

import intranet_data
import intranet_logger
from intranet_ui_utils import refreshable_na_klienta

POBOCKY = [
    'Praha', 'Pardubice', 'Most', 'Jilemnice', 'Liberec', 'Nová role', 'Brno',
    'Ostrava', 'Plzeň', 'Olomouc', 'České Budějovice', 'Hodonín',
    'Horšovský Týn', 'Zlín',
]

INTERVALY = ['Měsíční', 'Kvartální', 'Pololetní', 'Roční']
SKLAD6 = ['Ano', 'Ano – Centrála', 'Ano – Skupinový']
AN = ['A', 'N']

# (db pole, hlavička ve vzoru, typ, šířka v gridu)
# typ: text | an | datum | cislo | procento | interval | sklad6
SLOUPCE = [
    ('ico_zadane',             'IČO - zadané',                            'text',     110),
    ('pobocka',                'Pobočka',                                 'text',     110),
    ('databaze',               'Databáze',                                'text',      90),
    ('ico_kontrola',           'IČO - kontrola',                          'text',     110),
    ('nazev_jmeno',            'Název jméno',                             'text',     200),
    ('nazev_provozovny',       'Název provozovny',                        'text',     200),
    ('dalsi_odbery',           'Další odběry (pobočky)',                  'text',     160),
    ('typ_bonusu',             'Typ bonusu',                              'text',     120),
    ('interval',               'Interval',                                'interval', 120),
    ('datum_od',               'Od',                                      'datum',    105),
    ('datum_do',               'Do',                                      'datum',    105),
    ('majetek',                'Majetek',                                 'text',     130),
    ('investice',              'Investice',                               'text',     130),
    ('cislo_dokladu',          'Číslo dokladu s prodlouženou splatností', 'text',     170),
    ('obrat_dp',               'Obrat - DP',                              'an',        95),
    ('bonus_dp',               'Bonus - DP',                              'an',        95),
    ('obrat_akce',             'Obrat - Akce',                            'an',       100),
    ('bonus_akce',             'Bonus - Akce',                            'an',       100),
    ('tabak',                  'Tabák',                                   'an',        80),
    ('ceniny',                 'Ceniny',                                  'an',        80),
    ('nektar_natura',          'Nektar Natura',                           'an',       110),
    ('premier_wines',          'Premier Wines',                           'an',       110),
    ('eso',                    'ESO',                                     'an',        70),
    ('pivo',                   'Pivo',                                    'an',        70),
    ('vyjmuto_z_obratu',       'Vyjmuto z obratu',                        'text',     160),
    ('bonus_pouze_nadrazena',  'Bonus pouze z nadřazené skupiny',         'text',     200),
    ('minimalni_odber',        'Minimální odběr',                         'cislo',    120),
    ('skupina',                'Skupina',                                 'text',     140),
    ('poznamka',               'Poznámka',                                'text',     200),
    ('poznamka_investice',     'Poznámka k investici',                    'text',     200),
    ('procento_bonusu',        '% bonusu',                                'procento',  95),
    ('procento_bonusu_tabak',  '% bonusu tabák',                          'procento', 110),
] + [
    sl
    for i in range(1, 8)
    for sl in ((f'interval_bonusu_{i}', 'interval bonusu', 'cislo', 120),
               (f'procento_{i}',        '%',               'procento', 80))
] + [
    ('sklad6',                 'nastavení Sklad6',                        'sklad6',   150),
]

_SQL_TYP = {
    'text':     'TEXT',
    'an':       'VARCHAR(3)',
    'datum':    'DATE',
    'cislo':    'DECIMAL(18,2)',
    'procento': 'DECIMAL(9,6)',
    'interval': 'VARCHAR(30)',
    'sklad6':   'VARCHAR(30)',
}
_TYP = {f: t for f, _h, t, _w in SLOUPCE}
POLE = [f for f, _h, _t, _w in SLOUPCE]
# `interval` je v MySQL rezervované slovo → všechna pole v SQL v backticích.
POLE_SQL = ', '.join(f'`{f}`' for f in POLE)

TABULKA = 'bonusy_ao_podklad'


# ─── normalizace / převody ────────────────────────────────────────────────────

def _norm(s) -> str:
    """Malá písmena bez diakritiky, sloučené mezery — pro porovnávání hlaviček."""
    s = ''.join(c for c in unicodedata.normalize('NFKD', str(s or ''))
                if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', s).strip().lower()


_INTERVAL_MAPA = {
    'mesicne': 'Měsíční', 'mesicni': 'Měsíční',
    'ctvrtletne': 'Kvartální', 'kvartalni': 'Kvartální', 'ctvrtletni': 'Kvartální',
    'pololetne': 'Pololetní', 'pololetni': 'Pololetní',
    'rocne': 'Roční', 'rocni': 'Roční',
}
_SKLAD6_MAPA = {
    'ano': 'Ano',
    'ano - centrala': 'Ano – Centrála', 'ano - na centrale': 'Ano – Centrála',
    'ano centrala': 'Ano – Centrála',
    'ano - skupinovy': 'Ano – Skupinový', 'ano skupinovy': 'Ano – Skupinový',
}


def preved(typ: str, hodnota):
    """Hodnota z Excelu / z editace gridu → hodnota do DB (nebo None)."""
    if hodnota is None:
        return None
    if isinstance(hodnota, str):
        hodnota = hodnota.strip()
        if not hodnota:
            return None

    if typ == 'datum':
        if isinstance(hodnota, datetime.datetime):
            return hodnota.date()
        if isinstance(hodnota, datetime.date):
            return hodnota
        for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y', '%d/%m/%Y'):
            try:
                return datetime.datetime.strptime(str(hodnota).strip(), fmt).date()
            except ValueError:
                continue
        return None

    if typ in ('cislo', 'procento'):
        if isinstance(hodnota, bool):
            return None
        if isinstance(hodnota, (int, float)):
            cislo = float(hodnota)
        else:
            txt = re.sub(r'[^\d,.\-]', '', str(hodnota)).replace(',', '.')
            if txt.count('.') > 1:            # 1.234.567 → oddělovač tisíců
                txt = txt.replace('.', '')
            try:
                cislo = float(txt)
            except ValueError:
                return None
            # ponytail: procento zadané jako „3" bereme jako 3 %, „0,03" jako zlomek.
            # Hranice 1 = 100 %; vyšší bonus než 100 % neexistuje.
            if typ == 'procento' and cislo > 1:
                cislo /= 100.0
        return round(cislo, 6 if typ == 'procento' else 2)

    if typ == 'an':
        z = _norm(hodnota)[:1].upper()
        return z if z in ('A', 'N') else None

    if typ == 'interval':
        return _INTERVAL_MAPA.get(_norm(hodnota)) or (
            hodnota if hodnota in INTERVALY else None)

    if typ == 'sklad6':
        return _SKLAD6_MAPA.get(_norm(hodnota).replace('–', '-')) or (
            hodnota if hodnota in SKLAD6 else None)

    return str(hodnota)[:2000]


def mapuj_hlavicky(hlavicka: list) -> dict:
    """Hlavičkový řádek z Excelu → {index sloupce: db pole}.

    Opakující se hlavičky („interval bonusu", „%") se párují zleva doprava,
    takže sedmice dvojic skončí ve správných polích.
    """
    volne = list(enumerate(_norm(h) for h in hlavicka))
    mapa = {}
    for pole, hlav, _t, _w in SLOUPCE:
        cil = _norm(hlav)
        for poz, (idx, h) in enumerate(volne):
            if h == cil:
                mapa[idx] = pole
                del volne[poz]
                break
    return mapa


# ─── DB ───────────────────────────────────────────────────────────────────────

def inicializace_bonusy_ao_db():
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        sloupce_ddl = ',\n                '.join(
            f'`{f}` {_SQL_TYP[t]}' for f, _h, t, _w in SLOUPCE)
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABULKA} (
                id      INT AUTO_INCREMENT PRIMARY KEY,
                pobocka_klic VARCHAR(50) NOT NULL,
                poradi  INT NOT NULL DEFAULT 0,
                {sloupce_ddl},
                INDEX idx_pobocka (pobocka_klic, poradi)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f'[bonusy_ao] Chyba inicializace DB: {e}')
    finally:
        conn.close()


def nacti_radky(pobocka: str) -> list[dict]:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            f'SELECT id, {POLE_SQL} FROM {TABULKA} '
            f'WHERE pobocka_klic=%s ORDER BY poradi, id', (pobocka,))
        radky = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    for r in radky:
        for f in POLE:
            v = r[f]
            if _TYP[f] == 'datum':
                r[f] = v.strftime('%d.%m.%Y') if v else ''
            elif _TYP[f] in ('cislo', 'procento'):
                r[f] = float(v) if v is not None else None
            else:
                r[f] = v or ''
    return radky


def pridej_radek(pobocka: str) -> int:
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT COALESCE(MAX(poradi), 0) + 1 FROM {TABULKA} '
                    f'WHERE pobocka_klic=%s', (pobocka,))
        poradi = cur.fetchone()[0]
        cur.execute(f'INSERT INTO {TABULKA} (pobocka_klic, poradi, `pobocka`) '
                    f'VALUES (%s, %s, %s)', (pobocka, poradi, pobocka))
        conn.commit()
        rid = cur.lastrowid
        cur.close()
        return rid
    finally:
        conn.close()


def uloz_bunku(rid: int, pole: str, hodnota) -> None:
    if pole not in POLE:                       # název sloupce jde do SQL přímo
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(f'UPDATE {TABULKA} SET `{pole}`=%s WHERE id=%s',
                    (preved(_TYP[pole], hodnota), rid))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def smaz_radky(ids: list[int]) -> int:
    if not ids:
        return 0
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {TABULKA} WHERE id IN '
                    f'({",".join(["%s"] * len(ids))})', tuple(ids))
        conn.commit()
        pocet = cur.rowcount
        cur.close()
        return pocet
    finally:
        conn.close()


def uloz_import(pobocka: str, radky: list[dict], nahradit: bool) -> int:
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        if nahradit:
            cur.execute(f'DELETE FROM {TABULKA} WHERE pobocka_klic=%s', (pobocka,))
            poradi = 0
        else:
            cur.execute(f'SELECT COALESCE(MAX(poradi), 0) FROM {TABULKA} '
                        f'WHERE pobocka_klic=%s', (pobocka,))
            poradi = cur.fetchone()[0]
        sql = (f'INSERT INTO {TABULKA} (pobocka_klic, poradi, {POLE_SQL}) '
               f'VALUES ({", ".join(["%s"] * (len(POLE) + 2))})')
        data = []
        for r in radky:
            poradi += 1
            data.append((pobocka, poradi, *[r.get(f) for f in POLE]))
        cur.executemany(sql, data)
        conn.commit()
        cur.close()
        return len(data)
    finally:
        conn.close()


# ─── import z Excelu ──────────────────────────────────────────────────────────

def nacti_listy(raw: bytes) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def parsuj_excel(raw: bytes, list_nazev: str, pobocka: str) -> tuple[list[dict], str]:
    """Vrátí (řádky připravené k uložení, chybová hláška)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        ws = wb[list_nazev]
        radky_it = ws.iter_rows(values_only=True)
        try:
            hlavicka = next(radky_it)
        except StopIteration:
            return [], 'List je prázdný.'
        mapa = mapuj_hlavicky(list(hlavicka))
        if len(mapa) < 10:
            return [], ('V prvním řádku listu nejsou hlavičky podkladové tabulky '
                        f'(rozpoznáno {len(mapa)} sloupců).')
        vysledek = []
        for radek in radky_it:
            if not any(v is not None and str(v).strip() != '' for v in radek):
                continue
            zaznam = {f: None for f in POLE}
            for idx, pole in mapa.items():
                if idx < len(radek):
                    zaznam[pole] = preved(_TYP[pole], radek[idx])
            zaznam['pobocka'] = zaznam.get('pobocka') or pobocka
            vysledek.append(zaznam)
        return vysledek, ''
    except KeyError:
        return [], f'List „{list_nazev}" v souboru není.'
    finally:
        wb.close()


# ─── UI ───────────────────────────────────────────────────────────────────────

# Chování gridu 1:1 jako tabulky v modulu Sankce: výška do patičky okna a
# sloupce nejdřív roztažené na obsah, pak dorovnané na plnou šířku tabulky.
_GRID_STYLE = 'height: calc(100vh - 360px); min-height: 420px'
_AUTOSIZE_FIT = (
    "function(p){var api=p.api;if(!api||!api.autoSizeAllColumns||!api.sizeColumnsToFit)return;"
    "requestAnimationFrame(function(){api.autoSizeAllColumns();"
    "var cols=api.getColumns?api.getColumns():null;if(!cols)return;"
    "var lim=cols.filter(function(c){return c.isVisible();})"
    ".map(function(c){return{key:c.getColId(),minWidth:Math.ceil(c.getActualWidth())};});"
    "api.sizeColumnsToFit({columnLimits:lim});});}"
)

_FMT_PROCENTO = ("function(p){return (p.value===null||p.value===undefined||p.value==='')"
                 " ? '' : (p.value*100).toFixed(2).replace('.',',')+' %';}")
_FMT_CISLO = ("function(p){return (p.value===null||p.value===undefined||p.value==='')"
              " ? '' : Number(p.value).toLocaleString('cs-CZ');}")


def _col_defs() -> list[dict]:
    cols = [{
        'headerName': '', 'field': '_sel', 'width': 44, 'minWidth': 44, 'maxWidth': 44,
        'pinned': 'left', 'checkboxSelection': True, 'headerCheckboxSelection': True,
        'sortable': False, 'editable': False, 'resizable': False, 'filter': False,
        'suppressMovable': True, 'headerTooltip': 'Označení řádků ke smazání',
    }]
    for pole, hlav, typ, sirka in SLOUPCE:
        col = {'headerName': hlav, 'field': pole, 'width': sirka,
               'editable': pole != 'pobocka', 'sortable': True, 'filter': True,
               'headerTooltip': hlav}
        if typ in ('interval', 'sklad6', 'an'):
            col['cellEditor'] = 'agSelectCellEditor'
            col['cellEditorParams'] = {
                'values': {'interval': INTERVALY, 'sklad6': SKLAD6, 'an': AN}[typ]}
        elif typ == 'procento':
            col[':valueFormatter'] = _FMT_PROCENTO
            col['type'] = 'numericColumn'
        elif typ == 'cislo':
            col[':valueFormatter'] = _FMT_CISLO
            col['type'] = 'numericColumn'
        cols.append(col)
    return cols


def _podkladova_tabulka(pobocka: str, user_name: str) -> None:
    stav = {'radky': nacti_radky(pobocka)}

    @ui.refreshable
    def _grid_box():
        grid = ui.aggrid({
            'columnDefs': _col_defs(),
            'rowData': stav['radky'],
            'defaultColDef': {'resizable': True, 'sortable': True, 'filter': True},
            'rowHeight': 32,
            'rowSelection': 'multiple',
            'suppressRowClickSelection': True,
            'suppressMovableColumns': True,
            'singleClickEdit': True,
            'stopEditingWhenCellsLoseFocus': True,
            ':onFirstDataRendered': _AUTOSIZE_FIT,
            ':onGridSizeChanged': _AUTOSIZE_FIT,
            ':getRowId': 'function(p){return String(p.data.id);}',
        }).classes('w-full').style(_GRID_STYLE)
        stav['grid'] = grid

        def _on_change(e):
            a = e.args or {}
            pole = a.get('colId')
            if pole not in POLE:
                return
            rid = (a.get('data') or {}).get('id')
            if not rid:
                return
            uloz_bunku(int(rid), pole, a.get('newValue'))
            for r in stav['radky']:
                if r['id'] == rid:
                    r[pole] = a.get('newValue')
                    break

        grid.on('cellValueChanged', _on_change)

        async def _smaz():
            vybrane = await grid.get_selected_rows()
            ids = [int(r['id']) for r in vybrane if r.get('id')]
            if not ids:
                ui.notify('Nejsou označené žádné řádky.', type='warning')
                return
            pocet = smaz_radky(ids)
            stav['radky'] = nacti_radky(pobocka)
            _grid_box.refresh()
            ui.notify(f'Smazáno řádků: {pocet}', type='positive')
            intranet_logger.log_activity(
                user_name, 'Bonusy AO', f'{pobocka}: smazáno {pocet} řádků podkladové tabulky')

        with ui.row().classes('w-full justify-between items-center mt-2'):
            ui.label(f'Řádků: {len(stav["radky"])}').classes('text-sm text-gray-500')
            ui.button('Smazat označené', icon='delete', on_click=_smaz) \
                .props('color=negative outline no-caps dense')

    def _skoc_na_konec():
        grid = stav.get('grid')
        if grid is None or not stav['radky']:
            return
        idx = len(stav['radky']) - 1
        grid.run_grid_method('ensureIndexVisible', idx, 'bottom')
        grid.run_grid_method('startEditingCell', {'rowIndex': idx, 'colKey': POLE[0]})

    def _pridej():
        if not pridej_radek(pobocka):
            ui.notify('Řádek se nepodařilo přidat.', type='negative')
            return
        stav['radky'] = nacti_radky(pobocka)
        _grid_box.refresh()
        # grid se po refreshi staví znovu – skok až je na klientovi
        ui.timer(0.3, _skoc_na_konec, once=True)

    # ── dialog importu ──
    imp = {'raw': None, 'listy': [], 'nazev': ''}
    with ui.dialog() as dlg_import, ui.card().classes('w-[560px]'):
        ui.label(f'Import podkladové tabulky – {pobocka}').classes('text-lg font-bold')
        ui.label('Očekává se .xlsx se stejnými hlavičkami jako vzorová podkladová '
                 'tabulka (hlavičky v prvním řádku listu).') \
            .classes('text-xs text-gray-500')

        list_sel = ui.select([], label='List v sešitu').classes('w-full')
        nahradit = ui.checkbox('Před importem smazat stávající řádky pobočky')

        async def _on_upload(e):
            zdroj = next((getattr(e, a, None) for a in ('content', 'file', 'stream', 'data')
                          if hasattr(getattr(e, a, None), 'read')), None)
            if zdroj is None:
                ui.notify('Nepodařilo se načíst obsah souboru.', type='negative')
                return
            raw = zdroj.read()
            if inspect.isawaitable(raw):
                raw = await raw
            try:
                listy = await asyncio.to_thread(nacti_listy, raw)
            except Exception as exc:
                ui.notify(f'Soubor nejde otevřít: {exc}', type='negative')
                return
            imp['raw'] = raw
            imp['listy'] = listy
            list_sel.options = listy
            list_sel.value = listy[0] if listy else None
            list_sel.update()
            ui.notify(f'Načteno, listů: {len(listy)}', type='positive')

        ui.upload(on_upload=_on_upload, auto_upload=True, max_file_size=50_000_000,
                  label='Vybrat .xlsx soubor').props('accept=.xlsx').classes('w-full')

        async def _importuj():
            if not imp['raw'] or not list_sel.value:
                ui.notify('Nejdřív nahrajte soubor a vyberte list.', type='warning')
                return
            radky, err = await asyncio.to_thread(
                parsuj_excel, imp['raw'], list_sel.value, pobocka)
            if err:
                ui.notify(f'Chyba: {err}', type='negative', timeout=10000)
                return
            if not radky:
                ui.notify('List neobsahuje žádné datové řádky.', type='warning')
                return
            pocet = await asyncio.to_thread(uloz_import, pobocka, radky, nahradit.value)
            stav['radky'] = nacti_radky(pobocka)
            _grid_box.refresh()
            dlg_import.close()
            ui.notify(f'Importováno řádků: {pocet}', type='positive')
            intranet_logger.log_activity(
                user_name, 'Bonusy AO',
                f'{pobocka}: import podkladové tabulky – {pocet} řádků '
                f'(list {list_sel.value}{", nahrazeno" if nahradit.value else ""})')

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Zavřít', on_click=dlg_import.close).props('flat no-caps')
            ui.button('Importovat', icon='upload', on_click=_importuj).props('color=primary no-caps')

    with ui.column().classes('w-full gap-2'):
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Přidat řádek', icon='add', on_click=_pridej) \
                .props('color=primary outline no-caps dense')
            ui.button('Import z Excelu', icon='upload_file', on_click=dlg_import.open) \
                .props('color=teal outline no-caps dense')
        _grid_box()


# ─── Data (SLK/XLSX z pobočkových složek) ─────────────────────────────────────

DATA_DIR = 'Bonusy'                 # kořen vedle aplikace: Bonusy/<pobočka>/Edit|Zpracovano
TABULKA_DATA = 'bonusy_ao_data'
TABULKA_KONTAKTY = 'bonusy_ao_kontakty'

# (db pole, hlavička, sql typ, šířka v gridu)
DATA_SLOUPCE = [
    ('ico',              'IČO',                       'VARCHAR(20)',   110),
    ('jmeno',            'Jméno',                     'VARCHAR(200)',  200),
    ('k_jmeno',          'K. Jméno',                  'VARCHAR(200)',  200),
    ('zakazka',          'Zakázka',                   'VARCHAR(30)',    90),
    ('typ_dokladu',      'Typ dokladu',               'VARCHAR(30)',   110),
    ('cislo_dokladu',    'Číslo dokladu',             'VARCHAR(30)',   120),
    ('datum',            'Datum zdanitelného plnění', 'DATE',          140),
    ('ext_id',           'Ext.ID',                    'VARCHAR(30)',   110),
    ('kod_zbozi',        'Kód zboží',                 'VARCHAR(30)',   100),
    ('nazev_zbozi',      'Název zboží',               'VARCHAR(150)',  220),
    ('skupina2',         'Skupina2',                  'VARCHAR(20)',    90),
    ('skupina2_popis',   'Skupina2-popis',            'VARCHAR(150)',  180),
    ('nadr_skup2',       'Nadř.skup.2',               'VARCHAR(20)',   100),
    ('nadr_skup2_popis', 'Nadř.skup.2 pop.',          'VARCHAR(150)',  180),
    ('dodavatel_popis',  'Dodavatel-popis',           'VARCHAR(150)',  180),
    ('sazba_dph',        'Sazba DPH',                 'VARCHAR(10)',    90),
    ('pouzita_ic',       'Použitá IC',                'VARCHAR(30)',   110),
    ('celk_bez_dph',     'Celk.bez DPH',              'DECIMAL(15,2)', 120),
]
DATA_POLE = [f for f, _h, _s, _w in DATA_SLOUPCE]
DATA_POLE_SQL = ', '.join(f'`{f}`' for f in DATA_POLE)

# pořadí sloupců ve zdrojovém souboru (hlavička je 5. řádek, data od 6.)
ZDROJ_POLE = [
    'ico', 'zakazka', 'typ_dokladu', 'cislo_dokladu', 'datum', 'ext_id', 'kod_zbozi',
    'nazev_zbozi', 'skupina2', 'skupina2_popis', 'nadr_skup2', 'nadr_skup2_popis',
    'dodavatel_popis', 'sazba_dph', 'pouzita_ic', 'celk_bez_dph',
]
PRVNI_DATOVY_RADEK = 6
VYNECHAT_DODAVATELE = {'fu', 'obaly'}       # DOD. popis „FÚ.“ a „Obaly“ → smazat řádky


def _slozka(pobocka: str, pod: str) -> str:
    cesta = os.path.join(DATA_DIR, pobocka, pod)
    os.makedirs(cesta, exist_ok=True)
    return cesta


def _datum_hodnota(hodnota):
    if isinstance(hodnota, datetime.datetime):
        return hodnota.date()
    if isinstance(hodnota, datetime.date):
        return hodnota
    txt = str(hodnota or '').strip().replace('/', '.')      # „/“ → „.“
    if not txt:
        return None
    for fmt in ('%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def _cislo_hodnota(hodnota):
    if isinstance(hodnota, (int, float)):
        return float(hodnota)
    txt = str(hodnota or '').strip().replace(' ', '').replace(',', '.')
    try:
        return float(txt)
    except ValueError:
        return None


_SLK_BUNKA = re.compile(r'C;(?:Y(\d+);)?(?:X(\d+);)?K(.*)')


def _radky_slk(cesta: str):
    y = x = 0
    radek: dict = {}
    with io.open(cesta, 'r', encoding='cp1250', errors='replace') as f:
        for line in f:
            if not line.startswith('C;'):
                continue
            m = _SLK_BUNKA.match(line.rstrip('\r\n'))
            if not m:
                continue
            sy, sx, k = m.groups()
            if sy and int(sy) != y:
                if radek:
                    yield y, radek
                radek = {}
                y = int(sy)
            if sx:
                x = int(sx)
            v = k.strip()
            if v.startswith('"'):
                v = v[1:-1] if v.endswith('"') else v[1:]
            radek[x] = v.strip()
    if radek:
        yield y, radek


def _radky_xlsx(cesta: str):
    import openpyxl
    wb = openpyxl.load_workbook(cesta, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        for y, radek in enumerate(ws.iter_rows(values_only=True), 1):
            yield y, {i: v for i, v in enumerate(radek, 1) if v is not None}
    finally:
        wb.close()


def zdrojove_radky(cesta: str):
    """Datové řádky zdroje – prvních 5 řádků (4 hlavičkové + názvy sloupců) pryč."""
    fn = _radky_slk if cesta.lower().endswith('.slk') else _radky_xlsx
    for y, radek in fn(cesta):
        if y >= PRVNI_DATOVY_RADEK:
            yield radek


def uprav_radek(zdroj: dict, kontakty: dict) -> list | None:
    """Pravidla úpravy. None = řádek se zahazuje."""
    hod = {}
    for i, pole in enumerate(ZDROJ_POLE, 1):
        v = zdroj.get(i)
        hod[pole] = v.strip() if isinstance(v, str) else v
    ico = str(hod['ico'] or '').strip()
    if not ico or _norm(ico) == 'celkem':
        return None                                     # prázdný / součtový řádek
    if ico == '00000000':
        ico = str(hod['ext_id'] or '').strip()          # Ext.ID → IČO
    if _norm(hod['typ_dokladu']).replace('.', '').startswith('zavst'):
        return None                                     # ZAVStrvzenka
    if _norm(hod['dodavatel_popis']).replace('.', '') in VYNECHAT_DODAVATELE:
        return None                                     # FÚ. / Obaly
    jmeno, k_jmeno = kontakty.get(ico, ('', ''))        # SVYHLEDAT podle IČO
    hod['ico'] = ico
    hod['jmeno'] = jmeno
    hod['k_jmeno'] = k_jmeno
    hod['datum'] = _datum_hodnota(hod['datum'])
    hod['celk_bez_dph'] = _cislo_hodnota(hod['celk_bez_dph'])
    return [None if hod.get(p) == '' else hod.get(p) for p in DATA_POLE]


def _obdobi_z_nazvu(nazev: str) -> str:
    m = re.search(r'(\d{2})[_\-. ](\d{4})', nazev)
    if m:
        return f'{m.group(2)}-{m.group(1)}'
    return datetime.date.today().strftime('%Y-%m')


# ─── Kontaktní údaje VO (číselník pro Jméno / K. jméno) ──────────────────────

def parsuj_kontakty(raw: bytes, nazev: str) -> list[tuple]:
    if nazev.lower().endswith('.xlsb'):
        import pyxlsb
        radky = []
        with pyxlsb.open_workbook(io.BytesIO(raw)) as wb:
            with wb.get_sheet(1) as ws:
                for r in ws.rows():
                    radky.append([c.v for c in r])
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            ws = wb[wb.sheetnames[0]]
            radky = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()

    def klic(v):
        return _norm(v).replace(' ', '').replace('.', '')

    mapa, start = {}, None
    for idx, r in enumerate(radky):
        volne = {klic(v): i for i, v in enumerate(r) if v not in (None, '')}
        if 'ico' in volne and 'jmeno' in volne:
            mapa, start = volne, idx + 1
            break
    if start is None:
        raise ValueError('V souboru nejsou hlavičky IČO / Jméno.')
    vysledek, videna = [], set()
    for r in radky[start:]:
        ico = str(r[mapa['ico']] or '').strip() if mapa['ico'] < len(r) else ''
        if not ico:
            continue
        def bunka(k):
            i = mapa.get(k)
            return str(r[i] or '').strip()[:200] if i is not None and i < len(r) else ''
        ico = ico[:20]
        if ico in videna:
            continue            # SVYHLEDAT bere první výskyt IČO
        videna.add(ico)
        vysledek.append((ico, bunka('jmeno'), bunka('kjmeno')))
    return vysledek


def uloz_kontakty(dvojice: list[tuple]) -> int:
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {TABULKA_KONTAKTY}')
        sql = f'INSERT INTO {TABULKA_KONTAKTY} (ico, jmeno, k_jmeno) VALUES (%s, %s, %s)'
        for i in range(0, len(dvojice), 5000):
            cur.executemany(sql, dvojice[i:i + 5000])
        conn.commit()
        cur.close()
        return len(dvojice)
    finally:
        conn.close()


def nacti_kontakty() -> dict:
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT ico, jmeno, k_jmeno FROM {TABULKA_KONTAKTY}')
        return {r[0]: (r[1] or '', r[2] or '') for r in cur.fetchall()}
    finally:
        conn.close()


def pocet_kontaktu() -> int:
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM {TABULKA_KONTAKTY}')
        return cur.fetchone()[0]
    finally:
        conn.close()


# ─── Zpracování souboru ──────────────────────────────────────────────────────

def inicializace_bonusy_data_db():
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        sloupce_ddl = ',\n                '.join(
            f'`{f}` {t}' for f, _h, t, _w in DATA_SLOUPCE)
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABULKA_DATA} (
                id      BIGINT AUTO_INCREMENT PRIMARY KEY,
                pobocka_klic VARCHAR(50) NOT NULL,
                obdobi  VARCHAR(7) NOT NULL,
                {sloupce_ddl},
                INDEX idx_pob_obd (pobocka_klic, obdobi),
                INDEX idx_ico (ico)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABULKA_KONTAKTY} (
                ico     VARCHAR(20) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                jmeno   VARCHAR(200),
                k_jmeno VARCHAR(200)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f'[bonusy_ao] Chyba inicializace DB dat: {e}')
    finally:
        conn.close()


def zpracuj_soubor(pobocka: str, nazev: str) -> tuple[int, str]:
    """Načte soubor z Edit, aplikuje pravidla, uloží do DB a do Zpracovano."""
    import openpyxl
    zdroj = os.path.join(_slozka(pobocka, 'Edit'), nazev)
    obdobi = _obdobi_z_nazvu(nazev)
    kontakty = nacti_kontakty()
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet(title='data')
    ws.append([h for _f, h, _s, _w in DATA_SLOUPCE])
    sql = (f'INSERT INTO {TABULKA_DATA} (pobocka_klic, obdobi, {DATA_POLE_SQL}) '
           f'VALUES ({", ".join(["%s"] * (len(DATA_POLE) + 2))})')
    davka, pocet = [], 0
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {TABULKA_DATA} WHERE pobocka_klic=%s AND obdobi=%s',
                    (pobocka, obdobi))
        conn.commit()
        for zdrojovy in zdrojove_radky(zdroj):
            radek = uprav_radek(zdrojovy, kontakty)
            if radek is None:
                continue
            ws.append(radek)
            davka.append((pobocka, obdobi, *radek))
            pocet += 1
            if len(davka) >= 5000:
                cur.executemany(sql, davka)
                conn.commit()
                davka = []
        if davka:
            cur.executemany(sql, davka)
            conn.commit()
        cur.close()
    finally:
        conn.close()
    cil = os.path.join(_slozka(pobocka, 'Zpracovano'),
                       f'{os.path.splitext(nazev)[0]}_zpracovano.xlsx')
    wb.save(cil)
    return pocet, cil


def nacti_obdobi(pobocka: str) -> list[tuple[str, int]]:
    """Období pobočky v DB, nejnovější první: [(obdobi, pocet_radku)]."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            f'SELECT obdobi, COUNT(*) FROM {TABULKA_DATA} WHERE pobocka_klic=%s '
            f'GROUP BY obdobi ORDER BY obdobi DESC', (pobocka,))
        out = [(r[0], r[1]) for r in cur.fetchall()]
        cur.close()
        return out
    finally:
        conn.close()


def smaz_obdobi(pobocka: str, obdobi: str) -> int:
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {TABULKA_DATA} WHERE pobocka_klic=%s AND obdobi=%s',
                    (pobocka, obdobi))
        pocet = cur.rowcount
        conn.commit()
        cur.close()
        return pocet
    finally:
        conn.close()


def nacti_data_nahled(pobocka: str, obdobi: str | None = None,
                      limit: int = 500) -> tuple[list[dict], int]:
    conn = intranet_data.get_db_connection()
    if not conn:
        return [], 0
    kde = 'pobocka_klic=%s' + (' AND obdobi=%s' if obdobi else '')
    par = (pobocka, obdobi) if obdobi else (pobocka,)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT COUNT(*) AS c FROM {TABULKA_DATA} WHERE {kde}', par)
        celkem = cur.fetchone()['c']
        cur.execute(
            f'SELECT {DATA_POLE_SQL} FROM {TABULKA_DATA} WHERE {kde} '
            f'ORDER BY id LIMIT {int(limit)}', par)
        radky = cur.fetchall()
        cur.close()
        for r in radky:
            if isinstance(r.get('datum'), (datetime.date, datetime.datetime)):
                r['datum'] = r['datum'].strftime('%d.%m.%Y')
            if r.get('celk_bez_dph') is not None:
                r['celk_bez_dph'] = float(r['celk_bez_dph'])
        return radky, celkem
    finally:
        conn.close()


def _soubory(cesta: str) -> list[tuple]:
    out = []
    for nazev in sorted(os.listdir(cesta)):
        plna = os.path.join(cesta, nazev)
        if os.path.isfile(plna) and not nazev.startswith('~$'):
            out.append((nazev, os.path.getsize(plna)))
    return out


def _velikost(b: int) -> str:
    for jed in ('B', 'kB', 'MB', 'GB'):
        if b < 1024 or jed == 'GB':
            return f'{b:.0f} {jed}' if jed == 'B' else f'{b:.1f} {jed}'
        b /= 1024
    return f'{b:.1f} GB'


def _data_col_defs() -> list[dict]:
    cols = []
    for f, h, typ, sirka in DATA_SLOUPCE:
        col = {'headerName': h, 'field': f, 'width': sirka,
               'sortable': True, 'filter': True, 'headerTooltip': h}
        if typ.startswith('DECIMAL'):
            col['type'] = 'numericColumn'
            col['valueFormatter'] = _FMT_CISLO
        cols.append(col)
    return cols


@refreshable_na_klienta
def _data_sekce(pobocka: str, user_name: str) -> None:
    inicializace_bonusy_data_db()
    edit = _slozka(pobocka, 'Edit')
    hotovo = _slozka(pobocka, 'Zpracovano')
    obdobi_klic = f'bonusy_ao_obdobi_{pobocka}'
    obdobi_list = nacti_obdobi(pobocka)
    obdobi_klice = [o for o, _ in obdobi_list]
    obdobi_sel = app.storage.user.get(obdobi_klic)
    if obdobi_sel not in obdobi_klice:
        obdobi_sel = obdobi_klice[0] if obdobi_klice else None
        app.storage.user[obdobi_klic] = obdobi_sel
    radky, celkem = nacti_data_nahled(pobocka, obdobi_sel)

    async def _zpracuj(nazev: str):
        notif = ui.notification(f'Zpracovávám {nazev}…', spinner=True, timeout=None)
        try:
            pocet, cil = await asyncio.to_thread(zpracuj_soubor, pobocka, nazev)
        except Exception as exc:
            notif.dismiss()
            ui.notify(f'Chyba zpracování: {exc}', type='negative', timeout=10000)
            return
        notif.dismiss()
        ui.notify(f'{nazev}: {pocet} řádků → {os.path.basename(cil)}', type='positive')
        intranet_logger.log_activity(
            user_name, 'Bonusy AO',
            f'{pobocka}: zpracován soubor {nazev} ({pocet} řádků)')
        app.storage.user[obdobi_klic] = _obdobi_z_nazvu(nazev)
        _data_sekce.refresh()

    def _zmen_obdobi(e):
        app.storage.user[obdobi_klic] = e.value
        _data_sekce.refresh()

    async def _smaz_obdobi():
        if not obdobi_sel:
            return
        pocet = await asyncio.to_thread(smaz_obdobi, pobocka, obdobi_sel)
        ui.notify(f'Smazáno období {obdobi_sel}: {pocet} řádků.', type='positive')
        intranet_logger.log_activity(
            user_name, 'Bonusy AO',
            f'{pobocka}: smazáno období {obdobi_sel} ({pocet} řádků)')
        app.storage.user.pop(obdobi_klic, None)
        dlg_smaz.close()
        _data_sekce.refresh()

    with ui.dialog() as dlg_smaz, ui.card().classes('w-[420px]'):
        ui.label('Smazat období').classes('text-lg font-bold')
        ui.label(f'Opravdu smazat všechna data pobočky {pobocka} za období '
                 f'{obdobi_sel}? ({celkem} řádků) Akce je nevratná – soubory ve '
                 f'složce Zpracovano zůstanou.').classes('text-sm text-gray-600')
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Zrušit', on_click=dlg_smaz.close).props('flat no-caps')
            ui.button('Smazat', icon='delete', on_click=_smaz_obdobi) \
                .props('color=negative no-caps')

    async def _on_kontakty(e):
        zdroj = None
        for att in ('content', 'file', 'stream', 'data', 'file_obj'):
            val = getattr(e, att, None)
            if val is not None and hasattr(val, 'read'):
                zdroj = val
                break
        if zdroj is None:
            ui.notify('Nepodařilo se načíst obsah souboru.', type='negative')
            return
        raw = zdroj.read()
        if inspect.isawaitable(raw):
            raw = await raw
        try:
            dvojice = await asyncio.to_thread(parsuj_kontakty, raw, e.name)
            pocet = await asyncio.to_thread(uloz_kontakty, dvojice)
        except Exception as exc:
            ui.notify(f'Chyba: {exc}', type='negative', timeout=10000)
            return
        ui.notify(f'Načteno {pocet} kontaktů.', type='positive')
        dlg_kontakty.close()
        _data_sekce.refresh()

    with ui.dialog() as dlg_kontakty, ui.card().classes('w-[560px]'):
        ui.label('Kontaktní údaje VO').classes('text-lg font-bold')
        ui.label('Nahraj nejnovější soubor „Kontaktní údaje – Změna OZ k DD.MM.YYYY“ '
                 '(.xlsb / .xlsx). Podle IČO se z něj doplňuje Jméno a K. jméno.') \
            .classes('text-xs text-gray-500')
        ui.label(f'V databázi: {pocet_kontaktu()} záznamů').classes('text-sm')
        ui.upload(on_upload=_on_kontakty, auto_upload=True, max_file_size=50_000_000,
                  label='Vybrat soubor').props('accept=.xlsb,.xlsx').classes('w-full')
        with ui.row().classes('w-full justify-end'):
            ui.button('Zavřít', on_click=dlg_kontakty.close).props('flat no-caps')

    with ui.column().classes('w-full gap-2 mt-6'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Data').classes('text-lg font-bold text-gray-800')
            with ui.row().classes('gap-2'):
                ui.button('Kontaktní údaje VO', icon='contacts',
                          on_click=dlg_kontakty.open) \
                    .props('color=primary outline no-caps dense')
                ui.button('Obnovit', icon='refresh', on_click=_data_sekce.refresh) \
                    .props('color=teal outline no-caps dense')
        ui.label(f'Vstup: {os.path.abspath(edit)}   •   Výstup: {os.path.abspath(hotovo)}') \
            .classes('text-xs text-gray-500')

        with ui.row().classes('w-full gap-4 items-stretch'):
            with ui.card().classes('flex-1 p-3 gap-2'):
                ui.label('Ke zpracování – složka Edit') \
                    .classes('text-sm font-semibold text-gray-700')
                soubory = _soubory(edit)
                if not soubory:
                    ui.label('Složka je prázdná.').classes('text-xs text-gray-400')
                for nazev, vel in soubory:
                    with ui.row().classes('items-center gap-2 w-full'):
                        ui.icon('description', color='grey-6')
                        ui.label(f'{nazev} ({_velikost(vel)})').classes('text-sm')
                        ui.space()
                        ui.button('Zpracovat', icon='play_arrow',
                                  on_click=lambda _e, n=nazev: _zpracuj(n)) \
                            .props('color=primary outline no-caps dense')
            with ui.card().classes('flex-1 p-3 gap-2'):
                ui.label('Hotové – složka Zpracovano') \
                    .classes('text-sm font-semibold text-gray-700')
                hotove = _soubory(hotovo)
                if not hotove:
                    ui.label('Zatím nic zpracovaného.').classes('text-xs text-gray-400')
                for nazev, vel in hotove:
                    with ui.row().classes('items-center gap-2 w-full'):
                        ui.icon('task_alt', color='green-6')
                        ui.label(f'{nazev} ({_velikost(vel)})').classes('text-sm')
                        ui.space()
                        ui.button('Stáhnout', icon='download',
                                  on_click=lambda _e, n=nazev:
                                      ui.download.file(os.path.join(hotovo, n), n)) \
                            .props('color=green outline no-caps dense')

        with ui.row().classes('w-full items-center gap-3 mt-2'):
            if obdobi_klice:
                ui.select({o: f'{o}  ({p} řádků)' for o, p in obdobi_list},
                          value=obdobi_sel, label='Období', on_change=_zmen_obdobi) \
                    .props('dense outlined options-dense').style('min-width: 220px')
                ui.button('Smazat období', icon='delete', on_click=dlg_smaz.open) \
                    .props('color=negative outline no-caps dense')
            else:
                ui.label('Žádná data v databázi.').classes('text-sm text-gray-400')
            ui.space()
            ui.label(f'V databázi: {celkem} řádků • náhled prvních {len(radky)}') \
                .classes('text-xs text-gray-500')
        ui.aggrid({
            'columnDefs': _data_col_defs(),
            'rowData': radky,
            'defaultColDef': {'resizable': True, 'sortable': True, 'filter': True},
            'rowHeight': 32,
            'suppressMovableColumns': True,
            ':onFirstDataRendered': _AUTOSIZE_FIT,
            ':onGridSizeChanged': _AUTOSIZE_FIT,
        }).classes('w-full').style(_GRID_STYLE)


def vykresli_bonusy_ao(user_id: int, user_name: str, vsechna_prava: list):
    if 'vse' not in vsechna_prava:
        with ui.column().classes('items-center py-24 gap-4'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('Modul Bonusy AO je zatím jen pro administrátora.') \
                .classes('text-gray-400 text-lg')
        return

    inicializace_bonusy_ao_db()
    _state_key = f'bonusy_ao_pobocka_{user_id}'

    @ui.refreshable
    def _panel():
        sel = app.storage.user.get(_state_key)
        if sel not in POBOCKY:
            ui.label('Bonusy AO').classes('text-3xl font-bold text-gray-800 mb-6')
            with ui.grid(columns=4).classes('w-full gap-4'):
                for p in POBOCKY:
                    with ui.card().classes(
                        'cursor-pointer hover:shadow-xl hover:-translate-y-0.5 transition-all '
                        'duration-200 p-6 items-center gap-2 rounded-2xl border border-gray-100'
                    ) as card:
                        ui.label('💰').classes('text-5xl mb-1')
                        ui.label(p).classes('text-lg font-bold text-center text-gray-800')
                        card.on('click', lambda pb=p: _otevri(pb))
        else:
            with ui.column().classes('w-full gap-0'):
                with ui.row().classes('items-center gap-3 mb-4'):
                    ui.button(icon='arrow_back', on_click=_zpet).props('flat round') \
                        .tooltip('Zpět na výběr pobočky')
                    ui.label(f'Bonusy AO – {sel}').classes('text-2xl font-bold text-gray-800')
                _sekce_defs = {
                    'podklad': ('📋 Podkladová tabulka',
                                lambda: _podkladova_tabulka(sel, user_name)),
                    'data':    ('🗂️ Data',
                                lambda: _data_sekce(sel, user_name)),
                }
                with ui.tabs().props(
                    'align=left active-color=primary indicator-color=primary'
                ).classes('w-full border-b border-gray-200') as _tabs_sekce:
                    for _k, (_lbl, _) in _sekce_defs.items():
                        ui.tab(_k, label=_lbl)
                with ui.tab_panels(_tabs_sekce, value='podklad').classes('w-full pt-4'):
                    for _k, (_, _fn) in _sekce_defs.items():
                        with ui.tab_panel(_k):
                            _fn()

    def _otevri(pb: str):
        app.storage.user[_state_key] = pb
        _panel.refresh()

    def _zpet():
        app.storage.user.pop(_state_key, None)
        _panel.refresh()

    _panel()
