"""Zalistovací komise – zápisy z komise po obdobích.

Přehled = dlaždice období (datum komise). Detail = listy „Sumarizace", „Změna K2"
a „Delist" v tomto pořadí, zobrazené 1:1 vůči nahranému souboru (výplně, písmo,
zarovnání, šířky sloupců, výšky řádků, ohraničení i podmíněné zvýraznění duplicit).
Zapisuje se jen do zeleného sloupce: Sumarizace F (ID listing), Změna K2 a Delist
P (ID změny).

Nahraný sešit se ukládá celý (BLOB). Export listu vychází z něj – vezme původní
list se vším formátováním a jen do zeleného sloupce dosadí aktuální zápisy, takže
je 1:1 s originálem.

Práva (intranet_prava.ZALISTOVACI_ROLE):
  Komise – OffNákup  … vidí a zapisuje list Sumarizace
  Komise – OffObchod … vidí a zapisuje listy Změna K2 a Delist
  Komise – Správce   … vše (vč. importu a mazání období)
  Komise – Čtenář    … vidí vše, filtruje, exportuje; na Sumarizaci jen řádky
                       svých zkratek nákupčího (sloupec A, „Rozdělení nákupčích")
  Komise – Import    … nevidí nic, jen nahrává soubor

Staré tabulky (zalistovaci_komise, zalistovaci_komise_meta) se nepoužívají a
nemažou – nový formát potřebuje původní soubor, který staré importy neukládaly.
"""
import asyncio
import colorsys
import copy
import datetime
import hashlib
import inspect
import io
import json
import re
import unicodedata
from collections import Counter

from nicegui import background_tasks, ui

import intranet_data
import intranet_logger
import intranet_prava
import intranet_sankce
from intranet_ui_utils import prekryv_kolecko, refreshable_na_klienta

NAZEV = 'Zalistovací komise'
T_SOUBOR = 'zalist_soubor'
T_LIST = 'zalist_list'
T_RADEK = 'zalist_radek'

# (klíč, název listu, výchozí zelený sloupec, hlavičky zeleného sloupce, ikona)
LISTY = (
    ('sumarizace', 'Sumarizace', 'F', ('id listing',), 'summarize'),
    ('zmena_k2', 'Změna K2', 'P', ('id zmeny', 'id zmena'), 'swap_horiz'),
    ('delist', 'Delist', 'P', ('id zmeny', 'id zmena'), 'remove_shopping_cart'),
)
_LIST = {l[0]: l for l in LISTY}

# Kdo dostane e-mail o nahrání: každý, kdo v modulu něco vidí (ne čistý Import).
_ROLE_PRIJEMCI = ('zalistovaci_offnakup', 'zalistovaci_offobchod',
                  'zalistovaci_ctenar', 'zalistovaci_admin')
_MAX_SOUBOR = 20_000_000
_MAX_ZAPIS = 255
_MAX_VLOZENI = 5000   # strop buněk na jedno hromadné vložení (Ctrl+V / Delete výběru)
_DNY = ('pondělí', 'úterý', 'středa', 'čtvrtek', 'pátek', 'sobota', 'neděle')


def _norm(text) -> str:
    s = unicodedata.normalize('NFKD', str(text or ''))
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', s).strip().lower()


def _datum_txt(d) -> str:
    return d.strftime('%d.%m.%Y') if d else ''


# ================================================================ PRÁVA ==
def prava(vsechna_prava) -> dict:
    p = set(vsechna_prava or ())
    admin = 'vse' in p or 'zalistovaci_admin' in p
    nakup = 'zalistovaci_offnakup' in p
    obchod = 'zalistovaci_offobchod' in p
    ctenar = 'zalistovaci_ctenar' in p
    vidi = {'sumarizace': admin or nakup or ctenar,
            'zmena_k2': admin or obchod or ctenar,
            'delist': admin or obchod or ctenar}
    pise = {'sumarizace': admin or nakup,
            'zmena_k2': admin or obchod,
            'delist': admin or obchod}
    # Celou Sumarizaci vidí Správce a OffNákup; samotný Čtenář jen řádky svých zkratek.
    prefix = intranet_prava.ZALISTOVACI_KOD_PREFIX
    kody = None if (admin or nakup) else {
        k for k in intranet_prava.ZALISTOVACI_KODY if f'{prefix}{k.lower()}' in p}
    return {'vidi': vidi, 'pise': pise, 'kody': kody, 'admin': admin,
            'importuje': admin or 'zalistovaci_vkladatel' in p}


# =================================================================== DB ==
_DB_HOTOVO = False


def inicializace_db():
    global _DB_HOTOVO
    if _DB_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T_SOUBOR} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                obdobi DATE NOT NULL,
                nazev_souboru VARCHAR(255),
                obsah LONGBLOB NOT NULL,
                imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                imported_by VARCHAR(255),
                UNIQUE KEY uniq_obdobi (obdobi)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T_LIST} (
                soubor_id INT NOT NULL,
                list_klic VARCHAR(20) NOT NULL,
                list_nazev VARCHAR(100) NOT NULL,
                meta LONGTEXT NOT NULL,
                PRIMARY KEY (soubor_id, list_klic)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T_RADEK} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                soubor_id INT NOT NULL,
                list_klic VARCHAR(20) NOT NULL,
                radek INT NOT NULL,
                row_hash CHAR(40) NOT NULL,
                nakupci VARCHAR(32) NOT NULL DEFAULT '',
                data LONGTEXT NOT NULL,
                zapis VARCHAR(255) NOT NULL DEFAULT '',
                zapis_by VARCHAR(255),
                zapis_at DATETIME,
                INDEX idx_list (soubor_id, list_klic, radek)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""")
        conn.commit()
        _DB_HOTOVO = True
    except Exception as e:
        print(f'[zalistovaci] Chyba inicializace DB: {e}')
    finally:
        if cur:
            cur.close()
        conn.close()


def _prehled() -> list:
    """Období (nejnovější první) + počty řádků/vyplněných po listech a nákupčích."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT id, obdobi, nazev_souboru, imported_at, imported_by '
                    f'FROM {T_SOUBOR} ORDER BY obdobi DESC')
        soubory = cur.fetchall()
        cur.execute(f"SELECT soubor_id, list_klic, nakupci, COUNT(*) AS n, "
                    f"SUM(TRIM(zapis) NOT IN ('', 'není', 'neni')) AS vypl "
                    f"FROM {T_RADEK} GROUP BY soubor_id, list_klic, nakupci")
        pocty = {}
        for r in cur.fetchall():
            pocty.setdefault(r['soubor_id'], []).append(
                (r['list_klic'], r['nakupci'], int(r['n'] or 0), int(r['vypl'] or 0)))
        for s in soubory:
            s['pocty'] = pocty.get(s['id'], [])
        return soubory
    except Exception as e:
        print(f'[zalistovaci] Chyba načtení přehledu: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


def _info_souboru(soubor_id):
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT id, obdobi, nazev_souboru, imported_at, imported_by '
                    f'FROM {T_SOUBOR} WHERE id=%s', (soubor_id,))
        info = cur.fetchone()
        if info:
            cur.execute(f'SELECT list_klic FROM {T_LIST} WHERE soubor_id=%s', (soubor_id,))
            info['listy'] = {r['list_klic'] for r in cur.fetchall()}
        return info
    except Exception as e:
        print(f'[zalistovaci] Chyba načtení zápisu: {e}')
        return None
    finally:
        if cur:
            cur.close()
        conn.close()


def _id_obdobi(obdobi):
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT id FROM {T_SOUBOR} WHERE obdobi=%s', (obdobi,))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None
    finally:
        if cur:
            cur.close()
        conn.close()


def _kdy(v) -> str:
    return v.strftime('%d.%m.%Y %H:%M') if isinstance(v, datetime.datetime) else ''


