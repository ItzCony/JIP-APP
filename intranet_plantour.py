# -*- coding: utf-8 -*-
"""Modul ASM Plantour — jak obchodní zástupci (OZ) využívají Plantour.

Přepis makra modReport ze souboru Plantour_report.xlsm. Analytik každý měsíc nahraje
export z Plantouru (list „Data"), aplikace z něj spočítá listy „Detail OZ" a „Report"
stejně jako makro. List „Ovládání" se nepoužívá — limity jsou pevné (LIMITY),
počet pracovních dnů se bere z hlavičky „Fond (20 prac. dnů)" jako v makru.

Kdo co vidí (filtr je na serveru, platí i pro export):
  plantour_analytik, „vse"  – všechna ASM, nahrává data
  plantour_ctenar           – všechna ASM
  plantour_pobocka_<kód>    – vedoucí pobočky: ASM své pobočky (pobočka ASM z asm_oz)
  ASM (bez práva)           – svá data; ASM se páruje s uživatelem podle příjmení
                              jako v Lupě (nejednoznačné příjmení = nikdo)
"""
import asyncio
import datetime
import io
import json
import re
import time
from decimal import Decimal, ROUND_HALF_UP

from nicegui import ui, app

import intranet_asm
import intranet_data
import intranet_logger
from intranet_lupa import slug, zaklad_asm, _tmp_cesta, _stahni_soubor, _MESICE_CELE

PRAVO_ANALYTIK = 'plantour_analytik'
PRAVO_CTENAR = 'plantour_ctenar'
PREFIX_POBOCKA = 'plantour_pobocka_'

HODIN_DEN = 8.0       # čistá práce za den (makro: P_Hodiny)
DNY_VYCHOZI = 20      # když export počet dnů v hlavičce neuvádí
# Limity z listu Ovládání (zadání: pevné)
LIMIT_KANCELAR = 0.25     # podíl kanceláře + tel. objednávek z fondu, víc = „hodně kanceláře"
MIN_NAVSTEV_DEN = 3
MIN_POKRYTI = 0.30
LIMIT_MIMORADNE = 0.50
MIN_DELKA_MIN = 5         # průměrná návštěva kratší = podezřelá
LIMIT_NEVYKAZANO = 0.30

# ASM, která v asm_oz chybí — pobočku určil zadavatel (kód jako asm_oz.pobocka)
_POBOCKA_NAVIC = {'ZYKA': '014', 'FULIER': '026'}   # Liberec, Ostrava

_TABULKA = """CREATE TABLE IF NOT EXISTS plantour_data (
        obdobi CHAR(7) NOT NULL PRIMARY KEY,
        hlavicka TEXT NOT NULL,
        radky LONGTEXT NOT NULL,
        asm_seznam TEXT NOT NULL,
        pocet INT NOT NULL DEFAULT 0,
        soubor VARCHAR(255),
        nahral VARCHAR(255),
        kdy DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"""

_DB_INIT_HOTOVO = False


def inicializace_db():
    global _DB_INIT_HOTOVO
    if _DB_INIT_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        conn.cursor().execute(_TABULKA)
        conn.commit()
        _DB_INIT_HOTOVO = True
    finally:
        conn.close()


def _dotaz(sql, parametry=()) -> list:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, parametry)
        return cur.fetchall()
    finally:
        conn.close()


# ── Práva ────────────────────────────────────────────────────────────────────

def je_analytik(prava) -> bool:
    return 'vse' in prava or PRAVO_ANALYTIK in prava


def vidi_vse(prava) -> bool:
    return je_analytik(prava) or PRAVO_CTENAR in prava


def _moje_pobocky(prava) -> set:
    return {p[len(PREFIX_POBOCKA):] for p in prava if p.startswith(PREFIX_POBOCKA)}


_CACHE = {'cas': 0.0, 'mapa': {}}
_CACHE_TTL = 600.0


def vymaz_cache():
    _CACHE['cas'] = 0.0


# Změna příjmení / deaktivace účtu → párování ASM hned znovu.
intranet_data.PO_ZMENE_UZIVATELU.append(vymaz_cache)


def _mapa_asm() -> dict:
    """{ASM: (user_id | None, kód pobočky | None)} pro ASM ze všech nahraných měsíců.
    Volá se při každém vykreslení přehledu dlaždic → 10min cache, chyba DB = prázdná mapa."""
    if time.time() - _CACHE['cas'] < _CACHE_TTL:
        return _CACHE['mapa']
    mapa = {}
    try:
        inicializace_db()
        asmy = {zaklad_asm(a) for r in _dotaz("SELECT asm_seznam FROM plantour_data")
                for a in r['asm_seznam'].split(',')} - {''}
        uzivatele = {}
        for u in _dotaz("SELECT iduser, surname FROM user WHERE is_active=1"):
            uzivatele.setdefault(slug(u['surname']), []).append(u['iduser'])
        pobocky = {}
        # vzestupně podle počtu OZ → u ASM na více pobočkách vyhraje ta nejčastější
        for r in _dotaz("SELECT asm, pobocka, COUNT(*) AS n FROM asm_oz "
                        "WHERE asm<>'' AND pobocka<>'' GROUP BY asm, pobocka ORDER BY n"):
            pobocky[zaklad_asm(r['asm'])] = r['pobocka']
        for a in asmy:
            uid = uzivatele.get(a, [])
            mapa[a] = (uid[0] if len(uid) == 1 else None, pobocky.get(a) or _POBOCKA_NAVIC.get(a))
    except Exception as e:
        print(f'[plantour] _mapa_asm: {e!r}')
    _CACHE.update(cas=time.time(), mapa=mapa)
    return mapa


def videna_asm(user_id, prava):
    """None = všechna ASM, jinak množina ASM (zaklad_asm), jejichž řádky uživatel vidí."""
    if vidi_vse(prava):
        return None
    pobocky = _moje_pobocky(prava)
    return {a for a, (uid, pob) in _mapa_asm().items() if uid == user_id or pob in pobocky}


