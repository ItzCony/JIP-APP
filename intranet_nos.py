# -*- coding: utf-8 -*-
"""Modul NOS — podklady pro odměny obchodních zástupců (OZ).

Zadání: „Podklad účtárna OZ_2026" (list „odměna"). Hotová kostra:
  • Informace k OZ   – smluvní odměna, její termín, náklady 1 (auto)   [sl. F, G, N]
  • Odměny a malusy  – měsíčně srážka ASM, odměna ASM, odměna akce     [sl. I, J, K]
  • Náklady 2        – měsíční položky ostatních nákladů, sčítají se   [sl. O]
  • Přístupy         – správce přiřazuje uživatelům jejich ASM
  • Data             – import dat a podmínek od účtárny, výpočet provize po řádcích
  • Podrobný podklad – výpočet odměny po OZ podle vzorců ze zadání      [sl. E–P]
Zbývá: Podklady NOS (PDF), Přehled dokladů NOS.

Číslo, jméno, ASM a status OZ se nikam nekopírují — čtou se z asm_oz (Formuláře ASM).

Práva:
  nos_vkladatel, nos_ctenar – vidí všechny OZ, needitují
  nos_spravce (a „vse")     – vidí i zadává vše, spravuje přístupy
  nos_asm, nos_vedouci      – vidí a zadávají jen OZ svých ASM (tabulka nos_pristup;
                              ASM má jedno, vedoucí pobočky jich může mít víc)
Každá změna hodnoty jde do nos_log (ikona oka u řádku).
"""
import asyncio
import bisect
import datetime
import io
import json
import re
from decimal import Decimal, InvalidOperation

from nicegui import ui, app

import intranet_asm
import intranet_data
import intranet_logger
import intranet_lupa

PRAVA_ZADAVATEL = ('nos_asm', 'nos_vedouci')
PRAVA_MODULU = ('nos_vkladatel', 'nos_ctenar', 'nos_spravce') + PRAVA_ZADAVATEL

# (sloupec v DB, popisek, typ vstupu)
POLE_INFO = [
    ('smluvni_odmena', 'Smluvní odměna', 'kc'),
    ('smluvni_do', 'Termín smluvní odměny (do)', 'mesic'),
    ('naklady_auto', 'Náklady 1 (auto)', 'kc'),
]
POLE_MESIC = [
    ('srazka_asm', 'Srážka ASM', 'kc'),
    ('odmena_asm', 'Odměna ASM', 'kc'),
    ('odmena_akce', 'Odměna akce (tabák)', 'kc'),
]
# Srážky a náklady se ve výpočtu přičítají se záporným znaménkem (v zadání −2000,
# −10500). Ukládají se vždy záporně, ať uživatel napíše 2000 nebo −2000.
ZAPORNE = {'srazka_asm', 'naklady_auto'}

_OBDOBI_RE = re.compile(r'\d{4}-(0[1-9]|1[0-2])')
_FORMAT_KC = ('v => v == null ? "" : Number(v).toLocaleString("cs-CZ", '
              '{minimumFractionDigits: 2, maximumFractionDigits: 2})')

