"""Náklady veletrh — evidence a schvalování nákladů firemního veletrhu JIP 2027.

Samostatná dlaždice na přehledu; s Plánem Veletrh 2027 (intranet_veletrh) nesouvisí.

Zadavatel zadá náklad (kategorie, dodavatel, částka bez DPH + sazba → s DPH,
splatnost, zdůvodnění, přílohy) a uloží ho jako koncept, nebo odešle ke schválení
— schvalovatelům přijde e-mail s odkazem. Schvalovatel náklad schválí, zamítne,
nebo vrátí k doplnění (zamítnutí a vrácení s povinným komentářem), zadavateli
přijde e-mail s výsledkem; zaplacený náklad označí jako uhrazený. Do čerpání se
počítá jen schválené a uhrazené, čekající se ukazuje zvlášť jako rezervace.
Náklad, se kterým by čerpání přesáhlo celkový rozpočet, schválit nejde. Každá
akce i změna rozpočtu jde do historie. Na náklady čekající déle než 3 pracovní
dny upozorní schvalovatele souhrnný e-mail. Koncept vidí jen jeho autor,
o vlastním nákladu schvalovatel nerozhoduje. Částečný správce smí totéž co
schvalovatel (rozpočet, kategorie, uhrazeno, komentáře), jen o nákladech nerozhoduje
a nechodí mu e-maily o nákladech ke schválení.
Práva: naklady_veletrh_zadavatel / _schvalovatel / _spravce / _ctenar ('vse' = všechna).
Role 'spravce' = správa modulu; schvalovatel ji má automaticky navíc k rozhodování.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import io
import math
import os
import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi.responses import FileResponse, PlainTextResponse
from nicegui import app, background_tasks, ui

import intranet_data
import intranet_emaily
import intranet_logger

PRAVO_ZADAVATEL = 'naklady_veletrh_zadavatel'
PRAVO_SCHVALOVATEL = 'naklady_veletrh_schvalovatel'
PRAVO_SPRAVCE = 'naklady_veletrh_spravce'      # Částečný správce — jako schvalovatel, ale nerozhoduje
PRAVO_CTENAR = 'naklady_veletrh_ctenar'
POVINNA_PRILOHA_OD = 10_000     # Kč s DPH — od této částky je povinná příloha (nabídka/faktura)
VAROVANI_KATEGORIE = 0.85       # čerpání kategorie včetně čekajících
VAROVANI_CELKEM = 0.90          # čerpání celého rozpočtu včetně čekajících
PRIPOMINKA_PO = 3               # pracovních dní čekání → souhrnná připomínka schvalovatelům
PRIPOMINKA_HODINA = 8           # nejdřív v kolik (jednou za pracovní den)
DPH_SAZBY = (21, 12, 0)
MAX_PRILOHA = 20 * 1024 * 1024
PRILOHY = ('.pdf', '.png', '.jpg', '.jpeg', '.xlsx', '.xls', '.docx', '.doc', '.csv', '.txt')
_V_PROHLIZECI = ('.pdf', '.png', '.jpg', '.jpeg')       # ostatní přílohy se stáhnou
ADRESAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'naklady_veletrh_prilohy')
_LOG = 'Náklady veletrh'
_DEEP = 'naklady_veletrh_naklad'     # app.storage.user: deep-link z e-mailu (/naklady_veletrh?naklad=<id>)

_STAVY = {
    'koncept':   ('Koncept', 'grey-6'),
    'ceka':      ('Čeká na schválení', 'amber-8'),
    'vraceno':   ('Vráceno k doplnění', 'orange-9'),
    'schvaleno': ('Schváleno', 'green-7'),
    'zamitnuto': ('Zamítnuto', 'red-7'),
    'uhrazeno':  ('Uhrazeno', 'teal-7'),
}
CERPANO = ('schvaleno', 'uhrazeno')
OTEVRENE = ('koncept', 'vraceno', 'ceka')       # autor ještě smí upravovat
_ROZHODNUTI = {'schvalit': ('schvaleno', 'Schválil(a) náklad'),
               'zamitnout': ('zamitnuto', 'Zamítl(a) náklad'),
               'vratit': ('vraceno', 'Vrátil(a) náklad k doplnění')}
_HLASKY = {'komentar': 'Komentář přidán.', 'odeslat': 'Náklad odeslán ke schválení.',
           'stahnout': 'Náklad stažen zpět do konceptu.', 'schvalit': 'Náklad schválen.',
           'zamitnout': 'Náklad zamítnut.', 'vratit': 'Náklad vrácen k doplnění.',
           'uhrazeno': 'Náklad označen jako uhrazený.'}
_CERPANO_BARVA, _CEKA_BARVA, _TENTO_BARVA, _VOLNE_BARVA = '#1B2430', '#f59e0b', '#ea580c', '#e5e7eb'
_MESICE = ('led', 'úno', 'bře', 'dub', 'kvě', 'čvn', 'čvc', 'srp', 'zář', 'říj', 'lis', 'pro')

_VYCHOZI_KATEGORIE = (      # doporučení ze zadání; rozpočty jsou ilustrativní, upraví je schvalovatel
    ('Pronájem výstavní plochy', 600_000, '#1B2430'),
    ('Stánek a expozice', 700_000, '#E4572E'),
    ('Doprava a logistika', 250_000, '#3D7EA6'),
    ('Ubytování a cestovné', 300_000, '#7A9E7E'),
    ('Marketing a tisk', 280_000, '#D9A21B'),
    ('Catering a hosté', 220_000, '#9B5DE5'),
    ('Personál a služby', 100_000, '#8C8C8C'),
    ('Rezerva', 50_000, '#C9C3B8'),
)
_VYCHOZI_NASTAVENI = {
    'rozpocet': '2500000',
    'akce': 'Firemní veletrh JIP 2027',
    'datum_akce': '2027-05-12',
    'plan_od': '2026-07',       # od kterého měsíce se plánuje rovnoměrné čerpání (graf)
    'pripominka': '',           # den poslední souhrnné připomínky
}


def _role(prava) -> frozenset:
    """Role uživatele v modulu podle práv intranetu ('vse' = všechny)."""
    p = set(prava or ())
    if 'vse' in p:
        return frozenset(('zadavatel', 'schvalovatel', 'spravce', 'ctenar'))
    role = {r for r, pravo in (('zadavatel', PRAVO_ZADAVATEL), ('schvalovatel', PRAVO_SCHVALOVATEL),
                               ('spravce', PRAVO_SPRAVCE), ('ctenar', PRAVO_CTENAR)) if pravo in p}
    if 'schvalovatel' in role:
        role.add('spravce')
    return frozenset(role)


def _zapnuto() -> bool:
    return bool(intranet_data.nacti_nastaveni_intranetu().get('naklady_veletrh_zapnuty', True))


def ma_pristup_k_modulu(prava) -> bool:
    return bool(_role(prava)) and _zapnuto()


class Chyba(Exception):
    """Porušené pravidlo nebo souběh — text se ukáže uživateli."""


# =========================================================
# FORMÁTOVÁNÍ
# =========================================================
def _kc(x, presne=False) -> str:
    """12 500 Kč; presne=True ukáže i haléře, když nějaké jsou (12 500,50 Kč)."""
    if presne and x != int(x):
        return f'{x:,.2f}'.replace(',', ' ').replace('.', ',') + ' Kč'
    return f'{x:,.0f}'.replace(',', ' ') + ' Kč'


def _cislo_text(x) -> str:
    """Hodnota do editačního pole: 12500 / 12500,50."""
    return str(int(x)) if x == int(x) else f'{x:.2f}'.replace('.', ',')


def _castka(text) -> Decimal | None:
    """'12 500,50' → Decimal('12500.50'); nesmysl → None."""
    try:
        d = Decimal(str(text).replace(' ', '').replace(' ', '').replace(',', '.'))
    except (InvalidOperation, ValueError):
        return None
    return d.quantize(Decimal('0.01'), ROUND_HALF_UP) if d.is_finite() and abs(d) < 10 ** 12 else None


def _s_dph(bez, sazba) -> Decimal:
    return (bez * (100 + sazba) / 100).quantize(Decimal('0.01'), ROUND_HALF_UP)


def _kod(nid) -> str:
    return f'N-{int(nid):04d}'


def _pct(x) -> str:
    return f'{x * 100:.0f} %'


def _podil(a, b) -> float:
    return float(a) / float(b) if b else 0.0


def _datum(d) -> str:
    return d.strftime('%d.%m.%Y') if d else ''


def _cas(d) -> str:
    return d.strftime('%d.%m.%Y %H:%M') if d else ''


def _cz_datum(text):
    """'DD.MM.RRRR' → date; prázdné nebo nesmysl → None."""
    try:
        return datetime.datetime.strptime((text or '').strip(), '%d.%m.%Y').date()
    except ValueError:
        return None


def _pocet(n, tvary=('náklad', 'náklady', 'nákladů')) -> str:
    return f'{n} {tvary[0] if n == 1 else tvary[1] if 2 <= n <= 4 else tvary[2]}'


def _mesic(m) -> str:
    """'2027-05' → 'kvě 27'."""
    return f'{_MESICE[int(m[5:7]) - 1]} {m[2:4]}'


def _do_veletrhu(nast) -> str:
    try:
        d = datetime.date.fromisoformat(nast['datum_akce'])
    except ValueError:
        return ''
    dni = (d - datetime.date.today()).days
    kdy = f'za {_pocet(dni, ("den", "dny", "dní"))}' if dni > 0 else 'dnes' if dni == 0 else 'proběhl'
    return f'veletrh {_datum(d)} ({kdy})'


# =========================================================
# DATABÁZE (blokující — volat přes asyncio.to_thread)
# =========================================================
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_kategorie (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nazev VARCHAR(120) NOT NULL,
        rozpocet DECIMAL(15,2) NOT NULL DEFAULT 0,
        barva VARCHAR(9) NOT NULL DEFAULT '#888888',
        poradi INT NOT NULL DEFAULT 0,
        UNIQUE KEY uq_nazev (nazev)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS naklady_veletrh (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nazev VARCHAR(255) NOT NULL,
        kategorie_id INT NOT NULL,
        dodavatel VARCHAR(255) NOT NULL,
        dodavatel_ico VARCHAR(20) NULL,
        castka_bez_dph DECIMAL(15,2) NOT NULL,
        sazba_dph INT NOT NULL,
        castka_s_dph DECIMAL(15,2) NOT NULL,
        splatnost DATE NULL,
        zduvodneni TEXT NOT NULL,
        stav VARCHAR(20) NOT NULL DEFAULT 'koncept',
        zadal_id INT NOT NULL,
        zadal_jmeno VARCHAR(255),
        vytvoreno DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        upraveno DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        odeslano DATETIME NULL,
        rozhodl_id INT NULL,
        rozhodl_jmeno VARCHAR(255),
        rozhodnuto DATETIME NULL,
        uhrazeno DATETIME NULL,
        INDEX idx_stav (stav),
        INDEX idx_zadal (zadal_id),
        INDEX idx_kategorie (kategorie_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_prilohy (
        id INT AUTO_INCREMENT PRIMARY KEY,
        naklad_id INT NOT NULL,
        nazev VARCHAR(255) NOT NULL,
        disk VARCHAR(64) NOT NULL,
        velikost INT NOT NULL,
        nahral_jmeno VARCHAR(255),
        nahrano DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_naklad (naklad_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_historie (
        id INT AUTO_INCREMENT PRIMARY KEY,
        naklad_id INT NULL,
        user_id INT NULL,
        kdo VARCHAR(255),
        akce VARCHAR(255) NOT NULL,
        komentar TEXT,
        cas DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_naklad (naklad_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_nastaveni (
        klic VARCHAR(40) PRIMARY KEY,
        hodnota VARCHAR(255) NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
)

_DB_HOTOVO = False


def inicializace_db():
    """Tabulky, výchozí nastavení a kategorie (kategorie jen do prázdné tabulky)."""
    global _DB_HOTOVO
    if _DB_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor(buffered=True)
        for prikaz in _SCHEMA:
            cur.execute(prikaz)
        cur.executemany('INSERT IGNORE INTO naklady_veletrh_nastaveni (klic, hodnota) VALUES (%s, %s)',
                        list(_VYCHOZI_NASTAVENI.items()))
        cur.execute('SELECT COUNT(*) FROM naklady_veletrh_kategorie')
        if not cur.fetchone()[0]:
            cur.executemany('INSERT INTO naklady_veletrh_kategorie (nazev, rozpocet, barva, poradi) '
                            'VALUES (%s, %s, %s, %s)',
                            [(n, r, b, i) for i, (n, r, b) in enumerate(_VYCHOZI_KATEGORIE, 1)])
        conn.commit()
        cur.close()
        _DB_HOTOVO = True
    except Exception as e:
        print(f'[naklady_veletrh] inicializace_db: {e}')
    finally:
        conn.close()


@contextmanager
def _transakce():
    """Kurzor v jedné transakci: commit na konci, rollback při výjimce (i Chyba)."""
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


def _sql(dotaz, parametry=(), vysledek=None):
    """Jeden dotaz. vysledek: 'vse' | 'jeden' | None (= počet změněných řádků)."""
    with _transakce() as cur:
        cur.execute(dotaz, parametry)
        if vysledek == 'vse':
            return cur.fetchall()
        if vysledek == 'jeden':
            return cur.fetchone()
        return cur.rowcount


def _zapis(cur, naklad_id, user_id, kdo, akce, komentar=None):
    """Auditní historie; naklad_id None = změna rozpočtu."""
    cur.execute('INSERT INTO naklady_veletrh_historie (naklad_id, user_id, kdo, akce, komentar) '
                'VALUES (%s, %s, %s, %s, %s)', (naklad_id, user_id, kdo, akce[:255], komentar or None))


_SELECT = ('SELECT n.*, k.nazev AS kategorie, '
           '(SELECT COUNT(*) FROM naklady_veletrh_prilohy p WHERE p.naklad_id = n.id) AS pocet_priloh '
           'FROM naklady_veletrh n JOIN naklady_veletrh_kategorie k ON k.id = n.kategorie_id')


def _nastaveni(cur) -> dict:
    cur.execute('SELECT klic, hodnota FROM naklady_veletrh_nastaveni')
    return {**_VYCHOZI_NASTAVENI, **{r['klic']: r['hodnota'] for r in cur.fetchall()}}


def _prehled(cur) -> dict:
    """Rozpočet a čerpání celku i kategorií. Čerpáno = schváleno + uhrazeno, čeká = rezervace."""
    nast = _nastaveni(cur)
    cur.execute("SELECT k.id, k.nazev, k.rozpocet, k.barva, "
                "COALESCE(SUM(CASE WHEN n.stav IN ('schvaleno','uhrazeno') THEN n.castka_s_dph END), 0) AS cerpano, "
                "COALESCE(SUM(CASE WHEN n.stav = 'ceka' THEN n.castka_s_dph END), 0) AS ceka "
                "FROM naklady_veletrh_kategorie k LEFT JOIN naklady_veletrh n ON n.kategorie_id = k.id "
                "GROUP BY k.id ORDER BY k.poradi, k.id")
    kategorie = cur.fetchall()
    for k in kategorie:
        k['volne'] = k['rozpocet'] - k['cerpano'] - k['ceka']
        k['podil'] = _podil(k['cerpano'] + k['ceka'], k['rozpocet'])
        k['prekroceno'] = k['volne'] < 0
        k['varovani'] = k['podil'] > VAROVANI_KATEGORIE
    celkem = _castka(nast['rozpocet']) or Decimal(0)
    cerpano = sum((k['cerpano'] for k in kategorie), Decimal(0))
    ceka = sum((k['ceka'] for k in kategorie), Decimal(0))
    return {'nastaveni': nast, 'kategorie': kategorie, 'celkem': celkem, 'cerpano': cerpano, 'ceka': ceka,
            'volne': celkem - cerpano - ceka, 'podil': _podil(cerpano + ceka, celkem),
            'rozdeleno': sum((k['rozpocet'] for k in kategorie), Decimal(0))}


def _mesice(od, do) -> list[str]:
    """Měsíce 'RRRR-MM' od–do včetně (nejvýš 5 let)."""
    try:
        r, m = (int(x) for x in od[:7].split('-'))
        kr, km = (int(x) for x in do[:7].split('-'))
    except ValueError:
        return []
    out = []
    while (r, m) <= (kr, km) and len(out) < 60:
        out.append(f'{r:04d}-{m:02d}')
        r, m = (r + 1, 1) if m == 12 else (r, m + 1)
    return out


def _graf(cur, p) -> dict:
    """Kumulativní čerpání podle měsíce schválení vs. rovnoměrný plán do měsíce veletrhu."""
    cur.execute("SELECT LEFT(rozhodnuto, 7) AS m, SUM(castka_s_dph) AS s FROM naklady_veletrh "
                "WHERE stav IN ('schvaleno','uhrazeno') GROUP BY LEFT(rozhodnuto, 7)")
    po_mesicich = {r['m']: r['s'] for r in cur.fetchall()}
    dnes = datetime.date.today().strftime('%Y-%m')
    mesice = _mesice(p['nastaveni']['plan_od'], p['nastaveni']['datum_akce']) or [dnes]
    bezi = sum((s for m, s in po_mesicich.items() if m and m < mesice[0]), Decimal(0))
    skutecnost = []
    for m in mesice:
        bezi += po_mesicich.get(m) or 0
        skutecnost.append(float(bezi) if m <= dnes else None)
    return {'popisky': [_mesic(m) for m in mesice], 'skutecnost': skutecnost,
            'plan': [round(float(p['celkem']) * (i + 1) / len(mesice)) for i in range(len(mesice))]}


def _nacti(user_id) -> dict:
    """Vše, co modul zobrazuje: rozpočet, náklady, které uživatel vidí (cizí koncepty ne),
    jejich přílohy a historie, historie rozpočtu a řada pro graf čerpání."""
    with _transakce() as cur:
        p = _prehled(cur)
        cur.execute(_SELECT + " WHERE n.stav <> 'koncept' OR n.zadal_id = %s ORDER BY n.id DESC", (user_id,))
        naklady = cur.fetchall()
        viditelne = {n['id'] for n in naklady}
        prilohy, historie = defaultdict(list), defaultdict(list)
        cur.execute('SELECT id, naklad_id, nazev, velikost FROM naklady_veletrh_prilohy ORDER BY id')
        for r in cur.fetchall():
            if r['naklad_id'] in viditelne:
                prilohy[r['naklad_id']].append(r)
        cur.execute('SELECT naklad_id, kdo, akce, komentar, cas FROM naklady_veletrh_historie ORDER BY cas, id')
        for r in cur.fetchall():
            if r['naklad_id'] is None or r['naklad_id'] in viditelne:
                historie[r['naklad_id']].append(r)
        graf = _graf(cur, p)
    return {'prehled': p, 'naklady': naklady, 'podle_id': {n['id']: n for n in naklady},
            'prilohy': prilohy, 'historie': historie, 'graf': graf}


def _dopad(n, p) -> dict | None:
    """Co udělá schválení nákladu s rozpočtem kategorie a celé akce."""
    kat = next((k for k in p['kategorie'] if k['id'] == n['kategorie_id']), None)
    if not kat:
        return None
    castka = n['castka_s_dph']
    otevreny = n['stav'] in OTEVRENE
    po = kat['rozpocet'] - kat['cerpano'] - (castka if otevreny else 0)
    return {'kat': kat, 'castka': castka, 'otevreny': otevreny, 'po': po,
            'po_podil': _podil(po, kat['rozpocet']),
            'ostatni_ceka': kat['ceka'] - (castka if n['stav'] == 'ceka' else 0),
            'celkem_po': p['celkem'] - p['cerpano'] - (castka if otevreny else 0),
            'blokovano': n['stav'] == 'ceka' and p['cerpano'] + castka > p['celkem']}


def _chyby_odeslani(cur, nid) -> list[str]:
    cur.execute('SELECT n.castka_s_dph, (SELECT COUNT(*) FROM naklady_veletrh_prilohy p '
                'WHERE p.naklad_id = n.id) AS prilohy FROM naklady_veletrh n WHERE n.id = %s', (nid,))
    r = cur.fetchone()
    if r and r['castka_s_dph'] >= POVINNA_PRILOHA_OD and not r['prilohy']:
        return [f'U nákladu od {_kc(POVINNA_PRILOHA_OD)} s DPH je povinná příloha (nabídka nebo faktura).']
    return []


def _odeslat(cur, nid, user_id, kdo):
    """Ke schválení; rozhodnutí z minulého kola (vrácení) se maže — zůstane v historii."""
    cur.execute("UPDATE naklady_veletrh SET stav = 'ceka', odeslano = NOW(), upraveno = NOW(), "
                "rozhodl_id = NULL, rozhodl_jmeno = NULL, rozhodnuto = NULL WHERE id = %s", (nid,))
    _zapis(cur, nid, user_id, kdo, 'Odeslal(a) ke schválení')


_POLE_ZMENY = (('nazev', 'název'), ('kategorie_id', 'kategorie'), ('dodavatel', 'dodavatel'),
               ('dodavatel_ico', 'IČO'), ('castka_s_dph', 'částka s DPH'), ('sazba_dph', 'sazba DPH'),
               ('splatnost', 'splatnost'), ('zduvodneni', 'zdůvodnění'))


def _zmeny(stary, novy, kategorie) -> str:
    """Čitelný souhrn úprav do historie — schvalovatel vidí, co se změnilo."""
    def txt(klic, v):
        if v is None or v == '':
            return '—'
        return {'kategorie_id': lambda: kategorie.get(v, '?'), 'castka_s_dph': lambda: _kc(v, True),
                'sazba_dph': lambda: f'{v} %', 'splatnost': lambda: _datum(v)}.get(klic, lambda: str(v))()
    out = []
    for klic, popis in _POLE_ZMENY:
        if stary[klic] != novy[klic]:
            out.append(popis if klic == 'zduvodneni'
                       else f'{popis}: {txt(klic, stary[klic])} → {txt(klic, novy[klic])}')
    return '; '.join(out)


def _uloz(user_id, kdo, nid, d, nove_prilohy, odeslat) -> tuple[int, list[str]]:
    """Založí (nid None) nebo upraví vlastní náklad, připojí nahrané přílohy
    [(název, disk, velikost)] a případně ho odešle. Vrací (nid, chyby odeslání) — s chybami
    zůstane uložený (nový jako koncept). Čekající náklad musí pravidla splňovat i po úpravě."""
    hodnoty = (d['nazev'], d['kategorie_id'], d['dodavatel'], d['dodavatel_ico'], d['castka_bez_dph'],
               d['sazba_dph'], d['castka_s_dph'], d['splatnost'], d['zduvodneni'])
    with _transakce() as cur:
        cur.execute('SELECT id, nazev FROM naklady_veletrh_kategorie')
        kategorie = {r['id']: r['nazev'] for r in cur.fetchall()}
        if d['kategorie_id'] not in kategorie:
            raise Chyba('Vybraná kategorie už neexistuje.')
        if nid is None:
            cur.execute('INSERT INTO naklady_veletrh (nazev, kategorie_id, dodavatel, dodavatel_ico, castka_bez_dph, '
                        'sazba_dph, castka_s_dph, splatnost, zduvodneni, zadal_id, zadal_jmeno) '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', hodnoty + (user_id, kdo))
            nid, stav = cur.lastrowid, 'koncept'
            _zapis(cur, nid, user_id, kdo, 'Vytvořil(a) náklad')
        else:
            cur.execute('SELECT * FROM naklady_veletrh WHERE id = %s FOR UPDATE', (nid,))
            n = cur.fetchone()
            if not n or n['zadal_id'] != user_id or n['stav'] not in OTEVRENE:
                raise Chyba('Náklad už upravit nelze — mezitím o něm někdo rozhodl.')
            stav = n['stav']
            cur.execute('UPDATE naklady_veletrh SET nazev = %s, kategorie_id = %s, dodavatel = %s, '
                        'dodavatel_ico = %s, castka_bez_dph = %s, sazba_dph = %s, castka_s_dph = %s, '
                        'splatnost = %s, zduvodneni = %s, upraveno = NOW() WHERE id = %s', hodnoty + (nid,))
            zmeny = _zmeny(n, d, kategorie)
            if zmeny:
                _zapis(cur, nid, user_id, kdo, 'Upravil(a) náklad', zmeny)
        for nazev, disk, velikost in nove_prilohy:
            cur.execute('INSERT INTO naklady_veletrh_prilohy (naklad_id, nazev, disk, velikost, nahral_jmeno) '
                        'VALUES (%s, %s, %s, %s, %s)', (nid, nazev, disk, velikost, kdo))
            _zapis(cur, nid, user_id, kdo, f'Přidal(a) přílohu „{nazev}“')
        if stav == 'ceka' or odeslat:
            chyby = _chyby_odeslani(cur, nid)
            if chyby and stav == 'ceka':
                raise Chyba(' '.join(chyby))
            if not chyby and stav != 'ceka':
                _odeslat(cur, nid, user_id, kdo)
            return nid, chyby
    return nid, []


def _proved(user_id, kdo, role, nid, akce, komentar='') -> dict:
    """Akce nad nákladem v jedné transakci se zamčeným řádkem — dva schvalovatelé
    nerozhodnou totéž dvakrát. Schválení zamyká i celkový rozpočet (stejně jako jeho
    změna), takže ho souběžná schválení nepřečerpají. Vrací náklad před akcí."""
    komentar = (komentar or '').strip()[:4000]
    with _transakce() as cur:
        if akce == 'schvalit':
            cur.execute("SELECT hodnota FROM naklady_veletrh_nastaveni WHERE klic = 'rozpocet' FOR UPDATE")
            celkem = _castka(cur.fetchone()['hodnota']) or Decimal(0)
        cur.execute('SELECT * FROM naklady_veletrh WHERE id = %s FOR UPDATE', (nid,))
        n = cur.fetchone()
        if not n or (n['stav'] == 'koncept' and n['zadal_id'] != user_id):
            raise Chyba('Náklad nenalezen.')
        vlastni = n['zadal_id'] == user_id
        if akce == 'komentar':
            if not role & {'zadavatel', 'spravce'}:
                raise Chyba('Komentáře píše jen zadavatel, správce nebo schvalovatel.')
            if not komentar:
                raise Chyba('Napište text komentáře.')
            _zapis(cur, nid, user_id, kdo, 'Komentář', komentar)
        elif akce == 'odeslat':
            if not (vlastni and 'zadavatel' in role and n['stav'] in ('koncept', 'vraceno')):
                raise Chyba('Náklad teď odeslat nejde.')
            chyby = _chyby_odeslani(cur, nid)
            if chyby:
                raise Chyba(' '.join(chyby))
            _odeslat(cur, nid, user_id, kdo)
        elif akce == 'stahnout':
            if not (vlastni and n['stav'] == 'ceka'):
                raise Chyba('Stáhnout jde jen vlastní náklad, který čeká na schválení.')
            cur.execute("UPDATE naklady_veletrh SET stav = 'koncept', upraveno = NOW() WHERE id = %s", (nid,))
            _zapis(cur, nid, user_id, kdo, 'Stáhl(a) náklad zpět do konceptu', komentar)
        elif akce in _ROZHODNUTI:
            if 'schvalovatel' not in role:
                raise Chyba('O nákladech rozhoduje jen schvalovatel.')
            if n['stav'] != 'ceka':
                raise Chyba('Náklad už na schválení nečeká — mezitím o něm někdo rozhodl, nebo ho autor stáhl.')
            if vlastni:
                raise Chyba('O vlastním nákladu rozhodnout nejde.')
            if akce != 'schvalit' and not komentar:
                raise Chyba('U zamítnutí a vrácení je komentář povinný.')
            if akce == 'schvalit':
                cur.execute("SELECT COALESCE(SUM(castka_s_dph), 0) AS s FROM naklady_veletrh "
                            "WHERE stav IN ('schvaleno','uhrazeno')")
                if cur.fetchone()['s'] + n['castka_s_dph'] > celkem:
                    raise Chyba('Náklad by překročil celkový rozpočet veletrhu. '
                                'Nejprve navyšte rozpočet (záložka Rozpočet).')
            novy, popis = _ROZHODNUTI[akce]
            cur.execute('UPDATE naklady_veletrh SET stav = %s, rozhodl_id = %s, rozhodl_jmeno = %s, '
                        'rozhodnuto = NOW(), upraveno = NOW() WHERE id = %s', (novy, user_id, kdo, nid))
            _zapis(cur, nid, user_id, kdo, popis, komentar)
        elif akce == 'uhrazeno':
            if 'spravce' not in role or n['stav'] != 'schvaleno':
                raise Chyba('Jako uhrazený jde označit jen schválený náklad.')
            cur.execute("UPDATE naklady_veletrh SET stav = 'uhrazeno', uhrazeno = NOW(), upraveno = NOW() "
                        "WHERE id = %s", (nid,))
            _zapis(cur, nid, user_id, kdo, 'Označil(a) náklad jako uhrazený', komentar)
        else:
            raise Chyba('Neznámá akce.')
    return n


def _uloz_rozpocet(user_id, kdo, nast, kategorie, nova) -> list[str]:
    """Nastavení akce, celkový rozpočet a rozpočty kategorií (+ nová kategorie) v jedné
    transakci. Zamyká celkový rozpočet jako schválení, takže souběžné schválení počítá
    s novou hodnotou. Vrací seznam změn (ten jde i do historie)."""
    zmeny = []
    with _transakce() as cur:
        cur.execute("SELECT hodnota FROM naklady_veletrh_nastaveni WHERE klic = 'rozpocet' FOR UPDATE")
        stare = _nastaveni(cur)
        for klic, popis in (('akce', 'název akce'), ('datum_akce', 'datum veletrhu'),
                            ('plan_od', 'plán čerpání od'), ('rozpocet', 'celkový rozpočet')):
            if klic == 'rozpocet':
                stara = _castka(stare[klic]) or Decimal(0)
                if stara == nast[klic]:
                    continue
                zmeny.append(f'{popis} {_kc(stara)} → {_kc(nast[klic])}')
            elif stare[klic] == nast[klic]:
                continue
            else:
                zmeny.append(f'{popis} {stare[klic] or "—"} → {nast[klic]}')
            cur.execute('UPDATE naklady_veletrh_nastaveni SET hodnota = %s WHERE klic = %s', (str(nast[klic]), klic))
        cur.execute('SELECT id, nazev, rozpocet, barva FROM naklady_veletrh_kategorie')
        puvodni = {k['id']: k for k in cur.fetchall()}
        for k in kategorie:
            p = puvodni.get(k['id'])
            if not p:
                continue
            barva = p['barva'].lower() != k['barva'].lower()
            if p['nazev'] == k['nazev'] and p['rozpocet'] == k['rozpocet'] and not barva:
                continue
            if p['nazev'] != k['nazev']:
                zmeny.append(f'kategorie „{p["nazev"]}“ přejmenována na „{k["nazev"]}“')
            if p['rozpocet'] != k['rozpocet']:
                zmeny.append(f'{k["nazev"]} {_kc(p["rozpocet"])} → {_kc(k["rozpocet"])}')
            if barva:
                zmeny.append(f'{k["nazev"]}: nová barva')
            cur.execute('UPDATE naklady_veletrh_kategorie SET nazev = %s, rozpocet = %s, barva = %s WHERE id = %s',
                        (k['nazev'], k['rozpocet'], k['barva'], k['id']))
        if nova:
            cur.execute('INSERT INTO naklady_veletrh_kategorie (nazev, rozpocet, barva, poradi) '
                        'SELECT %s, %s, %s, COALESCE(MAX(poradi), 0) + 1 FROM naklady_veletrh_kategorie',
                        (nova['nazev'], nova['rozpocet'], nova['barva']))
            zmeny.append(f'nová kategorie „{nova["nazev"]}“ {_kc(nova["rozpocet"])}')
        if zmeny:
            _zapis(cur, None, user_id, kdo, 'Změna rozpočtu', '; '.join(zmeny))
    return zmeny


def pocet_akci(user_id, prava) -> int:
    """Odznak dlaždice: náklady čekající na rozhodnutí (schvalovatel; vlastní ne)
    + vlastní vrácené k doplnění (zadavatel). Chyba = 0, přehled se kvůli tomu nerozbije."""
    role = _role(prava)
    if not role & {'zadavatel', 'schvalovatel'}:
        return 0
    try:
        inicializace_db()
        r = _sql("SELECT COALESCE(SUM(stav = 'ceka' AND zadal_id <> %s), 0) AS ceka, "
                 "COALESCE(SUM(stav = 'vraceno' AND zadal_id = %s), 0) AS vraceno FROM naklady_veletrh",
                 (user_id, user_id), 'jeden')
        return int(r['ceka']) * ('schvalovatel' in role) + int(r['vraceno']) * ('zadavatel' in role)
    except Exception:
        return 0


# =========================================================
# PŘÍLOHY — na disk pod náhodným jménem, ven jen přes kontrolu práv
# =========================================================
async def _uloz_soubor(f) -> tuple[str, str, int]:
    """Nahraný soubor → (původní název, jméno na disku, velikost). Kontrola na serveru
    (accept v prohlížeči jde obejít): přípona a velikost."""
    nazev = os.path.basename((f.name or '').replace('\\', '/')).strip()[:200] or 'priloha'
    ext = os.path.splitext(nazev)[1].lower()
    if ext not in PRILOHY:
        raise Chyba(f'„{nazev}“: nepovolený typ souboru. Povoleno: '
                    f'{", ".join(x[1:].upper() for x in PRILOHY)}.')
    velikost = f.size()
    if not 0 < velikost <= MAX_PRILOHA:
        raise Chyba(f'„{nazev}“: soubor je prázdný, nebo větší než {MAX_PRILOHA // 1024 // 1024} MB.')
    disk = uuid.uuid4().hex + ext
    os.makedirs(ADRESAR, exist_ok=True)
    await f.save(os.path.join(ADRESAR, disk))
    return nazev, disk, velikost


def _smaz_soubory(disky):
    for disk in disky:
        try:
            os.remove(os.path.join(ADRESAR, os.path.basename(disk)))
        except OSError:
            pass


def _priloha_ke_stazeni(user_id, pid):
    """(cesta, název) přílohy, kterou uživatel smí vidět — zapnutý modul, aktivní účet,
    některá z rolí a není to cizí koncept. Jinak None (i při chybě: fail closed)."""
    try:
        if not _zapnuto():
            return None
        prava, aktivni = intranet_data.ziskej_prava_a_aktivitu(user_id)
        if not aktivni or not _role(prava):
            return None
        p = _sql('SELECT p.nazev, p.disk, n.stav, n.zadal_id FROM naklady_veletrh_prilohy p '
                 'JOIN naklady_veletrh n ON n.id = p.naklad_id WHERE p.id = %s', (pid,), 'jeden')
        if not p or (p['stav'] == 'koncept' and p['zadal_id'] != user_id):
            return None
        cesta = os.path.join(ADRESAR, os.path.basename(p['disk']))
        return (cesta, p['nazev']) if os.path.isfile(cesta) else None
    except Exception:
        return None


@app.get('/naklady_veletrh/priloha/{pid}')
async def _stazeni_prilohy(pid: int):
    try:
        user_id = app.storage.user.get('user_id')
    except Exception:
        user_id = None      # bez relace = nepřihlášen
    s = await asyncio.to_thread(_priloha_ke_stazeni, user_id, pid) if user_id else None
    if not s:
        return PlainTextResponse('Nenalezeno', status_code=404)
    cesta, nazev = s
    inline = os.path.splitext(nazev)[1].lower() in _V_PROHLIZECI
    return FileResponse(cesta, filename=nazev, content_disposition_type='inline' if inline else 'attachment',
                        headers={'X-Content-Type-Options': 'nosniff'})


# =========================================================
# E-MAILY (blokující — volat přes _na_pozadi)
# =========================================================
def _url(nid=None) -> str:
    if not intranet_data.APP_URL:
        return ''
    return f'{intranet_data.APP_URL}/naklady_veletrh' + (f'?naklad={int(nid)}' if nid else '')


def _posli(prijemci, predmet, text, nid=None):
    odkaz = _url(nid)
    if odkaz:
        text = f'{text}\n\nOtevřít v portálu: {odkaz}'
    for p in dict.fromkeys(prijemci):
        if p and '@' in p:
            try:
                intranet_emaily.odesli_upozorneni_email(p, predmet, text)
            except Exception as e:
                print(f'[naklady_veletrh] e-mail {p}: {e}')


def _email(uid) -> str:
    r = _sql('SELECT email FROM user WHERE iduser = %s', (uid,), 'jeden')
    return (r or {}).get('email') or ''


def _shrnuti(n) -> str:
    radky = (('Náklad', f"{_kod(n['id'])} {n['nazev']}"), ('Kategorie', n['kategorie']),
             ('Dodavatel', n['dodavatel'] + (f", IČO {n['dodavatel_ico']}" if n['dodavatel_ico'] else '')),
             ('Částka', f"{_kc(n['castka_s_dph'], True)} s DPH ({_kc(n['castka_bez_dph'], True)} bez DPH, "
                        f"DPH {n['sazba_dph']} %)"),
             ('Splatnost', _datum(n['splatnost'])), ('Zdůvodnění', n['zduvodneni']),
             ('Přílohy', str(n['pocet_priloh'])), ('Zadal(a)', n['zadal_jmeno']))
    return '\n'.join(f'{k}: {v}' for k, v in radky if v)


def _notifikuj_odeslani(nid):
    n = _sql(_SELECT + ' WHERE n.id = %s', (nid,), 'jeden')
    if not n or n['stav'] != 'ceka':
        return
    autor = _email(n['zadal_id']).lower()
    # Jen držitelé práva Schvalovatel ('vse' záměrně ne); autor o svém nákladu nerozhoduje.
    prijemci = [e for e in intranet_data.ziskej_emaily_s_pravem(PRAVO_SCHVALOVATEL) if e and e.lower() != autor]
    _posli(prijemci, f"Náklad ke schválení — {n['nazev']} ({_kc(n['castka_s_dph'])})",
           f"{n['zadal_jmeno']} odeslal(a) ke schválení náklad veletrhu.\n\n{_shrnuti(n)}", nid)


def _notifikuj_rozhodnuti(nid, stav, komentar):
    n = _sql(_SELECT + ' WHERE n.id = %s', (nid,), 'jeden')
    vysledek = {'schvaleno': 'schválen', 'zamitnuto': 'zamítnut', 'vraceno': 'vrácen k doplnění'}.get(stav)
    if not n or not vysledek:
        return
    text = (f"Váš náklad {_kod(nid)} „{n['nazev']}“ ({_kc(n['castka_s_dph'], True)} s DPH) byl {vysledek}. "
            f"Rozhodl(a): {n['rozhodl_jmeno']}.")
    if komentar:
        text += f"\n{'Důvod' if stav == 'zamitnuto' else 'Komentář'}: {komentar}"
    if stav == 'vraceno':
        text += '\n\nDoplňte prosím náklad a odešlete ho znovu ke schválení.'
    _posli([_email(n['zadal_id'])], f"Náklad {vysledek} — {n['nazev']}", text, nid)


def _na_pozadi(fn, *args):
    """E-maily neblokují UI ani event loop."""
    background_tasks.create(asyncio.to_thread(fn, *args), name=f'naklady_veletrh.{fn.__name__}')


def _pracovni_den(d) -> bool:
    if d.weekday() >= 5:
        return False
    try:
        from intranet_obsah import je_statni_svatek     # až tady — intranet_obsah importuje tento modul
        return not je_statni_svatek(d)
    except Exception:
        return True


def _posli_pripominku(dnes):
    """Souhrn nákladů čekajících déle než PRIPOMINKA_PO pracovních dní, jednou za pracovní den.
    Den si zabere atomický UPDATE — víc procesů ani restart nepošle souhrn dvakrát."""
    if not _zapnuto() or not _pracovni_den(dnes):
        return
    inicializace_db()
    if not _sql("UPDATE naklady_veletrh_nastaveni SET hodnota = %s WHERE klic = 'pripominka' AND hodnota <> %s",
                (dnes.isoformat(), dnes.isoformat())):
        return
    cekajici = []
    for n in _sql(_SELECT + " WHERE n.stav = 'ceka' ORDER BY n.odeslano", vysledek='vse'):
        od = (n['odeslano'] or n['upraveno']).date()
        dni = sum(_pracovni_den(od + datetime.timedelta(days=i)) for i in range(1, (dnes - od).days))
        if dni >= PRIPOMINKA_PO:
            cekajici.append((n, dni))
    if not cekajici:
        return
    radky = '\n'.join(f"• {_kod(n['id'])} {n['nazev']} — {_kc(n['castka_s_dph'])}, zadal(a) {n['zadal_jmeno']}, "
                      f"odesláno {_datum(n['odeslano'])} (čeká {dni} prac. dní)" for n, dni in cekajici)
    _posli(intranet_data.ziskej_emaily_s_pravem(PRAVO_SCHVALOVATEL),
           f'Náklady veletrh: {_pocet(len(cekajici))} čeká na schválení déle než {PRIPOMINKA_PO} pracovní dny',
           f'Na schválení čekají déle než {PRIPOMINKA_PO} pracovní dny:\n\n{radky}')


async def _bg_pripominky():
    await asyncio.sleep(120)
    while True:
        try:
            ted = datetime.datetime.now()
            if ted.hour >= PRIPOMINKA_HODINA:
                await asyncio.to_thread(_posli_pripominku, ted.date())
        except Exception as e:
            print(f'[naklady_veletrh] připomínka: {e}')
        await asyncio.sleep(600)


app.on_startup(lambda: asyncio.create_task(_bg_pripominky()))


# =========================================================
# REPORTY — Excel a PDF pro vedení
# =========================================================
def _souhrny(naklady) -> dict:
    """Odeslané náklady (bez konceptů) podle stavu, zadavatelů a měsíců splatnosti."""
    stavy, autori, mesice = {}, {}, {}
    for n in naklady:
        if n['stav'] == 'koncept':
            continue
        castka = n['castka_s_dph']
        st = stavy.setdefault(n['stav'], [0, Decimal(0)])
        st[0] += 1
        st[1] += castka
        a = autori.setdefault(n['zadal_id'], [n['zadal_jmeno'] or '?', 0, Decimal(0), Decimal(0)])
        a[1] += 1
        if n['stav'] in CERPANO:
            a[2] += castka
            kdy = n['splatnost'] or (n['rozhodnuto'].date() if n['rozhodnuto'] else None)
            if kdy:
                m = mesice.setdefault(kdy.strftime('%Y-%m'), [0, Decimal(0)])
                m[0] += 1
                m[1] += castka
        elif n['stav'] == 'ceka':
            a[3] += castka
    return {'stavy': [(k, *stavy[k]) for k in _STAVY if k in stavy],
            'autori': sorted(autori.values(), key=lambda a: (-a[2], a[0])),
            'mesice': [(f'{_MESICE[int(m[5:]) - 1]} {m[:4]}', *v) for m, v in sorted(mesice.items())]}


_EXPORT = (('ID', 9), ('Název', 36), ('Kategorie', 24), ('Dodavatel', 26), ('IČO', 11), ('Bez DPH', 15),
           ('DPH %', 7), ('S DPH', 15), ('Stav', 18), ('Zadal(a)', 20), ('Vytvořeno', 16), ('Rozhodl(a)', 20),
           ('Rozhodnuto', 16), ('Splatnost', 12), ('Uhrazeno', 16), ('Zdůvodnění', 60))


def _xlsx(naklady, p) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = 'Náklady'
    ws.append([h for h, _ in _EXPORT])
    for n in naklady:
        ws.append([_kod(n['id']), n['nazev'], n['kategorie'], n['dodavatel'], n['dodavatel_ico'] or '',
                   n['castka_bez_dph'], n['sazba_dph'], n['castka_s_dph'], _STAVY[n['stav']][0],
                   n['zadal_jmeno'] or '', n['vytvoreno'], n['rozhodl_jmeno'] or '', n['rozhodnuto'],
                   n['splatnost'], n['uhrazeno'], n['zduvodneni']])
    s = wb.create_sheet('Rozpočet')
    s.append(['Kategorie', 'Rozpočet', 'Čerpáno', 'Čeká na schválení', 'Volné'])
    for k in p['kategorie']:
        s.append([k['nazev'], k['rozpocet'], k['cerpano'], k['ceka'], k['volne']])
    s.append(['Součet kategorií', p['rozdeleno'], p['cerpano'], p['ceka'], p['rozdeleno'] - p['cerpano'] - p['ceka']])
    s.append(['Celkový rozpočet akce', p['celkem'], p['cerpano'], p['ceka'], p['volne']])
    kc = '#,##0.00 "Kč"'
    for list_, sirky in ((ws, [w for _, w in _EXPORT]), (s, (30, 16, 16, 18, 16))):
        for cell in list_[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='1B2430')
        for i, w in enumerate(sirky, 1):
            list_.column_dimensions[get_column_letter(i)].width = w
        list_.freeze_panes = 'A2'
    for radek in ws.iter_rows(min_row=2):
        radek[5].number_format = radek[7].number_format = kc
        for i in (10, 12, 14):
            radek[i].number_format = 'dd.mm.yyyy hh:mm'
        radek[13].number_format = 'dd.mm.yyyy'
    ws.auto_filter.ref = ws.dimensions
    for radek in s.iter_rows(min_row=2, min_col=2, max_col=5):
        for cell in radek:
            cell.number_format = kc
    for cell in s[s.max_row - 1] + s[s.max_row]:
        cell.font = Font(bold=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _varovani(p) -> list[tuple[str, str]]:
    """Upozornění rozpočtu: (text, 'red' | 'amber')."""
    out = []
    pres = p['cerpano'] + p['ceka'] - p['celkem']
    if pres > 0:
        out.append((f'Čerpání včetně čekajících převyšuje celkový rozpočet o {_kc(pres)}.', 'red'))
    elif p['podil'] > VAROVANI_CELKEM:
        out.append((f"Čerpání celého rozpočtu včetně čekajících je {_pct(p['podil'])} "
                    f'(varování nad {_pct(VAROVANI_CELKEM)}).', 'amber'))
    for k in p['kategorie']:
        if k['prekroceno']:
            out.append((f"Kategorie „{k['nazev']}“ je překročena o {_kc(-k['volne'])}.", 'red'))
        elif k['varovani']:
            out.append((f"Kategorie „{k['nazev']}“ je vyčerpána z {_pct(k['podil'])} (včetně čekajících).", 'amber'))
    return out


def _svg_kolac(kategorie) -> str:
    polozky = [(k['barva'], float(k['cerpano'])) for k in kategorie if k['cerpano'] > 0]
    celkem = sum(v for _, v in polozky)
    if not celkem:
        return '<p class="muted">Zatím nic schváleno.</p>'
    obvod, posun, kruhy = 2 * math.pi * 60, 0.0, []
    for barva, v in polozky:
        delka = v / celkem * obvod
        kruhy.append(f'<circle r="60" cx="80" cy="80" fill="none" stroke="{html.escape(barva)}" stroke-width="28" '
                     f'stroke-dasharray="{delka:.2f} {obvod - delka:.2f}" stroke-dashoffset="{-posun:.2f}" '
                     'transform="rotate(-90 80 80)"/>')
        posun += delka
    return f'<svg width="160" height="160" viewBox="0 0 160 160">{"".join(kruhy)}</svg>'


def _svg_cara(g, limit, w=600, h=230) -> str:
    plan, skut = g['plan'], g['skutecnost']
    maxv = max([float(limit)] + plan + [v for v in skut if v is not None]) or 1.0
    n = len(plan)

    def x(i):
        return 60 + i * (w - 80) / max(n - 1, 1)

    def y(v):
        return h - 30 - v / maxv * (h - 50)

    def cara(body, barva, styl=''):
        if not body:
            return ''
        if len(body) == 1:
            i, v = body[0]
            return f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" fill="{barva}"/>'
        pts = ' '.join(f'{x(i):.1f},{y(v):.1f}' for i, v in body)
        return f'<polyline points="{pts}" fill="none" stroke="{barva}" stroke-width="2" {styl}/>'

    osa = ''.join(f'<line x1="60" x2="{w - 20}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e5e7eb"/>'
                  f'<text x="54" y="{y(v) + 3:.1f}" font-size="9" text-anchor="end" fill="#6b7280">'
                  f'{_kc(v / 1000).replace(" Kč", "")} tis.</text>' for v in (0, maxv / 2, maxv))
    krok = 1 if n <= 12 else 2
    popisky = ''.join(f'<text x="{x(i):.1f}" y="{h - 12}" font-size="9" text-anchor="middle" fill="#6b7280">'
                      f'{html.escape(t)}</text>' for i, t in enumerate(g['popisky']) if i % krok == 0)
    ly = y(float(limit))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">{osa}{popisky}'
            f'<line x1="60" x2="{w - 20}" y1="{ly:.1f}" y2="{ly:.1f}" stroke="#dc2626" stroke-dasharray="6 4"/>'
            f'<text x="{w - 22}" y="{ly - 4:.1f}" font-size="9" text-anchor="end" fill="#dc2626">limit</text>'
            + cara(list(enumerate(plan)), '#64748b', 'stroke-dasharray="5 4"')
            + cara([(i, v) for i, v in enumerate(skut) if v is not None], _TENTO_BARVA) + '</svg>')


_CSS_REPORT = '''
body { font-family: Arial, sans-serif; font-size: 9.5pt; color: #111827; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 2px; } h2 { font-size: 11.5pt; margin: 14px 0 6px; }
.muted { color: #6b7280; font-size: 8.5pt; margin: 0; }
.kpi { display: flex; gap: 10px; margin: 10px 0; }
.kpi div { flex: 1; border: 1px solid #e5e7eb; border-radius: 6px; padding: 7px 10px; }
.kpi span { display: block; color: #6b7280; font-size: 7.5pt; text-transform: uppercase; }
.kpi b { display: block; font-size: 14pt; } .kpi small { color: #6b7280; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid #e5e7eb; padding: 3px 6px; text-align: left; font-size: 8.5pt; vertical-align: top; }
th { background: #f3f4f6; } .r { text-align: right; white-space: nowrap; } tr { page-break-inside: avoid; }
.bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: #e5e7eb; min-width: 110px; }
.bar i { display: block; height: 100%; }
.tecka { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
.grafy { display: flex; gap: 30px; align-items: flex-start; } .vedle { display: flex; gap: 24px; }
.vedle > div { flex: 1; } .warn { color: #b45309; margin: 2px 0; } .err { color: #b91c1c; margin: 2px 0; }
'''


def _report_html(d, kdo) -> str:
    """PDF report pro vedení: ukazatele, grafy, kategorie, zadavatelé, měsíce, seznam nákladů."""
    e = html.escape
    p = d['prehled']
    nast = p['nastaveni']
    sou = _souhrny(d['naklady'])

    def tab(hlavicka, radky, vpravo=()):
        th = ''.join(f'<th{" class=r" if i in vpravo else ""}>{e(h)}</th>' for i, h in enumerate(hlavicka))
        tr = ''.join('<tr>' + ''.join(f'<td{" class=r" if i in vpravo else ""}>{v}</td>' for i, v in enumerate(r))
                     + '</tr>' for r in radky)
        return f'<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'

    def pruh(casti, celek):
        celek, zbyva, divy = float(celek) or 1.0, 100.0, []
        for hodnota, barva in casti:
            sirka = max(0.0, min(zbyva, float(hodnota) / celek * 100))
            zbyva -= sirka
            divy.append(f'<i style="width:{sirka:.2f}%;background:{barva}"></i>')
        return f'<div class="bar">{"".join(divy)}</div>'

    ceka_n = sum(1 for n in d['naklady'] if n['stav'] == 'ceka')
    kpi = ''.join(f'<div><span>{e(t)}</span><b>{v}</b><small>{e(pod)}</small></div>' for t, v, pod in (
        ('Celkový rozpočet', _kc(p['celkem']), f"v kategoriích {_kc(p['rozdeleno'])}"),
        ('Schváleno a čerpáno', _kc(p['cerpano']), f"{_pct(_podil(p['cerpano'], p['celkem']))} rozpočtu"),
        ('Čeká na schválení', _kc(p['ceka']), _pocet(ceka_n)),
        ('Zbývá k dispozici', _kc(p['volne']), 'po odečtení čekajících')))
    varovani = ''.join(f'<p class="{"err" if typ == "red" else "warn"}">⚠ {e(t)}</p>' for t, typ in _varovani(p))
    legenda = ''.join(f'<div><span class="tecka" style="background:{e(k["barva"])}"></span>{e(k["nazev"])} '
                      f'— {_kc(k["cerpano"])}</div>' for k in p['kategorie'] if k['cerpano'] > 0)
    kategorie = tab(('Kategorie', 'Rozpočet', 'Čerpáno', 'Čeká', 'Volné', 'Čerpání', ''), [
        (f'<span class="tecka" style="background:{e(k["barva"])}"></span>{e(k["nazev"])}', _kc(k['rozpocet']),
         _kc(k['cerpano']), _kc(k['ceka']), _kc(k['volne']), _pct(k['podil']),
         pruh([(k['cerpano'], _CERPANO_BARVA), (k['ceka'], _CEKA_BARVA)], k['rozpocet']))
        for k in p['kategorie']] + [(
            '<b>Součet kategorií</b>', f"<b>{_kc(p['rozdeleno'])}</b>", f"<b>{_kc(p['cerpano'])}</b>",
            f"<b>{_kc(p['ceka'])}</b>", f"<b>{_kc(p['rozdeleno'] - p['cerpano'] - p['ceka'])}</b>", '', '')],
        vpravo=(1, 2, 3, 4, 5))
    autori = tab(('Zadavatel', 'Nákladů', 'Schváleno a uhrazeno', 'Čeká'),
                 [(e(j), c, _kc(s), _kc(w)) for j, c, s, w in sou['autori']], (1, 2, 3))
    mesice = tab(('Měsíc splatnosti', 'Nákladů', 'Schváleno a uhrazeno'),
                 [(e(m), c, _kc(s)) for m, c, s in sou['mesice']], (1, 2))
    seznam = tab(('ID', 'Název', 'Kategorie', 'Dodavatel', 'Zadal(a)', 'S DPH', 'Splatnost', 'Stav'), [
        (_kod(n['id']), e(n['nazev']), e(n['kategorie']), e(n['dodavatel']), e(n['zadal_jmeno'] or ''),
         _kc(n['castka_s_dph'], True), _datum(n['splatnost']), _STAVY[n['stav']][0])
        for n in sorted(d['naklady'], key=lambda n: n['id']) if n['stav'] != 'koncept'], (5,))
    hlavicka = ' · '.join(x for x in (f'Stav k {_cas(datetime.datetime.now())}', f'vytiskl(a) {kdo}',
                                       _do_veletrhu(nast)) if x)
    return f'''<!doctype html><html lang="cs"><head><meta charset="utf-8"><style>{_CSS_REPORT}</style></head><body>
<h1>Náklady veletrhu — {e(nast['akce'])}</h1><p class="muted">{e(hlavicka)}</p>
<div class="kpi">{kpi}</div>
{pruh([(p['cerpano'], _CERPANO_BARVA), (p['ceka'], _CEKA_BARVA)], p['celkem'])}
<p class="muted">Čerpáno {_kc(p['cerpano'])} · čeká {_kc(p['ceka'])} · volné {_kc(max(p['volne'], 0))}</p>
{varovani}
<div class="grafy"><div><h2>Struktura čerpání</h2>{_svg_kolac(p['kategorie'])}<div>{legenda}</div></div>
<div><h2>Kumulativní čerpání vs. plán</h2>{_svg_cara(d['graf'], p['celkem'])}
<p class="muted">Oranžová = skutečnost (podle měsíce schválení), šedá = rovnoměrný plán, červená = limit.</p></div></div>
<h2>Rozpočet vs. čerpání podle kategorií</h2>{kategorie}
<div class="vedle"><div><h2>Podle zadavatelů</h2>{autori}</div><div><h2>Podle měsíců splatnosti</h2>{mesice}</div></div>
<h2>Seznam nákladů</h2>{seznam}
</body></html>'''


# =========================================================
# UI
# =========================================================
def _odznak(stav):
    popis, barva = _STAVY.get(stav, (stav, 'grey'))
    ui.badge(popis, color=barva).props('rounded').classes('px-2 py-1 text-xs')


def _pruh(casti, celek, vyska=12):
    """Vodorovný pruh z částí [(hodnota, barva)] vůči celku; přesah se ořízne."""
    celek, zbyva, divy = float(celek) or 1.0, 100.0, []
    for hodnota, barva in casti:
        sirka = max(0.0, min(zbyva, float(hodnota) / celek * 100))
        zbyva -= sirka
        divy.append(f'<div style="width:{sirka:.2f}%;background:{barva}"></div>')
    ui.html(f'<div style="display:flex;height:{vyska}px;border-radius:{vyska // 2}px;overflow:hidden;'
            f'background:{_VOLNE_BARVA}">{"".join(divy)}</div>', sanitize=False).classes('w-full')


def _kpi(nazev, hodnota, pod, barva='text-gray-900'):
    with ui.card().classes('p-4 gap-0 shadow-sm'):
        ui.label(nazev).classes('text-xs uppercase tracking-wide text-gray-500')
        ui.label(hodnota).classes(f'text-2xl font-bold {barva}')
        ui.label(pod).classes('text-xs text-gray-500')


def _hlaska(text, typ):
    barvy = {'red': 'bg-red-50 text-red-800 border-red-200', 'amber': 'bg-amber-50 text-amber-900 border-amber-200'}
    with ui.row().classes(f'w-full items-center gap-2 px-3 py-2 rounded border text-sm no-wrap {barvy[typ]}'):
        ui.icon('error' if typ == 'red' else 'warning').classes('text-lg')
        ui.label(text)


def _tecka(barva, kulata=False):
    ui.element('span').style(f'width:10px;height:10px;border-radius:{"50%" if kulata else "2px"};'
                             f'background:{barva};display:inline-block;flex-shrink:0')


def _datum_input(label, value=''):
    """Textové pole DD.MM.RRRR s kalendářem (lze i psát)."""
    inp = ui.input(label, value=value).props('outlined dense clearable').classes('w-full')
    with inp.add_slot('append'):
        ui.icon('event').classes('cursor-pointer text-blue-500').on('click', lambda: menu.open())
    with ui.menu().props('no-parent-event') as menu:
        ui.date(mask='DD.MM.YYYY').bind_value(inp).props('today-btn').classes('p-0')
    return inp


def _karta_tabulka(titulek, hlavicka, radky):
    with ui.card().classes('w-full p-4 gap-2 shadow-sm'):
        ui.label(titulek).classes('font-semibold')
        sloupce = [{'name': f'c{i}', 'label': h, 'field': f'c{i}', 'align': 'left' if i == 0 else 'right'}
                   for i, h in enumerate(hlavicka)]
        ui.table(columns=sloupce, rows=[{'id': j, **{f'c{i}': v for i, v in enumerate(r)}} for j, r in enumerate(radky)],
                 row_key='id', pagination=0) \
            .props('flat dense hide-bottom' if radky else 'flat dense no-data-label="Zatím žádné údaje"') \
            .classes('w-full')


def _udaje(n) -> list[tuple[str, str]]:
    return [r for r in (
        ('Kategorie', n['kategorie']),
        ('Dodavatel', n['dodavatel'] + (f", IČO {n['dodavatel_ico']}" if n['dodavatel_ico'] else '')),
        ('Částka bez DPH', _kc(n['castka_bez_dph'], True)),
        (f"DPH {n['sazba_dph']} %", _kc(n['castka_s_dph'] - n['castka_bez_dph'], True)),
        ('Splatnost', _datum(n['splatnost']) or '—'),
        ('Zadal(a)', f"{n['zadal_jmeno']} · {_cas(n['vytvoreno'])}"),
        ('Odesláno', _cas(n['odeslano'])) if n['odeslano'] and n['stav'] != 'koncept' else None,
        ('Vrátil(a)' if n['stav'] == 'vraceno' else 'Rozhodl(a)', f"{n['rozhodl_jmeno']} · {_cas(n['rozhodnuto'])}")
        if n['rozhodl_jmeno'] else None,
        ('Uhrazeno', _cas(n['uhrazeno'])) if n['uhrazeno'] else None,
    ) if r]


def _radek(n) -> dict:
    return {'id': n['id'], 'kod': _kod(n['id']), 'nazev': n['nazev'], 'kategorie': n['kategorie'],
            'dodavatel': n['dodavatel'], 'zadal': n['zadal_jmeno'] or '', 'castka': float(n['castka_s_dph']),
            'splatnost': n['splatnost'].isoformat() if n['splatnost'] else '',
            'stav': _STAVY[n['stav']][0], 'barva': _STAVY[n['stav']][1],
            'vytvoreno': n['vytvoreno'].strftime('%Y-%m-%d %H:%M'), 'prilohy': n['pocet_priloh']}


_FMT_DATUM = 'v => v ? v.slice(8, 10) + "." + v.slice(5, 7) + "." + v.slice(0, 4) : ""'
_SLOUPCE = [
    {'name': 'kod', 'label': 'ID', 'field': 'kod', 'sortable': True, 'align': 'left'},
    {'name': 'nazev', 'label': 'Název', 'field': 'nazev', 'sortable': True, 'align': 'left'},
    {'name': 'kategorie', 'label': 'Kategorie', 'field': 'kategorie', 'sortable': True, 'align': 'left'},
    {'name': 'dodavatel', 'label': 'Dodavatel', 'field': 'dodavatel', 'sortable': True, 'align': 'left'},
    {'name': 'zadal', 'label': 'Zadal(a)', 'field': 'zadal', 'sortable': True, 'align': 'left'},
    {'name': 'castka', 'label': 'S DPH', 'field': 'castka', 'sortable': True, 'align': 'right',
     ':format': 'v => Math.round(v).toLocaleString("cs-CZ") + " Kč"'},
    {'name': 'splatnost', 'label': 'Splatnost', 'field': 'splatnost', 'sortable': True, 'align': 'left',
     ':format': _FMT_DATUM},
    {'name': 'stav', 'label': 'Stav', 'field': 'stav', 'sortable': True, 'align': 'left'},
    {'name': 'vytvoreno', 'label': 'Zadáno', 'field': 'vytvoreno', 'sortable': True, 'align': 'left',
     ':format': _FMT_DATUM},
]
_SLOT_STAV = r'''<q-td :props="props"><q-badge rounded :color="props.row.barva" :label="props.value"
    class="q-px-sm q-py-xs" /></q-td>'''
_SLOT_NAZEV = r'''<q-td :props="props">{{ props.value }}<q-icon v-if="props.row.prilohy" name="attach_file"
    size="14px" class="text-grey-6 q-ml-xs" /></q-td>'''


async def vykresli(user_id, user_name, vsechna_prava):
    role = _role(vsechna_prava)
    zadavatel, schvalovatel, spravce = 'zadavatel' in role, 'schvalovatel' in role, 'spravce' in role
    try:
        deep = app.storage.user.pop(_DEEP, None)
    except Exception:
        deep = None
    try:
        await asyncio.to_thread(inicializace_db)
        data = await asyncio.to_thread(_nacti, user_id)
    except Exception as e:
        ui.label(f'Náklady veletrh se nepodařilo načíst: {e}').classes('text-red-600')
        return

    s = {'data': data, 'vybrany': None,
         'filtr': {'q': '', 'stav': None, 'kategorie': None, 'zadal': None, 'od': '', 'do': '',
                   'min': '', 'max': '', 'moje': False}}
    otevrene = []       # překreslení otevřených detailů (dialogy) po obnově dat
    host = ui.element('div')    # dialogy žijí mimo obnovované části — refresh je nezavře

    async def obnov():
        try:
            s['data'] = await asyncio.to_thread(_nacti, user_id)
        except Exception as e:
            ui.notify(f'Data se nepodařilo načíst: {e}', type='negative')
            return
        for fn in (hlavicka, odznak_fronty, prehled, seznam, schvaleni, rozpocet, reporty):
            fn.refresh()
        for kresli in list(otevrene):
            kresli()

    # ---------- akce ----------
    async def proved(nid, akce, komentar=''):
        try:
            await asyncio.to_thread(_proved, user_id, user_name, role, nid, akce, komentar)
        except Chyba as e:
            ui.notify(str(e), type='warning', multi_line=True)
            await obnov()       # stav se mohl mezitím změnit — ukázat aktuální
            return
        except Exception as e:
            ui.notify(f'Akci se nepodařilo provést: {e}', type='negative')
            return
        ui.notify(_HLASKY[akce], type='positive')
        intranet_logger.log_activity(user_name, _LOG, f'{_kod(nid)}: {_HLASKY[akce]}')
        if akce == 'odeslat':
            _na_pozadi(_notifikuj_odeslani, nid)
        elif akce in _ROZHODNUTI:
            _na_pozadi(_notifikuj_rozhodnuti, nid, _ROZHODNUTI[akce][0], (komentar or '').strip())
        await obnov()

    async def rozhodnuti(n, akce):
        titulek, popis, tlacitko, barva = {
            'schvalit': ('Schválit náklad', 'Komentář (nepovinný)', 'Schválit', 'green-7'),
            'vratit': ('Vrátit k doplnění', 'Co je potřeba doplnit *', 'Vrátit', 'orange-9'),
            'zamitnout': ('Zamítnout náklad', 'Důvod zamítnutí *', 'Zamítnout', 'red-7'),
            'uhrazeno': ('Označit jako uhrazené', 'Poznámka (nepovinná)', 'Označit', 'teal-7'),
        }[akce]
        povinny = akce in ('vratit', 'zamitnout')
        with host, ui.dialog() as dlg, ui.card().classes('w-[480px] max-w-[95vw] gap-3'):
            ui.label(titulek).classes('text-lg font-semibold')
            ui.label(f"{_kod(n['id'])} {n['nazev']} — {_kc(n['castka_s_dph'], True)}").classes('text-sm text-gray-600')
            txt = ui.textarea(popis).props('outlined autogrow autofocus').classes('w-full')

            def potvrdit():
                if povinny and not (txt.value or '').strip():
                    txt.props('error error-message="Vyplňte prosím komentář."')
                    return
                dlg.submit((txt.value or '').strip())

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zpět', on_click=lambda: dlg.submit(None)).props('flat')
                ui.button(tlacitko, on_click=potvrdit).props(f'unelevated color={barva}')
        vysledek = await dlg
        dlg.delete()
        if vysledek is not None:
            await proved(n['id'], akce, vysledek)

    async def export_xlsx(radky):
        try:
            obsah = await asyncio.to_thread(_xlsx, radky, s['data']['prehled'])
        except Exception as e:
            ui.notify(f'Export se nepodařil: {e}', type='negative')
            return
        ui.download.content(obsah, f'naklady_veletrh_{datetime.date.today().isoformat()}.xlsx')
        intranet_logger.log_activity(user_name, _LOG, f'Export do Excelu ({_pocet(len(radky))})')

    async def export_pdf():
        import intranet_gastrokurzy     # sdílený převod HTML → PDF (Playwright, jedno Chromium naráz)
        d = s['data']
        cekej = ui.notification('Připravuji PDF report…', spinner=True, timeout=None)
        try:
            pdf = await intranet_gastrokurzy._render_pdf(
                _report_html(d, user_name), f"{d['prehled']['nastaveni']['akce']} — náklady")
        except Exception as e:
            ui.notify(f'PDF se nepodařilo vytvořit: {e}', type='negative')
            return
        finally:
            cekej.dismiss()
        ui.download.content(pdf, f'naklady_veletrh_report_{datetime.date.today().isoformat()}.pdf',
                            'application/pdf')
        intranet_logger.log_activity(user_name, _LOG, 'PDF report pro vedení')

    # ---------- detail nákladu ----------
    def dopad_ui(n, dp):
        kat = dp['kat']
        with ui.card().classes('w-full p-4 gap-2 shadow-none border border-gray-200'):
            ui.label(f"Dopad na rozpočet kategorie „{kat['nazev']}“").classes('font-semibold')
            _pruh([(kat['cerpano'], _CERPANO_BARVA)] + ([(dp['castka'], _TENTO_BARVA)] if dp['otevreny'] else []),
                  kat['rozpocet'], 14)
            with ui.row().classes('w-full justify-between text-sm gap-2'):
                ui.label(f"Čerpáno {_kc(kat['cerpano'])} z {_kc(kat['rozpocet'])}")
                if dp['otevreny']:
                    ui.label(f"Tento náklad {_kc(dp['castka'])}").classes('text-orange-700 font-medium')
                    ui.label(f"Po schválení zbývá {_kc(dp['po'])} ({_pct(dp['po_podil'])})")
            if dp['ostatni_ceka'] > 0:
                ui.label(f"V kategorii čekají na schválení další náklady za {_kc(dp['ostatni_ceka'])}.") \
                    .classes('text-xs text-gray-500')
            if dp['otevreny']:
                ui.label(f"Celý rozpočet veletrhu: po schválení zbývá {_kc(dp['celkem_po'])} "
                         f"z {_kc(s['data']['prehled']['celkem'])}.").classes('text-sm text-gray-700')
            if dp['blokovano']:
                _hlaska('Schválením by byl překročen celkový rozpočet veletrhu. Nejprve je nutné navýšit rozpočet.',
                        'red')
            elif dp['otevreny'] and dp['po'] < 0:
                _hlaska(f"Schválením bude překročen rozpočet kategorie o {_kc(-dp['po'])}. "
                        'Celkový rozpočet zůstane v limitu.', 'red')
            elif dp['otevreny'] and dp['po_podil'] < 1 - VAROVANI_KATEGORIE:
                _hlaska(f'Po schválení klesne volný rozpočet kategorie pod {_pct(1 - VAROVANI_KATEGORIE)}.', 'amber')

    def akce_ui(n, vlastni, dp):
        nid, stav = n['id'], n['stav']
        with ui.row().classes('w-full gap-2 items-center'):
            if vlastni and zadavatel and stav in OTEVRENE:
                ui.button('Upravit', icon='edit', on_click=lambda: formular(n)).props('outline color=grey-8')
            if vlastni and zadavatel and stav in ('koncept', 'vraceno'):
                ui.button('Odeslat ke schválení', icon='send', on_click=lambda: proved(nid, 'odeslat')) \
                    .props('unelevated color=orange-8')
            if vlastni and stav == 'ceka':
                ui.button('Stáhnout zpět', icon='undo', on_click=lambda: proved(nid, 'stahnout')) \
                    .props('outline color=grey-8').tooltip('Vrátí náklad do konceptu a zmizí z fronty ke schválení.')
            if schvalovatel and stav == 'ceka':
                if vlastni:
                    ui.label('O vlastním nákladu rozhoduje jiný schvalovatel.').classes('text-sm text-gray-500')
                else:
                    b = ui.button('Schválit', icon='check', on_click=lambda: rozhodnuti(n, 'schvalit')) \
                        .props('unelevated color=green-7')
                    b.set_enabled(not (dp and dp['blokovano']))
                    ui.button('Vrátit k doplnění', icon='reply', on_click=lambda: rozhodnuti(n, 'vratit')) \
                        .props('outline color=orange-9')
                    ui.button('Zamítnout', icon='block', on_click=lambda: rozhodnuti(n, 'zamitnout')) \
                        .props('outline color=red-7')
            if spravce and stav == 'schvaleno':
                ui.button('Označit jako uhrazené', icon='paid', on_click=lambda: rozhodnuti(n, 'uhrazeno')) \
                    .props('outline color=teal-7')

    def historie_ui(nid):
        ui.label('Historie a komentáře').classes('font-semibold')
        with ui.column().classes('w-full gap-2'):
            for r in s['data']['historie'].get(nid, []):
                with ui.row().classes('w-full gap-3 items-start no-wrap'):
                    ui.icon('chat_bubble_outline' if r['akce'] == 'Komentář' else 'history') \
                        .classes('text-gray-400 text-lg mt-0.5')
                    with ui.column().classes('gap-0 flex-1 min-w-0'):
                        with ui.row().classes('gap-2 items-baseline'):
                            ui.label(r['akce']).classes('text-sm font-medium')
                            ui.label(f"{r['kdo'] or ''} · {_cas(r['cas'])}").classes('text-xs text-gray-500')
                        if r['komentar']:
                            ui.label(r['komentar']) \
                                .classes('text-sm text-gray-700 whitespace-pre-line bg-gray-50 rounded px-2 py-1 mt-1')
        if role & {'zadavatel', 'spravce'}:
            with ui.row().classes('w-full items-end gap-2 no-wrap'):
                txt = ui.textarea(placeholder='Napsat komentář…').props('outlined dense autogrow').classes('flex-1')

                async def poslat():
                    if (txt.value or '').strip():
                        await proved(nid, 'komentar', txt.value)

                ui.button(icon='send', on_click=poslat).props('round unelevated color=orange-8') \
                    .tooltip('Přidat komentář')

    def detail_obsah(nid):
        d = s['data']
        n = d['podle_id'].get(nid)
        if not n:
            ui.label('Náklad nenalezen, nebo k němu nemáte přístup.').classes('text-gray-500 py-6')
            return
        dp = _dopad(n, d['prehled'])
        with ui.row().classes('w-full justify-between items-start gap-4'):
            with ui.column().classes('gap-1'):
                ui.label(_kod(nid)).classes('text-xs text-gray-500')
                ui.label(n['nazev']).classes('text-xl font-bold text-gray-900')
                _odznak(n['stav'])
            with ui.column().classes('items-end gap-0'):
                ui.label('Částka s DPH').classes('text-xs text-gray-500')
                ui.label(_kc(n['castka_s_dph'], True)).classes('text-2xl font-bold text-gray-900')
        with ui.row().classes('w-full gap-8 items-start'):
            with ui.grid(columns='auto 1fr').classes('gap-x-4 gap-y-1 text-sm flex-1 min-w-[300px]'):
                for popis, hodnota in _udaje(n):
                    ui.label(popis).classes('text-gray-500')
                    ui.label(hodnota).classes('font-medium')
            with ui.column().classes('gap-1 flex-1 min-w-[260px]'):
                ui.label('Zdůvodnění').classes('text-xs text-gray-500')
                ui.label(n['zduvodneni']).classes('text-sm whitespace-pre-line')
                ui.label('Přílohy').classes('text-xs text-gray-500 mt-3')
                for pr in d['prilohy'].get(nid, []):
                    ui.link(f"📄 {pr['nazev']}", f"/naklady_veletrh/priloha/{pr['id']}", new_tab=True) \
                        .classes('text-sm')
                if not d['prilohy'].get(nid):
                    ui.label('Bez příloh').classes('text-sm text-gray-400')
        if dp:
            dopad_ui(n, dp)
        akce_ui(n, n['zadal_id'] == user_id, dp)
        historie_ui(nid)

    def detail(nid):
        with host, ui.dialog() as dlg, ui.card().classes('w-[1000px] max-w-[96vw] p-0 gap-0'):
            with ui.row().classes('w-full justify-end px-2 pt-2'):
                ui.button(icon='close', on_click=dlg.close).props('flat round dense')
            obsah = ui.column().classes('w-full px-6 pb-6 gap-4')

        def kresli():
            obsah.clear()
            with obsah:
                detail_obsah(nid)

        def zavreno():
            if kresli in otevrene:
                otevrene.remove(kresli)
            dlg.delete()

        kresli()
        otevrene.append(kresli)
        dlg.on('hide', zavreno)
        dlg.open()

    # ---------- formulář nového / upravovaného nákladu ----------
    def formular(n=None):
        p = s['data']['prehled']
        kategorie = {k['id']: k for k in p['kategorie']}
        stav = n['stav'] if n else 'koncept'
        existujici = s['data']['prilohy'].get(n['id'], []) if n else []
        cekajici = []       # nahrané, zatím neuložené přílohy: (název, disk, velikost)

        def prepocet():
            bez = _castka(castka.value or '')
            hruba = _s_dph(bez, sazba.value) if bez and bez > 0 and sazba.value in DPH_SAZBY else None
            s_dph.text = f'S DPH: {_kc(hruba, True)}' if hruba is not None else 'S DPH: —'
            info.clear()
            with info:
                k = kategorie.get(kat.value)
                if k:
                    volne = k['rozpocet'] - k['cerpano'] - k['ceka']
                    if n and n['stav'] == 'ceka' and n['kategorie_id'] == k['id']:
                        volne += n['castka_s_dph']      # vlastní rezervace se nepočítá dvakrát
                    text = f"V kategorii „{k['nazev']}“ zbývá {_kc(volne)} z {_kc(k['rozpocet'])}"
                    po = volne - hruba if hruba is not None else None
                    if po is not None and po < 0:
                        _hlaska(f'{text}. Náklad by rozpočet kategorie překročil o {_kc(-po)}.', 'red')
                    elif po is not None and _podil(po, k['rozpocet']) < 1 - VAROVANI_KATEGORIE:
                        _hlaska(f'{text}, po tomto nákladu {_kc(po)} — pod {_pct(1 - VAROVANI_KATEGORIE)}.', 'amber')
                    else:
                        ui.label(f'{text}' + (f', po tomto nákladu {_kc(po)}.' if po is not None else '.')) \
                            .classes('text-sm text-gray-600')
                if hruba is not None and hruba >= POVINNA_PRILOHA_OD and not (existujici or cekajici):
                    _hlaska(f'Od {_kc(POVINNA_PRILOHA_OD)} s DPH je povinná příloha (nabídka nebo faktura) '
                            '— bez ní náklad ke schválení odeslat nepůjde.', 'amber')

        def kresli_cekajici():
            cekajici_box.clear()
            with cekajici_box:
                for polozka in cekajici:
                    ui.chip(polozka[0][:60], icon='attach_file', removable=True,
                            on_value_change=lambda _, x=polozka: odeber(x)).props('dense outline color=orange-9')

        def odeber(polozka):
            if polozka in cekajici:
                cekajici.remove(polozka)
                _smaz_soubory([polozka[1]])
            kresli_cekajici()
            prepocet()

        async def nahrano(e):
            for f in e.files:
                try:
                    cekajici.append(await _uloz_soubor(f))
                except Chyba as ex:
                    ui.notify(str(ex), type='warning')
                except Exception as ex:
                    ui.notify(f'Přílohu se nepodařilo uložit: {ex}', type='negative')
            upl.reset()
            kresli_cekajici()
            prepocet()

        def zrusit():
            _smaz_soubory([x[1] for x in cekajici])
            cekajici.clear()
            dlg.close()

        async def ulozit(odeslat):
            data, chyby = _zpracuj({'nazev': nazev.value, 'kategorie_id': kat.value, 'dodavatel': dodavatel.value,
                                    'ico': ico.value, 'castka': castka.value, 'sazba': sazba.value,
                                    'splatnost': splatnost.value, 'zduvodneni': zduvodneni.value})
            if chyby:
                ui.notify(' '.join(chyby), type='warning', multi_line=True)
                return
            try:
                nid, chyby = await asyncio.to_thread(_uloz, user_id, user_name, n['id'] if n else None,
                                                     data, list(cekajici), odeslat)
            except Chyba as e:
                ui.notify(str(e), type='warning', multi_line=True)
                return
            except Exception as e:
                ui.notify(f'Náklad se nepodařilo uložit: {e}', type='negative')
                return
            cekajici.clear()        # soubory už patří uloženému nákladu
            dlg.close()
            odeslano = odeslat and not chyby and stav != 'ceka'
            if chyby and odeslat:
                ui.notify('Náklad je uložený, ale ke schválení zatím odeslat nejde: ' + ' '.join(chyby),
                          type='warning', multi_line=True, timeout=10000)
            else:
                ui.notify('Náklad odeslán ke schválení.' if odeslano else 'Náklad uložen.', type='positive')
            intranet_logger.log_activity(user_name, _LOG, f"{_kod(nid)}: {'upraven' if n else 'nový'} náklad "
                                         f"„{data['nazev']}“" + (' — odeslán ke schválení' if odeslano else ''))
            if odeslano:
                _na_pozadi(_notifikuj_odeslani, nid)
            await obnov()
            if not n:
                detail(nid)

        with host, ui.dialog().props('persistent') as dlg, ui.card().classes('w-[780px] max-w-[96vw] gap-3'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label(f"Upravit náklad {_kod(n['id'])}" if n else 'Nový náklad').classes('text-lg font-semibold')
                if n:
                    _odznak(stav)
            nazev = ui.input('Název / popis *', value=n['nazev'] if n else '',
                             placeholder='např. Výroba LED stěny stánku') \
                .props('outlined dense maxlength=255').classes('w-full')
            with ui.row().classes('w-full gap-3 no-wrap'):
                kat = ui.select({k['id']: k['nazev'] for k in p['kategorie']}, label='Kategorie *',
                                value=n['kategorie_id'] if n and n['kategorie_id'] in kategorie else None) \
                    .props('outlined dense').classes('flex-1')
                with ui.element('div').classes('w-48'):
                    splatnost = _datum_input('Datum splatnosti', _datum(n['splatnost']) if n else '')
            with ui.row().classes('w-full gap-3 no-wrap'):
                dodavatel = ui.input('Dodavatel *', value=n['dodavatel'] if n else '') \
                    .props('outlined dense maxlength=255').classes('flex-1')
                ico = ui.input('IČO', value=(n['dodavatel_ico'] or '') if n else '') \
                    .props('outlined dense maxlength=12').classes('w-40')
            with ui.row().classes('w-full gap-3 items-center'):
                castka = ui.input('Částka bez DPH (Kč) *', value=_cislo_text(n['castka_bez_dph']) if n else '') \
                    .props('outlined dense inputmode=decimal').classes('w-56')
                sazba = ui.toggle({x: f'{x} %' for x in DPH_SAZBY}, value=n['sazba_dph'] if n else DPH_SAZBY[0]) \
                    .props('no-caps unelevated toggle-color=orange-8')
                s_dph = ui.label().classes('text-sm font-medium text-gray-700')
            zduvodneni = ui.textarea('Zdůvodnění *', value=n['zduvodneni'] if n else '',
                                     placeholder='Proč je náklad potřeba, případně porovnání nabídek') \
                .props('outlined autogrow maxlength=5000').classes('w-full')
            info = ui.column().classes('w-full gap-1')
            ui.label('Přílohy — nabídka, objednávka, faktura (PDF, obrázek, Excel, Word; max 20 MB)') \
                .classes('text-sm text-gray-600')
            for pr in existujici:
                ui.link(f"📄 {pr['nazev']}", f"/naklady_veletrh/priloha/{pr['id']}", new_tab=True).classes('text-sm')
            cekajici_box = ui.row().classes('w-full gap-1')
            upl = ui.upload(on_multi_upload=nahrano, auto_upload=True, multiple=True, max_file_size=MAX_PRILOHA,
                            on_rejected=lambda: ui.notify('Soubor je větší než 20 MB.', type='warning')) \
                .props(f'accept="{",".join(PRILOHY)}" flat bordered color=grey-7 label="Přidat přílohy"') \
                .classes('w-full')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=zrusit).props('flat')
                ui.button('Uložit změny' if stav == 'ceka' else 'Uložit koncept', on_click=lambda: ulozit(False)) \
                    .props('outline color=grey-8')
                if stav != 'ceka':
                    ui.button('Odeslat ke schválení', icon='send', on_click=lambda: ulozit(True)) \
                        .props('unelevated color=orange-8')
        for prvek in (castka, sazba, kat):
            prvek.on_value_change(prepocet)
        prepocet()
        dlg.on('hide', dlg.delete)
        dlg.open()

    # ---------- tabulka nákladů ----------
    def tabulka(radky, strankovani=True):
        t = ui.table(columns=_SLOUPCE, rows=[_radek(n) for n in radky], row_key='id',
                     pagination={'rowsPerPage': 25, 'sortBy': 'vytvoreno', 'descending': True} if strankovani else 0) \
            .props('flat dense bordered no-data-label="Žádné náklady"').classes('w-full cursor-pointer')
        if not strankovani:
            t.props('hide-bottom')
        t.add_slot('body-cell-stav', _SLOT_STAV)
        t.add_slot('body-cell-nazev', _SLOT_NAZEV)
        t.on('rowClick', lambda e: detail(e.args[1]['id']))
        return t

    # ---------- obnovované části ----------
    @ui.refreshable
    def hlavicka():
        nast = s['data']['prehled']['nastaveni']
        with ui.row().classes('w-full items-center justify-between gap-2'):
            with ui.column().classes('gap-0'):
                ui.label('🧾 Náklady veletrh').classes('text-2xl font-bold text-gray-800')
                ui.label(' · '.join(x for x in (nast['akce'], _do_veletrhu(nast)) if x)).classes('text-sm text-gray-500')
            with ui.row().classes('gap-2 items-center'):
                ui.button(icon='refresh', on_click=obnov).props('flat round color=grey-7').tooltip('Načíst znovu')
                if zadavatel:
                    ui.button('Nový náklad', icon='add', on_click=lambda: formular(None)) \
                        .props('unelevated color=orange-8')

    @ui.refreshable
    def odznak_fronty():
        pocet = sum(1 for n in s['data']['naklady'] if n['stav'] == 'ceka' and n['zadal_id'] != user_id)
        if pocet:
            ui.badge(str(pocet), color='red').props('floating')

    @ui.refreshable
    def prehled():
        d = s['data']
        p = d['prehled']
        ceka_n = sum(1 for n in d['naklady'] if n['stav'] == 'ceka')
        with ui.grid().classes('w-full gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4'):
            _kpi('Celkový rozpočet', _kc(p['celkem']), f"v kategoriích {_kc(p['rozdeleno'])}")
            _kpi('Schváleno a čerpáno', _kc(p['cerpano']), f"{_pct(_podil(p['cerpano'], p['celkem']))} rozpočtu")
            _kpi('Čeká na schválení', _kc(p['ceka']), _pocet(ceka_n), 'text-amber-700')
            _kpi('Zbývá k dispozici', _kc(p['volne']), 'po odečtení čekajících',
                 'text-red-700' if p['volne'] < 0 else 'text-green-700')
        with ui.card().classes('w-full p-4 gap-2 shadow-sm'):
            with ui.row().classes('w-full justify-between items-baseline'):
                ui.label('Odpočet rozpočtu').classes('font-semibold')
                ui.label(_do_veletrhu(p['nastaveni'])).classes('text-sm text-gray-600')
            _pruh([(p['cerpano'], _CERPANO_BARVA), (p['ceka'], _CEKA_BARVA)], p['celkem'], 16)
            with ui.row().classes('gap-4 text-xs text-gray-600'):
                for barva, text in ((_CERPANO_BARVA, f"Čerpáno {_kc(p['cerpano'])}"),
                                    (_CEKA_BARVA, f"Čeká {_kc(p['ceka'])}"),
                                    (_VOLNE_BARVA, f"Volné {_kc(max(p['volne'], 0))}")):
                    with ui.row().classes('items-center gap-1'):
                        _tecka(barva)
                        ui.label(text)
            for text, typ in _varovani(p):
                _hlaska(text, typ)
        with ui.grid().classes('w-full gap-3 grid-cols-1 lg:grid-cols-5'):
            with ui.card().classes('p-4 gap-1 shadow-sm lg:col-span-2'):
                ui.label('Struktura čerpání').classes('font-semibold')
                kolac = [{'name': k['nazev'], 'value': float(k['cerpano']), 'itemStyle': {'color': k['barva']}}
                         for k in p['kategorie'] if k['cerpano'] > 0]
                if kolac:
                    ui.echart({
                        'tooltip': {'trigger': 'item', ':formatter': 'p => p.name + "<br>" + '
                                    'Math.round(p.value).toLocaleString("cs-CZ") + " Kč (" + p.percent + " %)"'},
                        'legend': {'type': 'scroll', 'bottom': 0},
                        'series': [{'type': 'pie', 'radius': ['45%', '70%'], 'center': ['50%', '45%'],
                                    'label': {'show': False}, 'data': kolac}],
                    }).classes('w-full h-72')
                else:
                    ui.label('Zatím nic schváleno.').classes('text-sm text-gray-500 py-8')
            with ui.card().classes('p-4 gap-1 shadow-sm lg:col-span-3'):
                ui.label('Kumulativní čerpání vs. plán').classes('font-semibold')
                g = d['graf']
                ui.echart({
                    'tooltip': {'trigger': 'axis', ':valueFormatter':
                                'v => v == null ? "–" : Math.round(v).toLocaleString("cs-CZ") + " Kč"'},
                    'legend': {'bottom': 0},
                    'grid': {'left': 70, 'right': 20, 'top': 20, 'bottom': 50},
                    'xAxis': {'type': 'category', 'data': g['popisky']},
                    'yAxis': {'type': 'value',
                              'axisLabel': {':formatter': 'v => (v / 1000).toLocaleString("cs-CZ") + " tis."'}},
                    'series': [
                        {'name': 'Skutečnost', 'type': 'line', 'data': g['skutecnost'], 'connectNulls': False,
                         'itemStyle': {'color': _TENTO_BARVA}, 'areaStyle': {'opacity': 0.08},
                         'markLine': {'symbol': 'none', 'silent': True,
                                      'lineStyle': {'color': '#dc2626', 'type': 'dashed'},
                                      'label': {'formatter': 'limit', 'position': 'insideEndTop'},
                                      'data': [{'yAxis': float(p['celkem'])}]}},
                        {'name': 'Plán', 'type': 'line', 'data': g['plan'], 'symbol': 'none',
                         'itemStyle': {'color': '#64748b'}, 'lineStyle': {'type': 'dashed'}},
                    ],
                }).classes('w-full h-72')
        with ui.card().classes('w-full p-4 gap-3 shadow-sm'):
            ui.label('Rozpočet vs. čerpání podle kategorií').classes('font-semibold')
            for k in p['kategorie']:
                with ui.column().classes('w-full gap-1'):
                    with ui.row().classes('w-full justify-between text-sm gap-2'):
                        with ui.row().classes('items-center gap-2'):
                            _tecka(k['barva'], True)
                            ui.label(k['nazev']).classes('font-medium')
                        barva = 'text-red-700 font-semibold' if k['prekroceno'] else \
                            'text-amber-700' if k['varovani'] else 'text-gray-600'
                        ui.label(f"{_kc(k['cerpano'])} + čeká {_kc(k['ceka'])} z {_kc(k['rozpocet'])} · "
                                 f"{_pct(k['podil'])}").classes(barva)
                    _pruh([(k['cerpano'], _CERPANO_BARVA), (k['ceka'], _CEKA_BARVA)], k['rozpocet'], 10)
        with ui.card().classes('w-full p-4 gap-2 shadow-sm'):
            ui.label('Poslední náklady').classes('font-semibold')
            posledni = sorted(d['naklady'], key=lambda n: n['upraveno'], reverse=True)[:8]
            if posledni:
                tabulka(posledni, strankovani=False)
            else:
                ui.label('Zatím žádné náklady.').classes('text-sm text-gray-500')

    @ui.refreshable
    def seznam():
        f = s['filtr']
        d = s['data']
        autori = dict(sorted({n['zadal_id']: n['zadal_jmeno'] or '?' for n in d['naklady']}.items(),
                             key=lambda x: x[1]))
        if f['zadal'] not in autori:
            f['zadal'] = None       # zadavatel po obnově zmizel (stáhl jediný náklad) — filtr by skryl vše

        def filtrovane():
            q = (f['q'] or '').strip().lower()
            od, do = _cz_datum(f['od']), _cz_datum(f['do'])
            mn = _castka(f['min']) if (f['min'] or '').strip() else None
            mx = _castka(f['max']) if (f['max'] or '').strip() else None
            out = []
            for n in d['naklady']:
                if ((f['moje'] and n['zadal_id'] != user_id) or (f['stav'] and n['stav'] != f['stav'])
                        or (f['kategorie'] and n['kategorie_id'] != f['kategorie'])
                        or (f['zadal'] and n['zadal_id'] != f['zadal'])
                        or (od and n['vytvoreno'].date() < od) or (do and n['vytvoreno'].date() > do)
                        or (mn is not None and n['castka_s_dph'] < mn) or (mx is not None and n['castka_s_dph'] > mx)):
                    continue
                if q and q not in ' '.join(str(x or '') for x in (
                        _kod(n['id']), n['nazev'], n['dodavatel'], n['dodavatel_ico'], n['kategorie'],
                        n['zadal_jmeno'], n['zduvodneni'])).lower():
                    continue
                out.append(n)
            return out

        def souhrn(radky):
            return f"{_pocet(len(radky))} · {_kc(sum((n['castka_s_dph'] for n in radky), Decimal(0)))} s DPH"

        def zmen(klic, hodnota):
            f[klic] = hodnota
            radky = filtrovane()
            t.rows = [_radek(n) for n in radky]
            t.update()
            pocet.text = souhrn(radky)

        with ui.row().classes('w-full items-center gap-2'):
            hledat = ui.input(placeholder='Hledat (název, dodavatel, IČO, ID…)', value=f['q'],
                              on_change=lambda e: zmen('q', e.value)) \
                .props('outlined dense clearable debounce=300').classes('w-72')
            with hledat.add_slot('prepend'):
                ui.icon('search')
            ui.select({k: v[0] for k, v in _STAVY.items()}, label='Stav', value=f['stav'], clearable=True,
                      on_change=lambda e: zmen('stav', e.value)).props('outlined dense options-dense').classes('w-48')
            ui.select({k['id']: k['nazev'] for k in d['prehled']['kategorie']}, label='Kategorie',
                      value=f['kategorie'], clearable=True, on_change=lambda e: zmen('kategorie', e.value)) \
                .props('outlined dense options-dense').classes('w-56')
            ui.select(autori, label='Zadavatel', value=f['zadal'], clearable=True,
                      on_change=lambda e: zmen('zadal', e.value)).props('outlined dense options-dense').classes('w-48')
            for klic, popis in (('od', 'Zadáno od'), ('do', 'Zadáno do')):
                with ui.element('div').classes('w-40'):
                    _datum_input(popis, f[klic]).on_value_change(lambda e, k=klic: zmen(k, e.value or ''))
            for klic, popis in (('min', 'Částka od'), ('max', 'Částka do')):
                ui.input(popis, value=f[klic], on_change=lambda e, k=klic: zmen(k, e.value or '')) \
                    .props('outlined dense clearable debounce=400 inputmode=decimal').classes('w-32')
            if zadavatel:
                ui.switch('Moje náklady', value=f['moje'], on_change=lambda e: zmen('moje', e.value))
        radky = filtrovane()
        with ui.row().classes('w-full items-center justify-between'):
            pocet = ui.label(souhrn(radky)).classes('text-sm text-gray-600')
            ui.button('Export do Excelu', icon='table_view', on_click=lambda: export_xlsx(filtrovane())) \
                .props('outline color=green-8').tooltip('Exportuje náklady podle aktuálních filtrů')
        t = tabulka(radky)

    @ui.refreshable
    def schvaleni():
        fronta = sorted((n for n in s['data']['naklady'] if n['stav'] == 'ceka'),
                        key=lambda n: n['odeslano'] or n['upraveno'])
        if not fronta:
            ui.label('Nic nečeká na schválení.').classes('text-gray-500 py-8')
            return
        if s['vybrany'] not in {n['id'] for n in fronta}:
            cizi = [n for n in fronta if n['zadal_id'] != user_id]
            s['vybrany'] = (cizi or fronta)[0]['id']

        def vyber(nid):
            s['vybrany'] = nid
            schvaleni.refresh()

        with ui.row().classes('w-full gap-4 items-start lg:flex-nowrap'):
            with ui.column().classes('w-full lg:w-80 shrink-0 gap-2'):
                ui.label(f'Fronta ({len(fronta)})').classes('text-sm font-semibold text-gray-600')
                for n in fronta:
                    vybrany = n['id'] == s['vybrany']
                    with ui.card().classes('w-full p-3 gap-0 cursor-pointer shadow-none border '
                                           + ('border-orange-400 bg-orange-50' if vybrany else 'hover:bg-gray-50')) \
                            .on('click', lambda _, i=n['id']: vyber(i)):
                        with ui.row().classes('w-full justify-between'):
                            ui.label(_kod(n['id'])).classes('text-xs text-gray-500')
                            ui.label(f"odesláno {_datum(n['odeslano'])}").classes('text-xs text-gray-500')
                        ui.label(n['nazev']).classes('font-medium')
                        ui.label(f"{n['kategorie']} · {n['zadal_jmeno']}").classes('text-xs text-gray-500')
                        ui.label(_kc(n['castka_s_dph'], True)).classes('text-sm font-semibold')
            with ui.card().classes('flex-1 min-w-0 w-full p-5 gap-4 shadow-sm'):
                detail_obsah(s['vybrany'])

    @ui.refreshable
    def rozpocet():
        p = s['data']['prehled']
        nast = p['nastaveni']
        radky = []

        def prepocet():
            celkem = _castka(celkem_in.value or '') or Decimal(0)
            soucet = sum((_castka(c.value or '') or Decimal(0) for _, _, c, _ in radky), Decimal(0))
            if (nova_nazev.value or '').strip():
                soucet += _castka(nova_castka.value or '0') or Decimal(0)
            rozdil = celkem - soucet
            if rozdil >= 0:
                souhrn.text = (f'Součet kategorií {_kc(soucet)} z celkového rozpočtu {_kc(celkem)} '
                               f'— nerozdělená rezerva {_kc(rozdil)}.')
                souhrn.classes(replace='text-sm text-gray-700')
            else:
                souhrn.text = (f'Součet kategorií {_kc(soucet)} převyšuje celkový rozpočet o {_kc(-rozdil)} '
                               '— takhle uložit nejde.')
                souhrn.classes(replace='text-sm text-red-700 font-semibold')

        async def ulozit():
            chyby = []
            celkem = _castka(celkem_in.value or '')
            if celkem is None or celkem < 0:
                chyby.append('Zadejte platný celkový rozpočet.')
            datum = _cz_datum(datum_in.value)
            if not datum:
                chyby.append('Datum veletrhu zadejte ve tvaru DD.MM.RRRR.')
            plan = (plan_in.value or '').strip()
            if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', plan):
                chyby.append('Plán čerpání od zadejte ve tvaru RRRR-MM.')
            elif datum and plan > datum.strftime('%Y-%m'):
                chyby.append('Plán čerpání musí začít nejpozději v měsíci veletrhu.')

            def kategorie_z(nazev, castka_, barva):
                return {'nazev': (nazev or '').strip()[:120], 'rozpocet': _castka(castka_ or '0'),
                        'barva': barva if re.fullmatch(r'#[0-9a-fA-F]{6}', barva or '') else None}

            kat = [{'id': kid, **kategorie_z(nz.value, c.value, b.value)} for kid, nz, c, b in radky]
            nova = kategorie_z(nova_nazev.value, nova_castka.value, nova_barva.value) \
                if (nova_nazev.value or '').strip() else None
            jmena = set()
            for k in kat + ([nova] if nova else []):
                if not k['nazev']:
                    chyby.append('Název kategorie nesmí být prázdný.')
                elif k['nazev'].casefold() in jmena:
                    chyby.append(f"Kategorie „{k['nazev']}“ je v seznamu dvakrát.")
                jmena.add(k['nazev'].casefold())
                if k['rozpocet'] is None or k['rozpocet'] < 0:
                    chyby.append(f"Neplatný rozpočet kategorie „{k['nazev']}“.")
                if not k['barva']:
                    chyby.append(f"Neplatná barva kategorie „{k['nazev']}“ (#RRGGBB).")
            if not chyby and sum(k['rozpocet'] for k in kat + ([nova] if nova else [])) > celkem:
                chyby.append('Součet rozpočtů kategorií převyšuje celkový rozpočet — navyšte celek, '
                             'nebo snižte kategorie.')
            if chyby:
                ui.notify(' '.join(dict.fromkeys(chyby)), type='warning', multi_line=True)
                return
            try:
                zmeny = await asyncio.to_thread(
                    _uloz_rozpocet, user_id, user_name,
                    {'akce': (akce_in.value or '').strip()[:120] or _VYCHOZI_NASTAVENI['akce'],
                     'datum_akce': datum.isoformat(), 'plan_od': plan, 'rozpocet': celkem}, kat, nova)
            except Exception as e:
                ui.notify('Kategorie s tímto názvem už existuje.' if 'Duplicate' in str(e)
                          else f'Rozpočet se nepodařilo uložit: {e}', type='negative')
                return
            if not zmeny:
                ui.notify('Beze změny.')
                return
            ui.notify('Rozpočet uložen.', type='positive')
            intranet_logger.log_activity(user_name, _LOG, 'Změna rozpočtu: ' + '; '.join(zmeny))
            await obnov()

        with ui.card().classes('w-full p-4 gap-3 shadow-sm'):
            ui.label('Akce a celkový rozpočet').classes('font-semibold')
            with ui.row().classes('w-full gap-3 items-start'):
                akce_in = ui.input('Název akce', value=nast['akce']).props('outlined dense maxlength=120').classes('w-72')
                with ui.element('div').classes('w-44'):
                    try:
                        datum_akce = _datum(datetime.date.fromisoformat(nast['datum_akce']))
                    except ValueError:
                        datum_akce = ''
                    datum_in = _datum_input('Datum veletrhu', datum_akce)
                plan_in = ui.input('Plán čerpání od (RRRR-MM)', value=nast['plan_od']) \
                    .props('outlined dense maxlength=7').classes('w-52') \
                    .tooltip('Od kterého měsíce se v grafu plánuje rovnoměrné čerpání až do měsíce veletrhu')
                celkem_in = ui.input('Celkový rozpočet (Kč)', value=_cislo_text(p['celkem'])) \
                    .props('outlined dense inputmode=decimal').classes('w-52')
        with ui.card().classes('w-full p-4 gap-3 shadow-sm'):
            ui.label('Rozpočty kategorií').classes('font-semibold')
            with ui.grid(columns='9.5rem minmax(12rem, 1fr) 10rem repeat(3, minmax(7.5rem, auto))') \
                    .classes('w-full gap-x-3 gap-y-2 items-center text-sm'):
                for h in ('Barva', 'Kategorie', 'Rozpočet (Kč)', 'Čerpáno', 'Čeká', 'Volné'):
                    ui.label(h).classes('text-xs text-gray-500' + (' text-right' if h in ('Čerpáno', 'Čeká', 'Volné')
                                                                     else ''))
                for k in p['kategorie']:
                    barva = ui.color_input(value=k['barva']).props('outlined dense')
                    nazev = ui.input(value=k['nazev']).props('outlined dense maxlength=120')
                    castka = ui.input(value=_cislo_text(k['rozpocet'])) \
                        .props('outlined dense inputmode=decimal input-class=text-right')
                    for v, cls in ((k['cerpano'], ''), (k['ceka'], ''),
                                   (k['volne'], 'text-red-700 font-semibold' if k['volne'] < 0 else '')):
                        ui.label(_kc(v)).classes(f'text-right {cls}')
                    radky.append((k['id'], nazev, castka, barva))
                nova_barva = ui.color_input(value='#64748b').props('outlined dense')
                nova_nazev = ui.input(placeholder='+ nová kategorie').props('outlined dense maxlength=120')
                nova_castka = ui.input(placeholder='0').props('outlined dense inputmode=decimal input-class=text-right')
            souhrn = ui.label()
            ui.button('Uložit rozpočet', icon='save', on_click=ulozit).props('unelevated color=orange-8')
        for prvek in [celkem_in, nova_nazev, nova_castka] + [c for _, _, c, _ in radky]:
            prvek.on_value_change(prepocet)
        prepocet()
        with ui.card().classes('w-full p-4 gap-2 shadow-sm'):
            ui.label('Historie změn rozpočtu').classes('font-semibold')
            zaznamy = list(reversed(s['data']['historie'].get(None, [])))
            for r in zaznamy[:50]:
                with ui.column().classes('w-full gap-0'):
                    ui.label(f"{_cas(r['cas'])} · {r['kdo'] or ''}").classes('text-xs text-gray-500')
                    ui.label(r['komentar'] or r['akce']).classes('text-sm')
            if not zaznamy:
                ui.label('Zatím beze změn.').classes('text-sm text-gray-400')

    @ui.refreshable
    def reporty():
        d = s['data']
        sou = _souhrny(d['naklady'])
        odeslane = [n for n in d['naklady'] if n['stav'] != 'koncept']
        with ui.row().classes('w-full gap-2 items-center'):
            ui.button('Export do Excelu', icon='table_view', on_click=lambda: export_xlsx(odeslane)) \
                .props('unelevated color=green-8')
            ui.button('PDF report pro vedení', icon='picture_as_pdf', on_click=export_pdf).props('unelevated color=red-8')
            ui.label('Excel obsahuje všechny odeslané náklady a list Rozpočet; export podle filtrů je v záložce Náklady.') \
                .classes('text-xs text-gray-500')
        with ui.grid().classes('w-full gap-3 grid-cols-1 xl:grid-cols-3'):
            _karta_tabulka('Podle stavu', ['Stav', 'Nákladů', 'Částka s DPH'],
                           [(_STAVY[k][0], c, _kc(v)) for k, c, v in sou['stavy']])
            _karta_tabulka('Podle zadavatelů', ['Zadavatel', 'Nákladů', 'Schváleno a uhrazeno', 'Čeká'],
                           [(j, c, _kc(v), _kc(w)) for j, c, v, w in sou['autori']])
            _karta_tabulka('Podle měsíců splatnosti', ['Měsíc', 'Nákladů', 'Schváleno a uhrazeno'],
                           [(m, c, _kc(v)) for m, c, v in sou['mesice']])

    # ---------- stránka ----------
    start = 'prehled'
    if deep is not None:
        n = s['data']['podle_id'].get(deep)
        if n and schvalovatel and n['stav'] == 'ceka' and n['zadal_id'] != user_id:
            start, s['vybrany'] = 'schvaleni', deep
    with ui.column().classes('w-full gap-3'):
        hlavicka()
        with ui.tabs().props('dense no-caps inline-label align=left active-color=orange-9 indicator-color=orange-9') \
                .classes('w-full text-gray-700 border-b border-gray-200') as taby:
            ui.tab('prehled', 'Přehled', icon='insights')
            ui.tab('naklady', 'Náklady', icon='receipt_long')
            if schvalovatel:
                with ui.tab('schvaleni', 'Ke schválení', icon='fact_check'):
                    odznak_fronty()
            if spravce:
                ui.tab('rozpocet', 'Rozpočet', icon='account_balance_wallet')
            ui.tab('reporty', 'Reporty a export', icon='download')
        with ui.tab_panels(taby, value=start).classes('w-full bg-transparent'):
            with ui.tab_panel('prehled').classes('p-0 gap-3'):
                prehled()
            with ui.tab_panel('naklady').classes('p-0 gap-3'):
                seznam()
            if schvalovatel:
                with ui.tab_panel('schvaleni').classes('p-0'):
                    schvaleni()
            if spravce:
                with ui.tab_panel('rozpocet').classes('p-0 gap-3'):
                    rozpocet()
            with ui.tab_panel('reporty').classes('p-0 gap-3'):
                reporty()
    if deep is not None and start != 'schvaleni':
        if deep in s['data']['podle_id']:
            taby.set_value('naklady')
            detail(deep)
        else:
            ui.notify('Náklad z odkazu nenalezen, nebo k němu nemáte přístup.', type='warning')


def _zpracuj(h) -> tuple[dict, list[str]]:
    """Hodnoty formuláře → data pro DB a chyby (povinná pole podle zadání)."""
    chyby = []
    nazev = (h['nazev'] or '').strip()[:255]
    dodavatel = (h['dodavatel'] or '').strip()[:255]
    ico = ''.join((h['ico'] or '').split())[:20]
    zduvodneni = (h['zduvodneni'] or '').strip()[:5000]
    bez = _castka(h['castka'] or '')
    splatnost = _cz_datum(h['splatnost'])
    if not nazev:
        chyby.append('Vyplňte název nákladu.')
    if not h['kategorie_id']:
        chyby.append('Vyberte kategorii.')
    if not dodavatel:
        chyby.append('Vyplňte dodavatele.')
    if ico and not ico.isdigit():
        chyby.append('IČO smí obsahovat jen číslice.')
    if bez is None or bez <= 0:
        chyby.append('Zadejte platnou částku bez DPH.')
    if h['sazba'] not in DPH_SAZBY:
        chyby.append('Vyberte sazbu DPH.')
    if (h['splatnost'] or '').strip() and not splatnost:
        chyby.append('Datum splatnosti zadejte ve tvaru DD.MM.RRRR.')
    if not zduvodneni:
        chyby.append('Vyplňte zdůvodnění.')
    if chyby:
        return {}, chyby
    return {'nazev': nazev, 'kategorie_id': h['kategorie_id'], 'dodavatel': dodavatel, 'dodavatel_ico': ico or None,
            'castka_bez_dph': bez, 'sazba_dph': h['sazba'], 'castka_s_dph': _s_dph(bez, h['sazba']),
            'splatnost': splatnost, 'zduvodneni': zduvodneni}, []
