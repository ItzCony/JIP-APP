import os
import re
import json
import asyncio
import unicodedata
import tempfile
import hashlib
import mysql.connector
from mysql.connector import pooling
import time
import random
import secrets as _secrets
import threading as _threading
import intranet_prava  # jen statický katalog práv (bez dalších importů)

# K-5: bcrypt import s fallbackem
try:
    import bcrypt as _bcrypt
    _BCRYPT_DOSTUPNY = True
except ImportError:
    _bcrypt = None
    _BCRYPT_DOSTUPNY = False

# Pevný, neměnný seznam poboček — sdílená konstanta, používá se v UI i denormalizaci
POBOCKY = ["010", "011", "012", "013", "014", "017", "019", "020", "026", "028", "032", "033", "034", "037"]

# K-6: Rate limiting přihlášení
_LOGIN_POKUSY: dict = {}  # email → {'pocet': int, 'prvni': float, 'zamcen_do': float}
_LOGIN_LOCK = _threading.Lock()
_MAX_POKUSY = 5
_OKNO_SEKUND = 300   # 5 minut
_LOCK_SEKUND = 900   # 15 minut

def _zkontroluj_rate_limit(email: str) -> tuple:
    """
    Vrátí (je_zamcen: bool, zbyvajici_sekundy: int).
    Zamkne účet po _MAX_POKUSY neúspěšných pokusech v _OKNO_SEKUND sekundách.
    """
    now = time.time()
    with _LOGIN_LOCK:
        info = _LOGIN_POKUSY.get(email)
        if info and now < info.get('zamcen_do', 0):
            return True, int(info['zamcen_do'] - now)
        return False, 0

def _zaznamenej_neuspech(email: str):
    now = time.time()
    with _LOGIN_LOCK:
        info = _LOGIN_POKUSY.get(email, {'pocet': 0, 'prvni': now, 'zamcen_do': 0})
        if now - info['prvni'] > _OKNO_SEKUND:
            info = {'pocet': 0, 'prvni': now, 'zamcen_do': 0}
        info['pocet'] += 1
        if info['pocet'] >= _MAX_POKUSY:
            info['zamcen_do'] = now + _LOCK_SEKUND
        _LOGIN_POKUSY[email] = info

def _zaznamenej_uspech(email: str):
    with _LOGIN_LOCK:
        _LOGIN_POKUSY.pop(email, None)

def vycistit_stare_login_pokusy():
    """Odstraní vyhaslé záznamy rate-limitu přihlášení (mimo okno a bez aktivního
    zámku) — jinak dict roste s každým překlepnutým e-mailem donekonečna."""
    now = time.time()
    with _LOGIN_LOCK:
        stare = [e for e, i in _LOGIN_POKUSY.items()
                 if now - i.get('prvni', 0) > _OKNO_SEKUND and now > i.get('zamcen_do', 0)]
        for e in stare:
            del _LOGIN_POKUSY[e]

def _je_nouzovy_admin_email(email: str) -> bool:
    """True jen pro nouzového (break-glass) admina z env proměnných.

    POZOR: DB administrátoři (právo 'vse') ZÁMĚRNĚ NEJSOU vyňati z rate-limitingu
    přihlášení — jinak by šel nejmocnější účet brute-forcovat bez zámku (viz
    bezpečnostní audit). Z limitu je vyňat pouze nouzový admin, který je chráněn
    serverovým tajemstvím (env) a slouží jako záchrana při výpadku DB."""
    return bool(_NOUZOVY_EMAIL) and email.strip().lower() == _NOUZOVY_EMAIL.strip().lower()

# K-7: Rate limiting pro reset kódy
_RESET_POKUSY: dict = {}  # email → list of timestamps
_RESET_LOCK = _threading.Lock()

def _zkontroluj_reset_limit(email: str) -> bool:
    """Vrátí True pokud je dovoleno odeslat kód (max 3 za hodinu)."""
    now = time.time()
    with _RESET_LOCK:
        times = [t for t in _RESET_POKUSY.get(email, []) if now - t < 3600]
        if len(times) >= 3:
            return False
        times.append(now)
        _RESET_POKUSY[email] = times
        return True

# Hlavní administrátor (iduser=1) je skrytý: nezobrazuje se v UI ani nedostává
# žádné e-maily/notifikace. Účet dál plně funguje (přihlášení, práva 'vse').
SKRYTY_ADMIN_ID = 1

SERVISNI_EMAIL = 'admin@admin.cz'

# Cache identit skrytých účtů (jména + e-maily). Audit log drží jen jméno, proto
# potřebujeme obojí. None = ještě nenačteno / DB nebyla dostupná (zkusí se znovu).
_SKRYTE_UCTY = None
_SKRYTE_UCTY_POKUS = 0.0   # čas posledního neúspěchu — bez DB nezkoušíme každý zápis

def _nacti_skryte_ucty():
    """(jména, e-maily) skrytého admina a servisního účtu — vše lowercase."""
    global _SKRYTE_UCTY, _SKRYTE_UCTY_POKUS
    if time.time() - _SKRYTE_UCTY_POKUS < 60:
        return set(), {SERVISNI_EMAIL}
    _SKRYTE_UCTY_POKUS = time.time()
    conn = get_db_connection()
    if not conn:
        return set(), {SERVISNI_EMAIL}
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, surname, email FROM user WHERE iduser=%s OR email=%s",
            (SKRYTY_ADMIN_ID, SERVISNI_EMAIL))
        jmena, emaily = set(), {SERVISNI_EMAIL}
        for name, surname, email in cursor.fetchall():
            jmeno = f"{name or ''} {surname or ''}".strip().lower()
            if jmeno:
                jmena.add(jmeno)
            if email:
                emaily.add(email.strip().lower())
        _SKRYTE_UCTY = (jmena, emaily)
        return _SKRYTE_UCTY
    except Exception:
        return set(), {SERVISNI_EMAIL}
    finally:
        if cursor: cursor.close()
        conn.close()

def je_skryty_ucet(jmeno: str = '', email: str = '') -> bool:
    """True pro skrytého admina (iduser=1) a servisní admin@admin.cz.

    Tyhle účty se nesmí objevit v audit logu ani v přehledu přihlášených.
    """
    jmena, emaily = _SKRYTE_UCTY if _SKRYTE_UCTY is not None else _nacti_skryte_ucty()
    return ((jmeno or '').strip().lower() in jmena
            or (email or '').strip().lower() in emaily)

def _bez_admin_prav(prava_str: str) -> str:
    """Vyřízne práva 'Administrace portálu' z řetězce práv.

    Přiděluje se jen skrytému adminovi (iduser=1) — přes UI ani přes
    podvrženou hodnotu se k nikomu jinému nesmí dostat.
    """
    return ",".join(p for p in [p.strip() for p in (prava_str or '').split(',')]
                    if p and p not in intranet_prava.ADMIN_ONLY_PRAVA)

# K-2: Nouzový admin z env proměnných
_NOUZOVY_EMAIL = os.environ.get('EMERGENCY_ADMIN_EMAIL', '')
_NOUZOVY_HESLO = os.environ.get('EMERGENCY_ADMIN_PASSWORD', '')

def _overit_nouzovy_admin(email: str, heslo: str):
    """Nouzový admin pouze pokud jsou env proměnné nastaveny a neprázdné."""
    if not _NOUZOVY_EMAIL or not _NOUZOVY_HESLO:
        return None
    if email == _NOUZOVY_EMAIL and heslo == _NOUZOVY_HESLO:
        return 999999, 'Nouzový Administrátor', 'OK'
    return None

def export_zip_heslo() -> bytes:
    """Heslo k šifrovaným ZIP exportům — pouze z env, žádný fallback v kódu.

    Bez JIPKA_EXPORT_ZIP_HESLO export selže s jasnou hláškou; heslo natvrdo
    v repu by odemklo každý dřívější i budoucí export osobních údajů.
    """
    heslo = os.environ.get('JIPKA_EXPORT_ZIP_HESLO', '').strip()
    if not heslo:
        raise RuntimeError(
            'Export není nakonfigurován: chybí proměnná prostředí '
            'JIPKA_EXPORT_ZIP_HESLO (heslo k šifrovanému ZIP).'
        )
    return heslo.encode('utf-8')


SOUBOR_NASTAVENI_INTRANETU = 'nastaveni_intranetu.json'

# Veřejná URL portálu pro odkazy v e-mailech. Není tajemství, ale patří ke
# konfiguraci prostředí (test/ostrý), ne do editovatelného nastavení portálu.
APP_URL = os.environ.get('JIPKA_APP_URL', '').strip().rstrip('/')

# ==========================================
# GLOBÁLNÍ PAMĚŤOVÁ CACHE (RAM)
# ==========================================
CACHE_NASTAVENI = None
_CACHE_NASTAVENI_MTIME = 0.0
CACHE_MYSQL = None
CACHE_SMTP = None
DB_POOL = None
DB_ZAKLAD_INICIALIZOVAN = False

# Per-user permission cache: {user_id: (prava_list, timestamp)}
_CACHE_PRAVA = {}
_CACHE_PRAVA_TTL = 600  # 10 minut (bylo 5) — invaliduje vymazat_cache_prav() při změně práv

# Krátkodobá cache pro těžké dotazy (zamezuje blokování event loop při refresh)
_CACHE_UZIVATELE = {'data': None, 'ts': 0.0}
_CACHE_ZADOSTI_ALL = {'data': None, 'ts': 0.0}
_CACHE_REFRESH_TTL = 3600.0  # sekund (bylo 300) — cache přehrává bg job co 20 min, TTL je jen pojistka

# Cache pro správu uživatelů / oddělení / číselníky
_CACHE_ODDELENI = {'data': None, 'ts': 0.0}
_CACHE_ROLE = {'data': None, 'ts': 0.0}
_CACHE_TYPY_VOLNA = {'data': None, 'ts': 0.0}
_CACHE_VOLNA_KALENDAR_ALL = {'data': None, 'ts': 0.0}
_CACHE_PRESZASY_ALL = {'data': None, 'ts': 0.0}
_CACHE_SPRAVA_TTL = 3600.0  # sekund (bylo 900) — oddělení/role/typy volna se mění zřídka

def invaliduj_cache_dochazky():
    """Zneplatní cache žádostí i uživatelů — volat po každé změně stavu žádosti."""
    _CACHE_UZIVATELE['ts'] = 0.0
    _CACHE_ZADOSTI_ALL['ts'] = 0.0
    _CACHE_PRESZASY_ALL['ts'] = 0.0
    _CACHE_VOLNA_KALENDAR_ALL['ts'] = 0.0

def invaliduj_cache_sprava():
    """Zneplatní cache správy (oddělení, role, volna) — volat po mutacích v admin sekci."""
    _CACHE_UZIVATELE['ts'] = 0.0
    _CACHE_ODDELENI['ts'] = 0.0
    _CACHE_ROLE['ts'] = 0.0
    _CACHE_TYPY_VOLNA['ts'] = 0.0
    _CACHE_VOLNA_KALENDAR_ALL['ts'] = 0.0

def _prehraj_cache_sync():
    """Přetáhne všechna sdílená data z DB do cache. Běží ve vlákně, ne v event-loopu.

    Každou cache zneplatníme těsně před jejím načtením, takže studený je vždy
    max. jeden slovník po dobu jednoho DB dotazu — ne všechny naráz.
    """
    for cache, nacti in (
        (_CACHE_ODDELENI, ziskej_vsechna_oddeleni),
        (_CACHE_ROLE, ziskej_vsechny_role),
        (_CACHE_TYPY_VOLNA, ziskej_typy_volna),
        (_CACHE_UZIVATELE, ziskej_vsechny_uzivatele),
        (_CACHE_ZADOSTI_ALL, ziskej_zadosti),
        (_CACHE_PRESZASY_ALL, ziskej_presczasy),
        (_CACHE_VOLNA_KALENDAR_ALL, ziskej_vsechna_volna_kalendar),
    ):
        try:
            cache['ts'] = 0.0
            nacti()
        except Exception as e:
            print(f"[cache] Obnova {nacti.__name__} selhala: {e}")

async def bg_obnova_cache(interval: float = 1200.0) -> None:
    """Na pozadí drží cache dochazky teplou. Volat z app.on_startup.

    První běh hned po startu (uživatel po deployi nečeká na studenou cache),
    pak každých 20 minut. TTL v getterech zůstává jako pojistka, kdyby task umřel.
    """
    while True:
        try:
            await asyncio.to_thread(_prehraj_cache_sync)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[cache] bg_obnova_cache: {e}")
        await asyncio.sleep(interval)

def hash_heslo(heslo: str) -> str:
    """Hashuje heslo pomocí bcrypt (12 rounds).

    Bez bcryptu NOVÝ hash nevytvoříme — nesolený SHA-256 byl bezpečnostní riziko
    (rychlé prolomení). Ověřování starých SHA-256 hashů zůstává v overit_heslo
    kvůli migraci, ale nové se ukládají výhradně přes bcrypt."""
    if not _BCRYPT_DOSTUPNY:
        raise RuntimeError(
            "bcrypt není nainstalován — nelze bezpečně uložit heslo. "
            "Nainstalujte balíček 'bcrypt' (pip install bcrypt)."
        )
    return _bcrypt.hashpw(str(heslo).encode('utf-8'), _bcrypt.gensalt(rounds=12)).decode('utf-8')

def _je_bcrypt_hash(h: str) -> bool:
    return h.startswith('$2b$') or h.startswith('$2a$') or h.startswith('$2y$')

def overit_heslo(heslo: str, ulozeny_hash: str) -> bool:
    """Ověří heslo. Podporuje bcrypt i starý SHA-256 (pro migraci)."""
    if _BCRYPT_DOSTUPNY and _je_bcrypt_hash(ulozeny_hash):
        try:
            return _bcrypt.checkpw(str(heslo).encode('utf-8'), ulozeny_hash.encode('utf-8'))
        except Exception:
            return False
    else:
        # SHA-256 fallback pro staré hashe
        return hashlib.sha256(str(heslo).encode('utf-8')).hexdigest() == ulozeny_hash

# Dočasné úložiště reset kódů: email -> {'kod': str, 'expiry': float}
_RESET_KODY = {}

def _vycistit_reset_kody():
    """Odstraní všechny vypršelé reset kódy — volat periodicky."""
    now = time.time()
    expiry_emails = [e for e, v in _RESET_KODY.items() if now > v['expiry']]
    for e in expiry_emails:
        del _RESET_KODY[e]

