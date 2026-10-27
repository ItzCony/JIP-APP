"""Datové úložiště — sekce → složky → podsložky se soubory (max 50 MB na soubor).

Vzhled po vzoru SharePointu: vlevo rozbalovací strom složek, vpravo drobečková
navigace a seznam Název / Změněno / Změnil / Velikost.

Kdo co smí (role se dědí do podsložek, platí ta vyšší):
  * Administrátor (právo `datuloziste_spravce`, admin `vse`) vidí a spravuje vše:
    zakládá sekce, přiděluje přístup ke složkám (role správce / čtenář, našeptávač
    uživatelů), vidí historii změn a může odpojit uživatele od otevřeného souboru.
  * Správce složky v ní a pod ní nahrává, upravuje v Office, maže, přesouvá
    (i přetažením myší), zakládá podsložky, píše popis složky a zamyká soubory,
    které sám nahrál. Přidělenou složku samotnou smaže / přesune jen administrátor.
  * Čtenář složku prochází, stahuje a dokumenty Office otevírá jen pro čtení.
  * Cesta k přiděleným složkám se zviditelní sama a je jen průchozí: v nadřazené
    složce vidí každý jen svou větev — ne sourozence (Novák nevidí Novotného) ani soubory.

Ctrl+klikem se vybere víc položek a přesunou / smažou se naráz (Delete, pravé tlačítko,
lišta výběru). Složka se maže i s obsahem a přístupy k ní zaniknou — nová složka
stejného jména nic nezdědí. Uzamčený soubor nikdo nezmění, nesmaže ani nepřesune
(ani se složkou, ve které leží), dokud ho neodemkne ten, kdo ho zamkl, nebo
administrátor. Soubor stejného jména (nahrání, přesun) se přepíše jen po potvrzení.
Lupa hledá v názvech jen tam, kam uživatel vidí.

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

PRAVO_SPRAVCE = 'datuloziste_spravce'    # administrátor úložiště
ROLE_SPRAVCE, ROLE_CTENAR = 'spravce', 'ctenar'     # role přístupu ke složce
_ROLE = {ROLE_SPRAVCE: 'Správce', ROLE_CTENAR: 'Čtenář'}
MAX_VELIKOST = 50 * 1024 * 1024          # 50 MB na soubor
MAX_POPIS = 2000                         # znaků popisu složky
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
                popis TEXT NULL,
                INDEX idx_parent (parent_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # nahral / nahrano = poslední změna (i uložení z Office); vlozil_id = kdo soubor
        # nahrál (jen ten ho smí zamknout); zamkl_id = zamčeno proti jakékoli změně.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS datuloziste_soubory (
                id INT AUTO_INCREMENT PRIMARY KEY,
                slozka_id INT NOT NULL,
                nazev VARCHAR(255) NOT NULL,
                disk VARCHAR(64) NOT NULL,
                velikost BIGINT NOT NULL DEFAULT 0,
                nahral VARCHAR(255),
                nahrano DATETIME DEFAULT CURRENT_TIMESTAMP,
                vlozil_id INT NULL,
                zamkl_id INT NULL,
                zamkl VARCHAR(255) NULL,
                zamceno DATETIME NULL,
                INDEX idx_slozka (slozka_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS datuloziste_pristupy (
                slozka_id INT NOT NULL,
                user_id INT NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'spravce',
                udelil VARCHAR(255),
                udeleno DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (slozka_id, user_id),
                INDEX idx_user (user_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # Historie změn pro administrátora. Název a místo se ukládají textem — záznam
        # zůstane čitelný i po smazání nebo přesunu položky.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS datuloziste_historie (
                id INT AUTO_INCREMENT PRIMARY KEY,
                kdy DATETIME DEFAULT CURRENT_TIMESTAMP,
                user_id INT NULL,
                kdo VARCHAR(255),
                akce VARCHAR(32) NOT NULL,
                nazev VARCHAR(255) NOT NULL DEFAULT '',
                misto VARCHAR(1024) NOT NULL DEFAULT '',
                detail VARCHAR(1024) NOT NULL DEFAULT '',
                slozka_id INT NULL,
                soubor_id INT NULL,
                INDEX idx_slozka (slozka_id),
                INDEX idx_soubor (soubor_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # Tabulky z předchozí verze modulu: chybějící sloupce doplnit. Dosavadní přístupy
        # dostanou roli správce (DEFAULT) — nikdo nepřijde o to, co dosud uměl.
        for tabulka, sloupec, definice in (
                ('datuloziste_slozky', 'popis', 'TEXT NULL'),
                ('datuloziste_soubory', 'vlozil_id', 'INT NULL'),
                ('datuloziste_soubory', 'zamkl_id', 'INT NULL'),
                ('datuloziste_soubory', 'zamkl', 'VARCHAR(255) NULL'),
                ('datuloziste_soubory', 'zamceno', 'DATETIME NULL'),
                ('datuloziste_pristupy', 'role', "VARCHAR(16) NOT NULL DEFAULT 'spravce'")):
            cur.execute('SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() '
                        'AND TABLE_NAME = %s AND COLUMN_NAME = %s', (tabulka, sloupec))
            if not cur.fetchone()[0]:
                cur.execute(f'ALTER TABLE {tabulka} ADD COLUMN {sloupec} {definice}')
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
def je_admin(prava) -> bool:
    """Administrátor úložiště: právo `datuloziste_spravce` nebo admin `vse`."""
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


def viditelnost(rodice: dict, granty: dict) -> tuple[set, set, set]:
    """rodice: id složky → id rodiče (None = sekce); granty: id udělené složky → role.

    Vrací (ctene, spravovane, pruchozi): ctene = udělené složky a vše pod nimi (podsložky
    i soubory), spravovane = totéž jen z udělení s rolí správce (role se dědí dolů, platí
    ta vyšší), pruchozi = předci udělených složek — z nich je vidět jen větev dolů."""
    ctene = _podstrom(rodice, granty)
    spravovane = _podstrom(rodice, [g for g, role in granty.items() if role == ROLE_SPRAVCE])
    pruchozi = {p for g in granty if g in rodice for p in _predci(rodice, g)}
    return ctene, spravovane, pruchozi - ctene


def _viditelne(user_id, admin):
    """(slozky {id: řádek}, ctene, spravovane, pruchozi) — čerstvě z DB."""
    slozky = {r['id']: r for r in _sql(_SLOZKY, vysledek='vse')}
    if admin:
        return slozky, set(slozky), set(slozky), set()
    granty = {r['slozka_id']: r['role'] for r in _sql(
        'SELECT slozka_id, role FROM datuloziste_pristupy WHERE user_id = %s', (user_id,), 'vse')}
    return (slozky, *viditelnost({k: v['parent_id'] for k, v in slozky.items()}, granty))


def _smi_menit_slozku(slozky, spravovane, admin, sid) -> bool:
    """Složku samotnou (ne jen její obsah) přesune / smaže administrátor, nebo správce
    nadřazené složky — přidělenou složku ani sekci tedy správce nesmaže."""
    return sid in slozky and (admin or slozky[sid]['parent_id'] in spravovane)


def ma_pristup_k_modulu(user_id, prava) -> bool:
    """Brána modulu: administrátor, nebo kdo má přístup aspoň do jedné složky."""
    if je_admin(prava):
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


_SOUBORY = ('SELECT id, slozka_id, nazev, velikost, nahral, nahrano, vlozil_id, zamkl_id, zamkl, zamceno '
            'FROM datuloziste_soubory')


def _nacti_pohled(user_id, admin, sid):
    """Data pro vykreslení složky (sid None = kořen se sekcemi).
    False = složka neexistuje nebo do ní uživatel nesmí."""
    slozky, ctene, spravovane, pruchozi = _viditelne(user_id, admin)
    vidi = ctene | pruchozi
    if sid is not None and sid not in vidi:
        return False
    # Jen to, co smí vidět. plny = vidí obsah, sprava = spravuje obsah (nahrává, maže…),
    # muze = smí přesunout / smazat složku samotnou.
    strom = [dict(slozky[i], plny=i in ctene, sprava=i in spravovane,
                  muze=_smi_menit_slozku(slozky, spravovane, admin, i)) for i in vidi]
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
    soubory, popis = None, ''      # None = soubory nevidí (kořen / průchozí složka)
    if sid is not None and sid in ctene:
        soubory = _sql(f'{_SOUBORY} WHERE slozka_id = %s ORDER BY nazev', (sid,), 'vse')
        popis = (_sql('SELECT popis FROM datuloziste_slozky WHERE id = %s', (sid,), 'jeden') or {}).get('popis')
    return {'id': sid, 'cesta': _cesta(slozky, sid), 'strom': strom, 'soubory': soubory,
            'slozky': [dict(s, pocet=pocty.get(s['id'], 0)) for s in deti],
            'sprava': admin if sid is None else sid in spravovane, 'popis': popis or ''}


def _hledej(user_id, admin, text, limit=200):
    """Lupa: složky a soubory, jejichž název obsahuje `text` — jen tam, kam uživatel vidí
    obsah. Velikost písmen ani diakritika nehrají roli (u souborů to zařídí collation
    sloupce). Vrací (složky, soubory) s cestou (misto) a příznaky jako _nacti_pohled."""
    slozky, ctene, spravovane, _ = _viditelne(user_id, admin)
    vzor = _abc(text)
    nalezene = sorted((dict(s, plny=True, sprava=i in spravovane, pocet=None,
                            muze=_smi_menit_slozku(slozky, spravovane, admin, i))
                       for i, s in slozky.items() if i in ctene and vzor in _abc(s['nazev'])),
                      key=lambda s: _abc(s['nazev']))[:limit]
    soubory = []
    if ctene:
        like = '%' + re.sub(r'([\\%_])', r'\\\1', text.strip()) + '%'
        kde = '' if admin else f'slozka_id IN ({_mista(len(ctene))}) AND '
        soubory = _sql(f'{_SOUBORY} WHERE {kde}nazev LIKE %s ORDER BY nazev LIMIT {int(limit)}',
                       (*(() if admin else ctene), like), 'vse')
    for s in nalezene:
        s['misto'] = _cesta_text(_cesta(slozky, s['parent_id'])) or 'Datové úložiště'
    for f in soubory:
        f['sprava'] = f['slozka_id'] in spravovane
        f['misto'] = _cesta_text(_cesta(slozky, f['slozka_id']))
    return nalezene, soubory


def _pristupy(sid):
    """Kdo má do složky přístup: přímo (jde odebrat a měnit roli) i zděděně z nadřazené složky."""
    slozky = {r['id']: r for r in _sql(_SLOZKY, vysledek='vse')}
    cesta = [s['id'] for s in _cesta(slozky, sid)]
    if not cesta:
        return []
    radky = _sql('SELECT p.slozka_id, p.user_id, p.role, u.name, u.surname, u.email '
                 'FROM datuloziste_pristupy p JOIN user u ON u.iduser = p.user_id '
                 f'WHERE p.slozka_id IN ({_mista(len(cesta))}) '
                 'ORDER BY u.surname, u.name', tuple(cesta), 'vse')
    for r in radky:
        r['zdedeno'] = None if r['slozka_id'] == sid else slozky[r['slozka_id']]['nazev']
    return radky


def _uzivatele():
    """Aktivní účty intranetu pro našeptávač (bez skrytých servisních)."""
    return [r for r in _sql('SELECT iduser, name, surname, email FROM user WHERE is_active = 1',
                            vysledek='vse')
            if not intranet_data.je_skryty_ucet(jmeno=f"{r['name'] or ''} {r['surname'] or ''}",
                                                email=r['email'] or '')]


def _napoveda(uzivatele, text, vynech=(), limit=8):
    """Našeptávač: účty, jejichž jméno nebo e-mail obsahuje všechna slova z `text` (bez
    ohledu na diakritiku a velikost písmen); `vynech` = ID, která už přístup mají."""
    slova = _abc(text).split()
    if not slova:
        return []
    shody = [u for u in uzivatele if u['iduser'] not in vynech and all(
        s in _abc(f"{u['name'] or ''} {u['surname'] or ''} {u['email'] or ''}") for s in slova)]
    return sorted(shody, key=lambda u: (_abc(u['surname'] or ''), _abc(u['name'] or '')))[:limit]


# =========================================================
# SLOŽKY A SOUBORY
# =========================================================
def _disk(nazev):
    return os.path.join(ADRESAR, os.path.basename(nazev))


def _zaznam(user_id, kdo, akce, nazev, misto='', detail='', slozka_id=None, soubor_id=None):
    """Jeden záznam historie změn (viz _AKCE)."""
    return {'user_id': user_id, 'kdo': kdo, 'akce': akce, 'nazev': nazev, 'misto': misto,
            'detail': detail, 'slozka_id': slozka_id, 'soubor_id': soubor_id}


# Akce historie změn: klíč → (popis, ikona, barva)
_AKCE = {
    'nahrano': ('Nahráno', 'upload', '#0078d4'),
    'upraveno': ('Upraveno v Office', 'edit', '#0078d4'),
    'prepsano': ('Přepsáno novou verzí', 'published_with_changes', '#8764b8'),
    'presunuto': ('Přesunuto', 'drive_file_move', '#605e5c'),
    'smazano': ('Smazáno', 'delete', '#a4262c'),
    'zamceno': ('Uzamčeno', 'lock', '#a4262c'),
    'odemceno': ('Odemčeno', 'lock_open', '#107c10'),
    'odpojeno': ('Odpojen z Office', 'logout', '#a4262c'),
    'slozka': ('Vytvořena složka', 'create_new_folder', '#605e5c'),
    'popis': ('Změněn popis složky', 'notes', '#605e5c'),
}
_SLOUCIT_UPRAVY = 15            # min — opakovaná uložení z Office jednoho člověka = jeden záznam
_HISTORIE = ('user_id', 'kdo', 'akce', 'nazev', 'misto', 'detail', 'slozka_id', 'soubor_id')
_HISTORIE_DELKY = {'kdo': 255, 'akce': 32, 'nazev': 255, 'misto': 1024, 'detail': 1024}


def _zapis_historie(*zaznamy):
    """Zapíše záznamy historie změn. Chyba historie akci nezastaví — jen se vypíše."""
    if not zaznamy:
        return
    conn = None
    try:
        conn = intranet_data.get_db_connection()
        cur = conn.cursor()
        cur.executemany(f'INSERT INTO datuloziste_historie ({", ".join(_HISTORIE)}) '
                        f'VALUES ({_mista(len(_HISTORIE))})',
                        [tuple(str(z[k] or '')[:_HISTORIE_DELKY[k]] if k in _HISTORIE_DELKY else z[k]
                               for k in _HISTORIE) for z in zaznamy])
        conn.commit()
    except Exception as e:
        print(f'[datuloziste] historie: {e}')
    finally:
        if conn:
            conn.close()


def _historie(slozka_id=None, soubor_id=None, limit=300):
    """Historie změn, nejnovější první: souboru, složky i se vším pod ní, nebo celého úložiště."""
    if soubor_id is not None:
        kde, parametry = 'WHERE soubor_id = %s', (soubor_id,)
    elif slozka_id is not None:
        rodice = {r['id']: r['parent_id'] for r in _sql('SELECT id, parent_id FROM datuloziste_slozky',
                                                         vysledek='vse')}
        ids = _podstrom(rodice, [slozka_id]) or {slozka_id}
        kde, parametry = f'WHERE slozka_id IN ({_mista(len(ids))})', tuple(ids)
    else:
        kde, parametry = '', ()
    return _sql(f'SELECT id, kdy, kdo, akce, nazev, misto, detail FROM datuloziste_historie {kde} '
                f'ORDER BY id DESC LIMIT {int(limit)}', parametry, 'vse')


def _vytvor_slozku(user_id, admin, rodic_id, nazev, kdo):
    """True = založena, jinak text chyby pro uživatele. Sekci zakládá administrátor,
    podsložku i správce nadřazené složky."""
    slozky, _, spravovane, _ = _viditelne(user_id, admin)
    if not (admin or rodic_id in spravovane):
        return 'Tady nové složky zakládat nemůžete.'
    if _sql('SELECT 1 AS x FROM datuloziste_slozky WHERE parent_id <=> %s AND nazev = %s',
            (rodic_id, nazev), 'jeden'):
        return 'Složka s tímto názvem tu už je.'
    if rodic_id is None:
        sid, pocet = _sql('INSERT INTO datuloziste_slozky (parent_id, nazev, vytvoril) VALUES (NULL, %s, %s)',
                          (nazev, kdo))
    else:
        # INSERT … SELECT čte rodiče se zámkem: souběžně mazaný rodič = složka nevznikne.
        sid, pocet = _sql('INSERT INTO datuloziste_slozky (parent_id, nazev, vytvoril) '
                          'SELECT id, %s, %s FROM datuloziste_slozky WHERE id = %s', (nazev, kdo, rodic_id))
    if not pocet:
        return 'Nadřazená složka mezitím zanikla.'
    _zapis_historie(_zaznam(user_id, kdo, 'slozka', nazev, _cesta_text(_cesta(slozky, rodic_id)), '', sid))
    _zmeneno(rodic_id)
    return True


def _pridej_pristup(sid, uid, kdo, role):
    """'pridan' | 'ma' (přístup už má) | 'neni' (složka mezitím zanikla).
    INSERT … SELECT: přístup jen k existující složce — ke smazané nikdy."""
    _, pocet = _sql('INSERT IGNORE INTO datuloziste_pristupy (slozka_id, user_id, role, udelil) '
                    'SELECT id, %s, %s, %s FROM datuloziste_slozky WHERE id = %s', (uid, role, kdo, sid))
    if pocet:
        return 'pridan'
    return 'ma' if _sql('SELECT 1 AS x FROM datuloziste_slozky WHERE id = %s', (sid,), 'jeden') else 'neni'


def _nastav_roli(sid, uid, role):
    """Změní roli přímého přístupu (zděděný se mění u složky, odkud pochází)."""
    _sql('UPDATE datuloziste_pristupy SET role = %s WHERE slozka_id = %s AND user_id = %s', (role, sid, uid))
    return True


def _odeber_pristup(sid, uid):
    _sql('DELETE FROM datuloziste_pristupy WHERE slozka_id = %s AND user_id = %s', (sid, uid))
    return True


def _soubor(user_id, admin, soubor_id):
    """Řádek souboru (+ sprava = smí ho měnit, misto = cesta) — jen když uživatel
    smí číst jeho složku."""
    s = _sql('SELECT id, slozka_id, nazev, disk, velikost, vlozil_id, zamkl_id, zamkl '
             'FROM datuloziste_soubory WHERE id = %s', (soubor_id,), 'jeden')
    if not s:
        return None
    slozky, ctene, spravovane, _ = _viditelne(user_id, admin)
    if s['slozka_id'] not in ctene:
        return None
    return dict(s, sprava=s['slozka_id'] in spravovane, misto=_cesta_text(_cesta(slozky, s['slozka_id'])))


def _plan_mazani(user_id, admin, klice):
    """Co přesně smaže výběr `klice` ('s12' složka, 'f7' soubor) — jen čtení, pro potvrzení
    (a pro dialog přesunu: přesun bere tentýž rozsah, uzamčené soubory ho zastaví stejně).

    Bere jen to, co uživatel smí: složky správce nadřazené složky a administrátor (i s celým
    obsahem), soubory správce jejich složky. Složka uvnitř jiné vybrané složky i soubor
    v mazané složce padnou s ní, samostatně se nepočítají. `zamcene` = uzamčené soubory,
    které by smazání zasáhlo — pak se nesmaže nic."""
    slozky, _, spravovane, _ = _viditelne(user_id, admin)
    rodice = {k: v['parent_id'] for k, v in slozky.items()}
    vybrane = {i for i in (_id(k, 's') for k in klice) if _smi_menit_slozku(slozky, spravovane, admin, i)}
    vrchni = sorted((i for i in vybrane if not set(_predci(rodice, i)) & vybrane),
                    key=lambda i: _abc(slozky[i]['nazev']))
    podstromy = {i: _podstrom(rodice, [i]) for i in vrchni}
    vse = set().union(*podstromy.values())
    pocty = {'soubory': {}, 'pristupy': {}}
    zamcene = []
    if vse:
        for tab, sl in (('soubory', 'datuloziste_soubory'), ('pristupy', 'datuloziste_pristupy')):
            for r in _sql(f'SELECT slozka_id, COUNT(*) AS n FROM {sl} '
                          f'WHERE slozka_id IN ({_mista(len(vse))}) GROUP BY slozka_id', tuple(vse), 'vse'):
                pocty[tab][r['slozka_id']] = int(r['n'])
        zamcene = [r['nazev'] for r in _sql('SELECT nazev FROM datuloziste_soubory WHERE zamkl_id IS NOT NULL '
                                            f'AND slozka_id IN ({_mista(len(vse))})', tuple(vse), 'vse')]
    polozky = [{'typ': 0, 'id': i, 'nazev': slozky[i]['nazev'], 'sekce': slozky[i]['parent_id'] is None,
                'slozka_id': slozky[i]['parent_id'],
                'misto': _cesta_text(_cesta(slozky, slozky[i]['parent_id'])),
                'podslozky': len(podstromy[i]) - 1,
                'soubory': sum(pocty['soubory'].get(s, 0) for s in podstromy[i]),
                'pristupy': sum(pocty['pristupy'].get(s, 0) for s in podstromy[i])} for i in vrchni]
    soubory_ids = list(dict.fromkeys(i for i in (_id(k, 'f') for k in klice) if i is not None))
    if soubory_ids:
        radky = _sql(f'SELECT id, slozka_id, nazev, zamkl_id FROM datuloziste_soubory '
                     f'WHERE id IN ({_mista(len(soubory_ids))})', tuple(soubory_ids), 'vse')
        radky = [f for f in sorted(radky, key=lambda f: _abc(f['nazev']))
                 if f['slozka_id'] in spravovane and f['slozka_id'] not in vse]
        polozky += [{'typ': 1, 'id': f['id'], 'nazev': f['nazev'], 'slozka_id': f['slozka_id'],
                     'misto': _cesta_text(_cesta(slozky, f['slozka_id']))} for f in radky]
        zamcene += [f['nazev'] for f in radky if f['zamkl_id']]
    return {'polozky': polozky, 'slozky': vse, 'zamcene': zamcene}


def _smaz_polozky(user_id, admin, slozky_ids, soubory_ids):
    """Smaže složky (i s celým obsahem) a soubory v jedné transakci a zruší přístupy ke
    smazaným složkám. Vrací (ID smazaných složek, ID smazaných souborů — i těch uvnitř
    složek, počet zrušených přístupů), nebo 'zamceno', když by smazání zasáhlo uzamčený
    soubor — pak se nesmaže nic. Oprávnění se ověří znovu (jako v _plan_mazani).

    Složky se zamykají vrstvu po vrstvě (FOR UPDATE = aktuální stav, ne snímek):
    souběžné založení podsložky, nahrání souboru nebo udělení přístupu do mazané
    složky (INSERT … SELECT nad ní) počká na konec transakce a pak neprojde."""
    mapa, _, spravovane, _ = _viditelne(user_id, admin)
    slozky_ids = [i for i in slozky_ids if _smi_menit_slozku(mapa, spravovane, admin, i)]
    soubory_ids = list(soubory_ids)
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    slozky, radky, pristupy = set(), [], 0
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
            cur.execute(f'SELECT id, disk, zamkl_id FROM datuloziste_soubory '
                        f'WHERE slozka_id IN ({_mista(len(slozky))}) FOR UPDATE', tuple(slozky))
            radky += cur.fetchall()
        if soubory_ids:
            cur.execute(f'SELECT id, slozka_id, disk, zamkl_id FROM datuloziste_soubory '
                        f'WHERE id IN ({_mista(len(soubory_ids))}) FOR UPDATE', tuple(soubory_ids))
            radky += [r for r in cur.fetchall() if r['slozka_id'] in spravovane]
        if any(r['zamkl_id'] for r in radky):
            conn.rollback()
            return 'zamceno'
        disky = {r['id']: r['disk'] for r in radky}
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
        _smaz_z_disku(_disk(disk))
    _zmeneno(*slozky, *{mapa[i]['parent_id'] for i in slozky if i in mapa}, *{r.get('slozka_id') for r in radky})
    return slozky, set(disky), pristupy


def _zapis_smazani(user_id, kdo, polozky, slozky, soubory):
    """Historie po smazání: vybrané položky z plánu, které opravdu zmizely (jejich obsah
    se nevypisuje — jen počty u složky)."""
    _zapis_historie(*[
        _zaznam(user_id, kdo, 'smazano', p['nazev'], p['misto'],
                (f'složka ({_pocet(p["podslozky"], "podsložka", "podsložky", "podsložek")}, '
                 f'{_pocet(p["soubory"], "soubor", "soubory", "souborů")})') if p['typ'] == 0 else '',
                p['slozka_id'], None if p['typ'] == 0 else p['id'])
        for p in polozky if p['id'] in (slozky if p['typ'] == 0 else soubory)])


def _presun(user_id, admin, klice, cil_id, kdo, prepsat=False):
    """Přesune položky `klice` ('s12' složka, 'f7' soubor) do složky `cil_id`. Smí správce
    (zdrojové i cílové složky; složku samotnou jen správce nadřazené) a administrátor.

    Vrací {'chyba': text} — nepřesunulo se nic; {'konflikty': [názvy]} — v cíli už jsou soubory
    stejného jména a bez `prepsat` se nic nepřesune; jinak {'presunuto': [položky], 'cil': cesta}.
    Složka se stejným názvem v cíli přesun vždy zastaví (slučovat složky by bylo matoucí),
    stejně jako uzamčený soubor — vybraný i v přesouvané složce (jako u mazání)."""
    slozky, _, spravovane, _ = _viditelne(user_id, admin)
    if cil_id not in spravovane:
        return {'chyba': 'Do této složky nemůžete přesouvat.'}
    rodice = {k: v['parent_id'] for k, v in slozky.items()}
    s_ids = list(dict.fromkeys(i for i in (_id(k, 's') for k in klice) if i in slozky))
    f_ids = list(dict.fromkeys(i for i in (_id(k, 'f') for k in klice) if i is not None))
    for i in s_ids:
        if not _smi_menit_slozku(slozky, spravovane, admin, i):
            return {'chyba': f'Složku „{slozky[i]["nazev"]}“ přesouvat nemůžete.'}
        if cil_id in _podstrom(rodice, [i]):
            return {'chyba': f'Složku „{slozky[i]["nazev"]}“ nejde přesunout do ní samé ani do její podsložky.'}
    vybrane = set(s_ids)        # složka uvnitř jiné přesouvané jde s ní, stejně tak soubory v nich
    s_ids = [i for i in s_ids if not set(_predci(rodice, i)) & vybrane]
    uvnitr = _podstrom(rodice, s_ids)
    soubory = []
    if f_ids:
        soubory = _sql(f'SELECT id, slozka_id, nazev, zamkl_id FROM datuloziste_soubory '
                       f'WHERE id IN ({_mista(len(f_ids))})', tuple(f_ids), 'vse')
        for f in soubory:
            if f['slozka_id'] not in spravovane:
                return {'chyba': f'Soubor „{f["nazev"]}“ přesouvat nemůžete.'}
        soubory = [f for f in soubory if f['slozka_id'] not in uvnitr]
    s_ids = [i for i in s_ids if slozky[i]['parent_id'] != cil_id]        # už v cíli jsou
    soubory = [f for f in soubory if f['slozka_id'] != cil_id]
    if not s_ids and not soubory:
        return {'presunuto': [], 'cil': _cesta_text(_cesta(slozky, cil_id))}
    zamcene = [f['nazev'] for f in soubory if f['zamkl_id']]
    pod = _podstrom(rodice, s_ids)
    if pod:
        zamcene += [r['nazev'] for r in _sql('SELECT nazev FROM datuloziste_soubory WHERE zamkl_id IS NOT NULL '
                                             f'AND slozka_id IN ({_mista(len(pod))})', tuple(pod), 'vse')]
    if zamcene:
        return {'chyba': _hlaska_zamcene(sorted(zamcene, key=_abc), 'přesunout')}
    v_cili ={_abc(s['nazev']) for s in slozky.values() if s['parent_id'] == cil_id}
    jmena = [_abc(slozky[i]['nazev']) for i in s_ids]
    for i in s_ids:
        if _abc(slozky[i]['nazev']) in v_cili or jmena.count(_abc(slozky[i]['nazev'])) > 1:
            return {'chyba': f'V cílové složce by byly dvě složky „{slozky[i]["nazev"]}“.'}
    jmena = [_abc(f['nazev']) for f in soubory]
    if len(set(jmena)) < len(jmena):
        return {'chyba': 'Mezi přesouvanými jsou dva soubory stejného jména.'}
    stejne = {_abc(f['nazev']): f for f in _sql('SELECT id, nazev, zamkl_id FROM datuloziste_soubory '
                                                'WHERE slozka_id = %s', (cil_id,), 'vse')}
    konflikty = [stejne[j] for j in jmena if j in stejne]
    if konflikty and not prepsat:
        return {'konflikty': [f['nazev'] for f in konflikty]}
    for f in konflikty:
        if f['zamkl_id']:
            return {'chyba': f'Soubor „{f["nazev"]}“ v cílové složce je uzamčený — nejde přepsat.'}
        if (z := _otevreny(f['id'])):
            return {'chyba': f'Soubor „{f["nazev"]}“ v cílové složce má právě otevřený {z["jmeno"]}.'}
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    stare_disky = []
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        # Cíl se zamkne: souběžné smazání počká na konec přesunu, nebo už proběhlo.
        cur.execute('SELECT id FROM datuloziste_slozky WHERE id = %s FOR UPDATE', (cil_id,))
        if not cur.fetchone():
            conn.rollback()
            return {'chyba': 'Cílová složka mezitím zanikla.'}
        zamcene = []
        if s_ids:
            # Strom čerstvě a se zámky — souběžný přesun jinam nesmí vyrobit cyklus.
            cur.execute('SELECT id, parent_id FROM datuloziste_slozky FOR UPDATE')
            aktualni = {r['id']: r['parent_id'] for r in cur.fetchall()}
            s_ids = [i for i in s_ids if i in aktualni]
            if cil_id in _podstrom(aktualni, s_ids):
                conn.rollback()
                return {'chyba': 'Složku nejde přesunout do ní samé ani do její podsložky.'}
            pod = _podstrom(aktualni, s_ids)
            if pod:     # zamknou se i soubory ve složkách: souběžné uzamčení počká, nebo už je vidět
                cur.execute(f'SELECT nazev, zamkl_id FROM datuloziste_soubory '
                            f'WHERE slozka_id IN ({_mista(len(pod))}) FOR UPDATE', tuple(pod))
                zamcene += [r['nazev'] for r in cur.fetchall() if r['zamkl_id']]
        if soubory:
            cur.execute(f'SELECT id, zamkl_id FROM datuloziste_soubory WHERE id IN ({_mista(len(soubory))}) '
                        'FOR UPDATE', tuple(f['id'] for f in soubory))
            zbyle = {r['id']: r['zamkl_id'] for r in cur.fetchall()}    # mezitím smazané se nepřesunou
            soubory = [f for f in soubory if f['id'] in zbyle]
            zamcene += [f['nazev'] for f in soubory if zbyle[f['id']]]
        if zamcene:
            conn.rollback()
            return {'chyba': _hlaska_zamcene(sorted(zamcene, key=_abc), 'přesunout')}
        if s_ids:
            cur.execute(f'UPDATE datuloziste_slozky SET parent_id = %s WHERE id IN ({_mista(len(s_ids))})',
                        (cil_id, *s_ids))
        for f in konflikty:
            cur.execute('SELECT disk, zamkl_id FROM datuloziste_soubory WHERE id = %s FOR UPDATE', (f['id'],))
            r = cur.fetchone()
            if r and r['zamkl_id']:
                conn.rollback()
                return {'chyba': f'Soubor „{f["nazev"]}“ v cílové složce je uzamčený — nejde přepsat.'}
            if r:
                cur.execute('DELETE FROM datuloziste_soubory WHERE id = %s', (f['id'],))
                stare_disky.append(r['disk'])
        if soubory:
            cur.execute(f'UPDATE datuloziste_soubory SET slozka_id = %s '
                        f'WHERE id IN ({_mista(len(soubory))})', (cil_id, *[f['id'] for f in soubory]))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    for disk in stare_disky:
        _smaz_z_disku(_disk(disk))
    cil = _cesta_text(_cesta(slozky, cil_id))
    presunuto = ([{'typ': 0, 'id': i, 'nazev': slozky[i]['nazev'],
                   'z': _cesta_text(_cesta(slozky, slozky[i]['parent_id'])), 'z_id': slozky[i]['parent_id']}
                  for i in s_ids]
                 + [{'typ': 1, 'id': f['id'], 'nazev': f['nazev'], 'z': _cesta_text(_cesta(slozky, f['slozka_id'])),
                     'z_id': f['slozka_id']} for f in soubory])
    _zapis_historie(
        *[_zaznam(user_id, kdo, 'presunuto', p['nazev'], cil, f'z „{p["z"] or "Datové úložiště"}“',
                  p['id'] if not p['typ'] else cil_id, p['id'] if p['typ'] else None) for p in presunuto],
        *[_zaznam(user_id, kdo, 'smazano', f['nazev'], cil, 'přepsán přesunutým souborem', cil_id, f['id'])
          for f in konflikty])
    _zmeneno(cil_id, *[p['z_id'] for p in presunuto])
    return {'presunuto': presunuto, 'cil': cil}


def _zamkni(user_id, admin, soubor_id, kdo, zamknout):
    """Uzamkne / odemkne soubor proti jakékoli změně. Zamknout smí ten, kdo soubor nahrál
    (a smí ve složce spravovat obsah), a administrátor; odemknout ten, kdo zamkl, a
    administrátor. True, nebo text chyby."""
    f = _soubor(user_id, admin, soubor_id)
    if not f:
        return 'Soubor už neexistuje.'
    if zamknout:
        if not (admin or (f['sprava'] and f['vlozil_id'] == user_id)):
            return 'Uzamknout můžete jen soubory, které jste sem nahráli.'
        if (z := _otevreny(soubor_id)):
            return f'Soubor má právě otevřený {z["jmeno"]} — uzamknout ho půjde, až ho zavře.'
        _, pocet = _sql('UPDATE datuloziste_soubory SET zamkl_id = %s, zamkl = %s, zamceno = NOW() '
                        'WHERE id = %s AND zamkl_id IS NULL', (user_id, kdo, soubor_id))
        if not pocet:
            return 'Soubor už je uzamčený.'
    else:
        if not f['zamkl_id']:
            return 'Soubor už je odemčený.'
        if not (admin or f['zamkl_id'] == user_id):
            return 'Odemknout ho může jen ten, kdo ho uzamkl, nebo administrátor.'
        _sql('UPDATE datuloziste_soubory SET zamkl_id = NULL, zamkl = NULL, zamceno = NULL WHERE id = %s',
             (soubor_id,))
    _zapis_historie(_zaznam(user_id, kdo, 'zamceno' if zamknout else 'odemceno', f['nazev'], f['misto'],
                            '' if zamknout or f['zamkl_id'] == user_id else f'uzamkl/a {f["zamkl"]}',
                            f['slozka_id'], soubor_id))
    _zmeneno(f['slozka_id'])
    return True


def _uloz_popis(user_id, admin, sid, popis, kdo):
    """Popis složky (co v ní najdete) — mění ho správce složky. True, nebo text chyby."""
    slozky, _, spravovane, _ = _viditelne(user_id, admin)
    if sid not in spravovane:
        return 'Popis složky může měnit jen její správce.'
    popis = (popis or '').strip()[:MAX_POPIS]
    _sql('UPDATE datuloziste_slozky SET popis = %s WHERE id = %s', (popis or None, sid))
    _zapis_historie(_zaznam(user_id, kdo, 'popis', slozky[sid]['nazev'],
                            _cesta_text(_cesta(slozky, slozky[sid]['parent_id'])),
                            popis[:300] or '(popis smazán)', sid))
    _zmeneno(sid)
    return True


def _uloz_soubor(zdroj, slozka_id, nazev, kdo, user_id):
    """Zkopíruje nahraný soubor na disk a zapíše ho do DB. Vrací (velikost, id souboru),
    nebo (velikost, {'disk', 'id'}), když už ve složce soubor stejného jména je — pak se
    do DB nic nezapíše a nová verze čeká na disku, než uživatel zvolí přepsat / zrušit.
    None, když cílová složka mezitím zanikla (soubor z disku zase zmizí)."""
    disk = uuid.uuid4().hex
    os.makedirs(ADRESAR, exist_ok=True)
    zdroj.seek(0)
    with open(_disk(disk), 'wb') as cil:
        shutil.copyfileobj(zdroj, cil, 1024 * 1024)
        velikost = cil.tell()
    try:
        stejny = _sql('SELECT id FROM datuloziste_soubory WHERE slozka_id = %s AND nazev = %s LIMIT 1',
                      (slozka_id, nazev), 'jeden')
        if stejny:
            return velikost, {'disk': disk, 'id': stejny['id']}
        # INSERT … SELECT: zápis jen do existující složky (souběžné smazání ji zamkne).
        soubor_id, pocet = _sql('INSERT INTO datuloziste_soubory (slozka_id, nazev, disk, velikost, nahral, '
                                'vlozil_id) SELECT id, %s, %s, %s, %s, %s FROM datuloziste_slozky WHERE id = %s',
                                (nazev, disk, velikost, kdo, user_id, slozka_id))
    except Exception:
        _smaz_z_disku(_disk(disk))
        raise
    if not pocet:
        _smaz_z_disku(_disk(disk))
        return None
    return velikost, soubor_id


# Nahrané soubory, jejichž jméno už ve složce je: (user_id, klíč okna) → [{'disk', 'id', 'nazev',
# 'velikost', 'slozka_id', 'cesta', 'cas'}]. Čekají na rozhodnutí v okně, odkud se nahrávalo;
# co do hodiny nikdo nevyřídí (zavřené okno), z disku zmizí. Jen v event loopu.
_CEKAJICI: dict = {}
_CEKANI_MAX = 3600


def _uklid_cekajici():
    """Čekající verze, o kterých do hodiny nikdo nerozhodl (zavřené okno), z disku pryč."""
    ted = time.monotonic()
    for klic in list(_CEKAJICI):
        zbyle = []
        for c in _CEKAJICI[klic]:
            if ted - c['cas'] > _CEKANI_MAX:
                _smaz_z_disku(_disk(c['disk']))
            else:
                zbyle.append(c)
        if zbyle:
            _CEKAJICI[klic] = zbyle
        else:
            del _CEKAJICI[klic]


def _odloz(klic, konflikt):
    _uklid_cekajici()
    _CEKAJICI.setdefault(klic, []).append(dict(konflikt, cas=time.monotonic()))


def _vyzvedni(klic, polozky):
    """Rozhodnutí v okně: z čekajících odebere `polozky` a vrátí ty, které ještě čekaly
    (co mezitím uklidil _uklid_cekajici, už na disku není)."""
    vse = _CEKAJICI.get(klic, [])
    platne = [c for c in vse if any(c is x for x in polozky)]
    zbyle = [c for c in vse if not any(c is x for x in polozky)]
    if zbyle:
        _CEKAJICI[klic] = zbyle
    else:
        _CEKAJICI.pop(klic, None)
    return platne


def _prepis_nahranim(user_id, admin, c, kdo):
    """Uživatel zvolil přepsat: existující soubor dostane čekající nahranou verzi.
    True, nebo text chyby — čekající verze pak z disku zmizí (i když selže DB)."""
    try:
        f = _soubor(user_id, admin, c['id'])
        if not os.path.isfile(_disk(c['disk'])):
            chyba = 'čekal na rozhodnutí příliš dlouho — nahrajte ho znovu'
        elif not f or not f['sprava']:
            chyba = 'už neexistuje nebo ho nemůžete přepsat'
        elif f['zamkl_id']:
            chyba = f'je uzamčený ({f["zamkl"]})'
        elif (z := _otevreny(c['id'])) and z['uid'] != user_id:
            chyba = f'má právě otevřený {z["jmeno"]}'
        else:
            chyba = None
    except Exception:
        _smaz_z_disku(_disk(c['disk']))
        raise
    if chyba:
        _smaz_z_disku(_disk(c['disk']))
        return chyba
    # Při neúspěchu (i výjimce) čekající verzi z disku uklidí _prepis_soubor sám.
    vysledek = _prepis_soubor(c['id'], c['disk'], c['velikost'], kdo, user_id)
    if vysledek is not True:
        return 'mezitím někdo uzamkl' if vysledek == 'zamceno' else 'mezitím někdo smazal'
    _zapis_historie(_zaznam(user_id, kdo, 'prepsano', f['nazev'], f['misto'],
                            _velikost(c['velikost']), f['slozka_id'], f['id']))
    _zmeneno(f['slozka_id'])
    return True


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
    """None = nesmí nic (modul vypnut, neaktivní účet); jinak True/False = je administrátor."""
    if not user_id or not intranet_data.nacti_nastaveni_intranetu().get('datuloziste_zapnuty', True):
        return None
    prava, aktivni = intranet_data.ziskej_prava_a_aktivitu(user_id)
    return je_admin({p.lower() for p in prava}) if aktivni else None


def _soubor_ke_stazeni(user_id, soubor_id):
    try:
        admin = _opravneni(user_id)
        s = None if admin is None else _soubor(user_id, admin, soubor_id)
        return s if s and os.path.isfile(_disk(s['disk'])) else None
    except Exception:
        return None         # fail closed


def _cil_nahrani(user_id, slozka_id):
    """Text cesty cílové složky, nebo None, když do ní uživatel nahrávat nesmí (jen správce)."""
    try:
        admin = _opravneni(user_id)
        if admin is None:
            return None
        slozky, _, spravovane, _ = _viditelne(user_id, admin)
        return _cesta_text(_cesta(slozky, slozka_id)) if slozka_id in spravovane else None
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
    ani když uživatel během nahrávání přejde do jiné složky. Klíč okna (?k=) určuje,
    kde se uživatel rozhodne o souborech, jejichž jméno už ve složce je."""
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
        ulozeno = await asyncio.to_thread(_uloz_soubor, f.file, slozka_id, nazev, kdo, user_id)
    if ulozeno is None:         # složku mezitím někdo smazal
        return PlainTextResponse('Nenalezeno', status_code=404)
    velikost, vysledek = ulozeno
    if isinstance(vysledek, dict):      # jméno už ve složce je — rozhodne se v okně
        _odloz((user_id, request.query_params.get('k', '')[:64]),
               dict(vysledek, nazev=nazev, velikost=velikost, slozka_id=slozka_id, cesta=cesta))
        return {'upload': 'success'}
    await asyncio.to_thread(_zapis_historie, _zaznam(user_id, kdo, 'nahrano', nazev, cesta,
                                                     _velikost(velikost), slozka_id, vysledek))
    _zmeneno(slozka_id)
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
_ZMENY: dict = {}               # slozka_id → čas (monotonic) poslední změny — otevřená okna se překreslí
# Odpojení administrátorem: (soubor_id, user_id) → kdy (time.time()). Office s tokenem
# vydaným před odpojením už soubor nezamkne ani neuloží — smí jen číst.
_ODPOJENI: dict = {}


