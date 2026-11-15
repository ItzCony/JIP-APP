"""OIDC přihlášení (Microsoft Entra ID) — Authorization Code + PKCE.

Modul je VOLITELNÝ: aktivuje se jen když jsou vyplněné všechny čtyři env
proměnné. Dokud nejsou, `je_zapnuto()` vrací False, tlačítko se na login
stránce nezobrazí a aplikace se chová přesně jako předtím (heslo + TOTP).

Env:
    OIDC_ISSUER         https://login.microsoftonline.com/<tenant-id>/v2.0
    OIDC_CLIENT_ID      Application (client) ID z App registrations
    OIDC_CLIENT_SECRET  hodnota (ne ID) client secretu
    OIDC_REDIRECT_URI   https://<doména>/auth/callback  (znak po znaku shodné
                        s hodnotou registrovanou v Entra)

Bez externích závislostí — HTTP jede přes urllib ze stdlib.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app

import intranet_data
import intranet_logger
import intranet_session

ISSUER = os.environ.get('OIDC_ISSUER', '').strip().rstrip('/')
CLIENT_ID = os.environ.get('OIDC_CLIENT_ID', '').strip()
CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET', '').strip()
REDIRECT_URI = os.environ.get('OIDC_REDIRECT_URI', '').strip()

_HTTP_TIMEOUT = 10
_discovery_cache: dict | None = None


def je_zapnuto() -> bool:
    """OIDC je aktivní jen s kompletní konfigurací — půlka nastavení = vypnuto."""
    return bool(ISSUER and CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)


# --------------------------------------------------------------------------
# HTTP + discovery
# --------------------------------------------------------------------------
def _http_json(url: str, data: dict | None = None) -> dict:
    telo = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=telo, headers={
        'Accept': 'application/json',
        'User-Agent': 'JIPka-intranet',
    })
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _discovery() -> dict:
    """Endpointy (authorize/token) si stáhneme z well-known dokumentu.
    Cache na modulu — mění se řádově jednou za roky, restart to obnoví."""
    global _discovery_cache
    if _discovery_cache is None:
        _discovery_cache = _http_json(ISSUER + '/.well-known/openid-configuration')
    return _discovery_cache


# --------------------------------------------------------------------------
# PKCE + ID token
# --------------------------------------------------------------------------
def vytvor_pkce() -> tuple[str, str]:
    """(verifier, challenge) — S256 dle RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    otisk = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(otisk).decode('ascii').rstrip('=')
    return verifier, challenge


def rozbal_id_token(id_token: str) -> dict:
    """Payload ID tokenu jako dict.

    ponytail: podpis NEOVĚŘUJEME (žádná JWKS, žádná krypto závislost).
    Legitimní jen proto, že token bereme přímo z token endpointu přes ověřené
    TLS — OIDC Core 3.1.3.7 to pro code flow povoluje. Strop: kdyby se tokeny
    kdy braly z jiného kanálu (implicit/hybrid, front-channel), MUSÍ sem přijít
    ověření podpisu přes JWKS.
    """
    cast = id_token.split('.')[1]
    cast += '=' * (-len(cast) % 4)
    return json.loads(base64.urlsafe_b64decode(cast).decode('utf-8'))


def overit_claims(claims: dict, nonce: str, ted: float | None = None) -> str:
    """Vrátí '' když je token v pořádku, jinak důvod odmítnutí."""
    ted = time.time() if ted is None else ted
    if (claims.get('iss') or '').rstrip('/') != ISSUER:
        return 'issuer'
    aud = claims.get('aud')
    if not isinstance(aud, str):
        aud = (list(aud) or [None])[0] if aud else None
    if aud != CLIENT_ID:
        return 'audience'
    try:
        if float(claims.get('exp') or 0) <= ted:
            return 'expirace'
    except (TypeError, ValueError):
        return 'expirace'
    if nonce and claims.get('nonce') != nonce:
        return 'nonce'
    return ''