def ma_pristup(user_id, prava) -> bool:
    return vidi_vse(prava) or bool(_moje_pobocky(prava)) or bool(videna_asm(user_id, prava))


# ── Výpočet (přepis makra) ───────────────────────────────────────────────────

# (klíč, název sloupce v exportu, výchozí pozice) — NajdiSloupec v makru
_SLOUPCE = (('asm', 'ASM', 1), ('cislo', 'Číslo', 3), ('jmeno', 'Jméno', 4),
            ('nav_plan', 'Počet návštěv', 5), ('zak_nav', 'Navštívených zákazníků', 6),
            ('zak', 'Počet zákazníků', 7), ('hruby', 'Hrubý čas u zákazníka', 9),
            ('cisty', 'Čistý čas u zákazníka', 10), ('mim', 'Mimořádná návštěva', 11),
            ('nav_mim', 'Počet mimořádných návštěv', 12), ('kanc', 'Kancelář', 13),
            ('rozvoz', 'Rozvoz', 14), ('tel', 'Tel. objednávky', 15),
            ('nahr', 'Náhradní aktivita', 16), ('dov', 'Dovolená', 17), ('nemoc', 'Nemoc', 18))


def _indexy(hlavicka) -> dict:
    nazvy = [str(h or '').strip().lower() for h in hlavicka]
    return {k: nazvy.index(n.lower()) if n.lower() in nazvy else vychozi - 1
            for k, n, vychozi in _SLOUPCE}


def _bunka(radek, ix, klic):
    i = ix[klic]
    return radek[i] if i < len(radek) else None


def _txt(v) -> str:
    return '' if v is None else str(v)


def _num(v) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace('\xa0', '').replace(' ', '').replace(',', '.'))
    except ValueError:
        return 0.0


def _val(s) -> float:
    """VBA Val(): číslo na začátku textu, jinak 0."""
    g = re.match(r'\d*\.?\d*', s).group()
    return float(g) if g.strip('.') else 0.0


def _hodiny(v) -> float:
    """„122h 42min" → hodiny; číslo = excelový čas ve dnech. Jako Hodiny() v makru."""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return v * 24
    s = str(v).lower().replace(' ', '').replace('\xa0', '')
    h = 0.0
    if 'h' in s:
        pred, s = s.split('h', 1)
        h = _val(pred)
    m = _val(s.split('m', 1)[0]) if 'm' in s else 0.0
    return h + m / 60


def _podil(a, b) -> float:
    return a / b if b else 0.0


def _fmt(x, des=0) -> str:
    """Jako VBA Format(x, "0.0") v českém Excelu: 15 platných číslic, půlka nahoru, čárka."""
    q = Decimal(1).scaleb(-des)
    return str(Decimal(f'{x:.15g}').quantize(q, ROUND_HALF_UP)).replace('.', ',')


def _razeni_makra(seznam, pred):
    """Řazení výměnami jako v makru — u shodných hodnot dá stejné pořadí jako Excel."""
    a = list(seznam)
    for i in range(len(a) - 1):
        for j in range(i + 1, len(a)):
            if pred(a[j], a[i]):
                a[i], a[j] = a[j], a[i]
    return a


def _dny_z_hlavicky(hlavicka) -> float:
    """„Fond (20 prac. dnů)" → 20; bez takového sloupce 0."""
    for h in hlavicka:
        h = str(h or '')
        if h.strip().lower().startswith('fond'):
            m = re.search(r'\d+', h.split('(', 1)[1]) if '(' in h else None
            return float(m.group()) if m else 0.0
    return 0.0


def _prekroceni(o):
    """(sloupec, text do hodnocení, červeně) — překročené limity aktivního OZ v pořadí makra."""
    if o['p_nevyk'] > LIMIT_NEVYKAZANO:
        yield 'p_nevyk', f"nevykázaný čas {_fmt(o['p_nevyk'] * 100)} %", True
    if o['nav_den'] < MIN_NAVSTEV_DEN:
        yield 'nav_den', 'málo návštěv', False
    if o['p_kanc'] > LIMIT_KANCELAR:
        yield 'p_kanc', 'hodně kanceláře', True
    if o['zak'] > 0 and o['pokryti'] < MIN_POKRYTI:
        yield 'pokryti', 'nízké pokrytí zákazníků', False
    if o['p_mim'] > LIMIT_MIMORADNE:
        yield 'p_mim', 'převaha mimořádných návštěv', False
    if o['nav'] > 0 and o['delka'] < MIN_DELKA_MIN:
        yield 'delka', f"podezřele krátké návštěvy (Ø {_fmt(o['delka'], 1)} min)", True


