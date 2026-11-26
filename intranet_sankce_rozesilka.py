"""
intranet_sankce_rozesilka.py — e-mailové doručování sankcí („Sankce k vystavení").

1) Databáze e-mailů dodavatelů
   Export „Dod. sankce" z ERP (.xlsx: IČO | Jméno | Info Sankce) se nahraje přes
   Rozesílka → E-maily dodavatelů. Spojovací znak je IČ. Originál souboru se drží
   v DB (jde stáhnout zpět) a kontrola ukazuje IČ bez adresy, neplatná IČ
   (kontrolní součet) a neplatné / useknuté e-maily.

2) Rozeslat dodavatelům — VŽDY jen tlačítkem, nikdy automat
   Po měsíčním importu analytiky (do 8. dne) Lenka zvolí období a jedním
   tlačítkem rozešle všechny jeho sankce, které ještě dodavateli neodešly
   (kromě stavu „Nevyfakturovat") → za každé IČ jeden e-mail s Kompletní
   sestavou (předběžné oznámení + podklad, stejné PDF jako ruční Tisk) ze
   schránky sankce.dodavatel@jip-napoje.cz. Odeslaný řádek dostane razítko
   odeslano_dod (+ adresy), takže podruhé neodejde; re-import razítko zachová
   (je v „rucni" polích sestavy). Přehled IČ bez platné adresy ukazuje
   i nákupčího, aby Lenka věděla, koho požádat o opravu kontaktu.

3) Pátek 3:00 — automat pro účtárnu (VÝCHOZÍ VYPNUTÝ)
   „Pouze podklad" všech řádků ve stavu „Fakturovat" → e-mail na účtárnu a po
   ÚSPĚŠNÉM odeslání přepne právě tyto řádky na „Odevzdáno účtárně". Když se
   odeslat nepovede, stav se nemění a automat to zkusí znovu (á 30 min).
   Zapíná / vypíná ho administrátor obálkou vedle tlačítka „Rozesílka"
   (červená přeškrtnutá = vypnuto, zelená = zapnuto) — stejně jako v Lupě.

SMTP schránky sankcí (EnvironmentFile služby, vedle JIPKA_SMTP_*):
    JIPKA_SANKCE_SMTP_USER=sankce.dodavatel@jip-napoje.cz
    JIPKA_SANKCE_SMTP_PASS=…
    volitelně JIPKA_SANKCE_SMTP_HOST / _PORT / _IMAP_HOST (jinak z JIPKA_SMTP_*)
Kontrola na serveru:
    python3 -c "import intranet_emaily as e; print(e.nacti_smtp_uctu('JIPKA_SANKCE_SMTP')['duvod'] or 'OK')"
"""
from __future__ import annotations

import asyncio
import datetime
import inspect
import io
import re
import traceback
import zipfile
from collections import OrderedDict

from nicegui import ui

import intranet_data
import intranet_emaily
import intranet_logger
import intranet_sankce as S


SANKCE_SCHRANKA = 'sankce.dodavatel@jip-napoje.cz'
UCTARNA_EMAIL = 'uctarna@jip-napoje.cz'
SMTP_PREFIX = 'JIPKA_SANKCE_SMTP'
ODESILATEL_JMENO = 'JIP východočeská, a.s. – sankce'

STAV_FAKTUROVAT = 'vyfakturovano'        # label „Fakturovat"
STAV_UCTARNA = 'odevzdano_uctarne'       # label „Odevzdáno účtárně"
# Rozesílka dodavatelům nebere řádky „Nevyfakturovat" — sankce se neuplatní,
# takže dodavatel nemá dostat ani předběžné oznámení. Ostatní stavy (Nová data,
# Rozpracováno, Nákup, Provoz, Fakturovat, Odevzdáno účtárně) se posílají.
_NEPOSILAT_STAVY = ('nevyfakturovano',)

_T_EMAILY = 'sankce_dod_emaily'          # IČ → adresy (poslední nahraný soubor)
_T_SOUBOR = 'sankce_dod_emaily_soubor'   # originály nahraných souborů (ke stažení)
_T_LOG = 'sankce_rozesilka'              # historie odeslání (dodavatelé i účtárna)
_T_BEHY = 'sankce_uctarna_behy'          # páteční sloty automatu (idempotence)
_SOUBORU_DRZET = 5
_MAX_UPLOAD = 10 * 1024 * 1024

_UCTARNA_DEN = 4            # pátek (Po = 0)
_UCTARNA_HODINA = 3         # 3:00
_UCTARNA_DOHNAT_H = 12      # neběžel-li server ve 3:00, dožene se nejpozději do 15:00
_UCTARNA_POKUSU = 6         # neúspěch → nový pokus á 30 min, po 6 pokusech se slot vzdá
_UCTARNA_PAUZA_MIN = 30
_UCTARNA_ZASEKNUTO_H = 2    # „bezi" déle než 2 h = server spadl uprostřed → zkusit znovu
_AUTOMAT_KLIC = 'sankce_uctarna_automat'   # nastavení intranetu: {'zapnuto': bool}
# Příjemci podkladu účtárně podle poboček (ozubené kolečko v Sankce k vystavení):
# {'emaily': [e1, e2], 'pobocky': {'010': 0, '011': 1, …}} — hodnota = index příjemce.
# Pobočka bez přiřazení a neznámý kód → příjemce 1.
_PRIJEMCI_KLIC = 'sankce_uctarna_prijemci'
_PRIJEMCU = 2

# Přeškrtnutí obálky (vypnuto) — stejné jako v Lupě na nákup.
_PRESKRTNUTI = ('position:absolute;left:50%;top:50%;width:2.5px;height:120%;'
                'background:currentColor;border-radius:2px;box-shadow:0 0 0 1.5px #fff;'
                'transform:translate(-50%,-50%) rotate(-45deg);pointer-events:none')

_ZAMEK_DOD = 'jipka_sankce_rozesilka_dod'
_ZAMEK_UCT = 'jipka_sankce_rozesilka_uct'

PLAN_STAV_LABEL = OrderedDict([
    ('ok', 'Připraveno'),
    ('neplatny_email', 'Jen neplatné e-maily'),
    ('bez_emailu', 'V databázi bez e-mailu'),
    ('neni_v_db', 'IČ není v databázi'),
    ('chybi_ico', 'Sankce nemá IČ'),
])
_BEH_LABEL = {
    'ok': 'Odesláno', 'prazdne': 'Nic k odeslání', 'chyba': 'Chyba', 'bezi': 'Běží',
    'nasazeno': 'Automat nasazen', 'zmeskano': 'Zmeškáno (server neběžel)',
    'vypnuto': 'Automat vypnutý',
}


# =========================================================
# IČ A E-MAILY — normalizace a kontrola
# =========================================================
_RE_IC_PRIPONA = re.compile(r'(\d{8})[A-Z0-9+\-]{1,3}')
_RE_DELIC = re.compile(r'[;,\s/|]+')


def norm_ico(v) -> str:
    """Kód dodavatele pro párování: bez mezer, velká písmena. Číslo z Excelu
    (12345678.0) → text; sedmimístné číslo dostane ztracenou vedoucí nulu."""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r'\s+', '', str(v)).upper()
    if re.fullmatch(r'\d+\.0', s):
        s = s[:-2]
    if re.fullmatch(r'\d{7}', s):
        s = s.zfill(8)
    return s


def zaklad_ico(ico: str) -> str:
    """Osmimístné IČ bez přípony ERP účtu („48362425-1", „45791678F" → 48362425…).
    Kód, který nezačíná osmi číslicemi (SPOLTTP…, zahraniční ID), vrací ''."""
    if re.fullmatch(r'\d{8}', ico or ''):
        return ico
    m = _RE_IC_PRIPONA.fullmatch(ico or '')
    return m.group(1) if m else ''


def ic_soucet_ok(ic8: str) -> bool:
    """Kontrolní součet českého/slovenského IČ (váhy 8…2, modulo 11)."""
    if not re.fullmatch(r'\d{8}', ic8 or ''):
        return False
    s = sum(int(ic8[i]) * (8 - i) for i in range(7))
    return int(ic8[7]) == (11 - s % 11) % 10


def typ_kodu(ico: str) -> str:
    """'ic' (8 číslic, příp. s příponou), 'interni' (začíná písmenem — SPOLTTP…),
    'jiny' (zahraniční / nestandardní číslo), 'prazdny'."""
    if not ico:
        return 'prazdny'
    if zaklad_ico(ico):
        return 'ic'
    if ico[0].isalpha():
        return 'interni'
    return 'jiny'


def rozbor_emailu(raw) -> dict:
    """Rozebere buňku „Info Sankce" na platné a neplatné adresy.

    Oddělovače ; , / mezera. Kus s @ se validuje. Kus bez @ s tečkou je
    nejspíš useknutá adresa („lukas.drli") → neplatný. Ostatní slova bez @
    (jméno, „nemáme kontakt") se ignorují; když v buňce není žádný @, vrátí
    se celý text jako poznámka."""
    txt = str(raw or '').strip()
    if '@' not in txt:
        return {'platne': [], 'neplatne': [], 'poznamka': txt}
    platne, neplatne, videno = [], [], set()
    for cast in _RE_DELIC.split(txt):
        c = cast.strip().strip('\'"<>()[]{}').rstrip('.')
        if not c or c.lower() in videno:
            continue
        if '@' in c:
            videno.add(c.lower())
            (platne if intranet_emaily.je_platny_email(c) else neplatne).append(c)
        elif '.' in c:
            videno.add(c.lower())
            neplatne.append(c)
    return {'platne': platne, 'neplatne': neplatne, 'poznamka': ''}


# =========================================================
# DB
# =========================================================
_DB_INIT = False


