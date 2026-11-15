# ========================================================
# DVOUFAKTOROVÉ OVĚŘENÍ (TOTP — RFC 6238)
# ========================================================
# Autentifikační aplikace (Google/Microsoft Authenticator, Aegis, …).
# TOTP je implementováno čistě přes stdlib (hmac/struct/base64) — žádná
# externí závislost, kterou by šlo omylem odinstalovat a tím 2FA obejít.
# Knihovna `qrcode` je volitelná: bez ní se při aktivaci zobrazí jen
# ruční klíč (Base32), který lze do aplikace opsat.

import base64
import hashlib
import hmac
import io
import json
import struct
import threading
import time
import urllib.parse
import secrets as _secrets

import intranet_data

try:
    import qrcode as _qrcode
    _QRCODE_DOSTUPNY = True
except ImportError:
    _qrcode = None
    _QRCODE_DOSTUPNY = False

_ISSUER = 'Moje JIPka'
_TOTP_KROK_S = 30          # délka časového okna (standard)
_TOTP_CISLIC = 6
_TOTP_TOLERANCE = 1        # ±1 okno kvůli posunu hodin telefonu
_POCET_ZALOZNICH_KODU = 10
_DUVERA_ZARIZENI_DNU = 30   # jak dlouho zůstane zařízení "zapamatované"

# Abeceda záložních kódů bez zaměnitelných znaků (0/O, 1/I/L)
_ABECEDA_KODU = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'

# Rate limiting ověřování 2FA kódů (stejné limity jako u hesla — K-6):
# 6místný kód nesmí jít uhádnout hrubou silou.
_2FA_POKUSY: dict = {}     # user_id → {'pocet': int, 'prvni': float, 'zamcen_do': float}
_2FA_LOCK = threading.Lock()
_MAX_POKUSY = 5
_OKNO_SEKUND = 300         # 5 minut
_LOCK_SEKUND = 900         # 15 minut


# ========================================================
# TOTP — čistý stdlib (RFC 4226 / RFC 6238, SHA-1, 30 s, 6 číslic)
# ========================================================

def _dekoduj_base32(secret: str) -> bytes:
    s = (secret or '').strip().replace(' ', '').upper()
    return base64.b32decode(s + '=' * ((8 - len(s) % 8) % 8))