def _nacti_list(soubor_id, klic, kody):
    """(meta, řádky pro grid). kody=None → všechny řádky, jinak jen nakupci ∈ kody."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None, []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT meta FROM {T_LIST} WHERE soubor_id=%s AND list_klic=%s',
                    (soubor_id, klic))
        r = cur.fetchone()
        if not r:
            return None, []
        meta = json.loads(r['meta'])
        if kody is not None and not kody:
            return meta, []
        sql = (f'SELECT id, radek, data, zapis, zapis_by, zapis_at FROM {T_RADEK} '
               f'WHERE soubor_id=%s AND list_klic=%s')
        par = [soubor_id, klic]
        if kody is not None:
            sql += f" AND nakupci IN ({','.join(['%s'] * len(kody))})"
            par += sorted(kody)
        cur.execute(sql + ' ORDER BY radek', tuple(par))
        zel = meta.get('zeleny')
        radky = []
        for r in cur.fetchall():
            d = json.loads(r['data'])
            row = dict(d.get('v') or {})
            row['id'] = r['id']
            row['_r'] = r['radek']
            if d.get('s'):
                row['_s'] = d['s']
            if d.get('h'):
                row['_h'] = d['h']
            if zel:
                row[zel] = r['zapis'] or ''
            row['_zby'] = r['zapis_by'] or ''
            row['_zat'] = _kdy(r['zapis_at'])
            radky.append(row)
        return meta, radky
    except Exception as e:
        print(f'[zalistovaci] Chyba načtení listu {klic}: {e}')
        return None, []
    finally:
        if cur:
            cur.close()
        conn.close()


def _uloz_zapis(radek_id, klic, ocekavana, nova, user_id, user_name):
    """Zapíše hodnotu zeleného sloupce. Hlídá souběh: když hodnotu mezitím změnil
    někdo jiný, nic nepřepíše. Vrací (ok, zpráva, aktuální hodnota, zapsal, kdy)."""
    return _uloz_zapisy([(radek_id, ocekavana, nova)], klic, user_id, user_name)[0][1:]


def _uloz_zapisy(polozky, klic, user_id, user_name):
    """Zapíše více buněk zeleného sloupce v jedné transakci (vložení Ctrl+V, Delete
    výběru). polozky = [(radek_id, očekávaná, nová)]. Souběh se hlídá po řádcích —
    řádek, který mezitím změnil někdo jiný, se přeskočí, ostatní se zapíšou.
    Vrací [(radek_id, ok, zpráva, aktuální hodnota, zapsal, kdy)] ve stejném pořadí."""
    def _vse_chyba(msg):
        return [(rid, False, msg, ocek, '', '') for rid, ocek, _ in polozky]

    if not polozky:
        return []
    conn = intranet_data.get_db_connection()
    if not conn:
        return _vse_chyba('Není spojení s databází.')
    cur = None
    vysl, zmeny = [], []
    ted = datetime.datetime.now().replace(microsecond=0)
    try:
        cur = conn.cursor(dictionary=True)
        ids = [p[0] for p in polozky]
        cur.execute(f'SELECT id, list_klic, row_hash, zapis, zapis_by, zapis_at FROM {T_RADEK} '
                    f'WHERE id IN ({",".join(["%s"] * len(ids))}) FOR UPDATE', tuple(ids))
        db = {r['id']: r for r in cur.fetchall()}
        for rid, ocek, nova in polozky:
            r = db.get(rid)
            if not r or r['list_klic'] != klic:
                vysl.append((rid, False, 'Řádek už neexistuje (zápis byl mezitím znovu nahrán?).',
                             ocek, '', ''))
            elif (r['zapis'] or '') != (ocek or ''):
                vysl.append((rid, False, f"Hodnotu mezitím změnil(a) {r['zapis_by'] or 'někdo jiný'}.",
                             r['zapis'] or '', r['zapis_by'] or '', _kdy(r['zapis_at'])))
            else:
                vysl.append((rid, True, '', nova, user_name, _kdy(ted)))
                zmeny.append((r['row_hash'], rid, ocek, nova))
        if zmeny:
            cur.executemany(f'UPDATE {T_RADEK} SET zapis=%s, zapis_by=%s, zapis_at=%s WHERE id=%s',
                            [(nova, user_name, ted, rid) for _, rid, _, nova in zmeny])
        conn.commit()
    except Exception as e:
        conn.rollback()
        return _vse_chyba(f'Chyba zápisu: {e}')
    finally:
        if cur:
            cur.close()
        conn.close()
    pole = f"{_LIST[klic][1]}"[:30]
    intranet_sankce.zapis_audit_hromadne(
        T_RADEK, [(h, rid, pole, ocek, nova) for h, rid, ocek, nova in zmeny], user_id, user_name)
    return vysl


def _smaz_obdobi(soubor_id) -> bool:
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {T_RADEK} WHERE soubor_id=%s', (soubor_id,))
        cur.execute(f'DELETE FROM {T_LIST} WHERE soubor_id=%s', (soubor_id,))
        cur.execute(f'DELETE FROM {T_SOUBOR} WHERE id=%s', (soubor_id,))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f'[zalistovaci] Chyba mazání období: {e}')
        return False
    finally:
        if cur:
            cur.close()
        conn.close()


# ============================================================ STYLY XLSX ==
_THEME_VYCHOZI = ('FFFFFF', '000000', 'E7E6E6', '44546A', '4472C4', 'ED7D31',
                  'A5A5A5', 'FFC000', '5B9BD5', '70AD47', '0563C1', '954F72')
# Pořadí indexů, které používají buňky (lt1/dk1 a lt2/dk2 jsou proti XML prohozené).
_THEME_TAGY = ('lt1', 'dk1', 'lt2', 'dk2', 'accent1', 'accent2', 'accent3', 'accent4',
               'accent5', 'accent6', 'hlink', 'folHlink')
_OHRANICENI = {
    'thin': '1px solid', 'hair': '1px dotted', 'dotted': '1px dotted', 'dashed': '1px dashed',
    'dashDot': '1px dashed', 'dashDotDot': '1px dashed', 'medium': '2px solid',
    'mediumDashed': '2px dashed', 'mediumDashDot': '2px dashed',
    'mediumDashDotDot': '2px dashed', 'slantDashDot': '2px dashed',
    'thick': '3px solid', 'double': '3px double'}
_MRIZKA = '1px solid #d4d4d4'   # mřížka Excelu tam, kde buňka vlastní ohraničení nemá


def _theme_barvy(wb) -> list:
    xml = getattr(wb, 'loaded_theme', None)
    if isinstance(xml, bytes):
        xml = xml.decode('utf-8', 'ignore')
    out = list(_THEME_VYCHOZI)
    if not xml:
        return out
    for i, tag in enumerate(_THEME_TAGY):
        m = re.search(rf'<a:{tag}>(.*?)</a:{tag}>', xml, re.S)
        if m:
            b = re.search(r'(?:srgbClr val|lastClr)="([0-9A-Fa-f]{6})"', m.group(1))
            if b:
                out[i] = b.group(1).upper()
    return out


def _tint(hex6: str, tint: float) -> str:
    if not tint:
        return hex6
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = l * (1 + tint) if tint < 0 else l * (1 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), s)
    return '%02X%02X%02X' % (round(r * 255), round(g * 255), round(b * 255))


def _barva(col, theme):
    if col is None:
        return None
    try:
        if col.type == 'rgb':
            v = col.rgb
            return '#' + v[-6:].upper() if isinstance(v, str) and len(v) >= 6 else None
        if col.type == 'theme':
            i = col.theme
            return '#' + _tint(theme[i], col.tint or 0) if i is not None and 0 <= i < len(theme) else None
        if col.type == 'indexed':
            from openpyxl.styles.colors import COLOR_INDEX
            i = col.indexed
            if i is not None and 0 <= i < len(COLOR_INDEX) and i not in (64, 65):
                return '#' + COLOR_INDEX[i][-6:].upper()
    except Exception:
        pass
    return None


def _font_family(name):
    if not name:
        return None
    n = str(name).strip()
    zaklad = re.sub(r'\s+(CE|CYR|Baltic|Greek|Tur)$', '', n)   # „Arial CE" = Arial (středoevropská sada)
    rodina = [f"'{n}'"]
    if zaklad != n:
        rodina.append(f"'{zaklad}'")
    if zaklad.lower() == 'calibri':
        rodina.append('Carlito')
    rodina.append('sans-serif')
    return ', '.join(rodina)


def _css_bunky(cell, theme, hodnota=None) -> dict:
    """Styl buňky Excelu → CSS (camelCase) pro AG Grid."""
    st = {}
    f = cell.fill
    if f is not None and f.fill_type and f.fill_type != 'none':
        b = _barva(f.fgColor, theme)
        if b:
            st['backgroundColor'] = b
    ft = cell.font
    if ft is not None:
        fam = _font_family(ft.name)
        if fam:
            st['fontFamily'] = fam
        if ft.sz:
            st['fontSize'] = f'{float(ft.sz):g}pt'
        st['fontWeight'] = '700' if ft.b else '400'
        if ft.i:
            st['fontStyle'] = 'italic'
        deco = []
        if ft.u and ft.u != 'none':
            deco.append('underline')
        if ft.strike:
            deco.append('line-through')
        if deco:
            st['textDecoration'] = ' '.join(deco)
        st['color'] = _barva(ft.color, theme) or '#000000'
    al = cell.alignment
    h = al.horizontal if al is not None else None
    if h in (None, 'general'):   # Excel „Obecné": čísla doprava, text doleva
        h = 'right' if isinstance(hodnota, (int, float)) and not isinstance(hodnota, bool) else 'left'
    st['textAlign'] = {'centerContinuous': 'center', 'fill': 'left',
                       'distributed': 'center'}.get(h, h)
    if al is not None and al.wrap_text:
        st['whiteSpace'] = 'normal'
        st['lineHeight'] = '1.2'
    if al is not None and al.indent:
        st['paddingLeft'] = f'{3 + int(al.indent) * 9}px'
    bd = cell.border
    for strana, css in (('right', 'borderRight'), ('bottom', 'borderBottom')):
        s = getattr(bd, strana, None) if bd is not None else None
        styl = getattr(s, 'style', None)
        st[css] = (f"{_OHRANICENI[styl]} {_barva(s.color, theme) or '#000000'}"
                   if styl in _OHRANICENI else _MRIZKA)
    return st


def _css_dxf(dxf, theme) -> dict:
    """Formát podmíněného formátování (dxf) → CSS přepis."""
    st = {}
    if dxf is None:
        return st
    f = dxf.fill
    if f is not None:
        b = None
        for col in (f.bgColor, f.fgColor):   # u dxf je barva plné výplně v bgColor
            b = _barva(col, theme)
            if b and b != '#000000':
                break
            b = None
        if b:
            st['backgroundColor'] = b
    ft = dxf.font
    if ft is not None:
        c = _barva(ft.color, theme)
        if c:
            st['color'] = c
        if ft.b:
            st['fontWeight'] = '700'
        if ft.i:
            st['fontStyle'] = 'italic'
    return st


def _formatuj_cislo(v, fmt: str):
    sekce = re.sub(r'\[[^\]]*\]|"[^"]*"|\\.|_.|\*.', '', fmt.split(';')[0])
    if '%' in sekce:
        v = v * 100
    m = re.search(r'\.([0#]+)', sekce)
    des = len(m.group(1)) if m else 0
    tis = ',' in sekce.split('.')[0]
    txt = f'{v:,.{des}f}' if tis else f'{v:.{des}f}'
    txt = txt.replace(',', ' ').replace('.', ',')
    if '%' in sekce:
        txt += ' %'
    if 'Kč' in fmt:
        txt += ' Kč'
    return txt


def _zobraz(v, fmt):
    """Hodnota buňky tak, jak ji Excel ukáže (bez exotických formátů)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 'PRAVDA' if v else 'NEPRAVDA'
    if isinstance(v, datetime.datetime):
        if v.time() == datetime.time(0) and 'h' not in (fmt or '').lower():
            return v.strftime('%d.%m.%Y')
        return v.strftime('%d.%m.%Y %H:%M')
    if isinstance(v, datetime.date):
        return v.strftime('%d.%m.%Y')
    if isinstance(v, datetime.time):
        return v.strftime('%H:%M')
    if isinstance(v, (int, float)):
        f = fmt or 'General'
        if f in ('General', '@'):
            if isinstance(v, float):
                return int(v) if v.is_integer() and abs(v) < 1e15 else float(f'{v:.10g}')
            return v
        try:
            return _formatuj_cislo(v, f)
        except Exception:
            return v
    return str(v)


