# -*- coding: utf-8 -*-
"""Modul „Rezervace ind. schůzky s vedoucím".

Workflow:
  • Žadatel (právo `schuzky_zadatel`, typicky ASM) si na vypsaném termínu vybere
    začátek a délku schůzky (10–90 min po 10 v okně 9:00–17:00), uvede, zda chce
    zajistit ubytování, a odešle jednu společnou žádost. Vidí jen své žádosti.
  • Schůzka je vždy se všemi vedoucími najednou — vedoucí se nevybírá.
    Žádost přijde e-mailem všem vedoucím (právo `schuzky_vedouci`) a každý si
    v aplikaci odklikne, zda se účastní.
  • Termíny vypisuje a uzavírá vedoucí (`schuzky_vedouci`) i správce.
  • Správce (právo `schuzky_spravce`) vidí a může vše.

Stavy: ceka → potvrzeno | zamitnuto | zruseno (zrušil sám žadatel).
Stav řídí účast vedoucích, počítá se po každém kliknutí:
  • aspoň jedno ANO            → potvrzeno
  • NE od všech vedoucích      → zamitnuto (slot se uvolní)
  • jinak                      → ceka

Rezervovat lze nejpozději UZAVERKA_DNI dní před termínem; po uzávěrce se termín
v kalendáři jen prohlíží. Zrušit svou žádost jde jen déle než STORNO_DNI dní
před schůzkou.

Obsazenost drží už PODANÁ žádost — slot se zamkne okamžitě a další ASM na něj
nemůže. V jednom čase je v celé firmě jen jedna schůzka.

Vzor: intranet_vizitky.py (workflow žádostí + e-mailové notifikace).
"""

from nicegui import ui, app
import intranet_data
import intranet_logger
import intranet_emaily
import datetime
import asyncio


# =========================================================
# KONSTANTY
# =========================================================
PRAC_OD = datetime.time(9, 0)
PRAC_DO = datetime.time(17, 0)
KROK_MIN = 30                       # rastr rezervací (sloupce kalendáře i začátky)
DELKY_MIN = tuple(range(10, 100, 10))   # povolené délky schůzky: 10 … 90 min po 10


def _popis_delky(d: int) -> str:
    h, m = divmod(d, 60)
    if not h:
        return f'{m} minut'
    if not m:
        return '1 hodina'
    return '1,5 hodiny' if m == 30 else f'1 hodina {m} minut'


DELKA_POPIS = {d: _popis_delky(d) for d in DELKY_MIN}

# Termíny vypsané při první inicializaci. Další přidává výhradně správce v UI.
SEED_TERMINY = ('2026-09-15', '2026-10-20', '2026-11-17')

# Rezervovat lze nejpozději tolik dní před termínem (uzávěrka).
UZAVERKA_DNI = 10
# Zrušit žádost lze, jen pokud do termínu zbývá víc než tolik dní.
STORNO_DNI = 7

STAV_CEKA = 'ceka'
STAV_POTVRZENO = 'potvrzeno'
STAV_ZAMITNUTO = 'zamitnuto'
STAV_ZRUSENO = 'zruseno'

STAV_POPIS = {
    STAV_CEKA: ('Čeká na potvrzení', 'bg-amber-100 text-amber-800'),
    STAV_POTVRZENO: ('Potvrzeno', 'bg-green-100 text-green-800'),
    STAV_ZAMITNUTO: ('Zamítnuto', 'bg-red-100 text-red-800'),
    STAV_ZRUSENO: ('Zrušeno žadatelem', 'bg-gray-200 text-gray-600'),
}


# =========================================================
# POMOCNÉ FUNKCE
# =========================================================
def _s(v) -> str:
    return '' if v is None else str(v).strip()


def _minuty(t) -> int:
    """TIME z MySQL (timedelta) / datetime.time / 'HH:MM' → minuty od půlnoci."""
    if isinstance(t, datetime.timedelta):
        return int(t.total_seconds()) // 60
    if isinstance(t, datetime.time):
        return t.hour * 60 + t.minute
    if isinstance(t, str) and ':' in t:
        h, m = t.split(':')[:2]
        return int(h) * 60 + int(m)
    raise ValueError(f'nepodporovaný čas: {t!r}')


def _hhmm(minuty: int) -> str:
    return f'{minuty // 60:02d}:{minuty % 60:02d}'


def _sql_cas(minuty: int) -> str:
    return f'{_hhmm(minuty)}:00'


def _datum_cz(d) -> str:
    return d.strftime('%d.%m.%Y') if hasattr(d, 'strftime') else _s(d)


def _uzaverka(datum) -> datetime.date:
    """Poslední den, kdy jde na `datum` ještě rezervovat."""
    return datum - datetime.timedelta(days=UZAVERKA_DNI)


def _pred_uzaverkou(datum, dnes=None) -> bool:
    return (dnes or datetime.date.today()) <= _uzaverka(datum)


def _storno_do(datum) -> datetime.date:
    """Poslední den, kdy jde žádost ještě zrušit."""
    return datum - datetime.timedelta(days=STORNO_DNI + 1)


def _lze_stornovat(datum, dnes=None) -> bool:
    """Zrušit jde jen déle než týden před schůzkou; přesně týden předem už ne."""
    return (datum - (dnes or datetime.date.today())).days > STORNO_DNI


SLOTY = list(range(_minuty(PRAC_OD), _minuty(PRAC_DO), KROK_MIN))   # 09:00 … 16:30


def _dopocti_stav(ano: int, ne: int, pocet_vedoucich: int) -> tuple:
    """Stav žádosti podle účastí vedoucích. Vrací (stav, duvod_zamitnuti).

    Aspoň jedno ANO potvrzuje schůzku, NE od všech vedoucích ji zamítá
    a uvolní slot; jinak se pořád čeká.
    """
    if ano:
        return STAV_POTVRZENO, ''
    if pocet_vedoucich and ne >= pocet_vedoucich:
        return STAV_ZAMITNUTO, 'Nikdo z vedoucích se nemůže zúčastnit.'
    return STAV_CEKA, ''


def _rozsah(r) -> str:
    return f'{_hhmm(_minuty(r["cas_od"]))}–{_hhmm(_minuty(r["cas_do"]))}'


def _mozne_delky(start_min: int, blokovane: set) -> list:
    """Délky schůzky, které se od `start_min` vejdou do volna a do konce pracovní doby.

    Délka je po 10 min, ale rastr obsazenosti zůstává KROK_MIN - schůzka 9:00–9:40
    zabere sloupce 9:00 i 9:30.
    """
    return [d for d in DELKY_MIN
            if start_min + d <= _minuty(PRAC_DO)
            and not any(x in blokovane for x in range(start_min, start_min + d, KROK_MIN))]