def _init_db() -> bool:
    global _DB_INIT
    if _DB_INIT:
        return True
    S.inicializace_sankce_db()          # sloupce odeslano_dod* v sankce_vystaveni
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_T_EMAILY} (
                ico VARCHAR(40) PRIMARY KEY,
                jmeno VARCHAR(255),
                emaily_raw TEXT,
                duplicit INT DEFAULT 1,
                radek INT
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_T_SOUBOR} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                nazev VARCHAR(255),
                data LONGBLOB,
                velikost INT,
                pocet INT,
                kdy DATETIME DEFAULT CURRENT_TIMESTAMP,
                kdo VARCHAR(255)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_T_LOG} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                kdy DATETIME DEFAULT CURRENT_TIMESTAMP,
                typ VARCHAR(20),
                ico VARCHAR(40),
                jmeno VARCHAR(255),
                prijemci VARCHAR(1000),
                pocet_radku INT,
                castka DOUBLE,
                ok TINYINT,
                zprava TEXT,
                spustil VARCHAR(255),
                INDEX idx_kdy (kdy)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_T_BEHY} (
                slot DATETIME PRIMARY KEY,
                stav VARCHAR(20),
                pokusy INT DEFAULT 0,
                info TEXT,
                zmeneno DATETIME
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
        _DB_INIT = True
        return True
    except Exception as e:
        print(f'[sankce-rozesilka] _init_db error: {e}')
        return False
    finally:
        conn.close()


def _zamek_ziskej(nazev: str):
    """MySQL GET_LOCK na vlastním spojení — rozesílka nesmí běžet dvakrát naráz
    (dva lidé kliknou zároveň / ruční odeslání účtárně v době automatu).
    Vrací spojení (drží zámek) nebo None."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute('SELECT GET_LOCK(%s, 0)', (nazev,))
        ok = (cur.fetchone() or [0])[0]
        cur.close()
        if ok == 1:
            return conn
    except Exception as e:
        print(f'[sankce-rozesilka] GET_LOCK {nazev}: {e}')
    try:
        conn.close()
    except Exception:
        pass
    return None


def _zamek_uvolni(conn, nazev: str):
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute('SELECT RELEASE_LOCK(%s)', (nazev,))
        cur.fetchone()
        cur.close()
    except Exception as e:
        print(f'[sankce-rozesilka] RELEASE_LOCK {nazev}: {e}')
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _zapis_log(typ, ico, jmeno, prijemci, pocet, castka, ok, zprava, spustil):
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            f'INSERT INTO {_T_LOG} (typ,ico,jmeno,prijemci,pocet_radku,castka,ok,zprava,spustil) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (typ, ico or '', (jmeno or '')[:255], ', '.join(prijemci or [])[:1000],
             pocet, castka, 1 if ok else 0, zprava or '', (spustil or '')[:255]))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f'[sankce-rozesilka] _zapis_log error: {e}')
    finally:
        conn.close()


def nacti_log(limit: int = 300) -> list:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT kdy,typ,ico,jmeno,prijemci,pocet_radku,castka,ok,zprava,spustil '
                    f'FROM {_T_LOG} ORDER BY id DESC LIMIT %s', (limit,))
        return cur.fetchall()
    finally:
        conn.close()


# =========================================================
# DATABÁZE E-MAILŮ DODAVATELŮ — import / čtení
# =========================================================
def _txt(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def precti_xlsx(raw: bytes) -> tuple:
    """Najde v .xlsx hlavičku se sloupci IČO + „Info Sankce" (nebo sloupec
    s „mail") a vrátí ([{radek, ico, ico_raw, jmeno, emaily_raw}], chyba)."""
    try:
        import openpyxl
    except ImportError:
        return [], 'Na serveru chybí knihovna openpyxl.'
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        return [], f'Soubor nejde otevřít jako .xlsx: {e}'
    try:
        for ws in wb.worksheets:
            hlav = None
            out = []
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if hlav is None:
                    if i > 40:
                        break
                    nn = [S._norm(c) for c in row]
                    c_ico = next((j for j, n in enumerate(nn)
                                  if n in ('ico', 'ic', 'icododavatele', 'icdodavatele')), None)
                    c_mail = next((j for j, n in enumerate(nn)
                                   if 'sankce' in n or 'mail' in n), None)
                    if c_ico is not None and c_mail is not None:
                        c_jm = next((j for j, n in enumerate(nn)
                                     if n in ('jmeno', 'nazev', 'dodavatel',
                                              'jmenododavatele', 'nazevdodavatele')), None)
                        hlav = (c_ico, c_jm, c_mail)
                    continue

                def _b(j):
                    return row[j] if j is not None and j < len(row) else None
                ico_raw, jm, mail = _b(hlav[0]), _b(hlav[1]), _b(hlav[2])
                if _txt(ico_raw) == '' and _txt(mail) == '':
                    continue
                out.append({'radek': i, 'ico': norm_ico(ico_raw), 'ico_raw': _txt(ico_raw),
                            'jmeno': _txt(jm), 'emaily_raw': _txt(mail)})
            if hlav is not None:
                return out, ''
        return [], ('V souboru jsem nenašel hlavičku se sloupci „IČO" a „Info Sankce" '
                    '(sloupec s e-maily). Nahrajte export „Dod. sankce".')
    finally:
        try:
            wb.close()
        except Exception:
            pass


def uloz_databazi(zaznamy: list, nazev: str, raw: bytes, kdo: str) -> tuple:
    """Nahradí celou databázi adres obsahem souboru (jedna transakce) a uloží
    originál ke stažení. Duplicitní IČ sloučí (adresy spojí). Vrací
    (počet_IČ, počet_řádků_bez_IČ, chyba|'')."""
    slouc = OrderedDict()
    bez_ico = 0
    for z in zaznamy:
        if not z['ico']:
            bez_ico += 1
            continue
        s = slouc.get(z['ico'])
        if s is None:
            slouc[z['ico']] = dict(z, duplicit=1)
            continue
        s['duplicit'] += 1
        if z['emaily_raw'] and z['emaily_raw'] not in s['emaily_raw']:
            s['emaily_raw'] = '; '.join(x for x in (s['emaily_raw'], z['emaily_raw']) if x)
        if not s['jmeno']:
            s['jmeno'] = z['jmeno']
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0, bez_ico, 'Není spojení s databází.'
    try:
        cur = conn.cursor()
        cur.execute(f'DELETE FROM {_T_EMAILY}')
        cur.executemany(
            f'INSERT INTO {_T_EMAILY} (ico,jmeno,emaily_raw,duplicit,radek) VALUES (%s,%s,%s,%s,%s)',
            [(s['ico'][:40], s['jmeno'][:255], s['emaily_raw'], s['duplicit'], s['radek'])
             for s in slouc.values()])
        cur.execute(f'INSERT INTO {_T_SOUBOR} (nazev,data,velikost,pocet,kdo) VALUES (%s,%s,%s,%s,%s)',
                    ((nazev or 'dodavatele.xlsx')[:255], raw, len(raw), len(slouc), (kdo or '')[:255]))
        cur.execute(f'SELECT id FROM {_T_SOUBOR} ORDER BY id DESC LIMIT %s, 1000', (_SOUBORU_DRZET,))
        stare = [r[0] for r in cur.fetchall()]
        if stare:
            cur.executemany(f'DELETE FROM {_T_SOUBOR} WHERE id=%s', [(i,) for i in stare])
        conn.commit()
        cur.close()
        return len(slouc), bez_ico, ''
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f'[sankce-rozesilka] uloz_databazi error: {e}')
        return 0, bez_ico, f'Uložení selhalo: {e}'
    finally:
        conn.close()


def nacti_databazi() -> dict:
    """{ico: {ico, jmeno, emaily_raw, duplicit, radek, rozbor}}"""
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT ico,jmeno,emaily_raw,duplicit,radek FROM {_T_EMAILY}')
        out = {}
        for r in cur.fetchall():
            r['rozbor'] = rozbor_emailu(r.get('emaily_raw'))
            out[r['ico']] = r
        return out
    finally:
        conn.close()


def nacti_meta():
    """Poslední nahraný soubor (bez dat) nebo None."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f'SELECT id,nazev,velikost,pocet,kdy,kdo FROM {_T_SOUBOR} ORDER BY id DESC LIMIT 1')
        return cur.fetchone()
    finally:
        conn.close()


def nacti_soubor(soubor_id: int):
    conn = intranet_data.get_db_connection()
    if not conn:
        return None, None
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT nazev,data FROM {_T_SOUBOR} WHERE id=%s', (soubor_id,))
        r = cur.fetchone()
        return (r[0], bytes(r[1])) if r else (None, None)
    finally:
        conn.close()


def problemy_databaze(db: dict) -> list:
    """Záznamy nahrané databáze, se kterými je něco špatně. „Bez e-mailu" se
    nehlásí u interních kódů (SPOLTTP…) ani u ERP variant IČ s příponou
    (12345678-1, …F) — těch jsou stovky a většinou se nefakturují; když na
    takovou variantu čeká sankce, ukáže ji přehled „Sankce bez platné adresy"."""
    out = []
    for ico, e in db.items():
        typ = typ_kodu(ico)
        rb = e['rozbor']
        prob = []
        if typ == 'ic' and not ic_soucet_ok(zaklad_ico(ico)):
            prob.append('Neplatné IČ (nesedí kontrolní součet)')
        if (e.get('duplicit') or 1) > 1:
            prob.append(f'IČ je v souboru {e["duplicit"]}×')
        if rb['neplatne']:
            prob.append('Neplatný e-mail')
        varianta = typ == 'ic' and zaklad_ico(ico) != ico
        if not rb['platne'] and not rb['neplatne'] and typ != 'interni' and not varianta:
            prob.append('Bez e-mailu')
        if prob:
            out.append({'ico': ico, 'jmeno': e.get('jmeno') or '', 'problem': '; '.join(prob),
                        'platne': ', '.join(rb['platne']), 'neplatne': ', '.join(rb['neplatne']),
                        'poznamka': rb['poznamka'], 'emaily_raw': e.get('emaily_raw') or '',
                        'radek': e.get('radek')})
    out.sort(key=lambda r: (r['problem'], r['ico']))
    return out


