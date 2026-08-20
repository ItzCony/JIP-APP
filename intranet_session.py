"""
Centrální evidence aktivních relací a „chytré" automatické odhlašování.

Proč modul existuje
-------------------
Původní logika odhlašovala uživatele 15 s po KAŽDÉM výpadku WebSocketu. NiceGUI
ale volá ``on_disconnect`` okamžitě při každém přerušení spojení (uspání PC,
přepnutí Wi-Fi mezi AP, záložka na pozadí, blokující import/export…), přičemž
tichý reconnect prohlížeče stránku znovu nespustí. Výsledkem bylo náhodné
odhlašování i aktivních uživatelů — nezávislé na nastavení minut nečinnosti.

Pravidla
--------
* **1 účet = 1 aktivní relace** (single-session). ``GLOBAL_ACTIVE_SESSIONS`` drží
  pro každý e-mail nejnovější ``login_token``; starší relaci ``kontrola_relace``
  odhlásí. ``setdefault`` v ``registruj_pripojeni`` zajistí, že pravidlo platí
  i po restartu serveru (kdy je evidence v paměti prázdná), aniž by se přepsalo
  novější přihlášení z jiného zařízení.
* **Odhlášení „po zavření prohlížeče" se spustí, až když opravdu nikde není živé
  připojení.** Počítáme živá WebSocket připojení na token (``ZIVE_PRIPOJENI``)
  napříč kartami i routami. Session smažeme až po ochranné lhůtě (``ODHLASENI_PO_S``),
  pokud do té doby nedojde k reconnectu/otevření jiné karty.
"""
from __future__ import annotations

import asyncio
import collections
import glob
import json
import uuid

from nicegui import app

import intranet_data
import intranet_monitor
import intranet_logger

# email (lower) -> aktuální (nejnovější) login_token  → single-session
GLOBAL_ACTIVE_SESSIONS: dict[str, str] = {}
# login_token -> počet právě živých WebSocket připojení (napříč kartami i routami)
ZIVE_PRIPOJENI: "collections.Counter[str]" = collections.Counter()
# login_token -> asyncio.Task s odloženým odhlášením
PENDING_LOGOUTS: dict[str, asyncio.Task] = {}
# e-maily, kterým admin VYNUTIL odhlášení (drženo dokud se znovu nepřihlásí) —
# kontrola_relace je při nejbližším tiku odhlásí a přesměruje na login.
FORCE_LOGOUT_EMAILS: set[str] = set()

# Kolik sekund po ztrátě POSLEDNÍHO připojení čekáme, než relaci zrušíme.
# Musí být VÍC než reconnect_timeout v ui.run() (aktuálně 30 s ve web_main.py),
# ať se stihne tichý reconnect prohlížeče.
ODHLASENI_PO_S = 35


