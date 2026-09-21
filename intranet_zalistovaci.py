"""Zalistovací komise — statická data ze zápisu komise + dvojí vyjádření.

Mladší sourozenec Monitoru (intranet_monitory.py): sloupce sestavy nejsou pevné,
z nahraného sešitu se vezme hlavička a celý řádek se uloží jako JSON snímek.
Proti Monitoru jsou tu dvě kola vyjádření, a to POSTUPNĚ:

  1. nákup napíše vyjádření k položce,
  2. teprve pak smí kontrola potvrdit, že k zalistování došlo (ANO/NE + komentář).

Dokud nákup nenapsal, je potvrzení zamčené — v gridu i na serveru.

Další kola importu: nahrání téhož zápisu znovu (doplněný o nové položky) řádky
NEMAŽE. Páruje se přes `row_hash` (UNIQUE), takže se obnoví statická data a obě
vyjádření zůstanou. Nové řádky přibydou, staré zůstanou i když už v souboru
nejsou — komise se k nim vrací.
"""

from nicegui import ui, app
import intranet_data
import intranet_logger
import intranet_sankce
import intranet_monitory
from intranet_ui_utils import prekryv_kolecko, refreshable_na_klienta
import asyncio
import datetime
import inspect
import io
import json

# Sdílené helpery — tenhle modul je jejich mladší sourozenec a nemá smysl je
# psát podruhé (čtení buněk, export XLSX, audit, klíče sloupců, otisk řádku).
_norm = intranet_sankce._norm
_s = intranet_sankce._s
_f = intranet_sankce._f
_export_xlsx = intranet_sankce._export_xlsx
zapis_audit = intranet_sankce.zapis_audit
_klic_sloupce = intranet_monitory._klic_sloupce
_row_hash = intranet_monitory._row_hash
_obdobi_z_nazvu = intranet_monitory._obdobi_z_nazvu

TABULKA = 'zalistovaci_komise'
META_TABULKA = 'zalistovaci_komise_meta'
NAZEV = 'Zalistovací komise'

# Pole, která píší lidé (ne import). Mapa: pole → (kdo, kdy, právo).
_RUCNI = {
    'vyj_nakup':    ('vyj_nakup_by', 'vyj_nakup_at', 'nakup'),
    'zalistovano':  ('vyj_kontrola_by', 'vyj_kontrola_at', 'kontrola'),
    'vyj_kontrola': ('vyj_kontrola_by', 'vyj_kontrola_at', 'kontrola'),
}
_STAVY = ['', 'ANO', 'NE']

# Sloupce, které z hlavičky zápisu tvoří identitu řádku. Vezmou se ty, které
# v listu opravdu jsou — zápis komise a list „Změna K2" mají jinou hlavičku.
_KLIC_KANDIDATI = ('kod9', 'kod_2', 'kod2', 'kod', 'nazev', 'dodavatel', 'nakupci', 'nak')


# ============================================================
# ==                          DB                            ==
# ============================================================