_TABULKY = (
    """CREATE TABLE IF NOT EXISTS nos_oz_info (
        cislo_oz VARCHAR(20) PRIMARY KEY,
        smluvni_odmena DECIMAL(12,2) DEFAULT NULL,
        smluvni_do CHAR(7) DEFAULT NULL,
        naklady_auto DECIMAL(12,2) DEFAULT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS nos_mesicni (
        obdobi CHAR(7) NOT NULL,
        cislo_oz VARCHAR(20) NOT NULL,
        srazka_asm DECIMAL(12,2) DEFAULT NULL,
        odmena_asm DECIMAL(12,2) DEFAULT NULL,
        odmena_akce DECIMAL(12,2) DEFAULT NULL,
        PRIMARY KEY (obdobi, cislo_oz)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS nos_naklady2 (
        id INT AUTO_INCREMENT PRIMARY KEY,
        obdobi CHAR(7) NOT NULL,
        cislo_oz VARCHAR(20) NOT NULL,
        castka DECIMAL(12,2) NOT NULL,
        popis VARCHAR(500) NOT NULL,
        zadal VARCHAR(255),
        kdy DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_nos_n2 (obdobi, cislo_oz)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS nos_pristup (
        user_id INT NOT NULL,
        asm VARCHAR(255) NOT NULL,
        PRIMARY KEY (user_id, asm)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS nos_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        cislo_oz VARCHAR(20) NOT NULL,
        obdobi CHAR(7) DEFAULT NULL,
        kdo VARCHAR(255),
        pole VARCHAR(100),
        stara_hodnota VARCHAR(600),
        nova_hodnota VARCHAR(600),
        kdy DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_nos_log (cislo_oz, id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    # Řádky ze souboru data.xlsx tak, jak přišly. Provize se neukládá — počítá se
    # při čtení z aktuálních podmínek, takže nová tabulka podmínek nic nerozbije.
    """CREATE TABLE IF NOT EXISTS nos_data (
        id INT AUTO_INCREMENT PRIMARY KEY,
        obdobi CHAR(7) NOT NULL,
        kategorie VARCHAR(40) NOT NULL,
        cislo_oz VARCHAR(20) NOT NULL,
        site VARCHAR(10),
        skupina SMALLINT NOT NULL,
        skupina_nazev VARCHAR(100),
        obrat DOUBLE NOT NULL DEFAULT 0,
        nzisk DOUBLE NOT NULL DEFAULT 0,
        obrat_site DOUBLE NOT NULL DEFAULT 0,
        obrat_cp2 DOUBLE NOT NULL DEFAULT 0,
        zisk_cp2 DOUBLE NOT NULL DEFAULT 0,
        kg DOUBLE NOT NULL DEFAULT 0,
        asm VARCHAR(255),
        jmeno VARCHAR(255),
        INDEX idx_nos_data (obdobi, cislo_oz)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS nos_podminky (
        klic VARCHAR(20) PRIMARY KEY,
        data LONGTEXT NOT NULL,
        soubor VARCHAR(255),
        nahral VARCHAR(255),
        kdy DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
)

_DB_INIT_HOTOVO = False


def inicializace_db():
    global _DB_INIT_HOTOVO
    if _DB_INIT_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        for sql in _TABULKY:
            cur.execute(sql)
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

def je_spravce(prava) -> bool:
    return 'vse' in prava or 'nos_spravce' in prava


def je_analytik(prava) -> bool:
    """Dlaždice Data, Podrobný podklad a Přehled dokladů (role 1 a 3 ze zadání)."""
    return je_spravce(prava) or 'nos_vkladatel' in prava


def vidi_vse(prava) -> bool:
    return je_analytik(prava) or 'nos_ctenar' in prava


def ma_pristup(prava) -> bool:
    return 'vse' in prava or any(p in prava for p in PRAVA_MODULU)


def moje_asm(user_id) -> set:
    return {r['asm'] for r in _dotaz("SELECT asm FROM nos_pristup WHERE user_id=%s", (user_id,))}


def nacti_oz(user_id, prava) -> list:
    """OZ, které uživatel vidí. Každý řádek nese 'smi' = smí k němu zadávat."""
    # ponytail: jen aktivní OZ — ukončený OZ zmizí i ze starých období;
    # až bude uzávěrka období, brát OZ ze snímku měsíce.
    radky = _dotaz("SELECT cislo_oz, jmeno_oz, asm, osvc FROM asm_oz "
                   "WHERE aktivni=1 AND cislo_oz<>'' ORDER BY asm, cislo_oz")
    spravce = je_spravce(prava)
    moje = moje_asm(user_id) if any(p in prava for p in PRAVA_ZADAVATEL) else set()
    vse = vidi_vse(prava)
    vysledek = []
    for r in radky:
        smi = spravce or (bool(r['asm']) and r['asm'] in moje)
        if vse or smi:
            osvc = r.pop('osvc')
            r['status'] = '' if osvc is None else ('OSVČ' if osvc else 'ZAM')
            r['smi'] = smi
            vysledek.append(r)
    return vysledek


def _smi_oz(cislo_oz, user_id, prava) -> bool:
    return any(r['cislo_oz'] == cislo_oz and r['smi'] for r in nacti_oz(user_id, prava))


# ── Zápis ────────────────────────────────────────────────────────────────────

def _kc(hodnota, zaporne=False):
    """Vstup → Decimal na haléře, prázdno → None. Vyhazuje InvalidOperation."""
    if hodnota is None or hodnota == '':
        return None
    d = Decimal(str(hodnota)).quantize(Decimal('0.01'))
    return -abs(d) if zaporne else d


def _je_obdobi(text) -> bool:
    return bool(text) and bool(_OBDOBI_RE.fullmatch(text))


def _log(cur, cislo_oz, obdobi, kdo, pole, stara, nova):
    cur.execute("INSERT INTO nos_log (cislo_oz, obdobi, kdo, pole, stara_hodnota, nova_hodnota) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (cislo_oz, obdobi, kdo, pole,
                 None if stara is None else str(stara), None if nova is None else str(nova)))


def uloz(pole, cislo_oz, obdobi, hodnoty: dict, user_id, user_name, prava) -> bool:
    """Uloží hodnoty jednoho OZ: POLE_INFO (obdobi=None) nebo POLE_MESIC (za období).
    Každá změněná hodnota jde do logu. Vrací False při chybě nebo bez oprávnění."""
    if obdobi is not None and not _je_obdobi(obdobi):
        return False
    if not _smi_oz(cislo_oz, user_id, prava):
        return False
    try:
        nove = {}
        for k, _, typ in pole:
            v = hodnoty.get(k)
            if typ == 'mesic':
                v = v or None
                if v and not _je_obdobi(v):
                    return False
            else:
                v = _kc(v, k in ZAPORNE)
            nove[k] = v
    except (InvalidOperation, ValueError):
        return False
    if (nove.get('smluvni_odmena') or 0) < 0:
        return False

    # Názvy tabulek a sloupců jsou konstanty z tohoto souboru, nikdy vstup uživatele.
    tabulka = 'nos_oz_info' if obdobi is None else 'nos_mesicni'
    klic = {'cislo_oz': cislo_oz} if obdobi is None else {'obdobi': obdobi, 'cislo_oz': cislo_oz}
    popisky = {k: p for k, p, _ in pole}
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT {', '.join(nove)} FROM {tabulka} "
                    f"WHERE {' AND '.join(f'{k}=%s' for k in klic)} FOR UPDATE",
                    tuple(klic.values()))
        stare = cur.fetchone() or {}
        # 0 a prázdno jsou totéž („není stanoveno") — do logu jako změna nejdou.
        zmeny = {k: v for k, v in nove.items() if (stare.get(k) or None) != (v or None)}
        if zmeny:
            radek = {**klic, **zmeny}
            cur.execute(
                f"INSERT INTO {tabulka} ({', '.join(radek)}) "
                f"VALUES ({', '.join(['%s'] * len(radek))}) "
                f"ON DUPLICATE KEY UPDATE {', '.join(f'{k}=VALUES({k})' for k in zmeny)}",
                tuple(radek.values()))
            for k, v in zmeny.items():
                _log(cur, cislo_oz, obdobi, user_name, popisky[k], stare.get(k), v)
            conn.commit()
        return True
    except Exception as e:
        print(f'[nos] uloz {tabulka} {cislo_oz}: {e}')
        return False
    finally:
        conn.close()


def pridej_naklad(cislo_oz, obdobi, castka, popis, user_id, user_name, prava) -> bool:
    popis = (popis or '').strip()[:500]
    try:
        castka = _kc(castka, zaporne=True)
    except (InvalidOperation, ValueError):
        return False
    if not castka or not popis or not _je_obdobi(obdobi) or not _smi_oz(cislo_oz, user_id, prava):
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO nos_naklady2 (obdobi, cislo_oz, castka, popis, zadal) "
                    "VALUES (%s,%s,%s,%s,%s)", (obdobi, cislo_oz, castka, popis, user_name))
        _log(cur, cislo_oz, obdobi, user_name, 'Náklady 2 – přidáno', None, f'{castka} · {popis}')
        conn.commit()
        return True
    except Exception as e:
        print(f'[nos] pridej_naklad {cislo_oz}: {e}')
        return False
    finally:
        conn.close()


def smaz_naklad(naklad_id, user_id, user_name, prava) -> bool:
    radky = _dotaz("SELECT * FROM nos_naklady2 WHERE id=%s", (naklad_id,))
    if not radky or not _smi_oz(radky[0]['cislo_oz'], user_id, prava):
        return False
    n = radky[0]
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM nos_naklady2 WHERE id=%s", (naklad_id,))
        _log(cur, n['cislo_oz'], n['obdobi'], user_name, 'Náklady 2 – smazáno',
             f"{n['castka']} · {n['popis']}", None)
        conn.commit()
        return True
    except Exception as e:
        print(f'[nos] smaz_naklad {naklad_id}: {e}')
        return False
    finally:
        conn.close()


def nastav_pristup(cil_user_id, asm_seznam, user_name, prava) -> bool:
    """Přepíše seznam ASM jednoho uživatele. Jen správce."""
    if not je_spravce(prava):
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM nos_pristup WHERE user_id=%s", (cil_user_id,))
        cur.executemany("INSERT INTO nos_pristup (user_id, asm) VALUES (%s,%s)",
                        [(cil_user_id, a) for a in sorted(set(asm_seznam))])
        conn.commit()
    except Exception as e:
        print(f'[nos] nastav_pristup {cil_user_id}: {e}')
        return False
    finally:
        conn.close()
    intranet_logger.log_activity(
        user_name, 'NOS',
        f'Přístupy: uživatel #{cil_user_id} → {", ".join(sorted(set(asm_seznam))) or "nic"}')
    return True


# ── Čtení ────────────────────────────────────────────────────────────────────