def vygeneruj_reset_kod(email):
    """Vrátí 6místný kód pokud email existuje a účet je aktivní, jinak None."""
    # K-7: Rate limiting pro generování reset kódů
    if not _zkontroluj_reset_limit(email):
        return None  # rate limit exceeded — volající zobrazí stejnou zprávu jako při neexistujícím emailu
    db = ziskej_vsechny_uzivatele()
    if email not in db or not db[email]['aktivni']:
        return None
    # K-10: Bezpečný generátor kódu
    kod = f"{_secrets.randbelow(1_000_000):06d}"
    _RESET_KODY[email] = {'kod': kod, 'expiry': time.time() + 900}
    return kod

def overit_reset_kod(email, kod):
    """Vrátí True pokud kód sedí a nevypršel (platnost 15 min).

    Anti-brute-force: po 5 chybných pokusech se kód zneplatní, aby nešlo
    6místný kód uhádnout opakovaným zkoušením (generování je limitované zvlášť)."""
    zaznam = _RESET_KODY.get(email)
    if not zaznam:
        return False
    if time.time() > zaznam['expiry']:
        del _RESET_KODY[email]
        return False
    if zaznam['kod'] == (kod or '').strip():
        return True
    zaznam['pokusy'] = zaznam.get('pokusy', 0) + 1
    if zaznam['pokusy'] >= 5:
        _RESET_KODY.pop(email, None)
    return False

