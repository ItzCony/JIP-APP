# intranet_komunikace.py
# ═══════════════════════════════════════════════════════════════════════════════
#   Komunikační portál JIP — místnosti (dlaždice), nástěnka, chat
# ═══════════════════════════════════════════════════════════════════════════════
import asyncio
import os
import uuid
from datetime import datetime

import intranet_data
import intranet_emaily
import intranet_notifikace
from nicegui import ui

# Adresář pro přílohy komentářů
KOM_PRILOHY_DIR = 'kom_prilohy'

# ───────────────────────────────────────────────────────────────────────────────
# KONSTANTY
# ───────────────────────────────────────────────────────────────────────────────
PERM_SCHVALOVAT = 'nastenkove_schvalovani'

_DB_INIT = False

# Per-user UI stav
_UI_STATE = {}   # user_id → {'aktivni_skupina_id': None}

# Připojení klienti dlaždice — client.id → {'refresh': fn, 'feed': fn|None,
# 'skupina_id': int|None}. Úklid přes client.on_disconnect (viz vykresli_komunikaci).
_KLIENTI = {}


def _broadcast(vyjimka=None):
    """Překreslí celou dlaždici všem připojeným klientům (kromě `vyjimka` —
    ten, kdo akci vyvolal, se překresluje sám lokálně). Volat jen při datové
    změně viditelné pro ostatní (nová/schválená místnost, archivace…)."""
    for cid, info in list(_KLIENTI.items()):
        if cid == vyjimka:
            continue
        try:
            info['refresh']()
        except Exception:
            _KLIENTI.pop(cid, None)


def _broadcast_feed(skupina_id, vyjimka=None):
    """Překreslí jen feed příspěvků klientům, kteří mají otevřenou stejnou
    místnost. Nesahá na rozepsaný text v hlavním vstupu (ten je mimo feed)."""
    for cid, info in list(_KLIENTI.items()):
        if cid == vyjimka or info.get('skupina_id') != skupina_id:
            continue
        fn = info.get('feed')
        if not fn:
            continue
        try:
            fn()
        except Exception:
            _KLIENTI.pop(cid, None)

STAV_META = {
    'ceka':      ('#fef3c7', '#92400e', '⏳', 'Čeká na schválení'),
    'schvaleno': ('#d1fae5', '#065f46', '✅', 'Schváleno'),
    'zamitnuto': ('#fee2e2', '#991b1b', '❌', 'Zamítnuto'),
    'vraceno':   ('#ffedd5', '#9a3412', '↩️', 'Vráceno k opravě'),
}

BARVY_AVATAR = [
    '#2563eb', '#7c3aed', '#0891b2', '#059669',
    '#d97706', '#dc2626', '#db2777', '#65a30d',
]