def _px_sirka(w) -> int:
    """Šířka sloupce Excelu (znaky) → px při výchozím písmu Calibri 11."""
    return int((256 * float(w) + 18) / 256 * 7)


def _px_vyska(pt) -> int:
    return int(round(float(pt) * 4 / 3))


def _duplicity(ws, max_r, theme) -> dict:
    """Podmíněné formátování „duplicitní/jedinečné hodnoty" → {(sloupec, řádek): css}.
    Ostatní typy pravidel se v náhledu nezobrazují (v exportu zůstanou)."""
    zasahy = {}
    for cf in ws.conditional_formatting:
        for rule in cf.rules:
            if rule.type not in ('duplicateValues', 'uniqueValues'):
                continue
            css = _css_dxf(rule.dxf, theme)
            if not css:
                continue
            bunky = []
            for rng in cf.sqref.ranges:
                for c in range(rng.min_col, rng.max_col + 1):
                    for r in range(rng.min_row, min(rng.max_row, max_r) + 1):
                        v = ws.cell(r, c).value
                        if v is not None and str(v).strip() != '':
                            klic = _norm(v) if isinstance(v, str) else v
                            bunky.append(((c, r), klic))
            pocty = Counter(k for _, k in bunky)
            for (c, r), k in bunky:
                if (pocty[k] > 1) == (rule.type == 'duplicateValues'):
                    zasahy.setdefault((c, r), []).append((rule.priority or 0, css))
    # vyšší priorita (menší číslo) má u stejné vlastnosti přednost → aplikovat poslední
    return {k: {kk: vv for _, css in sorted(v, key=lambda x: -x[0]) for kk, vv in css.items()}
            for k, v in zasahy.items()}


def _rozeber_list(ws, klic, theme) -> dict:
    from openpyxl.utils import column_index_from_string, get_column_letter
    _, nazev, vychozi_zel, zel_hlavicky, _ = _LIST[klic]
    max_r, max_c = ws.max_row, ws.max_column
    posl_c = posl_r = 1
    for r in range(1, max_r + 1):
        for c in range(1, max_c + 1):
            v = ws.cell(r, c).value
            if v is not None and str(v).strip() != '':
                posl_r = max(posl_r, r)
                posl_c = max(posl_c, c)
    pismena = [get_column_letter(c) for c in range(1, posl_c + 1)]

    zel = next((L for c, L in enumerate(pismena, 1)
                if _norm(ws.cell(1, c).value) in zel_hlavicky), None)
    if zel is None and vychozi_zel in pismena:
        zel = vychozi_zel

    sirky = {}
    vychozi_w = ws.sheet_format.defaultColWidth or ((ws.sheet_format.baseColWidth or 8) + 0.7109375)
    for key, dim in ws.column_dimensions.items():
        try:
            lo = dim.min or column_index_from_string(key)
            hi = dim.max or lo
        except Exception:
            continue
        for c in range(lo, min(hi, posl_c) + 1):
            sirky[c] = (dim.width or vychozi_w, bool(dim.hidden))
    vychozi_h = ws.sheet_format.defaultRowHeight or 15

    def _vyska(r):
        d = ws.row_dimensions.get(r) if hasattr(ws.row_dimensions, 'get') else None
        return _px_vyska(d.height if d is not None and d.height else vychozi_h)

    dup = _duplicity(ws, posl_r, theme)
    styly, idx_stylu = [], {}

    def _idx(css):
        k = json.dumps(css, sort_keys=True)
        if k not in idx_stylu:
            idx_stylu[k] = len(styly)
            styly.append(css)
        return idx_stylu[k]

    # 1. průchod: hodnoty a styly buněk
    surove = []
    for r in range(2, posl_r + 1):
        vals, sty = {}, {}
        for c, L in enumerate(pismena, 1):
            cell = ws.cell(r, c)
            v = _zobraz(cell.value, cell.number_format)
            vals[L] = v
            css = _css_bunky(cell, theme, v)
            if (c, r) in dup:
                css = {**css, **dup[(c, r)]}
            sty[L] = _idx(css)
        surove.append((r, vals, sty, _vyska(r)))

    # 2. číselné sloupce (kvůli filtru a řazení), ostatní jednotně jako text
    ciselne = set()
    for L in pismena:
        hod = [vals[L] for _, vals, _, _ in surove if vals[L] not in (None, '')]
        if L != zel and hod and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in hod):
            ciselne.add(L)

    def _txt(v):
        if v is None:
            return ''
        if isinstance(v, float):
            return f'{v:.10g}'.replace('.', ',')
        return str(v)

    vysky = Counter(h for *_, h in surove)
    row_h = vysky.most_common(1)[0][0] if vysky else _px_vyska(vychozi_h)
    dominanty = {L: Counter(sty[L] for _, _, sty, _ in surove).most_common(1)[0][0] if surove else 0
                 for L in pismena}

    sloupce = []
    for c, L in enumerate(pismena, 1):
        hc = ws.cell(1, c)
        hcss = _css_bunky(hc, theme, str(hc.value or ''))
        zarov = hcss.pop('textAlign', 'left')
        hcss.pop('whiteSpace', None)
        hcss.pop('lineHeight', None)
        w, skryt = sirky.get(c, (vychozi_w, False))
        sloupce.append({
            'L': L, 'hdr': '' if hc.value is None else str(hc.value), 'w': _px_sirka(w),
            'hide': skryt, 'hst': hcss, 'hal': zarov if zarov in ('center', 'right') else 'left',
            'hwrap': bool(hc.alignment is not None and hc.alignment.wrap_text),
            'dom': dominanty.get(L, 0), 'num': L in ciselne})

    pin = 0
    if ws.freeze_panes:
        try:
            from openpyxl.utils.cell import coordinate_from_string
            pin = column_index_from_string(coordinate_from_string(ws.freeze_panes)[0]) - 1
        except Exception:
            pin = 0

    radky = []
    for r, vals, sty, h in surove:
        v_out = {}
        for L in pismena:
            if L == zel:
                continue
            v = vals[L]
            v_out[L] = (None if v in (None, '') else v) if L in ciselne else _txt(v)
        zapis = _txt(vals.get(zel)) if zel else ''
        hash_ = hashlib.sha1(json.dumps([klic, v_out], ensure_ascii=False, sort_keys=True,
                                        default=str).encode('utf-8')).hexdigest()
        data = {'v': v_out}
        odchylky = {L: i for L, i in sty.items() if i != dominanty[L]}
        if odchylky:
            data['s'] = odchylky
        if h != row_h:
            data['h'] = h
        nakupci = _txt(vals.get('A')).strip().upper()[:32] if klic == 'sumarizace' else ''
        radky.append({'radek': r, 'hash': hash_, 'nakupci': nakupci,
                      'data': json.dumps(data, ensure_ascii=False, default=str),
                      'zapis': zapis[:_MAX_ZAPIS]})

    meta = {'nazev': ws.title, 'sloupce': sloupce, 'styly': styly, 'zeleny': zel,
            'row_h': row_h, 'hdr_h': _vyska(1), 'pin': pin}
    return {'meta': meta, 'radky': radky}