# =========================================================
# PLÁN ROZESÍLKY — co komu odejde
# =========================================================
def _castka(rows: list) -> float:
    return round(sum((S._f(r.get('hodn_sankce')) or 0.0) * (1 - (S._f(r.get('sleva')) or 0.0))
                     for r in rows), 2)


def _najdi(db: dict, varianty: dict, ico: str) -> tuple:
    """Záznamy databáze pro IČ sankce + způsob shody. Nejdřív přesně, pak přes
    základní IČ (sankce „12345678-1" → databáze „12345678" a naopak, případně
    všechny ERP varianty téhož IČ)."""
    z = zaklad_ico(ico)
    urovne = []                                  # (záznamy, popis shody) v pořadí priority
    if ico in db:
        urovne.append(([db[ico]], ''))
    if z and z != ico and z in db:
        urovne.append(([db[z]], f'adresa podle základního IČ {z}'))
    if z:
        jine = [e for e in varianty.get(z, []) if e['ico'] != ico]
        if jine:
            kody = ', '.join(e['ico'] for e in jine[:5]) + (' …' if len(jine) > 5 else '')
            urovne.append((jine, f'adresa podle variant IČ ({kody})'))
    # Přednost má první úroveň, která má platnou adresu — přesná shoda bez e-mailu
    # (ERP podúčet) nesmí zablokovat dodavatele, jehož jiný účet adresu má.
    for zaznamy, popis in urovne:
        if any(e['rozbor']['platne'] for e in zaznamy):
            return zaznamy, popis
    return (urovne[0] if urovne else ([], ''))


def nacti_cekajici() -> list:
    """Řádky sestavy, které ještě dodavateli neodešly (bez razítka) a nejsou ve
    stavu „Nevyfakturovat" — o těch je rozhodnuto, že se sankce neuplatní."""
    return [r for r in S._nacti('sankce_vystaveni')
            if not r.get('odeslano_dod') and r.get('stav') not in _NEPOSILAT_STAVY]


def obdobi_k_rozeslani(cekajici: list) -> list:
    """Všechna období sestavy, nejnovější první: [{'obdobi', 'pocet'}], kde
    `pocet` = řádky, které ještě čekají na rozeslání (0 = vše odesláno → ✓)."""
    pocty = {}
    for r in cekajici:
        if r.get('obdobi'):
            pocty[r['obdobi']] = pocty.get(r['obdobi'], 0) + 1
    poradi = list(S._seznam_obdobi('sankce_vystaveni'))      # nejnovější nahoře
    poradi += sorted(o for o in pocty if o not in poradi)     # pojistka
    return [{'obdobi': o, 'pocet': pocty.get(o, 0)} for o in poradi]


def priprav_plan(db: dict | None = None, obdobi: str | None = None,
                 cekajici: list | None = None) -> list:
    """Sankce zvoleného období, které ještě dodavateli neodešly (viz
    nacti_cekajici), seskupené po IČ. `obdobi=None` = všechna období.
    Každá položka: {ico, jmeno, nakupci, obdobi, rows, pocet, castka, emaily,
    neplatne, stav, info}."""
    if db is None:
        db = nacti_databazi()
    if cekajici is None:
        cekajici = nacti_cekajici()
    varianty = {}
    for kod, e in db.items():
        z = zaklad_ico(kod)
        if z and z != kod:
            varianty.setdefault(z, []).append(e)
    if obdobi is not None:
        cekajici = [r for r in cekajici if r.get('obdobi') == obdobi]
    skup = OrderedDict()
    for r in sorted(cekajici, key=lambda r: ((r.get('jmeno_dodavatele') or '').lower(),
                                             r.get('id') or 0)):
        skup.setdefault(norm_ico(r.get('ico')), []).append(r)

    plan = []
    for ico, rows in skup.items():
        jmeno = next((S._s(r.get('jmeno_dodavatele')) for r in rows
                      if r.get('jmeno_dodavatele')), '')
        p = {'ico': ico, 'jmeno': jmeno, 'rows': rows, 'pocet': len(rows),
             'nakupci': ', '.join(sorted({S._kod_nakupci(r) for r in rows} - {''})),
             'obdobi': ', '.join(sorted({S._s(r.get('obdobi')) for r in rows} - {''})),
             'castka': _castka(rows), 'emaily': [], 'neplatne': [], 'stav': '', 'info': ''}
        if not ico:
            p['stav'] = 'chybi_ico'
        else:
            zaznamy, zpusob = _najdi(db, varianty, ico)
            for e in zaznamy:
                for a in e['rozbor']['platne']:
                    if a.lower() not in {x.lower() for x in p['emaily']}:
                        p['emaily'].append(a)
                p['neplatne'] += [a for a in e['rozbor']['neplatne'] if a not in p['neplatne']]
            poznamky = [e['rozbor']['poznamka'] for e in zaznamy if e['rozbor']['poznamka']]
            info = [zpusob] if zpusob else []
            if not zaznamy:
                p['stav'] = 'neni_v_db'
            elif p['emaily']:
                p['stav'] = 'ok'
                if p['neplatne']:
                    info.append('neplatné adresy se přeskočí: ' + ', '.join(p['neplatne']))
            elif p['neplatne']:
                p['stav'] = 'neplatny_email'
                info.append('neplatné: ' + ', '.join(p['neplatne']))
            else:
                p['stav'] = 'bez_emailu'
            info += poznamky
            p['info'] = '; '.join(info)
        plan.append(p)
    return plan


# =========================================================
# ROZESLÁNÍ DODAVATELŮM
# =========================================================
def _predmet_dod(p: dict) -> str:
    return (f'Předběžné oznámení o uplatnění smluvní pokuty – {p["jmeno"] or p["ico"]}'
            + (f' – {p["obdobi"]}' if p.get('obdobi') else ''))


def _text_dod(p: dict) -> str:
    return (
        'Dobrý den,\n\n'
        'v příloze Vám zasíláme předběžné oznámení o uplatnění smluvní pokuty '
        'včetně podkladu – přehledu dotčených objednávek.\n\n'
        f'Dodavatel: {p["jmeno"]}\n'
        f'IČ: {p["ico"]}\n'
        + (f'Období: {p["obdobi"]}\n' if p.get('obdobi') else '')
        + f'Počet položek: {p["pocet"]}\n'
        f'Celková výše smluvní pokuty: {S._cz_money(p["castka"])} Kč\n\n'
        'Případné námitky prosím uplatněte postupem uvedeným v oznámení.\n\n'
        'S pozdravem\n'
        'JIP východočeská, a.s.'
    )


def _oznac_odeslano(p: dict, user_id, user_name):
    """Razítko odeslání na řádky + audit (historie řádku 👁️)."""
    ted = datetime.datetime.now().replace(microsecond=0)
    adresy = ', '.join(p['emaily'])[:500]
    conn = intranet_data.get_db_connection()
    if not conn:
        print(f'[sankce-rozesilka] razítko odeslání {p["ico"]}: není spojení s DB!')
        return
    try:
        cur = conn.cursor()
        cur.executemany('UPDATE sankce_vystaveni SET odeslano_dod=%s, odeslano_dod_na=%s '
                        'WHERE id=%s AND odeslano_dod IS NULL',
                        [(ted, adresy, r.get('id')) for r in p['rows']])
        conn.commit()
        cur.close()
    finally:
        conn.close()
    S._zapis_audit_bulk([
        ('sankce_vystaveni', r.get('row_hash'), r.get('id'), 'odeslano_dod', None,
         f'{ted:%d.%m.%Y %H:%M} → {adresy}', user_id, user_name)
        for r in p['rows']])


async def rozesli_dodavatelum(ica: set, user_id, user_name, prubeh=None,
                              obdobi: str | None = None) -> dict:
    """Odešle Kompletní sestavu dodavatelům ze seznamu `ica` (IČ potvrzená
    v náhledu) za období `obdobi`. Plán se přepočítá znovu, takže se pošle jen
    to, co ještě neodešlo a mezitím nebylo přepnuto na Nevyfakturovat.
    Vrací {odeslano, chyby, chyba}."""
    out = {'odeslano': [], 'chyby': [], 'chyba': ''}

    def _hlas(txt):
        if prubeh:
            try:
                prubeh(txt)
            except Exception:
                pass

    smtp = intranet_emaily.nacti_smtp_uctu(SMTP_PREFIX)
    if not smtp['enabled']:
        # Technický důvod jen do logu serveru — hláška jde do UI běžnému uživateli.
        print(f'[sankce-rozesilka] SMTP {SMTP_PREFIX} nenastaveno: {smtp["duvod"]}')
        out['chyba'] = ('Rozesílka sankcí zatím není zprovozněná — obraťte se '
                        'na administrátora intranetu.')
        return out
    zamek = await asyncio.to_thread(_zamek_ziskej, _ZAMEK_DOD)
    if zamek is None:
        out['chyba'] = ('Rozesílka právě běží (spustil ji někdo jiný), nebo není spojení '
                        's databází. Zkuste to za chvíli.')
        return out
    try:
        await asyncio.to_thread(_init_db)
        plan = await asyncio.to_thread(priprav_plan, None, obdobi)
        davka = [p for p in plan if p['stav'] == 'ok' and p['ico'] in ica]
        if not davka:
            out['chyba'] = 'Mezitím už není co odeslat (stavy se změnily nebo už bylo rozesláno).'
            return out

        html_list, kam = [], []
        for i, p in enumerate(davka):
            for fn, html, _rows in S._dokumenty_k_tisku(p['rows'], False):
                html_list.append((fn, html))
                kam.append(i)
        _hlas(f'Generuji PDF… (0 / {len(html_list)})')
        pdfs = await S._render_pdf_bytes_batch(
            html_list,
            status_cb=lambda i, n, _fn: _hlas(f'Generuji PDF… ({i} / {n})'),
            on_wait=lambda: _hlas('Čekám ve frontě — tiskne jiný uživatel…'))
        prilohy = [[] for _ in davka]
        for (fn, pdf), i in zip(pdfs, kam):
            prilohy[i].append((fn, pdf))

        for i, p in enumerate(davka):
            _hlas(f'Odesílám e-maily… ({i + 1} / {len(davka)}) {p["jmeno"]}')
            ok, zprava = await asyncio.to_thread(
                intranet_emaily.odesli_email_z_uctu, smtp, p['emaily'],
                _predmet_dod(p), _text_dod(p), prilohy[i], ODESILATEL_JMENO)
            zaznam = {'ico': p['ico'], 'jmeno': p['jmeno'], 'emaily': ', '.join(p['emaily']),
                      'pocet': p['pocet'], 'castka': p['castka'], 'zprava': zprava}
            if ok:
                await asyncio.to_thread(_oznac_odeslano, p, user_id, user_name)
                out['odeslano'].append(zaznam)
            else:
                out['chyby'].append(zaznam)
            await asyncio.to_thread(_zapis_log, 'dodavatel', p['ico'], p['jmeno'], p['emaily'],
                                    p['pocet'], p['castka'], ok, zprava, user_name)
    except RuntimeError as e:            # Playwright / Chromium chybí
        out['chyba'] = str(e)
    except Exception as e:
        traceback.print_exc()
        out['chyba'] = f'Neočekávaná chyba: {e}'
    finally:
        await asyncio.to_thread(_zamek_uvolni, zamek, _ZAMEK_DOD)
    intranet_logger.log_activity(
        user_name, 'Sankce',
        f'Rozesílka dodavatelům{f" ({obdobi})" if obdobi else ""}: '
        f'odesláno {len(out["odeslano"])}, chyb {len(out["chyby"])}'
        + (f' — {out["chyba"]}' if out['chyba'] else ''))
    return out