def _hotp(klic: bytes, citac: int) -> str:
    """RFC 4226 — HMAC-SHA1 + dynamická trunkace na 6 číslic."""
    h = hmac.new(klic, struct.pack('>Q', citac), hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    kod = (struct.unpack('>I', h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** _TOTP_CISLIC)
    return str(kod).zfill(_TOTP_CISLIC)

def totp_kod(secret: str, cas: float | None = None) -> str:
    """Aktuální TOTP kód pro daný Base32 secret (pro testy a ověření)."""
    t = time.time() if cas is None else cas
    return _hotp(_dekoduj_base32(secret), int(t // _TOTP_KROK_S))

def overit_totp(secret: str, kod: str, cas: float | None = None) -> bool:
    """Porovná kód s tolerancí ±1 časové okno, konstantním časem."""
    kod = (kod or '').strip().replace(' ', '')
    if not kod.isdigit() or len(kod) != _TOTP_CISLIC:
        return False
    try:
        klic = _dekoduj_base32(secret)
    except Exception:
        return False
    t = time.time() if cas is None else cas
    citac = int(t // _TOTP_KROK_S)
    ok = False
    # Projdeme VŠECHNA okna (bez early-return) — konstantní čas.
    for posun in range(-_TOTP_TOLERANCE, _TOTP_TOLERANCE + 1):
        if hmac.compare_digest(_hotp(klic, citac + posun), kod):
            ok = True
    return ok

def vygeneruj_secret() -> str:
    """160bitový secret v Base32 (32 znaků) — doporučení RFC 4226."""
    return base64.b32encode(_secrets.token_bytes(20)).decode('ascii')

def otpauth_uri(email: str, secret: str) -> str:
    label = urllib.parse.quote(f'{_ISSUER}:{email}')
    issuer = urllib.parse.quote(_ISSUER)
    return f'otpauth://totp/{label}?secret={secret}&issuer={issuer}'

def qr_data_uri(uri: str):
    """PNG QR kód jako data URI pro ui.image, None pokud qrcode chybí."""
    if not _QRCODE_DOSTUPNY:
        return None
    try:
        img = _qrcode.make(uri, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        return None


# ========================================================
# Záložní kódy
# ========================================================

def _hash_zalozni_kod(kod: str) -> str:
    # Kódy jsou náhodné s vysokou entropií (~40 bitů) — sha256 stačí,
    # bcrypt by při ověřování 10 kódů najednou blokoval přihlášení.
    norm = kod.strip().upper().replace('-', '').replace(' ', '')
    return hashlib.sha256(norm.encode('utf-8')).hexdigest()

def _vygeneruj_zalozni_kody() -> list:
    kody = []
    for _ in range(_POCET_ZALOZNICH_KODU):
        znaky = ''.join(_secrets.choice(_ABECEDA_KODU) for _ in range(8))
        kody.append(f'{znaky[:4]}-{znaky[4:]}')
    return kody


# ========================================================
# Rate limiting (zrcadlí K-6 mechanismus přihlášení)
# ========================================================

def _zkontroluj_limit(user_id) -> tuple:
    now = time.time()
    with _2FA_LOCK:
        info = _2FA_POKUSY.get(user_id)
        if info and now < info.get('zamcen_do', 0):
            return True, int(info['zamcen_do'] - now)
        return False, 0

def _zaznamenej_neuspech(user_id):
    now = time.time()
    with _2FA_LOCK:
        info = _2FA_POKUSY.get(user_id, {'pocet': 0, 'prvni': now, 'zamcen_do': 0})
        if now - info['prvni'] > _OKNO_SEKUND:
            info = {'pocet': 0, 'prvni': now, 'zamcen_do': 0}
        info['pocet'] += 1
        if info['pocet'] >= _MAX_POKUSY:
            info['zamcen_do'] = now + _LOCK_SEKUND
        _2FA_POKUSY[user_id] = info

def _zaznamenej_uspech(user_id):
    with _2FA_LOCK:
        _2FA_POKUSY.pop(user_id, None)


# ========================================================
# DB operace
# ========================================================

def _nacti_totp_zaznam(user_id):
    """Vrátí dict {'secret', 'aktivni', 'kody': list} nebo None."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            "SELECT totp_secret, totp_aktivni, totp_zalozni_kody FROM user WHERE iduser = %s",
            (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        try:
            kody = json.loads(row['totp_zalozni_kody']) if row['totp_zalozni_kody'] else []
        except Exception:
            kody = []
        return {'secret': row['totp_secret'], 'aktivni': bool(row['totp_aktivni']), 'kody': kody}
    except Exception as e:
        print(f"Chyba DB 2FA čtení: {e}")
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def _uloz_totp(user_id, secret, aktivni, kody_hashe):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user SET totp_secret = %s, totp_aktivni = %s, totp_zalozni_kody = %s WHERE iduser = %s",
            (secret, 1 if aktivni else 0,
             json.dumps(kody_hashe) if kody_hashe is not None else None, user_id))
        conn.commit()
        return cursor.rowcount >= 0
    except Exception as e:
        print(f"Chyba DB 2FA zápis: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# ========================================================
# Veřejné API
# ========================================================

def ma_aktivni_2fa(user_id) -> bool:
    """Rychlá kontrola při přihlášení. Nouzový admin (999999) 2FA nemá."""
    if not user_id or user_id == 999999:
        return False
    zaznam = _nacti_totp_zaznam(user_id)
    return bool(zaznam and zaznam['aktivni'] and zaznam['secret'])

def zahaj_aktivaci(user_id, email):
    """Vygeneruje nový secret (přepíše případný nepotvrzený) a vrátí
    (secret, otpauth_uri, qr_data_uri | None). Aktivace platí až po potvrzení."""
    secret = vygeneruj_secret()
    if not _uloz_totp(user_id, secret, False, None):
        return None, None, None
    uri = otpauth_uri(email, secret)
    return secret, uri, qr_data_uri(uri)

def potvrd_aktivaci(user_id, kod):
    """Ověří první kód z aplikace; při úspěchu 2FA zapne a vrátí seznam
    záložních kódů (zobrazí se JEDNOU). Jinak None."""
    zaznam = _nacti_totp_zaznam(user_id)
    if not zaznam or not zaznam['secret'] or zaznam['aktivni']:
        return None
    if not overit_totp(zaznam['secret'], kod):
        return None
    kody = _vygeneruj_zalozni_kody()
    if not _uloz_totp(user_id, zaznam['secret'], True, [_hash_zalozni_kod(k) for k in kody]):
        return None
    _zaznamenej_uspech(user_id)
    return kody

def overit_2fa_kod(user_id, kod):
    """Ověření při přihlášení — TOTP kód, nebo záložní kód (jednorázový).

    Vrátí (ok: bool, zprava: str | None). Zprava 'ZAMCEN:<s>' při zámku
    (stejný protokol jako overit_prihlaseni), jinak text chyby / upozornění
    na docházející záložní kódy."""
    zamcen, zbyva = _zkontroluj_limit(user_id)
    if zamcen:
        return False, f'ZAMCEN:{zbyva}'

    zaznam = _nacti_totp_zaznam(user_id)
    if not zaznam or not zaznam['aktivni'] or not zaznam['secret']:
        return False, 'Dvoufaktorové ověření není u účtu aktivní.'

    if overit_totp(zaznam['secret'], kod):
        _zaznamenej_uspech(user_id)
        return True, None

    # Záložní kód — po použití se zneplatní.
    h = _hash_zalozni_kod(kod or '')
    zbyle = [k for k in zaznam['kody'] if not hmac.compare_digest(k, h)]
    if len(zbyle) < len(zaznam['kody']):
        if not _uloz_totp(user_id, zaznam['secret'], True, zbyle):
            return False, 'Chyba databáze při ověření kódu.'
        _zaznamenej_uspech(user_id)
        if len(zbyle) <= 2:
            return True, f'Použit záložní kód. Zbývá jich už jen {len(zbyle)} — vygenerujte si v nastavení nové.'
        return True, f'Použit záložní kód (zbývá {len(zbyle)}).'

    _zaznamenej_neuspech(user_id)
    return False, 'Neplatný ověřovací kód.'

def deaktivuj_2fa(user_id) -> bool:
    """Vypne 2FA a smaže secret i záložní kódy (uživatel po ověření hesla,
    nebo administrátor při ztrátě telefonu). Zruší i všechna důvěryhodná
    zařízení — po opětovné aktivaci musí projít ověřením znovu."""
    ok = _uloz_totp(user_id, None, False, None)
    if ok:
        zrus_duveryhodna_zarizeni(user_id)
    return ok

def pocet_zaloznich_kodu(user_id) -> int:
    zaznam = _nacti_totp_zaznam(user_id)
    return len(zaznam['kody']) if zaznam and zaznam['aktivni'] else 0

def generuj_nove_zalozni_kody(user_id):
    """Nahradí záložní kódy novými (jen při aktivní 2FA). Vrátí seznam nebo None."""
    zaznam = _nacti_totp_zaznam(user_id)
    if not zaznam or not zaznam['aktivni'] or not zaznam['secret']:
        return None
    kody = _vygeneruj_zalozni_kody()
    if not _uloz_totp(user_id, zaznam['secret'], True, [_hash_zalozni_kod(k) for k in kody]):
        return None
    return kody


# ========================================================
# Důvěryhodná ("zapamatovaná") zařízení
# ========================================================
# Uživatel může při aktivaci nebo přihlášení zaškrtnout "Pamatovat toto
# zařízení". Vygeneruje se náhodný token — jeho SHA-256 hash se uloží do DB,
# samotný token drží prohlížeč (app.storage.user). Při dalším přihlášení se
# token ověří proti DB a při shodě se TOTP kód nevyžaduje (do expirace).

def _hash_duvera_token(token: str) -> str:
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()

def zaregistruj_duveryhodne_zarizeni(user_id, popis=None, dnu=_DUVERA_ZARIZENI_DNU):
    """Vytvoří token důvěry pro toto zařízení a vrátí jej (uloží se jen hash).
    Vrátí None při chybě. Token je bezpečné uložit do prohlížeče uživatele."""
    if not user_id or user_id == 999999:
        return None
    token = _secrets.token_urlsafe(32)
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cursor = None
    try:
        cursor = conn.cursor()
        # Úklid vlastních expirovaných záznamů, ať tabulka neroste donekonečna.
        cursor.execute(
            "DELETE FROM totp_duveryhodne_zarizeni WHERE user_id = %s AND expiruje <= NOW()",
            (user_id,))
        cursor.execute(
            "INSERT INTO totp_duveryhodne_zarizeni (user_id, token_hash, popis, vytvoreno, expiruje) "
            "VALUES (%s, %s, %s, NOW(), DATE_ADD(NOW(), INTERVAL %s DAY))",
            (user_id, _hash_duvera_token(token), (popis or None) and str(popis)[:255], int(dnu)))
        conn.commit()
        return token
    except Exception as e:
        print(f"Chyba DB 2FA důvěra (zápis): {e}")
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def je_zarizeni_duveryhodne(user_id, token) -> bool:
    """True, pokud token odpovídá platnému (neexpirovanému) záznamu uživatele."""
    if not user_id or user_id == 999999 or not token:
        return False
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM totp_duveryhodne_zarizeni "
            "WHERE user_id = %s AND token_hash = %s AND expiruje > NOW() LIMIT 1",
            (user_id, _hash_duvera_token(token)))
        return cursor.fetchone() is not None
    except Exception as e:
        print(f"Chyba DB 2FA důvěra (čtení): {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def pocet_duveryhodnych_zarizeni(user_id) -> int:
    """Počet platných zapamatovaných zařízení (pro zobrazení v nastavení)."""
    if not user_id or user_id == 999999:
        return 0
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM totp_duveryhodne_zarizeni WHERE user_id = %s AND expiruje > NOW()",
            (user_id,))
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def zrus_duveryhodna_zarizeni(user_id) -> int:
    """Smaže všechna zapamatovaná zařízení uživatele (odhlášení ze všech).
    Vrátí počet zrušených záznamů, nebo -1 při chybě."""
    if not user_id:
        return -1
    conn = intranet_data.get_db_connection()
    if not conn:
        return -1
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM totp_duveryhodne_zarizeni WHERE user_id = %s", (user_id,))
        conn.commit()
        return cursor.rowcount
    except Exception as e:
        print(f"Chyba DB 2FA důvěra (mazání): {e}")
        return -1
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