# ═══════════════════════════════════════════════════════════════════════════════
# DB INICIALIZACE
# ═══════════════════════════════════════════════════════════════════════════════
def _init_db():
    global _DB_INIT
    if _DB_INIT:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kom_skupiny (
                id               VARCHAR(36)  PRIMARY KEY,
                nazev            VARCHAR(200) NOT NULL,
                duvod            TEXT,
                stav             ENUM('ceka','schvaleno','zamitnuto','vraceno') DEFAULT 'ceka',
                vytvoril_id      INT NOT NULL,
                vytvoril_jmeno   VARCHAR(200),
                datum_vytvoreni  DATETIME DEFAULT CURRENT_TIMESTAMP,
                komentar_schval  TEXT,
                datum_schvaleni  DATETIME,
                schvalil_id      INT,
                je_verejna       TINYINT(1) DEFAULT 0,
                INDEX idx_stav      (stav),
                INDEX idx_vytvoril  (vytvoril_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kom_clenove (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                skupina_id  VARCHAR(36)  NOT NULL,
                email       VARCHAR(200) NOT NULL,
                UNIQUE KEY  uk_skup_email (skupina_id, email),
                INDEX       idx_email (email)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kom_prispevky (
                id           VARCHAR(36)  PRIMARY KEY,
                skupina_id   VARCHAR(36)  NOT NULL,
                autor_id     INT NOT NULL,
                autor_jmeno  VARCHAR(200),
                text         TEXT NOT NULL,
                datum        DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_sk_datum (skupina_id, datum)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kom_komentare (
                id            VARCHAR(36)  PRIMARY KEY,
                prispevek_id  VARCHAR(36)  NOT NULL,
                autor_id      INT NOT NULL,
                autor_jmeno   VARCHAR(200),
                text          TEXT NOT NULL,
                datum         DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_prispevek (prispevek_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kom_prilohy (
                id             VARCHAR(36)   PRIMARY KEY,
                komentar_id    VARCHAR(36)   NOT NULL,
                soubor_nazev   VARCHAR(500)  NOT NULL,
                soubor_cesta   VARCHAR(500)  NOT NULL,
                datum          DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_kom (komentar_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        conn.commit()
        # Migrace: přidat sloupec notifikovat_email pokud tabulka již existovala bez něj
        try:
            cur.execute("""
                ALTER TABLE kom_skupiny
                ADD COLUMN notifikovat_email TINYINT(1) DEFAULT 0
            """)
            conn.commit()
        except Exception:
            pass  # sloupec již existuje
        # Migrace: archivace místností
        for _ddl in (
            "ALTER TABLE kom_skupiny ADD COLUMN archivovano TINYINT(1) DEFAULT 0",
            "ALTER TABLE kom_skupiny ADD COLUMN datum_archivace DATETIME NULL",
            "ALTER TABLE kom_skupiny ADD COLUMN archivoval_id INT NULL",
        ):
            try:
                cur.execute(_ddl)
                conn.commit()
            except Exception:
                pass  # sloupec již existuje
        _DB_INIT = True
        _ensure_jip_room(cur, conn)
    except Exception as e:
        print(f'[Komunikace] DB init: {e}')
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _ensure_jip_room(cur, conn):
    """Zajistí existenci výchozí veřejné místnosti JIP."""
    try:
        cur.execute("SELECT id FROM kom_skupiny WHERE je_verejna = 1 LIMIT 1")
        if cur.fetchone():
            return
        jip_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO kom_skupiny
                (id, nazev, duvod, stav, vytvoril_id, vytvoril_jmeno, je_verejna, datum_schvaleni)
            VALUES (%s, 'JIP', 'Veřejná místnost pro všechny zaměstnance', 'schvaleno',
                    0, 'Systém', 1, NOW())
        """, (jip_id,))
        conn.commit()
    except Exception as e:
        print(f'[Komunikace] ensure_jip_room: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _get_email_uzivatele(user_id):
    """Vrátí e-mail uživatele z DB podle iduser."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return ''
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM user WHERE iduser = %s", (int(user_id),))
        row = cur.fetchone()
        return (row[0] or '').lower() if row else ''
    except Exception:
        return ''
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _notify_schvalovatele(text):
    """Notifikuje všechny uživatele s právem schvalovat nástěnky."""
    schvalovatele = intranet_data.ziskej_uzivatele_s_pravem(PERM_SCHVALOVAT, 'vse')
    for uid in schvalovatele:
        intranet_notifikace.pridej(uid, text, 'info')


def _notify_members(skupina_id, text, vyjimka_id=None):
    """Notifikuje členy skupiny, kteří mají účet v systému."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM kom_clenove WHERE skupina_id = %s", (skupina_id,))
        emails = [r[0].lower() for r in cur.fetchall()]
        if not emails:
            return
        placeholders = ','.join(['%s'] * len(emails))
        cur.execute(f"SELECT iduser FROM user WHERE LOWER(email) IN ({placeholders})", emails)
        vyjimky = (vyjimka_id if isinstance(vyjimka_id, (set, frozenset, list, tuple))
                   else {vyjimka_id})
        for (uid,) in cur.fetchall():
            if uid not in vyjimky:
                intranet_notifikace.pridej(uid, text, 'info')
    except Exception as e:
        print(f'[Komunikace] notify_members: {e}')
    finally:
        if cur:  cur.close()
        if conn: conn.close()


MENTION_MAX = 40          # max. délka rozepsaného jména za „@"
MENTION_NABIDKA = 8      # kolik jmen ukázat v nabídce


def _mention_dotaz(hodnota):
    """Rozepsaný text za posledním „@" (None = uživatel právě @ nepíše)."""
    i = (hodnota or '').rfind('@')
    if i < 0:
        return None
    chunk = hodnota[i + 1:]
    if len(chunk) > MENTION_MAX or '\n' in chunk or chunk.endswith(' '):
        return None
    return chunk.strip()


def najdi_mentions(text, kandidati):
    """Vrátí uid kandidátů, jejichž „@Jméno Příjmení" se vyskytuje v textu."""
    # ponytail: prosté hledání podřetězce, stačí na desítky členů místnosti;
    # „@Jan Novák" je i podřetězcem „@Jan Nováková" — dvojí notifikace je levnější
    # než tokenizace jmen.
    t = (text or '').lower()
    if '@' not in t:
        return set()
    return {k['uid'] for k in kandidati if '@' + k['jmeno'].lower() in t}


def ziskej_kandidaty_mention(skupina_id, je_verejna):
    """Lidé, které lze v místnosti označit přes @ — ve veřejné místnosti všichni
    aktivní uživatelé, jinak jen členové místnosti."""
    vsichni = intranet_data.ziskej_vsechny_uzivatele()
    povolene = None if je_verejna else {e.lower() for e in ziskej_clenove(skupina_id)}
    kandidati = [
        {'uid': u['id'], 'jmeno': u['jmeno_cele'].strip(), 'email': email}
        for email, u in vsichni.items()
        if u.get('aktivni') and (u.get('jmeno_cele') or '').strip()
        and (povolene is None or email.lower() in povolene)
    ]
    kandidati.sort(key=lambda k: k['jmeno'].lower())
    return kandidati


def _notify_mentions(text, skupina_id, je_verejna, nazev, autor_jmeno, autor_id):
    """Pošle zvoneček každému @označenému člověku místnosti. Vrací set uid."""
    if '@' not in (text or ''):
        return set()
    oznaceni = najdi_mentions(text, ziskej_kandidaty_mention(skupina_id, je_verejna))
    oznaceni.discard(autor_id)
    nahled = text[:80] + ('…' if len(text) > 80 else '')
    for uid in oznaceni:
        intranet_notifikace.pridej(
            uid, f'🔔 {autor_jmeno} vás označil/a v místnosti „{nazev}": {nahled}', 'warning')
    return oznaceni


MENTION_TRIDA = 'jip-mention-open'   # třída, podle které JS pozná otevřenou nabídku
# Šipky/Enter/Tab/Esc si bereme (a rušíme jejich výchozí chování) jen když nabídka
# opravdu visí v DOM — jinak klávesa projde do pole jako obvykle.
MENTION_JS_V_MENU = ('(e) => { if (document.querySelector(".%s"))'
                     ' { e.preventDefault(); emit(); } }' % MENTION_TRIDA)
MENTION_JS_MIMO = ('(e) => { if (!document.querySelector(".%s")) emit(); }' % MENTION_TRIDA)


def _pripoj_mention(vstup, kandidati):
    """Napojí na textové pole nabídku členů místnosti, která vyskočí po „@".
    V nabídce se chodí šipkami, potvrzuje Enterem/Tabem, zavírá Escapem."""
    if not kandidati:
        return
    with vstup:
        menu = ui.menu().props('no-focus no-refocus no-parent-event') \
                        .classes(f'{MENTION_TRIDA} max-h-96 overflow-y-auto')
    stav = {'shoda': [], 'idx': 0}

    def _vloz(k):
        hodnota = vstup.value or ''
        i = hodnota.rfind('@')
        if i < 0:
            return
        vstup.set_value(hodnota[:i] + '@' + k['jmeno'] + ' ')
        _zavri()

    def _zavri():
        stav['shoda'] = []
        menu.close()

    def _vykresli():
        menu.clear()
        with menu:
            for i, k in enumerate(stav['shoda']):
                polozka = ui.menu_item(k['jmeno'], on_click=lambda _k=k: _vloz(_k))
                if i == stav['idx']:
                    polozka.classes('bg-blue-50 text-blue-700 font-bold')
        menu.open()

    def _obnov():
        dotaz = _mention_dotaz(vstup.value)
        stav['shoda'] = ([k for k in kandidati if dotaz.lower() in k['jmeno'].lower()]
                         [:MENTION_NABIDKA] if dotaz is not None else [])
        stav['idx'] = 0
        if not stav['shoda']:
            _zavri()
            return
        _vykresli()

    def _posun(o):
        if not stav['shoda']:
            return
        stav['idx'] = (stav['idx'] + o) % len(stav['shoda'])
        _vykresli()

    def _potvrd():
        if stav['shoda']:
            _vloz(stav['shoda'][stav['idx']])

    vstup.on_value_change(lambda _: _obnov())
    vstup.on('keydown.down', lambda e: _posun(1), js_handler=MENTION_JS_V_MENU)
    vstup.on('keydown.up', lambda e: _posun(-1), js_handler=MENTION_JS_V_MENU)
    vstup.on('keydown.enter', lambda e: _potvrd(), js_handler=MENTION_JS_V_MENU)
    vstup.on('keydown.tab', lambda e: _potvrd(), js_handler=MENTION_JS_V_MENU)
    vstup.on('keydown.esc', lambda e: _zavri(), js_handler=MENTION_JS_V_MENU)


def _fmt_datum(dt):
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt[:16].replace('T', ' ')
    return dt.strftime('%d.%m.%Y %H:%M')


def _avatar_color(text):
    idx = sum(ord(c) for c in (text or '?')) % len(BARVY_AVATAR)
    return BARVY_AVATAR[idx]


def _initials(jmeno):
    parts = (jmeno or '?').split()
    return ''.join(p[0].upper() for p in parts[:2])


def _ziskej_emaily_vsech():
    """Vrátí dict {email: 'Jméno Příjmení (email)'} všech aktivních uživatelů."""
    vsichni = intranet_data.ziskej_vsechny_uzivatele()
    result = {}
    for email, u in vsichni.items():
        jmeno = f"{u.get('name', '')} {u.get('surname', '')}".strip()
        label = f"{jmeno} ({email})" if jmeno else email
        result[email.lower()] = label
    return result


def _nazev_existuje(nazev):
    """Vrátí True pokud místnost se stejným názvem (case-insensitive) již existuje
    a není zamítnutá."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM kom_skupiny
            WHERE LOWER(nazev) = LOWER(%s) AND stav != 'zamitnuto'
            LIMIT 1
        """, (nazev.strip(),))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _email_nove_nastenky(skupina_id, nazev):
    """Po schválení místnosti rozešle email všem jejím členům."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM kom_clenove WHERE skupina_id = %s", (skupina_id,))
        emails = [r[0] for r in cur.fetchall()]
    except Exception as e:
        print(f'[Komunikace] _email_nove_nastenky load: {e}')
        return
    finally:
        if cur:  cur.close()
        if conn: conn.close()

    for email in emails:
        try:
            intranet_emaily.odesli_upozorneni_email(
                email,
                f'Byla vytvořena komunikační místnost „{nazev}"',
                f'Dobrý den,\n\n'
                f'komunikační místnost „{nazev}" na portálu Moje JIPka byla schválena '
                f'a Vy jste jejím členem.\n\n'
                f'Nyní se můžete přihlásit a začít komunikovat.\n\n'
                f'-- Portál Moje JIPka'
            )
        except Exception as e:
            print(f'[Komunikace] email pro {email}: {e}')


def _email_nova_verejna_mistnost(nazev, vytvoril_jmeno):
    """Rozešle e-mail všem aktivním uživatelům o nové globální (veřejné) místnosti."""
    vsichni = intranet_data.ziskej_vsechny_uzivatele()
    for email in vsichni:
        try:
            intranet_emaily.odesli_upozorneni_email(
                email,
                f'Nová globální komunikační místnost „{nazev}"',
                f'Dobrý den,\n\n'
                f'na portálu Moje JIPka byla vytvořena nová globální komunikační místnost '
                f'„{nazev}" (vytvořil/a: {vytvoril_jmeno}).\n\n'
                f'Místnost je dostupná všem zaměstnancům — přihlaste se a přidejte se ke konverzaci.\n\n'
                f'-- Portál Moje JIPka'
            )
        except Exception as e:
            print(f'[Komunikace] _email_nova_verejna pro {email}: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# READ FUNKCE
# ═══════════════════════════════════════════════════════════════════════════════
def ziskej_skupiny(user_id, user_email):
    """Vrátí místnosti viditelné pro uživatele (schválené + vlastní čekající)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        # Schválené místnosti: veřejné NEBO jsem člen NEBO jsem tvůrce
        cur.execute("""
            SELECT DISTINCT s.*
            FROM kom_skupiny s
            LEFT JOIN kom_clenove c ON c.skupina_id = s.id AND LOWER(c.email) = %s
            WHERE s.stav = 'schvaleno'
              AND (s.je_verejna = 1 OR c.email IS NOT NULL OR s.vytvoril_id = %s)
            ORDER BY s.nazev
        """, (user_email.lower(), user_id))
        schvalene = cur.fetchall()
        # Moje vlastní čekající/vrácené/zamítnuté
        cur.execute("""
            SELECT * FROM kom_skupiny
            WHERE vytvoril_id = %s AND stav != 'schvaleno'
            ORDER BY datum_vytvoreni DESC
        """, (user_id,))
        vlastni = cur.fetchall()
        ids = {s['id'] for s in schvalene}
        for s in vlastni:
            if s['id'] not in ids:
                schvalene.append(s)
        return schvalene
    except Exception as e:
        print(f'[Komunikace] ziskej_skupiny: {e}')
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_cekajici():
    """Vrátí žádosti čekající na schválení (stav ceka nebo vraceno — po opravě)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT s.*, GROUP_CONCAT(c.email ORDER BY c.email SEPARATOR ', ') AS clenove_str
            FROM kom_skupiny s
            LEFT JOIN kom_clenove c ON c.skupina_id = s.id
            WHERE s.stav = 'ceka'
            GROUP BY s.id
            ORDER BY s.datum_vytvoreni
        """)
        return cur.fetchall()
    except Exception as e:
        print(f'[Komunikace] ziskej_cekajici: {e}')
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_skupinu(skupina_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM kom_skupiny WHERE id = %s", (skupina_id,))
        return cur.fetchone()
    except Exception:
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_clenove(skupina_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM kom_clenove WHERE skupina_id = %s ORDER BY email", (skupina_id,))
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_prispevky(skupina_id, limit=60):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM kom_prispevky WHERE skupina_id = %s
            ORDER BY datum DESC LIMIT %s
        """, (skupina_id, limit))
        return cur.fetchall()
    except Exception as e:
        print(f'[Komunikace] ziskej_prispevky: {e}')
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_komentare(prispevek_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM kom_komentare WHERE prispevek_id = %s ORDER BY datum
        """, (prispevek_id,))
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_prilohy_komentare(komentar_id):
    """Vrátí seznam příloh daného komentáře."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM kom_prilohy WHERE komentar_id = %s ORDER BY datum
        """, (komentar_id,))
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def ziskej_prilohy_skupiny(skupina_id):
    """Vrátí všechny přílohy v místnosti seřazené od nejnovější."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT pr.id, pr.soubor_nazev, pr.soubor_cesta, pr.datum,
                   k.autor_jmeno, k.autor_id
            FROM kom_prilohy pr
            JOIN kom_komentare k ON k.id = pr.komentar_id
            JOIN kom_prispevky p ON p.id = k.prispevek_id
            WHERE p.skupina_id = %s
            ORDER BY pr.datum DESC
        """, (skupina_id,))
        return cur.fetchall()
    except Exception as e:
        print(f'[Komunikace] ziskej_prilohy_skupiny: {e}')
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def pocet_prispevku(skupina_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM kom_prispevky WHERE skupina_id = %s", (skupina_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# WRITE FUNKCE
# ═══════════════════════════════════════════════════════════════════════════════
def vytvor_verejnou_skupinu(nazev, duvod, user_id, user_name, muze_schvalovat=False):
    """Vytvoří globální (veřejnou) místnost ihned jako schválenou.
    Povoleno pouze uživatelům s právem schvalovat místnosti."""
    if not muze_schvalovat:
        return None
    if _nazev_existuje(nazev):
        return 'duplicit'
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    skupina_id = str(uuid.uuid4())
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kom_skupiny
                (id, nazev, duvod, stav, vytvoril_id, vytvoril_jmeno,
                 je_verejna, schvalil_id, datum_schvaleni)
            VALUES (%s, %s, %s, 'schvaleno', %s, %s, 1, %s, NOW())
        """, (skupina_id, nazev[:200], duvod, user_id, user_name, user_id))
        conn.commit()
        return skupina_id
    except Exception as e:
        print(f'[Komunikace] vytvor_verejnou_skupinu: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def vytvor_zadost(nazev, duvod, clenove_emails, user_id, user_name):
    """Vytvoří žádost o místnost (stav: ceka) a notifikuje schvalovatele."""
    if _nazev_existuje(nazev):
        return 'duplicit'
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    skupina_id = str(uuid.uuid4())
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kom_skupiny (id, nazev, duvod, stav, vytvoril_id, vytvoril_jmeno)
            VALUES (%s, %s, %s, 'ceka', %s, %s)
        """, (skupina_id, nazev[:200], duvod, user_id, user_name))
        for email in clenove_emails:
            email = email.strip().lower()
            if email:
                cur.execute(
                    "INSERT IGNORE INTO kom_clenove (skupina_id, email) VALUES (%s, %s)",
                    (skupina_id, email))
        conn.commit()
        _notify_schvalovatele(
            f'📋 Nová žádost o komunikační místnost „{nazev}" od {user_name} čeká na schválení.')
        return skupina_id
    except Exception as e:
        print(f'[Komunikace] vytvor_zadost: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def uprav_zadost(skupina_id, nazev, duvod, clenove_emails, user_id):
    """Upraví vrácenou žádost a znovu ji zařadí do fronty ke schválení."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT stav FROM kom_skupiny WHERE id = %s AND vytvoril_id = %s",
                    (skupina_id, user_id))
        row = cur.fetchone()
        if not row or row['stav'] != 'vraceno':
            return False
        cur = conn.cursor()
        cur.execute("""
            UPDATE kom_skupiny
            SET nazev=%s, duvod=%s, stav='ceka', komentar_schval=NULL
            WHERE id=%s
        """, (nazev[:200], duvod, skupina_id))
        cur.execute("DELETE FROM kom_clenove WHERE skupina_id = %s", (skupina_id,))
        for email in clenove_emails:
            email = email.strip().lower()
            if email:
                cur.execute(
                    "INSERT IGNORE INTO kom_clenove (skupina_id, email) VALUES (%s, %s)",
                    (skupina_id, email))
        conn.commit()
        _notify_schvalovatele(
            f'📋 Opravená žádost o místnost „{nazev}" čeká na schválení.')
        return True
    except Exception as e:
        print(f'[Komunikace] uprav_zadost: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def schval_skupinu(skupina_id, schvalil_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM kom_skupiny WHERE id = %s", (skupina_id,))
        sk = cur.fetchone()
        if not sk:
            return False
        cur = conn.cursor()
        cur.execute("""
            UPDATE kom_skupiny
            SET stav='schvaleno', schvalil_id=%s, datum_schvaleni=NOW()
            WHERE id=%s
        """, (schvalil_id, skupina_id))
        conn.commit()
        intranet_notifikace.pridej(
            sk['vytvoril_id'],
            f'✅ Vaše žádost o místnost „{sk["nazev"]}" byla schválena. Nyní můžete přidávat příspěvky!',
            'success')
        import threading
        threading.Thread(
            target=_email_nove_nastenky,
            args=(skupina_id, sk['nazev']),
            daemon=True
        ).start()
        return True
    except Exception as e:
        print(f'[Komunikace] schval: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def zamitni_skupinu(skupina_id, komentar, schvalil_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM kom_skupiny WHERE id = %s", (skupina_id,))
        sk = cur.fetchone()
        if not sk:
            return False
        cur = conn.cursor()
        cur.execute("""
            UPDATE kom_skupiny
            SET stav='zamitnuto', schvalil_id=%s, datum_schvaleni=NOW(), komentar_schval=%s
            WHERE id=%s
        """, (schvalil_id, komentar, skupina_id))
        conn.commit()
        intranet_notifikace.pridej(
            sk['vytvoril_id'],
            f'❌ Žádost o místnost „{sk["nazev"]}" byla zamítnuta. Důvod: {komentar}',
            'error')
        return True
    except Exception as e:
        print(f'[Komunikace] zamitni: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def vrat_skupinu(skupina_id, komentar, schvalil_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM kom_skupiny WHERE id = %s", (skupina_id,))
        sk = cur.fetchone()
        if not sk:
            return False
        cur = conn.cursor()
        cur.execute("""
            UPDATE kom_skupiny
            SET stav='vraceno', schvalil_id=%s, datum_schvaleni=NOW(), komentar_schval=%s
            WHERE id=%s
        """, (schvalil_id, komentar, skupina_id))
        conn.commit()
        intranet_notifikace.pridej(
            sk['vytvoril_id'],
            f'↩️ Žádost o místnost „{sk["nazev"]}" vrácena k opravě. Připomínka: {komentar}',
            'warning')
        return True
    except Exception as e:
        print(f'[Komunikace] vrat: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def uloz_prilohu(komentar_id, soubor_nazev, obsah_bytes):
    """Uloží přílohu na disk a zapíše záznam do DB. Vrátí id nebo None."""
    _init_db()
    os.makedirs(KOM_PRILOHY_DIR, exist_ok=True)
    priloha_id = str(uuid.uuid4())
    ext = os.path.splitext(soubor_nazev)[1]
    soubor_cesta = os.path.join(KOM_PRILOHY_DIR, f'{priloha_id}{ext}')
    try:
        with open(soubor_cesta, 'wb') as f:
            f.write(obsah_bytes)
    except Exception as e:
        print(f'[Komunikace] uloz_prilohu zapis: {e}')
        return None
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kom_prilohy (id, komentar_id, soubor_nazev, soubor_cesta)
            VALUES (%s, %s, %s, %s)
        """, (priloha_id, komentar_id, soubor_nazev, soubor_cesta))
        conn.commit()
        return priloha_id
    except Exception as e:
        print(f'[Komunikace] uloz_prilohu db: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def pridej_prispevek(skupina_id, text, autor_id, autor_jmeno):
    """Přidá příspěvek na nástěnku a notifikuje členy skupiny."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    prispevek_id = str(uuid.uuid4())
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT nazev, je_verejna FROM kom_skupiny "
                    "WHERE id = %s AND stav = 'schvaleno' AND archivovano = 0",
                    (skupina_id,))
        sk = cur.fetchone()
        if not sk:
            return None
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kom_prispevky (id, skupina_id, autor_id, autor_jmeno, text)
            VALUES (%s, %s, %s, %s, %s)
        """, (prispevek_id, skupina_id, autor_id, autor_jmeno, text))
        conn.commit()
        oznaceni = _notify_mentions(text, skupina_id, sk['je_verejna'],
                                    sk['nazev'], autor_jmeno, autor_id)
        if not sk['je_verejna']:
            nahled = text[:80] + ('…' if len(text) > 80 else '')
            _notify_members(
                skupina_id,
                f'📌 Nový příspěvek v místnosti „{sk["nazev"]}" od {autor_jmeno}: {nahled}',
                vyjimka_id={autor_id} | oznaceni)
        return prispevek_id
    except Exception as e:
        print(f'[Komunikace] pridej_prispevek: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def pridej_komentar(prispevek_id, skupina_id, text, autor_id, autor_jmeno):
    """Přidá komentář a notifikuje autora příspěvku."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    komentar_id = str(uuid.uuid4())
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT autor_id, autor_jmeno FROM kom_prispevky WHERE id = %s", (prispevek_id,))
        prispevek = cur.fetchone()
        cur.execute("SELECT nazev, archivovano, je_verejna FROM kom_skupiny WHERE id = %s",
                    (skupina_id,))
        sk = cur.fetchone()
        if not prispevek or (sk and sk['archivovano']):
            return False
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kom_komentare (id, prispevek_id, autor_id, autor_jmeno, text)
            VALUES (%s, %s, %s, %s, %s)
        """, (komentar_id, prispevek_id, autor_id, autor_jmeno, text))
        conn.commit()
        oznaceni = (_notify_mentions(text, skupina_id, sk['je_verejna'],
                                     sk['nazev'], autor_jmeno, autor_id) if sk else set())
        if prispevek['autor_id'] != autor_id and prispevek['autor_id'] not in oznaceni:
            nazev_sk = sk['nazev'] if sk else '?'
            nahled = text[:60] + ('…' if len(text) > 60 else '')
            intranet_notifikace.pridej(
                prispevek['autor_id'],
                f'💬 {autor_jmeno} okomentoval váš příspěvek v „{nazev_sk}": {nahled}',
                'info')
        return komentar_id   # vrací ID pro případné přiložení přílohy
    except Exception as e:
        print(f'[Komunikace] pridej_komentar: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def smazat_komentar(komentar_id, autor_id, muze_vse=False):
    """Smaže komentář včetně příloh (jen autor nebo admin)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        if not muze_vse:
            cur.execute(
                "SELECT id FROM kom_komentare WHERE id = %s AND autor_id = %s",
                (komentar_id, autor_id))
            if not cur.fetchone():
                return False
        cur.execute("DELETE FROM kom_prilohy  WHERE komentar_id = %s", (komentar_id,))
        cur.execute("DELETE FROM kom_komentare WHERE id = %s",          (komentar_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] smazat_komentar: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def smazat_prispevek(prispevek_id, autor_id, muze_vse=False):
    """Smaže příspěvek i jeho komentáře (jen autor nebo admin)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        if muze_vse:
            cur.execute("DELETE FROM kom_komentare WHERE prispevek_id = %s", (prispevek_id,))
            cur.execute("DELETE FROM kom_prispevky WHERE id = %s", (prispevek_id,))
        else:
            cur.execute(
                "DELETE FROM kom_prispevky WHERE id = %s AND autor_id = %s",
                (prispevek_id, autor_id))
            if cur.rowcount > 0:
                cur.execute("DELETE FROM kom_komentare WHERE prispevek_id = %s", (prispevek_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] smazat_prispevek: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def pridej_clena_skupiny(skupina_id, email, user_id, muze_vse=False):
    """Přidá člena do existující schválené místnosti (tvůrce nebo admin)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT vytvoril_id, je_verejna, stav, archivovano FROM kom_skupiny WHERE id = %s",
                    (skupina_id,))
        sk = cur.fetchone()
        if not sk or sk['stav'] != 'schvaleno' or sk['archivovano']:
            return False
        if sk['je_verejna']:
            return False
        if not muze_vse and sk['vytvoril_id'] != user_id:
            return False
        cur = conn.cursor()
        cur.execute(
            "INSERT IGNORE INTO kom_clenove (skupina_id, email) VALUES (%s, %s)",
            (skupina_id, email.strip().lower()))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] pridej_clena: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def odeber_clena_skupiny(skupina_id, email, user_id, muze_vse=False):
    """Odebere člena z existující místnosti (tvůrce nebo admin)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT vytvoril_id, je_verejna, stav FROM kom_skupiny WHERE id = %s",
                    (skupina_id,))
        sk = cur.fetchone()
        if not sk or sk['stav'] != 'schvaleno':
            return False
        if sk['je_verejna']:
            return False
        if not muze_vse and sk['vytvoril_id'] != user_id:
            return False
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM kom_clenove WHERE skupina_id = %s AND LOWER(email) = %s",
            (skupina_id, email.strip().lower()))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] odeber_clena: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def smazat_skupinu(skupina_id, user_id, muze_vse=False):
    """Smaže místnost včetně příspěvků a komentářů (tvůrce nebo admin)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT vytvoril_id, je_verejna FROM kom_skupiny WHERE id = %s", (skupina_id,))
        sk = cur.fetchone()
        if not sk:
            return False
        if sk['je_verejna']:          # Veřejnou JIP místnost nelze smazat
            return False
        if not muze_vse and sk['vytvoril_id'] != user_id:
            return False
        cur = conn.cursor()
        # Smazat v pořadí FK závislostí
        cur.execute("""
            DELETE k FROM kom_komentare k
            JOIN kom_prispevky p ON k.prispevek_id = p.id
            WHERE p.skupina_id = %s
        """, (skupina_id,))
        cur.execute("DELETE FROM kom_prispevky WHERE skupina_id = %s", (skupina_id,))
        cur.execute("DELETE FROM kom_clenove   WHERE skupina_id = %s", (skupina_id,))
        cur.execute("DELETE FROM kom_skupiny   WHERE id = %s",         (skupina_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] smazat_skupinu: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def archivuj_skupinu(skupina_id, user_id, muze_vse=False):
    """Archivuje místnost (jen tvůrce/správce nebo admin). Diskuze přejde do režimu
    jen pro čtení — nelze přidávat ani upravovat příspěvky, komentáře a členy."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT vytvoril_id, stav, archivovano FROM kom_skupiny WHERE id = %s",
                    (skupina_id,))
        sk = cur.fetchone()
        if not sk or sk['stav'] != 'schvaleno' or sk['archivovano']:
            return False
        if not muze_vse and sk['vytvoril_id'] != user_id:
            return False
        cur = conn.cursor()
        cur.execute("""
            UPDATE kom_skupiny
            SET archivovano=1, datum_archivace=NOW(), archivoval_id=%s
            WHERE id=%s
        """, (user_id, skupina_id))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] archivuj_skupinu: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def obnovit_skupinu(skupina_id, user_id, muze_vse=False):
    """Obnoví archivovanou místnost zpět mezi aktivní (jen tvůrce/správce nebo admin)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT vytvoril_id, archivovano FROM kom_skupiny WHERE id = %s",
                    (skupina_id,))
        sk = cur.fetchone()
        if not sk or not sk['archivovano']:
            return False
        if not muze_vse and sk['vytvoril_id'] != user_id:
            return False
        cur = conn.cursor()
        cur.execute("""
            UPDATE kom_skupiny
            SET archivovano=0, datum_archivace=NULL, archivoval_id=NULL
            WHERE id=%s
        """, (skupina_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Komunikace] obnovit_skupinu: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# HLAVNÍ UI
# ═══════════════════════════════════════════════════════════════════════════════
async def vykresli_komunikaci(user_id, user_name, vsechna_prava):
    """Vstupní bod dlaždice.

    Refreshable je per-klient — navigace jednoho uživatele (otevření místnosti,
    návrat, přepnutí archivu) NEpřekresluje ostatní. Datové změny se ostatním
    šíří cíleně přes _broadcast() / _broadcast_feed().
    """
    try:
        klient = ui.context.client
    except Exception:
        klient = None

    @ui.refreshable
    async def _render():
        await _vykresli_telo(user_id, user_name, vsechna_prava, _render.refresh)

    if klient is not None:
        _KLIENTI[klient.id] = {'refresh': _render.refresh,
                               'feed': None, 'skupina_id': None}
        klient.on_disconnect(lambda: _KLIENTI.pop(klient.id, None))
    await _render()


async def _vykresli_telo(user_id, user_name, vsechna_prava, muj_refresh):
    # DB dotazy běží ve vlákně — nedrží event loop celého serveru
    await asyncio.to_thread(_init_db)
    try:
        _cid = ui.context.client.id
    except Exception:
        _cid = None

    # ── Per-user stav ─────────────────────────────────────────────────────────
    if user_id not in _UI_STATE:
        _UI_STATE[user_id] = {'aktivni_skupina_id': None, 'zobrazit_archiv': False}
    st = _UI_STATE[user_id]
    st.setdefault('zobrazit_archiv', False)

    user_email = await asyncio.to_thread(_get_email_uzivatele, user_id)
    muze_schvalovat = 'vse' in vsechna_prava or PERM_SCHVALOVAT in vsechna_prava

    # ══════════════════════════════════════════════════════════════════════════
    # DIALOGY (definovány před obsahem, vždy přítomné v DOM)
    # ══════════════════════════════════════════════════════════════════════════

    # ── Načtení emailů pro našeptávač ─────────────────────────────────────────
    _email_options = await asyncio.to_thread(_ziskej_emaily_vsech)   # {email: "Jméno (email)"}

    # ── Dialog: Nová místnost ─────────────────────────────────────────────────
    with ui.dialog().classes('!max-w-2xl w-full') as dlg_nova:
        with ui.card().classes('w-full p-6'):
            ui.label('Nová komunikační místnost').classes('text-xl font-bold text-gray-800 mb-1')
            ui.label('Žádost bude posouzena schvalovatelem. Po schválení (zpravidla do 24 h) získáte přístup.') \
                .classes('text-sm text-blue-700 bg-blue-50 border border-blue-200 p-3 rounded-lg mb-4')
            n_nazev  = ui.input('Název místnosti *').classes('w-full mb-3').props('outlined')
            n_duvod  = ui.textarea('Důvod zřízení *', placeholder='Popište, k čemu místnost slouží…') \
                         .classes('w-full mb-3').props('outlined rows=3')
            ui.label('Členové místnosti').classes('text-xs font-bold text-gray-600 mb-1')
            n_clenove = ui.select(
                options=_email_options,
                multiple=True,
                label='Vyberte nebo napište e-mail…',
                value=[],
            ).classes('w-full mb-4').props(
                'outlined use-input use-chips new-value-mode=add-unique '
                'input-debounce=0 hide-dropdown-icon'
            )
            ui.label('Tip: vyberte ze seznamu nebo napište libovolný e-mail a potvrďte Enterem.') \
                .classes('text-xs text-gray-400 -mt-3 mb-4')

            def _podat_zadost():
                if not (n_nazev.value or '').strip():
                    ui.notify('Zadejte název místnosti!', type='warning'); return
                if not (n_duvod.value or '').strip():
                    ui.notify('Zadejte důvod zřízení!', type='warning'); return
                raw_emails = n_clenove.value or []
                emails = [e.strip().lower() for e in raw_emails if e.strip()]
                vysledek = vytvor_zadost(n_nazev.value.strip(), n_duvod.value.strip(),
                                         emails, user_id, user_name)
                if vysledek == 'duplicit':
                    ui.notify('Místnost s tímto názvem již existuje!', type='warning')
                    return
                dlg_nova.close()
                ui.notify('✅ Žádost odeslána. Budete informováni o výsledku.', type='positive')
                muj_refresh()
                _broadcast(vyjimka=_cid)   # schvalovatelé uvidí novou žádost

            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                ui.button('Zrušit', on_click=dlg_nova.close).props('flat').classes('text-gray-500')
                ui.button('📨 Odeslat žádost', on_click=_podat_zadost) \
                    .classes('bg-blue-600 text-white font-bold')

    # ── Dialog: Schválení / zamítnutí / vrácení ───────────────────────────────
    _akce_ctx = {'typ': None, 'skupina_id': None, 'nazev': ''}
    with ui.dialog().classes('!max-w-lg w-full') as dlg_akce:
        with ui.card().classes('w-full p-6'):
            lbl_akce = ui.label('').classes('text-lg font-bold mb-3')
            lbl_akce_hint = ui.label('').classes('text-sm text-gray-500 mb-3')
            a_komentar = ui.textarea('Komentář / připomínka') \
                           .classes('w-full mb-4').props('outlined rows=3')

            def _proved_akci():
                typ = _akce_ctx['typ']
                sid = _akce_ctx['skupina_id']
                kom = (a_komentar.value or '').strip()
                if typ in ('zamitni', 'vrat') and not kom:
                    ui.notify('Zadejte komentář!', type='warning'); return
                if   typ == 'schval':   schval_skupinu(sid, user_id)
                elif typ == 'zamitni':  zamitni_skupinu(sid, kom, user_id)
                elif typ == 'vrat':     vrat_skupinu(sid, kom, user_id)
                dlg_akce.close()
                ui.notify('Hotovo.', type='positive')
                muj_refresh()
                _broadcast(vyjimka=_cid)   # žadatel/členové uvidí výsledek schválení

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg_akce.close).props('flat').classes('text-gray-500')
                ui.button('Potvrdit', on_click=_proved_akci).classes('bg-blue-600 text-white font-bold')

    def _otevri_akci(typ, skupina_id, nazev):
        _akce_ctx.update({'typ': typ, 'skupina_id': skupina_id, 'nazev': nazev})
        a_komentar.set_value('')
        nadpisy = {
            'schval':   (f'✅ Schválit místnost „{nazev}"?',  'Místnost bude okamžitě dostupná.'),
            'zamitni':  (f'❌ Zamítnout místnost „{nazev}"',  'Zadejte důvod zamítnutí — zadavatel jej obdrží v notifikaci.'),
            'vrat':     (f'↩️ Vrátit místnost „{nazev}" k opravě', 'Zadejte, co je třeba opravit. Zadavatel žádost upraví a znovu odešle.'),
        }
        tit, hint = nadpisy.get(typ, (typ, ''))
        lbl_akce.set_text(tit)
        lbl_akce_hint.set_text(hint)
        dlg_akce.open()

    # ── Dialog: Oprava vrácené žádosti ────────────────────────────────────────
    _edit_ctx = {'skupina_id': None}
    with ui.dialog().classes('!max-w-2xl w-full') as dlg_edit:
        with ui.card().classes('w-full p-6'):
            ui.label('Oprava žádosti').classes('text-xl font-bold mb-2')
            lbl_komentar_edit = ui.label('').classes(
                'text-sm text-orange-800 bg-orange-50 border border-orange-200 p-3 rounded-lg mb-4 w-full')
            e_nazev  = ui.input('Název místnosti *').classes('w-full mb-3').props('outlined')
            e_duvod  = ui.textarea('Důvod zřízení *').classes('w-full mb-3').props('outlined rows=3')
            ui.label('Členové místnosti').classes('text-xs font-bold text-gray-600 mb-1')
            e_clenove = ui.select(
                options=_email_options,
                multiple=True,
                label='Vyberte nebo napište e-mail…',
                value=[],
            ).classes('w-full mb-4').props(
                'outlined use-input use-chips new-value-mode=add-unique '
                'input-debounce=0 hide-dropdown-icon'
            )

            def _uloz_opravu():
                if not (e_nazev.value or '').strip():
                    ui.notify('Zadejte název!', type='warning'); return
                raw_emails = e_clenove.value or []
                emails = [x.strip().lower() for x in raw_emails if x.strip()]
                if uprav_zadost(_edit_ctx['skupina_id'], e_nazev.value.strip(),
                                e_duvod.value.strip(), emails, user_id):
                    dlg_edit.close()
                    ui.notify('✅ Žádost znovu odeslána ke schválení.', type='positive')
                    muj_refresh()
                    _broadcast(vyjimka=_cid)
                else:
                    ui.notify('Chyba při ukládání.', type='negative')

            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                ui.button('Zrušit', on_click=dlg_edit.close).props('flat').classes('text-gray-500')
                ui.button('📨 Znovu odeslat', on_click=_uloz_opravu) \
                    .classes('bg-orange-500 text-white font-bold')

    def _otevri_editaci(sk):
        _edit_ctx['skupina_id'] = sk['id']
        e_nazev.set_value(sk['nazev'])
        e_duvod.set_value(sk.get('duvod') or '')
        clenove = ziskej_clenove(sk['id'])
        # Přednastavíme vybrané emaily jako seznam — hodnoty musí odpovídat klíčům v options
        e_clenove.set_value([c.lower() for c in clenove])
        komentar = sk.get('komentar_schval') or '—'
        lbl_komentar_edit.set_text(f'📝 Připomínka schvalovatele: {komentar}')
        dlg_edit.open()

    # ── Dialog: Správa členů existující místnosti ─────────────────────────────
    _sprava_ctx = {'skupina_id': None}
    with ui.dialog().classes('!max-w-lg w-full') as dlg_sprava_clenu:
        with ui.card().classes('w-full p-6'):
            ui.label('Správa členů místnosti').classes('text-xl font-bold text-gray-800 mb-1')

            @ui.refreshable
            async def _seznam_clenu():
                sid = _sprava_ctx['skupina_id']
                if not sid:
                    return
                aktualni = await asyncio.to_thread(ziskej_clenove, sid)
                if not aktualni:
                    ui.label('Místnost zatím nemá žádné členy.') \
                        .classes('text-sm text-gray-400 italic mb-2')
                    return
                with ui.column().classes('w-full gap-1 mb-4'):
                    for em in aktualni:
                        label_em = _email_options.get(em.lower(), em)
                        with ui.row().classes('w-full items-center justify-between '
                                             'bg-gray-50 border border-gray-200 '
                                             'rounded-lg px-3 py-1.5 gap-2'):
                            ui.label(label_em).classes('text-sm text-gray-700 flex-1 min-w-0 truncate')
                            ui.button(
                                icon='person_remove',
                                on_click=lambda _em=em: _odeber(_em),
                            ).props('flat round dense').classes('text-red-400') \
                             .tooltip('Odebrat z místnosti')

            def _odeber(email):
                sid = _sprava_ctx['skupina_id']
                if odeber_clena_skupiny(sid, email, user_id, muze_schvalovat):
                    _seznam_clenu.refresh()
                    ui.notify(f'Odebrán: {email}', type='positive')
                else:
                    ui.notify('Nepodařilo se odebrat člena.', type='negative')

            await _seznam_clenu()

            ui.separator().classes('my-3')
            ui.label('Přidat člena').classes('text-xs font-bold text-gray-600 mb-1')
            sc_pridat = ui.select(
                options=_email_options,
                multiple=False,
                label='Vyberte nebo napište e-mail…',
                value=None,
            ).classes('w-full mb-2').props(
                'outlined use-input clearable new-value-mode=add-unique '
                'input-debounce=0 hide-dropdown-icon'
            )

            def _pridat():
                sid = _sprava_ctx['skupina_id']
                em = (sc_pridat.value or '').strip().lower()
                if not em:
                    ui.notify('Zadejte e-mail.', type='warning')
                    return
                if pridej_clena_skupiny(sid, em, user_id, muze_schvalovat):
                    sc_pridat.set_value(None)
                    _seznam_clenu.refresh()
                    ui.notify(f'Přidán: {em}', type='positive')
                else:
                    ui.notify('Nepodařilo se přidat (zkontrolujte práva nebo e-mail).', type='negative')

            ui.button('➕ Přidat', on_click=_pridat) \
                .classes('bg-blue-600 text-white font-bold w-full mb-3')

            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg_sprava_clenu.close) \
                    .props('flat').classes('text-gray-500 font-bold')

    def _otevri_spravu_clenu(skupina_id):
        _sprava_ctx['skupina_id'] = skupina_id
        _seznam_clenu.refresh()
        dlg_sprava_clenu.open()

    # ── Dialog: Nová globální místnost (jen pro schvalovatele) ───────────────
    with ui.dialog().classes('!max-w-2xl w-full') as dlg_nova_verejna:
        with ui.card().classes('w-full p-6'):
            ui.label('Nová globální místnost').classes('text-xl font-bold text-gray-800 mb-1')
            ui.label(
                'Místnost bude ihned dostupná všem zaměstnancům.'
            ).classes('text-sm text-green-700 bg-green-50 border border-green-200 '
                      'p-3 rounded-lg mb-4')
            gv_nazev = ui.input('Název místnosti *').classes('w-full mb-3').props('outlined')
            gv_duvod = ui.textarea(
                'Popis / důvod zřízení',
                placeholder='Volitelně popište, k čemu místnost slouží…'
            ).classes('w-full mb-4').props('outlined rows=3')
            gv_email_sw = ui.switch('Informovat zaměstnance e-mailem o vytvoření místnosti') \
                .classes('mb-4 text-sm text-gray-700')

            def _vytvorit_verejenou():
                if not muze_schvalovat:
                    ui.notify('Nemáte oprávnění vytvářet globální místnosti.', type='negative')
                    return
                nazev_v = (gv_nazev.value or '').strip()
                if not nazev_v:
                    ui.notify('Zadejte název místnosti!', type='warning')
                    return
                duvod_v      = (gv_duvod.value or '').strip()
                poslat_email = bool(gv_email_sw.value)   # zachytit před resetem formuláře
                sid = vytvor_verejnou_skupinu(
                    nazev_v, duvod_v, user_id, user_name,
                    muze_schvalovat=muze_schvalovat)
                if sid == 'duplicit':
                    ui.notify('Místnost s tímto názvem již existuje!', type='warning')
                    return
                if not sid:
                    ui.notify('Chyba při vytváření místnosti.', type='negative')
                    return
                dlg_nova_verejna.close()
                gv_nazev.set_value('')
                gv_duvod.set_value('')
                gv_email_sw.set_value(False)
                ui.notify(f'✅ Globální místnost „{nazev_v}" byla vytvořena.', type='positive')
                if poslat_email:
                    import threading
                    threading.Thread(
                        target=_email_nova_verejna_mistnost,
                        args=(nazev_v, user_name),
                        daemon=True
                    ).start()
                    ui.notify('📧 E-mailové upozornění se rozesílá všem zaměstnancům.',
                              type='info')
                muj_refresh()
                _broadcast(vyjimka=_cid)   # veřejná místnost je hned vidět všem

            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                ui.button('Zrušit', on_click=dlg_nova_verejna.close) \
                    .props('flat').classes('text-gray-500')
                ui.button('🌐 Vytvořit globální místnost', on_click=_vytvorit_verejenou) \
                    .classes('bg-green-600 text-white font-bold')

    # ── Dialog: Potvrzení archivace diskuze ───────────────────────────────────
    _archiv_ctx = {'skupina_id': None, 'nazev': ''}
    with ui.dialog() as dlg_archiv:
        with ui.card().classes('w-full max-w-md p-6'):
            ui.label('Archivovat diskuzi?').classes('text-lg font-bold text-gray-800 mb-2')
            lbl_archiv = ui.label('').classes('text-sm text-gray-600 mb-4')

            def _proved_archivaci():
                sid = _archiv_ctx['skupina_id']
                if archivuj_skupinu(sid, user_id, muze_schvalovat):
                    dlg_archiv.close()
                    ui.notify('🕒 Diskuze byla archivována.', type='positive')
                    st['aktivni_skupina_id'] = None   # návrat na seznam
                    muj_refresh()
                    _broadcast(vyjimka=_cid)
                else:
                    ui.notify('Diskuzi se nepodařilo archivovat.', type='negative')

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg_archiv.close) \
                    .props('flat').classes('text-gray-500')
                ui.button('🕒 Archivovat', on_click=_proved_archivaci) \
                    .classes('bg-amber-600 text-white font-bold')

    def _otevri_archivaci(sid, nazev):
        _archiv_ctx.update({'skupina_id': sid, 'nazev': nazev})
        lbl_archiv.set_text(
            f'Diskuze „{nazev}" se přesune do archivu (ikona hodin na úvodní stránce). '
            f'Zůstane k nahlédnutí, ale nebude možné přidávat ani upravovat příspěvky, '
            f'komentáře ani členy. Archivaci může později zrušit správce místnosti '
            f'nebo administrátor.')
        dlg_archiv.open()

    def _obnovit_diskuzi(sid):
        if obnovit_skupinu(sid, user_id, muze_schvalovat):
            ui.notify('Diskuze byla obnovena mezi aktivní.', type='positive')
            muj_refresh()
            _broadcast(vyjimka=_cid)
        else:
            ui.notify('Diskuzi se nepodařilo obnovit.', type='negative')

    # ══════════════════════════════════════════════════════════════════════════
    # ROUTING: seznam dlaždic  vs.  detail místnosti
    # ══════════════════════════════════════════════════════════════════════════
    sid_aktivni = st['aktivni_skupina_id']

    # Registrace pro cílený broadcast (viz _broadcast / _broadcast_feed)
    _reg = _KLIENTI.get(_cid)
    if _reg is not None:
        _reg['skupina_id'] = sid_aktivni
        _reg['feed'] = None   # nastaví se níže, až feed vznikne

    if sid_aktivni:
        # ── DETAIL MÍSTNOSTI ──────────────────────────────────────────────────
        sk = await asyncio.to_thread(ziskej_skupinu, sid_aktivni)
        if not sk or sk['stav'] != 'schvaleno':
            st['aktivni_skupina_id'] = None
            muj_refresh()   # jen lokální navigace
            return

        clenove = await asyncio.to_thread(ziskej_clenove, sid_aktivni)
        is_verejna = bool(sk['je_verejna'])
        kandidati_mention = await asyncio.to_thread(
            ziskej_kandidaty_mention, sid_aktivni, is_verejna)
        je_archiv = bool(sk.get('archivovano'))
        muze_spravovat = muze_schvalovat or sk['vytvoril_id'] == user_id
        je_clen = (is_verejna
                   or user_email.lower() in [c.lower() for c in clenove]
                   or sk['vytvoril_id'] == user_id
                   or muze_schvalovat)

        def _zpet():
            st['aktivni_skupina_id'] = None
            muj_refresh()   # jen lokální navigace

        with ui.column().classes('w-full h-full gap-0'):

            # ── Záhlaví místnosti ─────────────────────────────────────────────
            with ui.row().classes('w-full items-center gap-3 px-5 py-3 '
                                  'bg-white border-b border-gray-200 shadow-sm flex-shrink-0'):
                ui.button(icon='arrow_back', on_click=_zpet).props('flat round') \
                    .classes('text-gray-600 -ml-1')
                barva = _avatar_color(sk['nazev'])
                ui.element('div') \
                    .classes('w-9 h-9 rounded-full flex-shrink-0') \
                    .style(f'background:{barva};display:flex;align-items:center;'
                           f'justify-content:center;color:white;font-weight:700;font-size:.75rem') \
                    .tooltip(sk['nazev']) \
                    .text = (_initials(sk['nazev']) if not is_verejna else '🌐')
                with ui.column().classes('gap-0 flex-1 min-w-0'):
                    ui.label(sk['nazev']).classes('font-bold text-gray-800 text-base leading-tight')
                    info = 'Veřejná místnost' if is_verejna else f'{len(clenove)} člen/ů'
                    ui.label(info).classes('text-xs text-gray-400')

                # Tlačítka pro tvůrce/správce a admin (zarovnaná doprava)
                with ui.row().classes('items-center gap-1 ml-auto flex-shrink-0'):
                    if je_archiv:
                        ui.label('🕒 Archivováno') \
                            .style('background:#e5e7eb;color:#374151;font-size:.7rem;'
                                   'padding:3px 10px;border-radius:99px;font-weight:700') \
                            .tooltip('Diskuze je v archivu — pouze pro čtení')
                    if muze_spravovat and not is_verejna and not je_archiv:
                        ui.button(icon='group', on_click=lambda: _otevri_spravu_clenu(sid_aktivni)) \
                            .props('flat round').classes('text-gray-400') \
                            .tooltip('Správa členů')
                    if muze_spravovat and not je_archiv:
                        ui.button('Archivovat diskuzi', icon='archive',
                                  on_click=lambda: _otevri_archivaci(sid_aktivni, sk['nazev'])) \
                            .props('flat dense').classes('text-amber-700') \
                            .tooltip('Přesunout diskuzi do archivu (jen pro čtení)')
                    if muze_spravovat and je_archiv:
                        ui.button('Obnovit', icon='unarchive',
                                  on_click=lambda: _obnovit_diskuzi(sid_aktivni)) \
                            .props('flat dense').classes('text-green-700') \
                            .tooltip('Vrátit diskuzi mezi aktivní')
                if muze_spravovat:
                    with ui.button(icon='info_outline').props('flat round') \
                            .classes('text-gray-400'):
                        with ui.menu().classes('p-4 min-w-72 max-w-xs'):
                            ui.label('Info o místnosti') \
                                .classes('font-bold text-sm text-gray-700 mb-2 pb-1 border-b border-gray-100')
                            ui.label(f'Vytvořil: {sk["vytvoril_jmeno"] or "—"}') \
                                .classes('text-xs text-gray-600 mb-1')
                            ui.label(f'Datum: {_fmt_datum(sk["datum_vytvoreni"])}') \
                                .classes('text-xs text-gray-500 mb-1')
                            if sk.get('duvod'):
                                ui.label(f'Důvod: {sk["duvod"]}') \
                                    .classes('text-xs text-gray-600 mb-2')
                            if not is_verejna and clenove:
                                ui.label('Členové:').classes('text-xs font-bold text-gray-700 mt-1')
                                for cl in clenove:
                                    ui.label(f'• {cl}').classes('text-xs text-gray-500')
                            if muze_schvalovat:
                                ui.separator().classes('my-2')
                                ui.menu_item(
                                    '🗑️ Smazat místnost',
                                    on_click=lambda: (
                                        smazat_skupinu(sid_aktivni, user_id, True),
                                        _zpet()
                                    ))

            # ── Záložky ───────────────────────────────────────────────────────
            with ui.tabs(value='nastenska') \
                    .classes('w-full bg-white border-b border-gray-100 '
                             'flex-shrink-0 px-2') as room_tabs:
                ui.tab('nastenska', label='Nástěnka', icon='campaign')
                ui.tab('soubory', label='Soubory', icon='attach_file')

            with ui.tab_panels(room_tabs, value='nastenska') \
                    .classes('w-full flex-1 flex flex-col') \
                    .style('min-height:0;overflow:hidden'):

                # ── Panel: Nástěnka ───────────────────────────────────────────
                with ui.tab_panel('nastenska') \
                        .classes('p-0 h-full flex flex-col gap-0') \
                        .style('min-height:0'):

                    with ui.column().classes('w-full flex-1 overflow-y-auto p-5 gap-4 bg-gray-50') \
                            .style('min-height:0'):

                        if je_archiv:
                            with ui.row().classes('w-full items-center gap-2 bg-gray-100 '
                                                  'border border-gray-300 rounded-lg px-4 py-2.5'):
                                ui.icon('history').classes('text-gray-500')
                                ui.label('Tato diskuze je archivovaná — pouze pro čtení. '
                                         'Nové příspěvky a komentáře nelze přidávat.') \
                                    .classes('text-sm text-gray-600 font-medium')

                        if not je_clen:
                            with ui.column().classes('w-full items-center py-16 gap-3'):
                                ui.icon('lock').classes('text-gray-300 text-5xl')
                                ui.label('Nejste členem této místnosti.') \
                                    .classes('font-semibold text-gray-500')
                                ui.label('Požádejte administrátora o přidání do místnosti.') \
                                    .classes('text-sm text-gray-400')
                        else:

                            @ui.refreshable
                            async def feed():
                                def _nacti_feed():
                                    # Příspěvky + komentáře + přílohy jedním průchodem
                                    # ve vlákně (dřív dotaz per příspěvek/komentář na loopu)
                                    prispevky = ziskej_prispevky(sid_aktivni)
                                    komentare_map = {p['id']: ziskej_komentare(p['id']) for p in prispevky}
                                    prilohy_map = {k['id']: ziskej_prilohy_komentare(k['id'])
                                                   for koms in komentare_map.values() for k in koms}
                                    return prispevky, komentare_map, prilohy_map

                                prispevky, komentare_map, prilohy_map = await asyncio.to_thread(_nacti_feed)
                                if not prispevky:
                                    with ui.column().classes('w-full items-center py-16 gap-3'):
                                        ui.icon('campaign').classes('text-gray-300 text-5xl')
                                        ui.label('Zatím žádné příspěvky') \
                                            .classes('text-gray-400 font-semibold text-lg')
                                        ui.label('Buďte první — přidejte příspěvek na nástěnku.') \
                                            .classes('text-sm text-gray-400')
                                    return

                                for p in prispevky:
                                    je_autor_p = (p['autor_id'] == user_id)
                                    barva_p = _avatar_color(p['autor_jmeno'] or '')
                                    with ui.card().classes('w-full bg-white border border-gray-200 '
                                                           'p-4 rounded-xl shadow-sm'):
                                        with ui.row().classes('items-start gap-3 w-full'):
                                            # Avatar
                                            ui.element('div') \
                                                .classes('w-8 h-8 rounded-full flex-shrink-0 mt-0.5') \
                                                .style(
                                                    f'background:{barva_p};display:flex;'
                                                    f'align-items:center;justify-content:center;'
                                                    f'color:white;font-weight:700;font-size:.65rem') \
                                                .tooltip(p['autor_jmeno'] or '') \
                                                .text = _initials(p['autor_jmeno'] or '')

                                            with ui.column().classes('flex-1 gap-1 min-w-0'):
                                                # Záhlaví příspěvku
                                                with ui.row().classes('w-full items-center gap-2 '
                                                                      'justify-between'):
                                                    with ui.row().classes('items-center gap-2'):
                                                        ui.label(p['autor_jmeno'] or '') \
                                                            .classes('font-bold text-sm text-gray-800')
                                                        ui.label(_fmt_datum(p['datum'])) \
                                                            .classes('text-xs text-gray-400')
                                                    if (je_autor_p or muze_schvalovat) and not je_archiv:
                                                        pid = p['id']
                                                        with ui.button(icon='more_vert') \
                                                                .props('flat round dense') \
                                                                .classes('text-gray-300'):
                                                            with ui.menu():
                                                                ui.menu_item(
                                                                    '🗑️ Smazat příspěvek',
                                                                    on_click=lambda _pid=pid: (
                                                                        smazat_prispevek(
                                                                            _pid, user_id, muze_schvalovat),
                                                                        feed.refresh(),
                                                                        _broadcast_feed(
                                                                            sid_aktivni,
                                                                            vyjimka=_cid)))

                                                # Text příspěvku
                                                ui.label(p['text']) \
                                                    .classes('text-sm text-gray-700 whitespace-pre-wrap mt-1')

                                                # Komentáře
                                                komentare = komentare_map.get(p['id'], [])
                                                if komentare:
                                                    with ui.column().classes('mt-3 pl-3 border-l-2 '
                                                                             'border-blue-100 gap-1.5'):
                                                        for k in komentare:
                                                            barva_k = _avatar_color(k['autor_jmeno'] or '')
                                                            with ui.row().classes('items-start gap-2'):
                                                                ui.element('div') \
                                                                    .classes('w-5 h-5 rounded-full '
                                                                             'flex-shrink-0 mt-0.5') \
                                                                    .style(
                                                                        f'background:{barva_k};display:flex;'
                                                                        f'align-items:center;justify-content:center;'
                                                                        f'color:white;font-weight:700;font-size:.5rem') \
                                                                    .tooltip(k['autor_jmeno'] or '') \
                                                                    .text = _initials(k['autor_jmeno'] or '')
                                                                with ui.column().classes('gap-0 flex-1'):
                                                                    with ui.row().classes('items-center gap-1.5 w-full justify-between'):
                                                                        with ui.row().classes('items-baseline gap-1.5'):
                                                                            ui.label(k['autor_jmeno'] or '') \
                                                                                .classes('text-xs font-bold '
                                                                                         'text-blue-700')
                                                                            ui.label(_fmt_datum(k['datum'])) \
                                                                                .classes('text-xs text-gray-300')
                                                                        if (k['autor_id'] == user_id or muze_schvalovat) and not je_archiv:
                                                                            kid2 = k['id']
                                                                            ui.button(
                                                                                icon='delete_outline',
                                                                                on_click=lambda _kid=kid2: (
                                                                                    smazat_komentar(_kid, user_id, muze_schvalovat),
                                                                                    feed.refresh(),
                                                                                    _broadcast_feed(sid_aktivni, vyjimka=_cid)),
                                                                            ).props('flat round dense') \
                                                                             .classes('text-gray-300 hover:text-red-400') \
                                                                             .tooltip('Smazat komentář')
                                                                    ui.label(k['text']) \
                                                                        .classes('text-xs text-gray-600')
                                                                    # Přílohy komentáře
                                                                    prilohy_k = prilohy_map.get(k['id'], [])
                                                                    if prilohy_k:
                                                                        with ui.row().classes('flex-wrap gap-1.5 mt-1'):
                                                                            for pr in prilohy_k:
                                                                                ext = os.path.splitext(pr['soubor_nazev'])[1].lower()
                                                                                ikona = 'image' if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp') else 'attach_file'
                                                                                url = f'/kom_prilohy/{os.path.basename(pr["soubor_cesta"])}'
                                                                                with ui.element('a').props(f'href="{url}" target="_blank"') \
                                                                                        .classes('flex items-center gap-1 text-xs text-blue-600 '
                                                                                                 'bg-blue-50 border border-blue-200 '
                                                                                                 'rounded px-2 py-0.5 hover:bg-blue-100 '
                                                                                                 'no-underline'):
                                                                                    ui.icon(ikona).classes('text-sm')
                                                                                    ui.label(pr['soubor_nazev'][:30] + ('…' if len(pr['soubor_nazev']) > 30 else ''))

                                                # Input pro nový komentář + příloha
                                                # (v archivované diskuzi se nezobrazuje)
                                                if je_archiv:
                                                    continue
                                                pid2 = p['id']
                                                _pending = {'bytes': None, 'nazev': None}
                                                _refs    = {}

                                                with ui.column().classes('w-full gap-1 mt-3'):
                                                    lbl_priloha = ui.label('').classes(
                                                        'text-xs text-green-600 italic')

                                                    with ui.row().classes('w-full gap-2 items-center'):
                                                        _refs['ki'] = ui.input(
                                                            placeholder='Přidat komentář… '
                                                                        '(@ označí člena, Enter odešle)') \
                                                            .classes('flex-1').props('outlined dense')
                                                        _pripoj_mention(_refs['ki'], kandidati_mention)

                                                        async def _nahrat_prilohu(e, _p=_pending,
                                                                                  _lbl=lbl_priloha):
                                                            _p['bytes'] = await e.file.read()
                                                            _p['nazev'] = e.file.name
                                                            _lbl.set_text(f'📎 {e.file.name[:30]}')

                                                        def _odesli_kom(_r=_refs, _pid=pid2,
                                                                        _p=_pending, _lbl=lbl_priloha):
                                                            v = (_r['ki'].value or '').strip()
                                                            if not v and not _p.get('bytes'):
                                                                return
                                                            text_k = v or '(příloha)'
                                                            kid = pridej_komentar(
                                                                _pid, sid_aktivni, text_k,
                                                                user_id, user_name)
                                                            if kid and _p.get('bytes'):
                                                                nazev_souboru = _p['nazev']
                                                                uloz_prilohu(
                                                                    kid, nazev_souboru, _p['bytes'])
                                                                _p['bytes'] = None
                                                                _p['nazev']  = None
                                                                _lbl.set_text('')
                                                                _notify_members(
                                                                    sid_aktivni,
                                                                    f'📎 {user_name} nahrál/a přílohu '
                                                                    f'„{nazev_souboru}" v místnosti '
                                                                    f'„{sk["nazev"]}".',
                                                                    vyjimka_id=user_id)
                                                            _r['ki'].set_value('')
                                                            feed.refresh()
                                                            _broadcast_feed(sid_aktivni,
                                                                            vyjimka=_cid)

                                                        _refs['ki'].on(
                                                            'keydown.enter',
                                                            lambda e, fn=_odesli_kom: fn(),
                                                            js_handler=MENTION_JS_MIMO)
                                                        _upl = ui.upload(
                                                            auto_upload=True,
                                                            on_upload=_nahrat_prilohu,
                                                        ).props('accept="*/*"') \
                                                         .style('display:none')

                                                        async def _otevri_soubor(_u=_upl):
                                                            await ui.run_javascript(
                                                                f'document.querySelector'
                                                                f'("#c{_u.id} input[type=file]").click()')

                                                        ui.button(icon='add') \
                                                            .props('flat round dense') \
                                                            .classes('text-gray-400') \
                                                            .on('click', _otevri_soubor) \
                                                            .tooltip('Přiložit soubor')
                                                        ui.button(icon='send', on_click=_odesli_kom) \
                                                            .props('flat round dense') \
                                                            .classes('text-blue-500')

                            await feed()
                            if _reg is not None:
                                _reg['feed'] = feed.refresh   # živé příspěvky pro ostatní v místnosti

                    # ── Vstup nového příspěvku (sticky dole) ──────────────────
                    if je_clen and not je_archiv:
                        with ui.row().classes('w-full items-end gap-3 px-5 py-4 bg-white '
                                              'border-t border-gray-200 shadow-lg flex-shrink-0'):
                            post_in = ui.textarea(
                                placeholder='Napište příspěvek na nástěnku… '
                                            '(@ označí člena, Ctrl+Enter odešle)') \
                                .classes('flex-1').props('outlined rows=2 auto-grow')
                            _pripoj_mention(post_in, kandidati_mention)

                            def _odesli_post():
                                v = (post_in.value or '').strip()
                                if not v: return
                                pridej_prispevek(sid_aktivni, v, user_id, user_name)
                                post_in.set_value('')
                                feed.refresh()
                                _broadcast_feed(sid_aktivni, vyjimka=_cid)

                            post_in.on('keydown.ctrl.enter', lambda e, fn=_odesli_post: fn())
                            ui.button(icon='send', text='Vložit', on_click=_odesli_post) \
                                .classes('bg-blue-600 text-white font-bold h-11 flex-shrink-0')

                # ── Panel: Soubory ────────────────────────────────────────────
                with ui.tab_panel('soubory') \
                        .classes('p-0 h-full overflow-y-auto bg-gray-50') \
                        .style('min-height:0'):

                    @ui.refreshable
                    async def soubory_panel():
                        prilohy_vse = await asyncio.to_thread(ziskej_prilohy_skupiny, sid_aktivni)
                        if not prilohy_vse:
                            with ui.column().classes('w-full items-center py-16 gap-3'):
                                ui.icon('folder_open').classes('text-gray-300 text-5xl')
                                ui.label('Zatím žádné soubory') \
                                    .classes('text-gray-400 font-semibold text-lg')
                                ui.label('Soubory přiložte ke komentářům v záložce Nástěnka.') \
                                    .classes('text-sm text-gray-400')
                            return

                        with ui.column().classes('w-full p-5 gap-2'):
                            ui.label(f'Nahrané soubory ({len(prilohy_vse)})') \
                                .classes('text-xs font-bold text-gray-500 uppercase tracking-widest mb-1')
                            for pr in prilohy_vse:
                                ext = os.path.splitext(pr['soubor_nazev'])[1].lower()
                                if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                                    ikona = 'image'
                                elif ext == '.pdf':
                                    ikona = 'picture_as_pdf'
                                else:
                                    ikona = 'description'
                                url = f'/kom_prilohy/{os.path.basename(pr["soubor_cesta"])}'
                                with ui.card().classes('w-full bg-white border border-gray-200 '
                                                       'p-3 rounded-lg shadow-sm'):
                                    with ui.row().classes('items-center gap-3 w-full'):
                                        ui.icon(ikona).classes('text-blue-400 text-2xl flex-shrink-0')
                                        with ui.column().classes('flex-1 gap-0 min-w-0'):
                                            with ui.element('a') \
                                                    .props(f'href="{url}" target="_blank"') \
                                                    .classes('text-sm font-medium text-blue-700 '
                                                             'hover:text-blue-900 no-underline '
                                                             'truncate block'):
                                                ui.label(pr['soubor_nazev'])
                                            ui.label(
                                                f'{pr["autor_jmeno"] or "?"} · {_fmt_datum(pr["datum"])}') \
                                                .classes('text-xs text-gray-400')

                    await soubory_panel()

    else:
        # ── SEZNAM DLAŽDIC ────────────────────────────────────────────────────
        skupiny  = await asyncio.to_thread(ziskej_skupiny, user_id, user_email)
        cekajici = (await asyncio.to_thread(ziskej_cekajici)) if muze_schvalovat else []

        # Vlastní žádosti se stavem (pro tvůrce)
        vlastni_cekajici = [s for s in skupiny if s['stav'] != 'schvaleno']
        schvalene        = [s for s in skupiny if s['stav'] == 'schvaleno']
        aktivni          = [s for s in schvalene if not s.get('archivovano')]
        archivovane      = [s for s in schvalene if s.get('archivovano')]

        def _toggle_archiv():
            st['zobrazit_archiv'] = not st.get('zobrazit_archiv', False)
            muj_refresh()   # jen lokální navigace

        with ui.column().classes('w-full p-6 gap-6 overflow-y-auto h-full'):

            # Záhlaví portálu
            with ui.row().classes('w-full justify-between items-start'):
                with ui.column().classes('gap-0'):
                    ui.label('Komunikační portál JIP') \
                        .classes('text-2xl font-black text-blue-900')
                    ui.label('Místnosti, nástěnky a skupinová komunikace') \
                        .classes('text-sm text-gray-500 mt-0.5')
                with ui.row().classes('gap-2 flex-shrink-0 items-center'):
                    if archivovane:
                        _arch_aktiv = st.get('zobrazit_archiv', False)
                        ui.button(
                            icon='history',
                            text=('Skrýt archiv' if _arch_aktiv
                                  else f'Archiv ({len(archivovane)})'),
                            on_click=_toggle_archiv) \
                            .props('' if _arch_aktiv else 'outline') \
                            .classes(('bg-gray-700 text-white' if _arch_aktiv
                                      else 'text-gray-600') + ' font-bold shadow-md px-5') \
                            .tooltip('Archivované diskuze (jen pro čtení)')
                    if muze_schvalovat:
                        ui.button(icon='public', text='Globální místnost',
                                  on_click=dlg_nova_verejna.open) \
                            .classes('bg-green-600 text-white font-bold shadow-md px-5') \
                            .tooltip('Vytvořit globální místnost pro všechny zaměstnance')
                    ui.button(icon='add', text='Nová místnost', on_click=dlg_nova.open) \
                        .classes('bg-blue-600 text-white font-bold shadow-md px-5')

            # ── Sekce: Schválení fronty (jen pro schvalovatele) ───────────────
            if muze_schvalovat and cekajici:
                with ui.card().classes('w-full bg-amber-50 border border-amber-200 '
                                       'rounded-xl p-4 shadow-sm'):
                    with ui.row().classes('items-center gap-2 mb-3'):
                        ui.icon('pending_actions').classes('text-amber-600 text-xl')
                        ui.label(f'Čekající žádosti ({len(cekajici)})') \
                            .classes('font-bold text-amber-800 text-base')
                    for sk in cekajici:
                        bg, fg, ico, txt_stav = STAV_META.get(sk['stav'],
                                                               ('#f3f4f6', '#374151', '?', sk['stav']))
                        with ui.card().classes('w-full bg-white border border-amber-100 '
                                               'p-4 mb-2 rounded-lg shadow-sm'):
                            with ui.row().classes('w-full justify-between items-start gap-3'):
                                with ui.column().classes('gap-1 flex-1 min-w-0'):
                                    with ui.row().classes('items-center gap-2 flex-wrap'):
                                        ui.label(sk['nazev']) \
                                            .classes('font-bold text-gray-800')
                                        ui.label(f'{ico} {txt_stav}') \
                                            .style(f'background:{bg};color:{fg};'
                                                   f'font-size:.7rem;padding:2px 8px;'
                                                   f'border-radius:99px;font-weight:600')
                                    ui.label(
                                        f'Vytvořil: {sk["vytvoril_jmeno"]} | '
                                        f'{_fmt_datum(sk["datum_vytvoreni"])}') \
                                        .classes('text-xs text-gray-400')
                                    if sk.get('duvod'):
                                        ui.label(f'Důvod: {sk["duvod"]}') \
                                            .classes('text-sm text-gray-600 mt-0.5')
                                    if sk.get('clenove_str'):
                                        ui.label(f'Členové: {sk["clenove_str"]}') \
                                            .classes('text-xs text-gray-400')
                                with ui.row().classes('gap-2 flex-shrink-0 flex-wrap'):
                                    _sid2, _sn2 = sk['id'], sk['nazev']
                                    ui.button(
                                        '✅ Schválit',
                                        on_click=lambda s=_sid2, n=_sn2:
                                        _otevri_akci('schval', s, n)) \
                                        .classes('bg-green-600 text-white text-xs h-8 font-bold')
                                    ui.button(
                                        '↩️ Vrátit',
                                        on_click=lambda s=_sid2, n=_sn2:
                                        _otevri_akci('vrat', s, n)) \
                                        .classes('bg-orange-500 text-white text-xs h-8 font-bold')
                                    ui.button(
                                        '❌ Zamítnout',
                                        on_click=lambda s=_sid2, n=_sn2:
                                        _otevri_akci('zamitni', s, n)) \
                                        .classes('bg-red-600 text-white text-xs h-8 font-bold')

            # ── Sekce: Stav mých žádostí (pro tvůrce) ────────────────────────
            if vlastni_cekajici:
                with ui.card().classes('w-full bg-blue-50 border border-blue-200 '
                                       'rounded-xl p-4 shadow-sm'):
                    with ui.row().classes('items-center gap-2 mb-2'):
                        ui.icon('assignment').classes('text-blue-500 text-xl')
                        ui.label('Stav mých žádostí').classes('font-bold text-blue-800')
                    for sk in vlastni_cekajici:
                        bg, fg, ico, txt_stav = STAV_META.get(sk['stav'],
                                                               ('#f3f4f6', '#374151', '?', sk['stav']))
                        with ui.row().classes('w-full justify-between items-center '
                                             'bg-white rounded-lg p-3 mb-1.5 '
                                             'border border-blue-100 gap-3'):
                            with ui.column().classes('gap-0.5 flex-1 min-w-0'):
                                with ui.row().classes('items-center gap-2 flex-wrap'):
                                    ui.label(sk['nazev']).classes('font-bold text-sm text-gray-800')
                                    ui.label(f'{ico} {txt_stav}') \
                                        .style(f'background:{bg};color:{fg};'
                                               f'font-size:.7rem;padding:2px 8px;'
                                               f'border-radius:99px;font-weight:600')
                                if sk.get('komentar_schval'):
                                    ui.label(f'📝 {sk["komentar_schval"]}') \
                                        .classes('text-xs text-orange-700 bg-orange-50 '
                                                 'px-2 py-1 rounded mt-1 max-w-xl')
                            if sk['stav'] == 'vraceno':
                                ui.button(
                                    '✏️ Opravit a znovu odeslat',
                                    on_click=lambda s=sk: _otevri_editaci(s)) \
                                    .classes('bg-orange-500 text-white text-xs h-8 font-bold '
                                             'flex-shrink-0')

            # ── Mřížka schválených místností (aktivní / archivované) ──────────
            zobrazit_arch = st.get('zobrazit_archiv', False) and bool(archivovane)
            zobrazene = archivovane if zobrazit_arch else aktivni

            ui.label('🕒 Archivované diskuze' if zobrazit_arch else 'Vaše místnosti') \
                .classes('font-bold text-xs text-gray-500 uppercase tracking-widest mt-1')

            if not zobrazene:
                with ui.column().classes('w-full items-center py-16 gap-3 '
                                         'bg-gray-50 rounded-xl border border-dashed '
                                         'border-gray-300'):
                    if zobrazit_arch:
                        ui.icon('history').classes('text-gray-300 text-5xl')
                        ui.label('Žádné archivované diskuze') \
                            .classes('font-semibold text-gray-400 text-lg')
                        ui.label('Archivované diskuze se zobrazí zde.') \
                            .classes('text-sm text-gray-400')
                    else:
                        ui.icon('forum').classes('text-gray-300 text-5xl')
                        ui.label('Zatím žádné místnosti') \
                            .classes('font-semibold text-gray-400 text-lg')
                        ui.label('Vytvořte novou místnost nebo počkejte na schválení.') \
                            .classes('text-sm text-gray-400')
                        ui.button('+ Vytvořit místnost', on_click=dlg_nova.open) \
                            .classes('bg-blue-600 text-white font-bold mt-2')
            else:
                # Počty příspěvků a členů všech místností jedním průchodem ve
                # vlákně (dřív 2 dotazy per dlaždici na event loopu)
                _pocty = await asyncio.to_thread(lambda: {
                    sk['id']: (pocet_prispevku(sk['id']), len(ziskej_clenove(sk['id'])))
                    for sk in zobrazene})
                with ui.grid(columns=3).classes('w-full gap-4'):
                    for sk in zobrazene:
                        is_ver = bool(sk['je_verejna'])
                        barva  = _avatar_color(sk['nazev'])
                        pocet, pocet_cl = _pocty[sk['id']]
                        _sk_id = sk['id']

                        def _otevrit(s_id=_sk_id):
                            st['aktivni_skupina_id'] = s_id
                            muj_refresh()   # jen lokální navigace

                        with ui.card() \
                                .classes('w-full bg-white border border-gray-200 '
                                         'hover:border-blue-400 hover:shadow-lg '
                                         'cursor-pointer p-4 rounded-xl transition-all') \
                                .on('click', _otevrit):
                            with ui.row().classes('items-start justify-between mb-2'):
                                with ui.row().classes('items-center gap-2 flex-1 min-w-0'):
                                    if is_ver:
                                        ui.element('div') \
                                            .classes('w-9 h-9 rounded-full flex-shrink-0') \
                                            .style('background:#2563eb;display:flex;'
                                                   'align-items:center;justify-content:center;'
                                                   'font-size:1.1rem') \
                                            .text = '🌐'
                                    else:
                                        ui.element('div') \
                                            .classes('w-9 h-9 rounded-full flex-shrink-0') \
                                            .style(
                                                f'background:{barva};display:flex;'
                                                f'align-items:center;justify-content:center;'
                                                f'color:white;font-weight:700;font-size:.75rem') \
                                            .text = _initials(sk['nazev'])
                                    ui.label(sk['nazev']) \
                                        .classes('font-bold text-gray-800 text-base '
                                                 'truncate')
                                ui.icon('history' if bool(sk.get('archivovano')) else 'chevron_right') \
                                    .classes('text-gray-300 flex-shrink-0')

                            if sk.get('duvod'):
                                ui.label(sk['duvod'] or '') \
                                    .classes('text-xs text-gray-500 mb-2 '
                                             'leading-relaxed break-words '
                                             'line-clamp-6') \
                                    .tooltip(sk['duvod'] or '')

                            with ui.row().classes('items-center gap-3 pt-2 '
                                                  'border-t border-gray-100 mt-auto'):
                                if is_ver:
                                    ui.label('Veřejná').classes('text-xs text-blue-500 font-medium')
                                else:
                                    ui.label(f'👥 {pocet_cl}').classes('text-xs text-gray-400')
                                ui.label(f'📌 {pocet}').classes('text-xs text-gray-400')
                                if bool(sk.get('archivovano')):
                                    ui.label('🕒 Archiv') \
                                        .classes('text-xs text-gray-500 font-bold ml-auto')