# =========================================================
# ÚČTÁRNA — „Pouze podklad" všeho ve stavu Fakturovat
# =========================================================
def _prepni_na_uctarnu(rows: list, spustil: str, user_id) -> int:
    """Fakturovat → Odevzdáno účtárně jen u řádků, které byly v odeslaném
    podkladu A jsou stále ve stavu Fakturovat (mezitím je mohl někdo změnit)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    zmenene = []
    try:
        cur = conn.cursor()
        for r in rows:
            cur.execute('UPDATE sankce_vystaveni SET stav=%s WHERE id=%s AND stav=%s',
                        (STAV_UCTARNA, r.get('id'), STAV_FAKTUROVAT))
            if cur.rowcount:
                zmenene.append(r)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    S._zapis_audit_bulk([
        ('sankce_vystaveni', r.get('row_hash'), r.get('id'), 'stav',
         S.STAV_LABEL[STAV_FAKTUROVAT], S.STAV_LABEL[STAV_UCTARNA], user_id, spustil)
        for r in zmenene])
    return len(zmenene)


async def _odesli_cast_uctarne(smtp, email: str, rows: list, spustil: str) -> tuple:
    """Jeden e-mail „Pouze podklad" pro jednoho příjemce účtárny. → (ok, zpráva)."""
    n_dod = len({norm_ico(r.get('ico')) for r in rows})
    pobocky = sorted({S._pobocka_kod(r.get('id_pobocky')) or '?' for r in rows})
    dokumenty = S._dokumenty_k_tisku(rows, True)
    pdfs = await S._render_pdf_bytes_batch([(fn, html) for fn, html, _r in dokumenty])
    dnes = datetime.date.today()
    if len(pdfs) == 1:
        prilohy = list(pdfs)
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fn, pdf in pdfs:
                zf.writestr(fn, pdf)
        prilohy = [(f'Podklad_sankce_{dnes:%Y-%m-%d}.zip', buf.getvalue())]

    predmet = f'Sankce – podklad k fakturaci {dnes:%d.%m.%Y} ({n_dod} dodavatelů)'
    text = (
        'Dobrý den,\n\n'
        'v příloze posíláme podklady k fakturaci sankcí (tisk „Pouze podklad") — '
        'řádky ve stavu Fakturovat za pobočky přiřazené této adrese.\n\n'
        f'Pobočky: {", ".join(pobocky)}\n'
        f'Dodavatelů: {n_dod}\n'
        f'Dokumentů: {len(pdfs)}\n'
        f'Řádků: {len(rows)}\n'
        f'Celkem po slevách: {S._cz_money(_castka(rows))} Kč\n\n'
        'Po odeslání této zprávy se řádky v intranetu převedou do stavu '
        '„Odevzdáno účtárně".\n\n'
        f'Automatická zpráva z intranetu JIP (spustil: {spustil}).'
    )
    return await asyncio.to_thread(
        intranet_emaily.odesli_email_z_uctu, smtp, [email], predmet, text,
        prilohy, ODESILATEL_JMENO)


async def odesli_uctarne(spustil: str, user_id=None) -> dict:
    """Pošle účtárně „Pouze podklad" všech řádků ve stavu Fakturovat — rozdělené
    podle poboček na příjemce z nastavení (ozubené kolečko). Po
    úspěšném odeslání přepne řádky daného příjemce na Odevzdáno účtárně; co
    neodešlo, zůstává ve Fakturovat a dožene ho další pokus automatu.
    Volá páteční automat i ruční tlačítko hlavního admina."""
    out = {'ok': False, 'prazdne': False, 'pocet': 0, 'prepnuto': 0, 'zprava': ''}
    zamek = await asyncio.to_thread(_zamek_ziskej, _ZAMEK_UCT)
    if zamek is None:
        out['zprava'] = 'Odeslání účtárně právě běží, nebo není spojení s databází.'
        return out
    rows, prijemci, logy = [], [UCTARNA_EMAIL], []
    try:
        await asyncio.to_thread(_init_db)
        cfg = await asyncio.to_thread(nacti_prijemce)
        prijemci = [e for e in cfg['emaily'] if e]
        rows = [r for r in await asyncio.to_thread(S._nacti, 'sankce_vystaveni')
                if r.get('stav') == STAV_FAKTUROVAT]
        out['pocet'] = len(rows)
        if not rows:
            out.update(ok=True, prazdne=True,
                       zprava='Ve stavu Fakturovat nic není — účtárně se nic neposílalo.')
            return out

        smtp = intranet_emaily.nacti_smtp_uctu(SMTP_PREFIX)
        if not smtp['enabled']:
            smtp = intranet_data.nacti_smtp()   # záloha: hlavní účet (účtárna je interní)
        if not smtp.get('enabled'):
            # Zpráva se ukládá do historie a ukazuje všem — názvy env proměnných jen do logu.
            print(f'[sankce-rozesilka] účtárna: chybí {SMTP_PREFIX}_USER/_PASS i JIPKA_SMTP_*')
            out['zprava'] = ('Odesílání e-mailů není na serveru nastavené — '
                             'obraťte se na administrátora intranetu.')
            return out

        zalohovano = False
        for email, cast in _rozdel_podle_prijemcu(rows, cfg):
            n_dod = len({norm_ico(r.get('ico')) for r in cast})
            castka = _castka(cast)
            try:
                ok, zprava = await _odesli_cast_uctarne(smtp, email, cast, spustil)
            except RuntimeError as e:            # Playwright / Chromium chybí
                ok, zprava = False, str(e)
            except Exception as e:
                traceback.print_exc()
                ok, zprava = False, f'Neočekávaná chyba: {e}'
            if ok:
                # Pojistka před hromadnou změnou stavů — jde vrátit přes Obnovu ze zálohy.
                if not zalohovano:
                    try:
                        await asyncio.to_thread(S._vytvor_zalohu, 'sankce_vystaveni', 'auto',
                                                f'{spustil} (před odevzdáním účtárně)', True)
                    except Exception as e:
                        print(f'[sankce-rozesilka] záloha před odevzdáním: {e}')
                    zalohovano = True
                n = await asyncio.to_thread(_prepni_na_uctarnu, cast, spustil, user_id)
                out['prepnuto'] += n
                zprava = (f'Odesláno na {email}: {len(cast)} řádků, {n_dod} dodavatelů, '
                          f'{S._cz_money(castka)} Kč; na Odevzdáno účtárně přepnuto {n} řádků.'
                          + (f' ({zprava})' if zprava else ''))
            else:
                zprava = f'Na {email} NEODESLÁNO ({len(cast)} řádků): {zprava}'
            logy.append((email, len(cast), castka, ok, zprava))
        out['ok'] = all(l[3] for l in logy)
        out['zprava'] = ' '.join(l[4] for l in logy)
    except Exception as e:
        traceback.print_exc()
        out['zprava'] = f'Neočekávaná chyba: {e}'
    finally:
        await asyncio.to_thread(_zamek_uvolni, zamek, _ZAMEK_UCT)
        for email, pocet, castka, ok, zprava in logy or [
                (', '.join(prijemci), out['pocet'], _castka(rows), out['ok'], out['zprava'])]:
            await asyncio.to_thread(_zapis_log, 'uctarna', '', 'Účtárna', [email],
                                    pocet, castka, ok, zprava, spustil)
    intranet_logger.log_activity(spustil, 'Sankce', f'Podklad účtárně: {out["zprava"]}')
    return out


# =========================================================
# PÁTEČNÍ AUTOMAT
# =========================================================
def _slot_pro(ted: datetime.datetime) -> datetime.datetime:
    """Poslední pátek 3:00, který už nastal."""
    d = ted.date() - datetime.timedelta(days=(ted.weekday() - _UCTARNA_DEN) % 7)
    slot = datetime.datetime.combine(d, datetime.time(_UCTARNA_HODINA))
    if slot > ted:
        slot -= datetime.timedelta(days=7)
    return slot


def _beh_sql(sql: str, params=(), fetch: bool = False):
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        res = cur.fetchall() if fetch else cur.rowcount
        conn.commit()
        cur.close()
        return res
    finally:
        conn.close()


def _beh_nacti(slot):
    r = _beh_sql(f'SELECT * FROM {_T_BEHY} WHERE slot=%s', (slot,), fetch=True)
    return r[0] if r else None


def _behy_prazdne() -> bool:
    r = _beh_sql(f'SELECT COUNT(*) AS n FROM {_T_BEHY}', fetch=True)
    return bool(r) and r[0]['n'] == 0