def _rozeber_sesit(raw: bytes):
    """Rozebere nahraný sešit. Vrací (listy {klic: {meta, radky}}, varování, chyba)."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as e:
        return {}, [], f'Soubor nejde otevřít ({e}) — očekávám .xlsx / .xlsm.'
    theme = _theme_barvy(wb)
    podle_jmena = {_norm(ws.title): ws for ws in wb.worksheets}
    listy, varovani = {}, []
    for klic, nazev, *_ in LISTY:
        ws = podle_jmena.get(_norm(nazev))
        if ws is None:
            varovani.append(f'List „{nazev}" v souboru chybí.')
            continue
        try:
            listy[klic] = _rozeber_list(ws, klic, theme)
        except Exception as e:
            varovani.append(f'List „{nazev}" nejde načíst: {e}')
            continue
        if not listy[klic]['meta']['zeleny']:
            varovani.append(f'List „{nazev}": zelený sloupec pro zápis nenalezen.')
    if not listy:
        return {}, varovani, 'V souboru není žádný z listů Sumarizace / Změna K2 / Delist.'
    return listy, varovani, None


def _uloz_import(obdobi, nazev_souboru, raw, listy, user_name):
    """Uloží zápis za období. Existující období nahradí; u řádků se shodným obsahem
    převezme hodnoty zapsané v aplikaci. Vrací (ok, zpráva, nahrazeno, převzato)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False, 'Chyba připojení k databázi.', False, 0
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT id FROM {T_SOUBOR} WHERE obdobi=%s FOR UPDATE', (obdobi,))
        r = cur.fetchone()
        stare = {}
        if r:
            soubor_id = r[0]
            cur.execute(f'SELECT list_klic, row_hash, zapis, zapis_by, zapis_at FROM {T_RADEK} '
                        f'WHERE soubor_id=%s AND zapis_by IS NOT NULL ORDER BY radek', (soubor_id,))
            for k, h, z, by, at in cur.fetchall():
                stare.setdefault((k, h), []).append((z, by, at))
            cur.execute(f'UPDATE {T_SOUBOR} SET nazev_souboru=%s, obsah=%s, imported_at=NOW(), '
                        f'imported_by=%s WHERE id=%s', (nazev_souboru[:255], raw, user_name, soubor_id))
            cur.execute(f'DELETE FROM {T_RADEK} WHERE soubor_id=%s', (soubor_id,))
            cur.execute(f'DELETE FROM {T_LIST} WHERE soubor_id=%s', (soubor_id,))
        else:
            cur.execute(f'INSERT INTO {T_SOUBOR} (obdobi, nazev_souboru, obsah, imported_by) '
                        f'VALUES (%s, %s, %s, %s)', (obdobi, nazev_souboru[:255], raw, user_name))
            soubor_id = cur.lastrowid
        prevzato = 0
        for klic, obsah in listy.items():
            cur.execute(f'INSERT INTO {T_LIST} (soubor_id, list_klic, list_nazev, meta) '
                        f'VALUES (%s, %s, %s, %s)',
                        (soubor_id, klic, obsah['meta']['nazev'][:100],
                         json.dumps(obsah['meta'], ensure_ascii=False)))
            hodnoty = []
            for rd in obsah['radky']:
                z, by, at = rd['zapis'], None, None
                fronta = stare.get((klic, rd['hash']))
                if fronta:
                    z, by, at = fronta.pop(0)
                    prevzato += 1
                hodnoty.append((soubor_id, klic, rd['radek'], rd['hash'], rd['nakupci'],
                                rd['data'], z or '', by, at))
            if hodnoty:
                cur.executemany(
                    f'INSERT INTO {T_RADEK} (soubor_id, list_klic, radek, row_hash, nakupci, data, '
                    f'zapis, zapis_by, zapis_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)', hodnoty)
        conn.commit()
        return True, '', bool(r), prevzato
    except Exception as e:
        conn.rollback()
        return False, f'Chyba zápisu do databáze: {e}', False, 0
    finally:
        if cur:
            cur.close()
        conn.close()


# ================================================================ EXPORT ==
def _hodnota_do_excelu(z: str):
    z = (z or '').strip()
    return int(z) if re.fullmatch(r'[1-9]\d{0,14}', z) else z


def _prepocti_rozsahy(ws, mapa: dict, posledni: int):
    """Po zhuštění řádků přepočítá rozsahy podmíněného formátování."""
    from openpyxl.formatting.formatting import ConditionalFormattingList
    from openpyxl.utils import get_column_letter
    MAXR = 1048576
    puvodni = list(ws.conditional_formatting)
    ws.conditional_formatting = ConditionalFormattingList()
    for cf in puvodni:
        casti = []
        for rng in cf.sqref.ranges:
            radky = sorted({1} if rng.min_row <= 1 else set()) + sorted(
                n for o, n in mapa.items() if rng.min_row <= o <= rng.max_row)
            behy = []
            for n in radky:
                if behy and n == behy[-1][1] + 1:
                    behy[-1][1] = n
                else:
                    behy.append([n, n])
            if rng.max_row >= MAXR:   # rozsah „do konce listu" zůstane otevřený
                if behy and behy[-1][1] == posledni:
                    behy[-1][1] = MAXR
                else:
                    behy.append([posledni + 1, MAXR])
            c1, c2 = get_column_letter(rng.min_col), get_column_letter(rng.max_col)
            casti += [f'{c1}{a}:{c2}{b}' for a, b in behy]
        if casti:
            for rule in cf.rules:
                ws.conditional_formatting.add(' '.join(casti), rule)


def _export_listu(soubor_id, klic, kody, ids):
    """xlsx s jediným listem 1:1 vůči originálu + aktuální zápisy v zeleném sloupci.
    kody / ids zúží řádky (Čtenář na Sumarizaci, filtr v gridu). Vrací (bytes, jméno, chyba)."""
    import openpyxl
    conn = intranet_data.get_db_connection()
    if not conn:
        return None, '', 'Chyba připojení k databázi.'
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT obdobi, obsah FROM {T_SOUBOR} WHERE id=%s', (soubor_id,))
        s = cur.fetchone()
        cur.execute(f'SELECT meta FROM {T_LIST} WHERE soubor_id=%s AND list_klic=%s', (soubor_id, klic))
        m = cur.fetchone()
        cur.execute(f'SELECT id, radek, nakupci, zapis FROM {T_RADEK} '
                    f'WHERE soubor_id=%s AND list_klic=%s ORDER BY radek', (soubor_id, klic))
        radky = cur.fetchall()
    except Exception as e:
        return None, '', f'Chyba čtení: {e}'
    finally:
        if cur:
            cur.close()
        conn.close()
    if not s or not m:
        return None, '', 'Zápis už neexistuje.'
    meta = json.loads(m['meta'])
    try:
        wb = openpyxl.load_workbook(io.BytesIO(s['obsah']))
    except Exception as e:
        return None, '', f'Uložený soubor nejde otevřít: {e}'
    ws = next((w for w in wb.worksheets if _norm(w.title) == _norm(meta['nazev'])), None)
    if ws is None:
        return None, '', 'List v uloženém souboru chybí.'
    for jiny in [w for w in wb.worksheets if w is not ws]:
        wb.remove(jiny)
    wb.active = 0
    ws.sheet_view.tabSelected = True

    zel = meta.get('zeleny')
    if zel:
        for rd in radky:
            ws[f"{zel}{rd['radek']}"].value = _hodnota_do_excelu(rd['zapis'])

    ponechat = [rd['radek'] for rd in radky
                if (kody is None or rd['nakupci'] in kody) and (ids is None or rd['id'] in ids)]
    if len(ponechat) < len(radky):
        max_c = ws.max_column
        mapa, cil = {}, 2
        for r in ponechat:
            if r != cil:
                for c in range(1, max_c + 1):
                    src, dst = ws.cell(r, c), ws.cell(cil, c)
                    dst.value = src.value
                    dst._style = copy.copy(src._style)
                ws.row_dimensions[cil].height = ws.row_dimensions[r].height
            mapa[r] = cil
            cil += 1
        posledni = cil - 1
        if ws.max_row > posledni:
            ws.delete_rows(posledni + 1, ws.max_row - posledni)
        _prepocti_rozsahy(ws, mapa, posledni)
        if ws.auto_filter.ref:
            from openpyxl.utils import range_boundaries, get_column_letter
            c1, r1, c2, r2 = range_boundaries(ws.auto_filter.ref)
            if r2 > r1:   # filtr jen na hlavičce (jako v originálu) nechat beze změny
                ws.auto_filter.ref = f'{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{max(posledni, r1)}'

    out = io.BytesIO()
    wb.save(out)
    slug = re.sub(r'[^A-Za-z0-9]+', '_', _norm(meta['nazev']).title()).strip('_')
    return out.getvalue(), f"Zalistovaci_komise_{s['obdobi']:%Y-%m-%d}_{slug}.xlsx", None