def email_z_claims(claims: dict) -> str:
    """Entra posílá `email` jen když má uživatel vyplněný mail atribut —
    proto fallback na `preferred_username` a `upn`."""
    for klic in ('email', 'preferred_username', 'upn'):
        hodnota = (claims.get(klic) or '').strip()
        if '@' in hodnota:
            return hodnota.lower()
    return ''


def _vymen_kod(code: str, verifier: str) -> dict:
    return _http_json(_discovery()['token_endpoint'], {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code_verifier': verifier,
    })


def _klient_info(request: Request) -> tuple[str, str]:
    """(IP, popis zařízení) z hlaviček — v callbacku není NiceGUI klient."""
    ip = ''
    device = ''
    try:
        xff = request.headers.get('x-forwarded-for', '')
        if xff:
            ip = xff.split(',')[0].strip()
        if not ip:
            ip = (request.headers.get('x-real-ip', '') or '').strip()
        if not ip:
            ip = getattr(getattr(request, 'client', None), 'host', '') or ''
        device = intranet_logger.popis_zarizeni(request.headers.get('user-agent', ''))
    except Exception:
        pass
    return ip, device


def _zpet_s_chybou(zprava: str) -> RedirectResponse:
    try:
        app.storage.user['oidc_chyba'] = zprava
    except Exception:
        pass
    return RedirectResponse('/')


# --------------------------------------------------------------------------
# Routy
# --------------------------------------------------------------------------
@app.get('/auth/login')
async def oidc_login(request: Request):
    if not je_zapnuto():
        return RedirectResponse('/')

    # ?brana=<nazev> = druhé ověření přihlášeného uživatele pro citlivý modul
    # (viz intranet_session.BRANY). Neznámá brána / nepřihlášený = běžné přihlášení.
    brana = request.query_params.get('brana', '')
    if brana not in intranet_session.BRANY or not app.storage.user.get('user_id'):
        brana = ''

    verifier, challenge = vytvor_pkce()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    app.storage.user.update({
        'oidc_state': state,
        'oidc_verifier': verifier,
        'oidc_nonce': nonce,
        'oidc_brana': brana,
    })

    try:
        disc = await asyncio.to_thread(_discovery)
    except Exception as e:
        intranet_logger.log_activity('Systém', 'OIDC', f'Discovery selhalo: {e}')
        if brana:
            app.storage.user['brana_chyba'] = 'Ověření přes firemní účet je dočasně nedostupné.'
            return RedirectResponse(intranet_session.BRANY[brana])
        return _zpet_s_chybou('Přihlášení přes firemní účet je dočasně nedostupné.')

    parametry = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'response_mode': 'query',
        'scope': 'openid profile email',
        'state': state,
        'nonce': nonce,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }

    # login_hint → IdP rovnou nabídne konkrétní účet místo obrazovky „Vyberte účet".
    # Zdroj: `oidc_hint` (uložený po předchozím přihlášení přes firemní účet), jinak
    # e-mail ze „Zapamatovat si mě". Jen nápověda, uživatel může účet v Entra přepnout.
    try:
        hint = str(app.storage.user.get('oidc_hint')
                   or app.storage.user.get('login_email') or '').strip()
        if brana:
            hint = str(app.storage.user.get('user_email') or '').strip()
        if '@' in hint:
            parametry['login_hint'] = hint
    except Exception:
        pass
    if brana:
        # Brána musí ověřit člověka u počítače, ne jen platnou SSO cookie u Microsoftu.
        parametry['prompt'] = 'login'

    dotaz = urllib.parse.urlencode(parametry)
    return RedirectResponse(disc['authorization_endpoint'] + '?' + dotaz)


