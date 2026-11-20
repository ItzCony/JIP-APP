"""Náklady veletrh — evidence nákladů a rozpočtu firemního veletrhu JIP 2027.

Samostatná dlaždice na přehledu; s Plánem Veletrh 2027 (intranet_veletrh) nesouvisí.

Bez schvalování. Náklad má očekávanou částku a po příchodu faktury i oficiální částku
z faktury. Všechny částky i rozpočty jsou bez DPH, DPH modul neřeší (sloupce sazba_dph,
castka_s_dph a faktura_s_dph zůstaly ve staré DB jen jako archiv). Rozdíl = očekáváno − faktura
(kladný = ušetřeno). Stav se odvozuje: bez faktury „Čeká na fakturu“, s ní „Fakturováno“.
Rozpočet: čerpáno = faktury, čeká = očekávané náklady bez faktury; jejich součet je
odhad konečných nákladů a volné = rozpočet − odhad. Každá změna jde do historie.
Náklad upravuje a maže jeho autor (zadavatel), cizí jen správce.
Práva: naklady_veletrh_zadavatel / _schvalovatel / _spravce / _ctenar ('vse' = všechna).
Schvalovatel (z doby schvalování) = správce: navíc nastavuje rozpočet a kategorie.
Záložka Organizátor (zadavatel + spravce): body plánu s hotovo/nehotovo a komentáře
k nim. Bod upravuje, odškrtává a maže jen jeho autor, komentář maže jen jeho autor.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import io
import math
import os
import re
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi.responses import FileResponse, PlainTextResponse
from nicegui import app, ui

import intranet_data
import intranet_logger

PRAVO_ZADAVATEL = 'naklady_veletrh_zadavatel'
PRAVO_SCHVALOVATEL = 'naklady_veletrh_schvalovatel'
PRAVO_SPRAVCE = 'naklady_veletrh_spravce'      # Částečný správce — od zrušení schvalování totéž co schvalovatel
PRAVO_CTENAR = 'naklady_veletrh_ctenar'
VAROVANI_KATEGORIE = 0.85       # odhad nákladů kategorie (faktury + očekávané) vůči jejímu rozpočtu
VAROVANI_CELKEM = 0.90          # odhad nákladů celé akce vůči celkovému rozpočtu
MAX_PRILOHA = 20 * 1024 * 1024
PRILOHY = ('.pdf', '.png', '.jpg', '.jpeg', '.xlsx', '.xls', '.docx', '.doc', '.csv', '.txt')
_V_PROHLIZECI = ('.pdf', '.png', '.jpg', '.jpeg')       # ostatní přílohy se stáhnou
ADRESAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'naklady_veletrh_prilohy')
_LOG = 'Náklady veletrh'
_DEEP = 'naklady_veletrh_naklad'     # app.storage.user: deep-link z e-mailu (/naklady_veletrh?naklad=<id>)

_STAVY = {      # odvozený z faktury, uživatel ho nenastavuje
    'ocekavano':   ('Čeká na fakturu', 'amber-8'),
    'fakturovano': ('Fakturováno', 'green-7'),
}
_HLASKY = {'komentar': 'Komentář přidán.', 'smazat': 'Náklad smazán.'}
_CERPANO_BARVA, _CEKA_BARVA, _TENTO_BARVA, _VOLNE_BARVA = '#1B2430', '#f59e0b', '#ea580c', '#e5e7eb'
_MESICE = ('led', 'úno', 'bře', 'dub', 'kvě', 'čvn', 'čvc', 'srp', 'zář', 'říj', 'lis', 'pro')

_VYCHOZI_KATEGORIE = (      # doporučení ze zadání; rozpočty jsou ilustrativní, upraví je správce
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
        faktura_bez_dph DECIMAL(15,2) NULL,
        fakturovano DATETIME NULL,
        splatnost DATE NULL,
        zduvodneni TEXT NOT NULL,
        stav VARCHAR(20) NOT NULL DEFAULT 'ocekavano',
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
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_plany (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        kdo VARCHAR(255),
        obsah TEXT NOT NULL,
        hotovo TINYINT(1) NOT NULL DEFAULT 0,
        vytvoreno DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_user (user_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS naklady_veletrh_plany_komentare (
        id INT AUTO_INCREMENT PRIMARY KEY,
        plan_id INT NOT NULL,
        user_id INT NOT NULL,
        kdo VARCHAR(255),
        obsah TEXT NOT NULL,
        cas DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_plan (plan_id)
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
        cur.execute("SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'naklady_veletrh'")
        sloupce = dict(cur.fetchall())
        if 'faktura_bez_dph' not in sloupce:     # přechod ze schvalování
            cur.execute('ALTER TABLE naklady_veletrh ADD COLUMN faktura_bez_dph DECIMAL(15,2) NULL, '
                        'ADD COLUMN fakturovano DATETIME NULL, '
                        "ALTER COLUMN stav SET DEFAULT 'ocekavano'")
            cur.execute("UPDATE naklady_veletrh SET stav = 'ocekavano'")
        if sloupce.get('sazba_dph') == 'NO':     # zrušení DPH: staré sloupce s DPH zůstávají jen jako archiv
            cur.execute('ALTER TABLE naklady_veletrh MODIFY sazba_dph INT NULL, '
                        'MODIFY castka_s_dph DECIMAL(15,2) NULL')
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
    """Rozpočet celku i kategorií. Čerpáno = faktury, čeká = očekávané náklady bez faktury,
    čerpáno + čeká = odhad konečných nákladů. Rozdíl = očekáváno − faktura u fakturovaných."""
    nast = _nastaveni(cur)
    cur.execute("SELECT k.id, k.nazev, k.rozpocet, k.barva, "
                "COALESCE(SUM(n.faktura_bez_dph), 0) AS cerpano, "
                "COALESCE(SUM(CASE WHEN n.faktura_bez_dph IS NULL THEN n.castka_bez_dph END), 0) AS ceka, "
                "COALESCE(SUM(n.castka_bez_dph), 0) AS ocekavano, "
                "COALESCE(SUM(n.castka_bez_dph - n.faktura_bez_dph), 0) AS rozdil "
                "FROM naklady_veletrh_kategorie k LEFT JOIN naklady_veletrh n ON n.kategorie_id = k.id "
                "GROUP BY k.id ORDER BY k.poradi, k.id")
    kategorie = cur.fetchall()
    for k in kategorie:
        k['volne'] = k['rozpocet'] - k['cerpano'] - k['ceka']
        k['podil'] = _podil(k['cerpano'] + k['ceka'], k['rozpocet'])
        k['prekroceno'] = k['volne'] < 0
        k['varovani'] = k['podil'] > VAROVANI_KATEGORIE
    celkem = _castka(nast['rozpocet']) or Decimal(0)

    def soucet(klic):
        return sum((k[klic] for k in kategorie), Decimal(0))

    cerpano, ceka = soucet('cerpano'), soucet('ceka')
    return {'nastaveni': nast, 'kategorie': kategorie, 'celkem': celkem, 'cerpano': cerpano, 'ceka': ceka,
            'ocekavano': soucet('ocekavano'), 'rozdil': soucet('rozdil'),
            'volne': celkem - cerpano - ceka, 'podil': _podil(cerpano + ceka, celkem),
            'rozdeleno': soucet('rozpocet')}


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
    """Kumulativní fakturace podle měsíce zadání faktury vs. rovnoměrný plán do měsíce veletrhu."""
    cur.execute("SELECT LEFT(fakturovano, 7) AS m, SUM(faktura_bez_dph) AS s FROM naklady_veletrh "
                "WHERE faktura_bez_dph IS NOT NULL GROUP BY LEFT(fakturovano, 7)")
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


def _nacti() -> dict:
    """Vše, co modul zobrazuje: rozpočet, náklady, jejich přílohy a historie, historie
    rozpočtu a řada pro graf fakturace."""
    with _transakce() as cur:
        p = _prehled(cur)
        cur.execute(_SELECT + ' ORDER BY n.id DESC')
        naklady = cur.fetchall()
        prilohy, historie = defaultdict(list), defaultdict(list)
        cur.execute('SELECT id, naklad_id, nazev, velikost FROM naklady_veletrh_prilohy ORDER BY id')
        for r in cur.fetchall():
            prilohy[r['naklad_id']].append(r)
        cur.execute('SELECT naklad_id, kdo, akce, komentar, cas FROM naklady_veletrh_historie ORDER BY cas, id')
        for r in cur.fetchall():
            historie[r['naklad_id']].append(r)
        graf = _graf(cur, p)
    return {'prehled': p, 'naklady': naklady, 'podle_id': {n['id']: n for n in naklady},
            'prilohy': prilohy, 'historie': historie, 'graf': graf}


_POLE_ZMENY = (('nazev', 'název'), ('kategorie_id', 'kategorie'), ('dodavatel', 'dodavatel'),
               ('dodavatel_ico', 'IČO'), ('castka_bez_dph', 'očekáváno'), ('faktura_bez_dph', 'faktura'),
               ('splatnost', 'splatnost'), ('zduvodneni', 'poznámka'))


def _zmeny(stary, novy, kategorie) -> str:
    """Čitelný souhrn úprav do historie."""
    def txt(klic, v):
        if v is None or v == '':
            return '—'
        if klic in ('castka_bez_dph', 'faktura_bez_dph'):
            return _kc(v, True)
        return {'kategorie_id': lambda: kategorie.get(v, '?'),
                'splatnost': lambda: _datum(v)}.get(klic, lambda: str(v))()
    out = []
    for klic, popis in _POLE_ZMENY:
        if stary[klic] != novy[klic]:
            out.append(popis if klic == 'zduvodneni'
                       else f'{popis}: {txt(klic, stary[klic])} → {txt(klic, novy[klic])}')
    return '; '.join(out)


def _uloz(user_id, kdo, role, nid, d, nove_prilohy) -> int:
    """Založí (nid None) nebo upraví náklad a připojí nahrané přílohy [(název, disk, velikost)].
    Cizí náklad upraví jen správce. Vyplněná faktura přepne stav na Fakturováno."""
    if not role & {'zadavatel', 'spravce'}:
        raise Chyba('Náklady zadává jen zadavatel nebo správce.')
    hodnoty = (d['nazev'], d['kategorie_id'], d['dodavatel'], d['dodavatel_ico'], d['castka_bez_dph'],
               d['faktura_bez_dph'], d['splatnost'], d['zduvodneni'],
               'ocekavano' if d['faktura_bez_dph'] is None else 'fakturovano')
    with _transakce() as cur:
        cur.execute('SELECT id, nazev FROM naklady_veletrh_kategorie')
        kategorie = {r['id']: r['nazev'] for r in cur.fetchall()}
        if d['kategorie_id'] not in kategorie:
            raise Chyba('Vybraná kategorie už neexistuje.')
        if nid is None:
            cur.execute('INSERT INTO naklady_veletrh (nazev, kategorie_id, dodavatel, dodavatel_ico, castka_bez_dph, '
                        'faktura_bez_dph, splatnost, zduvodneni, stav, fakturovano, zadal_id, zadal_jmeno) '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, IF(%s IS NULL, NULL, NOW()), %s, %s)',
                        hodnoty + (d['faktura_bez_dph'], user_id, kdo))
            nid = cur.lastrowid
            _zapis(cur, nid, user_id, kdo, 'Vytvořil(a) náklad')
        else:
            cur.execute('SELECT * FROM naklady_veletrh WHERE id = %s FOR UPDATE', (nid,))
            n = cur.fetchone()
            if not n:
                raise Chyba('Náklad mezitím někdo smazal.')
            if n['zadal_id'] != user_id and 'spravce' not in role:
                raise Chyba('Cizí náklad upravuje jen správce.')
            # fakturovano až za faktura_bez_dph — MySQL přiřazuje zleva, takže vidí novou hodnotu
            cur.execute('UPDATE naklady_veletrh SET nazev = %s, kategorie_id = %s, dodavatel = %s, '
                        'dodavatel_ico = %s, castka_bez_dph = %s, faktura_bez_dph = %s, splatnost = %s, '
                        'zduvodneni = %s, stav = %s, '
                        'fakturovano = IF(faktura_bez_dph IS NULL, NULL, COALESCE(fakturovano, NOW())), '
                        'upraveno = NOW() WHERE id = %s', hodnoty + (nid,))
            zmeny = _zmeny(n, d, kategorie)
            if zmeny:
                _zapis(cur, nid, user_id, kdo, 'Upravil(a) náklad', zmeny)
        for nazev, disk, velikost in nove_prilohy:
            cur.execute('INSERT INTO naklady_veletrh_prilohy (naklad_id, nazev, disk, velikost, nahral_jmeno) '
                        'VALUES (%s, %s, %s, %s, %s)', (nid, nazev, disk, velikost, kdo))
            _zapis(cur, nid, user_id, kdo, f'Přidal(a) přílohu „{nazev}“')
    return nid


def _proved(user_id, kdo, role, nid, akce, komentar='') -> dict:
    """Komentář, nebo smazání nákladu (vlastní, cizí jen správce) v transakci se zamčeným
    řádkem. Vrací náklad před akcí."""
    komentar = (komentar or '').strip()[:4000]
    disky = []
    with _transakce() as cur:
        cur.execute('SELECT * FROM naklady_veletrh WHERE id = %s FOR UPDATE', (nid,))
        n = cur.fetchone()
        if not n:
            raise Chyba('Náklad nenalezen — mezitím ho někdo smazal.')
        if akce == 'komentar':
            if not role & {'zadavatel', 'spravce'}:
                raise Chyba('Komentáře píše jen zadavatel nebo správce.')
            if not komentar:
                raise Chyba('Napište text komentáře.')
            _zapis(cur, nid, user_id, kdo, 'Komentář', komentar)
        elif akce == 'smazat':
            if not ('spravce' in role or ('zadavatel' in role and n['zadal_id'] == user_id)):
                raise Chyba('Smazat jde jen vlastní náklad; cizí maže správce.')
            cur.execute('SELECT disk FROM naklady_veletrh_prilohy WHERE naklad_id = %s', (nid,))
            disky = [r['disk'] for r in cur.fetchall()]
            cur.execute('DELETE FROM naklady_veletrh_prilohy WHERE naklad_id = %s', (nid,))
            cur.execute('DELETE FROM naklady_veletrh WHERE id = %s', (nid,))
            _zapis(cur, None, user_id, kdo, 'Smazání nákladu',      # historie rozpočtu — náklad už není
                   f"{_kod(nid)} {n['nazev']} (očekáváno {_kc(n['castka_bez_dph'], True)})")
        else:
            raise Chyba('Neznámá akce.')
    _smaz_soubory(disky)        # až po commitu
    return n


def _uloz_rozpocet(user_id, kdo, nast, kategorie, nove) -> list[str]:
    """Nastavení akce, celkový rozpočet a rozpočty kategorií (+ nové kategorie) v jedné
    transakci (zamčený řádek celkového rozpočtu). Vrací seznam změn (ten jde i do historie)."""
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
        for nova in nove:
            cur.execute('INSERT INTO naklady_veletrh_kategorie (nazev, rozpocet, barva, poradi) '
                        'SELECT %s, %s, %s, COALESCE(MAX(poradi), 0) + 1 FROM naklady_veletrh_kategorie',
                        (nova['nazev'], nova['rozpocet'], nova['barva']))
            zmeny.append(f'nová kategorie „{nova["nazev"]}“ {_kc(nova["rozpocet"])}')
        if zmeny:
            _zapis(cur, None, user_id, kdo, 'Změna rozpočtu', '; '.join(zmeny))
    return zmeny


# ---------- Organizátor ----------
ORGANIZATOR_ROLE = frozenset(('zadavatel', 'spravce'))     # spravce má i schvalovatel
ORG_TIK_S = 3.0             # jak často otevřený Organizátor kontroluje změny
ORG_POJISTKA_S = 30.0       # jak často se i bez známé změny podívá do DB (zápisy z jiného procesu)
_ORG_VERZE = 0              # zvedá se po každém zápisu — otevřené stránky v procesu se obnoví hned


def _org_zmena():
    global _ORG_VERZE
    _ORG_VERZE += 1


def _nacti_plany() -> dict:
    with _transakce() as cur:
        cur.execute('SELECT * FROM naklady_veletrh_plany ORDER BY id')
        plany = cur.fetchall()
        cur.execute('SELECT * FROM naklady_veletrh_plany_komentare ORDER BY id')
        komentare = defaultdict(list)
        for r in cur.fetchall():
            komentare[r['plan_id']].append(r)
    return {'plany': plany, 'komentare': komentare}


def _plan_akce(user_id, kdo, role, akce, cil=None, hodnota=''):
    """Zápis v Organizátoru. Bod plánu upravuje, odškrtává a maže jen autor, komentovat
    smí každý s přístupem, komentář maže jen jeho autor."""
    if not role & ORGANIZATOR_ROLE:
        raise Chyba('K Organizátoru nemáte oprávnění.')
    text = hodnota.strip()[:5000] if isinstance(hodnota, str) else ''
    if akce in ('pridat', 'upravit', 'komentar') and not text:
        raise Chyba('Napište text.')
    with _transakce() as cur:
        if akce == 'pridat':
            cur.execute('INSERT INTO naklady_veletrh_plany (user_id, kdo, obsah) VALUES (%s, %s, %s)',
                        (user_id, kdo, text))
            return
        if akce == 'smazat_komentar':
            cur.execute('DELETE FROM naklady_veletrh_plany_komentare WHERE id = %s AND user_id = %s',
                        (cil, user_id))
            if not cur.rowcount:
                raise Chyba('Komentář už neexistuje, nebo není váš.')
            return
        cur.execute('SELECT user_id FROM naklady_veletrh_plany WHERE id = %s FOR UPDATE', (cil,))
        plan = cur.fetchone()
        if not plan:
            raise Chyba('Bod plánu mezitím autor smazal.')
        if akce == 'komentar':
            cur.execute('INSERT INTO naklady_veletrh_plany_komentare (plan_id, user_id, kdo, obsah) '
                        'VALUES (%s, %s, %s, %s)', (cil, user_id, kdo, text))
            return
        if plan['user_id'] != user_id:
            raise Chyba('Cizí plán můžete jen komentovat.')
        if akce == 'upravit':
            cur.execute('UPDATE naklady_veletrh_plany SET obsah = %s WHERE id = %s', (text, cil))
        elif akce == 'hotovo':
            cur.execute('UPDATE naklady_veletrh_plany SET hotovo = %s WHERE id = %s', (bool(hodnota), cil))
        elif akce == 'smazat':
            cur.execute('DELETE FROM naklady_veletrh_plany_komentare WHERE plan_id = %s', (cil,))
            cur.execute('DELETE FROM naklady_veletrh_plany WHERE id = %s', (cil,))


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
    některá z rolí. Jinak None (i při chybě: fail closed)."""
    try:
        if not _zapnuto():
            return None
        prava, aktivni = intranet_data.ziskej_prava_a_aktivitu(user_id)
        if not aktivni or not _role(prava):
            return None
        p = _sql('SELECT p.nazev, p.disk, n.stav, n.zadal_id FROM naklady_veletrh_prilohy p '
                 'JOIN naklady_veletrh n ON n.id = p.naklad_id WHERE p.id = %s', (pid,), 'jeden')
        if not p:
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
# REPORTY — Excel a PDF pro vedení
# =========================================================
def _souhrny(naklady) -> dict:
    """Náklady podle stavu, zadavatelů a měsíců splatnosti: počet, očekáváno, fakturováno."""
    stavy, autori, mesice = {}, {}, {}

    def pricti(skupiny, klic, n, *popis):
        r = skupiny.setdefault(klic, [*popis, 0, Decimal(0), Decimal(0)])
        r[-3] += 1
        r[-2] += n['castka_bez_dph']
        r[-1] += n['faktura_bez_dph'] or 0

    for n in naklady:
        pricti(stavy, n['stav'], n)
        pricti(autori, n['zadal_id'], n, n['zadal_jmeno'] or '?')
        if n['splatnost']:
            pricti(mesice, n['splatnost'].strftime('%Y-%m'), n)
    return {'stavy': [(k, *stavy[k]) for k in _STAVY if k in stavy],
            'autori': sorted(autori.values(), key=lambda a: (-a[2], a[0])),
            'mesice': [(f'{_MESICE[int(m[5:]) - 1]} {m[:4]}', *v) for m, v in sorted(mesice.items())]}


