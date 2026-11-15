"""Lupou na obchod — analytika prodejů ASM.

Etapa 1: schéma, dávkový import GIST prodejů, párování ASM ↔ uživatel, dlaždice.

Přístup se NEŘÍDÍ per-ASM právy. Odvozuje se:
  ASM     → příjmení v datech se páruje s user.surname (bez diakritiky)
  Vedoucí → má právo hlavni_vedouci_{oddělení} → vidí všechna ASM v tom oddělení
  Extra   → lupa_extra    (vše + komentáře, záložka 3)
  Vkladatel → lupa_vkladatel (jen import)
  Admin   → lupa_admin / vse
"""

import asyncio
import datetime
import html
import io
import math
import os
import re
import tempfile
import threading
import time
import unicodedata

from nicegui import ui, app

import intranet_data
import intranet_logger
import intranet_notifikace
from intranet_ui_utils import prekryv_kolecko

# ============================================================================
# Konstanty
# ============================================================================

# Import běží paralelně. Kolidují jen dvě místa a ta se hlídají zvlášť:
#   1) partice (asm, rok, mesic) — dva importy téhož ASM by si rozbily DELETE+INSERT
#      → _zamek_asm(), jedno ASM naráz. Různá ASM sahají na disjunktní řádky.
#   2) dimenze (produkt, zákazník, dealer) — sdílené klíče napříč ASM, souběžné
#      ON DUPLICATE KEY UPDATE = deadlock → _DIMENZE_ZAMEK, zápis i commit uvnitř.
_IMPORT_SOUBEZNE = 3   # každý běh drží XLSX v paměti a jedno DB spojení
_IMPORT_SEM = asyncio.Semaphore(_IMPORT_SOUBEZNE)
_ASM_ZAMKY = {}
# Běžící importy napříč celým procesem: id(stav) -> stav. Drží je task na event
# loopu, ne klient — odchod ze stránky import nezastaví, jen přijde o UI.
_BEZICI_IMPORTY = {}
_DIMENZE_ZAMEK = threading.Lock()   # vlákna, ne event loop — drží ho importuj_soubor
_DAVKA = 2000        # řádků na jeden executemany
_COMMIT_PO = 50_000  # commit po N řádcích (aby neroste undo log)
_CACHE_TTL = 600.0   # s — mapy ASM↔uživatel↔oddělení

_DB_INIT_HOTOVO = False

# Sloupce zdrojového xlsx. Párujeme podle NÁZVU v hlavičce, ne podle pozice —
# export mezi verzemi mění pořadí i psaní ("K. jméno" → "K jméno",
# "Produkt - kód" → "Produkt - Kód") a nový sloupec "K ulice" přibyl doprostřed.
_SLOUPCE = {
    'asm':                'asm',
    'dealer':             'dealer',
    'dealer jmeno':       'dealer_jmeno',
    'mesic':              'mesic',
    'ico':                'ico',
    'jmeno':              'jmeno',
    'k jmeno':            'k_jmeno',
    'k mesto':            'k_mesto',
    'k ulice':            'k_ulice',
    'site':               'site',
    'produkt kod':        'kod',
    'produkt nazev':      'nazev',
    'dodavatel popis':    'dodavatel',
    'obrat v mj':         'obrat_mj',
    'obrat v kc bez dph': 'obrat_kc',
}

_POVINNE = ('asm', 'mesic', 'ico', 'kod', 'obrat_mj', 'obrat_kc')

# Karty zboží (.xlsb) — hlavička na 5. řádku, data od 6.
_KARTY_SLOUPCE = {'kod9': 'kod', 'skupina2 popis': 'skupina'}

_KARTY_KLIC = '#KARTY'   # pseudo-ASM pro zámek a protokol; slug() ho nevyrobí

_MESIC_RE = re.compile(r'^\s*(\d{4})\s*M\s*(\d{1,2})\s*$', re.I)


# ============================================================================
# Pomocné
# ============================================================================

def _bez_diakritiky(text):
    t = unicodedata.normalize('NFD', str(text or ''))
    return ''.join(ch for ch in t if unicodedata.category(ch) != 'Mn')


def slug(text):
    """'Korábečný' → 'KORABECNY'. Klíč pro párování ASM ↔ příjmení."""
    return re.sub(r'[^A-Z0-9]+', '', _bez_diakritiky(text).upper())


def zaklad_asm(hodnota):
    """'BRAUN-VO' → 'BRAUN'. Sufixy -K / -VO / -ALG patří pod stejné ASM."""
    return slug(str(hodnota or '').split('-')[0])


def klic_ze_souboru(nazev_souboru):
    """'data prodeje zak3 – Weinberger.xlsx' → 'WEINBERGER'.

    Klíč bereme z názvu souboru, ne z dat: kód v datech je useknutý na 8 znaků,
    takže Weinberger má v jednom souboru 'WEINBERGER' i 'WEINBERG-K'. Název
    souboru je zároveň příjmení, na které se ASM páruje s uživatelem.
    """
    zaklad = re.split(r'[–—-]', str(nazev_souboru or '').rsplit('.', 1)[0])[-1]
    return slug(zaklad)


def _sedi_asm(zaklad, klic):
    """'WEINBERG' vs 'WEINBERGER' = totéž ASM (useknutý kód)."""
    return bool(zaklad) and (zaklad.startswith(klic) or klic.startswith(zaklad))


def _norm_hlavicka(text):
    """'Obrat v Kč bez DPH' → 'obrat v kc bez dph'."""
    t = _bez_diakritiky(text).lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', t)).strip()


def _cislo(hodnota):
    """Číslo z buňky. Excel dává float; textový export dává '1 234,50' / nbsp."""
    if hodnota is None or hodnota == '':
        return 0.0
    if isinstance(hodnota, (int, float)):
        return float(hodnota)
    t = str(hodnota).replace('\xa0', '').replace(' ', '').replace(',', '.')
    try:
        return float(t)
    except ValueError:
        return 0.0


def _ano_ne(hodnota):
    """Buňka 'Ano'/'Ne' (i 1/0) → 1/0. Prázdno = Ne."""
    if isinstance(hodnota, (int, float)) and not isinstance(hodnota, bool):
        return 1 if hodnota else 0
    return 1 if str(hodnota or '').strip().lower().startswith(('a', 'y', 't')) else 0


def _obdobi(hodnota):
    """'2026M01' → (2026, 1). Nerozpoznané → (None, None)."""
    m = _MESIC_RE.match(str(hodnota or ''))
    if not m:
        return None, None
    mes = int(m.group(2))
    return (int(m.group(1)), mes) if 1 <= mes <= 12 else (None, None)


# ============================================================================
# Schéma
# ============================================================================