def nacti_info() -> dict:
    return {r['cislo_oz']: r for r in _dotaz("SELECT * FROM nos_oz_info")}


def nacti_mesic(obdobi) -> dict:
    return {r['cislo_oz']: r
            for r in _dotaz("SELECT * FROM nos_mesicni WHERE obdobi=%s", (obdobi,))}


def nacti_naklady(obdobi, cislo_oz=None) -> list:
    if cislo_oz is None:
        return _dotaz("SELECT * FROM nos_naklady2 WHERE obdobi=%s ORDER BY id", (obdobi,))
    return _dotaz("SELECT * FROM nos_naklady2 WHERE obdobi=%s AND cislo_oz=%s ORDER BY id",
                  (obdobi, cislo_oz))


def nacti_log(cislo_oz) -> list:
    return _dotaz("SELECT * FROM nos_log WHERE cislo_oz=%s ORDER BY id DESC LIMIT 300",
                  (cislo_oz,))

# ── Data a provize ───────────────────────────────────────────────────────────
# Výklad podmínek od účtárny (ověřit na skutečně vyplacených číslech):
#   provize řádku = obrat × pevná sazba(kategorie, skupina) + schodová provize skupiny
#   schody „CP2 kč/kg"        : práh = zisk nad CP2 / kg,        provize = sazba × zisk nad CP2
#   schody „Zisk nad CP2 v %" : práh = zisk nad CP2 / obrat,     provize = sazba × obrat
#   platí nejvyšší práh ≤ hodnota; pod prvním prahem (záporný zisk) je sazba 0.
# Shodně s původním ModelOZ.py: schody mají jen tyto kategorie, ostatní jen pevnou sazbu.
SCHODY_KATEGORIE = {'Tabák', 'Bez sítí'}

# hlavička v data.xlsx → sloupec v nos_data
_DATA_SLOUPCE = {
    'Kategorie': 'kategorie', 'Měsíc': 'obdobi', 'Dealer 3': 'cislo_oz', 'sítě': 'site',
    'Nadřazená skupina Z': 'skupina_nazev', 'Obrat v Kč bez DPH': 'obrat',
    'Nzisk v KČ bez DPH': 'nzisk', 'Obrat v Kč bez DPH sítě': 'obrat_site',
    'Obrat CP2': 'obrat_cp2', 'Zisk nad CP2': 'zisk_cp2', 'Obrat v Kg': 'kg',
    'ASM': 'asm', 'Dealer Jméno': 'jmeno',
}
_DATA_CISLA = ('obrat', 'nzisk', 'obrat_site', 'obrat_cp2', 'zisk_cp2', 'kg')
_DATA_DB = ('obdobi', 'kategorie', 'cislo_oz', 'site', 'skupina', 'skupina_nazev',
            *_DATA_CISLA, 'asm', 'jmeno')
_SKUPINA_V_KOMENTARI = re.compile(r'(\d{1,2})-\s*[^\W\d]')