def _nacti_oz(hlavicka, radky, dny, fond) -> list:
    """NactiData + SpocitejOZ z makra."""
    ix = _indexy(hlavicka)
    vysledek = []
    for r in radky:
        jmeno, cislo = _txt(_bunka(r, ix, 'jmeno')).strip(), _txt(_bunka(r, ix, 'cislo')).strip()
        if not (jmeno or cislo):
            continue
        h = {k: _hodiny(_bunka(r, ix, k)) for k in
             ('hruby', 'cisty', 'mim', 'kanc', 'rozvoz', 'tel', 'nahr', 'dov', 'nemoc')}
        o = {'asm': _txt(_bunka(r, ix, 'asm')).strip() or '(bez ASM)', 'cislo': cislo, 'jmeno': jmeno,
             'nav_plan': _num(_bunka(r, ix, 'nav_plan')), 'nav_mim': _num(_bunka(r, ix, 'nav_mim')),
             'zak_nav': _num(_bunka(r, ix, 'zak_nav')), 'zak': _num(_bunka(r, ix, 'zak')),
             'h_zak': h['cisty'] + h['mim'], 'h_kanc': h['kanc'], 'h_roz': h['rozvoz'],
             'h_tel': h['tel'], 'h_jine': h['nahr'] + h['dov'] + h['nemoc']}
        o['h_hruby'] = max(h['hruby'], o['h_zak'])
        o['nav'] = o['nav_plan'] + o['nav_mim']
        o['aktivni'] = o['nav'] > 0 or o['h_zak'] + o['h_kanc'] + o['h_tel'] + o['h_roz'] > 0
        o['nav_den'] = _podil(o['nav'], dny)
        o['p_zak'] = _podil(o['h_zak'], fond)
        o['p_kanc'] = _podil(o['h_kanc'] + o['h_tel'], fond)
        o['p_roz'] = _podil(o['h_roz'], fond)
        o['p_prej'] = _podil(o['h_hruby'] - o['h_zak'], fond)
        o['p_jine'] = _podil(o['h_jine'], fond)
        o['p_nevyk'] = max(0.0, 1 - _podil(o['h_hruby'] + o['h_kanc'] + o['h_tel'] + o['h_roz']
                                           + o['h_jine'], fond))
        o['p_mim'] = _podil(o['nav_mim'], o['nav'])
        o['pokryti'] = _podil(o['zak_nav'], o['zak'])
        o['frekv'] = _podil(o['nav_plan'], o['zak_nav'])
        o['delka'] = _podil(o['h_zak'] * 60, o['nav'])
        if o['aktivni']:
            prekroceni = list(_prekroceni(o))
            o['hodnoceni'] = ', '.join(t for _, t, _ in prekroceni) or 'OK'
            o['uroven'] = 1 if prekroceni else 0
            o['barvy'] = {k: 'cervena' if c else 'zluta' for k, _, c in prekroceni}
        else:
            o['hodnoceni'] = 'NEPOUŽÍVÁ PLANTOUR' + (
                ' (evidována jen dovolená / nemoc / náhr. aktivita)' if o['h_jine'] > 0 else '')
            o['uroven'] = 2
            o['barvy'] = {k: 'seda' for k, _, _ in DETAIL[:-1]}
        o['barvy']['hodnoceni'] = ('zelena', 'zluta', 'cervena_tucne')[o['uroven']]
        vysledek.append(o)
    return vysledek


_SOUCTY = ('nav', 'nav_mim', 'nav_plan', 'zak_nav', 'h_zak', 'h_kanc', 'h_tel', 'h_roz',
           'h_hruby', 'h_jine')


def _souhrn(nazev, seznam, dny, fond) -> dict:
    """PricistOZ + DopocitejASM: týmové hodnoty jen z OZ, kteří Plantour používají."""
    akt = [o for o in seznam if o['aktivni']]
    pokr = [o for o in akt if o['zak'] > 0]
    a = {k: sum(o[k] for o in akt) for k in _SOUCTY}
    f = len(akt) * fond
    a.update(nazev=nazev, pocet=len(seznam), aktivni=len(akt), nepouziva=len(seznam) - len(akt),
             nav_den=_podil(a['nav'], len(akt) * dny),
             pokryti=_podil(sum(o['zak_nav'] for o in pokr), sum(o['zak'] for o in pokr)),
             p_zak=_podil(a['h_zak'], f), p_kanc=_podil(a['h_kanc'] + a['h_tel'], f),
             p_roz=_podil(a['h_roz'], f), p_prej=_podil(a['h_hruby'] - a['h_zak'], f),
             p_jine=_podil(a['h_jine'], f),
             p_nevyk=max(0.0, 1 - _podil(a['h_hruby'] + a['h_kanc'] + a['h_tel'] + a['h_roz']
                                         + a['h_jine'], f)) if akt else 0.0,
             p_mim=_podil(a['nav_mim'], a['nav']), frekv=_podil(a['nav_plan'], a['zak_nav']),
             delka=_podil(a['h_zak'] * 60, a['nav']))
    return a


def vypocet(hlavicka, radky) -> dict:
    """Celý výpočet makra nad řádky listu Data (už vyfiltrovanými na to, co uživatel vidí)."""
    dny_hlavicka = _dny_z_hlavicky(hlavicka)
    dny = dny_hlavicka or DNY_VYCHOZI
    fond = dny * HODIN_DEN
    oz = _nacti_oz(hlavicka, radky, dny, fond)
    po_asm = {}
    for o in oz:
        po_asm.setdefault(o['asm'], []).append(o)
    asm = _razeni_makra([_souhrn(n, s, dny, fond) for n, s in po_asm.items()],
                        lambda a, b: a['nav_den'] > b['nav_den'])
    return {'dny': dny, 'dny_z_exportu': bool(dny_hlavicka), 'fond': fond, 'oz': oz, 'asm': asm,
            'celkem': _souhrn('CELKEM', oz, dny, fond)}


def detail(v) -> list:
    """List Detail OZ: řazeno podle ASM a návštěv za den (sestupně)."""
    return sorted(v['oz'], key=lambda o: (o['asm'].upper(), -o['nav_den']))


def report(v) -> dict:
    """List Report: struktura doby po ASM (+ CELKEM), kdo Plantour nepoužil, TOP 10 kancelář."""
    struktura = [a for a in _razeni_makra(v['asm'], lambda a, b: a['p_zak'] > b['p_zak'])
                 if a['aktivni'] > 0]
    top = _razeni_makra([o for o in v['oz'] if o['aktivni']],
                        lambda a, b: a['p_kanc'] > b['p_kanc'])[:10]
    return {'struktura': struktura + [v['celkem']],
            'nepouziva': [o for a in v['asm'] for o in v['oz']
                          if not o['aktivni'] and o['asm'] == a['nazev']],
            'top_kancelar': [{**o, 'jmeno': f"{i}. {o['jmeno']}"} for i, o in enumerate(top, 1)]}


def text_fondu(v) -> str:
    t = (f"Pracovní fond na OZ: {_fmt(v['dny'])} prac. dnů × {_fmt(HODIN_DEN, 1)} h = "
         f"{_fmt(v['fond'], 1)} h")
    return t + (' (počet dnů převzat z exportu)' if v['dny_z_exportu']
                else f' (export počet dnů neuvádí, počítáno s {DNY_VYCHOZI})')