# ================================================================ E-MAIL ==
async def _rozesli_oznameni(obdobi, listy_pocty, user_name):
    import intranet_emaily
    try:
        prijemci = await asyncio.to_thread(intranet_data.ziskej_emaily_s_pravem, *_ROLE_PRIJEMCI)
    except Exception as e:
        print(f'[zalistovaci] Příjemci e-mailu: {e}')
        return
    if not prijemci:
        return
    obsah = ', '.join(f'{n} ({p} řádků)' for n, p in listy_pocty)
    odkaz = f'\n\nOdkaz: {intranet_data.APP_URL}' if getattr(intranet_data, 'APP_URL', '') else ''
    predmet = f'Zalistovací komise – nový zápis za {_datum_txt(obdobi)}'
    text = ('Dobrý den,\n\n'
            f'v aplikaci Moje JIPka byl nahrán zápis ze zalistovací komise za období {_datum_txt(obdobi)}.\n\n'
            f'Obsah: {obsah}.\n\n'
            'Zápis najdete v sekci „Zalistovací komise".'
            f'{odkaz}\n\nS pozdravem\nMoje JIPka')
    ok = chyb = 0
    for email in prijemci:
        try:
            if await asyncio.to_thread(intranet_emaily.odesli_upozorneni_email, email, predmet, text):
                ok += 1
            else:
                chyb += 1
        except Exception as e:
            print(f'[zalistovaci] E-mail {email}: {e}')
            chyb += 1
    intranet_logger.log_activity(user_name, NAZEV,
                                 f'Oznámení o zápisu {_datum_txt(obdobi)}: odesláno {ok}, chyb {chyb}')


# ==================================================================== UI ==
_CSS = """
.zk-grid .ag-header-cell-text { color: inherit; }
.zk-grid .ag-cell.zk-zel { user-select: none !important; -webkit-user-select: none !important; }
.zk-grid .ag-cell.zk-sel { background-image: linear-gradient(rgba(37,99,235,.16), rgba(37,99,235,.16));
  box-shadow: inset 2px 0 0 #2563eb, inset -2px 0 0 #2563eb; }
.zk-grid .ag-cell.zk-sel.zk-sel-t { box-shadow: inset 2px 0 0 #2563eb, inset -2px 0 0 #2563eb, inset 0 2px 0 #2563eb; }
.zk-grid .ag-cell.zk-sel.zk-sel-b { box-shadow: inset 2px 0 0 #2563eb, inset -2px 0 0 #2563eb, inset 0 -2px 0 #2563eb; }
.zk-grid .ag-cell.zk-sel.zk-sel-t.zk-sel-b { box-shadow: inset 0 0 0 2px #2563eb; }
"""

# Výběr buněk a schránka v zeleném sloupci jako v Excelu. AG Grid Community nemá
# výběr rozsahu ani schránku (jen Enterprise), proto vlastní řešení:
#  - výběr: tažení myší, Shift+klik, Shift+šipky (jen v rámci zeleného sloupce),
#    stav v gridOptions.context.sel = {a: kotva, b: konec} jako indexy zobrazených řádků
#    (po řazení/filtru se ruší),
#  - Ctrl+C: hodnoty výběru jako TSV (vloží se i do Excelu),
#  - Ctrl+V: jedna hodnota vyplní celý výběr; víc řádků se vloží od horní buňky dolů
#    (je-li výběr jejich násobkem, opakují se) — přes zobrazené, tj. vyfiltrované řádky,
#  - Delete: vymaže výběr.
# Schránka jde přes skrytou textareu a události copy/paste — navigator.clipboard
# by na http (bez HTTPS) nefungoval. Zápis se pošle na server jedním eventem zkZapis.
_ZK_JS = r"""
if(!window.zkSel){window.zkSel=(function(){
var Z={pend:null,drag:null,mys:false,lock:false,ta:null,kop:null};
function C(api){return api.getGridOption('context')||{};}
function sl(col){return !col?null:(typeof col==='string'?col:col.getColId());}
function roz(s){return [Math.min(s.a,s.b),Math.max(s.a,s.b)];}
function obnov(api){var c=C(api);if(c.zel)api.refreshCells({columns:[c.zel],force:true});}
function nastav(api,a,b){var c=C(api),s=c.sel;if(s&&s.a===a&&s.b===b)return;
 var bylo=s&&s.a!==s.b;c.sel={a:a,b:b};if(bylo||a!==b)obnov(api);}
function zrus(api){var c=C(api),s=c.sel;c.sel=null;if(s&&s.a!==s.b)obnov(api);}
function hodnoty(api,c){var r=roz(c.sel),o=[];for(var i=r[0];i<=r[1];i++){
 var n=api.getDisplayedRowAtIndex(i);o.push(n&&n.data&&n.data[c.zel]!=null?String(n.data[c.zel]):'');}return o;}
function tsv(v){return /[\t\n\r"]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
function parse(t){var rows=[],row=[],f='',q=false,i=0,n=t.length,ch;
 while(i<n){ch=t[i];
  if(q){if(ch==='"'){if(t[i+1]==='"'){f+='"';i+=2;continue;}q=false;i++;continue;}f+=ch;i++;continue;}
  if(ch==='"'&&f===''){q=true;i++;continue;}
  if(ch==='\t'){row.push(f);f='';i++;continue;}
  if(ch==='\r'||ch==='\n'){row.push(f);rows.push(row);row=[];f='';if(ch==='\r'&&t[i+1]==='\n')i++;i++;continue;}
  f+=ch;i++;}
 if(f!==''||row.length){row.push(f);rows.push(row);}
 return rows.map(function(r){return r[0]==null?'':r[0];});}
function posli(el,z){try{var v=el&&getElement(el);if(v)v.$emit('zkZapis',{zmeny:z});}catch(x){console.error(x);}}
function zpet(q){Z.lock=true;try{if(q.cell&&document.body.contains(q.cell))q.cell.focus({preventScroll:true});
 else q.api.setFocusedCell(q.row,q.col);}catch(x){}finally{Z.lock=false;}}
function TA(){if(Z.ta&&document.body.contains(Z.ta))return Z.ta;var t=document.createElement('textarea');
 t.tabIndex=-1;t.setAttribute('aria-hidden','true');
 t.style.cssText='position:fixed;left:-10000px;top:0;width:10px;height:10px;opacity:0;pointer-events:none;';
 t.addEventListener('paste',function(e){var d=e.clipboardData||window.clipboardData;
  var txt=d?(d.getData('text/plain')||d.getData('text')||''):'';e.preventDefault();
  var q=Z.pend;Z.pend=null;if(!q)return;zpet(q);if(txt)vloz(q,txt);});
 t.addEventListener('copy',function(e){if(Z.kop==null||!e.clipboardData)return;
  e.clipboardData.setData('text/plain',Z.kop);e.preventDefault();});
 document.body.appendChild(t);Z.ta=t;return t;}
function kopiruj(q,txt){var t=TA(),ok=false;Z.kop=txt;t.value=txt||' ';t.focus({preventScroll:true});t.select();
 try{ok=document.execCommand('copy');}catch(x){}Z.kop=null;t.value='';zpet(q);
 if(!ok&&navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(txt).catch(function(){});}
function vloz(q,txt){var api=q.api,c=C(api);if(!c.pise||!c.sel)return;var v=parse(txt);if(!v.length)return;
 var r=roz(c.sel),n=r[1]-r[0]+1,m=v.length,cnt=(n>1&&n%m===0)?n:m,tot=api.getDisplayedRowCount(),z=[],k;
 for(k=0;k<cnt&&r[0]+k<tot;k++){var nd=api.getDisplayedRowAtIndex(r[0]+k);
  if(nd&&nd.data)z.push({id:nd.data.id,v:v[k%m]});}
 if(!z.length)return;nastav(api,r[0],r[0]+k-1);posli(q.el,z);}
function konecTazeni(){Z.btn=false;if(Z.drag){Z.drag.drag=false;Z.drag=null;}}
/* AG Grid doručuje cellMouseDown/Focused asynchronně — často až PO puštění tlačítka.
   Stav tlačítka proto bereme z DOM (Z.btn) a z event.buttons, ne z pořadí gridových událostí.
   Výběr myší řídí down/over, fokus resetuje výběr jen po klávesnici (šipky, Tab). */
document.addEventListener('mousedown',function(e){Z.mys=true;if(e.button===0)Z.btn=true;},true);
document.addEventListener('keydown',function(){Z.mys=false;},true);
document.addEventListener('mouseup',konecTazeni,true);
window.addEventListener('blur',konecTazeni);
return {
 clear:function(p){zrus(p.api);},
 down:function(p){var c=C(p.api),e=p.event||{},i=p.rowIndex;
  if(sl(p.column)!==c.zel||i==null||(p.node&&p.node.rowPinned)){zrus(p.api);return;}
  if(e.button!==0){if(c.sel){var r=roz(c.sel);if(i>=r[0]&&i<=r[1])return;}nastav(p.api,i,i);return;}
  if(e.shiftKey&&c.sel)nastav(p.api,c.sel.a,i);else nastav(p.api,i,i);
  if(Z.btn){c.drag=true;Z.drag=c;}},
 over:function(p){var c=C(p.api),e=p.event;if(!c.drag)return;
  if(!Z.btn||(e&&e.buttons!=null&&!(e.buttons&1))){c.drag=false;if(Z.drag===c)Z.drag=null;return;}
  if(!c.sel||sl(p.column)!==c.zel||p.rowIndex==null)return;
  nastav(p.api,c.sel.a,p.rowIndex);},
 focus:function(p){var c=C(p.api);if(Z.mys||Z.lock||p.rowIndex==null)return;
  if(sl(p.column)!==c.zel||p.rowPinned){zrus(p.api);return;}nastav(p.api,p.rowIndex,p.rowIndex);},
 key:function(p){var e=p.event;if(!e||e.type!=='keydown'||p.editing)return false;
  var api=p.api,c=C(api),k=e.key||'',i=p.node?p.node.rowIndex:null;
  var mod=(e.ctrlKey||e.metaKey)&&!e.altKey,holy=!e.ctrlKey&&!e.metaKey&&!e.altKey;
  if(i==null||p.node.rowPinned)return false;
  function q(){if(!c.sel)nastav(api,i,i);return {api:api,el:e.target&&e.target.closest?e.target.closest('.zk-grid'):null,
   cell:document.activeElement,row:i,col:c.zel};}
  if(mod&&(e.code==='KeyC'||k.toLowerCase()==='c')){var Q=q();
   kopiruj(Q,hodnoty(api,c).map(tsv).join('\r\n')+'\r\n');e.preventDefault();return true;}
  if(mod&&(e.code==='KeyV'||k.toLowerCase()==='v')){var Q=q();
   if(!c.pise){e.preventDefault();return true;}
   Z.pend=Q;var t=TA();t.value='';t.focus({preventScroll:true});
   setTimeout(function(){if(Z.pend===Q){Z.pend=null;zpet(Q);}},400);return true;}
  if(k==='Delete'&&holy&&!e.shiftKey){var Q=q();if(c.pise){var z=[],r=roz(c.sel);
   for(var j=r[0];j<=r[1];j++){var nd=api.getDisplayedRowAtIndex(j);
    if(nd&&nd.data&&(nd.data[c.zel]||'')!=='')z.push({id:nd.data.id,v:''});}
   if(z.length)posli(Q.el,z);}e.preventDefault();return true;}
  if(e.shiftKey&&holy&&(k==='ArrowDown'||k==='ArrowUp')){q();var b=c.sel.b+(k==='ArrowDown'?1:-1);
   if(b>=0&&b<api.getDisplayedRowCount()){nastav(api,c.sel.a,b);api.ensureIndexVisible(b);}
   e.preventDefault();return true;}
  return false;}
};})();}
"""
_ZK_CALL = 'function(p){{window.zkSel&&window.zkSel.{0}(p);}}'
_SEL_RULE = ("function(p){var s=p.context&&p.context.sel,i=p.rowIndex;"
             "if(!s||s.a===s.b||i==null||p.node.rowPinned)return false;"
             "var lo=Math.min(s.a,s.b),hi=Math.max(s.a,s.b);return %s;}")