def _beh_zaloz(slot, stav: str, info: str = '', pokusy: int = 0) -> bool:
    """INSERT IGNORE — True jen pro toho, kdo slot založil (pojistka proti 2 instancím)."""
    n = _beh_sql(f'INSERT IGNORE INTO {_T_BEHY} (slot,stav,pokusy,info,zmeneno) '
                 'VALUES (%s,%s,%s,%s,%s)',
                 (slot, stav, pokusy, info, datetime.datetime.now().replace(microsecond=0)))
    return bool(n)


def _beh_zaber_znovu(slot, ted) -> bool:
    """Nový pokus po chybě (po 30 min) nebo po zaseknutém běhu (server spadl)."""
    n = _beh_sql(
        f"UPDATE {_T_BEHY} SET stav='bezi', pokusy=pokusy+1, zmeneno=%s "
        "WHERE slot=%s AND pokusy<%s AND ((stav='chyba' AND zmeneno<=%s) "
        "OR (stav='bezi' AND zmeneno<=%s))",
        (ted.replace(microsecond=0), slot, _UCTARNA_POKUSU,
         ted - datetime.timedelta(minutes=_UCTARNA_PAUZA_MIN),
         ted - datetime.timedelta(hours=_UCTARNA_ZASEKNUTO_H)))
    return bool(n)


def _beh_uzavri(slot, stav: str, info: str):
    _beh_sql(f'UPDATE {_T_BEHY} SET stav=%s, info=%s, zmeneno=%s WHERE slot=%s',
             (stav, info, datetime.datetime.now().replace(microsecond=0), slot))


def posledni_beh():
    r = _beh_sql(f'SELECT * FROM {_T_BEHY} ORDER BY slot DESC LIMIT 1', fetch=True)
    return r[0] if r else None


def automat_zapnuty() -> bool:
    """Páteční automat účtárny. Výchozí VYPNUTO — dokud ho admin nezapne obálkou."""
    n = intranet_data.nacti_nastaveni_intranetu().get(_AUTOMAT_KLIC)
    return isinstance(n, dict) and n.get('zapnuto') is True


def uloz_automat(zapnuto: bool):
    n = intranet_data.nacti_nastaveni_intranetu()
    stare = n.get(_AUTOMAT_KLIC)
    n[_AUTOMAT_KLIC] = dict(stare if isinstance(stare, dict) else {}, zapnuto=bool(zapnuto))
    intranet_data.uloz_nastaveni_intranetu(n)


def nacti_prijemce() -> dict:
    """{'emaily': [e1, e2], 'pobocky': {kód: index}}. Bez nastavení jde vše
    na UCTARNA_EMAIL (příjemce 1)."""
    n = intranet_data.nacti_nastaveni_intranetu().get(_PRIJEMCI_KLIC)
    n = n if isinstance(n, dict) else {}
    emaily = [str(e or '').strip() for e in (n.get('emaily') or [])][:_PRIJEMCU]
    emaily += [''] * (_PRIJEMCU - len(emaily))
    emaily[0] = emaily[0] or UCTARNA_EMAIL
    pobocky = {str(k): v for k, v in (n.get('pobocky') or {}).items()
               if isinstance(v, int) and 0 <= v < _PRIJEMCU}
    return {'emaily': emaily, 'pobocky': pobocky}


def uloz_prijemce(emaily: list, pobocky: dict):
    n = intranet_data.nacti_nastaveni_intranetu()
    n[_PRIJEMCI_KLIC] = {'emaily': list(emaily), 'pobocky': dict(pobocky)}
    intranet_data.uloz_nastaveni_intranetu(n)


def _rozdel_podle_prijemcu(rows: list, cfg: dict) -> list:
    """Řádky → [(e-mail, řádky)] v pořadí příjemců. Příjemce bez adresy
    a neznámá pobočka padají na příjemce 1; stejná adresa 2× = jeden e-mail."""
    emaily = cfg['emaily']
    skup = OrderedDict()
    for e in emaily:
        if e:
            skup.setdefault(e.lower(), (e, []))
    for r in rows:
        i = cfg['pobocky'].get(S._pobocka_kod(r.get('id_pobocky')), 0)
        e = emaily[i] or emaily[0]
        skup[e.lower()][1].append(r)
    return [v for v in skup.values() if v[1]]


def _popis_prijemcu(cfg: dict) -> str:
    """Do textů dialogů: „a@x (010, 011) a b@x (012)" / jen „a@x"."""
    skup = _rozdel_podle_prijemcu(
        [{'id_pobocky': k} for k in S._POBOCKY_NAZVY], cfg)
    if len(skup) <= 1:
        return skup[0][0] if skup else cfg['emaily'][0]
    return ' a '.join(f'{e} ({", ".join(r["id_pobocky"] for r in rr)})' for e, rr in skup)


async def bg_uctarna_patek():
    """Serverová smyčka (web_main → app.on_startup): každý pátek ve 3:00 pošle
    účtárně „Pouze podklad" a přepne Fakturovat → Odevzdáno účtárně.

    Stav drží tabulka slotů, takže restart nic nezopakuje a dvě instance
    serveru neodešlou dvakrát. Úplně první start jen zapíše „nasazeno" — první
    odeslání proběhne nejbližší pátek, ne hned po nasazení. Neběžel-li server
    ve 3:00, dožene se to do 15:00; později už ne (stavy by se měnily uprostřed
    týdne) a vše počká na další pátek.

    Vypnutý automat (výchozí) slot jen zapíše jako „vypnuto" — po pozdějším
    zapnutí se tak tento pátek nedohání a první odeslání je až další pátek 3:00.
    Vypnutí zastaví i opakované pokusy po chybě."""
    await asyncio.sleep(120)
    while True:
        try:
            if await asyncio.to_thread(_init_db):
                ted = datetime.datetime.now()
                slot = _slot_pro(ted)
                beh = await asyncio.to_thread(_beh_nacti, slot)
                zapnuto = await asyncio.to_thread(automat_zapnuty)
                spustit = False
                if not zapnuto:
                    if beh is None:
                        await asyncio.to_thread(
                            _beh_zaloz, slot, 'vypnuto',
                            'Automat je vypnutý — nic se neodeslalo, stavy beze změny.')
                elif beh is None:
                    if await asyncio.to_thread(_behy_prazdne):
                        await asyncio.to_thread(
                            _beh_zaloz, slot, 'nasazeno',
                            'Automat nasazen — první odeslání proběhne nejbližší pátek ve 3:00.')
                    elif ted - slot > datetime.timedelta(hours=_UCTARNA_DOHNAT_H):
                        await asyncio.to_thread(
                            _beh_zaloz, slot, 'zmeskano',
                            'Server v pátek ve 3:00–15:00 neběžel — řádky počkají na další pátek.')
                    else:
                        spustit = await asyncio.to_thread(_beh_zaloz, slot, 'bezi', '', 1)
                else:
                    spustit = await asyncio.to_thread(_beh_zaber_znovu, slot, ted)
                if spustit:
                    print(f'[sankce-rozesilka] páteční odeslání účtárně (slot {slot:%d.%m.%Y %H:%M})')
                    vysl = await odesli_uctarne('Automat (pátek 3:00)')
                    stav = 'prazdne' if vysl['prazdne'] else ('ok' if vysl['ok'] else 'chyba')
                    await asyncio.to_thread(_beh_uzavri, slot, stav, vysl['zprava'])
                    print(f'[sankce-rozesilka] účtárna: {stav} — {vysl["zprava"]}')
        except Exception as e:
            print(f'[sankce-rozesilka] bg_uctarna_patek error: {e}')
        await asyncio.sleep(60)


# =========================================================
# XLSX — přehled pro stažení
# =========================================================
def _xlsx_kontrola(plan: list, problemy: list) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    listy = [
        ('Rozesílka', ['IČ', 'Dodavatel', 'Stav', 'Nákupčí', 'Období', 'Řádků', 'Částka (Kč)',
                       'E-maily', 'Poznámka'],
         [[p['ico'], p['jmeno'], PLAN_STAV_LABEL.get(p['stav'], p['stav']), p['nakupci'],
           p['obdobi'], p['pocet'], p['castka'], ', '.join(p['emaily']), p['info']]
          for p in plan]),
        ('Chyby v databázi', ['IČ', 'Jméno', 'Problém', 'Platné e-maily', 'Neplatné',
                              'Info Sankce (originál)', 'Řádek v souboru'],
         [[r['ico'], r['jmeno'], r['problem'], r['platne'], r['neplatne'], r['emaily_raw'],
           r['radek']] for r in problemy]),
    ]
    for i, (nazev, hlav, data) in enumerate(listy):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = nazev
        ws.append(hlav)
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor='4F46E5')
        for radek in data:
            ws.append(radek)
            ws.cell(ws.max_row, 1).number_format = '@'     # IČ = text (vedoucí nuly)
        if nazev == 'Rozesílka':
            for c in ws['G'][1:]:
                c.number_format = '#,##0.00'
            sirky = (14, 38, 24, 12, 22, 9, 16, 50, 40)
        else:
            sirky = (14, 38, 26, 22, 22, 50, 40)
        for col, sirka in zip('ABCDEFGHI', sirky):
            ws.column_dimensions[col].width = sirka
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =========================================================
# UI
# =========================================================
def _fmt_dt(v) -> str:
    if isinstance(v, datetime.datetime):
        return v.strftime('%d.%m.%Y %H:%M')
    return S._s(v)


def _tabulka(sloupce: list, radky: list, row_key: str, prazdno: str, strankovat: int = 20):
    """ui.table s vyhledáváním. sloupce = [(field, label, align)]."""
    if not radky:
        ui.label(prazdno).classes('text-sm text-gray-500 italic py-3')
        return
    cols = [{'name': f, 'label': lab, 'field': f, 'sortable': True, 'align': al}
            for f, lab, al in sloupce]
    hledat = ui.input(placeholder='Hledat…').props('dense outlined clearable') \
        .classes('w-72 mb-1')
    t = ui.table(columns=cols, rows=radky, row_key=row_key,
                 pagination={'rowsPerPage': strankovat}) \
        .props('dense flat bordered wrap-cells').classes('w-full text-xs')
    hledat.bind_value(t, 'filter')


