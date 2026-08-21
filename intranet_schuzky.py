# -*- coding: utf-8 -*-
"""Modul „Rezervace ind. schůzky s vedoucím".

Workflow:
  • Žadatel (právo `schuzky_zadatel`, typicky ASM) si na vypsaném termínu vybere
    vedoucího, začátek a délku schůzky (10–90 min po 10 v okně 9:00–17:00),
    uvede, zda chce zajistit ubytování, a odešle žádost. Vidí jen své žádosti.
  • Vedoucí (právo `schuzky_vedouci`) vidí všechny žádosti na svoji osobu
    a potvrzuje / zamítá je. V kalendáři vidí jména jen ve svém sloupci.
  • Správce (právo `schuzky_spravce`) vidí a může vše + spravuje termíny.

Stavy: ceka → potvrzeno | zamitnuto | zruseno (zrušil sám žadatel).

Rezervovat lze nejpozději UZAVERKA_DNI dní před termínem; po uzávěrce se termín
v kalendáři jen prohlíží.

Obsazenost drží už PODANÁ žádost — slot se zamkne okamžitě a další ASM na něj
nemůže. Uvolní ho až zamítnutí vedoucím nebo zrušení žadatelem. Potvrzení tedy
řeší jen stav, ne konkurenci.

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


SLOTY = list(range(_minuty(PRAC_OD), _minuty(PRAC_DO), KROK_MIN))   # 09:00 … 16:30


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
                vedouci_user_id INT NOT NULL,
                vedouci_jmeno VARCHAR(255),
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
                INDEX idx_slot (termin_id, vedouci_user_id, cas_od),
                INDEX idx_zadatel (zadatel_user_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
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


def _zadosti_na_vedouciho(vedouci_id=None) -> list:
    """Žádosti na konkrétního vedoucího; `vedouci_id=None` → všechny (správce)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        sql = """SELECT r.*, t.datum FROM schuzky_rezervace r
                 JOIN schuzky_terminy t ON t.id = r.termin_id"""
        par = ()
        if vedouci_id is not None:
            sql += " WHERE r.vedouci_user_id = %s"
            par = (vedouci_id,)
        sql += " ORDER BY FIELD(r.stav,'ceka','potvrzeno','zamitnuto','zruseno'), t.datum, r.cas_od"
        cur.execute(sql, par)
        return cur.fetchall()
    except Exception as e:
        print(f'[schuzky] _zadosti_na_vedouciho: {e}')
        return []
    finally:
        if cur:
            cur.close()
        conn.close()