def popis_detailu(v) -> str:
    return (f"Modelováno na proces: OZ pracuje pondělí až pátek {_fmt(HODIN_DEN + 0.5, 1)} h denně, "
            f"z toho 0,5 h pauza na oběd = {_fmt(HODIN_DEN, 1)} h čisté práce. Pracovní fond = "
            f"{_fmt(v['dny'])} prac. dnů × {_fmt(HODIN_DEN, 1)} h = {_fmt(v['fond'], 1)} h na OZ. "
            "Naplánované návštěvy celkem = návštěvy z Plantouru + mimořádné návštěvy (naplánované OZ). "
            "Čas u zákazníka = čistý čas u zákazníka + mimořádné návštěvy. Přejezdy = hrubý čas "
            "u zákazníka − čas u zákazníka. Kancelář zahrnuje i telefonické objednávky. NEVYKÁZÁNO = "
            "fond, který nepokrývá žádný záznam v Plantouru (zákazník, přejezdy, kancelář, rozvoz, "
            "dovolená, nemoc, náhr. aktivita). Procenta jsou podílem z pracovního fondu. Řazeno podle "
            "ASM a počtu návštěv za den. Barevně jsou zvýrazněna překročení limitů: méně než "
            f"{MIN_NAVSTEV_DEN} návštěvy za den, pokrytí portfolia pod {_fmt(MIN_POKRYTI * 100)} %, "
            f"kancelář + tel. nad {_fmt(LIMIT_KANCELAR * 100)} %, mimořádné návštěvy nad "
            f"{_fmt(LIMIT_MIMORADNE * 100)} %, návštěva kratší než {MIN_DELKA_MIN} min, "
            f"nevykázáno nad {_fmt(LIMIT_NEVYKAZANO * 100)} %.")


VYSVETLIVKY = ("Procenta jsou podílem z pracovního fondu. Kancelář = Kancelář + Tel. objednávky. "
               "Čas u zákazníka = Čistý čas u zákazníka + Mimořádná návštěva. Týmové hodnoty v grafu "
               "jsou počítány jen z OZ, kteří Plantour používají. Přejezdy = hrubý čas u zákazníka − "
               "čas u zákazníka. NEVYKÁZÁNO = část pracovního fondu, kterou nepokrývá žádný záznam "
               "v Plantouru. Kompletní údaje o všech OZ jsou v Detailu OZ.")

# (klíč, popisek, formát) — sloupce listu Detail OZ
DETAIL = [
    ('asm', 'ASM', 't'), ('cislo', 'Číslo', 't'), ('jmeno', 'Jméno OZ', 't'),
    ('nav', 'Naplánované návštěvy celkem', 'int'), ('nav_mim', 'z toho mimořádné', 'int'),
    ('p_mim', 'Podíl mimořádných', 'pct0'), ('nav_den', 'Návštěv / den', 'd1'),
    ('zak_nav', 'Navštívení zákazníci', 'int'), ('zak', 'Přidělení zákazníci', 'int'),
    ('pokryti', 'Pokrytí portfolia', 'pct0'), ('frekv', 'Návštěv na 1 zákazníka', 'd1'),
    ('h_zak', 'Čas u zákazníka (h:mm)', 'hm'), ('delka', 'Ø délka návštěvy (min)', 'd0'),
    ('p_zak', '% doby u zákazníka', 'pct1'), ('h_kanc', 'Kancelář (h:mm)', 'hm'),
    ('h_tel', 'Tel. objednávky (h:mm)', 'hm'), ('p_kanc', '% doby kancelář + tel.', 'pct1'),
    ('h_roz', 'Rozvoz (h:mm)', 'hm'), ('p_roz', '% doby rozvoz', 'pct1'),
    ('p_prej', '% doby přejezdy', 'pct1'),
    ('p_jine', '% doby dovolená, nemoc, náhr. aktivita', 'pct1'),
    ('p_nevyk', '% doby NEVYKÁZÁNO', 'pct1'), ('hodnoceni', 'Hodnocení', 't'),
]
# sloupec zůstane prázdný, když je hodnota v klíči nulová (jako v makru)
_PRAZDNE_BEZ = {'p_mim': 'nav', 'zak': 'zak', 'pokryti': 'zak', 'frekv': 'zak_nav',
                'delka': 'nav', 'p_nevyk': 'aktivni'}

# (klíč, název, barva) — graf i tabulka struktury pracovní doby
STRUKTURA = [('p_zak', 'U zákazníka', '#70AD47'), ('p_prej', 'Přejezdy', '#BFBFBF'),
             ('p_kanc', 'Kancelář + tel. objednávky', '#ED7D31'), ('p_roz', 'Rozvoz', '#5B9BD5'),
             ('p_jine', 'Dovolená, nemoc, náhr. aktivita', '#B4A7D6'),
             ('p_nevyk', 'NEVYKÁZÁNO', '#FF5050')]

NEPOUZIVA = [('jmeno', 'Jméno OZ', 't'), ('asm', 'ASM', 't'), ('cislo', 'Číslo', 't'),
             ('zak', 'Přidělení zákazníci', 'int')]

TOP_KANCELAR = [('jmeno', 'Jméno OZ', 't'), ('asm', 'ASM', 't'),
                ('nav', 'Naplánované návštěvy celkem', 'int'), ('nav_den', 'Návštěvy / den', 'd1'),
                ('pokryti', 'Pokrytí portfolia', 'pct1'), ('p_zak', '% doby u zákazníka', 'pct1'),
                ('p_kanc', '% doby kancelář + tel.', 'pct1'), ('p_roz', '% doby rozvoz', 'pct1'),
                ('p_nevyk', '% doby NEVYKÁZÁNO', 'pct1'), ('hodnoceni', 'Hodnocení', 't')]


def hodnota(o, klic):
    zdroj = _PRAZDNE_BEZ.get(klic)
    return None if zdroj and not o[zdroj] else o[klic]


# ── Import ───────────────────────────────────────────────────────────────────

def nazev_obdobi(obdobi) -> str:
    rok, mes = obdobi.split('-')
    return f'{_MESICE_CELE[int(mes) - 1]} {rok}'


