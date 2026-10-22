"""Chráněné servírování nahraných souborů.

`app.add_static_files()` pošle soubor komukoli, kdo zná URL — bez ohledu na
přihlášení. Jména nahraných souborů jsou přitom často odvoditelná
(např. podle ID záznamu nebo data exportu), takže veřejný mount = stažení
firemních dokumentů bez přihlášení.

Všechny adresáře s uploady proto jdou přes `chranene_soubory()`, která před
odesláním souboru ověří relaci. Veřejné zůstávají jen soubory z
`_STATIC_WHITELIST` v intranet.py (pozadí přihlašovací stránky, logo, ikony) —
ty musí být dostupné před přihlášením.
"""
from __future__ import annotations

import os

from fastapi.responses import FileResponse, PlainTextResponse
from nicegui import app

import intranet_data

# Prefixy už zaregistrované (ukol_prilohy/projekt_prilohy se mountují ze dvou modulů).
_ZAREGISTROVANE: set[str] = set()


def je_prihlasen() -> bool:
    """True, pokud request patří přihlášené relaci.

    Chyba (mimo request kontext, bez session cookie, nedostupné úložiště) =
    nepřihlášen. Fail closed: raději soubor nepošleme.
    """
    try:
        return bool(app.storage.user.get('user_id'))
    except Exception:
        return False


def ma_pravo(pravo: str | tuple[str, ...]) -> bool:
    """True, pokud přihlášený a aktivní uživatel requestu má `pravo`
    (u n-tice aspoň jedno z nich).

    Čte práva z DB při každém requestu, takže odebrání práva platí hned.
    Chyba nebo výpadek DB (= prázdná práva) → False. Fail closed.
    """
    hledana = {pravo} if isinstance(pravo, str) else set(pravo)
    try:
        prava, aktivni = intranet_data.ziskej_prava_a_aktivitu(app.storage.user.get('user_id'))
        return aktivni and any(p.lower() in hledana for p in prava)
    except Exception:
        return False


def chranene_soubory(url_prefix: str, adresar: str,
                     pravo: str | tuple[str, ...] | None = None,
                     kontrola=None) -> None:
    """Servíruje obsah `adresar` na `url_prefix` pouze přihlášeným uživatelům
    (se zadaným `pravo` jen těm, kdo ho mají; n-tice = stačí kterékoli).
    `kontrola` = volitelná funkce bez argumentů, další podmínka (např. odemčená
    druhá brána); výjimka = zamítnuto."""
    url_prefix = '/' + url_prefix.strip('/')
    if url_prefix in _ZAREGISTROVANE:
        return
    _ZAREGISTROVANE.add(url_prefix)

    os.makedirs(adresar, exist_ok=True)
    koren = os.path.realpath(adresar)

    @app.get(url_prefix + '/{cesta:path}',
             include_in_schema=False,
             name=f'chranene_{url_prefix.strip("/")}')
    def _servuj(cesta: str):
        # Nepřihlášenému hlásíme 404, ne 401 — ať se z odpovědi nedá zjistit,
        # jestli soubor existuje.
        if not je_prihlasen() or (pravo and not ma_pravo(pravo)):
            return PlainTextResponse('Nenalezeno', status_code=404)
        if kontrola:
            try:
                povoleno = bool(kontrola())
            except Exception:
                povoleno = False
            if not povoleno:
                return PlainTextResponse('Nenalezeno', status_code=404)
        soubor = os.path.realpath(os.path.join(koren, cesta))
        if soubor != koren and not soubor.startswith(koren + os.sep):
            return PlainTextResponse('Nenalezeno', status_code=404)  # ../ traversal
        if not os.path.isfile(soubor):
            return PlainTextResponse('Nenalezeno', status_code=404)
        return FileResponse(soubor)