def _zmeneno(*slozky):
    """Obsah složek se změnil: okna, která je mají otevřené, se do pár sekund překreslí."""
    ted = time.monotonic()
    for sid in slozky:
        if sid is not None:
            _ZMENY[sid] = ted


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
    """(soubor_id, user_id, kdy vydán) — nebo None: podvržený, poškozený nebo prošlý token."""
    try:
        data, podpis = token.rsplit('.', 1)
        soubor_id, user_id, platnost = (int(x) for x in data.split('.'))
        platny = hmac.compare_digest(podpis.encode(), _podpis(data))
    except (AttributeError, ValueError):        # i UnicodeError
        return None
    if not platny or platnost < (ted or time.time()):
        return None
    return soubor_id, user_id, platnost - _TOKEN_PLATNOST


# ---------- DB (blokující) ----------
def _dav_kontext(soubor_id, user_id):
    """Soubor pro Office (+ cesta, jméno uživatele, zapis = smí ukládat: správce složky
    a soubor není uzamčený) — None, když k němu uživatel nesmí vůbec."""
    try:
        admin = _opravneni(user_id)
        s = None if admin is None else _sql(
            'SELECT id, slozka_id, nazev, disk, velikost, nahrano, zamkl_id, zamkl FROM datuloziste_soubory '
            'WHERE id = %s', (soubor_id,), 'jeden')
        if not s or not os.path.isfile(_disk(s['disk'])):
            return None
        slozky, ctene, spravovane, _ = _viditelne(user_id, admin)
        if s['slozka_id'] not in ctene:
            return None
        u = _sql('SELECT name, surname FROM user WHERE iduser = %s', (user_id,), 'jeden') or {}
        cesta = _cesta(slozky, s['slozka_id'])
        return dict(s, cesta=_cesta_text(cesta), slozka=cesta[-1]['nazev'] if cesta else '',
                    zapis=s['slozka_id'] in spravovane and not s['zamkl_id'],
                    kdo=f"{u.get('name') or ''} {u.get('surname') or ''}".strip() or 'Neznámý')
    except Exception:
        return None         # fail closed