def _hodnota_bunky(v):
    """Buňka Excelu → hodnota pro JSON. Časy jako text „5h 30min", stejně jako v exportu."""
    if isinstance(v, datetime.timedelta):
        minuty = round(v.total_seconds() / 60)
    elif isinstance(v, datetime.time):
        minuty = v.hour * 60 + v.minute + round(v.second / 60)
    elif isinstance(v, float) and v.is_integer():
        return int(v)
    elif isinstance(v, datetime.date):
        return v.isoformat()
    elif isinstance(v, str):
        return v.strip()
    else:
        return v
    return f'{minuty // 60}h {minuty % 60}min'


def _obdobi_souboru(wb):
    """Měsíc z P_Obdobi (list Ovládání, „listopad 2026") → '2026-11'; nejde-li zjistit, None."""
    try:
        nazev_listu, bunka = next(wb.defined_names['P_Obdobi'].destinations)
        v = wb[nazev_listu][bunka.replace('$', '')].value
    except Exception:
        return None
    if isinstance(v, datetime.date):
        return f'{v.year:04d}-{v.month:02d}'
    m = re.match(r'\s*(\w+)\s+(\d{4})', str(v or '').lower())
    if m and m.group(1) in _MESICE_CELE:
        return f'{m.group(2)}-{_MESICE_CELE.index(m.group(1)) + 1:02d}'
    return None


def _najdi_data(wb):
    """List s daty a číslo řádku hlavičky (ASM v 1. sloupci) — NajdiDataList v makru."""
    for ws in sorted(wb.worksheets, key=lambda w: w.title.strip().lower() != 'data'):
        if ws.title.strip().lower() in ('report', 'detail oz', 'ovládání'):
            continue
        for i, radek in enumerate(ws.iter_rows(max_row=20, values_only=True), start=1):
            if radek and str(radek[0] or '').strip().upper() == 'ASM':
                return ws, i
    return None, 0