@app.get('/auth/callback')
async def oidc_callback(request: Request):
    if not je_zapnuto():
        return RedirectResponse('/')

    ceka_state = app.storage.user.pop('oidc_state', '')
    verifier = app.storage.user.pop('oidc_verifier', '')
    nonce = app.storage.user.pop('oidc_nonce', '')
    brana = app.storage.user.pop('oidc_brana', '')
    if brana not in intranet_session.BRANY:
        brana = ''

    def _chyba(zprava: str) -> RedirectResponse:
        if not brana:
            return _zpet_s_chybou(zprava)
        try:
            app.storage.user['brana_chyba'] = zprava
        except Exception:
            pass
        return RedirectResponse(intranet_session.BRANY[brana])

    chyba = request.query_params.get('error_description') or request.query_params.get('error')
    if chyba:
        intranet_logger.log_activity('Systém', 'OIDC', f'IdP vrátil chybu: {chyba}')
        return _chyba('Ověření přes firemní účet se nezdařilo.')

    code = request.query_params.get('code', '')
    prisel_state = request.query_params.get('state', '')
    # CSRF: state musí sedět na to, co jsme vydali do TÉTO relace
    if not code or not ceka_state or not secrets.compare_digest(prisel_state, ceka_state):
        intranet_logger.log_activity('Systém', 'OIDC', 'Callback odmítnut — neplatný state')
        return _chyba('Přihlášení vypršelo, zkuste to prosím znovu.')

    try:
        odpoved = await asyncio.to_thread(_vymen_kod, code, verifier)
        claims = rozbal_id_token(odpoved['id_token'])
    except Exception as e:
        intranet_logger.log_activity('Systém', 'OIDC', f'Výměna kódu selhala: {e}')
        return _chyba('Přihlášení přes firemní účet se nezdařilo.')

    duvod = overit_claims(claims, nonce)
    if duvod:
        intranet_logger.log_activity('Systém', 'OIDC', f'ID token odmítnut ({duvod})')
        return _chyba('Přihlášení přes firemní účet se nezdařilo.')

    email = email_z_claims(claims)
    if not email:
        return _chyba('Firemní účet nemá e-mail, přihlaste se heslem.')

    if brana:
        # Druhé ověření: firemní účet musí patřit TOMU, kdo je přihlášený.
        prihlaseny = str(app.storage.user.get('user_email') or '').strip().lower()
        jmeno = str(app.storage.user.get('user_name') or prihlaseny)
        if not prihlaseny or email.strip().lower() != prihlaseny:
            intranet_logger.log_activity(jmeno, 'Druhé ověření',
                                         f'Odmítnuto ({brana}): firemní účet {email} nepatří přihlášenému')
            return _chyba('Firemní účet nepatří přihlášenému uživateli.')
        intranet_session.odemkni_branu(brana)
        intranet_logger.log_activity(jmeno, 'Druhé ověření', f'Odemčeno přes M365 ({brana})')
        return RedirectResponse(intranet_session.BRANY[brana])

    id_u, jmeno, potiz = await asyncio.to_thread(intranet_data.najdi_uzivatele_dle_emailu, email)
    if not id_u:
        intranet_logger.log_activity('Systém', 'OIDC', f'Odmítnut účet {email}: {potiz}')
        return _zpet_s_chybou(potiz or 'Účet v intranetu neexistuje.')

    ip, device = _klient_info(request)
    # Bez auto-provisioningu: účet musí v intranetu existovat (práva, útvary).
    intranet_session.zahaj_relaci(id_u, email, jmeno)
    intranet_session.zaznamenej_klienta(email, jmeno, ip, device)
    # Zapamatovat účet pro `login_hint` při dalším přihlášení → uživatel už neuvidí
    # u Microsoftu výběr účtu. Ukládá se JEN e-mail, nikdy token ani heslo.
    try:
        app.storage.user['oidc_hint'] = email
    except Exception:
        pass
    # Návrat na původně kliknutou URL (deep-link z e-mailu, /asm?pripad=206).
    # Jen lokální cesta — nikdy cizí doména (open redirect).
    try:
        cil = str(app.storage.user.pop('po_prihlaseni', '') or '/')
    except Exception:
        cil = '/'
    if not cil.startswith('/') or cil.startswith('//'):
        cil = '/'
    return RedirectResponse(cil)