def _prepis_soubor(soubor_id, disk, velikost, kdo, vlozil_id=None):
    """Nová verze (uložení z Office, přepsání nahráním): řádek souboru ukáže na nový soubor
    `disk`, starý se smaže. True; False = soubor mezitím někdo smazal; 'zamceno' = mezitím
    ho někdo uzamkl (v obou případech nová verze z disku zase zmizí). `vlozil_id` = nahrání
    nahradilo obsah — soubor pak patří tomu, kdo ho nahrál."""
    hotovo = None
    try:
        conn = intranet_data.get_db_connection()
        if not conn:
            raise RuntimeError('Databáze není dostupná.')
        try:
            cur = conn.cursor(dictionary=True, buffered=True)
            # FOR UPDATE: souběžné smazání / zamčení souboru počká, nebo už proběhlo.
            cur.execute('SELECT disk, zamkl_id FROM datuloziste_soubory WHERE id = %s FOR UPDATE',
                        (soubor_id,))
            stary = cur.fetchone()
            if stary and stary['zamkl_id']:
                stary = 'zamceno'
            elif stary:
                cur.execute('UPDATE datuloziste_soubory SET disk = %s, velikost = %s, nahral = %s, '
                            'nahrano = NOW(), vlozil_id = COALESCE(%s, vlozil_id) WHERE id = %s',
                            (disk, velikost, kdo, vlozil_id, soubor_id))
            conn.commit()
            hotovo = stary      # až po commitu — jinak by se při chybě smazala platná stará verze
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    finally:
        # Po zápisu je navíc stará verze, jinak (soubor smazán / zamčen / chyba) ta nová.
        _smaz_z_disku(_disk(hotovo['disk'] if isinstance(hotovo, dict) else disk))
    return True if isinstance(hotovo, dict) else (hotovo or False)


def _zapis_upravu(user_id, s, velikost):
    """Uložení z Office do historie. Další uložení téhož člověka do _SLOUCIT_UPRAVY minut
    jen posune čas posledního záznamu — Ctrl+S každou chvíli by historii zahltilo."""
    try:
        posledni = _sql('SELECT id, user_id, akce, kdy > NOW() - INTERVAL %s MINUTE AS cerstvy '
                        'FROM datuloziste_historie WHERE soubor_id = %s ORDER BY id DESC LIMIT 1',
                        (_SLOUCIT_UPRAVY, s['id']), 'jeden')
        if posledni and posledni['akce'] == 'upraveno' and posledni['user_id'] == user_id and posledni['cerstvy']:
            _sql('UPDATE datuloziste_historie SET kdy = NOW(), detail = %s WHERE id = %s',
                 (_velikost(velikost), posledni['id']))
            return
    except Exception as e:
        print(f'[datuloziste] historie: {e}')
        return
    _zapis_historie(_zaznam(user_id, s['kdo'], 'upraveno', s['nazev'], s['cesta'], _velikost(velikost),
                            s['slozka_id'], s['id']))


def _smaz_z_disku(cesta):
    try:
        os.remove(cesta)
    except OSError:
        pass


# ---------- WebDAV (jen to, co Office potřebuje) ----------
def _zamek(soubor_id):
    """Platný zámek souboru (prošlý zahodí), nebo None. Jen v event loopu."""
    z = _ZAMKY.get(soubor_id)
    if z and z['do'] <= time.monotonic():
        del _ZAMKY[soubor_id]
        z = None
    return z


def _otevreny(soubor_id):
    """Platný zámek z Office jen ke čtení — smí se volat i z vlákna DB."""
    z = _ZAMKY.get(soubor_id)
    return z if z and z['do'] > time.monotonic() else None


def _odpoj(soubor_id, slozka_id):
    """Administrátor odpojí toho, kdo má soubor otevřený v Office: zámek zmizí (soubor může
    otevřít někdo jiný) a jeho Office už nic neuloží. Vrací zrušený zámek, nebo None."""
    z = _zamek(soubor_id)
    if not z:
        return None
    del _ZAMKY[soubor_id]
    _ODPOJENI[(soubor_id, z['uid'])] = time.time()
    ted = time.time()
    for k in [k for k, kdy in _ODPOJENI.items() if ted - kdy > _TOKEN_PLATNOST]:
        del _ODPOJENI[k]        # starší tokeny už stejně prošly
    _zmeneno(slozka_id)
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


def _odpojen(soubor_id, user_id, vydano) -> bool:
    """Administrátor uživatele ze souboru odpojil až po vydání jeho tokenu."""
    return _ODPOJENI.get((soubor_id, user_id), 0) >= vydano