# V aplikaci se list kreslí jako ostatní tabulky (Sankce) — z formátu Excelu se
# přebírají jen barvy, řez písma a zarovnání. Písmo, velikost, ohraničení a výšky
# řádků jsou aplikační. Export do Excelu jde dál 1:1 z originálu.
_UI_KLICE = ('backgroundColor', 'color', 'fontWeight', 'fontStyle', 'textDecoration', 'textAlign')


def _ui_styl(st: dict) -> dict:
    out = {k: v for k, v in (st or {}).items() if k in _UI_KLICE}
    if str(out.get('backgroundColor', '')).upper() == '#FFFFFF':
        out.pop('backgroundColor')
    if str(out.get('color', '')).upper() == '#000000':
        out.pop('color')
    if out.get('fontWeight') == '400':
        out.pop('fontWeight')
    if out.get('textAlign') == 'left':
        out.pop('textAlign')
    return out


def _ui_hlavicka(st: dict) -> dict:
    return {k: v for k, v in _ui_styl(st).items() if k in ('backgroundColor', 'color')}

_CELL_STYLE = ("function(p){{var S=(p.context&&p.context.S)||[];var m=p.data&&p.data._s;"
               "var i=(m&&m['{L}']!=null)?m['{L}']:{dom};return S[i]||null;}}")
_TOOLTIP_ZAPIS = ("function(p){var d=p.data||{};"
                  "return d._zby?('Zapsal(a): '+d._zby+(d._zat?' · '+d._zat:'')):null;}")
_CISLO_FMT = ("function(p){var v=p.value;if(v==null||v==='')return '';"
              "return (typeof v==='number')?String(v).replace('.',','):v;}")


def _grid_options(meta, radky, pise, prazdno_txt):
    zel = meta.get('zeleny')
    cols = []
    for i, s in enumerate(meta['sloupce']):
        L = s['L']
        cd = {
            'headerName': s['hdr'], 'field': L, 'colId': L, 'width': s['w'], 'minWidth': 24,
            'hide': bool(s.get('hide')), 'headerStyle': _ui_hlavicka(s['hst']),
            'filter': 'agNumberColumnFilter' if s.get('num') else 'agTextColumnFilter',
            'editable': bool(pise and L == zel),
            ':cellStyle': _CELL_STYLE.format(L=L, dom=int(s['dom'])),
        }
        if s.get('num'):
            cd[':valueFormatter'] = _CISLO_FMT
        if i < int(meta.get('pin') or 0):
            cd['pinned'] = 'left'
        if L == zel:
            cd[':tooltipValueGetter'] = _TOOLTIP_ZAPIS
            cd['cellClass'] = 'zk-zel'
            cd['cellClassRules'] = {':zk-sel': _SEL_RULE % 'i>=lo&&i<=hi',
                                    ':zk-sel-t': _SEL_RULE % 'i===lo',
                                    ':zk-sel-b': _SEL_RULE % 'i===hi'}
            cd[':suppressKeyboardEvent'] = 'function(p){return !!(window.zkSel&&window.zkSel.key(p));}'
            if pise:
                cd['cellEditor'] = 'agTextCellEditor'
                cd['cellEditorParams'] = {'maxLength': _MAX_ZAPIS}
        cols.append(cd)
    # Zelený sloupec na konci listu (Změna K2 / Delist — P) připnout vpravo, ať je
    # při širokém listu vidět bez posouvání; pořadí sloupců se tím nemění.
    viditelne = [c for c in cols if not c['hide']]
    if viditelne and viditelne[-1]['field'] == zel and 'pinned' not in viditelne[-1]:
        viditelne[-1]['pinned'] = 'right'
    opts = {
        'columnDefs': cols,
        'rowData': radky,
        'context': {'S': [_ui_styl(st) for st in meta['styly']],
                    'zel': zel, 'pise': bool(pise), 'sel': None},
        'defaultColDef': {'resizable': True, 'sortable': True, 'filter': True,
                          'cellDataType': False},
        'rowHeight': 32,
        'suppressMovableColumns': True,
        ':onFirstDataRendered': intranet_sankce._AUTOSIZE_FIT,
        ':onGridSizeChanged': intranet_sankce._AUTOSIZE_FIT,
        ':getRowId': "function(p){return ''+p.data.id;}",
        # Jako v Excelu: klik buňku vybere (a táhnutím rozšíří výběr), psaní/Enter/
        # dvojklik ji začne editovat.
        'singleClickEdit': False,
        ':onCellMouseDown': _ZK_CALL.format('down'),
        ':onCellMouseOver': _ZK_CALL.format('over'),
        ':onCellFocused': _ZK_CALL.format('focus'),
        ':onSortChanged': _ZK_CALL.format('clear'),
        ':onFilterChanged': _ZK_CALL.format('clear'),
        'stopEditingWhenCellsLoseFocus': True,
        'enableCellTextSelection': True,
        'tooltipShowDelay': 300,
        'animateRows': False,
        'overlayNoRowsTemplate': f'<span style="color:#64748b">{prazdno_txt}</span>',
    }
    return opts