def importuj_soubor(raw, nazev, obdobi, user_name) -> str:
    """Uloží list Data k měsíci (nahradí ho celý). Chyba → ValueError. Blokuje — volat ve vlákně."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as e:
        raise ValueError(f'soubor nejde otevřít jako Excel ({e})')
    ze_souboru = _obdobi_souboru(wb)
    if ze_souboru and ze_souboru != obdobi:
        raise ValueError(f'soubor je za {nazev_obdobi(ze_souboru)}, '
                         f'ale vybraný měsíc je {nazev_obdobi(obdobi)}')
    ws, radek_hlavicky = _najdi_data(wb)
    if not ws:
        raise ValueError('v souboru chybí list Data (hlavička se sloupcem ASM v prvním sloupci)')
    radky = ws.iter_rows(min_row=radek_hlavicky, values_only=True)
    hlavicka = [str(h or '').strip() for h in next(radky)]
    while hlavicka and not hlavicka[-1]:
        hlavicka.pop()
    ix = _indexy(hlavicka)
    data = []
    for r in radky:
        r = [_hodnota_bunky(v) for v in r[:len(hlavicka)]]
        if _txt(_bunka(r, ix, 'jmeno')).strip() or _txt(_bunka(r, ix, 'cislo')).strip():
            data.append(r)
    if not data:
        raise ValueError('list Data neobsahuje žádného OZ')
    asm_seznam = sorted({_txt(_bunka(r, ix, 'asm')).strip() for r in data} - {''})
    inicializace_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        raise ValueError('databáze není dostupná')
    try:
        conn.cursor().execute(
            "REPLACE INTO plantour_data (obdobi, hlavicka, radky, asm_seznam, pocet, soubor, nahral) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (obdobi, json.dumps(hlavicka, ensure_ascii=False),
             json.dumps(data, ensure_ascii=False, default=str), ','.join(asm_seznam), len(data),
             nazev, user_name))
        conn.commit()
    finally:
        conn.close()
    vymaz_cache()
    return f'{nazev_obdobi(obdobi)}: {len(data)} OZ, {len(asm_seznam)} ASM'


def nahrana_obdobi() -> list:
    inicializace_db()
    return _dotaz("SELECT obdobi, pocet, soubor, nahral, kdy FROM plantour_data ORDER BY obdobi DESC")


def nacti_obdobi(obdobi):
    r = _dotaz("SELECT obdobi, hlavicka, radky FROM plantour_data WHERE obdobi=%s", (obdobi,))
    if not r:
        return None
    return {'obdobi': obdobi, 'hlavicka': json.loads(r[0]['hlavicka']),
            'radky': json.loads(r[0]['radky'])}


def videne_radky(data, user_id, prava) -> list:
    """Řádky listu Data, které uživatel smí vidět (podle sloupce ASM)."""
    videna = videna_asm(user_id, prava)
    if videna is None:
        return data['radky']
    ix = _indexy(data['hlavicka'])
    return [r for r in data['radky'] if zaklad_asm(_bunka(r, ix, 'asm')) in videna]


# ── Export XLSX ──────────────────────────────────────────────────────────────

_XLSX_FORMATY = {'int': '#,##0', 'd0': '0', 'd1': '0.0', 'pct0': '0%', 'pct1': '0.0%',
                 'hm': '[h]:mm'}
# barva → (pozadí, písmo) jako v makru
_BARVY = {'zelena': ('C6EFCE', '006100'), 'zluta': ('FFEB9C', '9C5700'),
          'cervena': ('FFC7CE', '9C0006'), 'cervena_tucne': ('FFC7CE', '9C0006'),
          'seda': ('F8F8F8', 'A6A6A6'), 'modra_tucne': ('DDEBF7', '000000')}


def _barvy_top(o):
    """TOP 10: hodnocení barevně, sloupec řazení modře a tučně (jako v makru)."""
    return {'hodnoceni': o['barvy']['hodnoceni'], 'p_kanc': 'modra_tucne'}


def _xlsx_tabulka(ws, radek, sloupce, zaznamy, hodnoty=hodnota, barvy=lambda o: {}):
    from openpyxl.styles import Font, PatternFill
    for c, (_, popisek, _) in enumerate(sloupce, start=1):
        bunka = ws.cell(radek, c, popisek)
        bunka.font = Font(bold=True, color='FFFFFF')
        bunka.fill = PatternFill('solid', fgColor='1F4E78')
    for o in zaznamy:
        radek += 1
        b = barvy(o)
        for c, (klic, _, fmt) in enumerate(sloupce, start=1):
            v = hodnoty(o, klic)
            bunka = ws.cell(radek, c, v / 24 if fmt == 'hm' and v is not None else v)
            if fmt in _XLSX_FORMATY:
                bunka.number_format = _XLSX_FORMATY[fmt]
            if klic in b:
                pozadi, pismo = _BARVY[b[klic]]
                bunka.fill = PatternFill('solid', fgColor=pozadi)
                bunka.font = Font(color=pismo, bold=b[klic].endswith('_tucne'))
    return radek + 2


def export_xlsx(data, radky) -> str:
    """Report, Detail OZ a Data z vybraných řádků → cesta k dočasnému souboru."""
    import openpyxl
    from openpyxl.styles import Font
    v = vypocet(data['hlavicka'], radky)
    rep = report(v)
    obdobi = nazev_obdobi(data['obdobi'])
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = 'Report'
    ws['A1'] = 'PLANTOUR – struktura práce, nevyužívání systému a podíl kanceláře'
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = f'Období: {obdobi}   |   {text_fondu(v)}'
    r = 4
    for nadpis, sloupce, zaznamy, kwargs in (
            ('STRUKTURA PRACOVNÍ DOBY PODLE ASM (podíl z pracovního fondu aktivních OZ)',
             [('nazev', 'ASM', 't')] + [(k, n, 'pct1') for k, n, _ in STRUKTURA],
             rep['struktura'], {'hodnoty': lambda o, k: o[k]}),
            ('OZ, KTEŘÍ PLANTOUR ZA OBDOBÍ NEPOUŽILI (žádná návštěva ani záznam času)',
             NEPOUZIVA, rep['nepouziva'], {}),
            ('TOP 10 – NEJVĚTŠÍ PODÍL KANCELÁŘE A TEL. OBJEDNÁVEK', TOP_KANCELAR,
             rep['top_kancelar'], {'barvy': _barvy_top})):
        ws.cell(r, 1, nadpis).font = Font(bold=True)
        if zaznamy:
            r = _xlsx_tabulka(ws, r + 1, sloupce, zaznamy, **kwargs)
        else:
            ws.cell(r + 1, 1, 'Všichni OZ mají v Plantouru za období alespoň jeden záznam.')
            r += 3
    ws.cell(r, 1, f'Vysvětlivky: {VYSVETLIVKY}')
    ws.column_dimensions['A'].width = 28

    ws = wb.create_sheet('Detail OZ')
    ws['A1'] = f'Detail obchodních zástupců – {obdobi}'
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = popis_detailu(v)
    posledni = _xlsx_tabulka(ws, 3, DETAIL, detail(v), barvy=lambda o: o['barvy']) - 2
    ws.freeze_panes = 'D4'
    ws.auto_filter.ref = f'A3:{openpyxl.utils.get_column_letter(len(DETAIL))}{posledni}'

    ws = wb.create_sheet('Data')
    ws.append(data['hlavicka'])
    for radek in radky:
        ws.append(radek)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions

    cesta = _tmp_cesta(f"Plantour_{data['obdobi']}.xlsx")
    wb.save(cesta)
    return cesta


# ── UI ───────────────────────────────────────────────────────────────────────

_JS_FORMATY = {
    'int': 'v => v == null ? "" : Number(v).toLocaleString("cs-CZ", {maximumFractionDigits: 0})',
    'd0': 'v => v == null ? "" : Number(v).toLocaleString("cs-CZ", {maximumFractionDigits: 0})',
    'd1': ('v => v == null ? "" : Number(v).toLocaleString("cs-CZ", '
           '{minimumFractionDigits: 1, maximumFractionDigits: 1})'),
    'pct0': ('v => v == null ? "" : (v * 100).toLocaleString("cs-CZ", '
             '{maximumFractionDigits: 0}) + " %"'),
    'pct1': ('v => v == null ? "" : (v * 100).toLocaleString("cs-CZ", '
             '{minimumFractionDigits: 1, maximumFractionDigits: 1}) + " %"'),
    'hm': ('v => { if (v == null) return ""; const m = Math.round(v * 60); '
           'return Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0"); }'),
}
_STYLY = {k: f'background:#{p};color:#{f}' + (';font-weight:bold' if k.endswith('_tucne') else '')
          for k, (p, f) in _BARVY.items()}


def _tabulka(sloupce, zaznamy, hodnoty=hodnota, barvy=lambda o: {}, hledat=False, stranka=0):
    """Tabulka jen ke čtení; barvy(o) = {sloupec: barva} se promítne do buněk."""
    radky = []
    for i, o in enumerate(zaznamy):
        b = barvy(o)
        radky.append({'_id': i, '_styl': {k: _STYLY[c] for k, c in b.items()},
                      **{k: hodnoty(o, k) for k, _, _ in sloupce}})
    if hledat:
        pole = ui.input(placeholder='Hledat…').props('dense outlined clearable').classes('w-72 mb-2')
    t = ui.table(columns=[
        {'name': k, 'label': p, 'field': k, 'sortable': True,
         'align': 'left' if f == 't' else 'right',
         # zalamuje se jen hlavička a dlouhé hodnocení — jinak by řádky zbytečně narostly
         'style': 'min-width: 260px' if k == 'hodnoceni' else 'white-space: nowrap',
         **({':format': _JS_FORMATY[f]} if f in _JS_FORMATY else {})}
        for k, p, f in sloupce], rows=radky, row_key='_id', pagination=stranka) \
        .classes('w-full').props('flat bordered dense wrap-cells')
    if not stranka:
        t.props('hide-pagination')
    t.add_slot('body-cell', '<q-td :props="props" :style="props.row._styl[props.col.name]">'
                            '{{ props.value }}</q-td>')
    if hledat:
        pole.bind_value_to(t, 'filter')
    return t


def _graf(struktura) -> dict:
    """Skládaný 100% pruhový graf jako v makru; popisky = skutečný podíl z fondu."""
    serie = []
    for k, nazev, barva in STRUKTURA:
        body = []
        for a in struktura:
            soucet = sum(a[x] for x, _, _ in STRUKTURA) or 1
            body.append({'value': round(a[k] / soucet * 100, 1),
                         'label': {'show': a[k] >= 0.04, 'formatter': f'{_fmt(a[k] * 100)} %'}})
        serie.append({'name': nazev, 'type': 'bar', 'stack': 'doba', 'data': body,
                      'itemStyle': {'color': barva},
                      'label': {'position': 'inside', 'fontSize': 10}})
    return {'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'shadow'}},
            'legend': {'bottom': 0},
            'grid': {'left': 120, 'right': 20, 'top': 10, 'bottom': 50},
            'xAxis': {'type': 'value', 'max': 100, 'axisLabel': {'formatter': '{value} %'}},
            'yAxis': {'type': 'category', 'inverse': True, 'data': [a['nazev'] for a in struktura]},
            'series': serie}


def _nadpis(text):
    ui.label(text).classes('text-base font-bold text-gray-700 mt-6 mb-1')


def _vykresli_report(data, radky):
    v = vypocet(data['hlavicka'], radky)
    rep = report(v)
    ui.label('PLANTOUR – struktura práce, nevyužívání systému a podíl kanceláře') \
        .classes('text-xl font-bold text-gray-800')
    ui.label(f"Období: {nazev_obdobi(data['obdobi'])}   |   {text_fondu(v)}") \
        .classes('text-sm text-gray-500')
    _nadpis('STRUKTURA PRACOVNÍ DOBY PODLE ASM (podíl z pracovního fondu aktivních OZ)')
    ui.echart(_graf(rep['struktura'])).classes('w-full') \
        .style(f"height: {max(300, 80 + 26 * len(rep['struktura']))}px")
    _tabulka([('nazev', 'ASM', 't')] + [(k, n, 'pct1') for k, n, _ in STRUKTURA],
             rep['struktura'], hodnoty=lambda o, k: o[k])
    _nadpis('OZ, KTEŘÍ PLANTOUR ZA OBDOBÍ NEPOUŽILI (žádná návštěva ani záznam času)')
    if rep['nepouziva']:
        _tabulka(NEPOUZIVA, rep['nepouziva'])
    else:
        ui.label('Všichni OZ mají v Plantouru za období alespoň jeden záznam.').classes('text-gray-600')
    _nadpis('TOP 10 – NEJVĚTŠÍ PODÍL KANCELÁŘE A TEL. OBJEDNÁVEK')
    _tabulka(TOP_KANCELAR, rep['top_kancelar'], barvy=_barvy_top)
    ui.label(f'Vysvětlivky: {VYSVETLIVKY}').classes('text-xs text-gray-500 mt-4')


def _vykresli_detail(data, radky):
    v = vypocet(data['hlavicka'], radky)
    ui.label(f"Detail obchodních zástupců – {nazev_obdobi(data['obdobi'])}") \
        .classes('text-xl font-bold text-gray-800')
    ui.label(popis_detailu(v)).classes('text-xs text-gray-500 mb-2')
    _tabulka(DETAIL, detail(v), barvy=lambda o: o['barvy'], hledat=True, stranka=50)


def _vykresli_data(data, radky):
    sloupce = [(f'c{i}', h or f'Sloupec {i + 1}', 't') for i, h in enumerate(data['hlavicka'])]
    _tabulka(sloupce, radky, hodnoty=lambda r, k: (r[int(k[1:])] if int(k[1:]) < len(r) else None),
             hledat=True, stranka=50)


def _sekce_mesic(user_id, user_name, prava, vykresli):
    """Výběr měsíce, export a obsah sekce — jen z řádků, které uživatel smí vidět."""
    obdobi = [r['obdobi'] for r in nahrana_obdobi()]
    if not obdobi:
        ui.label('Zatím nejsou nahraná žádná data.').classes('text-gray-500')
        return
    klic = f'plantour_obdobi_{user_id}'
    stav = {'obdobi': app.storage.user.get(klic) if app.storage.user.get(klic) in obdobi
            else obdobi[0]}

    def zmena(e):
        stav['obdobi'] = app.storage.user[klic] = e.value
        obsah.refresh()

    async def export():
        data = nacti_obdobi(stav['obdobi'])
        radky = videne_radky(data, user_id, prava) if data else []
        if not radky:
            ui.notify('Za vybraný měsíc nejsou žádná data.', type='warning')
            return
        cesta = await asyncio.to_thread(export_xlsx, data, radky)
        intranet_logger.log_activity(user_name, 'ASM Plantour', f"Export {stav['obdobi']}")
        _stahni_soubor(cesta, f"Plantour_{stav['obdobi']}.xlsx")

    with ui.row().classes('items-center gap-3 mb-2'):
        ui.select({o: nazev_obdobi(o) for o in obdobi}, label='Měsíc', value=stav['obdobi'],
                  on_change=zmena).props('outlined dense options-dense').classes('w-48')
        ui.button('Export XLSX', icon='download', on_click=export).props('flat')

    @ui.refreshable
    def obsah():
        data = nacti_obdobi(stav['obdobi'])
        radky = videne_radky(data, user_id, prava) if data else []
        if not radky:
            ui.label('Za vybraný měsíc tu pro vás nejsou žádná data.').classes('text-gray-500')
            return
        vykresli(data, radky)
    obsah()


def _sekce_import(user_name):
    dnes = datetime.date.today()
    rok, mes = dnes.year, dnes.month
    nabidka = {}
    for _ in range(13):
        nabidka[f'{rok:04d}-{mes:02d}'] = nazev_obdobi(f'{rok:04d}-{mes:02d}')
        rok, mes = (rok, mes - 1) if mes > 1 else (rok - 1, 12)
    # export se dělá ke konci měsíce (nebo na začátku dalšího) — odhadneme, za který je
    stav = {'obdobi': list(nabidka)[0 if dnes.day >= 15 else 1]}

    async def zpracuj(e):
        raw, nazev = await intranet_asm._precti_upload(e, up)
        if raw is None:
            return
        try:
            vysledek = await asyncio.to_thread(importuj_soubor, raw, nazev, stav['obdobi'], user_name)
        except ValueError as exc:
            ui.notify(f'{nazev}: {exc}', type='negative', multi_line=True)
            return
        except Exception as exc:
            print(f'[plantour] import {nazev}: {exc!r}')
            ui.notify(f'{nazev}: import selhal, nic se neuložilo.', type='negative')
            return
        intranet_logger.log_activity(user_name, 'ASM Plantour', f'Import {nazev} → {vysledek}')
        ui.notify(f'{nazev} → {vysledek}', type='positive', multi_line=True)
        prehled.refresh()

    ui.label('Vyberte měsíc a nahrajte Plantour report (.xlsm) nebo export z Plantouru (.xlsx). '
             'Uloží se jen list Data, Detail OZ a Report se počítají z něj. Nahrání nahradí data '
             'vybraného měsíce, pokud už existují.').classes('text-sm text-gray-500')
    with ui.row().classes('items-center gap-3'):
        ui.select(nabidka, label='Měsíc', value=stav['obdobi'],
                  on_change=lambda e: stav.update(obdobi=e.value)) \
            .props('outlined dense options-dense').classes('w-48')
        up = ui.upload(on_upload=zpracuj, auto_upload=True, max_file_size=50_000_000,
                       label='Vybrat soubor') \
            .props('accept=.xlsx,.xlsm').classes('w-96')

    @ui.refreshable
    def prehled():
        _nadpis('Nahrané měsíce')
        _tabulka([('obdobi', 'Měsíc', 't'), ('pocet', 'Počet OZ', 'int'), ('soubor', 'Soubor', 't'),
                  ('nahral', 'Nahrál', 't'), ('kdy', 'Kdy', 't')], nahrana_obdobi(),
                 hodnoty=lambda r, k: (nazev_obdobi(r[k]) if k == 'obdobi' else
                                       f"{r[k]:%d.%m.%Y %H:%M}" if k == 'kdy' and r[k] else r[k]))
    prehled()


# (klíč, ikona, název, popis, kdo vidí: None = každý s přístupem do modulu)
_DLAZDICE = [
    ('report', '📊', 'Report', 'Struktura práce po ASM, kdo Plantour nepoužívá, TOP 10 kancelář', None),
    ('detail', '🧑‍💼', 'Detail OZ', 'Všichni OZ s vyhodnocením limitů', None),
    ('data', '📋', 'Data', 'Nahraný export z Plantouru s filtrováním', None),
    ('import', '📥', 'Import', 'Měsíční nahrání exportu z Plantouru', je_analytik),
]


def vykresli_plantour(user_id: int, user_name: str, vsechna_prava: list):
    if not ma_pristup(user_id, vsechna_prava):
        with ui.column().classes('items-center py-24 gap-4'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('K modulu ASM Plantour nemáte přístup.').classes('text-gray-400 text-lg')
        return

    inicializace_db()
    stav_klic = f'plantour_dlazdice_{user_id}'
    sekce = {
        'report': lambda: _sekce_mesic(user_id, user_name, vsechna_prava, _vykresli_report),
        'detail': lambda: _sekce_mesic(user_id, user_name, vsechna_prava, _vykresli_detail),
        'data': lambda: _sekce_mesic(user_id, user_name, vsechna_prava, _vykresli_data),
        'import': lambda: _sekce_import(user_name),
    }
    viditelne = [d for d in _DLAZDICE if d[4] is None or d[4](vsechna_prava)]

    def otevri(klic):
        app.storage.user[stav_klic] = klic
        panel.refresh()

    def zpet():
        app.storage.user.pop(stav_klic, None)
        panel.refresh()

    @ui.refreshable
    def panel():
        vybrana = next((d for d in viditelne if d[0] == app.storage.user.get(stav_klic)), None)
        if vybrana:
            with ui.row().classes('items-center gap-3 mb-4'):
                ui.button(icon='arrow_back', on_click=zpet).props('flat round') \
                    .tooltip('Zpět na dlaždice')
                ui.label(f'ASM Plantour – {vybrana[2]}').classes('text-2xl font-bold text-gray-800')
            sekce[vybrana[0]]()
            return
        ui.label('ASM Plantour').classes('text-3xl font-bold text-gray-800 mb-6')
        with ui.grid(columns=4).classes('w-full gap-4'):
            for klic, ikona, nazev, popis, _ in viditelne:
                with ui.card().classes(
                        'p-6 items-center gap-2 rounded-2xl border border-gray-100 cursor-pointer '
                        'hover:shadow-xl hover:-translate-y-0.5 transition-all duration-200') as karta:
                    ui.label(ikona).classes('text-5xl mb-1')
                    ui.label(nazev).classes('text-lg font-bold text-center text-gray-800')
                    ui.label(popis).classes('text-sm text-center text-gray-500')
                karta.on('click', lambda k=klic: otevri(k))
    panel()


if __name__ == '__main__':   # rychlá kontrola výpočtu (bez DB)
    assert _hodiny('122h 42min') == 122.7 and _hodiny('45min') == 0.75 and _hodiny(0.5) == 12
    assert _hodiny(None) == 0 and _hodiny('') == 0 and _hodiny('2h') == 2
    assert _fmt(2.85, 1) == '2,9' and _fmt(28.499999999999996) == '29' and _fmt(96.4) == '96'
    assert _dny_z_hlavicky(['ASM', 'Fond (21 prac. dnů)']) == 21 and _dny_z_hlavicky(['ASM']) == 0
    assert _razeni_makra([3, 1, 2], lambda a, b: a > b) == [3, 2, 1]
    print('OK: intranet_plantour')