_EXPORT = (('ID', 9), ('Název', 36), ('Kategorie', 24), ('Dodavatel', 26), ('IČO', 11), ('Očekáváno bez DPH', 17),
           ('Faktura bez DPH', 17), ('Rozdíl', 15), ('Stav', 16), ('Zadal(a)', 20), ('Vytvořeno', 16),
           ('Faktura zadána', 16), ('Splatnost', 12), ('Poznámka', 60))


def _rozdil(n):
    """Očekáváno − faktura (kladný = ušetřeno); bez faktury None."""
    return None if n['faktura_bez_dph'] is None else n['castka_bez_dph'] - n['faktura_bez_dph']


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
                   n['castka_bez_dph'], n['faktura_bez_dph'], _rozdil(n), _STAVY[n['stav']][0],
                   n['zadal_jmeno'] or '', n['vytvoreno'], n['fakturovano'], n['splatnost'], n['zduvodneni']])
    s = wb.create_sheet('Rozpočet')
    s.append(['Kategorie', 'Rozpočet', 'Očekáváno', 'Fakturováno', 'Rozdíl', 'Očekáváno bez faktury', 'Volné'])
    for k in p['kategorie']:
        s.append([k['nazev'], k['rozpocet'], k['ocekavano'], k['cerpano'], k['rozdil'], k['ceka'], k['volne']])
    for nazev, rozpocet, volne in (('Součet kategorií', p['rozdeleno'], p['rozdeleno'] - p['cerpano'] - p['ceka']),
                                   ('Celkový rozpočet akce', p['celkem'], p['volne'])):
        s.append([nazev, rozpocet, p['ocekavano'], p['cerpano'], p['rozdil'], p['ceka'], volne])
    kc = '#,##0.00 "Kč"'
    for list_, sirky in ((ws, [w for _, w in _EXPORT]), (s, (30, 16, 16, 16, 16, 22, 16))):
        for cell in list_[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='1B2430')
        for i, w in enumerate(sirky, 1):
            list_.column_dimensions[get_column_letter(i)].width = w
        list_.freeze_panes = 'A2'
    for radek in ws.iter_rows(min_row=2):
        for i in (5, 6, 7):
            radek[i].number_format = kc
        for i in (10, 11):
            radek[i].number_format = 'dd.mm.yyyy hh:mm'
        radek[12].number_format = 'dd.mm.yyyy'
    ws.auto_filter.ref = ws.dimensions
    for radek in s.iter_rows(min_row=2, min_col=2, max_col=7):
        for cell in radek:
            cell.number_format = kc
    for cell in s[s.max_row - 1] + s[s.max_row]:
        cell.font = Font(bold=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _varovani(p) -> list[tuple[str, str]]:
    """Upozornění rozpočtu podle odhadu (faktury + očekávané bez faktury): (text, 'red' | 'amber')."""
    out = []
    pres = p['cerpano'] + p['ceka'] - p['celkem']
    if pres > 0:
        out.append((f'Odhad nákladů (faktury + očekávané) převyšuje celkový rozpočet o {_kc(pres)}.', 'red'))
    elif p['podil'] > VAROVANI_CELKEM:
        out.append((f"Odhad nákladů je {_pct(p['podil'])} celkového rozpočtu "
                    f'(varování nad {_pct(VAROVANI_CELKEM)}).', 'amber'))
    for k in p['kategorie']:
        if k['prekroceno']:
            out.append((f"Kategorie „{k['nazev']}“ je překročena o {_kc(-k['volne'])}.", 'red'))
        elif k['varovani']:
            out.append((f"Kategorie „{k['nazev']}“ je vyčerpána z {_pct(k['podil'])} (faktury + očekávané).",
                        'amber'))
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

    def kc_nebo(v):
        return '—' if v is None else _kc(v, True)

    kpi = ''.join(f'<div><span>{e(t)}</span><b>{v}</b><small>{e(pod)}</small></div>' for t, v, pod in (
        ('Celkový rozpočet', _kc(p['celkem']), f"v kategoriích {_kc(p['rozdeleno'])}"),
        ('Očekávané náklady', _kc(p['ocekavano']), _pocet(len(d['naklady']))),
        ('Fakturováno', _kc(p['cerpano']), f"rozdíl proti očekávání {_kc(p['rozdil'])}"),
        ('Zbývá k dispozici', _kc(p['volne']), 'rozpočet − faktury − očekávané bez faktury')))
    varovani = ''.join(f'<p class="{"err" if typ == "red" else "warn"}">⚠ {e(t)}</p>' for t, typ in _varovani(p))
    legenda = ''.join(f'<div><span class="tecka" style="background:{e(k["barva"])}"></span>{e(k["nazev"])} '
                      f'— {_kc(k["cerpano"])}</div>' for k in p['kategorie'] if k['cerpano'] > 0)
    kategorie = tab(('Kategorie', 'Rozpočet', 'Očekáváno', 'Fakturováno', 'Rozdíl', 'Volné', 'Odhad', ''), [
        (f'<span class="tecka" style="background:{e(k["barva"])}"></span>{e(k["nazev"])}', _kc(k['rozpocet']),
         _kc(k['ocekavano']), _kc(k['cerpano']), _kc(k['rozdil']), _kc(k['volne']), _pct(k['podil']),
         pruh([(k['cerpano'], _CERPANO_BARVA), (k['ceka'], _CEKA_BARVA)], k['rozpocet']))
        for k in p['kategorie']] + [tuple(f'<b>{v}</b>' if v else '' for v in (
            'Součet kategorií', _kc(p['rozdeleno']), _kc(p['ocekavano']), _kc(p['cerpano']), _kc(p['rozdil']),
            _kc(p['rozdeleno'] - p['cerpano'] - p['ceka']), '', ''))],
        vpravo=(1, 2, 3, 4, 5, 6))
    autori = tab(('Zadavatel', 'Nákladů', 'Očekáváno', 'Fakturováno'),
                 [(e(j), c, _kc(o), _kc(f)) for j, c, o, f in sou['autori']], (1, 2, 3))
    mesice = tab(('Měsíc splatnosti', 'Nákladů', 'Očekáváno', 'Fakturováno'),
                 [(e(m), c, _kc(o), _kc(f)) for m, c, o, f in sou['mesice']], (1, 2, 3))
    seznam = tab(('ID', 'Název', 'Kategorie', 'Dodavatel', 'Očekáváno', 'Faktura', 'Rozdíl', 'Splatnost', 'Stav'), [
        (_kod(n['id']), e(n['nazev']), e(n['kategorie']), e(n['dodavatel']), _kc(n['castka_bez_dph'], True),
         kc_nebo(n['faktura_bez_dph']), kc_nebo(_rozdil(n)), _datum(n['splatnost']), _STAVY[n['stav']][0])
        for n in sorted(d['naklady'], key=lambda n: n['id'])], (4, 5, 6))
    hlavicka = ' · '.join(x for x in (f'Stav k {_cas(datetime.datetime.now())}', f'vytiskl(a) {kdo}',
                                       _do_veletrhu(nast)) if x)
    return f'''<!doctype html><html lang="cs"><head><meta charset="utf-8"><style>{_CSS_REPORT}</style></head><body>
<h1>Náklady veletrhu — {e(nast['akce'])}</h1><p class="muted">{e(hlavicka)}</p>
<div class="kpi">{kpi}</div>
{pruh([(p['cerpano'], _CERPANO_BARVA), (p['ceka'], _CEKA_BARVA)], p['celkem'])}
<p class="muted">Fakturováno {_kc(p['cerpano'])} · očekáváno bez faktury {_kc(p['ceka'])} · volné {_kc(max(p['volne'], 0))}</p>
{varovani}
<div class="grafy"><div><h2>Struktura fakturace</h2>{_svg_kolac(p['kategorie'])}<div>{legenda}</div></div>
<div><h2>Kumulativní fakturace vs. plán</h2>{_svg_cara(d['graf'], p['celkem'])}
<p class="muted">Oranžová = fakturováno (podle měsíce zadání faktury), šedá = rovnoměrný plán, červená = limit.</p></div></div>
<h2>Rozpočet podle kategorií</h2>{kategorie}
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
        ('Dodavatel', (n['dodavatel'] or '—') + (f", IČO {n['dodavatel_ico']}" if n['dodavatel_ico'] else '')),
        ('Splatnost', _datum(n['splatnost']) or '—'),
        ('Zadal(a)', f"{n['zadal_jmeno']} · {_cas(n['vytvoreno'])}"),
        ('Faktura zadána', _cas(n['fakturovano'])) if n['fakturovano'] else None,
    ) if r]


def _radek(n) -> dict:
    rozdil = _rozdil(n)
    return {'id': n['id'], 'kod': _kod(n['id']), 'nazev': n['nazev'], 'kategorie': n['kategorie'],
            'dodavatel': n['dodavatel'], 'zadal': n['zadal_jmeno'] or '', 'castka': float(n['castka_bez_dph']),
            'faktura': None if rozdil is None else float(n['faktura_bez_dph']),
            'rozdil': None if rozdil is None else float(rozdil),
            'splatnost': n['splatnost'].isoformat() if n['splatnost'] else '',
            'stav': _STAVY[n['stav']][0], 'barva': _STAVY[n['stav']][1],
            'vytvoreno': n['vytvoreno'].strftime('%Y-%m-%d %H:%M'), 'prilohy': n['pocet_priloh']}


_FMT_DATUM = 'v => v ? v.slice(8, 10) + "." + v.slice(5, 7) + "." + v.slice(0, 4) : ""'
_FMT_KC = 'v => v == null ? "—" : Math.round(v).toLocaleString("cs-CZ") + " Kč"'
_SLOUPCE = [
    {'name': 'kod', 'label': 'ID', 'field': 'kod', 'sortable': True, 'align': 'left'},
    {'name': 'nazev', 'label': 'Název', 'field': 'nazev', 'sortable': True, 'align': 'left'},
    {'name': 'kategorie', 'label': 'Kategorie', 'field': 'kategorie', 'sortable': True, 'align': 'left'},
    {'name': 'dodavatel', 'label': 'Dodavatel', 'field': 'dodavatel', 'sortable': True, 'align': 'left'},
    {'name': 'zadal', 'label': 'Zadal(a)', 'field': 'zadal', 'sortable': True, 'align': 'left'},
    {'name': 'castka', 'label': 'Očekáváno', 'field': 'castka', 'sortable': True, 'align': 'right',
     ':format': _FMT_KC},
    {'name': 'faktura', 'label': 'Faktura', 'field': 'faktura', 'sortable': True, 'align': 'right',
     ':format': _FMT_KC},
    {'name': 'rozdil', 'label': 'Rozdíl', 'field': 'rozdil', 'sortable': True, 'align': 'right',
     ':format': _FMT_KC},
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
    zadavatel, spravce = 'zadavatel' in role, 'spravce' in role
    try:
        deep = app.storage.user.pop(_DEEP, None)
    except Exception:
        deep = None
    try:
        await asyncio.to_thread(inicializace_db)
        data = await asyncio.to_thread(_nacti)
    except Exception as e:
        ui.label(f'Náklady veletrh se nepodařilo načíst: {e}').classes('text-red-600')
        return

    org = bool(role & ORGANIZATOR_ROLE)
    s = {'data': data, 'plany': None, 'rozbalene': set(),
         'koncepty': {}, 'fokus': False, 'org_verze': -1, 'org_cas': 0.0,
         'filtr': {'q': '', 'stav': None, 'kategorie': None, 'zadal': None, 'od': '', 'do': '',
                   'min': '', 'max': '', 'moje': False}}
    otevrene = []       # překreslení otevřených detailů (dialogy) po obnově dat
    host = ui.element('div')    # dialogy žijí mimo obnovované části — refresh je nezavře

    async def obnov():
        try:
            s['data'] = await asyncio.to_thread(_nacti)
        except Exception as e:
            ui.notify(f'Data se nepodařilo načíst: {e}', type='negative')
            return
        for fn in (hlavicka, prehled, seznam, rozpocet, reporty):
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
        await obnov()

    async def smazat(n):
        if await dotaz(f"Smazat náklad {_kod(n['id'])} „{n['nazev']}“ i s přílohami?", 'Smazat', 'red-7'):
            await proved(n['id'], 'smazat')

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
    def kategorie_ui(n):
        kat = next((k for k in s['data']['prehled']['kategorie'] if k['id'] == n['kategorie_id']), None)
        if not kat:
            return
        with ui.card().classes('w-full p-4 gap-2 shadow-none border border-gray-200'):
            ui.label(f"Rozpočet kategorie „{kat['nazev']}“").classes('font-semibold')
            _pruh([(kat['cerpano'], _CERPANO_BARVA), (kat['ceka'], _CEKA_BARVA)], kat['rozpocet'], 14)
            ui.label(f"Fakturováno {_kc(kat['cerpano'])} + očekáváno bez faktury {_kc(kat['ceka'])} "
                     f"z {_kc(kat['rozpocet'])} · volné {_kc(kat['volne'])}") \
                .classes('text-sm ' + ('text-red-700 font-semibold' if kat['prekroceno'] else 'text-gray-700'))

    def akce_ui(n):
        if spravce or (zadavatel and n['zadal_id'] == user_id):
            with ui.row().classes('w-full gap-2 items-center'):
                ui.button('Upravit / zadat fakturu' if n['faktura_bez_dph'] is None else 'Upravit', icon='edit',
                          on_click=lambda: formular(n)).props('outline color=grey-8')
                ui.button('Smazat', icon='delete', on_click=lambda: smazat(n)).props('flat color=red-7')

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
            ui.label('Náklad nenalezen.').classes('text-gray-500 py-6')
            return
        rozdil = _rozdil(n)
        with ui.row().classes('w-full justify-between items-start gap-4'):
            with ui.column().classes('gap-1'):
                ui.label(_kod(nid)).classes('text-xs text-gray-500')
                ui.label(n['nazev']).classes('text-xl font-bold text-gray-900')
                _odznak(n['stav'])
            with ui.row().classes('gap-6 items-start'):
                for popis, hodnota, barva in (
                        ('Očekáváno bez DPH', _kc(n['castka_bez_dph'], True), 'text-gray-900'),
                        ('Faktura bez DPH', '—' if rozdil is None else _kc(n['faktura_bez_dph'], True),
                         'text-gray-900'),
                        ('Rozdíl', '—' if rozdil is None else _kc(rozdil, True),
                         'text-gray-400' if rozdil is None else 'text-green-700' if rozdil >= 0 else 'text-red-700')):
                    with ui.column().classes('items-end gap-0'):
                        ui.label(popis).classes('text-xs text-gray-500')
                        ui.label(hodnota).classes(f'text-2xl font-bold {barva}')
        with ui.row().classes('w-full gap-8 items-start'):
            with ui.grid(columns='auto 1fr').classes('gap-x-4 gap-y-1 text-sm flex-1 min-w-[300px]'):
                for popis, hodnota in _udaje(n):
                    ui.label(popis).classes('text-gray-500')
                    ui.label(hodnota).classes('font-medium')
            with ui.column().classes('gap-1 flex-1 min-w-[260px]'):
                ui.label('Poznámka').classes('text-xs text-gray-500')
                ui.label(n['zduvodneni'] or '—').classes('text-sm whitespace-pre-line')
                ui.label('Přílohy').classes('text-xs text-gray-500 mt-3')
                for pr in d['prilohy'].get(nid, []):
                    ui.link(f"📄 {pr['nazev']}", f"/naklady_veletrh/priloha/{pr['id']}", new_tab=True) \
                        .classes('text-sm')
                if not d['prilohy'].get(nid):
                    ui.label('Bez příloh').classes('text-sm text-gray-400')
        kategorie_ui(n)
        akce_ui(n)
        historie_ui(nid)

    def detail(nid):
        with host, ui.dialog() as dlg, ui.card().classes('w-[1000px] max-w-[96vw] p-0 gap-0'):
            with ui.row().classes('w-full justify-end px-2 pt-2'):
                ui.button(icon='close', on_click=dlg.close).props('flat round dense')
            obsah = ui.column().classes('w-full px-6 pb-6 gap-4')

        def kresli():
            if nid not in s['data']['podle_id']:
                dlg.close()         # náklad mezitím smazán
                return
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
        existujici = s['data']['prilohy'].get(n['id'], []) if n else []
        cekajici = []       # nahrané, zatím neuložené přílohy: (název, disk, velikost)

        def hodnota(pole):
            c = _castka(pole.value or '')
            return c if c is not None and c >= 0 else None

        def prepocet():
            ocek, fakt = hodnota(castka), hodnota(faktura)
            rozdil_text.text = '' if ocek is None or fakt is None else f'Rozdíl: {_kc(ocek - fakt, True)}'
            info.clear()
            with info:
                k = kategorie.get(kat.value)
                if not k:
                    return
                volne = k['volne']
                if n and n['kategorie_id'] == k['id']:      # vlastní částka se nepočítá dvakrát
                    volne += n['castka_bez_dph'] if n['faktura_bez_dph'] is None else n['faktura_bez_dph']
                text = f"V kategorii „{k['nazev']}“ zbývá {_kc(volne)} z {_kc(k['rozpocet'])}"
                konecna = fakt if fakt is not None else ocek
                po = volne - konecna if konecna is not None else None
                if po is not None and po < 0:
                    _hlaska(f'{text}. Náklad by rozpočet kategorie překročil o {_kc(-po)}.', 'red')
                elif po is not None and _podil(po, k['rozpocet']) < 1 - VAROVANI_KATEGORIE:
                    _hlaska(f'{text}, po tomto nákladu {_kc(po)} — pod {_pct(1 - VAROVANI_KATEGORIE)}.', 'amber')
                else:
                    ui.label(f'{text}' + (f', po tomto nákladu {_kc(po)}.' if po is not None else '.')) \
                        .classes('text-sm text-gray-600')

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

        def zrusit():
            _smaz_soubory([x[1] for x in cekajici])
            cekajici.clear()
            dlg.close()

        async def ulozit():
            data, chyby = _zpracuj({'nazev': nazev.value, 'kategorie_id': kat.value, 'dodavatel': dodavatel.value,
                                    'ico': ico.value, 'castka': castka.value, 'faktura': faktura.value,
                                    'splatnost': splatnost.value,
                                    'zduvodneni': zduvodneni.value})
            if chyby:
                ui.notify(' '.join(chyby), type='warning', multi_line=True)
                return
            try:
                nid = await asyncio.to_thread(_uloz, user_id, user_name, role, n['id'] if n else None,
                                              data, list(cekajici))
            except Chyba as e:
                ui.notify(str(e), type='warning', multi_line=True)
                return
            except Exception as e:
                ui.notify(f'Náklad se nepodařilo uložit: {e}', type='negative')
                return
            cekajici.clear()        # soubory už patří uloženému nákladu
            dlg.close()
            ui.notify('Náklad uložen.', type='positive')
            intranet_logger.log_activity(user_name, _LOG, f"{_kod(nid)}: {'upraven' if n else 'nový'} náklad "
                                         f"„{data['nazev']}“")
            await obnov()
            if not n:
                detail(nid)

        with host, ui.dialog().props('persistent') as dlg, ui.card().classes('w-[780px] max-w-[96vw] gap-3'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label(f"Upravit náklad {_kod(n['id'])}" if n else 'Nový náklad').classes('text-lg font-semibold')
                if n:
                    _odznak(n['stav'])
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
                dodavatel = ui.input('Dodavatel', value=n['dodavatel'] if n else '') \
                    .props('outlined dense maxlength=255').classes('flex-1')
                ico = ui.input('IČO', value=(n['dodavatel_ico'] or '') if n else '') \
                    .props('outlined dense maxlength=12').classes('w-40')
            with ui.row().classes('w-full gap-3 items-center'):
                castka = ui.input('Očekávaný náklad bez DPH (Kč) *',
                                  value=_cislo_text(n['castka_bez_dph']) if n else '') \
                    .props('outlined dense inputmode=decimal').classes('w-64')
                faktura = ui.input('Faktura bez DPH (Kč)', value=_cislo_text(n['faktura_bez_dph'])
                                   if n and n['faktura_bez_dph'] is not None else '') \
                    .props('outlined dense clearable inputmode=decimal').classes('w-64') \
                    .tooltip('Oficiální částka z faktury bez DPH — vyplňte, až faktura přijde')
                rozdil_text = ui.label().classes('text-sm font-medium text-gray-700')
            zduvodneni = ui.textarea('Poznámka', value=n['zduvodneni'] if n else '',
                                     placeholder='Co náklad zahrnuje, případně porovnání nabídek') \
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
                ui.button('Uložit', icon='save', on_click=ulozit).props('unelevated color=orange-8')
        for prvek in (castka, faktura, kat):
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
                if (zadavatel or spravce) and s['tab'] in ('prehled', 'naklady'):     # jinde se nákladů netýká
                    ui.button('Nový náklad', icon='add', on_click=lambda: formular(None)) \
                        .props('unelevated color=orange-8')

    @ui.refreshable
    def prehled():
        d = s['data']
        p = d['prehled']
        bez_faktury = sum(1 for n in d['naklady'] if n['faktura_bez_dph'] is None)
        with ui.grid().classes('w-full gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-5'):
            _kpi('Celkový rozpočet', _kc(p['celkem']), f"v kategoriích {_kc(p['rozdeleno'])}")
            _kpi('Očekávané náklady', _kc(p['ocekavano']), _pocet(len(d['naklady'])))
            _kpi('Fakturováno', _kc(p['cerpano']), f"bez faktury ještě {_pocet(bez_faktury)} za {_kc(p['ceka'])}")
            _kpi('Rozdíl', _kc(p['rozdil']), 'očekáváno − faktury (kladný = ušetřeno)',
                 'text-red-700' if p['rozdil'] < 0 else 'text-green-700')
            _kpi('Zbývá k dispozici', _kc(p['volne']), 'rozpočet − faktury − očekávané bez faktury',
                 'text-red-700' if p['volne'] < 0 else 'text-green-700')
        with ui.card().classes('w-full p-4 gap-2 shadow-sm'):
            with ui.row().classes('w-full justify-between items-baseline'):
                ui.label('Odpočet rozpočtu').classes('font-semibold')
                ui.label(_do_veletrhu(p['nastaveni'])).classes('text-sm text-gray-600')
            _pruh([(p['cerpano'], _CERPANO_BARVA), (p['ceka'], _CEKA_BARVA)], p['celkem'], 16)
            with ui.row().classes('gap-4 text-xs text-gray-600'):
                for barva, text in ((_CERPANO_BARVA, f"Fakturováno {_kc(p['cerpano'])}"),
                                    (_CEKA_BARVA, f"Očekáváno bez faktury {_kc(p['ceka'])}"),
                                    (_VOLNE_BARVA, f"Volné {_kc(max(p['volne'], 0))}")):
                    with ui.row().classes('items-center gap-1'):
                        _tecka(barva)
                        ui.label(text)
            for text, typ in _varovani(p):
                _hlaska(text, typ)
        with ui.grid().classes('w-full gap-3 grid-cols-1 lg:grid-cols-5'):
            with ui.card().classes('p-4 gap-1 shadow-sm lg:col-span-2'):
                ui.label('Rozpočet, očekáváno a faktury podle kategorií').classes('font-semibold')
                kat = p['kategorie']
                ui.echart({
                    'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'shadow'}, ':valueFormatter':
                                'v => Math.round(v).toLocaleString("cs-CZ") + " Kč"'},
                    'legend': {'bottom': 0},
                    'grid': {'left': 10, 'right': 20, 'top': 10, 'bottom': 40, 'containLabel': True},
                    'xAxis': {'type': 'value',
                              'axisLabel': {':formatter': 'v => (v / 1000).toLocaleString("cs-CZ") + " tis."'}},
                    'yAxis': {'type': 'category', 'inverse': True, 'data': [k['nazev'] for k in kat]},
                    'series': [{'name': nazev, 'type': 'bar', 'data': [float(k[klic]) for k in kat],
                                'itemStyle': {'color': barva}}
                               for nazev, klic, barva in (('Rozpočet', 'rozpocet', '#cbd5e1'),
                                                          ('Očekáváno', 'ocekavano', _CEKA_BARVA),
                                                          ('Fakturováno', 'cerpano', _CERPANO_BARVA))],
                }).classes('w-full h-80')
            with ui.card().classes('p-4 gap-1 shadow-sm lg:col-span-3'):
                ui.label('Kumulativní fakturace vs. plán').classes('font-semibold')
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
                        {'name': 'Fakturováno', 'type': 'line', 'data': g['skutecnost'], 'connectNulls': False,
                         'itemStyle': {'color': _TENTO_BARVA}, 'areaStyle': {'opacity': 0.08},
                         'markLine': {'symbol': 'none', 'silent': True,
                                      'lineStyle': {'color': '#dc2626', 'type': 'dashed'},
                                      'label': {'formatter': 'limit', 'position': 'insideEndTop'},
                                      'data': [{'yAxis': float(p['celkem'])}]}},
                        {'name': 'Plán', 'type': 'line', 'data': g['plan'], 'symbol': 'none',
                         'itemStyle': {'color': '#64748b'}, 'lineStyle': {'type': 'dashed'}},
                    ],
                }).classes('w-full h-80')
        with ui.card().classes('w-full p-4 gap-2 shadow-sm'):
            ui.label('Rozpočet podle kategorií').classes('font-semibold')
            sloupce = (('rozpocet', 'Rozpočet'), ('ocekavano', 'Očekáváno'), ('cerpano', 'Fakturováno'),
                       ('rozdil', 'Rozdíl'), ('volne', 'Volné'))
            with ui.grid(columns='minmax(12rem, 1.6fr) repeat(5, minmax(6.5rem, 1fr))') \
                    .classes('w-full gap-x-3 gap-y-1 items-center text-sm'):
                ui.label('Kategorie').classes('text-xs text-gray-500')
                for _, h in sloupce:
                    ui.label(h).classes('text-xs text-gray-500 text-right')
                for k in p['kategorie']:
                    with ui.row().classes('items-center gap-2 no-wrap'):
                        _tecka(k['barva'], True)
                        ui.label(k['nazev']).classes('font-medium')
                    for klic, _ in sloupce:
                        cls = 'text-right'
                        if klic == 'volne' and (k['prekroceno'] or k['varovani']):
                            cls += ' text-red-700 font-semibold' if k['prekroceno'] else ' text-amber-700'
                        elif klic == 'rozdil' and k['rozdil'] < 0:
                            cls += ' text-red-700'
                        ui.label(_kc(k[klic])).classes(cls)
                    with ui.element('div').classes('col-span-6 pb-1'):
                        _pruh([(k['cerpano'], _CERPANO_BARVA), (k['ceka'], _CEKA_BARVA)], k['rozpocet'], 8)
                ui.label('Součet kategorií').classes('font-semibold')
                soucet = {'rozpocet': p['rozdeleno'], 'ocekavano': p['ocekavano'], 'cerpano': p['cerpano'],
                          'rozdil': p['rozdil'], 'volne': p['rozdeleno'] - p['cerpano'] - p['ceka']}
                for klic, _ in sloupce:
                    ui.label(_kc(soucet[klic])).classes('text-right font-semibold')
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
                        or (mn is not None and n['castka_bez_dph'] < mn)
                        or (mx is not None and n['castka_bez_dph'] > mx)):
                    continue
                if q and q not in ' '.join(str(x or '') for x in (
                        _kod(n['id']), n['nazev'], n['dodavatel'], n['dodavatel_ico'], n['kategorie'],
                        n['zadal_jmeno'], n['zduvodneni'])).lower():
                    continue
                out.append(n)
            return out

        def souhrn(radky):
            ocek = sum((n['castka_bez_dph'] for n in radky), Decimal(0))
            fakt = sum((n['faktura_bez_dph'] or 0 for n in radky), Decimal(0))
            return f'{_pocet(len(radky))} · očekáváno {_kc(ocek)} · fakturováno {_kc(fakt)} (bez DPH)'

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
    def rozpocet():
        p = s['data']['prehled']
        nast = p['nastaveni']
        radky = []
        nove = []       # řádky nových kategorií: {'barva', 'nazev', 'castka', 'prvky'}

        def pridat():
            pouzite = {k['barva'].lower() for k in p['kategorie']} | {(r['barva'].value or '').lower() for r in nove}
            barva_v = next((b for b in ('#0d9488', '#7c3aed', '#db2777', '#ea580c', '#65a30d', '#0284c7',
                                        '#ca8a04') if b not in pouzite), '#64748b')
            with mrizka:
                barva = ui.color_input(value=barva_v).props('outlined dense')
                nazev = ui.input(placeholder='Název nové kategorie').props('outlined dense maxlength=120')
                castka = ui.input(placeholder='0').props('outlined dense inputmode=decimal input-class=text-right')
                with ui.element('div').classes('col-span-3 flex justify-end') as konec:
                    ui.button(icon='close', on_click=lambda: odebrat(r)) \
                        .props('flat round dense color=grey-7').tooltip('Odebrat řádek')
            r = {'barva': barva, 'nazev': nazev, 'castka': castka, 'prvky': (barva, nazev, castka, konec)}
            nove.append(r)
            castka.on_value_change(prepocet)
            return r

        def odebrat(r):
            for prvek in r['prvky']:
                prvek.delete()
            nove.remove(r)
            prepocet()

        def pridat_a_fokus():
            pridat()['nazev'].run_method('focus')
            prepocet()

        def prepocet():
            celkem = _castka(celkem_in.value or '') or Decimal(0)
            soucet = sum((_castka(c.value or '') or Decimal(0) for _, _, c, _ in radky), Decimal(0))
            soucet += sum((_castka(r['castka'].value or '0') or Decimal(0) for r in nove), Decimal(0))
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
            nove_kat = [kategorie_z(r['nazev'].value, r['castka'].value, r['barva'].value) for r in nove
                        if (r['nazev'].value or '').strip() or (r['castka'].value or '').strip()]
            jmena = set()
            for k in kat + nove_kat:
                if not k['nazev']:
                    chyby.append('Název kategorie nesmí být prázdný.')
                elif k['nazev'].casefold() in jmena:
                    chyby.append(f"Kategorie „{k['nazev']}“ je v seznamu dvakrát.")
                jmena.add(k['nazev'].casefold())
                if k['rozpocet'] is None or k['rozpocet'] < 0:
                    chyby.append(f"Neplatný rozpočet kategorie „{k['nazev']}“.")
                if not k['barva']:
                    chyby.append(f"Neplatná barva kategorie „{k['nazev']}“ (#RRGGBB).")
            if not chyby and sum(k['rozpocet'] for k in kat + nove_kat) > celkem:
                chyby.append('Součet rozpočtů kategorií převyšuje celkový rozpočet — navyšte celek, '
                             'nebo snižte kategorie.')
            if chyby:
                ui.notify(' '.join(dict.fromkeys(chyby)), type='warning', multi_line=True)
                return
            try:
                zmeny = await asyncio.to_thread(
                    _uloz_rozpocet, user_id, user_name,
                    {'akce': (akce_in.value or '').strip()[:120] or _VYCHOZI_NASTAVENI['akce'],
                     'datum_akce': datum.isoformat(), 'plan_od': plan, 'rozpocet': celkem}, kat, nove_kat)
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
                    .classes('w-full gap-x-3 gap-y-2 items-center text-sm') as mrizka:
                for h in ('Barva', 'Kategorie', 'Rozpočet (Kč)', 'Fakturováno', 'Bez faktury', 'Volné'):
                    ui.label(h).classes('text-xs text-gray-500' + (' text-right' if h in ('Fakturováno', 'Bez faktury', 'Volné')
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
            pridat()
            ui.button('Přidat kategorii', icon='add', on_click=pridat_a_fokus) \
                .props('flat dense no-caps color=orange-8').classes('self-start')
            souhrn = ui.label()
            ui.button('Uložit rozpočet', icon='save', on_click=ulozit).props('unelevated color=orange-8')
        for prvek in [celkem_in] + [c for _, _, c, _ in radky]:
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
        with ui.row().classes('w-full gap-2 items-center'):
            ui.button('Export do Excelu', icon='table_view', on_click=lambda: export_xlsx(d['naklady'])) \
                .props('unelevated color=green-8')
            ui.button('PDF report pro vedení', icon='picture_as_pdf', on_click=export_pdf).props('unelevated color=red-8')
            ui.label('Excel obsahuje všechny náklady a list Rozpočet; export podle filtrů je v záložce Náklady.') \
                .classes('text-xs text-gray-500')
        sloupce = ['Nákladů', 'Očekáváno', 'Fakturováno']
        with ui.grid().classes('w-full gap-3 grid-cols-1 xl:grid-cols-3'):
            _karta_tabulka('Podle stavu', ['Stav', *sloupce],
                           [(_STAVY[k][0], c, _kc(o), _kc(f)) for k, c, o, f in sou['stavy']])
            _karta_tabulka('Podle zadavatelů', ['Zadavatel', *sloupce],
                           [(j, c, _kc(o), _kc(f)) for j, c, o, f in sou['autori']])
            _karta_tabulka('Podle měsíců splatnosti', ['Měsíc', *sloupce],
                           [(m, c, _kc(o), _kc(f)) for m, c, o, f in sou['mesice']])

    # ---------- organizátor ----------
    async def obnov_plany(vynutit=True):
        """Načte Organizátor. Bez vynucení (automatika) překreslí jen při změně dat a chyby nehlásí."""
        verze = _ORG_VERZE          # před čtením — zápis během čtení se chytí v dalším tiku
        try:
            data = await asyncio.to_thread(_nacti_plany)
        except Exception as e:
            if vynutit:
                ui.notify(f'Organizátor se nepodařilo načíst: {e}', type='negative')
            return
        s['org_verze'], s['org_cas'] = verze, time.monotonic()
        if vynutit or data != s['plany']:
            s['plany'], s['fokus'] = data, False     # nová pole nemají fokus
            organizator.refresh()

    async def auto_obnova_planu():
        if taby.value != 'organizator' or s['fokus']:      # nepřekreslovat pod rukama při psaní
            return
        if s['org_verze'] == _ORG_VERZE and time.monotonic() - s['org_cas'] < ORG_POJISTKA_S:
            return
        await obnov_plany(vynutit=False)

    async def pri_prepnuti(e):
        s['tab'] = e.value
        hlavicka.refresh()          # tlačítko Nový náklad jen na záložkách, kterých se týká
        if org and e.value == 'organizator':
            await obnov_plany(vynutit=False)

    async def plan_akce(akce, cil=None, hodnota='', koncept=None):
        try:
            await asyncio.to_thread(_plan_akce, user_id, user_name, role, akce, cil, hodnota)
        except Chyba as e:
            ui.notify(str(e), type='warning')       # stav se mohl změnit — níž načíst aktuální
        except Exception as e:
            ui.notify(f'Akci se nepodařilo provést: {e}', type='negative')
            return
        else:
            _org_zmena()
            s['koncepty'].pop(koncept, None)
        await obnov_plany()

    def pole(klic, placeholder):
        """Textarea, jejíž rozepsaný text přežije překreslení; při fokusu automatika čeká."""
        def fokus(stav):
            s['fokus'] = stav

        def koncept(e):
            s['koncepty'][klic] = e.value or ''

        return ui.textarea(placeholder=placeholder, value=s['koncepty'].get(klic, ''), on_change=koncept) \
            .props('outlined dense autogrow').classes('flex-1') \
            .on('focus', lambda: fokus(True)).on('blur', lambda: fokus(False))

    async def dotaz(titulek, tlacitko, barva, text=None):
        """Dialog: s textem vrátí upravený text, bez textu True; Zpět / zavření = None."""
        with host, ui.dialog() as dlg, ui.card().classes('w-[520px] max-w-[95vw] gap-3'):
            ui.label(titulek).classes('text-lg font-semibold')
            txt = None if text is None else \
                ui.textarea(value=text).props('outlined autogrow autofocus').classes('w-full')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zpět', on_click=lambda: dlg.submit(None)).props('flat')
                ui.button(tlacitko, on_click=lambda: dlg.submit(True if txt is None else txt.value)) \
                    .props(f'unelevated color={barva}')
        vysledek = await dlg
        dlg.delete()
        return vysledek

    async def upravit_plan(p):
        text = await dotaz('Upravit bod plánu', 'Uložit', 'orange-8', p['obsah'])
        if text is not None:
            await plan_akce('upravit', p['id'], text)

    async def smazat_plan(pid):
        if await dotaz('Smazat bod plánu i s komentáři?', 'Smazat', 'red-7'):
            await plan_akce('smazat', pid)

    async def smazat_komentar(kid):
        if await dotaz('Smazat komentář?', 'Smazat', 'red-7'):
            await plan_akce('smazat_komentar', kid)

    def rozbal(pid, otevreno):
        (s['rozbalene'].add if otevreno else s['rozbalene'].discard)(pid)

    def plan_ui(p, komentare):
        pid, vlastni = p['id'], p['user_id'] == user_id
        with ui.column().classes('w-full gap-0 border-t border-gray-100 pt-2'):
            with ui.row().classes('w-full items-start gap-2 no-wrap'):
                ui.checkbox(value=bool(p['hotovo']), on_change=lambda e: plan_akce('hotovo', pid, e.value)) \
                    .props('dense color=green-7' + ('' if vlastni else ' disable')).classes('mt-0.5')
                ui.label(p['obsah']).classes('text-sm whitespace-pre-line break-words flex-1 min-w-0'
                                             + (' line-through text-gray-400' if p['hotovo'] else ''))
                if vlastni:
                    ui.button(icon='edit', on_click=lambda: upravit_plan(p)) \
                        .props('flat round dense size=sm color=grey-7').tooltip('Upravit')
                    ui.button(icon='delete', on_click=lambda: smazat_plan(pid)) \
                        .props('flat round dense size=sm color=grey-7').tooltip('Smazat')
            with ui.expansion(f'Komentáře ({len(komentare)})' if komentare else 'Komentovat',
                              icon='chat_bubble_outline', value=pid in s['rozbalene'],
                              on_value_change=lambda e: rozbal(pid, e.value)) \
                    .props('dense header-class="text-xs text-gray-500 px-0"').classes('w-full'):
                for k in komentare:
                    with ui.row().classes('w-full gap-2 items-start no-wrap'):
                        with ui.column().classes('gap-0 flex-1 min-w-0'):
                            ui.label(f"{k['kdo'] or ''} · {_cas(k['cas'])}").classes('text-xs text-gray-500')
                            ui.label(k['obsah']).classes('text-sm text-gray-700 whitespace-pre-line break-words '
                                                         'bg-gray-50 rounded px-2 py-1')
                        if k['user_id'] == user_id:
                            ui.button(icon='delete', on_click=lambda _=None, kid=k['id']: smazat_komentar(kid)) \
                                .props('flat round dense size=sm color=grey-6').tooltip('Smazat komentář')
                with ui.row().classes('w-full items-end gap-2 no-wrap'):
                    txt = pole(pid, 'Napsat komentář…')

                    async def poslat():
                        if (txt.value or '').strip():
                            await plan_akce('komentar', pid, txt.value, koncept=pid)

                    ui.button(icon='send', on_click=poslat).props('round unelevated dense color=orange-8') \
                        .tooltip('Přidat komentář')

    @ui.refreshable
    def organizator():
        with ui.card().classes('w-full gap-2 shadow-sm'):
            with ui.row().classes('w-full items-center gap-2'):
                ui.label('Nový bod plánu').classes('font-semibold')
                ui.label('Vlastní body odškrtnete, upravíte a smažete; komentovat můžete všechny.') \
                    .classes('text-xs text-gray-500')
                ui.space()
                ui.button(icon='refresh', on_click=obnov_plany).props('flat round dense color=grey-7') \
                    .tooltip('Načíst změny ostatních')
            with ui.row().classes('w-full items-end gap-2 no-wrap'):
                novy = pole('novy', 'Co mám v plánu…')

                async def pridat():
                    if (novy.value or '').strip():
                        await plan_akce('pridat', hodnota=novy.value, koncept='novy')

                ui.button('Přidat', icon='add', on_click=pridat).props('unelevated color=orange-8')
        if s['plany'] is None:
            ui.label('Organizátor se nepodařilo načíst.').classes('text-red-600')
            return
        autori = defaultdict(list)
        for p in s['plany']['plany']:
            autori[p['user_id']].append(p)
        if not autori:
            ui.label('Zatím tu nikdo nemá žádný plán.').classes('text-sm text-gray-400')
            return
        with ui.grid().classes('w-full gap-3 grid-cols-1 xl:grid-cols-2'):
            for uid in sorted(autori, key=lambda u: (u != user_id, (autori[u][-1]['kdo'] or '').lower())):
                plany = autori[uid]
                hotovych = sum(bool(p['hotovo']) for p in plany)
                with ui.card().classes('w-full gap-2 shadow-sm'):
                    with ui.row().classes('w-full items-center gap-2'):
                        ui.label('Můj plán' if uid == user_id else plany[-1]['kdo'] or '—').classes('font-semibold')
                        ui.space()
                        ui.label(f'hotovo {hotovych} z {len(plany)}').classes('text-xs text-gray-500')
                    _pruh([(hotovych, '#16a34a')], len(plany), 6)
                    for p in plany:
                        plan_ui(p, s['plany']['komentare'].get(p['id'], []))

    # ---------- stránka ----------
    start = 'prehled'
    s['tab'] = start
    if org:
        await obnov_plany()
    with ui.column().classes('w-full gap-3'):
        hlavicka()
        with ui.tabs().props('dense no-caps inline-label align=left active-color=orange-9 indicator-color=orange-9') \
                .classes('w-full text-gray-700 border-b border-gray-200') as taby:
            ui.tab('prehled', 'Přehled', icon='insights')
            ui.tab('naklady', 'Náklady', icon='receipt_long')
            if spravce:
                ui.tab('rozpocet', 'Rozpočet', icon='account_balance_wallet')
            ui.tab('reporty', 'Reporty a export', icon='download')
            if org:
                ui.tab('organizator', 'Organizátor', icon='checklist')
        with ui.tab_panels(taby, value=start).classes('w-full bg-transparent'):
            with ui.tab_panel('prehled').classes('p-0 gap-3'):
                prehled()
            with ui.tab_panel('naklady').classes('p-0 gap-3'):
                seznam()
            if spravce:
                with ui.tab_panel('rozpocet').classes('p-0 gap-3'):
                    rozpocet()
            with ui.tab_panel('reporty').classes('p-0 gap-3'):
                reporty()
            if org:
                with ui.tab_panel('organizator').classes('p-0 gap-3'):
                    organizator()
    taby.on_value_change(pri_prepnuti)
    if org:
        with host:
            ui.timer(ORG_TIK_S, auto_obnova_planu)
    if deep is not None:
        if deep in s['data']['podle_id']:
            taby.set_value('naklady')
            detail(deep)
        else:
            ui.notify('Náklad z odkazu nenalezen, nebo k němu nemáte přístup.', type='warning')


def _zpracuj(h) -> tuple[dict, list[str]]:
    """Hodnoty formuláře → data pro DB a chyby. Povinné: název, kategorie, očekávaná částka."""
    chyby = []
    nazev = (h['nazev'] or '').strip()[:255]
    dodavatel = (h['dodavatel'] or '').strip()[:255]
    ico = ''.join((h['ico'] or '').split())[:20]
    zduvodneni = (h['zduvodneni'] or '').strip()[:5000]
    bez = _castka(h['castka'] or '')
    faktura_text = (h['faktura'] or '').strip()
    faktura = _castka(faktura_text) if faktura_text else None
    splatnost = _cz_datum(h['splatnost'])
    if not nazev:
        chyby.append('Vyplňte název nákladu.')
    if not h['kategorie_id']:
        chyby.append('Vyberte kategorii.')
    if ico and not ico.isdigit():
        chyby.append('IČO smí obsahovat jen číslice.')
    if bez is None or bez <= 0:
        chyby.append('Zadejte platný očekávaný náklad bez DPH.')
    if faktura_text and (faktura is None or faktura < 0):
        chyby.append('Částku z faktury zadejte jako číslo, nebo pole nechte prázdné.')
    if (h['splatnost'] or '').strip() and not splatnost:
        chyby.append('Datum splatnosti zadejte ve tvaru DD.MM.RRRR.')
    if chyby:
        return {}, chyby
    return {'nazev': nazev, 'kategorie_id': h['kategorie_id'], 'dodavatel': dodavatel, 'dodavatel_ico': ico or None,
            'castka_bez_dph': bez, 'faktura_bez_dph': faktura, 'splatnost': splatnost, 'zduvodneni': zduvodneni}, []