def inicializace_db():
    """Tabulka zápisů + tabulka hlaviček. Volá se líně při renderu (IF NOT EXISTS)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABULKA} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                obdobi VARCHAR(60),
                obdobi_od DATE,
                list_nazev VARCHAR(60) NOT NULL DEFAULT '',
                row_hash VARCHAR(40) NOT NULL,
                data JSON NOT NULL,
                vyj_nakup TEXT,
                vyj_nakup_by VARCHAR(255),
                vyj_nakup_at DATETIME DEFAULT NULL,
                zalistovano VARCHAR(10) NOT NULL DEFAULT '',
                vyj_kontrola TEXT,
                vyj_kontrola_by VARCHAR(255),
                vyj_kontrola_at DATETIME DEFAULT NULL,
                import_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                imported_by VARCHAR(255),
                UNIQUE KEY uniq_hash (row_hash),
                INDEX idx_obdobi (obdobi), INDEX idx_list (list_nazev)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {META_TABULKA} (
                list_nazev VARCHAR(60) NOT NULL PRIMARY KEY,
                sloupce JSON NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f'[zalistovaci] Chyba při inicializaci DB: {e}')
    finally:
        conn.close()


def _nacti(obdobi: str = None, list_nazev: str = None) -> list:
    """Řádky zápisu: JSON `data` rozbalené do plochého dictu pro grid."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        sql = (f'SELECT id,obdobi,list_nazev,row_hash,data,vyj_nakup,vyj_nakup_by,'
               f'vyj_nakup_at,zalistovano,vyj_kontrola,vyj_kontrola_by,'
               f'vyj_kontrola_at FROM {TABULKA}')
        kde, params = [], []
        if obdobi:
            kde.append('obdobi=%s'); params.append(obdobi)
        if list_nazev:
            kde.append('list_nazev=%s'); params.append(list_nazev)
        if kde:
            sql += ' WHERE ' + ' AND '.join(kde)
        cur.execute(sql + ' ORDER BY obdobi_od DESC, id ASC', tuple(params))
        rows = []
        for r in cur.fetchall():
            try:
                data = json.loads(r['data']) if isinstance(r['data'], str) else (r['data'] or {})
            except Exception:
                data = {}
            radek = dict(data)
            radek.update({
                'id': r['id'], 'obdobi': r['obdobi'], 'list_nazev': r['list_nazev'],
                'row_hash': r['row_hash'],
                'vyj_nakup': r['vyj_nakup'] or '',
                'vyj_nakup_by': r['vyj_nakup_by'] or '',
                'vyj_nakup_at': _kdy(r['vyj_nakup_at']),
                'zalistovano': r['zalistovano'] or '',
                'vyj_kontrola': r['vyj_kontrola'] or '',
                'vyj_kontrola_by': r['vyj_kontrola_by'] or '',
                'vyj_kontrola_at': _kdy(r['vyj_kontrola_at']),
            })
            rows.append(radek)
        cur.close()
        return rows
    finally:
        conn.close()


def _kdy(v) -> str:
    return v.strftime('%d.%m.%Y %H:%M') if v else ''