async def _list_ui(soubor_id, klic, pr, user_id, user_name):
    kody = pr['kody'] if klic == 'sumarizace' else None
    meta, radky = await asyncio.to_thread(_nacti_list, soubor_id, klic, kody)
    if meta is None:
        ui.label('List se nepodařilo načíst.').classes('text-red-600 p-4')
        return
    pise = pr['pise'][klic]
    zel = meta.get('zeleny')
    mapa = {r['id']: r for r in radky}

    if kody is not None and not kody:
        prazdno = ('Nemáte přiřazenou žádnou zkratku nákupčího — požádejte správce '
                   '(Zalistovací komise → Rozdělení nákupčích).')
    else:
        prazdno = 'Žádné řádky.'

    with ui.row().classes('w-full items-center gap-3 py-3 flex-wrap'):
        hledat = ui.input(placeholder='Hledat v listu…').props('dense outlined clearable debounce=250') \
            .classes('w-72')
        with hledat.add_slot('prepend'):
            ui.icon('search', size='1.1rem').classes('text-gray-400')
        if kody is not None and kody:
            ui.label(f"Jen řádky nákupčích: {', '.join(sorted(kody))}") \
                .classes('text-xs font-semibold text-indigo-700 bg-indigo-50 rounded-md px-2 py-1')
        ui.space()
        btn_export = ui.button('Export do Excelu', icon='download') \
            .props('outline dense no-caps color=secondary') \
            .tooltip('Stáhne list 1:1 jako v nahraném souboru, se zapsanými hodnotami. '
                     'Je-li v listu aktivní filtr, exportují se jen vyfiltrované řádky.')

    grid = ui.aggrid(_grid_options(meta, radky, pise, prazdno)) \
        .classes('zk-grid w-full').style(intranet_sankce._GRID_STYLE)
    grid._update_method = None   # nepřestavovat grid při změně options (zmizely by filtry)

    hledat.on_value_change(lambda e: grid.run_grid_method('setGridOption', 'quickFilterText', e.value or ''))

    async def _on_change(e):
        a = e.args or {}
        rid = (a.get('data') or {}).get('id')
        if a.get('colId') != zel or rid not in mapa:
            return
        radek = mapa[rid]
        stara = radek.get(zel) or ''
        nova = str(a.get('newValue') if a.get('newValue') is not None else '').strip()
        if not pise:
            grid.run_grid_method('applyTransaction', {'update': [radek]})
            return
        if nova == stara:
            if str(a.get('newValue') or '') != nova:   # jen mezery kolem → vrátit oříznuté
                grid.run_grid_method('applyTransaction', {'update': [radek]})
            return
        if len(nova) > _MAX_ZAPIS:
            ui.notify(f'Hodnota je delší než {_MAX_ZAPIS} znaků.', type='warning')
            grid.run_grid_method('applyTransaction', {'update': [radek]})
            return
        ok, msg, akt, by, at = await asyncio.to_thread(
            _uloz_zapis, rid, klic, stara, nova, user_id, user_name)
        if not ok:
            ui.notify(msg, type='warning')
        radek[zel] = akt
        if by:
            radek['_zby'], radek['_zat'] = by, at
        grid.run_grid_method('applyTransaction', {'update': [radek]})

    grid.on('cellValueChanged', _on_change)

    async def _on_zapis(e):
        """Hromadný zápis z prohlížeče (Ctrl+V do výběru, Delete výběru)."""
        if not pise:
            return
        polozky, dlouhe = [], 0
        for z in ((e.args or {}).get('zmeny') or [])[:_MAX_VLOZENI]:
            rid = z.get('id') if isinstance(z, dict) else None
            if not isinstance(rid, int) or rid not in mapa:
                continue
            v = z.get('v')
            nova = str(v if v is not None else '').strip()
            if len(nova) > _MAX_ZAPIS:
                dlouhe += 1
                continue
            stara = mapa[rid].get(zel) or ''
            if nova != stara:
                polozky.append((rid, stara, nova))
        if dlouhe:
            ui.notify(f'{dlouhe}× hodnota delší než {_MAX_ZAPIS} znaků — nevložena.', type='warning')
        if not polozky:
            return
        vysl = await asyncio.to_thread(_uloz_zapisy, polozky, klic, user_id, user_name)
        upd, chyby = [], []
        for rid, ok, msg, akt, by, at in vysl:
            radek = mapa[rid]
            radek[zel] = akt
            if by:
                radek['_zby'], radek['_zat'] = by, at
            upd.append(radek)
            if not ok:
                chyby.append(msg)
        grid.run_grid_method('applyTransaction', {'update': upd})
        if chyby:
            ui.notify(f'Nezapsáno {len(chyby)} z {len(vysl)}: {chyby[0]}', type='warning')

    grid.on('zkZapis', _on_zapis)

    async def _export():
        ids = None
        try:
            vyfiltr = await grid.get_client_data(method='filtered_unsorted', timeout=5)
            if len(vyfiltr) < len(radky):
                ids = {r.get('id') for r in vyfiltr}
        except Exception:
            pass   # bez odpovědi klienta exportuje celý (povolený) list
        btn_export.props('loading')
        try:
            data, jmeno, chyba = await asyncio.to_thread(_export_listu, soubor_id, klic, kody, ids)
        finally:
            btn_export.props(remove='loading')
        if chyba:
            ui.notify(chyba, type='negative')
            return
        intranet_sankce._stahni_pres_http(data, jmeno)
        intranet_logger.log_activity(user_name, NAZEV, f'Export {jmeno}'
                                     + (f' ({len(ids)} vyfiltrovaných řádků)' if ids is not None else ''))

    btn_export.on_click(_export)


