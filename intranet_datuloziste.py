"""Datové úložiště — sekce → složky → podsložky se soubory (max 50 MB na soubor).

Vzhled po vzoru SharePointu: vlevo rozbalovací strom složek, vpravo drobečková
navigace a seznam Název / Změněno / Změnil / Velikost.

Kdo co vidí:
  * Správce Dat (právo `datuloziste_spravce`, admin `vse`) vidí vše, pravým
    tlačítkem myši (kdekoli v boční liště i v seznamu) zakládá sekce a složky
    a řídí přístup ke složkám. Ctrl+klikem vybere víc složek / souborů a smaže
    je naráz (Delete nebo pravé tlačítko). Složka se maže i s obsahem a přístupy
    k ní zaniknou — nová složka stejného jména nic nezdědí.
  * Ostatní vidí jen složky, do kterých dostali přístup, a vše pod nimi.
    Cesta k nim se zviditelní sama a je jen průchozí: v nadřazené složce
    vidí jen svou větev — ne sourozence (Novák nevidí Novotného) ani soubory.

Soubory se otevírají klikem jako v SharePointu: dokumenty Office v desktopovém
Wordu / Excelu / PowerPointu přes WebDAV — uložení v aplikaci (Ctrl+S) zapíše novou
verzi sem; PDF, obrázky a text v nové záložce prohlížeče; ostatní se stáhnou.

Přístup se ověřuje v DB při každé akci (otevření složky, nahrání, stažení,
smazání, každý request Office). ID složky z URL ani z prohlížeče se nikdy nebere
bez kontroly.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import datetime
import email.utils
import functools
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import time
import unicodedata
import uuid
from urllib.parse import quote
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from fastapi import Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from nicegui import app, background_tasks, ui
from starlette.datastructures import UploadFile

import intranet_data
import intranet_emaily
import intranet_logger

PRAVO_SPRAVCE = 'datuloziste_spravce'
MAX_VELIKOST = 50 * 1024 * 1024          # 50 MB na soubor
ADRESAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Datove_Uloziste')
_LOG = 'Datové úložiště'
_SLOZKY = 'SELECT id, parent_id, nazev, vytvoril, vytvoreno FROM datuloziste_slozky'

_DB_HOTOVO = False


# =========================================================
# DATABÁZE (blokující — volat přes asyncio.to_thread)
# =========================================================
def inicializace_db():
    global _DB_HOTOVO
    if _DB_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS datuloziste_slozky (
                id INT AUTO_INCREMENT PRIMARY KEY,
                parent_id INT NULL,
                nazev VARCHAR(255) NOT NULL,
                vytvoril VARCHAR(255),
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_parent (parent_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS datuloziste_soubory (
                id INT AUTO_INCREMENT PRIMARY KEY,
                slozka_id INT NOT NULL,
                nazev VARCHAR(255) NOT NULL,
                disk VARCHAR(64) NOT NULL,
                velikost BIGINT NOT NULL DEFAULT 0,
                nahral VARCHAR(255),
                nahrano DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_slozka (slozka_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS datuloziste_pristupy (
                slozka_id INT NOT NULL,
                user_id INT NOT NULL,
                udelil VARCHAR(255),
                udeleno DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (slozka_id, user_id),
                INDEX idx_user (user_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # Přístupy ke složkám, které už neexistují, pryč: starší MySQL/MariaDB po restartu
        # znovu přidělí nejvyšší smazané ID a nová složka by takový přístup zdědila.
        cur.execute('DELETE FROM datuloziste_pristupy '
                    'WHERE slozka_id NOT IN (SELECT id FROM datuloziste_slozky)')
        conn.commit()
        cur.close()
        _DB_HOTOVO = True
    except Exception as e:
        print(f'[datuloziste] inicializace_db: {e}')
    finally:
        conn.close()


def _sql(dotaz, parametry=(), vysledek=None):
    """Jeden dotaz ve vlastním spojení.
    vysledek: 'vse' | 'jeden' | None (= commit, vrací (lastrowid, rowcount))."""
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute(dotaz, parametry)
        if vysledek == 'vse':
            return cur.fetchall()
        if vysledek == 'jeden':
            return cur.fetchone()
        conn.commit()
        return cur.lastrowid, cur.rowcount
    finally:
        conn.close()


def _mista(n):
    return ', '.join(['%s'] * n)


# =========================================================
# PŘÍSTUP
# =========================================================
def je_spravce(prava) -> bool:
    return 'vse' in prava or PRAVO_SPRAVCE in prava


def _podstrom(rodice: dict, korene) -> set:
    """Existující složky z `korene` a všechny složky pod nimi."""
    deti: dict = {}
    for sid, rodic in rodice.items():
        deti.setdefault(rodic, []).append(sid)
    vysledek, fronta = set(), [k for k in korene if k in rodice]
    while fronta:
        sid = fronta.pop()
        if sid not in vysledek:
            vysledek.add(sid)
            fronta.extend(deti.get(sid, ()))
    return vysledek


def _predci(rodice: dict, sid) -> list:
    """ID nadřazených složek od rodiče až po sekci."""
    predci, rodic = [], rodice.get(sid)
    while rodic is not None and rodic not in predci and len(predci) <= len(rodice):
        predci.append(rodic)
        rodic = rodice.get(rodic)
    return predci


def viditelnost(rodice: dict, granty) -> tuple[set, set]:
    """rodice: id složky → id rodiče (None = sekce); granty: ID udělených složek.

    Vrací (plne, pruchozi): plne = udělené složky a vše pod nimi (podsložky
    i soubory), pruchozi = jejich předci — z nich je vidět jen větev dolů."""
    plne = _podstrom(rodice, granty)
    pruchozi = {p for g in granty if g in rodice for p in _predci(rodice, g)}
    return plne, pruchozi - plne


def _viditelne(user_id, spravce):
    """(slozky {id: řádek}, plne, pruchozi) — čerstvě z DB."""
    slozky = {r['id']: r for r in _sql(_SLOZKY, vysledek='vse')}
    if spravce:
        return slozky, set(slozky), set()
    granty = [r['slozka_id'] for r in _sql(
        'SELECT slozka_id FROM datuloziste_pristupy WHERE user_id = %s', (user_id,), 'vse')]
    plne, pruchozi = viditelnost({k: v['parent_id'] for k, v in slozky.items()}, granty)
    return slozky, plne, pruchozi


def _ma_plny_pristup(user_id, spravce, sid) -> bool:
    return sid in _viditelne(user_id, spravce)[1]


def ma_pristup_k_modulu(user_id, prava) -> bool:
    """Brána modulu: správce, nebo kdo má přístup aspoň do jedné složky."""
    if je_spravce(prava):
        return True
    if not user_id:
        return False
    try:
        return bool(_sql('SELECT 1 AS x FROM datuloziste_pristupy WHERE user_id = %s LIMIT 1',
                         (user_id,), 'jeden'))
    except Exception:
        return False        # tabulky ještě nejsou / DB nedostupná


def _cesta(slozky, sid):
    """Řádky složek od sekce po `sid`."""
    cesta = []
    while sid in slozky and len(cesta) <= len(slozky):
        cesta.insert(0, slozky[sid])
        sid = slozky[sid]['parent_id']
    return cesta


def _nacti_pohled(user_id, spravce, sid):
    """Data pro vykreslení složky (sid None = kořen se sekcemi).
    False = složka neexistuje nebo do ní uživatel nesmí."""
    slozky, plne, pruchozi = _viditelne(user_id, spravce)
    vidi = plne | pruchozi
    if sid is not None and sid not in vidi:
        return False
    strom = [dict(slozky[i], plny=i in plne) for i in vidi]     # jen to, co smí vidět
    pocty: dict = {}
    for s in strom:
        pocty[s['parent_id']] = pocty.get(s['parent_id'], 0) + 1
    deti = [s for s in strom if s['parent_id'] == sid]
    s_obsahem = [s['id'] for s in deti if s['plny']]           # soubory průchozích nepočítat
    if s_obsahem:
        for r in _sql('SELECT slozka_id, COUNT(*) AS n FROM datuloziste_soubory '
                      f'WHERE slozka_id IN ({_mista(len(s_obsahem))}) GROUP BY slozka_id',
                      tuple(s_obsahem), 'vse'):
            pocty[r['slozka_id']] = pocty.get(r['slozka_id'], 0) + int(r['n'])
    soubory = None      # None = soubory nevidí (kořen / průchozí složka)
    if sid is not None and sid in plne:
        soubory = _sql('SELECT id, nazev, velikost, nahral, nahrano FROM datuloziste_soubory '
                       'WHERE slozka_id = %s ORDER BY nazev', (sid,), 'vse')
    return {'id': sid, 'cesta': _cesta(slozky, sid), 'strom': strom, 'soubory': soubory,
            'slozky': [dict(s, pocet=pocty.get(s['id'], 0)) for s in deti]}


def _pristupy(sid):
    """Kdo má do složky přístup: přímo (jde odebrat) i zděděně z nadřazené složky."""
    slozky = {r['id']: r for r in _sql(_SLOZKY, vysledek='vse')}
    cesta = [s['id'] for s in _cesta(slozky, sid)]
    if not cesta:
        return []
    radky = _sql('SELECT p.slozka_id, p.user_id, u.name, u.surname, u.email '
                 'FROM datuloziste_pristupy p JOIN user u ON u.iduser = p.user_id '
                 f'WHERE p.slozka_id IN ({_mista(len(cesta))}) '
                 'ORDER BY u.surname, u.name', tuple(cesta), 'vse')
    for r in radky:
        r['zdedeno'] = None if r['slozka_id'] == sid else slozky[r['slozka_id']]['nazev']
    return radky


def _najdi_uzivatele(adresa):
    uid, jmeno, chyba = intranet_data.najdi_uzivatele_dle_emailu(adresa)
    if uid and intranet_data.je_skryty_ucet(email=adresa):
        return None, None, 'Účet v intranetu neexistuje.'
    return uid, jmeno, chyba


# =========================================================
# SLOŽKY A SOUBORY
# =========================================================
def _disk(nazev):
    return os.path.join(ADRESAR, os.path.basename(nazev))


def _vytvor_slozku(rodic_id, nazev, kdo):
    """True = založena, jinak text chyby pro uživatele."""
    if _sql('SELECT 1 AS x FROM datuloziste_slozky WHERE parent_id <=> %s AND nazev = %s',
            (rodic_id, nazev), 'jeden'):
        return 'Složka s tímto názvem tu už je.'
    if rodic_id is None:
        _sql('INSERT INTO datuloziste_slozky (parent_id, nazev, vytvoril) VALUES (NULL, %s, %s)',
             (nazev, kdo))
        return True
    # INSERT … SELECT čte rodiče se zámkem: souběžně mazaný rodič = složka nevznikne.
    _, pocet = _sql('INSERT INTO datuloziste_slozky (parent_id, nazev, vytvoril) '
                    'SELECT id, %s, %s FROM datuloziste_slozky WHERE id = %s', (nazev, kdo, rodic_id))
    return True if pocet else 'Nadřazená složka mezitím zanikla.'


def _pridej_pristup(sid, uid, kdo):
    """'pridan' | 'ma' (přístup už má) | 'neni' (složka mezitím zanikla).
    INSERT … SELECT: přístup jen k existující složce — ke smazané nikdy."""
    _, pocet = _sql('INSERT IGNORE INTO datuloziste_pristupy (slozka_id, user_id, udelil) '
                    'SELECT id, %s, %s FROM datuloziste_slozky WHERE id = %s', (uid, kdo, sid))
    if pocet:
        return 'pridan'
    return 'ma' if _sql('SELECT 1 AS x FROM datuloziste_slozky WHERE id = %s', (sid,), 'jeden') else 'neni'


def _soubor(user_id, spravce, soubor_id):
    """Řádek souboru — jen když má uživatel plný přístup do jeho složky."""
    s = _sql('SELECT id, slozka_id, nazev, disk FROM datuloziste_soubory WHERE id = %s',
             (soubor_id,), 'jeden')
    return s if s and _ma_plny_pristup(user_id, spravce, s['slozka_id']) else None


def _plan_mazani(user_id, spravce, klice):
    """Co přesně smaže výběr `klice` ('s12' složka, 'f7' soubor) — jen čtení, pro potvrzení.

    Bere jen to, co uživatel smí: složky jen správce (i s celým obsahem), soubory
    kdo má plný přístup do jejich složky. Složka uvnitř jiné vybrané složky
    i soubor v mazané složce padnou s ní, samostatně se nepočítají."""
    slozky, plne, _ = _viditelne(user_id, spravce)
    rodice = {k: v['parent_id'] for k, v in slozky.items()}
    vybrane = {i for i in (_id(k, 's') for k in klice) if i in slozky} if spravce else set()
    vrchni = sorted((i for i in vybrane if not set(_predci(rodice, i)) & vybrane),
                    key=lambda i: _abc(slozky[i]['nazev']))
    podstromy = {i: _podstrom(rodice, [i]) for i in vrchni}
    vse = set().union(*podstromy.values())
    pocty = {'soubory': {}, 'pristupy': {}}
    if vse:
        for tab, sl in (('soubory', 'datuloziste_soubory'), ('pristupy', 'datuloziste_pristupy')):
            for r in _sql(f'SELECT slozka_id, COUNT(*) AS n FROM {sl} '
                          f'WHERE slozka_id IN ({_mista(len(vse))}) GROUP BY slozka_id', tuple(vse), 'vse'):
                pocty[tab][r['slozka_id']] = int(r['n'])
    polozky = [{'typ': 0, 'id': i, 'nazev': slozky[i]['nazev'], 'sekce': slozky[i]['parent_id'] is None,
                'misto': _cesta_text(_cesta(slozky, slozky[i]['parent_id'])),
                'podslozky': len(podstromy[i]) - 1,
                'soubory': sum(pocty['soubory'].get(s, 0) for s in podstromy[i]),
                'pristupy': sum(pocty['pristupy'].get(s, 0) for s in podstromy[i])} for i in vrchni]
    soubory_ids = list(dict.fromkeys(i for i in (_id(k, 'f') for k in klice) if i is not None))
    if soubory_ids:
        radky = _sql(f'SELECT id, slozka_id, nazev FROM datuloziste_soubory '
                     f'WHERE id IN ({_mista(len(soubory_ids))})', tuple(soubory_ids), 'vse')
        polozky += [{'typ': 1, 'id': f['id'], 'nazev': f['nazev'],
                     'misto': _cesta_text(_cesta(slozky, f['slozka_id']))}
                    for f in sorted(radky, key=lambda f: _abc(f['nazev']))
                    if f['slozka_id'] in plne and f['slozka_id'] not in vse]
    return {'polozky': polozky, 'slozky': vse}


def _smaz_polozky(user_id, spravce, slozky_ids, soubory_ids):
    """Smaže složky (i s celým obsahem — jen správce) a soubory v jedné transakci
    a zruší přístupy ke smazaným složkám. Vrací (ID smazaných složek, ID smazaných souborů
    — i těch uvnitř složek, počet zrušených přístupů).

    Složky se zamykají vrstvu po vrstvě (FOR UPDATE = aktuální stav, ne snímek):
    souběžné založení podsložky, nahrání souboru nebo udělení přístupu do mazané
    složky (INSERT … SELECT nad ní) počká na konec transakce a pak neprojde."""
    slozky_ids = list(slozky_ids) if spravce else []
    soubory_ids = list(soubory_ids)
    plne = set() if spravce or not soubory_ids else _viditelne(user_id, False)[1]
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    slozky, disky, pristupy = set(), {}, 0
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        vrstva = []
        if slozky_ids:
            cur.execute(f'SELECT id FROM datuloziste_slozky WHERE id IN ({_mista(len(slozky_ids))}) '
                        'FOR UPDATE', tuple(slozky_ids))
            vrstva = [r['id'] for r in cur.fetchall()]
        while vrstva:
            slozky.update(vrstva)
            cur.execute(f'SELECT id FROM datuloziste_slozky WHERE parent_id IN ({_mista(len(vrstva))}) '
                        'FOR UPDATE', tuple(vrstva))
            vrstva = [r['id'] for r in cur.fetchall() if r['id'] not in slozky]
        if slozky:
            cur.execute(f'SELECT id, disk FROM datuloziste_soubory WHERE slozka_id IN ({_mista(len(slozky))}) '
                        'FOR UPDATE', tuple(slozky))
            disky.update((r['id'], r['disk']) for r in cur.fetchall())
        if soubory_ids:
            cur.execute(f'SELECT id, slozka_id, disk FROM datuloziste_soubory '
                        f'WHERE id IN ({_mista(len(soubory_ids))}) FOR UPDATE', tuple(soubory_ids))
            disky.update((r['id'], r['disk']) for r in cur.fetchall() if spravce or r['slozka_id'] in plne)
        if disky:
            cur.execute(f'DELETE FROM datuloziste_soubory WHERE id IN ({_mista(len(disky))})', tuple(disky))
        if slozky:
            cur.execute(f'DELETE FROM datuloziste_pristupy WHERE slozka_id IN ({_mista(len(slozky))})',
                        tuple(slozky))
            pristupy = cur.rowcount
            cur.execute(f'DELETE FROM datuloziste_slozky WHERE id IN ({_mista(len(slozky))})', tuple(slozky))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    for disk in disky.values():     # až po commitu — při chybě DB soubory zůstanou celé
        try:
            os.remove(_disk(disk))
        except OSError:
            pass
    return slozky, set(disky), pristupy


def _uloz_soubor(zdroj, slozka_id, nazev, kdo):
    """Zkopíruje nahraný soubor na disk a zapíše ho do DB. Vrací velikost v bajtech,
    nebo None, když cílová složka mezitím zanikla (soubor z disku zase zmizí)."""
    disk = uuid.uuid4().hex
    os.makedirs(ADRESAR, exist_ok=True)
    zdroj.seek(0)
    with open(_disk(disk), 'wb') as cil:
        shutil.copyfileobj(zdroj, cil, 1024 * 1024)
        velikost = cil.tell()
    try:
        # INSERT … SELECT: zápis jen do existující složky (souběžné smazání ji zamkne).
        _, pocet = _sql('INSERT INTO datuloziste_soubory (slozka_id, nazev, disk, velikost, nahral) '
                        'SELECT id, %s, %s, %s, %s FROM datuloziste_slozky WHERE id = %s',
                        (nazev, disk, velikost, kdo, slozka_id))
    except Exception:
        os.remove(_disk(disk))
        raise
    if not pocet:
        os.remove(_disk(disk))
        return None
    return velikost


def _posli_email(adresa, cesta, sid, udelil):
    odkaz = f'{intranet_data.APP_URL}/uloziste?slozka={int(sid)}'
    text = (f'Dobrý den,\n\nv Datovém úložišti vám byl udělen přístup do složky „{cesta}“ '
            f'(udělil/a {udelil}).\n\nOtevřít složku: {odkaz}')
    try:
        intranet_emaily.odesli_upozorneni_email(
            adresa, f'Datové úložiště – přístup do složky {cesta.rsplit(" / ", 1)[-1]}', text)
    except Exception as e:
        print(f'[datuloziste] e-mail {adresa}: {e}')


# =========================================================
# HTTP — stažení a nahrání; přístup se ověřuje při každém requestu (relace + DB)
# =========================================================
def _opravneni(user_id):
    """None = nesmí nic (modul vypnut, neaktivní účet); jinak True/False = je správce."""
    if not user_id or not intranet_data.nacti_nastaveni_intranetu().get('datuloziste_zapnuty', True):
        return None
    prava, aktivni = intranet_data.ziskej_prava_a_aktivitu(user_id)
    return je_spravce({p.lower() for p in prava}) if aktivni else None


def _soubor_ke_stazeni(user_id, soubor_id):
    try:
        spravce = _opravneni(user_id)
        s = None if spravce is None else _soubor(user_id, spravce, soubor_id)
        return s if s and os.path.isfile(_disk(s['disk'])) else None
    except Exception:
        return None         # fail closed


def _cil_nahrani(user_id, slozka_id):
    """Text cesty cílové složky, nebo None, když do ní uživatel nahrávat nesmí."""
    try:
        spravce = _opravneni(user_id)
        if spravce is None:
            return None
        slozky, plne, _ = _viditelne(user_id, spravce)
        return _cesta_text(_cesta(slozky, slozka_id)) if slozka_id in plne else None
    except Exception:
        return None         # fail closed


def _relace():
    try:
        return app.storage.user.get('user_id'), app.storage.user.get('user_name') or 'Neznámý'
    except Exception:
        return None, None   # bez relace = nepřihlášen


@app.get('/uloziste/soubor/{soubor_id}')
async def _stazeni(soubor_id: int):
    user_id, _ = _relace()
    s = await asyncio.to_thread(_soubor_ke_stazeni, user_id, soubor_id) if user_id else None
    if not s:
        return PlainTextResponse('Nenalezeno', status_code=404)
    return FileResponse(_disk(s['disk']), filename=s['nazev'],
                        media_type='application/octet-stream')


@app.post('/uloziste/nahrat/{slozka_id}')
async def _nahrani(slozka_id: int, request: Request):
    """Cílová složka je v URL každého uploadu — soubor nemůže skončit jinde,
    ani když uživatel během nahrávání přejde do jiné složky."""
    user_id, kdo = _relace()
    cesta = await asyncio.to_thread(_cil_nahrani, user_id, slozka_id) if user_id else None
    if cesta is None:
        return PlainTextResponse('Nenalezeno', status_code=404)
    try:
        delka = int(request.headers.get('content-length') or -1)
    except ValueError:
        delka = -1
    if not 0 < delka <= MAX_VELIKOST + 64 * 1024:     # i bez Content-Length (chunked) = odmítnout
        return PlainTextResponse('Soubor je větší než 50 MB.', status_code=413)
    async with request.form(max_files=1, max_fields=1) as form:
        soubory = [f for f in form.values() if isinstance(f, UploadFile)]
        if len(soubory) != 1:
            return PlainTextResponse('Neplatný požadavek.', status_code=400)
        f = soubory[0]
        if (f.size or 0) > MAX_VELIKOST:
            return PlainTextResponse('Soubor je větší než 50 MB.', status_code=413)
        nazev = os.path.basename((f.filename or '').replace('\\', '/')).strip()[:255] or 'soubor'
        velikost = await asyncio.to_thread(_uloz_soubor, f.file, slozka_id, nazev, kdo)
    if velikost is None:        # složku mezitím někdo smazal
        return PlainTextResponse('Nenalezeno', status_code=404)
    intranet_logger.log_activity(
        kdo, _LOG, f'Nahrán soubor „{nazev}“ ({_velikost(velikost)}) do „{cesta}“')
    return {'upload': 'success'}


# =========================================================
# OTEVÍRÁNÍ SOUBORŮ — náhled v prohlížeči, úpravy v Office (WebDAV)
# =========================================================
# Jako v SharePointu: klik na dokument Office ho otevře v desktopové aplikaci (URI
# schéma ms-excel:ofe|u|<url> = „Otevřít v aplikaci“). Ta soubor čte a ukládá přes
# WebDAV na /uloziste/dav/<token>/<název> — Ctrl+S v Excelu nahraje novou verzi sem.
# Office neposílá cookies relace, oprávnění proto nese URL: podepsaný token (soubor,
# uživatel, platnost). Sám nic nepovolí — přístup se ověřuje v DB při každém requestu,
# takže odebrání práva nebo smazání souboru platí okamžitě.
_OFFICE = {
    **dict.fromkeys(('doc', 'docx', 'docm', 'dot', 'dotx', 'dotm', 'rtf', 'odt'), 'ms-word'),
    **dict.fromkeys(('xls', 'xlsx', 'xlsm', 'xlsb', 'xlt', 'xltx', 'xltm', 'ods'), 'ms-excel'),
    **dict.fromkeys(('ppt', 'pptx', 'pptm', 'pps', 'ppsx', 'pot', 'potx', 'odp'), 'ms-powerpoint'),
}
_APLIKACE = {'ms-word': 'Word', 'ms-excel': 'Excel', 'ms-powerpoint': 'PowerPoint'}
# Co prohlížeč bezpečně zobrazí sám — otevře se v nové záložce. HTML ani SVG ne (skript
# ze stejného originu = XSS), ty se jen stahují; text jde vždy jako text/plain.
_NAHLED = {
    'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'gif': 'image/gif', 'webp': 'image/webp', 'bmp': 'image/bmp',
    'mp4': 'video/mp4', 'webm': 'video/webm', 'mp3': 'audio/mpeg', 'wav': 'audio/wav',
    'ogg': 'audio/ogg', 'm4a': 'audio/mp4',
    **dict.fromkeys(('txt', 'log', 'csv', 'md', 'json', 'xml'), 'text/plain'),
}
_DAV = '/uloziste/dav'
_DAV_METODY = ['OPTIONS', 'GET', 'HEAD', 'PUT', 'PROPFIND', 'PROPPATCH', 'LOCK', 'UNLOCK',
               'DELETE', 'MKCOL', 'COPY', 'MOVE', 'POST']
_DAV_HLAVICKY = {'DAV': '1, 2', 'MS-Author-Via': 'DAV',
                 'Allow': 'OPTIONS, GET, HEAD, PUT, PROPFIND, PROPPATCH, LOCK, UNLOCK'}
_TOKEN_PLATNOST = 12 * 3600     # s — pak soubor otevře jen nový klik v intranetu
_ZAMEK_MAX = 3600               # s — Office zámek během úprav sám obnovuje
_MAX_XML = 64 * 1024            # tělo PROPFIND / PROPPATCH / LOCK
_RE_ZAMEK = re.compile(r'<(opaquelocktoken:[0-9a-fA-F-]{8,64})>')
# Zámky souborů otevřených v Office: soubor_id → {'token', 'uid', 'jmeno', 'do'}. Jen
# v paměti (mění se jen v event loopu) — po restartu je Office při dalším uložení
# nebo obnově převezme, rozpracované úpravy se neztratí.
_ZAMKY: dict = {}
_ZMENY: dict = {}               # slozka_id → čas (monotonic) posledního uložení / zámku z Office


def _pripona(nazev) -> str:
    return nazev.rsplit('.', 1)[-1].lower() if '.' in nazev else ''


def _mime(nazev) -> str:
    return mimetypes.guess_type('x.' + _pripona(nazev))[0] or 'application/octet-stream'


def _kodovani(cesta) -> str:
    """Znaková sada textu pro náhled: UTF-8, jinak Windows-1250 (české Windows)."""
    with open(cesta, 'rb') as f:
        zacatek = f.read(256 * 1024)
    if zacatek.startswith((b'\xff\xfe', b'\xfe\xff')):
        return 'utf-16'
    try:
        codecs.getincrementaldecoder('utf-8')().decode(zacatek)    # useknutý konec bloku nevadí
        return 'utf-8'
    except UnicodeDecodeError:
        return 'windows-1250'


@app.get('/uloziste/nahled/{soubor_id}/{nazev}')
async def _nahled(soubor_id: int, nazev: str):
    """Soubor přímo v prohlížeči (název v URL je jen kvůli titulku záložky)."""
    user_id, _ = _relace()
    s = await asyncio.to_thread(_soubor_ke_stazeni, user_id, soubor_id) if user_id else None
    if not s:
        return PlainTextResponse('Nenalezeno', status_code=404)
    typ = _NAHLED.get(_pripona(s['nazev']))
    if not typ:
        return FileResponse(_disk(s['disk']), filename=s['nazev'], media_type='application/octet-stream')
    if typ == 'text/plain':
        typ += '; charset=' + await asyncio.to_thread(_kodovani, _disk(s['disk']))
    return FileResponse(_disk(s['disk']), filename=s['nazev'], media_type=typ,
                        content_disposition_type='inline')


# ---------- token v URL pro Office ----------
@functools.cache
def _klic_tokenu() -> bytes:
    """Klíč podpisu — odvozený z tajemství relací, takže tokeny přežijí restart serveru.
    Bez něj (testy) náhodný, platný do restartu."""
    tajemstvi = os.environ.get('NICEGUI_SECRET', '').strip().encode() or secrets.token_bytes(32)
    return hmac.new(tajemstvi, b'datuloziste-dav', hashlib.sha256).digest()


def _podpis(data) -> bytes:
    return base64.urlsafe_b64encode(hmac.new(_klic_tokenu(), data.encode(), hashlib.sha256).digest()[:24])


def _token(soubor_id, user_id, ted=None) -> str:
    """Token do URL pro Office: soubor.uživatel.platnost.podpis."""
    data = f'{int(soubor_id)}.{int(user_id)}.{int((ted or time.time()) + _TOKEN_PLATNOST)}'
    return f'{data}.{_podpis(data).decode()}'


def _over_token(token, ted=None):
    """(soubor_id, user_id) — nebo None: podvržený, poškozený nebo prošlý token."""
    try:
        data, podpis = token.rsplit('.', 1)
        soubor_id, user_id, platnost = (int(x) for x in data.split('.'))
        platny = hmac.compare_digest(podpis.encode(), _podpis(data))
    except (AttributeError, ValueError):        # i UnicodeError
        return None
    return (soubor_id, user_id) if platny and platnost >= (ted or time.time()) else None


# ---------- DB (blokující) ----------
def _dav_kontext(soubor_id, user_id):
    """Soubor pro Office (+ cesta, jméno uživatele) — None, když k němu uživatel nesmí."""
    try:
        spravce = _opravneni(user_id)
        s = None if spravce is None else _sql(
            'SELECT id, slozka_id, nazev, disk, velikost, nahrano FROM datuloziste_soubory '
            'WHERE id = %s', (soubor_id,), 'jeden')
        if not s or not os.path.isfile(_disk(s['disk'])):
            return None
        slozky, plne, _ = _viditelne(user_id, spravce)
        if s['slozka_id'] not in plne:
            return None
        u = _sql('SELECT name, surname FROM user WHERE iduser = %s', (user_id,), 'jeden') or {}
        cesta = _cesta(slozky, s['slozka_id'])
        return dict(s, cesta=_cesta_text(cesta), slozka=cesta[-1]['nazev'] if cesta else '',
                    kdo=f"{u.get('name') or ''} {u.get('surname') or ''}".strip() or 'Neznámý')
    except Exception:
        return None         # fail closed


def _prepis_soubor(soubor_id, disk, velikost, kdo) -> bool:
    """Nová verze z Office: řádek souboru ukáže na nový soubor `disk`, starý se smaže.
    False = soubor mezitím někdo smazal (nová verze z disku zase zmizí)."""
    hotovo = None
    try:
        conn = intranet_data.get_db_connection()
        if not conn:
            raise RuntimeError('Databáze není dostupná.')
        try:
            cur = conn.cursor(dictionary=True, buffered=True)
            # FOR UPDATE: souběžné smazání souboru počká, nebo už proběhlo a řádek chybí.
            cur.execute('SELECT disk FROM datuloziste_soubory WHERE id = %s FOR UPDATE', (soubor_id,))
            stary = cur.fetchone()
            if stary:
                cur.execute('UPDATE datuloziste_soubory SET disk = %s, velikost = %s, nahral = %s, '
                            'nahrano = NOW() WHERE id = %s', (disk, velikost, kdo, soubor_id))
            conn.commit()
            hotovo = stary or False
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    finally:
        # Po zápisu je navíc stará verze, jinak (soubor smazán / chyba) ta nová.
        _smaz_z_disku(_disk(hotovo['disk'] if hotovo else disk))
    return bool(hotovo)


def _smaz_z_disku(cesta):
    try:
        os.remove(cesta)
    except OSError:
        pass


# ---------- WebDAV (jen to, co Office potřebuje) ----------
def _zamek(soubor_id):
    """Platný zámek souboru (prošlý zahodí), nebo None."""
    z = _ZAMKY.get(soubor_id)
    if z and z['do'] <= time.monotonic():
        del _ZAMKY[soubor_id]
        z = None
    return z


def _tokeny_zamku(hlavicka) -> set:
    """Tokeny zámků z hlavičky If / Lock-Token."""
    return set(_RE_ZAMEK.findall(hlavicka or ''))


def _timeout(hlavicka) -> int:
    """Platnost zámku ze žádosti (Second-N / Infinite) v mezích 60 s … _ZAMEK_MAX."""
    for cast in (hlavicka or '').split(','):
        cast = cast.strip().lower()
        if cast.startswith('second-'):
            try:
                return max(60, min(int(cast[7:]), _ZAMEK_MAX))
            except ValueError:
                pass
    return _ZAMEK_MAX


def _http_datum(dt) -> str:
    return email.utils.formatdate(dt.timestamp() if dt else 0, usegmt=True)


def _iso_datum(dt) -> str:
    utc = datetime.datetime.fromtimestamp(dt.timestamp() if dt else 0, datetime.timezone.utc)
    return utc.strftime('%Y-%m-%dT%H:%M:%SZ')


def _etag(disk) -> str:
    return f'"{disk}"'      # každá uložená verze je nový soubor na disku = nový ETag


def _xml_zamku(z, href) -> str:
    zbyva = max(1, int(z['do'] - time.monotonic()))
    return ('<D:activelock><D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope>'
            f'<D:depth>0</D:depth><D:owner><D:href>{escape(z["jmeno"])}</D:href></D:owner>'
            f'<D:timeout>Second-{zbyva}</D:timeout><D:locktoken><D:href>{z["token"]}</D:href>'
            f'</D:locktoken><D:lockroot><D:href>{escape(href)}</D:href></D:lockroot></D:activelock>')


def _vlastnosti(s, href, slozka, zamek=None) -> dict:
    """WebDAV vlastnosti (jméno v DAV: → XML obsah) souboru, nebo jeho složky."""
    v = {'displayname': escape(s['slozka'] if slozka else s['nazev']),
         'resourcetype': '<D:collection/>' if slozka else '',
         'creationdate': _iso_datum(s['nahrano']), 'getlastmodified': _http_datum(s['nahrano'])}
    if not slozka:
        v.update(getcontentlength=str(int(s['velikost'] or 0)), getcontenttype=escape(_mime(s['nazev'])),
                 getetag=escape(_etag(s['disk'])),
                 supportedlock='<D:lockentry><D:lockscope><D:exclusive/></D:lockscope>'
                               '<D:locktype><D:write/></D:locktype></D:lockentry>',
                 lockdiscovery=_xml_zamku(zamek, href) if zamek else '')
    return v


def _vlastnosti_z_xml(telo):
    """Názvy vlastností ('{DAV:}getetag', …) z těla PROPFIND / PROPPATCH.
    None = všechny (prázdné tělo, allprop); ValueError = nečitelné XML."""
    if not telo.strip():
        return None
    try:
        koren = ElementTree.fromstring(telo)
    except ElementTree.ParseError as e:
        raise ValueError('Neplatné XML') from e
    props = list(koren.iter('{DAV:}prop'))
    return [p.tag for prop in props for p in prop] if props else None


def _tag_xml(tag) -> str:
    """Prázdný element vlastnosti '{ns}jméno' z požadavku (i mimo jmenný prostor DAV:)."""
    ns, _, jmeno = tag[1:].partition('}') if tag.startswith('{') else ('', '', tag)
    if ns == 'DAV:':
        return f'<D:{jmeno}/>'
    return f'<x:{jmeno} xmlns:x="{escape(ns, {chr(34): "&quot;"})}"/>' if ns else f'<{jmeno} xmlns=""/>'


def _propstaty(href, *propstaty) -> str:
    """<D:response> s propstaty [(stav, XML vlastností)] — prázdné vynechá."""
    casti = ''.join(f'<D:propstat><D:prop>{xml}</D:prop><D:status>HTTP/1.1 {stav}</D:status></D:propstat>'
                    for stav, xml in propstaty if xml)
    return f'<D:response><D:href>{escape(href)}</D:href>{casti}</D:response>'


def _odpoved(href, vlastnosti, pozadovane=None) -> str:
    """Odpověď PROPFIND: požadované vlastnosti (None = všechny), neznámé jako 404."""
    zname = {t: t[6:] for t in pozadovane or () if t.startswith('{DAV:}') and t[6:] in vlastnosti}
    nalezene = vlastnosti if pozadovane is None else {zname[t]: vlastnosti[zname[t]] for t in zname}
    ok = ''.join(f'<D:{k}>{v}</D:{k}>' if v else f'<D:{k}/>' for k, v in nalezene.items())
    chybi = ''.join(_tag_xml(t) for t in pozadovane or () if t not in zname)
    return _propstaty(href, ('200 OK', ok), ('404 Not Found', chybi))


def _xml(telo, status=207, hlavicky=None) -> Response:
    return Response('<?xml version="1.0" encoding="utf-8"?>\n' + telo, status_code=status,
                    media_type='application/xml; charset=utf-8', headers=hlavicky)


def _multistatus(*odpovedi) -> Response:
    return _xml('<D:multistatus xmlns:D="DAV:">' + ''.join(odpovedi) + '</D:multistatus>')


def _zamceno(href) -> Response:
    """423 — soubor upravuje někdo jiný (Office pak nabídne otevření jen pro čtení)."""
    return _xml(f'<D:error xmlns:D="DAV:"><D:no-conflicting-lock><D:href>{escape(href)}</D:href>'
                '</D:no-conflicting-lock></D:error>', 423)


async def _telo_xml(request) -> bytes:
    """Malé XML tělo požadavku; větší než _MAX_XML → ValueError."""
    telo = b''
    async for kus in request.stream():
        telo += kus
        if len(telo) > _MAX_XML:
            raise ValueError('Příliš velký požadavek')
    return telo


async def _uloz_telo(request, cesta):
    """Tělo PUT na disk po kusech. None = větší než MAX_VELIKOST (rozepsané se smaže)."""
    velikost = 0
    os.makedirs(ADRESAR, exist_ok=True)
    try:
        with open(cesta, 'wb') as f:
            async for kus in request.stream():
                velikost += len(kus)
                if velikost > MAX_VELIKOST:
                    break
                if kus:
                    await asyncio.to_thread(f.write, kus)
    except BaseException:       # i odpojení klienta / zrušení — nic rozepsaného nezůstane
        _smaz_z_disku(cesta)
        raise
    if velikost > MAX_VELIKOST:
        _smaz_z_disku(cesta)
        return None
    return velikost


async def _dav_zamkni(request, s, user_id, href):
    try:
        telo = await _telo_xml(request)
    except ValueError:
        return PlainTextResponse('Neplatný požadavek', status_code=400)
    tokeny = _tokeny_zamku(request.headers.get('if'))
    platnost = time.monotonic() + _timeout(request.headers.get('timeout'))
    z = _zamek(s['id'])
    if z and z['token'] in tokeny:              # obnova vlastního zámku
        z['do'] = platnost
    elif z and z['uid'] != user_id:
        return _zamceno(href)
    else:
        # Nový zámek. Vlastní starý (pád Office, druhý počítač) se přebije — žádné
        # „soubor máte zamčený vy“. Obnovu zámku, který server zapomněl (restart),
        # přijme se stejným tokenem.
        token = min(tokeny) if tokeny and not telo.strip() else f'opaquelocktoken:{uuid.uuid4()}'
        z = _ZAMKY[s['id']] = {'token': token, 'uid': user_id, 'jmeno': s['kdo'], 'do': platnost}
        _ZMENY[s['slozka_id']] = time.monotonic()
    return _xml(f'<D:prop xmlns:D="DAV:"><D:lockdiscovery>{_xml_zamku(z, href)}</D:lockdiscovery></D:prop>',
                200, {'Lock-Token': f'<{z["token"]}>'})


async def _dav_uloz(request, s, user_id, href):
    """Uložení z Office (Ctrl+S) = nová verze souboru."""
    z = _zamek(s['id'])
    if z and z['uid'] != user_id and z['token'] not in _tokeny_zamku(request.headers.get('if')):
        return _zamceno(href)
    try:
        if int(request.headers.get('content-length') or 0) > MAX_VELIKOST:
            return PlainTextResponse('Soubor je větší než 50 MB.', status_code=413)
    except ValueError:
        return PlainTextResponse('Neplatný požadavek', status_code=400)
    disk = uuid.uuid4().hex
    velikost = await _uloz_telo(request, _disk(disk))
    if velikost is None:
        return PlainTextResponse('Soubor je větší než 50 MB.', status_code=413)
    if velikost == 0:
        # Prázdným PUT některé klienty soubor jen „zakládají“ před zápisem dat — dokument
        # Office prázdný nebývá a přepsáním by se ztratil. Obsah zůstává.
        _smaz_z_disku(_disk(disk))
        return Response(status_code=204, headers={'ETag': _etag(s['disk'])})
    try:
        ulozeno = await asyncio.to_thread(_prepis_soubor, s['id'], disk, velikost, s['kdo'])
    except Exception as e:
        print(f'[datuloziste] uložení z Office: {e}')
        return PlainTextResponse('Chyba databáze', status_code=500)
    if not ulozeno:         # soubor mezitím někdo smazal
        return PlainTextResponse('Nenalezeno', status_code=404)
    _ZMENY[s['slozka_id']] = time.monotonic()
    intranet_logger.log_activity(
        s['kdo'], _LOG, f'Upraven soubor „{s["nazev"]}“ ({_velikost(velikost)}) v „{s["cesta"]}“ (Office)')
    return Response(status_code=204, headers={'ETag': _etag(disk)})


@app.api_route(_DAV, methods=['OPTIONS', 'PROPFIND'], include_in_schema=False)
@app.api_route(_DAV + '/', methods=['OPTIONS', 'PROPFIND'], include_in_schema=False)
async def _dav_koren(request: Request):
    """Office se ptá i kořene webu a nadřazených složek, jestli server umí WebDAV —
    odpověď je prázdná kolekce (GET stránek se to netýká)."""
    if request.method == 'OPTIONS':
        return Response(headers=_DAV_HLAVICKY)
    return _multistatus(_propstaty(request.url.path, ('200 OK', '<D:resourcetype><D:collection/></D:resourcetype>')))


@app.on_startup
def _dav_nad_strankami():
    """Totéž pro kořen webu a /uloziste — až při startu: @ui.page při registraci maže
    všechny routy své cesty (intranet.py definuje '/' i '/uloziste' až po importu modulu)."""
    for cesta in ('/', '/uloziste', '/uloziste/'):
        app.add_api_route(cesta, _dav_koren, methods=['OPTIONS', 'PROPFIND'], include_in_schema=False)


@app.api_route(_DAV + '/{token}', methods=_DAV_METODY, include_in_schema=False)
@app.api_route(_DAV + '/{token}/{nazev:path}', methods=_DAV_METODY, include_in_schema=False)
async def _dav(request: Request, token: str):
    """WebDAV jednoho souboru: <token>/<název> je soubor, <token>/ jeho složka."""
    metoda = request.method
    if metoda == 'OPTIONS':
        return Response(headers=_DAV_HLAVICKY)
    nazev = request.path_params.get('nazev', '')
    ids = _over_token(token)
    s = await asyncio.to_thread(_dav_kontext, *ids) if ids else None
    slozka = nazev == ''
    if not s or not (slozka or unicodedata.normalize('NFC', nazev) == unicodedata.normalize('NFC', s['nazev'])):
        return PlainTextResponse('Nenalezeno', status_code=404)
    user_id = ids[1]
    # href požadovaného zdroje přesně tak, jak ho klient poslal (Office je porovnává).
    href = (request.scope.get('raw_path') or b'').decode('latin-1') or request.url.path
    zaklad = f'{request.scope.get("root_path", "")}{_DAV}/{token}/'
    if metoda == 'PROPFIND':
        try:
            pozadovane = _vlastnosti_z_xml(await _telo_xml(request))
        except ValueError:
            return PlainTextResponse('Neplatný požadavek', status_code=400)
        if not slozka:
            return _multistatus(_odpoved(href, _vlastnosti(s, href, False, _zamek(s['id'])), pozadovane))
        odpovedi = [_odpoved(href, _vlastnosti(s, href, True), pozadovane)]
        if request.headers.get('depth', 'infinity') != '0':
            soubor = zaklad + quote(s['nazev'], safe='')
            odpovedi.append(_odpoved(soubor, _vlastnosti(s, soubor, False, _zamek(s['id'])), pozadovane))
        return _multistatus(*odpovedi)
    if slozka:
        if metoda in ('GET', 'HEAD'):
            return Response()
        return Response(status_code=405, headers={'Allow': 'OPTIONS, GET, HEAD, PROPFIND'})
    if metoda in ('GET', 'HEAD'):
        return FileResponse(_disk(s['disk']), media_type=_mime(s['nazev']),
                            headers={'ETag': _etag(s['disk']), 'Last-Modified': _http_datum(s['nahrano']),
                                     'Cache-Control': 'no-cache'})
    if metoda == 'LOCK':
        return await _dav_zamkni(request, s, user_id, href)
    if metoda == 'UNLOCK':
        z = _zamek(s['id'])
        if z and z['token'] not in _tokeny_zamku(request.headers.get('lock-token')):
            return Response(status_code=409)
        if z:
            del _ZAMKY[s['id']]
            _ZMENY[s['slozka_id']] = time.monotonic()
        return Response(status_code=204)
    if metoda == 'PUT':
        return await _dav_uloz(request, s, user_id, href)
    if metoda == 'PROPPATCH':
        # Office si tak ukládá jen pomocné atributy — stačí potvrdit.
        try:
            tagy = _vlastnosti_z_xml(await _telo_xml(request)) or []
        except ValueError:
            return PlainTextResponse('Neplatný požadavek', status_code=400)
        return _multistatus(_propstaty(href, ('200 OK', ''.join(_tag_xml(t) for t in tagy))))
    return PlainTextResponse('Mazat a přesouvat soubory lze jen v intranetu.', status_code=403)


# =========================================================
# FORMÁTOVÁNÍ
# =========================================================
_MESICE = ('ledna', 'února', 'března', 'dubna', 'května', 'června',
           'července', 'srpna', 'září', 'října', 'listopadu', 'prosince')

# (přípony, ikona, barva) — barvy aplikací Office
_IKONY = (
    (('pdf',), 'picture_as_pdf', '#d13438'),
    (('doc', 'docx', 'docm', 'dot', 'dotx', 'dotm', 'odt', 'rtf'), 'description', '#2b579a'),
    (('xls', 'xlsx', 'xlsm', 'xlsb', 'xlt', 'xltx', 'xltm', 'csv', 'ods'), 'table_chart', '#217346'),
    (('ppt', 'pptx', 'pptm', 'pps', 'ppsx', 'pot', 'potx', 'odp'), 'slideshow', '#c43e1c'),
    (('jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'heic', 'tif', 'tiff'), 'image', '#8764b8'),
    (('zip', 'rar', '7z', 'gz', 'tar'), 'folder_zip', '#7a7574'),
    (('mp4', 'mov', 'avi', 'mkv', 'wmv'), 'movie', '#c239b3'),
    (('mp3', 'wav', 'm4a', 'ogg'), 'audiotrack', '#0078d4'),
    (('txt', 'log', 'md', 'json', 'xml'), 'article', '#605e5c'),
)
_SLOZKA = ('folder', '#f2b51c')
_SEKCE = ('topic', '#0078d4')
_NOVA = (('Vytvořit sekci', 'Nová sekce'), ('Vytvořit složku', 'Nová složka'),
         ('Vytvořit podsložku', 'Nová podsložka'))


def _ikona(nazev):
    pripona = _pripona(nazev)
    for pripony, ikona, barva in _IKONY:
        if pripona in pripony:
            return ikona, barva
    return 'insert_drive_file', '#605e5c'


def _abc(text):
    """Klíč řazení bez diakritiky (Šimon patří k S, ne za Z)."""
    return unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode()


def _velikost(b) -> str:
    b = int(b or 0)
    if b < 1024:
        return f'{b} B'
    if b < 1024 * 1024:
        return f'{b / 1024:.0f} kB'
    return f'{b / 1024 / 1024:.1f} MB'.replace('.', ',')


def _pocet(n, jeden, dva, pet) -> str:
    """Číslo s českým tvarem: 1 složka, 2–4 složky, 0 / 5+ složek."""
    return f'{n} {jeden if n == 1 else dva if 2 <= n <= 4 else pet}'


def _polozky(n) -> str:
    return _pocet(n, 'položka', 'položky', 'položek')


def _text_mazani(plan) -> tuple[str, list]:
    """(otázka, upozornění) potvrzovacího dialogu mazání."""
    polozky = plan['polozky']
    if len(polozky) == 1:
        p = polozky[0]
        druh = 'soubor' if p['typ'] else 'sekci' if p['sekce'] else 'složku'
        otazka = f'Opravdu smazat {druh} „{p["nazev"]}“?'
    else:
        otazka = f'Opravdu smazat {_pocet(len(polozky), "položku", "položky", "položek")}?'
    podslozky = sum(p.get('podslozky', 0) for p in polozky)
    soubory = sum(p.get('soubory', 0) for p in polozky)
    pristupy = sum(p.get('pristupy', 0) for p in polozky)
    obsah = ' a '.join(t for n, t in (
        (podslozky, _pocet(podslozky, 'podsložka', 'podsložky', 'podsložek')),
        (soubory, _pocet(soubory, 'soubor', 'soubory', 'souborů'))) if n)
    upozorneni = [f'Smaže se i obsah: {obsah}.'] if obsah else []
    if pristupy:
        upozorneni.append('Zruší se ' + _pocet(pristupy, 'udělený přístup', 'udělené přístupy',
                                                 'udělených přístupů') + '.')
    return otazka, upozorneni + ['Akci nelze vrátit zpět.']


def _kdy(dt) -> str:
    """Čas po vzoru SharePointu: Před chvílí / Před 5 min / Včera v 9:41 / 3. března."""
    if not dt:
        return ''
    ted = datetime.datetime.now()
    s = (ted - dt).total_seconds()
    if 0 <= s < 60:
        return 'Před chvílí'
    if 0 <= s < 3600:
        return f'Před {int(s // 60)} min'
    cas = f'{dt.hour}:{dt.minute:02d}'
    if dt.date() == ted.date():
        return f'Dnes v {cas}'
    if dt.date() == ted.date() - datetime.timedelta(days=1):
        return f'Včera v {cas}'
    if dt.year == ted.year:
        return f'{dt.day}. {_MESICE[dt.month - 1]}'
    return f'{dt.day}. {dt.month}. {dt.year}'


def _presne(dt) -> str:
    return f'{dt.day}. {dt.month}. {dt.year} {dt.hour}:{dt.minute:02d}' if dt else ''


def _cesta_text(cesta) -> str:
    return ' / '.join(s['nazev'] for s in cesta)


def _klic(sid) -> str:
    return 'koren' if sid is None else f's{sid}'


def _id(klic, predpona):
    """'s12' → 12 (složka), 'f7' → 7 (soubor); cokoli jiného → None."""
    if isinstance(klic, str) and klic[:1] == predpona and klic[1:].isdigit():
        return int(klic[1:])
    return None


def _nova(rodic, kratce=False) -> str:
    """Popisek akce podle úrovně: kořen → sekce, sekce → složka, složka → podsložka."""
    return _NOVA[0 if rodic is None else 1 if rodic['parent_id'] is None else 2][kratce]


def _vyber(radek, klic, vyber):
    """Příznak výběru (Ctrl+klik) pro šablonu; počet kvůli menu „Smazat vybrané (N)“."""
    if klic in vyber:
        radek.update(vyb=True, nvyb=len(vyber))
    return radek


def _uzly_stromu(strom, spravce, vyber=()):
    """Uzly pro ui.tree — jen složky, které uživatel smí vidět."""
    deti: dict = {}
    for s in strom:
        deti.setdefault(s['parent_id'], []).append(s)

    def uzel(s):
        ikona, barva = _SEKCE if s['parent_id'] is None else _SLOZKA
        n = _vyber({'id': _klic(s['id']), 'label': s['nazev'], 'ikona': ikona, 'barva': barva,
                    'menu': spravce, 'nova': _nova(s)}, _klic(s['id']), vyber)
        pod = sorted(deti.get(s['id'], ()), key=lambda x: _abc(x['nazev']))
        if pod:
            n['children'] = [uzel(x) for x in pod]
        return n

    sekce = sorted(deti.get(None, ()), key=lambda x: _abc(x['nazev']))
    # Kořen vlastní menu nemá — pravý klik na něj i kamkoli do volné části lišty
    # otevře menu celého panelu („Vytvořit sekci“).
    return [{'id': 'koren', 'label': 'Datové úložiště', 'ikona': 'home', 'barva': '#605e5c',
             'menu': False, 'children': [uzel(s) for s in sekce]}]


def _zamcene(p) -> frozenset:
    """Soubory zobrazené složky, které má někdo otevřené v Office."""
    return frozenset(f['id'] for f in p['soubory'] or () if _zamek(f['id']))


def _radky(p, spravce, vyber=(), mobil=False):
    """Řádky seznamu: nejdřív složky, pak soubory. U souboru: v jaké aplikaci Office
    se otevře (na mobilu žádné — stáhne se), zda jde do náhledu a kdo ho právě upravuje."""
    radky = []
    for s in sorted(p['slozky'], key=lambda x: _abc(x['nazev'])):
        ikona, barva = _SEKCE if s['parent_id'] is None else _SLOZKA
        radky.append(_vyber({
            'klic': _klic(s['id']), 'typ': 0, 'nazev': s['nazev'], 'ikona': ikona,
            'barva': barva, 'ts': s['vytvoreno'].timestamp() if s['vytvoreno'] else 0,
            'zmeneno': _kdy(s['vytvoreno']), 'presne': _presne(s['vytvoreno']),
            'zmenil': s['vytvoril'] or '', 'bajty': s['pocet'],
            'velikost': _polozky(s['pocet']), 'menu': spravce, 'nova': _nova(s)}, _klic(s['id']), vyber))
    for f in p['soubory'] or ():
        ikona, barva = _ikona(f['nazev'])
        pripona, zamek = _pripona(f['nazev']), _zamek(f['id'])
        radky.append(_vyber({
            'klic': f"f{f['id']}", 'typ': 1, 'nazev': f['nazev'], 'ikona': ikona,
            'barva': barva, 'ts': f['nahrano'].timestamp() if f['nahrano'] else 0,
            'zmeneno': _kdy(f['nahrano']), 'presne': _presne(f['nahrano']),
            'zmenil': f['nahral'] or '', 'bajty': int(f['velikost'] or 0),
            'velikost': _velikost(f['velikost']),
            'app': '' if mobil else _APLIKACE.get(_OFFICE.get(pripona), ''),
            'nahled': pripona in _NAHLED, 'zamek': zamek['jmeno'] if zamek else ''}, f"f{f['id']}", vyber))
    return radky


def _prazdno(p, spravce) -> str:
    if p['id'] is None:
        return ('Zatím tu nejsou žádné sekce — první založíte pravým tlačítkem myši.'
                if spravce else 'Nemáte přístup do žádné složky.')
    if p['soubory'] is None:
        return 'Žádné dostupné položky.'
    return 'Složka je prázdná. Soubory sem přetáhněte nebo použijte „Nahrát“.'


# =========================================================
# UI — šablony a vzhled (SharePoint)
# =========================================================
# Události ze šablon jdou přes $root.$refs.r<id>: $parent by u vnořených uzlů
# stromu byl animační obal, ne samotný strom.
# Pravý klik na položku, která je součástí výběru (Ctrl+klik) víc položek.
_MENU_VYBERU = r'''
<q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz_vyber'})">
  <q-item-section avatar><q-icon name="delete" /></q-item-section>
  <q-item-section>Smazat vybrané ({{ X.nvyb }})</q-item-section>
</q-item>
<q-item clickable v-close-popup @click="EMIT('akce', {a: 'zrus_vyber'})">
  <q-item-section avatar><q-icon name="deselect" /></q-item-section>
  <q-item-section>Zrušit výběr</q-item-section>
</q-item>
'''

# data-k: klíč pro Ctrl+klik a pravý klik mimo popisek (JS na kořeni modulu).
_SLOT_UZEL = r'''
<div class="du-uzel row items-center no-wrap" :class="{'du-vybrano': props.node.vyb}"
     :data-k="props.node.id" :title="props.node.label">
  <q-icon :name="props.node.ikona" size="20px" :style="{color: props.node.barva}" class="q-mr-sm" />
  <span class="ellipsis">{{ props.node.label }}</span>
  <q-icon v-if="props.node.vyb" name="check_circle" size="16px" class="du-check q-ml-xs" />
  <q-menu v-if="props.node.menu" context-menu touch-position>
    <q-list dense class="du-menu">
     <template v-if="props.node.vyb && props.node.nvyb > 1">VYBER</template>
     <template v-else>
      <q-item clickable v-close-popup @click="EMIT('akce', {a: 'otevri', k: props.node.id})">
        <q-item-section avatar><q-icon name="folder_open" /></q-item-section>
        <q-item-section>Otevřít</q-item-section>
      </q-item>
      <q-item clickable v-close-popup @click="EMIT('akce', {a: 'nova', k: props.node.id})">
        <q-item-section avatar><q-icon name="create_new_folder" /></q-item-section>
        <q-item-section>{{ props.node.nova }}</q-item-section>
      </q-item>
      <q-item clickable v-close-popup @click="EMIT('akce', {a: 'pristup', k: props.node.id})">
        <q-item-section avatar><q-icon name="manage_accounts" /></q-item-section>
        <q-item-section>Řízení přístupu</q-item-section>
      </q-item>
      <q-separator />
      <q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz', k: props.node.id})">
        <q-item-section avatar><q-icon name="delete" /></q-item-section>
        <q-item-section>Smazat</q-item-section>
      </q-item>
     </template>
    </q-list>
  </q-menu>
</div>
'''.replace('VYBER', _MENU_VYBERU.replace('X.', 'props.node.'))

_MENU_RADKU = r'''
<q-list dense class="du-menu">
  <template v-if="props.row.vyb && props.row.nvyb > 1">VYBER</template>
  <template v-else-if="props.row.typ === 0">
    <q-item clickable v-close-popup @click="EMIT('akce', {a: 'otevri', k: props.row.klic})">
      <q-item-section avatar><q-icon name="folder_open" /></q-item-section>
      <q-item-section>Otevřít</q-item-section>
    </q-item>
    <template v-if="props.row.menu">
      <q-item clickable v-close-popup @click="EMIT('akce', {a: 'nova', k: props.row.klic})">
        <q-item-section avatar><q-icon name="create_new_folder" /></q-item-section>
        <q-item-section>{{ props.row.nova }}</q-item-section>
      </q-item>
      <q-item clickable v-close-popup @click="EMIT('akce', {a: 'pristup', k: props.row.klic})">
        <q-item-section avatar><q-icon name="manage_accounts" /></q-item-section>
        <q-item-section>Řízení přístupu</q-item-section>
      </q-item>
      <q-separator />
      <q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz', k: props.row.klic})">
        <q-item-section avatar><q-icon name="delete" /></q-item-section>
        <q-item-section>Smazat</q-item-section>
      </q-item>
    </template>
  </template>
  <template v-else>
    <q-item v-if="props.row.app || props.row.nahled" clickable v-close-popup
            @click="EMIT('akce', {a: 'otevri', k: props.row.klic})">
      <q-item-section avatar><q-icon :name="props.row.ikona" :style="{color: props.row.barva}" /></q-item-section>
      <q-item-section>{{ props.row.app ? 'Otevřít v aplikaci ' + props.row.app : 'Otevřít' }}</q-item-section>
    </q-item>
    <q-item clickable v-close-popup @click="EMIT('akce', {a: 'stahni', k: props.row.klic})">
      <q-item-section avatar><q-icon name="download" /></q-item-section>
      <q-item-section>Stáhnout</q-item-section>
    </q-item>
    <q-separator />
    <q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz', k: props.row.klic})">
      <q-item-section avatar><q-icon name="delete" /></q-item-section>
      <q-item-section>Smazat</q-item-section>
    </q-item>
  </template>
</q-list>
'''.replace('VYBER', _MENU_VYBERU.replace('X.', 'props.row.'))

_SLOT_RADEK = r'''
<q-tr :props="props" class="du-radek" :class="{'du-vybrano': props.row.vyb}" :data-k="props.row.klic"
      @contextmenu.stop @click="EMIT('akce', {a: 'otevri', k: props.row.klic})">
  <q-td key="nazev" :props="props">
    <div class="row items-center no-wrap">
      <q-icon :name="props.row.ikona" size="22px" :style="{color: props.row.barva}" class="q-mr-md" />
      <span class="du-nazev ellipsis">{{ props.row.nazev }}</span>
      <q-icon v-if="props.row.zamek" name="lock" size="16px" class="du-zamek q-ml-sm">
        <q-tooltip>Právě upravuje {{ props.row.zamek }}</q-tooltip>
      </q-icon>
      <q-icon v-if="props.row.vyb" name="check_circle" size="18px" class="du-check q-ml-sm" />
      <q-btn flat round dense size="sm" icon="more_vert" class="du-tri q-ml-auto" title="Zobrazit akce" @click.stop>
        <q-menu>MENU</q-menu>
      </q-btn>
    </div>
  </q-td>
  <q-td key="zmeneno" :props="props"><span :title="props.row.presne">{{ props.row.zmeneno }}</span></q-td>
  <q-td key="zmenil" :props="props">{{ props.row.zmenil }}</q-td>
  <q-td key="velikost" :props="props">{{ props.row.velikost }}</q-td>
  <q-menu context-menu touch-position>MENU</q-menu>
</q-tr>
'''.replace('MENU', _MENU_RADKU)

# Ctrl+klik (na Macu Cmd+klik) na složku / soubor ji přidá do výběru, nebo z něj odebere.
# Zachytává se na kořeni modulu už ve fázi capture — strom ani řádek seznamu klik
# nedostanou, takže se nic neotevře (ani u nevybíratelné položky — výběr se tím neztratí).
# Šipka dál jen rozbaluje; kořen nejde vybrat, složky vybírá jen správce.
_JS_VYBER = '''(e) => {
  if (!(e.ctrlKey || e.metaKey) || e.button !== 0 || e.target.closest('.q-tree__arrow')) return;
  const radek = e.target.closest('.q-tree__node-header');
  const el = e.target.closest('[data-k]') || (radek && radek.querySelector('[data-k]'));
  if (!el) return;
  e.preventDefault();
  e.stopPropagation();
  if (!e.currentTarget.contains(document.activeElement)) e.currentTarget.focus({preventScroll: true});
  const k = el.dataset.k;
  if (k !== 'koren' && (SPRAVCE || k[0] === 'f')) emit(k);
}'''

# Delete smaže výběr, Esc ho zruší — když je modul zaměřený (klik do něj), mimo textová pole
# a jen když nějaký výběr je (lišta výběru v záhlaví).
_JS_KLAVESA = '''(e) => {
  if ((e.key === 'Delete' || e.key === 'Escape') && e.currentTarget.querySelector('.du-vyber')
      && !e.target.closest('input, textarea, select, [contenteditable="true"]')) emit(e.key);
}'''

# Pravý klik na šipku nebo okraj řádku stromu (mimo popisek) otevře menu té složky, ne menu
# lišty: událost se přepošle na popisek, kde menu složky čeká (vnitřní menu má přednost).
_JS_MENU_RADKU_STROMU = '''(e) => {
  const radek = e.target.closest('.q-tree__node-header');
  const uzel = radek && !e.target.closest('.du-uzel') ? radek.querySelector('.du-uzel') : null;
  if (!uzel) return;
  e.preventDefault();
  e.stopPropagation();
  uzel.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true, button: 2,
    clientX: e.clientX, clientY: e.clientY, screenX: e.screenX, screenY: e.screenY}));
}'''

_MAX_V_DIALOGU = 8      # kolik mazaných položek dialog vypíše jménem

_SLOUPCE = (
    {'name': 'nazev', 'label': 'Název', 'field': 'nazev', 'align': 'left', 'sortable': True},
    {'name': 'zmeneno', 'label': 'Změněno', 'field': 'ts', 'align': 'left', 'sortable': True,
     'classes': 'gt-xs', 'headerClasses': 'gt-xs', 'style': 'width: 150px', 'headerStyle': 'width: 150px'},
    {'name': 'zmenil', 'label': 'Změnil', 'field': 'zmenil', 'align': 'left', 'sortable': True,
     'classes': 'gt-sm', 'headerClasses': 'gt-sm', 'style': 'width: 200px', 'headerStyle': 'width: 200px'},
    {'name': 'velikost', 'label': 'Velikost', 'field': 'bajty', 'align': 'right', 'sortable': True,
     'classes': 'gt-xs', 'headerClasses': 'gt-xs', 'style': 'width: 110px', 'headerStyle': 'width: 110px'},
)

# Řazení jako v SharePointu: složky vždy nad soubory, v obou směrech.
_RAZENI = '''(rows, sortBy, descending) => {
  const pole = {nazev: 'nazev', zmeneno: 'ts', zmenil: 'zmenil', velikost: 'bajty'}[sortBy];
  const smer = descending ? -1 : 1;
  return [...rows].sort((a, b) => (a.typ - b.typ) || smer * (typeof a[pole] === 'number'
    ? a[pole] - b[pole] : String(a[pole]).localeCompare(String(b[pole]), 'cs')));
}'''

_CSS = r'''
/* Celá plocha vedle menu a pod hlavičkou (jako SharePoint), ne okno uprostřed. Obaly
   intranetu mezi stránkou Quasaru a modulem (obsah, obal p-8 s min-h-screen, panely tabů,
   kontejner tabu) ztratí odsazení a natáhnou se na výšku stránky — tu dává její min-height
   (okno − hlavička, počítá Quasar); height: 0 z ní dělá pevnou výšku pro vnořené flexy.
   :has() platí jen s otevřeným tabem úložiště — keep-alive skryté taby vyjímá z DOM. */
.q-page:has(.du) { display: flex; flex-direction: column; height: 0; }
.q-page :has(.du) { display: flex; flex-direction: column; flex: 1 1 0; min-height: 0; padding: 0; gap: 0; }
.du { display: flex; flex-direction: column; flex: 1 1 0; min-height: 0; font-family: "Segoe UI", "Segoe UI Web (West European)", -apple-system, BlinkMacSystemFont, Roboto, "Helvetica Neue", sans-serif; color: #323130; }
.du-ram { display: flex; align-items: stretch; flex: 1 1 0; min-height: 320px; background: #fff; overflow: hidden; }
.du-panel { width: 272px; flex-shrink: 0; padding: 10px 6px; background: #faf9f8; border-right: 1px solid #edebe9; overflow: auto; }
.du-obsah { flex: 1 1 0; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
@media (max-width: 767.98px) {
  .du-ram { flex-direction: column; }
  .du-panel { width: auto; max-height: 45vh; border-right: 0; border-bottom: 1px solid #edebe9; }
  .du-panel:not(.du-otevren) { display: none; }
}
@media (min-width: 768px) { .du-jen-mobil { display: none !important; } }

.du-strom { color: #605e5c; font-size: 14px; }
.du-strom .q-tree__node { padding-bottom: 0; }
.du-strom .q-tree__children { padding-left: 14px; }
.du-strom .q-tree__node-header { padding: 5px 6px; margin-top: 1px; border-radius: 4px; }
.du-strom .q-tree__node-header:hover { background: #f3f2f1; }
.du-strom .q-tree__node-header-content { color: #323130; min-width: 0; }
.du-strom .q-tree__node--selected { background: #edebe9; }
.du-strom .q-tree__node--selected:hover { background: #e1dfdd; }
.du-strom .q-tree__node--selected .q-tree__node-header-content { color: #201f1e; font-weight: 600; }
.du-strom .q-tree__arrow { color: #605e5c; }
.du-strom .q-focus-helper { display: none; }
.du-uzel { width: 100%; min-width: 0; }
.du-uzel > span { flex: 1 1 auto; min-width: 0; }

/* Výběr Ctrl+klikem — barvy výběru SharePointu, odlišné od šedé otevřené složky. */
.du:focus { outline: none; }
.du-strom .q-tree__node-header:has(.du-vybrano) { background: #deecf9; }
.du-strom .q-tree__node-header:has(.du-vybrano):hover { background: #c7e0f4; }
.du-check { color: #0078d4; flex-shrink: 0; }
.du-zamek { color: #a4262c; flex-shrink: 0; }
.du-vyber { gap: 2px; padding: 0 2px 0 10px; border-radius: 4px; background: #deecf9; color: #323130; font-size: 14px; white-space: nowrap; }

.du-hlava { padding: 12px 12px 6px 16px; min-height: 56px; }
.du-drobek { font-size: 20px; line-height: 28px; padding: 0 6px; border-radius: 4px; color: #605e5c; cursor: pointer; white-space: nowrap; }
.du-drobek:hover { background: #f3f2f1; color: #323130; }
.du-drobek-akt { color: #323130; font-weight: 600; }
.du-drobky-sep { color: #8a8886; font-size: 18px; }
@media (max-width: 767.98px) { .du-drobek { font-size: 16px; line-height: 24px; } .du-drobky-sep { font-size: 16px; } }
.du-btn { color: #323130 !important; font-size: 14px; padding: 4px 10px; }
.du-btn .q-icon { color: #0078d4; font-size: 20px; }
.du-info { margin: 0 18px 8px; padding: 8px 12px; gap: 8px; background: #f3f2f1; border-radius: 4px; font-size: 13px; color: #605e5c; }

/* Seznam se posouvá uvnitř (q-table__middle); drobečky i hlavička tabulky zůstávají nahoře. */
.du-tabulka { flex: 1 1 0; min-height: 0; background: transparent; color: #323130; }
.du-tabulka thead th { position: sticky; top: 0; z-index: 1; background: #fff; font-size: 13px; font-weight: 600; color: #323130; height: 40px; }
.du-tabulka tbody td { font-size: 14px; color: #605e5c; height: 44px; }
.du-radek { cursor: pointer; }
.du-radek:hover > td { background: #f3f2f1; }
.du-radek.du-vybrano > td { background: #deecf9; }
.du-radek.du-vybrano:hover > td { background: #c7e0f4; }
.du-nazev { color: #323130; min-width: 0; }
.du-radek:hover .du-nazev { text-decoration: underline; }
.du-tri { opacity: 0; color: #605e5c; }
.du-radek:hover .du-tri, .du-tri:focus { opacity: 1; }
@media (hover: none) { .du-tri { opacity: .7; } }
.du-prazdno { width: 100%; padding: 40px 16px; text-align: center; font-size: 14px; color: #605e5c; }
.du-menu { min-width: 200px; }
.du-menu .q-item { min-height: 36px; font-size: 14px; color: #323130; }
.du-menu .q-item__section--avatar { min-width: 34px; color: #0078d4; }
.du-menu .du-cervena, .du-menu .du-cervena .q-item__section--avatar { color: #a4262c; }
.du-drop { outline: 2px dashed #0078d4; outline-offset: -8px; background: #eff6fc; }
.du-skryty { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden; opacity: 0; pointer-events: none; }
'''


# =========================================================
# UI
# =========================================================
async def vykresli(user_id, user_name, vsechna_prava):
    """Vstupní bod modulu (volá intranet.py)."""
    await asyncio.to_thread(inicializace_db)
    spravce = je_spravce(vsechna_prava)
    # vyber: klíče položek vybraných Ctrl+klikem ('s12' složka, 'f7' soubor).
    # nacteno: kdy se pohled naposledy četl z DB; zamky: zamčené soubory v zobrazeném seznamu.
    st = {'pohled': None, 'mapa': {}, 'radky': {}, 'nahravani': False, 'notif': None, 'vyber': set(),
          'nacteno': 0.0, 'zamky': frozenset()}
    menu_vyberu = []    # (oddělovač, „Smazat vybrané“, její popisek, „Zrušit výběr“) v menu volné plochy
    # Telefon / tablet: desktopový Office přes WebDAV tam není — dokumenty se stahují.
    ua = ui.context.client.request.headers.get('user-agent', '') if ui.context.client.request else ''
    mobil = any(z in ua for z in ('Mobi', 'Android', 'iPhone', 'iPad'))
    # Deep-link z e-mailu (/uloziste?slozka=<id>) — přístup ověří _nacti_pohled.
    start = app.storage.user.pop('datuloziste_slozka', None)
    ui.add_css(_CSS)

    koren = ui.element('div').classes('du w-full')

    def zprava(text, typ='warning'):
        # Z kořene: tlačítko, které akci spustilo, už mohl smazat refresh.
        with koren:
            ui.notify(text, type=typ, position='top')

    async def db(fn, *args):
        """Blokující DB mimo event loop; chyba → hláška a None."""
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:
            print(f'[datuloziste] {fn.__name__}: {e}')
            zprava('Chyba databáze — akce se neprovedla.', 'negative')
            return None

    def aktualni():
        p = st['pohled']
        return p['cesta'][-1] if p and p['cesta'] else None

    def muze_nahravat():
        p = st['pohled']
        return bool(p) and p['id'] is not None and p['soubory'] is not None and not st['nahravani']

    # ---------- navigace ----------
    async def otevri(sid):
        st['nacteno'] = time.monotonic()    # změna z Office během čtení → hlidej() načte znovu
        p = await db(_nacti_pohled, user_id, spravce, sid)
        if p is False:
            zprava('Ke složce nemáte přístup nebo už neexistuje.')
            p = await db(_nacti_pohled, user_id, spravce, None)
        if not p:
            return
        if not st['pohled'] or p['id'] != st['pohled']['id']:
            st['vyber'].clear()     # přechod do jiné složky = nový výběr (jako v Průzkumníku)
        st['pohled'] = p
        st['mapa'] = {s['id']: s for s in p['strom']}
        # Z výběru pryč, co už neexistuje (smazané, soubory jiné složky).
        st['vyber'] &= ({_klic(i) for i in st['mapa']} if spravce else set()) \
            | {f"f{f['id']}" for f in p['soubory'] or ()}
        prekresli()
        strom.select(_klic(p['id']))
        strom.expand(['koren'] + [_klic(s['id']) for s in p['cesta']])
        prazdno.set_text(_prazdno(p, spravce))
        upl.props['url'] = f'/uloziste/nahrat/{p["id"] or 0}'
        if muze_nahravat():
            obsah.classes(add='du-nahravani')
        else:
            obsah.classes(remove='du-nahravani')
        if menu_plochy:
            menu_plochy.set_text(_nova(aktualni()))
        zahlavi.refresh()

    def prekresli():
        """Strom, seznam a menu výběru podle st (bez dotazu do DB)."""
        p = st['pohled']
        uzly = _uzly_stromu(p['strom'], spravce, st['vyber'])
        if strom.props['nodes'] != uzly:
            strom.props['nodes'] = uzly
            strom.update()
        radky = _radky(p, spravce, st['vyber'], mobil)
        st['radky'] = {r['klic']: r for r in radky}
        st['zamky'] = _zamcene(p)
        tabulka.update_rows(radky)
        n = len(st['vyber'])
        for oddelovac, smazat, popisek, zrusit in menu_vyberu:
            for prvek in (oddelovac, smazat, zrusit):
                prvek.set_visibility(n > 0)
            popisek.set_text(f'Smazat vybrané ({n})')

    # ---------- výběr Ctrl+klikem ----------
    def prepni_vyber(e):
        """Ctrl+klik na složku / soubor: přidat do výběru, nebo z něj odebrat.
        Klíč přichází z prohlížeče — bere se jen to, co uživatel právě vidí."""
        k = e.args if isinstance(e.args, str) else None
        if not st['pohled'] or not k:
            return
        slozka = spravce and _id(k, 's') in st['mapa']
        soubor = _id(k, 'f') is not None and k in st['radky']
        if slozka or soubor:
            st['vyber'] ^= {k}
            prekresli()
            zahlavi.refresh()

    def zrus_vyber():
        if st['vyber']:
            st['vyber'].clear()
            prekresli()
            zahlavi.refresh()

    async def smaz_vyber():
        await smaz(list(st['vyber']))

    async def klavesa(e):
        if e.args == 'Escape':
            zrus_vyber()
        elif e.args == 'Delete' and st['vyber']:
            await smaz_vyber()

    async def otevri_klic(k):
        if k == 'koren':
            await otevri(None)
        elif _id(k, 's') is not None:
            await otevri(_id(k, 's'))

    def otevri_soubor(radek):
        """Klik na soubor jako v SharePointu: dokument Office v desktopové aplikaci (uložení
        zapíše novou verzi sem), PDF / obrázek / text v nové záložce, ostatní se stáhne."""
        fid, nazev = _id(radek['klic'], 'f'), radek['nazev']
        if radek['app']:
            # Adresu serveru doplní prohlížeč (location.origin) — Office se připojí přesně
            # tam, kam uživatel (testovací i ostrý portál, desktopová aplikace).
            cesta = f'{_DAV}/{_token(fid, user_id)}/{quote(nazev, safe="")}'
            schema = f'{_OFFICE[_pripona(nazev)]}:ofe|u|'
            ui.run_javascript(f'window.location.href = {json.dumps(schema)} + location.origin + {json.dumps(cesta)}')
            with koren:
                ui.notify(f'Otevírám v aplikaci {radek["app"]}…', type='info', position='top',
                          caption=f'Uložením (Ctrl+S) se změny propíší sem. Když se {radek["app"]} '
                                  'nespustí, soubor si stáhněte.')
        elif radek['nahled']:
            ui.navigate.to(f'/uloziste/nahled/{fid}/{quote(nazev, safe="")}', new_tab=True)
        else:
            ui.download.from_url(f'/uloziste/soubor/{fid}')

    async def hlidej():
        """Uložení / zámek z Office v otevřené složce → přenačíst z DB; prošlý zámek
        (Office spadl) → jen překreslit."""
        p = st['pohled']
        if not p or p['id'] is None or st['nahravani']:
            return
        if _ZMENY.get(p['id'], 0) > st['nacteno']:
            await otevri(p['id'])
        elif _zamcene(p) != st['zamky']:
            prekresli()

    async def vyber(e):
        if st['pohled'] and e.value not in (None, _klic(st['pohled']['id'])):
            await otevri_klic(e.value)

    async def akce(e):
        """Akce z kontextových menu stromu i seznamu. Klíč přichází z prohlížeče —
        bere se jen to, co uživatel právě vidí, a DB funkce přístup ověří znovu."""
        args = e.args if isinstance(e.args, dict) else {}
        a, k = args.get('a'), args.get('k')
        radek = st['radky'].get(k)
        slozka = st['mapa'].get(_id(k, 's'))
        if a == 'otevri' and radek and radek['typ'] == 1:
            otevri_soubor(radek)
        elif a == 'otevri':
            await otevri_klic(k)
        elif a == 'stahni' and radek and radek['typ'] == 1:
            ui.download.from_url(f'/uloziste/soubor/{_id(k, "f")}')
        elif a == 'smaz' and radek and radek['typ'] == 1:
            await smaz([k])
        elif a == 'smaz_vyber':
            await smaz_vyber()
        elif a == 'zrus_vyber':
            zrus_vyber()
        elif not spravce:
            return
        elif a == 'nova' and (k == 'koren' or slozka):
            dialog_nova_slozka(slozka)
        elif a == 'pristup' and slozka:
            await dialog_pristupu(slozka)
        elif a == 'smaz' and slozka:
            await smaz([k])

    # ---------- mazání (jedna položka z menu i celý výběr) ----------
    async def smaz(klice):
        plan = await db(_plan_mazani, user_id, spravce, klice)
        if plan is None:
            return
        if not plan['polozky']:
            zprava('Není co mazat — položky už neexistují nebo k nim nemáte přístup.')
            await otevri(st['pohled']['id'])
            return
        otazka, upozorneni = _text_mazani(plan)
        polozky = plan['polozky']
        with koren, ui.dialog() as dlg, ui.card().classes('w-[32rem] max-w-full gap-3'):
            ui.label(otazka).classes('text-lg font-bold text-gray-800 break-words')
            with ui.column().classes('w-full gap-2'):
                for p in polozky[:_MAX_V_DIALOGU]:
                    ikona, barva = _ikona(p['nazev']) if p['typ'] else _SEKCE if p['sekce'] else _SLOZKA
                    with ui.row().classes('w-full items-center no-wrap gap-3'):
                        ui.icon(ikona, size='22px').style(f'color: {barva}')
                        with ui.column().classes('min-w-0 gap-0'):
                            ui.label(p['nazev']).classes('text-sm text-gray-800 truncate max-w-full')
                            ui.label(p['misto'] or 'Datové úložiště') \
                                .classes('text-xs text-gray-500 truncate max-w-full')
                if len(polozky) > _MAX_V_DIALOGU:
                    ui.label(f'… a další ({len(polozky) - _MAX_V_DIALOGU})').classes('text-sm text-gray-500')
            for text in upozorneni:
                ui.label(text).classes('text-sm text-gray-700')

            async def ano():
                dlg.close()
                await proved_mazani(plan)

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Smazat', icon='delete', on_click=ano).props('color=negative')
        dlg.on('hide', dlg.delete)
        dlg.open()

    async def proved_mazani(plan):
        polozky = plan['polozky']
        vysledek = await db(_smaz_polozky, user_id, spravce,
                            [p['id'] for p in polozky if not p['typ']], [p['id'] for p in polozky if p['typ']])
        if vysledek is None:
            return
        smazane, smazane_soubory, _ = vysledek
        # Jen to, co smazalo právě tohle volání (souběžně mohl mazat i někdo jiný).
        polozky = [p for p in polozky if p['id'] in (smazane_soubory if p['typ'] else smazane)]
        if polozky:
            for p in polozky:
                misto = p['misto'] or 'Datové úložiště'
                if p['typ']:
                    text = f'Smazán soubor „{p["nazev"]}“ z „{misto}“'
                else:
                    text = f'Smazána {"sekce" if p["sekce"] else "složka"} „{p["nazev"]}“ z „{misto}“'
                    if p['podslozky'] or p['soubory'] or p['pristupy']:
                        text += (f' i s obsahem ({p["podslozky"]} podsl., {p["soubory"]} soub.)'
                                 f', zrušené přístupy: {p["pristupy"]}')
                intranet_logger.log_activity(user_name, _LOG, text)
            zprava(f'Smazáno: „{polozky[0]["nazev"]}“.' if len(polozky) == 1
                   else f'Smazáno: {_polozky(len(polozky))}.', 'positive')
        else:
            zprava('Položky už mezitím zmizely.')
        st['vyber'] -= {_klic(p['id']) if not p['typ'] else f"f{p['id']}" for p in polozky}
        # Smazaná otevřená složka → nejbližší nadřazená, která zůstala.
        cil = next((c['id'] for c in reversed(st['pohled']['cesta']) if c['id'] not in smazane), None)
        await otevri(cil)

    # ---------- složky (jen správce) ----------
    def dialog_nova_slozka(rodic):
        """rodic = řádek složky, None = kořen (nová sekce)."""
        if not spravce:
            return
        with koren, ui.dialog() as dlg, ui.card().classes('w-96 max-w-full'):
            ui.label(_nova(rodic)).classes('text-lg font-bold text-gray-800')
            if rodic:
                ui.label(f'v „{rodic["nazev"]}“').classes('text-sm text-gray-500 -mt-2')
            vstup = ui.input('Název').props('outlined dense autofocus maxlength=255') \
                .classes('w-full')

            async def vytvor():
                nazev = ' '.join((vstup.value or '').split())
                if not nazev:
                    zprava('Zadejte název.')
                    return
                ok = await db(_vytvor_slozku, rodic['id'] if rodic else None, nazev, user_name)
                if ok is False:
                    zprava('Složka s tímto názvem tu už je.')
                if not ok:
                    return
                intranet_logger.log_activity(
                    user_name, _LOG, f'Vytvořena složka „{nazev}“'
                    + (f' v „{rodic["nazev"]}“' if rodic else ' (sekce)'))
                dlg.close()
                if rodic:
                    strom.expand([_klic(rodic['id'])])     # ať je nová složka ve stromu vidět
                await otevri(st['pohled']['id'])

            vstup.on('keydown.enter', vytvor)
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Vytvořit', on_click=vytvor).props('color=primary')
        dlg.on('hide', dlg.delete)
        dlg.open()

    async def dialog_pristupu(s):
        if not spravce or not s:
            return
        cesta_txt = _cesta_text(_cesta(st['mapa'], s['id']))
        stav = {'radky': []}
        with koren, ui.dialog() as dlg, ui.card().classes('w-[36rem] max-w-full gap-3'):
            ui.label('Řízení přístupu').classes('text-lg font-bold text-gray-800')
            ui.label(cesta_txt).classes('text-sm text-gray-500 -mt-2 break-words')
            ui.label('Přístup mají (správci dat vidí vše):') \
                .classes('text-sm font-semibold text-gray-700 mt-2')

            @ui.refreshable
            def seznam():
                if not stav['radky']:
                    ui.label('Zatím nikdo.').classes('text-sm text-gray-400 italic')
                for r in stav['radky']:
                    with ui.row().classes('w-full items-center gap-2 no-wrap'):
                        ui.icon('person', color='grey-6')
                        with ui.column().classes('flex-1 min-w-0 gap-0'):
                            ui.label(f"{r['name']} {r['surname']}") \
                                .classes('text-sm text-gray-800 truncate')
                            zdedeno = f" · zděděno ze složky „{r['zdedeno']}“" if r['zdedeno'] else ''
                            ui.label(f"{r['email']}{zdedeno}").classes('text-xs text-gray-500 truncate')
                        if not r['zdedeno']:
                            ui.button(icon='person_remove', on_click=lambda r=r: odeber(r)) \
                                .props('flat round dense color=negative').tooltip('Odebrat přístup')

            seznam()
            ui.separator()
            with ui.row().classes('w-full items-center gap-2 no-wrap'):
                vstup = ui.input('E-mail uživatele').props('outlined dense').classes('flex-1')
                ui.button('Vyhledat', icon='search', on_click=lambda: hledej()) \
                    .props('color=primary no-caps')
            nalez = ui.row().classes('w-full items-center gap-2 no-wrap')
            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg.close).props('flat')

        async def nacti():
            radky = await db(_pristupy, s['id'])
            if radky is not None:
                stav['radky'] = radky
                seznam.refresh()

        async def hledej():
            adresa = (vstup.value or '').strip().lower()
            nalez.clear()
            if not adresa:
                return
            vysledek = await db(_najdi_uzivatele, adresa)
            if not vysledek:
                return
            uid, jmeno, chyba = vysledek
            with nalez:
                if not uid:
                    ui.label(chyba or 'Uživatel nenalezen.').classes('text-sm text-red-600')
                    return
                ui.icon('person', color='primary')
                ui.label(f'{jmeno} ({adresa})').classes('text-sm flex-1 min-w-0 truncate')
                ui.button('Potvrdit přidání přístupu', icon='check',
                          on_click=lambda: pridej(uid, jmeno, adresa)).props('color=positive no-caps')

        async def pridej(uid, jmeno, adresa):
            r = await db(_pridej_pristup, s['id'], uid, user_name)
            if r is None:
                return
            if r == 'neni':
                zprava('Složka mezitím zanikla — přístup nebyl přidán.', 'negative')
                dlg.close()
                await otevri(st['pohled']['id'])
                return
            if r == 'pridan':
                background_tasks.create(
                    asyncio.to_thread(_posli_email, adresa, cesta_txt, s['id'], user_name),
                    name='datuloziste.email')
                intranet_logger.log_activity(user_name, _LOG, f'Přístup do „{cesta_txt}“ pro {jmeno}')
                zprava(f'Přístup přidán — {jmeno} dostane e-mail s odkazem.', 'positive')
            else:
                zprava(f'{jmeno} už do této složky přístup má.', 'info')
            vstup.value = ''
            nalez.clear()
            await nacti()

        async def odeber(r):
            if await db(_sql, 'DELETE FROM datuloziste_pristupy WHERE slozka_id = %s AND user_id = %s',
                        (s['id'], r['user_id'])) is None:
                return
            intranet_logger.log_activity(
                user_name, _LOG, f"Odebrán přístup do „{cesta_txt}“: {r['name']} {r['surname']}")
            await nacti()

        vstup.on('keydown.enter', hledej)
        dlg.on('hide', dlg.delete)
        await nacti()
        dlg.open()

    # ---------- soubory ----------
    def nahravani_start():
        # Během nahrávání nejde přidat další soubory (Quasar by je jen zařadil
        # do fronty a po skončení zahodil).
        st['nahravani'] = True
        obsah.classes(remove='du-nahravani')
        zahlavi.refresh()
        if st['notif'] is None:
            with koren:
                st['notif'] = ui.notification('Nahrávám soubory…', spinner=True, timeout=None,
                                              position='bottom-right')

    async def nahravani_konec():
        st['nahravani'] = False
        if st['notif'] is not None:
            st['notif'].dismiss()
            st['notif'] = None
        upl.reset()     # jinak by Quasar stejný soubor podruhé nepřijal
        await otevri(st['pohled']['id'])

    # ---------- části stránky ----------
    def polozka_menu(ikona, text, akce_klik, cervena=False):
        """Položka kontextového menu volné plochy; vrací (položku, její popisek)."""
        with ui.menu_item(on_click=akce_klik) as polozka:
            with ui.item_section().props('avatar'):
                ui.icon(ikona)
            popisek = ui.item_section(text)
        if cervena:
            polozka.classes('du-cervena')
        return polozka, popisek

    def pridej_menu_vyberu():
        oddelovac = ui.separator()
        smazat, popisek = polozka_menu('delete', '', smaz_vyber, cervena=True)
        zrusit, _ = polozka_menu('deselect', 'Zrušit výběr', zrus_vyber)
        for prvek in (oddelovac, smazat, zrusit):
            prvek.set_visibility(False)
        menu_vyberu.append((oddelovac, smazat, popisek, zrusit))

    @ui.refreshable
    def zahlavi():
        p = st['pohled']
        body = [('koren', 'Datové úložiště')] + [(_klic(s['id']), s['nazev']) for s in (p['cesta'] if p else [])]
        with ui.row().classes('du-hlava w-full items-center no-wrap gap-2'):
            ui.button(icon='account_tree', color=None).props('flat round dense').classes('du-btn du-jen-mobil') \
                .tooltip('Strom složek') \
                .on('click', js_handler=f'() => getHtmlElement({panel.id}).classList.toggle("du-otevren")')
            with ui.row().classes('items-center gap-0 min-w-0 flex-1'):
                for i, (k, nazev) in enumerate(body):
                    if i:
                        ui.icon('chevron_right').classes('du-drobky-sep')
                    ui.label(nazev).classes('du-drobek' + (' du-drobek-akt' if i == len(body) - 1 else '')) \
                        .on('click', lambda k=k: otevri_klic(k))
            if st['vyber']:     # lišta výběru (Ctrl+klik) — jako příkazový řádek SharePointu
                with ui.row().classes('du-vyber items-center no-wrap'):
                    ui.icon('check_circle', size='18px').classes('du-check')
                    ui.label(_pocet(len(st['vyber']), 'vybraná položka', 'vybrané položky', 'vybraných položek'))
                    ui.button('Smazat', icon='delete', color=None, on_click=smaz_vyber) \
                        .props('flat dense no-caps').classes('du-btn').tooltip('Smazat vybrané (Delete)')
                    ui.button(icon='close', color=None, on_click=zrus_vyber) \
                        .props('flat round dense').classes('du-btn').tooltip('Zrušit výběr (Esc)')
            if muze_nahravat():
                ui.button('Nahrát', icon='upload', color=None).props('flat no-caps').classes('du-btn') \
                    .on('click', js_handler=f'() => runMethod({upl.id}, "pickFiles", [])')
        if p and p['id'] is not None and p['soubory'] is None:
            with ui.row().classes('du-info items-center no-wrap'):
                ui.icon('info', size='18px').style('color: #0078d4')
                ui.label('Vidíte jen cestu ke složkám, do kterých máte přístup.')

    with koren:
        upl = ui.upload(multiple=True, auto_upload=True, max_file_size=MAX_VELIKOST,
                        on_rejected=lambda: zprava('Soubor je větší než 50 MB — nenahrán.')) \
            .classes('du-skryty')
        upl.on('start', nahravani_start, args=[])
        upl.on('finish', nahravani_konec, args=[])
        upl.on('failed', lambda: zprava('Některý soubor se nepodařilo nahrát.', 'negative'), args=[])

        with ui.element('div').classes('du-ram'):
            with ui.element('div').classes('du-panel') as panel:
                strom = ui.tree([], node_key='id', on_select=vyber) \
                    .props('no-connectors no-selection-unset icon=chevron_right').classes('du-strom')
                strom.add_slot('default-header', _SLOT_UZEL.replace('EMIT', f'$root.$refs.r{strom.id}.$emit'))
                strom.on('akce', akce)
                if spravce:     # pravý klik kamkoli do lišty mimo složky (i na kořen) = nová sekce
                    with ui.context_menu().classes('du-menu'):
                        polozka_menu('create_new_folder', _nova(None), lambda: dialog_nova_slozka(None))
                        pridej_menu_vyberu()
                    panel.on('contextmenu.capture', js_handler=_JS_MENU_RADKU_STROMU)

            with ui.element('div').classes('du-obsah') as obsah:
                zahlavi()
                tabulka = ui.table(columns=[dict(c) for c in _SLOUPCE], rows=[], row_key='klic') \
                    .props('flat').classes('du-tabulka w-full')
                tabulka.props[':sort-method'] = _RAZENI
                tabulka.add_slot('body', _SLOT_RADEK.replace('EMIT', f'$root.$refs.r{tabulka.id}.$emit'))
                with tabulka.add_slot('no-data'):
                    prazdno = ui.label('Načítám…').classes('du-prazdno')
                tabulka.on('akce', akce)

                menu_plochy = None
                if spravce:     # pravý klik do volné plochy (řádky si menu řeší samy)
                    with ui.context_menu().classes('du-menu'):
                        _, menu_plochy = polozka_menu('create_new_folder', _nova(None),
                                                      lambda: dialog_nova_slozka(aktualni()))
                        pridej_menu_vyberu()

            # Přetažení souborů do seznamu = nahrání (jen ve složce s plným přístupem);
            # jinde se drop jen zablokuje, aby prohlížeč soubor neotevřel místo aplikace.
            obsah.on('dragover', js_handler='(e) => { e.preventDefault(); '
                     'const ok = e.currentTarget.classList.contains("du-nahravani"); '
                     'e.dataTransfer.dropEffect = ok ? "copy" : "none"; '
                     'e.currentTarget.classList.toggle("du-drop", ok); }')
            obsah.on('dragleave', js_handler='(e) => { if (!e.currentTarget.contains(e.relatedTarget)) '
                     'e.currentTarget.classList.remove("du-drop"); }')
            obsah.on('drop', js_handler='(e) => { e.preventDefault(); e.currentTarget.classList.remove("du-drop"); '
                     'if (e.currentTarget.classList.contains("du-nahravani") && e.dataTransfer.files.length) '
                     f'runMethod({upl.id}, "addFiles", [e.dataTransfer.files]); }}')
            panel.on('dragover', js_handler='(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "none"; }')

    # Výběr Ctrl+klikem a klávesy Delete / Esc (tabindex: klik do modulu ho zaměří).
    koren.props('tabindex=-1')
    koren.on('click.capture', prepni_vyber, js_handler=_JS_VYBER.replace('SPRAVCE', str(spravce).lower()))
    koren.on('keydown', klavesa, js_handler=_JS_KLAVESA)
    with koren:
        ui.timer(3, hlidej)     # uložení z Office se v otevřené složce ukáže samo

    await otevri(start if isinstance(start, int) else None)
    # Průchozí složky (cesta k vlastním složkám) rovnou rozbalit — uživatel vidí jen svou větev.
    strom.expand([_klic(s['id']) for s in st['pohled']['strom'] if not s['plny']] if st['pohled'] else [])


if __name__ == '__main__':
    # ASM(1) → Obchod(2) → Nákupčí(3) → Novák(4) / Novotný(5); Novák → Archiv(6); Logistika(7)
    r = {1: None, 2: 1, 3: 2, 4: 3, 5: 3, 6: 4, 7: None}
    assert viditelnost(r, [4]) == ({4, 6}, {1, 2, 3})       # Novotný(5) ani Logistika(7) ne
    assert viditelnost(r, [99]) == (set(), set())            # neexistující složka nic neotevře
    assert viditelnost(r, [2, 4]) == ({2, 3, 4, 5, 6}, {1})
    _strom = [{'id': i, 'parent_id': p, 'nazev': n} for i, p, n in
              ((1, None, 'ASM'), (2, 1, 'Obchod'), (3, 2, 'Nákupčí'), (4, 3, 'Novák'))]
    _u = _uzly_stromu(_strom, False)[0]
    assert _u['id'] == 'koren' and _u['children'][0]['children'][0]['children'][0]['label'] == 'Nákupčí'
    assert [_id('s12', 's'), _id('f7', 'f'), _id('s12', 'f'), _id('sx', 's'), _id(None, 's')] == [12, 7, None, None, None]
    assert [_polozky(n) for n in (0, 1, 3, 5)] == ['0 položek', '1 položka', '3 položky', '5 položek']
    assert [_nova(None), _nova(_strom[0], True), _nova(_strom[1])] == ['Vytvořit sekci', 'Nová složka', 'Vytvořit podsložku']
    assert _podstrom(r, [3]) == {3, 4, 5, 6} and _podstrom(r, [99]) == set()
    assert _predci(r, 6) == [4, 3, 2, 1] and _predci({1: 2, 2: 1}, 1) == [2, 1]     # cyklus nezacyklí
    _u = _uzly_stromu(_strom, True, {'s2', 's4'})[0]
    assert _u['menu'] is False                                  # kořen = menu lišty, ne vlastní
    _obchod = _u['children'][0]['children'][0]
    assert _obchod['vyb'] and _obchod['nvyb'] == 2 and not _u['children'][0].get('vyb')
    _plan = {'polozky': [{'typ': 0, 'id': 2, 'nazev': 'Obchod', 'sekce': False, 'misto': 'ASM',
                          'podslozky': 3, 'soubory': 1, 'pristupy': 2},
                         {'typ': 1, 'id': 7, 'nazev': 'a.pdf', 'misto': 'ASM'}]}
    assert _text_mazani(_plan) == ('Opravdu smazat 2 položky?',
                                   ['Smaže se i obsah: 3 podsložky a 1 soubor.',
                                    'Zruší se 2 udělené přístupy.', 'Akci nelze vrátit zpět.'])
    assert _text_mazani({'polozky': _plan['polozky'][1:]})[0] == 'Opravdu smazat soubor „a.pdf“?'
    # Token pro Office: platný, prošlý, podvržený, nesmysl.
    _t = _token(7, 3, ted=1000)
    assert _over_token(_t, ted=1000) == (7, 3) and _over_token(_t, ted=1001 + _TOKEN_PLATNOST) is None
    assert _over_token('8' + _t[1:], ted=1000) is None
    assert all(_over_token(x) is None for x in (None, '', 'a.b', '1.2.3', '1.2.x.yy', 'č.ř'))
    assert [_timeout(x) for x in ('Second-120', 'Infinite, Second-4100000000', 'Second-5', None, 'Second-x')] \
        == [120, _ZAMEK_MAX, 60, _ZAMEK_MAX, _ZAMEK_MAX]
    assert _tokeny_zamku('<http://x/a.xlsx> (<opaquelocktoken:1234abcd-00ff> ["e"])') == {'opaquelocktoken:1234abcd-00ff'}
    assert _vlastnosti_z_xml(b' ') is None
    assert _vlastnosti_z_xml(b'<D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>') is None
    _pozadovane = _vlastnosti_z_xml(b'<propfind xmlns="DAV:"><prop><getetag/><x:Attr xmlns:x="urn:x"/></prop></propfind>')
    assert _pozadovane == ['{DAV:}getetag', '{urn:x}Attr']
    _o = _odpoved('/a b', {'getetag': '"e"', 'resourcetype': ''}, _pozadovane)
    assert '<D:getetag>"e"</D:getetag>' in _o and '<x:Attr xmlns:x="urn:x"/>' in _o and '404' in _o
    assert '<D:resourcetype/>' in _odpoved('/', {'resourcetype': ''}) and '404' not in _odpoved('/', {'a': '1'})
    try:
        _vlastnosti_z_xml(b'<nedokoncene')
        raise AssertionError('nečitelné XML musí selhat')
    except ValueError:
        pass
    assert _OFFICE['xlsx'] == 'ms-excel' and _APLIKACE[_OFFICE['DOCX'.lower()]] == 'Word'
    assert _pripona('Ceník Q3.XLSX') == 'xlsx' and _pripona('bez_pripony') == ''
    print('ok')