def _plan_radky(plan: list) -> list:
    return [{'ico': p['ico'] or '—', 'jmeno': p['jmeno'], 'pocet': p['pocet'],
             'nakupci': p['nakupci'], 'obdobi': p['obdobi'],
             'castka': S._cz_money(p['castka']), 'emaily': ', '.join(p['emaily']),
             'stav': PLAN_STAV_LABEL.get(p['stav'], p['stav']), 'info': p['info']}
            for p in plan]


def _banner(text: str, barva: str = 'amber', ikona: str = 'warning'):
    with ui.row().classes(f'w-full items-start gap-2 p-3 rounded-lg bg-{barva}-50 '
                          f'border border-{barva}-200 no-wrap'):
        ui.icon(ikona).classes(f'text-{barva}-600 text-xl')
        ui.label(text).classes(f'text-sm text-{barva}-900')


async def _nacti_upload(e) -> tuple:
    """(bytes, název) z ui.upload eventu — funguje se starým i novým API NiceGUI."""
    zdroj = None
    for attr in ('content', 'file', 'stream', 'data', 'file_obj'):
        val = getattr(e, attr, None)
        if val is not None and hasattr(val, 'read'):
            zdroj = val
            break
    if zdroj is None:
        return None, ''
    raw = zdroj.read()
    if inspect.isawaitable(raw):
        raw = await raw
    nazev = getattr(e, 'name', '') or getattr(getattr(e, 'file', None), 'name', '') or ''
    return raw, nazev