def _jen_cteni(s, user_id, href) -> Response:
    """Odpověď na LOCK / PUT, když uživatel soubor smí jen číst (čtenář, uzamčený soubor,
    odpojený administrátorem). Office pak dokument nechá jen pro čtení."""
    z = _zamek(s['id'])
    if z and z['uid'] != user_id:
        return _zamceno(href)
    return PlainTextResponse('Soubor je jen pro čtení.', status_code=403)


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
        _zmeneno(s['slozka_id'])
    return _xml(f'<D:prop xmlns:D="DAV:"><D:lockdiscovery>{_xml_zamku(z, href)}</D:lockdiscovery></D:prop>',
                200, {'Lock-Token': f'<{z["token"]}>'})


async def _dav_uloz(request, s, user_id, href, vydano):
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
    if _odpojen(s['id'], user_id, vydano):      # administrátor ho odpojil během nahrávání
        _smaz_z_disku(_disk(disk))
        return _jen_cteni(s, user_id, href)
    try:
        ulozeno = await asyncio.to_thread(_prepis_soubor, s['id'], disk, velikost, s['kdo'])
    except Exception as e:
        print(f'[datuloziste] uložení z Office: {e}')
        return PlainTextResponse('Chyba databáze', status_code=500)
    if ulozeno == 'zamceno':    # mezitím ho někdo uzamkl
        return PlainTextResponse('Soubor je uzamčený.', status_code=403)
    if not ulozeno:             # soubor mezitím někdo smazal
        return PlainTextResponse('Nenalezeno', status_code=404)
    _zmeneno(s['slozka_id'])
    await asyncio.to_thread(_zapis_upravu, user_id, s, velikost)
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
    s = await asyncio.to_thread(_dav_kontext, ids[0], ids[1]) if ids else None
    slozka = nazev == ''
    if not s or not (slozka or unicodedata.normalize('NFC', nazev) == unicodedata.normalize('NFC', s['nazev'])):
        return PlainTextResponse('Nenalezeno', status_code=404)
    user_id, vydano = ids[1], ids[2]
    # Ukládat smí správce složky do neuzamčeného souboru — a jen dokud ho administrátor neodpojí.
    zapis = s['zapis'] and not _odpojen(s['id'], user_id, vydano)
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
        return await _dav_zamkni(request, s, user_id, href) if zapis else _jen_cteni(s, user_id, href)
    if metoda == 'UNLOCK':
        z = _zamek(s['id'])
        if z and z['token'] not in _tokeny_zamku(request.headers.get('lock-token')):
            return Response(status_code=409)
        if z:
            del _ZAMKY[s['id']]
            _zmeneno(s['slozka_id'])
        return Response(status_code=204)
    if metoda == 'PUT':
        return await _dav_uloz(request, s, user_id, href, vydano) if zapis else _jen_cteni(s, user_id, href)
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


def _hlaska_zamcene(zamcene, co) -> str:
    """Proč nejde smazat / přesunout (`co`): uzamčené soubory, které by akce zasáhla."""
    n = len(zamcene)
    jmena = ', '.join(f'„{z}“' for z in zamcene[:3]) + (f' a {_pocet(n - 3, "další", "další", "dalších")}'
                                                        if n > 3 else '')
    ho = 'ho' if n == 1 else 'je'
    return (f'Nejde {co} — {_pocet(n, "uzamčený soubor", "uzamčené soubory", "uzamčených souborů")}: {jmena}. '
            f'Odemknout {ho} může ten, kdo {ho} uzamkl, nebo administrátor.')


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


def _uzly_stromu(strom, admin, vyber=()):
    """Uzly pro ui.tree — jen složky, které uživatel smí vidět. Příznaky pro menu
    a přetahování: nova = smí zakládat podsložky, cil = smí do ní přesouvat (obojí správce),
    muze = smí složku přesunout / smazat, admin = řízení přístupu a historie změn."""
    deti: dict = {}
    for s in strom:
        deti.setdefault(s['parent_id'], []).append(s)

    def uzel(s):
        ikona, barva = _SEKCE if s['parent_id'] is None else _SLOZKA
        n = _vyber({'id': _klic(s['id']), 'label': s['nazev'], 'ikona': ikona, 'barva': barva,
                    'menu': True, 'nova': _nova(s) if s['sprava'] else '', 'cil': bool(s['sprava']),
                    'muze': bool(s['muze']), 'admin': admin}, _klic(s['id']), vyber)
        pod = sorted(deti.get(s['id'], ()), key=lambda x: _abc(x['nazev']))
        if pod:
            n['children'] = [uzel(x) for x in pod]
        return n

    sekce = sorted(deti.get(None, ()), key=lambda x: _abc(x['nazev']))
    # Kořen vlastní menu nemá — pravý klik na něj i kamkoli do volné části lišty
    # otevře menu celého panelu („Vytvořit sekci“).
    return [{'id': 'koren', 'label': 'Datové úložiště', 'ikona': 'home', 'barva': '#605e5c',
             'menu': False, 'children': [uzel(s) for s in sekce]}]


def _uzly_cilu(strom, vyloucene=frozenset()):
    """Strom pro výběr cíle přesunu: zvolit jde jen složku, kterou uživatel spravuje a která
    není ve `vyloucene` (přesouvané složky a vše pod nimi). Ostatní jsou vidět kvůli cestě."""
    deti: dict = {}
    for s in strom:
        deti.setdefault(s['parent_id'], []).append(s)

    def uzel(s):
        ikona, barva = _SEKCE if s['parent_id'] is None else _SLOZKA
        n = {'id': _klic(s['id']), 'label': s['nazev'], 'ikona': ikona, 'barva': barva,
             'selectable': bool(s['sprava']) and s['id'] not in vyloucene}
        pod = sorted(deti.get(s['id'], ()), key=lambda x: _abc(x['nazev']))
        if pod:
            n['children'] = [uzel(x) for x in pod]
        return n

    return [uzel(s) for s in sorted(deti.get(None, ()), key=lambda x: _abc(x['nazev']))]


def _otevrene(soubory) -> frozenset:
    """Kdo má které ze souborů otevřené v Office — změna = překreslit seznam."""
    return frozenset((f['id'], z['jmeno']) for f in soubory or () if (z := _zamek(f['id'])))


def _radky(p, admin, user_id, vyber=(), mobil=False):
    """Řádky seznamu: nejdřív složky, pak soubory, s příznaky pro menu a přetahování
    (viz _uzly_stromu). U souboru: v jaké aplikaci Office se otevře (na mobilu žádné —
    stáhne se), zda jde do náhledu, smí-li ho uživatel měnit, kdo ho má otevřený v Office
    a kdo ho uzamkl. Výsledky lupy nesou u každé položky umístění (misto) a právo (sprava)."""
    radky = []
    for s in sorted(p['slozky'], key=lambda x: _abc(x['nazev'])):
        ikona, barva = _SEKCE if s['parent_id'] is None else _SLOZKA
        k = _klic(s['id'])
        radky.append(_vyber({
            'klic': k, 'typ': 0, 'nazev': s['nazev'], 'ikona': ikona, 'barva': barva,
            'ts': s['vytvoreno'].timestamp() if s['vytvoreno'] else 0,
            'zmeneno': _kdy(s['vytvoreno']), 'presne': _presne(s['vytvoreno']),
            'zmenil': s['vytvoril'] or '', 'bajty': s['pocet'] or 0,
            'velikost': '' if s['pocet'] is None else _polozky(s['pocet']), 'misto': s.get('misto', ''),
            'nova': _nova(s) if s['sprava'] else '', 'cil': bool(s['sprava']), 'muze': bool(s['muze']),
            'presun': bool(s['muze']), 'admin': admin}, k, vyber))
    for f in p['soubory'] or ():
        ikona, barva = _ikona(f['nazev'])
        k = f"f{f['id']}"
        pripona, zamek = _pripona(f['nazev']), _zamek(f['id'])
        sprava = bool(f.get('sprava', p.get('sprava')))
        zamceny = bool(f['zamkl_id'])
        kdo_kdy = ', '.join(x for x in (f['zamkl'], _presne(f['zamceno'])) if x)
        radky.append(_vyber({
            'klic': k, 'typ': 1, 'nazev': f['nazev'], 'ikona': ikona, 'barva': barva,
            'ts': f['nahrano'].timestamp() if f['nahrano'] else 0,
            'zmeneno': _kdy(f['nahrano']), 'presne': _presne(f['nahrano']),
            'zmenil': f['nahral'] or '', 'bajty': int(f['velikost'] or 0),
            'velikost': _velikost(f['velikost']), 'misto': f.get('misto', ''), 'slozka_id': f['slozka_id'],
            'app': '' if mobil else _APLIKACE.get(_OFFICE.get(pripona), ''),
            'nahled': pripona in _NAHLED, 'upravy': sprava and not zamceny,
            'otevren': zamek['jmeno'] if zamek else '',
            'zamceno': (f'Uzamčeno{f" ({kdo_kdy})" if kdo_kdy else ""} — nikdo ho nemůže měnit, '
                        'přesunout ani smazat.' if zamceny else ''),
            'muze': sprava, 'smaz': sprava and not zamceny, 'presun': sprava and not zamceny,
            'zamknout': not zamceny and (admin or (sprava and f['vlozil_id'] == user_id)),
            'odemknout': zamceny and (admin or f['zamkl_id'] == user_id),
            'admin': admin}, k, vyber))
    return radky


def _prazdno(p, admin, hledani=None) -> str:
    if hledani is not None:
        return f'Nic nenalezeno — žádná dostupná složka ani soubor nemá v názvu „{hledani}“.'
    if p['id'] is None:
        return ('Zatím tu nejsou žádné sekce — první založíte pravým tlačítkem myši.'
                if admin else 'Nemáte přístup do žádné složky.')
    if p['soubory'] is None:
        return 'Žádné dostupné položky.'
    if not p['sprava']:
        return 'Složka je prázdná.'
    return 'Složka je prázdná. Soubory sem přetáhněte nebo použijte „Nahrát“.'


def _nadpis_presunu(nazvy) -> str:
    return f'Přesunout „{nazvy[0]}“' if len(nazvy) == 1 else f'Přesunout {_polozky(len(nazvy))}'


def _hlaska_presunu(vysledek) -> str:
    presunuto = vysledek['presunuto']
    co = f'„{presunuto[0]["nazev"]}“' if len(presunuto) == 1 else _polozky(len(presunuto))
    return f'Přesunuto do „{vysledek["cil"]}“: {co}.'


def _jmeno_uctu(u) -> str:
    return f"{u['name'] or ''} {u['surname'] or ''}".strip() or u['email'] or '?'


def _radky_historie(zaznamy):
    """Řádky tabulky historie změn (popis, ikona a barva akce z _AKCE)."""
    radky = []
    for z in zaznamy:
        popis, ikona, barva = _AKCE.get(z['akce'], (z['akce'], 'history', '#605e5c'))
        radky.append({'id': z['id'], 'ts': z['kdy'].timestamp() if z['kdy'] else 0, 'kdy': _kdy(z['kdy']),
                      'presne': _presne(z['kdy']), 'akce': popis, 'ikona': ikona, 'barva': barva,
                      'nazev': z['nazev'], 'misto': z['misto'] or 'Datové úložiště', 'kdo': z['kdo'] or '',
                      'detail': z['detail'] or ''})
    return radky


# =========================================================
# UI — šablony a vzhled (SharePoint)
# =========================================================
# Události ze šablon jdou přes $root.$refs.r<id>: $parent by u vnořených uzlů
# stromu byl animační obal, ne samotný strom.
# Pravý klik na položku, která je součástí výběru (Ctrl+klik) víc položek.
_MENU_VYBERU = r'''
<q-item clickable v-close-popup @click="EMIT('akce', {a: 'presun_vyber'})">
  <q-item-section avatar><q-icon name="drive_file_move" /></q-item-section>
  <q-item-section>Přesunout vybrané ({{ X.nvyb }})…</q-item-section>
</q-item>
<q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz_vyber'})">
  <q-item-section avatar><q-icon name="delete" /></q-item-section>
  <q-item-section>Smazat vybrané ({{ X.nvyb }})</q-item-section>
</q-item>
<q-separator />
<q-item clickable v-close-popup @click="EMIT('akce', {a: 'zrus_vyber'})">
  <q-item-section avatar><q-icon name="deselect" /></q-item-section>
  <q-item-section>Zrušit výběr</q-item-section>
</q-item>
'''

# Menu složky — stejné ve stromu i v seznamu (X. = props.node. / props.row., KLIC = klíč složky).
_MENU_SLOZKY = r'''
<q-item clickable v-close-popup @click="EMIT('akce', {a: 'otevri', k: KLIC})">
  <q-item-section avatar><q-icon name="folder_open" /></q-item-section>
  <q-item-section>Otevřít</q-item-section>
</q-item>
<q-item v-if="X.misto" clickable v-close-popup @click="EMIT('akce', {a: 'umisteni', k: KLIC})">
  <q-item-section avatar><q-icon name="folder" /></q-item-section>
  <q-item-section>Otevřít umístění</q-item-section>
</q-item>
<q-item v-if="X.nova" clickable v-close-popup @click="EMIT('akce', {a: 'nova', k: KLIC})">
  <q-item-section avatar><q-icon name="create_new_folder" /></q-item-section>
  <q-item-section>{{ X.nova }}</q-item-section>
</q-item>
<q-item v-if="X.muze" clickable v-close-popup @click="EMIT('akce', {a: 'presun', k: KLIC})">
  <q-item-section avatar><q-icon name="drive_file_move" /></q-item-section>
  <q-item-section>Přesunout…</q-item-section>
</q-item>
<template v-if="X.admin">
  <q-item clickable v-close-popup @click="EMIT('akce', {a: 'pristup', k: KLIC})">
    <q-item-section avatar><q-icon name="manage_accounts" /></q-item-section>
    <q-item-section>Řízení přístupu</q-item-section>
  </q-item>
  <q-item clickable v-close-popup @click="EMIT('akce', {a: 'historie', k: KLIC})">
    <q-item-section avatar><q-icon name="history" /></q-item-section>
    <q-item-section>Historie změn</q-item-section>
  </q-item>
</template>
<template v-if="X.muze">
  <q-separator />
  <q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz', k: KLIC})">
    <q-item-section avatar><q-icon name="delete" /></q-item-section>
    <q-item-section>Smazat</q-item-section>
  </q-item>
</template>
'''

_MENU_SOUBORU = r'''
<q-item v-if="props.row.app || props.row.nahled" clickable v-close-popup
        @click="EMIT('akce', {a: 'otevri', k: props.row.klic})">
  <q-item-section avatar><q-icon :name="props.row.ikona" :style="{color: props.row.barva}" /></q-item-section>
  <q-item-section>{{ props.row.app ? 'Otevřít v aplikaci ' + props.row.app : 'Otevřít' }}</q-item-section>
</q-item>
<q-item clickable v-close-popup @click="EMIT('akce', {a: 'stahni', k: props.row.klic})">
  <q-item-section avatar><q-icon name="download" /></q-item-section>
  <q-item-section>Stáhnout</q-item-section>
</q-item>
<q-item v-if="props.row.misto" clickable v-close-popup @click="EMIT('akce', {a: 'umisteni', k: props.row.klic})">
  <q-item-section avatar><q-icon name="folder" /></q-item-section>
  <q-item-section>Otevřít umístění</q-item-section>
</q-item>
<q-item v-if="props.row.presun" clickable v-close-popup @click="EMIT('akce', {a: 'presun', k: props.row.klic})">
  <q-item-section avatar><q-icon name="drive_file_move" /></q-item-section>
  <q-item-section>Přesunout…</q-item-section>
</q-item>
<q-item v-if="props.row.zamknout" clickable v-close-popup @click="EMIT('akce', {a: 'zamkni', k: props.row.klic})">
  <q-item-section avatar><q-icon name="lock" /></q-item-section>
  <q-item-section>Uzamknout soubor</q-item-section>
</q-item>
<q-item v-if="props.row.odemknout" clickable v-close-popup @click="EMIT('akce', {a: 'odemkni', k: props.row.klic})">
  <q-item-section avatar><q-icon name="lock_open" /></q-item-section>
  <q-item-section>Odemknout soubor</q-item-section>
</q-item>
<template v-if="props.row.admin">
  <q-item v-if="props.row.otevren" clickable v-close-popup @click="EMIT('akce', {a: 'odpoj', k: props.row.klic})">
    <q-item-section avatar><q-icon name="logout" /></q-item-section>
    <q-item-section>Odpojit uživatele ({{ props.row.otevren }})</q-item-section>
  </q-item>
  <q-item clickable v-close-popup @click="EMIT('akce', {a: 'historie', k: props.row.klic})">
    <q-item-section avatar><q-icon name="history" /></q-item-section>
    <q-item-section>Historie souboru</q-item-section>
  </q-item>
</template>
<template v-if="props.row.smaz">
  <q-separator />
  <q-item clickable v-close-popup class="du-cervena" @click="EMIT('akce', {a: 'smaz', k: props.row.klic})">
    <q-item-section avatar><q-icon name="delete" /></q-item-section>
    <q-item-section>Smazat</q-item-section>
  </q-item>
</template>
'''