def _je_cislo(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parsuj_kategorie(ws) -> dict:
    """List „Kategorie": {kategorie: {'sazba', 'skupiny': {'15': sazba}, 'ostatni'}}.
    Kategorie stojí na 2. řádku ob tři sloupce (název, kód, sazba/komentář); pod ní
    řádky kombinací (kód, sazba, komentář se seznamem skupin „15-dutinky; 20-…")."""
    vysledek = {}
    for c in range(2, ws.max_column + 1, 3):
        nazev = ws.cell(2, c).value
        if not isinstance(nazev, str) or not nazev.strip():
            continue
        sazba = ws.cell(2, c + 2).value
        kat = {'sazba': sazba if _je_cislo(sazba) else None, 'skupiny': {}, 'ostatni': None}
        for r in range(3, ws.max_row + 1):
            s = ws.cell(r, c + 1).value
            if not _je_cislo(s):
                continue
            skupiny = _SKUPINA_V_KOMENTARI.findall(str(ws.cell(r, c + 2).value or ''))
            if skupiny:
                kat['skupiny'].update({str(int(g)): s for g in skupiny})
            else:
                kat['ostatni'] = s          # „vše ostatní"
        vysledek[nazev.strip()] = kat
    if not vysledek:
        raise ValueError('na listu Kategorie není žádná kategorie (čekám je na 2. řádku)')
    return vysledek


def parsuj_skupiny(ws) -> dict:
    """List „Skupiny": {'1': {'typ': 'kg'|'pct'|'nic', 'stupne': [[práh, sazba], …]}}.
    Dvojice sloupců: 1. řádek číslo skupiny, 2. řádek typ, od 3. řádku schody."""
    vysledek = {}
    for c in range(1, ws.max_column + 1, 2):
        cislo = ws.cell(1, c).value
        if not _je_cislo(cislo):
            continue
        hlavicka = str(ws.cell(2, c).value or '').lower()
        typ = 'kg' if 'kg' in hlavicka else 'pct' if '%' in hlavicka else 'nic'
        stupne = []
        for r in range(3, ws.max_row + 1):
            prah, sazba = ws.cell(r, c).value, ws.cell(r, c + 1).value
            if _je_cislo(prah) and _je_cislo(sazba):
                stupne.append([prah, sazba])
        vysledek[str(int(cislo))] = {'typ': typ, 'stupne': sorted(stupne)}
    if not vysledek:
        raise ValueError('na listu Skupiny nejsou žádné skupiny (čekám čísla na 1. řádku)')
    return vysledek


def _stupen(stupne, hodnota) -> float:
    i = bisect.bisect_right(stupne, [hodnota, float('inf')])
    return stupne[i - 1][1] if i else 0.0


def provize_radku(r, podminky) -> float:
    """Provize jednoho řádku dat v Kč (nezaokrouhleno). Výklad viz komentář výše."""
    kat = podminky['kategorie'].get(r['kategorie'])
    if kat is None:
        return 0.0
    g = str(r['skupina'])
    sazba = kat['skupiny'].get(g)
    if sazba is None:
        sazba = kat['ostatni'] if kat['ostatni'] is not None else (kat['sazba'] or 0)
    # kategorie Sítě má obrat ve sloupci „Obrat v Kč bez DPH sítě", ostatní v hlavním
    obrat = r['obrat'] + r.get('obrat_site', 0)
    provize = obrat * sazba
    if r['kategorie'] not in SCHODY_KATEGORIE:
        return provize
    schody = podminky['skupiny'].get(g)
    if schody and schody['typ'] == 'kg' and r['kg']:
        provize += r['zisk_cp2'] * _stupen(schody['stupne'], r['zisk_cp2'] / r['kg'])
    elif schody and schody['typ'] == 'pct' and obrat:
        provize += obrat * _stupen(schody['stupne'], r['zisk_cp2'] / obrat)
    return provize


def parsuj_data(ws) -> list:
    """List „DATA" → řádky pro nos_data. Sloupce se hledají podle názvu hlavičky."""
    it = ws.iter_rows(values_only=True)
    hlavicka = [str(h or '').strip() for h in next(it, ())]
    chybi = [h for h in _DATA_SLOUPCE if h not in hlavicka]
    if chybi:
        raise ValueError('v listu DATA chybí sloupce: ' + ', '.join(chybi))
    idx = {db: hlavicka.index(h) for h, db in _DATA_SLOUPCE.items()}
    radky = []
    for n, radek in enumerate(it, start=2):
        if not any(v is not None for v in radek):
            continue
        r = {db: radek[i] if i < len(radek) else None for db, i in idx.items()}
        m = re.fullmatch(r'(\d{4})M(\d{1,2})', str(r['obdobi'] or '').strip())
        obdobi = f'{m.group(1)}-{int(m.group(2)):02d}' if m else ''
        g = re.match(r'\s*(\d+)', str(r['skupina_nazev'] or ''))
        if not _je_obdobi(obdobi) or not r['cislo_oz'] or not r['kategorie']:
            raise ValueError(f'řádek {n}: chybí kategorie, dealer nebo měsíc ve tvaru 2026M06')
        r['obdobi'] = obdobi
        r['skupina'] = int(g.group(1)) if g else 0     # bez skupiny = skupina 0 (dle podmínek)
        r['cislo_oz'] = str(r['cislo_oz']).strip()
        r['kategorie'] = str(r['kategorie']).strip()
        for k in _DATA_CISLA:
            r[k] = float(r[k]) if _je_cislo(r[k]) else 0.0
        radky.append(r)
    if not radky:
        raise ValueError('list DATA je prázdný')
    return radky


def importuj_soubor(raw, nazev, user_name) -> str:
    """Pozná soubor podle názvu listu (DATA / Kategorie / Skupiny) a uloží ho.
    Vrací text pro uživatele, při chybě vyhazuje ValueError. Blokuje — volat ve vlákně."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=False)
    except Exception as e:
        raise ValueError(f'soubor nejde otevřít jako XLSX ({e})')
    listy = {ws.title.strip().lower(): ws for ws in wb.worksheets}
    conn = intranet_data.get_db_connection()
    if not conn:
        raise ValueError('databáze není dostupná')
    try:
        cur = conn.cursor()
        for klic, parser in (('kategorie', parsuj_kategorie), ('skupiny', parsuj_skupiny)):
            if klic in listy:
                data = parser(listy[klic])
                cur.execute("REPLACE INTO nos_podminky (klic, data, soubor, nahral) "
                            "VALUES (%s,%s,%s,%s)",
                            (klic, json.dumps(data, ensure_ascii=False), nazev, user_name))
                conn.commit()
                return f'podmínky – {klic}: {len(data)} položek'
        if 'data' not in listy:
            raise ValueError('neznámý soubor – čekám list DATA, Kategorie nebo Skupiny')
        radky = parsuj_data(listy['data'])
        obdobi = sorted({r['obdobi'] for r in radky})
        # Soubor je pro své měsíce úplný — nahrazuje je celé, ostatní měsíce nechává.
        cur.execute(f"DELETE FROM nos_data WHERE obdobi IN ({','.join(['%s'] * len(obdobi))})",
                    obdobi)
        sql = (f"INSERT INTO nos_data ({', '.join(_DATA_DB)}) "
               f"VALUES ({','.join(['%s'] * len(_DATA_DB))})")
        for i in range(0, len(radky), 2000):
            cur.executemany(sql, [tuple(r[k] for k in _DATA_DB) for r in radky[i:i + 2000]])
        conn.commit()
        return f'data: {len(radky)} řádků, období {obdobi[0]} až {obdobi[-1]}'
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def nacti_podminky() -> dict:
    """{'kategorie': …, 'skupiny': …, 'info': {klic: řádek}}; chybějící část je {}."""
    p = {'kategorie': {}, 'skupiny': {}, 'info': {}}
    for r in _dotaz("SELECT klic, data, soubor, nahral, kdy FROM nos_podminky"):
        p[r['klic']] = json.loads(r.pop('data'))
        p['info'][r['klic']] = r
    return p


def nacti_data(obdobi, podminky=None) -> list:
    """Řádky dat za období, každý doplněný o 'provize' (float, nezaokrouhleno)."""
    podminky = podminky or nacti_podminky()
    radky = _dotaz("SELECT * FROM nos_data WHERE obdobi=%s ORDER BY cislo_oz, kategorie, skupina",
                   (obdobi,))
    for r in radky:
        r['provize'] = provize_radku(r, podminky)
    return radky


def provize_oz(obdobi) -> dict:
    """{cislo_oz: {'jmeno', 'asm', 'obrat', 'zisk_cp2', 'provize'}} za období."""
    soucty = {}
    for r in nacti_data(obdobi):
        s = soucty.setdefault(r['cislo_oz'], {'cislo_oz': r['cislo_oz'], 'jmeno': r['jmeno'],
                                              'asm': r['asm'], 'obrat': 0.0, 'zisk_cp2': 0.0,
                                              'provize': 0.0})
        s['obrat'] += r['obrat'] + r['obrat_site']
        s['zisk_cp2'] += r['zisk_cp2']
        s['provize'] += r['provize']
    return soucty


# sloupce Podrobného podkladu = sloupce E–P zadání
POLE_PODKLAD = [
    ('provize', 'Odměna (provize)', 'kc'),
    ('smluvni', 'Smluvní odměna', 'kc'),
    ('smluvni_do', 'Termín', 'mesic'),
    ('dorovnani', 'Dorovnání', 'kc'),
    ('srazka_asm', 'Srážka ASM', 'kc'),
    ('odmena_asm', 'Odměna ASM', 'kc'),
    ('odmena_akce', 'Odměna akce', 'kc'),
    ('mezisoucet', 'Mezisoučet', 'kc'),
    ('odvod', 'Odvod 25 %', 'kc'),
    ('naklady1', 'Náklady 1', 'kc'),
    ('naklady2', 'Náklady 2', 'kc'),
    ('final', 'Final FA, HM', 'kc'),
]
_NULA = Decimal('0.00')


def vypocet_oz(status, provize, smluvni, smluvni_do, obdobi, srazka, odmena, akce, n1, n2) -> dict:
    """Vzorce ze zadání (ř. 6): H=max(F−E,0), L=E+H+I+J+K, M=−25 % L mimo OSVČ, P=L+M+N+O.
    Smluvní odměna platí jen do svého termínu (včetně); bez termínu platí stále."""
    d = lambda v: _NULA if v is None else Decimal(str(v)).quantize(Decimal('0.01'))
    provize = d(provize)
    smluvni = d(smluvni) if not smluvni_do or obdobi <= smluvni_do else _NULA
    dorovnani = max(smluvni - provize, _NULA)
    mezisoucet = provize + dorovnani + d(srazka) + d(odmena) + d(akce)
    odvod = _NULA if status == 'OSVČ' else -(mezisoucet * Decimal('0.25')).quantize(Decimal('0.01'))
    return {'provize': provize, 'smluvni': smluvni, 'smluvni_do': smluvni_do or '',
            'dorovnani': dorovnani, 'srazka_asm': d(srazka), 'odmena_asm': d(odmena),
            'odmena_akce': d(akce), 'mezisoucet': mezisoucet, 'odvod': odvod,
            'naklady1': d(n1), 'naklady2': d(n2),
            'final': mezisoucet + odvod + d(n1) + d(n2)}


def podrobny_podklad(obdobi, user_id, prava) -> list:
    """Řádek na OZ (jen ty, které uživatel vidí) se všemi sloupci zadání."""
    provize, info, mesic = provize_oz(obdobi), nacti_info(), nacti_mesic(obdobi)
    n2 = {}
    for n in nacti_naklady(obdobi):
        n2[n['cislo_oz']] = n2.get(n['cislo_oz'], _NULA) + n['castka']
    radky = []
    for oz in nacti_oz(user_id, prava):
        c = oz['cislo_oz']
        i, m = info.get(c, {}), mesic.get(c, {})
        radky.append({**oz, **vypocet_oz(
            oz['status'], provize.get(c, {}).get('provize'), i.get('smluvni_odmena'),
            i.get('smluvni_do'), obdobi, m.get('srazka_asm'), m.get('odmena_asm'),
            m.get('odmena_akce'), i.get('naklady_auto'), n2.get(c))})
    return radky


def _export_xlsx(jmeno, hlavicka, radky):
    """Zapíše XLSX do dočasného souboru a pošle ho klientovi."""
    import openpyxl
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet('NOS')
    ws.append(hlavicka)
    for r in radky:
        ws.append([float(v) if isinstance(v, Decimal) else v for v in r])
    cesta = intranet_lupa._tmp_cesta(jmeno)
    wb.save(cesta)
    intranet_lupa._stahni_soubor(cesta, jmeno)


# ── UI ───────────────────────────────────────────────────────────────────────

def _f(hodnota):
    return None if hodnota is None else float(hodnota)


def _kc_text(hodnota) -> str:
    return '' if hodnota is None else f'{hodnota:,.2f}'.replace(',', ' ').replace('.', ',')


def _dialog_log(cislo_oz, jmeno):
    with ui.dialog() as d, ui.card().classes('w-[48rem] max-w-full'):
        ui.label('Historie změn').classes('text-lg font-bold')
        ui.label(f'{cislo_oz} · {jmeno}').classes('text-gray-500 -mt-2')
        zaznamy = nacti_log(cislo_oz)
        if not zaznamy:
            ui.label('Zatím žádná změna.').classes('text-gray-400 py-6')
        else:
            ui.table(
                columns=[{'name': k, 'label': p, 'field': k, 'align': 'left'} for k, p in (
                    ('kdy', 'Kdy'), ('kdo', 'Kdo'), ('obdobi', 'Období'), ('pole', 'Pole'),
                    ('stara', 'Původní'), ('nova', 'Nová'))],
                rows=[{'id': z['id'], 'kdy': z['kdy'].strftime('%d.%m.%Y %H:%M'),
                       'kdo': z['kdo'], 'obdobi': z['obdobi'] or '', 'pole': z['pole'],
                       'stara': z['stara_hodnota'] or '—', 'nova': z['nova_hodnota'] or '—'}
                      for z in zaznamy],
                row_key='id', pagination=15).classes('w-full').props('dense flat')
        with ui.row().classes('w-full justify-end'):
            ui.button('Zavřít', on_click=d.close).props('flat')
    d.open()


def _tabulka(pole, radky, on_akce, ikona='edit', akce_vzdy=False):
    """Tabulka OZ: ASM, číslo, jméno, status + sloupce `pole`. U řádku akce
    (jen kde uživatel smí zadávat, pokud není akce_vzdy) a oko s historií změn."""
    sloupce = [{'name': k, 'label': p, 'field': k, 'align': 'left', 'sortable': True}
               for k, p in (('asm', 'ASM'), ('cislo_oz', 'Číslo OZ'),
                            ('jmeno_oz', 'Jméno OZ'), ('status', 'Status'))]
    for k, p, typ in pole:
        s = {'name': k, 'label': p, 'field': k, 'sortable': True,
             'align': 'right' if typ == 'kc' else 'left'}
        if typ == 'kc':
            s[':format'] = _FORMAT_KC
        sloupce.append(s)
    sloupce.append({'name': 'akce', 'label': '', 'field': 'akce', 'align': 'right'})

    hledat = ui.input(placeholder='Hledat OZ, ASM…').props('dense outlined clearable') \
        .classes('w-72 mb-2')
    t = ui.table(columns=sloupce, rows=radky, row_key='cislo_oz', pagination=50) \
        .classes('w-full').props('flat bordered')
    hledat.bind_value_to(t, 'filter')
    podminka = '' if akce_vzdy else 'v-if="props.row.smi"'
    t.add_slot('body-cell-akce', f'''
        <q-td :props="props" auto-width>
            <q-btn {podminka} flat round dense size="sm" icon="{ikona}"
                   @click="$parent.$emit('akce', props.row)" />
            <q-btn flat round dense size="sm" icon="visibility"
                   @click="$parent.$emit('log', props.row)"><q-tooltip>Historie změn</q-tooltip></q-btn>
        </q-td>''')
    t.on('akce', lambda e: on_akce(e.args))
    t.on('log', lambda e: _dialog_log(e.args['cislo_oz'], e.args['jmeno_oz']))
    if not radky:
        ui.label('Nemáte přiřazené žádné OZ. O přiřazení ASM požádejte správce modulu.') \
            .classes('text-gray-400 py-6')


def _dialog_uprav(titulek, radek, pole, uloz_fn, po_ulozeni):
    with ui.dialog() as d, ui.card().classes('w-96'):
        ui.label(titulek).classes('text-lg font-bold')
        ui.label(f"{radek['cislo_oz']} · {radek['jmeno_oz']}").classes('text-gray-500 -mt-2')
        vstupy = {}
        for k, popis, typ in pole:
            if typ == 'mesic':
                vstupy[k] = ui.input(popis, value=radek.get(k) or '') \
                    .props('type=month stack-label clearable').classes('w-full')
            else:
                vstupy[k] = ui.number(popis, value=radek.get(k), format='%.2f', suffix='Kč') \
                    .classes('w-full')
                if k in ZAPORNE:
                    vstupy[k].props('hint="Ukládá se vždy jako záporná částka"')

        def potvrd():
            if uloz_fn({k: v.value for k, v in vstupy.items()}):
                ui.notify('Uloženo', type='positive')
                d.close()
                po_ulozeni()
            else:
                ui.notify('Uložení se nepodařilo — zkontrolujte hodnoty.', type='negative')

        with ui.row().classes('w-full justify-end'):
            ui.button('Zrušit', on_click=d.close).props('flat')
            ui.button('Uložit', on_click=potvrd)
    d.open()


def _vyber_obdobi(stav, po_zmene):
    def zmena(e):
        if _je_obdobi(e.value):
            stav['obdobi'] = e.value
            po_zmene()
    ui.input('Období', value=stav['obdobi'], on_change=zmena) \
        .props('type=month stack-label outlined dense').classes('w-48 mb-2')


def _sekce_info(user_id, user_name, prava):
    @ui.refreshable
    def obsah():
        info = nacti_info()
        radky = []
        for oz in nacti_oz(user_id, prava):
            i = info.get(oz['cislo_oz'], {})
            radky.append({**oz, 'smluvni_odmena': _f(i.get('smluvni_odmena')),
                          'smluvni_do': i.get('smluvni_do') or '',
                          'naklady_auto': _f(i.get('naklady_auto'))})
        _tabulka(POLE_INFO, radky, lambda r: _dialog_uprav(
            'Informace k OZ', r, POLE_INFO,
            lambda h: uloz(POLE_INFO, r['cislo_oz'], None, h, user_id, user_name, prava),
            obsah.refresh))
    obsah()


def _sekce_odmeny(user_id, user_name, prava):
    stav = {'obdobi': datetime.date.today().strftime('%Y-%m')}

    @ui.refreshable
    def obsah():
        obdobi = stav['obdobi']
        mesic = nacti_mesic(obdobi)
        radky = [{**oz, **{k: _f(mesic.get(oz['cislo_oz'], {}).get(k)) for k, _, _ in POLE_MESIC}}
                 for oz in nacti_oz(user_id, prava)]
        _tabulka(POLE_MESIC, radky, lambda r: _dialog_uprav(
            f'Odměny a malusy {obdobi}', r, POLE_MESIC,
            lambda h: uloz(POLE_MESIC, r['cislo_oz'], obdobi, h, user_id, user_name, prava),
            obsah.refresh))

    _vyber_obdobi(stav, lambda: obsah.refresh())
    obsah()


def _dialog_naklady(radek, obdobi, user_id, user_name, prava, po_zavreni):
    with ui.dialog() as d, ui.card().classes('w-[40rem] max-w-full'):
        ui.label(f'Náklady 2 · {obdobi}').classes('text-lg font-bold')
        ui.label(f"{radek['cislo_oz']} · {radek['jmeno_oz']}").classes('text-gray-500 -mt-2')

        def smaz(naklad_id):
            if smaz_naklad(naklad_id, user_id, user_name, prava):
                polozky.refresh()
            else:
                ui.notify('Smazání se nepodařilo.', type='negative')

        @ui.refreshable
        def polozky():
            seznam = nacti_naklady(obdobi, radek['cislo_oz'])
            if not seznam:
                ui.label('Zatím žádná položka.').classes('text-gray-400 py-2')
            for n in seznam:
                with ui.row().classes('w-full items-center gap-3 no-wrap'):
                    ui.label(n['popis']).classes('flex-grow')
                    ui.label(n['zadal'] or '').classes('text-xs text-gray-400')
                    ui.label(f"{_kc_text(n['castka'])} Kč").classes('font-semibold whitespace-nowrap')
                    if radek['smi']:
                        ui.button(icon='delete', on_click=lambda _, i=n['id']: smaz(i)) \
                            .props('flat round dense size=sm color=negative')
            if seznam:
                ui.separator()
                ui.label(f"Celkem: {_kc_text(sum(n['castka'] for n in seznam))} Kč") \
                    .classes('w-full text-right font-bold')
        polozky()

        if radek['smi']:
            with ui.row().classes('w-full items-start gap-2 no-wrap'):
                popis = ui.input('Popis nákladu').classes('flex-grow')
                castka = ui.number('Částka', format='%.2f', suffix='Kč').classes('w-40') \
                    .props('hint="Ukládá se záporně"')

                def pridej():
                    if pridej_naklad(radek['cislo_oz'], obdobi, castka.value, popis.value,
                                     user_id, user_name, prava):
                        popis.value, castka.value = '', None
                        polozky.refresh()
                    else:
                        ui.notify('Vyplňte popis i nenulovou částku.', type='negative')
                ui.button(icon='add', on_click=pridej).props('round dense').classes('mt-3')
        with ui.row().classes('w-full justify-end'):
            ui.button('Zavřít', on_click=d.close).props('flat')
    # Tabulka pod dialogem se překreslí až po zavření — refresh by dialog smazal.
    d.on('hide', po_zavreni)
    d.open()


def _sekce_naklady2(user_id, user_name, prava):
    stav = {'obdobi': datetime.date.today().strftime('%Y-%m')}
    pole = [('naklady2', 'Náklady 2 celkem', 'kc'), ('pocet', 'Položek', 'pocet')]

    @ui.refreshable
    def obsah():
        obdobi = stav['obdobi']
        soucty = {}
        for n in nacti_naklady(obdobi):
            s = soucty.setdefault(n['cislo_oz'], [Decimal(0), 0])
            s[0] += n['castka']
            s[1] += 1
        radky = []
        for oz in nacti_oz(user_id, prava):
            s = soucty.get(oz['cislo_oz'])
            radky.append({**oz, 'naklady2': _f(s[0]) if s else None, 'pocet': s[1] if s else None})
        _tabulka(pole, radky,
                 lambda r: _dialog_naklady(r, obdobi, user_id, user_name, prava, obsah.refresh),
                 ikona='list', akce_vzdy=True)

    _vyber_obdobi(stav, lambda: obsah.refresh())
    obsah()


def _sekce_pristupy(user_name, prava):
    uzivatele = {u['iduser']: f"{u['surname']} {u['name']}" for u in _dotaz(
        "SELECT iduser, name, surname FROM user WHERE is_active=1 ORDER BY surname, name")}
    asm_vse = [r['asm'] for r in _dotaz(
        "SELECT DISTINCT asm FROM asm_oz WHERE aktivni=1 AND asm<>'' ORDER BY asm")]

    def prirazeni() -> dict:
        vysledek = {}
        for r in _dotaz("SELECT user_id, asm FROM nos_pristup ORDER BY asm"):
            vysledek.setdefault(r['user_id'], []).append(r['asm'])
        return vysledek

    ui.label('ASM dostane své jedno ASM, vedoucí pobočky všechna ASM, která pod něj patří. '
             'Uživatel k tomu potřebuje právo „ASM" nebo „Vedoucí pobočky" modulu NOS.') \
        .classes('text-gray-500 mb-2')
    with ui.row().classes('w-full items-center gap-3'):
        vyber_u = ui.select(uzivatele, label='Uživatel', with_input=True).classes('w-72')
        vyber_a = ui.select(asm_vse, label='Přiřazená ASM', multiple=True) \
            .props('use-chips').classes('flex-grow')

        def uloz_pristup():
            if not vyber_u.value:
                ui.notify('Vyberte uživatele.', type='warning')
            elif nastav_pristup(vyber_u.value, vyber_a.value or [], user_name, prava):
                ui.notify('Uloženo', type='positive')
                prehled.refresh()
            else:
                ui.notify('Uložení se nepodařilo.', type='negative')
        ui.button('Uložit', on_click=uloz_pristup)
    vyber_u.on_value_change(lambda e: vyber_a.set_value(prirazeni().get(e.value, [])))

    @ui.refreshable
    def prehled():
        data = prirazeni()
        ui.table(
            columns=[{'name': 'u', 'label': 'Uživatel', 'field': 'u', 'align': 'left', 'sortable': True},
                     {'name': 'a', 'label': 'ASM', 'field': 'a', 'align': 'left'}],
            rows=[{'id': uid, 'u': uzivatele.get(uid, f'#{uid} (neaktivní)'), 'a': ', '.join(a)}
                  for uid, a in data.items()],
            row_key='id').classes('w-full mt-4').props('flat bordered')
    prehled()


def _prehled(sloupce, radky, row_key='cislo_oz'):
    """Tabulka jen ke čtení s hledáním. sloupce = [(klíč, popisek, typ)]."""
    hledat = ui.input(placeholder='Hledat…').props('dense outlined clearable').classes('w-72 mb-2')
    t = ui.table(columns=[
        {'name': k, 'label': p, 'field': k, 'sortable': True,
         'align': 'right' if typ == 'kc' else 'left',
         **({':format': _FORMAT_KC} if typ == 'kc' else {})}
        for k, p, typ in sloupce], rows=radky, row_key=row_key, pagination=50) \
        .classes('w-full').props('flat bordered dense')
    hledat.bind_value_to(t, 'filter')


_OZ_SLOUPCE = [('asm', 'ASM', ''), ('cislo_oz', 'Číslo OZ', ''), ('jmeno_oz', 'Jméno OZ', ''),
               ('status', 'Status', '')]
_DATA_EXPORT = [('kategorie', 'Kategorie'), ('obdobi', 'Měsíc'), ('cislo_oz', 'Dealer 3'),
                ('site', 'sítě'), ('skupina_nazev', 'Nadřazená skupina Z'),
                ('obrat', 'Obrat v Kč bez DPH'), ('nzisk', 'Nzisk v KČ bez DPH'),
                ('obrat_site', 'Obrat v Kč bez DPH sítě'), ('obrat_cp2', 'Obrat CP2'),
                ('zisk_cp2', 'Zisk nad CP2'), ('kg', 'Obrat v Kg')]


def _podil(a, b):
    return a / b if b else None


def _posledni_obdobi() -> str:
    """Poslední měsíc, za který jsou nahraná data; bez dat aktuální měsíc."""
    r = _dotaz("SELECT MAX(obdobi) AS o FROM nos_data")
    return (r and r[0]['o']) or datetime.date.today().strftime('%Y-%m')


def _sekce_data(user_name):
    stav = {'obdobi': _posledni_obdobi()}

    @ui.refreshable
    def stav_importu():
        info = nacti_podminky()['info']
        for klic, popis in (('kategorie', 'Podmínky – kategorie'), ('skupiny', 'Podmínky – skupiny')):
            r = info.get(klic)
            ui.label(f"{popis}: {r['soubor']} ({r['nahral']}, {r['kdy']:%d.%m.%Y %H:%M})" if r
                     else f'{popis}: zatím nenahráno — provize vyjde 0') \
                .classes('text-sm ' + ('text-gray-600' if r else 'text-red-600'))
        mesice = _dotaz("SELECT obdobi, COUNT(*) AS n FROM nos_data GROUP BY obdobi ORDER BY obdobi")
        ui.label('Data: ' + (', '.join(f"{m['obdobi']} ({m['n']})" for m in mesice)
                             or 'zatím nenahráno')).classes('text-sm text-gray-600')

    @ui.refreshable
    def obsah():
        soucty = provize_oz(stav['obdobi'])
        radky = [{**s, 'provize': round(s['provize'], 2)} for s in soucty.values()]
        ui.label(f"Provize celkem {_kc_text(Decimal(str(sum(r['provize'] for r in radky))))} Kč "
                 f"· {len(radky)} OZ").classes('font-bold text-gray-700 mb-2')
        _prehled([('asm', 'ASM', ''), ('cislo_oz', 'Číslo OZ', ''), ('jmeno', 'Jméno OZ', ''),
                  ('obrat', 'Obrat bez DPH', 'kc'), ('zisk_cp2', 'Zisk nad CP2', 'kc'),
                  ('provize', 'Provize Kč', 'kc')], radky)

    def export():
        data = nacti_data(stav['obdobi'])
        if not data:
            ui.notify('Za zvolené období nejsou data.', type='warning')
            return
        _export_xlsx(f"NOS_data_{stav['obdobi']}.xlsx",
                     [h for _, h in _DATA_EXPORT] + ['Nzisk/kg', 'Zisk na CP2 v %', 'Zisk nad CP2/kg',
                                                     'ASM', 'Celkem provize %', 'Celkem provize Kč',
                                                     'Dealer Jméno'],
                     ([r[k] for k, _ in _DATA_EXPORT]
                      + [_podil(r['nzisk'], r['kg']), _podil(r['zisk_cp2'], r['obrat']),
                         _podil(r['zisk_cp2'], r['kg']), r['asm'],
                         _podil(r['provize'], r['obrat'] + r['obrat_site']), round(r['provize'], 2), r['jmeno']]
                      for r in data))

    async def zpracuj(e):
        raw, nazev = await intranet_asm._precti_upload(e, up, reset=False)
        if raw is None:
            return
        try:
            vysledek = await asyncio.to_thread(importuj_soubor, raw, nazev, user_name)
        except ValueError as exc:
            ui.notify(f'{nazev}: {exc}', type='negative', multi_line=True)
            return
        except Exception as exc:
            print(f'[nos] import {nazev}: {exc!r}')
            ui.notify(f'{nazev}: import selhal, nic se neuložilo.', type='negative')
            return
        intranet_logger.log_activity(user_name, 'NOS', f'Import {nazev} → {vysledek}')
        ui.notify(f'{nazev} → {vysledek}', type='positive', multi_line=True)
        stav_importu.refresh()
        obsah.refresh()

    ui.label('Nahrajte data.xlsx (list DATA) a obě tabulky podmínek (listy Kategorie, Skupiny). '
             'Soubor se pozná podle listu. Data nahradí měsíce, které soubor obsahuje.') \
        .classes('text-sm text-gray-500')
    up = ui.upload(on_upload=zpracuj, auto_upload=True, multiple=True, max_file_size=120_000_000,
                   label='Vybrat .xlsx (lze více najednou)') \
        .props('accept=.xlsx multiple').classes('w-full max-w-2xl')
    with ui.column().classes('gap-0 my-3'):
        stav_importu()
    with ui.row().classes('items-center gap-3'):
        _vyber_obdobi(stav, obsah.refresh)
        ui.button('Export XLSX', icon='download', on_click=export).props('flat').classes('mb-2')
    obsah()


def _sekce_podrobny(user_id, prava):
    stav = {'obdobi': _posledni_obdobi()}

    @ui.refreshable
    def obsah():
        radky = podrobny_podklad(stav['obdobi'], user_id, prava)
        _prehled(_OZ_SLOUPCE + POLE_PODKLAD,
                 [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in r.items()}
                  for r in radky])

    def export():
        sloupce = _OZ_SLOUPCE[1:3] + [_OZ_SLOUPCE[0], _OZ_SLOUPCE[3]] + POLE_PODKLAD
        _export_xlsx(f"NOS_podrobny_podklad_{stav['obdobi']}.xlsx", [p for _, p, _ in sloupce],
                     ([r[k] for k, _, _ in sloupce]
                      for r in podrobny_podklad(stav['obdobi'], user_id, prava)))

    with ui.row().classes('items-center gap-3'):
        _vyber_obdobi(stav, obsah.refresh)
        ui.button('Export XLSX', icon='download', on_click=export).props('flat').classes('mb-2')
    obsah()


# (klíč, ikona, název, popis, kdo vidí: None = každý s přístupem do modulu)
_DLAZDICE = [
    ('data', '📥', 'Data', 'Import dat NOS a výpočet provizí', je_analytik),
    ('info', '🧑‍💼', 'Informace k OZ', 'Smluvní odměna, termín, náklady na auto', None),
    ('odmeny', '⚖️', 'Odměny a malusy', 'Měsíční srážky a odměny ASM, akce', None),
    ('podrobny', '🧮', 'Podrobný podklad NOS', 'Výpočet odměny po OZ, export XLSX', je_analytik),
    ('podklady', '📄', 'Podklady NOS', 'PDF za každého OZ, podklady k vyplacení', None),
    ('doklady', '🧾', 'Přehled dokladů NOS', 'Čísla dokladů k vyplacené provizi', je_analytik),
    ('naklady2', '🚗', 'Náklady 2', 'Cestovné a ostatní náklady po měsících', None),
    ('pristupy', '🔑', 'Přístupy', 'Přiřazení ASM uživatelům', je_spravce),
]


def vykresli_nos(user_id: int, user_name: str, vsechna_prava: list):
    if not ma_pristup(vsechna_prava):
        with ui.column().classes('items-center py-24 gap-4'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('K modulu NOS nemáte přístup.').classes('text-gray-400 text-lg')
        return

    inicializace_db()
    stav_klic = f'nos_dlazdice_{user_id}'
    sekce = {   # dlaždice bez záznamu zde ještě nejsou hotové
        'data': lambda: _sekce_data(user_name),
        'podrobny': lambda: _sekce_podrobny(user_id, vsechna_prava),
        'info': lambda: _sekce_info(user_id, user_name, vsechna_prava),
        'odmeny': lambda: _sekce_odmeny(user_id, user_name, vsechna_prava),
        'naklady2': lambda: _sekce_naklady2(user_id, user_name, vsechna_prava),
        'pristupy': lambda: _sekce_pristupy(user_name, vsechna_prava),
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
        vybrana = next((d for d in viditelne
                        if d[0] == app.storage.user.get(stav_klic) and d[0] in sekce), None)
        if vybrana:
            with ui.row().classes('items-center gap-3 mb-4'):
                ui.button(icon='arrow_back', on_click=zpet).props('flat round') \
                    .tooltip('Zpět na dlaždice')
                ui.label(f'NOS – {vybrana[2]}').classes('text-2xl font-bold text-gray-800')
            sekce[vybrana[0]]()
            return
        ui.label('NOS').classes('text-3xl font-bold text-gray-800 mb-6')
        with ui.grid(columns=4).classes('w-full gap-4'):
            for klic, ikona, nazev, popis, _ in viditelne:
                hotova = klic in sekce
                with ui.card().classes(
                    'p-6 items-center gap-2 rounded-2xl border border-gray-100 '
                    + ('cursor-pointer hover:shadow-xl hover:-translate-y-0.5 transition-all duration-200'
                       if hotova else 'opacity-50')
                ) as karta:
                    ui.label(ikona).classes('text-5xl mb-1')
                    ui.label(nazev).classes('text-lg font-bold text-center text-gray-800')
                    ui.label(popis if hotova else 'Připravuje se') \
                        .classes('text-sm text-center text-gray-500')
                    if hotova:
                        karta.on('click', lambda k=klic: otevri(k))

    panel()


if __name__ == '__main__':      # rychlá kontrola vstupů (bez DB)
    assert _kc('2000', zaporne=True) == Decimal('-2000.00')
    assert _kc(-10500, zaporne=True) == Decimal('-10500.00')
    assert _kc('') is None and _kc(None) is None
    assert _kc(0.1 + 0.2) == Decimal('0.30')
    assert _je_obdobi('2026-06') and not _je_obdobi('2026-13') and not _je_obdobi('6/2026')
    assert not _je_obdobi(None) and not _je_obdobi('2026-06-01')
    assert je_spravce(['vse']) and je_analytik(['nos_vkladatel']) and not je_analytik(['nos_asm'])
    assert vidi_vse(['nos_ctenar']) and not vidi_vse(['nos_asm']) and ma_pristup(['nos_vedouci'])
    assert not ma_pristup(['asm_zadatel'])
    assert _kc_text(Decimal('-10500.5')) == '-10 500,50'
    # schody: platí nejvyšší práh ≤ hodnota, pod prvním prahem 0
    _schody = [[0, 0], [3.5, 0.1], [4, 0.101]]
    assert _stupen(_schody, 3.99) == 0.1 and _stupen(_schody, 4) == 0.101
    assert _stupen(_schody, 99) == 0.101 and _stupen(_schody, -1) == 0.0
    _p = {'kategorie': {'Bez sítí': {'sazba': None, 'skupiny': {'1': 0.0015}, 'ostatni': None},
                        'Výprodej': {'sazba': 0.0015, 'skupiny': {}, 'ostatni': None}},
          'skupiny': {'1': {'typ': 'kg', 'stupne': _schody}}}
    _r = {'kategorie': 'Bez sítí', 'skupina': 1, 'obrat': 1000.0, 'zisk_cp2': 100.0,
          'obrat_cp2': 900.0, 'kg': 25.0}
    assert abs(provize_radku(_r, _p) - (1.5 + 100 * 0.101)) < 1e-9      # 4 Kč/kg
    assert abs(provize_radku({**_r, 'kategorie': 'Výprodej'}, _p) - 1.5) < 1e-9
    assert provize_radku({**_r, 'kategorie': 'Neznámá'}, _p) == 0.0
    # zadání ř. 6 a 7: OSVČ, provize 55 555, smluvní 60 000 do 6/2026
    _v = vypocet_oz('OSVČ', 55555, 60000, '2026-06', '2026-06', -2000, 500, 1500, 0, 0)
    assert _v['dorovnani'] == 4445 and _v['mezisoucet'] == 60000 and _v['final'] == 60000
    _v = vypocet_oz('ZAM', 55555, 60000, '2026-06', '2026-07', -2000, 500, 1500, -10500, None)
    assert _v['dorovnani'] == 0 and _v['mezisoucet'] == 55555
    assert _v['odvod'] == Decimal('-13888.75') and _v['final'] == Decimal('31166.25')
    print('OK: intranet_nos')