def _nacti_meta() -> dict:
    """{list_nazev: [{'k': klíč, 'n': nadpis}, …]} — hlavičky posledního importu."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT list_nazev, sloupce FROM {META_TABULKA}')
        out = {}
        for r in cur.fetchall():
            try:
                sl = json.loads(r['sloupce']) if isinstance(r['sloupce'], str) else r['sloupce']
            except Exception:
                sl = []
            out[r['list_nazev']] = sl or []
        cur.close()
        return out
    finally:
        conn.close()


def _seznam_obdobi() -> list:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT obdobi FROM {TABULKA} WHERE obdobi IS NOT NULL '
                    f'GROUP BY obdobi, obdobi_od ORDER BY obdobi_od DESC')
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


# ============================================================
# ==                        IMPORT                          ==
# ============================================================

def _importuj_sync(raw: bytes, nazev_souboru: str, user_name: str, pokrok=None):
    """Načte VŠECHNY listy sešitu a upsertne je. Vrací (nových, aktualizovaných, chyba)."""
    import openpyxl

    od_iso, _do = _obdobi_z_nazvu(nazev_souboru)
    if not od_iso:
        return 0, 0, ('Z názvu souboru nejde vyčíst datum komise. Pojmenujte soubor '
                      's datem, např. „Zápis ze zalistovací komise_2026-05-13.xlsx".')
    obdobi = datetime.date.fromisoformat(od_iso).strftime('%d.%m.%Y')

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as e:
        return 0, 0, f'Soubor nejde otevřít ({e}) — očekávám .xlsx / .xlsm.'

    zaznamy, meta = [], {}
    try:
        for ws in wb.worksheets:
            hlavicka, klice, videno = None, None, {}
            skupina = None          # nákupčí stojí v souboru na vlastním řádku nad svými položkami
            for r in ws.iter_rows(values_only=True):
                if r is None or all(c is None or _s(c).strip() == '' for c in r):
                    continue
                if hlavicka is None:            # první neprázdný řádek je hlavička
                    hlavicka = list(r)
                    klice = _klic_sloupce(hlavicka)
                    meta[ws.title] = [{'k': k, 'n': _s(h).strip() or k}
                                      for k, h in zip(klice, hlavicka)]
                    continue
                radek = {}
                for i, k in enumerate(klice):
                    v = r[i] if i < len(r) else None
                    if isinstance(v, (datetime.date, datetime.datetime)):
                        v = v.strftime('%d.%m.%Y')
                    radek[k] = v if isinstance(v, (int, float)) else _s(v).strip()
                if not any(_s(v).strip() for v in radek.values()):
                    continue
                # Řádek jen s prvním sloupcem = hlavička skupiny (nákupčí); protáhneme ji dolů,
                # ať každá položka ví, čí je - jinak se podle nákupčího nedá filtrovat.
                prvni = klice[0]
                if _s(radek.get(prvni)).strip() and not any(
                        _s(v).strip() for k, v in radek.items() if k != prvni):
                    skupina = radek[prvni]
                    continue
                if not _s(radek.get(prvni)).strip():
                    radek[prvni] = skupina or ''
                casti = [radek.get(k) for k in _KLIC_KANDIDATI if k in radek] or \
                        list(radek.values())
                ckl = '|'.join(_s(c) for c in casti)
                videno[ckl] = videno.get(ckl, 0) + 1
                zaznamy.append((ws.title,
                                _row_hash(TABULKA, ws.title, obdobi, casti, videno[ckl]),
                                radek))
    finally:
        wb.close()

    if pokrok:
        pokrok(0.8)
    if not zaznamy:
        return 0, 0, 'V sešitu nejsou žádné datové řádky.'

    conn = intranet_data.get_db_connection()
    if not conn:
        return 0, 0, 'Chyba připojení k databázi.'
    try:
        # Období se přepíše celé (jinak by po opravě buňky zůstal v gridu i starý
        # řádek se starým hashem), obě vyjádření se párují zpět přes row_hash.
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT row_hash, vyj_nakup, vyj_nakup_by, vyj_nakup_at, zalistovano, '
                    f'vyj_kontrola, vyj_kontrola_by, vyj_kontrola_at '
                    f'FROM {TABULKA} WHERE obdobi=%s', (obdobi,))
        zachov = {x['row_hash']: x for x in cur.fetchall()}
        cur.close()
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {TABULKA} WHERE obdobi=%s', (obdobi,))
        cur.executemany(
            f'INSERT INTO {TABULKA} (obdobi, obdobi_od, list_nazev, row_hash, data, imported_by, '
            f'vyj_nakup, vyj_nakup_by, vyj_nakup_at, zalistovano, '
            f'vyj_kontrola, vyj_kontrola_by, vyj_kontrola_at) '
            f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            [(obdobi, od_iso, list_nazev, rh,
              json.dumps(radek, ensure_ascii=False, default=str), user_name,
              (z := zachov.get(rh, {})).get('vyj_nakup'), z.get('vyj_nakup_by'),
              z.get('vyj_nakup_at'), z.get('zalistovano') or '',
              z.get('vyj_kontrola'), z.get('vyj_kontrola_by'), z.get('vyj_kontrola_at'))
             for list_nazev, rh, radek in zaznamy])
        cur.executemany(
            f'INSERT INTO {META_TABULKA} (list_nazev, sloupce) VALUES (%s,%s) '
            f'ON DUPLICATE KEY UPDATE sloupce=VALUES(sloupce)',
            [(l, json.dumps(sl, ensure_ascii=False)) for l, sl in meta.items()])
        conn.commit()
        cur.close()
    except Exception as e:
        return 0, 0, f'Chyba zápisu do databáze: {e}'
    finally:
        conn.close()

    novych = len({rh for _l, rh, _r in zaznamy} - set(zachov))
    return novych, len(zaznamy) - novych, None


def _smaz_obdobi(obdobi: str) -> int:
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {TABULKA} WHERE obdobi=%s', (obdobi,))
        pocet = cur.rowcount
        conn.commit()
        cur.close()
        return pocet
    finally:
        conn.close()


# ============================================================
# ==                         PRÁVA                          ==
# ============================================================

def prava(vsechna_prava) -> tuple:
    """(vidí, píše za nákup, potvrzuje zalistování, je správce, smí nahrávat).

    Vkladatel je role napříč — data nahraje, ale žádná neuvidí, proto se
    nedědí ze čtenáře."""
    ma_vse = 'vse' in vsechna_prava
    admin = ma_vse or 'zalistovaci_admin' in vsechna_prava
    nakup = admin or 'zalistovaci_nakup' in vsechna_prava
    kontrola = admin or 'zalistovaci_kontrola' in vsechna_prava
    vidi = nakup or kontrola or 'zalistovaci_ctenar' in vsechna_prava
    vklad = admin or 'zalistovaci_vkladatel' in vsechna_prava
    return vidi, nakup, kontrola, admin, vklad


# ============================================================
# ==                     GRID + ZÁPIS                       ==
# ============================================================

# Potvrzení je zamčené, dokud nákup nenapsal vyjádření — kontrola potvrzuje
# jeho stanovisko, ne prázdný řádek. Na serveru se to kontroluje znovu.
_EDIT_KONTROLA = ("function(p){return !!(p.data && "
                  "String(p.data.vyj_nakup||'').trim().length);}")
_STAV_STYLE = ("function(p){var v=String(p.value||'');"
               "if(v==='ANO')return {backgroundColor:'#dcfce7',fontWeight:'600'};"
               "if(v==='NE')return {backgroundColor:'#fee2e2',fontWeight:'600'};"
               "return {backgroundColor:'#f8fafc'};}")


_PRAZDNE_FMT = ("function(p){var v=p.value;"
                "return (v===null||v===undefined||String(v).trim()==='') ? '-' : v;}")


def _col_defs(sloupce: list, psat_nakup: bool, psat_kontrola: bool) -> list:
    cols = [{'headerName': s['n'], 'field': s['k'], 'width': 150, 'sortable': True,
             ':valueFormatter': _PRAZDNE_FMT}
            for s in sloupce]
    cols += [
        {'headerName': 'Vyjádření nákupu', 'field': 'vyj_nakup', 'width': 300,
         'editable': psat_nakup, 'cellEditor': 'agLargeTextCellEditor',
         'cellEditorPopup': True, 'cellStyle': {'backgroundColor': '#fffbeb'},
         'headerTooltip': 'Stanovisko nákupu k položce — píše nákup jako první.'},
        {'headerName': 'Zapsal', 'field': 'vyj_nakup_by', 'width': 140,
         'editable': False, 'cellStyle': {'fontSize': '12px', 'color': '#475569'}},
        {'headerName': 'Kdy', 'field': 'vyj_nakup_at', 'width': 120, 'editable': False,
         'cellStyle': {'fontSize': '12px', 'color': '#475569'}},
        {'headerName': 'Zalistováno', 'field': 'zalistovano', 'width': 120,
         ':editable': _EDIT_KONTROLA if psat_kontrola else 'function(p){return false;}',
         'cellEditor': 'agSelectCellEditor',
         'cellEditorParams': {'values': _STAVY},
         ':cellStyle': _STAV_STYLE,
         'headerTooltip': 'Potvrzení kontroly — odemkne se, až nákup napíše vyjádření.'},
        {'headerName': 'Komentář kontroly', 'field': 'vyj_kontrola', 'width': 280,
         ':editable': _EDIT_KONTROLA if psat_kontrola else 'function(p){return false;}',
         'cellEditor': 'agLargeTextCellEditor', 'cellEditorPopup': True,
         'cellStyle': {'backgroundColor': '#eff6ff'},
         'headerTooltip': 'Doplňující komentář ke kontrole zalistování.'},
        {'headerName': 'Potvrdil', 'field': 'vyj_kontrola_by', 'width': 140,
         'editable': False, 'cellStyle': {'fontSize': '12px', 'color': '#475569'}},
        {'headerName': 'Kdy', 'field': 'vyj_kontrola_at', 'width': 120, 'editable': False,
         'cellStyle': {'fontSize': '12px', 'color': '#475569'}},
    ]
    return cols


def _uloz(pole: str, radek: dict, hodnota: str, user_id, user_name: str) -> bool:
    """Zápis jedné buňky (DB + audit + razítko kdo/kdy). Vrací, zda se zapsalo."""
    if (radek.get(pole) or '') == (hodnota or ''):
        return False
    sl_by, sl_at, _role = _RUCNI[pole]
    # Invarianty modulu hlídáme tady - tudy vede jediná cesta do DB.
    if pole == 'zalistovano' and (hodnota or '') not in _STAVY:
        return False
    if _role == 'kontrola' and not (radek.get('vyj_nakup') or '').strip():
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        ui.notify('Není spojení s databází — nic se neuložilo.', type='negative')
        return False
    try:
        cur = conn.cursor()
        cur.execute(f'UPDATE {TABULKA} SET {pole}=%s, {sl_by}=%s, {sl_at}=NOW() '
                    f'WHERE id=%s', (hodnota, user_name, radek.get('id')))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    zapis_audit(TABULKA, radek.get('row_hash'), radek.get('id'), pole,
                radek.get(pole), hodnota, user_id, user_name)
    radek[pole] = hodnota
    radek[sl_by] = user_name
    radek[sl_at] = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    return True


@refreshable_na_klienta
async def vykresli_zalistovaci(user_id, user_name: str, vsechna_prava):
    await asyncio.to_thread(inicializace_db)
    vidi, psat_nakup, psat_kontrola, je_admin, smi_vkladat = prava(vsechna_prava)

    if not vidi and not smi_vkladat:
        with ui.column().classes('items-center py-20 gap-3 w-full'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('Nemáte přístup k Zalistovací komisi.').classes('text-lg text-gray-400')
        return

    with ui.row().classes('w-full items-center gap-3 mb-4'):
        ui.icon('how_to_vote', size='2.2rem').classes('text-indigo-600')
        with ui.column().classes('gap-0'):
            ui.label(NAZEV).classes('text-3xl font-extrabold text-gray-800')
            ui.label('Zápis z komise — vyjádření nákupu a potvrzení zalistování') \
                .classes('text-sm text-gray-500')
        ui.space()
        if smi_vkladat:
            _import_button(user_name, vykresli_zalistovaci.refresh)

    if not vidi:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('upload_file', size='4rem', color='grey-4')
            ui.label('Smíte pouze nahrávat data.').classes('text-xl text-gray-400 font-bold')
            ui.label('Sestavu zobrazit nemůžete.').classes('text-sm text-gray-400')
        return

    obdobi_list = await asyncio.to_thread(_seznam_obdobi)
    if not obdobi_list:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('inventory_2', size='4rem', color='grey-4')
            ui.label('Zatím není naimportován žádný zápis.') \
                .classes('text-xl text-gray-400 font-bold')
            if smi_vkladat:
                ui.label('Nahrajte soubor tlačítkem „Nahrát zápis" vpravo nahoře.') \
                    .classes('text-sm text-gray-400')
        return

    meta = await asyncio.to_thread(_nacti_meta)
    ulozene = app.storage.user.get('zalistovaci_obdobi')
    stav = {'obdobi': ulozene if ulozene in obdobi_list else obdobi_list[0],
            'list': None, 'filtr': 'vse'}

    vsechny = await asyncio.to_thread(_nacti, stav['obdobi'])
    listy = sorted({r.get('list_nazev') or '' for r in vsechny})
    stav['list'] = listy[0] if listy else ''

    def _zobrazene() -> list:
        data = [r for r in vsechny if (r.get('list_nazev') or '') == stav['list']]
        if stav['filtr'] == 'bez_nakupu':
            data = [r for r in data if not (r.get('vyj_nakup') or '').strip()]
        elif stav['filtr'] == 'ceka':
            data = [r for r in data if (r.get('vyj_nakup') or '').strip()
                    and not (r.get('zalistovano') or '').strip()]
        elif stav['filtr'] == 'hotovo':
            data = [r for r in data if (r.get('zalistovano') or '').strip()]
        return data

    def _info_text() -> str:
        data = [r for r in vsechny if (r.get('list_nazev') or '') == stav['list']]
        s_nakupem = sum(1 for r in data if (r.get('vyj_nakup') or '').strip())
        potvrzeno = sum(1 for r in data if (r.get('zalistovano') or '').strip())
        return (f'{len(data)} položek · vyjádření nákupu {s_nakupem} · '
                f'potvrzeno {potvrzeno}')

    with ui.row().classes('w-full items-center gap-3 mb-2 flex-wrap'):
        sel_obd = ui.select(obdobi_list, value=stav['obdobi'], label='Komise ze dne') \
            .props('outlined dense options-dense').classes('w-48')
        sel_list = ui.select(listy, value=stav['list'], label='List') \
            .props('outlined dense options-dense').classes('w-56')
        sel_list.set_visibility(len(listy) > 1)
        sel_filtr = ui.select({'vse': 'Vše', 'bez_nakupu': 'Bez vyjádření nákupu',
                               'ceka': 'Čeká na potvrzení', 'hotovo': 'Potvrzené'},
                              value='vse', label='Stav') \
            .props('outlined dense options-dense').classes('w-56')
        ui.space()
        info = ui.label('').classes('text-sm text-gray-500')
        ui.button(icon='download', text='Export', on_click=lambda: _export()) \
            .props('color=secondary outline dense no-caps') \
            .tooltip('Stáhne .xlsx podle zvoleného listu a filtru.')
        if je_admin:
            ui.button(icon='delete_forever', text='Smazat období',
                      on_click=lambda: _smazat_dialog()) \
                .props('color=negative outline dense no-caps') \
                .tooltip('Nevratně smaže celý zápis zvoleného data včetně vyjádření.')

    if psat_kontrola and not psat_nakup:
        ui.label('✔ Potvrzujete zalistování. Buňka se odemkne až poté, co nákup '
                 'napíše své vyjádření.').classes('text-xs text-gray-500 mb-1')
    elif psat_nakup and not psat_kontrola:
        ui.label('✍ Píšete vyjádření za nákup. Potvrzení zalistování dělá kontrola.') \
            .classes('text-xs text-gray-500 mb-1')
    elif not psat_nakup and not psat_kontrola:
        ui.label('👁 Sestavu vidíte jen pro čtení — filtrovat, řadit a exportovat '
                 'můžete.').classes('text-xs text-gray-500 mb-1')

    sloupce = meta.get(stav['list'], [])
    grid = ui.aggrid({
        'columnDefs': _col_defs(sloupce, psat_nakup, psat_kontrola),
        'rowData': _zobrazene(),
        # cellDataType vypnuté: AG v31+ si jinak odvodí typ sloupce z prvních řádků
        # a prázdnou buňku v číselném sloupci vypíše jako „Invalid Number".
        'defaultColDef': {'resizable': True, 'sortable': False, 'filter': True,
                          'cellDataType': False},
        'rowHeight': 32,
        'singleClickEdit': True,
        'stopEditingWhenCellsLoseFocus': True,
        'suppressMovableColumns': True,
        ':getRowId': intranet_sankce._GET_ROW_ID,
        ':onFirstDataRendered': intranet_sankce._AUTOSIZE_FIT,
        ':onGridSizeChanged': intranet_sankce._AUTOSIZE_FIT,
    }).classes('w-full').style(intranet_sankce._GRID_STYLE)

    def _prekresli():
        grid.options['columnDefs'] = _col_defs(meta.get(stav['list'], []),
                                               psat_nakup, psat_kontrola)
        grid.options['rowData'] = _zobrazene()
        grid.update()
        info.set_text(_info_text())

    async def _zmen_obdobi(e):
        nonlocal vsechny, listy
        stav['obdobi'] = e.value
        app.storage.user['zalistovaci_obdobi'] = e.value
        vsechny = await asyncio.to_thread(_nacti, stav['obdobi'])
        listy = sorted({r.get('list_nazev') or '' for r in vsechny})
        stav['list'] = listy[0] if listy else ''
        sel_list.set_options(listy, value=stav['list'])
        sel_list.set_visibility(len(listy) > 1)
        _prekresli()

    def _zmen_list(e):
        stav['list'] = e.value or ''
        _prekresli()

    def _zmen_filtr(e):
        stav['filtr'] = e.value or 'vse'
        _prekresli()

    sel_obd.on_value_change(_zmen_obdobi)
    sel_list.on_value_change(_zmen_list)
    sel_filtr.on_value_change(_zmen_filtr)
    info.set_text(_info_text())

    # ── Zápis vyjádření ──
    def _on_change(e):
        a = e.args or {}
        pole = a.get('colId')
        if pole not in _RUCNI:
            return
        rid = (a.get('data') or {}).get('id')
        radek = next((r for r in vsechny if r.get('id') == rid), None)
        if radek is None:
            return
        _sl_by, _sl_at, role = _RUCNI[pole]
        # Server kontroluje totéž co zámek v gridu — grid jen zamyká buňku.
        if (role == 'nakup' and not psat_nakup) or (role == 'kontrola' and not psat_kontrola):
            ui.notify('K tomuto sloupci nemáte právo.', type='warning')
            _prekresli()
            return
        if role == 'kontrola' and not (radek.get('vyj_nakup') or '').strip():
            ui.notify('Nejdřív musí napsat vyjádření nákup, teprve pak se potvrzuje.',
                      type='warning')
            _prekresli()
            return
        nova = _s(a.get('newValue') or '').strip()
        if pole == 'zalistovano' and nova not in _STAVY:
            ui.notify('Zalistováno smí být jen ANO nebo NE.', type='warning')
            _prekresli()
            return
        if _uloz(pole, radek, nova, user_id, user_name):
            intranet_logger.log_activity(user_name, NAZEV,
                                         f'{pole} #{rid} ({stav["obdobi"]})')
            grid.run_grid_method('applyTransaction', {'update': [radek]})
            info.set_text(_info_text())

    grid.on('cellValueChanged', _on_change)

    async def _export():
        data = _zobrazene()
        cols = [(s['n'], s['k'], 'text', 18) for s in meta.get(stav['list'], [])]
        cols += [('Vyjádření nákupu', 'vyj_nakup', 'text', 40),
                 ('Zapsal', 'vyj_nakup_by', 'text', 18),
                 ('Kdy', 'vyj_nakup_at', 'text', 16),
                 ('Zalistováno', 'zalistovano', 'text', 14),
                 ('Komentář kontroly', 'vyj_kontrola', 'text', 40),
                 ('Potvrdil', 'vyj_kontrola_by', 'text', 18),
                 ('Kdy', 'vyj_kontrola_at', 'text', 16)]
        await _export_xlsx(cols, data, None,
                           f'Zalistovaci_komise_{stav["obdobi"].replace(".", "-")}',
                           sheet=(stav['list'] or 'Data')[:31])

    def _smazat_dialog():
        with ui.dialog() as dlg, ui.card().classes('gap-3'):
            ui.label(f'Smazat zápis komise {stav["obdobi"]}?') \
                .classes('text-lg font-bold')
            ui.label('Smaže se celý zápis včetně vyjádření nákupu i potvrzení '
                     'kontroly. Nevratné.').classes('text-sm text-gray-600')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')

                async def _potvrd():
                    dlg.close()
                    pocet = await asyncio.to_thread(_smaz_obdobi, stav['obdobi'])
                    intranet_logger.log_activity(user_name, NAZEV,
                                                 f'Smazán zápis {stav["obdobi"]} '
                                                 f'({pocet} řádků)')
                    ui.notify(f'Smazáno {pocet} řádků.', type='positive')
                    vykresli_zalistovaci.refresh()

                ui.button('Smazat', on_click=_potvrd).props('color=negative no-caps')
        dlg.open()


# ============================================================
# ==                    IMPORT — DIALOG                     ==
# ============================================================

def _import_button(user_name: str, refresh_fn):
    ui.button(icon='upload_file', text='Nahrát zápis',
              on_click=lambda: _otevri_import_dialog(user_name, refresh_fn)) \
        .props('color=primary dense no-caps') \
        .tooltip('Nahraje .xlsx zápisu komise. Opakovaný import doplní nové '
                 'položky a obnoví data — vyjádření zůstanou.')


def _otevri_import_dialog(user_name: str, refresh_fn):
    with ui.dialog() as dlg, ui.card().classes('w-[560px] gap-3'):
        ui.label('Import zápisu ze zalistovací komise').classes('text-lg font-bold')
        ui.label('Datum komise se bere z názvu souboru (např. „Zápis ze zalistovací '
                 'komise_2026-05-13.xlsx"). Nahrají se všechny listy sešitu. '
                 'Opakovaný import téhož data jen doplní a obnoví data — vyjádření '
                 'nákupu i potvrzení kontroly zůstanou.') \
            .classes('text-sm text-gray-600')
        vysledek = ui.label('').classes('text-sm')

        async def _zprac(raw: bytes, jmeno: str):
            dlg_kruh, _kruh, _popisek = prekryv_kolecko('Importuji zápis…', procenta=False)
            dlg_kruh.open()
            try:
                novych, obnovenych, chyba = await asyncio.to_thread(
                    _importuj_sync, raw, jmeno, user_name)
            finally:
                dlg_kruh.close()
                dlg_kruh.delete()
            if chyba:
                vysledek.set_text(f'❌ {chyba}')
                vysledek.classes(replace='text-sm text-red-600')
                return
            intranet_logger.log_activity(
                user_name, NAZEV,
                f'Import {jmeno}: {novych} nových, {obnovenych} obnovených řádků')
            ui.notify(f'Naimportováno: {novych} nových, {obnovenych} obnovených.',
                      type='positive')
            dlg.close()
            refresh_fn()

        async def _on_upload(e):
            # Napříč verzemi NiceGUI se obsah nahraného souboru jmenuje různě.
            zdroj = None
            for attr in ('content', 'file', 'stream', 'data', 'file_obj'):
                val = getattr(e, attr, None)
                if val is not None and hasattr(val, 'read'):
                    zdroj = val
                    break
            if zdroj is None:
                vysledek.set_text('❌ Soubor se nepodařilo načíst.')
                return
            raw = zdroj.read()
            if inspect.isawaitable(raw):
                raw = await raw
            up.reset()
            jmeno = getattr(e, 'name', '') or getattr(zdroj, 'name', '')
            await _zprac(raw, jmeno)

        up = ui.upload(on_upload=_on_upload, auto_upload=True, max_file_size=50_000_000,
                       label='Vybrat soubor .xlsx / .xlsm') \
            .props('accept=.xlsx,.xlsm').classes('w-full')

        with ui.row().classes('w-full justify-end'):
            ui.button('Zavřít', on_click=dlg.close).props('flat no-caps')
    dlg.open()