# =========================================================
# BUSINESS LOGIKA — zakládání, potvrzení, zamítnutí
# =========================================================
def _vloz_rezervaci(termin_id, vedouci, cas_od_min, delka,
                    zadatel_id, zadatel_jmeno, zadatel_email, ubytovani, poznamka):
    """Založí žádost. Vrací (ok, zprava, rez_id).

    Uzávěrku i obsazenost kontroluje server znovu — UI je jen nápověda, hodnoty
    chodí z klienta.

    Kontrola obsazenosti je součástí jediného INSERT ... SELECT ... WHERE NOT EXISTS,
    takže je atomická i při souběhu dvou žadatelů. Slot blokuje každá živá žádost
    (čekající i potvrzená) — buď u téhož vedoucího, nebo u téhož žadatele
    (nemůže být na dvou místech naráz). Uvolní ho zamítnutí nebo zrušení.
    """
    # --- validace vstupu (hranice důvěry: hodnoty chodí z klienta) ---
    if delka not in DELKY_MIN:
        return False, 'Neplatná délka schůzky.', None
    if cas_od_min % KROK_MIN:
        return False, 'Začátek schůzky musí být v celou nebo v půl hodiny.', None
    if cas_od_min < _minuty(PRAC_OD) or cas_od_min + delka > _minuty(PRAC_DO):
        return (False, f'Schůzku lze rezervovat pouze v čase '
                       f'{_hhmm(_minuty(PRAC_OD))}\u2013{_hhmm(_minuty(PRAC_DO))}.', None)

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
                (termin_id, vedouci_user_id, vedouci_jmeno, cas_od, cas_do,
                 zadatel_user_id, zadatel_jmeno, zadatel_email, ubytovani, poznamka, stav)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s FROM DUAL
             WHERE NOT EXISTS (
                SELECT 1 FROM (
                    SELECT termin_id, stav, vedouci_user_id, zadatel_user_id, cas_od, cas_do
                      FROM schuzky_rezervace
                ) r
                WHERE r.termin_id = %s AND r.stav IN (%s, %s)
                  AND (r.vedouci_user_id = %s OR r.zadatel_user_id = %s)
                  AND r.cas_od < %s AND r.cas_do > %s
             )
        """, (termin_id, vedouci['id'], vedouci['jmeno'], od, do,
              zadatel_id, zadatel_jmeno, zadatel_email,
              1 if ubytovani else 0, _s(poznamka), STAV_CEKA,
              termin_id, STAV_CEKA, STAV_POTVRZENO, vedouci['id'], zadatel_id, do, od))
        if cur.rowcount == 0:
            conn.rollback()
            return False, 'Tento termín je již rezervovaný, zvolte prosím jiný.', None
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


def _potvrd_rezervaci(rez_id, kdo_jmeno, kdo_user_id, je_spravce):
    """Potvrdí čekající žádost. Vrací (ok, zprava, rezervace).

    Kolize se tu neřeší: slot drží už podaná žádost (viz `_vloz_rezervaci`), takže
    dvě překrývající se živé žádosti na téhož vedoucího ani na téhož žadatele
    vzniknout nemohou. Podmínka `stav = 'ceka'` v UPDATE ošetří dvojklik i souběh
    vedoucího se správcem — proto stačí jeden atomický příkaz bez transakce.
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
        if not je_spravce and int(r['vedouci_user_id']) != int(kdo_user_id):
            return False, 'Tuto žádost může vyřídit jen vedoucí, kterého se týká.', None
        cur.execute("""UPDATE schuzky_rezervace
                          SET stav = %s, rozhodl_jmeno = %s, rozhodnuto_at = NOW()
                        WHERE id = %s AND stav = %s""",
                    (STAV_POTVRZENO, kdo_jmeno, rez_id, STAV_CEKA))
        if cur.rowcount == 0:
            conn.rollback()
            return False, 'Žádost už byla vyřízena.', None
        conn.commit()
        r['stav'] = STAV_POTVRZENO
        return True, '', r
    except Exception as e:
        print(f'[schuzky] _potvrd_rezervaci: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 'Potvrzení se nepodařilo uložit.', None
    finally:
        if cur:
            cur.close()
        conn.close()


def _zamitni_rezervaci(rez_id, kdo_jmeno, kdo_user_id, je_spravce, duvod):
    """Vrací (ok, zprava, rezervace)."""
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
        if not je_spravce and int(r['vedouci_user_id']) != int(kdo_user_id):
            return False, 'Tuto žádost může vyřídit jen vedoucí, kterého se týká.', None
        cur.execute("""UPDATE schuzky_rezervace
                          SET stav = %s, duvod_zamitnuti = %s,
                              rozhodl_jmeno = %s, rozhodnuto_at = NOW()
                        WHERE id = %s AND stav = %s""",
                    (STAV_ZAMITNUTO, _s(duvod), kdo_jmeno, rez_id, STAV_CEKA))
        if cur.rowcount == 0:
            conn.rollback()
            return False, 'Žádost už byla vyřízena.', None
        conn.commit()
        r['stav'] = STAV_ZAMITNUTO
        r['duvod_zamitnuti'] = _s(duvod)
        return True, '', r
    except Exception as e:
        print(f'[schuzky] _zamitni_rezervaci: {e}')
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 'Zamítnutí se nepodařilo uložit.', None
    finally:
        if cur:
            cur.close()
        conn.close()


def _zrus_rezervaci(rez_id, user_id):
    """Žadatel ruší svou dosud nevyřízenou žádost. Vrací (ok, zprava, rezervace)."""
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
        cur.execute("""UPDATE schuzky_rezervace SET stav = %s, rozhodnuto_at = NOW()
                        WHERE id = %s AND zadatel_user_id = %s AND stav = %s""",
                    (STAV_ZRUSENO, rez_id, user_id, STAV_CEKA))
        if cur.rowcount == 0:
            conn.rollback()
            return False, 'Zrušit lze jen vlastní dosud nepotvrzenou žádost.', None
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
        return f'{u}/schuzky' if u else ''
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


def _mail_nova_zadost(r, datum, vedouci_email):
    hl = _hlavicka(r, datum)
    text = (f'Dobrý den,\n\n{r["zadatel_jmeno"]} žádá o individuální schůzku.\n\n'
            f'Termín: {hl}\n'
            f'Zajistit ubytování: {"ANO" if r.get("ubytovani") else "ne"}\n'
            + (f'Poznámka: {_s(r.get("poznamka"))}\n' if _s(r.get('poznamka')) else '')
            + '\nŽádost prosím potvrďte nebo zamítněte v portálu.')
    _odesli_emaily([vedouci_email],
                   f'Nová žádost o schůzku – {hl} ({r["zadatel_jmeno"]})', text)


def _mail_rozhodnuto(r, datum, potvrzeno: bool, duvod=''):
    hl = _hlavicka(r, datum)
    if potvrzeno:
        text = (f'Dobrý den,\n\nVaše žádost o individuální schůzku byla POTVRZENA.\n\n'
                f'Termín: {hl}\nVedoucí: {r["vedouci_jmeno"]}\n'
                f'Zajistit ubytování: {"ANO" if r.get("ubytovani") else "ne"}')
        predmet = f'Schůzka potvrzena – {hl} s {r["vedouci_jmeno"]}'
    else:
        text = (f'Dobrý den,\n\nVaše žádost o individuální schůzku byla ZAMÍTNUTA.\n\n'
                f'Termín: {hl}\nVedoucí: {r["vedouci_jmeno"]}\n'
                + (f'Důvod: {_s(duvod)}\n' if _s(duvod) else '')
                + '\nVyberte si prosím jiný volný termín v portálu.')
        predmet = f'Schůzka zamítnuta – {hl} s {r["vedouci_jmeno"]}'
    _odesli_emaily([r.get('zadatel_email')], predmet, text)


# =========================================================
# HLAVNÍ VYKRESLOVACÍ FUNKCE
# =========================================================
@ui.refreshable
def vykresli_schuzky(user_id, user_name, user_email, vsechna_prava):
    inicializace_schuzky_db()

    je_admin = 'vse' in vsechna_prava
    je_spravce = je_admin or 'schuzky_spravce' in vsechna_prava
    je_vedouci = 'schuzky_vedouci' in vsechna_prava
    je_zadatel = je_spravce or 'schuzky_zadatel' in vsechna_prava

    vedouci = _vedouci_seznam()
    terminy = _terminy(vsechny=je_spravce)

    # --- výběr termínu (drží se v session, default = nejbližší budoucí) ---
    ids = [t['id'] for t in terminy]
    tid = app.storage.user.get('schuzky_termin')
    if tid not in ids:
        dnes = datetime.date.today()
        # přednost má nejbližší termín, na který jde ještě rezervovat
        otevrene = [t for t in terminy if t['aktivni'] and _pred_uzaverkou(t['datum'], dnes)]
        budouci = [t for t in terminy if t['datum'] >= dnes]
        tid = (otevrene[0]['id'] if otevrene
               else (budouci[0]['id'] if budouci else (ids[0] if ids else None)))
        app.storage.user['schuzky_termin'] = tid
    termin = next((t for t in terminy if t['id'] == tid), None)

    rezervace = _rezervace_terminu(tid) if tid else []

    # --- mapa obsazenosti: (vedouci_id, slot) → rezervace, která slot drží ---
    # Slot drží i dosud nepotvrzená žádost; uvolní ho až zamítnutí nebo zrušení.
    obsazeno = {}
    for r in rezervace:
        if r['stav'] in (STAV_ZAMITNUTO, STAV_ZRUSENO):
            continue
        od, do = _minuty(r['cas_od']), _minuty(r['cas_do'])
        for s in SLOTY:
            if od <= s < do:
                obsazeno[(int(r['vedouci_user_id']), s)] = r

    def _vidi_jmena(vid) -> bool:
        """Žadatel vidí jen šedou obsazenost; vedoucí jen svůj sloupec; správce vše."""
        return je_spravce or (je_vedouci and int(vid) == int(user_id))

    def _blokovane(vid) -> set:
        """Sloty, kam tento uživatel u tohoto vedoucího rezervovat NESMÍ —
        zabraný vedoucí NEBO vlastní schůzka jinde ve stejném čase."""
        return {s for (v, s), o in obsazeno.items()
                if v == int(vid) or int(o['zadatel_user_id']) == int(user_id)}

    po_uzaverce = bool(termin and not _pred_uzaverkou(termin['datum']))
    lze_rezervovat = bool(je_zadatel and termin and termin['aktivni']
                          and vedouci and not po_uzaverce)

    # =====================================================
    # DIALOG — nová rezervace
    # =====================================================
    def _dialog_nova(vid_pref=None, start_pref=None):
        if not lze_rezervovat:
            return
        with ui.dialog() as dlg, ui.card().classes('w-[560px] max-w-full p-6 gap-3'):
            ui.label('Nová rezervace schůzky').classes('text-xl font-bold text-gray-800')
            ui.label(f'Termín: {_datum_cz(termin["datum"])}   ·   '
                     f'{_hhmm(_minuty(PRAC_OD))}–{_hhmm(_minuty(PRAC_DO))}, '
                     f'délka po 10 min, max. 1,5 hodiny   ·   uzávěrka '
                     f'{_datum_cz(_uzaverka(termin["datum"]))}').classes('text-sm text-gray-500')

            sel_v = ui.select({v['id']: v['jmeno'] for v in vedouci}, label='Vedoucí',
                              value=(vid_pref if vid_pref in [v['id'] for v in vedouci]
                                     else vedouci[0]['id'])).classes('w-full').props('outlined')
            sel_od = ui.select({}, label='Začátek').classes('w-full').props('outlined')
            sel_delka = ui.select({}, label='Délka schůzky').classes('w-full').props('outlined')
            sw_ubyt = ui.switch('Zajistit ubytování')
            ta_pozn = ui.textarea('Poznámka (nepovinné)').classes('w-full').props('outlined rows=2')

            def _obnov_delky():
                vid, st = sel_v.value, sel_od.value
                mozne = (_mozne_delky(int(st), _blokovane(vid))
                         if (vid is not None and st is not None) else [])
                sel_delka.options = {d: DELKA_POPIS[d] for d in mozne}
                if sel_delka.value not in mozne:
                    # výchozí 30 min, jinak nejdelší kratší varianta
                    sel_delka.value = (30 if 30 in mozne else (mozne[-1] if mozne else None))
                sel_delka.update()

            def _obnov_casy():
                vid = sel_v.value
                blok = _blokovane(vid) if vid is not None else set()
                volne = [s for s in SLOTY if s not in blok]
                sel_od.options = {s: _hhmm(s) for s in volne}
                if sel_od.value not in volne:
                    sel_od.value = (start_pref if start_pref in volne
                                    else (volne[0] if volne else None))
                sel_od.update()
                _obnov_delky()

            sel_v.on_value_change(lambda _: _obnov_casy())
            sel_od.on_value_change(lambda _: _obnov_delky())
            _obnov_casy()

            def _uloz():
                v = next((x for x in vedouci if x['id'] == sel_v.value), None)
                if not v or sel_od.value is None or not sel_delka.value:
                    ui.notify('Vyberte vedoucího, začátek i délku schůzky.', type='warning')
                    return
                od_min, delka = int(sel_od.value), int(sel_delka.value)
                ok, zprava, rez_id = _vloz_rezervaci(
                    termin['id'], v, od_min, delka,
                    user_id, user_name, user_email, sw_ubyt.value, ta_pozn.value)
                dlg.close()
                if not ok:
                    ui.notify(zprava, type='negative')
                else:
                    r = {'cas_od': _sql_cas(od_min), 'cas_do': _sql_cas(od_min + delka),
                         'zadatel_jmeno': user_name, 'zadatel_email': user_email,
                         'vedouci_jmeno': v['jmeno'], 'ubytovani': sw_ubyt.value,
                         'poznamka': ta_pozn.value}
                    _mail_nova_zadost(r, termin['datum'], v['email'])
                    intranet_logger.log_activity(
                        user_name, 'Schůzky',
                        f'Žádost o schůzku #{rez_id} — {v["jmeno"]}, '
                        f'{_datum_cz(termin["datum"])} {_hhmm(od_min)}–{_hhmm(od_min + delka)}')
                    ui.notify('Žádost byla odeslána ke schválení.', type='positive')
                vykresli_schuzky.refresh()

            with ui.row().classes('w-full justify-end gap-2 pt-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat color=grey')
                ui.button('Odeslat žádost', icon='send', on_click=_uloz) \
                    .classes('bg-sky-600 text-white font-bold')
        dlg.open()

    # =====================================================
    # AKCE VEDOUCÍHO / SPRÁVCE
    # =====================================================
    def _akce_potvrd(rid):
        ok, zprava, r = _potvrd_rezervaci(rid, user_name, user_id, je_spravce)
        if not ok:
            ui.notify(zprava, type='negative')
        else:
            _mail_rozhodnuto(r, r.get('datum'), True)
            intranet_logger.log_activity(user_name, 'Schůzky',
                                         f'Potvrzena schůzka #{rid} s {r["zadatel_jmeno"]}')
            ui.notify('Schůzka potvrzena.', type='positive')
        vykresli_schuzky.refresh()

    def _dialog_zamitni(rid, jmeno):
        with ui.dialog() as dlg, ui.card().classes('w-[460px] max-w-full p-6 gap-3'):
            ui.label(f'Zamítnout žádost — {jmeno}').classes('text-lg font-bold')
            ta = ui.textarea('Důvod zamítnutí (nepovinné)').classes('w-full').props('outlined rows=3')

            def _ok():
                ok, zprava, r = _zamitni_rezervaci(rid, user_name, user_id, je_spravce, ta.value)
                dlg.close()
                if not ok:
                    ui.notify(zprava, type='negative')
                else:
                    _mail_rozhodnuto(r, r.get('datum'), False, ta.value)
                    intranet_logger.log_activity(user_name, 'Schůzky',
                                                 f'Zamítnuta schůzka #{rid} s {r["zadatel_jmeno"]}')
                    ui.notify('Žádost zamítnuta.', type='warning')
                vykresli_schuzky.refresh()

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zpět', on_click=dlg.close).props('flat color=grey')
                ui.button('Zamítnout', icon='block', on_click=_ok).classes('bg-red-600 text-white')
        dlg.open()

    def _akce_zrus(rid):
        ok, zprava, r = _zrus_rezervaci(rid, user_id)
        if not ok:
            ui.notify(zprava, type='negative')
        else:
            ui.notify('Žádost byla zrušena.', type='info')
            intranet_logger.log_activity(user_name, 'Schůzky', f'Zrušena vlastní žádost #{rid}')
        vykresli_schuzky.refresh()

    # =====================================================
    # LAYOUT
    # =====================================================
    with ui.column().classes('w-full p-4 gap-4'):
        with ui.row().classes('w-full items-center justify-between'):
            with ui.column().classes('gap-0'):
                ui.label('🗓️ Rezervace ind. schůzky s vedoucím').classes('text-3xl font-bold text-gray-800')
                ui.label(f'Schůzky {_hhmm(_minuty(PRAC_OD))}–{_hhmm(_minuty(PRAC_DO))}, '
                         f'délka 10–90 minut po 10. Rezervovat lze nejpozději '
                         f'{UZAVERKA_DNI} dní před termínem.').classes('text-sm text-gray-500')
            if lze_rezervovat:
                ui.button('Nová rezervace', icon='add', on_click=lambda: _dialog_nova()) \
                    .classes('bg-sky-600 text-white font-bold px-6 py-3 rounded-lg shadow-md')

        if not vedouci:
            ui.label('⚠️ Zatím nemá nikdo právo „Vedoucí schůzky" — rezervace nelze zakládat. '
                     'Přidělte právo v Uživatelích.') \
                .classes('w-full p-4 bg-amber-50 border border-amber-200 rounded-lg text-amber-800')
        if not terminy:
            ui.label('Zatím není vypsaný žádný termín.') \
                .classes('w-full p-4 bg-gray-50 border border-gray-200 rounded-lg text-gray-600')

        with ui.tabs().classes('w-full') as taby:
            t_kal = ui.tab('kalendar', label='Kalendář', icon='calendar_month')
            t_moje = ui.tab('moje', label='Moje žádosti', icon='list_alt')
            t_na_me = ui.tab('name', label='Žádosti na mě', icon='how_to_reg') \
                if (je_vedouci or je_spravce) else None
            t_term = ui.tab('terminy', label='Termíny', icon='event_repeat') if je_spravce else None

        with ui.tab_panels(taby, value=t_kal).classes('w-full bg-transparent'):

            # ---------- KALENDÁŘ ----------
            with ui.tab_panel(t_kal).classes('p-0'):
                with ui.row().classes('w-full items-center gap-2 flex-wrap pb-3'):
                    ui.label('Termín:').classes('font-bold text-gray-700')
                    for t in terminy:
                        aktivni_tab = (t['id'] == tid)
                        popis = _datum_cz(t['datum'])
                        if not t['aktivni']:
                            popis += ' (uzavřeno)'
                        elif not _pred_uzaverkou(t['datum']):
                            popis += ' (po uzávěrce)'

                        def _vyber(tid_new=t['id']):
                            app.storage.user['schuzky_termin'] = tid_new
                            vykresli_schuzky.refresh()

                        # barvu nese Quasar prop, ne Tailwind - .text-white z defaultu
                        # color=primary jinak přebije text-gray-* a popisek zmizí
                        ui.button(popis, on_click=_vyber) \
                            .props('no-caps' if aktivni_tab
                                   else 'no-caps color=white text-color=grey-8') \
                            .classes('font-bold rounded-lg px-4 ' +
                                     ('bg-sky-600 text-white' if aktivni_tab
                                      else 'border border-gray-300'))

                with ui.row().classes('w-full items-center gap-4 text-xs text-gray-600 pb-2 flex-wrap'):
                    ui.html('<span style="display:inline-block;width:14px;height:14px;'
                            'background:#d1d5db;border:1px solid #9ca3af;vertical-align:-2px"></span> potvrzeno')
                    ui.html('<span style="display:inline-block;width:14px;height:14px;'
                            'background:#fef3c7;border:1px solid #fcd34d;vertical-align:-2px"></span> '
                            'rezervováno, čeká na potvrzení (slot je už zabraný)')
                    ui.html('<span style="display:inline-block;width:14px;height:14px;'
                            'background:#ffffff;border:1px solid #d1d5db;vertical-align:-2px"></span> volno')
                    if not (je_spravce or je_vedouci):
                        ui.label('· jména účastníků vidí pouze vedoucí a správce')

                if po_uzaverce and termin['aktivni'] and je_zadatel:
                    ui.label(f'⏳ Uzávěrka rezervací na {_datum_cz(termin["datum"])} byla '
                             f'{_datum_cz(_uzaverka(termin["datum"]))} '
                             f'({UZAVERKA_DNI} dní předem). Termín lze už jen prohlížet.') \
                        .classes('w-full p-3 mb-2 bg-amber-50 border border-amber-200 '
                                 'rounded-lg text-amber-800 text-sm')

                if termin and vedouci:
                    with ui.column().classes('w-full gap-0 overflow-x-auto'):
                        with ui.row().classes('w-full gap-0 no-wrap'):
                            ui.label('').classes('w-16 shrink-0')
                            for v in vedouci:
                                ui.label(v['jmeno']).classes(
                                    'flex-1 min-w-[150px] text-center font-bold text-sm text-gray-700 '
                                    'py-2 bg-gray-100 border border-gray-300')
                        for s in SLOTY:
                            with ui.row().classes('w-full gap-0 no-wrap'):
                                ui.label(_hhmm(s)).classes(
                                    'w-16 shrink-0 text-xs text-gray-500 text-right pr-2 '
                                    'h-9 flex items-center justify-end')
                                for v in vedouci:
                                    _vykresli_bunku(v, s, obsazeno, _vidi_jmena,
                                                    _blokovane, lze_rezervovat, _dialog_nova)
                elif termin:
                    ui.label('Kalendář se zobrazí, jakmile bude nastaven alespoň jeden vedoucí.') \
                        .classes('text-gray-500 p-4')

            # ---------- MOJE ŽÁDOSTI ----------
            with ui.tab_panel(t_moje).classes('p-0'):
                moje = _moje_rezervace(user_id)
                if not moje:
                    ui.label('Zatím nemáte žádnou žádost o schůzku.').classes('text-gray-500 p-4')
                for r in moje:
                    popis, barva = STAV_POPIS.get(r['stav'], (r['stav'], 'bg-gray-100'))
                    with ui.card().classes('w-full p-4 mb-2 shadow-sm'):
                        with ui.row().classes('w-full items-center justify-between flex-wrap gap-2'):
                            with ui.column().classes('gap-0'):
                                ui.label(f'{_datum_cz(r["datum"])} · {_rozsah(r)}') \
                                    .classes('font-bold text-gray-800')
                                ui.label(f'Vedoucí: {_s(r["vedouci_jmeno"])} · '
                                         f'ubytování: {"ANO" if r["ubytovani"] else "ne"}') \
                                    .classes('text-sm text-gray-600')
                                if _s(r.get('poznamka')):
                                    ui.label(f'Poznámka: {_s(r["poznamka"])}').classes('text-xs text-gray-500')
                                if _s(r.get('duvod_zamitnuti')):
                                    ui.label(f'Důvod: {_s(r["duvod_zamitnuti"])}').classes('text-xs text-red-600')
                            with ui.row().classes('items-center gap-2'):
                                ui.label(popis).classes(f'{barva} px-3 py-1 rounded-full text-xs font-bold')
                                if r['stav'] == STAV_CEKA:
                                    ui.button('Zrušit', icon='close',
                                              on_click=lambda rid=r['id']: _akce_zrus(rid)) \
                                        .props('flat dense color=grey')

            # ---------- ŽÁDOSTI NA MĚ ----------
            if t_na_me is not None:
                with ui.tab_panel(t_na_me).classes('p-0'):
                    zadosti = _zadosti_na_vedouciho(None if je_spravce else user_id)
                    cekajici = [r for r in zadosti if r['stav'] == STAV_CEKA]
                    vyrizene = [r for r in zadosti if r['stav'] != STAV_CEKA]
                    if not zadosti:
                        ui.label('Žádné žádosti.').classes('text-gray-500 p-4')
                    for r in cekajici + vyrizene:
                        popis, barva = STAV_POPIS.get(r['stav'], (r['stav'], 'bg-gray-100'))
                        with ui.card().classes('w-full p-4 mb-2 shadow-sm'):
                            with ui.row().classes('w-full items-center justify-between flex-wrap gap-2'):
                                with ui.column().classes('gap-0'):
                                    ui.label(f'{_s(r["zadatel_jmeno"])} — {_datum_cz(r["datum"])} · {_rozsah(r)}') \
                                        .classes('font-bold text-gray-800')
                                    ui.label(f'Vedoucí: {_s(r["vedouci_jmeno"])} · '
                                             f'ubytování: {"ANO" if r["ubytovani"] else "ne"}') \
                                        .classes('text-sm text-gray-600')
                                    if _s(r.get('poznamka')):
                                        ui.label(f'Poznámka: {_s(r["poznamka"])}').classes('text-xs text-gray-500')
                                    if _s(r.get('duvod_zamitnuti')):
                                        ui.label(f'Důvod: {_s(r["duvod_zamitnuti"])}').classes('text-xs text-red-600')
                                with ui.row().classes('items-center gap-2'):
                                    ui.label(popis).classes(f'{barva} px-3 py-1 rounded-full text-xs font-bold')
                                    if r['stav'] == STAV_CEKA:
                                        ui.button('Potvrdit', icon='check',
                                                  on_click=lambda rid=r['id']: _akce_potvrd(rid)) \
                                            .classes('bg-green-600 text-white font-bold')
                                        ui.button('Zamítnout', icon='block',
                                                  on_click=lambda rid=r['id'], j=_s(r['zadatel_jmeno']):
                                                      _dialog_zamitni(rid, j)) \
                                            .props('flat').classes('text-red-600 font-bold')

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
                                             + ('' if _pred_uzaverkou(t['datum']) else ' — proběhla')) \
                                        .classes('text-xs ' + ('text-gray-500' if _pred_uzaverkou(t['datum'])
                                                               else 'text-amber-700 font-bold'))
                                    if _s(t.get('poznamka')):
                                        ui.label(_s(t['poznamka'])).classes('text-xs text-gray-500')
                                ui.switch('Otevřeno pro rezervace', value=bool(t['aktivni']),
                                          on_change=lambda e, tid_=t['id']: (
                                              _prepni_termin(tid_, e.value),
                                              vykresli_schuzky.refresh()))