async def _detail_ui(soubor_id, pr, user_id, user_name, zpet):
    info = await asyncio.to_thread(_info_souboru, soubor_id)
    if not info:   # smazaný mezitím jiným uživatelem; zpet() tady ne — jsme uvnitř vykreslování
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.label('Zápis už neexistuje.').classes('text-lg text-gray-500')
            ui.button('Zpět na přehled', icon='arrow_back', on_click=zpet).props('flat no-caps')
        return
    viditelne = [l for l in LISTY if pr['vidi'][l[0]] and l[0] in info['listy']]

    async def _potvrd_smazani():
        with ui.dialog() as dlg, ui.card().classes('gap-3'):
            ui.label(f"Smazat zápis za {_datum_txt(info['obdobi'])}?").classes('text-lg font-bold')
            ui.label('Smaže se nahraný soubor i všechny zapsané hodnoty. Nevratné.') \
                .classes('text-sm text-red-700')
            with ui.row().classes('w-full justify-end'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')

                async def _smaz():
                    ok = await asyncio.to_thread(_smaz_obdobi, soubor_id)
                    dlg.close()
                    if not ok:
                        ui.notify('Smazání se nepodařilo.', type='negative')
                        return
                    intranet_logger.log_activity(
                        user_name, NAZEV, f"Smazán zápis {_datum_txt(info['obdobi'])}")
                    ui.notify('Zápis smazán.', type='positive')
                    zpet()
                ui.button('Smazat', icon='delete_forever', on_click=_smaz) \
                    .props('unelevated no-caps color=negative')
        dlg.open()

    def _tlacitko_smazat():
        if pr['admin']:
            ui.button(icon='delete_forever', on_click=_potvrd_smazani) \
                .props('flat round dense color=negative').tooltip('Smazat zápis')

    with ui.row().classes('w-full items-center gap-2 mb-1'):
        ui.button(icon='arrow_back', on_click=zpet).props('flat round dense color=primary') \
            .tooltip('Zpět na přehled')
        ui.label(f"Zápis komise {_datum_txt(info['obdobi'])}") \
            .classes('text-xl font-bold text-gray-800')

    if not viditelne:
        with ui.row().classes('items-center gap-2 p-6'):
            ui.label('V tomto zápisu není list, ke kterému máte přístup.').classes('text-gray-500')
            _tlacitko_smazat()
        return
    # záložky listů + koš hned za poslední záložkou, společná spodní linka
    with ui.row().classes('w-full items-center gap-1 no-wrap border-b border-gray-200'):
        with ui.tabs().props('dense no-caps align=left inline-label active-color=indigo-7 '
                             'indicator-color=indigo-7') as tabs:
            for klic, nazev, _, _, ikona in viditelne:
                ui.tab(klic, label=nazev, icon=ikona)
        _tlacitko_smazat()
    with ui.tab_panels(tabs, value=viditelne[0][0]).props('keep-alive').classes('w-full bg-transparent'):
        for klic, *_ in viditelne:
            with ui.tab_panel(klic).classes('p-0'):
                await _list_ui(soubor_id, klic, pr, user_id, user_name)


def _pocty_pro_uzivatele(pocty, klic, kody):
    n = v = 0
    for k, nakupci, pocet, vypl in pocty:
        if k != klic:
            continue
        if kody is not None and klic == 'sumarizace' and nakupci not in kody:
            continue
        n += pocet
        v += vypl
    return n, v


async def _prehled_ui(pr, otevri):
    data = await asyncio.to_thread(_prehled)
    if not data:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('inventory_2', size='4rem', color='grey-4')
            ui.label('Zatím nebyl nahrán žádný zápis.').classes('text-xl text-gray-400 font-bold')
        return
    with ui.element('div').classes('w-full grid gap-4') \
            .style('grid-template-columns: repeat(auto-fill, minmax(290px, 1fr))'):
        for s in data:
            listy = {k for k, *_ in s['pocty']}
            viditelne = [l for l in LISTY if pr['vidi'][l[0]] and l[0] in listy]
            if not viditelne:
                continue
            d = s['obdobi']
            with ui.card().classes('p-5 gap-3 cursor-pointer rounded-2xl border border-slate-200 '
                                   'shadow-sm hover:shadow-lg hover:border-indigo-300 transition') \
                    .on('click', lambda sid=s['id']: otevri(sid)):
                with ui.row().classes('items-center gap-3 no-wrap'):
                    ui.icon('event_note', size='2rem').classes('text-indigo-600 bg-indigo-50 p-2 rounded-xl')
                    with ui.column().classes('gap-0'):
                        ui.label(_datum_txt(d)).classes('text-2xl font-extrabold text-gray-800 leading-tight')
                        ui.label(f'Zalistovací komise · {_DNY[d.weekday()]}').classes('text-xs text-gray-500')
                for klic, nazev, *_ in viditelne:
                    n, v = _pocty_pro_uzivatele(s['pocty'], klic, pr['kody'])
                    with ui.column().classes('w-full gap-1'):
                        with ui.row().classes('w-full items-center justify-between'):
                            ui.label(nazev).classes('text-sm font-semibold text-gray-700')
                            ui.label(f'vyplněno {v} / {n}').classes('text-xs text-gray-500')
                        ui.linear_progress(value=(v / n) if n else 0, show_value=False, size='6px') \
                            .props(f"color={'positive' if n and v == n else 'indigo-5'} rounded track-color=grey-3")
                ui.label(f"Nahrál(a) {s['imported_by'] or '—'} · {_kdy(s['imported_at'])}") \
                    .classes('text-xs text-gray-400')


def _otevri_import(user_name, po_importu):
    stav = {'raw': None, 'jmeno': '', 'listy': None}

    with ui.dialog() as dlg, ui.card().classes('w-[580px] max-w-full gap-3'):
        ui.label('Nahrát zápis ze zalistovací komise').classes('text-lg font-bold')
        ui.label('Načtou se listy Sumarizace, Změna K2 a Delist. Nahrání stejného období zápis nahradí '
                 '— hodnoty zapsané v aplikaci u nezměněných řádků zůstanou.').classes('text-sm text-gray-600')
        datum = ui.input('Období (DD-MM-RR)', placeholder='18-09-26') \
            .props('outlined dense mask="##-##-##"').classes('w-56')
        upozorneni = ui.label().classes('text-sm text-amber-700')
        upozorneni.set_visibility(False)
        souhrn = ui.column().classes('w-full gap-1')
        mail = ui.checkbox('Zaslat e-mailem oznámení o nahrání nových dat', value=True)
        mail.set_visibility(False)

        def _parsuj_datum():
            try:
                return datetime.datetime.strptime(datum.value or '', '%d-%m-%y').date()
            except ValueError:
                return None

        async def _kontrola_obdobi():
            d = _parsuj_datum()
            existuje = d is not None and await asyncio.to_thread(_id_obdobi, d) is not None
            upozorneni.set_text(f'⚠ Zápis za {_datum_txt(d)} už existuje — bude nahrazen.' if existuje else '')
            upozorneni.set_visibility(existuje)

        datum.on_value_change(_kontrola_obdobi)

        async def _on_upload(e):
            zdroj = None   # napříč verzemi NiceGUI se obsah nahraného souboru jmenuje různě
            for attr in ('content', 'file', 'stream', 'data', 'file_obj'):
                val = getattr(e, attr, None)
                if val is not None and hasattr(val, 'read'):
                    zdroj = val
                    break
            up.reset()
            souhrn.clear()
            stav.update(raw=None, listy=None)
            btn.disable()
            mail.set_visibility(False)
            if zdroj is None:
                with souhrn:
                    ui.label('❌ Soubor se nepodařilo načíst.').classes('text-sm text-red-600')
                return
            raw = zdroj.read()
            if inspect.isawaitable(raw):
                raw = await raw
            jmeno = getattr(e, 'name', '') or getattr(zdroj, 'name', '') or 'zapis.xlsx'
            if len(raw) > _MAX_SOUBOR:
                with souhrn:
                    ui.label('❌ Soubor je větší než 20 MB.').classes('text-sm text-red-600')
                return
            listy, varovani, chyba = await asyncio.to_thread(_rozeber_sesit, raw)
            with souhrn:
                ui.label(f'📄 {jmeno}').classes('text-sm font-semibold text-gray-700')
                for klic, nazev, *_ in LISTY:
                    if klic in listy:
                        ui.label(f"✓ {nazev} — {len(listy[klic]['radky'])} řádků") \
                            .classes('text-sm text-green-700')
                for v in varovani:
                    ui.label(f'⚠ {v}').classes('text-sm text-amber-700')
                if chyba:
                    ui.label(f'❌ {chyba}').classes('text-sm text-red-600')
            if chyba:
                return
            stav.update(raw=raw, jmeno=jmeno, listy=listy)
            if not datum.value:
                m = re.search(r'(20\d{2})-(\d{2})-(\d{2})', jmeno)
                if m:
                    datum.set_value(f'{m.group(3)}-{m.group(2)}-{m.group(1)[2:]}')
            mail.set_visibility(True)
            btn.enable()

        up = ui.upload(on_upload=_on_upload, auto_upload=True, max_file_size=_MAX_SOUBOR,
                       label='Vybrat soubor .xlsx / .xlsm') \
            .props('accept=.xlsx,.xlsm').classes('w-full')

        async def _importuj():
            d = _parsuj_datum()
            if d is None:
                ui.notify('Zadejte období ve formátu DD-MM-RR.', type='warning')
                return
            if not stav['listy']:
                ui.notify('Nejdřív vyberte soubor.', type='warning')
                return
            dlg_kruh, _, _ = prekryv_kolecko('Importuji zápis…', procenta=False)
            dlg_kruh.open()
            try:
                ok, msg, nahrazeno, prevzato = await asyncio.to_thread(
                    _uloz_import, d, stav['jmeno'], stav['raw'], stav['listy'], user_name)
            finally:
                dlg_kruh.close()
                dlg_kruh.delete()
            if not ok:
                ui.notify(msg, type='negative')
                return
            listy_pocty = [(nazev, len(stav['listy'][klic]['radky']))
                           for klic, nazev, *_ in LISTY if klic in stav['listy']]
            intranet_logger.log_activity(
                user_name, NAZEV,
                f"{'Nahrazen' if nahrazeno else 'Nahrán'} zápis {_datum_txt(d)} ({stav['jmeno']}): "
                + ', '.join(f'{n} {p}' for n, p in listy_pocty)
                + (f'; převzato {prevzato} zápisů' if nahrazeno else ''))
            ui.notify(f"Zápis za {_datum_txt(d)} {'nahrazen' if nahrazeno else 'nahrán'}"
                      + (f' — převzato {prevzato} zapsaných hodnot.' if nahrazeno and prevzato else '.'),
                      type='positive')
            if mail.value:
                background_tasks.create(_rozesli_oznameni(d, listy_pocty, user_name),
                                        name='zalistovaci-oznameni')
            dlg.close()
            po_importu()

        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
            btn = ui.button('Importovat', icon='upload', on_click=_importuj) \
                .props('unelevated no-caps color=primary')
            btn.disable()
    dlg.open()


@refreshable_na_klienta
async def vykresli_zalistovaci(user_id, user_name: str, vsechna_prava):
    await asyncio.to_thread(inicializace_db)
    pr = prava(vsechna_prava)
    vidi = any(pr['vidi'].values())

    if not vidi and not pr['importuje']:
        with ui.column().classes('items-center py-20 gap-3 w-full'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('Nemáte přístup k Zalistovací komisi.').classes('text-lg text-gray-400')
        return

    ui.add_css(_CSS)
    ui.run_javascript(_ZK_JS)
    stav = {'soubor': None}

    def _hlavicka():
        """Hlavička modulu — jen na přehledu; v detailu zápisu je nahoře pouze jeho název."""
        with ui.row().classes('w-full items-center gap-3 mb-4'):
            ui.icon('how_to_vote', size='2.2rem').classes('text-indigo-600')
            with ui.column().classes('gap-0'):
                ui.label(NAZEV).classes('text-3xl font-extrabold text-gray-800')
                ui.label('Zápisy z komise — Sumarizace, Změna K2, Delist').classes('text-sm text-gray-500')
            ui.space()
            if pr['importuje']:
                ui.button('Nahrát zápis', icon='upload_file',
                          on_click=lambda: _otevri_import(user_name, obsah.refresh if vidi else (lambda: None))) \
                    .props('unelevated no-caps color=primary')

    @ui.refreshable
    async def obsah():
        if stav['soubor'] is None:
            _hlavicka()
            await _prehled_ui(pr, otevri)
        else:
            await _detail_ui(stav['soubor'], pr, user_id, user_name, zpet)

    def otevri(soubor_id):
        stav['soubor'] = soubor_id
        obsah.refresh()

    def zpet():
        stav['soubor'] = None
        obsah.refresh()

    if not vidi:
        _hlavicka()
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('upload_file', size='4rem', color='grey-4')
            ui.label('Smíte pouze nahrávat data.').classes('text-xl text-gray-400 font-bold')
            ui.label('Zápisy zobrazit nemůžete.').classes('text-sm text-gray-400')
        return

    await obsah()
