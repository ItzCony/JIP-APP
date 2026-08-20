"""
Modul „Společenský večer 2026 – předběžné náklady".

Každá VO pobočka vyplní formulář předběžných nákladů (dle Excel vzoru
„Společenský večer_předběžné náklady_2026.xlsx"). Vedle tabulky běží logování
a dvoustupňové schvalování, pod tabulkou chat s možností přílohy.

Vzory převzaté z projektu:
  - dlaždice poboček + práva na pobočku → intranet_vysledky.py
  - chat s přílohou (skrytý ui.upload + e.file.read) → intranet_komunikace.py
  - DB / logování → znacky_engine.py, intranet_logger.py

intranet.py volá:  intranet_spolvecer.vykresli(user_id, user_name, vsechna_prava)
web_main.py volá:  intranet_spolvecer.inicializace_db()  (+ statická routa příloh)
"""

from nicegui import ui, app
import intranet_data
import intranet_logger
import asyncio
import datetime
import os
import uuid

# ==========================================
# KONSTANTY
# ==========================================
ROK = 2026

# (kód VO, název) – pořadí dle obrázku zadání
POBOCKY = [
    ('010', 'Pardubice'), ('011', 'Praha'), ('012', 'Jilemnice'),
    ('014', 'Liberec'),   ('020', 'Zlín'),  ('026', 'Ostrava'),
    ('032', 'Olomouc'),   ('034', 'Plzeň'), ('037', 'Nová Role'),
]
POBOCKY_KLICE = {
    'Pardubice': 'pardubice', 'Praha': 'praha', 'Jilemnice': 'jilemnice',
    'Liberec': 'liberec', 'Zlín': 'zlin', 'Ostrava': 'ostrava',
    'Olomouc': 'olomouc', 'Plzeň': 'plzen', 'Nová Role': 'nova_role',
}
# klíč → (kód, název)
_KLIC_INFO = {POBOCKY_KLICE[nazev]: (kod, nazev) for kod, nazev in POBOCKY}
# pořadí klíčů
_KLICE_PORADI = [POBOCKY_KLICE[nazev] for _, nazev in POBOCKY]

# 11 nákladových položek (řádky 7–17 Excel vzoru, pevné pořadí)
POLOZKY = [
    'Pronájem prostorů',
    'Další náklady k pronájmu (stoly, židle apod.)',
    'Lidské zdroje (šatna, hostesky apod.)',
    'Program (náplň spol. akce)',
    'Gastro studio JIP (vč. nákladu na personál)',
    'Externí catering (vč. nákladu na personál)',
    'Marketingová propagace',
    'Náklady na ubytování, dopravu',
    'Náklady na zásobu z JIP - potraviny',
    'Náklady na zásobu JIP/dodavatel - lihoviny',
    'Ostatní náklady (co není popsané)',
]

# Referenční náklady minulého období (read-only informační pole u každé pobočky).
# naklad = Kč bez DPH, ext = počet externích hostů, int = počet interních účastníků.
MINULE_OBDOBI = {
    'pardubice': {'naklad': 468301, 'ext': 400, 'int': 30},
    'praha':     {'naklad': 581544, 'ext': 450, 'int': 50},
    'jilemnice': {'naklad': 224581, 'ext': 400, 'int': 10},
    'liberec':   {'naklad': 267935, 'ext': 400, 'int': 15},
    'zlin':      {'naklad': 291929, 'ext': 400, 'int': 20},
    'ostrava':   {'naklad': 380980, 'ext': 350, 'int': 20},
    'olomouc':   {'naklad': 322500, 'ext': 250, 'int': 15},
    'plzen':     {'naklad': 280900, 'ext': 300, 'int': 20},
    'nova_role': {'naklad': 275133, 'ext': 200, 'int': 10},
}

PRILOHY_DIR   = 'spolvecer_prilohy'
PRILOHY_ROUTE = '/spolvecer_prilohy'
LOG_KATEGORIE = 'Společenský večer'

# Barvy převzaté 1:1 z Excel vzoru (theme barvy + tint → reálný hex).
# Text necháváme čitelně tmavý (bílý text z předlohy by byl na světlých výplních nečitelný).
BARVA_MODRA    = '#CCD8E7'   # „modré" buňky: Místo konání, Termín konání, Náklad celkem
BARVA_MODRA_OKR = '#B9C8DE'  # tmavší okraj k modré
BARVA_HLAVICKA = '#ECD3DC'   # záhlaví sloupců tabulky (Služba/zboží · Náklad · Poznámky)
BARVA_POCTY    = '#EBE7F2'   # počty účastníků (externí/interní)
BARVA_POCTY_OKR = '#D8D0E6'  # tmavší okraj k levandulové

# stav → (popisek, css třídy badge, ikona)
_STAVY = {
    'rozpracovano': ('Rozpracováno', 'bg-amber-100 text-amber-700 border border-amber-300',  'edit_note'),
    'ke_schvaleni': ('Ke schválení', 'bg-blue-100 text-blue-700 border border-blue-300',     'hourglass_top'),
    'schvaleno':    ('Schváleno',    'bg-green-100 text-green-700 border border-green-300',   'verified'),
}

_HLAVICKA_POLE = {
    'misto_konani':  'Místo konání',
    'termin_konani': 'Termín konání',
    'pocet_externi': 'Počet externích hostů',
    'pocet_interni': 'Počet interních účastníků',
}

# ==========================================
# FORMÁTOVÁNÍ ČÍSEL (CZ)
# ==========================================
def _fmt(v):
    """12345.67 → '12 345,67' (mezera tisíce, čárka desetiny, 2 des. místa)."""
    try:
        return f'{float(v):,.2f}'.replace(',', ' ').replace('.', ',')
    except (TypeError, ValueError):
        return '0,00'


def _parse_cislo(s):
    """Z textu '12 345,67 Kč' / '12345.67' / číslo → float nebo None.
    Toleruje mezery (i pevné), příponu „Kč" i jiné nečíselné znaky."""
    if s is None or isinstance(s, (int, float)):
        return float(s) if s is not None else None
    import re as _re
    txt = _re.sub(r'[^0-9.,-]', '', str(s))
    if txt in ('', '-', '.', ','):
        return None
    if '.' in txt and ',' in txt:
        txt = txt.replace('.', '')      # tečky = oddělovač tisíců
    txt = txt.replace(',', '.')
    try:
        return float(txt)
    except ValueError:
        return None


def _fmt_cele(v):
    """Celé číslo s mezerou jako oddělovačem tisíců: 468301 → '468 301'."""
    try:
        return f'{int(round(float(v))):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return '0'


# Sentinel: vstup byl vyplněn, ale nejde ho přečíst jako datum.
# Odlišuje „uživatel smazal datum" (None) od „uživatel napsal nesmysl".
_NEPLATNE_DATUM = object()


def _parse_datum(s, prisne=False):
    """ISO řetězec → datetime.date.

    Prázdný vstup → None (= záměrné vymazání termínu).
    Nečitelný vstup → None, nebo _NEPLATNE_DATUM při prisne=True, aby volající
    mohl zápis odmítnout místo přepsání uloženého data na NULL.
    """
    if s is None or not str(s).strip():
        return None
    try:
        return datetime.date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return _NEPLATNE_DATUM if prisne else None


def _fmt_datum(d):
    if not d:
        return '—'
    try:
        if not isinstance(d, datetime.date):
            d = datetime.date.fromisoformat(str(d)[:10])
        return f'{d.day}. {d.month}. {d.year}'
    except Exception:
        return str(d)


# ==========================================
# DB INICIALIZACE
# ==========================================
_db_init = False