# data-k: klíč pro Ctrl+klik a pravý klik mimo popisek (JS na kořeni modulu);
# draggable / data-cil: co jde táhnout a kam pustit (přesun).
_SLOT_UZEL = r'''
<div class="du-uzel row items-center no-wrap" :class="{'du-vybrano': props.node.vyb}"
     :data-k="props.node.id" :data-cil="props.node.cil ? props.node.id : null"
     :draggable="!!props.node.muze" :title="props.node.label">
  <q-icon :name="props.node.ikona" size="20px" :style="{color: props.node.barva}" class="q-mr-sm" />
  <span class="ellipsis">{{ props.node.label }}</span>
  <q-icon v-if="props.node.vyb" name="check_circle" size="16px" class="du-check q-ml-xs" />
  <q-menu v-if="props.node.menu" context-menu touch-position>
    <q-list dense class="du-menu">
      <template v-if="props.node.vyb && props.node.nvyb > 1">VYBER</template>
      <template v-else>SLOZKA</template>
    </q-list>
  </q-menu>
</div>
'''.replace('VYBER', _MENU_VYBERU.replace('X.', 'props.node.')) \
   .replace('SLOZKA', _MENU_SLOZKY.replace('X.', 'props.node.').replace('KLIC', 'props.node.id'))

_MENU_RADKU = r'''
<q-list dense class="du-menu">
  <template v-if="props.row.vyb && props.row.nvyb > 1">VYBER</template>
  <template v-else-if="props.row.typ === 0">SLOZKA</template>
  <template v-else>SOUBOR</template>
</q-list>
'''.replace('VYBER', _MENU_VYBERU.replace('X.', 'props.row.')) \
   .replace('SLOZKA', _MENU_SLOZKY.replace('X.', 'props.row.').replace('KLIC', 'props.row.klic')) \
   .replace('SOUBOR', _MENU_SOUBORU)

_SLOT_RADEK = r'''
<q-tr :props="props" class="du-radek" :class="{'du-vybrano': props.row.vyb}" :data-k="props.row.klic"
      :data-cil="props.row.cil ? props.row.klic : null" :draggable="!!props.row.presun"
      @contextmenu.stop @click="EMIT('akce', {a: 'otevri', k: props.row.klic})">
  <q-td key="nazev" :props="props">
    <div class="row items-center no-wrap">
      <q-icon :name="props.row.ikona" size="22px" :style="{color: props.row.barva}" class="q-mr-md" />
      <div class="du-jmeno column no-wrap">
        <span class="du-nazev ellipsis">{{ props.row.nazev }}</span>
        <span v-if="props.row.misto" class="du-misto ellipsis">{{ props.row.misto }}</span>
      </div>
      <q-icon v-if="props.row.zamceno" name="lock" size="16px" class="du-zamceno q-ml-sm">
        <q-tooltip>{{ props.row.zamceno }}</q-tooltip>
      </q-icon>
      <q-icon v-if="props.row.otevren" name="edit" size="16px" class="du-otevren q-ml-sm">
        <q-tooltip>Právě upravuje {{ props.row.otevren }}</q-tooltip>
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

# Strom v dialogu „Přesunout…“ — složky, kam přesouvat nejde, jsou šedé a nejdou zvolit.
_SLOT_CIL = r'''
<div class="row items-center no-wrap" :class="{'du-nelze': !props.node.selectable}">
  <q-icon :name="props.node.ikona" size="20px" class="q-mr-sm"
          :style="{color: props.node.selectable ? props.node.barva : '#c8c6c4'}" />
  <span class="ellipsis">{{ props.node.label }}</span>
</div>
'''

_SLOUPCE_HISTORIE = (
    {'name': 'kdy', 'label': 'Kdy', 'field': 'ts', 'align': 'left', 'sortable': True,
     'style': 'width: 120px', 'headerStyle': 'width: 120px'},
    {'name': 'akce', 'label': 'Akce', 'field': 'akce', 'align': 'left', 'sortable': True},
    {'name': 'nazev', 'label': 'Položka', 'field': 'nazev', 'align': 'left', 'sortable': True},
    {'name': 'kdo', 'label': 'Kdo', 'field': 'kdo', 'align': 'left', 'sortable': True},
    {'name': 'detail', 'label': 'Podrobnosti', 'field': 'detail', 'align': 'left'},
)

_SLOT_HISTORIE = r'''
<q-tr :props="props">
  <q-td key="kdy" :props="props"><span :title="props.row.presne">{{ props.row.kdy }}</span></q-td>
  <q-td key="akce" :props="props">
    <div class="row items-center no-wrap">
      <q-icon :name="props.row.ikona" size="18px" :style="{color: props.row.barva}" class="q-mr-sm" />
      <span>{{ props.row.akce }}</span>
    </div>
  </q-td>
  <q-td key="nazev" :props="props">
    <div class="du-h-nazev">{{ props.row.nazev }}</div>
    <div class="du-misto">{{ props.row.misto }}</div>
  </q-td>
  <q-td key="kdo" :props="props">{{ props.row.kdo }}</q-td>
  <q-td key="detail" :props="props"><div class="du-h-detail">{{ props.row.detail }}</div></q-td>