def _vymaz_session_soubor(token: str) -> bool:
    """Najde NiceGUI session soubor podle login_token a vyprázdní ho."""
    try:
        for cesta in glob.glob('.nicegui/storage-user-*.json'):
            try:
                with open(cesta, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('login_token') == token:
                    intranet_data.zapis_json_atomicky(cesta, {})
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def zaznamenej_prihlaseni_token(email: str, token: str) -> None:
    """Po úspěšném loginu: tento token je nově ten platný pro daný účet
    (jakákoli starší relace bude odhlášena přes ``kontrola_relace``)."""
    if email and token:
        GLOBAL_ACTIVE_SESSIONS[email] = token
        # Nové přihlášení ruší případné admin-vynucené odhlášení, jinak by se
        # uživatel odhlásil hned po opětovném přihlášení.
        FORCE_LOGOUT_EMAILS.discard(email.lower())


def zahaj_relaci(id_u, email: str, jmeno: str) -> str:
    """Vznik přihlášené relace — jediné místo pro formulář i OIDC callback.
    Rychlá část: zápis do ``app.storage.user`` + evidence tokenu. Musí proběhnout
    dřív, než se kamkoli naviguje, jinak stráž na stránce vyhodí zpět na login."""
    token = str(uuid.uuid4())
    app.storage.user.update({
        'user_id': id_u,
        'user_email': email,
        'user_name': jmeno,
        'login_token': token,
        'intranet_tab': 'prehled',
    })
    zaznamenej_prihlaseni_token(email, token)
    return token


def zaznamenej_klienta(email: str, jmeno: str, ip: str, device: str) -> None:
    """Pomalá část: monitor + audit log. Odděleno od ``zahaj_relaci``, protože
    ve formuláři se IP/zařízení zjišťuje až po detekci Brave (až 2 s) — to nesmí
    zdržet samotné přihlášení."""
    # Uložíme do relace, aby je znalo i (synchronní) odhlášení
    app.storage.user['login_ip'] = ip
    app.storage.user['login_device'] = device
    intranet_monitor.zaznamenej_prihlaseni(email, jmeno, ip=ip, device=device)
    intranet_logger.log_activity(jmeno, "Přihlášení", f"Úspěšné přihlášení (E-mail: {email})", ip=ip, device=device)


def vynut_odhlaseni(email: str) -> None:
    """Admin VYNUTÍ odhlášení uživatele. Jeho živá relace se při nejbližší
    ``kontrola_relace`` (~30 s) sama vyčistí a přesměruje na login. Navíc hned
    vyprázdníme session soubor a evidenci, aby byl odhlášen i po zavření a
    opětovném otevření prohlížeče."""
    if not email:
        return
    email = email.lower()
    FORCE_LOGOUT_EMAILS.add(email)
    token = GLOBAL_ACTIVE_SESSIONS.pop(email, None)
    intranet_monitor.odeber_prihlaseni(email)
    if token:
        _vymaz_session_soubor(token)
        ZIVE_PRIPOJENI.pop(token, None)
        t = PENDING_LOGOUTS.pop(token, None)
        if t:
            t.cancel()


def ma_vynuceno_odhlaseni(email: str) -> bool:
    """True = tomuto e-mailu admin vynutil odhlášení (dokud se znovu nepřihlásí)."""
    return bool(email) and email.lower() in FORCE_LOGOUT_EMAILS


def je_nahrazena(email: str, token: str) -> bool:
    """True = tuto relaci nahradila novější (jako aktuální je veden jiný token).
    Používá ``kontrola_relace`` pro vynucení single-session."""
    if not (email and token):
        return False
    aktualni = GLOBAL_ACTIVE_SESSIONS.get(email)
    return aktualni is not None and aktualni != token


def registruj_pripojeni(client, token: str, email: str, jmeno: str) -> None:
    """Zaregistruje živé připojení této stránky/karty a navěsí
    ``on_connect``/``on_disconnect``, aby počet připojení zůstal správný i při
    reconnectu. Volat z KAŽDÉ přihlášené stránky."""
    if not token:
        return

    # Single-session přežije i restart serveru (evidence je jen v paměti).
    # setdefault NEpřepíše případné novější přihlášení z jiného zařízení.
    if email:
        GLOBAL_ACTIVE_SESSIONS.setdefault(email, token)

    # Per-klient guard, aby se připojení započítalo právě jednou (i kdyby
    # NiceGUI zavolal on_connect i pro prvotní handshake).
    stav = {'pripojeno': False}

    def _pridej() -> None:
        if stav['pripojeno']:
            return
        stav['pripojeno'] = True
        ZIVE_PRIPOJENI[token] += 1
        # Uživatel je (zase) online → zruš případné čekající odhlášení.
        t = PENDING_LOGOUTS.pop(token, None)
        if t:
            t.cancel()

    def _uber() -> None:
        if not stav['pripojeno']:
            return
        stav['pripojeno'] = False
        if ZIVE_PRIPOJENI[token] > 0:
            ZIVE_PRIPOJENI[token] -= 1
        _naplanuj_odhlaseni(token, email, jmeno)

    _pridej()                       # počáteční připojení této karty
    client.on_connect(_pridej)      # reconnect (idempotentní díky guardu)
    client.on_disconnect(_uber)     # výpadek / zavření karty


def _naplanuj_odhlaseni(token: str, email: str, jmeno: str) -> None:
    """Naplánuje odhlášení po ochranné lhůtě — provede se jen, pokud do té doby
    nepřibude žádné živé připojení (tj. uživatel opravdu zavřel prohlížeč)."""
    old = PENDING_LOGOUTS.pop(token, None)
    if old:
        old.cancel()

    async def _mozna_odhlasit() -> None:
        try:
            await asyncio.sleep(ODHLASENI_PO_S)
            if ZIVE_PRIPOJENI.get(token, 0) > 0:
                return                       # někde je živé připojení → nech být
            ZIVE_PRIPOJENI.pop(token, None)
            smazano = _vymaz_session_soubor(token)   # uklidí jen tenhle token
            # Side-efekty (single-session evidence, monitor, log) jen pokud je
            # tahle relace pořád ta platná — ať nepřepíšeme novější login.
            if smazano and GLOBAL_ACTIVE_SESSIONS.get(email) == token:
                GLOBAL_ACTIVE_SESSIONS.pop(email, None)
                intranet_monitor.odeber_prihlaseni(email)
                intranet_logger.log_activity(jmeno or 'Neznámý', "Odhlášení",
                                             "Automatické odhlášení po zavření prohlížeče")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            PENDING_LOGOUTS.pop(token, None)

    PENDING_LOGOUTS[token] = asyncio.create_task(_mozna_odhlasit())


def odhlas_rucne(email: str, token: str) -> None:
    """Úklid evidence při explicitním odhlášení (tlačítko / ``/logout``)."""
    if email and GLOBAL_ACTIVE_SESSIONS.get(email) == token:
        GLOBAL_ACTIVE_SESSIONS.pop(email, None)
    if token:
        ZIVE_PRIPOJENI.pop(token, None)
        t = PENDING_LOGOUTS.pop(token, None)
        if t:
            t.cancel()