def zmen_heslo_emailem(email, nove_heslo):
    """Změní heslo uživateli a vymaže reset kód. Vrátí True/False."""
    conn = get_db_connection()
    if not conn:
        return False
    cursor = None
    try:
        cursor = conn.cursor()
        # uživatel si heslo zvolil sám → vynucená změna je splněná
        cursor.execute("UPDATE user SET password_hash=%s, zmena_hesla_nutna=0, heslo_zmeneno=NOW() WHERE email=%s", (hash_heslo(nove_heslo), email))
        conn.commit()
        _CACHE_UZIVATELE['ts'] = 0.0
        _RESET_KODY.pop(email, None)
        return cursor.rowcount > 0
    except Exception as e:
        print(f"[zmen_heslo_emailem] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def nastav_zustatek_dovolene(user_id, nova_zbyvajici_h, vybrano_h):
    """Přepočítá a uloží base_vacation tak, aby zbývající zůstatek odpovídal nova_zbyvajici_h.
    Vzorec: base_vacation = nova_zbyvajici_h + vybrano_h - carried_over_vacation  (min 0)
    Vrátí True při úspěchu, False při chybě."""
    conn = get_db_connection()
    if not conn:
        return False
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT carried_over_vacation FROM user WHERE iduser=%s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
        prevod = float(row['carried_over_vacation'] or 0)
        novy_zaklad = max(0.0, nova_zbyvajici_h + vybrano_h - prevod)
        cursor.execute("UPDATE user SET base_vacation=%s WHERE iduser=%s", (round(novy_zaklad, 2), user_id))
        conn.commit()
        _CACHE_UZIVATELE['ts'] = 0.0
        return cursor.rowcount > 0
    except Exception as e:
        print(f"[nastav_zustatek_dovolene] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def _norm_text(s):
    """Normalizace textu pro porovnání: bez diakritiky, malá písmena, sjednocené mezery."""
    if s is None:
        return ''
    s = str(s).strip().lower().replace('\xa0', ' ')
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    return ' '.join(s.split())

def import_realnych_zustatku_dovolene(mapa_zustatku, datum_ke_dni=None, nahled=False):
    """Uloží reálné zůstatky dovolené importované z Excelu.
    mapa_zustatku: dict {osobni_cislo(int): {'zustatek': float, 'priznak': str}}
        – párováno na user.iduser; navíc se ověřuje shoda příznaku (písmenná předpona
        z „Osobního čísla", např. „JV") s priznak.nazev v systému (bez diakritiky,
        case-insensitive). Při neshodě se řádek neimportuje a vrátí v seznamu
        nesedi_priznak. Prázdný příznak na obou stranách se považuje za shodu.
        (Pro zpětnou kompatibilitu lze předat i {osobni_cislo: zustatek(float)}.)
    datum_ke_dni: datum (datetime.date / 'YYYY-MM-DD'), ke kterému zůstatek platí.
    nahled: True = pouze spočítá shody se systémem, nic nezapíše (pro potvrzovací dotaz).
    Vrátí (shodne, nenalezeno_seznam, nesedi_priznak_seznam) nebo (0, None, None) při chybě.
        shodne = počet záznamů spárovaných se systémem (zapsaných, nebo při náhledu k zápisu připravených)."""
    conn = get_db_connection()
    if not conn:
        return 0, None, None
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        shodne = 0
        nenalezeno = []
        nesedi_priznak = []
        for os_cislo, info in mapa_zustatku.items():
            if isinstance(info, dict):
                zustatek = info.get('zustatek')
                priznak_f = info.get('priznak', '')
            else:
                zustatek, priznak_f = info, ''

            cursor.execute(
                "SELECT pr.nazev AS priznak_nazev FROM user u "
                "LEFT JOIN priznak pr ON u.priznak_id = pr.id WHERE u.iduser=%s",
                (os_cislo,))
            row = cursor.fetchone()
            if not row:
                nenalezeno.append(os_cislo)
                continue
            # Ověření příznaku (písmenná předpona osobního čísla) proti systému.
            priznak_sys = row.get('priznak_nazev') or ''
            if _norm_text(priznak_f) != _norm_text(priznak_sys):
                nesedi_priznak.append(f"{priznak_f}{os_cislo} (systém: {priznak_sys or '—'})")
                continue
            if not nahled:
                cursor.execute(
                    "UPDATE user SET realny_zustatek_dovolene=%s, realny_zustatek_dovolene_datum=%s WHERE iduser=%s",
                    (round(float(zustatek), 2), datum_ke_dni, os_cislo))
            shodne += 1
        if not nahled:
            conn.commit()
            _CACHE_UZIVATELE['ts'] = 0.0
        return shodne, nenalezeno, nesedi_priznak
    except Exception as e:
        print(f"[import_realnych_zustatku_dovolene] {e}")
        return 0, None, None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def vycistit_stare_cache_prava(max_vek_sekund=7200):
    """Odstraní zastaralé záznamy z per-user cache práv (ochrana před memory leak)."""
    now = time.time()
    stare = [uid for uid, (_, ts) in _CACHE_PRAVA.items() if now - ts > max_vek_sekund]
    for uid in stare:
        del _CACHE_PRAVA[uid]

# Moduly portálu: klíč v nastavení → (popisek v UI, název do audit logu).
# Jediný zdroj pravdy pro příkaz /modul v audit konzoli (v UI se moduly
# nepřepínají). Alias pro konzoli = klíč bez sufixu „_zapnuty".
MODULY = {
    'finance_zapnuty':       ('Modul Aprovia (Finance)',        'Aprovia'),
    'kviz_zapnuty':          ('Modul Zkouškový Kvíz',           'Kvíz'),
    'veletrh_zapnuty':       ('Modul Veletrh',                  'Veletrh'),
    'znacky_zapnuty':        ('Modul Značky JIP',               'Značky JIP'),
    'znacky_emaily_zapnuty': ('E-maily: Modul Značky JIP',      'E-maily Značky JIP'),
    'smeny_zapnuty':         ('Modul Plánování směn',           'Plánování směn'),
    'planogram_zapnuty':     ('🚬 Modul Plánogram tabáku',       'Plánogram tabáku'),
    'ochutnavky_zapnuty':    ('🍽️ Modul Ochutnávky MO a CC',     'Ochutnávky MO a CC'),
    'sankce_zapnuty':        ('⚖️ Modul Sankce',                 'Sankce'),
    'spolvecer_zapnuty':     ('🎉 Modul Společenský večer',      'Společenský večer'),
    'vizitky_zapnuty':       ('🪪 Modul Vizitky a podpisy',      'Vizitky a podpisy'),
    'cenopripad_zapnuty':    ('🏷️ Modul Cenopřípad',             'Cenopřípad'),
    'asm_zapnuty':           ('📝 Modul Formuláře ASM',          'Formuláře ASM'),
    'lupa_zapnuty':          ('🔍 Modul Lupou na obchod',        'Lupou na obchod'),
    'schuzky_zapnuty':       ('🗓️ Modul Schůzky s vedoucími',    'Schůzky s vedoucími'),
    'presczasy_zapnuty':     ('⏰ Přesčasy (Evidence absencí)',   'Přesčasy'),
    # Má i vlastní přepínač na záložce Narozeniny v Nastavení portálu.
    'narozeniny_zapnuty':    ('Modul Narozeniny',               'Narozeniny'),
}

def nacti_nastaveni_intranetu():
    """Nastavení portálu z JSONu, cachované v RAM podle mtime souboru.

    Cache se zahodí, jakmile se soubor na disku změní — tedy i při ruční editaci
    na serveru nebo zápisu z jiného workeru, ne jen po zápisu z tohohle procesu.
    Cena je jeden stat() na čtení. Strop: mtime má na některých FS granularitu
    1 s, takže dvě změny ve stejné sekundě se můžou slít; u zápisů z UI to jistí
    uloz_nastaveni_intranetu(), které cache nastaví rovnou.
    """
    global CACHE_NASTAVENI, _CACHE_NASTAVENI_MTIME
    try:
        mtime = os.path.getmtime(SOUBOR_NASTAVENI_INTRANETU)
    except OSError:
        mtime = 0.0

    if CACHE_NASTAVENI is not None and mtime == _CACHE_NASTAVENI_MTIME:
        return CACHE_NASTAVENI

    _CACHE_NASTAVENI_MTIME = mtime
    if not os.path.exists(SOUBOR_NASTAVENI_INTRANETU):
        CACHE_NASTAVENI = {"dlazdice_1": "Zkouškový Kvíz", "dlazdice_2": "Dokumenty", "dlazdice_3": "Docházka a Volno"}
        return CACHE_NASTAVENI
    try:
        with open(SOUBOR_NASTAVENI_INTRANETU, 'r', encoding='utf-8') as f:
            CACHE_NASTAVENI = json.load(f)
            return CACHE_NASTAVENI
    except Exception:
        CACHE_NASTAVENI = {"dlazdice_1": "Zkouškový Kvíz", "dlazdice_2": "Dokumenty", "dlazdice_3": "Docházka a Volno"}
        return CACHE_NASTAVENI

def je_validni_db_identifikator(s: str) -> bool:
    """Bezpečný název DB/tabulky/sloupce pro místa, kam nelze dát %s placeholder.

    Povolí jen [A-Za-z0-9_$] a délku 1–64 (limit MySQL). Brání SQL injection
    přes interpolovaný identifikátor (např. CREATE DATABASE `{nazev}`)."""
    return bool(s) and len(s) <= 64 and re.fullmatch(r'[A-Za-z0-9_$]+', s) is not None


def zapis_json_atomicky(cesta: str, data, *, tajne: bool = False, **json_kwargs) -> None:
    """Atomický zápis JSON na disk.

    Zapíše do dočasného souboru ve stejném adresáři, vynutí flush+fsync a teprve
    pak ho přejmenuje na cílový (``os.replace`` = atomický rename). Když dojde
    k pádu nebo SIGKILL (např. po vypršení TimeoutStopSec v systemd) uprostřed
    zápisu, cílový soubor zůstane buď v původním, nebo už v novém stavu — nikdy
    prázdný ani oříznutý. Výjimku propaguje volajícímu (ten ji loguje).

    tajne=True → soubor dostane práva 0600 (jen vlastník), pro konfiguráky
    s hesly (MySQL/SMTP). mkstemp je 0600 už od začátku, chmod navíc opraví
    i případný starší soubor s volnějšími právy."""
    json_kwargs.setdefault('ensure_ascii', False)
    cilovy_adresar = os.path.dirname(os.path.abspath(cesta))
    fd, tmp = tempfile.mkstemp(dir=cilovy_adresar, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, **json_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, cesta)
        if tajne:
            try:
                os.chmod(cesta, 0o600)
            except OSError:
                pass
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def uloz_nastaveni_intranetu(data):
    global CACHE_NASTAVENI, _CACHE_NASTAVENI_MTIME
    CACHE_NASTAVENI = data
    try:
        # Hesla už tu nejsou (SMTP jde z env), ale soubor leží v docrootu →
        # 0600 zůstává jako pojistka proti servírování přes HTTP.
        zapis_json_atomicky(SOUBOR_NASTAVENI_INTRANETU, data, tajne=True, indent=4)
        # Vlastní zápis nesmí zneplatnit právě naplněnou cache.
        _CACHE_NASTAVENI_MTIME = os.path.getmtime(SOUBOR_NASTAVENI_INTRANETU)
    except Exception as e:
        print(f"Chyba uložení nastavení na disk: {e}")

def nacti_mysql():
    """Přístup k DB výhradně z proměnných prostředí JIPKA_DB_* — na disku nic není.

    Config je read-only (žádné UI ho nepřepisuje), takže ho stačí přečíst jednou.
    Změna údajů = úprava EnvironmentFile na serveru + restart služby.

    enabled se neukládá, odvozuje se: prázdný user/db nebo nesmyslný název DB
    znamená "neexistuje připojení" — aplikace pak jede v nouzovém režimu
    (break-glass admin z EMERGENCY_ADMIN_*) místo padání na půl cesty.
    """
    global CACHE_MYSQL
    if CACHE_MYSQL is not None:
        return CACHE_MYSQL

    cfg = {
        "host": os.environ.get('JIPKA_DB_HOST', 'localhost'),
        "port": os.environ.get('JIPKA_DB_PORT', '3306'),
        "user": os.environ.get('JIPKA_DB_USER', '').strip(),
        "pass": os.environ.get('JIPKA_DB_PASS', ''),
        "db":   os.environ.get('JIPKA_DB_NAME', '').strip(),
    }
    # Název DB jde do CREATE DATABASE jako identifikátor (nelze %s), a od zrušení
    # konfigurace v UI ho nikdo jiný nevaliduje — kontrola musí být tady.
    cfg["enabled"] = bool(cfg["user"] and je_validni_db_identifikator(cfg["db"]))
    if cfg["user"] and not cfg["enabled"]:
        print("[db] JIPKA_DB_NAME chybí nebo je neplatný název databáze — připojení vypnuto.")

    CACHE_MYSQL = cfg
    return CACHE_MYSQL


def nacti_smtp():
    """Přístup k SMTP výhradně z proměnných prostředí JIPKA_SMTP_* — na disku nic není.

    Config je read-only (žádné UI ho nepřepisuje), takže ho stačí přečíst jednou.
    Změna údajů = úprava EnvironmentFile na serveru + restart služby.

    enabled se neukládá, odvozuje se z kompletnosti údajů. JIPKA_SMTP_ENABLED=0
    je vypínač pro odstávku, kdy údaje mají zůstat nastavené.
    Klíče se jmenují stejně jako dřív v JSONu, aby volající kód zůstal beze změny.
    """
    global CACHE_SMTP
    if CACHE_SMTP is not None:
        return CACHE_SMTP

    # float() kvůli starým hodnotám z ui.number, které se ukládaly jako 465.0.
    try:
        port = int(float(os.environ.get('JIPKA_SMTP_PORT', '') or 465))
    except (ValueError, TypeError):
        print("[smtp] JIPKA_SMTP_PORT není číslo — používám 465.")
        port = 465

    cfg = {
        "smtp_server":   os.environ.get('JIPKA_SMTP_HOST', '').strip(),
        "smtp_port":     port,
        "smtp_user":     os.environ.get('JIPKA_SMTP_USER', '').strip(),
        "smtp_password": os.environ.get('JIPKA_SMTP_PASS', ''),
        # Prázdný = odvodí se ze smtp_server (viz intranet_emaily).
        "imap_server":   os.environ.get('JIPKA_SMTP_IMAP_HOST', '').strip(),
    }
    cfg["udaje_kompletni"] = bool(cfg["smtp_server"] and cfg["smtp_user"] and cfg["smtp_password"])
    cfg["vypnuto_operatorem"] = os.environ.get('JIPKA_SMTP_ENABLED', '1').strip().lower() in ('0', 'false', 'no')
    cfg["enabled"] = cfg["udaje_kompletni"] and not cfg["vypnuto_operatorem"]

    if not cfg["enabled"]:
        duvod = "vypnuto přes JIPKA_SMTP_ENABLED" if cfg["vypnuto_operatorem"] else "chybí JIPKA_SMTP_HOST/USER/PASS"
        print(f"[smtp] Odesílání e-mailů je vypnuto — {duvod}.")

    CACHE_SMTP = cfg
    return CACHE_SMTP

# ========================================================
# CONNECTION POOLING (ZRYCHLENÍ DATABÁZE)
# ========================================================
def _zaplatuj_pooled_close() -> None:
    """conn.close() nesmí nikdy vyhodit výjimku.

    PooledMySQLConnection.close() volá při pool_reset_session=True metodu
    cnx.reset_session(). Na mrtvém spojení (server zavřel wait_timeout, restart
    MySQL, výpadek sítě) to hodí OperationalError. Protože se close() volá
    v `finally:` blocích (207 míst v kódu), taková výjimka PŘEPÍŠE původní
    chybu i návratovou hodnotu — uživatel pak vidí "MySQL Connection not
    available" místo skutečné příčiny.

    Konektor vrací spojení do poolu ve `finally` uvnitř close(), takže
    spolknutí výjimky pool neponičí — jen přestane maskovat chyby.
    """
    trida = pooling.PooledMySQLConnection
    if getattr(trida, "_jip_bezpecny_close", False):
        return
    _puvodni_close = trida.close

    def _bezpecny_close(self):
        try:
            _puvodni_close(self)
        except Exception:
            # Podle verze konektoru padne reset_session() PŘED add_connection()
            # → spojení by se do poolu nevrátilo a pool by po pool_size
            # takových chybách vyschl. Vrátíme ho ručně; mrtvé spojení pool
            # při dalším get_connection() sám reconnectne.
            cnx = getattr(self, "_cnx", None)
            self._cnx = None
            if cnx is None:
                return
            try:
                cnx.close()
            except Exception:
                pass
            try:
                self._cnx_pool.add_connection(cnx)
            except Exception:
                pass

    trida.close = _bezpecny_close
    trida._jip_bezpecny_close = True

_zaplatuj_pooled_close()

def get_db_connection():
    global DB_POOL
    cfg = nacti_mysql()
    if not cfg.get("enabled"): return None

    if DB_POOL is None:
        try:
            # Více souběžných I/O vláken (viz intranet_jobs.io) si bere spojení
            # z poolu — proto vyšší velikost, ať se nevyčerpá při exportech/
            # náporu. Lze přepsat proměnnou JIPKA_DB_POOL (max 32 dle konektoru).
            try:
                _pool_size = int(os.environ.get("JIPKA_DB_POOL", "20"))
            except (ValueError, TypeError):
                _pool_size = 20
            _pool_size = max(4, min(_pool_size, 32))
            DB_POOL = mysql.connector.pooling.MySQLConnectionPool(
                pool_name="intranet_pool",
                pool_size=_pool_size,
                pool_reset_session=True,
                host=cfg["host"],
                port=cfg["port"],
                user=cfg["user"],
                password=cfg["pass"],
                database=cfg["db"],
                charset="utf8mb4",
                collation="utf8mb4_unicode_ci"
            )
        except Exception as e:
            print(f"Chyba při tvorbě DB Poolu: {e}")
            return None

    conn = None
    try:
        conn = DB_POOL.get_connection()
        # Spojení mohlo v poolu odumřít (wait_timeout serveru, restart MySQL,
        # výpadek sítě). Bez ověření by chyba vylezla až uprostřed práce —
        # nebo až v conn.close(). ping(reconnect=True) mrtvé spojení oživí.
        conn.ping(reconnect=True, attempts=2, delay=1)
        return conn
    except Exception as e:
        print(f"Chyba při získávání připojení z poolu: {e}")
        if conn is not None:
            conn.close()  # zpět do poolu, jinak by pool po chybách vyschl
        return None

def zavri_db_pool() -> None:
    """Zavře nečinná spojení v DB poolu. Volat z app.on_shutdown.

    mysql-connector nemá veřejné API pro zrušení celého poolu, proto best-effort
    přes interní _remove_connections(); MySQL serveru tím ubudou „Aborted
    connection" hlášky při zastavení služby. Případné selhání se polkne, aby
    nikdy nezdrželo ani neshodilo shutdown."""
    global DB_POOL
    if DB_POOL is None:
        return
    try:
        DB_POOL._remove_connections()
    except Exception:
        pass
    DB_POOL = None

def ziskej_osobni_auto_odhlaseni(user_id):
    conn = get_db_connection()
    if not conn: return None
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_odhlaseni_minuty FROM user WHERE iduser = %s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_email_narozeniny(user_id):
    """True = uživatel chce narozeninové přání e-mailem (výchozí i při nedostupné DB)."""
    conn = get_db_connection()
    if not conn: return True
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT email_narozeniny FROM user WHERE iduser = %s", (user_id,))
        row = cursor.fetchone()
        return bool(row[0]) if row and row[0] is not None else True
    except Exception:
        return True
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def uloz_email_narozeniny(user_id, zapnuto):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE user SET email_narozeniny = %s WHERE iduser = %s", (1 if zapnuto else 0, user_id))
        conn.commit()
        _CACHE_UZIVATELE['ts'] = 0.0  # ať to background job přání vidí hned, ne až po TTL
        return True
    except Exception as e:
        print(f"Chyba DB email_narozeniny: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def uloz_osobni_auto_odhlaseni(user_id, minuty):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor()
        val = int(minuty) if minuty is not None else None
        cursor.execute("UPDATE user SET auto_odhlaseni_minuty = %s WHERE iduser = %s", (val, user_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"Chyba DB auto_odhlaseni: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def _vycisti_admin_prava(cursor):
    """Práva 'Administrace portálu' smí mít jen skrytý admin (iduser=1).

    Běží při každém startu — po nahození na ostrý server tedy zmizí i vazby
    založené dřív (u uživatelů, rolí i oddělení). Adminovi se práva doplní
    napřímo, aby nezávisel na roli 'Administrátor'.
    """
    prava = sorted(intranet_prava.ADMIN_ONLY_PRAVA)
    if not prava:
        return
    try:
        misto = ",".join(["%s"] * len(prava))
        cursor.execute(f"SELECT idprivileges FROM privileges WHERE name IN ({misto})", tuple(prava))
        ids = [r[0] for r in cursor.fetchall()]
        if not ids:
            return
        id_misto = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"DELETE FROM user_To_privileges WHERE privileges_idprivileges IN ({id_misto}) AND user_iduser <> %s",
            tuple(ids) + (SKRYTY_ADMIN_ID,))
        cursor.execute(
            f"DELETE FROM jobPosition_To_privileges WHERE privileges_idprivileges IN ({id_misto})",
            tuple(ids))
        cursor.execute(
            f"DELETE FROM department_To_privileges WHERE privileges_idprivileges IN ({id_misto})",
            tuple(ids))
        cursor.execute("SELECT 1 FROM user WHERE iduser=%s", (SKRYTY_ADMIN_ID,))
        if cursor.fetchone():
            for pid in ids:
                cursor.execute(
                    "INSERT IGNORE INTO user_To_privileges (user_iduser, privileges_idprivileges) VALUES (%s, %s)",
                    (SKRYTY_ADMIN_ID, pid))
        vymazat_cache_prav()
    except Exception as e:
        print(f"Chyba čištění admin práv: {e}")

# ========================================================
# INICIALIZACE DB (VČETNĚ NOVÝCH RELAČNÍCH TABULEK)
# ========================================================
def inicializace_db():
    global DB_ZAKLAD_INICIALIZOVAN
    if DB_ZAKLAD_INICIALIZOVAN: return

    cfg = nacti_mysql()
    if not cfg.get("enabled"): return

    # Prvotní vytvoření DB bez poolu
    conn_init = None
    cursor_init = None
    try:
        conn_init = mysql.connector.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["pass"])
        cursor_init = conn_init.cursor(buffered=True)
        cursor_init.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['db']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    except Exception:
        pass
    finally:
        if cursor_init: cursor_init.close()
        if conn_init: conn_init.close()

    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)

        cursor.execute("CREATE TABLE IF NOT EXISTS department (iddepartment INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(45) UNIQUE, shortname VARCHAR(45)) ENGINE=InnoDB")
        try:
            cursor.execute("ALTER TABLE department ADD COLUMN is_majitele BOOLEAN DEFAULT 0")
            conn.commit()
        except Exception: pass

        cursor.execute("CREATE TABLE IF NOT EXISTS user (iduser INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(100) UNIQUE, name VARCHAR(45), surname VARCHAR(45), password_hash VARCHAR(255), is_active BOOLEAN DEFAULT 1, base_vacation DECIMAL(5,2) DEFAULT 160.00, carried_over_vacation DECIMAL(5,2) DEFAULT 0.00) ENGINE=InnoDB")

        for col, col_def in [
            ("base_vacation", "DECIMAL(5,2) DEFAULT 160.00"),
            ("carried_over_vacation", "DECIMAL(5,2) DEFAULT 0.00"),
            ("email_nova_zadost", "BOOLEAN DEFAULT 1"),
            ("email_vyrizeni_zadosti", "BOOLEAN DEFAULT 1"),
            ("email_narozeniny", "BOOLEAN DEFAULT 1"),
            ("osobni_cislo", "INT DEFAULT NULL"),
            ("datum_narozeni", "DATE DEFAULT NULL"),
            ("realny_zustatek_dovolene", "DECIMAL(6,2) DEFAULT NULL"),
            ("realny_zustatek_dovolene_datum", "DATE DEFAULT NULL"),
            ("auto_odhlaseni_minuty", "INT DEFAULT NULL"),
            # Dvoufaktorové ověření (TOTP) — viz intranet_2fa.py
            ("totp_secret", "VARCHAR(64) DEFAULT NULL"),
            ("totp_aktivni", "BOOLEAN DEFAULT 0"),
            ("totp_zalozni_kody", "TEXT DEFAULT NULL"),
            ("pobocka", "VARCHAR(3) DEFAULT NULL"),
            # Vynucená změna hesla — 1 u nově založených účtů a po admin resetu.
            # Stávající uživatelé zůstávají na 0 (DEFAULT), takže je to neotravuje.
            ("zmena_hesla_nutna", "BOOLEAN DEFAULT 0"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE user ADD COLUMN {col} {col_def}")
                conn.commit()
            except Exception: pass

        # Datum poslední změny hesla — pro připomínku po HESLO_MAX_DNI.
        # Vlastní blok (ne v seznamu výše), protože po přidání potřebuje jednorázový
        # backfill: stávajícím uživatelům se počítá od nasazení, jinak by den poté
        # dostali hlášku všichni najednou.
        cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE "
                       "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user' AND COLUMN_NAME='heslo_zmeneno'")
        if cursor.fetchone()[0] == 0:
            cursor.execute("ALTER TABLE user ADD COLUMN heslo_zmeneno DATETIME DEFAULT NULL")
            cursor.execute("UPDATE user SET heslo_zmeneno = NOW()")
            conn.commit()

        # Zapamatovaná ("důvěryhodná") zařízení pro 2FA — na nich se po
        # úspěšném ověření nevyžaduje TOTP kód, dokud token nevyexpiruje.
        # V DB je jen SHA-256 hash tokenu; samotný token drží prohlížeč.
        cursor.execute("""CREATE TABLE IF NOT EXISTS totp_duveryhodne_zarizeni (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            token_hash VARCHAR(64) NOT NULL,
            popis VARCHAR(255) DEFAULT NULL,
            vytvoreno DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expiruje DATETIME NOT NULL,
            UNIQUE KEY uq_totp_duv_hash (token_hash),
            KEY idx_totp_duv_user (user_id),
            FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        conn.commit()

        cursor.execute("CREATE TABLE IF NOT EXISTS priznak (id INT AUTO_INCREMENT PRIMARY KEY, nazev VARCHAR(100) NOT NULL UNIQUE, barva VARCHAR(20) NOT NULL DEFAULT '#6366f1') ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        try:
            cursor.execute("ALTER TABLE user ADD COLUMN priznak_id INT DEFAULT NULL")
            cursor.execute("ALTER TABLE user ADD CONSTRAINT fk_user_priznak FOREIGN KEY (priznak_id) REFERENCES priznak(id) ON DELETE SET NULL")
            conn.commit()
        except Exception: pass

        cursor.execute("CREATE TABLE IF NOT EXISTS spolecnost (id INT AUTO_INCREMENT PRIMARY KEY, nazev VARCHAR(100) NOT NULL UNIQUE) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

        cursor.execute("SELECT COUNT(*) FROM spolecnost")
        if cursor.fetchone()[0] == 0:
            cursor.executemany("INSERT IGNORE INTO spolecnost (nazev) VALUES (%s)", [('JIP Východočeská',), ('JIP Východočeská a.s.',), ('JIP Majetková',)])

        cursor.execute("CREATE TABLE IF NOT EXISTS user_manager (user_id INT, manager_id INT, PRIMARY KEY (user_id, manager_id), FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE, FOREIGN KEY (manager_id) REFERENCES user(iduser) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS user_spolecnost (user_id INT, spolecnost_id INT, PRIMARY KEY (user_id, spolecnost_id), FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE, FOREIGN KEY (spolecnost_id) REFERENCES spolecnost(id) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS user_watched_users (user_id INT, watched_user_id INT, PRIMARY KEY (user_id, watched_user_id), FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE, FOREIGN KEY (watched_user_id) REFERENCES user(iduser) ON DELETE CASCADE) ENGINE=InnoDB")

        cursor.execute("CREATE TABLE IF NOT EXISTS department_To_user (department_iddepartment INT, user_iduser INT, PRIMARY KEY(department_iddepartment, user_iduser), FOREIGN KEY(department_iddepartment) REFERENCES department(iddepartment) ON DELETE CASCADE, FOREIGN KEY(user_iduser) REFERENCES user(iduser) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS jobPosition (idjobPosition INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(45) UNIQUE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS user_To_jobPosition (user_iduser INT, jobPosition_idjobPosition INT, PRIMARY KEY(user_iduser, jobPosition_idjobPosition), FOREIGN KEY(user_iduser) REFERENCES user(iduser) ON DELETE CASCADE, FOREIGN KEY(jobPosition_idjobPosition) REFERENCES jobPosition(idjobPosition) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS privileges (idprivileges INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(45) UNIQUE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS jobPosition_To_privileges (jobPosition_idjobPosition INT, privileges_idprivileges INT, PRIMARY KEY(jobPosition_idjobPosition, privileges_idprivileges), FOREIGN KEY(jobPosition_idjobPosition) REFERENCES jobPosition(idjobPosition) ON DELETE CASCADE, FOREIGN KEY(privileges_idprivileges) REFERENCES privileges(idprivileges) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS department_To_privileges (department_iddepartment INT, privileges_idprivileges INT, PRIMARY KEY(department_iddepartment, privileges_idprivileges), FOREIGN KEY(department_iddepartment) REFERENCES department(iddepartment) ON DELETE CASCADE, FOREIGN KEY(privileges_idprivileges) REFERENCES privileges(idprivileges) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS user_To_privileges (user_iduser INT, privileges_idprivileges INT, PRIMARY KEY(user_iduser, privileges_idprivileges), FOREIGN KEY(user_iduser) REFERENCES user(iduser) ON DELETE CASCADE, FOREIGN KEY(privileges_idprivileges) REFERENCES privileges(idprivileges) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS leaveStatus (idleaveStatus INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(45) NOT NULL UNIQUE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS typeOfLeave (idtypeOfLeave INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(45) NOT NULL UNIQUE) ENGINE=InnoDB")
        try: cursor.execute("ALTER TABLE leaveStatus ADD UNIQUE KEY uq_leaveStatus_name (name)")
        except Exception: pass
        try: cursor.execute("ALTER TABLE typeOfLeave ADD UNIQUE KEY uq_typeOfLeave_name (name)")
        except Exception: pass
        cursor.execute("CREATE TABLE IF NOT EXISTS leaveRequest (idleaveRequest INT AUTO_INCREMENT PRIMARY KEY, addedTime DATETIME, user_iduser INT, `from` DATE, `to` DATE, sumaHours DECIMAL(5,2), created_at DATETIME, approved_at DATETIME, typeOfLeave_idtypeOfLeave INT, leaveStatus_idleaveStatus INT, approved_by_iduser INT, rejection_reason TEXT, FOREIGN KEY(user_iduser) REFERENCES user(iduser) ON DELETE CASCADE, FOREIGN KEY(typeOfLeave_idtypeOfLeave) REFERENCES typeOfLeave(idtypeOfLeave) ON DELETE RESTRICT, FOREIGN KEY(leaveStatus_idleaveStatus) REFERENCES leaveStatus(idleaveStatus) ON DELETE RESTRICT, FOREIGN KEY(approved_by_iduser) REFERENCES user(iduser) ON DELETE SET NULL) ENGINE=InnoDB")
        try: cursor.execute("ALTER TABLE leaveRequest ADD COLUMN suma_hodin INT NOT NULL DEFAULT 0")
        except Exception: pass
        try: cursor.execute("ALTER TABLE leaveRequest ADD COLUMN suma_minut TINYINT UNSIGNED NOT NULL DEFAULT 0")
        except Exception: pass
        try: cursor.execute("UPDATE leaveRequest SET suma_hodin = FLOOR(sumaHours), suma_minut = ROUND((sumaHours - FLOOR(sumaHours)) * 60) WHERE suma_hodin = 0 AND suma_minut = 0 AND sumaHours > 0")
        except Exception: pass
        try: cursor.execute("ALTER TABLE leaveRequest ADD COLUMN cas_od TIME DEFAULT NULL")
        except Exception: pass
        try: cursor.execute("ALTER TABLE leaveRequest ADD COLUMN cas_do TIME DEFAULT NULL")
        except Exception: pass
        # Žádost zaměstnance o storno již schválené absence (stav řádku se nemění, dokud vedoucí storno neschválí)
        try: cursor.execute("ALTER TABLE leaveRequest ADD COLUMN storno_req_at DATETIME DEFAULT NULL")
        except Exception: pass
        try: cursor.execute("ALTER TABLE leaveRequest ADD COLUMN storno_req_reason TEXT")
        except Exception: pass
        cursor.execute("""CREATE TABLE IF NOT EXISTS overtimeRequest (
            idovertimeRequest INT AUTO_INCREMENT PRIMARY KEY,
            user_iduser INT NOT NULL,
            datum_od DATE NOT NULL,
            datum_do DATE NOT NULL,
            cas_od TIME NOT NULL,
            cas_do TIME NOT NULL,
            sumaHours DECIMAL(5,2) NOT NULL,
            duvod TEXT,
            created_at DATETIME DEFAULT NOW(),
            leaveStatus_idleaveStatus INT NOT NULL DEFAULT 2,
            storno_by_iduser INT,
            storno_at DATETIME,
            storno_reason TEXT,
            FOREIGN KEY(user_iduser) REFERENCES user(iduser) ON DELETE CASCADE,
            FOREIGN KEY(leaveStatus_idleaveStatus) REFERENCES leaveStatus(idleaveStatus) ON DELETE RESTRICT,
            FOREIGN KEY(storno_by_iduser) REFERENCES user(iduser) ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        try: cursor.execute("ALTER TABLE overtimeRequest ADD COLUMN suma_hodin INT NOT NULL DEFAULT 0")
        except Exception: pass
        try: cursor.execute("ALTER TABLE overtimeRequest ADD COLUMN suma_minut TINYINT UNSIGNED NOT NULL DEFAULT 0")
        except Exception: pass
        try: cursor.execute("UPDATE overtimeRequest SET suma_hodin = FLOOR(sumaHours), suma_minut = ROUND((sumaHours - FLOOR(sumaHours)) * 60) WHERE suma_hodin = 0 AND suma_minut = 0 AND sumaHours > 0")
        except Exception: pass
        cursor.execute("CREATE TABLE IF NOT EXISTS vysledky_kvizu (id INT AUTO_INCREMENT PRIMARY KEY, user_iduser INT, stav_testu VARCHAR(50), uspesnost VARCHAR(20), body VARCHAR(20), doba_trvani VARCHAR(50), datum TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_iduser) REFERENCES user(iduser) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS zaznamy_odpovedi (id INT AUTO_INCREMENT PRIMARY KEY, vysledek_id INT, poradi INT, otazka TEXT, tvoje_volba TEXT, spravna_odpoved TEXT, hodnoceni VARCHAR(50), FOREIGN KEY (vysledek_id) REFERENCES vysledky_kvizu(id) ON DELETE CASCADE) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS veletrh_smlouvy (id INT AUTO_INCREMENT PRIMARY KEY, res_id VARCHAR(100), dodavatel VARCHAR(255), ico VARCHAR(50), cesta_k_souboru VARCHAR(500), vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB")
        cursor.execute("CREATE TABLE IF NOT EXISTS dodavatel_firma (ico VARCHAR(50) PRIMARY KEY, nazev VARCHAR(255) NOT NULL) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        try: cursor.execute("INSERT IGNORE INTO dodavatel_firma (ico, nazev) SELECT DISTINCT ico, dodavatel FROM veletrh_smlouvy WHERE ico IS NOT NULL AND ico != '' AND dodavatel IS NOT NULL AND dodavatel != ''")
        except Exception: pass
        # ── Výkonnostní indexy ─────────────────────────────────────────────
        # Každý CREATE INDEX je v samostatném try/except — chyba "Duplicate key"
        # při opakovaném startu serveru je normální a nevadí.
        _perf_indexes = [
            # Práva uživatelů (3 UNION větve v ziskej_prava_uzivatele)
            "CREATE INDEX idx_utp_user   ON user_To_privileges(user_iduser)",
            "CREATE INDEX idx_utj_user   ON user_To_jobPosition(user_iduser)",
            "CREATE INDEX idx_dtu_user   ON department_To_user(user_iduser)",
            "CREATE INDEX idx_jtp_job    ON jobPosition_To_privileges(jobPosition_idjobPosition)",
            "CREATE INDEX idx_dtp_dept   ON department_To_privileges(department_iddepartment)",
            # Žádosti o volno (filtrování podle uživatele a stavu)
            "CREATE INDEX idx_lr_user    ON leaveRequest(user_iduser)",
            "CREATE INDEX idx_lr_status  ON leaveRequest(leaveStatus_idleaveStatus)",
            "CREATE INDEX idx_lr_dates   ON leaveRequest(`from`, `to`)",
            # Přesčasy
            "CREATE INDEX idx_ot_user    ON overtimeRequest(user_iduser)",
            "CREATE INDEX idx_ot_status  ON overtimeRequest(leaveStatus_idleaveStatus)",
        ]
        for idx_sql in _perf_indexes:
            try:
                cursor.execute(idx_sql)
                conn.commit()
            except Exception:
                pass  # index již existuje nebo tabulka nemá sloupec — nevadí

        for s in ['Čeká na schválení', 'Schváleno', 'Zamítnuto', 'Stornováno']:
            cursor.execute("SELECT idleaveStatus FROM leaveStatus WHERE name=%s", (s,))
            if not cursor.fetchone(): cursor.execute("INSERT INTO leaveStatus (name) VALUES (%s)", (s,))

        for t in ['Dovolená', 'Nemoc', 'Ošetřovné (OČR)', 'Lékař', 'Neplacené volno', 'Homeoffice', 'Paragraf']:
            cursor.execute("SELECT idtypeOfLeave FROM typeOfLeave WHERE name=%s", (t,))
            if not cursor.fetchone(): cursor.execute("INSERT INTO typeOfLeave (name) VALUES (%s)", (t,))

        cursor.execute("SELECT COUNT(*) FROM user")
        if cursor.fetchone()[0] == 0:
            for p in ['vse', 'kviz', 'dochazka_zadosti', 'dochazka_admin', 'dochazka_schvalovat_sebe', 'dochazka_mazani', 'dochazka_export', 'dochazka_email', 'vystup_osobni', 'vystup_vse', 'uzivatele', 'mysql', 'dlazdice', 'slozky_vse', 'kalendar_vse', 'dlazdice_dochazka_zaklad', 'tisk_vse', 'tisk_odd_vse', 'veletrh_admin', 'veletrh_uzivatel', 'veletrh_pristup', 'nakup_uzivatel', 'nakup_schvalit', 'faktury_seznam_schvalit', 'admin_logy', 'admin_server']:
                cursor.execute("INSERT IGNORE INTO privileges (name) VALUES (%s)", (p,))

            cursor.execute("INSERT IGNORE INTO jobPosition (name) VALUES ('Administrátor')")
            cursor.execute("SELECT idjobPosition FROM jobPosition WHERE name='Administrátor'")
            role_row = cursor.fetchone()
            if role_row:
                admin_role_id = role_row[0]
                cursor.execute("SELECT idprivileges FROM privileges")
                for priv_id in cursor.fetchall():
                    cursor.execute("INSERT IGNORE INTO jobPosition_To_privileges (jobPosition_idjobPosition, privileges_idprivileges) VALUES (%s, %s)", (admin_role_id, priv_id[0]))

                # Náhodné iniciální heslo (ne „1234") — vypíše se jednorázově do
                # konzole/journalu. Operátor si ho IHNED po přihlášení změní.
                _init_admin_heslo = os.environ.get('JIPKA_INIT_ADMIN_HESLO') or _secrets.token_urlsafe(12)
                cursor.execute("INSERT INTO user (email, name, surname, password_hash, base_vacation, carried_over_vacation) VALUES (%s, %s, %s, %s, %s, %s)", ("admin@admin.cz", "Hlavní", "Administrátor", hash_heslo(_init_admin_heslo), 160.0, 0.0))
                admin_user_id = cursor.lastrowid
                print("\n" + "=" * 72)
                print("  PRVNÍ SPUŠTĚNÍ — vytvořen výchozí administrátor:")
                print("     e-mail: admin@admin.cz")
                print(f"     heslo:  {_init_admin_heslo}")
                print("  >>> HESLO SI IHNED PO PŘIHLÁŠENÍ ZMĚŇTE. Tento výpis je jen jednou. <<<")
                print("=" * 72 + "\n")
                cursor.execute("INSERT INTO user_To_jobPosition (user_iduser, jobPosition_idjobPosition) VALUES (%s, %s)", (admin_user_id, admin_role_id))

        _vycisti_admin_prava(cursor)

        conn.commit()
        DB_ZAKLAD_INICIALIZOVAN = True
    except Exception as e:
        print(f"Chyba DB inicializace: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def _rozdel_ident(ident):
    """'JV323514' -> ('JV', 323514). Bez písmen nebo bez číslic -> None."""
    m = re.match(r'^([^\W\d_]+)(\d{1,9})$', str(ident or '').strip(), re.UNICODE)
    if not m:
        return None
    return m.group(1), int(m.group(2))

def email_z_osobniho_cisla(ident):
    """Přihlašovací jméno 'příznak + osobní číslo' -> e-mail uživatele, jinak None.

    Osobní číslo = user.iduser, příznak = priznak.nazev (porovnání bez diakritiky
    a velikosti písmen). Uživatel bez příznaku se touto cestou přihlásit nemůže.
    """
    rozdeleno = _rozdel_ident(ident)
    if not rozdeleno:
        return None
    priznak_zadany, user_id = rozdeleno
    conn = get_db_connection()
    if not conn:
        return None
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            "SELECT u.email, pr.nazev AS priznak FROM user u "
            "LEFT JOIN priznak pr ON u.priznak_id = pr.id WHERE u.iduser=%s",
            (user_id,))
        row = cursor.fetchone()
        if not row or not row.get('priznak'):
            return None
        if _norm_text(priznak_zadany) != _norm_text(row['priznak']):
            return None
        return row['email']
    except Exception as e:
        print(f"[email_z_osobniho_cisla] {e}")
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def najdi_uzivatele_dle_emailu(email):
    """Pro OIDC: heslo už ověřil IdP, hledáme jen účet v intranetu.
    Vrací (user_id, "Jméno Příjmení", chyba) — stejný tvar jako overit_prihlaseni.

    Bez auto-provisioningu: neznámý e-mail = odmítnutí. Účty zakládá admin,
    protože z nich visí práva, útvary a schvalovací role."""
    email = (email or '').strip().lower()
    if not email:
        return None, None, "Chybný e-mail."

    if not nacti_mysql().get("enabled"):
        return None, None, "Systém není napojen na databázi."

    conn = get_db_connection()
    if not conn:
        return None, None, "Chyba databáze (Zkontrolujte nastavení MySQL)"

    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT iduser, name, surname, is_active FROM user WHERE email = %s", (email,))
        u = cursor.fetchone()
        if not u:
            return None, None, "Účet v intranetu neexistuje."
        if not u['is_active']:
            return None, None, "Účet byl deaktivován administrátorem!"
        return u['iduser'], f"{u['name']} {u['surname']}", "OK"
    except Exception as e:
        return None, None, str(e)
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def overit_prihlaseni(email, heslo):
    # K-6: Rate limiting platí pro VŠECHNY (včetně DB adminů). Vyňat je jen
    # nouzový admin z env (break-glass), aby ho útočník nemohl zamknout.
    if not _je_nouzovy_admin_email(email):
        zamcen, zbyvaji = _zkontroluj_rate_limit(email)
        if zamcen:
            return None, None, f'ZAMCEN:{zbyvaji}'

    cfg = nacti_mysql()
    if not cfg.get("enabled"):
        # K-2: Nouzový admin z env proměnných
        nouzovy = _overit_nouzovy_admin(email, heslo)
        if nouzovy:
            _zaznamenej_uspech(email)
            return nouzovy
        _zaznamenej_neuspech(email)
        return None, None, "Systém není napojen na databázi."

    inicializace_db()

    conn = get_db_connection()
    if not conn:
        # K-2: Nouzový admin z env proměnných
        nouzovy = _overit_nouzovy_admin(email, heslo)
        if nouzovy:
            _zaznamenej_uspech(email)
            return nouzovy
        _zaznamenej_neuspech(email)
        return None, None, "Chyba databáze (Zkontrolujte nastavení MySQL)"

    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT iduser, name, surname, password_hash, is_active FROM user WHERE email = %s", (email,))
        u = cursor.fetchone()

        if u and overit_heslo(heslo, u['password_hash']):
            if not u['is_active']:
                _zaznamenej_neuspech(email)
                return None, None, "Účet byl deaktivován administrátorem!"
            user_id = u['iduser']
            ulozeny_hash = u['password_hash']
            # K-5: Migrace SHA-256 → bcrypt při přihlášení
            if _BCRYPT_DOSTUPNY and not _je_bcrypt_hash(ulozeny_hash):
                novy_hash = hash_heslo(heslo)
                try:
                    conn2 = get_db_connection()
                    if conn2:
                        c2 = conn2.cursor()
                        c2.execute("UPDATE user SET password_hash=%s WHERE iduser=%s", (novy_hash, user_id))
                        conn2.commit()
                        c2.close()
                        conn2.close()
                except Exception:
                    pass
            _zaznamenej_uspech(email)
            return user_id, f"{u['name']} {u['surname']}", "OK"
        _zaznamenej_neuspech(email)
        return None, None, "Chybný e-mail nebo heslo!"
    except Exception as e:
        return None, None, str(e)
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def vymazat_cache_prav(user_id=None):
    """Invaliduje cache práv – pro konkrétního uživatele nebo celou cache."""
    global _CACHE_PRAVA
    if user_id is None:
        _CACHE_PRAVA = {}
    else:
        _CACHE_PRAVA.pop(user_id, None)

def heslo_je_silne(heslo: str) -> bool:
    """Min. 8 znaků, velké i malé písmeno a číslice. Jediný zdroj pravdy pro UI i backend."""
    return bool(re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', heslo or ""))

HESLO_MAX_DNI = 30  # po kolika dnech bez změny hesla upozornit (jen připomínka, nic se nevynucuje)

def stav_hesla(user_id):
    """(nutna_zmena: bool, dni_od_zmeny: int|None). Jeden dotaz pro bránu i připomínku."""
    if user_id == 999999: return False, None
    conn = get_db_connection()
    if not conn: return False, None
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT zmena_hesla_nutna, DATEDIFF(NOW(), heslo_zmeneno) FROM user WHERE iduser=%s", (user_id,))
        row = cursor.fetchone()
        if not row: return False, None
        return bool(row[0]), (int(row[1]) if row[1] is not None else None)
    except Exception as e:
        print(f"[stav_hesla] {e}")
        return False, None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def nastav_heslo_a_zrus_priznak(user_id, nove_heslo) -> bool:
    """Uloží nové heslo a shodí příznak vynucené změny. Vrátí True/False."""
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE user SET password_hash=%s, zmena_hesla_nutna=0, heslo_zmeneno=NOW() WHERE iduser=%s", (hash_heslo(nove_heslo), user_id))
        conn.commit()
        _CACHE_UZIVATELE['ts'] = 0.0
        return cursor.rowcount > 0
    except Exception as e:
        print(f"[nastav_heslo_a_zrus_priznak] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_prava_uzivatele(user_id):
    if user_id == 999999: return ['vse']

    # Kontrola cache
    cached = _CACHE_PRAVA.get(user_id)
    if cached is not None:
        prava_list, ts = cached
        if time.time() - ts < _CACHE_PRAVA_TTL:
            return prava_list

    inicializace_db()
    conn = get_db_connection()
    if not conn: return []

    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        # 3 jednoduché UNION větve místo 5 LEFT JOINů s OR — každá větev projde
        # jen svůj index (user_iduser), žádný full-scan tabulky privileges.
        query = """
            SELECT p.name
            FROM user_To_privileges utp
            JOIN privileges p ON utp.privileges_idprivileges = p.idprivileges
            WHERE utp.user_iduser = %s
            UNION
            SELECT p.name
            FROM user_To_jobPosition utj
            JOIN jobPosition_To_privileges jtp ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
            JOIN privileges p ON jtp.privileges_idprivileges = p.idprivileges
            WHERE utj.user_iduser = %s
            UNION
            SELECT p.name
            FROM department_To_user dtu
            JOIN department_To_privileges dtp ON dtu.department_iddepartment = dtp.department_iddepartment
            JOIN privileges p ON dtp.privileges_idprivileges = p.idprivileges
            WHERE dtu.user_iduser = %s
        """
        cursor.execute(query, (user_id, user_id, user_id))
        prava = [row[0] for row in cursor.fetchall()]
        _CACHE_PRAVA[user_id] = (prava, time.time())
        return prava
    except Exception:
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_uzivatele_s_pravem(*prava, pouze_jmena=False):
    """Vrátí uživatele, kteří mají alespoň jedno z požadovaných práv (přes přímé, role nebo oddělení).
    pouze_jmena=True → list jmen; False → {id: jmeno} dict.
    """
    if not prava:
        return [] if pouze_jmena else {}
    inicializace_db()
    conn = get_db_connection()
    if not conn:
        return [] if pouze_jmena else {}

    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        placeholders = ','.join(['%s'] * len(prava))
        prava_tuple = tuple(prava) * 3  # 3× pro tři podmínky EXISTS
        query = f"""
            SELECT DISTINCT u.iduser, u.name, u.surname, CONCAT(u.name, ' ', u.surname) AS jmeno_cele
            FROM user u
            WHERE u.is_active = 1
              AND u.iduser <> {SKRYTY_ADMIN_ID}
              AND (
                EXISTS (
                    SELECT 1 FROM user_To_privileges utp
                    JOIN privileges p ON utp.privileges_idprivileges = p.idprivileges
                    WHERE utp.user_iduser = u.iduser AND p.name IN ({placeholders})
                )
                OR EXISTS (
                    SELECT 1 FROM user_To_jobPosition utj
                    JOIN jobPosition_To_privileges jtp ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
                    JOIN privileges p ON jtp.privileges_idprivileges = p.idprivileges
                    WHERE utj.user_iduser = u.iduser AND p.name IN ({placeholders})
                )
                OR EXISTS (
                    SELECT 1 FROM department_To_user dtu
                    JOIN department_To_privileges dtp ON dtu.department_iddepartment = dtp.department_iddepartment
                    JOIN privileges p ON dtp.privileges_idprivileges = p.idprivileges
                    WHERE dtu.user_iduser = u.iduser AND p.name IN ({placeholders})
                )
              )
            ORDER BY u.name, u.surname
        """
        cursor.execute(query, prava_tuple)
        rows = cursor.fetchall()
        # row: (iduser, name, surname, jmeno_cele) — index 3
        if pouze_jmena:
            return [row[3] for row in rows if row[3] and str(row[3]).strip()]
        return {row[0]: row[3] for row in rows if row[3] and str(row[3]).strip()}
    except Exception as e:
        print(f"Chyba ziskej_uzivatele_s_pravem: {e}")
        return [] if pouze_jmena else {}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_matici_prav() -> dict:
    """Kompletní podklad pro matici práv — 6 bulk dotazů, žádné N+1.

    Vrací {'uzivatele': [{id, jmeno, email, aktivni, oddeleni:[], role:[]}],
    'primo': {uid: set(prava)}, 'role_prava': {role: set}, 'odd_prava': {oddeleni: set}}.
    Skrytý admin (SKRYTY_ADMIN_ID) ani servisní admin@admin.cz se do matice
    nikdy nedostanou — stejná výluka jako v ziskej_uzivatele_s_pravem.
    """
    prazdno = {'uzivatele': [], 'primo': {}, 'role_prava': {}, 'odd_prava': {}}
    inicializace_db()
    conn = get_db_connection()
    if not conn:
        return prazdno
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute(f"""
            SELECT u.iduser, CONCAT(u.name, ' ', u.surname), u.email, u.is_active
            FROM user u
            WHERE u.iduser <> {SKRYTY_ADMIN_ID} AND u.email <> 'admin@admin.cz'
            ORDER BY u.surname, u.name
        """)
        uzivatele = [
            {'id': r[0], 'jmeno': (r[1] or '').strip(), 'email': r[2] or '',
             'aktivni': bool(r[3]), 'oddeleni': [], 'role': []}
            for r in cursor.fetchall()
        ]
        idx = {u['id']: u for u in uzivatele}

        cursor.execute("""
            SELECT dtu.user_iduser, d.name
            FROM department_To_user dtu
            JOIN department d ON d.iddepartment = dtu.department_iddepartment
        """)
        for uid, nazev in cursor.fetchall():
            if uid in idx and nazev:
                idx[uid]['oddeleni'].append(nazev)

        cursor.execute("""
            SELECT utj.user_iduser, jp.name
            FROM user_To_jobPosition utj
            JOIN jobPosition jp ON jp.idjobPosition = utj.jobPosition_idjobPosition
        """)
        for uid, nazev in cursor.fetchall():
            if uid in idx and nazev:
                idx[uid]['role'].append(nazev)

        primo = {}
        cursor.execute("""
            SELECT utp.user_iduser, p.name
            FROM user_To_privileges utp
            JOIN privileges p ON p.idprivileges = utp.privileges_idprivileges
        """)
        for uid, pravo in cursor.fetchall():
            if uid in idx and pravo:
                primo.setdefault(uid, set()).add(pravo)

        role_prava = {}
        cursor.execute("""
            SELECT jp.name, p.name
            FROM jobPosition_To_privileges jtp
            JOIN jobPosition jp ON jp.idjobPosition = jtp.jobPosition_idjobPosition
            JOIN privileges p ON p.idprivileges = jtp.privileges_idprivileges
        """)
        for role, pravo in cursor.fetchall():
            if role and pravo:
                role_prava.setdefault(role, set()).add(pravo)

        odd_prava = {}
        cursor.execute("""
            SELECT d.name, p.name
            FROM department_To_privileges dtp
            JOIN department d ON d.iddepartment = dtp.department_iddepartment
            JOIN privileges p ON p.idprivileges = dtp.privileges_idprivileges
        """)
        for odd, pravo in cursor.fetchall():
            if odd and pravo:
                odd_prava.setdefault(odd, set()).add(pravo)

        for u in uzivatele:
            u['oddeleni'].sort()
            u['role'].sort()
        return {'uzivatele': uzivatele, 'primo': primo,
                'role_prava': role_prava, 'odd_prava': odd_prava}
    except Exception as e:
        print(f"Chyba ziskej_matici_prav: {e}")
        return prazdno
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_uzivatele_pravo_detail(pravo):
    """Detailní přehled pro JEDNO právo: kdo jej má a JAKÝM kanálem.

    Na rozdíl od `ziskej_uzivatele_s_pravem` (jen aktivní, bez zdroje) vrací i
    NEAKTIVNÍ uživatele a u každého rozlišuje, odkud právo plyne — přiřazeno
    přímo (`user_To_privileges`), zděděno přes pracovní pozici
    (`jobPosition_To_privileges`) nebo zděděno z oddělení
    (`department_To_privileges`). Uživatel může figurovat ve více kanálech.

    Vrací list dictů seřazený podle jména:
        {'id', 'jmeno', 'email', 'aktivni', 'primo': bool,
         'pozice': [názvy pozic], 'oddeleni': [názvy oddělení]}
    Prázdný list, když právo neexistuje nebo jej nikdo nemá. Skrytý admin je vynechán.
    """
    inicializace_db()
    conn = get_db_connection()
    if not conn:
        return []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT idprivileges FROM privileges WHERE name = %s", (pravo,))
        r = cursor.fetchone()
        if not r:
            return []
        pid = r['idprivileges']

        uzivatele: dict = {}  # iduser → dict

        def _slot(row):
            uid = row['iduser']
            u = uzivatele.get(uid)
            if u is None:
                u = uzivatele[uid] = {
                    'id': uid,
                    'jmeno': f"{row['name'] or ''} {row['surname'] or ''}".strip(),
                    'email': row['email'] or '',
                    'aktivni': bool(row['is_active']),
                    'primo': False,
                    'pozice': [],
                    'oddeleni': [],
                }
            return u

        base_where = f"u.iduser <> {SKRYTY_ADMIN_ID} AND u.email <> 'admin@admin.cz'"

        # 1) Přiřazeno přímo uživateli
        cursor.execute(f"""
            SELECT u.iduser, u.name, u.surname, u.email, u.is_active
            FROM user_To_privileges utp
            JOIN user u ON u.iduser = utp.user_iduser
            WHERE utp.privileges_idprivileges = %s AND {base_where}
        """, (pid,))
        for row in cursor.fetchall():
            _slot(row)['primo'] = True

        # 2) Zděděno přes pracovní pozici
        cursor.execute(f"""
            SELECT u.iduser, u.name, u.surname, u.email, u.is_active, jp.name AS pozice
            FROM jobPosition_To_privileges jtp
            JOIN user_To_jobPosition utj ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
            JOIN jobPosition jp ON jp.idjobPosition = jtp.jobPosition_idjobPosition
            JOIN user u ON u.iduser = utj.user_iduser
            WHERE jtp.privileges_idprivileges = %s AND {base_where}
        """, (pid,))
        for row in cursor.fetchall():
            u = _slot(row)
            if row['pozice'] and row['pozice'] not in u['pozice']:
                u['pozice'].append(row['pozice'])

        # 3) Zděděno z oddělení
        cursor.execute(f"""
            SELECT u.iduser, u.name, u.surname, u.email, u.is_active, d.name AS oddeleni
            FROM department_To_privileges dtp
            JOIN department_To_user dtu ON dtu.department_iddepartment = dtp.department_iddepartment
            JOIN department d ON d.iddepartment = dtp.department_iddepartment
            JOIN user u ON u.iduser = dtu.user_iduser
            WHERE dtp.privileges_idprivileges = %s AND {base_where}
        """, (pid,))
        for row in cursor.fetchall():
            u = _slot(row)
            if row['oddeleni'] and row['oddeleni'] not in u['oddeleni']:
                u['oddeleni'].append(row['oddeleni'])

        return sorted(uzivatele.values(),
                      key=lambda x: (not x['aktivni'], x['jmeno'].lower()))
    except Exception as e:
        print(f"Chyba ziskej_uzivatele_pravo_detail: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_emaily_s_pravem(*prava):
    """E-maily AKTIVNÍCH uživatelů, kteří mají alespoň jedno z `prava` — počítáno z
    EFEKTIVNÍCH práv (osobní `user_To_privileges` + pozice `jobPosition_To_privileges`
    + ZDĚDĚNÁ z oddělení `department_To_privileges`), stejně jako přístup do modulů
    (`ziskej_prava_uzivatele`/`ziskej_uzivatele_s_pravem`).

    ⚠️ TOTO používat pro výběr příjemců rolových e-mailových notifikací — NIKDY
    `ziskej_vsechny_uzivatele()['prava']` (to je GROUP_CONCAT jen z user_To_privileges =
    pouze OSOBNÍ práva → komu je role office/správce/… ZDĚDĚNA z oddělení nebo pozice,
    tomu by e-maily nechodily). 'vse' (admin) je nutné předat explicitně, je-li žádán."""
    if not prava:
        return []
    inicializace_db()
    conn = get_db_connection()
    if not conn:
        return []
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        placeholders = ','.join(['%s'] * len(prava))
        prava_tuple = tuple(prava) * 3  # 3× pro tři podmínky EXISTS
        query = f"""
            SELECT DISTINCT u.email
            FROM user u
            WHERE u.is_active = 1 AND u.email IS NOT NULL
              AND u.iduser <> {SKRYTY_ADMIN_ID}
              AND (
                EXISTS (
                    SELECT 1 FROM user_To_privileges utp
                    JOIN privileges p ON utp.privileges_idprivileges = p.idprivileges
                    WHERE utp.user_iduser = u.iduser AND p.name IN ({placeholders})
                )
                OR EXISTS (
                    SELECT 1 FROM user_To_jobPosition utj
                    JOIN jobPosition_To_privileges jtp ON utj.jobPosition_idjobPosition = jtp.jobPosition_idjobPosition
                    JOIN privileges p ON jtp.privileges_idprivileges = p.idprivileges
                    WHERE utj.user_iduser = u.iduser AND p.name IN ({placeholders})
                )
                OR EXISTS (
                    SELECT 1 FROM department_To_user dtu
                    JOIN department_To_privileges dtp ON dtu.department_iddepartment = dtp.department_iddepartment
                    JOIN privileges p ON dtp.privileges_idprivileges = p.idprivileges
                    WHERE dtu.user_iduser = u.iduser AND p.name IN ({placeholders})
                )
              )
        """
        cursor.execute(query, prava_tuple)
        return [row[0] for row in cursor.fetchall() if row[0] and '@' in str(row[0])]
    except Exception as e:
        print(f"Chyba ziskej_emaily_s_pravem: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechna_oddeleni():
    if _CACHE_ODDELENI['data'] is not None and (time.time() - _CACHE_ODDELENI['ts']) < _CACHE_SPRAVA_TTL:
        return _CACHE_ODDELENI['data']
    inicializace_db()
    conn = get_db_connection()
    if not conn: return _CACHE_ODDELENI['data'] or {}
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        # Viz ziskej_vsechny_uzivatele: zvedáme limit GROUP_CONCAT, aby se
        # u oddělení s mnoha právy seznam práv neořízl na 1024 bajtech.
        cursor.execute("SET SESSION group_concat_max_len = 1000000")
        query = """
            SELECT d.name as d_name, d.is_majitele, GROUP_CONCAT(p.name SEPARATOR ',') as prava
            FROM department d
            LEFT JOIN department_To_privileges dtp ON d.iddepartment = dtp.department_iddepartment
            LEFT JOIN privileges p ON dtp.privileges_idprivileges = p.idprivileges
            GROUP BY d.iddepartment
            ORDER BY d.name ASC
        """
        cursor.execute(query)
        depts = cursor.fetchall()
        vysledek = {}
        for d in depts:
            vysledek[d['d_name']] = {
                'prava': d['prava'] if d['prava'] else "",
                'is_majitele': bool(d.get('is_majitele', 0))
            }
        _CACHE_ODDELENI['data'] = vysledek
        _CACHE_ODDELENI['ts'] = time.time()
        return vysledek
    except Exception:
        return _CACHE_ODDELENI['data'] or {}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def pridej_uprav_oddeleni(nazev, prava_str, is_majitele=False):
    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("INSERT INTO department (name, is_majitele) VALUES (%s, %s) ON DUPLICATE KEY UPDATE is_majitele=%s", (nazev, int(is_majitele), int(is_majitele)))
        cursor.execute("SELECT iddepartment FROM department WHERE name=%s", (nazev,))
        dept_row = cursor.fetchone()
        if not dept_row:
            return
        dept_id = dept_row[0]

        cursor.execute("DELETE FROM department_To_privileges WHERE department_iddepartment=%s", (dept_id,))
        prava_str = _bez_admin_prav(prava_str)
        for p in [p.strip() for p in prava_str.split(',') if p.strip()]:
            cursor.execute("INSERT IGNORE INTO privileges (name) VALUES (%s)", (p,))
            cursor.execute("SELECT idprivileges FROM privileges WHERE name=%s", (p,))
            priv_row = cursor.fetchone()
            if priv_row:
                cursor.execute("INSERT INTO department_To_privileges (department_iddepartment, privileges_idprivileges) VALUES (%s, %s)", (dept_id, priv_row[0]))
        conn.commit()
        vymazat_cache_prav()  # Změna oddělení ovlivní práva všech jeho členů
    except Exception as e:
        print(f"Chyba pridej_uprav_oddeleni: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def smaz_oddeleni(nazev):
    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("DELETE FROM department WHERE name=%s", (nazev,))
        conn.commit()
    except Exception: pass
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechny_role():
    if _CACHE_ROLE['data'] is not None and (time.time() - _CACHE_ROLE['ts']) < _CACHE_SPRAVA_TTL:
        return _CACHE_ROLE['data']
    conn = get_db_connection()
    if not conn: return _CACHE_ROLE['data'] or []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT name FROM jobPosition ORDER BY name ASC")
        roles = [r['name'] for r in cursor.fetchall()]
        _CACHE_ROLE['data'] = roles
        _CACHE_ROLE['ts'] = time.time()
        return roles
    except Exception: return _CACHE_ROLE['data'] or []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def pridej_roli(nazev):
    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("INSERT IGNORE INTO jobPosition (name) VALUES (%s)", (nazev,))
        conn.commit()
    except Exception: pass
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def smaz_roli(nazev):
    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("DELETE FROM jobPosition WHERE name=%s", (nazev,))
        conn.commit()
    except Exception: pass
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_typy_volna():
    if _CACHE_TYPY_VOLNA['data'] is not None and (time.time() - _CACHE_TYPY_VOLNA['ts']) < _CACHE_SPRAVA_TTL:
        return _CACHE_TYPY_VOLNA['data']
    conn = get_db_connection()
    if not conn: return _CACHE_TYPY_VOLNA['data'] or {}
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT idtypeOfLeave, name FROM typeOfLeave")
        res = {r['idtypeOfLeave']: r['name'] for r in cursor.fetchall()}
        _CACHE_TYPY_VOLNA['data'] = res
        _CACHE_TYPY_VOLNA['ts'] = time.time()
        return res
    except Exception: return _CACHE_TYPY_VOLNA['data'] or {}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def pridej_zadost_volno(user_id, od_data, do_data, hodin, minut, typ_id, cas_od=None, cas_do=None):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        suma_hours = round((hodin * 60 + minut) / 60, 4)
        cursor.execute("""INSERT INTO leaveRequest (user_iduser, `from`, `to`, sumaHours, suma_hodin, suma_minut, cas_od, cas_do, typeOfLeave_idtypeOfLeave, leaveStatus_idleaveStatus, created_at, addedTime)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, NOW(), NOW())""", (user_id, od_data, do_data, suma_hours, hodin, minut, cas_od or None, cas_do or None, typ_id))
        conn.commit()
        return True
    except Exception as e:
        print(e)
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_zadosti(user_id=None):
    # Krátkodobá cache pouze pro dotaz "všechny žádosti" (user_id=None)
    if user_id is None:
        now = time.time()
        if _CACHE_ZADOSTI_ALL['data'] is not None and now - _CACHE_ZADOSTI_ALL['ts'] < _CACHE_REFRESH_TTL:
            return _CACHE_ZADOSTI_ALL['data']

    conn = get_db_connection()
    if not conn: return []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        q = """SELECT lr.idleaveRequest, lr.user_iduser, lr.`from`, lr.`to`, lr.sumaHours, lr.suma_hodin, lr.suma_minut, lr.cas_od, lr.cas_do, lr.created_at, lr.approved_at, lr.rejection_reason,
                      lr.storno_req_at, lr.storno_req_reason,
                      t.name as typ, lr.typeOfLeave_idtypeOfLeave as typ_id, s.name as stav, lr.leaveStatus_idleaveStatus as stav_id,
                      u.name as u_jmeno, u.surname as u_prijmeni,
                      a.name as a_jmeno, a.surname as a_prijmeni,
                      GROUP_CONCAT(d.name SEPARATOR ', ') as oddeleni
               FROM leaveRequest lr
               JOIN typeOfLeave t ON lr.typeOfLeave_idtypeOfLeave = t.idtypeOfLeave
               JOIN leaveStatus s ON lr.leaveStatus_idleaveStatus = s.idleaveStatus
               JOIN user u ON lr.user_iduser = u.iduser
               LEFT JOIN user a ON lr.approved_by_iduser = a.iduser
               LEFT JOIN department_To_user dtu ON u.iduser = dtu.user_iduser
               LEFT JOIN department d ON dtu.department_iddepartment = d.iddepartment"""
        if user_id:
            q += " WHERE lr.user_iduser = %s GROUP BY lr.idleaveRequest ORDER BY lr.created_at DESC"
            cursor.execute(q, (user_id,))
        else:
            q += " GROUP BY lr.idleaveRequest ORDER BY lr.created_at DESC"
            cursor.execute(q)
        res = cursor.fetchall()
        if user_id is None:
            _CACHE_ZADOSTI_ALL['data'] = res
            _CACHE_ZADOSTI_ALL['ts'] = time.time()
        return res
    except Exception as e:
        print(e)
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def zmen_stav_zadosti(zadost_id, novy_stav_id, admin_id, reason=None):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("UPDATE leaveRequest SET leaveStatus_idleaveStatus=%s, approved_by_iduser=%s, approved_at=NOW(), rejection_reason=%s WHERE idleaveRequest=%s", (novy_stav_id, admin_id, reason, zadost_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"[zmen_stav_zadosti] Chyba: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def smaz_zadost_volno(zadost_id):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("DELETE FROM leaveRequest WHERE idleaveRequest=%s", (zadost_id,))
        conn.commit()
        return True
    except Exception: return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def pozadej_o_storno(zadost_id, user_id, duvod):
    """Zaměstnanec žádá o storno své schválené absence. Stav řádku se NEMĚNÍ."""
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute(
            "UPDATE leaveRequest SET storno_req_at=NOW(), storno_req_reason=%s "
            "WHERE idleaveRequest=%s AND user_iduser=%s",
            (duvod, zadost_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"[pozadej_o_storno] Chyba: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def zrus_pozadavek_storna(zadost_id):
    """Vedoucí zamítl žádost o storno – absence zůstává schválená."""
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute(
            "UPDATE leaveRequest SET storno_req_at=NULL, storno_req_reason=NULL WHERE idleaveRequest=%s",
            (zadost_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[zrus_pozadavek_storna] Chyba: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def stornuj_zadost_volno(zadost_id, admin_id, duvod):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("SELECT idleaveStatus FROM leaveStatus WHERE name='Stornováno'")
        res = cursor.fetchone()
        stav_id = res[0] if res else 4

        cursor.execute("UPDATE leaveRequest SET leaveStatus_idleaveStatus=%s, approved_by_iduser=%s, approved_at=NOW(), rejection_reason=%s, storno_req_at=NULL, storno_req_reason=NULL WHERE idleaveRequest=%s", (stav_id, admin_id, f"STORNO: {duvod}", zadost_id))
        conn.commit()
        return True
    except Exception as e:
        print(e)
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def uprav_zadost_volno(zadost_id, admin_id, datum_od, datum_do, hodin, minut, typ_id, duvod_upravy, cas_od=None, cas_do=None):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        suma_hours = round((hodin * 60 + minut) / 60, 4)
        cursor.execute("""UPDATE leaveRequest
            SET `from`=%s, `to`=%s, sumaHours=%s, suma_hodin=%s, suma_minut=%s,
                cas_od=%s, cas_do=%s,
                typeOfLeave_idtypeOfLeave=%s, approved_by_iduser=%s, approved_at=NOW(), rejection_reason=%s
            WHERE idleaveRequest=%s""",
            (datum_od, datum_do, suma_hours, hodin, minut, cas_od or None, cas_do or None, typ_id, admin_id, f"ÚPRAVA: {duvod_upravy}", zadost_id))
        conn.commit()
        _CACHE_ZADOSTI_ALL['ts'] = 0.0
        return True
    except Exception as e:
        print(f"[uprav_zadost_volno] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechna_volna_kalendar(jen_budouci=False):
    # Cachujeme pouze dotaz bez filtru (jen_budouci=False) — ten je nejnáročnější
    if not jen_budouci:
        if _CACHE_VOLNA_KALENDAR_ALL['data'] is not None and (time.time() - _CACHE_VOLNA_KALENDAR_ALL['ts']) < _CACHE_SPRAVA_TTL:
            return _CACHE_VOLNA_KALENDAR_ALL['data']
    conn = get_db_connection()
    if not conn: return _CACHE_VOLNA_KALENDAR_ALL['data'] if (not jen_budouci) else []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        q = """SELECT lr.user_iduser, lr.`from`, lr.`to`, lr.sumaHours, lr.suma_hodin, lr.suma_minut, lr.cas_od, lr.cas_do, lr.leaveStatus_idleaveStatus as stav_id,
                      lr.created_at,
                      t.name as typ, s.name as stav_nazev,
                      u.name as u_jmeno, u.surname as u_prijmeni,
                      GROUP_CONCAT(d.name SEPARATOR ', ') as oddeleni
               FROM leaveRequest lr
               JOIN typeOfLeave t ON lr.typeOfLeave_idtypeOfLeave = t.idtypeOfLeave
               JOIN leaveStatus s ON lr.leaveStatus_idleaveStatus = s.idleaveStatus
               JOIN user u ON lr.user_iduser = u.iduser
               LEFT JOIN department_To_user dtu ON u.iduser = dtu.user_iduser
               LEFT JOIN department d ON dtu.department_iddepartment = d.iddepartment
               """
        if jen_budouci:
            q += " WHERE lr.`to` >= CURDATE() "
        q += """ GROUP BY lr.idleaveRequest ORDER BY lr.`from` ASC"""
        cursor.execute(q)
        res = cursor.fetchall()
        if not jen_budouci:
            _CACHE_VOLNA_KALENDAR_ALL['data'] = res
            _CACHE_VOLNA_KALENDAR_ALL['ts'] = time.time()
        return res
    except Exception as e:
        print(e)
        return _CACHE_VOLNA_KALENDAR_ALL['data'] if (not jen_budouci) else []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ==========================================
# PŘESČASY (overtimeRequest)
# ==========================================
def pridej_presczas(user_id, datum_od, datum_do, cas_od, cas_do, hodin, minut, duvod):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        suma_hours = round((hodin * 60 + minut) / 60, 4)
        cursor.execute("""INSERT INTO overtimeRequest
            (user_iduser, datum_od, datum_do, cas_od, cas_do, sumaHours, suma_hodin, suma_minut, duvod, created_at, leaveStatus_idleaveStatus)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 2)""",
            (user_id, datum_od, datum_do, cas_od, cas_do, suma_hours, hodin, minut, duvod or None))
        conn.commit()
        _CACHE_PRESZASY_ALL['ts'] = 0.0
        return True
    except Exception as e:
        print(f"[pridej_presczas] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_presczasy(user_id=None):
    if user_id is None:
        now = time.time()
        if _CACHE_PRESZASY_ALL['data'] is not None and now - _CACHE_PRESZASY_ALL['ts'] < _CACHE_REFRESH_TTL:
            return _CACHE_PRESZASY_ALL['data']
    conn = get_db_connection()
    if not conn: return []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        q = """SELECT ot.idovertimeRequest, ot.user_iduser, ot.datum_od, ot.datum_do,
                      ot.cas_od, ot.cas_do, ot.sumaHours, ot.suma_hodin, ot.suma_minut, ot.duvod, ot.created_at,
                      ot.leaveStatus_idleaveStatus as stav_id, s.name as stav,
                      u.name as u_jmeno, u.surname as u_prijmeni,
                      sb.name as sb_jmeno, sb.surname as sb_prijmeni,
                      ot.storno_at, ot.storno_reason,
                      GROUP_CONCAT(d.name SEPARATOR ', ') as oddeleni
               FROM overtimeRequest ot
               JOIN leaveStatus s ON ot.leaveStatus_idleaveStatus = s.idleaveStatus
               JOIN user u ON ot.user_iduser = u.iduser
               LEFT JOIN user sb ON ot.storno_by_iduser = sb.iduser
               LEFT JOIN department_To_user dtu ON u.iduser = dtu.user_iduser
               LEFT JOIN department d ON dtu.department_iddepartment = d.iddepartment"""
        if user_id:
            q += " WHERE ot.user_iduser = %s GROUP BY ot.idovertimeRequest ORDER BY ot.created_at DESC"
            cursor.execute(q, (user_id,))
        else:
            q += " GROUP BY ot.idovertimeRequest ORDER BY ot.created_at DESC"
            cursor.execute(q)
        res = cursor.fetchall()
        if user_id is None:
            _CACHE_PRESZASY_ALL['data'] = res
            _CACHE_PRESZASY_ALL['ts'] = time.time()
        return res
    except Exception as e:
        print(f"[ziskej_presczasy] {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def stornuj_presczas(presczas_id, admin_id, duvod):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("SELECT idleaveStatus FROM leaveStatus WHERE name='Stornováno'")
        res = cursor.fetchone()
        stav_id = res[0] if res else 4
        cursor.execute("""UPDATE overtimeRequest SET leaveStatus_idleaveStatus=%s,
            storno_by_iduser=%s, storno_at=NOW(), storno_reason=%s
            WHERE idovertimeRequest=%s""", (stav_id, admin_id, duvod, presczas_id))
        conn.commit()
        _CACHE_PRESZASY_ALL['ts'] = 0.0
        return True
    except Exception as e:
        print(f"[stornuj_presczas] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def smaz_presczas(presczas_id):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("DELETE FROM overtimeRequest WHERE idovertimeRequest=%s", (presczas_id,))
        conn.commit()
        _CACHE_PRESZASY_ALL['ts'] = 0.0
        return True
    except Exception as e:
        print(f"[smaz_presczas] {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechny_spolecnosti():
    inicializace_db()
    conn = get_db_connection()
    if not conn: return []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, nazev FROM spolecnost ORDER BY nazev")
        res = cursor.fetchall()
        return res
    except Exception as e:
        print(f"Chyba ziskej_vsechny_spolecnosti: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def uloz_sledovane_uzivatele(user_id, watched_ids):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_watched_users WHERE user_id=%s", (user_id,))
        if watched_ids:
            cursor.executemany("INSERT INTO user_watched_users (user_id, watched_user_id) VALUES (%s, %s)", [(user_id, wid) for wid in watched_ids])
        conn.commit()
        return True
    except Exception as e:
        print(f"Chyba uloz_sledovane_uzivatele: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechny_uzivatele():
    now = time.time()
    if _CACHE_UZIVATELE['data'] is not None and now - _CACHE_UZIVATELE['ts'] < _CACHE_REFRESH_TTL:
        return _CACHE_UZIVATELE['data']

    inicializace_db()
    conn = get_db_connection()
    if not conn: return {}
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)

        # Výchozí group_concat_max_len v MySQL je 1024 BAJTŮ. Uživatel s mnoha
        # právy (Výsledky poboček generují ~18 dlouhých klíčů vysledky_pobocka_*,
        # navíc klíče s diakritikou jsou v utf8mb4 vícebajtové) tento limit
        # přesáhne → GROUP_CONCAT(prava) se TIŠE ořízne. Oříznutá práva pak chybí
        # v přehledu i v editaci (nesvítí zeleně), ačkoliv za běhu fungují
        # (ziskej_prava_uzivatele používá řádkový UNION, ne GROUP_CONCAT).
        cursor.execute("SET SESSION group_concat_max_len = 1000000")

        query = f"""
            SELECT
                u.iduser, u.email, u.name, u.surname, u.is_active,
                u.base_vacation, u.carried_over_vacation, u.osobni_cislo,
                u.realny_zustatek_dovolene, u.realny_zustatek_dovolene_datum,
                u.email_nova_zadost, u.email_vyrizeni_zadosti, u.email_narozeniny,
                u.datum_narozeni, u.priznak_id, u.auto_odhlaseni_minuty, u.pobocka,
                pr.nazev AS priznak_nazev, pr.barva AS priznak_barva,
                MAX(jp.name) AS role,
                GROUP_CONCAT(DISTINCT d.name SEPARATOR ',') AS oddeleni,
                GROUP_CONCAT(DISTINCT p.name SEPARATOR ',') AS prava
            FROM user u
            LEFT JOIN priznak pr ON u.priznak_id = pr.id
            LEFT JOIN user_To_jobPosition utj ON u.iduser = utj.user_iduser
            LEFT JOIN jobPosition jp ON utj.jobPosition_idjobPosition = jp.idjobPosition
            LEFT JOIN department_To_user dtu ON u.iduser = dtu.user_iduser
            LEFT JOIN department d ON dtu.department_iddepartment = d.iddepartment
            LEFT JOIN user_To_privileges utp ON u.iduser = utp.user_iduser
            LEFT JOIN privileges p ON utp.privileges_idprivileges = p.idprivileges
            WHERE u.email != 'admin@admin.cz' AND u.iduser <> {SKRYTY_ADMIN_ID}
            GROUP BY u.iduser
        """
        cursor.execute(query)
        users = cursor.fetchall()

        cursor.execute("SELECT user_id, manager_id FROM user_manager")
        manager_map = {}
        for m in cursor.fetchall():
            manager_map.setdefault(m['user_id'], []).append(m['manager_id'])

        cursor.execute("SELECT us.user_id, s.id as spolecnost_id, s.nazev FROM user_spolecnost us JOIN spolecnost s ON us.spolecnost_id = s.id")
        spolecnost_map = {}
        for s in cursor.fetchall():
            spolecnost_map.setdefault(s['user_id'], []).append({'id': s['spolecnost_id'], 'nazev': s['nazev']})

        cursor.execute("SELECT user_id, watched_user_id FROM user_watched_users")
        watched_map = {}
        for w in cursor.fetchall():
            watched_map.setdefault(w['user_id'], []).append(w['watched_user_id'])

        vysledek = {}
        for u in users:
            if not u['iduser']: continue
            uid = u['iduser']
            vysledek[u['email']] = {
                "id": uid,
                "jmeno_cele": f"{u['name']} {u['surname']}",
                "jmeno": u['name'],
                "prijmeni": u['surname'],
                "role": u['role'] if u['role'] else "Bez role",
                "oddeleni": u['oddeleni'] if u['oddeleni'] else "Bez oddělení",
                "prava": u['prava'] if u['prava'] else "",
                "aktivni": bool(u['is_active']),
                "base_vacation": float(u['base_vacation'] if u['base_vacation'] is not None else 160.0),
                "carried_over_vacation": float(u['carried_over_vacation'] if u['carried_over_vacation'] is not None else 0.0),
                "osobni_cislo": u.get('osobni_cislo'),
                "realny_zustatek_dovolene": float(u['realny_zustatek_dovolene']) if u.get('realny_zustatek_dovolene') is not None else None,
                "realny_zustatek_dovolene_datum": u['realny_zustatek_dovolene_datum'].strftime('%Y-%m-%d') if u.get('realny_zustatek_dovolene_datum') else None,
                "manager_id": manager_map.get(uid, []),
                "spolecnosti": spolecnost_map.get(uid, []),
                "sledovani_uzivatele": watched_map.get(uid, []),
                "email_nova_zadost": bool(u.get('email_nova_zadost', 1)),
                "email_vyrizeni_zadosti": bool(u.get('email_vyrizeni_zadosti', 1)),
                "email_narozeniny": bool(u.get('email_narozeniny', 1)),
                "datum_narozeni": u['datum_narozeni'].strftime('%Y-%m-%d') if u.get('datum_narozeni') else "",
                "priznak_id": u.get('priznak_id'),
                "priznak_nazev": u.get('priznak_nazev') or '',
                "priznak_barva": u.get('priznak_barva') or '',
                "auto_odhlaseni_minuty": u.get('auto_odhlaseni_minuty'),
                "pobocka": u.get('pobocka'),
            }
        _CACHE_UZIVATELE['data'] = vysledek
        _CACHE_UZIVATELE['ts'] = time.time()
        return vysledek
    except Exception as e:
        print(f"Chyba DB pri nacitani uzivatelu: {e}")
        return {}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def pridej_uprav_uzivatele(email, jmeno, prijmeni, heslo_raw, nazev_role, nazev_oddeleni, prava_str, aktivni, base_vacation, carried_over_vacation, osobni_cislo=None, manager_ids=None, spolecnost_ids=None, datum_narozeni=None, priznak_id=None, pobocka=None):
    if manager_ids is None: manager_ids = []
    if spolecnost_ids is None: spolecnost_ids = []

    conn = get_db_connection()
    if not conn: return False, "Chyba připojení k databázi."
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("SELECT iduser FROM user WHERE email=%s", (email,))
        row = cursor.fetchone()

        # Normalizuj datum narození — prázdný string → None
        dn = datum_narozeni if datum_narozeni and str(datum_narozeni).strip() else None

        if row:
            user_id = row[0]
            cursor.execute("UPDATE user SET name=%s, surname=%s, is_active=%s, base_vacation=%s, carried_over_vacation=%s, datum_narozeni=%s, priznak_id=%s WHERE iduser=%s", (jmeno, prijmeni, 1 if aktivni else 0, base_vacation, carried_over_vacation, dn, priznak_id or None, user_id))
            # admin nastavil heslo → uživatel si ho musí při dalším přihlášení změnit
            if heslo_raw: cursor.execute("UPDATE user SET password_hash=%s, zmena_hesla_nutna=1, heslo_zmeneno=NOW() WHERE iduser=%s", (hash_heslo(heslo_raw), user_id))
            # pobočku měníme jen když je explicitně předána (None = ponech beze změny, chrání ostatní call-sites)
            if pobocka is not None: cursor.execute("UPDATE user SET pobocka=%s WHERE iduser=%s", (pobocka or None, user_id))
        else:
            if osobni_cislo:
                cursor.execute("SELECT iduser FROM user WHERE iduser=%s", (osobni_cislo,))
                if cursor.fetchone():
                    return False, f"Osobní číslo {osobni_cislo} je již v systému obsazeno!"
                cursor.execute("INSERT INTO user (iduser, email, name, surname, password_hash, is_active, base_vacation, carried_over_vacation, datum_narozeni, priznak_id, pobocka, zmena_hesla_nutna, heslo_zmeneno) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())", (osobni_cislo, email, jmeno, prijmeni, hash_heslo(heslo_raw) if heslo_raw else "", 1 if aktivni else 0, base_vacation, carried_over_vacation, dn, priznak_id or None, pobocka or None))
                user_id = osobni_cislo
            else:
                cursor.execute("INSERT INTO user (email, name, surname, password_hash, is_active, base_vacation, carried_over_vacation, datum_narozeni, priznak_id, pobocka, zmena_hesla_nutna, heslo_zmeneno) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())", (email, jmeno, prijmeni, hash_heslo(heslo_raw) if heslo_raw else "", 1 if aktivni else 0, base_vacation, carried_over_vacation, dn, priznak_id or None, pobocka or None))
                user_id = cursor.lastrowid

        cursor.execute("DELETE FROM user_To_jobPosition WHERE user_iduser=%s", (user_id,))
        if nazev_role and nazev_role != "Bez role":
            cursor.execute("INSERT IGNORE INTO jobPosition (name) VALUES (%s)", (nazev_role,))
            cursor.execute("SELECT idjobPosition FROM jobPosition WHERE name=%s", (nazev_role,))
            res_role = cursor.fetchone()
            if res_role:
                cursor.execute("INSERT INTO user_To_jobPosition (user_iduser, jobPosition_idjobPosition) VALUES (%s, %s)", (user_id, res_role[0]))

        cursor.execute("DELETE FROM department_To_user WHERE user_iduser=%s", (user_id,))
        if nazev_oddeleni and nazev_oddeleni != "Bez oddělení":
            cursor.execute("INSERT IGNORE INTO department (name) VALUES (%s)", (nazev_oddeleni,))
            cursor.execute("SELECT iddepartment FROM department WHERE name=%s", (nazev_oddeleni,))
            res_odd = cursor.fetchone()
            if res_odd:
                cursor.execute("INSERT INTO department_To_user (department_iddepartment, user_iduser) VALUES (%s, %s)", (res_odd[0], user_id))

        cursor.execute("DELETE FROM user_To_privileges WHERE user_iduser=%s", (user_id,))
        if user_id != SKRYTY_ADMIN_ID:
            prava_str = _bez_admin_prav(prava_str)
        for p in [p.strip() for p in prava_str.split(',') if p.strip()]:
            cursor.execute("INSERT IGNORE INTO privileges (name) VALUES (%s)", (p,))
            cursor.execute("SELECT idprivileges FROM privileges WHERE name=%s", (p,))
            res_priv = cursor.fetchone()
            if res_priv:
                cursor.execute("INSERT INTO user_To_privileges (user_iduser, privileges_idprivileges) VALUES (%s, %s)", (user_id, res_priv[0]))

        cursor.execute("DELETE FROM user_manager WHERE user_id=%s", (user_id,))
        if manager_ids:
            cursor.executemany("INSERT INTO user_manager (user_id, manager_id) VALUES (%s, %s)", [(user_id, mid) for mid in manager_ids])

        cursor.execute("DELETE FROM user_spolecnost WHERE user_id=%s", (user_id,))
        if spolecnost_ids:
            cursor.executemany("INSERT INTO user_spolecnost (user_id, spolecnost_id) VALUES (%s, %s)", [(user_id, sid) for sid in spolecnost_ids])

        conn.commit()
        vymazat_cache_prav(user_id)
        return True, "OK"
    except Exception as e:
        return False, str(e)
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechny_priznaky() -> list[dict]:
    """Vrátí seznam všech příznaků [{id, nazev, barva}]."""
    conn = get_db_connection()
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, nazev, barva FROM priznak ORDER BY nazev")
        return cur.fetchall()
    except Exception as e:
        print(f'[intranet_data.ziskej_vsechny_priznaky] {e}')
        return []
    finally:
        cur.close(); conn.close()

def pridej_priznak(nazev: str, barva: str) -> tuple[bool, str]:
    """Vytvoří nový příznak. Vrátí (True, id) nebo (False, chyba)."""
    conn = get_db_connection()
    if not conn: return False, 'Chyba připojení k DB.'
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO priznak (nazev, barva) VALUES (%s, %s)", (nazev.strip(), barva))
        conn.commit()
        return True, str(cur.lastrowid)
    except Exception as e:
        return False, str(e)
    finally:
        cur.close(); conn.close()

def nastav_priznak_oddeleni(oddeleni_nazev: str, priznak_id) -> tuple[bool, int]:
    """Nastaví příznak všem uživatelům daného oddělení. Vrátí (True, počet_aktualizovaných)."""
    conn = get_db_connection()
    if not conn: return False, 0
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE user u"
            " JOIN department_To_user dtu ON u.iduser = dtu.user_iduser"
            " JOIN department d ON dtu.department_iddepartment = d.iddepartment"
            " SET u.priznak_id = %s"
            " WHERE d.name = %s",
            (priznak_id or None, oddeleni_nazev)
        )
        conn.commit()
        return True, cur.rowcount
    except Exception as e:
        print(f'[intranet_data.nastav_priznak_oddeleni] {e}')
        return False, 0
    finally:
        cur.close(); conn.close()

def smaz_priznak(priznak_id: int) -> tuple[bool, str]:
    """Smaže příznak (uživatelé s tímto příznakem budou mít priznak_id = NULL)."""
    conn = get_db_connection()
    if not conn: return False, 'Chyba připojení k DB.'
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM priznak WHERE id=%s", (priznak_id,))
        conn.commit()
        return True, 'OK'
    except Exception as e:
        return False, str(e)
    finally:
        cur.close(); conn.close()

def smaz_uzivatele(email):
    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("SELECT iduser FROM user WHERE email=%s", (email,))
        row = cursor.fetchone()
        cursor.execute("DELETE FROM user WHERE email=%s", (email,))
        conn.commit()
        if row:
            vymazat_cache_prav(row[0])
    except Exception: pass
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def uloz_vysledek_kvizu(user_id, stav, uspesnost, body, doba, historie_odpovedi=None):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("INSERT INTO vysledky_kvizu (user_iduser, stav_testu, uspesnost, body, doba_trvani) VALUES (%s, %s, %s, %s, %s)", (user_id, stav, uspesnost, body, doba))
        vysledek_id = cursor.lastrowid
        if historie_odpovedi:
            for o in historie_odpovedi:
                cursor.execute("INSERT INTO zaznamy_odpovedi (vysledek_id, poradi, otazka, tvoje_volba, spravna_odpoved, hodnoceni) VALUES (%s, %s, %s, %s, %s, %s)",
                               (vysledek_id, o.get("Pořadí v testu"), o.get("Otázka"), o.get("Tvoje volba"), o.get("Správná odpověď"), o.get("Hodnocení")))
        conn.commit()
        return True
    except Exception: return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def uloz_smlouvu_veletrh(res_id, dodavatel, ico, cesta):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("INSERT INTO veletrh_smlouvy (res_id, dodavatel, ico, cesta_k_souboru, vytvoreno) VALUES (%s, %s, %s, %s, NOW())",
                       (res_id, dodavatel, ico, cesta))
        if ico and ico.strip() and dodavatel and dodavatel.strip():
            try: cursor.execute("INSERT IGNORE INTO dodavatel_firma (ico, nazev) VALUES (%s, %s)", (ico.strip(), dodavatel.strip()))
            except Exception: pass
        conn.commit()
        return True
    except Exception as e:
        print(f"Chyba při ukládání smlouvy do DB: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_smlouvy_veletrh():
    conn = get_db_connection()
    if not conn: return []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT * FROM veletrh_smlouvy ORDER BY vytvoreno DESC")
        res = cursor.fetchall()
        return res
    except Exception: return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def smaz_smlouvu_veletrh(smlouva_id):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("SELECT cesta_k_souboru FROM veletrh_smlouvy WHERE id=%s", (smlouva_id,))
        row = cursor.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            try: os.remove(row[0])
            except Exception: pass

        cursor.execute("DELETE FROM veletrh_smlouvy WHERE id=%s", (smlouva_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Chyba při mazání smlouvy: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def inicializace_faktur_db():
    conn = get_db_connection()
    if not conn: return
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS faktury (
                id VARCHAR(50) PRIMARY KEY,
                zadavatel VARCHAR(100),
                dodavatel VARCHAR(255),
                ico VARCHAR(50),
                variabilni_symbol VARCHAR(100),
                cislo_faktury VARCHAR(100),
                duzp VARCHAR(20),
                splatnost VARCHAR(20),
                castka DECIMAL(12,2),
                castka_21 DECIMAL(12,2) DEFAULT 0.00,
                castka_12 DECIMAL(12,2) DEFAULT 0.00,
                castka_0 DECIMAL(12,2) DEFAULT 0.00,
                popis TEXT,
                schvalovatel VARCHAR(100),
                soubor_original VARCHAR(500),
                soubor_schvaleny VARCHAR(500),
                stav VARCHAR(50),
                datum_zadani DATETIME,
                datum_schvaleni DATETIME NULL
            ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)

        for col, col_def in [("ico", "VARCHAR(50) DEFAULT ''"), ("variabilni_symbol", "VARCHAR(100) DEFAULT ''"), ("castka_21", "DECIMAL(12,2) DEFAULT 0.00"), ("castka_12", "DECIMAL(12,2) DEFAULT 0.00"), ("castka_0", "DECIMAL(12,2) DEFAULT 0.00")]:
            try:
                cursor.execute(f"ALTER TABLE faktury ADD COLUMN {col} {col_def}")
                conn.commit()
            except Exception: pass

        for p in ['nakup_uzivatel', 'nakup_schvalit', 'faktury_seznam_schvalit']:
            cursor.execute("INSERT IGNORE INTO privileges (name) VALUES (%s)", (p,))

        # ── Migrace Aprovia práv na 3 role dle dokumentu ──────────────────
        # ADITIVNÍ a idempotentní: uživatelům se zrušenými/sloučenými právy se
        # pouze PŘIDÁ ekvivalentní nové právo (nic se nemaže), aby nikdo neztratil
        # přístup. Zrušené názvy práv zůstanou v DB neškodně (kód je už nečte).
        # Granty existují ve 3 vazebních tabulkách → projdeme všechny.
        try:
            _vazby = [
                ("user_To_privileges", "user_iduser"),
                ("jobPosition_To_privileges", "jobPosition_idjobPosition"),
                ("department_To_privileges", "department_iddepartment"),
            ]
            # (zdrojové právo → cílové právo, které má držitel získat)
            _mapovani = [
                ('faktury_admin', 'faktury_seznam_schvalit'),
                ('faktury_uzivatel', 'nakup_uzivatel'),
                ('nakup_admin', 'nakup_schvalit'),
                ('nakup_admin', 'nakup_uzivatel'),
            ]
            for tab, sloupec in _vazby:
                for stare, nove in _mapovani:
                    cursor.execute(
                        f"INSERT IGNORE INTO {tab} ({sloupec}, privileges_idprivileges) "
                        f"SELECT t.{sloupec}, pn.idprivileges FROM {tab} t "
                        f"JOIN privileges ps ON ps.idprivileges = t.privileges_idprivileges AND ps.name=%s "
                        f"JOIN privileges pn ON pn.name=%s",
                        (stare, nove))
            conn.commit()
        except Exception:
            pass

        # Naplnit dodavatel_firma z existujících faktur (3NF: ico → dodavatel)
        try: cursor.execute("INSERT IGNORE INTO dodavatel_firma (ico, nazev) SELECT DISTINCT ico, dodavatel FROM faktury WHERE ico IS NOT NULL AND ico != '' AND dodavatel IS NOT NULL AND dodavatel != ''")
        except Exception: pass

        conn.commit()
    except Exception as e:
        print(f"Chyba při inicializaci DB faktur: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def uloz_fakturu(data):
    inicializace_faktur_db()
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO faktury (id, zadavatel, dodavatel, ico, variabilni_symbol, cislo_faktury, duzp, splatnost, castka, popis, schvalovatel, soubor_original, stav, datum_zadani)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (data['id'], data['zadavatel'], data['dodavatel'], data.get('ico', ''), data.get('variabilni_symbol', ''), data['cislo_faktury'], data['duzp'], data['splatnost'], data['castka'], data['popis'], data['schvalovatel'], data['soubor_original'], data['stav'], data['datum_zadani']))
        conn.commit()
        return True
    except Exception as e:
        print(f"Chyba uložení faktury: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ziskej_vsechny_faktury():
    inicializace_faktur_db()
    conn = get_db_connection()
    if not conn: return []
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM faktury ORDER BY datum_zadani DESC")
        res = cursor.fetchall()

        for row in res:
            if row['datum_zadani']: row['datum_zadani'] = row['datum_zadani'].strftime('%Y-%m-%d %H:%M')
            if row['datum_schvaleni']: row['datum_schvaleni'] = row['datum_schvaleni'].strftime('%Y-%m-%d %H:%M')
            row['castka'] = float(row['castka'])

        return res
    except Exception as e:
        print(f"Chyba načtení faktur: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def aktualizuj_stav_faktury(f_id, stav, soubor_schvaleny, datum_schvaleni):
    conn = get_db_connection()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE faktury 
            SET stav = %s, soubor_schvaleny = %s, datum_schvaleni = %s 
            WHERE id = %s
        """, (stav, soubor_schvaleny, datum_schvaleni, f_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"Chyba aktualizace faktury: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()