def inicializace_db():
    global _DB_INIT_HOTOVO
    if _DB_INIT_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Fakta. ~7,1 M řádků za 20 souborů → texty jdou do dimenzí, tady jen klíče.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_obrat (
                id       BIGINT AUTO_INCREMENT PRIMARY KEY,
                asm      VARCHAR(40) NOT NULL,
                dealer   VARCHAR(40) NOT NULL DEFAULT '',
                ico      VARCHAR(40) COLLATE utf8mb4_bin NOT NULL,
                kod      VARCHAR(40) NOT NULL DEFAULT '',
                rok      SMALLINT NOT NULL,
                mesic    TINYINT NOT NULL,
                obrat_mj DOUBLE DEFAULT 0,
                obrat_kc DOUBLE DEFAULT 0,
                INDEX idx_asm_obd (asm, rok, mesic),
                INDEX idx_ico_obd (ico, rok, mesic),
                INDEX idx_dealer (dealer, rok, mesic),
                INDEX idx_kod (kod)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Dimenze. ico má utf8mb4_bin shodně s asm_kontakty_oz — jinak JOIN nesedne.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_zakaznik (
                ico     VARCHAR(40) COLLATE utf8mb4_bin PRIMARY KEY,
                jmeno   VARCHAR(255),
                k_jmeno VARCHAR(255),
                k_ulice VARCHAR(255),
                k_mesto VARCHAR(255),
                asm     VARCHAR(40),
                site    TINYINT NOT NULL DEFAULT 0,
                INDEX idx_asm (asm)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Migrace: sloupec Sítě přibyl do exportu až dodatečně. NULL nesmí
        # zůstat — kód slibuje NOT NULL, aby 'WHERE site=0' bylo past pro staré
        # zákazníky.
        cur.execute("SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE "
                    "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='lupa_zakaznik' AND "
                    "COLUMN_NAME='site'")
        radek = cur.fetchone()
        if not radek:
            cur.execute('ALTER TABLE lupa_zakaznik '
                        'ADD COLUMN site TINYINT NOT NULL DEFAULT 0')
        elif radek[0] == 'YES':
            cur.execute('UPDATE lupa_zakaznik SET site = 0 WHERE site IS NULL')
            cur.execute('ALTER TABLE lupa_zakaznik '
                        'MODIFY site TINYINT NOT NULL DEFAULT 0')
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_produkt (
                kod       VARCHAR(40) PRIMARY KEY,
                nazev     VARCHAR(255),
                dodavatel VARCHAR(255),
                INDEX idx_dod (dodavatel)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Katalog karet zboží (samostatný import .xlsb). Vlastní tabulka, ne
        # sloupec v lupa_produkt: katalog má ~123 tis. karet proti ~18 tis.
        # prodaných produktů a plnit jím dimenzi by rozbilo volby dodavatelů.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_karta (
                kod     VARCHAR(40) PRIMARY KEY,
                skupina VARCHAR(255)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_dealer (
                dealer VARCHAR(40) PRIMARY KEY,
                jmeno  VARCHAR(255),
                asm    VARCHAR(40),
                INDEX idx_asm (asm)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Souhrn per ASM — dlaždice se z něj kreslí bez dotazu do 7M tabulky.
        # user_id = spárovaný obchoďák (auto dle příjmení, admin může přepsat).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_asm (
                asm           VARCHAR(40) PRIMARY KEY,
                user_id       INT DEFAULT NULL,
                radku         BIGINT DEFAULT 0,
                zakazniku     INT DEFAULT 0,
                obdobi_od     VARCHAR(7) DEFAULT '',
                obdobi_do     VARCHAR(7) DEFAULT '',
                obrat_kc      DOUBLE DEFAULT 0,
                aktualizovano DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user (user_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Poznámky ASM. Vážou se na ASM, ne na uživatele — když se obchoďák
        # vymění, poznámky k území zůstanou nástupci.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_poznamka (
                id        INT AUTO_INCREMENT PRIMARY KEY,
                asm       VARCHAR(40) NOT NULL,
                user_id   INT,
                uzivatel  VARCHAR(200),
                text      TEXT,
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP,
                upraveno  DATETIME DEFAULT NULL,
                INDEX idx_asm (asm, id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Komentáře extra čtenářů k poznámkám. Mazání poznámky je uklidí ručně —
        # cizí klíč nedáváme, zbytek modulu ho taky nemá.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_komentar (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                poznamka_id INT NOT NULL,
                user_id     INT,
                uzivatel    VARCHAR(200),
                text        TEXT,
                vytvoreno   DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_poznamka (poznamka_id, id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Lupou na nákup — odběry z GIST (měsíc × divize × pobočka × odběratel ×
        # produkt). Nákupčí a dodavatel v GIST nejsou: dopárují se podle kódu
        # z DATA_POROVNANI (cenopripad_master) do lupa_nakup_produkt. Kolace jako
        # u cenopripad_master, jinak JOIN na kód spadne na „Illegal mix of collations“.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_nakup_obrat (
                id       BIGINT AUTO_INCREMENT PRIMARY KEY,
                rok      SMALLINT NOT NULL,
                mesic    TINYINT  NOT NULL,
                divize   VARCHAR(60)  NOT NULL DEFAULT '',
                pobocka  VARCHAR(100) NOT NULL DEFAULT '',
                ico      VARCHAR(20)  NOT NULL DEFAULT '',
                jmeno    VARCHAR(255) NOT NULL DEFAULT '',
                kod      VARCHAR(40)  NOT NULL,
                nazev    VARCHAR(255) NOT NULL DEFAULT '',
                obrat_mj DOUBLE NOT NULL DEFAULT 0,
                obrat_kc DOUBLE NOT NULL DEFAULT 0,
                INDEX idx_obd (rok, mesic),
                INDEX idx_kod (kod)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_nakup_produkt (
                kod             VARCHAR(40) PRIMARY KEY,
                nak             VARCHAR(10)  DEFAULT NULL,
                dodavatel       VARCHAR(40)  DEFAULT NULL,
                dodavatel_nazev VARCHAR(255) DEFAULT NULL,
                sortiment_popis VARCHAR(255) DEFAULT NULL,
                skupina2_popis  VARCHAR(255) DEFAULT NULL,
                sparovano       DATETIME     DEFAULT NULL,
                INDEX idx_nak (nak),
                INDEX idx_dod (dodavatel)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Migrace: popis sortimentu a kategorie (Skupina2-popis) pro měsíční report
        # nákupčím přibyly dodatečně; vyplní je nejbližší dopárování.
        for _col in ('sortiment_popis', 'skupina2_popis'):
            cur.execute("SELECT 1 FROM information_schema.COLUMNS WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='lupa_nakup_produkt' AND "
                        "COLUMN_NAME=%s", (_col,))
            if not cur.fetchall():
                cur.execute(f'ALTER TABLE lupa_nakup_produkt '
                            f'ADD COLUMN {_col} VARCHAR(255) DEFAULT NULL')
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_nakup_import (
                id       INT AUTO_INCREMENT PRIMARY KEY,
                kdy      DATETIME DEFAULT CURRENT_TIMESTAMP,
                uzivatel VARCHAR(200),
                soubor   VARCHAR(255),
                radku    INT DEFAULT 0,
                obdobi   VARCHAR(1000)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        # Měsíční report nákupčím: co už odešlo (období „RRRR-MM“ × zkratka).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lupa_nakup_report (
                obdobi   CHAR(7)     NOT NULL,
                kod      VARCHAR(10) NOT NULL,
                odeslano DATETIME DEFAULT CURRENT_TIMESTAMP,
                prijemci TEXT,
                PRIMARY KEY (obdobi, kod)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        conn.commit()
        cur.close()
        _DB_INIT_HOTOVO = True
    except Exception as e:
        print(f'[lupa] inicializace_db: {e}')
    finally:
        if conn:
            conn.close()


_AKCE_NAZVY = {
    'nahled': 'Náhled odběratelů',
    'export_pdf': 'Export PDF — odběratelé',
    'export_xlsx': 'Export XLSX — odběratelé',
    'export_xlsx_detail': 'Export XLSX — detailní pohled',
    'obraty_nahled': 'Náhled obratů',
    'obraty_export_pdf': 'Export PDF — obraty',
    'obraty_export_xlsx': 'Export XLSX — obraty',
    'srovnani_nahled': 'Náhled srovnání zákazníků',
    'srovnani_export_xlsx': 'Export XLSX — srovnání zákazníků',
    'import': 'Import dat',
    'import_karty': 'Import karet zboží',
    'poznamka': 'Poznámka',
    'poznamka_edit': 'Úprava poznámky',
    'poznamka_smaz': 'Smazání poznámky',
    'komentar': 'Komentář',
    'komentar_smaz': 'Smazání komentáře',
    'nakup_import': 'Lupou na nákup — import GIST',
    'nakup_parovani': 'Lupou na nákup — dopárování z DATA_POROVNANI',
    'nakup_report': 'Lupou na nákup — měsíční e-mail nákupčím',
    'nakup_nahled': 'Lupou na nákup — výjezd',
    'nakup_tisk': 'Lupou na nákup — tisk výjezdu',
    'nakup_export_pdf': 'Lupou na nákup — export PDF výjezdu',
    'nakup_export_xlsx': 'Lupou na nákup — export Excel výjezdu',
    'nakup_srovnani': 'Lupou na nákup — srovnání dodavatelů',
    'nakup_detail': 'Lupou na nákup — detail srovnání',
    'nakup_srovnani_tisk': 'Lupou na nákup — tisk srovnání',
    'nakup_srovnani_pdf': 'Lupou na nákup — export PDF srovnání',
    'nakup_srovnani_xlsx': 'Lupou na nákup — export Excel srovnání',
}


def zapis_log(user_id, uzivatel, akce, asm='', parametry='', pocet_radku=0):
    """Audit výjezdů do centrální audit konzole (úroveň „Lupa").

    Soubory se neukládají, jen nastavení, se kterým se export vyjížděl.
    `user_id` konzole nezná — identifikuje jménem, drží se kvůli volajícím."""
    casti = [_AKCE_NAZVY.get(akce, akce)]
    if asm:
        casti.append(f'ASM {asm.capitalize()}')
    if pocet_radku:
        casti.append(f'{pocet_radku} řádků')
    if parametry:
        casti.append(str(parametry)[:2000])
    intranet_logger.log_activity(uzivatel, 'Lupa', ' · '.join(casti))


# ============================================================================
# Poznámky a komentáře
# ============================================================================

def _zapis(sql, par):
    """Jednorázový zápis. Vrací lastrowid, nebo None když DB neběží."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(sql, par)
        conn.commit()
        nove = cur.lastrowid
        cur.close()
        return nove
    except Exception as e:
        print(f'[lupa] zapis: {e}')
        return None
    finally:
        conn.close()


def poznamky(asm):
    """Poznámky ASM od nejnovější, každá s komentáři od nejstaršího."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute('SELECT * FROM lupa_poznamka WHERE asm=%s ORDER BY id DESC', (asm,))
        radky = cur.fetchall()
        for p in radky:
            p['komentare'] = []
        if radky:
            dle_id = {p['id']: p for p in radky}
            cur.execute(
                'SELECT * FROM lupa_komentar WHERE poznamka_id IN ({}) ORDER BY id'
                .format(','.join(['%s'] * len(dle_id))), tuple(dle_id))
            for k in cur.fetchall():
                dle_id[k['poznamka_id']]['komentare'].append(k)
        cur.close()
        return radky
    except Exception as e:
        print(f'[lupa] poznamky: {e}')
        return []
    finally:
        conn.close()


def pridej_poznamku(asm, user_id, uzivatel, text):
    return _zapis('INSERT INTO lupa_poznamka (asm, user_id, uzivatel, text) '
                  'VALUES (%s,%s,%s,%s)', (asm, user_id, uzivatel, text[:60000]))


def uprav_poznamku(pid, text):
    _zapis('UPDATE lupa_poznamka SET text=%s, upraveno=NOW() WHERE id=%s',
           (text[:60000], pid))


def smaz_poznamku(pid):
    _zapis('DELETE FROM lupa_komentar WHERE poznamka_id=%s', (pid,))
    _zapis('DELETE FROM lupa_poznamka WHERE id=%s', (pid,))


def pridej_komentar(pid, user_id, uzivatel, text):
    return _zapis('INSERT INTO lupa_komentar (poznamka_id, user_id, uzivatel, text) '
                  'VALUES (%s,%s,%s,%s)', (pid, user_id, uzivatel, text[:60000]))


def smaz_komentar(kid):
    _zapis('DELETE FROM lupa_komentar WHERE id=%s', (kid,))


# ============================================================================
# Import
# ============================================================================

def _radky_xlsx(raw_bytes, stav=None):
    """Řádky prvního listu. read_only → drží konstantní paměť i u 900k řádků."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        if stav is not None:
            stav['celkem'] = ws.max_row   # z dimenze listu, None když ji soubor nemá
        for r in ws.iter_rows(values_only=True):
            yield r
    finally:
        wb.close()


def _najdi_hlavicku(radek):
    """Mapa {interní_název: index} pokud je řádek hlavička, jinak None."""
    if not radek:
        return None
    mapa = {}
    for i, bunka in enumerate(radek):
        klic = _SLOUPCE.get(_norm_hlavicka(bunka))
        if klic and klic not in mapa:
            mapa[klic] = i
    return mapa if all(k in mapa for k in _POVINNE) else None


def _radky_xlsb(raw_bytes, stav=None):
    """Řádky prvního listu .xlsb. pyxlsb streamuje stejně jako openpyxl read_only."""
    from pyxlsb import open_workbook
    with open_workbook(io.BytesIO(raw_bytes)) as wb:
        with wb.get_sheet(1) as ws:
            if stav is not None:
                stav['celkem'] = ws.dimension.h or None
            for radek in ws.rows():
                yield [b.v for b in radek]


def _kod_karty(hodnota):
    """'40207992!' → '40207992'. Vykřičník má jen katalog, prodeje ho nemají."""
    if isinstance(hodnota, float) and hodnota.is_integer():
        hodnota = int(hodnota)
    return str(hodnota if hodnota is not None else '').strip().rstrip('!')


def _uloz_karty(cur, davka):
    cur.executemany(
        'INSERT INTO lupa_karta (kod, skupina) VALUES (%s,%s) '
        'ON DUPLICATE KEY UPDATE skupina=VALUES(skupina)', davka)


def importuj_karty(raw_bytes, nazev_souboru, stav=None):
    """Katalog karet zboží (.xlsb) → lupa_karta. Vrací dict jako importuj_soubor.

    Jen upsert, nic nemaže: zrušená karta navíc v číselníku nevadí, kdežto
    výpadek skupiny u prodané položky by v rozpadu zel prázdnem.
    """
    stav = stav if stav is not None else {}
    stav.update(faze='otevírám soubor', hotovo=0, celkem=None)
    vysledek = {'soubor': nazev_souboru, 'asm': _KARTY_KLIC, 'radku': 0,
                'preskoceno': 0, 'cizi': 0, 'obdobi': [], 'chyba': None}
    inicializace_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        vysledek['chyba'] = 'Není spojení s databází.'
        return vysledek

    davka = []
    od_commitu = 0
    cur = None
    try:
        cur = conn.cursor()
        mapa = None
        stav['faze'] = 'načítám řádky'
        for i, radek in enumerate(_radky_xlsb(raw_bytes, stav), 1):
            stav['hotovo'] = i
            if mapa is None:
                najdene = {}
                for j, bunka in enumerate(radek):
                    klic = _KARTY_SLOUPCE.get(_norm_hlavicka(bunka))
                    if klic and klic not in najdene:
                        najdene[klic] = j
                mapa = najdene if len(najdene) == len(_KARTY_SLOUPCE) else None
                continue

            def bunka(klic):
                j = mapa[klic]
                return radek[j] if j < len(radek) else None

            kod = _kod_karty(bunka('kod'))
            if not kod:
                vysledek['preskoceno'] += 1   # součtové řádky, prázdné patičky
                continue
            davka.append((kod[:40], str(bunka('skupina') or '').strip()[:255]))
            if len(davka) >= _DAVKA:
                _uloz_karty(cur, davka)
                vysledek['radku'] += len(davka)
                od_commitu += len(davka)
                davka = []
                if od_commitu >= _COMMIT_PO:
                    conn.commit()
                    od_commitu = 0

        if mapa is None:
            vysledek['chyba'] = ('Nenalezena hlavička. Očekávám sloupce Kód9 '
                                 'a Skupina2-popis (5. řádek souboru).')
            return vysledek
        if davka:
            _uloz_karty(cur, davka)
            vysledek['radku'] += len(davka)
        conn.commit()
        if not vysledek['radku']:
            vysledek['chyba'] = 'Soubor neobsahuje žádné karty zboží.'
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        vysledek['chyba'] = str(e)
        print(f'[lupa] import karet {nazev_souboru}: {e}')
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
    return vysledek


def importuj_soubor(raw_bytes, nazev_souboru, stav=None):
    """Jeden soubor = jedno ASM. Vrací dict s výsledkem.

    Replace je částečný: maže se jen (asm, rok, měsíc), které soubor obsahuje.
    Dohrání nového měsíce tedy nesmaže historii, reimport téhož měsíce přepíše.

    `stav` je volitelný dict pro průběh — běžíme ve vlákně, UI ho čte timerem.
    Zápis do dictu je pod GIL atomický, jiná synchronizace netřeba.
    """
    stav = stav if stav is not None else {}
    stav.update(faze='otevírám soubor', hotovo=0, celkem=None)
    klic = klic_ze_souboru(nazev_souboru)
    vysledek = {'soubor': nazev_souboru, 'asm': klic, 'radku': 0, 'preskoceno': 0,
                'cizi': 0, 'obdobi': [], 'chyba': None}
    if not klic:
        vysledek['chyba'] = ('Z názvu souboru nejde určit ASM. Očekávám tvar '
                             '„data prodeje zak3 – Příjmení.xlsx".')
        return vysledek
    inicializace_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        vysledek['chyba'] = 'Není spojení s databází.'
        return vysledek

    zakaznici, produkty, dealeri = {}, {}, {}
    smazane_partice = set()
    davka = []
    od_commitu = 0
    cur = None
    try:
        cur = conn.cursor()
        mapa = None
        stav['faze'] = 'načítám řádky'
        for i, radek in enumerate(_radky_xlsx(raw_bytes, stav), 1):
            stav['hotovo'] = i
            if mapa is None:
                mapa = _najdi_hlavicku(radek)
                continue

            def bunka(klic):
                i = mapa.get(klic)
                return radek[i] if i is not None and i < len(radek) else None

            asm_raw = bunka('asm')
            if not asm_raw:
                continue
            rok, mesic = _obdobi(bunka('mesic'))
            ico = str(bunka('ico') or '').strip()
            if rok is None or not ico:
                vysledek['preskoceno'] += 1   # součtové řádky, prázdné patičky
                continue

            if not _sedi_asm(zaklad_asm(asm_raw), klic):
                vysledek['cizi'] += 1   # cizí ASM v souboru — nemícháme
                continue

            asm = klic
            kod = str(bunka('kod') or '').strip()
            dealer = str(bunka('dealer') or '').strip()

            partice = (asm, rok, mesic)
            if partice not in smazane_partice:
                cur.execute('DELETE FROM lupa_obrat WHERE asm=%s AND rok=%s AND mesic=%s',
                            partice)
                conn.commit()
                smazane_partice.add(partice)

            zakaznici.setdefault(ico, (
                str(bunka('jmeno') or '')[:255], str(bunka('k_jmeno') or '')[:255],
                str(bunka('k_ulice') or '')[:255], str(bunka('k_mesto') or '')[:255], asm,
                _ano_ne(bunka('site'))))
            if kod:
                produkty.setdefault(kod, (str(bunka('nazev') or '')[:255],
                                          str(bunka('dodavatel') or '')[:255]))
            if dealer:
                dealeri.setdefault(dealer, (str(bunka('dealer_jmeno') or '')[:255], asm))

            davka.append((asm, dealer[:40], ico[:40], kod[:40], rok, mesic,
                          _cislo(bunka('obrat_mj')), _cislo(bunka('obrat_kc'))))
            if len(davka) >= _DAVKA:
                _uloz_davku(cur, davka)
                vysledek['radku'] += len(davka)
                od_commitu += len(davka)
                davka = []
                if od_commitu >= _COMMIT_PO:
                    conn.commit()
                    od_commitu = 0

        if mapa is None:
            vysledek['chyba'] = ('Nenalezena hlavička. Očekávám sloupce ASM, Měsíc, '
                                 'IČO, Produkt - Kód, Obrat v MJ, Obrat v Kč bez DPH.')
            return vysledek
        if davka:
            _uloz_davku(cur, davka)
            vysledek['radku'] += len(davka)
        conn.commit()

        if not vysledek['radku']:
            vysledek['chyba'] = ('Soubor neobsahuje žádná data pro ASM '
                                 f'{klic} (přeskočeno {vysledek["cizi"]} cizích řádků).')
            return vysledek

        stav['faze'] = 'ukládám číselníky'
        _uloz_dimenze(conn, cur, zakaznici, produkty, dealeri,
                      ma_site='site' in mapa)

        vysledek['obdobi'] = sorted({f'{r}M{m:02d}' for _, r, m in smazane_partice})
        stav['faze'] = 'přepočítávám souhrn'
        _prepocti_souhrn(cur, conn, klic)
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        vysledek['chyba'] = str(e)
        print(f'[lupa] import {nazev_souboru}: {e}')
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
    vymaz_cache()
    return vysledek


def _uloz_davku(cur, davka):
    cur.executemany(
        'INSERT INTO lupa_obrat (asm, dealer, ico, kod, rok, mesic, obrat_mj, obrat_kc) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', davka)


def _uloz_dimenze(conn, cur, zakaznici, produkty, dealeri, ma_site=True):
    """Sdílené číselníky — katalog produktů má každé ASM stejný.

    Souběžné ON DUPLICATE KEY UPDATE nad týmiž klíči = deadlock, proto jeden
    zapisovatel naráz. Commit patří dovnitř, jinak držíme zámky řádků i po
    uvolnění mutexu. Sekce je krátká — desítky tisíc klíčů, ne miliony faktů.
    """
    with _DIMENZE_ZAMEK:
        if zakaznici:
            # Soubor bez sloupce Sítě hodnotu nezná — nesmí přepsat dřív
            # naimportovanou.
            cur.executemany(
                'INSERT INTO lupa_zakaznik '
                '(ico, jmeno, k_jmeno, k_ulice, k_mesto, asm, site) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE '
                'jmeno=VALUES(jmeno), k_jmeno=VALUES(k_jmeno), k_ulice=VALUES(k_ulice), '
                'k_mesto=VALUES(k_mesto), asm=VALUES(asm), '
                + ('site=VALUES(site)' if ma_site else 'site=site'),
                [(k[:40],) + v for k, v in sorted(zakaznici.items())])
        if produkty:
            cur.executemany(
                'INSERT INTO lupa_produkt (kod, nazev, dodavatel) VALUES (%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE nazev=VALUES(nazev), dodavatel=VALUES(dodavatel)',
                [(k[:40],) + v for k, v in sorted(produkty.items())])
        if dealeri:
            cur.executemany(
                'INSERT INTO lupa_dealer (dealer, jmeno, asm) VALUES (%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE jmeno=VALUES(jmeno), asm=VALUES(asm)',
                [(k[:40],) + v for k, v in sorted(dealeri.items())])
        conn.commit()


def _prepocti_souhrn(cur, conn, asm):
    cur.execute(
        'SELECT COUNT(*), COUNT(DISTINCT ico), MIN(rok*100+mesic), MAX(rok*100+mesic), '
        'COALESCE(SUM(obrat_kc),0) FROM lupa_obrat WHERE asm=%s', (asm,))
    radku, zak, obd_od, obd_do, obrat = cur.fetchone()

    def _fmt(v):
        return f'{v // 100}M{v % 100:02d}' if v else ''

    cur.execute(
        'INSERT INTO lupa_asm (asm, user_id, radku, zakazniku, obdobi_od, obdobi_do, '
        'obrat_kc, aktualizovano) VALUES (%s,%s,%s,%s,%s,%s,%s,NOW()) '
        'ON DUPLICATE KEY UPDATE radku=VALUES(radku), zakazniku=VALUES(zakazniku), '
        'obdobi_od=VALUES(obdobi_od), obdobi_do=VALUES(obdobi_do), '
        'obrat_kc=VALUES(obrat_kc), aktualizovano=NOW(), '
        # user_id přepíšeme jen když ještě není — ruční volbu admina nepřebíjíme.
        'user_id=COALESCE(user_id, VALUES(user_id))',
        (asm, _uzivatel_dle_prijmeni(cur, asm), radku, zak,
         _fmt(obd_od), _fmt(obd_do), float(obrat or 0)))


def _uzivatel_dle_prijmeni(cur, asm):
    """ASM 'KORABECNY' → iduser Miroslava Korábečného. Nejednoznačné → None."""
    cur.execute('SELECT iduser, surname FROM user')
    nalezeni = [r[0] for r in cur.fetchall() if slug(r[1]) == asm]
    return nalezeni[0] if len(nalezeni) == 1 else None


# ============================================================================
# Práva a viditelnost
# ============================================================================

_CACHE = {'cas': 0.0, 'asm_user': {}, 'user_odd': {}, 'odd_users': {}}


def vymaz_cache():
    _CACHE['cas'] = 0.0


# Úprava / deaktivace / smazání účtu ve správě uživatelů → Lupa hned přepočítá mapy.
intranet_data.PO_ZMENE_UZIVATELU.append(vymaz_cache)

_MAZANI = set()   # ASM, jejichž smazání právě běží ve vlákně (ať se nespustí dvakrát)


def _mapy():
    """{asm: user_id}, {user_id: {oddělení}}, {oddělení: {user_id}} — 10min cache.

    Stav účtu spárovaného obchodníka:
      deaktivovaný → ASM v asm_user není (nikdo ho nevidí ani neotevře), data
                     zůstávají; šedou dlaždici vidí jen SKRYTY_ADMIN_ID
      smazaný      → ASM se smaže celé i s daty (_smaz_asm)"""
    if time.time() - _CACHE['cas'] < _CACHE_TTL and _CACHE['asm_user']:
        return _CACHE['asm_user'], _CACHE['user_odd'], _CACHE['odd_users']
    inicializace_db()
    asm_user, user_odd, odd_users, neaktivni, smazana = {}, {}, {}, [], []
    conn = intranet_data.get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute('SELECT a.asm, a.user_id, u.iduser IS NULL, u.is_active = 0 '
                        'FROM lupa_asm a LEFT JOIN user u ON u.iduser = a.user_id')
            for asm, uid, chybi, vypnuty in cur.fetchall():
                if uid and chybi:
                    smazana.append(asm)
                elif uid and vypnuty:
                    neaktivni.append(asm)
                else:
                    asm_user[asm] = uid
            sparovana = len(smazana) + len(neaktivni) + sum(1 for u in asm_user.values() if u)
            if len(smazana) > 1 and len(smazana) == sparovana:
                # Všichni obchodníci naráz smazaní = rozbitá tabulka user, ne odchody.
                print(f'[lupa] mazání přeskočeno — smazaní by byli všichni: {sorted(smazana)}')
            elif smazana:
                _smaz_asm_na_pozadi(smazana)
            cur.execute('SELECT dtu.user_iduser, LOWER(d.name) FROM department_To_user dtu '
                        'JOIN department d ON d.iddepartment = dtu.department_iddepartment')
            for uid, odd in cur.fetchall():
                user_odd.setdefault(uid, set()).add(odd)
                odd_users.setdefault(odd, set()).add(uid)
            cur.close()
        except Exception as e:
            print(f'[lupa] _mapy: {e}')
        finally:
            conn.close()
    _CACHE.update({'cas': time.time(), 'asm_user': asm_user, 'user_odd': user_odd,
                   'odd_users': odd_users, 'neaktivni': neaktivni})
    return asm_user, user_odd, odd_users


def neaktivni_asm(user_id):
    """Šedé dlaždice — ASM deaktivovaných účtů. Vidí je jen SKRYTY_ADMIN_ID."""
    if user_id != intranet_data.SKRYTY_ADMIN_ID:
        return []
    _mapy()
    return sorted(_CACHE.get('neaktivni', []))


def _smaz_asm_na_pozadi(asmy):
    """Mazání obratů jsou desítky tisíc řádků — mimo event loop."""
    nova = [a for a in asmy if a not in _MAZANI]
    if nova:
        _MAZANI.update(nova)
        threading.Thread(target=_smaz_asm, args=(nova,), daemon=True).start()


def _smaz_asm(asmy, kdo='Systém', duvod='uživatel byl smazán'):
    """Úplné smazání ASM: dlaždice, obraty, poznámky, komentáře. Vrací smazaná ASM.

    Volá se při smazání spárovaného uživatele (_mapy) i ručně z dlaždice (admin).
    Zákazníci a dealeři nesou sloupec asm, ale sdílí je víc ASM — maže se jen to,
    na co už žádný obrat neukazuje. Produkty a karty zboží jsou společné, zůstávají."""
    smazana = []
    conn = intranet_data.get_db_connection()
    try:
        if not conn:
            return smazana
        cur = conn.cursor()
        for asm in asmy:
            try:
                cur.execute('DELETE FROM lupa_komentar WHERE poznamka_id IN '
                            '(SELECT id FROM lupa_poznamka WHERE asm=%s)', (asm,))
                cur.execute('DELETE FROM lupa_poznamka WHERE asm=%s', (asm,))
                cur.execute('DELETE FROM lupa_obrat WHERE asm=%s', (asm,))
                cur.execute('DELETE FROM lupa_asm WHERE asm=%s', (asm,))
                conn.commit()
                with _DIMENZE_ZAMEK:   # import zapisuje dimenze pod stejným zámkem
                    cur.execute('DELETE z FROM lupa_zakaznik z WHERE z.asm=%s AND NOT EXISTS '
                                '(SELECT 1 FROM lupa_obrat o WHERE o.ico = z.ico)', (asm,))
                    cur.execute('DELETE d FROM lupa_dealer d WHERE d.asm=%s AND NOT EXISTS '
                                '(SELECT 1 FROM lupa_obrat o WHERE o.dealer = d.dealer)', (asm,))
                    conn.commit()
                smazana.append(asm)
                intranet_logger.log_activity(kdo, 'Lupou na obchod',
                                             f'Smazáno ASM {asm} i s daty — {duvod}')
            except Exception as e:
                conn.rollback()
                print(f'[lupa] smazání ASM {asm}: {e}')
        cur.close()
    finally:
        if conn:
            conn.close()
        _MAZANI.difference_update(asmy)
        vymaz_cache()
    return smazana


def je_admin(prava):
    return 'vse' in prava or 'lupa_admin' in prava


def je_extra(prava):
    return je_admin(prava) or 'lupa_extra' in prava


def je_vkladatel(prava):
    return je_admin(prava) or 'lupa_vkladatel' in prava


def pristupna_asm(user_id, prava):
    """Seznam ASM, které uživatel vidí. Admin/extra = všechna."""
    asm_user, _, odd_users = _mapy()
    if je_extra(prava):
        return sorted(asm_user)

    videna = {a for a, uid in asm_user.items() if uid and uid == user_id}

    # Vedoucí oddělení vidí všechna ASM svých lidí.
    vedeni = {p[len('hlavni_vedouci_'):] for p in prava if p.startswith('hlavni_vedouci_')}
    if vedeni:
        clenove = set()
        for odd in vedeni:
            clenove |= odd_users.get(odd, set())
        videna |= {a for a, uid in asm_user.items() if uid in clenove}
    return sorted(videna)


def ma_pristup(user_id, prava):
    """Brána modulu. Vkladatel se dovnitř dostane i bez jediného vlastního ASM,
    nákupčí (Lupou na nákup) i bez jakéhokoli ASM."""
    if je_extra(prava) or je_vkladatel(prava) or ma_pristup_nakup(prava):
        return True
    return bool(pristupna_asm(user_id, prava))


def je_moje_asm(user_id, asm, prava):
    """Kdo píše poznámky: spárovaný obchoďák daného ASM (a admin).

    Extra čtenář ani vedoucí sem nepíšou — ti komentují. Poznámky jsou hlas
    toho, kdo území drží."""
    if je_admin(prava):
        return True
    asm_user, _, _ = _mapy()
    return bool(user_id) and asm_user.get(asm) == user_id


def nesparovana_asm():
    asm_user, _, _ = _mapy()
    return sorted(a for a, uid in asm_user.items() if not uid)


def _adminska_id():
    """Uživatelé s právem lupa_admin / vse — cíl notifikací o nespárovaných ASM."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        # Právo může viset na uživateli, pozici i oddělení — stejné tři kanály
        # jako ziskej_prava_uzivatele(). „vse" je v ostrých datech jen na oddělení.
        cur.execute("""
            SELECT utp.user_iduser FROM user_To_privileges utp
                JOIN privileges p ON p.idprivileges = utp.privileges_idprivileges
                WHERE p.name IN ('vse', 'lupa_admin')
            UNION
            SELECT utj.user_iduser FROM user_To_jobPosition utj
                JOIN jobPosition_To_privileges jtp
                    ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
                JOIN privileges p ON p.idprivileges = jtp.privileges_idprivileges
                WHERE p.name IN ('vse', 'lupa_admin')
            UNION
            SELECT dtu.user_iduser FROM department_To_user dtu
                JOIN department_To_privileges dtp
                    ON dtu.department_iddepartment = dtp.department_iddepartment
                JOIN privileges p ON p.idprivileges = dtp.privileges_idprivileges
                WHERE p.name IN ('vse', 'lupa_admin')
        """)
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ============================================================================
# UI
# ============================================================================

_KARTA = ('w-72 h-56 items-center justify-center shadow-xl hover:scale-105 '
          'transition-transform duration-300 cursor-pointer bg-white rounded-2xl '
          'border border-indigo-200')
_KARTA_SEDA = ('w-72 h-56 items-center justify-center cursor-default bg-gray-100 '
               'rounded-2xl border border-gray-300 opacity-60')
# Rozcestník (Lupou na obchod / Lupou na nákup / Import dat) — jako _KARTA, jen barvu
# okraje doplní volající (dvě border-* třídy by se přetahovaly).
_KARTA_SEKCE = ('w-72 h-56 items-center justify-center shadow-xl hover:scale-105 '
                'transition-transform duration-300 cursor-pointer bg-white rounded-2xl border ')

OBCHOD_POHLED = '#obchod'   # podpohled s dlaždicemi ASM — slug ASM „#“ nemá


def _kc(hodnota):
    return f'{hodnota:,.0f}'.replace(',', ' ') + ' Kč'


_UZAVRENE_PREFIX = 'lupa_uzavrene_mesice_'
# Nákup se uzavírá zvlášť — GIST odběrů chodí jindy než prodeje ASM. Prefix se
# s obchodním nesmí překrývat, _nekompletni_klice třídí klíče přes startswith.
_UZAVRENE_NAKUP_PREFIX = 'lupa_nakup_uzavrene_mesice_'


def _uzavrene_key(rok, prefix=_UZAVRENE_PREFIX):
    return f'{prefix}{rok}'


def _stav_bunka_mesic(rok, mesic, uzavren, editable, user_name, refresh,
                      prefix=_UZAVRENE_PREFIX):
    """Jedna měsíční dlaždice (zelená = uzavřené období k dispozici).
    Stejné značení jako ve Výsledcích poboček. Klik přepne stav (vkladatel)."""
    bg = '#16a34a' if uzavren else '#dc2626'
    cls = 'flex items-center justify-center rounded text-white select-none'
    if editable:
        cls += ' cursor-pointer hover:opacity-80 transition-opacity'
    box = ui.element('div').classes(cls).style(
        f'width:46px;height:36px;background:{bg};font-size:13px;font-weight:700')
    with box:
        ui.label(('✓ ' if uzavren else '') + f'{mesic:02d}')
    popis = ('Uzavřené období — data k dispozici' if uzavren else
             ('Klikněte pro označení uzavřeného období — jinak zůstane vyjmuté z filtru'
              if editable else 'Nekompletní období — ve filtru nejde zvolit'))
    box.props(f'title="{popis}"')
    if editable:
        def _toggle(_=None, m=mesic):
            akt = set(app.storage.general.get(_uzavrene_key(rok, prefix)) or [])
            akt.symmetric_difference_update({m})
            app.storage.general[_uzavrene_key(rok, prefix)] = sorted(akt)
            oblast = ' nákupu' if prefix == _UZAVRENE_NAKUP_PREFIX else ''
            intranet_logger.log_activity(
                user_name, 'Lupa',
                f'Uzavřené období{oblast} {rok}-{m:02d}: '
                f'{"označeno" if m in akt else "zrušeno"}')
            refresh()
        box.on('click', _toggle)


def _vykresli_uzavrene_mesice(user_name, editable, prefix=_UZAVRENE_PREFIX, co='obraty',
                              po_zmene=None):
    """Panel nahraných (uzavřených) měsíců aktuálního roku. Obchod a nákup mají
    každý svůj (`prefix` klíče v app.storage.general); `co` nahrává ten, kdo značí.
    `po_zmene` = co dalšího překreslit, když jsou filtry měsíců na stejné stránce."""
    rok = datetime.datetime.now().year

    @ui.refreshable
    def panel():
        uzavrene = set(app.storage.general.get(_uzavrene_key(rok, prefix)) or [])
        with ui.card().classes('w-full p-4 mb-6 rounded-2xl border border-gray-100'):
            with ui.row().classes('items-center gap-2 mb-1'):
                ui.icon('event_available', color='primary')
                ui.label(f'Nahraná data za rok {rok}') \
                    .classes('text-lg font-bold text-gray-800')
            ui.label('🟢 zelené = uzavřené období k dispozici · 🔴 nekompletní — '
                     've filtru měsíců je šedé a nejde zvolit') \
                .classes('text-xs text-gray-500 mb-2')
            if not editable:
                ui.label(f'Označuje ten, kdo nahrává {co}.') \
                    .classes('text-xs text-gray-400 mb-2')
            with ui.row().classes('items-center gap-1.5 flex-wrap'):
                for m in range(1, 13):
                    _stav_bunka_mesic(rok, m, m in uzavrene, editable,
                                      user_name, obnov, prefix)

    def obnov():
        panel.refresh()
        if po_zmene:
            po_zmene()

    panel()


def _kopirovaci_skript():
    """Delegovaný posluchač pro kopírování identifikátorů z gridů (IČO, kód
    produktu) do schránky. Jeden na stránku, v capture fázi — klik na hodnotu se
    tak nedostane k posluchači řádku v AG Gridu a neotevře detail zákazníka.
    Zápis přes textarea+execCommand, protože navigator.clipboard je na http
    (intranet bez TLS) nedostupný; clipboard API je jen fallback.

    Injektáž přes run_javascript, ne add_body_html: Lupa se kreslí až při
    přepnutí tabu, tedy po odeslání stránky — NiceGUI takové HTML vkládá přes
    insertAdjacentHTML, který <script> nikdy nespustí."""
    ui.run_javascript('''
    (function () {
      if (window.__lupaKopie) return;
      window.__lupaKopie = true;
      const st = document.createElement('style');
      st.textContent = '.lupa-kopie{cursor:copy;border-bottom:1px dashed #94a3b8}'
        + '.lupa-kopie:hover{background:#e0e7ff}';
      document.head.appendChild(st);
      const hlaska = (txt) => {
        const d = document.createElement('div');
        d.textContent = 'Zkopírováno: ' + txt;
        d.style.cssText = 'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);'
          + 'background:#1e293b;color:#fff;padding:6px 14px;border-radius:6px;'
          + 'font-size:13px;z-index:9999;pointer-events:none;';
        document.body.appendChild(d);
        setTimeout(() => d.remove(), 1200);
      };
      document.addEventListener('click', (ev) => {
        const el = ev.target.closest && ev.target.closest('.lupa-kopie');
        if (!el) return;
        const txt = (el.textContent || '').trim();
        if (!txt) return;
        ev.stopPropagation();
        let ok = false;
        const onCopy = (e) => {
          e.clipboardData.setData('text/plain', txt);
          e.preventDefault(); ok = true;
        };
        try {
          const ta = document.createElement('textarea');
          ta.value = txt;
          ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.focus(); ta.select();
          document.addEventListener('copy', onCopy, true);
          document.execCommand('copy');
          document.removeEventListener('copy', onCopy, true);
          ta.remove();
        } catch (_) {
          document.removeEventListener('copy', onCopy, true);
        }
        if (!ok && navigator.clipboard) {
          try { navigator.clipboard.writeText(txt); ok = true; } catch (e) {}
        }
        hlaska(ok ? txt : txt + ' — kopie selhala, označ a Ctrl+C');
      }, true);
    })();
    ''')


def vykresli_lupa(user_id, user_name, vsechna_prava):
    inicializace_db()
    _kopirovaci_skript()
    stav_klic = f'lupa_pohled_{user_id}'

    @ui.refreshable
    def obsah():
        pohled = app.storage.user.get(stav_klic)
        moje = pristupna_asm(user_id, vsechna_prava)
        vkladatel = je_vkladatel(vsechna_prava)
        # Vkladatel chodí do obou sekcí označovat uzavřené měsíce, i bez práv k datům.
        nakup = ma_pristup_nakup(vsechna_prava) or vkladatel
        obchod = bool(moje) or vkladatel or bool(neaktivni_asm(user_id))

        if pohled == 'import' and not vkladatel:
            pohled = None
        if pohled == NAKUP_POHLED and not nakup:
            pohled = None
        if pohled == OBCHOD_POHLED and not obchod:
            pohled = None
        if pohled and pohled not in moje \
                and pohled not in ('import', NAKUP_POHLED, OBCHOD_POHLED):
            pohled = None

        if pohled == 'import':
            _zpet_hlavicka('Import dat', obsah, stav_klic)
            _vykresli_import(user_id, user_name, vsechna_prava)
            return
        if pohled == NAKUP_POHLED:
            hlavicka = _zpet_hlavicka('🛒 Lupou na nákup', obsah, stav_klic)
            if nakup_kody(vsechna_prava) is None:     # e-mail nákupčím řídí Správce
                with hlavicka.classes('w-full'):
                    ui.space()
                    _nakup_report_obalka(user_id, user_name)
            _vykresli_nakup(user_id, user_name, vsechna_prava)
            return
        if pohled == OBCHOD_POHLED:
            _zpet_hlavicka('🔍 Lupou na obchod', obsah, stav_klic)
            _vykresli_obchod(user_id, user_name, vsechna_prava, moje, stav_klic, obsah)
            return
        if pohled:
            _zpet_hlavicka(f'ASM {pohled.capitalize()}', obsah, stav_klic,
                           zpet_na=OBCHOD_POHLED)
            _vykresli_detail(pohled, user_id, user_name, vsechna_prava)
            return

        ui.label('🔍 Lupopřípad').classes('text-3xl font-bold text-gray-800 mb-1')
        ui.label('Vyberte oblast.').classes('text-gray-500 mb-4')
        if not obchod and not nakup:
            with ui.column().classes('items-center py-20 gap-3 w-full'):
                ui.icon('insights', size='5rem', color='grey-3')
                ui.label('Žádná data').classes('text-xl font-semibold text-gray-400')
                ui.label('Zatím vám není přiřazeno žádné ASM.').classes('text-gray-400')
            return

        with ui.row().classes('w-full gap-6 flex-wrap items-stretch'):
            if obchod:
                _dlazdice_obchod(stav_klic, obsah)
            if nakup:
                _dlazdice_nakup(stav_klic, obsah)
            if vkladatel:
                _dlazdice_import(stav_klic, obsah)

    obsah()


def _vykresli_obchod(user_id, user_name, vsechna_prava, moje, stav_klic, refreshable):
    """Sekce „Lupou na obchod“: panel uzavřených měsíců + dlaždice ASM."""
    ui.label('Obratová analytika ASM, obchodních zástupců a zákazníků.') \
        .classes('text-gray-500 mb-4')
    _vykresli_uzavrene_mesice(user_name, je_vkladatel(vsechna_prava))

    souhrny = _nacti_souhrny(moje)
    sede = _nacti_souhrny(neaktivni_asm(user_id))
    if not souhrny and not sede:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('insights', size='5rem', color='grey-3')
            ui.label('Zatím žádná data ASM').classes('text-xl font-semibold text-gray-400')
            ui.label('Prodeje GIST nahrává vkladatel v dlaždici Import dat.') \
                .classes('text-gray-400')
        return

    muze_mazat = je_admin(vsechna_prava)
    with ui.row().classes('gap-6 flex-wrap items-stretch'):
        for s in souhrny:
            karta = _dlazdice_asm(s, stav_klic, refreshable)
            if muze_mazat:
                _menu_smazat(karta, s['asm'], refreshable, user_name)
        for s in sede:
            karta = _dlazdice_neaktivni(s)
            if muze_mazat:
                _menu_smazat(karta, s['asm'], refreshable, user_name)


def _zpet_hlavicka(titulek, refreshable, stav_klic, zpet_na=None):
    """Hlavička podpohledu se šipkou zpět; `zpet_na` = cílový pohled (None = rozcestník).
    Vrací řádek, aby do něj sekce mohla přidat ovládání vpravo."""
    def zpet():
        app.storage.user[stav_klic] = zpet_na
        refreshable.refresh()

    with ui.row().classes('items-center gap-3 mb-4') as radek:
        ui.button(icon='arrow_back', on_click=zpet).props('flat round') \
            .tooltip('Zpět na výběr ASM' if zpet_na == OBCHOD_POHLED
                     else 'Zpět na výběr oblasti')
        ui.label(titulek).classes('text-2xl font-bold text-gray-800')
    return radek


def _nacti_souhrny(asmy):
    if not asmy:
        return []
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        ph = ','.join(['%s'] * len(asmy))
        cur.execute(f'SELECT * FROM lupa_asm WHERE asm IN ({ph}) ORDER BY asm', tuple(asmy))
        return cur.fetchall()
    except Exception as e:
        print(f'[lupa] _nacti_souhrny: {e}')
        return []
    finally:
        conn.close()


async def _prepni_pohled(stav_klic, hodnota, refreshable, popis):
    """Překryv s kolečkem po dobu, než se pohled vykreslí.
    Sestavení detailu = desítky dotazů na volby filtrů, bez překryvu to vypadá
    jako zamrzlá dlaždice."""
    app.storage.user[stav_klic] = hodnota
    dlg, _kruh, _popisek = prekryv_kolecko(popis, procenta=False)
    dlg.open()
    try:
        await asyncio.sleep(0.15)   # ať se překryv stihne vykreslit
        refreshable.refresh()
    finally:
        try:
            dlg.close()
        except RuntimeError:
            pass    # refresh dlaždici i s dialogem smazal - překryv je už pryč


def _dlazdice_sekce(emoji, nadpis, okraj, on_click):
    """Dlaždice rozcestníku — jen název. Které měsíce jsou nahrané, ukazuje panel
    uzavřených měsíců uvnitř sekce."""
    with ui.card().classes(f'{_KARTA_SEKCE} {okraj}').on('click', on_click):
        ui.label(emoji).classes('text-5xl mb-2')
        ui.label(nadpis).classes('text-xl font-bold text-gray-800 text-center w-full')


def _dlazdice_obchod(stav_klic, refreshable):
    async def otevri():
        await _prepni_pohled(stav_klic, OBCHOD_POHLED, refreshable, 'Načítám Lupou na obchod…')

    _dlazdice_sekce('🔍', 'Lupou na obchod', 'border-indigo-200', otevri)


def _dlazdice_asm(s, stav_klic, refreshable):
    async def otevri():
        await _prepni_pohled(stav_klic, s['asm'], refreshable,
                             f"Načítám ASM {s['asm'].capitalize()}…")

    with ui.card().classes(_KARTA).on('click', otevri) as karta:
        ui.label('🔍').classes('text-5xl mb-2')
        ui.label(s['asm'].capitalize()) \
            .classes('text-xl font-bold text-gray-800 text-center w-full')
    return karta


def _dlazdice_neaktivni(s):
    """ASM deaktivovaného účtu — data drží pro případnou reaktivaci, otevřít nejde."""
    with ui.card().classes(_KARTA_SEDA) as karta:
        ui.label('🔍').classes('text-5xl mb-2 grayscale')
        ui.label(s['asm'].capitalize()) \
            .classes('text-xl font-bold text-gray-500 text-center w-full')
    return karta


def _menu_smazat(karta, asm, refreshable, user_name):
    """Pravý klik na dlaždici → Smazat: ASM zmizí i se všemi svými daty (jen admin).

    Dialogy visí na kartě, ne na menu — obsah zavřeného QMenu se nevykresluje."""
    nazev = asm.capitalize()

    async def smaz(dlg):
        dlg.close()     # hide → dlg.delete; odtud dál jen v kontextu karty
        with karta:
            if asm in _MAZANI:
                ui.notify(f'ASM {nazev} se už maže.', type='warning')
                return
            if _blokuje_import(asm):
                return
            prekryv, _kruh, _popisek = prekryv_kolecko(f'Mažu ASM {nazev}…', procenta=False)
            prekryv.open()
            try:
                async with _zamek_asm(asm):     # import téhož ASM počká, až mazání doběhne
                    _MAZANI.add(asm)            # _smaz_asm ho ve finally zase uvolní
                    smazana = await asyncio.to_thread(
                        _smaz_asm, [asm], user_name, 'smazáno ručně z dlaždice')
            finally:
                try:
                    prekryv.close()
                except RuntimeError:
                    pass    # karta mezitím zmizela jiným refreshem
            if smazana:
                ui.notify(f'ASM {nazev} smazáno i s daty.', type='positive')
            else:
                ui.notify(f'ASM {nazev} se nepodařilo smazat.', type='negative')
            refreshable.refresh()

    def potvrd():
        with karta, ui.dialog() as dlg, ui.card().classes('max-w-md'):
            ui.label(f'Smazat ASM {nazev}?').classes('text-lg font-bold text-gray-800')
            ui.label('Dlaždice zmizí i se všemi daty: obraty, poznámky, komentáře '
                     'a zákazníci/dealeři, na které už nevede žádný jiný obrat. '
                     'Akci nelze vrátit — data vrátí jen nový import.') \
                .classes('text-sm text-gray-600')
            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Smazat', icon='delete', on_click=lambda: smaz(dlg)) \
                    .props('color=negative')
        dlg.on('hide', dlg.delete)      # ať se zrušené dialogy na kartě nehromadí
        dlg.open()

    with karta, ui.context_menu():
        ui.menu_item('Smazat', potvrd).classes('text-red-600')


def _dlazdice_import(stav_klic, refreshable):
    async def otevri():
        await _prepni_pohled(stav_klic, 'import', refreshable, 'Načítám import…')

    with ui.card().classes(f'{_KARTA_SEKCE} border-amber-200').on('click', otevri):
        ui.label('⬆️').classes('text-5xl mb-2')
        ui.label('Import dat').classes('text-xl font-bold text-gray-800')
        ui.label('GIST prodeje, karty zboží a odběry') \
            .classes('text-sm text-gray-500 text-center w-full mt-1')


# ============================================================================
# Záložka „Lupni odběratele" — dotazy
# ============================================================================

_STROP_NAHLED = 5000     # řádků do aggridu (souhrn po zákazníkovi bývá do 2 tis.)
_STROP_PDF = 3000        # řádků do PDF — Chromium nad to renderuje minuty


def _dotaz(sql, params=(), slovnik=False):
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True) if slovnik else conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception as e:
        print(f'[lupa] dotaz: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


def _obd_int(txt):
    """'2026M08' → 202608. Neplatný tvar → None (= filtr se neuplatní)."""
    m = _MESIC_RE.match(str(txt or ''))
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def _volby_obdobi(asm):
    """Měsíce, které ASM v datech opravdu má."""
    return [f'{r}M{m:02d}' for r, m in _dotaz(
        'SELECT DISTINCT rok, mesic FROM lupa_obrat WHERE asm=%s ORDER BY rok, mesic',
        (asm,))]


def _nekompletni_klice(prefix=_UZAVRENE_PREFIX):
    """Nekompletní měsíce jako rok*100+měsíc — vkladatel je neoznačil za uzavřené.

    Čteme jen roky, ke kterým panel něco eviduje; rok bez záznamu (historie)
    neřešíme, jinak by se po zapnutí funkce vyřadila celá minulost."""
    ven = []
    for klic, uzavrene in list(app.storage.general.items()):
        if not klic.startswith(prefix):
            continue
        rok = klic[len(prefix):]
        if not rok.isdigit():
            continue
        ven += [int(rok) * 100 + m for m in range(1, 13)
                if m not in (uzavrene or [])]
    return ven


def _nekompletni_obdobi(obdobi, prefix=_UZAVRENE_PREFIX):
    """Měsíce ze seznamu voleb, které jsou nekompletní (pro šedé položky selectu)."""
    ne = set(_nekompletni_klice(prefix))
    return {klic for klic in obdobi if _obd_int(klic) in ne}


class _VyberMesice(ui.select):
    """Výběr měsíce, kde neuzavřené měsíce zůstanou v nabídce, ale šedé.

    Quasar bere příznak `disable` přímo z položky options. Nestačí ho dopsat po
    vytvoření — ChoiceElement.update() staví options pokaždé znovu, takže se
    značka musí vracet právě tam."""

    def __init__(self, label, obdobi, hodnota, nevolitelne):
        self._nevolitelne = set(nevolitelne)
        super().__init__(obdobi, label=label, clearable=True,
                         value=hodnota if hodnota in obdobi
                         and hodnota not in self._nevolitelne else None)
        # Vlastní vykreslení položky jen kvůli tooltipu u šedého měsíce.
        self.add_slot('option', '''
            <q-item v-bind="props.itemProps">
                <q-item-section>
                    <q-item-label>{{ props.opt.label }}</q-item-label>
                </q-item-section>
                <q-tooltip v-if="props.opt.disable">Měsíc je nekompletní, nelze zvolit</q-tooltip>
            </q-item>
        ''')

    def _update_options(self):
        super()._update_options()
        with self._props.suspend_updates():
            for volba, klic in zip(self._props['options'], self._values):
                if klic in self._nevolitelne:
                    volba['disable'] = True


def _sel_obdobi(label, obdobi, hodnota, prefix=_UZAVRENE_PREFIX):
    """Výběr měsíce. Neuzavřené měsíce jsou šedé a nejdou zvolit."""
    ven = _nekompletni_obdobi(obdobi, prefix)
    sel = _VyberMesice(label, obdobi, hodnota, ven) \
        .props('dense outlined options-dense').style('min-width: 140px')
    if ven:
        sel.tooltip('Šedé měsíce nejsou uzavřené — data zatím nekompletní.')
    return sel


def _volby_zakazniku(asm):
    """{ičo: 'Jméno · Město · IČO'} pro ui.select. Bereme z dimenze, ne z faktů.
    IČO je v labelu schválně — with_input hledá v labelu, takže jde vyhledat i psaním IČ."""
    return {
        r[0]: ' · '.join([x for x in (r[1], r[2]) if x] + [r[0]])
        for r in _dotaz('SELECT ico, jmeno, k_mesto FROM lupa_zakaznik '
                        'WHERE asm=%s ORDER BY jmeno', (asm,))
    }


def _najdi_ica(text, zak_volby, vybrane=()):
    """Nalepený seznam IČ → (klíče do filtru, nespárované tokeny).
    IČ z GISTu mívají příponu (`05783704J`), proto bereme i shodu na začátku —
    ale až od 4 znaků, aby „12“ nevybralo půl ASM."""
    vybrane = list(vybrane)
    neznama = []
    for t in [t.upper() for t in re.split(r'[^0-9A-Za-z]+', text or '') if t]:
        shody = [k for k in zak_volby if k.upper() == t]
        if not shody and len(t) >= 4:
            shody = [k for k in zak_volby if k.upper().startswith(t)]
        if not shody:
            neznama.append(t)
        vybrane += [k for k in shody if k not in vybrane]
    return vybrane, neznama


def _pole_ica(sel_zak, zak_volby):
    """Hromadné vložení IČ do filtru zákazníků — nalepení seznamu z Excelu."""
    pole = ui.input(label='Vložit IČ') \
        .props('dense outlined clearable').style('min-width: 200px') \
        .tooltip('Nalep IČ oddělená čárkou, mezerou nebo novým řádkem a stiskni Enter.')

    def _pridej():
        if not (pole.value or '').strip():
            return
        vybrane, neznama = _najdi_ica(pole.value, zak_volby, sel_zak.value or [])
        sel_zak.value = vybrane
        pole.value = ''
        if neznama:
            ui.notify('V tomto ASM nenalezeno: ' + ', '.join(neznama[:10]),
                      type='warning', multi_line=True)
        else:
            ui.notify(f'Ve filtru je {len(vybrane)} zákazníků.', type='positive')

    pole.on('keydown.enter', _pridej)
    return pole


def _volby_dodavatelu():
    """Dodavatelé napříč všemi ASM. Per-ASM seznam by znamenal JOIN přes stovky
    tisíc řádků faktů při každém otevření záložky — 3 s za trochu kratší nabídku."""
    return [r[0] for r in _dotaz(
        'SELECT DISTINCT dodavatel FROM lupa_produkt '
        "WHERE dodavatel <> '' ORDER BY dodavatel") if r[0]]


def _volby_oz(asm):
    """{'635': '635 · Jméno'} — kód zkrácený na 3 znaky, varianty 635A/635D
    sloučené pod jeden klíč. Jméno bereme první ze skupiny, stejně jako grafy."""
    return {
        r[0]: f'{r[0]} · {r[1]}' if r[1] else r[0]
        for r in _dotaz('SELECT LEFT(dealer, 3), MIN(jmeno) FROM lupa_dealer '
                        'WHERE asm=%s GROUP BY LEFT(dealer, 3) ORDER BY 1', (asm,))
    }


def _filtr_sql(asm, filtr, produkt_uz_pripojen=False):
    """WHERE pro `lupa_obrat o`. Vrací (sql, params, je_potreba_join_produktu).

    Nekompletní měsíce vyřazujeme tady, ne až v UI: prázdný filtr Od/Do jinak
    pustí rozjetý měsíc do souhrnů i grafů."""
    kde, par = ['o.asm = %s'], [asm]
    nekompletni = _nekompletni_klice()
    if nekompletni:
        kde.append('o.rok*100+o.mesic NOT IN (%s)'
                   % ','.join(['%s'] * len(nekompletni)))
        par += nekompletni
    od, do = _obd_int(filtr.get('od')), _obd_int(filtr.get('do'))
    if od:
        kde.append('o.rok*100+o.mesic >= %s')
        par.append(od)
    if do:
        kde.append('o.rok*100+o.mesic <= %s')
        par.append(do)
    ica = [i for i in (filtr.get('ico') or []) if i]
    if ica:
        kde.append('o.ico IN (%s)' % ','.join(['%s'] * len(ica)))
        par += ica
    oz = [d for d in (filtr.get('oz') or []) if d]
    if oz:
        # ponytail: LEFT() obchází idx_dealer, ale filtr na ASM scan zúží dost.
        # Až bude pomalé: OR o.dealer LIKE '635%' přes prefixy.
        kde.append('LEFT(o.dealer, 3) IN (%s)' % ','.join(['%s'] * len(oz)))
        par += oz
    dod = [d for d in (filtr.get('dodavatel') or []) if d]
    if dod:
        kde.append('p.dodavatel IN (%s)' % ','.join(['%s'] * len(dod)))
        par += dod
    site = filtr.get('site')
    if site in ('ano', 'ne'):
        # Poddotaz místo JOINu: lupa_zakaznik má ~6 tis. řádků s ico jako PK,
        # takže semi-join je levný a volající nemusí řešit další join.
        kde.append('o.ico IN (SELECT ico FROM lupa_zakaznik WHERE site = %s)')
        par.append(1 if site == 'ano' else 0)
    return ' AND '.join(kde), par, bool(dod) and not produkt_uz_pripojen


def _souhrn_odberatelu(asm, filtr):
    """Řádek = zákazník za zvolené období. Dimenze (jméno, adresa) joinujeme až
    NA VÝSLEDEK agregace — GROUP BY přes VARCHAR(255) jména a adresy dělal
    filesort nad 600 tis. řádků a trval 15 s místo 2,4 s.
    S filtrem 'rozpad' je řádek zákazník × produkt (řádků násobně víc)."""
    kde, par, join_p = _filtr_sql(asm, filtr)
    jp = 'JOIN lupa_produkt p ON p.kod = o.kod' if join_p else ''
    if filtr.get('rozpad'):
        return _dotaz(f"""
            SELECT a.ico, z.jmeno, z.k_jmeno, z.k_ulice, z.k_mesto,
                   IF(z.site, 'Ano', 'Ne') AS site,
                   a.kod, p.nazev, p.dodavatel, a.mj, a.kc
            FROM (SELECT o.ico, o.kod,
                         SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc
                  FROM lupa_obrat o {jp}
                  WHERE {kde}
                  GROUP BY o.ico, o.kod) a
            LEFT JOIN lupa_zakaznik z ON z.ico = a.ico
            LEFT JOIN lupa_produkt p ON p.kod = a.kod
            ORDER BY a.kc DESC
        """, par, slovnik=True)
    return _dotaz(f"""
        SELECT a.ico, z.jmeno, z.k_jmeno, z.k_ulice, z.k_mesto,
               IF(z.site, 'Ano', 'Ne') AS site,
               a.polozek, a.mj, a.kc
        FROM (SELECT o.ico, COUNT(DISTINCT o.kod) AS polozek,
                     SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc
              FROM lupa_obrat o {jp}
              WHERE {kde}
              GROUP BY o.ico) a
        LEFT JOIN lupa_zakaznik z ON z.ico = a.ico
        ORDER BY a.kc DESC
    """, par, slovnik=True)


def _produkty_zakaznika(asm, ico, filtr):
    """Rozklik řádku: co ten zákazník ve zvoleném období bral, po produktech."""
    f = dict(filtr, ico=[ico])
    kde, par, _ = _filtr_sql(asm, f, produkt_uz_pripojen=True)
    return _dotaz(f"""
        SELECT o.kod, p.nazev, p.dodavatel,
               SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc
        FROM lupa_obrat o
        LEFT JOIN lupa_produkt p ON p.kod = o.kod
        WHERE {kde}
        GROUP BY o.kod, p.nazev, p.dodavatel
        ORDER BY kc DESC
        LIMIT {_STROP_NAHLED}
    """, par, slovnik=True)


# ============================================================================
# Záložka "Lupni srovnání" — zákazník proti zákazníkovi po produktech
# ============================================================================

def _srovnani_zakazniku(asm, ico_a, ico_b, filtr):
    """Řádek = produkt, sloupce = obrat A vs B za zvolené období.

    Jeden dotaz na obě IČ a pivot v Pythonu — dva zákazníci dají stovky řádků,
    dvojitý dotaz ani SQL pivot se nevyplatí."""
    f = dict(filtr, ico=[ico_a, ico_b])
    kde, par, _ = _filtr_sql(asm, f, produkt_uz_pripojen=True)
    syrove = _dotaz(f"""
        SELECT o.ico, o.kod, p.nazev, p.dodavatel,
               SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc
        FROM lupa_obrat o
        LEFT JOIN lupa_produkt p ON p.kod = o.kod
        WHERE {kde}
        GROUP BY o.ico, o.kod, p.nazev, p.dodavatel
    """, par, slovnik=True)

    radky = {}
    for r in syrove:
        radek = radky.setdefault(r['kod'], {
            'kod': r['kod'], 'nazev': r['nazev'], 'dodavatel': r['dodavatel'],
            'a_mj': 0.0, 'a_kc': 0.0, 'b_mj': 0.0, 'b_kc': 0.0})
        strana = 'a' if r['ico'] == ico_a else 'b'
        radek[strana + '_mj'] += float(r['mj'] or 0)
        radek[strana + '_kc'] += float(r['kc'] or 0)

    for radek in radky.values():
        ma_a = bool(radek['a_mj'] or radek['a_kc'])
        ma_b = bool(radek['b_mj'] or radek['b_kc'])
        radek['rozdil_kc'] = radek['a_kc'] - radek['b_kc']
        radek['stav'] = 'oba' if ma_a and ma_b else ('jen A' if ma_a else 'jen B')
    return sorted(radky.values(), key=lambda r: abs(r['rozdil_kc']), reverse=True)


def _cols_srovnani(jm_a, jm_b):
    """Sloupce pro grid i XLSX. Jména zákazníků jdou do hlaviček, jinak by
    v exportu nebylo poznat, kdo je A a kdo B."""
    return [
        ('Kód', 'kod', 'text', 16),
        ('Produkt', 'nazev', 'text', 38),
        ('Dodavatel', 'dodavatel', 'text', 24),
        (f'A · {jm_a} — MJ', 'a_mj', 'num', 16),
        (f'A · {jm_a} — Kč', 'a_kc', 'money', 20),
        (f'B · {jm_b} — MJ', 'b_mj', 'num', 16),
        (f'B · {jm_b} — Kč', 'b_kc', 'money', 20),
        ('Rozdíl Kč (A−B)', 'rozdil_kc', 'money', 20),
        ('Stav', 'stav', 'text', 10),
    ]


def _srovnani_total(rows):
    return {
        'nazev': f'CELKEM {len(rows)} produktů',
        'a_mj': sum(r['a_mj'] for r in rows),
        'a_kc': sum(r['a_kc'] for r in rows),
        'b_mj': sum(r['b_mj'] for r in rows),
        'b_kc': sum(r['b_kc'] for r in rows),
        'rozdil_kc': sum(r['rozdil_kc'] for r in rows),
    }


# ============================================================================
# Záložka "Lupni obraty" — časové řady, OZ, propady, TOP položky
# ============================================================================

_TOP_ZAK_GRAF = 10      # kolik zákazníků kreslit do grafu vývoje
_TOP_POLOZEK = 300      # TOP N produktů do tabulky/exportu
_PROPAD_OKNO = 3        # kolik předchozích měsíců tvoří srovnávací základ

_MESICE_KR = ['Led', 'Úno', 'Bře', 'Dub', 'Kvě', 'Čvn',
              'Čvc', 'Srp', 'Zář', 'Říj', 'Lis', 'Pro']

# Odstíny pro série grafů (roky, OZ, zákazníci) — dost odlišné i po tisku.
_GRAF_PALETA = [
    '#4338CA', '#EA580C', '#059669', '#7C3AED', '#0891B2', '#DC2626',
    '#65A30D', '#DB2777', '#0284C7', '#B45309', '#4F46E5', '#15803D',
]


def _mesic_txt(klic):
    """202408 → '08/2024'."""
    return f'{klic % 100:02d}/{klic // 100}'


def _rada_dealeru(asm, filtr):
    """Obrat po měsících × OZ. Jeden dotaz pokrývá i celkovou řadu ASM —
    součet přes OZ je totéž a šetří druhý průchod tabulkou.

    Kód OZ zkrácen na první 3 znaky — varianty 635A-PLUS / 635D-PLUS
    se sloučí pod jedno 635. Jméno = abecedně první ze skupiny."""
    kde, par, join_p = _filtr_sql(asm, filtr)
    jp = 'JOIN lupa_produkt p ON p.kod = o.kod' if join_p else ''
    return _dotaz(f"""
        SELECT o.rok, o.mesic, LEFT(o.dealer, 3) AS dealer,
               MIN(COALESCE(d.jmeno, o.dealer)) AS dealer_jmeno,
               SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc
        FROM lupa_obrat o {jp}
        LEFT JOIN lupa_dealer d ON d.dealer = o.dealer
        WHERE {kde}
        GROUP BY o.rok, o.mesic, LEFT(o.dealer, 3)
        ORDER BY o.rok, o.mesic
    """, par, slovnik=True)


def _pokryti_oz(asm, filtr):
    """Zákazníků a položek na OZ za celé období. Vlastní dotaz schválně: přidat
    OZ do řady zákazníků by řádky rozmnožilo na zákazník × OZ × měsíc."""
    kde, par, join_p = _filtr_sql(asm, filtr)
    jp = 'JOIN lupa_produkt p ON p.kod = o.kod' if join_p else ''
    return _dotaz(f"""
        SELECT LEFT(o.dealer, 3) AS dealer,
               COUNT(DISTINCT o.ico) AS zakazniku,
               COUNT(DISTINCT o.kod) AS polozek
        FROM lupa_obrat o {jp}
        WHERE {kde}
        GROUP BY LEFT(o.dealer, 3)
    """, par, slovnik=True)


def _rada_zakazniku(asm, filtr):
    """Obrat po měsících × zákazník. Řádů je ~počet zákazníků × počet měsíců
    (jednotky desítek tisíc) — vývoj v čase i propady dopočítáme v Pythonu."""
    kde, par, join_p = _filtr_sql(asm, filtr)
    jp = 'JOIN lupa_produkt p ON p.kod = o.kod' if join_p else ''
    return _dotaz(f"""
        SELECT o.rok, o.mesic, o.ico, z.jmeno, SUM(o.obrat_kc) AS kc
        FROM lupa_obrat o {jp}
        LEFT JOIN lupa_zakaznik z ON z.ico = o.ico
        WHERE {kde}
        GROUP BY o.rok, o.mesic, o.ico, z.jmeno
        ORDER BY o.rok, o.mesic
    """, par, slovnik=True)


def _top_produkty(asm, filtr, limit=_TOP_POLOZEK):
    """TOP odebrané položky za celé zvolené období."""
    kde, par, _ = _filtr_sql(asm, filtr, produkt_uz_pripojen=True)
    return _dotaz(f"""
        SELECT o.kod, p.nazev, p.dodavatel,
               MAX(k.skupina) AS skupina,
               COUNT(DISTINCT o.ico) AS zakazniku,
               SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc
        FROM lupa_obrat o
        LEFT JOIN lupa_produkt p ON p.kod = o.kod
        LEFT JOIN lupa_karta k ON k.kod = o.kod
        WHERE {kde}
        GROUP BY o.kod, p.nazev, p.dodavatel
        ORDER BY kc DESC
        LIMIT {int(limit)}
    """, par, slovnik=True)


def _propady(zak, mesice, prah_pct, prah_kc, okno=_PROPAD_OKNO):
    """Poslední měsíc v datech proti průměru předchozích `okno` měsíců.

    Zákazník bez obratu v posledním měsíci = pokles 100 % ("ztracený") — právě
    ti se v běžném součtovém pohledu ztratí, proto je hlídáme zvlášť."""
    if len(mesice) < 2:
        return []
    posl = mesice[-1]
    predch = mesice[-1 - okno:-1]
    ven = []
    for ico, z in zak.items():
        zaklad = sum(z['kc'].get(m, 0.0) for m in predch) / len(predch)
        if zaklad <= 0:
            continue
        akt = z['kc'].get(posl, 0.0)
        rozdil = akt - zaklad
        pct = rozdil / zaklad * 100.0
        if -pct < prah_pct or -rozdil < prah_kc:
            continue
        ven.append({'ico': ico, 'jmeno': z['jmeno'] or ico, 'zaklad': zaklad,
                    'akt': akt, 'rozdil': rozdil, 'pct': pct,
                    'stav': 'ztracený' if akt <= 0 else 'propad'})
    # Při stejné korunové ztrátě je horší ten, kdo propadl relativně víc —
    # zákazník na nule je jiný problém než zákazník, co jen ubral.
    ven.sort(key=lambda r: (r['rozdil'], r['pct']))
    return ven


def _obraty_prehled(asm, filtr, prah_pct, prah_kc):
    """Vše pro záložku obratů: 4 dotazy, zbytek dopočet nad načtenými řádky."""
    dealeri = _rada_dealeru(asm, filtr)
    zakaznici = _rada_zakazniku(asm, filtr)
    produkty = _top_produkty(asm, filtr)
    pokryti = {r['dealer']: r for r in _pokryti_oz(asm, filtr)}

    mes_kc, mes_mj = {}, {}
    oz = {}
    for r in dealeri:
        k = r['rok'] * 100 + r['mesic']
        kc, mj = float(r['kc'] or 0), float(r['mj'] or 0)
        mes_kc[k] = mes_kc.get(k, 0.0) + kc
        mes_mj[k] = mes_mj.get(k, 0.0) + mj
        d = oz.setdefault(r['dealer'], {'dealer': r['dealer'],
                                        'dealer_jmeno': r['dealer_jmeno'],
                                        'kc': 0.0, 'mj': 0.0, 'rada': {}})
        d['kc'] += kc
        d['mj'] += mj
        d['rada'][k] = d['rada'].get(k, 0.0) + kc

    zak, mes_zak = {}, {}
    for r in zakaznici:
        k = r['rok'] * 100 + r['mesic']
        kc = float(r['kc'] or 0)
        z = zak.setdefault(r['ico'], {'jmeno': r['jmeno'], 'celkem': 0.0, 'kc': {}})
        z['celkem'] += kc
        z['kc'][k] = z['kc'].get(k, 0.0) + kc
        mes_zak.setdefault(k, set()).add(r['ico'])

    mesice = sorted(mes_kc)
    celkem_kc = sum(mes_kc.values())
    radky_mesice = [{'klic': k, 'mesic': _mesic_txt(k), 'kc': mes_kc[k],
                     'mj': mes_mj.get(k, 0.0),
                     'zakazniku': len(mes_zak.get(k, ()))} for k in mesice]
    posl = mesice[-1] if mesice else None
    predch = mesice[-2] if len(mesice) > 1 else None
    lonske = (posl - 100) if posl else None

    oz_radky = sorted(oz.values(), key=lambda d: d['kc'], reverse=True)
    for d in oz_radky:
        d['podil'] = d['kc'] / celkem_kc * 100.0 if celkem_kc else 0.0
        p = pokryti.get(d['dealer'], {})
        d['zakazniku'] = p.get('zakazniku', 0)
        d['polozek'] = p.get('polozek', 0)
        d['posl_kc'] = d['rada'].get(posl, 0.0)
        d['mom'] = _zmena_pct(d['rada'].get(posl), d['rada'].get(predch))
        d['yoy'] = _zmena_pct(d['rada'].get(posl), d['rada'].get(lonske))
        d['prumer_kc'] = d['kc'] / len(mesice) if mesice else 0.0

    top_zak = sorted(zak.items(), key=lambda p: p[1]['celkem'], reverse=True)
    top_zak = top_zak[:_TOP_ZAK_GRAF]
    return {
        'mesice': mesice,
        'radky_mesice': radky_mesice,
        'oz': oz_radky,
        'zak_serie': [{'name': (z['jmeno'] or ico), 'ico': ico,
                       'data': [round(z['kc'].get(m, 0.0), 2) for m in mesice]}
                      for ico, z in top_zak],
        'propady': _propady(zak, mesice, prah_pct, prah_kc),
        'produkty': produkty,
        'kpi': {
            'celkem_kc': celkem_kc,
            'celkem_mj': sum(mes_mj.values()),
            'zakazniku': len(zak),
            'oz': len(oz),
            'posl_mesic': _mesic_txt(posl) if posl else '—',
            'posl_kc': mes_kc.get(posl, 0.0) if posl else 0.0,
            'mom': _zmena_pct(mes_kc.get(posl), mes_kc.get(predch)),
            'yoy': _zmena_pct(mes_kc.get(posl), mes_kc.get(lonske)),
        },
    }


def _zmena_pct(nove, stare):
    if not stare or nove is None:
        return None
    # abs: u záporného základu (vratky převýší odběr) by růst vyšel jako pokles
    return (nove - stare) / abs(stare) * 100.0


_DETAIL_SQL = """
    SELECT o.asm, o.dealer, d.jmeno AS dealer_jmeno,
           CONCAT(o.rok, 'M', LPAD(o.mesic, 2, '0')) AS mesic,
           o.ico, z.jmeno, z.k_jmeno, z.k_ulice, z.k_mesto,
           IF(z.site, 'Ano', 'Ne') AS site,
           o.kod, p.nazev, p.dodavatel, o.obrat_mj, o.obrat_kc
    FROM lupa_obrat o
    LEFT JOIN lupa_dealer d ON d.dealer = o.dealer
    LEFT JOIN lupa_zakaznik z ON z.ico = o.ico
    LEFT JOIN lupa_produkt p ON p.kod = o.kod
    WHERE {kde}
    ORDER BY o.ico, o.rok, o.mesic, o.kod
"""


def _souhrn_total(rows, rozpad=False):
    return {
        'jmeno': (f'CELKEM {len(rows)} řádků' if rozpad
                  else f'CELKEM {len(rows)} zákazníků'),
        'polozek': sum(int(r.get('polozek') or 0) for r in rows),
        'mj': sum(float(r['mj'] or 0) for r in rows),
        'kc': sum(float(r['kc'] or 0) for r in rows),
    }


# ============================================================================
# Exporty (XLSX streamem, PDF přes Chromium)
# ============================================================================

_COLS_SOUHRN = [
    ('IČO', 'ico', 'text', 16),
    ('Zákazník', 'jmeno', 'text', 38),
    ('K. jméno', 'k_jmeno', 'text', 30),
    ('K. ulice', 'k_ulice', 'text', 28),
    ('K. město', 'k_mesto', 'text', 20),
    ('Sítě', 'site', 'text', 8),
    ('Položek', 'polozek', 'int', 10),
    ('Obrat v MJ', 'mj', 'num', 14),
    ('Obrat v Kč bez DPH', 'kc', 'money', 20),
]

_COLS_SOUHRN_ROZPAD = [
    ('IČO', 'ico', 'text', 16),
    ('Zákazník', 'jmeno', 'text', 38),
    ('K. jméno', 'k_jmeno', 'text', 30),
    ('K. ulice', 'k_ulice', 'text', 28),
    ('K. město', 'k_mesto', 'text', 20),
    ('Sítě', 'site', 'text', 8),
    ('Produkt - Kód', 'kod', 'text', 16),
    ('Produkt - název', 'nazev', 'text', 40),
    ('Dodavatel - popis', 'dodavatel', 'text', 30),
    ('Obrat v MJ', 'mj', 'num', 14),
    ('Obrat v Kč bez DPH', 'kc', 'money', 20),
]

_COLS_DETAIL = [
    ('ASM', 'asm', 'text', 14),
    ('Dealer', 'dealer', 'text', 12),
    ('Dealer jméno', 'dealer_jmeno', 'text', 30),
    ('Měsíc', 'mesic', 'text', 11),
    ('IČO', 'ico', 'text', 16),
    ('Jméno', 'jmeno', 'text', 38),
    ('K. jméno', 'k_jmeno', 'text', 30),
    ('K. ulice', 'k_ulice', 'text', 28),
    ('K. město', 'k_mesto', 'text', 20),
    ('Sítě', 'site', 'text', 8),
    ('Produkt - Kód', 'kod', 'text', 16),
    ('Produkt - název', 'nazev', 'text', 40),
    ('Dodavatel - popis', 'dodavatel', 'text', 30),
    ('Obrat v MJ', 'obrat_mj', 'num', 14),
    ('Obrat v Kč bez DPH', 'obrat_kc', 'money', 20),
]

_COLS_MESICE = [
    ('Měsíc', 'mesic', 'text', 12),
    ('Obrat v MJ', 'mj', 'num', 14),
    ('Obrat v Kč bez DPH', 'kc', 'money', 20),
    ('Zákazníků', 'zakazniku', 'int', 12),
]

_COLS_OZ = [
    ('OZ', 'dealer', 'text', 12),
    ('Jméno', 'dealer_jmeno', 'text', 32),
    ('Obrat v MJ', 'mj', 'num', 14),
    ('Obrat v Kč bez DPH', 'kc', 'money', 20),
    ('Podíl %', 'podil', 'num', 10),
    ('Zákazníků', 'zakazniku', 'int', 12),
    ('Položek', 'polozek', 'int', 10),
    ('Průměr / měsíc Kč', 'prumer_kc', 'money', 18),
    ('Poslední měsíc Kč', 'posl_kc', 'money', 18),
    ('MoM %', 'mom', 'num', 10),
    ('YoY %', 'yoy', 'num', 10),
]

_COLS_PROPADY = [
    ('IČO', 'ico', 'text', 16),
    ('Zákazník', 'jmeno', 'text', 38),
    ('Průměr předchozích Kč', 'zaklad', 'money', 22),
    ('Poslední měsíc Kč', 'akt', 'money', 20),
    ('Rozdíl Kč', 'rozdil', 'money', 18),
    ('Změna %', 'pct', 'num', 12),
    ('Stav', 'stav', 'text', 12),
]

_COLS_POLOZKY = [
    ('Kód', 'kod', 'text', 16),
    ('Produkt', 'nazev', 'text', 40),
    ('Dodavatel', 'dodavatel', 'text', 30),
    ('Zákazníků', 'zakazniku', 'int', 12),
    ('Obrat v MJ', 'mj', 'num', 14),
    ('Obrat v Kč bez DPH', 'kc', 'money', 20),
]


def _cols_polozky(rozpad):
    """Sloupce TOP položek do exportu — skupina zboží jen v rozpadu, ať papír
    odpovídá tomu, co měl uživatel na obrazovce."""
    if not rozpad:
        return _COLS_POLOZKY
    return (_COLS_POLOZKY[:2] + [('Skupina zboží', 'skupina', 'text', 28)]
            + _COLS_POLOZKY[2:])


_XLSX_FMT = {'text': '@', 'int': '#,##0', 'num': '#,##0.###', 'money': '#,##0.00'}


def _xlsx_format(typ, val):
    """Formát buňky. U „num" (#,##0.###) Excel vykreslí desetinný oddělovač i u
    celého čísla („42,"), proto celá čísla dostanou formát bez desetin — i plovoucí
    šum typu 199,99999999999997, který se na zobrazené 3 desetiny zaokrouhlí na celé."""
    if typ == 'num' and isinstance(val, (int, float)) and round(float(val), 3).is_integer():
        return _XLSX_FMT['int']
    return _XLSX_FMT.get(typ, 'General')

_TMP_DIR = os.path.join(tempfile.gettempdir(), 'jip_lupa')


def _xlsx_hodnota(v, typ):
    if v is None or v == '':
        return None
    if typ in ('int', 'num', 'money'):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return str(v)


def _xlsx_list(wb, sheet, cols, radky, total=None):
    """Zapíše jeden list write_only sešitu; `radky` smí být iterátor."""
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet((sheet or 'Data')[:31])
    for i, (_n, _f, _t, sirka) in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = sirka

    hlavicka = []
    for nadpis, _f, _t, _w in cols:
        c = WriteOnlyCell(ws, value=nadpis)
        c.font = Font(bold=True, color='FFFFFF', size=11)
        c.fill = PatternFill('solid', fgColor='3730A3')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        hlavicka.append(c)
    ws.append(hlavicka)
    ws.freeze_panes = 'A2'

    pocet = 0
    for r in radky:
        bunky = []
        for _n, field, typ, _w in cols:
            v = _xlsx_hodnota(r.get(field), typ)
            c = WriteOnlyCell(ws, value=v)
            c.number_format = _xlsx_format(typ, v)
            bunky.append(c)
        ws.append(bunky)
        pocet += 1

    ws.auto_filter.ref = f'A1:{get_column_letter(len(cols))}{pocet + 1}'

    if total:
        # Prázdný řádek a až pod ním součet — mimo rozsah autofiltru, aby ho
        # filtrování v Excelu neschovalo a nepočítalo mezi data.
        ws.append([])
        radek = []
        for _n, field, typ, _w in cols:
            v = _xlsx_hodnota(total.get(field), typ)
            c = WriteOnlyCell(ws, value=v)
            c.number_format = _xlsx_format(typ, v)
            c.font = Font(bold=True)
            radek.append(c)
        ws.append(radek)

    return pocet


def _xlsx_na_disk(cesta, cols, radky, total=None, sheet='Data'):
    """Streamovaný zápis .xlsx (openpyxl write_only) rovnou na disk.

    Proč ne běžný Workbook jako v modulu Sankce: detailní výjezd jednoho ASM je
    až 600 tis. řádků × 14 sloupců. Klasický sešit drží každou buňku jako objekt
    v paměti (~3 GB) — write_only zapisuje průběžně a paměť zůstává konstantní.
    `radky` je proto iterátor, ne list."""
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    pocet = _xlsx_list(wb, sheet, cols, radky, total)
    wb.save(cesta)
    return pocet


def _xlsx_listy_na_disk(cesta, listy):
    """Jeden sešit, víc listů: listy = [(nazev, cols, radky, total)]."""
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    for nazev, cols, radky, total in listy:
        _xlsx_list(wb, nazev, cols, radky, total)
    wb.save(cesta)


def _detail_iter(conn, asm, filtr, davka=2000):
    """Nebufferovaný kurzor → generátor dictů. Řádky tečou z DB rovnou do sešitu."""
    kde, par, _ = _filtr_sql(asm, filtr, produkt_uz_pripojen=True)
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(_DETAIL_SQL.format(kde=kde), par)
        while True:
            radky = cur.fetchmany(davka)
            if not radky:
                return
            for r in radky:
                yield r
    finally:
        cur.close()


def _export_detail_na_disk(asm, filtr, cesta):
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Není spojení s databází.')
    try:
        return _xlsx_na_disk(cesta, _COLS_DETAIL, _detail_iter(conn, asm, filtr),
                             sheet='Detail')
    finally:
        conn.close()


def _uklid_tmp(max_age_s=1800):
    """Smaže dočasné exporty starší než ~30 min (obsahují obchodní data)."""
    try:
        ted = time.time()
        for f in os.listdir(_TMP_DIR):
            cesta = os.path.join(_TMP_DIR, f)
            try:
                if os.path.isfile(cesta) and ted - os.path.getmtime(cesta) > max_age_s:
                    os.remove(cesta)
            except OSError:
                pass
    except FileNotFoundError:
        pass


def _tmp_cesta(jmeno):
    os.makedirs(_TMP_DIR, exist_ok=True)
    _uklid_tmp()
    return os.path.join(_TMP_DIR, f'{int(time.time() * 1000)}_{jmeno}')


def _stahni_soubor(cesta, jmeno):
    """Přes HTTP, ne přes WebSocket — ui.download.content posílá bajty socketem
    a nad ~1 MB padá na engine.io limitu. Takhle jde klientovi jen URL."""
    ui.download.file(cesta, jmeno)

    async def _pozdeji():
        await asyncio.sleep(300)
        try:
            os.remove(cesta)
        except OSError:
            pass

    try:
        asyncio.create_task(_pozdeji())
    except RuntimeError:
        pass   # mimo event loop — uklidí to _uklid_tmp při příštím exportu


def _stahni_bajty(data, jmeno):
    cesta = _tmp_cesta(jmeno)
    with open(cesta, 'wb') as fh:
        fh.write(data)
    _stahni_soubor(cesta, jmeno)


def _bezpecne_jmeno(s):
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^A-Za-z0-9_\- ]', '', s).strip().replace(' ', '_') or 'lupa'


def _cz(hodnota, desetin=2):
    """Číslo v českém tvaru: 1 234 567,89 (mezera = NBSP, aby se nezlomilo)."""
    try:
        s = f'{float(hodnota or 0):,.{desetin}f}'
    except (TypeError, ValueError):
        return ''
    return s.replace(',', ' ').replace('.', ',')


_PDF_CSS = """
<style>
  .lupa-doc { font-family: Arial, sans-serif; color:#111; width:190mm; }
  .lupa-doc .hdr { display:flex; justify-content:space-between; align-items:flex-start;
        border-bottom:2px solid #3730A3; padding-bottom:8px; margin-bottom:10px; }
  .lupa-doc h1 { font-size:18px; margin:0 0 2px; color:#312e81; }
  .lupa-doc h2 { font-size:13px; margin:14px 0 5px; color:#312e81;
        page-break-after:avoid; }
  .lupa-doc .firma { font-size:11px; color:#374151; }
  .lupa-doc .meta { font-size:11px; color:#374151; line-height:1.55; text-align:right; }
  .lupa-doc .meta b { color:#111; }
  .lupa-doc .filtr { font-size:11px; color:#374151; margin-bottom:8px; }
  .lupa-doc table.t { width:100%; border-collapse:collapse; font-size:10px;
        page-break-inside:auto; }
  .lupa-doc table.t thead { display:table-header-group; }
  .lupa-doc table.t tr { page-break-inside:avoid; break-inside:avoid; }
  .lupa-doc table.t th { background:#e0e7ff; color:#312e81; border:1px solid #c7d2fe;
        padding:5px 6px; text-align:left; }
  .lupa-doc table.t td { border:1px solid #e5e7eb; padding:4px 6px; vertical-align:top; }
  .lupa-doc table.t td.r { text-align:right; white-space:nowrap; }
  .lupa-doc table.t.husta { font-size:9px; }
  .lupa-doc table.t.husta th, .lupa-doc table.t.husta td { padding:3px 4px; }
  .lupa-doc table.t tr:nth-child(even) td { background:#fafafa; }
  .lupa-doc table.t tr.celkem td { background:#eef2ff; font-weight:bold;
        border-top:2px solid #3730A3; }
  .lupa-doc .pozn { margin-top:14px; font-size:9px; color:#6b7280; }
  .lupa-doc .graf { margin:12px 0 2px; page-break-inside:avoid; break-inside:avoid; }
</style>
"""


def _popis_filtru(filtr, zakaznici_volby):
    casti = []
    od, do = filtr.get('od'), filtr.get('do')
    casti.append(f'Období: {od or "od začátku"} – {do or "do konce"}')
    ica = filtr.get('ico') or []
    if ica:
        jmena = [zakaznici_volby.get(i, i) for i in ica[:5]]
        casti.append('Zákazníci: ' + ', '.join(jmena)
                     + (f' (+{len(ica) - 5} dalších)' if len(ica) > 5 else ''))
    oz = filtr.get('oz') or []
    if oz:
        casti.append('OZ: ' + ', '.join(oz[:8])
                     + (f' (+{len(oz) - 8} dalších)' if len(oz) > 8 else ''))
    dod = filtr.get('dodavatel') or []
    if dod:
        casti.append('Dodavatelé: ' + ', '.join(dod[:5])
                     + (f' (+{len(dod) - 5} dalších)' if len(dod) > 5 else ''))
    site = filtr.get('site')
    if site in ('ano', 'ne'):
        casti.append('Jen sítě' if site == 'ano' else 'Bez sítí')
    if filtr.get('rozpad'):
        casti.append('Rozpad po produktech')
    return ' · '.join(casti)


def _pdf_html(asm, rows, total, popis_filtru, uzivatel, rozpad=False):
    e = html.escape
    # Rozpad po produktech = široká tabulka → adresa zákazníka se do PDF nevejde
    cols = ([c for c in _COLS_SOUHRN_ROZPAD
             if c[1] not in ('k_jmeno', 'k_ulice', 'k_mesto')]
            if rozpad else _COLS_SOUHRN)
    des = {'int': 0, 'num': 1, 'money': 2}
    hlavicky = ''.join(f'<th>{e(n)}</th>' for n, _f, _t, _w in cols)
    textovych = sum(1 for _n, _f, t, _w in cols if t == 'text')

    def _bunka(r, field, typ):
        if typ in des:
            return f'<td class="r">{_cz(r.get(field), des[typ])}</td>'
        return f'<td>{e(str(r.get(field) or ""))}</td>'

    telo = ['<tr>' + ''.join(_bunka(r, f, t) for _n, f, t, _w in cols) + '</tr>'
            for r in rows]
    celkem = (f'<td colspan="{textovych}">'
              f'{e(str(total.get("jmeno") or "CELKEM"))}</td>'
              + ''.join(_bunka(total, f, t) for _n, f, t, _w in cols
                        if t != 'text'))

    return (
        '<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
        f'<title>Lupou na obchod — {e(asm)}</title>{_PDF_CSS}</head><body>'
        '<div class="lupa-doc">'
        '<div class="hdr"><div>'
        f'<h1>Lupou na obchod — odběratelé</h1>'
        f'<div class="firma">JIP východočeská, a.s. · ASM {e(asm.capitalize())}</div>'
        '</div><div class="meta">'
        f'<b>Vytiskl:</b> {e(uzivatel)}<br>'
        f'<b>Datum:</b> {datetime.datetime.now():%d.%m.%Y %H:%M}<br>'
        f'<b>{"Řádků" if rozpad else "Zákazníků"}:</b> {len(rows)}'
        '</div></div>'
        f'<div class="filtr">{e(popis_filtru)}</div>'
        '<table class="t"><thead><tr>' + hlavicky + '</tr></thead>'
        '<tbody>' + ''.join(telo)
        + '<tr class="celkem">' + celkem
        + '</tr></tbody></table>'
        '<div class="pozn">Obraty bez DPH. Sestava vychází z dat GIST nahraných '
        'do modulu Lupopřípad; zdrojem pravdy zůstává GIST.</div>'
        '</div></body></html>'
    )


def _pdf_tabulka(nadpis, cols, rows, limit=60):
    """Obecná tabulka do PDF podle `cols` (nadpis, field, typ, šířka)."""
    e = html.escape
    if not rows:
        return f'<h2>{e(nadpis)}</h2><div class="filtr">Žádná data.</div>'
    des = {'int': 0, 'num': 1, 'money': 2}
    hlav = ''.join(f'<th>{e(n)}</th>' for n, _f, _t, _w in cols)
    telo = []
    for r in rows[:limit]:
        bunky = []
        for _n, field, typ, _w in cols:
            v = r.get(field)
            if typ in des:
                bunky.append(f'<td class="r">{_cz(v, des[typ])}</td>')
            else:
                bunky.append(f'<td>{e(str(v or ""))}</td>')
        telo.append('<tr>' + ''.join(bunky) + '</tr>')
    vic = (f'<div class="pozn">Zobrazeno prvních {limit} z {len(rows)} řádků — '
           'úplný seznam je v XLSX.</div>') if len(rows) > limit else ''
    return (f'<h2>{e(nadpis)}</h2>'
            '<table class="t"><thead><tr>' + hlav + '</tr></thead><tbody>'
            + ''.join(telo) + '</tbody></table>' + vic)


# ── Grafy do PDF ───────────────────────────────────────────────────────────
# Kreslíme SVG ručně v Pythonu. Grafy v UI dělá ECharts (JS) a tiskové
# Chromium by na doběhnutí skriptu muselo čekat — render vrací PDF po `load`,
# takže by graf vyšel prázdný. Tři typy grafů ručně jsou levnější než
# přestavovat sdílenou tiskárnu na čekání a vozit do PDF 1 MB JS knihovny.
_SVG_W = 720            # 190 mm sazby při 96 dpi
_SVG_POLE = 210         # výška kreslicí plochy


def _svg_txt(x, y, text, velikost=10, barva='#374151', kotva='middle', tucne=False):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{velikost}" fill="{barva}" '
            f'text-anchor="{kotva}"{" font-weight=\"600\"" if tucne else ""}>'
            f'{html.escape(str(text))}</text>')


def _svg_kroky(maxv, n=4):
    """Popisky osy Y v hezkých krocích (1/2/2,5/5 × 10^k), ne v 137 429."""
    if not maxv or maxv <= 0:
        return [0.0, 1.0]
    hrubo = maxv / n
    exp = 10 ** math.floor(math.log10(hrubo))
    krok = next(m * exp for m in (1, 2, 2.5, 5, 10) if m * exp >= hrubo)
    return [i * krok for i in range(int(maxv / krok) + 2)]


def _svg_obal(vyska, telo):
    return (f'<div class="graf"><svg viewBox="0 0 {_SVG_W} {vyska:.0f}" width="100%" '
            f'xmlns="http://www.w3.org/2000/svg" font-family="Arial, sans-serif">'
            f'{telo}</svg></div>')


def _svg_graf(nadpis, podnadpis, popisky_x, serie, typ='line', pozn=''):
    """Spojnice nebo skupinové sloupce; série = rok / OZ / zákazník."""
    if not popisky_x or not serie:
        return f'<h2>{html.escape(nadpis)}</h2><div class="filtr">Žádná data.</div>'
    x0, x1 = 84, _SVG_W - 14
    y0 = 46 if podnadpis else 32
    y1 = y0 + _SVG_POLE
    hodnoty = [v for s in serie for v in s['data'] if v is not None]
    kroky = _svg_kroky(max(hodnoty) if hodnoty else 0)
    vrchol = kroky[-1] or 1
    o = [_svg_txt(_SVG_W / 2, 18, nadpis, 13, '#312e81', tucne=True)]
    if podnadpis:
        o.append(_svg_txt(_SVG_W / 2, 33, podnadpis, 10, '#6b7280'))
    for k in kroky:
        y = y1 - (k / vrchol) * _SVG_POLE
        o.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb" stroke-width="1"/>')
        o.append(_svg_txt(x0 - 6, y + 3.5, _cz(k, 0), 9, '#6b7280', 'end'))
    krok_x = (x1 - x0) / len(popisky_x)
    kazdy = max(1, -(-len(popisky_x) // 14))   # popisky ředíme, ať se nepřekrývají
    for i, p in enumerate(popisky_x):
        if i % kazdy == 0:
            o.append(_svg_txt(x0 + (i + 0.5) * krok_x, y1 + 14, p, 9, '#6b7280'))
    for si, s in enumerate(serie):
        barva = _GRAF_PALETA[si % len(_GRAF_PALETA)]
        if typ == 'bar':
            sirka = max(1.2, krok_x * 0.74 / len(serie))
            for i, v in enumerate(s['data']):
                if not v or v <= 0:
                    continue
                x = x0 + (i + 0.5) * krok_x - len(serie) * sirka / 2 + si * sirka
                y = y1 - (v / vrchol) * _SVG_POLE
                o.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{sirka:.1f}" '
                         f'height="{max(0.0, y1 - y):.1f}" fill="{barva}"/>')
        else:
            body = [(x0 + (i + 0.5) * krok_x, y1 - (v / vrchol) * _SVG_POLE)
                    for i, v in enumerate(s['data']) if v is not None]
            if not body:
                continue
            o.append(f'<polyline fill="none" stroke="{barva}" stroke-width="1.6" '
                     'points="' + ' '.join(f'{x:.1f},{y:.1f}' for x, y in body) + '"/>')
            o += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.7" fill="{barva}"/>'
                  for x, y in body]
    # Legenda pod osu; delší jména lámeme do dalšího řádku
    y_leg, x_leg = y1 + 32, x0
    for si, s in enumerate(serie):
        jmeno = str(s['name'])[:28]
        sirka_p = 22 + len(jmeno) * 5.2
        if x_leg + sirka_p > x1 and x_leg > x0:
            x_leg, y_leg = x0, y_leg + 14
        o.append(f'<rect x="{x_leg:.1f}" y="{y_leg - 8}" width="9" height="9" '
                 f'fill="{_GRAF_PALETA[si % len(_GRAF_PALETA)]}"/>')
        o.append(_svg_txt(x_leg + 13, y_leg, jmeno, 9, '#374151', 'start'))
        x_leg += sirka_p
    if pozn:
        y_leg += 14
        o.append(_svg_txt(x0, y_leg, pozn, 9, '#6b7280', 'start'))
    return _svg_obal(y_leg + 8, ''.join(o))


def _svg_graf_polozky(produkty, n=15):
    """Vodorovné sloupce — názvy produktů se na osu X nevejdou."""
    top = [p for p in produkty[:n] if float(p['kc'] or 0) > 0]
    if not top:
        return ''
    x0, x1, y0 = 250, _SVG_W - 70, 46
    radek = 18
    maxv = max(float(p['kc'] or 0) for p in top)
    o = [_svg_txt(_SVG_W / 2, 18, f'TOP {len(top)} položek podle obratu (Kč bez DPH)',
                  13, '#312e81', tucne=True),
         _svg_txt(_SVG_W / 2, 33, 'Nejsilnější produkt nahoře, součet za celé '
                                  'zvolené období.', 10, '#6b7280')]
    for i, p in enumerate(top):
        y = y0 + i * radek
        v = float(p['kc'] or 0)
        sirka = (v / maxv) * (x1 - x0)
        o.append(_svg_txt(x0 - 6, y + 10, (p['nazev'] or p['kod'] or '')[:40],
                          9, '#374151', 'end'))
        o.append(f'<rect x="{x0}" y="{y + 2:.1f}" width="{sirka:.1f}" height="12" '
                 f'fill="#4338CA"/>')
        o.append(_svg_txt(x0 + sirka + 5, y + 12, _cz(v, 0), 9, '#374151', 'start'))
    return _svg_obal(y0 + len(top) * radek + 8, ''.join(o))


def _pdf_grafy(data, max_serii=10):
    """Grafy z karty Obraty ve stejném pořadí jako v UI."""
    mesice = data['mesice']
    oz = data['oz'][:max_serii]
    pozn_oz = (f'Zobrazeno {len(oz)} OZ s největším obratem z {len(data["oz"])} '
               '— úplný přehled je v tabulce.') if len(data['oz']) > len(oz) else ''
    return (
        _svg_graf('Obrat ASM po měsících (Kč bez DPH, meziroční srovnání)',
                  'Sloupec = měsíc, barva = rok.', _MESICE_KR,
                  _serie_yoy(data['radky_mesice']), typ='bar')
        + _svg_graf('Obrat OZ v čase (Kč bez DPH)',
                    'Čára = jeden obchodní zástupce, měsíc po měsíci.',
                    [_mesic_txt(m) for m in mesice],
                    [{'name': d['dealer_jmeno'],
                      'data': [round(d['rada'].get(m, 0.0), 2) for m in mesice]}
                     for d in oz], pozn=pozn_oz)
        + _svg_graf(f'Vývoj obratů — TOP {len(data["zak_serie"])} zákazníků',
                    'Čára = jeden zákazník s největším obratem.',
                    [_mesic_txt(m) for m in mesice], data['zak_serie'])
        + _svg_graf_polozky(data['produkty'])
    )


def _serie_yoy(radky_mesice):
    """Osa X = měsíc 1–12, série = rok (týž rozpad jako `_graf_yoy_option`)."""
    kc = {r['klic']: r['kc'] for r in radky_mesice}
    return [{'name': str(rok),
             'data': [kc.get(rok * 100 + m) for m in range(1, 13)]}
            for rok in sorted({k // 100 for k in kc})]


def _pdf_obraty_html(asm, data, popis_filtru, uzivatel, rozpad=False):
    e = html.escape
    k = data['kpi']
    zmena = lambda v: '—' if v is None else f'{v:+.1f} %'.replace('.', ',')
    return (
        '<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
        f'<title>Lupou na obchod — obraty {e(asm)}</title>{_PDF_CSS}</head><body>'
        '<div class="lupa-doc">'
        '<div class="hdr"><div>'
        '<h1>Lupou na obchod — obraty</h1>'
        f'<div class="firma">JIP východočeská, a.s. · ASM {e(asm.capitalize())}</div>'
        '</div><div class="meta">'
        f'<b>Vytiskl:</b> {e(uzivatel)}<br>'
        f'<b>Datum:</b> {datetime.datetime.now():%d.%m.%Y %H:%M}<br>'
        f'<b>Obrat celkem:</b> {_cz(k["celkem_kc"])} Kč'
        '</div></div>'
        f'<div class="filtr">{e(popis_filtru)}</div>'
        f'<div class="filtr">Zákazníků: {k["zakazniku"]} · OZ: {k["oz"]} · '
        f'Poslední měsíc {e(k["posl_mesic"])}: {_cz(k["posl_kc"])} Kč '
        f'(meziměsíčně {zmena(k["mom"])}, meziročně {zmena(k["yoy"])})</div>'
        + _pdf_tabulka('Obrat po měsících', _COLS_MESICE, data['radky_mesice'], 40)
        + _pdf_tabulka('Výsledky OZ', _COLS_OZ, data['oz'], 40)
        + _pdf_tabulka('Propady zákazníků', _COLS_PROPADY, data['propady'])
        + _pdf_tabulka('TOP odebrané položky', _cols_polozky(rozpad),
                        data['produkty'])
        + _pdf_grafy(data)
        + '<div class="pozn">Obraty bez DPH. Poslední měsíc může být neúplný, '
        'pokud import proběhl v jeho průběhu. Zdrojem pravdy zůstává GIST. '
        'PDF ukazuje celý zvolený výběr — filtr TOP položek nastavený v aplikaci '
        'se do něj nepromítá, na filtrovaný výběr použijte XLSX.</div>'
        '</div></body></html>'
    )


async def _render_pdf(html_text, jmeno, notifikace=None):
    """Render deleguje na modul Sankce — na serveru je jedno sdílené Chromium
    chráněné semaforem. Vlastní launch by znamenal dvě Chromia naráz a dvojnásobek
    paměti při souběžném tisku."""
    import intranet_sankce
    pdfs = await intranet_sankce._render_pdf_bytes_batch(
        [(jmeno, html_text)],
        on_wait=lambda: notifikace and notifikace.__setattr__(
            'message', 'Čekám ve frontě na tiskárnu…'),
        on_start=lambda: notifikace and notifikace.__setattr__(
            'message', 'Renderuji PDF…'))
    return pdfs[0][1]


def _cas_txt(hodnota):
    return hodnota.strftime('%d.%m.%Y %H:%M') if hodnota else ''


def _vykresli_poznamky(asm, user_id, user_name, vsechna_prava):
    admin = je_admin(vsechna_prava)
    smi_psat = je_moje_asm(user_id, asm, vsechna_prava)

    def smi_smazat(radek):
        return admin or (bool(user_id) and radek.get('user_id') == user_id)

    @ui.refreshable
    def seznam():
        radky = poznamky(asm)
        if not radky:
            with ui.column().classes('items-center py-16 gap-3 w-full'):
                ui.icon('sticky_note_2', size='4rem', color='grey-3')
                ui.label('Zatím žádná poznámka.' if smi_psat else
                         'ASM zatím nic nezapsal.') \
                    .classes('text-lg font-semibold text-gray-400')
            return

        for p in radky:
            with ui.card().classes('w-full shadow-sm rounded-xl mb-3'):
                with ui.row().classes('w-full items-center gap-2'):
                    ui.icon('person', color='indigo-4')
                    ui.label(p['uzivatel'] or '—').classes('font-semibold text-gray-800')
                    ui.label('· ' + _cas_txt(p['vytvoreno'])).classes('text-xs text-gray-500')
                    if p['upraveno']:
                        ui.label(f"(upraveno {_cas_txt(p['upraveno'])})") \
                            .classes('text-xs text-gray-400 italic')
                    ui.space()
                    if smi_smazat(p):
                        ui.button(icon='edit', on_click=lambda _, p=p: _dialog_edit(p)) \
                            .props('flat round dense color=grey-7').tooltip('Upravit')
                        ui.button(icon='delete',
                                  on_click=lambda _, p=p: _smaz_poznamku(p)) \
                            .props('flat round dense color=red-5').tooltip('Smazat')
                ui.label(p['text'] or '').classes('whitespace-pre-wrap text-gray-800 mt-1')

                with ui.column().classes('w-full gap-2 mt-2 pl-4 border-l-4 border-gray-200'):
                    for k in p['komentare']:
                        with ui.row().classes('w-full items-start gap-2'):
                            ui.icon('reply', size='1rem', color='grey-5').classes('mt-1')
                            with ui.column().classes('gap-0 grow'):
                                with ui.row().classes('items-center gap-2'):
                                    ui.label(k['uzivatel'] or '—') \
                                        .classes('text-sm font-semibold text-gray-700')
                                    ui.label(_cas_txt(k['vytvoreno'])) \
                                        .classes('text-xs text-gray-500')
                                ui.label(k['text'] or '') \
                                    .classes('whitespace-pre-wrap text-sm text-gray-700')
                            if smi_smazat(k):
                                ui.button(icon='close',
                                          on_click=lambda _, k=k: _smaz_komentar(k)) \
                                    .props('flat round dense size=sm color=grey-5')

                    with ui.row().classes('w-full items-center gap-2'):
                        pole = ui.input(placeholder='Napsat komentář…') \
                            .props('dense outlined').classes('grow')
                        pole.on('keydown.enter', lambda _, p=p, pole=pole: _pridej_komentar(p, pole))
                        ui.button(icon='send', on_click=lambda _, p=p, pole=pole:
                                  _pridej_komentar(p, pole)) \
                            .props('flat round dense color=indigo-6')

    def _pridej_komentar(p, pole):
        text = (pole.value or '').strip()
        if not text:
            return
        pridej_komentar(p['id'], user_id, user_name, text)
        zapis_log(user_id, user_name, 'komentar', asm, f"poznamka {p['id']}")
        # Autor poznámky se o zpětné vazbě dozví, i když zrovna není v modulu.
        if p.get('user_id') and p['user_id'] != user_id:
            intranet_notifikace.pridej(
                p['user_id'], f'{user_name} okomentoval vaši poznámku v Lupou na obchod ({asm}).')
        pole.value = ''
        seznam.refresh()

    def _smaz_komentar(k):
        smaz_komentar(k['id'])
        zapis_log(user_id, user_name, 'komentar_smaz', asm, str(k['id']))
        seznam.refresh()

    def _smaz_poznamku(p):
        smaz_poznamku(p['id'])
        zapis_log(user_id, user_name, 'poznamka_smaz', asm, str(p['id']))
        ui.notify('Poznámka smazána.', type='positive')
        seznam.refresh()

    def _dialog_edit(p):
        with ui.dialog() as dlg, ui.card().classes('w-full max-w-2xl'):
            ui.label('Úprava poznámky').classes('text-lg font-bold text-gray-800')
            pole = ui.textarea(value=p['text'] or '').props('outlined autogrow').classes('w-full')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zavřít', on_click=dlg.close).props('flat')

                def uloz():
                    text = (pole.value or '').strip()
                    if not text:
                        ui.notify('Prázdnou poznámku neuložím.', type='warning')
                        return
                    uprav_poznamku(p['id'], text)
                    zapis_log(user_id, user_name, 'poznamka_edit', asm, str(p['id']))
                    dlg.close()
                    seznam.refresh()

                ui.button('Uložit', on_click=uloz).props('color=indigo-6')
        dlg.open()

    ui.label('Moje poznámky').classes('text-3xl font-bold text-gray-800 mb-1')
    ui.label('Postřehy k území. Extra čtenáři je vidí a mohou přidat komentář.') \
        .classes('text-gray-500 mb-4')

    if smi_psat:
        with ui.card().classes('w-full shadow-sm rounded-xl mb-4'):
            nova = ui.textarea(placeholder='Co je nového u zákazníků, kde je příležitost…') \
                .props('outlined autogrow').classes('w-full')

            def pridej():
                text = (nova.value or '').strip()
                if not text:
                    ui.notify('Napište nejdřív text poznámky.', type='warning')
                    return
                pridej_poznamku(asm, user_id, user_name, text)
                zapis_log(user_id, user_name, 'poznamka', asm, pocet_radku=len(text))
                nova.value = ''
                ui.notify('Poznámka uložena.', type='positive')
                seznam.refresh()

            with ui.row().classes('w-full justify-end'):
                ui.button('Přidat poznámku', icon='add', on_click=pridej) \
                    .props('unelevated color=indigo-7')
    else:
        ui.label('Psát poznámky může obchodní zástupce daného ASM — vy můžete komentovat.') \
            .classes('text-xs text-gray-500 mb-3')

    seznam()


def _vykresli_detail(asm, user_id, user_name, vsechna_prava):
    with ui.tabs().props('align=left active-color=primary indicator-color=primary') \
            .classes('w-full border-b border-gray-200') as taby:
        ui.tab('odberatele', label='📄 Lupni odběratele')
        ui.tab('obraty', label='📊 Lupni obraty')
        ui.tab('srovnani', label='⚖️ Lupni srovnání')
        ui.tab('poznamky', label='📝 Moje poznámky')
    with ui.tab_panels(taby, value='odberatele').classes('w-full pt-4'):
        with ui.tab_panel('odberatele'):
            _vykresli_odberatele(asm, user_id, user_name, vsechna_prava)
        with ui.tab_panel('obraty'):
            _vykresli_obraty(asm, user_id, user_name, vsechna_prava)
        with ui.tab_panel('srovnani'):
            _vykresli_srovnani(asm, user_id, user_name, vsechna_prava)
        with ui.tab_panel('poznamky'):
            _vykresli_poznamky(asm, user_id, user_name, vsechna_prava)


# Formátování čísel v gridu necháváme na prohlížeči (Intl) — stejný vzor jako
# ve Výsledcích poboček; hodnoty tak zůstávají čísla a dají se řadit.
def _fmt_js(desetin):
    return (
        "function(p){"
        "if(p.value===null||p.value===undefined||p.value==='')return '—';"
        "var n=parseFloat(p.value);if(isNaN(n))return '—';"
        "return new Intl.NumberFormat('cs-CZ',{minimumFractionDigits:%d,"
        "maximumFractionDigits:%d}).format(n);}" % (desetin, desetin)
    )


_STYL_ZMENA = (
    "function(p){"
    "var n=parseFloat(p.value);if(isNaN(n))return null;"
    "return {color: n<0 ? '#b91c1c' : '#15803d'};}"
)


_GRID_ZAKLAD = {
    'defaultColDef': {'resizable': True, 'sortable': True, 'wrapHeaderText': True,
                      'autoHeaderHeight': True},
    'rowHeight': 32,
    'suppressMovableColumns': True,
    ':getRowStyle': ("function(p){if(p.node&&p.node.rowPinned)"
                     "return{fontWeight:'bold',backgroundColor:'#e0e7ff'};return null;}"),
}


def _filtry(cols):
    """Doplní filtr sloupce — AG Grid Community umí text i čísla, jen je nevybírá
    sám. Volá se i mimo _grid(), protože přepínač rozpadu mění columnDefs za běhu."""
    for c in cols:
        c.setdefault('filter', 'agNumberColumnFilter'
                     if c.get('type') == 'numericColumn' else 'agTextColumnFilter')
    return cols


# Identifikátory, které se z gridu kopírují klikem (IČO do JIP, kód produktu do
# objednávek). Renderer vrací element, ne HTML string — hodnota se tak nikdy
# neinterpretuje jako markup. Součtový řádek (rowPinned) kopírovatelný není.
_KOPIROVATELNE = ('ico', 'kod')

_KOPIE_RENDERER = (
    "function(p){"
    "var v=(p.value===null||p.value===undefined)?'':String(p.value);"
    "var e=document.createElement('span');e.textContent=v;"
    "if(v&&!(p.node&&p.node.rowPinned)){e.className='lupa-kopie';"
    "e.title='Kliknutím zkopírujete do schránky';}"
    "return e;}"
)


def _kopirovatelne(cols):
    """Označí sloupce s identifikátory jako kopírovatelné klikem."""
    for c in cols:
        if c.get('field') in _KOPIROVATELNE:
            c.setdefault(':cellRenderer', _KOPIE_RENDERER)
    return cols


def _grid(opts, filtry=False):
    """AG Grid 34 odmítá colDef.flex spolu s gridOptions.autoSizeStrategy, kterou
    NiceGUI přidává při auto_size_columns=True (default) — grid se pak nevykreslí.
    Šířky řešíme přes flex/minWidth, takže strategii vypínáme.

    `filtry` zapíná řádek filtrů pod hlavičkou. Jen tam, kde je grid pracovní
    tabulka bez součtového řádku — pod součtem celé sady by filtr lhal."""
    _kopirovatelne(opts.get('columnDefs') or [])
    if filtry:
        _filtry(opts.get('columnDefs') or [])
        opts['defaultColDef'] = {**(opts.get('defaultColDef') or {}),
                                 'floatingFilter': True}
    return ui.aggrid(opts, auto_size_columns=False)


def _bez_klienta(fn):
    """Načtení a exporty běží desítky sekund až minuty. Klient mezitím může zmizet
    (reload, odchod ze stránky, refresh dlaždic) a každé další UI volání —
    ui.download, ui.notify, notification.dismiss — pak hodí RuntimeError o smazaném
    elementu. Doručovat není komu, takže jen tichý log místo tří tracebacků."""
    async def obal(*args):
        try:
            await fn()
        except RuntimeError as e:
            if 'has been deleted' not in str(e):
                raise
            print(f'[lupa] {fn.__name__}: klient odešel před dokončením')
    return obal


def _vykresli_odberatele(asm, user_id, user_name, vsechna_prava):
    filtr_klic = f'lupa_filtr_{user_id}_{asm}'
    ulozeny = app.storage.user.get(filtr_klic) or {}
    obdobi = _volby_obdobi(asm)
    zak_volby = _volby_zakazniku(asm)
    dod_volby = _volby_dodavatelu()
    oz_volby = _volby_oz(asm)

    # 'vse' = co vrátila DB, 'rows' = co je vidět po přepínači sítí.
    stav = {'vse': [], 'rows': [], 'total': {}, 'filtr': {},
            'popis': '', 'popis_vse': ''}

    def _zaklad_jmena(co):
        return (f'lupa_{_bezpecne_jmeno(asm)}_{co}_'
                f'{datetime.datetime.now():%Y-%m-%d_%H%M}')

    _pruh_importu(asm)

    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_od = _sel_obdobi('Od měsíce', obdobi, ulozeny.get('od'))
        sel_do = _sel_obdobi('Do měsíce', obdobi, ulozeny.get('do'))
        sel_zak = ui.select(
            zak_volby, label='Zákazníci', multiple=True, with_input=True, clearable=True,
            value=[i for i in (ulozeny.get('ico') or []) if i in zak_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 340px') \
            .tooltip('Prázdné = všichni zákazníci ASM. Pište jméno nebo IČO.')
        _pole_ica(sel_zak, zak_volby)
        sel_oz = ui.select(
            oz_volby, label='OZ', multiple=True, with_input=True, clearable=True,
            value=[o for o in (ulozeny.get('oz') or []) if o in oz_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 240px') \
            .tooltip('Prázdné = všichni OZ. Varianty 635A/635D jsou pod kódem 635.')
        sel_dod = ui.select(
            dod_volby, label='Dodavatelé', multiple=True, with_input=True, clearable=True,
            value=[d for d in (ulozeny.get('dodavatel') or []) if d in dod_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 280px') \
            .tooltip('Prázdné = všichni dodavatelé.')
        chk_rozpad = ui.checkbox('Rozpad po produktech',
                                 value=bool(ulozeny.get('rozpad'))) \
            .props('dense color=indigo-7') \
            .tooltip('Řádek = zákazník × produkt, s kódem, názvem a dodavatelem. '
                     'Řádků je násobně víc — na PDF platí strop '
                     f'{_STROP_PDF} řádků.')

    def _soucasny_filtr():
        return {'od': sel_od.value, 'do': sel_do.value,
                'ico': list(sel_zak.value or []), 'oz': list(sel_oz.value or []),
                'dodavatel': list(sel_dod.value or []),
                'rozpad': bool(chk_rozpad.value)}

    with ui.row().classes('w-full items-center gap-3 mb-3 flex-wrap'):
        btn_nacti = ui.button('Načíst', icon='search') \
            .props('unelevated color=indigo-7')
        btn_pdf = ui.button('PDF souhrn', icon='picture_as_pdf') \
            .props('outline color=indigo-7')
        btn_xlsx = ui.button('XLSX souhrn', icon='table_view') \
            .props('outline color=green-7')
        btn_detail = ui.button('Detailní pohled (Excel)', icon='download') \
            .props('outline color=green-9') \
            .tooltip('Surové řádky dle filtru — zákazník × produkt × měsíc. '
                     'U celého ASM jde o stovky tisíc řádků a export trvá minuty. '
                     'Přepínač sítí neřeší, jede vždy celý filtr.')
        tgl_site = ui.toggle({'vse': 'Vše', 'ano': 'Jen sítě', 'ne': 'Bez sítí'},
                             value='vse') \
            .props('dense no-caps unelevated toggle-color=indigo-7') \
            .tooltip('Překresluje už načtená data, Načíst se nemačká znovu. '
                     'PDF a XLSX souhrn exportují zobrazený výběr.')

    for b in (btn_pdf, btn_xlsx, btn_detail, tgl_site):
        b.disable()

    vysledek = ui.column().classes('w-full')

    with vysledek:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('filter_alt', size='4rem', color='grey-3')
            ui.label('Zvolte filtr a klikněte na Načíst') \
                .classes('text-lg font-semibold text-gray-400')

    async def otevri_zakaznika(e):
        radek = (e.args or {}).get('data') or {}
        ico = radek.get('ico')
        if not ico or radek.get('_celkem') or _blokuje_import(asm):
            return
        produkty = await asyncio.to_thread(_produkty_zakaznika, asm, ico, stav['filtr'])
        with ui.dialog() as dlg, ui.card().classes('w-full max-w-6xl'):
            ui.label(f"{radek.get('jmeno') or ico} · {ico}") \
                .classes('text-xl font-bold text-gray-800')
            ui.label(f"{radek.get('k_ulice') or ''} {radek.get('k_mesto') or ''} · "
                     f"{stav['popis']}").classes('text-xs text-gray-500 mb-2')
            _grid({
                'columnDefs': [
                    {'headerName': 'Kód', 'field': 'kod', 'width': 130},
                    {'headerName': 'Produkt', 'field': 'nazev', 'flex': 2, 'minWidth': 240},
                    {'headerName': 'Dodavatel', 'field': 'dodavatel', 'flex': 1,
                     'minWidth': 180},
                    {'headerName': 'Obrat MJ', 'field': 'mj', 'width': 130,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                    {'headerName': 'Obrat Kč', 'field': 'kc', 'width': 150,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(2)},
                ],
                'rowData': produkty,
                'pinnedBottomRowData': [{
                    'nazev': f'CELKEM {len(produkty)} položek',
                    'mj': sum(float(p['mj'] or 0) for p in produkty),
                    'kc': sum(float(p['kc'] or 0) for p in produkty)}],
                **_GRID_ZAKLAD,
            }).classes('w-full').style('height: 60vh')
            with ui.row().classes('w-full justify-end mt-2'):
                ui.button('Zavřít', on_click=dlg.close).props('flat')
        dlg.open()

    def vykresli_vysledek():
        rows, total = stav['rows'], stav['total']
        vysledek.clear()
        with vysledek:
            if not rows:
                with ui.column().classes('items-center py-16 gap-3 w-full'):
                    ui.icon('search_off', size='4rem', color='grey-3')
                    ui.label('Filtru nic neodpovídá') \
                        .classes('text-lg font-semibold text-gray-400')
                return
            zobrazene = rows[:_STROP_NAHLED]
            if len(rows) > _STROP_NAHLED:
                ui.label(f'Náhled ukazuje prvních {_STROP_NAHLED} řádků z '
                         f'{len(rows)} — exporty obsahují vše.') \
                    .classes('text-xs text-amber-700 mb-1')
            rozpad = bool(stav['filtr'].get('rozpad'))
            grid = _grid({
                'columnDefs': [
                    {'headerName': 'IČO', 'field': 'ico', 'width': 130},
                    {'headerName': 'Zákazník', 'field': 'jmeno', 'flex': 2,
                     'minWidth': 220},
                    {'headerName': 'K. jméno', 'field': 'k_jmeno', 'flex': 1,
                     'minWidth': 150},
                    {'headerName': 'K. ulice', 'field': 'k_ulice', 'flex': 1,
                     'minWidth': 150},
                    {'headerName': 'K. město', 'field': 'k_mesto', 'width': 150},
                    {'headerName': 'Sítě', 'field': 'site', 'width': 100},
                ] + ([
                    {'headerName': 'Produkt - Kód', 'field': 'kod', 'width': 130},
                    {'headerName': 'Produkt - název', 'field': 'nazev', 'flex': 2,
                     'minWidth': 220},
                    {'headerName': 'Dodavatel - popis', 'field': 'dodavatel',
                     'flex': 1, 'minWidth': 170},
                ] if rozpad else [
                    {'headerName': 'Položek', 'field': 'polozek', 'width': 110,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(0)},
                ]) + [
                    {'headerName': 'Obrat MJ', 'field': 'mj', 'width': 130,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                    {'headerName': 'Obrat Kč bez DPH', 'field': 'kc', 'width': 170,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(2)},
                ],
                'rowData': zobrazene,
                'pinnedBottomRowData': [dict(total, _celkem=True)],
                **_GRID_ZAKLAD,
            }).classes('w-full').style('height: calc(100vh - 430px); min-height: 380px')
            # Jen 'data': aggrid posílá i args.context (interní objekt AG Gridu
            # s cyklickými odkazy) a JSON.stringify na něm klik v prohlížeči shodí.
            grid.on('rowClicked', otevri_zakaznika, ['data'])
            ui.label('Klikem na řádek zobrazíte, co zákazník ve zvoleném období bral.') \
                .classes('text-xs text-gray-400 mt-1')

    def prepni_site():
        """Přepínač sítí jen filtruje už načtená data — do DB se nesahá."""
        volba = tgl_site.value or 'vse'
        vse = stav['vse']
        rows = vse if volba == 'vse' else [
            r for r in vse if (r.get('site') == 'Ano') == (volba == 'ano')]
        stav['rows'] = rows
        stav['total'] = _souhrn_total(rows, bool(stav['filtr'].get('rozpad')))
        # Export i hlavička dialogu se ptají na popis — musí přiznat zúžení.
        stav['popis'] = stav['popis_vse'] + {
            'ano': ' · Jen sítě', 'ne': ' · Bez sítí'}.get(volba, '')
        for b in (btn_pdf, btn_xlsx):
            b.set_enabled(bool(rows))
        vykresli_vysledek()

    tgl_site.on_value_change(lambda _=None: prepni_site())

    async def nacti():
        if _blokuje_import(asm):
            return
        filtr = _soucasny_filtr()
        app.storage.user[filtr_klic] = filtr
        btn_nacti.disable()
        chk_rozpad.disable()
        dlg, _kruh, _popisek = prekryv_kolecko('Generování přehledu', procenta=False)
        dlg.open()
        try:
            rows = await asyncio.to_thread(_souhrn_odberatelu, asm, filtr)
        finally:
            dlg.close()
            btn_nacti.enable()
            chk_rozpad.enable()
        stav.update({'vse': rows, 'filtr': filtr,
                     'popis_vse': _popis_filtru(filtr, zak_volby)})
        btn_detail.set_enabled(bool(rows))
        tgl_site.set_enabled(bool(rows))
        prepni_site()
        await asyncio.to_thread(zapis_log, user_id, user_name, 'nahled', asm,
                                stav['popis'], len(stav['rows']))

    async def export_pdf():
        rows = stav['rows']
        if len(rows) > _STROP_PDF:
            ui.notify(f'PDF zvládne {_STROP_PDF} řádků, filtr jich vrací {len(rows)}. '
                      'Zpřísněte filtr, nebo použijte XLSX.',
                      type='warning', multi_line=True)
            return
        jmeno = _zaklad_jmena('odberatele') + '.pdf'
        pozn = ui.notification('Připravuji PDF…', spinner=True, timeout=None)
        try:
            html_text = _pdf_html(asm, rows, stav['total'], stav['popis'], user_name,
                                  bool(stav['filtr'].get('rozpad')))
            data = await _render_pdf(html_text, jmeno, pozn)
            _stahni_bajty(data, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'export_pdf', asm,
                                    stav['popis'], len(rows))
        except Exception as e:
            ui.notify(f'PDF se nepodařilo vytvořit: {e}', type='negative',
                      multi_line=True, timeout=15000)
        finally:
            pozn.dismiss()

    async def export_xlsx():
        jmeno = _zaklad_jmena('odberatele') + '.xlsx'
        cesta = _tmp_cesta(jmeno)
        pozn = ui.notification('Sestavuji XLSX…', spinner=True, timeout=None)
        try:
            cols = (_COLS_SOUHRN_ROZPAD if stav['filtr'].get('rozpad')
                    else _COLS_SOUHRN)
            await asyncio.to_thread(_xlsx_na_disk, cesta, cols, stav['rows'],
                                    stav['total'], 'Odběratelé')
            _stahni_soubor(cesta, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'export_xlsx', asm,
                                    stav['popis'], len(stav['rows']))
        except Exception as e:
            ui.notify(f'Export selhal: {e}', type='negative', multi_line=True)
        finally:
            pozn.dismiss()

    async def export_detail():
        if _blokuje_import(asm):
            return
        jmeno = _zaklad_jmena('detail') + '.xlsx'
        cesta = _tmp_cesta(jmeno)
        pozn = ui.notification('Sestavuji detailní výjezd — u celého ASM to trvá '
                               'i pár minut…', spinner=True, timeout=None)
        btn_detail.disable()
        try:
            pocet = await asyncio.to_thread(_export_detail_na_disk, asm,
                                            stav['filtr'], cesta)
            _stahni_soubor(cesta, jmeno)
            ui.notify(f'Hotovo — {pocet} řádků.', type='positive')
            await asyncio.to_thread(zapis_log, user_id, user_name, 'export_xlsx_detail',
                                    asm, stav['popis_vse'], pocet)
        except Exception as e:
            ui.notify(f'Export selhal: {e}', type='negative', multi_line=True)
        finally:
            pozn.dismiss()
            btn_detail.enable()

    async def prepni_rozpad():
        # data na obrazovce = jiná granularita než nový stav přepínače → přenačíst
        if stav['rows']:
            await nacti()

    btn_nacti.on_click(_bez_klienta(nacti))
    btn_pdf.on_click(_bez_klienta(export_pdf))
    btn_xlsx.on_click(_bez_klienta(export_xlsx))
    btn_detail.on_click(_bez_klienta(export_detail))
    chk_rozpad.on_value_change(_bez_klienta(prepni_rozpad))


def _kratke_jmeno(ico, zak_volby):
    """Jméno z labelu selectu (`Jméno · Město · IČO`) do hlavičky sloupce."""
    return (zak_volby.get(ico) or ico).split(' · ')[0][:24]


def _vykresli_srovnani(asm, user_id, user_name, vsechna_prava):
    filtr_klic = f'lupa_srovnani_{user_id}_{asm}'
    ulozeny = app.storage.user.get(filtr_klic) or {}
    obdobi = _volby_obdobi(asm)
    zak_volby = _volby_zakazniku(asm)
    dod_volby = _volby_dodavatelu()

    stav = {'rows': [], 'filtr': {}, 'popis': '', 'jm_a': 'A', 'jm_b': 'B'}

    _pruh_importu(asm)

    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_od = _sel_obdobi('Od měsíce', obdobi, ulozeny.get('od'))
        sel_do = _sel_obdobi('Do měsíce', obdobi, ulozeny.get('do'))
        sel_a = ui.select(
            zak_volby, label='Zákazník A', with_input=True, clearable=True,
            value=ulozeny.get('a') if ulozeny.get('a') in zak_volby else None) \
            .props('dense outlined options-dense').style('min-width: 340px') \
            .tooltip('Pište jméno nebo IČO.')
        sel_b = ui.select(
            zak_volby, label='Zákazník B', with_input=True, clearable=True,
            value=ulozeny.get('b') if ulozeny.get('b') in zak_volby else None) \
            .props('dense outlined options-dense').style('min-width: 340px') \
            .tooltip('Pište jméno nebo IČO.')
        sel_dod = ui.select(
            dod_volby, label='Dodavatelé', multiple=True, with_input=True,
            clearable=True,
            value=[d for d in (ulozeny.get('dodavatel') or []) if d in dod_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 280px') \
            .tooltip('Prázdné = všichni dodavatelé.')

    with ui.row().classes('w-full items-center gap-3 mb-3 flex-wrap'):
        btn_nacti = ui.button('Načíst', icon='compare_arrows') \
            .props('unelevated color=indigo-7')
        btn_xlsx = ui.button('XLSX srovnání', icon='table_view') \
            .props('outline color=green-7')
        prepinac = ui.toggle(['Vše', 'Bere jen A', 'Bere jen B', 'Berou oba'],
                             value='Vše') \
            .props('dense unelevated color=indigo-7 no-caps') \
            .tooltip('Filtr produktů, ne zákazníků. „Bere jen B" = bílá místa '
                     'zákazníka A — co bere B a A ne.')
    btn_xlsx.disable()

    vysledek = ui.column().classes('w-full')

    with vysledek:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('compare_arrows', size='4rem', color='grey-3')
            ui.label('Zvolte dva zákazníky a klikněte na Načíst') \
                .classes('text-lg font-semibold text-gray-400')

    def _soucasny_filtr():
        return {'od': sel_od.value, 'do': sel_do.value,
                'a': sel_a.value, 'b': sel_b.value,
                'dodavatel': list(sel_dod.value or [])}

    def _vybrane_radky():
        volba = prepinac.value
        if volba == 'Bere jen A':
            return [r for r in stav['rows'] if r['stav'] == 'jen A']
        if volba == 'Bere jen B':
            return [r for r in stav['rows'] if r['stav'] == 'jen B']
        if volba == 'Berou oba':
            return [r for r in stav['rows'] if r['stav'] == 'oba']
        return stav['rows']

    def vykresli_vysledek():
        rows = _vybrane_radky()
        cols = _cols_srovnani(stav['jm_a'], stav['jm_b'])
        vysledek.clear()
        with vysledek:
            if not rows:
                with ui.column().classes('items-center py-16 gap-3 w-full'):
                    ui.icon('search_off', size='4rem', color='grey-3')
                    ui.label('Filtru nic neodpovídá') \
                        .classes('text-lg font-semibold text-gray-400')
                return
            zobrazene = rows[:_STROP_NAHLED]
            if len(rows) > _STROP_NAHLED:
                ui.label(f'Náhled ukazuje prvních {_STROP_NAHLED} řádků z '
                         f'{len(rows)} — export obsahuje vše.') \
                    .classes('text-xs text-amber-700 mb-1')
            _grid({
                'columnDefs': [
                    {'headerName': cols[0][0], 'field': 'kod', 'width': 130},
                    {'headerName': cols[1][0], 'field': 'nazev', 'flex': 2,
                     'minWidth': 220},
                    {'headerName': cols[2][0], 'field': 'dodavatel', 'flex': 1,
                     'minWidth': 170},
                    {'headerName': cols[3][0], 'field': 'a_mj', 'width': 130,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                    {'headerName': cols[4][0], 'field': 'a_kc', 'width': 160,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(2)},
                    {'headerName': cols[5][0], 'field': 'b_mj', 'width': 130,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                    {'headerName': cols[6][0], 'field': 'b_kc', 'width': 160,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(2)},
                    {'headerName': cols[7][0], 'field': 'rozdil_kc', 'width': 160,
                     'type': 'numericColumn', ':valueFormatter': _fmt_js(2),
                     ':cellStyle': _STYL_ZMENA},
                    {'headerName': cols[8][0], 'field': 'stav', 'width': 100},
                ],
                'rowData': zobrazene,
                'pinnedBottomRowData': [dict(_srovnani_total(rows), _celkem=True)],
                **_GRID_ZAKLAD,
            }).classes('w-full').style('height: calc(100vh - 430px); min-height: 380px')
            ui.label(f"A = {stav['jm_a']} · B = {stav['jm_b']} — kladný rozdíl znamená, "
                     'že A bral víc.').classes('text-xs text-gray-400 mt-1')

    async def nacti():
        if _blokuje_import(asm):
            return
        filtr = _soucasny_filtr()
        if not filtr['a'] or not filtr['b']:
            ui.notify('Vyberte oba zákazníky.', type='warning')
            return
        if filtr['a'] == filtr['b']:
            ui.notify('Zákazník A a B musí být různí.', type='warning')
            return
        app.storage.user[filtr_klic] = filtr
        btn_nacti.disable()
        dlg, _kruh, _popisek = prekryv_kolecko('Generování přehledu', procenta=False)
        dlg.open()
        try:
            rows = await asyncio.to_thread(_srovnani_zakazniku, asm,
                                           filtr['a'], filtr['b'], filtr)
        finally:
            dlg.close()
            btn_nacti.enable()
        stav.update({
            'rows': rows, 'filtr': filtr,
            'jm_a': _kratke_jmeno(filtr['a'], zak_volby),
            'jm_b': _kratke_jmeno(filtr['b'], zak_volby),
            'popis': _popis_filtru(dict(filtr, ico=[filtr['a'], filtr['b']]),
                                   zak_volby)})
        btn_xlsx.set_enabled(bool(rows))
        vykresli_vysledek()
        await asyncio.to_thread(zapis_log, user_id, user_name, 'srovnani_nahled', asm,
                                stav['popis'], len(rows))

    async def export_xlsx():
        rows = _vybrane_radky()
        if not rows:
            return
        jmeno = _zaklad_jmena_srovnani(asm) + '.xlsx'
        cesta = _tmp_cesta(jmeno)
        pozn = ui.notification('Sestavuji XLSX…', spinner=True, timeout=None)
        try:
            cols = _cols_srovnani(stav['jm_a'], stav['jm_b'])
            await asyncio.to_thread(_xlsx_na_disk, cesta, cols, rows,
                                    _srovnani_total(rows), 'Srovnání')
            _stahni_soubor(cesta, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name,
                                    'srovnani_export_xlsx', asm, stav['popis'],
                                    len(rows))
        except Exception as e:
            ui.notify(f'Export selhal: {e}', type='negative', multi_line=True)
        finally:
            pozn.dismiss()

    def prepni(_=None):
        # Filtrujeme už načtená data v paměti — žádný nový dotaz.
        if stav['rows']:
            vykresli_vysledek()

    btn_nacti.on_click(_bez_klienta(nacti))
    btn_xlsx.on_click(_bez_klienta(export_xlsx))
    prepinac.on_value_change(prepni)


def _zaklad_jmena_srovnani(asm):
    return (f'lupa_{_bezpecne_jmeno(asm)}_srovnani_'
            f'{datetime.datetime.now():%Y-%m-%d_%H%M}')


def _graf_zaklad(nadpis, legenda_vpravo=False, podnadpis=''):
    # Podnadpis říká, co je na osách a co je série — bez něj se graf luští.
    posun = 18 if podnadpis else 0
    return {
        'title': {'text': nadpis, 'subtext': podnadpis, 'left': 'center', 'top': 6,
                  'textStyle': {'fontSize': 14},
                  'subtextStyle': {'fontSize': 11, 'color': '#6B7280'}},
        'tooltip': {'trigger': 'axis'},
        'legend': ({'type': 'scroll', 'orient': 'vertical', 'right': 0,
                    'top': 40 + posun, 'bottom': 20} if legenda_vpravo else
                   {'type': 'scroll', 'top': 30 + posun}),
        'grid': {'top': (70 if not legenda_vpravo else 40) + posun, 'left': 80,
                 'right': 220 if legenda_vpravo else 24, 'bottom': 40},
        'yAxis': {'type': 'value'},
    }


def _graf_yoy_option(radky_mesice):
    """Osa X = měsíc 1–12, série = rok. Meziroční srovnání na první pohled."""
    kc = {r['klic']: r['kc'] for r in radky_mesice}
    roky = sorted({k // 100 for k in kc})
    opt = _graf_zaklad(
        'Obrat ASM po měsících (Kč bez DPH, meziroční srovnání)',
        podnadpis='Sloupec = měsíc, barva = rok. Stejný měsíc vedle sebe rok po roce.')
    opt['xAxis'] = {'type': 'category', 'data': _MESICE_KR}
    opt['series'] = [{
        'name': str(rok), 'type': 'bar', 'emphasis': {'focus': 'series'},
        'itemStyle': {'color': _GRAF_PALETA[i % len(_GRAF_PALETA)]},
        'data': [round(kc[rok * 100 + m]) if (rok * 100 + m) in kc else None
                 for m in range(1, 13)],
    } for i, rok in enumerate(roky)]
    return opt


def _graf_rada_option(nadpis, mesice, serie, podnadpis=''):
    """Chronologická spojnice přes zvolené období; série = OZ nebo zákazník."""
    opt = _graf_zaklad(nadpis, legenda_vpravo=True, podnadpis=podnadpis)
    opt['xAxis'] = {'type': 'category', 'data': [_mesic_txt(m) for m in mesice]}
    opt['series'] = [{
        'name': s['name'], 'type': 'line', 'smooth': True, 'connectNulls': True,
        'emphasis': {'focus': 'series'},
        'itemStyle': {'color': _GRAF_PALETA[i % len(_GRAF_PALETA)]},
        'lineStyle': {'color': _GRAF_PALETA[i % len(_GRAF_PALETA)]},
        'data': s['data'],
    } for i, s in enumerate(serie)]
    return opt


def _graf_polozky_option(produkty, n=15):
    """Vodorovné sloupce — názvy produktů se na osu X nevejdou."""
    top = list(reversed(produkty[:n]))
    opt = _graf_zaklad(
        f'TOP {len(top)} položek podle obratu (Kč bez DPH)',
        podnadpis='Nejsilnější produkt nahoře, součet za celé zvolené období.')
    opt['legend'] = {'show': False}
    opt['tooltip'] = {'trigger': 'axis', 'axis': 'shadow'}
    opt['grid'] = {'top': 62, 'left': 240, 'right': 40, 'bottom': 30}
    opt['xAxis'] = {'type': 'value'}
    opt['yAxis'] = {'type': 'category',
                    'data': [(p['nazev'] or p['kod'])[:38] for p in top],
                    'axisLabel': {'fontSize': 10}}
    opt['series'] = [{'type': 'bar', 'itemStyle': {'color': '#4338CA'},
                      'data': [round(float(p['kc'] or 0)) for p in top]}]
    return opt


def _cols_polozky_grid(rozpad):
    """Sloupce TOP položek; v rozpadu přibude skupina zboží z karet (lupa_karta).
    Kopírování se doplňuje i tady — přepínač rozpadu mění columnDefs mimo _grid()."""
    return _kopirovatelne(_filtry([
        {'headerName': 'Kód', 'field': 'kod', 'width': 130},
        {'headerName': 'Produkt', 'field': 'nazev', 'flex': 2, 'minWidth': 240},
    ] + ([{'headerName': 'Skupina zboží', 'field': 'skupina', 'flex': 1,
           'minWidth': 200}] if rozpad else []) + [
        {'headerName': 'Dodavatel', 'field': 'dodavatel', 'flex': 1, 'minWidth': 180},
        {'headerName': 'Zákazníků', 'field': 'zakazniku', 'width': 120,
         'type': 'numericColumn', ':valueFormatter': _fmt_js(0)},
        {'headerName': 'Obrat MJ', 'field': 'mj', 'width': 130,
         'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
        {'headerName': 'Obrat Kč', 'field': 'kc', 'width': 150,
         'type': 'numericColumn', ':valueFormatter': _fmt_js(2)},
    ]))


def _vykresli_obraty(asm, user_id, user_name, vsechna_prava):
    # Filtr sdílíme s odběrateli — je to týž pohled na táž data, jen jinak
    # vykreslený; dvě sady filtrů by uživatel jen držel v synchronizaci ručně.
    filtr_klic = f'lupa_filtr_{user_id}_{asm}'
    prah_klic = f'lupa_propad_prahy_{user_id}'
    ulozeny = app.storage.user.get(filtr_klic) or {}
    prahy = app.storage.user.get(prah_klic) or {}
    obdobi = _volby_obdobi(asm)
    zak_volby = _volby_zakazniku(asm)
    dod_volby = _volby_dodavatelu()
    oz_volby = _volby_oz(asm)

    stav = {'data': None, 'filtr': {}, 'popis': '', 'grid_polozky': None}

    def _zaklad_jmena(co):
        return (f'lupa_{_bezpecne_jmeno(asm)}_{co}_'
                f'{datetime.datetime.now():%Y-%m-%d_%H%M}')

    _pruh_importu(asm)

    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_od = _sel_obdobi('Od měsíce', obdobi, ulozeny.get('od'))
        sel_do = _sel_obdobi('Do měsíce', obdobi, ulozeny.get('do'))
        sel_zak = ui.select(
            zak_volby, label='Zákazníci', multiple=True, with_input=True, clearable=True,
            value=[i for i in (ulozeny.get('ico') or []) if i in zak_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 340px') \
            .tooltip('Prázdné = všichni zákazníci ASM. Pište jméno nebo IČO.')
        _pole_ica(sel_zak, zak_volby)
        sel_oz = ui.select(
            oz_volby, label='OZ', multiple=True, with_input=True, clearable=True,
            value=[o for o in (ulozeny.get('oz') or []) if o in oz_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 240px') \
            .tooltip('Prázdné = všichni OZ. Varianty 635A/635D jsou pod kódem 635.')
        sel_dod = ui.select(
            dod_volby, label='Dodavatelé', multiple=True, with_input=True, clearable=True,
            value=[d for d in (ulozeny.get('dodavatel') or []) if d in dod_volby]) \
            .props('dense outlined options-dense use-chips').style('min-width: 280px') \
            .tooltip('Prázdné = všichni dodavatelé.')
        num_pct = ui.number(label='Propad od %', value=prahy.get('pct', 25),
                            min=0, max=100, step=5, format='%.0f') \
            .props('dense outlined').style('min-width: 120px') \
            .tooltip('Pokles posledního měsíce proti průměru předchozích tří.')
        num_kc = ui.number(label='… a od Kč', value=prahy.get('kc', 10000),
                           min=0, step=1000, format='%.0f') \
            .props('dense outlined').style('min-width: 130px') \
            .tooltip('Filtruje drobné poklesy, které nestojí za telefonát.')

    def _soucasny_filtr():
        return {'od': sel_od.value, 'do': sel_do.value,
                'ico': list(sel_zak.value or []), 'oz': list(sel_oz.value or []),
                'dodavatel': list(sel_dod.value or []),
                'site': tgl_site.value or 'vse'}

    with ui.row().classes('w-full items-center gap-3 mb-3 flex-wrap'):
        btn_nacti = ui.button('Načíst', icon='insights').props('unelevated color=indigo-7')
        tgl_site = ui.toggle({'vse': 'Vše', 'ano': 'Jen sítě', 'ne': 'Bez sítí'},
                             value=ulozeny.get('site') or 'vse') \
            .props('dense no-caps unelevated toggle-color=indigo-7') \
            .tooltip('KPI, řady i propady počítá databáze — přepnutí pustí nový '
                     'dotaz samo, Načíst se nemačká.')
        btn_pdf = ui.button('PDF přehled', icon='picture_as_pdf') \
            .props('outline color=indigo-7') \
            .tooltip('Celý zvolený výběr včetně KPI a grafů — filtr TOP položek '
                     'se do PDF nepromítá.')
        btn_xlsx = ui.button('XLSX (4 listy)', icon='table_view') \
            .props('outline color=green-7') \
            .tooltip('Měsíce, OZ, propady, TOP položky — každý na vlastním listu. '
                     'Filtry a řazení v TOP položkách se do listu promítnou.')
        info = ui.label('').classes('text-sm text-gray-500')

    for b in (btn_pdf, btn_xlsx):
        b.disable()

    vysledek = ui.column().classes('w-full')

    with vysledek:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('query_stats', size='4rem', color='grey-3')
            ui.label('Zvolte filtr a klikněte na Načíst') \
                .classes('text-lg font-semibold text-gray-400')

    def _dlazdice(nadpis, hodnota, podtitul='', barva='indigo'):
        with ui.card().classes(f'flex-1 min-w-[200px] border-l-4 border-{barva}-500 '
                               'shadow-sm rounded-xl'):
            ui.label(nadpis).classes('text-xs uppercase text-gray-500')
            ui.label(hodnota).classes(f'text-2xl font-bold text-{barva}-700')
            if podtitul:
                ui.label(podtitul).classes('text-xs text-gray-500')

    def _zmena_txt(v):
        return '—' if v is None else f'{v:+.1f} %'.replace('.', ',')

    def _zmena_barva(v):
        if v is None:
            return 'gray'
        return 'green' if v >= 0 else 'red'

    def vykresli_vysledek():
        data = stav['data']
        stav['grid_polozky'] = None   # grid z minulého vykreslení už neexistuje
        vysledek.clear()
        with vysledek:
            if not data or not data['mesice']:
                with ui.column().classes('items-center py-16 gap-3 w-full'):
                    ui.icon('search_off', size='4rem', color='grey-3')
                    ui.label('Filtru nic neodpovídá') \
                        .classes('text-lg font-semibold text-gray-400')
                return
            k = data['kpi']
            with ui.row().classes('w-full gap-3 flex-wrap mb-2'):
                _dlazdice('Obrat celkem', _kc(k['celkem_kc']),
                          f"{len(data['mesice'])} měsíců · {k['oz']} OZ")
                _dlazdice('Zákazníků', f"{k['zakazniku']}",
                          's obratem ve zvoleném období', 'cyan')
                _dlazdice(f"Poslední měsíc ({k['posl_mesic']})", _kc(k['posl_kc']),
                          f"meziměsíčně {_zmena_txt(k['mom'])}",
                          _zmena_barva(k['mom']))
                _dlazdice('Meziročně', _zmena_txt(k['yoy']),
                          f"{k['posl_mesic']} proti stejnému měsíci loni",
                          _zmena_barva(k['yoy']))
            ui.label('Poslední měsíc v datech může být neúplný, pokud import '
                     'proběhl v jeho průběhu — meziměsíční srovnání pak podhodnocuje.') \
                .classes('text-xs text-gray-400 italic mb-2')

            with ui.card().classes('w-full shadow-sm rounded-xl mb-3'):
                ui.echart(_graf_yoy_option(data['radky_mesice'])) \
                    .classes('w-full').style('height: 340px')

            propady = data['propady']
            with ui.card().classes('w-full shadow-sm rounded-xl mb-3 '
                                   + ('border-l-4 border-red-500' if propady else '')):
                with ui.row().classes('items-center gap-2 mb-1'):
                    ui.icon('warning' if propady else 'check_circle',
                            color='red-6' if propady else 'green-6')
                    ui.label(f'Propady zákazníků — {len(propady)} nad prahem'
                             if propady else 'Propady zákazníků — nic nad prahem') \
                        .classes('text-lg font-semibold text-gray-800')
                ui.label(f'Poslední měsíc {k["posl_mesic"]} proti průměru '
                         f'předchozích {_PROPAD_OKNO} měsíců.') \
                    .classes('text-xs text-gray-500 mb-2')
                if propady:
                    _grid({
                        'columnDefs': [
                            {'headerName': 'IČO', 'field': 'ico', 'width': 120},
                            {'headerName': 'Zákazník', 'field': 'jmeno', 'flex': 2,
                             'minWidth': 220},
                            {'headerName': 'Průměr předchozích Kč', 'field': 'zaklad',
                             'width': 180, 'type': 'numericColumn',
                             ':valueFormatter': _fmt_js(0)},
                            {'headerName': 'Poslední měsíc Kč', 'field': 'akt',
                             'width': 160, 'type': 'numericColumn',
                             ':valueFormatter': _fmt_js(0)},
                            {'headerName': 'Rozdíl Kč', 'field': 'rozdil', 'width': 150,
                             'type': 'numericColumn', ':valueFormatter': _fmt_js(0)},
                            {'headerName': 'Změna %', 'field': 'pct', 'width': 120,
                             'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                            {'headerName': 'Stav', 'field': 'stav', 'width': 120},
                        ],
                        'rowData': propady[:_STROP_NAHLED],
                        **_GRID_ZAKLAD,
                    }).classes('w-full').style('height: 320px')

            with ui.card().classes('w-full shadow-sm rounded-xl mb-3'):
                ui.echart(_graf_rada_option(
                    'Obrat OZ v čase (Kč bez DPH)', data['mesice'],
                    [{'name': d['dealer_jmeno'],
                      'data': [round(d['rada'].get(m, 0.0), 2) for m in data['mesice']]}
                     for d in data['oz']],
                    podnadpis='Čára = jeden obchodní zástupce, měsíc po měsíci '
                              'za zvolené období.')).classes('w-full').style('height: 360px')
                _grid({
                    'columnDefs': [
                        {'headerName': 'OZ', 'field': 'dealer', 'width': 110},
                        {'headerName': 'Jméno', 'field': 'dealer_jmeno', 'flex': 1,
                         'minWidth': 200},
                        {'headerName': 'Obrat MJ', 'field': 'mj', 'width': 140,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                        {'headerName': 'Obrat Kč', 'field': 'kc', 'width': 160,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(2)},
                        {'headerName': 'Podíl %', 'field': 'podil', 'width': 110,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(1)},
                        {'headerName': 'Zákazníků', 'field': 'zakazniku', 'width': 120,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(0)},
                        {'headerName': 'Položek', 'field': 'polozek', 'width': 110,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(0)},
                        {'headerName': 'Průměr / měsíc Kč', 'field': 'prumer_kc',
                         'width': 150, 'type': 'numericColumn',
                         ':valueFormatter': _fmt_js(2)},
                        {'headerName': 'Poslední měsíc Kč', 'field': 'posl_kc',
                         'width': 150, 'type': 'numericColumn',
                         ':valueFormatter': _fmt_js(2)},
                        {'headerName': 'MoM %', 'field': 'mom', 'width': 110,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(1),
                         ':cellStyle': _STYL_ZMENA},
                        {'headerName': 'YoY %', 'field': 'yoy', 'width': 110,
                         'type': 'numericColumn', ':valueFormatter': _fmt_js(1),
                         ':cellStyle': _STYL_ZMENA},
                    ],
                    'rowData': data['oz'],
                    'pinnedBottomRowData': [{
                        'dealer_jmeno': f"CELKEM {len(data['oz'])} OZ",
                        'mj': k['celkem_mj'], 'kc': k['celkem_kc'], 'podil': 100.0,
                        'zakazniku': k['zakazniku'], 'posl_kc': k['posl_kc'],
                        'mom': k['mom'], 'yoy': k['yoy']}],
                    **_GRID_ZAKLAD,
                }).classes('w-full mt-2').style('height: 300px')

            with ui.card().classes('w-full shadow-sm rounded-xl mb-3'):
                ui.echart(_graf_rada_option(
                    f'Vývoj obratů — TOP {len(data["zak_serie"])} zákazníků',
                    data['mesice'], data['zak_serie'],
                    podnadpis='Čára = jeden zákazník s největším obratem, '
                              'měsíc po měsíci. Klik v legendě čáru skryje.')) \
                    .classes('w-full').style('height: 380px')

            with ui.card().classes('w-full shadow-sm rounded-xl'):
                ui.echart(_graf_polozky_option(data['produkty'])) \
                    .classes('w-full').style('height: 420px')
                with ui.row().classes('w-full justify-end mt-2'):
                    btn_rozpad = ui.button(on_click=lambda: _prepni_rozpad()) \
                        .props('outline dense color=indigo-7')
                grid_polozky = _grid({
                    'columnDefs': _cols_polozky_grid(False),
                    'rowData': data['produkty'],
                    **_GRID_ZAKLAD,
                }, filtry=True).classes('w-full mt-2').style('height: 320px')
                stav['grid_polozky'] = grid_polozky

                def _prepni_rozpad():
                    stav['rozpad'] = not stav.get('rozpad')
                    grid_polozky.options['columnDefs'] = \
                        _cols_polozky_grid(stav['rozpad'])
                    grid_polozky.update()
                    _popis_rozpadu()

                def _popis_rozpadu():
                    zap = bool(stav.get('rozpad'))
                    btn_rozpad.text = ('Skrýt skupiny zboží' if zap
                                       else 'Rozpad na skupiny zboží')
                    btn_rozpad.props(f'icon={"visibility_off" if zap else "category"}')

                _popis_rozpadu()

    async def nacti():
        if _blokuje_import(asm):
            return
        filtr = _soucasny_filtr()
        prah_pct = float(num_pct.value or 0)
        prah_kc = float(num_kc.value or 0)
        app.storage.user[filtr_klic] = filtr
        app.storage.user[prah_klic] = {'pct': prah_pct, 'kc': prah_kc}
        btn_nacti.disable()
        dlg, _kruh, _popisek = prekryv_kolecko('Generování přehledu', procenta=False)
        dlg.open()
        try:
            data = await asyncio.to_thread(_obraty_prehled, asm, filtr,
                                           prah_pct, prah_kc)
        finally:
            dlg.close()
            btn_nacti.enable()
        stav.update({'data': data, 'filtr': filtr,
                     'popis': _popis_filtru(filtr, zak_volby)})
        k = data['kpi']
        info.set_text(f"{_kc(k['celkem_kc'])} · {k['zakazniku']} zákazníků · "
                      f"{len(data['propady'])} propadů")
        for b in (btn_pdf, btn_xlsx):
            b.set_enabled(bool(data['mesice']))
        vykresli_vysledek()
        if data['propady']:
            ui.notify(f"{len(data['propady'])} zákazníků propadlo v měsíci "
                      f"{k['posl_mesic']} — viz červená karta.",
                      type='warning', multi_line=True, timeout=8000)
        await asyncio.to_thread(zapis_log, user_id, user_name, 'obraty_nahled', asm,
                                stav['popis'], len(data['mesice']))

    async def _polozky_k_exportu():
        """TOP položky tak, jak je uživatel vidí — s filtry i řazením z gridu.
        Klient neodpoví (odešel, grid se nevykreslil) → plná sada; export kvůli
        tomu padat nesmí."""
        grid = stav.get('grid_polozky')
        if grid is None:
            return stav['data']['produkty']
        try:
            return await grid.get_client_data(timeout=5, method='filtered_sorted')
        except Exception:
            return stav['data']['produkty']

    async def export_pdf():
        data = stav['data']
        jmeno = _zaklad_jmena('obraty') + '.pdf'
        pozn = ui.notification('Připravuji PDF…', spinner=True, timeout=None)
        try:
            html_text = _pdf_obraty_html(asm, data, stav['popis'], user_name,
                                         bool(stav.get('rozpad')))
            bajty = await _render_pdf(html_text, jmeno, pozn)
            _stahni_bajty(bajty, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'obraty_export_pdf',
                                    asm, stav['popis'], len(data['propady']))
        except Exception as e:
            ui.notify(f'PDF se nepodařilo vytvořit: {e}', type='negative',
                      multi_line=True, timeout=15000)
        finally:
            pozn.dismiss()

    async def export_xlsx():
        data = stav['data']
        jmeno = _zaklad_jmena('obraty') + '.xlsx'
        cesta = _tmp_cesta(jmeno)
        pozn = ui.notification('Sestavuji XLSX…', spinner=True, timeout=None)
        try:
            k = data['kpi']
            listy = [
                ('Měsíce', _COLS_MESICE, data['radky_mesice'],
                 {'mesic': 'CELKEM', 'mj': k['celkem_mj'], 'kc': k['celkem_kc']}),
                ('OZ', _COLS_OZ, data['oz'],
                 {'dealer_jmeno': f"CELKEM {len(data['oz'])} OZ",
                  'mj': k['celkem_mj'], 'kc': k['celkem_kc'], 'podil': 100.0,
                  'posl_kc': k['posl_kc'], 'mom': k['mom'], 'yoy': k['yoy']}),
                ('Propady', _COLS_PROPADY, data['propady'],
                 {'jmeno': f"CELKEM {len(data['propady'])} zákazníků",
                  'rozdil': sum(p['rozdil'] for p in data['propady'])}),
                ('TOP položky', _cols_polozky(stav.get('rozpad')),
                 await _polozky_k_exportu(), None),
            ]
            await asyncio.to_thread(_xlsx_listy_na_disk, cesta, listy)
            _stahni_soubor(cesta, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'obraty_export_xlsx',
                                    asm, stav['popis'], len(data['radky_mesice']))
        except Exception as e:
            ui.notify(f'Export selhal: {e}', type='negative', multi_line=True)
        finally:
            pozn.dismiss()

    async def prepni_site():
        # KPI, řady i propady počítá SQL → jiný okruh zákazníků = nutný nový
        # dotaz. Dokud se nic nenačetlo, není co překreslovat.
        if stav['data']:
            await nacti()

    btn_nacti.on_click(_bez_klienta(nacti))
    btn_pdf.on_click(_bez_klienta(export_pdf))
    btn_xlsx.on_click(_bez_klienta(export_xlsx))
    tgl_site.on_value_change(_bez_klienta(prepni_site))


def _zamek_asm(klic):
    """Jedno ASM = jeden import naráz. Zámků je tolik co ASM, tj. desítky."""
    zamek = _ASM_ZAMKY.get(klic)
    if zamek is None:
        zamek = _ASM_ZAMKY[klic] = asyncio.Lock()   # event loop → bez race
    return zamek


def bezi_import(asm=None):
    """Běží import daného ASM (nebo jakýkoli)? Čte se z každé relace."""
    if not _BEZICI_IMPORTY:
        return False
    if asm is None:
        return True
    hledany = (asm or '').upper()
    return any((s.get('asm') or '').upper() == hledany
               for s in list(_BEZICI_IMPORTY.values()))


def _blokuje_import(asm):
    """DELETE+INSERT má data rozpůlená — dotazy během něj nepouštíme."""
    if not bezi_import(asm):
        return False
    ui.notify('Probíhá import dat, zkuste to později.', type='warning')
    return True


def _pruh_importu(asm):
    """Hláška nad filtrem, dokud běží import daného ASM."""
    pruh = ui.row().classes('w-full items-center gap-2 mb-3 px-3 py-2 rounded-lg '
                            'bg-amber-50 border border-amber-200')
    with pruh:
        ui.spinner(size='sm', color='amber-8')
        ui.label('Probíhá import dat, zkuste to později.') \
            .classes('text-sm font-semibold text-amber-800')
    pruh.set_visibility(bezi_import(asm))
    ui.timer(1.5, lambda: pruh.set_visibility(bezi_import(asm)))


def _tise(fn):
    """UI update po odchodu ze stránky nesmí shodit import běžící na pozadí."""
    try:
        fn()
    except Exception:
        pass


def _vykresli_import(user_id, user_name, vsechna_prava):
    import intranet_asm

    ui.label('Vyberte jeden nebo více souborů „data prodeje zak3 – *.xlsx". '
             f'Zpracují se paralelně (až {_IMPORT_SOUBEZNE} naráz), '
             'jeden soubor = jedno ASM.') \
        .classes('text-gray-600 mb-2')
    ui.label('Reimport stejného měsíce data přepíše, nový měsíc se přidá.') \
        .classes('text-xs text-gray-400 mb-4')

    protokol = ui.column().classes('w-full max-w-2xl gap-2 mt-4')

    def slot_prubehu(stav, cizi=False, kam=None):
        """Řádek s progress barem. Timer jen čte stav, který plní import thread."""
        with kam or protokol:
            radek_ui = ui.column().classes('w-full gap-1')
        with radek_ui:
            with ui.row().classes('w-full items-center justify-between gap-3'):
                ui.label(stav['nazev']).classes('text-sm text-gray-700 truncate')
                popis = ui.label('ve frontě…').classes('text-xs text-gray-500 '
                                                       'whitespace-nowrap')
            bar = ui.linear_progress(value=0, show_value=False, size='8px') \
                .props('rounded instant-feedback color=indigo-7')

        def tik():
            if cizi and id(stav) not in _BEZICI_IMPORTY:
                # Import z jiné relace doběhl — výsledek zná jeho vlastní záložka.
                radek_ui.clear()
                with radek_ui:
                    ui.label(f"✅ {stav['nazev']} — import na pozadí dokončen") \
                        .classes('text-sm text-green-700')
                timer.cancel()
                return
            celkem, hotovo = stav['celkem'], stav['hotovo']
            if celkem:
                # Poslední fáze (číselníky, souhrn) běží po dočtení řádků — bar
                # by stál na 100 % a tvářil se zaseknutě. Držíme ho pod stropem.
                bar.value = min(hotovo / celkem, 1.0) * 0.95
                popis.text = f"{stav['faze']} · {bar.value:.0%}"
            else:
                bar.props('indeterminate')
                popis.text = f"{stav['faze']}… {hotovo:,} řádků".replace(',', ' ')

        timer = ui.timer(0.3, tik)
        return radek_ui, timer

    # Import doběhne i po odchodu ze stránky — po návratu ukážeme, co jede dál.
    for bezici in list(_BEZICI_IMPORTY.values()):
        if bezici.get('asm') != NAKUP_KLIC:      # GIST má vlastní protokol u svého uploadu
            slot_prubehu(bezici, cizi=True)

    async def zpracuj(e):
        # reset=False: reset() abortuje ostatní běžící uploady z téže dávky
        raw, nazev = await intranet_asm._precti_upload(e, up, reset=False)
        if raw is None:
            return
        # Paralelně, ale nejvýš _IMPORT_SOUBEZNE naráz a jedno ASM jen jednou —
        # platí napříč všemi klienty, lokální příznak by hlídal jen jednu záložku.
        klic = klic_ze_souboru(nazev) or nazev
        zamek = _zamek_asm(klic)
        stav = {'faze': 've frontě', 'hotovo': 0, 'celkem': None,
                'nazev': nazev, 'asm': klic}
        _BEZICI_IMPORTY[id(stav)] = stav   # zámek filtrů + stopa pro ostatní relace
        radek_ui, timer = slot_prubehu(stav)
        try:
            async with _IMPORT_SEM, zamek:
                vysledek = await asyncio.to_thread(importuj_soubor, raw, nazev, stav)
        finally:
            _BEZICI_IMPORTY.pop(id(stav), None)
            _tise(timer.cancel)
            _tise(radek_ui.clear)   # bar dosloužil, slot souboru zůstává na místě

        # Od téhle chvíle jen UI — klient mezitím mohl odejít, chyby polykáme.
        if vysledek['chyba']:
            def selhalo():
                with radek_ui:
                    ui.label(f"❌ {nazev}: {vysledek['chyba']}") \
                        .classes('text-sm text-red-600')
                ui.notify(f"{nazev}: {vysledek['chyba']}", type='negative')
            _tise(selhalo)
            return
        zapis_log(user_id, user_name, 'import', vysledek['asm'],
                  f"{nazev} · období {', '.join(vysledek['obdobi'])}",
                  vysledek['radku'])
        await asyncio.to_thread(_ohlas_nesparovane)

        def hotovo():
            with radek_ui:
                ui.label(f"✅ {nazev} → ASM {vysledek['asm']}, "
                         f"{vysledek['radku']:,} řádků, období "
                         f"{', '.join(vysledek['obdobi'])}"
                         .replace(',', ' ')).classes('text-sm text-green-700')
            # Překreslit smí jen karta níž. Refresh celé záložky by zrušil
            # ui.upload a s ním i soubory, které se ještě nahrávají
            # (ClientDisconnect), plus by smazal protokol dokončených importů.
            karta_nesparovane.refresh()
        _tise(hotovo)

    def odmitnuto(e):
        # Bez toho zmizí soubor odmítnutý klientem (velikost, přípona) beze slova.
        ui.notify(f'Soubor odmítnut (velikost / typ): {len(e.args or [])} ks — nahrajte znovu.',
                  type='negative', multi_line=True)

    up = ui.upload(on_upload=zpracuj, on_rejected=odmitnuto, auto_upload=True, multiple=True,
                   max_file_size=120_000_000, label='Vybrat .xlsx (lze více najednou)') \
        .props('accept=.xlsx multiple').classes('w-full max-w-2xl')

    ui.separator().classes('my-6 max-w-2xl')
    ui.label('Karty zboží (číselník skupin)').classes('font-bold text-gray-700')
    ui.label('Soubor „Karty k DD.MM.RRRR.xlsb" — hlavička na 5. řádku, data od 6. '
             'Naplní skupiny zboží pro rozpad v Lupni obraty. '
             'Nový soubor karty přepíše, nic nemaže.') \
        .classes('text-xs text-gray-400 mb-2')

    async def zpracuj_karty(e):
        raw, nazev = await intranet_asm._precti_upload(e, up_karty, reset=False)
        if raw is None:
            return
        stav = {'faze': 've frontě', 'hotovo': 0, 'celkem': None,
                'nazev': nazev, 'asm': _KARTY_KLIC}
        _BEZICI_IMPORTY[id(stav)] = stav
        radek_ui, timer = slot_prubehu(stav)
        try:
            async with _IMPORT_SEM, _zamek_asm(_KARTY_KLIC):
                vysledek = await asyncio.to_thread(importuj_karty, raw, nazev, stav)
        finally:
            _BEZICI_IMPORTY.pop(id(stav), None)
            _tise(timer.cancel)
            _tise(radek_ui.clear)

        if vysledek['chyba']:
            def selhalo():
                with radek_ui:
                    ui.label(f"❌ {nazev}: {vysledek['chyba']}") \
                        .classes('text-sm text-red-600')
                ui.notify(f"{nazev}: {vysledek['chyba']}", type='negative')
            _tise(selhalo)
            return
        zapis_log(user_id, user_name, 'import_karty', '', nazev, vysledek['radku'])

        def hotovo():
            with radek_ui:
                ui.label(f"✅ {nazev} → {vysledek['radku']:,} karet zboží"
                         .replace(',', ' ')).classes('text-sm text-green-700')
        _tise(hotovo)

    up_karty = ui.upload(on_upload=zpracuj_karty, on_rejected=odmitnuto,
                         auto_upload=True, max_file_size=120_000_000,
                         label='Vybrat .xlsb s kartami zboží') \
        .props('accept=.xlsb').classes('w-full max-w-2xl')

    # ── Lupou na nákup: GIST odběrů — jeden soubor, přepíše jen měsíce, které obsahuje
    ui.separator().classes('my-6 max-w-2xl')
    ui.label('🛒 Lupou na nákup — data GIST').classes('font-bold text-gray-700')
    ui.label('Soubor „GIST_Lupou na nákup.xlsx“ (Divize, Pobočka, Rok, Měsíc - číslo, IČO, '
             'Jméno, Produkt - Kód, Produkt - název, Obrat v MJ, Obrat v Kč bez DPH). '
             'Měsíce obsažené v souboru se přepíšou, ostatní zůstanou. Zkratka nákupčího '
             '(sloupec S „Nak“) a dodavatel se dopárují podle kódu produktu z DATA_POROVNANI '
             'nahraných v Cenopřípadu.') \
        .classes('text-xs text-gray-400 mb-2')

    async def zpracuj_nakup(e):
        raw, nazev = await intranet_asm._precti_upload(e, up_nakup, reset=False)
        if raw is None:
            return
        stav = {'faze': 've frontě', 'hotovo': 0, 'celkem': None,
                'nazev': nazev, 'asm': NAKUP_KLIC}
        _BEZICI_IMPORTY[id(stav)] = stav
        radek_ui, timer = slot_prubehu(stav, kam=protokol_nakup)
        try:
            async with _IMPORT_SEM, _zamek_asm(NAKUP_KLIC):
                vysledek = await asyncio.to_thread(importuj_nakup, raw, nazev, stav, user_name)
        finally:
            _BEZICI_IMPORTY.pop(id(stav), None)
            _tise(timer.cancel)
            _tise(radek_ui.clear)

        if vysledek['chyba']:
            def selhalo():
                with radek_ui:
                    ui.label(f"❌ {nazev}: {vysledek['chyba']}") \
                        .classes('text-sm text-red-600')
                ui.notify(f"{nazev}: {vysledek['chyba']}", type='negative', multi_line=True)
            _tise(selhalo)
            return
        zapis_log(user_id, user_name, 'nakup_import', '',
                  f"{nazev} · {', '.join(vysledek['obdobi'])}", vysledek['radku'])

        def hotovo():
            with radek_ui:
                ui.label(f"✅ {nazev} → {_nakup_cele_txt(vysledek['radku'])} řádků · "
                         f"{_nakup_obdobi_souhrn(vysledek['obdobi'])}") \
                    .classes('text-sm text-green-700')
                if vysledek['varovani']:
                    ui.label(f"⚠️ {vysledek['varovani']}").classes('text-xs text-amber-700')
            karta_nakup.refresh()
        _tise(hotovo)

    # Průběh hned u uploadu — sdílený protokol nahoře je u GIST mimo obrazovku.
    protokol_nakup = ui.column().classes('w-full max-w-2xl gap-2 mb-2')
    for bezici in list(_BEZICI_IMPORTY.values()):
        if bezici.get('asm') == NAKUP_KLIC:
            slot_prubehu(bezici, cizi=True, kam=protokol_nakup)

    up_nakup = ui.upload(on_upload=zpracuj_nakup, on_rejected=odmitnuto, auto_upload=True,
                         max_file_size=120_000_000, label='Vybrat GIST .xlsx (Lupou na nákup)') \
        .props('accept=.xlsx').classes('w-full max-w-2xl')

    @ui.refreshable
    def karta_nakup():
        _karta_nakup_stav(user_id, user_name, vsechna_prava, karta_nakup)

    karta_nakup()

    @ui.refreshable
    def karta_nesparovane():
        nesparovane = nesparovana_asm()
        if not nesparovane or not je_admin(vsechna_prava):
            return
        with ui.card().classes('w-full max-w-2xl mt-6 p-4 bg-amber-50 border '
                               'border-amber-200 rounded-xl'):
            ui.label('⚠️ ASM bez spárovaného uživatele').classes('font-bold text-amber-800')
            ui.label(', '.join(nesparovane)).classes('text-sm text-amber-700')
            ui.label('Data se nahrála, ale dlaždici zatím vidí jen admin a extra čtenář. '
                     'Zkontrolujte příjmení uživatele ve Správě uživatelů.') \
                .classes('text-xs text-amber-600 mt-1')

    karta_nesparovane()


def _ohlas_nesparovane():
    nesparovane = nesparovana_asm()
    if not nesparovane:
        return
    zprava = ('⚠️ Lupou na obchod: bez spárovaného uživatele zůstávají ASM '
              + ', '.join(nesparovane))
    for uid in _adminska_id():
        try:
            intranet_notifikace.pridej(uid, zprava, 'warning')
        except Exception:
            pass


# ============================================================================
# Lupou na nákup — odběry z GIST po dodavatelích a nákupčích
# ============================================================================
# GIST nemá dodavatele ani nákupčího. Obojí se dopárovává podle kódu produktu
# z DATA_POROVNANI, která už nahrává Cenopřípad (cenopripad_master: sloupec S
# „Nak“, K/L dodavatel). Výsledek drží lupa_nakup_produkt — poslední známé
# zařazení zůstane i zboží, které z DATA_POROVNANI mezitím vypadlo.
#
# Práva: zkratka nákupčího = právo lupa_nakup_<zkratka> (intranet_prava,
# podsekce „Oprávnění zobrazení“). Nákupčí vidí jen řádky zboží svých zkratek,
# a tedy jen „své“ dodavatele — dodavatel s víc nákupčími se každému ukáže jen
# svou částí. Správce (lupa_nakup_spravce), admin modulu a „vse“ vidí vše
# včetně nespárovaného zboží.

NAKUP_POHLED = '#nakup'              # pohled v app.storage — slug ASM „#“ nemá
NAKUP_KLIC = '#NAKUP'                # pseudo-ASM pro zámek a protokol importu
NAKUP_PREFIX = 'lupa_nakup_'
NAKUP_SPRAVCE = 'lupa_nakup_spravce'
_BEZ = '__bez__'                     # volba „(nespárováno)“ u dodavatele / nákupčího
_NAKUP_ZAMEK = threading.Lock()      # import a dopárování se nesmí potkat
_NAKUP_PAROVANI = {'import_id': None}    # nahrání DATA_POROVNANI, se kterým se párovalo
_NAKUP_PDF_RADKU = 300               # řádků srovnání do PDF / tisku, zbytek je v Excelu
_NAKUP_JOIN = 'FROM lupa_nakup_obrat o LEFT JOIN lupa_nakup_produkt p ON p.kod = o.kod'

_NAKUP_SLOUPCE = {
    'divize':             'divize',
    'pobocka':            'pobocka',
    'rok':                'rok',
    'mesic cislo':        'mesic',
    'mesic':              'mesic',
    'ico':                'ico',
    'jmeno':              'jmeno',
    'produkt kod':        'kod',
    'produkt nazev':      'nazev',
    'obrat v mj':         'obrat_mj',
    'obrat v kc bez dph': 'obrat_kc',
}
# Rok + Pobočka odliší GIST nákupu od prodejů ASM (ty mají „Měsíc“ 2026M08 a ASM).
_NAKUP_POVINNE = ('pobocka', 'rok', 'mesic', 'ico', 'kod', 'obrat_kc')

# Výjezd: řádek = kombinace zvolených údajů (všechny = zrnitost GIST).
_NAKUP_DIMENZE = (
    ('mesic', 'Měsíc'),
    ('divize', 'Divize'),
    ('pobocka', 'Pobočka'),
    ('odberatel', 'Odběratel (IČO, jméno)'),
    ('produkt', 'Produkt (kód, název)'),
    ('dodavatel', 'Dodavatel'),
    ('nak', 'Nákupčí'),
)
_NAKUP_DIMENZE_VYCHOZI = ['mesic', 'divize', 'pobocka', 'odberatel', 'produkt']

# Srovnání: klíč → (popisek, 2. pád mn. č., výraz klíče, výraz názvu,
#                   filtr pro proklik, rozpad v prokliku)
_NAKUP_ROZPADY = {
    'dodavatel': ('Dodavatel', 'dodavatelů', "COALESCE(p.dodavatel, '')",
                  'MAX(p.dodavatel_nazev)', 'dodavatel', 'produkt'),
    'nak':       ('Nákupčí', 'nákupčích', "COALESCE(p.nak, '')", None, 'nak', 'dodavatel'),
    'divize':    ('Divize', 'divizí', 'o.divize', None, 'divize', 'dodavatel'),
    'pobocka':   ('Pobočka', 'poboček', 'o.pobocka', None, 'pobocka', 'dodavatel'),
    'odberatel': ('Odběratel', 'odběratelů', 'o.ico', 'MAX(o.jmeno)', 'ico', 'dodavatel'),
    'produkt':   ('Produkt', 'produktů', 'o.kod', 'MAX(o.nazev)', 'kody', 'pobocka'),
}
_NAKUP_COLS_MESICE = [('Měsíc', 'mesic', 'text', 10), ('Odběry Kč bez DPH', 'kc', 'money', 18)]


# ── Práva ───────────────────────────────────────────────────────────────────
def nakup_kody(prava):
    """Zkratky nákupčích, jejichž zboží uživatel vidí. None = vše vč. nespárovaného."""
    prava = prava or ()
    if 'vse' in prava or 'lupa_admin' in prava or NAKUP_SPRAVCE in prava:
        return None
    return {p[len(NAKUP_PREFIX):].upper() for p in prava
            if isinstance(p, str) and p.startswith(NAKUP_PREFIX) and p != NAKUP_SPRAVCE}


def ma_pristup_nakup(prava):
    kody = nakup_kody(prava)
    return kody is None or bool(kody)


# ── Import GIST ─────────────────────────────────────────────────────────────
def _nakup_hlavicka(radek):
    mapa = {}
    for i, bunka in enumerate(radek):
        klic = _NAKUP_SLOUPCE.get(_norm_hlavicka(bunka))
        if klic and klic not in mapa:
            mapa[klic] = i
    return mapa if all(k in mapa for k in _NAKUP_POVINNE) else None


def _nakup_bunka(radek, i):
    return radek[i] if i is not None and i < len(radek) else None


def _nakup_text(hodnota, delka):
    """Text z buňky; číslo bez desetin bez „.0“ (Excel z kódu udělá float)."""
    if hodnota is None:
        return ''
    if isinstance(hodnota, float) and hodnota.is_integer():
        hodnota = int(hodnota)
    return str(hodnota).strip()[:delka]


def _nakup_cele(hodnota):
    """'2026' / 2026 / 2026.0 → 2026; nečíslo → None."""
    try:
        return int(float(str(hodnota).strip().replace(',', '.')))
    except (TypeError, ValueError, OverflowError):
        return None


def _nakup_uloz(cur, davka):
    cur.executemany(
        'INSERT INTO lupa_nakup_obrat (rok, mesic, divize, pobocka, ico, jmeno, kod, nazev, '
        'obrat_mj, obrat_kc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', davka)


def importuj_nakup(raw_bytes, nazev_souboru, stav=None, uzivatel=''):
    """GIST „Lupou na nákup“ → lupa_nakup_obrat.

    Měsíce obsažené v souboru se nahradí celé, ostatní zůstanou. Jedna transakce —
    rozbitý soubor nenechá napůl smazaný měsíc. Po commitu dopárování z
    DATA_POROVNANI; když chybí, import tím nepadá, jen se to ohlásí."""
    vysledek = {'soubor': nazev_souboru, 'radku': 0, 'preskoceno': 0, 'obdobi': [],
                'chyba': None, 'varovani': None}
    stav = stav if stav is not None else {}
    inicializace_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        vysledek['chyba'] = 'Není spojení s databází.'
        return vysledek
    cur = None
    try:
        with _NAKUP_ZAMEK:
            cur = conn.cursor()
            stav['faze'] = 'načítám řádky'
            mapa, ix, smazane, kody, davka = None, None, set(), set(), []
            for i, radek in enumerate(_radky_xlsx(raw_bytes, stav), 1):
                stav['hotovo'] = i
                if mapa is None:
                    mapa = _nakup_hlavicka(radek)
                    if mapa is not None:
                        ix = {k: mapa.get(k) for k in set(_NAKUP_SLOUPCE.values())}
                    elif i >= 50:
                        break
                    continue
                rok = _nakup_cele(_nakup_bunka(radek, ix['rok']))
                mes = _nakup_cele(_nakup_bunka(radek, ix['mesic']))
                kod = _nakup_text(_nakup_bunka(radek, ix['kod']), 40)
                if not kod or not rok or not mes or not (2000 <= rok <= 2100 and 1 <= mes <= 12):
                    if any(v not in (None, '') for v in radek):
                        vysledek['preskoceno'] += 1      # „Celkový součet“, patičky
                    continue
                if (rok, mes) not in smazane:
                    cur.execute('DELETE FROM lupa_nakup_obrat WHERE rok=%s AND mesic=%s',
                                (rok, mes))
                    smazane.add((rok, mes))
                davka.append((rok, mes,
                              _nakup_text(_nakup_bunka(radek, ix['divize']), 60),
                              _nakup_text(_nakup_bunka(radek, ix['pobocka']), 100),
                              _nakup_text(_nakup_bunka(radek, ix['ico']), 20),
                              _nakup_text(_nakup_bunka(radek, ix['jmeno']), 255),
                              kod,
                              _nakup_text(_nakup_bunka(radek, ix['nazev']), 255),
                              _cislo(_nakup_bunka(radek, ix['obrat_mj'])),
                              _cislo(_nakup_bunka(radek, ix['obrat_kc']))))
                kody.add(kod)
                if len(davka) >= _DAVKA:
                    _nakup_uloz(cur, davka)
                    vysledek['radku'] += len(davka)
                    davka = []

            if mapa is None:
                conn.rollback()
                vysledek['chyba'] = ('Nenalezena hlavička. Očekávám sloupce Divize, Pobočka, '
                                     'Rok, Měsíc - číslo, IČO, Jméno, Produkt - Kód, '
                                     'Produkt - název, Obrat v MJ, Obrat v Kč bez DPH.')
                return vysledek
            if davka:
                _nakup_uloz(cur, davka)
                vysledek['radku'] += len(davka)
            if not vysledek['radku']:
                conn.rollback()
                vysledek['chyba'] = 'Soubor neobsahuje žádné řádky s daty.'
                return vysledek

            stav['faze'] = 'ukládám číselník produktů'
            serazene = sorted(kody)
            for j in range(0, len(serazene), _DAVKA):
                cur.executemany('INSERT IGNORE INTO lupa_nakup_produkt (kod) VALUES (%s)',
                                [(k,) for k in serazene[j:j + _DAVKA]])
            vysledek['obdobi'] = [f'{r}M{m:02d}' for r, m in sorted(smazane)]
            cur.execute('INSERT INTO lupa_nakup_import (uzivatel, soubor, radku, obdobi) '
                        'VALUES (%s,%s,%s,%s)',
                        (str(uzivatel or '')[:200], str(nazev_souboru or '')[:255],
                         vysledek['radku'], ', '.join(vysledek['obdobi'])[:1000]))
            conn.commit()

            stav['faze'] = 'páruji s DATA_POROVNANI'
            par = _nakup_doparuj(conn)
            vysledek['varovani'] = par.get('chyba') or par.get('varovani')
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        vysledek['chyba'] = str(e)
        print(f'[lupa] import nákupu {nazev_souboru}: {e}')
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
    return vysledek


# ── Dopárování z DATA_POROVNANI ─────────────────────────────────────────────
def _nakup_doparuj(conn):
    """Zkratka nákupčího + dodavatel (+ popis sortimentu) z cenopripad_master
    do lupa_nakup_produkt.

    COALESCE drží poslední známou hodnotu: zboží, které z DATA_POROVNANI vypadlo
    (nebo tam má prázdnou buňku), nepřijde o zařazení. Volá se pod _NAKUP_ZAMEK."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='cenopripad_master'")
        sloupce = {str(r[0]).lower() for r in cur.fetchall()}
        if not {'kod', 'nak'} <= sloupce:
            return {'chyba': 'DATA_POROVNANI nejsou v systému — nahrajte je v modulu '
                             'Cenopřípad (Import dat). Zatím vše vidí jen Správce.'}
        sety = ["p.nak = COALESCE(NULLIF(TRIM(m.nak), ''), p.nak)"]
        s_dodavatelem = {'dodavatel', 'dodavatel_popis'} <= sloupce
        if s_dodavatelem:
            sety += ["p.dodavatel = COALESCE(NULLIF(TRIM(m.dodavatel), ''), p.dodavatel)",
                     "p.dodavatel_nazev = COALESCE(NULLIF(TRIM(m.dodavatel_popis), ''), "
                     "p.dodavatel_nazev)"]
        for _col in ('sortiment_popis', 'skupina2_popis'):
            if _col in sloupce:
                sety.append(f"p.{_col} = COALESCE(NULLIF(TRIM(m.{_col}), ''), p.{_col})")
        # Kolace explicitně: master vzniká v jiném modulu, JOIN nesmí spadnout
        # na „Illegal mix of collations“.
        cur.execute('UPDATE lupa_nakup_produkt p JOIN cenopripad_master m '
                    'ON m.kod = p.kod COLLATE utf8mb4_unicode_ci '
                    f"SET {', '.join(sety)}, p.sparovano = NOW()")
        conn.commit()
        if not s_dodavatelem:
            return {'chyba': None,
                    'varovani': 'Dodavatel v DATA_POROVNANI zatím chybí — otevřete modul '
                                'Cenopřípad a DATA_POROVNANI nahrajte znovu.'}
        return {'chyba': None, 'varovani': None}
    finally:
        cur.close()


def doparuj_nakup(cekat=True):
    """Dopárování mimo import (tlačítko, nová DATA_POROVNANI)."""
    ok = _NAKUP_ZAMEK.acquire(timeout=10) if cekat else _NAKUP_ZAMEK.acquire(blocking=False)
    if not ok:
        return {'chyba': 'Právě běží import Lupou na nákup — dopáruje se na jeho konci.'}
    try:
        conn = intranet_data.get_db_connection()
        if not conn:
            return {'chyba': 'Není spojení s databází.'}
        try:
            return _nakup_doparuj(conn)
        except Exception as e:
            print(f'[lupa] dopárování nákupu: {e}')
            return {'chyba': str(e)}
        finally:
            conn.close()
    finally:
        _NAKUP_ZAMEK.release()


def _posledni_import_porovnani():
    """Id posledního nahrání DATA_POROVNANI v Cenopřípadu (None = nikdy / bez tabulky)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor()
        cur.execute('SELECT MAX(id) FROM cenopripad_import WHERE soubor_data IS NOT NULL')
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None                 # Cenopřípad tu ještě neběžel
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()


def nakup_doparuj_je_li_treba():
    """Po novém nahrání DATA_POROVNANI dopáruje znovu. Bez změny jen jeden SELECT.
    False = dopárování neproběhlo (běží import, chyba) a zařazení může být staré."""
    posledni = _posledni_import_porovnani()
    if posledni is None or posledni == _NAKUP_PAROVANI['import_id']:
        return True
    if doparuj_nakup(cekat=False).get('chyba'):
        return False
    _NAKUP_PAROVANI['import_id'] = posledni
    return True


def _nakup_katalog_prav():
    import intranet_prava
    return intranet_prava.ZAKLADNI_PRAVA


def _nakup_stav_parovani():
    """Souhrn pro kartu v Importu: rozsah dat, poslední import, úspěšnost párování."""
    s = {}
    r = _dotaz('SELECT COUNT(*) AS radku, MIN(rok*100+mesic) AS od, MAX(rok*100+mesic) AS do_, '
               'SUM(obrat_kc) AS kc FROM lupa_nakup_obrat', slovnik=True)
    s.update(r[0] if r else {})
    r = _dotaz('SELECT COUNT(*) AS produktu, SUM(nak IS NULL) AS bez_nak, '
               'SUM(dodavatel IS NULL) AS bez_dod, MAX(sparovano) AS sparovano '
               'FROM lupa_nakup_produkt', slovnik=True)
    s.update(r[0] if r else {})
    r = _dotaz('SELECT SUM(CASE WHEN p.nak IS NULL THEN o.obrat_kc ELSE 0 END) AS kc_bez_nak, '
               'SUM(CASE WHEN p.dodavatel IS NULL THEN o.obrat_kc ELSE 0 END) AS kc_bez_dod '
               + _NAKUP_JOIN, slovnik=True)
    s.update(r[0] if r else {})
    imp = _dotaz('SELECT kdy, uzivatel, soubor, radku FROM lupa_nakup_import '
                 'ORDER BY id DESC LIMIT 1', slovnik=True)
    s['import'] = imp[0] if imp else None
    katalog = _nakup_katalog_prav()
    s['bez_prava'] = sorted(
        k for (k,) in _dotaz('SELECT DISTINCT nak FROM lupa_nakup_produkt WHERE nak IS NOT NULL')
        if f'{NAKUP_PREFIX}{str(k).lower()}' not in katalog)
    s['nesparovane'] = _dotaz(
        f'SELECT o.kod, MAX(o.nazev) AS nazev, SUM(o.obrat_kc) AS kc {_NAKUP_JOIN} '
        'WHERE p.nak IS NULL GROUP BY o.kod ORDER BY kc DESC LIMIT 10', slovnik=True)
    for k in ('radku', 'produktu', 'bez_nak', 'bez_dod'):
        s[k] = int(s.get(k) or 0)          # SUM(bool) vrací Decimal
    for k in ('kc', 'kc_bez_nak', 'kc_bez_dod'):
        s[k] = float(s.get(k) or 0)
    return s


def _karta_nakup_stav(user_id, user_name, vsechna_prava, refreshable):
    s = _nakup_stav_parovani()
    with ui.card().classes('w-full max-w-2xl mt-4 p-4 bg-emerald-50 border '
                           'border-emerald-200 rounded-xl'):
        if not s['radku']:
            ui.label('Lupou na nákup zatím nemá žádná data.').classes('text-sm text-gray-600')
        else:
            ui.label(f"Data {_nakup_obd_txt(s['od'])} – {_nakup_obd_txt(s['do_'])} · "
                     f"{_nakup_cele_txt(s['radku'])} řádků · {_kc(s['kc'])}") \
                .classes('text-sm font-semibold text-emerald-900')
            imp = s.get('import')
            if imp:
                kdy = imp['kdy'].strftime('%d.%m.%Y %H:%M') if imp.get('kdy') else ''
                ui.label(f"Poslední import: {kdy} · {imp.get('uzivatel') or '?'} · "
                         f"{imp.get('soubor') or ''}").classes('text-xs text-emerald-800')
            kc = s['kc'] or 1.0
            pct_nak = 100.0 - s['kc_bez_nak'] / kc * 100.0 if s['kc'] else 0.0
            pct_dod = 100.0 - s['kc_bez_dod'] / kc * 100.0 if s['kc'] else 0.0
            ui.label(f"Spárováno se zkratkou nákupčího: {_cz(pct_nak, 1)} % obratu "
                     f"({_nakup_cele_txt(s['produktu'] - s['bez_nak'])} z "
                     f"{_nakup_cele_txt(s['produktu'])} produktů) · "
                     f"s dodavatelem: {_cz(pct_dod, 1)} %") \
                .classes('text-xs text-emerald-800')
            if s['bez_dod'] and pct_dod < 50.0:
                ui.label('Dodavatel se bere ze sloupců K/L DATA_POROVNANI. Po nasazení je jednou '
                         'nahrajte v Cenopřípadu znovu — pak se dopáruje sám.') \
                    .classes('text-xs text-amber-700')
            if s['bez_prava']:
                ui.label(f"Zkratky bez oprávnění v katalogu: {', '.join(s['bez_prava'])} — "
                         'jejich zboží vidí jen Správce.').classes('text-xs text-amber-700')
            if s['nesparovane'] and nakup_kody(vsechna_prava) is None:
                with ui.expansion('Nespárované zboží (TOP 10 podle obratu)') \
                        .classes('w-full text-xs text-gray-700'):
                    for r in s['nesparovane']:
                        ui.label(f"{r['kod']} · {r['nazev'] or ''} · {_kc(float(r['kc'] or 0))}") \
                            .classes('text-xs text-gray-600')

        async def doparuj():
            vys = await asyncio.to_thread(doparuj_nakup)
            if vys.get('chyba'):
                ui.notify(vys['chyba'], type='negative', multi_line=True)
                return
            await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_parovani')
            ui.notify(vys.get('varovani') or 'Dopárováno z DATA_POROVNANI.',
                      type='warning' if vys.get('varovani') else 'positive', multi_line=True)
            refreshable.refresh()

        if s['radku']:
            ui.button('Dopárovat znovu', icon='sync', on_click=_bez_klienta(doparuj)) \
                .props('flat dense no-caps color=emerald-8').classes('mt-1') \
                .tooltip('Párování běží samo po každém importu i po novém nahrání '
                         'DATA_POROVNANI v Cenopřípadu.')


# ── Filtry a dotazy ─────────────────────────────────────────────────────────
def _nakup_cele_txt(n):
    """12345 → '12 345' (mezera = NBSP jako v _cz)."""
    return f'{int(n or 0):,}'.replace(',', '\xa0')


def _nakup_obd_txt(obd):
    """202610 → '2026M10'."""
    obd = int(obd or 0)
    return f'{obd // 100}M{obd % 100:02d}' if obd else ''


def _nakup_rozsah_txt(od, do):
    return _nakup_obd_txt(od) if od == do else f'{_nakup_obd_txt(od)}–{_nakup_obd_txt(do)}'


def _nakup_posun(obd, o_mesicu):
    r, m = divmod(int(obd), 100)
    i = r * 12 + (m - 1) + o_mesicu
    return (i // 12) * 100 + i % 12 + 1


def _nakup_obdobi_ab(f, posledni):
    """Sledované období A a srovnávací B (rok/rok, měsíc/měsíc = stejně dlouhé
    předchozí období, nebo vlastní). Vrací ints yyyymm."""
    a_od = _obd_int(f.get('od')) or posledni
    a_do = _obd_int(f.get('do')) or posledni
    if a_od > a_do:
        a_od, a_do = a_do, a_od
    rezim = f.get('srovnat') or 'rok'
    b_od, b_do = _obd_int(f.get('b_od')), _obd_int(f.get('b_do'))
    if rezim == 'vlastni' and b_od and b_do:
        b_od, b_do = min(b_od, b_do), max(b_od, b_do)
    elif rezim == 'mesic':
        delka = (a_do // 100 - a_od // 100) * 12 + (a_do % 100 - a_od % 100) + 1
        b_od, b_do = _nakup_posun(a_od, -delka), _nakup_posun(a_od, -1)
    else:
        b_od, b_do = _nakup_posun(a_od, -12), _nakup_posun(a_do, -12)
    return a_od, a_do, b_od, b_do


def _nakup_in(sloupec, hodnoty, kde, par):
    """`sloupec IN (…)`; volba _BEZ = nespárováno (NULL)."""
    hodnoty = [h for h in (hodnoty or []) if h is not None]
    if not hodnoty:
        return
    vlastni = [h for h in hodnoty if h != _BEZ]
    casti = []
    if vlastni:
        casti.append(f"{sloupec} IN ({','.join(['%s'] * len(vlastni))})")
        par.extend(vlastni)
    if len(vlastni) < len(hodnoty):
        casti.append(f'{sloupec} IS NULL')
    kde.append('(' + ' OR '.join(casti) + ')')


def _nakup_where(f, kody, s_obdobim=True):
    """WHERE nad o (lupa_nakup_obrat) + p (lupa_nakup_produkt). Práva první:
    nákupčí vidí jen zboží svých zkratek — nespárované (NULL) mu neprojde.
    Neuzavřené měsíce vyřazujeme vždy, i ve srovnání (s_obdobim=False), stejně
    jako obchod v _filtr_sql."""
    kde, par = [], []
    if kody is not None:
        if kody:
            kde.append(f"p.nak IN ({','.join(['%s'] * len(kody))})")
            par.extend(sorted(kody))
        else:
            kde.append('1=0')
    nekompletni = _nekompletni_klice(_UZAVRENE_NAKUP_PREFIX)
    if nekompletni:
        kde.append('o.rok*100+o.mesic NOT IN (%s)' % ','.join(['%s'] * len(nekompletni)))
        par.extend(nekompletni)
    if s_obdobim:
        od, do = _obd_int(f.get('od')), _obd_int(f.get('do'))
        if od and do and od > do:
            od, do = do, od
        if od:
            kde.append('o.rok*100+o.mesic >= %s')
            par.append(od)
        if do:
            kde.append('o.rok*100+o.mesic <= %s')
            par.append(do)
    _nakup_in('o.divize', f.get('divize'), kde, par)
    _nakup_in('o.pobocka', f.get('pobocka'), kde, par)
    _nakup_in('o.ico', f.get('ico'), kde, par)
    _nakup_in('p.dodavatel', f.get('dodavatel'), kde, par)
    _nakup_in('p.nak', f.get('nak'), kde, par)
    _nakup_in('o.kod', f.get('kody'), kde, par)
    # Produkt: přesný kód, nebo část názvu; víc hodnot oddělených čárkou = nebo.
    hledane = [h.strip() for h in re.split(r'[,;]', str(f.get('produkt') or '')) if h.strip()]
    if hledane:
        kde.append('(' + ' OR '.join(['(o.kod = %s OR o.nazev LIKE %s)'] * len(hledane)) + ')')
        for h in hledane:
            par += [h, f'%{h}%']
    return (' AND '.join(kde) or '1=1'), par


def _nakup_mesice():
    return [_nakup_obd_txt(k) for (k,) in
            _dotaz('SELECT DISTINCT rok*100+mesic AS k FROM lupa_nakup_obrat ORDER BY k')]


def _nakup_hotove(mesice):
    """Uzavřené měsíce nákupu — jen ty jdou zvolit ve filtrech."""
    ven = _nekompletni_obdobi(mesice, _UZAVRENE_NAKUP_PREFIX)
    return [m for m in mesice if m not in ven]


def _nakup_sel(label, mesice, hodnota):
    """Výběr měsíce nákupu: neuzavřené šedé jako u obchodu. Bez křížku —
    prázdné Od/Do by výjezd pustilo přes celou historii."""
    return _sel_obdobi(label, mesice, hodnota, _UZAVRENE_NAKUP_PREFIX) \
        .props(remove='clearable')


def _nakup_volby(kody):
    """Volby filtrů {hodnota: popisek}. Dodavatelé a nákupčí jen v rozsahu práv."""
    v = {
        'divize': {d: d for (d,) in _dotaz(
            "SELECT DISTINCT divize FROM lupa_nakup_obrat WHERE divize <> '' ORDER BY divize")},
        'pobocka': {d: d for (d,) in _dotaz(
            "SELECT DISTINCT pobocka FROM lupa_nakup_obrat WHERE pobocka <> '' ORDER BY pobocka")},
        'ico': {i: (f'{j} ({i})' if j else i) for i, j in _dotaz(
            "SELECT ico, MAX(jmeno) FROM lupa_nakup_obrat WHERE ico <> '' "
            'GROUP BY ico ORDER BY MAX(jmeno), ico')},
    }
    if kody is None:
        kde, par = '1=1', []
    elif kody:
        kde, par = f"p.nak IN ({','.join(['%s'] * len(kody))})", sorted(kody)
    else:
        kde, par = '1=0', []
    v['dodavatel'] = {d: (f'{d} – {n}' if n else d) for d, n in _dotaz(
        'SELECT p.dodavatel, MAX(p.dodavatel_nazev) FROM lupa_nakup_produkt p '
        f'WHERE p.dodavatel IS NOT NULL AND {kde} '
        'GROUP BY p.dodavatel ORDER BY MAX(p.dodavatel_nazev), p.dodavatel', par)}
    v['nak'] = {n: n for (n,) in _dotaz(
        f'SELECT DISTINCT p.nak FROM lupa_nakup_produkt p WHERE p.nak IS NOT NULL AND {kde} '
        'ORDER BY p.nak', par)}
    if kody is None:
        if _dotaz('SELECT 1 FROM lupa_nakup_produkt WHERE dodavatel IS NULL LIMIT 1'):
            v['dodavatel'] = {_BEZ: '(nespárováno)', **v['dodavatel']}
        if _dotaz('SELECT 1 FROM lupa_nakup_produkt WHERE nak IS NULL LIMIT 1'):
            v['nak'] = {_BEZ: '(nespárováno)', **v['nak']}
    return v


def _nakup_popis(f, volby, kody, s_dims=True):
    """Lidský popis filtru do PDF, tisku a auditu."""
    od, do = f.get('od') or '', f.get('do') or ''
    casti = [f'Období: {od}' if od == do else f'Období: {od} – {do}']
    for klic, nazev in (('divize', 'Divize'), ('pobocka', 'Pobočka'), ('ico', 'Odběratel'),
                        ('dodavatel', 'Dodavatel'), ('nak', 'Nákupčí')):
        hodnoty = f.get(klic) or []
        if hodnoty:
            opts = volby.get(klic) or {}
            jmena = [str(opts.get(h, h)) for h in hodnoty]
            casti.append(f"{nazev}: {', '.join(jmena[:5])}"
                         + (f' (+{len(jmena) - 5} dalších)' if len(jmena) > 5 else ''))
    if f.get('produkt'):
        casti.append(f"Produkt: {f['produkt']}")
    if s_dims and f.get('dims'):
        casti.append('Seskupeno: ' + ', '.join(n.split(' (')[0].lower()
                                               for d, n in _NAKUP_DIMENZE if d in f['dims']))
    if kody is not None:
        casti.append(f"Oprávnění: nákupčí {', '.join(sorted(kody))}")
    return ' · '.join(casti)


def _nakup_vyjezd_sql(f, kody):
    """SELECT výjezdu podle zvoleného seskupení → (sql, par, cols)."""
    vybrane = f.get('dims') or _NAKUP_DIMENZE_VYCHOZI
    dims = [d for d, _n in _NAKUP_DIMENZE if d in vybrane] or list(_NAKUP_DIMENZE_VYCHOZI)
    sel, grp, poradi, cols = [], [], [], []
    if 'mesic' in dims:
        sel.append("CONCAT(o.rok, 'M', LPAD(o.mesic, 2, '0')) AS mesic")
        grp += ['o.rok', 'o.mesic']
        poradi += ['o.rok', 'o.mesic']
        cols.append(('Měsíc', 'mesic', 'text', 9))
    if 'divize' in dims:
        sel.append('o.divize AS divize')
        grp.append('o.divize')
        poradi.append('o.divize')
        cols.append(('Divize', 'divize', 'text', 14))
    if 'pobocka' in dims:
        sel.append('o.pobocka AS pobocka')
        grp.append('o.pobocka')
        poradi.append('o.pobocka')
        cols.append(('Pobočka', 'pobocka', 'text', 22))
    if 'odberatel' in dims:
        sel += ['o.ico AS ico', 'MAX(o.jmeno) AS jmeno']
        grp.append('o.ico')
        poradi.append('o.ico')
        cols += [('IČO', 'ico', 'text', 15), ('Jméno', 'jmeno', 'text', 28)]
    if 'produkt' in dims:
        sel += ['o.kod AS kod', 'MAX(o.nazev) AS nazev']
        grp.append('o.kod')
        cols += [('Kód', 'kod', 'text', 12), ('Název', 'nazev', 'text', 38)]
    # Dodavatel a nákupčí jsou vlastnost produktu — u produktu je ukážeme vždy.
    if 'dodavatel' in dims or 'produkt' in dims:
        sel += ['p.dodavatel AS dodavatel', 'MAX(p.dodavatel_nazev) AS dodavatel_nazev']
        grp.append('p.dodavatel')
        if 'produkt' not in dims:
            poradi.append('dodavatel_nazev')
        cols += [('Dodavatel', 'dodavatel', 'text', 10),
                 ('Dodavatel – název', 'dodavatel_nazev', 'text', 28)]
    if 'nak' in dims or 'produkt' in dims:
        sel.append('p.nak AS nak')
        grp.append('p.nak')
        if 'produkt' not in dims and 'dodavatel' not in dims:
            poradi.append('p.nak')
        cols.append(('Nákupčí', 'nak', 'text', 8))
    if 'produkt' not in dims:
        sel.append('COUNT(DISTINCT o.kod) AS polozek')
        cols.append(('Položek', 'polozek', 'int', 9))
    sel += ['SUM(o.obrat_mj) AS mj', 'SUM(o.obrat_kc) AS kc']
    cols += [('Obrat MJ', 'mj', 'num', 13), ('Obrat Kč bez DPH', 'kc', 'money', 16)]
    kde, par = _nakup_where(f, kody)
    sql = (f"SELECT {', '.join(sel)} {_NAKUP_JOIN} WHERE {kde} "
           f"GROUP BY {', '.join(grp)} ORDER BY {', '.join(poradi + ['kc DESC'])}")
    return sql, par, cols


def _nakup_celkem(cols, souhrn, popisek='CELKEM'):
    """Součtový řádek: první textový sloupec nese popisek, čísla ze souhrnu."""
    celkem, prvni = {'_celkem': True}, True
    for _n, field, typ, _w in cols:
        if typ == 'text':
            celkem[field] = popisek if prvni else ''
            prvni = False
        else:
            celkem[field] = souhrn.get(field)
    return celkem


def _nakup_vyjezd_data(f, kody, limit):
    """Náhled: prvních `limit` řádků + přesný počet v jednom průchodu. LIMIT a pak
    COUNT nad seskupeným dotazem by stály dvě stejně drahá GROUP BY (celý měsíc
    v plném detailu = 130 tis. řádků, každé ~2,5 s). Chyba DB letí ven — UI ji ukáže."""
    sql, par, cols = _nakup_vyjezd_sql(f, kody)
    kde, par2 = _nakup_where(f, kody)
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Není spojení s databází.')
    rows, pocet = [], 0
    try:
        for r in _nakup_iter(conn, sql, par):
            if pocet < limit:
                rows.append(r)
            pocet += 1
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute('SELECT SUM(o.obrat_mj) AS mj, SUM(o.obrat_kc) AS kc, '
                        f'COUNT(DISTINCT o.kod) AS polozek {_NAKUP_JOIN} WHERE {kde}', par2)
            souhrn = cur.fetchone() or {}
        finally:
            cur.close()
    finally:
        conn.close()
    return {'rows': rows, 'cols': cols, 'total': _nakup_celkem(cols, souhrn),
            'celkem_radku': pocet}


def _nakup_iter(conn, sql, par, davka=2000):
    """Nebufferovaný kurzor → generátor dictů; výjezd teče z DB rovnou do sešitu."""
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, par)
        while True:
            radky = cur.fetchmany(davka)
            if not radky:
                return
            yield from radky
    finally:
        cur.close()


def _nakup_vyjezd_xlsx(f, kody, cesta, total):
    sql, par, cols = _nakup_vyjezd_sql(f, kody)
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Není spojení s databází.')
    try:
        return _xlsx_na_disk(cesta, cols, _nakup_iter(conn, sql, par), total, sheet='Výjezd')
    finally:
        conn.close()


def _nakup_cols_srovnani(rozpad, a_txt, b_txt):
    popisek = _NAKUP_ROZPADY[rozpad][0]
    if rozpad == 'dodavatel':
        cols = [('Dodavatel', 'klic', 'text', 10), ('Název dodavatele', 'nazev', 'text', 32),
                ('Nákupčí', 'nak', 'text', 9)]
    elif rozpad == 'produkt':
        cols = [('Kód', 'klic', 'text', 12), ('Název', 'nazev', 'text', 34),
                ('Dodavatel', 'dod_nazev', 'text', 24), ('Nákupčí', 'nak', 'text', 8)]
    elif rozpad == 'odberatel':
        cols = [('IČO', 'klic', 'text', 15), ('Jméno', 'nazev', 'text', 28),
                ('Dodavatelů', 'dodavatelu', 'int', 10)]
    else:
        cols = [(popisek, 'klic', 'text', 24), ('Dodavatelů', 'dodavatelu', 'int', 10)]
    cols += [(f'Kč {a_txt}', 'kc_a', 'money', 16), (f'Kč {b_txt}', 'kc_b', 'money', 16),
             ('Rozdíl Kč', 'rozdil', 'money', 14), ('Rozdíl Kč %', 'zmena', 'num', 11),
             ('Podíl %', 'podil', 'num', 8),
             (f'MJ {a_txt}', 'mj_a', 'num', 12), (f'MJ {b_txt}', 'mj_b', 'num', 12),
             ('Rozdíl MJ', 'rozdil_mj', 'num', 12), ('Rozdíl MJ %', 'zmena_mj', 'num', 11)]
    if rozpad != 'produkt':
        cols.append((f'Položek {a_txt}', 'polozek_a', 'int', 9))
    return cols


def _nakup_srovnani_data(f, kody, rozpad, posledni):
    """Odběry rozpadu (dodavatel, nákupčí, pobočka…) ve sledovaném období A
    proti srovnávacímu B + řada po měsících pro meziroční graf."""
    rozpad = rozpad if rozpad in _NAKUP_ROZPADY else 'dodavatel'
    _popisek, _gen, klic_sql, nazev_sql, _fk, _pod = _NAKUP_ROZPADY[rozpad]
    a_od, a_do, b_od, b_do = _nakup_obdobi_ab(f, posledni)
    kde, par = _nakup_where(f, kody, s_obdobim=False)
    v_a = v_b = '(o.rok*100+o.mesic) BETWEEN %s AND %s'
    nazev_expr = nazev_sql or "''"
    sql = (f'SELECT {klic_sql} AS klic, {nazev_expr} AS nazev, '
           "GROUP_CONCAT(DISTINCT p.nak ORDER BY p.nak SEPARATOR ', ') AS nak, "
           'MAX(p.dodavatel_nazev) AS dod_nazev, COUNT(DISTINCT p.dodavatel) AS dodavatelu, '
           f'SUM(CASE WHEN {v_a} THEN o.obrat_kc ELSE 0 END) AS kc_a, '
           f'SUM(CASE WHEN {v_b} THEN o.obrat_kc ELSE 0 END) AS kc_b, '
           f'SUM(CASE WHEN {v_a} THEN o.obrat_mj ELSE 0 END) AS mj_a, '
           f'SUM(CASE WHEN {v_b} THEN o.obrat_mj ELSE 0 END) AS mj_b, '
           f'COUNT(DISTINCT CASE WHEN {v_a} THEN o.kod END) AS polozek_a '
           f'{_NAKUP_JOIN} WHERE {kde} AND ({v_a} OR {v_b}) '
           f'GROUP BY {klic_sql} ORDER BY kc_a DESC, kc_b DESC')
    ab = [a_od, a_do, b_od, b_do]
    rows = _dotaz(sql, ab + ab + [a_od, a_do] + par + ab, slovnik=True)

    tot_a = round(sum(float(r['kc_a'] or 0) for r in rows), 2)
    tot_b = round(sum(float(r['kc_b'] or 0) for r in rows), 2)
    for r in rows:
        # Zaokrouhlení maže plovoucí šum součtů DOUBLE (1,1 + 2,2 − 3,3 = 4e-16) —
        # z takové „nuly“ v B by procento rozdílu vyšlo v řádu 10^17 %.
        for k, des in (('kc_a', 2), ('kc_b', 2), ('mj_a', 3), ('mj_b', 3)):
            r[k] = round(float(r[k] or 0), des)
        surovy = '' if r['klic'] is None else str(r['klic'])
        r['hodnota'] = surovy                      # pro proklik; '' = nespárováno / prázdné
        if not surovy:
            r['klic'] = '(nespárováno)' if rozpad == 'nak' else '—'
            if rozpad == 'dodavatel':
                r['nazev'] = '(nespárováno s DATA_POROVNANI)'
        r['rozdil'] = round(r['kc_a'] - r['kc_b'], 2)
        r['zmena'] = _zmena_pct(r['kc_a'], r['kc_b'])
        r['rozdil_mj'] = round(r['mj_a'] - r['mj_b'], 3)
        r['zmena_mj'] = _zmena_pct(r['mj_a'], r['mj_b'])
        r['podil'] = r['kc_a'] / tot_a * 100.0 if tot_a else None
    mesice = _dotaz(f'SELECT o.rok*100+o.mesic AS klic, SUM(o.obrat_kc) AS kc {_NAKUP_JOIN} '
                    f'WHERE {kde} GROUP BY o.rok, o.mesic ORDER BY klic', par, slovnik=True)
    a_txt, b_txt = _nakup_rozsah_txt(a_od, a_do), _nakup_rozsah_txt(b_od, b_do)
    mj_a = round(sum(r['mj_a'] for r in rows), 3)
    mj_b = round(sum(r['mj_b'] for r in rows), 3)
    celkem = {'_celkem': True, 'klic': 'CELKEM', 'nazev': '', 'kc_a': tot_a, 'kc_b': tot_b,
              'rozdil': round(tot_a - tot_b, 2), 'zmena': _zmena_pct(tot_a, tot_b),
              'podil': 100.0 if tot_a else None,
              'mj_a': mj_a, 'mj_b': mj_b, 'rozdil_mj': round(mj_a - mj_b, 3),
              'zmena_mj': _zmena_pct(mj_a, mj_b)}
    return {'rows': rows, 'cols': _nakup_cols_srovnani(rozpad, a_txt, b_txt),
            'celkem': celkem, 'mesice': mesice, 'rozpad': rozpad,
            'a_txt': a_txt, 'b_txt': b_txt,
            'aktivnich': sum(1 for r in rows if r['kc_a'] > 0)}


# ── Měsíční report nákupčím (e-mail k 10. dni) ──────────────────────────────
_NAKUP_REPORT_DEN = 10        # od tohoto dne v měsíci odchází report za předchozí měsíc
_NAKUP_REPORT_HODINA = 7
_MESICE_CELE = ['leden', 'únor', 'březen', 'duben', 'květen', 'červen',
                'červenec', 'srpen', 'září', 'říjen', 'listopad', 'prosinec']
_NAKUP_REPORT_CEKA = set()    # důvody čekání už vypsané do logu (kolo běží co 30 min)


_NAKUP_REPORT_KLIC = 'lupa_nakup_report'   # nastavení portálu: {zapnuto, listy, sloupce}
_NAKUP_REPORT_LISTY = {'mesic': 'Minulý měsíc',
                       'rok': 'Od začátku roku do minulého měsíce'}
# Datové sloupce (pole, záhlaví, typ, šířka); {loni}/{letos} = roky, ve volbě slovy.
# IČO ani nákupní ceny v datech nejsou: dodavatel je kód z DATA_POROVNANI,
# prodej je obrat GIST v Kč bez DPH.
_NAKUP_REPORT_SLOUPCE = [
    ('kc_b', 'Prodej {loni} (Kč bez DPH)', 'money', 17),
    ('kc_a', 'Prodej {letos} (Kč bez DPH)', 'money', 17),
    ('mj_b', 'Prodej {loni} (MJ)', 'num', 13),
    ('mj_a', 'Prodej {letos} (MJ)', 'num', 13),
    ('rozdil', 'Rozdíl Kč v Kč', 'money', 15),
    ('zmena', 'Rozdíl Kč v %', 'num', 11),
    ('rozdil_mj', 'Rozdíl MJ v množství', 'num', 14),
    ('zmena_mj', 'Rozdíl MJ v %', 'num', 11),
]
_NAKUP_REPORT_CELKEM = {'kc_b', 'kc_a', 'rozdil', 'zmena'}   # součty kategorií jen v Kč


def _nakup_report_nastaveni(nastaveni=None):
    """{'zapnuto', 'listy', 'sloupce'}. Výchozí vypnuto; dokud nikdo nevybíral, nabízí vše."""
    if nastaveni is None:
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
    n = nastaveni.get(_NAKUP_REPORT_KLIC)
    n = n if isinstance(n, dict) else {}
    vse_sloupce = [p for p, *_ in _NAKUP_REPORT_SLOUPCE]
    # Prázdný výběr nesmí projít: sešit bez listů by se zapsal jako „odesláno (bez dat)“.
    listy = [k for k in _NAKUP_REPORT_LISTY if k in (n.get('listy') or ())]
    sloupce = [p for p in vse_sloupce if p in (n.get('sloupce') or ())]
    return {'zapnuto': n.get('zapnuto') is True,
            'listy': listy or list(_NAKUP_REPORT_LISTY),
            'sloupce': sloupce or vse_sloupce}


def _nakup_report_uloz(zapnuto, listy=None, sloupce=None):
    n = intranet_data.nacti_nastaveni_intranetu()
    akt = dict(n.get(_NAKUP_REPORT_KLIC) or {}, zapnuto=bool(zapnuto))
    if listy is not None:
        akt['listy'] = list(listy)
    if sloupce is not None:
        akt['sloupce'] = list(sloupce)
    n[_NAKUP_REPORT_KLIC] = akt
    intranet_data.uloz_nastaveni_intranetu(n)


def _nakup_report_volba(pole):
    """Název sloupce pro výběr a log („Prodej loni (MJ)“)."""
    return next(h for p, h, *_ in _NAKUP_REPORT_SLOUPCE if p == pole) \
        .format(loni='loni', letos='letos')


def _nakup_report_cols(rok, sloupce, kategorie=False):
    return [('Kód dodavatele', 'dodavatel', 'text', 11),
            ('Název dodavatele', 'dod_nazev', 'text', 32),
            ('Popis sortimentu', 'sortiment', 'text', 30)] + (
        [('Kategorie', 'kategorie', 'text', 28)] if kategorie else []) + [
        (hlav.format(loni=rok - 1, letos=rok), pole, typ, sirka)
        for pole, hlav, typ, sirka in _NAKUP_REPORT_SLOUPCE if pole in sloupce]


def _nakup_report_pct(nove, stare):
    z = _zmena_pct(nove, stare)
    return None if z is None else round(z, 1)


def _nakup_report_dotaz(sql, params):
    """Jako _dotaz (slovníky), ale chybu DB vyhodí: prázdný výsledek pak opravdu
    znamená „bez dat“ a výpadek spojení se nezapíše jako vyřízený report."""
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Není spojení s databází.')
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        if cur:
            cur.close()
        conn.close()


def _nakup_report_data(kod, rok, mesice):
    """Prodej zboží nákupčího `kod` po dodavateli × popisu sortimentu × kategorii
    (Skupina2-popis): měsíce `mesice` roku `rok` (…_a) proti stejným měsícům
    loni (…_b). Kategorie je v klíči řádku, protože stejný popis sortimentu často
    spadá do víc kategorií — jen tak jdou součty kategorií v Excelu sečíst
    vzorcem přímo z řádků."""
    if not mesice:
        return []
    surove = _nakup_report_dotaz(
        'SELECT p.dodavatel, MAX(p.dodavatel_nazev) AS dod_nazev, '
        "COALESCE(TRIM(p.sortiment_popis), '') AS sort_popis, "
        "COALESCE(TRIM(p.skupina2_popis), '') AS kategorie, "
        'SUM(CASE WHEN o.rok = %s THEN o.obrat_kc ELSE 0 END) AS kc_a, '
        'SUM(CASE WHEN o.rok = %s THEN o.obrat_kc ELSE 0 END) AS kc_b, '
        'SUM(CASE WHEN o.rok = %s THEN o.obrat_mj ELSE 0 END) AS mj_a, '
        'SUM(CASE WHEN o.rok = %s THEN o.obrat_mj ELSE 0 END) AS mj_b '
        f'{_NAKUP_JOIN} WHERE p.nak = %s AND o.rok IN (%s, %s) '
        f"AND o.mesic IN ({','.join(['%s'] * len(mesice))}) "
        'GROUP BY p.dodavatel, sort_popis, kategorie',
        [rok, rok - 1, rok, rok - 1, kod, rok, rok - 1] + list(mesice))
    rows, jmena = [], {}
    for r in surove:
        # Zaokrouhlení maže plovoucí šum součtů DOUBLE (viz _nakup_srovnani_data).
        r = {'dodavatel': r['dodavatel'] or '—', 'dod_nazev': r['dod_nazev'] or '',
             'sortiment': r['sort_popis'] or '—', 'kategorie': r['kategorie'] or '',
             'kc_a': round(float(r['kc_a'] or 0), 2), 'kc_b': round(float(r['kc_b'] or 0), 2),
             'mj_a': round(float(r['mj_a'] or 0), 3), 'mj_b': round(float(r['mj_b'] or 0), 3)}
        # Řádek, kde vratky prodej v obou letech vynulovaly, je jen šum.
        if not (r['kc_a'] or r['kc_b'] or r['mj_a'] or r['mj_b']):
            continue
        # Kolace DB slučuje kategorie bez ohledu na velikost písmen, Excel je
        # porovnává stejně — jeden zápis na kategorii, ať součty sedí.
        r['kategorie'] = jmena.setdefault(r['kategorie'].casefold(),
                                          r['kategorie'] or 'Bez kategorie')
        r['rozdil'] = round(r['kc_a'] - r['kc_b'], 2)
        r['zmena'] = _nakup_report_pct(r['kc_a'], r['kc_b'])
        r['rozdil_mj'] = round(r['mj_a'] - r['mj_b'], 3)
        r['zmena_mj'] = _nakup_report_pct(r['mj_a'], r['mj_b'])
        rows.append(r)
    rows.sort(key=lambda r: (-r['kc_a'], -r['kc_b'], r['dod_nazev'].lower(),
                             r['sortiment'].lower(), r['kategorie'].lower()))
    return rows


def _nakup_report_kategorie(rows):
    """Součty kategorií v Kč (stejně jako je Excel sečte z řádků), seřazené od
    největšího letošního prodeje; „Bez kategorie“ na konec. Prázdné, dokud
    DATA_POROVNANI nebyla nahrána znovu — jediný blok „Bez kategorie“ by jen
    zopakoval celkový součet."""
    if all(r['kategorie'] == 'Bez kategorie' for r in rows):
        return []
    po_kat = {}
    for r in rows:
        k = po_kat.setdefault(r['kategorie'], {'kategorie': r['kategorie'],
                                               'kc_a': 0.0, 'kc_b': 0.0, 'rozdil': 0.0})
        for pole in ('kc_a', 'kc_b', 'rozdil'):
            k[pole] += r[pole]
    kat = []
    for k in po_kat.values():
        for pole in ('kc_a', 'kc_b', 'rozdil'):
            k[pole] = round(k[pole], 2)
        if k['kc_a'] or k['kc_b']:
            k['zmena'] = _nakup_report_pct(k['kc_a'], k['kc_b'])
            kat.append(k)
    kat.sort(key=lambda k: (k['kategorie'] == 'Bez kategorie', -k['kc_a'], -k['kc_b'],
                            k['kategorie'].lower()))
    return kat


_NAKUP_REPORT_SCITANE = ('kc_b', 'kc_a', 'rozdil')    # součty jen v Kč
_NAKUP_REPORT_CERVENE = {'rozdil', 'zmena', 'rozdil_mj', 'zmena_mj'}   # zápor červeně
# % vždy s jedním desetinným místem: u vzorce (CELKEM, kategorie) nejde předem poznat
# celé číslo a obecný formát '#,##0.###' by u 1161 ukázal „1 161,“.
_NAKUP_REPORT_PROCENTA = {'zmena', 'zmena_mj'}


def _nakup_report_xlsx(kod, listy):
    """Sešit reportu z listů [(klíč, název, cols, rows, se_součty)].
    Na listu barevná tabulka řádků a vedle ní tabulka kategorií (Skupina 2 popis).
    Součty jsou vzorce: CELKEM pod tabulkami je SUBTOTAL (při filtru sčítá jen
    viditelné řádky), kategorie SUMIFS ze sloupce Kategorie. Vzorcům se dopíše
    i spočtená hodnota — náhled přílohy v mobilu nebo v poště sám nepřepočítává."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter as pismeno
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = Workbook()
    wb.remove(wb.active)
    cache = {}     # (pořadí listu, 'E5') → hodnota vzorce
    for i, (klic, nazev, cols, rows, se_soucty) in enumerate(listy):
        ws = wb.create_sheet(nazev[:31])

        def bunka(r, c, v, typ='text', field=None, tucne=False, vypln=None):
            cell = ws.cell(r, c, v)
            if typ == 'text' and isinstance(v, str) and v.startswith('='):
                cell.data_type = 's'          # text z dat, ne vzorec
            fmt = '#,##0.0' if field in _NAKUP_REPORT_PROCENTA else _xlsx_format(typ, v)
            if field in _NAKUP_REPORT_CERVENE and typ != 'text':
                fmt = f'{fmt};[Red]-{fmt}'
            cell.number_format = fmt
            if tucne:
                cell.font = Font(bold=True)
            if vypln:
                cell.fill = PatternFill('solid', fgColor=vypln)
            return cell

        def tabulka(jmeno, styl, c0, tcols, data):
            """Záhlaví v řádku 1, data od řádku 2; vrací {pole: písmeno sloupce}."""
            m = {}
            for j, (nadpis, field, typ, sirka) in enumerate(tcols, c0):
                m[field] = pismeno(j)
                ws.column_dimensions[m[field]].width = sirka
                bunka(1, j, nadpis).alignment = Alignment(
                    horizontal='center', vertical='center', wrap_text=True)
                for r, d in enumerate(data, 2):
                    v = d.get(field)
                    if isinstance(v, tuple):          # (vzorec, spočtená hodnota)
                        cache[(i, f'{m[field]}{r}')] = v[1]
                        bunka(r, j, v[0], 'num' if field == 'zmena' else typ, field)
                    else:
                        bunka(r, j, _xlsx_hodnota(v, typ), typ, field)
            if data:
                ws.add_table(Table(
                    displayName=jmeno,
                    ref=f'{pismeno(c0)}1:{pismeno(c0 + len(tcols) - 1)}{len(data) + 1}',
                    tableStyleInfo=TableStyleInfo(name=styl, showRowStripes=True)))
            return m

        def pct(m, r, hodnota):
            """Rozdíl Kč v % ze sloupců téhož řádku; bez nich jen spočtená hodnota."""
            if 'kc_b' not in m or not {'kc_a', 'rozdil'} & m.keys():
                return hodnota
            b = f'{m["kc_b"]}{r}'
            d = f'{m["kc_a"]}{r}-{b}' if 'kc_a' in m else f'{m["rozdil"]}{r}'
            return (f'=IF({b}=0,"",ROUND(({d})/ABS({b})*100,1))', hodnota)

        def celkem(r, tcols, c0, m, posledni, popisky, hodnoty, vypln):
            """Řádek CELKEM pod tabulkou: SUBTOTAL ze řádků 2…posledni."""
            for j, (_n, field, typ, _w) in enumerate(tcols, c0):
                if field in _NAKUP_REPORT_SCITANE:
                    cache[(i, f'{m[field]}{r}')] = hodnoty[field]
                    v = f'=SUBTOTAL(109,{m[field]}2:{m[field]}{posledni})'
                elif field == 'zmena':
                    v = pct(m, r, hodnoty['zmena'])
                    if isinstance(v, tuple):
                        cache[(i, f'{m[field]}{r}')] = v[1]
                        v, typ = v[0], 'num'
                else:
                    v = popisky.get(field)
                bunka(r, j, v, typ, field, tucne=True, vypln=vypln)

        n = len(rows)
        m = tabulka(f'Prodeje_{klic}', 'TableStyleMedium2', 1, cols, rows)
        ws.row_dimensions[1].height = 32
        ws.freeze_panes = 'A2'
        if not rows:
            ws.cell(2, 1, 'Za toto období nákupčí nemá žádný prodej.')
            continue
        if not se_soucty:
            continue
        soucet = {f: round(sum(r[f] for r in rows), 2) for f in _NAKUP_REPORT_SCITANE}
        soucet['zmena'] = _nakup_report_pct(soucet['kc_a'], soucet['kc_b'])
        celkem(n + 2, cols, 1, m, n + 1, {'dodavatel': 'CELKEM', 'dod_nazev': f'Nákupčí {kod}'},
               soucet, 'D9E1F2')

        kategorie = _nakup_report_kategorie(rows) if 'kategorie' in m else []
        if not kategorie:
            continue
        c0 = len(cols) + 2                 # jeden prázdný sloupec mezi tabulkami
        ws.column_dimensions[pismeno(c0 - 1)].width = 3
        kcols = [('Kategorie', 'kategorie', 'text', 30)] + [
            c for c in cols if c[1] in _NAKUP_REPORT_SCITANE or c[1] == 'zmena']
        km = {f: pismeno(c0 + j) for j, (_n, f, _t, _w) in enumerate(kcols)}
        K = m['kategorie']
        data = []
        for r, k in enumerate(kategorie, 2):
            d = {'kategorie': k['kategorie']}
            for f in _NAKUP_REPORT_SCITANE:
                if f in km:
                    sl, kr = f'{m[f]}$2:{m[f]}${n + 1}', f'{K}$2:{K}${n + 1}'
                    # SUMIFS bere * ? ~ a úvodní < > = jako zástupné znaky / podmínku.
                    d[f] = ((f'=SUMPRODUCT(--({kr}={km["kategorie"]}{r}),{sl})'
                             if re.search(r'[*?~]|^[<>=]', k['kategorie']) else
                             f'=SUMIFS({sl},{kr},{km["kategorie"]}{r})'), k[f])
            d['zmena'] = pct(km, r, k['zmena'])
            data.append(d)
        tabulka(f'Kategorie_{klic}', 'TableStyleMedium7', c0, kcols, data)
        celkem(len(kategorie) + 2, kcols, c0, km, len(kategorie) + 1,
               {'kategorie': 'CELKEM'}, soucet, 'E2EFDA')
    buf = io.BytesIO()
    wb.save(buf)
    return _xlsx_s_hodnotami(buf.getvalue(), cache)


def _xlsx_s_hodnotami(data, cache):
    """Dopíše ke vzorcům spočtené hodnoty (<v>), které openpyxl nezapisuje.
    cache = {(pořadí listu, 'E5'): číslo}; vzorec bez čísla (např. "") zůstane prázdný."""
    import zipfile
    po_listech = {}
    for (i, ref), v in cache.items():
        if isinstance(v, (int, float)):
            po_listech.setdefault(f'xl/worksheets/sheet{i + 1}.xml', {})[ref.encode()] = v
    vzor = re.compile(rb'(<c r="([A-Z]+[0-9]+)"[^>]*><f>[^<]*</f>)(?:<v\s*/>|<v></v>)')
    ven = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zz, \
            zipfile.ZipFile(ven, 'w', zipfile.ZIP_DEFLATED) as zo:
        for info in zz.infolist():
            obsah = zz.read(info)
            hodnoty = po_listech.get(info.filename)
            if hodnoty:
                obsah = vzor.sub(lambda x: x.group(1) + (
                    b'<v>%s</v>' % repr(float(hodnoty[x.group(2)])).encode()
                    if x.group(2) in hodnoty else b'<v></v>'), obsah)
            zo.writestr(info, obsah)
    return ven.getvalue()


def _nakup_uzavrene_mesice(rok):
    """Měsíce roku `rok`, které vkladatel výslovně uzavřel (app.storage — volat ve smyčce)."""
    return set(app.storage.general.get(_uzavrene_key(rok, _UZAVRENE_NAKUP_PREFIX)) or [])


def _nakup_report_neuplne(rok, mesic):
    """(měsíce 1…mesic-1 neuzavřené letos nebo loni, je loňský `mesic` neuzavřený).
    Součet za rok neuzavřené měsíce vynechá v obou letech, jinak by chybějící měsíc
    vypadal jako propad. Loňský rok bez záznamu v panelu je historie (jako v
    _nekompletni_klice), ten se bere celý."""
    ne = set(_nekompletni_klice(_UZAVRENE_NAKUP_PREFIX))
    vynechat = {m for m in range(1, mesic) if rok * 100 + m in ne or (rok - 1) * 100 + m in ne}
    return vynechat, (rok - 1) * 100 + mesic in ne


def _nakup_report_sql(sql, params=()):
    """INSERT/UPDATE/DELETE nad lupa_nakup_report; vrací počet dotčených řádků."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    except Exception as e:
        print(f'[lupa] report nákupčím (DB): {e}')
        return 0
    finally:
        if cur:
            cur.close()
        conn.close()


def _nakup_report_sesit(kod, rok, mesic, vynechat, nast):
    """({'mesic'|'rok': název listu}, bajty .xlsx) z vybraných listů a sloupců —
    None, když nákupčí nemá ve vybraných listech ani korunu."""
    mes = _MESICE_CELE[mesic - 1]
    obdobi = {'mesic': (f'{mes.capitalize()} {rok}', [mesic]),
              'rok': (f'Rok {rok} (leden–{mes})' if mesic > 1 else f'Rok {rok} (leden)',
                      [m for m in range(1, mesic + 1) if m not in vynechat])}
    s_celkem = not _NAKUP_REPORT_CELKEM.isdisjoint(nast['sloupce'])
    listy = []
    for klic in nast['listy']:
        nazev, mesice = obdobi[klic]
        rows = _nakup_report_data(kod, rok, mesice)
        cols = _nakup_report_cols(rok, nast['sloupce'], kategorie=any(
            r['kategorie'] != 'Bez kategorie' for r in rows))
        listy.append((klic, nazev, cols, rows, s_celkem))
    if not any(l[3] for l in listy):
        return None
    return {k: obdobi[k][0] for k in nast['listy']}, _nakup_report_xlsx(kod, listy)


def _nakup_report_text(kod, rok, mesic, nazvy, nast, vynechat, loni_neuplny):
    def rozsah(r):
        return f'{mesic}/{r}' if mesic == 1 else f'1–{mesic}/{r}'
    radky = ['Dobrý den,', '',
             f'v příloze je přehled prodejů zboží nákupčího {kod} ve srovnání '
             f's rokem {rok - 1}:']
    if 'mesic' in nazvy:
        radky.append(f'• list „{nazvy["mesic"]}“: {mesic}/{rok} proti {mesic}/{rok - 1}')
    if 'rok' in nazvy:
        radky.append(f'• list „{nazvy["rok"]}“: {rozsah(rok)} proti {rozsah(rok - 1)}')
    soucet = ('; vedle tabulky jsou součty po kategoriích (Skupina 2 popis), pod ní '
              'celkový součet nákupčího' if not _NAKUP_REPORT_CELKEM.isdisjoint(nast['sloupce']) else '')
    radky += ['', 'Řádky jsou po dodavatelích a popisu sortimentu, seřazené od největšího '
                  f'prodeje v roce {rok}{soucet}. '
                  'Prodej je obrat podle dat GIST v Kč bez DPH a v MJ.']
    if vynechat and 'rok' in nazvy:
        cisla = ', '.join(str(m) for m in sorted(vynechat))
        kdo = f'měsíc {cisla} (v obou letech) – jeho' if len(vynechat) == 1 else \
            f'měsíce {cisla} (v obou letech) – jejich'
        radky += ['', f'V listu „{nazvy["rok"]}“ chybí {kdo} data letos nebo loni nejsou '
                      'uzavřená a srovnání by nesedělo.']
    if loni_neuplny:
        radky += ['', f'Pozor: data za {mesic}/{rok - 1} nejsou uzavřená, srovnání '
                      's loňskem proto může být neúplné.']
    if intranet_data.APP_URL:
        radky += ['', f'Podrobnosti najdete v modulu Lupopřípad: {intranet_data.APP_URL}/lupa']
    radky += ['', f'Tento e-mail se posílá automaticky každý měsíc k {_NAKUP_REPORT_DEN}. dni.']
    predmet = f'Lupou na nákup – prodeje {kod} za {_MESICE_CELE[mesic - 1]} {rok}'
    return predmet, '\n'.join(radky)


def _nakup_report_odesli(kod, rok, mesic, vynechat, loni_neuplny, nast):
    """Report jedné zkratky. Záznam v lupa_nakup_report si zamluví dřív, než cokoli
    pošle (druhá instance ani další kolo ho pak nezopakují); když e-mail neodejde
    nikomu, záznam uvolní a příští kolo to zkusí znovu."""
    import intranet_emaily
    prijemci = intranet_data.ziskej_emaily_s_pravem(NAKUP_PREFIX + kod.lower())
    if not prijemci:
        return        # zkratku zatím nikdo nemá — příští kolo (právo mohlo přibýt)
    obdobi = f'{rok}-{mesic:02d}'
    if _nakup_report_sql('INSERT IGNORE INTO lupa_nakup_report (obdobi, kod) VALUES (%s, %s)',
                         (obdobi, kod)) != 1:
        return
    stav = None       # None = uvolnit záznam, jinak co zapsat do `prijemci`
    try:
        sesit = _nakup_report_sesit(kod, rok, mesic, vynechat, nast)
        if sesit is None:
            stav = '(bez dat)'
            return
        nazvy, data = sesit
        predmet, text = _nakup_report_text(kod, rok, mesic, nazvy, nast,
                                           vynechat, loni_neuplny)
        priloha = [(f'Lupou_na_nakup_{kod}_{obdobi}.xlsx', data)]
        ok = [e for e in prijemci
              if intranet_emaily.odesli_upozorneni_email(e, predmet, text, prilohy=priloha)]
        if ok:
            stav = ', '.join(ok)
            intranet_logger.log_activity('Systém', 'Lupou na nákup',
                                         f'Měsíční report {kod} za {mesic}/{rok} → {stav}')
    finally:
        if stav is None:
            _nakup_report_sql('DELETE FROM lupa_nakup_report WHERE obdobi = %s AND kod = %s',
                              (obdobi, kod))
        else:
            _nakup_report_sql('UPDATE lupa_nakup_report SET prijemci = %s '
                              'WHERE obdobi = %s AND kod = %s', (stav, obdobi, kod))


def _nakup_report_ceka(obdobi, duvod):
    if (obdobi, duvod) not in _NAKUP_REPORT_CEKA:
        _NAKUP_REPORT_CEKA.add((obdobi, duvod))
        print(f'[lupa] měsíční report nákupčím za {obdobi} čeká: {duvod}')


async def _nakup_report_kolo(obd):
    """Jedno kolo pro měsíc `obd` (yyyymm): pošle zkratkám, kterým ještě neodešel."""
    import intranet_prava
    nastaveni = await asyncio.to_thread(intranet_data.nacti_nastaveni_intranetu)
    if not nastaveni.get('lupa_zapnuty', True):
        return
    if not _nakup_report_nastaveni(nastaveni)['zapnuto']:
        return
    if not (await asyncio.to_thread(intranet_data.nacti_smtp)).get('enabled'):
        return
    rok, mesic = divmod(obd, 100)
    obdobi = f'{rok}-{mesic:02d}'
    # Čeká na výslovné uzavření: rok bez záznamu v panelu bere _nekompletni_klice jako
    # hotovou historii, takže by leden odešel s rozpracovanými daty.
    if mesic not in _nakup_uzavrene_mesice(rok):
        _nakup_report_ceka(obdobi, f'{mesic}/{rok} ještě není uzavřený v panelu '
                                   'uzavřených měsíců')
        return
    await asyncio.to_thread(inicializace_db)
    # Zámluvu, kterou nedokončil pád / restart uprostřed odesílání, po hodině uvolní.
    await asyncio.to_thread(
        _nakup_report_sql, 'DELETE FROM lupa_nakup_report WHERE obdobi = %s AND '
        'prijemci IS NULL AND odeslano < NOW() - INTERVAL 1 HOUR', (obdobi,))
    hotovo = {r[0] for r in await asyncio.to_thread(
        _dotaz, 'SELECT kod FROM lupa_nakup_report WHERE obdobi = %s', (obdobi,))}
    zbyva = [k for k in intranet_prava.LUPA_NAKUP_KODY if k not in hotovo]
    if not zbyva:
        return
    if not await asyncio.to_thread(
            _dotaz, 'SELECT 1 FROM lupa_nakup_obrat WHERE rok = %s AND mesic = %s LIMIT 1',
            (rok, mesic)):
        _nakup_report_ceka(obdobi, f'data GIST za {mesic}/{rok} ještě nejsou nahraná')
        return
    # Zařazení zboží k nákupčím (i popis sortimentu) podle posledního DATA_POROVNANI.
    if not await asyncio.to_thread(nakup_doparuj_je_li_treba):
        _nakup_report_ceka(obdobi, 'dopárování zboží s DATA_POROVNANI neproběhlo')
        return
    vynechat, loni_neuplny = _nakup_report_neuplne(rok, mesic)
    for kod in zbyva:
        # Čte se před každou zkratkou: vypnutí zastaví i rozběhnuté kolo.
        nast = await asyncio.to_thread(_nakup_report_nastaveni)
        if not nast['zapnuto']:
            return
        try:
            await asyncio.to_thread(_nakup_report_odesli, kod, rok, mesic,
                                    vynechat, loni_neuplny, nast)
        except Exception as e:
            print(f'[lupa] měsíční report {kod} za {obdobi}: {e}')


async def bg_nakup_report():
    """Od 10. dne v měsíci (od 7:00) pošle každému nákupčímu Excel s prodeji jeho
    zboží za předchozí měsíc (list 1) a od začátku roku do toho měsíce (list 2),
    obojí proti stejnému období loni. Jen když ho Správce v sekci zapne (výchozí
    vypnuto); listy a sloupce podle jeho výběru.

    Co odešlo, eviduje lupa_nakup_report, takže restart nic nezopakuje. Dokud data
    za měsíc nejsou nahraná a uzavřená, čeká a zkouší to každou půlhodinu."""
    await asyncio.sleep(300)
    while True:
        try:
            nyni = datetime.datetime.now()
            if nyni.day >= _NAKUP_REPORT_DEN and nyni.hour >= _NAKUP_REPORT_HODINA:
                await _nakup_report_kolo(_nakup_posun(nyni.year * 100 + nyni.month, -1))
        except Exception as e:
            print(f'[lupa] měsíční report nákupčím: {e}')
        await asyncio.sleep(1800)


async def _nakup_report_vyber(zapinam):
    """Dialog výběru listů a sloupců. Vrátí (listy, sloupce), 'vypnout' (nabízí se,
    jen když posílání běží), nebo None po zrušení."""
    nast = _nakup_report_nastaveni()
    with ui.dialog() as dlg, ui.card().classes('w-full max-w-lg p-5 gap-2'):
        dlg.on('hide', dlg.delete)
        ui.label('Měsíční e-mail nákupčím').classes('text-lg font-bold text-gray-800')
        ui.label(f'Od {_NAKUP_REPORT_DEN}. dne v měsíci dostane každý nákupčí Excel s prodeji '
                 'svého zboží za předchozí měsíc proti stejnému období loni — jakmile je '
                 'měsíc uzavřený a data GIST nahraná.').classes('text-sm text-gray-600')
        ui.label('Listy v Excelu').classes('text-sm font-semibold text-gray-800 mt-2')
        ch_listy = {k: ui.checkbox(nazev, value=k in nast['listy']).props('dense')
                    for k, nazev in _NAKUP_REPORT_LISTY.items()}
        ui.label('Sloupce').classes('text-sm font-semibold text-gray-800 mt-2')
        ui.label('Kód a název dodavatele, popis sortimentu a kategorie jsou vždy. Součty po '
                 'kategoriích (vedle tabulky) a celkem pod ní jsou jen u korunových sloupců.'
                 ).classes('text-xs text-gray-500')
        with ui.grid(columns=2).classes('w-full gap-x-4 gap-y-0'):
            ch_sloupce = {p: ui.checkbox(_nakup_report_volba(p), value=p in nast['sloupce'])
                          .props('dense') for p, *_ in _NAKUP_REPORT_SLOUPCE}
        nyni = datetime.datetime.now()
        if zapinam and (nyni.day, nyni.hour) >= (_NAKUP_REPORT_DEN, _NAKUP_REPORT_HODINA):
            rok, mesic = divmod(_nakup_posun(nyni.year * 100 + nyni.month, -1), 100)
            ui.label(f'Report za {_MESICE_CELE[mesic - 1]} {rok} pak odejde nákupčím, kterým '
                     'ještě neodešel, nejpozději do půl hodiny.') \
                .classes('text-xs text-amber-700 mt-2')
        if not intranet_data.nacti_smtp().get('enabled'):
            ui.label('Odesílání e-mailů z portálu je teď vypnuté (SMTP) — report odejde, '
                     'až se zapne.').classes('text-xs text-amber-700 mt-2')

        def potvrdit():
            listy = [k for k, ch in ch_listy.items() if ch.value]
            sloupce = [p for p, ch in ch_sloupce.items() if ch.value]
            if not listy or not sloupce:
                ui.notify('Vyberte aspoň jeden list a jeden sloupec.', type='warning')
                return
            dlg.submit((listy, sloupce))

        with ui.row().classes('w-full items-center gap-2 mt-3'):
            if not zapinam:
                ui.button('Vypnout posílání', icon='unsubscribe',
                          on_click=lambda: dlg.submit('vypnout')) \
                    .props('flat no-caps color=red-7')
            ui.space()
            ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
            ui.button('Zapnout posílání' if zapinam else 'Uložit',
                      icon='forward_to_inbox' if zapinam else 'save', on_click=potvrdit) \
                .props('unelevated no-caps color=green-7')
    return await dlg


_PRESKRTNUTI = ('position:absolute;left:50%;top:50%;width:2.5px;height:120%;'
                'background:currentColor;border-radius:2px;box-shadow:0 0 0 1.5px #fff;'
                'transform:translate(-50%,-50%) rotate(-45deg);pointer-events:none')


def _nakup_report_obalka(user_id, user_name):
    """Obálka vpravo v hlavičce Lupou na nákup (jen Správce): červeně přeškrtnutá =
    měsíční e-mail nákupčím vypnutý (výchozí), zelená = zapnutý. Klik otevře výběr
    listů a sloupců — zapíná se, upravuje i vypíná v něm."""

    def uloz(zapnuto, vyber=(None, None)):
        _nakup_report_uloz(zapnuto, *vyber)
        nast = _nakup_report_nastaveni()
        txt = 'VYPNUTO'
        if zapnuto:
            txt = (f"ZAPNUTO · listy: {', '.join(_NAKUP_REPORT_LISTY[k] for k in nast['listy'])}"
                   f" · sloupce: {', '.join(_nakup_report_volba(p) for p in nast['sloupce'])}")
        zapis_log(user_id, user_name, 'nakup_report', '', txt)

    async def otevri():
        # Režim dialogu podle uloženého stavu, ne podle (možná zastaralé) obálky.
        zapnuto = _nakup_report_nastaveni()['zapnuto']
        vysledek = await _nakup_report_vyber(not zapnuto)
        if vysledek == 'vypnout':
            uloz(False)
            ui.notify('Měsíční e-mail nákupčím je vypnutý.', type='warning')
        elif vysledek:
            uloz(True, vysledek)
            ui.notify('Výběr uložen.' if zapnuto else 'Měsíční e-mail nákupčím je zapnutý.',
                      type='positive')
        obalka.refresh()

    @ui.refreshable
    def obalka():
        nast = _nakup_report_nastaveni()
        zap = nast['zapnuto']
        if zap:
            sloupce = ('všechny' if len(nast['sloupce']) == len(_NAKUP_REPORT_SLOUPCE)
                       else ', '.join(_nakup_report_volba(p) for p in nast['sloupce']))
            tip = (f"Měsíční e-mail nákupčím je zapnutý (od {_NAKUP_REPORT_DEN}. dne v měsíci) · "
                   f"listy: {', '.join(_NAKUP_REPORT_LISTY[k] for k in nast['listy'])} · "
                   f"sloupce: {sloupce}")
            if not intranet_data.nacti_smtp().get('enabled'):
                tip += ' · Odesílání e-mailů z portálu (SMTP) je teď vypnuté.'
            tip += ' — kliknutím upravíte nebo vypnete'
        else:
            tip = 'Měsíční e-mail nákupčím je vypnutý — kliknutím zapnete'
        with ui.button(on_click=_bez_klienta(otevri)).props('flat round') \
                .tooltip(tip):
            with ui.element('div').classes('relative flex ' +
                                           ('text-green-7' if zap else 'text-red-6')):
                ui.icon('mail', size='1.6rem')
                if not zap:
                    ui.element('div').style(_PRESKRTNUTI)

    obalka()


# ── Výstupy: grid, grafy, PDF / tisk ─────────────────────────────────────────
def _nakup_coldefs(cols):
    defs = []
    for nazev, field, typ, sirka in cols:
        if typ == 'text':
            # Minimum podle Excel šířky: „2026M10“, „Cash & Carry“ i IČO se vejdou celé.
            defs.append({'headerName': nazev, 'field': field,
                         'minWidth': min(230, max(100, sirka * 6 + 36)),
                         'flex': max(1, sirka // 8)})
            continue
        desetin = 0 if typ == 'int' else (1 if field in ('zmena', 'zmena_mj', 'podil') else 2)
        c = {'headerName': nazev, 'field': field, 'type': 'numericColumn',
             'width': max(105, sirka * 9), ':valueFormatter': _fmt_js(desetin)}
        if field in ('zmena', 'zmena_mj'):
            c[':cellStyle'] = _STYL_ZMENA
        defs.append(c)
    return defs


def _nakup_kratky(r, n=40):
    t = str(r.get('nazev') or r.get('klic') or '')
    return t if len(t) <= n else t[:n - 1] + '…'


def _nakup_zmena_txt(z):
    return '—' if z is None else f'{z:+.1f} %'.replace('.', ',')


def _nakup_graf_top(d, n=15):
    """TOP n podle odběrů v A; vedle sloupec B. Vodorovně — názvy jsou dlouhé."""
    popisek, gen = _NAKUP_ROZPADY[d['rozpad']][:2]
    top = [r for r in d['rows'] if r['kc_a'] > 0][:n]
    top.reverse()                                    # největší nahoře
    opt = _graf_zaklad(f'TOP {len(top)} {gen} podle odběrů',
                       podnadpis=f"Kč bez DPH · tmavě {d['a_txt']}, světle {d['b_txt']}")
    opt['tooltip'] = {'trigger': 'axis', 'axisPointer': {'type': 'shadow'}}
    opt['grid'] = {'top': 88, 'left': 10, 'right': 30, 'bottom': 16, 'containLabel': True}
    opt['xAxis'] = {'type': 'value'}
    opt['yAxis'] = {'type': 'category', 'data': [_nakup_kratky(r, 34) for r in top],
                    'axisLabel': {'fontSize': 11}}
    opt['series'] = [
        {'name': d['a_txt'], 'type': 'bar', 'itemStyle': {'color': '#4338CA'},
         'data': [round(r['kc_a']) for r in top]},
        {'name': d['b_txt'], 'type': 'bar', 'itemStyle': {'color': '#A5B4FC'},
         'data': [round(r['kc_b']) for r in top]},
    ]
    return opt


def _nakup_graf_mesice(mesice, nadpis):
    opt = _graf_yoy_option(mesice)
    opt['title']['text'] = nadpis
    opt['title']['subtext'] = 'Sloupec = měsíc, barva = rok — stejný měsíc vedle sebe rok po roce.'
    return opt


def _nakup_tabulka_html(cols, rows, total=None, husta=False):
    e = html.escape
    des = {'int': 0, 'num': 1, 'money': 2}

    def bunka(r, field, typ):
        v = r.get(field)
        if typ in des:
            return f'<td class="r">{_cz(v, des[typ]) if v is not None else "–"}</td>'
        return f'<td>{e(str(v if v is not None else ""))}</td>'

    hlav = ''.join(f'<th>{e(n)}</th>' for n, _f, _t, _w in cols)
    telo = ''.join('<tr>' + ''.join(bunka(r, f, t) for _n, f, t, _w in cols) + '</tr>'
                   for r in rows)
    if total:
        telo += ('<tr class="celkem">' + ''.join(bunka(total, f, t) for _n, f, t, _w in cols)
                 + '</tr>')
    return (f'<table class="t{" husta" if husta else ""}"><thead><tr>' + hlav
            + '</tr></thead><tbody>' + telo + '</tbody></table>')


def _nakup_doc(nadpis, popis, uzivatel, telo, radku=None):
    e = html.escape
    meta = (f'<b>Vytiskl:</b> {e(uzivatel or "")}<br>'
            f'<b>Datum:</b> {datetime.datetime.now():%d.%m.%Y %H:%M}'
            + (f'<br><b>Řádků:</b> {radku}' if radku is not None else ''))
    return ('<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
            f'<title>Lupou na nákup – {e(nadpis)}</title>{_PDF_CSS}</head><body>'
            '<div class="lupa-doc"><div class="hdr"><div>'
            f'<h1>Lupou na nákup — {e(nadpis)}</h1>'
            '<div class="firma">JIP východočeská, a.s.</div>'
            f'</div><div class="meta">{meta}</div></div>'
            f'<div class="filtr">{e(popis or "")}</div>' + telo +
            '<div class="pozn">Odběry bez DPH podle dat GIST. Zkratka nákupčího a dodavatel '
            'jsou dopárované podle kódu produktu z DATA_POROVNANI.</div>'
            '</div></body></html>')


def _nakup_vyjezd_html(stav, uzivatel):
    # Kód dodavatele vedle názvu by A4 na výšku rozbil — v PDF stačí název.
    sloupce = stav['cols']
    s_nazvem = any(c[1] == 'dodavatel_nazev' for c in sloupce)
    cols = [c for c in sloupce if not (c[1] == 'dodavatel' and s_nazvem)]
    return _nakup_doc('výjezd', stav['popis'], uzivatel,
                      _nakup_tabulka_html(cols, stav['rows'], stav['total']),
                      radku=len(stav['rows']))


def _nakup_srovnani_html(d, popis, uzivatel):
    e = html.escape
    popisek, gen = _NAKUP_ROZPADY[d['rozpad']][:2]
    k = d['celkem']
    souhrn = (f'<div class="filtr"><b>Odběry {e(d["a_txt"])}:</b> {_cz(k["kc_a"], 0)} Kč · '
              f'<b>{e(d["b_txt"])}:</b> {_cz(k["kc_b"], 0)} Kč · '
              f'<b>Změna:</b> {_nakup_zmena_txt(k["zmena"])} · '
              f'<b>{e(popisek)} s odběrem:</b> {d["aktivnich"]}</div>')
    top = [{'nazev': _nakup_kratky(r), 'kod': r['klic'], 'kc': r['kc_a']} for r in d['rows']]
    graf_top = _svg_graf_polozky(top, 15).replace(
        'položek podle obratu', f'{gen} podle odběrů {d["a_txt"]}')
    graf_mesice = (_svg_graf('Odběry po měsících (Kč bez DPH, meziroční srovnání)',
                             'Sloupec = měsíc, barva = rok.', _MESICE_KR,
                             _serie_yoy(d['mesice']), typ='bar') if d['mesice'] else '')
    rows = d['rows'][:_NAKUP_PDF_RADKU]
    vic = (f'<div class="pozn">Zobrazeno prvních {len(rows)} z {len(d["rows"])} řádků — '
           'úplný seznam je v Excelu.</div>') if len(d['rows']) > len(rows) else ''
    # A4 na výšku: bez MJ obou období; i tak ~9 číselných sloupců → hustá tabulka
    cols = [c for c in d['cols'] if c[1] not in ('mj_a', 'mj_b')]
    telo = (souhrn + graf_top + graf_mesice + f'<h2>Srovnání — {e(popisek.lower())}</h2>'
            + _nakup_tabulka_html(cols, rows, d['celkem'], husta=True) + vic)
    return _nakup_doc(f'srovnání ({popisek.lower()})', popis, uzivatel, telo,
                      radku=len(d['rows']))


def _nakup_tisk(html_text):
    """Tisk přes skrytý iframe: prohlížeč otevře svůj dialog, nic se nestahuje."""
    import json
    ui.run_javascript(
        '(() => { const f = document.createElement("iframe");'
        ' f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";'
        f' f.srcdoc = {json.dumps(html_text)};'
        ' f.onload = () => { f.contentWindow.focus(); f.contentWindow.print();'
        ' setTimeout(() => f.remove(), 60000); };'
        ' document.body.appendChild(f); })()')


def _nakup_jmeno(co):
    return f'lupa_nakup_{co}_{datetime.datetime.now():%Y%m%d_%H%M}'


def _nakup_multi(label, volby, hodnota, sirka):
    val = [v for v in (hodnota or []) if v in volby]
    return ui.select(volby, label=label, multiple=True, with_input=True, clearable=True,
                     value=val) \
        .props('dense outlined options-dense use-chips') \
        .style(f'min-width: {sirka}px; max-width: 460px')


def _nakup_kpi(popisek, hodnota, barva='text-gray-800'):
    with ui.card().classes('p-3 min-w-[190px] shadow-sm border border-gray-100 rounded-xl'):
        ui.label(popisek).classes('text-xs text-gray-500')
        ui.label(hodnota).classes(f'text-xl font-bold {barva}')


def _nakup_barva_zmeny(z):
    return 'text-gray-500' if z is None else ('text-green-700' if z >= 0 else 'text-red-600')


# ── UI ──────────────────────────────────────────────────────────────────────
def _dlazdice_nakup(stav_klic, refreshable):
    async def otevri():
        await _prepni_pohled(stav_klic, NAKUP_POHLED, refreshable, 'Načítám Lupou na nákup…')

    _dlazdice_sekce('🛒', 'Lupou na nákup', 'border-emerald-200', otevri)


def _vykresli_nakup(user_id, user_name, vsechna_prava):
    """Sekce „Lupou na nákup“: panel uzavřených měsíců + výjezdy a srovnání.
    Vkladatel bez nákupních práv sem chodí jen označit uzavřené měsíce."""
    kody = nakup_kody(vsechna_prava)
    vidi = kody is None or bool(kody)
    nakup_doparuj_je_li_treba()
    mesice = _nakup_mesice()

    @ui.refreshable
    def zalozky(volby):
        # Překresluje je i panel: po uzavření měsíce se mění šedé měsíce a výchozí
        # období ve filtrech. Volby se předají znovu stejné — DISTINCTy jsou drahé.
        with ui.tabs().props('align=left active-color=primary indicator-color=primary') \
                .classes('w-full border-b border-gray-200') as taby:
            ui.tab('vyjezdy', label='📄 Výjezdy')
            ui.tab('srovnani', label='📊 Srovnání dodavatelů')
        with ui.tab_panels(taby, value='vyjezdy').classes('w-full'):
            with ui.tab_panel('vyjezdy').classes('px-0'):
                _nakup_vyjezdy(user_id, user_name, kody, mesice, volby)
            with ui.tab_panel('srovnani').classes('px-0'):
                _nakup_srovnani(user_id, user_name, kody, mesice, volby)

    if mesice and vidi:
        rozsah = ('všechny nákupčí včetně nespárovaného zboží' if kody is None
                  else 'zboží nákupčích ' + ', '.join(sorted(kody)))
        ui.label(f'Odběry z GIST {mesice[0]} – {mesice[-1]} · vidíte {rozsah}.') \
            .classes('text-gray-500 mb-3')
        if kody is None:
            r = _dotaz('SELECT SUM(o.obrat_kc), SUM(CASE WHEN p.dodavatel IS NULL '
                       f'THEN o.obrat_kc ELSE 0 END) {_NAKUP_JOIN}')
            if r and r[0][0] and float(r[0][1] or 0) > 0.05 * float(r[0][0]):
                ui.label(f'Bez dodavatele je {_cz(float(r[0][1]) / float(r[0][0]) * 100, 1)} % '
                         'odběrů. Dodavatel se páruje ze sloupců K/L DATA_POROVNANI — nahrajte je '
                         'v Cenopřípadu znovu.').classes('text-sm text-amber-700 mb-3')
    _vykresli_uzavrene_mesice(user_name, je_vkladatel(vsechna_prava),
                              _UZAVRENE_NAKUP_PREFIX, 'odběry GIST', zalozky.refresh)
    if not vidi:
        ui.label('Odběry vidí jen nákupčí se svou zkratkou a Správce. '
                 'Vy tu označujete uzavřené měsíce.').classes('text-gray-400')
        return
    if not mesice:
        with ui.column().classes('items-center py-16 gap-3 w-full'):
            ui.icon('shopping_cart', size='5rem', color='grey-3')
            ui.label('Zatím žádná data').classes('text-xl font-semibold text-gray-400')
            ui.label('Data GIST „Lupou na nákup“ nahrává vkladatel v dlaždici Import dat.') \
                .classes('text-gray-400')
        return
    zalozky(_nakup_volby(kody))


def _nakup_vyjezdy(user_id, user_name, kody, mesice, volby):
    filtr_klic = f'lupa_nakup_vyjezd_{user_id}'
    ulozeny = app.storage.user.get(filtr_klic) or {}
    stav = {'rows': [], 'cols': [], 'total': {}, 'celkem_radku': 0, 'filtr': None, 'popis': ''}

    hotove = _nakup_hotove(mesice)
    posl = hotove[-1] if hotove else None

    def mesic(klic, vychozi):
        v = ulozeny.get(klic)
        return v if v in hotove else vychozi

    dims = dict(_NAKUP_DIMENZE)
    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_od = _nakup_sel('Od měsíce', mesice, mesic('od', posl))
        sel_do = _nakup_sel('Do měsíce', mesice, mesic('do', posl))
        sel_div = _nakup_multi('Divize', volby['divize'], ulozeny.get('divize'), 170)
        sel_pob = _nakup_multi('Pobočka', volby['pobocka'], ulozeny.get('pobocka'), 220)
        sel_ico = _nakup_multi('Odběratel (IČO, jméno)', volby['ico'], ulozeny.get('ico'), 260)
    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_dod = _nakup_multi('Dodavatel', volby['dodavatel'], ulozeny.get('dodavatel'), 300)
        sel_nak = _nakup_multi('Nákupčí', volby['nak'], ulozeny.get('nak'), 140)
        inp_prod = ui.input('Produkt — kód nebo část názvu', value=ulozeny.get('produkt') or '') \
            .props('dense outlined clearable').style('min-width: 260px') \
            .tooltip('Víc kódů nebo názvů oddělte čárkou.')
        sel_dims = ui.select(dims, label='Seskupit podle', multiple=True,
                             value=[d for d in (ulozeny.get('dims') or _NAKUP_DIMENZE_VYCHOZI)
                                    if d in dims]) \
            .props('dense outlined options-dense use-chips').style('min-width: 340px') \
            .tooltip('Řádek výjezdu = kombinace zvolených údajů. Všechny = zrnitost GIST.')
    with ui.row().classes('w-full items-center gap-2 mb-3 flex-wrap'):
        btn_nacti = ui.button('Generovat výjezd', icon='play_arrow') \
            .props('unelevated color=indigo-7')
        btn_tisk = ui.button('Tisk', icon='print').props('outline color=indigo-7')
        btn_pdf = ui.button('PDF', icon='picture_as_pdf').props('outline color=red-7')
        btn_xlsx = ui.button('Excel', icon='table_view').props('outline color=green-7') \
            .tooltip('Celý výjezd bez omezení počtu řádků.')
        info = ui.label('').classes('text-sm text-gray-500 ml-2')
    exporty = (btn_tisk, btn_pdf, btn_xlsx)
    for b in exporty:
        b.disable()
    vysledek = ui.column().classes('w-full')
    with vysledek:
        ui.label('Zvolte filtr a seskupení a klikněte na Generovat výjezd.') \
            .classes('text-sm text-gray-400')

    def soucasny_filtr():
        return {'od': sel_od.value, 'do': sel_do.value,
                'divize': list(sel_div.value or []), 'pobocka': list(sel_pob.value or []),
                'ico': list(sel_ico.value or []), 'dodavatel': list(sel_dod.value or []),
                'nak': list(sel_nak.value or []), 'produkt': (inp_prod.value or '').strip(),
                'dims': list(sel_dims.value or [])}

    def vykresli():
        vysledek.clear()
        rows = stav['rows']
        for b in exporty:
            b.set_enabled(bool(rows))
        info.text = (f"{stav['celkem_radku']:,} řádků · ".replace(',', ' ')
                     + _kc(float(stav['total'].get('kc') or 0)))
        with vysledek:
            if not rows:
                with ui.column().classes('items-center py-16 gap-2 w-full'):
                    ui.icon('search_off', size='4rem', color='grey-3')
                    ui.label('Filtru nic neodpovídá').classes('text-lg font-semibold text-gray-400')
                return
            if stav['celkem_radku'] > len(rows):
                ui.label(f"Náhled ukazuje prvních {len(rows):,} z {stav['celkem_radku']:,} "
                         'řádků — Excel obsahuje vše.'.replace(',', ' ')) \
                    .classes('text-xs text-amber-700 mb-1')
            _grid({**_GRID_ZAKLAD, 'columnDefs': _nakup_coldefs(stav['cols']), 'rowData': rows,
                   'pinnedBottomRowData': [stav['total']]}, filtry=True) \
                .classes('w-full').style('height: 65vh')

    async def nacti():
        f = soucasny_filtr()
        if not f['dims']:
            ui.notify('Vyberte aspoň jeden údaj v „Seskupit podle“.', type='warning')
            return
        app.storage.user[filtr_klic] = f
        dlg, _kruh, _popisek = prekryv_kolecko('Generuji výjezd…', procenta=False)
        dlg.open()
        try:
            data = await asyncio.to_thread(_nakup_vyjezd_data, f, kody, _STROP_NAHLED)
        except Exception as e:
            print(f'[lupa] výjezd nákupu: {e}')
            ui.notify(f'Výjezd se nepodařilo načíst: {e}', type='negative', multi_line=True)
            return
        finally:
            _tise(dlg.close)
        stav.update(data)
        stav['filtr'] = f
        stav['popis'] = _nakup_popis(f, volby, kody)
        vykresli()
        await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_nahled', '',
                                stav['popis'], stav['celkem_radku'])

    def vejde_se_do_pdf():
        if stav['celkem_radku'] > _STROP_PDF:
            ui.notify(f"Tisk a PDF zvládnou {_STROP_PDF} řádků, výjezd jich má "
                      f"{stav['celkem_radku']}. Zpřesněte filtr nebo seskupení, "
                      'případně použijte Excel.', type='warning', multi_line=True)
            return False
        return True

    async def tisk():
        if not stav['rows'] or not vejde_se_do_pdf():
            return
        _nakup_tisk(_nakup_vyjezd_html(stav, user_name))
        await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_tisk', '',
                                stav['popis'], len(stav['rows']))

    async def export_pdf():
        if not stav['rows'] or not vejde_se_do_pdf():
            return
        jmeno = _nakup_jmeno('vyjezd') + '.pdf'
        pozn = ui.notification('Připravuji PDF…', spinner=True, timeout=None)
        try:
            data = await _render_pdf(_nakup_vyjezd_html(stav, user_name), jmeno, pozn)
            _stahni_bajty(data, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_export_pdf', '',
                                    stav['popis'], len(stav['rows']))
        except Exception as e:
            ui.notify(f'PDF se nepodařilo vytvořit: {e}', type='negative', multi_line=True,
                      timeout=15000)
        finally:
            pozn.dismiss()

    async def export_xlsx():
        if not stav['rows']:
            return
        jmeno = _nakup_jmeno('vyjezd') + '.xlsx'
        cesta = _tmp_cesta(jmeno)
        pozn = ui.notification('Sestavuji Excel…', spinner=True, timeout=None)
        try:
            pocet = await asyncio.to_thread(_nakup_vyjezd_xlsx, stav['filtr'], kody, cesta,
                                            stav['total'])
            _stahni_soubor(cesta, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_export_xlsx', '',
                                    stav['popis'], pocet)
        except Exception as e:
            ui.notify(f'Export selhal: {e}', type='negative', multi_line=True)
        finally:
            pozn.dismiss()

    btn_nacti.on_click(_bez_klienta(nacti))
    btn_tisk.on_click(_bez_klienta(tisk))
    btn_pdf.on_click(_bez_klienta(export_pdf))
    btn_xlsx.on_click(_bez_klienta(export_xlsx))


def _nakup_srovnani(user_id, user_name, kody, mesice, volby):
    filtr_klic = f'lupa_nakup_srovnani_{user_id}'
    ulozeny = app.storage.user.get(filtr_klic) or {}
    hotove = _nakup_hotove(mesice)
    # Výchozí A = od ledna do posledního uzavřeného měsíce. Když není uzavřený žádný,
    # počítá se s posledním nahraným — srovnání pak ukáže jen B, ale nespadne.
    posl = (hotove or mesice)[-1]
    posledni = _obd_int(posl)
    od_vychozi = next((m for m in hotove if m[:4] == posl[:4]), posl)
    prvni = hotove[0] if hotove else None
    stav = {'data': None, 'filtr': None, 'popis': ''}

    def mesic(klic, vychozi):
        v = ulozeny.get(klic)
        return v if v in hotove else vychozi

    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_od = _nakup_sel('Období od', mesice, mesic('od', od_vychozi))
        sel_do = _nakup_sel('do', mesice, mesic('do', posl))
        ui.label('Srovnat:').classes('text-sm text-gray-600 self-center ml-2')
        tgl = ui.toggle({'rok': 'Rok/rok', 'mesic': 'Měsíc/měsíc', 'vlastni': 'Vlastní'},
                        value=ulozeny.get('srovnat') if ulozeny.get('srovnat') in
                        ('rok', 'mesic', 'vlastni') else 'rok') \
            .props('no-caps unelevated dense toggle-color=indigo-7').classes('self-center')
        sel_b_od = _nakup_sel('Srovnat od', mesice, mesic('b_od', prvni))
        sel_b_do = _nakup_sel('do', mesice, mesic('b_do', prvni))
        b_popis = ui.label('').classes('text-xs text-gray-500 self-center')
    with ui.row().classes('w-full items-end gap-3 mb-2 flex-wrap'):
        sel_div = _nakup_multi('Divize', volby['divize'], ulozeny.get('divize'), 170)
        sel_pob = _nakup_multi('Pobočka', volby['pobocka'], ulozeny.get('pobocka'), 220)
        sel_ico = _nakup_multi('Odběratel (IČO, jméno)', volby['ico'], ulozeny.get('ico'), 240)
        sel_dod = _nakup_multi('Dodavatel', volby['dodavatel'], ulozeny.get('dodavatel'), 280)
        sel_nak = _nakup_multi('Nákupčí', volby['nak'], ulozeny.get('nak'), 140)
        inp_prod = ui.input('Produkt — kód nebo část názvu', value=ulozeny.get('produkt') or '') \
            .props('dense outlined clearable').style('min-width: 240px')
    with ui.row().classes('w-full items-center gap-2 mb-3 flex-wrap'):
        sel_rozpad = ui.select({k: v[0] for k, v in _NAKUP_ROZPADY.items()}, label='Rozpad',
                               value=ulozeny.get('rozpad') if ulozeny.get('rozpad')
                               in _NAKUP_ROZPADY else 'dodavatel') \
            .props('dense outlined options-dense').style('min-width: 160px') \
            .tooltip('Co se proti sobě srovnává — výchozí jsou dodavatelé.')
        btn_nacti = ui.button('Srovnat', icon='insights').props('unelevated color=indigo-7')
        btn_tisk = ui.button('Tisk', icon='print').props('outline color=indigo-7')
        btn_pdf = ui.button('PDF', icon='picture_as_pdf').props('outline color=red-7')
        btn_xlsx = ui.button('Excel', icon='table_view').props('outline color=green-7')
    exporty = (btn_tisk, btn_pdf, btn_xlsx)
    for b in exporty:
        b.disable()
    vysledek = ui.column().classes('w-full')
    with vysledek:
        ui.label('Srovnání odběrů ve zvoleném období proti stejnému období loni (rok/rok), '
                 'proti předchozímu stejně dlouhému období (měsíc/měsíc) nebo vlastnímu.') \
            .classes('text-sm text-gray-400')

    def soucasny_filtr():
        return {'od': sel_od.value, 'do': sel_do.value, 'srovnat': tgl.value,
                'b_od': sel_b_od.value, 'b_do': sel_b_do.value,
                'divize': list(sel_div.value or []), 'pobocka': list(sel_pob.value or []),
                'ico': list(sel_ico.value or []), 'dodavatel': list(sel_dod.value or []),
                'nak': list(sel_nak.value or []), 'produkt': (inp_prod.value or '').strip(),
                'rozpad': sel_rozpad.value}

    def prepni_b():
        vlastni = tgl.value == 'vlastni'
        sel_b_od.set_visibility(vlastni)
        sel_b_do.set_visibility(vlastni)
        if vlastni:
            b_popis.text = ''
            return
        _a1, _a2, b1, b2 = _nakup_obdobi_ab(soucasny_filtr(), posledni)
        b_popis.text = f'→ srovnává se s {_nakup_rozsah_txt(b1, b2)}'

    for prvek in (tgl, sel_od, sel_do):
        prvek.on_value_change(lambda _e: prepni_b())
    prepni_b()

    def vykresli():
        d = stav['data']
        vysledek.clear()
        for b in exporty:
            b.set_enabled(bool(d and d['rows']))
        with vysledek:
            if not d or not d['rows']:
                with ui.column().classes('items-center py-16 gap-2 w-full'):
                    ui.icon('search_off', size='4rem', color='grey-3')
                    ui.label('Filtru nic neodpovídá').classes('text-lg font-semibold text-gray-400')
                return
            k = d['celkem']
            popisek = _NAKUP_ROZPADY[d['rozpad']][0]
            with ui.row().classes('w-full gap-3 mb-3 flex-wrap'):
                _nakup_kpi(f"Odběry {d['a_txt']}", _kc(k['kc_a']), 'text-indigo-800')
                _nakup_kpi(f"Odběry {d['b_txt']}", _kc(k['kc_b']), 'text-gray-600')
                _nakup_kpi('Změna', _nakup_zmena_txt(k['zmena']), _nakup_barva_zmeny(k['zmena']))
                _nakup_kpi(f'{popisek} s odběrem', f"{d['aktivnich']:,}".replace(',', ' '))
            with ui.row().classes('w-full gap-4 flex-wrap lg:flex-nowrap'):
                with ui.card().classes('w-full lg:w-1/2 p-2 shadow-sm'):
                    ui.echart(_nakup_graf_top(d)).classes('w-full').style('height: 440px')
                with ui.card().classes('w-full lg:w-1/2 p-2 shadow-sm'):
                    ui.echart(_nakup_graf_mesice(
                        d['mesice'], 'Odběry po měsících (Kč bez DPH, meziročně)')) \
                        .classes('w-full').style('height: 440px')
            ui.label('Klikněte na řádek — otevře se vývoj po měsících a rozpad.') \
                .classes('text-xs text-gray-400 mt-3 mb-1')
            if len(d['rows']) > _STROP_NAHLED:
                ui.label(f'Tabulka ukazuje prvních {_STROP_NAHLED} z {len(d["rows"])} řádků — '
                         'Excel obsahuje vše.').classes('text-xs text-amber-700 mb-1')
            grid = _grid({**_GRID_ZAKLAD, 'columnDefs': _nakup_coldefs(d['cols']),
                          'rowData': d['rows'][:_STROP_NAHLED],
                          'pinnedBottomRowData': [d['celkem']]}, filtry=True) \
                .classes('w-full').style('height: 60vh')
            grid.on('rowClicked', _bez_klienta_e(otevri_detail), ['data'])

    async def nacti():
        f = soucasny_filtr()
        app.storage.user[filtr_klic] = f
        dlg, _kruh, _popisek = prekryv_kolecko('Počítám srovnání…', procenta=False)
        dlg.open()
        try:
            d = await asyncio.to_thread(_nakup_srovnani_data, f, kody, f['rozpad'], posledni)
        finally:
            _tise(dlg.close)
        stav['data'], stav['filtr'] = d, f
        stav['popis'] = (_nakup_popis(f, volby, kody, s_dims=False)
                         + f" · Srovnání: {d['a_txt']} proti {d['b_txt']}"
                         + f" · Rozpad: {_NAKUP_ROZPADY[d['rozpad']][0].lower()}")
        vykresli()
        await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_srovnani', '',
                                stav['popis'], len(d['rows']))

    async def otevri_detail(e):
        radek = (e.args or {}).get('data') or {}
        d = stav['data']
        if not d or radek.get('_celkem') or 'hodnota' not in radek:
            return
        popisek, _gen, _k, _n, fklic, pod = _NAKUP_ROZPADY[d['rozpad']]
        surovy = radek.get('hodnota') or ''
        f2 = dict(stav['filtr'])
        if fklic == 'kody':
            f2['produkt'] = ''
        f2[fklic] = [surovy or (_BEZ if fklic in ('dodavatel', 'nak') else '')]
        cekani, _kruh, _popisek = prekryv_kolecko('Načítám detail…', procenta=False)
        cekani.open()
        try:
            det = await asyncio.to_thread(_nakup_srovnani_data, f2, kody, pod, posledni)
        finally:
            _tise(cekani.close)
        titulek = ' – '.join(str(x) for x in (radek.get('klic'), radek.get('nazev'))
                             if x and x != '—')
        k = det['celkem']
        with ui.dialog() as dlg, ui.card().classes('w-[95vw] max-w-6xl'):
            with ui.row().classes('w-full items-center justify-between no-wrap'):
                ui.label(f'{popisek}: {titulek}').classes('text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')
            with ui.row().classes('w-full gap-3 flex-wrap'):
                _nakup_kpi(f"Odběry {det['a_txt']}", _kc(k['kc_a']), 'text-indigo-800')
                _nakup_kpi(f"Odběry {det['b_txt']}", _kc(k['kc_b']), 'text-gray-600')
                _nakup_kpi('Změna', _nakup_zmena_txt(k['zmena']), _nakup_barva_zmeny(k['zmena']))
            ui.echart(_nakup_graf_mesice(det['mesice'], 'Odběry po měsících (Kč bez DPH)')) \
                .classes('w-full').style('height: 300px')
            ui.label(f'Rozpad: {_NAKUP_ROZPADY[pod][0].lower()}') \
                .classes('text-sm font-semibold text-gray-600 mt-2')
            _grid({**_GRID_ZAKLAD, 'columnDefs': _nakup_coldefs(det['cols']),
                   'rowData': det['rows'][:_STROP_NAHLED],
                   'pinnedBottomRowData': [det['celkem']]}) \
                .classes('w-full').style('height: 42vh')
        dlg.on('hide', dlg.delete)
        dlg.open()
        await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_detail', '',
                                f'{popisek}: {titulek} · {stav["popis"]}', len(det['rows']))

    async def tisk():
        d = stav['data']
        if not d or not d['rows']:
            return
        _nakup_tisk(_nakup_srovnani_html(d, stav['popis'], user_name))
        await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_srovnani_tisk', '',
                                stav['popis'], len(d['rows']))

    async def export_pdf():
        d = stav['data']
        if not d or not d['rows']:
            return
        jmeno = _nakup_jmeno(f"srovnani_{d['rozpad']}") + '.pdf'
        pozn = ui.notification('Připravuji PDF…', spinner=True, timeout=None)
        try:
            data = await _render_pdf(_nakup_srovnani_html(d, stav['popis'], user_name),
                                     jmeno, pozn)
            _stahni_bajty(data, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_srovnani_pdf', '',
                                    stav['popis'], len(d['rows']))
        except Exception as e:
            ui.notify(f'PDF se nepodařilo vytvořit: {e}', type='negative', multi_line=True,
                      timeout=15000)
        finally:
            pozn.dismiss()

    async def export_xlsx():
        d = stav['data']
        if not d or not d['rows']:
            return
        jmeno = _nakup_jmeno(f"srovnani_{d['rozpad']}") + '.xlsx'
        cesta = _tmp_cesta(jmeno)
        pozn = ui.notification('Sestavuji Excel…', spinner=True, timeout=None)
        try:
            mesicni = [{'mesic': _nakup_obd_txt(r['klic']), 'kc': r['kc']} for r in d['mesice']]
            await asyncio.to_thread(_xlsx_listy_na_disk, cesta, [
                ('Srovnání', d['cols'], d['rows'], d['celkem']),
                ('Po měsících', _NAKUP_COLS_MESICE, mesicni, None)])
            _stahni_soubor(cesta, jmeno)
            await asyncio.to_thread(zapis_log, user_id, user_name, 'nakup_srovnani_xlsx', '',
                                    stav['popis'], len(d['rows']))
        except Exception as e:
            ui.notify(f'Export selhal: {e}', type='negative', multi_line=True)
        finally:
            pozn.dismiss()

    btn_nacti.on_click(_bez_klienta(nacti))
    btn_tisk.on_click(_bez_klienta(tisk))
    btn_pdf.on_click(_bez_klienta(export_pdf))
    btn_xlsx.on_click(_bez_klienta(export_xlsx))


def _bez_klienta_e(fn):
    """_bez_klienta pro handlery s argumentem události (rowClicked)."""
    async def obal(e):
        try:
            await fn(e)
        except RuntimeError as chyba:
            if 'has been deleted' not in str(chyba):
                raise
            print(f'[lupa] {fn.__name__}: klient odešel před dokončením')
    return obal


def _nakup_obdobi_souhrn(obdobi):
    """['2025M01', …, '2026M10'] → '2025M01 – 2026M10 (22 měsíců)'."""
    if not obdobi:
        return ''
    if len(obdobi) <= 3:
        return ', '.join(obdobi)
    return f'{obdobi[0]} – {obdobi[-1]} ({len(obdobi)} měsíců)'