# =========================================================
# INICIALIZACE DATABÁZE
# =========================================================
def inicializace_schuzky_db():
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schuzky_terminy (
                id INT AUTO_INCREMENT PRIMARY KEY,
                datum DATE NOT NULL UNIQUE,
                aktivni TINYINT(1) DEFAULT 1,
                poznamka VARCHAR(255),
                vytvoril VARCHAR(255),
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schuzky_rezervace (
                id INT AUTO_INCREMENT PRIMARY KEY,
                termin_id INT NOT NULL,
                cas_od TIME NOT NULL,
                cas_do TIME NOT NULL,
                zadatel_user_id INT NOT NULL,
                zadatel_jmeno VARCHAR(255),
                zadatel_email VARCHAR(255),
                ubytovani TINYINT(1) DEFAULT 0,
                poznamka TEXT,
                stav VARCHAR(20) DEFAULT 'ceka',
                duvod_zamitnuti TEXT,
                rozhodl_jmeno VARCHAR(255),
                rozhodnuto_at DATETIME,
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_slot (termin_id, cas_od),
                INDEX idx_zadatel (zadatel_user_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # Kdo z vedoucích se schůzky účastní. Jeden řádek na vedoucího a žádost.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schuzky_ucast (
                id INT AUTO_INCREMENT PRIMARY KEY,
                rezervace_id INT NOT NULL,
                vedouci_user_id INT NOT NULL,
                vedouci_jmeno VARCHAR(255),
                ucast TINYINT(1) NOT NULL,
                rozhodnuto_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_rez_vedouci (rezervace_id, vedouci_user_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # MIGRACE: dřív měla žádost jednoho vedoucího. Sloupce zůstávají kvůli
        # historickým řádkům, ale nové žádosti je neplní.
        cur.execute("SHOW COLUMNS FROM schuzky_rezervace LIKE 'vedouci_user_id'")
        sloupec = cur.fetchone()
        if sloupec and str(sloupec[2]).upper() == 'NO':
            cur.execute("ALTER TABLE schuzky_rezervace "
                        "MODIFY vedouci_user_id INT NULL, "
                        "MODIFY vedouci_jmeno VARCHAR(255) NULL")
        for d in SEED_TERMINY:
            cur.execute("INSERT IGNORE INTO schuzky_terminy (datum, vytvoril) VALUES (%s, %s)",
                        (d, 'systém'))
        conn.commit()
    except Exception as e:
        print(f'[schuzky] inicializace_schuzky_db: {e}')
    finally:
        if cur:
            cur.close()
        conn.close()


# =========================================================
# DATOVÁ VRSTVA
# =========================================================
def _emaily_uzivatelu(ids) -> dict:
    """{user_id: e-mail} pro zadané ID (jen aktivní uživatelé)."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    cur = None
    try:
        cur = conn.cursor()
        ph = ','.join(['%s'] * len(ids))
        cur.execute(f"SELECT iduser, email FROM user WHERE iduser IN ({ph})", tuple(ids))
        return {int(r[0]): _s(r[1]) for r in cur.fetchall()}
    except Exception as e:
        print(f'[schuzky] _emaily_uzivatelu: {e}')
        return {}
    finally:
        if cur:
            cur.close()
        conn.close()


def _vedouci_seznam() -> list:
    """[{'id','jmeno','email'}] — uživatelé s právem `schuzky_vedouci`.

    Seznam se nikde nehardcoduje: přidání dalšího vedoucího = zaškrtnutí práva."""
    try:
        mapa = intranet_data.ziskej_uzivatele_s_pravem('schuzky_vedouci') or {}
    except Exception as e:
        print(f'[schuzky] _vedouci_seznam: {e}')
        return []
    if not mapa:
        return []
    emaily = _emaily_uzivatelu(mapa.keys())
    seznam = [{'id': int(i), 'jmeno': _s(j), 'email': emaily.get(int(i), '')}
              for i, j in mapa.items()]
    return sorted(seznam, key=lambda v: v['jmeno'].lower())


def _terminy(vsechny: bool = False) -> list:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, datum, aktivni, poznamka FROM schuzky_terminy"
                    + ("" if vsechny else " WHERE aktivni = 1")
                    + " ORDER BY datum")
        return cur.fetchall()
    except Exception as e:
        print(f'[schuzky] _terminy: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


def _rezervace_terminu(termin_id) -> list:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM schuzky_rezervace WHERE termin_id = %s "
                    "ORDER BY cas_od, id", (termin_id,))
        return cur.fetchall()
    except Exception as e:
        print(f'[schuzky] _rezervace_terminu: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


def _moje_rezervace(user_id) -> list:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""SELECT r.*, t.datum FROM schuzky_rezervace r
                       JOIN schuzky_terminy t ON t.id = r.termin_id
                       WHERE r.zadatel_user_id = %s
                       ORDER BY t.datum DESC, r.cas_od""", (user_id,))
        return cur.fetchall()
    except Exception as e:
        print(f'[schuzky] _moje_rezervace: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


def _vsechny_zadosti() -> list:
    """Všechny žádosti (od nejbližší) — pro vedoucí i správce."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT r.*, t.datum FROM schuzky_rezervace r "
                    "JOIN schuzky_terminy t ON t.id = r.termin_id "
                    "ORDER BY FIELD(r.stav, 'ceka', 'potvrzeno', 'zamitnuto', 'zruseno'), "
                    "t.datum, r.cas_od")
        return cur.fetchall()
    except Exception as e:
        print(f'[schuzky] _vsechny_zadosti: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


def _ucasti(rez_ids) -> dict:
    """{rezervace_id: {vedouci_user_id: ucast(bool)}} pro zadané žádosti."""
    ids = [int(i) for i in rez_ids]
    if not ids:
        return {}
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    cur = None
    try:
        cur = conn.cursor()
        ph = ','.join(['%s'] * len(ids))
        cur.execute(f"SELECT rezervace_id, vedouci_user_id, ucast FROM schuzky_ucast "
                    f"WHERE rezervace_id IN ({ph})", tuple(ids))
        mapa = {}
        for rid, vid, ucast in cur.fetchall():
            mapa.setdefault(int(rid), {})[int(vid)] = bool(ucast)
        return mapa
    except Exception as e:
        print(f'[schuzky] _ucasti: {e}')
        return {}
    finally:
        if cur:
            cur.close()
        conn.close()


# =========================================================
# BUSINESS LOGIKA — zakládání, účast vedoucích, rušení
# =========================================================
def _vloz_rezervaci(termin_id, cas_od_min, delka, zadatel_id, zadatel_jmeno,
                    zadatel_email, ubytovani, poznamka):
    """Založí žádost. Vrací (ok, zprava, rez_id).

    Kontrola obsazenosti je součástí jediného INSERT ... SELECT ... WHERE NOT
    EXISTS, takže je atomická. Slot blokuje každá živá žádost (čekající
    i potvrzená) — v jednom čase je vždy jen jedna schůzka. Uvolní ho až
    zamítnutí všemi vedoucími nebo zrušení žadatelem.
    """
    # --- validace vstupu (hranice dověry: hodnoty chodí z klienta) ---
    if delka not in DELKY_MIN:
        return False, 'Neplatná délka schůzky.', None
    if cas_od_min % KROK_MIN:
        return False, 'Začátek schůzky musí být v celou nebo v půl hodiny.', None
    if cas_od_min < _minuty(PRAC_OD) or cas_od_min + delka > _minuty(PRAC_DO):
        return False, f'Schůzku lze rezervovat pouze v čase ' \
                      f'{_hhmm(_minuty(PRAC_OD))}–{_hhmm(_minuty(PRAC_DO))}.', None
    conn = intranet_data.get_db_connection()
    if not conn:
        return False, 'Databáze není dostupná.', None
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT datum, aktivni FROM schuzky_terminy WHERE id = %s", (termin_id,))
        row = cur.fetchone()
        if not row:
            return False, 'Zvolený termín neexistuje.', None
        if not row[1]:
            return False, 'Zvolený termín již není otevřený pro rezervace.', None
        if not _pred_uzaverkou(row[0]):
            return (False, f'Rezervace na {_datum_cz(row[0])} jsou uzavřené — rezervovat lze '
                           f'nejpozději {UZAVERKA_DNI} dní předem '
                           f'(do {_datum_cz(_uzaverka(row[0]))}).', None)
        od, do = _sql_cas(cas_od_min), _sql_cas(cas_od_min + delka)
        cur.execute("""
            INSERT INTO schuzky_rezervace
                   (termin_id, cas_od, cas_do, zadatel_user_id, zadatel_jmeno,
                    zadatel_email, ubytovani, poznamka, stav)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s FROM DUAL
             WHERE NOT EXISTS (
                   SELECT 1 FROM (
                          SELECT termin_id, stav, cas_od, cas_do FROM schuzky_rezervace
                   ) r
                    WHERE r.termin_id = %s AND r.stav IN (%s, %s)
                      AND r.cas_od < %s AND r.cas_do > %s)
        """, (termin_id, od, do, zadatel_id, zadatel_jmeno, zadatel_email,
              1 if ubytovani else 0, _s(poznamka), STAV_CEKA,
              termin_id, STAV_CEKA, STAV_POTVRZENO, do, od))
        if cur.rowcount == 0:
            conn.rollback()
            return False, 'Tento termín je již obsazený, zvolte prosím jiný.', None
        rez_id = cur.lastrowid
        conn.commit()
        return True, '', rez_id
    except Exception as e:
        print(f'[schuzky] _vloz_rezervaci: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 'Žádost se nepodařilo uložit.', None
    finally:
        if cur:
            cur.close()
        conn.close()


def _uloz_ucast(rez_id, vedouci_id, vedouci_jmeno, ucast: bool, pocet_vedoucich: int):
    """Vedoucí odklikne účast. Vrací (ok, zprava, rezervace, zmena_stavu).

    Stav žádosti je vždy dopočítaný z účastí — aspoň jedno ANO potvrzuje,
    NE od všech vedoucích zamítá a uvolní slot. Celé v jedné transakci
    (SELECT ... FOR UPDATE), aby se dva souběžné kliky nepřepsaly.
    """
    conn = intranet_data.get_db_connection()
    if not conn:
        return False, 'Databáze není dostupná.', None, False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        conn.start_transaction()
        cur.execute("SELECT r.*, t.datum FROM schuzky_rezervace r "
                    "JOIN schuzky_terminy t ON t.id = r.termin_id "
                    "WHERE r.id = %s FOR UPDATE", (rez_id,))
        r = cur.fetchone()
        if not r:
            conn.rollback()
            return False, 'Žádost už neexistuje.', None, False
        if r['stav'] == STAV_ZRUSENO:
            conn.rollback()
            return False, 'Žádost zrušil sám žadatel.', None, False
        cur.execute("""INSERT INTO schuzky_ucast
                              (rezervace_id, vedouci_user_id, vedouci_jmeno, ucast)
                       VALUES (%s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE ucast = VALUES(ucast),
                                               vedouci_jmeno = VALUES(vedouci_jmeno),
                                               rozhodnuto_at = NOW()""",
                    (rez_id, vedouci_id, _s(vedouci_jmeno), 1 if ucast else 0))
        cur.execute("SELECT ucast, COUNT(*) AS pocet FROM schuzky_ucast "
                    "WHERE rezervace_id = %s GROUP BY ucast", (rez_id,))
        pocty = {int(x['ucast']): int(x['pocet']) for x in cur.fetchall()}
        novy, duvod = _dopocti_stav(pocty.get(1, 0), pocty.get(0, 0), pocet_vedoucich)
        zmena = novy != r['stav']
        if zmena:
            cur.execute("""UPDATE schuzky_rezervace
                              SET stav = %s, duvod_zamitnuti = %s,
                                  rozhodl_jmeno = %s, rozhodnuto_at = NOW()
                            WHERE id = %s""",
                        (novy, duvod, _s(vedouci_jmeno), rez_id))
            r['stav'] = novy
            r['duvod_zamitnuti'] = duvod
        conn.commit()
        return True, '', r, zmena
    except Exception as e:
        print(f'[schuzky] _uloz_ucast: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 'Účast se nepodařilo uložit.', None, False
    finally:
        if cur:
            cur.close()
        conn.close()


def _zrus_rezervaci(rez_id, user_id, jako_spravce=False):
    """Ruší žádost o schůzku. Vrací (ok, zprava, rezervace).

    Žadatel může zrušit jen vlastní dosud nevyřízenou žádost a jen do uzávěrky storna.
    Správce (`jako_spravce=True`) může zrušit jakoukoliv schůzku kdykoliv.
    """
    conn = intranet_data.get_db_connection()
    if not conn:
        return False, 'Databáze není dostupná.', None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT r.*, t.datum FROM schuzky_rezervace r "
                    "JOIN schuzky_terminy t ON t.id = r.termin_id WHERE r.id = %s", (rez_id,))
        r = cur.fetchone()
        if not r:
            return False, 'Žádost už neexistuje.', None
        if jako_spravce:
            if r['stav'] == STAV_ZRUSENO:
                return False, 'Žádost je už zrušená.', None
            cur.execute("""UPDATE schuzky_rezervace SET stav = %s, rozhodnuto_at = NOW()
                            WHERE id = %s AND stav <> %s""",
                        (STAV_ZRUSENO, rez_id, STAV_ZRUSENO))
        else:
            if not _lze_stornovat(r['datum']):
                return (False, f'Zrušit lze nejpozději {STORNO_DNI} dní před schůzkou '
                               f'(do {_datum_cz(_storno_do(r["datum"]))}). '
                               f'Domluvte se přímo s vedoucím.', None)
            cur.execute("""UPDATE schuzky_rezervace SET stav = %s, rozhodnuto_at = NOW()
                            WHERE id = %s AND zadatel_user_id = %s AND stav = %s""",
                        (STAV_ZRUSENO, rez_id, user_id, STAV_CEKA))
        if cur.rowcount == 0:
            conn.rollback()
            return False, ('Zrušení se nepodařilo provést.' if jako_spravce
                           else 'Zrušit lze jen vlastní dosud nepotvrzenou žádost.'), None
        conn.commit()
        r['stav'] = STAV_ZRUSENO
        return True, '', r
    except Exception as e:
        print(f'[schuzky] _zrus_rezervaci: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 'Zrušení se nepodařilo uložit.', None
    finally:
        if cur:
            cur.close()
        conn.close()


def _smaz_rezervaci(rez_id):
    """Trvale maže žádost i s odklikanou účastí. Vrací (ok, zprava, rezervace).

    Jen pro hlavního administrátora (právo `vse`) — volající si oprávnění hlídá.
    """
    conn = intranet_data.get_db_connection()
    if not conn:
        return False, 'Databáze není dostupná.', None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT r.*, t.datum FROM schuzky_rezervace r "
                    "JOIN schuzky_terminy t ON t.id = r.termin_id WHERE r.id = %s", (rez_id,))
        r = cur.fetchone()
        if not r:
            return False, 'Žádost už neexistuje.', None
        cur.execute("DELETE FROM schuzky_ucast WHERE rezervace_id = %s", (rez_id,))
        cur.execute("DELETE FROM schuzky_rezervace WHERE id = %s", (rez_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return False, 'Smazání se nepodařilo provést.', None
        conn.commit()
        return True, '', r
    except Exception as e:
        print(f'[schuzky] _smaz_rezervaci: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 'Smazání se nepodařilo uložit.', None
    finally:
        if cur:
            cur.close()
        conn.close()


def _pridej_termin(datum_iso, kdo, poznamka=''):
    try:
        d = datetime.date.fromisoformat(_s(datum_iso))
    except ValueError:
        return False, 'Neplatné datum.'
    conn = intranet_data.get_db_connection()
    if not conn:
        return False, 'Databáze není dostupná.'
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("INSERT IGNORE INTO schuzky_terminy (datum, poznamka, vytvoril) "
                    "VALUES (%s, %s, %s)", (d.isoformat(), _s(poznamka), _s(kdo)))
        if cur.rowcount == 0:
            return False, 'Tento termín už je vypsaný.'
        conn.commit()
        return True, ''
    except Exception as e:
        print(f'[schuzky] _pridej_termin: {e}')
        return False, 'Termín se nepodařilo uložit.'
    finally:
        if cur:
            cur.close()
        conn.close()


def _prepni_termin(termin_id, aktivni):
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("UPDATE schuzky_terminy SET aktivni = %s WHERE id = %s",
                    (1 if aktivni else 0, termin_id))
        conn.commit()
    except Exception as e:
        print(f'[schuzky] _prepni_termin: {e}')
    finally:
        if cur:
            cur.close()
        conn.close()


# =========================================================
# E-MAILY (best-effort, na pozadí — vzor intranet_vizitky.py)
# =========================================================
def _app_url() -> str:
    try:
        u = (intranet_data.nacti_nastaveni_intranetu().get('app_url', '') or '').strip().rstrip('/')
        return f'{u}/asm' if u else ''   # modul bydlí v rozcestníku Formulářů ASM
    except Exception:
        return ''


def _email_html(text: str, odkaz: str) -> str:
    import html as _h
    telo = _h.escape(text).replace('\n', '<br>')
    tlacitko = ''
    if odkaz:
        tlacitko = (
            f'<div style="margin:26px 0"><a href="{odkaz}" '
            'style="background:#0284c7;color:#ffffff;text-decoration:none;font-weight:bold;'
            'padding:12px 24px;border-radius:8px;display:inline-block">'
            '🗓️ Otevřít v portálu MOJEJIPka</a></div>'
            f'<div style="font-size:12px;color:#888">Nefunguje tlačítko? Otevřete: '
            f'<a href="{odkaz}" style="color:#0284c7">{odkaz}</a></div>'
        )
    return (
        '<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;font-size:14px;'
        'color:#222;line-height:1.6;max-width:620px;margin:0 auto;padding:24px">'
        f'{telo}{tlacitko}'
        '<hr style="border:none;border-top:1px solid #eee;margin:24px 0">'
        '<div style="font-size:12px;color:#999">Automatická zpráva z portálu MOJEJIPka — '
        'modul „Rezervace ind. schůzky s vedoucím".</div>'
        '</body></html>'
    )


def _posli_email_sync(prijemci, predmet, html_obsah):
    for p in prijemci:
        try:
            intranet_emaily.odesli_html_email(p, predmet, html_obsah)
        except Exception as e:
            print(f'[schuzky] e-mail {p}: {e}')


def _odesli_emaily(prijemci, predmet, text):
    prijemci = [p for p in dict.fromkeys(prijemci) if p and '@' in p]
    if not prijemci:
        return
    html_obsah = _email_html(text, _app_url())
    try:
        asyncio.create_task(asyncio.to_thread(_posli_email_sync, prijemci, predmet, html_obsah))
    except RuntimeError:                     # mimo event loop (např. testy)
        _posli_email_sync(prijemci, predmet, html_obsah)


def _hlavicka(r, datum) -> str:
    return f'{_datum_cz(datum)} {_rozsah(r)}'


def _mail_nova_zadost(r, datum, emaily_vedoucich):
    hl = _hlavicka(r, datum)
    text = (f'Dobrý den,\n\n{r["zadatel_jmeno"]} žádá o schůzku s vedoucími.\n\n'
            f'Termín: {hl}\n'
            f'Zajistit ubytování: {"ANO" if r.get("ubytovani") else "ne"}\n'
            + (f'Poznámka: {_s(r.get("poznamka"))}\n' if _s(r.get('poznamka')) else '')
            + '\nOdklikněte prosím v portálu, zda se schůzky účastníte.')
    _odesli_emaily(emaily_vedoucich, f'Nová žádost o schůzku – {hl}', text)


def _mail_rozhodnuto(r, datum, potvrzeno: bool, kdo='', duvod=''):
    hl = _hlavicka(r, datum)
    if potvrzeno:
        text = (f'Dobrý den,\n\nVaše žádost o schůzku s vedoucími byla POTVRZENA.\n\n'
                f'Termín: {hl}\n'
                + (f'Účast potvrdil: {_s(kdo)}\n' if _s(kdo) else '')
                + f'Zajistit ubytování: {"ANO" if r.get("ubytovani") else "ne"}\n'
                + '\nKdo přesně se schůzky zúčastní, vidíte v portálu u své žádosti.')
        predmet = f'Schůzka potvrzena – {hl}'
    else:
        text = (f'Dobrý den,\n\nVaše žádost o schůzku s vedoucími byla ZAMÍTNUTA.\n\n'
                f'Termín: {hl}\n'
                + (f'Důvod: {_s(duvod)}\n' if _s(duvod) else '')
                + '\nVyberte si prosím jiný volný termín v portálu.')
        predmet = f'Schůzka zamítnuta – {hl}'
    _odesli_emaily([r.get('zadatel_email')], predmet, text)


# =========================================================
# HLAVNÍ VYKRESLOVACÍ FUNKCE
# =========================================================
@ui.refreshable
def vykresli_schuzky(user_id, user_name, user_email, vsechna_prava, s_hlavickou=True):
    inicializace_schuzky_db()
    je_admin = 'vse' in vsechna_prava
    je_spravce = je_admin or 'schuzky_spravce' in vsechna_prava
    je_vedouci = 'schuzky_vedouci' in vsechna_prava
    je_zadatel = je_spravce or 'schuzky_zadatel' in vsechna_prava
    vedouci = _vedouci_seznam()
    pocet_vedoucich = len(vedouci)
    spravuje_terminy = je_spravce or je_vedouci
    terminy = _terminy(vsechny=spravuje_terminy)
    mapa_datum = {t['id']: t['datum'] for t in _terminy(vsechny=True)}

    # --- výběr termínu (drží se v session, default = nejbližší otevřený) ---
    ids = [t['id'] for t in terminy]
    tid = app.storage.user.get('schuzky_termin')
    if tid not in ids:
        dnes = datetime.date.today()
        otevrene = [t for t in terminy if t['aktivni'] and _pred_uzaverkou(t['datum'], dnes)]
        budouci = [t for t in terminy if t['datum'] >= dnes]
        tid = (otevrene[0]['id'] if otevrene
               else (budouci[0]['id'] if budouci else (ids[0] if ids else None)))
        app.storage.user['schuzky_termin'] = tid
    termin = next((t for t in terminy if t['id'] == tid), None)
    rezervace = _rezervace_terminu(tid) if tid else []

    # --- mapa obsazenosti: slot -> rezervace (živá žádost drží slot) ---
    obsazeno = {}
    for r in rezervace:
        if r['stav'] in (STAV_ZAMITNUTO, STAV_ZRUSENO):
            continue
        od, do = _minuty(r['cas_od']), _minuty(r['cas_do'])
        for s in SLOTY:
            if od <= s < do:
                obsazeno[s] = r
    blokovane = set(obsazeno)
    vidi_jmena = je_spravce or je_vedouci
    po_uzaverce = bool(termin and not _pred_uzaverkou(termin['datum']))
    lze_rezervovat = bool(je_zadatel and termin and termin['aktivni']
                          and vedouci and not po_uzaverce)

    def _ucast_skupiny(mapa_u):
        """Jména vedoucích rozdělená podle odklikané účasti: (ano, ne, nerozhodnuto)."""
        mapa_u = mapa_u or {}
        ano = [v['jmeno'] for v in vedouci if mapa_u.get(int(v['id'])) is True]
        ne = [v['jmeno'] for v in vedouci if mapa_u.get(int(v['id'])) is False]
        ceka = [v['jmeno'] for v in vedouci if int(v['id']) not in mapa_u]
        return ano, ne, ceka

    # =====================================================
    # DIALOG — nová rezervace
    # =====================================================
    def _dialog_nova(start_pref=None):
        if not lze_rezervovat:
            return
        with ui.dialog() as dlg, ui.card().classes('w-[560px] max-w-full p-6 gap-3'):
            ui.label('Nová rezervace schůzky').classes('text-xl font-bold text-gray-800')
            ui.label(f'Termín: {_datum_cz(termin["datum"])}   ·   '
                     f'{_hhmm(_minuty(PRAC_OD))}–{_hhmm(_minuty(PRAC_DO))}   ·   '
                     f'délka po 10 min, max. 1,5 hodiny   ·   uzávěrka '
                     f'{_datum_cz(_uzaverka(termin["datum"]))}').classes('text-sm text-gray-500')
            ui.label('Schůzka je společná se všemi vedoucími — účast si každý odklikne sám.') \
                .classes('text-sm text-gray-500')
            sel_od = ui.select({}, label='Začátek').classes('w-full').props('outlined')
            sel_delka = ui.select({}, label='Délka schůzky').classes('w-full').props('outlined')
            sw_ubyt = ui.switch('Zajistit ubytování')
            ta_pozn = ui.textarea('Poznámka (nepovinné)').classes('w-full').props('outlined rows=2')

            def _obnov_delky():
                st = sel_od.value
                mozne = _mozne_delky(int(st), blokovane) if st is not None else []
                sel_delka.options = {d: DELKA_POPIS[d] for d in mozne}
                if sel_delka.value not in mozne:
                    # výchozí 30 min, jinak nejdelší kratší varianta
                    sel_delka.value = (30 if 30 in mozne else (mozne[-1] if mozne else None))
                sel_delka.update()

            volne = [s for s in SLOTY if s not in blokovane]
            sel_od.options = {s: _hhmm(s) for s in volne}
            sel_od.value = (start_pref if start_pref in volne else (volne[0] if volne else None))
            sel_od.update()
            sel_od.on_value_change(lambda _: _obnov_delky())
            _obnov_delky()

            def _uloz():
                if sel_od.value is None or not sel_delka.value:
                    ui.notify('Vyberte začátek i délku schůzky.', type='warning')
                    return
                od_min, delka = int(sel_od.value), int(sel_delka.value)
                ok, zprava, rez_id = _vloz_rezervaci(
                    termin['id'], od_min, delka, user_id, user_name, user_email,
                    sw_ubyt.value, ta_pozn.value)
                dlg.close()
                if not ok:
                    ui.notify(zprava, type='negative')
                else:
                    r = {'cas_od': _sql_cas(od_min), 'cas_do': _sql_cas(od_min + delka),
                         'zadatel_jmeno': user_name, 'ubytovani': sw_ubyt.value,
                         'poznamka': ta_pozn.value}
                    _mail_nova_zadost(r, termin['datum'], [v['email'] for v in vedouci])
                    intranet_logger.log_activity(
                        user_name, 'Schůzky',
                        f'Žádost o schůzku #{rez_id} – {_datum_cz(termin["datum"])} '
                        f'{_hhmm(od_min)}–{_hhmm(od_min + delka)}')
                    ui.notify('Žádost byla odeslána všem vedoucím.', type='positive')
                    vykresli_schuzky.refresh()

            with ui.row().classes('w-full justify-end gap-2 pt-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat color=grey')
                ui.button('Odeslat žádost', icon='send', on_click=_uloz) \
                    .classes('bg-sky-600 text-white font-bold')
        dlg.open()

    # =====================================================
    # AKCE — účast vedoucího, zrušení žadatelem
    # =====================================================
    def _akce_ucast(rid, ucast):
        ok, zprava, r, zmena = _uloz_ucast(rid, user_id, user_name, ucast, pocet_vedoucich)
        if not ok:
            ui.notify(zprava, type='negative')
            return
        datum = mapa_datum.get(r['termin_id'])
        if zmena and r['stav'] == STAV_POTVRZENO:
            _mail_rozhodnuto(r, datum, True, kdo=user_name)
        elif zmena and r['stav'] == STAV_ZAMITNUTO:
            _mail_rozhodnuto(r, datum, False, duvod=r.get('duvod_zamitnuti'))
        intranet_logger.log_activity(
            user_name, 'Schůzky',
            f'Účast {"ANO" if ucast else "NE"} u schůzky #{rid} s {_s(r["zadatel_jmeno"])}')
        if zmena and r['stav'] == STAV_ZAMITNUTO:
            ui.notify('Nikdo z vedoucích se neúčastní — žádost zamítnuta.', type='warning')
        else:
            ui.notify('Účast uložena.' + (' Schůzka potvrzena.'
                                          if zmena and r['stav'] == STAV_POTVRZENO else ''),
                      type='positive')
        vykresli_schuzky.refresh()

    def _akce_zrus(rid, jako_spravce=False):
        ok, zprava, r = _zrus_rezervaci(rid, user_id, jako_spravce=jako_spravce)
        if not ok:
            ui.notify(zprava, type='negative')
        else:
            intranet_logger.log_activity(
                user_name, 'Schůzky',
                (f'Správce zrušil schůzku #{rid} s {_s(r["zadatel_jmeno"])}' if jako_spravce
                 else f'Zrušena vlastní žádost #{rid}'))
            ui.notify('Schůzka byla zrušena.' if jako_spravce else 'Žádost byla zrušena.',
                      type='info')
            vykresli_schuzky.refresh()

    def _dialog_zrus_spravce(rid, jmeno, datum, rozsah):
        with ui.dialog() as dlg, ui.card().classes('w-[420px] max-w-full p-6 gap-3'):
            ui.label('Zrušit schůzku?').classes('text-lg font-bold text-gray-800')
            ui.label(f'{jmeno} · {datum} · {rozsah}').classes('text-sm text-gray-600')
            ui.label('Slot se uvolní pro další rezervace.').classes('text-xs text-gray-500')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zpět', on_click=dlg.close).props('flat no-caps color=grey')
                ui.button('Zrušit schůzku', icon='delete',
                          on_click=lambda: (dlg.close(), _akce_zrus(rid, jako_spravce=True))) \
                    .props('no-caps color=red')
        dlg.open()

    def _akce_smaz(rid):
        if not je_admin:
            ui.notify('Mazat smí jen hlavní administrátor.', type='negative')
            return
        ok, zprava, r = _smaz_rezervaci(rid)
        if not ok:
            ui.notify(zprava, type='negative')
            return
        intranet_logger.log_activity(
            user_name, 'Schůzky',
            f'Trvale smazána schůzka #{rid} s {_s(r["zadatel_jmeno"])}')
        ui.notify('Žádost byla trvale smazána.', type='info')
        vykresli_schuzky.refresh()

    def _dialog_smaz_admin(rid, jmeno, datum, rozsah):
        with ui.dialog() as dlg, ui.card().classes('w-[420px] max-w-full p-6 gap-3'):
            ui.label('Smazat žádost natrvalo?').classes('text-lg font-bold text-gray-800')
            ui.label(f'{jmeno} · {datum} · {rozsah}').classes('text-sm text-gray-600')
            ui.label('Smaže se i odkliknutá účast vedoucích. Akci nelze vrátit — '
                     'v přehledu po ní nezůstane žádná stopa (jen záznam v logu).') \
                .classes('text-xs text-red-600')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zpět', on_click=dlg.close).props('flat no-caps color=grey')
                ui.button('Smazat natrvalo', icon='delete_forever',
                          on_click=lambda: (dlg.close(), _akce_smaz(rid))) \
                    .props('no-caps color=red')
        dlg.open()

    # =====================================================
    # KARTA ŽÁDOSTI
    # =====================================================
    def _karta(r, mapa_u, ucast_tlacitka=False, zrusit=False, zrusit_spravce=False,
               smazat_admin=False):
        popis, barva = STAV_POPIS.get(r['stav'], (r['stav'], 'bg-gray-100'))
        ano, ne, ceka = _ucast_skupiny(mapa_u)
        if r['stav'] == STAV_POTVRZENO and pocet_vedoucich:
            popis = f'{popis} {len(ano)}/{pocet_vedoucich}'
        moje_u = (mapa_u or {}).get(int(user_id))
        with ui.card().classes('w-full p-4 mb-2 shadow-sm'):
            with ui.row().classes('w-full items-center justify-between flex-wrap gap-2'):
                with ui.column().classes('gap-0'):
                    ui.label(f'{_s(r["zadatel_jmeno"])} · {_datum_cz(r["datum"])} · {_rozsah(r)}') \
                        .classes('font-bold text-gray-800')
                    ui.label(f'ubytování: {"ANO" if r["ubytovani"] else "ne"}') \
                        .classes('text-sm text-gray-600')
                    if _s(r.get('poznamka')):
                        ui.label(f'Poznámka: {_s(r["poznamka"])}').classes('text-xs text-gray-500')
                    with ui.row().classes('items-center gap-1 flex-wrap pt-1'):
                        ui.label('Vedoucí:').classes('text-xs text-gray-500')
                        if not (ano or ne or ceka):
                            ui.label('zatím nikdo neodpověděl').classes('text-xs text-gray-500')
                        stitky = ([(f'✓ {j}', 'bg-green-100 text-green-800') for j in ano]
                                  + [(f'✕ {j}', 'bg-red-100 text-red-700') for j in ne]
                                  + [(f'· {j}', 'bg-gray-100 text-gray-500') for j in ceka])
                        for text, cls in stitky:
                            ui.label(text).classes(
                                f'{cls} px-2 py-0.5 rounded-full text-xs font-bold')
                    if _s(r.get('duvod_zamitnuti')):
                        ui.label(f'Důvod: {_s(r["duvod_zamitnuti"])}').classes('text-xs text-red-600')
                with ui.row().classes('items-center gap-2'):
                    ui.label(popis).classes(f'{barva} px-3 py-1 rounded-full text-xs font-bold')
                    if ucast_tlacitka and r['stav'] != STAV_ZRUSENO:
                        ui.button('Účastním se', icon='check',
                                  on_click=lambda rid=r['id']: _akce_ucast(rid, True)) \
                            .props('no-caps color=green' + ('' if moje_u is True else ' outline'))
                        ui.button('Neúčastním se', icon='block',
                                  on_click=lambda rid=r['id']: _akce_ucast(rid, False)) \
                            .props('no-caps color=red' + ('' if moje_u is False else ' outline'))
                    if zrusit_spravce and r['stav'] != STAV_ZRUSENO:
                        ui.button('Zrušit schůzku', icon='delete',
                                  on_click=lambda rid=r['id'], jm=_s(r['zadatel_jmeno']),
                                  d=_datum_cz(r['datum']), rz=_rozsah(r):
                                  _dialog_zrus_spravce(rid, jm, d, rz)) \
                            .props('flat dense no-caps color=red')
                    if smazat_admin and je_admin:
                        ui.button('Smazat', icon='delete_forever',
                                  on_click=lambda rid=r['id'], jm=_s(r['zadatel_jmeno']),
                                  d=_datum_cz(r['datum']), rz=_rozsah(r):
                                  _dialog_smaz_admin(rid, jm, d, rz)) \
                            .props('flat dense no-caps color=red') \
                            .tooltip('Trvale smazat (jen hlavní administrátor)')
                    if zrusit and r['stav'] in (STAV_CEKA, STAV_POTVRZENO):
                        if _lze_stornovat(r['datum']):
                            ui.button('Zrušit', icon='close',
                                      on_click=lambda rid=r['id']: _akce_zrus(rid)) \
                                .props('flat dense color=grey')
                        else:
                            ui.label(f'Zrušení už není možné '
                                     f'(méně než {STORNO_DNI + 1} dní do schůzky)') \
                                .classes('text-xs text-gray-500')

    # =====================================================
    # LAYOUT
    # =====================================================
    with ui.column().classes('w-full p-4 gap-4'):
        with ui.row().classes('w-full items-center justify-between'):
            with ui.column().classes('gap-0'):
                if s_hlavickou:                      # uvnitř Formulářů ASM hlavičku dělá ASM
                    ui.label('🗓️ Rezervace ind. schůzky s vedoucími') \
                        .classes('text-3xl font-bold text-gray-800')
                ui.label(f'Schůzky {_hhmm(_minuty(PRAC_OD))}–{_hhmm(_minuty(PRAC_DO))}, '
                         f'délka 10–90 minut po 10. Jedna žádost pro všechny vedoucí — '
                         f'účast si každý odklikne sám. Rezervovat lze nejpozději '
                         f'{UZAVERKA_DNI} dní před termínem, zrušit déle než '
                         f'{STORNO_DNI} dní před schůzkou.').classes('text-sm text-gray-500')
            if lze_rezervovat:
                ui.button('Nová rezervace', icon='add', on_click=lambda: _dialog_nova()) \
                    .classes('bg-sky-600 text-white font-bold px-6 py-3 rounded-lg shadow-md')

        if not vedouci:
            ui.label('⚠ Zatím nemá nikdo právo „Vedoucí schůzky" – rezervace nelze zakládat. '
                     'Přidělte právo v Uživatelích.') \
                .classes('w-full p-4 bg-amber-50 border border-amber-200 rounded-lg text-amber-800')
        if not terminy:
            ui.label('Zatím není vypsaný žádný termín.') \
                .classes('w-full p-4 bg-gray-50 border border-gray-200 rounded-lg text-gray-600')

        # Vybraná záložka se drží v session — jinak by každý refresh (uložení účasti,
        # zrušení, smazání) shodil uživatele zpátky na kalendář.
        platne_taby = (['kalendar', 'moje']
                       + (['zadosti'] if (je_vedouci or je_spravce) else [])
                       + (['terminy'] if spravuje_terminy else []))
        akt_tab = app.storage.user.get('schuzky_tab')
        if akt_tab not in platne_taby:
            akt_tab = 'kalendar'

        def _uloz_tab(e):
            if e.value in platne_taby:
                app.storage.user['schuzky_tab'] = e.value

        with ui.tabs(value=akt_tab, on_change=_uloz_tab).classes('w-full') as taby:
            t_kal = ui.tab('kalendar', label='Kalendář', icon='calendar_month')
            t_moje = ui.tab('moje', label='Moje žádosti', icon='list_alt')
            t_zad = ui.tab('zadosti', label='Žádosti', icon='how_to_reg') \
                if (je_vedouci or je_spravce) else None
            t_term = ui.tab('terminy', label='Termíny', icon='event_repeat') if spravuje_terminy else None

        with ui.tab_panels(taby, value=akt_tab).classes('w-full bg-transparent'):
            # ---------- KALENDÁŘ ----------
            with ui.tab_panel(t_kal).classes('p-0'):
                with ui.row().classes('w-full items-center gap-2 flex-wrap pb-3'):
                    ui.label('Termín:').classes('font-bold text-gray-700')
                    for t in terminy:
                        aktivni_tab = (t['id'] == tid)
                        popis = _datum_cz(t['datum'])
                        if not t['aktivni']:
                            popis += '  (uzavřeno)'
                        elif not _pred_uzaverkou(t['datum']):
                            popis += '  (po uzávěrce)'

                        def _vyber(tid_new=t['id']):
                            app.storage.user['schuzky_termin'] = tid_new
                            vykresli_schuzky.refresh()

                        # barvu nese Quasar prop, ne Tailwind — .text-white z defaultu
                        # color=primary jinak přebije text-gray-* a popisek zmizí
                        ui.button(popis, on_click=_vyber) \
                            .props('no-caps ' + ('color=sky-600' if aktivni_tab
                                                 else 'color=white text-color=grey-8 outline')) \
                            .classes('font-bold rounded-lg px-4 '
                                     + ('bg-sky-600 text-white' if aktivni_tab else ''))

                with ui.row().classes('w-full items-center gap-4 text-xs text-gray-600 pb-2 flex-wrap'):
                    ui.html('<span style="display:inline-block;width:14px;height:14px;'
                            'background:#ffffff;border:1px solid #d1d5db;vertical-align:-2px"></span> volno')
                    ui.html('<span style="display:inline-block;width:14px;height:14px;'
                            'background:#fef3c7;border:1px solid #fcd34d;vertical-align:-2px"></span> '
                            'rezervováno, čeká na potvrzení (slot je už zabraný)')
                    ui.html('<span style="display:inline-block;width:14px;height:14px;'
                            'background:#d1d5db;border:1px solid #9ca3af;vertical-align:-2px"></span> potvrzeno')
                    if not vidi_jmena:
                        ui.label('· jména účastníků vidí jen vedoucí a správce')

                if po_uzaverce and termin:
                    ui.label(f'⚠ Uzávěrka rezervací na {_datum_cz(termin["datum"])} byla '
                             f'{_datum_cz(_uzaverka(termin["datum"]))} '
                             f'({UZAVERKA_DNI} dní předem). Termín lze už jen prohlížet.') \
                        .classes('w-full p-3 mb-2 bg-amber-50 border border-amber-200 '
                                 'rounded-lg text-amber-800 text-sm')

                if termin:
                    with ui.column().classes('w-full gap-0 max-w-[560px]'):
                        for s in SLOTY:
                            with ui.row().classes('w-full gap-0 no-wrap'):
                                ui.label(_hhmm(s)).classes(
                                    'w-16 shrink-0 text-xs text-gray-500 text-right pr-2 '
                                    'h-9 flex items-center justify-end')
                                _vykresli_bunku(s, obsazeno, vidi_jmena,
                                                lze_rezervovat, _dialog_nova)

            # ---------- MOJE ŽÁDOSTI ----------
            with ui.tab_panel(t_moje).classes('p-0'):
                moje = _moje_rezervace(user_id)
                mapa_moje = _ucasti([r['id'] for r in moje])
                if not moje:
                    ui.label('Zatím nemáte žádnou žádost o schůzku.').classes('text-gray-500 p-4')
                for r in moje:
                    _karta(r, mapa_moje.get(int(r['id'])), zrusit=True)

            # ---------- ŽÁDOSTI (vedoucí / správce) ----------
            if t_zad is not None:
                with ui.tab_panel(t_zad).classes('p-0'):
                    zadosti = _vsechny_zadosti()
                    mapa_zad = _ucasti([r['id'] for r in zadosti])
                    if not zadosti:
                        ui.label('Žádné žádosti o schůzku.').classes('text-gray-500 p-4')
                    if je_spravce and not je_vedouci:
                        ui.label('Účast odklikávají jen vedoucí — vy vidíte přehled.') \
                            .classes('text-xs text-gray-500 pb-2')
                    for r in zadosti:
                        _karta(r, mapa_zad.get(int(r['id'])), ucast_tlacitka=je_vedouci,
                               zrusit_spravce=je_spravce, smazat_admin=je_admin)

            # ---------- TERMÍNY (jen správce) ----------
            if t_term is not None:
                with ui.tab_panel(t_term).classes('p-0'):
                    with ui.row().classes('w-full items-end gap-3 pb-4'):
                        i_datum = ui.input('Nový termín').props('outlined readonly').classes('w-48')
                        with i_datum:
                            with ui.menu():
                                ui.date(mask='YYYY-MM-DD').bind_value(i_datum).props('today-btn')
                        i_pozn = ui.input('Poznámka (nepovinné)').props('outlined').classes('w-64')

                        def _pridej():
                            ok, zprava = _pridej_termin(i_datum.value, user_name, i_pozn.value)
                            ui.notify(zprava or 'Termín přidán.',
                                      type='negative' if not ok else 'positive')
                            if ok:
                                intranet_logger.log_activity(user_name, 'Schůzky',
                                                             f'Přidán termín {i_datum.value}')
                                vykresli_schuzky.refresh()

                        ui.button('Přidat termín', icon='add', on_click=_pridej) \
                            .classes('bg-sky-600 text-white font-bold')
                    for t in _terminy(vsechny=True):
                        with ui.card().classes('w-full p-3 mb-2 shadow-sm'):
                            with ui.row().classes('w-full items-center justify-between'):
                                with ui.column().classes('gap-0'):
                                    ui.label(_datum_cz(t['datum'])).classes('font-bold text-gray-800')
                                    ui.label(f'Uzávěrka rezervací: {_datum_cz(_uzaverka(t["datum"]))}'
                                             + ('' if _pred_uzaverkou(t['datum']) else ' – proběhla')) \
                                        .classes('text-xs ' + ('text-gray-500'
                                                               if _pred_uzaverkou(t['datum'])
                                                               else 'text-amber-700 font-bold'))
                                    if _s(t.get('poznamka')):
                                        ui.label(_s(t['poznamka'])).classes('text-xs text-gray-500')
                                ui.switch('Otevřeno pro rezervace', value=bool(t['aktivni']),
                                          on_change=lambda e, tid=t['id']: (
                                              _prepni_termin(tid, e.value),
                                              vykresli_schuzky.refresh()))


def _vykresli_bunku(s, obsazeno, vidi_jmena, lze_rezervovat, dialog_nova):
    """Jedna půlhodinová buňka kalendáře — schůzka je vždy se všemi vedoucími."""
    zaklad = ('flex-1 min-w-[150px] h-9 border border-gray-200 text-xs px-1 '
              'flex items-center justify-center text-center truncate')
    r = obsazeno.get(s)
    if r:
        ceka = (r['stav'] == STAV_CEKA)
        prvni = _minuty(r['cas_od']) == s
        if not prvni:
            popis = '·'
        elif vidi_jmena:
            popis = _s(r['zadatel_jmeno'])
        else:
            popis = 'Rezervováno' if ceka else 'Obsazeno'
        barva = 'bg-amber-100 text-amber-800' if ceka else 'bg-gray-300 text-gray-700'
        el = ui.label(popis).classes(f'{zaklad} {barva} font-semibold')
        if prvni:
            detail = (f'{_s(r["zadatel_jmeno"])} · ' if vidi_jmena else '')
            detail += _rozsah(r) + ' · ' + ('čeká na potvrzení' if ceka else 'potvrzeno')
            detail += f' · ubytování: {"ANO" if r["ubytovani"] else "ne"}'
            el.tooltip(detail)
        return
    if lze_rezervovat:
        ui.label('').classes(f'{zaklad} bg-white cursor-pointer hover:bg-sky-100') \
            .on('click', lambda s=s: dialog_nova(s))
    else:
        ui.label('').classes(f'{zaklad} bg-white')