</q-tr>
'''

_ROLE_VOLBY = {ROLE_CTENAR: _ROLE[ROLE_CTENAR], ROLE_SPRAVCE: _ROLE[ROLE_SPRAVCE]}

# Ctrl+klik (na Macu Cmd+klik) na složku / soubor ji přidá do výběru, nebo z něj odebere
# (co vybrat nejde, rozhodne server). Zachytává se na kořeni modulu už ve fázi capture —
# strom ani řádek seznamu klik nedostanou, takže se nic neotevře. Šipka dál jen rozbaluje.
_JS_VYBER = '''(e) => {
  if (!(e.ctrlKey || e.metaKey) || e.button !== 0 || e.target.closest('.q-tree__arrow')) return;
  const radek = e.target.closest('.q-tree__node-header');
  const el = e.target.closest('[data-k]') || (radek && radek.querySelector('[data-k]'));
  if (!el) return;
  e.preventDefault();
  e.stopPropagation();
  if (!e.currentTarget.contains(document.activeElement)) e.currentTarget.focus({preventScroll: true});
  if (el.dataset.k !== 'koren') emit(el.dataset.k);
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

# Přetahování složek a souborů do jiné složky (strom, seznam, drobečková navigace). Táhnout
# jde, co uživatel smí přesunout (draggable), pustit na složku, kterou spravuje (data-cil).
# Klíč tažené položky si pamatuje kořen modulu — dataTransfer při dragover číst nejde.
# Soubory přetažené z počítače (nahrávání) si řeší seznam sám, tady se jich nic netýká.
_JS_TAH_START = '''(e) => {
  const el = e.target.closest ? e.target.closest('[data-k][draggable="true"]') : null;
  if (!el) return;
  e.currentTarget.dataset.tah = el.dataset.k;
  e.dataTransfer.setData('application/x-du', el.dataset.k);
  e.dataTransfer.effectAllowed = 'move';
}'''

_JS_TAH = '''(e) => {
  const koren = e.currentTarget, tah = koren.dataset.tah;
  if (!tah) return;
  const vycisti = (krome) => koren.querySelectorAll('.du-cil').forEach((x) => {
    if (x !== krome) x.classList.remove('du-cil');
  });
  if (e.type === 'dragend') { delete koren.dataset.tah; vycisti(null); return; }
  if (e.type === 'dragleave') { if (!koren.contains(e.relatedTarget)) vycisti(null); return; }
  const radek = e.target.closest ? e.target.closest('.q-tree__node-header') : null;
  const cil = (e.target.closest ? e.target.closest('[data-cil]') : null)
    || (radek ? radek.querySelector('[data-cil]') : null);
  const ok = !!cil && cil.dataset.cil !== tah;
  const box = ok ? (cil.closest('.q-tree__node-header') || cil) : null;
  e.preventDefault();
  e.dataTransfer.dropEffect = ok ? 'move' : 'none';
  vycisti(e.type === 'drop' ? null : box);
  if (!ok) return;
  if (e.type === 'drop') { delete koren.dataset.tah; emit({k: tah, cil: cil.dataset.cil}); }
  else box.classList.add('du-cil');
}'''

_MAX_V_DIALOGU = 8      # kolik položek dialog (mazání, přepsání) vypíše jménem

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
.du-otevren { color: #ca5010; flex-shrink: 0; }
.du-zamceno { color: #a4262c; flex-shrink: 0; }
.du-vyber { gap: 2px; padding: 0 2px 0 10px; border-radius: 4px; background: #deecf9; color: #323130; font-size: 14px; white-space: nowrap; }
.du-cteni { gap: 4px; padding: 3px 10px; border-radius: 12px; background: #f3f2f1; color: #605e5c; font-size: 13px; white-space: nowrap; cursor: default; }

.du-hlava { padding: 12px 12px 6px 16px; min-height: 56px; }
.du-drobky { overflow: hidden; }
.du-drobek { font-size: 20px; line-height: 28px; padding: 0 6px; border-radius: 4px; color: #605e5c; cursor: pointer; white-space: nowrap; }
.du-drobek:hover { background: #f3f2f1; color: #323130; }
.du-drobek-akt { color: #323130; font-weight: 600; }
.du-drobky-sep { color: #8a8886; font-size: 18px; }
.du-btn { color: #323130 !important; font-size: 14px; padding: 4px 10px; }
.du-btn .q-icon { color: #0078d4; font-size: 20px; }
.du-hledani { width: 260px; flex-shrink: 0; }
@media (max-width: 767.98px) {
  .du-drobek { font-size: 16px; line-height: 24px; }
  .du-drobky-sep { font-size: 16px; }
  .du-hlava { flex-wrap: wrap; }
  .du-hledani { width: 100%; order: 10; }
}
.du-info { margin: 0 18px 8px; padding: 8px 12px; gap: 8px; background: #f3f2f1; border-radius: 4px; font-size: 13px; color: #605e5c; }
.du-popis { margin: 0 18px 8px; padding: 10px 8px 10px 14px; gap: 10px; background: #f3f2f1; border-left: 3px solid #0078d4; border-radius: 2px; }
.du-popis-ikona { color: #0078d4; flex-shrink: 0; margin-top: 1px; }
.du-popis-text { font-size: 14px; line-height: 20px; color: #323130; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 30vh; overflow: auto; }
.du-pridat-popis { align-self: flex-start; margin: -2px 0 6px 12px; color: #605e5c !important; font-size: 13px; }

/* Seznam se posouvá uvnitř (q-table__middle); drobečky i hlavička tabulky zůstávají nahoře. */
.du-tabulka { flex: 1 1 0; min-height: 0; background: transparent; color: #323130; }
.du-tabulka thead th { position: sticky; top: 0; z-index: 1; background: #fff; font-size: 13px; font-weight: 600; color: #323130; height: 40px; }
.du-tabulka tbody td { font-size: 14px; color: #605e5c; height: 44px; }
.du-radek { cursor: pointer; }
.du-radek:hover > td { background: #f3f2f1; }
.du-radek.du-vybrano > td { background: #deecf9; }
.du-radek.du-vybrano:hover > td { background: #c7e0f4; }
.du-jmeno { min-width: 0; }
.du-nazev { color: #323130; min-width: 0; }
.du-misto { font-size: 12px; line-height: 16px; color: #8a8886; }
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

/* Přetahování: cílová složka ve stromu, v seznamu i v drobečkové navigaci. */
.du-strom .q-tree__node-header.du-cil, .du-drobek.du-cil { background: #c7e0f4 !important; box-shadow: inset 0 0 0 2px #0078d4; color: #201f1e; }
.du-radek.du-cil > td { background: #c7e0f4 !important; }
.du-radek.du-cil { outline: 2px solid #0078d4; outline-offset: -2px; }

/* Dialogy (Quasar je přesouvá mimo modul — proto bez předpony .du). */
.du-cile { max-height: 50vh; overflow: auto; border: 1px solid #edebe9; border-radius: 4px; padding: 6px 4px; }
.du-nelze { color: #a19f9d; }
.du-napoveda { max-height: 280px; overflow: auto; }
.du-role { min-width: 92px; }
.du-historie .q-table__middle { max-height: 62vh; }
.du-historie thead th { position: sticky; top: 0; z-index: 1; background: #fff; font-weight: 600; }
.du-historie tbody td { vertical-align: top; white-space: normal; font-size: 13px; }
.du-h-nazev { color: #323130; overflow-wrap: anywhere; }
.du-h-detail { max-width: 240px; overflow-wrap: anywhere; color: #605e5c; }
'''


# =========================================================
# UI
# =========================================================
async def vykresli(user_id, user_name, vsechna_prava):
    """Vstupní bod modulu (volá intranet.py)."""
    await asyncio.to_thread(inicializace_db)
    admin = je_admin(vsechna_prava)
    # pohled: otevřená složka (_nacti_pohled); hledani: výsledky lupy, nebo None; mapa: viditelné
    # složky (s právy) podle id; radky: zobrazené řádky podle klíče; vyber: klíče vybrané
    # Ctrl+klikem ('s12' složka, 'f7' soubor); nacteno: kdy se pohled naposledy četl z DB;
    # zamky: kdo má zobrazené soubory otevřené v Office; cil: naposledy žádaný pohled (složka,
    # hledaný text) — i když se ještě načítá; verze / nacita: novější načtení přebije starší
    # a hlídač během načítání nečte; menu: co je v kontextových menu plochy.
    st = {'pohled': None, 'hledani': None, 'mapa': {}, 'radky': {}, 'vyber': set(), 'nacteno': 0.0,
          'zamky': frozenset(), 'cil': None, 'verze': 0, 'nacita': 0, 'nahravani': False, 'notif': None,
          'menu': {}}
    # Nahrané soubory, jejichž jméno už ve složce je, čekají na rozhodnutí v tomhle okně.
    klic_okna = secrets.token_hex(8)
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
        """Otevřená složka (s právy), v kořeni None."""
        p = st['pohled']
        return st['mapa'].get(p['id']) if p and p['id'] is not None else None

    def muze_nahravat():
        p = st['pohled']
        return (bool(p) and p['id'] is not None and p['soubory'] is not None and p['sprava']
                and st['hledani'] is None and not st['nahravani'])

    def muze_hybat(k):
        """Smí uživatel položku (klíč z prohlížeče) přesunout / smazat — podle toho, co právě vidí."""
        s = st['mapa'].get(_id(k, 's'))
        if s is not None:
            return bool(s['muze'])
        r = st['radky'].get(k)
        return bool(r and r['typ'] == 1 and r['muze'])

    def nazev_klice(k):
        s = st['mapa'].get(_id(k, 's'))
        return s['nazev'] if s is not None else st['radky'][k]['nazev'] if k in st['radky'] else k

    # ---------- načtení a vykreslení ----------
    async def otevri(sid, hledat=None, z_lupy=False):
        """Načte složku `sid` (s `hledat` i výsledky lupy) a překreslí stránku. Otevření
        složky lupu zavře (i vyprázdní její pole — ne když hledání ukončil text v něm samém);
        novější načtení přebije starší, které ještě běží."""
        st['verze'] += 1
        verze = st['verze']
        predchozi, st['cil'] = st['cil'], (sid, hledat)
        st['nacita'] += 1
        try:
            nacteno = time.monotonic()      # změna během čtení → hlidej() načte znovu
            p = await db(_nacti_pohled, user_id, admin, sid)
            if p is False:
                zprava('Ke složce nemáte přístup nebo už neexistuje.')
                p = await db(_nacti_pohled, user_id, admin, None)
            h = None
            if p and hledat:
                nalez = await db(_hledej, user_id, admin, hledat)
                if nalez is None:
                    return
                h = {'text': hledat, 'slozky': nalez[0], 'soubory': nalez[1]}
            if not p or verze != st['verze']:
                return
        finally:
            st['nacita'] -= 1
        stary, stare = st['pohled'], st['hledani']
        if not stary or p['id'] != stary['id'] or (h and h['text']) != (stare and stare['text']):
            st['vyber'].clear()     # jiná složka / jiné hledání = nový výběr (jako v Průzkumníku)
        st.update(pohled=p, hledani=h, nacteno=nacteno, mapa={s['id']: s for s in p['strom']},
                  cil=(p['id'], hledat if h else None))   # nepřístupná složka → kořen
        # Z výběru pryč, co už neexistuje nebo s čím uživatel už hýbat nesmí.
        st['vyber'] &= ({_klic(i) for i, s in st['mapa'].items() if s['muze']}
                        | {f"f{f['id']}" for f in (h or p)['soubory'] or () if f.get('sprava', p['sprava'])})
        if h is None and predchozi and predchozi[1] and not z_lupy:
            hledani.value = ''
        zobraz()

    def zobraz():
        """Překreslí stránku podle st (bez dotazu do DB)."""
        p, h = st['pohled'], st['hledani']
        prekresli()
        if h:
            strom.deselect()        # ve výsledcích lupy klik na kteroukoli složku lupu zavře
        else:
            strom.select(_klic(p['id']))
        strom.expand(['koren'] + [_klic(s['id']) for s in p['cesta']])
        prazdno.set_text(_prazdno(p, admin, h['text'] if h else None))
        upl.props['url'] = f'/uloziste/nahrat/{p["id"] or 0}?k={klic_okna}'
        if muze_nahravat():
            obsah.classes(add='du-nahravani')
        else:
            obsah.classes(remove='du-nahravani')
        zahlavi.refresh()
        pod_hlavou.refresh()

    def prekresli():
        """Strom, seznam a kontextová menu podle st (bez dotazu do DB)."""
        p, h = st['pohled'], st['hledani']
        uzly = _uzly_stromu(p['strom'], admin, st['vyber'])
        if strom.props['nodes'] != uzly:
            strom.props['nodes'] = uzly
            strom.update()
        radky = _radky(h or p, admin, user_id, st['vyber'], mobil)
        st['radky'] = {r['klic']: r for r in radky}
        st['zamky'] = _otevrene((h or p)['soubory'])
        tabulka.update_rows(radky)
        obnov_menu()

    async def obnov():
        """Znovu načte naposledy žádaný pohled (složku, příp. s výsledky lupy) — po vlastní akci
        i po změně odjinud. Žádaný, ne zobrazený: rozběhnuté hledání se tím nezahodí."""
        if st['cil'] is not None:
            await otevri(*st['cil'])

    async def hlidej():
        """Změna odjinud (uložení z Office, nahrání, přesun…) v zobrazených složkách →
        přenačíst z DB; jiný stav otevření v Office → jen překreslit."""
        p, h = st['pohled'], st['hledani']
        if not p or st['nahravani'] or st['nacita']:
            return
        slozky = ({f['slozka_id'] for f in h['soubory']} | {s['parent_id'] for s in h['slozky']}
                  if h else {p['id']})
        if any(_ZMENY.get(s, 0) > st['nacteno'] for s in slozky if s is not None):
            await obnov()
        elif _otevrene((h or p)['soubory']) != st['zamky']:
            prekresli()

    # ---------- lupa ----------
    async def hledej(text):
        """Hledá od dvou znaků v názvech všeho, kam uživatel vidí; kratší text výsledky zavře
        (i rozběhnuté hledání). Porovnává se s žádaným pohledem, ne se zobrazeným."""
        text = ' '.join(str(text or '').split())[:100]
        hledat = text if len(text) >= 2 else None
        if st['cil'] is not None and st['cil'][1] != hledat:
            await otevri(st['cil'][0], hledat, z_lupy=True)

    async def zavri_hledani():
        hledani.value = ''
        await hledej('')

    # ---------- výběr Ctrl+klikem ----------
    def prepni_vyber(e):
        """Ctrl+klik na složku / soubor: přidat do výběru, nebo z něj odebrat. Vybrat jde
        jen to, s čím uživatel smí hýbat (přesunout, smazat)."""
        k = e.args if isinstance(e.args, str) else None
        if st['pohled'] and k and muze_hybat(k):
            st['vyber'] ^= {k}
            prekresli()
            zahlavi.refresh()

    def zrus_vyber():
        if st['vyber']:
            st['vyber'].clear()
            prekresli()
            zahlavi.refresh()

    async def smaz_vyber():
        await smaz(sorted(st['vyber']))

    async def presun_vyber():
        await dialog_presunu(sorted(st['vyber']))

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
        zapíše novou verzi sem; čtenáři a uzamčený soubor jen pro čtení), PDF / obrázek /
        text v nové záložce, ostatní se stáhne."""
        fid, jmeno = _id(radek['klic'], 'f'), radek['nazev']
        if radek['app']:
            # Adresu serveru doplní prohlížeč (location.origin) — Office se připojí přesně
            # tam, kam uživatel (testovací i ostrý portál, desktopová aplikace).
            cesta = f'{_DAV}/{_token(fid, user_id)}/{quote(jmeno, safe="")}'
            schema = f'{_OFFICE[_pripona(jmeno)]}:{"ofe" if radek["upravy"] else "ofv"}|u|'
            ui.run_javascript(f'window.location.href = {json.dumps(schema)} + location.origin + {json.dumps(cesta)}')
            pozn = ('Uložením (Ctrl+S) se změny propíší sem.' if radek['upravy']
                    else 'Jen pro čtení — změny se sem neuloží.')
            with koren:
                ui.notify(f'Otevírám v aplikaci {radek["app"]}…', type='info', position='top',
                          caption=f'{pozn} Když se {radek["app"]} nespustí, soubor si stáhněte.')
        elif radek['nahled']:
            ui.navigate.to(f'/uloziste/nahled/{fid}/{quote(jmeno, safe="")}', new_tab=True)
        else:
            ui.download.from_url(f'/uloziste/soubor/{fid}')

    async def vyber(e):
        """Klik ve stromu (Quasar posílá klíč i při odznačení — pak zůstává aktuální)."""
        p = st['pohled']
        if p and e.value is not None and (st['hledani'] or e.value != _klic(p['id'])):
            await otevri_klic(e.value)

    async def akce(e):
        """Akce z kontextových menu stromu i seznamu. Klíč přichází z prohlížeče — bere se jen
        to, co uživatel právě vidí, s právy spočtenými tady; DB funkce je ověří znovu."""
        args = e.args if isinstance(e.args, dict) else {}
        a, k = args.get('a'), args.get('k')
        radek = st['radky'].get(k)
        soubor = radek if radek and radek['typ'] == 1 else None
        slozka = st['mapa'].get(_id(k, 's'))
        if a == 'otevri' and soubor:
            otevri_soubor(soubor)
        elif a == 'otevri':
            await otevri_klic(k)
        elif a == 'stahni' and soubor:
            ui.download.from_url(f'/uloziste/soubor/{_id(k, "f")}')
        elif a == 'umisteni' and (soubor or slozka):
            await otevri(soubor['slozka_id'] if soubor else slozka['parent_id'])
        elif a == 'smaz_vyber':
            await smaz_vyber()
        elif a == 'presun_vyber':
            await presun_vyber()
        elif a == 'zrus_vyber':
            zrus_vyber()
        elif a == 'smaz' and (soubor['smaz'] if soubor else slozka and slozka['muze']):
            await smaz([k])
        elif a == 'presun' and muze_hybat(k):
            await dialog_presunu([k])
        elif a == 'nova' and (slozka['sprava'] if slozka else k == 'koren' and admin):
            dialog_nova_slozka(slozka)
        elif a in ('zamkni', 'odemkni') and soubor and soubor['zamknout' if a == 'zamkni' else 'odemknout']:
            await zamkni(soubor, a == 'zamkni')
        elif not admin:
            return
        elif a == 'pristup' and slozka:
            await dialog_pristupu(slozka)
        elif a == 'historie' and slozka:
            await dialog_historie(_cesta_text(_cesta(st['mapa'], slozka['id'])), slozka_id=slozka['id'])
        elif a == 'historie' and soubor:
            misto = soubor['misto'] or _cesta_text(st['pohled']['cesta'])
            await dialog_historie(f'{misto} / {soubor["nazev"]}', soubor_id=_id(k, 'f'))
        elif a == 'odpoj' and soubor and soubor['otevren']:
            dialog_odpojeni(soubor)

    # ---------- mazání (jedna položka z menu i celý výběr) ----------
    async def smaz(klice):
        plan = await db(_plan_mazani, user_id, admin, klice)
        if plan is None:
            return
        if not plan['polozky']:
            zprava('Není co mazat — položky už neexistují nebo je smazat nemůžete.')
            await obnov()
            return
        if plan['zamcene']:
            zprava(_hlaska_zamcene(plan['zamcene'], 'smazat'))
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
        vysledek = await db(_smaz_polozky, user_id, admin,
                            [p['id'] for p in polozky if not p['typ']], [p['id'] for p in polozky if p['typ']])
        if vysledek is None:
            return
        if vysledek == 'zamceno':
            zprava('Mezitím někdo uzamkl soubor, kterého se mazání týká — nic se nesmazalo.')
            await obnov()
            return
        smazane, smazane_soubory, _ = vysledek
        await db(_zapis_smazani, user_id, user_name, polozky, smazane, smazane_soubory)
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
        await otevri(cil, st['cil'][1] if st['cil'] else None)

    # ---------- přesun (přetažením, z menu i celého výběru) ----------
    async def presun(klice, cil, prepsat=False):
        vysledek = await db(_presun, user_id, admin, klice, cil, user_name, prepsat)
        if vysledek is None:
            return
        if 'konflikty' in vysledek:
            misto = _cesta_text(_cesta(st['mapa'], cil))
            dialog_prepsani([(n, misto) for n in vysledek['konflikty']],
                            'Přepsat je přesouvanými soubory? Původní obsah se ztratí.',
                            lambda: presun(klice, cil, True))
            return
        if 'chyba' in vysledek:
            zprava(vysledek['chyba'])
        elif not vysledek['presunuto']:
            zprava('Položky už v cílové složce jsou.', 'info')
        else:
            for x in vysledek['presunuto']:
                intranet_logger.log_activity(
                    user_name, _LOG, f'{"Přesunut soubor" if x["typ"] else "Přesunuta složka"} „{x["nazev"]}“ '
                                     f'z „{x["z"] or "Datové úložiště"}“ do „{vysledek["cil"]}“')
            zprava(_hlaska_presunu(vysledek), 'positive')
            st['vyber'] -= set(klice)
        await obnov()

    async def pretazeni(e):
        """Puštění tažené složky / souboru (s celým výběrem, když je jeho součástí) na složku."""
        args = e.args if isinstance(e.args, dict) else {}
        k, cil = args.get('k'), _id(args.get('cil'), 's')
        if not st['pohled'] or not isinstance(k, str) or cil not in st['mapa'] or not st['mapa'][cil]['sprava']:
            return
        klice = [x for x in (sorted(st['vyber']) if k in st['vyber'] else [k]) if muze_hybat(x)]
        if klice:
            await presun(klice, cil)

    async def dialog_presunu(klice):
        """Přesunout… — výběr cílové složky; zvolit jde jen složku, kterou uživatel spravuje.
        Uzamčený soubor ve výběru (i ve vybrané složce) přesun zastaví hned, ještě před volbou cíle."""
        klice = [k for k in klice if muze_hybat(k)]
        if not klice or not st['pohled']:
            return
        plan = await db(_plan_mazani, user_id, admin, klice)     # přesun bere stejný rozsah jako mazání
        if plan is None:
            return
        if plan['zamcene']:
            zprava(_hlaska_zamcene(plan['zamcene'], 'přesunout'))
            return
        p = st['pohled']
        vyloucene =_podstrom({s['id']: s['parent_id'] for s in p['strom']},
                              [i for i in (_id(k, 's') for k in klice) if i is not None])
        cile = {s['id'] for s in p['strom'] if s['sprava'] and s['id'] not in vyloucene}
        if not cile:
            zprava('Nemáte žádnou složku, kam by šlo přesouvat.')
            return
        zvoleno = {'cil': None}
        with koren, ui.dialog() as dlg, ui.card().classes('w-[30rem] max-w-full gap-3'):
            ui.label(_nadpis_presunu([nazev_klice(k) for k in klice])) \
                .classes('text-lg font-bold text-gray-800 break-words')
            ui.label('Vyberte cílovou složku:').classes('text-sm text-gray-600 -mt-2')
            vyber_cile = ui.tree(_uzly_cilu(p['strom'], vyloucene), node_key='id',
                                 on_select=lambda e: zvol(e.value)) \
                .props('no-connectors icon=chevron_right').classes('du-strom du-cile w-full')
            vyber_cile.add_slot('default-header', _SLOT_CIL)
            vyber_cile.expand([_klic(s['id']) for s in p['cesta']]
                              + [_klic(s['id']) for s in p['strom'] if not s['plny']])
            kam = ui.label('').classes('text-sm text-gray-600 break-words')

            def zvol(k):
                sid = _id(k, 's')
                zvoleno['cil'] = sid if sid in cile else None
                tlacitko.set_enabled(zvoleno['cil'] is not None)
                kam.set_text(f'Do „{_cesta_text(_cesta(st["mapa"], sid))}“' if zvoleno['cil'] is not None else '')

            async def ano():
                if zvoleno['cil'] is not None:
                    dlg.close()
                    await presun(klice, zvoleno['cil'])

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                tlacitko = ui.button('Přesunout sem', icon='drive_file_move', on_click=ano).props('color=primary')
                tlacitko.disable()
        dlg.on('hide', dlg.delete)
        dlg.open()

    def dialog_prepsani(polozky, otazka, ano, ne=None):
        """Soubory stejného jména už v cílové složce jsou: přepsat (ano), nebo zrušit (ne — i když
        se dialog jen zavře). polozky = [(název, umístění)]."""
        stav = {'vyrizeno': False}
        with koren, ui.dialog() as dlg, ui.card().classes('w-[30rem] max-w-full gap-3'):
            jeden = len(polozky) == 1
            ui.label('Soubor už existuje' if jeden else 'Soubory už existují') \
                .classes('text-lg font-bold text-gray-800')
            ui.label('V cílové složce už je soubor stejného jména:' if jeden
                     else 'V cílové složce už jsou soubory stejného jména:').classes('text-sm text-gray-700')
            with ui.column().classes('w-full gap-2'):
                for jmeno, misto in polozky[:_MAX_V_DIALOGU]:
                    ikona, barva = _ikona(jmeno)
                    with ui.row().classes('w-full items-center no-wrap gap-3'):
                        ui.icon(ikona, size='22px').style(f'color: {barva}')
                        with ui.column().classes('min-w-0 gap-0'):
                            ui.label(jmeno).classes('text-sm text-gray-800 truncate max-w-full')
                            ui.label(misto or 'Datové úložiště').classes('text-xs text-gray-500 truncate max-w-full')
                if len(polozky) > _MAX_V_DIALOGU:
                    ui.label(f'… a další ({len(polozky) - _MAX_V_DIALOGU})').classes('text-sm text-gray-500')
            ui.label(otazka).classes('text-sm text-gray-700')

            async def prepsat():
                stav['vyrizeno'] = True
                dlg.close()
                await ano()

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Přepsat', icon='published_with_changes', on_click=prepsat).props('color=primary')

        async def zavreno():
            dlg.delete()
            if not stav['vyrizeno'] and ne:
                await ne()

        dlg.on('hide', zavreno)
        dlg.open()

    # ---------- nahrávání ----------
    def nahravani_start():
        # Během nahrávání nejde přidat další soubory (Quasar by je jen zařadil
        # do fronty a po skončení zahodil).
        st['nahravani'] = True
        obsah.classes(remove='du-nahravani')
        zahlavi.refresh()
        obnov_menu()
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
        # Soubory, jejichž jméno už ve složce je: přepsat / zrušit (každý se nabídne jen jednou).
        nove = [c for c in _CEKAJICI.get((user_id, klic_okna), []) if not c.get('ukazano')]
        for c in nove:
            c['ukazano'] = True
        await obnov()
        if nove:
            dialog_prepsani([(c['nazev'], c['cesta']) for c in nove],
                            'Přepsat je nově nahranou verzí? Původní obsah se ztratí.',
                            lambda: prepis_nahrane(nove), lambda: zahod_nahrane(nove))

    async def prepis_nahrane(nove):
        hotovo, chyby = [], []
        for c in _vyzvedni((user_id, klic_okna), nove):
            r = await db(_prepis_nahranim, user_id, admin, c, user_name)
            if r is True:
                hotovo.append(c['nazev'])
                intranet_logger.log_activity(user_name, _LOG, f'Přepsán soubor „{c["nazev"]}“ '
                                             f'({_velikost(c["velikost"])}) v „{c["cesta"]}“')
            elif r:
                chyby.append(f'Soubor „{c["nazev"]}“ {r}.')
        if hotovo:
            zprava(f'Přepsáno: „{hotovo[0]}“.' if len(hotovo) == 1
                   else f'Přepsáno: {_pocet(len(hotovo), "soubor", "soubory", "souborů")}.', 'positive')
        for text in chyby:
            zprava(text, 'negative')
        await obnov()

    async def zahod_nahrane(nove):
        for c in _vyzvedni((user_id, klic_okna), nove):
            await asyncio.to_thread(_smaz_z_disku, _disk(c['disk']))

    # ---------- zámky a Office ----------
    async def zamkni(radek, zamknout):
        r = await db(_zamkni, user_id, admin, _id(radek['klic'], 'f'), user_name, zamknout)
        if r is None:
            return
        if r is True:
            misto = radek['misto'] or _cesta_text(st['pohled']['cesta'])
            intranet_logger.log_activity(user_name, _LOG, f'{"Uzamčen" if zamknout else "Odemčen"} soubor '
                                                          f'„{radek["nazev"]}“ v „{misto}“')
            zprava(f'Soubor „{radek["nazev"]}“ je uzamčený — nikdo ho teď nemůže měnit, přesunout ani smazat.'
                   if zamknout else f'Soubor „{radek["nazev"]}“ je odemčený.', 'positive')
        else:
            zprava(r)
        await obnov()

    def dialog_odpojeni(radek):
        """Administrátor odpojí toho, kdo má soubor otevřený v Office."""
        with koren, ui.dialog() as dlg, ui.card().classes('w-[30rem] max-w-full gap-3'):
            ui.label('Odpojit uživatele?').classes('text-lg font-bold text-gray-800')
            ui.label(f'Soubor „{radek["nazev"]}“ má otevřený {radek["otevren"]}.') \
                .classes('text-sm text-gray-800 break-words')
            ui.label('Jeho Office už do souboru nic neuloží — neuložené změny si může uložit jen jako '
                     'kopii. Soubor pak může otevřít a upravovat někdo jiný.').classes('text-sm text-gray-700')

            async def ano():
                dlg.close()
                await odpoj(radek)

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Odpojit', icon='logout', on_click=ano).props('color=negative')
        dlg.on('hide', dlg.delete)
        dlg.open()

    async def odpoj(radek):
        fid = _id(radek['klic'], 'f')
        z = _odpoj(fid, radek['slozka_id'])         # zámky Office žijí jen v event loopu
        if not z:
            zprava('Soubor už nikdo otevřený nemá.', 'info')
        else:
            misto = radek['misto'] or _cesta_text(st['pohled']['cesta'])
            await db(_zapis_historie, _zaznam(user_id, user_name, 'odpojeno', radek['nazev'], misto,
                                              z['jmeno'], radek['slozka_id'], fid))
            intranet_logger.log_activity(user_name, _LOG, f'Odpojen uživatel {z["jmeno"]} od souboru '
                                                          f'„{radek["nazev"]}“ v „{misto}“')
            zprava(f'Odpojeno — {z["jmeno"]} už do „{radek["nazev"]}“ nic neuloží.', 'positive')
        await obnov()

    # ---------- složky ----------
    def dialog_nova_slozka(rodic):
        """rodic = složka (s právy), None = kořen (nová sekce — jen administrátor)."""
        if not (rodic['sprava'] if rodic else admin):
            return
        with koren, ui.dialog() as dlg, ui.card().classes('w-96 max-w-full'):
            ui.label(_nova(rodic)).classes('text-lg font-bold text-gray-800')
            if rodic:
                ui.label(f'v „{rodic["nazev"]}“').classes('text-sm text-gray-500 -mt-2')
            vstup = ui.input('Název').props('outlined dense autofocus maxlength=255') \
                .classes('w-full')

            async def vytvor():
                jmeno = ' '.join((vstup.value or '').split())
                if not jmeno:
                    zprava('Zadejte název.')
                    return
                ok = await db(_vytvor_slozku, user_id, admin, rodic['id'] if rodic else None, jmeno, user_name)
                if ok is None:
                    return
                if ok is not True:
                    zprava(ok)
                    return
                intranet_logger.log_activity(
                    user_name, _LOG, f'Vytvořena složka „{jmeno}“'
                    + (f' v „{_cesta_text(_cesta(st["mapa"], rodic["id"]))}“' if rodic else ' (sekce)'))
                dlg.close()
                if rodic:
                    strom.expand([_klic(rodic['id'])])     # ať je nová složka ve stromu vidět
                await obnov()

            vstup.on('keydown.enter', vytvor)
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Vytvořit', on_click=vytvor).props('color=primary')
        dlg.on('hide', dlg.delete)
        dlg.open()

    def dialog_popisu():
        """Popis otevřené složky (co v ní najdete) — píše ho správce složky."""
        p = st['pohled']
        if not p or p['id'] is None or not p['sprava'] or p['soubory'] is None or st['hledani']:
            return
        sid, cesta_txt = p['id'], _cesta_text(p['cesta'])
        with koren, ui.dialog() as dlg, ui.card().classes('w-[34rem] max-w-full gap-3'):
            ui.label('Popis složky').classes('text-lg font-bold text-gray-800')
            ui.label(cesta_txt).classes('text-sm text-gray-500 -mt-2 break-words')
            vstup = ui.textarea(placeholder='Co v této složce najdete a k čemu slouží…', value=p['popis']) \
                .props(f'outlined autogrow autofocus counter maxlength={MAX_POPIS}').classes('w-full')

            async def uloz():
                r = await db(_uloz_popis, user_id, admin, sid, vstup.value or '', user_name)
                if r is None:
                    return
                if r is not True:
                    zprava(r)
                    return
                dlg.close()
                intranet_logger.log_activity(user_name, _LOG, f'Změněn popis složky „{cesta_txt}“')
                await obnov()

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Uložit', on_click=uloz).props('color=primary')
        dlg.on('hide', dlg.delete)
        dlg.open()

    async def dialog_pristupu(s):
        """Řízení přístupu ke složce (jen administrátor): kdo v ní je čtenář a kdo správce."""
        if not admin or not s:
            return
        sid = s['id']
        cesta_txt = _cesta_text(_cesta(st['mapa'], sid))
        uzivatele = await db(_uzivatele)
        if uzivatele is None:
            return
        stav = {'radky': [], 'shody': []}
        with koren, ui.dialog() as dlg, ui.card().classes('w-[38rem] max-w-full gap-3'):
            ui.label('Řízení přístupu').classes('text-lg font-bold text-gray-800')
            ui.label(cesta_txt).classes('text-sm text-gray-500 -mt-2 break-words')
            with ui.row().classes('w-full items-center gap-2 no-wrap'):
                vstup = ui.input(placeholder='Přidat osobu — jméno nebo e-mail') \
                    .props('outlined dense clearable autofocus').classes('flex-1')
                role = ui.toggle(dict(_ROLE_VOLBY), value=ROLE_CTENAR) \
                    .props('no-caps unelevated toggle-color=primary')
            napoveda = ui.list().props('dense bordered separator').classes('w-full du-napoveda')
            napoveda.set_visibility(False)
            ui.label('Čtenář složku prohlíží a stahuje. Správce v ní navíc nahrává, upravuje, maže, '
                     'přesouvá, zamyká a píše popis. Role platí i pro podsložky.') \
                .classes('text-xs text-gray-500')
            ui.label('Přístup mají (administrátoři úložiště vidí vše):') \
                .classes('text-sm font-semibold text-gray-700 mt-1')

            @ui.refreshable
            def seznam():
                if not stav['radky']:
                    ui.label('Zatím nikdo.').classes('text-sm text-gray-400 italic')
                for r in stav['radky']:
                    with ui.row().classes('w-full items-center gap-2 no-wrap'):
                        ui.icon('person', color='grey-6')
                        with ui.column().classes('flex-1 min-w-0 gap-0'):
                            ui.label(_jmeno_uctu(r)).classes('text-sm text-gray-800 truncate')
                            zdedeno = f" · zděděno ze složky „{r['zdedeno']}“" if r['zdedeno'] else ''
                            ui.label(f"{r['email'] or ''}{zdedeno}").classes('text-xs text-gray-500 truncate')
                        if r['zdedeno'] or r['role'] not in _ROLE_VOLBY:
                            ui.label(_ROLE.get(r['role'], r['role'])).classes('du-role text-sm text-gray-500')
                        else:
                            ui.select(dict(_ROLE_VOLBY), value=r['role'],
                                      on_change=lambda e, r=r: zmen_roli(r, e.value)) \
                                .props('dense borderless options-dense').classes('du-role')
                        if not r['zdedeno']:
                            ui.button(icon='person_remove', on_click=lambda r=r: odeber(r)) \
                                .props('flat round dense color=negative').tooltip('Odebrat přístup')

            seznam()
            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg.close).props('flat')

        def napovez(text):
            """Našeptávač: aktivní účty podle jména / e-mailu (od dvou znaků), bez těch,
            kdo už do složky přístup mají přímo."""
            text = str(text or '')
            hledat = len(_abc(text).strip()) >= 2
            vynech = {r['user_id'] for r in stav['radky'] if not r['zdedeno']}
            stav['shody'] = _napoveda(uzivatele, text, vynech) if hledat else []
            napoveda.clear()
            with napoveda:
                for u in stav['shody']:
                    with ui.item(on_click=lambda u=u: pridej(u)):
                        with ui.item_section().props('avatar'):
                            ui.icon('person_add', color='primary')
                        with ui.item_section():
                            ui.item_label(_jmeno_uctu(u))
                            ui.item_label(u['email'] or '').props('caption')
                if hledat and not stav['shody']:
                    with ui.item():
                        ui.item_section('Nikdo takový — nebo už přístup má.').classes('text-sm text-gray-500')
            napoveda.set_visibility(hledat)

        async def nacti():
            radky = await db(_pristupy, sid)
            if radky is not None:
                stav['radky'] = radky
                seznam.refresh()

        async def pridej(u):
            nova = role.value if role.value in _ROLE_VOLBY else ROLE_CTENAR
            jmeno = _jmeno_uctu(u)
            r = await db(_pridej_pristup, sid, u['iduser'], user_name, nova)
            if r is None:
                return
            if r == 'neni':
                zprava('Složka mezitím zanikla — přístup nebyl přidán.', 'negative')
                dlg.close()
                await obnov()
                return
            if r == 'pridan':
                if u['email']:
                    background_tasks.create(
                        asyncio.to_thread(_posli_email, u['email'], cesta_txt, sid, user_name),
                        name='datuloziste.email')
                intranet_logger.log_activity(user_name, _LOG, f'Přístup do „{cesta_txt}“ pro {jmeno} '
                                                              f'({_ROLE[nova].lower()})')
                zprava(f'Přístup přidán — {jmeno} ({_ROLE[nova].lower()}) dostane e-mail s odkazem.', 'positive')
            else:
                zprava(f'{jmeno} už do této složky přístup má — roli změníte v seznamu.', 'info')
            vstup.value = ''
            napovez('')
            await nacti()

        async def zmen_roli(r, nova):
            if nova == r['role'] or nova not in _ROLE_VOLBY:
                return
            if await db(_nastav_roli, sid, r['user_id'], nova) is None:
                return
            intranet_logger.log_activity(user_name, _LOG, f'Změněna role v „{cesta_txt}“: '
                                                          f'{_jmeno_uctu(r)} → {_ROLE[nova].lower()}')
            zprava(f'{_jmeno_uctu(r)}: {_ROLE[nova].lower()}.', 'positive')
            await nacti()

        async def odeber(r):
            if await db(_odeber_pristup, sid, r['user_id']) is None:
                return
            intranet_logger.log_activity(user_name, _LOG, f'Odebrán přístup do „{cesta_txt}“: {_jmeno_uctu(r)}')
            await nacti()

        vstup.on('update:model-value', lambda e: napovez(e.args), throttle=0.2, leading_events=False)
        vstup.on('keydown.enter', lambda: pridej(stav['shody'][0]) if stav['shody'] else None)
        dlg.on('hide', dlg.delete)
        await nacti()
        dlg.open()

    async def dialog_historie(nazev, slozka_id=None, soubor_id=None):
        """Historie změn (jen administrátor): souboru, složky se vším pod ní, nebo celého úložiště."""
        if not admin:
            return
        zaznamy = await db(_historie, slozka_id, soubor_id)
        if zaznamy is None:
            return
        with koren, ui.dialog() as dlg, ui.card().classes('w-[60rem] max-w-full gap-2'):
            with ui.row().classes('w-full items-center no-wrap gap-3'):
                ui.icon('history', size='26px').style('color: #0078d4')
                with ui.column().classes('min-w-0 flex-1 gap-0'):
                    ui.label('Historie změn').classes('text-lg font-bold text-gray-800')
                    ui.label(nazev).classes('text-sm text-gray-500 break-words')
                ui.button(icon='close', on_click=dlg.close).props('flat round dense')
            tab = ui.table(columns=[dict(c) for c in _SLOUPCE_HISTORIE], rows=_radky_historie(zaznamy),
                           row_key='id').props('flat dense hide-pagination').classes('du-historie w-full')
            tab.add_slot('body', _SLOT_HISTORIE)
            with tab.add_slot('no-data'):
                ui.label('Zatím žádné změny.').classes('du-prazdno')
            if len(zaznamy) >= 300:
                ui.label('Zobrazeno posledních 300 změn.').classes('text-xs text-gray-500')
        dlg.on('hide', dlg.delete)
        dlg.open()

    async def historie_slozky():
        p = st['pohled']
        if admin and p:
            if p['id'] is None:
                await dialog_historie('Celé úložiště')
            else:
                await dialog_historie(_cesta_text(p['cesta']), slozka_id=p['id'])

    # ---------- části stránky ----------
    def polozka_menu(ikona, text, klik, cervena=False):
        """Položka kontextového menu volné plochy; klik = funkce, nebo JS (běží hned v prohlížeči —
        výběr souborů k nahrání chce čerstvé gesto uživatele)."""
        js = isinstance(klik, str)
        with ui.menu_item(on_click=None if js else klik) as polozka:
            with ui.item_section().props('avatar'):
                ui.icon(ikona)
            ui.item_section(text)
        if js:
            polozka.on('click', js_handler=klik)
        if cervena:
            polozka.classes('du-cervena')
        return polozka

    def nastav_menu(menu, polozky):
        """Obsah kontextového menu (None = oddělovač); bez položek se neotevře vůbec
        a zůstane menu prohlížeče."""
        while polozky and polozky[0] is None:
            polozky = polozky[1:]
        podpis = tuple(x and (x[0], x[1], x[3]) for x in polozky)
        if st['menu'].get(menu.id) == podpis:
            return
        st['menu'][menu.id] = podpis
        menu.clear()
        with menu:
            for x in polozky:
                if x is None:
                    ui.separator()
                else:
                    polozka_menu(*x)
        menu.props.set_bool('no-parent-event', not polozky)

    def obnov_menu():
        """Kontextová menu volné plochy seznamu a lišty se stromem podle toho, co teď jde."""
        p, h = st['pohled'], st['hledani']
        n = len(st['vyber'])
        vyber = [None, ('drive_file_move', f'Přesunout vybrané ({n})…', presun_vyber, False),
                 ('delete', f'Smazat vybrané ({n})', smaz_vyber, True),
                 None, ('deselect', 'Zrušit výběr', zrus_vyber, False)] if n else []
        plocha = []
        if p and not h and p['sprava']:         # v kořeni sprava = administrátor (nová sekce)
            plocha.append(('create_new_folder', _nova(aktualni()), lambda: dialog_nova_slozka(aktualni()), False))
        if muze_nahravat():
            plocha.append(('upload', 'Nahrát soubory', f'() => runMethod({upl.id}, "pickFiles", [])', False))
        if p and not h and p['id'] is not None and p['sprava']:
            plocha.append(('notes', 'Upravit popis složky' if p['popis'] else 'Přidat popis složky',
                           dialog_popisu, False))
        if admin and p and not h:
            plocha.append(('history', 'Historie změn', historie_slozky, False))
        nastav_menu(menu_plochy, plocha + vyber)
        nastav_menu(menu_panelu, ([('create_new_folder', _nova(None), lambda: dialog_nova_slozka(None), False)]
                                  if admin else []) + vyber)

    @ui.refreshable
    def zahlavi():
        """Drobečková navigace (ve výsledcích lupy jejich nadpis) a příkazy nad seznamem."""
        p, h = st['pohled'], st['hledani']
        with ui.row().classes('du-drobky items-center gap-0 min-w-0 flex-1 no-wrap'):
            if h:
                n = len(h['slozky']) + len(h['soubory'])
                ui.label(f'Výsledky hledání „{h["text"]}“').classes('du-drobek du-drobek-akt ellipsis')
                ui.label(f'({n}{"+" if len(h["soubory"]) >= 200 else ""})').classes('du-drobek')
                ui.button(icon='close', color=None, on_click=zavri_hledani).props('flat round dense') \
                    .classes('du-btn').tooltip('Zavřít hledání (Esc)')
            else:
                body = [(None, 'Datové úložiště')] + [(s['id'], s['nazev']) for s in (p['cesta'] if p else [])]
                for i, (sid, jmeno) in enumerate(body):
                    if i:
                        ui.icon('chevron_right').classes('du-drobky-sep')
                    posledni = i == len(body) - 1
                    drobek = ui.label(jmeno).classes('du-drobek' + (' du-drobek-akt' if posledni else '')) \
                        .on('click', lambda sid=sid: otevri(sid))
                    if not posledni and sid is not None and st['mapa'].get(sid, {}).get('sprava'):
                        drobek.props(f'data-cil={_klic(sid)}')        # i sem jde položku přetáhnout
        if st['vyber']:     # lišta výběru (Ctrl+klik) — jako příkazový řádek SharePointu
            with ui.row().classes('du-vyber items-center no-wrap'):
                ui.icon('check_circle', size='18px').classes('du-check')
                ui.label(_pocet(len(st['vyber']), 'vybraná položka', 'vybrané položky', 'vybraných položek'))
                ui.button('Přesunout', icon='drive_file_move', color=None, on_click=presun_vyber) \
                    .props('flat dense no-caps').classes('du-btn').tooltip('Přesunout vybrané do jiné složky')
                ui.button('Smazat', icon='delete', color=None, on_click=smaz_vyber) \
                    .props('flat dense no-caps').classes('du-btn').tooltip('Smazat vybrané (Delete)')
                ui.button(icon='close', color=None, on_click=zrus_vyber) \
                    .props('flat round dense').classes('du-btn').tooltip('Zrušit výběr (Esc)')
        if p and not h and p['soubory'] is not None and not p['sprava']:
            with ui.row().classes('du-cteni items-center no-wrap'):
                ui.icon('visibility', size='16px')
                ui.label('Jen ke čtení')
                ui.tooltip('Soubory si můžete prohlížet a stahovat; měnit je může správce složky.')
        if muze_nahravat():
            ui.button('Nahrát', icon='upload', color=None).props('flat no-caps').classes('du-btn') \
                .on('click', js_handler=f'() => runMethod({upl.id}, "pickFiles", [])')
        if admin and p and not h:
            ui.button(icon='history', color=None, on_click=historie_slozky).props('flat round dense') \
                .classes('du-btn').tooltip('Historie změn' + ('' if p['id'] is not None else ' celého úložiště'))

    @ui.refreshable
    def pod_hlavou():
        """Pod záhlavím: popis složky (správce ho píše), nebo upozornění u průchozí složky."""
        p = st['pohled']
        if not p or st['hledani'] or p['id'] is None:
            return
        if p['soubory'] is None:
            with ui.row().classes('du-info items-center no-wrap'):
                ui.icon('info', size='18px').style('color: #0078d4')
                ui.label('Vidíte jen cestu ke složkám, do kterých máte přístup.')
        elif p['popis']:
            with ui.row().classes('du-popis items-start no-wrap'):
                ui.icon('notes', size='20px').classes('du-popis-ikona')
                ui.label(p['popis']).classes('du-popis-text flex-1 min-w-0')
                if p['sprava']:
                    ui.button(icon='edit', color=None, on_click=dialog_popisu).props('flat round dense size=sm') \
                        .classes('du-btn').tooltip('Upravit popis složky')
        elif p['sprava']:
            ui.button('Přidat popis složky', icon='add', color=None, on_click=dialog_popisu) \
                .props('flat dense no-caps size=sm').classes('du-pridat-popis')

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
                menu_panelu = ui.context_menu().classes('du-menu')     # pravý klik do volné části lišty
                panel.on('contextmenu.capture', js_handler=_JS_MENU_RADKU_STROMU)

            with ui.element('div').classes('du-obsah') as obsah:
                with ui.row().classes('du-hlava w-full items-center no-wrap gap-2'):
                    ui.button(icon='account_tree', color=None).props('flat round dense') \
                        .classes('du-btn du-jen-mobil').tooltip('Strom složek') \
                        .on('click', js_handler=f'() => getHtmlElement({panel.id}).classList.toggle("du-otevren")')
                    zahlavi()
                    hledani = ui.input(placeholder='Hledat v úložišti') \
                        .props('dense outlined clearable bg-color=white').classes('du-hledani')
                    with hledani.add_slot('prepend'):
                        ui.icon('search', size='20px')
                pod_hlavou()
                tabulka = ui.table(columns=[dict(c) for c in _SLOUPCE], rows=[], row_key='klic') \
                    .props('flat').classes('du-tabulka w-full')
                tabulka.props[':sort-method'] = _RAZENI
                tabulka.add_slot('body', _SLOT_RADEK.replace('EMIT', f'$root.$refs.r{tabulka.id}.$emit'))
                with tabulka.add_slot('no-data'):
                    prazdno = ui.label('Načítám…').classes('du-prazdno')
                tabulka.on('akce', akce)
                menu_plochy = ui.context_menu().classes('du-menu')     # řádky mají svá menu
            nastav_menu(menu_panelu, [])
            nastav_menu(menu_plochy, [])

            # Soubory přetažené z počítače do seznamu = nahrání (jen tam, kam uživatel smí
            # nahrávat); jinde se drop jen zablokuje, aby prohlížeč soubor neotevřel místo
            # aplikace. Přetahování položek uvnitř úložiště řeší kořen modulu (_JS_TAH).
            obsah.on('dragover', js_handler='(e) => { if (!e.dataTransfer.types.includes("Files")) return; '
                     'e.preventDefault(); const ok = e.currentTarget.classList.contains("du-nahravani"); '
                     'e.dataTransfer.dropEffect = ok ? "copy" : "none"; '
                     'e.currentTarget.classList.toggle("du-drop", ok); }')
            obsah.on('dragleave', js_handler='(e) => { if (!e.currentTarget.contains(e.relatedTarget)) '
                     'e.currentTarget.classList.remove("du-drop"); }')
            obsah.on('drop', js_handler='(e) => { if (!e.dataTransfer.types.includes("Files")) return; '
                     'e.preventDefault(); e.currentTarget.classList.remove("du-drop"); '
                     'if (e.currentTarget.classList.contains("du-nahravani") && e.dataTransfer.files.length) '
                     f'runMethod({upl.id}, "addFiles", [e.dataTransfer.files]); }}')
            panel.on('dragover', js_handler='(e) => { if (e.dataTransfer.types.includes("Files")) { '
                     'e.preventDefault(); e.dataTransfer.dropEffect = "none"; } }')

    # Výběr Ctrl+klikem, klávesy Delete / Esc (tabindex: klik do modulu ho zaměří), přetahování.
    koren.props('tabindex=-1')
    koren.on('click.capture', prepni_vyber, js_handler=_JS_VYBER)
    koren.on('keydown', klavesa, js_handler=_JS_KLAVESA)
    koren.on('dragstart', js_handler=_JS_TAH_START)
    for udalost in ('dragover', 'dragleave', 'dragend'):
        koren.on(udalost, js_handler=_JS_TAH)
    koren.on('drop', pretazeni, js_handler=_JS_TAH)
    # Lupa hledá už při psaní (s malou prodlevou), Enter hned, Esc ji zavře.
    hledani.on('update:model-value', lambda e: hledej(e.args), throttle=0.4, leading_events=False)
    hledani.on('keydown.enter', lambda: hledej(hledani.value))
    hledani.on('keydown.esc', zavri_hledani)
    with koren:
        ui.timer(3, hlidej)     # změny odjinud (Office, další uživatelé) se v otevřené složce ukážou samy

    await otevri(start if isinstance(start, int) else None)
    # Průchozí složky (cesta k vlastním složkám) rovnou rozbalit — uživatel vidí jen svou větev.
    strom.expand([_klic(s['id']) for s in st['pohled']['strom'] if not s['plny']] if st['pohled'] else [])


if __name__ == '__main__':
    # ASM(1) → Obchod(2) → Nákupčí(3) → Novák(4) / Novotný(5); Novák → Archiv(6); Logistika(7)
    r = {1: None, 2: 1, 3: 2, 4: 3, 5: 3, 6: 4, 7: None}
    S, C = ROLE_SPRAVCE, ROLE_CTENAR
    assert viditelnost(r, {4: C}) == ({4, 6}, set(), {1, 2, 3})         # Novotný(5) ani Logistika(7) ne
    assert viditelnost(r, {99: S}) == (set(), set(), set())              # neexistující složka nic neotevře
    assert viditelnost(r, {2: C, 4: S}) == ({2, 3, 4, 5, 6}, {4, 6}, {1})
    assert viditelnost(r, {2: S, 4: C}) == ({2, 3, 4, 5, 6}, {2, 3, 4, 5, 6}, {1})    # platí vyšší role
    assert _podstrom(r, [3]) == {3, 4, 5, 6} and _podstrom(r, [99]) == set()
    assert _predci(r, 6) == [4, 3, 2, 1] and _predci({1: 2, 2: 1}, 1) == [2, 1]     # cyklus nezacyklí
    # Uživatel spravuje Nákupčího (3): ASM a Obchod jsou jen cesta, Nováka smí přesunout, Nákupčího ne.
    _strom = [{'id': i, 'parent_id': p, 'nazev': n, 'plny': i >= 3, 'sprava': i >= 3, 'muze': i >= 4}
              for i, p, n in ((1, None, 'ASM'), (2, 1, 'Obchod'), (3, 2, 'Nákupčí'), (4, 3, 'Novák'))]
    _u = _uzly_stromu(_strom, False)[0]
    _asm = _u['children'][0]
    _nakupci = _asm['children'][0]['children'][0]
    assert _u['id'] == 'koren' and _u['menu'] is False and _nakupci['label'] == 'Nákupčí'
    assert (_asm['nova'], _asm['cil'], _asm['muze']) == ('', False, False)
    assert (_nakupci['nova'], _nakupci['cil'], _nakupci['muze']) == ('Vytvořit podsložku', True, False)
    assert _nakupci['children'][0]['muze'] and not _nakupci['admin']
    _u = _uzly_stromu(_strom, True, {'s2', 's4'})[0]
    _obchod = _u['children'][0]['children'][0]
    assert _obchod['vyb'] and _obchod['nvyb'] == 2 and not _u['children'][0].get('vyb') and _obchod['admin']
    _c = _uzly_cilu(_strom, _podstrom({s['id']: s['parent_id'] for s in _strom}, [4]))[0]
    _c3 = _c['children'][0]['children'][0]
    assert (_c['selectable'], _c3['selectable'], _c3['children'][0]['selectable']) == (False, True, False)
    assert [_id('s12', 's'), _id('f7', 'f'), _id('s12', 'f'), _id('sx', 's'), _id(None, 's')] == [12, 7, None, None, None]
    assert [_polozky(n) for n in (0, 1, 3, 5)] == ['0 položek', '1 položka', '3 položky', '5 položek']
    assert [_nova(None), _nova(_strom[0], True), _nova(_strom[1])] == ['Vytvořit sekci', 'Nová složka', 'Vytvořit podsložku']
    # Soubory: zamknout smí, kdo nahrál (a je správce), odemknout, kdo zamkl; administrátor vše.
    _ted = datetime.datetime.now()
    _soubory = [{'id': i, 'slozka_id': 3, 'nazev': f'{i}.xlsx', 'velikost': 2048, 'nahral': 'Jan', 'nahrano': _ted,
                 'vlozil_id': v, 'zamkl_id': z, 'zamkl': 'Eva' if z else None, 'zamceno': _ted if z else None}
                for i, v, z in ((1, 5, None), (2, 6, None), (3, 5, 6))]
    _p = {'id': 3, 'sprava': True, 'slozky': [], 'soubory': _soubory}
    _r = {x['klic']: x for x in _radky(_p, False, 5)}
    assert [_r[k]['zamknout'] for k in ('f1', 'f2', 'f3')] == [True, False, False]
    assert (_r['f3']['odemknout'], _r['f3']['smaz'], _r['f3']['muze'], _r['f3']['upravy']) == (False, False, True, False)
    assert not _r['f3']['presun'] and _r['f1']['presun']       # uzamčený: bez „Přesunout…“, nejde táhnout
    assert 'Eva' in _r['f3']['zamceno'] and _r['f1']['app'] == 'Excel' and _r['f1']['upravy']
    assert _radky(_p, False, 6)[2]['odemknout'] and _radky(_p, True, 1)[1]['zamknout']
    assert _radky(_p, False, 5, mobil=True)[0]['app'] == ''
    _p['sprava'] = False        # čtenář: jen otevřít a stáhnout
    assert not any(x['muze'] or x['smaz'] or x['presun'] or x['zamknout'] or x['upravy'] for x in _radky(_p, False, 5))
    # Výsledky lupy: právo a umístění nese každý soubor zvlášť.
    _h = _radky({'slozky': [], 'soubory': [dict(_soubory[0], sprava=True, misto='ASM / Obchod')]}, False, 5)
    assert _h[0]['muze'] and _h[0]['misto'] == 'ASM / Obchod'
    _lide = [{'iduser': 1, 'name': 'Šimon', 'surname': 'Novák', 'email': 'simon.novak@x.cz'},
             {'iduser': 2, 'name': 'Jana', 'surname': 'Nováková', 'email': 'jana@x.cz'},
             {'iduser': 3, 'name': 'Petr', 'surname': 'Svoboda', 'email': 'petr@x.cz'}]
    assert [u['iduser'] for u in _napoveda(_lide, 'novak')] == [1, 2]
    assert [u['iduser'] for u in _napoveda(_lide, 'SIMON nov')] == [1] and _napoveda(_lide, '  ') == []
    assert [u['iduser'] for u in _napoveda(_lide, 'nov', vynech={1})] == [2]
    assert _jmeno_uctu(_lide[0]) == 'Šimon Novák' and _jmeno_uctu({'name': None, 'surname': '', 'email': 'a@b'}) == 'a@b'
    assert _nadpis_presunu(['a.pdf']) == 'Přesunout „a.pdf“' and _nadpis_presunu(['a', 'b', 'c']) == 'Přesunout 3 položky'
    assert _hlaska_presunu({'presunuto': [{'nazev': 'a'}, {'nazev': 'b'}], 'cil': 'ASM'}) == 'Přesunuto do „ASM“: 2 položky.'
    _hist = _radky_historie([{'id': 1, 'kdy': _ted, 'kdo': 'Jan', 'akce': 'zamceno', 'nazev': 'a.xlsx',
                              'misto': '', 'detail': None}])
    assert (_hist[0]['akce'], _hist[0]['ikona'], _hist[0]['misto'], _hist[0]['detail']) == ('Uzamčeno', 'lock', 'Datové úložiště', '')
    # Čekající nahrané verze: každou jde vyzvednout jen jednou.
    _CEKAJICI.clear()
    _odloz(('u', 'k'), {'disk': 'x1', 'id': 1})
    _odloz(('u', 'k'), {'disk': 'x2', 'id': 2})
    _a = list(_CEKAJICI[('u', 'k')])
    assert _vyzvedni(('u', 'k'), _a[:1]) == _a[:1] and _CEKAJICI[('u', 'k')] == _a[1:]
    assert _vyzvedni(('u', 'k'), _a[:1]) == [] and _vyzvedni(('u', 'k'), _a) == _a[1:] and ('u', 'k') not in _CEKAJICI
    _plan = {'polozky': [{'typ': 0, 'id': 2, 'nazev': 'Obchod', 'sekce': False, 'misto': 'ASM',
                          'podslozky': 3, 'soubory': 1, 'pristupy': 2},
                         {'typ': 1, 'id': 7, 'nazev': 'a.pdf', 'misto': 'ASM'}]}
    assert _text_mazani(_plan) == ('Opravdu smazat 2 položky?',
                                   ['Smaže se i obsah: 3 podsložky a 1 soubor.',
                                    'Zruší se 2 udělené přístupy.', 'Akci nelze vrátit zpět.'])
    assert _text_mazani({'polozky': _plan['polozky'][1:]})[0] == 'Opravdu smazat soubor „a.pdf“?'
    assert _hlaska_zamcene(['a.xlsx'], 'přesunout') == ('Nejde přesunout — 1 uzamčený soubor: „a.xlsx“. '
                                                        'Odemknout ho může ten, kdo ho uzamkl, nebo administrátor.')
    assert _hlaska_zamcene(list('abcd'), 'smazat') == ('Nejde smazat — 4 uzamčené soubory: „a“, „b“, „c“ a 1 další. '
                                                       'Odemknout je může ten, kdo je uzamkl, nebo administrátor.')
    # Šablony: všechny zástupné značky nahrazené.
    assert not any(z in _SLOT_UZEL + _SLOT_RADEK for z in ('VYBER', 'SLOZKA', 'SOUBOR', 'KLIC', 'X.'))
    # Token pro Office: platný, prošlý, podvržený, nesmysl.
    _t = _token(7, 3, ted=1000)
    assert _over_token(_t, ted=1000) == (7, 3, 1000) and _over_token(_t, ted=1001 + _TOKEN_PLATNOST) is None
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