def _vykresli_bunku(v, s, obsazeno, vidi_jmena, blokovane, lze_rezervovat, dialog_nova):
    """Jedna půlhodinová buňka kalendáře."""
    vid = int(v['id'])
    zaklad = ('flex-1 min-w-[150px] h-9 border border-gray-200 text-xs px-1 '
              'flex items-center justify-center text-center truncate')

    r = obsazeno.get((vid, s))
    if r:
        ceka = (r['stav'] == STAV_CEKA)
        prvni = _minuty(r['cas_od']) == s
        jmena = vidi_jmena(vid)
        if prvni:
            popis = _s(r['zadatel_jmeno']) if jmena else ('Rezervováno' if ceka else 'Obsazeno')
        else:
            popis = '·'
        barva = 'bg-amber-100 text-amber-800' if ceka else 'bg-gray-300 text-gray-700'
        el = ui.label(popis).classes(f'{zaklad} {barva} font-semibold')
        if prvni:
            detail = f'{_s(r["zadatel_jmeno"])} · ' if jmena else ''
            detail += _rozsah(r) + ' · ' + ('čeká na potvrzení' if ceka else 'potvrzeno')
            if jmena:
                detail += f' · ubytování: {"ANO" if r["ubytovani"] else "ne"}'
            el.tooltip(detail)
        return

    if s in blokovane(vid):
        # u vedoucího volno, ale žadatel má v tomto čase vlastní schůzku jinde
        ui.label('—').classes(f'{zaklad} bg-gray-100 text-gray-400') \
            .tooltip('V tomto čase už máte jinou schůzku.')
        return

    if lze_rezervovat:
        ui.label('').classes(f'{zaklad} bg-white cursor-pointer hover:bg-sky-100') \
            .on('click', lambda vid=vid, s=s: dialog_nova(vid, s))
    else:
        ui.label('').classes(f'{zaklad} bg-white')