def tlacitko_rozesilka(user_id, user_name, ma_vse: bool, refresh_fn):
    """Tlačítko „Rozesílka" do lišty Sankce k vystavení (účtárna / analytik).
    Dialog žije vedle tlačítka, ne v menu — obsah zavřeného q-menu se nevykresluje."""
    dlg = ui.dialog()
    stav = {'zmeneno': False}

    def _zavri():
        dlg.close()
        dlg.props(remove='persistent')
        if stav['zmeneno']:
            stav['zmeneno'] = False
            try:
                refresh_fn()
            except Exception:
                pass

    # ---------- Rozeslat dodavatelům ----------
    async def _otevri_rozeslat(volba: str | None = None):
        smtp = intranet_emaily.nacti_smtp_uctu(SMTP_PREFIX)
        await asyncio.to_thread(_init_db)
        meta = await asyncio.to_thread(nacti_meta)
        cekajici = await asyncio.to_thread(nacti_cekajici)
        obd_list = await asyncio.to_thread(obdobi_k_rozeslani, cekajici)
        if volba not in {o['obdobi'] for o in obd_list}:
            # Výchozí = nejnovější období, kde ještě něco čeká; jinak nejnovější vůbec.
            volba = next((o['obdobi'] for o in obd_list if o['pocet']),
                         obd_list[0]['obdobi'] if obd_list else None)
        plan = (await asyncio.to_thread(priprav_plan, None, volba, cekajici)) if volba else []
        pripraveno = [p for p in plan if p['stav'] == 'ok']
        nelze = [p for p in plan if p['stav'] != 'ok']
        dlg.clear()
        with dlg, ui.card().classes('w-[1100px] max-w-[96vw] gap-2'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Rozeslat sankce dodavatelům').classes('text-lg font-bold text-gray-800')
                ui.button(icon='close', on_click=_zavri).props('flat round dense')
            ui.label(f'Kompletní sestava (předběžné oznámení + podklad v PDF) e-mailem ze '
                     f'schránky {SANKCE_SCHRANKA} — za každé IČ jeden e-mail. Posílají se '
                     f'sankce zvoleného období, které dodavateli ještě neodešly (kromě stavu '
                     f'Nevyfakturovat). Odeslané řádky dostanou razítko (sloupec „Odesláno '
                     f'dod.") a podruhé neodejdou.') \
                .classes('text-sm text-gray-600')
            if obd_list:
                with ui.row().classes('items-center gap-2'):
                    ui.label('Období:').classes('text-sm font-semibold text-gray-700')
                    # ✓ = vše odesláno (nic nečeká), ✗ = něco ještě neodešlo.
                    ui.select({o['obdobi']: f'{o["obdobi"]}  {"✗" if o["pocet"] else "✓"}'
                               for o in obd_list}, value=volba,
                              on_change=lambda e: _otevri_rozeslat(e.value)) \
                        .props('outlined dense options-dense').classes('min-w-[320px]') \
                        .tooltip('✓ = odesláno, ✗ = neodesláno')
                    dalsi = sum(1 for o in obd_list if o['pocet'] and o['obdobi'] != volba)
                    if dalsi:
                        ui.label(f'Neodeslané sankce jsou i v dalších obdobích '
                                 f'({dalsi}).').classes('text-xs text-amber-700')
                if volba and not plan:
                    _banner(f'Období {volba} je kompletně odeslané — nic dalšího nečeká.',
                            'emerald', 'check_circle')
            if not smtp['enabled']:
                # Technický důvod (názvy env proměnných) vidí jen administrátor.
                if ma_vse:
                    _banner(f'Odesílání ze schránky {SANKCE_SCHRANKA} není na serveru nastavené '
                            f'({smtp["duvod"]}). Rozeslat zatím nejde.', 'red', 'error')
                else:
                    _banner('Rozesílka sankcí zatím není zprovozněná — obraťte se '
                            'na administrátora intranetu.', 'red', 'error')
            if meta is None:
                _banner('Databáze e-mailů dodavatelů ještě nebyla nahraná — '
                        'Rozesílka → E-maily dodavatelů.')
            else:
                ui.label(f'Databáze adres: {meta["nazev"]} · nahráno {_fmt_dt(meta["kdy"])} '
                         f'({meta["kdo"]}) · {meta["pocet"]} IČ').classes('text-xs text-gray-500')

            with ui.row().classes('gap-3'):
                with ui.card().classes('bg-emerald-50 border border-emerald-200 p-3 shadow-none'):
                    ui.label(f'Připraveno: {len(pripraveno)} dodavatelů').classes('font-bold text-emerald-800')
                    ui.label(f'{sum(p["pocet"] for p in pripraveno)} řádků · '
                             f'{S._cz_money(sum(p["castka"] for p in pripraveno))} Kč') \
                        .classes('text-xs text-emerald-700')
                with ui.card().classes('bg-rose-50 border border-rose-200 p-3 shadow-none'):
                    ui.label(f'Nelze odeslat: {len(nelze)} dodavatelů').classes('font-bold text-rose-800')
                    ui.label(f'{sum(p["pocet"] for p in nelze)} řádků — chybí / neplatná adresa') \
                        .classes('text-xs text-rose-700')

            sl = [('ico', 'IČ', 'left'), ('jmeno', 'Dodavatel', 'left'),
                  ('nakupci', 'Nákupčí', 'left'), ('pocet', 'Řádků', 'right'),
                  ('castka', 'Částka (Kč)', 'right'), ('emaily', 'E-maily', 'left'),
                  ('info', 'Poznámka', 'left')]
            with ui.tabs().classes('w-full') as tabs:
                t_ok = ui.tab(f'K odeslání ({len(pripraveno)})')
                t_ne = ui.tab(f'Nelze odeslat ({len(nelze)})')
            with ui.tab_panels(tabs, value=t_ok if pripraveno or not nelze else t_ne) \
                    .classes('w-full'):
                with ui.tab_panel(t_ok).classes('p-0'):
                    _tabulka(sl, _plan_radky(pripraveno), 'ico',
                             'Není nic, co by dodavatelům ještě neodešlo.')
                with ui.tab_panel(t_ne).classes('p-0'):
                    _tabulka([('ico', 'IČ', 'left'), ('jmeno', 'Dodavatel', 'left'),
                              ('nakupci', 'Nákupčí', 'left'),
                              ('stav', 'Důvod', 'left'), ('pocet', 'Řádků', 'right'),
                              ('castka', 'Částka (Kč)', 'right'), ('info', 'Poznámka', 'left')],
                             _plan_radky(nelze), 'ico', 'Všechny sankce mají platnou adresu.')

            prubeh = ui.row().classes('w-full items-center gap-2')
            prubeh.set_visibility(False)
            with prubeh:
                ui.spinner(size='sm')
                prubeh_lbl = ui.label('').classes('text-sm text-indigo-700 font-medium')
            vysledek = ui.column().classes('w-full gap-1')
            akce = ui.row().classes('w-full justify-end gap-2')

            def _stahni_prehled():
                S._stahni_pres_http(_xlsx_kontrola(plan, []),
                                    S._safe_filename(f'Rozesilka_sankci_{volba or "nahled"}')
                                    + f'_{datetime.date.today():%Y-%m-%d}.xlsx')

            async def _odeslat():
                akce.clear()
                dlg.props('persistent')
                prubeh.set_visibility(True)
                ica = {p['ico'] for p in pripraveno}

                def _hlas(txt):
                    prubeh_lbl.set_text(txt)
                vysl = await rozesli_dodavatelum(ica, user_id, user_name, prubeh=_hlas,
                                                 obdobi=volba)
                prubeh.set_visibility(False)
                stav['zmeneno'] = bool(vysl['odeslano'])
                with vysledek:
                    if vysl['chyba']:
                        _banner(vysl['chyba'], 'red', 'error')
                    if vysl['odeslano']:
                        _banner(f'Odesláno {len(vysl["odeslano"])} dodavatelům '
                                f'({sum(r["pocet"] for r in vysl["odeslano"])} řádků).',
                                'emerald', 'check_circle')
                        varovani = [r for r in vysl['odeslano'] if r['zprava']]
                        for r in varovani:
                            ui.label(f'⚠ {r["jmeno"]} ({r["ico"]}): {r["zprava"]}') \
                                .classes('text-xs text-amber-800')
                    if vysl['chyby']:
                        _banner(f'Nepodařilo se odeslat {len(vysl["chyby"])} dodavatelům — '
                                f'zůstávají neodeslané, jde to zkusit znovu.', 'red', 'error')
                        for r in vysl['chyby']:
                            ui.label(f'✗ {r["jmeno"]} ({r["ico"]}, {r["emaily"]}): {r["zprava"]}') \
                                .classes('text-xs text-rose-800')
                with akce:
                    ui.button('Zavřít', on_click=_zavri).props('unelevated no-caps color=primary')

            def _potvrdit():
                akce.clear()
                with akce:
                    ui.label(f'Opravdu odeslat {len(pripraveno)} e-mailů dodavatelům '
                             f'(období {volba})? '
                             f'Odeslání nejde vzít zpět.').classes('text-sm font-semibold text-rose-700 self-center')
                    ui.button('Zpět', on_click=_tlacitka).props('flat no-caps')
                    ui.button('Ano, odeslat', icon='send', on_click=_odeslat) \
                        .props('unelevated no-caps color=negative')

            def _tlacitka():
                akce.clear()
                with akce:
                    ui.button('Stáhnout náhled (.xlsx)', icon='download', on_click=_stahni_prehled) \
                        .props('outline no-caps color=secondary')
                    ui.button('Zavřít', on_click=_zavri).props('flat no-caps')
                    b = ui.button(f'Odeslat {len(pripraveno)} dodavatelům…', icon='send',
                                  on_click=_potvrdit) \
                        .props('unelevated no-caps') \
                        .classes('bg-indigo-600 text-white font-semibold')
                    if not pripraveno or not smtp['enabled']:
                        b.disable()
            _tlacitka()
        dlg.open()

    # ---------- E-maily dodavatelů (databáze + kontrola + historie) ----------
    async def _otevri_databazi():
        dlg.clear()
        with dlg, ui.card().classes('w-[1150px] max-w-[96vw] gap-2'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('E-maily dodavatelů pro rozesílku sankcí') \
                    .classes('text-lg font-bold text-gray-800')
                ui.button(icon='close', on_click=_zavri).props('flat round dense')
            obsah = ui.column().classes('w-full gap-2')
            with obsah:
                with ui.row().classes('items-center gap-2 py-6'):
                    ui.spinner(size='md')
                    ui.label('Načítám databázi a kontroluji adresy…').classes('text-sm text-gray-500')
        dlg.open()
        await _vykresli_databazi(obsah)

    async def _vykresli_databazi(obsah):
        await asyncio.to_thread(_init_db)
        meta = await asyncio.to_thread(nacti_meta)
        db = await asyncio.to_thread(nacti_databazi)
        plan = await asyncio.to_thread(priprav_plan, db)
        problemy = problemy_databaze(db)
        log = await asyncio.to_thread(nacti_log)
        beh = await asyncio.to_thread(posledni_beh)
        aut_zap = await asyncio.to_thread(automat_zapnuty)
        komu = _popis_prijemcu(await asyncio.to_thread(nacti_prijemce))
        nelze = [p for p in plan if p['stav'] != 'ok']
        obsah.clear()
        with obsah:
            if meta is None:
                _banner('Databáze zatím nebyla nahraná. Nahrajte export „Dod. sankce" (.xlsx) '
                        's dodavateli — sloupce IČO, Jméno a Info Sankce (e-maily).')
            else:
                platnych = sum(1 for e in db.values() if e['rozbor']['platne'])
                ui.label(f'Nahraný soubor: {meta["nazev"]} · {_fmt_dt(meta["kdy"])} ({meta["kdo"]}) · '
                         f'{meta["pocet"]} IČ, z toho {platnych} s platným e-mailem') \
                    .classes('text-sm text-gray-700')
            ui.label(f'Páteční automat ({"ZAPNUTÝ" if aut_zap else "VYPNUTÝ"}): '
                     f'každý pátek ve 3:00 „Pouze podklad" (stav Fakturovat) '
                     f'na {komu}, poté Fakturovat → Odevzdáno účtárně. '
                     + (f'Poslední slot {beh["slot"]:%d.%m.%Y}: '
                        f'{_BEH_LABEL.get(beh["stav"], beh["stav"])}'
                        + (f' — {beh["info"]}' if beh.get('info') else '')
                        if beh else 'Zatím neproběhl.')) \
                .classes('text-xs text-gray-500')

            async def _on_upload(e):
                try:
                    raw, nazev = await _nacti_upload(e)
                except Exception as exc:
                    ui.notify(f'Chyba čtení souboru: {exc}', type='negative')
                    up.reset()
                    return
                up.reset()
                if not raw:
                    ui.notify('Nepodařilo se načíst obsah souboru.', type='negative')
                    return
                if len(raw) > _MAX_UPLOAD:
                    ui.notify('Soubor je větší než 10 MB.', type='negative')
                    return
                zaznamy, chyba = await asyncio.to_thread(precti_xlsx, raw)
                if chyba:
                    ui.notify(chyba, type='negative', multi_line=True, timeout=10000)
                    return
                n, bez, chyba = await asyncio.to_thread(uloz_databazi, zaznamy,
                                                        nazev or 'dodavatele.xlsx', raw, user_name)
                if chyba:
                    ui.notify(chyba, type='negative', multi_line=True, timeout=10000)
                    return
                intranet_logger.log_activity(user_name, 'Sankce',
                                             f'Databáze e-mailů dodavatelů: {nazev} — {n} IČ')
                ui.notify(f'Databáze nahrána: {n} IČ'
                          + (f' ({bez} řádků bez IČ přeskočeno)' if bez else '') + '.',
                          type='positive', position='top-right')
                await _vykresli_databazi(obsah)

            async def _stahni_original():
                nazev, data = await asyncio.to_thread(nacti_soubor, meta['id'])
                if data is None:
                    ui.notify('Soubor nebyl nalezen.', type='negative')
                    return
                S._stahni_pres_http(data, S._safe_filename(nazev.rsplit('.', 1)[0]) + '.xlsx')

            def _stahni_kontrolu():
                S._stahni_pres_http(_xlsx_kontrola(plan, problemy),
                                    f'Kontrola_emailu_dodavatelu_{datetime.date.today():%Y-%m-%d}.xlsx')

            with ui.row().classes('w-full items-center gap-2'):
                up = ui.upload(on_upload=_on_upload, auto_upload=True, max_file_size=_MAX_UPLOAD,
                               label='Nahrát novou databázi (.xlsx) — nahradí stávající') \
                    .props('accept=.xlsx flat bordered').classes('w-96')
                b = ui.button('Stáhnout nahraný soubor', icon='download', on_click=_stahni_original) \
                    .props('outline no-caps color=secondary')
                if meta is None:
                    b.disable()
                ui.button('Stáhnout kontrolu (.xlsx)', icon='fact_check', on_click=_stahni_kontrolu) \
                    .props('outline no-caps color=secondary')

            with ui.tabs().classes('w-full') as tabs:
                t1 = ui.tab(f'Sankce bez platné adresy ({len(nelze)})')
                t2 = ui.tab(f'Chyby v databázi ({len(problemy)})')
                t3 = ui.tab('Historie rozesílek')
            with ui.tab_panels(tabs, value=t1).classes('w-full'):
                with ui.tab_panel(t1).classes('p-0'):
                    ui.label('IČ ze sankcí, které dodavateli ještě neodešly (všechna období, '
                             'kromě Nevyfakturovat) a na která nejde poslat e-mail — o opravu '
                             'kontaktu požádejte uvedeného nákupčího.') \
                        .classes('text-xs text-gray-500 mb-1')
                    _tabulka([('ico', 'IČ', 'left'), ('jmeno', 'Dodavatel', 'left'),
                              ('nakupci', 'Nákupčí', 'left'), ('obdobi', 'Období', 'left'),
                              ('stav', 'Důvod', 'left'), ('pocet', 'Řádků', 'right'),
                              ('castka', 'Částka (Kč)', 'right'), ('info', 'Poznámka', 'left')],
                             _plan_radky(nelze), 'ico',
                             'Všechny čekající sankce mají platnou adresu.')
                with ui.tab_panel(t2).classes('p-0'):
                    ui.label('Záznamy nahrané databáze: neplatné IČ (kontrolní součet), duplicitní '
                             'IČ, neplatné / useknuté e-maily a IČ bez e-mailu. Interní kódy '
                             '(SPOLTTP…) a ERP varianty IČ s příponou (-1, F, …) bez e-mailu se '
                             'nehlásí — pokud na ně čeká sankce, jsou v první záložce.') \
                        .classes('text-xs text-gray-500 mb-1')
                    _tabulka([('ico', 'IČ', 'left'), ('jmeno', 'Jméno', 'left'),
                              ('problem', 'Problém', 'left'), ('neplatne', 'Neplatné', 'left'),
                              ('platne', 'Platné e-maily', 'left'),
                              ('emaily_raw', 'Info Sankce (originál)', 'left')],
                             problemy, 'ico', 'V databázi nejsou žádné chyby.')
                with ui.tab_panel(t3).classes('p-0'):
                    _tabulka([('kdy', 'Kdy', 'left'), ('typ', 'Komu', 'left'), ('ico', 'IČ', 'left'),
                              ('jmeno', 'Dodavatel', 'left'), ('prijemci', 'Adresy', 'left'),
                              ('pocet', 'Řádků', 'right'), ('vysledek', 'Výsledek', 'left'),
                              ('zprava', 'Zpráva', 'left'), ('spustil', 'Spustil', 'left')],
                             [{'id': i, 'kdy': _fmt_dt(r['kdy']),
                               'typ': 'Účtárna' if r['typ'] == 'uctarna' else 'Dodavatel',
                               'ico': r['ico'] or '', 'jmeno': r['jmeno'] or '',
                               'prijemci': r['prijemci'] or '', 'pocet': r['pocet_radku'],
                               'vysledek': 'OK' if r['ok'] else 'CHYBA',
                               'zprava': r['zprava'] or '', 'spustil': r['spustil'] or ''}
                              for i, r in enumerate(log)],
                             'id', 'Zatím se nic nerozesílalo.')

    # ---------- Účtárna hned (hlavní admin) ----------
    async def _otevri_uctarna():
        rows = [r for r in await asyncio.to_thread(S._nacti, 'sankce_vystaveni')
                if r.get('stav') == STAV_FAKTUROVAT]
        komu = _popis_prijemcu(await asyncio.to_thread(nacti_prijemce))
        dlg.clear()
        with dlg, ui.card().classes('w-[560px] max-w-[95vw] gap-3'):
            ui.label('Odeslat podklad účtárně hned').classes('text-lg font-bold text-gray-800')
            ui.label(f'Totéž, co dělá automat každý pátek ve 3:00: „Pouze podklad" všech řádků '
                     f've stavu Fakturovat ({len(rows)} řádků) na {komu} a po odeslání '
                     f'přepnutí na Odevzdáno účtárně.').classes('text-sm text-gray-600')
            prubeh = ui.row().classes('items-center gap-2')
            prubeh.set_visibility(False)
            with prubeh:
                ui.spinner(size='sm')
                ui.label('Generuji PDF a odesílám…').classes('text-sm text-indigo-700')
            vysledek = ui.column().classes('w-full')
            akce = ui.row().classes('w-full justify-end gap-2')

            async def _spustit():
                akce.clear()
                dlg.props('persistent')
                prubeh.set_visibility(True)
                vysl = await odesli_uctarne(user_name, user_id)
                prubeh.set_visibility(False)
                stav['zmeneno'] = bool(vysl.get('prepnuto'))
                with vysledek:
                    _banner(vysl['zprava'], 'emerald' if vysl['ok'] else 'red',
                            'check_circle' if vysl['ok'] else 'error')
                with akce:
                    ui.button('Zavřít', on_click=_zavri).props('unelevated no-caps color=primary')

            with akce:
                ui.button('Zrušit', on_click=_zavri).props('flat no-caps')
                b = ui.button('Odeslat účtárně', icon='send', on_click=_spustit) \
                    .props('unelevated no-caps color=negative')
                if not rows:
                    b.disable()
        dlg.open()

    with ui.button('Rozesílka', icon='forward_to_inbox') \
            .props('unelevated no-caps') \
            .classes('bg-indigo-600 hover:bg-indigo-700 text-white font-semibold rounded-lg shadow-md px-5') \
            .tooltip('E-mailová rozesílka sankcí dodavatelům a databáze jejich adres'):
        with ui.menu():
            ui.menu_item('Rozeslat dodavatelům…', on_click=_otevri_rozeslat)
            ui.menu_item('E-maily dodavatelů (databáze a kontrola)', on_click=_otevri_databazi)
            if ma_vse:
                ui.separator()
                ui.menu_item('Odeslat podklad účtárně hned…', on_click=_otevri_uctarna)
    if ma_vse:
        _obalka_automatu(user_name)


def _obalka_automatu(user_name: str):
    """Obálka vedle „Rozesílka" (jen admin), vzor Lupa na nákup: červeně přeškrtnutá
    = páteční automat účtárny vypnutý (výchozí), zelená = zapnutý. Klik → potvrzení."""
    dlg = ui.dialog()

    def _zapni_sync(zapnout: bool):
        if zapnout and _init_db():
            # Slot, který už začal, zapnutím nedoháníme — jinak by po zapnutí
            # v pátek dopoledne podklad odešel hned. INSERT IGNORE: běh už zapsaný
            # smyčkou zůstane, jak je.
            _beh_zaloz(_slot_pro(datetime.datetime.now()), 'vypnuto',
                       'Automat byl v době slotu vypnutý — nic se neodeslalo.')
        uloz_automat(zapnout)

    async def _prepni(zapnout: bool):
        dlg.close()
        try:
            await asyncio.to_thread(_zapni_sync, zapnout)
        except Exception as e:
            ui.notify(f'Nastavení se nepodařilo uložit: {e}', type='negative')
            return
        intranet_logger.log_activity(user_name, 'Sankce',
                                     f'Páteční automat účtárny: {"ZAPNUTO" if zapnout else "VYPNUTO"}')
        if zapnout:
            ui.notify('Páteční automat pro účtárnu je zapnutý.', type='positive')
        else:
            ui.notify('Páteční automat pro účtárnu je vypnutý.', type='warning')
        obalka.refresh()

    async def _otevri():
        # Režim podle uloženého stavu, ne podle (možná zastaralé) obálky.
        zap = await asyncio.to_thread(automat_zapnuty)
        komu = _popis_prijemcu(await asyncio.to_thread(nacti_prijemce))
        smtp = intranet_emaily.nacti_smtp_uctu(SMTP_PREFIX)
        dlg.clear()
        with dlg, ui.card().classes('w-[520px] max-w-[95vw] gap-3'):
            ui.label('Páteční automat pro účtárnu').classes('text-lg font-bold text-gray-800')
            ui.label(f'Každý pátek ve 3:00 pošle „Pouze podklad" všech řádků ve stavu Fakturovat '
                     f'na {komu} a odeslané řádky přepne na Odevzdáno účtárně.') \
                .classes('text-sm text-gray-600')
            if zap:
                _banner('Automat je ZAPNUTÝ.', 'emerald', 'check_circle')
            else:
                _banner('Automat je VYPNUTÝ. Po zapnutí proběhne první odeslání v nejbližší '
                        'pátek ve 3:00, který teprve přijde — už začatý pátek se nedohání.')
            if not smtp['enabled']:
                _banner(f'Schránka {SANKCE_SCHRANKA} není na serveru nastavená '
                        f'({smtp["duvod"]}) — automat zatím nic neodešle.', 'red', 'error')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                if zap:
                    ui.button('Vypnout', icon='unsubscribe', on_click=lambda: _prepni(False)) \
                        .props('unelevated no-caps color=red-7')
                else:
                    ui.button('Zapnout', icon='forward_to_inbox', on_click=lambda: _prepni(True)) \
                        .props('unelevated no-caps color=green-7')
        dlg.open()

    @ui.refreshable
    def obalka():
        zap = automat_zapnuty()
        if zap:
            tip = (f'Páteční automat je zapnutý: každý pátek ve 3:00 „Pouze podklad" na '
                   f'{_popis_prijemcu(nacti_prijemce())} → Odevzdáno účtárně — kliknutím vypnete')
        else:
            tip = 'Páteční automat pro účtárnu je vypnutý — kliknutím zapnete'
        with ui.button(on_click=_otevri).props('flat round').tooltip(tip):
            with ui.element('div').classes('relative flex ' +
                                           ('text-green-7' if zap else 'text-red-6')):
                ui.icon('mail', size='1.6rem')
                if not zap:
                    ui.element('div').style(_PRESKRTNUTI)

    obalka()


def nastaveni_prijemcu(user_name: str):
    """Ozubené kolečko na pravém kraji lišty pohledů v Sankce k vystavení (jen admin):
    komu jde páteční podklad účtárně za kterou pobočku. Dialog vznikne v místě
    volání; vrací handler, který ho otevře."""
    dlg = ui.dialog()

    async def _otevri():
        cfg = await asyncio.to_thread(nacti_prijemce)
        dlg.clear()
        with dlg, ui.card().classes('w-[520px] max-w-[95vw] gap-3'):
            ui.label('Příjemci podkladu účtárně').classes('text-lg font-bold text-gray-800')
            ui.label('Páteční „Pouze podklad" (i ruční odeslání účtárně) se rozdělí podle '
                     'poboček — každý příjemce dostane jen řádky svých poboček. Řádek '
                     's neznámou pobočkou jde na příjemce 1.').classes('text-sm text-gray-600')
            vstupy = [ui.input(f'Příjemce {i + 1}', value=e,
                               placeholder='' if i == 0 else 'nepovinné')
                      .props('outlined dense').classes('w-full')
                      for i, e in enumerate(cfg['emaily'])]
            volby = {}
            with ui.grid(columns='1fr auto').classes('w-full items-center gap-x-4 gap-y-1'):
                ui.label('Pobočka').classes('text-xs font-bold text-gray-500')
                ui.label('Příjemce').classes('text-xs font-bold text-gray-500')
                for kod, nazev in S._POBOCKY_NAZVY.items():
                    ui.label(f'{kod} {nazev}').classes('text-sm text-gray-800')
                    volby[kod] = ui.toggle({i: str(i + 1) for i in range(_PRIJEMCU)},
                                           value=cfg['pobocky'].get(kod, 0)) \
                        .props('dense unelevated toggle-color=indigo')

            async def _ulozit():
                emaily = [(v.value or '').strip() for v in vstupy]
                pobocky = {k: int(t.value or 0) for k, t in volby.items()}
                chyby = [f'Příjemce {i + 1}: neplatná adresa „{e}".'
                         for i, e in enumerate(emaily)
                         if e and not intranet_emaily.je_platny_email(e)]
                if not emaily[0]:
                    chyby.append('Příjemce 1 je povinný.')
                chyby += [f'Příjemce {i + 1} nemá adresu, ale má přiřazené pobočky.'
                          for i in range(1, _PRIJEMCU)
                          if not emaily[i] and i in pobocky.values()]
                if chyby:
                    ui.notify(' '.join(chyby), type='negative')
                    return
                try:
                    await asyncio.to_thread(uloz_prijemce, emaily, pobocky)
                except Exception as e:
                    ui.notify(f'Nastavení se nepodařilo uložit: {e}', type='negative')
                    return
                popis = _popis_prijemcu({'emaily': emaily, 'pobocky': pobocky})
                intranet_logger.log_activity(user_name, 'Sankce',
                                             f'Příjemci podkladu účtárně: {popis}')
                dlg.close()
                ui.notify(f'Uloženo — podklad půjde na {popis}.', type='positive')

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                ui.button('Uložit', icon='save', on_click=_ulozit) \
                    .props('unelevated no-caps color=primary')
        dlg.open()

    return _otevri