def inicializace_db():
    """Vytvoří tabulky modulu (idempotentní). Volá web_main.py na startu."""
    global _db_init
    if _db_init:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS spolvecer_pobocka (
            id INT AUTO_INCREMENT PRIMARY KEY,
            pobocka_klic VARCHAR(50) NOT NULL UNIQUE,
            misto_konani VARCHAR(500) NULL,
            termin_konani DATE NULL,
            pocet_externi DECIMAL(15,2) NULL,
            pocet_interni DECIMAL(15,2) NULL,
            stav VARCHAR(20) NOT NULL DEFAULT 'rozpracovano',
            odeslano_kym INT NULL,
            odeslano_kdy DATETIME NULL,
            schvaleno_kym INT NULL,
            schvaleno_kdy DATETIME NULL,
            created_at DATETIME DEFAULT NOW(),
            updated_at DATETIME DEFAULT NOW() ON UPDATE NOW()
        ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS spolvecer_polozka (
            id INT AUTO_INCREMENT PRIMARY KEY,
            pobocka_klic VARCHAR(50) NOT NULL,
            poradi INT NOT NULL,
            nazev VARCHAR(255) NOT NULL,
            naklad DECIMAL(15,2) NULL,
            poznamka TEXT NULL,
            UNIQUE KEY uq_pol (pobocka_klic, poradi),
            INDEX idx_pob (pobocka_klic)
        ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS spolvecer_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            pobocka_klic VARCHAR(50) NOT NULL,
            user_id INT NULL,
            akce VARCHAR(40) NOT NULL,
            pole VARCHAR(200) NULL,
            stara_hodnota TEXT NULL,
            nova_hodnota TEXT NULL,
            created_at DATETIME DEFAULT NOW(),
            INDEX idx_pob (pobocka_klic)
        ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS spolvecer_chat (
            id INT AUTO_INCREMENT PRIMARY KEY,
            pobocka_klic VARCHAR(50) NOT NULL,
            user_id INT NOT NULL,
            text TEXT NULL,
            priloha_filename VARCHAR(500) NULL,
            priloha_original VARCHAR(500) NULL,
            created_at DATETIME DEFAULT NOW(),
            INDEX idx_pob (pobocka_klic),
            FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE
        ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
        conn.commit()
        _db_init = True
    except Exception as e:
        print(f"[spolvecer] DB init chyba: {e}")
    finally:
        cur.close()
        conn.close()
    os.makedirs(PRILOHY_DIR, exist_ok=True)


def _ensure_pobocka(klic):
    """Zajistí existenci hlavičkového řádku a 11 položek pro danou pobočku."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("INSERT IGNORE INTO spolvecer_pobocka (pobocka_klic) VALUES (%s)", (klic,))
        for poradi, nazev in enumerate(POLOZKY):
            cur.execute(
                "INSERT IGNORE INTO spolvecer_polozka (pobocka_klic, poradi, nazev) VALUES (%s,%s,%s)",
                (klic, poradi, nazev))
        conn.commit()
    except Exception as e:
        print(f"[spolvecer] ensure_pobocka: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ==========================================
# DB ČTENÍ
# ==========================================
def _nacti_hlavicku(klic):
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT * FROM spolvecer_pobocka WHERE pobocka_klic=%s", (klic,))
        return cur.fetchone() or {}
    except Exception as e:
        print(f"[spolvecer] nacti_hlavicku: {e}"); return {}
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_polozky(klic):
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT * FROM spolvecer_polozka WHERE pobocka_klic=%s ORDER BY poradi", (klic,))
        return cur.fetchall()
    except Exception as e:
        print(f"[spolvecer] nacti_polozky: {e}"); return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _soucet(klic):
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0.0
    cur = None
    try:
        cur = conn.cursor(buffered=True)
        cur.execute("SELECT COALESCE(SUM(naklad),0) FROM spolvecer_polozka WHERE pobocka_klic=%s", (klic,))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except Exception as e:
        print(f"[spolvecer] soucet: {e}"); return 0.0
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_stavy():
    """{klic: stav} pro dlaždice. Chybějící pobočky → 'rozpracovano'."""
    vysl = {k: 'rozpracovano' for k in _KLICE_PORADI}
    conn = intranet_data.get_db_connection()
    if not conn:
        return vysl
    cur = None
    try:
        cur = conn.cursor(buffered=True)
        cur.execute("SELECT pobocka_klic, stav FROM spolvecer_pobocka")
        for klic, stav in cur.fetchall():
            if klic in vysl:
                vysl[klic] = stav
        return vysl
    except Exception as e:
        print(f"[spolvecer] nacti_stavy: {e}"); return vysl
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _jmeno(user_id):
    if not user_id:
        return ''
    conn = intranet_data.get_db_connection()
    if not conn:
        return ''
    cur = None
    try:
        cur = conn.cursor(buffered=True)
        cur.execute("SELECT CONCAT(name,' ',surname) FROM user WHERE iduser=%s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else ''
    except Exception:
        return ''
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_log(klic, limit=40):
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT l.*, CONCAT(COALESCE(u.name,''),' ',COALESCE(u.surname,'')) AS jmeno
            FROM spolvecer_log l LEFT JOIN user u ON l.user_id = u.iduser
            WHERE l.pobocka_klic=%s
            ORDER BY l.created_at DESC, l.id DESC LIMIT %s
        """, (klic, int(limit)))
        return cur.fetchall()
    except Exception as e:
        print(f"[spolvecer] nacti_log: {e}"); return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_chat(klic):
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("""
            SELECT c.*, CONCAT(u.name,' ',u.surname) AS jmeno
            FROM spolvecer_chat c JOIN user u ON c.user_id = u.iduser
            WHERE c.pobocka_klic=%s ORDER BY c.created_at, c.id
        """, (klic,))
        return cur.fetchall()
    except Exception as e:
        print(f"[spolvecer] nacti_chat: {e}"); return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ==========================================
# DB ZÁPIS
# ==========================================
def _zaznam_log(klic, user_id, akce, pole=None, stara=None, nova=None):
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO spolvecer_log (pobocka_klic,user_id,akce,pole,stara_hodnota,nova_hodnota) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (klic, user_id, akce, pole,
             None if stara is None else str(stara),
             None if nova is None else str(nova)))
        conn.commit()
    except Exception as e:
        print(f"[spolvecer] zaznam_log: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _uloz_hlavicka_pole(klic, sloupec, hodnota, user_id):
    """Uloží jeden sloupec hlavičky. Vrátí True, pokud se hodnota změnila."""
    if sloupec not in ('misto_konani', 'termin_konani', 'pocet_externi', 'pocet_interni'):
        return False
    stara = _nacti_hlavicku(klic).get(sloupec)
    if _norm(stara) == _norm(hodnota):
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE spolvecer_pobocka SET {sloupec}=%s WHERE pobocka_klic=%s", (hodnota, klic))
        conn.commit()
        _zaznam_log(klic, user_id, 'zmena', _HLAVICKA_POLE.get(sloupec, sloupec),
                    _log_hodn(sloupec, stara), _log_hodn(sloupec, hodnota))
        return True
    except Exception as e:
        print(f"[spolvecer] uloz_hlavicka_pole: {e}"); return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _uloz_polozka_pole(klic, poradi, sloupec, hodnota, user_id):
    """Uloží náklad/poznámku položky. Vrátí True, pokud se hodnota změnila."""
    if sloupec not in ('naklad', 'poznamka'):
        return False
    stare = _nacti_polozky(klic)
    stara = next((p.get(sloupec) for p in stare if p['poradi'] == poradi), None)
    if _norm(stara) == _norm(hodnota):
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE spolvecer_polozka SET {sloupec}=%s WHERE pobocka_klic=%s AND poradi=%s",
            (hodnota, klic, poradi))
        conn.commit()
        oznaceni = POLOZKY[poradi] + (' – náklad' if sloupec == 'naklad' else ' – poznámka')
        _zaznam_log(klic, user_id, 'zmena', oznaceni,
                    _fmt(stara) if (sloupec == 'naklad' and stara is not None) else stara,
                    _fmt(hodnota) if (sloupec == 'naklad' and hodnota is not None) else hodnota)
        return True
    except Exception as e:
        print(f"[spolvecer] uloz_polozka_pole: {e}"); return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _po_editaci(klic, user_id):
    """Pokud byla pobočka schválená, vrátí ji editace zpět do stavu 'ke_schvaleni'.
    Vrací True, pokud došlo k resetu (UI pak obnoví stavový panel)."""
    hl = _nacti_hlavicku(klic)
    if hl.get('stav') != 'schvaleno':
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE spolvecer_pobocka SET stav='ke_schvaleni', schvaleno_kym=NULL, schvaleno_kdy=NULL "
            "WHERE pobocka_klic=%s", (klic,))
        conn.commit()
        _zaznam_log(klic, user_id, 'reset_schvaleni', 'Stav',
                    'Schváleno', 'Ke schválení (úprava po schválení)')
        return True
    except Exception as e:
        print(f"[spolvecer] po_editaci: {e}"); return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nastav_stav(klic, novy_stav, user_id):
    """Změní stav workflow a zapíše metadata (odesláno/schváleno)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        if novy_stav == 'ke_schvaleni':
            cur.execute(
                "UPDATE spolvecer_pobocka SET stav='ke_schvaleni', odeslano_kym=%s, odeslano_kdy=NOW() "
                "WHERE pobocka_klic=%s", (user_id, klic))
        elif novy_stav == 'schvaleno':
            cur.execute(
                "UPDATE spolvecer_pobocka SET stav='schvaleno', schvaleno_kym=%s, schvaleno_kdy=NOW() "
                "WHERE pobocka_klic=%s", (user_id, klic))
        elif novy_stav == 'rozpracovano':
            cur.execute(
                "UPDATE spolvecer_pobocka SET stav='rozpracovano', odeslano_kym=NULL, odeslano_kdy=NULL, "
                "schvaleno_kym=NULL, schvaleno_kdy=NULL WHERE pobocka_klic=%s", (klic,))
        else:
            return False
        conn.commit()
        return True
    except Exception as e:
        print(f"[spolvecer] nastav_stav: {e}"); return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _pridej_chat(klic, user_id, text, priloha_filename=None, priloha_original=None):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO spolvecer_chat (pobocka_klic,user_id,text,priloha_filename,priloha_original) "
            "VALUES (%s,%s,%s,%s,%s)",
            (klic, user_id, text or None, priloha_filename, priloha_original))
        conn.commit()
        return True
    except Exception as e:
        print(f"[spolvecer] pridej_chat: {e}"); return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _uloz_prilohu_na_disk(klic, original_nazev, obsah_bytes):
    """Uloží přílohu chatu na disk; vrátí relativní cestu (klic/uuid.ext) nebo None."""
    try:
        slozka = os.path.join(PRILOHY_DIR, klic)
        os.makedirs(slozka, exist_ok=True)
        ext = os.path.splitext(original_nazev or '')[1]
        rel = f'{klic}/{uuid.uuid4().hex}{ext}'
        with open(os.path.join(PRILOHY_DIR, *rel.split('/')), 'wb') as f:
            f.write(obsah_bytes)
        return rel
    except Exception as e:
        print(f"[spolvecer] uloz_prilohu: {e}"); return None


def _nacti_prijemce_pobocky(klic):
    """Vrátí [{'email','jmeno'}] uživatelů s přístupem k dané pobočce – právo
    přiřazené přímo, přes pracovní pozici nebo oddělení. Relevantní práva:
    vse / spolvecer_ctenar / spolvecer_schvalovatel / spolvecer_organizator_<klic>."""
    rel = ['vse', 'spolvecer_ctenar', 'spolvecer_schvalovatel', f'spolvecer_organizator_{klic}']
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor()
        ph = ', '.join(['%s'] * len(rel))
        cur.execute(f"""
            SELECT DISTINCT u.email, CONCAT(u.name,' ',u.surname) AS jmeno FROM (
                SELECT utp.user_iduser AS uid
                FROM user_To_privileges utp
                JOIN privileges p ON utp.privileges_idprivileges = p.idprivileges
                WHERE LOWER(p.name) IN ({ph})
                UNION
                SELECT utj.user_iduser
                FROM user_To_jobPosition utj
                JOIN jobPosition_To_privileges jtp ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
                JOIN privileges p ON jtp.privileges_idprivileges = p.idprivileges
                WHERE LOWER(p.name) IN ({ph})
                UNION
                SELECT dtu.user_iduser
                FROM department_To_user dtu
                JOIN department_To_privileges dtp ON dtu.department_iddepartment = dtp.department_iddepartment
                JOIN privileges p ON dtp.privileges_idprivileges = p.idprivileges
                WHERE LOWER(p.name) IN ({ph})
            ) x
            JOIN user u ON u.iduser = x.uid
            WHERE u.email IS NOT NULL AND u.email <> ''
            ORDER BY jmeno
        """, rel * 3)
        out, seen = [], set()
        for email, jmeno in cur.fetchall():
            e = (email or '').strip()
            if e and e.lower() not in seen:
                seen.add(e.lower())
                out.append({'email': e, 'jmeno': (jmeno or '').strip()})
        return out
    except Exception as exc:
        print(f"[spolvecer] nacti_prijemce_pobocky: {exc}")
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# Lidské popisky „globálních" rolí (přístup ke všem pobočkám) pro přehled práv.
_PRAVO_ROLE_LABEL = {
    'vse': 'Superadmin (vše)',
    'spolvecer_schvalovatel': 'Schvalovatel',
    'spolvecer_ctenar': 'Čtenář',
}


def _nacti_prehled_prav():
    """Pro hlavního administrátora: jak jsou nastavena práva na jednotlivé
    pobočky modulu Společenský večer (přiřazená přímo / přes pracovní pozici /
    přes oddělení). Vrací dict:
      {
        'globalni': [{'jmeno','email','aktivni','role','zdroje'}],   # přístup ke všem pobočkám
        'pobocky':  {klic: [{'jmeno','email','aktivni','zdroje'}]},   # organizátoři dané pobočky
      }
    Role Superadmin / Schvalovatel / Čtenář = přístup ke všem pobočkám.
    """
    prazdny = {'globalni': [], 'pobocky': {k: [] for k in _KLICE_PORADI}}
    role_klice = list(_PRAVO_ROLE_LABEL)
    org_klice = [f'spolvecer_organizator_{k}' for k in _KLICE_PORADI]
    rel = role_klice + org_klice
    conn = intranet_data.get_db_connection()
    if not conn:
        return prazdny
    cur = None
    try:
        cur = conn.cursor()
        ph = ', '.join(['%s'] * len(rel))
        cur.execute(f"""
            SELECT x.uid, u.name, u.surname, u.email, u.is_active, x.pname, x.zdroj FROM (
                SELECT utp.user_iduser AS uid, LOWER(p.name) AS pname, 'přímo' AS zdroj
                FROM user_To_privileges utp
                JOIN privileges p ON utp.privileges_idprivileges = p.idprivileges
                WHERE LOWER(p.name) IN ({ph})
                UNION
                SELECT utj.user_iduser, LOWER(p.name), 'pozice'
                FROM user_To_jobPosition utj
                JOIN jobPosition_To_privileges jtp ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
                JOIN privileges p ON jtp.privileges_idprivileges = p.idprivileges
                WHERE LOWER(p.name) IN ({ph})
                UNION
                SELECT dtu.user_iduser, LOWER(p.name), 'oddělení'
                FROM department_To_user dtu
                JOIN department_To_privileges dtp ON dtu.department_iddepartment = dtp.department_iddepartment
                JOIN privileges p ON dtp.privileges_idprivileges = p.idprivileges
                WHERE LOWER(p.name) IN ({ph})
            ) x
            JOIN user u ON u.iduser = x.uid
        """, rel * 3)
        raw = cur.fetchall()
    except Exception as exc:
        print(f'[spolvecer] nacti_prehled_prav: {exc}')
        return prazdny
    finally:
        if cur:  cur.close()
        if conn: conn.close()

    by_user = {}
    for uid, name, surname, email, aktivni, pname, zdroj in raw:
        d = by_user.setdefault(uid, {
            'jmeno': f'{name or ""} {surname or ""}'.strip() or (email or f'#{uid}'),
            'email': (email or '').strip(),
            'aktivni': bool(aktivni),
            'role': set(), 'pobocky': set(), 'zdroje': set(),
        })
        if pname in _PRAVO_ROLE_LABEL:
            d['role'].add(pname)
        elif pname.startswith('spolvecer_organizator_'):
            klic = pname[len('spolvecer_organizator_'):]
            if klic in _KLIC_INFO:
                d['pobocky'].add(klic)
        d['zdroje'].add(zdroj)

    globalni, pobocky = [], {k: [] for k in _KLICE_PORADI}
    for d in sorted(by_user.values(), key=lambda r: r['jmeno'].lower()):
        zaklad = {'jmeno': d['jmeno'], 'email': d['email'] or '—',
                  'aktivni': d['aktivni'], 'zdroje': sorted(d['zdroje'])}
        if d['role']:
            globalni.append({**zaklad,
                             'role': ', '.join(_PRAVO_ROLE_LABEL[r]
                                               for r in role_klice if r in d['role'])})
        for klic in _KLICE_PORADI:
            if klic in d['pobocky']:
                pobocky[klic].append(dict(zaklad))
    return {'globalni': globalni, 'pobocky': pobocky}


def _rozesli_pobocku_sync(klic, prijemci, zprava_extra=''):
    """Rozešle příjemcům informační e-mail o pobočce. Vrací (odesláno, chyb).
    Blokující (SMTP) – volat přes asyncio.to_thread."""
    import intranet_emaily
    kod, nazev = _KLIC_INFO.get(klic, ('', klic))
    hl = _nacti_hlavicku(klic)
    stav_popis = _STAVY.get(hl.get('stav', 'rozpracovano'), _STAVY['rozpracovano'])[0]
    soucet = _soucet(klic)
    server_url = 'https://analytikasys.jip-napoje.cz/spolvecer'
    predmet = f'Společenský večer {ROK} – {kod} {nazev}'
    extra = (zprava_extra or '').strip()
    sent = fail = 0
    for r in prijemci:
        radky = [
            'Dobrý den,', '',
            f'informace k pobočce {kod} {nazev} v modulu Společenský večer {ROK} (předběžné náklady):',
            '',
            f'  - Stav: {stav_popis}',
            f'  - Náklad celkem: {_fmt(soucet)} Kč',
            '',
        ]
        if extra:
            radky += [extra, '']
        radky += [f'Detail a úpravy: {server_url}', '', 'S pozdravem', 'Intranet MOJEJIPka']
        text = '\n'.join(radky)
        try:
            ok = intranet_emaily.odesli_upozorneni_email(r['email'], predmet, text)
        except Exception as exc:
            print(f"[spolvecer] e-mail {r.get('email')} selhal: {exc}")
            ok = False
        if ok:
            sent += 1
        else:
            fail += 1
    return sent, fail


def _norm(v):
    """Normalizace pro porovnání (číslo vs text vs datum)."""
    if v is None:
        return ''
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        try:
            return f'{float(v):.2f}'
        except (TypeError, ValueError):
            return str(v)
    return str(v).strip()


def _log_hodn(sloupec, hodnota):
    if hodnota is None:
        return None
    if sloupec == 'termin_konani':
        return _fmt_datum(hodnota)
    if sloupec in ('pocet_externi', 'pocet_interni'):
        return _fmt(hodnota)
    return hodnota


# ==========================================
# UI
# ==========================================
def vykresli(user_id, user_name, vsechna_prava):
    """Vstupní bod modulu (volá intranet.py)."""
    inicializace_db()

    ma_vse          = 'vse' in vsechna_prava
    je_schvalovatel = ma_vse or 'spolvecer_schvalovatel' in vsechna_prava
    je_ctenar       = je_schvalovatel or 'spolvecer_ctenar' in vsechna_prava
    organizuje      = set(_KLICE_PORADI) if ma_vse else {
        klic for klic in _KLICE_PORADI if f'spolvecer_organizator_{klic}' in vsechna_prava
    }

    if je_ctenar or je_schvalovatel:
        pristupne = list(_KLICE_PORADI)               # čtenář/schvalovatel/admin vidí vše
    else:
        pristupne = [k for k in _KLICE_PORADI if k in organizuje]

    if not pristupne:
        with ui.column().classes('items-center py-24 gap-4'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('Nemáte přístup k žádné pobočce tohoto modulu.').classes('text-gray-400 text-lg')
        return

    state_key = f'spolvecer_sel_{user_id}'

    # -----------------------------------------------------------------
    @ui.refreshable
    def _panel():
        sel = app.storage.user.get(state_key)
        if sel not in pristupne:
            sel = None
        if sel is None:
            _render_seznam()
        else:
            _render_detail(sel)

    # =================================================================
    # DIALOG: PŘEHLED PRÁV NA POBOČKY (jen hlavní administrátor)
    # =================================================================
    def _otevri_prava_dialog():
        ref = {'data': None}

        def _osoba_radek(o):
            tecky = '•'
            cls_jmeno = 'text-sm font-semibold text-gray-800' if o['aktivni'] \
                else 'text-sm font-semibold text-gray-400 line-through'
            with ui.row().classes('w-full items-center gap-2 py-0.5'):
                ui.label(tecky).classes('text-gray-300')
                ui.label(o['jmeno']).classes(cls_jmeno)
                ui.label(o['email']).classes('text-xs text-gray-400')
                ui.element('div').classes('flex-1')
                ui.label(', '.join(o['zdroje'])).classes(
                    'text-[10px] font-bold text-indigo-500 bg-indigo-50 px-2 py-0.5 rounded-full')
                if not o['aktivni']:
                    ui.label('neaktivní').classes(
                        'text-[10px] font-bold text-red-500 bg-red-50 px-2 py-0.5 rounded-full')

        with ui.dialog() as dlg, ui.card().classes('p-6 gap-3').style(
                'min-width: 720px; max-width: 980px; max-height: 86vh; overflow-y: auto'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('admin_panel_settings', color='indigo')
                ui.label('Přehled práv na pobočky').classes('text-lg font-bold text-gray-800')
            ui.label(
                'Jak jsou nastavena práva v modulu Společenský večer – přiřazená přímo, '
                'přes pracovní pozici nebo přes oddělení. Schvalovatel, Čtenář a Superadmin '
                'vidí všechny pobočky; organizátor edituje jen svou pobočku.'
            ).classes('text-xs text-gray-500')

            @ui.refreshable
            def _obsah():
                data = ref.get('data')
                if data is None:
                    with ui.row().classes('items-center gap-2 py-6'):
                        ui.spinner(size='sm')
                        ui.label('Načítám práva…').classes('text-sm text-gray-500')
                    return

                # ── Globální role (přístup ke všem pobočkám) ──
                with ui.card().classes('w-full p-4 rounded-2xl border border-indigo-200 gap-1').style(
                        'background-color:#EEF2FF'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('groups', size='sm').classes('text-indigo-600')
                        ui.label('Přístup ke všem pobočkám').classes(
                            'text-sm font-black text-indigo-700 uppercase tracking-wide')
                    glob = data.get('globalni') or []
                    if not glob:
                        ui.label('Žádný uživatel nemá globální roli (Superadmin / Schvalovatel / Čtenář).').classes(
                            'text-sm text-gray-500 italic')
                    else:
                        for o in glob:
                            with ui.row().classes('w-full items-center gap-2 py-0.5'):
                                ui.label('•').classes('text-indigo-300')
                                ui.label(o['jmeno']).classes(
                                    'text-sm font-semibold ' +
                                    ('text-gray-800' if o['aktivni'] else 'text-gray-400 line-through'))
                                ui.label(o['email']).classes('text-xs text-gray-400')
                                ui.element('div').classes('flex-1')
                                ui.label(o['role']).classes(
                                    'text-[10px] font-bold text-white bg-indigo-500 px-2 py-0.5 rounded-full')
                                ui.label(', '.join(o['zdroje'])).classes(
                                    'text-[10px] font-bold text-indigo-500 bg-white px-2 py-0.5 rounded-full')
                                if not o['aktivni']:
                                    ui.label('neaktivní').classes(
                                        'text-[10px] font-bold text-red-500 bg-red-50 px-2 py-0.5 rounded-full')

                # ── Organizátoři po jednotlivých pobočkách ──
                ui.label('Organizátoři pobočky (editace tabulky + odeslání ke schválení)').classes(
                    'text-sm font-black text-gray-700 uppercase tracking-wide mt-2')
                pob = data.get('pobocky') or {}
                with ui.column().classes('w-full gap-2'):
                    for klic in _KLICE_PORADI:
                        kod, nazev = _KLIC_INFO[klic]
                        organizatori = pob.get(klic) or []
                        with ui.card().classes('w-full p-4 rounded-2xl border border-gray-200 gap-1'):
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label(kod).classes(
                                    'text-xs font-black text-white bg-blue-700 px-2 py-1 rounded-lg')
                                ui.label(nazev).classes('text-base font-extrabold text-gray-800')
                                ui.element('div').classes('flex-1')
                                ui.label(f'{len(organizatori)} organizátor(ů)').classes(
                                    'text-xs font-bold text-gray-400')
                            if not organizatori:
                                ui.label('Bez přiřazeného organizátora.').classes(
                                    'text-sm text-amber-600 italic')
                            else:
                                for o in organizatori:
                                    _osoba_radek(o)

            _obsah()
            with ui.row().classes('w-full justify-end mt-2'):
                ui.button('Zavřít', on_click=dlg.close).props('flat no-caps').classes('text-gray-500')

        async def _nacti():
            ref['data'] = await asyncio.to_thread(_nacti_prehled_prav)
            _obsah.refresh()

        dlg.open()
        ui.timer(0.05, _nacti, once=True)

    # =================================================================
    # SEZNAM POBOČEK (dlaždice)
    # =================================================================
    def _render_seznam():
        with ui.row().classes('w-full items-start justify-between gap-4 mb-6 flex-wrap'):
            with ui.column().classes('gap-1'):
                ui.label(f'Společenský večer {ROK} – předběžné náklady').classes(
                    'text-3xl font-extrabold text-gray-800 mb-1')
                ui.label('Vyberte pobočku pro vyplnění / zobrazení nákladů.').classes(
                    'text-gray-500')
            if ma_vse:
                ui.button('Přehled práv na pobočky', icon='admin_panel_settings',
                          on_click=_otevri_prava_dialog).props('outline no-caps color=indigo')

        stavy = _nacti_stavy()
        with ui.row().classes('gap-6 flex-wrap'):
            for klic in pristupne:
                kod, nazev = _KLIC_INFO[klic]
                stav = stavy.get(klic, 'rozpracovano')
                popis, badge_cls, ikona = _STAVY.get(stav, _STAVY['rozpracovano'])

                def vstup(k=klic):
                    app.storage.user[state_key] = k
                    ui.timer(0, _panel.refresh, once=True)

                with ui.card().classes(
                    'w-72 h-44 p-5 cursor-pointer bg-white rounded-2xl border border-gray-200 '
                    'shadow-sm hover:shadow-lg hover:scale-[1.02] transition-all'
                ).on('click', vstup):
                    with ui.row().classes('w-full items-center justify-between'):
                        ui.label(kod).classes('text-xs font-black text-white bg-blue-700 px-2 py-1 rounded-lg')
                        ui.icon(ikona).classes('text-gray-300')
                    ui.label(nazev).classes('text-2xl font-extrabold text-gray-800 mt-2 leading-tight')
                    ui.element('div').classes('flex-1')
                    ui.label(popis).classes(f'text-xs font-black px-3 py-1 rounded-full self-start {badge_cls}')

    # =================================================================
    # DETAIL POBOČKY
    # =================================================================
    def _render_detail(klic):
        _ensure_pobocka(klic)
        kod, nazev = _KLIC_INFO[klic]
        muze_editovat = ma_vse or (klic in organizuje)
        muze_odeslat  = ma_vse or (klic in organizuje)
        muze_schvalit = je_schvalovatel

        hl       = _nacti_hlavicku(klic)
        polozky  = _nacti_polozky(klic)

        # --- hlavička: zpět + nadpis + stav ---
        @ui.refreshable
        def _badge_hlavni():
            stav = _nacti_hlavicku(klic).get('stav', 'rozpracovano')
            popis, badge_cls, ikona = _STAVY.get(stav, _STAVY['rozpracovano'])
            ui.label(popis).classes(f'text-sm font-black px-4 py-1.5 rounded-full {badge_cls}')

        def zpet():
            app.storage.user[state_key] = None
            ui.timer(0, _panel.refresh, once=True)

        with ui.row().classes('w-full items-center gap-4 mb-2'):
            ui.button('← Zpět', icon='arrow_back', on_click=zpet).props('flat').classes('text-gray-600 font-bold')
            ui.label(f'{kod}  {nazev}').classes('text-3xl font-extrabold text-gray-800 flex-1')
            _badge_hlavni()

        if not muze_editovat:
            ui.label('Máte přístup pouze pro čtení – můžete sledovat data a psát do chatu.').classes(
                'text-xs text-gray-400 italic mb-4')

        total_lbl = {'el': None}

        # ---- referencovatelné panely (definovány předem) ----
        @ui.refreshable
        def _log_panel():
            zaznamy = _nacti_log(klic, 40)
            if not zaznamy:
                ui.label('Zatím žádné záznamy.').classes('italic text-gray-400 text-sm')
                return
            with ui.column().classes('w-full gap-2'):
                for z in zaznamy:
                    ca = z.get('created_at')
                    cas = ca.strftime('%d.%m.%Y %H:%M') if isinstance(ca, datetime.datetime) else str(ca)[:16]
                    with ui.element('div').classes('w-full border-l-2 border-blue-200 pl-3 py-0.5'):
                        with ui.row().classes('w-full justify-between items-center gap-2'):
                            ui.label((z.get('jmeno') or 'Systém').strip() or 'Systém').classes(
                                'text-xs font-bold text-gray-700')
                            ui.label(cas).classes('text-[10px] text-gray-400 font-mono whitespace-nowrap')
                        popis = _popis_log(z)
                        ui.label(popis).classes('text-xs text-gray-500 leading-snug break-words')

        @ui.refreshable
        def _akce_panel():
            hl2 = _nacti_hlavicku(klic)
            stav = hl2.get('stav', 'rozpracovano')

            if stav == 'schvaleno':
                with ui.column().classes(
                    'w-full items-center gap-1 p-4 bg-green-50 border-2 border-green-300 rounded-2xl'):
                    ui.icon('verified', size='2.5rem', color='green')
                    ui.label('SCHVÁLENO').classes('text-lg font-black text-green-700 tracking-wide')
                    kdo = _jmeno(hl2.get('schvaleno_kym'))
                    kdy = hl2.get('schvaleno_kdy')
                    if kdo:
                        ui.label(kdo).classes('text-xs text-green-700 font-bold')
                    if isinstance(kdy, datetime.datetime):
                        ui.label(kdy.strftime('%d.%m.%Y %H:%M')).classes('text-[11px] text-green-600')
                if muze_schvalit:
                    ui.button('Zrušit schválení', icon='undo',
                              on_click=lambda: _zmen_stav('ke_schvaleni', 'vraceno')).props('flat dense').classes(
                        'text-gray-400 hover:text-red-500 text-xs')
                return

            if stav == 'ke_schvaleni':
                kdo = _jmeno(hl2.get('odeslano_kym'))
                kdy = hl2.get('odeslano_kdy')
                with ui.column().classes('w-full gap-1 p-3 bg-blue-50 border border-blue-200 rounded-2xl'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('hourglass_top', color='blue')
                        ui.label('Odesláno ke schválení').classes('font-bold text-blue-700 text-sm')
                    if kdo:
                        ui.label(f'{kdo}' + (f' · {kdy.strftime("%d.%m.%Y %H:%M")}'
                                            if isinstance(kdy, datetime.datetime) else '')).classes(
                            'text-xs text-blue-600')
                if muze_schvalit:
                    ui.button('Schválit náklady', icon='verified',
                              on_click=lambda: _zmen_stav('schvaleno', 'schvaleno')).props('no-caps').classes(
                        'w-full bg-green-600 hover:bg-green-700 text-white font-bold text-sm '
                        'min-h-[2.75rem] py-2 rounded-xl shadow-sm mt-2')
                else:
                    ui.label('Čeká na schválení schvalovatelem.').classes('text-xs text-gray-400 italic mt-1')
                if muze_odeslat:
                    ui.button('Vzít zpět k úpravám', icon='undo',
                              on_click=lambda: _zmen_stav('rozpracovano', 'vraceno')).props('flat dense').classes(
                        'text-gray-400 hover:text-amber-600 text-xs mt-1')
                return

            # rozpracovano
            with ui.row().classes('items-center gap-2'):
                ui.icon('edit_note', color='amber')
                ui.label('Rozpracováno').classes('font-bold text-amber-700 text-sm')
            if muze_odeslat:
                ui.button('Odeslat ke schválení', icon='send',
                          on_click=lambda: _zmen_stav('ke_schvaleni', 'odeslano')).props('no-caps').classes(
                    'w-full bg-blue-600 hover:bg-blue-700 text-white font-bold text-sm '
                    'min-h-[2.75rem] py-2 rounded-xl shadow-sm mt-2'
                ).tooltip('Označit náklady jako finální a odeslat ke schválení')
            else:
                ui.label('Vyplňuje organizátor pobočky.').classes('text-xs text-gray-400 italic mt-1')

        def _zmen_stav(novy, akce):
            if novy == 'ke_schvaleni' and not muze_odeslat:
                return
            if novy == 'schvaleno' and not muze_schvalit:
                return
            if not _nastav_stav(klic, novy, user_id):
                ui.notify('Chyba při změně stavu.', type='negative'); return
            popis_akce = {'odeslano': 'Odesláno ke schválení',
                          'schvaleno': 'Náklady schváleny',
                          'vraceno': 'Vráceno k přepracování'}.get(akce, akce)
            _zaznam_log(klic, user_id, akce, 'Stav', None, popis_akce)
            intranet_logger.log_activity(user_name, LOG_KATEGORIE, f'{nazev}: {popis_akce}')
            _akce_panel.refresh()
            _badge_hlavni.refresh()
            _log_panel.refresh()
            ui.notify(popis_akce, type='positive', position='top')

        # ---- po editaci buňky: ulož, přepočítej součet, zaloguj, případně resetuj stav ----
        async def _po_zmene(zmeneno):
            if not zmeneno:
                return
            if total_lbl['el'] is not None:
                total_lbl['el'].set_text(_fmt(_soucet(klic)) + ' Kč')
            reset = await asyncio.to_thread(_po_editaci, klic, user_id)
            _log_panel.refresh()
            if reset:
                _akce_panel.refresh()
                _badge_hlavni.refresh()

        # ---- rozeslání e-mailu všem s přístupem k pobočce ----
        def _otevri_email_dialog():
            ref = {'prijemci': None}
            with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl w-full max-w-lg gap-3'):
                ui.label(f'Rozeslat e-mail – {kod} {nazev}').classes('text-lg font-bold text-gray-800')
                ui.label('Odešle e-mail všem, kdo mají přístup k této pobočce '
                         '(organizátoři pobočky, čtenáři, schvalovatelé).').classes('text-xs text-gray-500')

                @ui.refreshable
                def _seznam():
                    if ref['prijemci'] is None:
                        with ui.row().classes('items-center gap-2 py-2'):
                            ui.spinner(size='sm')
                            ui.label('Načítám příjemce…').classes('text-sm text-gray-500')
                    elif not ref['prijemci']:
                        ui.label('K této pobočce nemá přístup žádný uživatel s e-mailem.').classes(
                            'text-sm text-amber-600 italic py-2')
                    else:
                        ui.label(f'Příjemci ({len(ref["prijemci"])}):').classes('text-xs font-black text-gray-600')
                        with ui.column().classes('w-full gap-0.5 max-h-44 overflow-y-auto '
                                                 'bg-gray-50 border border-gray-200 rounded-xl p-3'):
                            for pr in ref['prijemci']:
                                ui.label(f'• {pr["jmeno"] or pr["email"]}  ·  {pr["email"]}').classes(
                                    'text-xs text-gray-600')

                _seznam()
                zprava_in = ui.textarea('Vlastní zpráva (nepovinné)').classes('w-full').props('outlined rows=3')

                async def _odeslat():
                    if not ref.get('prijemci'):
                        return
                    odeslat_btn.props('loading')
                    sent, fail = await asyncio.to_thread(
                        _rozesli_pobocku_sync, klic, ref['prijemci'], zprava_in.value)
                    _zaznam_log(klic, user_id, 'email', 'Rozeslání e-mailu',
                                None, f'odesláno {sent}, chyb {fail}')
                    intranet_logger.log_activity(
                        user_name, LOG_KATEGORIE,
                        f'{nazev}: rozeslán e-mail osobám s přístupem ({sent} odesláno, {fail} chyb)')
                    _log_panel.refresh()
                    dlg.close()
                    if fail:
                        ui.notify(f'Odesláno {sent} e-mailů, {fail} se nepodařilo odeslat.',
                                  type='warning', position='top', timeout=8000)
                    else:
                        ui.notify(f'Odesláno {sent} e-mailů.', type='positive', position='top')

                with ui.row().classes('w-full justify-end gap-2 mt-2'):
                    ui.button('Zavřít', on_click=dlg.close).props('flat no-caps').classes('text-gray-500')
                    odeslat_btn = ui.button('Odeslat', icon='send', on_click=_odeslat).props('no-caps').classes(
                        'bg-blue-600 hover:bg-blue-700 text-white font-bold')

            async def _nacti():
                ref['prijemci'] = await asyncio.to_thread(_nacti_prijemce_pobocky, klic)
                _seznam.refresh()

            dlg.open()
            ui.timer(0.05, _nacti, once=True)

        # ---- informační pole: náklad minulého období (referenční hodnoty) ----
        _minule = MINULE_OBDOBI.get(klic)
        if _minule:
            with ui.card().classes('w-full p-4 mb-4 rounded-2xl border-l-4 shadow-sm').style(
                    'background-color:#F1F5F9; border-color:#475569'):
                with ui.row().classes('w-full items-center gap-4 flex-wrap'):
                    ui.icon('history', size='2.25rem').classes('text-slate-500')
                    with ui.column().classes('gap-0'):
                        ui.label('Náklad minulého období').classes(
                            'text-[11px] font-black uppercase tracking-widest text-red-600')
                        ui.label(f'{_fmt_cele(_minule["naklad"])} Kč').classes(
                            'text-3xl font-black text-slate-800 leading-tight')
                    ui.element('div').classes('flex-1')
                    with ui.column().classes('gap-1 items-end'):
                        with ui.row().classes('items-center gap-1.5'):
                            ui.icon('groups', size='sm').classes('text-slate-400')
                            ui.label(f'{_fmt_cele(_minule["ext"])} externích hostů').classes(
                                'text-sm font-bold text-slate-600')
                        with ui.row().classes('items-center gap-1.5'):
                            ui.icon('badge', size='sm').classes('text-slate-400')
                            ui.label(f'{_fmt_cele(_minule["int"])} interních účastníků').classes(
                                'text-sm font-bold text-slate-600')

        # =============================================================
        # ROZLOŽENÍ: tabulka (vlevo) + log/schvalování (vpravo)
        # =============================================================
        with ui.row().classes('w-full gap-6 flex-wrap items-start'):

            # ---------- LEVÁ ČÁST: TABULKA ----------
            with ui.column().classes('flex-1 min-w-[340px] gap-4'):

                # Místo + termín (v Excelu „modré" buňky)
                with ui.card().classes('w-full p-5 rounded-2xl border shadow-sm gap-3').style(
                        f'background-color:{BARVA_MODRA}; border-color:{BARVA_MODRA_OKR}'):
                    # Místo konání
                    ui.label('Místo konání včetně přesné adresy').classes('text-sm font-bold text-slate-700')
                    if muze_editovat:
                        i_misto = ui.input(value=hl.get('misto_konani') or '').classes('w-full').props('outlined dense')
                        async def _save_misto(_=None, el=i_misto):
                            z = await asyncio.to_thread(_uloz_hlavicka_pole, klic, 'misto_konani',
                                                        (el.value or '').strip() or None, user_id)
                            await _po_zmene(z)
                        i_misto.on('blur', _save_misto)
                    else:
                        ui.label(hl.get('misto_konani') or '—').classes('text-gray-800')

                    # Termín konání (kalendář / našeptávač)
                    ui.label('Termín konání').classes('text-sm font-bold text-slate-700 mt-2')
                    if muze_editovat:
                        termin_val = hl.get('termin_konani')
                        iso = termin_val.isoformat() if isinstance(termin_val, datetime.date) else (
                            str(termin_val)[:10] if termin_val else '')
                        with ui.input(value=iso, placeholder='RRRR-MM-DD').classes('w-60').props(
                                'outlined dense readonly') as i_termin:
                            with i_termin.add_slot('append'):
                                ui.icon('event').classes('cursor-pointer').on('click', lambda: menu_t.open())
                            with ui.menu() as menu_t:
                                kal_t = ui.date(mask='YYYY-MM-DD').bind_value(i_termin).props('today-btn')
                        async def _save_termin(_=None, el=i_termin):
                            d = _parse_datum(el.value, prisne=True)
                            if d is _NEPLATNE_DATUM:
                                ui.notify('Neplatný termín – vyberte datum v kalendáři.', type='warning')
                                return
                            z = await asyncio.to_thread(_uloz_hlavicka_pole, klic, 'termin_konani',
                                                        d, user_id)
                            await _po_zmene(z)
                        # Pole je readonly, takže klientský 'update:model-value' na něm nikdy nevystřelí
                        # a 'blur' přijde ještě před výběrem dne (otevření menu odebere fokus).
                        # Jediný spolehlivý bod je serverová změna hodnoty přes binding z kalendáře.
                        i_termin.on_value_change(_save_termin)
                        kal_t.on('update:model-value', lambda: menu_t.close())
                    else:
                        ui.label(_fmt_datum(hl.get('termin_konani'))).classes('text-gray-800')

                # Tabulka nákladů
                with ui.card().classes('w-full p-0 rounded-2xl border border-gray-200 shadow-sm overflow-hidden'):
                    # hlavička tabulky
                    with ui.element('div').classes(
                        'w-full grid px-4 py-2.5 text-[11px] font-black uppercase '
                        'tracking-wide text-gray-700').style(
                        f'grid-template-columns: 1fr 160px 1fr; background-color:{BARVA_HLAVICKA}'):
                        ui.label('Služba / zboží')
                        ui.label('Náklad bez DPH (Kč)')
                        ui.label('Poznámky pobočky')

                    for p in polozky:
                        poradi = p['poradi']
                        with ui.element('div').classes(
                            'w-full grid items-center px-4 py-2 border-t border-gray-100'
                        ).style('grid-template-columns: 1fr 160px 1fr'):
                            ui.label(p['nazev']).classes('text-sm text-gray-700 pr-2')

                            # náklad – textové pole: tisíce oddělené mezerou + přípona „Kč"
                            if muze_editovat:
                                n_inp = ui.input(
                                    value=(_fmt(p['naklad']) if p['naklad'] is not None else '')
                                ).props('outlined dense suffix="Kč" inputmode=decimal input-class=text-right').classes('w-full')
                                def _mk_save_naklad(el, por):
                                    async def _h(_=None):
                                        cislo = _parse_cislo(el.value)
                                        z = await asyncio.to_thread(_uloz_polozka_pole, klic, por, 'naklad',
                                                                    cislo, user_id)
                                        el.set_value(_fmt(cislo) if cislo is not None else '')
                                        await _po_zmene(z)
                                    return _h
                                h_n = _mk_save_naklad(n_inp, poradi)
                                n_inp.on('blur', h_n)
                                n_inp.on('keydown.enter', h_n)
                            else:
                                ui.label((_fmt(p['naklad']) + ' Kč') if p['naklad'] is not None else '—').classes(
                                    'text-sm text-gray-800 text-right pr-2')

                            # poznámka
                            if muze_editovat:
                                p_inp = ui.input(value=p.get('poznamka') or '').props('outlined dense').classes('w-full')
                                def _mk_save_pozn(el, por):
                                    async def _h(_=None):
                                        z = await asyncio.to_thread(_uloz_polozka_pole, klic, por, 'poznamka',
                                                                    (el.value or '').strip() or None, user_id)
                                        await _po_zmene(z)
                                    return _h
                                p_inp.on('blur', _mk_save_pozn(p_inp, poradi))
                            else:
                                ui.label(p.get('poznamka') or '—').classes('text-sm text-gray-500')

                    # součet
                    with ui.element('div').classes(
                        'w-full grid items-center px-4 py-3 border-t-2 border-gray-300'
                    ).style(f'grid-template-columns: 1fr 160px 1fr; background-color:{BARVA_MODRA}'):
                        ui.label('Náklad celkem v Kč bez DPH').classes('text-sm font-black text-slate-800')
                        total_lbl['el'] = ui.label(_fmt(_soucet(klic)) + ' Kč').classes(
                            'text-sm font-black text-blue-900 text-right pr-2')
                        ui.label('')  # 3. sloupec (zarovnání s tabulkou)

                # Počty účastníků (v Excelu levandulové buňky)
                with ui.card().classes('w-full p-5 rounded-2xl border shadow-sm').style(
                        f'background-color:{BARVA_POCTY}; border-color:{BARVA_POCTY_OKR}'):
                    with ui.row().classes('w-full gap-6 flex-wrap'):
                        for sloupec, popisek in (('pocet_externi', 'Počet externích hostů'),
                                                 ('pocet_interni', 'Počet interních účastníků (OZ, ASM…)')):
                            with ui.column().classes('gap-1'):
                                ui.label(popisek).classes('text-sm font-bold text-gray-600')
                                aktual = hl.get(sloupec)
                                if muze_editovat:
                                    c_inp = ui.number(value=(float(aktual) if aktual is not None else None),
                                                      format='%.2f', min=0).props('outlined dense').classes('w-48')
                                    def _mk_save_pocet(el, sl):
                                        async def _h(_=None):
                                            z = await asyncio.to_thread(_uloz_hlavicka_pole, klic, sl,
                                                                        _parse_cislo(el.value), user_id)
                                            await _po_zmene(z)
                                        return _h
                                    h_c = _mk_save_pocet(c_inp, sloupec)
                                    c_inp.on('blur', h_c)
                                    c_inp.on('keydown.enter', h_c)
                                else:
                                    ui.label(_fmt(aktual) if aktual is not None else '—').classes('text-gray-800')

            # ---------- PRAVÁ ČÁST: SCHVALOVÁNÍ + LOG ----------
            with ui.column().classes('w-full lg:w-80 gap-4'):
                with ui.card().classes('w-full p-4 rounded-2xl border border-gray-200 shadow-sm gap-2'):
                    ui.label('Schvalování nákladů').classes('text-sm font-black text-gray-700 uppercase tracking-wide')
                    _akce_panel()

                if muze_odeslat or muze_schvalit:
                    with ui.card().classes('w-full p-4 rounded-2xl border border-gray-200 shadow-sm gap-2'):
                        ui.label('Upozornit e-mailem').classes('text-sm font-black text-gray-700 uppercase tracking-wide')
                        ui.label('Rozešle e-mail všem s přístupem k této pobočce.').classes('text-xs text-gray-500')
                        ui.button('Rozeslat e-mail', icon='mail', on_click=_otevri_email_dialog).props('no-caps').classes(
                            'w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-sm min-h-[2.5rem] rounded-xl')

                with ui.card().classes('w-full p-4 rounded-2xl border border-gray-200 shadow-sm'):
                    with ui.row().classes('items-center gap-2 mb-2'):
                        ui.icon('history', size='sm', color='gray-500')
                        ui.label('Logování změn').classes('text-sm font-black text-gray-700 uppercase tracking-wide')
                    with ui.scroll_area().classes('w-full').style('max-height: 420px'):
                        _log_panel()

        # =============================================================
        # CHAT (pod tabulkou)
        # =============================================================
        ui.separator().classes('my-6')
        ui.label('Konverzace / poznámky').classes('text-xl font-bold text-gray-700 mb-3')

        @ui.refreshable
        def _chat_panel():
            zpravy = _nacti_chat(klic)
            with ui.column().classes('w-full gap-3 mb-4'):
                if not zpravy:
                    ui.label('Zatím žádné zprávy.').classes('italic text-gray-400')
                for z in zpravy:
                    ca = z.get('created_at')
                    cas = (f'{ca.day}. {ca.month}. {ca.year} {ca.strftime("%H:%M")}'
                           if isinstance(ca, datetime.datetime) else str(ca)[:16])
                    je_moje = z.get('user_id') == user_id
                    bublina = 'bg-blue-50 border-blue-200' if je_moje else 'bg-gray-50 border-gray-200'
                    with ui.card().classes(f'w-full p-3 rounded-xl border {bublina}'):
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label(z.get('jmeno') or '—').classes('font-bold text-gray-800 text-sm')
                            ui.label(cas).classes('text-xs text-gray-400')
                        if z.get('text'):
                            ui.label(z['text']).classes('text-gray-700 whitespace-pre-wrap')
                        if z.get('priloha_filename'):
                            url = f'{PRILOHY_ROUTE}/{z["priloha_filename"]}'
                            orig = z.get('priloha_original') or 'příloha'
                            ext = os.path.splitext(orig)[1].lower()
                            ikona = 'image' if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp') else 'attach_file'
                            with ui.element('a').props(f'href="{url}" target="_blank"').classes(
                                'flex items-center gap-1 text-xs text-blue-600 bg-blue-50 border border-blue-200 '
                                'rounded px-2 py-1 hover:bg-blue-100 no-underline w-fit mt-1'):
                                ui.icon(ikona).classes('text-sm')
                                ui.label(orig[:40] + ('…' if len(orig) > 40 else ''))

        _chat_panel()

        # vstup nové zprávy + příloha (smí každý s přístupem)
        _pending = {'bytes': None, 'nazev': None}
        with ui.column().classes('w-full gap-1'):
            lbl_priloha = ui.label('').classes('text-xs text-green-600 italic')
            with ui.row().classes('w-full gap-2 items-end'):
                txt_in = ui.textarea(placeholder='Napsat zprávu…').classes('flex-1').props('outlined dense autogrow')

                async def _nahraj(e, _p=_pending, _lbl=lbl_priloha):
                    _p['bytes'] = await e.file.read()
                    _p['nazev'] = e.file.name
                    _lbl.set_text(f'📎 {e.file.name[:40]}')

                upl = ui.upload(auto_upload=True, on_upload=_nahraj).props('accept="*/*"').style('display:none')

                async def _otevri_soubor(_u=upl):
                    await ui.run_javascript(
                        f'document.querySelector("#c{_u.id} input[type=file]").click()')

                async def _odesli():
                    text = (txt_in.value or '').strip()
                    if not text and not _pending.get('bytes'):
                        return
                    rel = None
                    if _pending.get('bytes'):
                        rel = await asyncio.to_thread(_uloz_prilohu_na_disk, klic,
                                                      _pending['nazev'], _pending['bytes'])
                    ok = await asyncio.to_thread(_pridej_chat, klic, user_id,
                                                 text or None, rel, _pending.get('nazev'))
                    if ok:
                        txt_in.set_value('')
                        _pending['bytes'] = None
                        _pending['nazev'] = None
                        lbl_priloha.set_text('')
                        _chat_panel.refresh()

                txt_in.on('keydown.enter', lambda e: None)  # Enter dělá nový řádek; odesílá tlačítko
                ui.button(icon='attach_file', on_click=_otevri_soubor).props('flat round dense').classes(
                    'text-gray-400').tooltip('Přiložit soubor')
                ui.button('Odeslat', icon='send', on_click=_odesli).classes(
                    'bg-blue-600 hover:bg-blue-700 text-white font-bold h-10 px-5 rounded-xl')

    # -----------------------------------------------------------------
    _panel()


def _popis_log(z):
    """Sestaví čitelný popis jednoho log záznamu."""
    akce = z.get('akce')
    pole = z.get('pole') or ''
    stara = z.get('stara_hodnota')
    nova = z.get('nova_hodnota')
    if akce == 'zmena':
        zmena = f'{stara or "—"} → {nova or "—"}'
        return f'Změna „{pole}": {zmena}'
    if akce == 'odeslano':
        return 'Náklady označeny jako finální a odeslány ke schválení.'
    if akce == 'schvaleno':
        return 'Náklady schváleny.'
    if akce == 'vraceno':
        return 'Vráceno k přepracování.'
    if akce == 'reset_schvaleni':
        return 'Úprava po schválení – stav vrácen na „Ke schválení".'
    return f'{akce}: {pole} {nova or ""}'.strip()
