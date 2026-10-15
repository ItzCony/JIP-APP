"""
Modul „Gastrokurzy" – termíny kurzů, přihlášky poboček a prezenční listiny.

Dva druhy kurzů:
  - běžné kurzy (Excel „KURZY_2026_termíny_a_přehled_kurzů_PODZIM"): místo konání
    Praha / Ostrava, datum, lektor (v Excelu komentář u data), pobočky a počty lidí;
  - TOP kurzy (Excel „Tabulka obsazenosti kurzu Praha…"): firemní kurzy
    ORKLA / UNILEVER / NESTLE s pevnou kapacitou a hlídáním volných míst.

Dvojklik na termín (resp. tlačítko „Prezenční listina") otevře prezenčku, kam
ASM/vedoucí pobočky zapisuje pozvané zákazníky: OZ, IČO, jméno, funkce, telefon
(nepovinný) a podpis (dá se doplnit až v den kurzu). Místo, datum, kurz i pobočka
se doplní podle termínu. Zrušený zápis se nemaže – zůstane přeškrtnutý s údajem
kdo a kdy ho zrušil. Tlačítko „+1" přidá kopii posledního řádku formuláře (bez jména
a podpisu) – u dalších osob ze stejné provozovny stačí doplnit jméno.

Práva:
  gastrokurzy_spravce      – zakládá/edituje termíny, zapisuje za kohokoliv, ruší zápisy
  gastrokurzy_zapisovatel  – ASM/vedoucí: zapisuje a ruší zápisy (vlastní zápisy)
  gastrokurzy_ctenar       – jen prohlížení

Vzory převzaté z projektu:
  - pobočky + klíče          → intranet_vysledky.py
  - DB init / async pattern  → intranet_spolvecer.py
  - logování                 → intranet_logger.py

intranet.py volá:  intranet_gastrokurzy.vykresli(user_id, user_name, vsechna_prava)
web_main.py volá:  intranet_gastrokurzy.inicializace_db()
"""

from nicegui import ui, app
import intranet_data
import intranet_logger
import intranet_emaily
import gastrokurzy_seed
import gastrokurzy_seed_prezence
import asyncio
import calendar
import datetime
import json
import re
import unicodedata

# ==========================================
# KONSTANTY
# ==========================================
LOG_KATEGORIE = 'Gastrokurzy'

MISTA = ['Praha', 'Ostrava']

# Pobočky, které se můžou na kurz hlásit (řazení dle intranet_vysledky.py).
POBOCKY = [
    ('pardubice', 'Pardubice'), ('praha', 'Praha'), ('jilemnice', 'Jilemnice'),
    ('most', 'Most'), ('liberec', 'Liberec'), ('hodonin', 'Hodonín'),
    ('zlin', 'Zlín'), ('ostrava', 'Ostrava'), ('olomouc', 'Olomouc'),
    ('ceske_budejovice', 'České Budějovice'), ('plzen', 'Plzeň'),
    ('horsovsky_tyn', 'Horšovský Týn'), ('nova_role', 'Nová Role'),
    ('winelife', 'Winelife'), ('kamiony', 'Kamiony'),
]
POBOCKY_NAZVY = dict(POBOCKY)

# TOP kurzy – barva podle firmy (gradient dlaždice).
FIRMY_STYL = {
    'ORKLA':    ('#7C3AED', '#C084FC'),
    'UNILEVER': ('#0E7490', '#38BDF8'),
    'NESTLE':   ('#B45309', '#FBBF24'),
}
_FIRMA_DEFAULT = ('#334155', '#94A3B8')
# Kurz JIP – barva podle místa konání (Praha růžová jako v Excelu, Ostrava zelená);
# druhý odstín = barva místa v kalendáři a popiscích.
_JIP_STYL = {'Praha': ('#F43F5E', '#E11D48'), 'Ostrava': ('#059669', '#047857')}


def _styl_mista(misto):
    return _JIP_STYL.get(misto, _JIP_STYL['Praha'])

_MESICE = ['', 'leden', 'únor', 'březen', 'duben', 'květen', 'červen',
           'červenec', 'srpen', 'září', 'říjen', 'listopad', 'prosinec']

_db_init = False


# ==========================================
# POMOCNÉ FORMÁTOVÁNÍ
# ==========================================
def _bez_diakritiky(s):
    """'Plzeň:' → 'plzen' – klíč pro párování hlaviček a názvů poboček z Excelu."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    return ''.join(c for c in s if not unicodedata.combining(c)).lower().replace(':', '').strip()


def _fmt_datum(d):
    """date → '09.09.2026'; None → 'neurčeno'."""
    if not d:
        return 'neurčeno'
    if isinstance(d, datetime.datetime):
        d = d.date()
    return f'{d.day:02d}.{d.month:02d}.{d.year}'


def _fmt_mesic(d):
    """date → 'září 2026' (hlavička skupiny)."""
    if not d:
        return 'Bez termínu'
    return f'{_MESICE[d.month]} {d.year}'


def _parse_datum(s):
    """'09.09.2026' / '2026-09-09' → datetime.date, jinak None."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ==========================================
# DB INICIALIZACE
# ==========================================
def inicializace_db():
    """Vytvoří tabulky modulu a naplní je daty z Excelu (idempotentní)."""
    global _db_init
    if _db_init:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS gastrokurzy_termin (
            id INT AUTO_INCREMENT PRIMARY KEY,
            typ VARCHAR(10) NOT NULL DEFAULT 'standard',
            misto VARCHAR(50) NOT NULL,
            datum DATE NULL,
            nazev VARCHAR(255) NULL,
            firma VARCHAR(100) NULL,
            lektor VARCHAR(100) NULL,
            kapacita INT NULL,
            stav VARCHAR(20) NOT NULL DEFAULT 'aktivni',
            poznamka TEXT NULL,
            zdroj VARCHAR(30) NULL,
            created_at DATETIME DEFAULT NOW(),
            updated_at DATETIME DEFAULT NOW() ON UPDATE NOW(),
            INDEX idx_typ (typ),
            INDEX idx_datum (datum)
        ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS gastrokurzy_prihlaska (
            id INT AUTO_INCREMENT PRIMARY KEY,
            termin_id INT NOT NULL,
            pobocka_klic VARCHAR(50) NOT NULL,
            oz VARCHAR(100) NULL,
            ico VARCHAR(50) NULL,
            provozovna VARCHAR(255) NULL,
            zakaznik VARCHAR(255) NULL,
            funkce VARCHAR(255) NULL,
            telefon VARCHAR(50) NULL,
            podpis TINYINT NOT NULL DEFAULT 0,
            stav VARCHAR(20) NOT NULL DEFAULT 'aktivni',
            zapsal VARCHAR(100) NULL,
            zrusil VARCHAR(100) NULL,
            zruseno_kdy DATETIME NULL,
            created_at DATETIME DEFAULT NOW(),
            updated_at DATETIME DEFAULT NOW() ON UPDATE NOW(),
            INDEX idx_termin (termin_id),
            INDEX idx_pob (pobocka_klic),
            FOREIGN KEY (termin_id) REFERENCES gastrokurzy_termin (id) ON DELETE CASCADE
        ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
        conn.commit()
        # Stopa o e-mailu o změnách — v DB, ne v RAM, aby restart serveru nezpůsobil
        # druhé odeslání téhož termínu. Verze se počítá, nesrovnává s časem:
        # `updated_at` má granularitu 1 s, takže úprava ve stejné sekundě jako
        # rozesílka by se ztratila. Termíny existující v okamžiku migrace se
        # označí za ohlášené (žádný mail zpětně za historii). `souhrn_data` =
        # JSON ohlášeného stavu, z něj e-mail vypisuje „původně → nyní".
        for prikaz in (
            "ALTER TABLE gastrokurzy_termin ADD COLUMN verze INT NOT NULL DEFAULT 1",
            "ALTER TABLE gastrokurzy_termin ADD COLUMN souhrn_verze INT NULL",
            "ALTER TABLE gastrokurzy_termin ADD COLUMN souhrn_odeslan DATETIME NULL",
            "ALTER TABLE gastrokurzy_termin ADD COLUMN souhrn_data TEXT NULL",
            "ALTER TABLE gastrokurzy_prihlaska ADD COLUMN provozovna VARCHAR(255) NULL AFTER ico",
            # IČO z prezenční listiny bývá i s názvem firmy („27553205 - JET Chlumec s.r.o.").
            "ALTER TABLE gastrokurzy_prihlaska MODIFY COLUMN ico VARCHAR(50) NULL",
            # `zdroj` říká, KTERÝ import termín založil. Guard proti opakovanému
            # importu tak drží na termínech, ne na přihláškách — smazání všech
            # přihlášených (úklid dat) jinak import při restartu vrátí zpátky.
            "ALTER TABLE gastrokurzy_termin ADD COLUMN zdroj VARCHAR(30) NULL",
            "UPDATE gastrokurzy_termin t SET t.zdroj = 'import-prezence', "
            "t.updated_at = t.updated_at WHERE t.zdroj IS NULL AND EXISTS ("
            "SELECT 1 FROM gastrokurzy_prihlaska p WHERE p.termin_id = t.id "
            "AND p.zapsal = 'import-prezence')",
            "UPDATE gastrokurzy_termin SET zdroj = 'import-zadani', "
            "updated_at = updated_at WHERE zdroj IS NULL",
            "UPDATE gastrokurzy_termin SET souhrn_verze = verze, souhrn_odeslan = NOW(), "
            "updated_at = updated_at WHERE souhrn_verze IS NULL",
            # Rezervace = místo zapsané bez jména zákazníka; upravovat ho smí jen
            # ten, kdo ho zapsal (`zapsal`), i po doplnění jména.
            "ALTER TABLE gastrokurzy_prihlaska ADD COLUMN rezervace TINYINT NOT NULL DEFAULT 0 AFTER podpis",
        ):
            try:
                cur.execute(prikaz)
                conn.commit()
            except Exception:
                conn.rollback()     # sloupec už existuje
        _db_init = True
    except Exception as e:
        print(f"[gastrokurzy] DB init chyba: {e}")
        cur.close()
        conn.close()
        return
    cur.close()
    conn.close()
    _naseeduj()
    _naseeduj_prezenci()


def _naseeduj():
    """Jednorázový import dat z Excelů zadání (jen když je tabulka prázdná).

    Počty přihlášených z Excelu nemají jména – zakládají se jako anonymní řádky
    prezenční listiny, které ASM/vedoucí doplní. Počet v přehledu = počet
    aktivních řádků, takže je jen jeden zdroj pravdy.
    """
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM gastrokurzy_termin WHERE zdroj = 'import-zadani'")
        if cur.fetchone()[0]:
            return
        vse = ([('standard', t) for t in gastrokurzy_seed.TERMINY]
               + [('top', t) for t in gastrokurzy_seed.TOP_TERMINY])
        for typ, t in vse:
            # souhrn_odeslan = NOW(): import zadání se do denního souhrnu nehlásí.
            cur.execute("""INSERT INTO gastrokurzy_termin
                (typ, misto, datum, nazev, firma, lektor, kapacita, stav,
                 zdroj, souhrn_verze, souhrn_odeslan)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'import-zadani', 1, NOW())""",
                        (typ, t['misto'], t.get('datum'), t.get('nazev'), t.get('firma'),
                         t.get('lektor'), t.get('kapacita'), t.get('stav', 'aktivni')))
            tid = cur.lastrowid
            for klic, pocet in t['prihlasky']:
                for _ in range(int(pocet or 0)):
                    cur.execute("""INSERT INTO gastrokurzy_prihlaska
                        (termin_id, pobocka_klic, zapsal) VALUES (%s, %s, 'import')""",
                                (tid, klic))
        conn.commit()
        print(f"[gastrokurzy] Import zadání: {len(vse)} termínů")
    except Exception as e:
        print(f"[gastrokurzy] Seed chyba: {e}")
    finally:
        cur.close()
        conn.close()


def _naseeduj_prezenci():
    """Import prezenční listiny ASM 2026 – jarní kurzy i s konkrétními účastníky.

    Jde o jiné termíny než podzimní z KURZY.xlsx, proto se zakládají samostatně
    (historie). Vlastní krok, aby šel spustit i nad už naplněnou tabulkou:
    poznávacím znamením je `gastrokurzy_termin.zdroj = 'import-prezence'`.
    """
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM gastrokurzy_termin WHERE zdroj = 'import-prezence'")
        if cur.fetchone()[0]:
            return
        osob = 0
        for t in gastrokurzy_seed_prezence.TERMINY:
            cur.execute("""INSERT INTO gastrokurzy_termin
                (typ, misto, datum, nazev, stav, zdroj, souhrn_verze, souhrn_odeslan)
                VALUES ('standard', %s, %s, %s, 'aktivni', 'import-prezence', 1, NOW())""",
                        (t['misto'], t['datum'], t['nazev']))
            tid = cur.lastrowid
            for o in t['ucastnici']:
                cur.execute("""INSERT INTO gastrokurzy_prihlaska
                    (termin_id, pobocka_klic, oz, ico, provozovna, zakaznik,
                     funkce, telefon, podpis, zapsal)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'import-prezence')""",
                            (tid, o['pobocka'], o['oz'], o['ico'], o['provozovna'],
                             o['jmeno'], o['funkce'], o['telefon'], o['podpis']))
                osob += 1
        conn.commit()
        print(f"[gastrokurzy] Import prezenční listiny: "
              f"{len(gastrokurzy_seed_prezence.TERMINY)} termínů, {osob} účastníků")
    except Exception as e:
        print(f"[gastrokurzy] Import prezence chyba: {e}")
    finally:
        cur.close()
        conn.close()


# Pobočky v Excelu bývají zkratkou, ne plným názvem.
_POBOCKA_ALIAS = {'ov': 'ostrava', 'cb': 'ceske_budejovice', 'ht': 'horsovsky_tyn'}


def _klic_pobocky(nazev):
    k = _bez_diakritiky(nazev)
    if not k:
        return None
    if k in _POBOCKA_ALIAS:
        return _POBOCKA_ALIAS[k]
    for klic, popis in POBOCKY:
        if k in (klic, _bez_diakritiky(popis)):
            return klic
    return None


def _pole_sloupce(hlavicka):
    """Název sloupce z Excelu → pole importu. 'provozovna' před 'oz' (obsahuje ho)."""
    k = _bez_diakritiky(hlavicka)
    for slovo, pole in (('datum', 'datum'), ('nazev', 'nazev'), ('pobocka', 'pobocka'),
                        ('provozovna', 'provozovna'), ('ico', 'ico'), ('jmeno', 'jmeno'),
                        ('funkce', 'funkce'), ('telefon', 'telefon'), ('podpis', 'podpis'),
                        ('misto', 'misto'), ('praha', 'misto')):
        if slovo in k:
            return pole
    return 'oz' if 'oz' in k.split() else None


def _txt(v):
    """Buňka → text bez '.0' u čísel; prázdná → None."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip() or None


def _import_navrh(raw, zapsat=True):
    """Import listu „Návrh kurzů" (KURZY_<rok>_termíny_a_přehled_kurzů….xlsx).

    Sloupec A = místo, B = datum (lektor v komentáři „Lektor: …"), řádek 1 = název
    kurzu, pod ním dvojice sloupců Pobočka + poč. Každé místo se zapíše jako řádek
    „K doplnění" (bez jména, rezervace=0 – doplnit ho smí kdokoli s právem zápisu).
    Termín se páruje podle místa a data; prázdný název a lektor se doplní,
    vyplněné hodnoty se nepřepisují. Import jen dorovnává aktivní zápisy pobočky
    na počet z Excelu a nic nemaže, takže jde spustit opakovaně.
    zapsat=False → jen náhled, do DB se nic nezapíše.
    """
    import io
    import re
    import openpyxl
    try:
        # read_only by neumožnil číst sloučené buňky ani komentáře
        ws = openpyxl.load_workbook(io.BytesIO(raw), data_only=True).worksheets[0]
    except Exception as e:
        return {'chyba': f'Soubor nejde přečíst: {e}'}

    sloucene = {}
    for m in ws.merged_cells.ranges:
        for r in range(m.min_row, m.max_row + 1):
            for c in range(m.min_col, m.max_col + 1):
                sloucene[(r, c)] = (m.min_row, m.min_col)

    def bunka(r, c):
        return ws.cell(*sloucene.get((r, c), (r, c)))

    kurzy = {c: _txt(bunka(1, c).value) for c in range(3, ws.max_column + 1)
             if _bez_diakritiky(bunka(2, c).value).startswith('pobocka')
             and _txt(bunka(1, c).value)}
    if not kurzy:
        return {'chyba': 'Tohle není Návrh kurzů – v 2. řádku chybí sloupce Pobočka / poč.'}
    rok = re.search(r'(20\d\d)', str(ws['A1'].value or ''))
    rok = int(rok[1]) if rok else datetime.date.today().year

    bloky = {}          # (místo, datum, název) → {'lektor': …, 'pobocky': {klíč: počet}}
    poznamky, nahlaseno = [], set()
    for r in range(3, ws.max_row + 1):
        misto = next((m for m in MISTA if _bez_diakritiky(m) == _bez_diakritiky(bunka(r, 1).value)),
                     None)
        bd = bunka(r, 2)
        if not misto or bd.value is None:
            continue
        if isinstance(bd.value, datetime.datetime):
            d = bd.value.date()
        else:
            mm = re.match(r'\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*$', str(bd.value))
            try:
                d = datetime.date(rok, int(mm[2]), int(mm[1])) if mm else None
            except ValueError:
                d = None
        if not d:
            if bd.coordinate not in nahlaseno:
                nahlaseno.add(bd.coordinate)
                poznamky.append(f'{bd.coordinate}: datum „{bd.value}" – přeskočeno')
            continue
        lektor = re.search(r'Lektor\s*:\s*([^\n]+)', bd.comment.text) if bd.comment else None
        lektor = lektor[1].strip() if lektor else None
        for c, nazev in kurzy.items():
            p = _txt(ws.cell(r, c).value)
            if not p:
                continue
            n = _txt(ws.cell(r, c + 1).value)
            mm = re.match(r'^(.*?)\s+(\d+)$', p)
            if n is None and mm:            # „Praha 2" bez vyplněného počtu
                p, n = mm[1], mm[2]
            klic = _klic_pobocky(p)
            if not klic or not str(n or '').isdigit():
                poznamky.append(f'{ws.cell(r, c).coordinate}: „{p}" / {n} – nerozpoznáno')
                continue
            b = bloky.setdefault((misto, d, nazev), {'lektor': lektor, 'pobocky': {}})
            b['pobocky'][klic] = b['pobocky'].get(klic, 0) + int(n)
    if not bloky:
        return {'chyba': 'V souboru nejsou žádné kurzy s pobočkami.'}

    conn = intranet_data.get_db_connection()
    if not conn:
        return {'chyba': 'Databáze není dostupná.'}
    cur = conn.cursor()
    vysl = {'nove': 0, 'doplnene': 0, 'mist': 0, 'terminu': 0, 'radky': [], 'poznamky': poznamky}
    try:
        for (misto, d, nazev), b in sorted(bloky.items(), key=lambda x: (x[0][1], x[0][0])):
            popis = f'{_fmt_datum(d)} {misto} – {nazev}'
            cur.execute("""SELECT id, nazev, lektor FROM gastrokurzy_termin
                  WHERE typ='standard' AND misto=%s AND datum=%s ORDER BY id""", (misto, d))
            kandidati = cur.fetchall()
            # při zdvojeném termínu vyhrává starší (nižší id)
            shoda = (next((k for k in kandidati if _bez_diakritiky(k[1]) == _bez_diakritiky(nazev)), None)
                     or next((k for k in kandidati if not (k[1] or '').strip()), None))
            if kandidati and not shoda:
                poznamky.append(f'{popis}: v intranetu je ten den „{kandidati[0][1]}" – přeskočeno')
                continue
            vysl['terminu'] += 1
            tid, stav_db, akce = None, {}, ''
            if shoda:
                tid = shoda[0]
                dopln = [k for k, je in (('název', not (shoda[1] or '').strip()),
                                         ('lektor', b['lektor'] and not (shoda[2] or '').strip())) if je]
                if dopln:
                    vysl['doplnene'] += 1
                    akce = 'doplněn ' + ' + '.join(dopln)
                    if zapsat:
                        cur.execute("""UPDATE gastrokurzy_termin
                              SET nazev = COALESCE(NULLIF(TRIM(nazev), ''), %s),
                                  lektor = COALESCE(NULLIF(TRIM(lektor), ''), %s)
                              WHERE id=%s""", (nazev, b['lektor'], tid))
                cur.execute("""SELECT pobocka_klic, COUNT(*) FROM gastrokurzy_prihlaska
                      WHERE termin_id=%s AND stav='aktivni' GROUP BY pobocka_klic""", (tid,))
                stav_db = {k: int(n) for k, n in cur.fetchall()}
            else:
                vysl['nove'] += 1
                akce = 'nový termín'
                if zapsat:
                    # souhrn_odeslan = NOW(): import se do denního souhrnu nehlásí
                    cur.execute("""INSERT INTO gastrokurzy_termin
                          (typ, misto, datum, nazev, lektor, stav, zdroj, souhrn_verze, souhrn_odeslan)
                          VALUES ('standard', %s, %s, %s, %s, 'aktivni', 'import-navrh', 1, NOW())""",
                                (misto, d, nazev, b['lektor']))
                    tid = cur.lastrowid
            pridat, vic = [], []
            for klic, pocet in b['pobocky'].items():
                chybi = pocet - stav_db.get(klic, 0)
                nazev_p = POBOCKY_NAZVY.get(klic, klic)
                if chybi > 0:
                    pridat.append(f'{nazev_p} +{chybi}')
                    vysl['mist'] += chybi
                    if zapsat:
                        cur.executemany("""INSERT INTO gastrokurzy_prihlaska
                              (termin_id, pobocka_klic, rezervace, zapsal)
                              VALUES (%s, %s, 0, 'import-navrh')""", [(tid, klic)] * chybi)
                elif chybi < 0:
                    vic.append(f'{nazev_p}: v intranetu {stav_db[klic]}, v Excelu {pocet}')
            if pridat or akce:
                vysl['radky'].append(f"{popis}: {', '.join(([akce] if akce else []) + pridat)}")
            if vic:
                poznamky.append(f"{popis} – víc zápisů než v Excelu (nic se nemaže): {'; '.join(vic)}")
        if zapsat:
            conn.commit()
        else:
            conn.rollback()
        return vysl
    except Exception as e:
        conn.rollback()
        print(f"[gastrokurzy] Import Návrhu kurzů: {e}")
        return {'chyba': f'Import selhal: {e}'}
    finally:
        cur.close()
        conn.close()


def _import_excel(raw):
    """Import kurzů i účastníků z Excelu (jeden řádek = jeden účastník).

    Termín se páruje podle místa, data a názvu – existující se jen doplní o lidi,
    jinak vznikne nový. Účastník se přeskočí, když na termínu už stejné jméno + IČO
    je, takže opakovaný import nic nezdvojí.
    """
    import io
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        radky = list(wb.worksheets[0].iter_rows(values_only=True))
    except Exception as e:
        return {'chyba': f'Soubor nejde přečíst: {e}'}
    if len(radky) < 2:
        return {'chyba': 'Soubor je prázdný.'}

    idx = {}
    for i, h in enumerate(radky[0]):
        pole = _pole_sloupce(h)
        if pole and pole not in idx:
            idx[pole] = i
    chybi = [p for p in ('misto', 'datum', 'nazev', 'jmeno') if p not in idx]
    if chybi:
        return {'chyba': f"V hlavičce chybí sloupce: {', '.join(chybi)}"}

    def bunka(r, pole):
        i = idx.get(pole)
        return _txt(r[i]) if i is not None and i < len(r) else None

    skupiny = {}
    spatnych = 0
    for r in radky[1:]:
        jmeno = bunka(r, 'jmeno')
        d = r[idx['datum']] if idx['datum'] < len(r) else None
        datum = d.date() if isinstance(d, datetime.datetime) else (
            d if isinstance(d, datetime.date) else _parse_datum(_txt(d)))
        nazev = bunka(r, 'nazev')
        misto_txt = bunka(r, 'misto')
        if not (jmeno and datum and nazev and misto_txt):
            if any(v is not None for v in r):
                spatnych += 1
            continue
        misto = next((m for m in MISTA if _bez_diakritiky(m) == _bez_diakritiky(misto_txt)),
                     misto_txt)
        skupiny.setdefault((misto, datum, nazev), []).append({
            'pobocka': _klic_pobocky(bunka(r, 'pobocka')) or _klic_pobocky(misto),
            'oz': bunka(r, 'oz'), 'ico': bunka(r, 'ico'),
            'provozovna': bunka(r, 'provozovna'), 'jmeno': jmeno,
            'funkce': bunka(r, 'funkce'), 'telefon': bunka(r, 'telefon'),
            'podpis': 1 if bunka(r, 'podpis') else 0,
        })
    if not skupiny:
        return {'chyba': 'Žádný použitelný řádek (chybí místo, datum, název nebo jméno).'}

    conn = intranet_data.get_db_connection()
    if not conn:
        return {'chyba': 'Databáze není dostupná.'}
    cur = conn.cursor()
    nove = doplnene = osob = duplicit = 0
    try:
        for (misto, datum, nazev), lide in skupiny.items():
            cur.execute("""SELECT id FROM gastrokurzy_termin
                  WHERE misto=%s AND datum=%s AND nazev=%s ORDER BY id LIMIT 1""",
                        (misto, datum, nazev))
            radek = cur.fetchone()
            if radek:
                tid = radek[0]
                doplnene += 1
            else:
                # souhrn_odeslan = NOW(): import se do denního souhrnu nehlásí.
                cur.execute("""INSERT INTO gastrokurzy_termin
                      (typ, misto, datum, nazev, stav, zdroj, souhrn_verze, souhrn_odeslan)
                      VALUES ('standard', %s, %s, %s, 'aktivni', 'import-excel', 1, NOW())""",
                            (misto, datum, nazev))
                tid = cur.lastrowid
                nove += 1
            cur.execute("SELECT zakaznik, ico FROM gastrokurzy_prihlaska WHERE termin_id=%s",
                        (tid,))
            uz_tam = {(_bez_diakritiky(a), _bez_diakritiky(b)) for a, b in cur.fetchall()}
            for o in lide:
                klic = (_bez_diakritiky(o['jmeno']), _bez_diakritiky(o['ico']))
                if klic in uz_tam:
                    duplicit += 1
                    continue
                uz_tam.add(klic)
                cur.execute("""INSERT INTO gastrokurzy_prihlaska
                      (termin_id, pobocka_klic, oz, ico, provozovna, zakaznik, funkce,
                       telefon, podpis, zapsal)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'import-excel')""",
                            (tid, o['pobocka'], o['oz'], o['ico'], o['provozovna'],
                             o['jmeno'], o['funkce'], o['telefon'], o['podpis']))
                osob += 1
        conn.commit()
        return {'nove': nove, 'doplnene': doplnene, 'osob': osob,
                'duplicit': duplicit, 'spatnych': spatnych}
    except Exception as e:
        conn.rollback()
        print(f"[gastrokurzy] Import Excelu: {e}")
        return {'chyba': f'Import selhal: {e}'}
    finally:
        cur.close()
        conn.close()


# ==========================================
# EXPORT DO EXCELU (blokující – volat přes asyncio.to_thread)
# ==========================================
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _nazev_souboru(*casti, pripona='xlsx'):
    """('prezencka', date, 'Praha') → 'prezencka_2026-09-09_praha.xlsx' (jen ASCII)."""
    return '_'.join(re.sub(r'[^a-z0-9]+', '-', _bez_diakritiky(c)).strip('-')
                    for c in casti if c) + '.' + pripona


def _export_xlsx(terminy, prihlasky, titulek, podtitulek):
    """Formátovaný .xlsx (bytes) s účastníky zadaných termínů.

    Jeden termín → list „Prezenční listina" (údaje kurzu v podtitulku).
    Víc termínů → list „Kurzy" (přehled s počty) + list „Účastníci" se sloupci kurzu.
    prihlasky = zápisy (dicty s termin_id); zápisy jiných termínů se ignorují,
    zrušené jsou na konci kurzu šedé a přeškrtnuté.
    """
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    tenka = Side(style='thin', color='D1D5DB')
    ramecek = Border(left=tenka, right=tenka, top=tenka, bottom=tenka)
    zebra = PatternFill('solid', fgColor='FFF1F2')
    H = 4                                   # řádek hlavičky tabulky

    def _list(ws, sloupce, radky, zrusene, souhrn=None):
        ws['A1'] = titulek
        ws['A1'].font = Font(bold=True, size=14, color='E11D48')
        ws['A2'] = podtitulek
        ws['A2'].font = Font(italic=True, color='6B7280')
        for c, (nadpis, sirka) in enumerate(sloupce, 1):
            b = ws.cell(row=H, column=c, value=nadpis)
            b.font = Font(bold=True, color='FFFFFF')
            b.fill = PatternFill('solid', fgColor='E11D48')
            b.border = ramecek
            b.alignment = Alignment(vertical='center', wrap_text=True)
            ws.column_dimensions[get_column_letter(c)].width = sirka
        for i, hodnoty in enumerate(radky):
            for c, v in enumerate(hodnoty, 1):
                b = ws.cell(row=H + 1 + i, column=c, value=v)
                b.border = ramecek
                b.alignment = Alignment(vertical='top', wrap_text=True)
                if isinstance(v, datetime.date):
                    b.number_format = 'DD.MM.YYYY'
                if i in zrusene:
                    b.font = Font(color='9CA3AF', strike=True)
                elif i % 2:
                    b.fill = zebra
        for c, v in (souhrn or {}).items():
            ws.cell(row=H + 1 + len(radky), column=c, value=v).font = Font(bold=True)
        ws.freeze_panes = ws.cell(row=H + 1, column=1)
        ws.auto_filter.ref = f'A{H}:{get_column_letter(len(sloupce))}{H + max(len(radky), 1)}'
        ws.print_title_rows = f'{H}:{H}'
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True

    podle_terminu = {}
    for p in prihlasky:
        podle_terminu.setdefault(p['termin_id'], []).append(p)
    vice = len(terminy) > 1
    wb = openpyxl.Workbook()
    ws = wb.active

    if vice:
        ws.title = 'Kurzy'
        radky, zrusene = [], set()
        for t in terminy:
            pp = podle_terminu.get(t['id'], [])
            if t['stav'] == 'zruseno':
                zrusene.add(len(radky))
            radky.append([
                t['datum'], t['nazev'] or '',
                t.get('firma') or ('dodavatel' if t['typ'] == 'top' else 'JIP'),
                t['misto'] or '', t['lektor'] or '', t.get('kapacita') or None,
                sum(p['stav'] == 'aktivni' for p in pp), _volna_mista(t, pp),
                sum(p['stav'] == 'zruseno' for p in pp),
                ', '.join(f'{POBOCKY_NAZVY.get(k, k)} {n}' for k, n in t.get('pobocky', [])),
                'Zrušen' if t['stav'] == 'zruseno' else 'Aktivní'])
        _list(ws, [('Datum', 12), ('Kurz', 34), ('Pořadatel', 13), ('Místo', 11), ('Lektor', 22),
                   ('Kapacita', 10), ('Přihlášeno', 11), ('Volná místa', 11),
                   ('Zrušené zápisy', 11), ('Pobočky', 40), ('Stav termínu', 12)],
              radky, zrusene, souhrn={1: 'Celkem', 7: sum(r[6] for r in radky)})
        ws = wb.create_sheet('Účastníci')
    else:
        ws.title = 'Prezenční listina'

    radky, zrusene = [], set()
    for t in terminy:
        kurz = [t['datum'], t['nazev'] or '', t['misto'] or ''] if vice else []
        for p in sorted(podle_terminu.get(t['id'], []), key=lambda p: p['stav'] == 'zruseno'):
            if p['stav'] == 'zruseno':
                zrusene.add(len(radky))
            radky.append([len(radky) + 1] + kurz + [
                POBOCKY_NAZVY.get(p['pobocka_klic'], p['pobocka_klic'] or ''),
                p['oz'] or '', p['ico'] or '', p['provozovna'] or '',
                p['zakaznik'] or ('(rezervace)' if p.get('rezervace') else ''),
                p['funkce'] or '', p['telefon'] or '', '✓' if p['podpis'] else '',
                'Zrušeno' if p['stav'] == 'zruseno' else 'Aktivní', p['zapsal'] or ''])
    _list(ws, [('Č.', 5)] + ([('Datum', 12), ('Kurz', 30), ('Místo', 11)] if vice else []) + [
              ('Pobočka', 16), ('OZ', 12), ('IČO', 12), ('Provozovna', 28),
              ('Jméno zákazníka', 26), ('Funkce', 18), ('Telefon', 15), ('Podpis', 14),
              ('Stav', 10), ('Zapsal', 20)],
          radky, zrusene)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ==========================================
# EXPORT DO PDF – jen tabulka zapsaných z prezenčky (Playwright/Chromium)
# ==========================================
PDF_MIME = 'application/pdf'
_PDF_ZAMEK = None
_SVG_FAJFKA = ('<svg viewBox="0 0 12 12"><polyline points="2.5,6.2 5,8.7 9.5,3.5" fill="none" '
               'stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _pdf_zamek():
    """V jeden okamžik běží jen jedno Chromium (paměť serveru)."""
    global _PDF_ZAMEK
    if _PDF_ZAMEK is None:
        _PDF_ZAMEK = asyncio.Semaphore(1)
    return _PDF_ZAMEK


def _prezencka_html(prihlasky, titulek):
    """HTML jen s tabulkou zapsaných – stejné sloupce a vzhled jako prezenčka v aplikaci."""
    from html import escape as e
    radky = []
    for p in prihlasky:
        jmeno = (p['zakaznik'] or '').strip()
        if jmeno:
            jmeno_td = f'<td class="jm">{e(jmeno)}</td>'
        elif p.get('rezervace'):
            jmeno_td = '<td class="rez">Rezervováno</td>'
        else:
            jmeno_td = '<td class="dopl">K doplnění</td>'
        radky.append(
            '<tr>'
            f'<td class="pob">{e(POBOCKY_NAZVY.get(p["pobocka_klic"], p["pobocka_klic"] or ""))}</td>'
            f'<td>{e(p["oz"] or "—")}</td>'
            f'<td class="ico">{e(p["ico"] or "—")}</td>'
            f'<td>{e(p["provozovna"] or "—")}</td>'
            f'{jmeno_td}'
            f'<td>{e(p["funkce"] or "—")}</td>'
            f'<td>{e(p["telefon"] or "—")}</td>'
            f'<td class="pod"><span class="box{" ok" if p["podpis"] else ""}">'
            f'{_SVG_FAJFKA if p["podpis"] else ""}</span></td>'
            '</tr>')
    if not radky:
        radky.append('<tr><td colspan="8" class="prazdne">Zatím se nikdo nezapsal.</td></tr>')
    return (
        '<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
        f'<title>{e(titulek)}</title><style>'
        '*{box-sizing:border-box}'
        'body{margin:0;font-family:Roboto,"Segoe UI",Arial,sans-serif;font-size:9pt;color:#374151}'
        'table{width:100%;border-collapse:collapse}'
        'thead{display:table-header-group}'
        'tr{page-break-inside:avoid}'
        'th{background:#f3f4f6;color:#6b7280;font-size:7pt;font-weight:900;text-transform:uppercase;'
        'letter-spacing:.04em;text-align:left;padding:7px 6px}'
        'th:first-child{border-top-left-radius:4px}th:last-child{border-top-right-radius:4px}'
        'td{padding:6px;border-bottom:1px solid #f3f4f6;vertical-align:middle}'
        '.pob{font-weight:700;color:#1f2937}'
        '.ico{font-family:"Roboto Mono",Consolas,"DejaVu Sans Mono",monospace;font-size:8.5pt}'
        '.jm{font-weight:600;color:#1f2937}'
        '.rez{font-style:italic;color:#6366f1}.dopl{font-style:italic;color:#d97706}'
        '.pod{text-align:center}'
        '.box{display:inline-block;width:13px;height:13px;border:1.5px solid #6b7280;'
        'border-radius:2px;vertical-align:middle}'
        '.box.ok{background:#1976d2;border-color:#1976d2}.box svg{display:block;width:100%;height:100%}'
        '.prazdne{text-align:center;color:#9ca3af;padding:24px}'
        '</style></head><body><table><colgroup>'
        '<col style="width:13%"><col style="width:9%"><col style="width:9%"><col style="width:17%">'
        '<col style="width:19%"><col style="width:14%"><col style="width:11%"><col style="width:8%">'
        '</colgroup><thead><tr>'
        + ''.join(f'<th>{h}</th>' for h in ('Pobočka', 'OZ', 'IČO', 'Provozovna',
                                             'Jméno zákazníka', 'Funkce', 'Telefon'))
        + '<th class="pod">Podpis</th>'
        + '</tr></thead><tbody>' + ''.join(radky) + '</tbody></table></body></html>')


async def _render_pdf(html_, zapati):
    """HTML → PDF (A4 na šířku) přes Playwright/Chromium; zapati = text vlevo v patičce."""
    from html import escape
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError('Na serveru chybí Playwright (pip install playwright; '
                           f'playwright install chromium). Detail: {e}')
    async with _pdf_zamek():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception as e:
                raise RuntimeError(f'Nepodařilo se spustit Chromium ({e}). '
                                   'Spusťte na serveru: playwright install chromium')
            try:
                page = await browser.new_page()
                await page.set_content(html_, wait_until='load')
                return await page.pdf(
                    format='A4', landscape=True, print_background=True,
                    margin={'top': '10mm', 'right': '10mm', 'bottom': '13mm', 'left': '10mm'},
                    display_header_footer=True, header_template='<div></div>',
                    footer_template=(
                        '<div style="width:100%;font-size:7pt;color:#9ca3af;'
                        'font-family:Arial,sans-serif;padding:0 10mm;'
                        'display:flex;justify-content:space-between;">'
                        f'<span>{escape(zapati)}</span>'
                        '<span>Strana <span class="pageNumber"></span> / '
                        '<span class="totalPages"></span></span></div>'))
            finally:
                await browser.close()


# ==========================================
# DB VRSTVA (blokující – volat přes asyncio.to_thread)
# ==========================================
def _nacti_terminy(typ):
    """Termíny daného typu + počty přihlášených (aktivních / zrušených)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT t.*,
                  SUM(p.stav = 'aktivni') AS pocet,
                  SUM(p.stav = 'zruseno') AS pocet_zrusenych
              FROM gastrokurzy_termin t
              LEFT JOIN gastrokurzy_prihlaska p ON p.termin_id = t.id
              WHERE t.typ = %s
              GROUP BY t.id
              ORDER BY t.datum IS NULL, t.datum, t.misto, t.nazev""", (typ,))
        terminy = cur.fetchall()
        if not terminy:
            return []
        cur.execute("""SELECT termin_id, pobocka_klic, COUNT(*) AS pocet
              FROM gastrokurzy_prihlaska p
              JOIN gastrokurzy_termin t ON t.id = p.termin_id
              WHERE t.typ = %s AND p.stav = 'aktivni'
              GROUP BY termin_id, pobocka_klic
              ORDER BY pocet DESC""", (typ,))
        podle_pobocky = {}
        for r in cur.fetchall():
            podle_pobocky.setdefault(r['termin_id'], []).append((r['pobocka_klic'], r['pocet']))
        for t in terminy:
            t['pocet'] = int(t['pocet'] or 0)
            t['pocet_zrusenych'] = int(t['pocet_zrusenych'] or 0)
            t['pobocky'] = podle_pobocky.get(t['id'], [])
        return terminy
    except Exception as e:
        print(f"[gastrokurzy] Načtení termínů: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def _nacti_prihlasky(termin_id):
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT * FROM gastrokurzy_prihlaska
              WHERE termin_id = %s
              ORDER BY stav = 'zruseno', pobocka_klic, id""", (termin_id,))
        return cur.fetchall()
    except Exception as e:
        print(f"[gastrokurzy] Načtení prezence: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def _nacti_vsechny_prihlasky():
    """Kompletní soupis účastníků napříč všemi termíny (datová sada)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT p.id, p.termin_id, p.pobocka_klic, p.oz, p.ico, p.provozovna,
                      p.zakaznik, p.funkce, p.telefon, p.podpis, p.rezervace, p.stav, p.zapsal,
                      t.datum, t.misto, t.nazev, t.firma, t.lektor, t.typ
                  FROM gastrokurzy_prihlaska p
                  JOIN gastrokurzy_termin t ON t.id = p.termin_id
                  ORDER BY t.datum IS NULL, t.datum DESC, t.id, p.pobocka_klic, p.id""")
        return cur.fetchall()
    except Exception as e:
        print(f"[gastrokurzy] Načtení soupisu účastníků: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def _uloz_termin(data, termin_id=None):
    """INSERT/UPDATE termínu. Vrací id, nebo None při chybě."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        if termin_id:
            _zachyt_ohlaseny_stav(cur, termin_id)
            cur.execute("""UPDATE gastrokurzy_termin
                  SET typ=%s, misto=%s, datum=%s, nazev=%s, firma=%s, lektor=%s,
                      kapacita=%s, stav=%s, poznamka=%s, verze = verze + 1
                  WHERE id=%s""",
                        (data['typ'], data['misto'], data['datum'], data['nazev'], data.get('firma'),
                         data.get('lektor'), data.get('kapacita'), data['stav'],
                         data.get('poznamka'), termin_id))
        else:
            cur.execute("""INSERT INTO gastrokurzy_termin
                  (typ, misto, datum, nazev, firma, lektor, kapacita, stav, poznamka)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (data['typ'], data['misto'], data['datum'], data['nazev'],
                         data.get('firma'), data.get('lektor'), data.get('kapacita'),
                         data['stav'], data.get('poznamka')))
            termin_id = cur.lastrowid
        conn.commit()
        return termin_id
    except Exception as e:
        print(f"[gastrokurzy] Uložení termínu: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def _smaz_termin(termin_id):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM gastrokurzy_termin WHERE id=%s", (termin_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[gastrokurzy] Smazání termínu: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _cizi_rezervace(cur, prihlaska_id, user_name):
    """True = zápis je rezervace jiného uživatele (nebo neexistuje) – měnit ho nelze."""
    cur.execute("SELECT rezervace, zapsal FROM gastrokurzy_prihlaska WHERE id=%s",
                (prihlaska_id,))
    r = cur.fetchone()
    return r is None or (bool(r[0]) and r[1] != user_name)


def _uloz_prihlasku(data, prihlaska_id, user_name):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        if _cizi_rezervace(cur, prihlaska_id, user_name):
            return False
        cur.execute("""UPDATE gastrokurzy_prihlaska
              SET pobocka_klic=%s, oz=%s, ico=%s, provozovna=%s, zakaznik=%s,
                  funkce=%s, telefon=%s, podpis=%s
              WHERE id=%s""",
                    (data['pobocka_klic'], data['oz'], data['ico'], data['provozovna'],
                     data['zakaznik'], data['funkce'], data['telefon'],
                     int(data['podpis']), prihlaska_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"[gastrokurzy] Uložení přihlášky: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _zapis_prihlasky(zaznamy):
    """Zapíše nové přihlášky v jedné transakci – buď všechny, nebo žádnou
    (zákazník + další účastníci ze stejné provozovny)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.executemany("""INSERT INTO gastrokurzy_prihlaska
              (termin_id, pobocka_klic, oz, ico, provozovna, zakaznik,
               funkce, telefon, podpis, rezervace, zapsal)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        [(d['termin_id'], d['pobocka_klic'], d['oz'], d['ico'],
                          d['provozovna'], d['zakaznik'], d['funkce'],
                          d['telefon'], int(d['podpis']), int(not d['zakaznik']),
                          d['zapsal']) for d in zaznamy])
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[gastrokurzy] Zápis přihlášek: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _zmen_stav_prihlasky(prihlaska_id, stav, user_name, spravce=False):
    """Zrušení zápisu (stav 'zruseno') nebo jeho obnovení ('aktivni').
    Cizí rezervaci smí správce zrušit (ne obnovit ani upravit)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        if not (spravce and stav == 'zruseno') and _cizi_rezervace(cur, prihlaska_id, user_name):
            return False
        if stav == 'zruseno':
            cur.execute("""UPDATE gastrokurzy_prihlaska
                  SET stav='zruseno', zrusil=%s, zruseno_kdy=NOW() WHERE id=%s""",
                        (user_name, prihlaska_id))
        else:
            cur.execute("""UPDATE gastrokurzy_prihlaska
                  SET stav='aktivni', zrusil=NULL, zruseno_kdy=NULL WHERE id=%s""",
                        (prihlaska_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[gastrokurzy] Změna stavu přihlášky: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _prepni_podpis(prihlaska_id, hodnota, user_name):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        if _cizi_rezervace(cur, prihlaska_id, user_name):
            return False
        cur.execute("UPDATE gastrokurzy_prihlaska SET podpis=%s WHERE id=%s",
                    (int(hodnota), prihlaska_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"[gastrokurzy] Podpis: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _nazvy_kurzu():
    """Nabídka názvů kurzů pro autocomplete (co už v DB je)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor()
    try:
        cur.execute("""SELECT DISTINCT nazev FROM gastrokurzy_termin
              WHERE nazev IS NOT NULL AND nazev <> '' ORDER BY nazev""")
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        cur.close()
        conn.close()


# ==========================================
# E-MAILY – ruční rozeslání nových a upravených termínů
# ==========================================
# Údaje, které příjemce v e-mailu vidí; jejich ohlášený stav drží `souhrn_data`.
_POLE_OHLASENI = ('typ', 'misto', 'datum', 'nazev', 'firma', 'lektor', 'kapacita', 'stav', 'poznamka')


def _snimek_terminu(t):
    """Porovnatelný stav termínu (JSON-serializovatelný, prázdný text = None)."""
    s = {}
    for k in _POLE_OHLASENI:
        v = t.get(k)
        if isinstance(v, (datetime.date, datetime.datetime)):
            v = v.isoformat()[:10]
        elif isinstance(v, str):
            v = v.strip() or None
        elif k == 'kapacita' and v is not None:
            v = int(v)
        s[k] = v
    return s


def _zachyt_ohlaseny_stav(cur, termin_id):
    """Před úpravou uloží snímek termínu, je-li jeho současný stav ohlášený a snímek
    chybí (termíny z importu a z doby před zavedením `souhrn_data`) — jinak by
    e-mail neměl s čím porovnat."""
    cur.execute(f"""SELECT {', '.join(_POLE_OHLASENI)} FROM gastrokurzy_termin
          WHERE id=%s AND souhrn_data IS NULL AND souhrn_verze = verze""", (termin_id,))
    row = cur.fetchone()
    if row:
        t = row if isinstance(row, dict) else dict(zip(_POLE_OHLASENI, row))
        cur.execute("UPDATE gastrokurzy_termin SET souhrn_data=%s, updated_at=updated_at WHERE id=%s",
                    (json.dumps(_snimek_terminu(t), ensure_ascii=False), termin_id))


def _nacti_neohlasene_terminy():
    """Termíny, které ještě nebyly rozeslány, nebo se od rozeslání změnily.
    None = chyba DB. `puvodne` = ohlášený snímek (None u nového / neznámého)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT t.*, SUM(p.stav = 'aktivni') AS pocet,
                              (t.souhrn_verze IS NULL) AS je_novy
                         FROM gastrokurzy_termin t
                         LEFT JOIN gastrokurzy_prihlaska p ON p.termin_id = t.id
                        WHERE t.souhrn_verze IS NULL OR t.souhrn_verze < t.verze
                        GROUP BY t.id
                        ORDER BY je_novy DESC, t.datum IS NULL, t.datum, t.misto, t.nazev""")
        terminy = cur.fetchall()
        for t in terminy:
            t['pocet'] = int(t['pocet'] or 0)
            t['je_novy'] = bool(t['je_novy'])
            t['volno'] = max(0, int(t['kapacita']) - t['pocet']) if t['kapacita'] else None
            try:
                t['puvodne'] = json.loads(t['souhrn_data']) if t.get('souhrn_data') else None
            except ValueError:
                t['puvodne'] = None
        return terminy
    except Exception as e:
        print(f"[gastrokurzy] Načtení neohlášených termínů: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def _oznac_jako_ohlasene(terminy):
    """Uloží ohlášenou verzi a její snímek. Verze je ta načtená před odesláním —
    úprava provedená mezitím zůstane neohlášená a nabídne se příště."""
    if not terminy:
        return True
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        cur.executemany("""UPDATE gastrokurzy_termin
              SET souhrn_verze = %s, souhrn_odeslan = NOW(), souhrn_data = %s,
                  updated_at = updated_at
              WHERE id = %s""",
                        [(t['verze'], json.dumps(_snimek_terminu(t), ensure_ascii=False), t['id'])
                         for t in terminy])
        conn.commit()
        return True
    except Exception as e:
        print(f"[gastrokurzy] Označení ohlášených termínů: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _hodnota_pole(klic, v):
    """Hodnota údaje pro e-mail."""
    if klic == 'datum':
        return _fmt_datum(datetime.date.fromisoformat(v) if isinstance(v, str) else v)
    if klic == 'stav':
        return 'TERMÍN ZRUŠEN' if v == 'zruseno' else 'aktivní'
    if klic == 'typ':
        return 'TOP' if v == 'top' else 'běžný'
    if klic == 'kapacita':
        return 'neomezeno' if v is None else str(v)
    if klic == 'nazev' and v is None:
        return '(bez názvu)'
    return '—' if v is None else str(v)


def _zmenena_pole(t):
    """Klíče údajů změněných od ohlášení (prázdné, když snímek chybí)."""
    p = t.get('puvodne')
    if not p:
        return []
    s = _snimek_terminu(t)
    return [k for k in _POLE_OHLASENI if p.get(k) != s[k]]


def _blok_terminu(t):
    """Odstavec jednoho termínu v e-mailu; změněné údaje jako „► původně → nyní"."""
    s = _snimek_terminu(t)
    p = t.get('puvodne') or {}
    zmeny = set(_zmenena_pole(t))
    radky = []
    odsaz = '  ' if zmeny else ''      # ať nezměněné řádky lícují s „► …“

    def radek(popis, klic, vzdy=True):
        if klic in zmeny:
            radky.append(f"► {popis + ':':<15}{_hodnota_pole(klic, p.get(klic))} → "
                         f"{_hodnota_pole(klic, s[klic])}")
        elif vzdy:
            radky.append(f"{odsaz}{popis + ':':<15}{_hodnota_pole(klic, s[klic])}")

    radek('Kurz', 'nazev')
    radek('Typ', 'typ', vzdy=False)
    radek('Firma', 'firma', vzdy=s['typ'] == 'top' and bool(s['firma']))
    radek('Místo konání', 'misto')
    radek('Datum', 'datum')
    radek('Lektor', 'lektor', vzdy=bool(s['lektor']))
    radek('Kapacita', 'kapacita', vzdy=False)
    radky.append(f"{odsaz}{'Volná místa:':<15}"
                 + ('neomezeno' if t['volno'] is None else f"{t['volno']} z {t['kapacita']}"))
    radek('Stav', 'stav', vzdy=s['stav'] == 'zruseno')
    radek('Poznámka', 'poznamka', vzdy=bool(s['poznamka']))
    return '\n'.join(radky)


def _text_souhrnu(terminy):
    """Tělo e-mailu o změnách; None = není co poslat."""
    nove = [t for t in terminy if t['je_novy']]
    zmenene = [t for t in terminy if not t['je_novy']]
    if not nove and not zmenene:
        return None

    casti = ["Dobrý den,\n"]
    if nove:
        casti.append(f"v Gastrokurzech přibyly nové termíny ({len(nove)}):\n")
        casti.append('\n\n'.join(_blok_terminu(t) for t in nove))
    if zmenene:
        casti.append(f"{chr(10) + 'Upravené' if nove else 'v Gastrokurzech byly upraveny'} termíny "
                     f"({len(zmenene)}) — změny jsou označené ►, ve tvaru původně → nyní:\n")
        casti.append('\n\n'.join(_blok_terminu(t) for t in zmenene))
    casti.append("\nPřihlášky na kurzy zapisujte v modulu Gastrokurzy.")
    if intranet_data.APP_URL:
        casti.append(f"\nOtevřít v portálu: {intranet_data.APP_URL}/gastrokurzy")
    return '\n'.join(casti)


def _predmet_souhrnu(terminy):
    nove = sum(1 for t in terminy if t['je_novy'])
    upravene = len(terminy) - nove
    casti = ([f"nové termíny ({nove})"] if nove else []) \
        + ([f"upravené termíny ({upravene})"] if upravene else [])
    return "Gastrokurzy – " + ", ".join(casti)


def _odesli_souhrn(terminy, user_name):
    """Rozešle e-mail o změnách zapisovatelům (spouští správce tlačítkem). Blokující.
    `terminy` = přesně ty, které správce viděl v náhledu.

    Termíny se označí za ohlášené jen při skutečném odeslání — když e-maily
    selžou, zůstanou v nabídce k dalšímu odeslání. Vrací (odeslano, pocet_prijemcu)."""
    text = _text_souhrnu(terminy)
    if not text:
        return 0, 0
    prijemci = intranet_data.ziskej_emaily_s_pravem('gastrokurzy_zapisovatel')
    if not prijemci:
        return 0, 0
    predmet = _predmet_souhrnu(terminy)
    odeslano = 0
    for p in prijemci:
        try:
            if intranet_emaily.odesli_upozorneni_email(p, predmet, text):
                odeslano += 1
        except Exception as e:
            print(f"[gastrokurzy] e-mail {p}: {e}")
    if odeslano:
        _oznac_jako_ohlasene(terminy)
    intranet_logger.log_activity(
        user_name, LOG_KATEGORIE,
        f"E-mail o změnách: {odeslano}/{len(prijemci)} příjemců, {len(terminy)} termínů")
    return odeslano, len(prijemci)


# ==========================================
# UI – HLAVNÍ VSTUP
# ==========================================
def vykresli(user_id, user_name, vsechna_prava):
    """Vstupní bod modulu (volá intranet.py)."""
    inicializace_db()

    ma_vse    = 'vse' in vsechna_prava
    je_spravce = ma_vse or 'gastrokurzy_spravce' in vsechna_prava
    muze_zapisovat = je_spravce or 'gastrokurzy_zapisovatel' in vsechna_prava
    je_ctenar = muze_zapisovat or 'gastrokurzy_ctenar' in vsechna_prava

    if not je_ctenar:
        with ui.column().classes('items-center py-24 gap-4'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('Nemáte přístup do modulu Gastrokurzy.').classes('text-gray-400 text-lg')
        return

    dnes_ = datetime.date.today()
    filtr = {'misto': 'vse', 'hledej': '', 'skryt_stare': True, 'pohled': 'seznam',
             'typ': 'vse', 'od': '', 'do': ''}
    kal = {'rok': dnes_.year, 'mesic': dnes_.month}
    posledni_pobocka_key = f'gastrokurzy_pob_{user_id}'
    skryt_zrusene_key = f'gastrokurzy_skryt_zrusene_{user_id}'

    # =================================================================
    # DIALOG: PREZENČNÍ LISTINA
    # =================================================================
    async def _otevri_prezencku(t):
        prihlasky = await asyncio.to_thread(_nacti_prihlasky, t['id'])
        ref = {'data': prihlasky}
        zruseny_termin = t['stav'] == 'zruseno'
        editovat = muze_zapisovat and not zruseny_termin

        async def _export_prezencky():
            # exportuje to, co je vidět – respektuje „Skrýt zrušené"
            skryt = bool(app.storage.user.get(skryt_zrusene_key, False))
            data = [p for p in ref['data'] if not skryt or p['stav'] == 'aktivni']
            info = [t['misto'], _fmt_datum(t['datum']),
                    f"lektor {t['lektor']}" if t['lektor'] else 'lektor neurčen']
            if t.get('firma'):
                info.append(f"firemní kurz {t['firma']}, kapacita {t.get('kapacita') or '—'} míst")
            if zruseny_termin:
                info.append('TERMÍN ZRUŠEN')
            obsah = await asyncio.to_thread(
                _export_xlsx, [t], data, f"Prezenční listina – {t['nazev'] or 'kurz'}",
                ' · '.join(info))
            ui.download.content(obsah, _nazev_souboru('prezencka', t['datum'], t['misto'],
                                                      t['nazev']), XLSX_MIME)
            intranet_logger.log_activity(
                user_name, LOG_KATEGORIE,
                f"Export účastníků kurzu {t['nazev']} ({t['misto']} {_fmt_datum(t['datum'])})")

        async def _export_pdf():
            # v PDF je vždy jen tabulka zapsaných (aktivní zápisy) – k tisku a podpisům
            data = [p for p in ref['data'] if p['stav'] == 'aktivni']
            popis = f"{t['nazev'] or 'Kurz'} – {t['misto']} {_fmt_datum(t['datum'])}"
            notif = ui.notification('Připravuji PDF…', type='ongoing', spinner=True, timeout=None)
            try:
                obsah = await _render_pdf(_prezencka_html(data, f'Prezenční listina – {popis}'),
                                          f'Prezenční listina – {popis}')
            except Exception as e:
                notif.dismiss()
                ui.notify(f'Nepodařilo se vytvořit PDF: {e}', type='negative', multi_line=True)
                return
            notif.dismiss()
            ui.download.content(obsah, _nazev_souboru('prezencka', t['datum'], t['misto'],
                                                      t['nazev'], pripona='pdf'), PDF_MIME)
            intranet_logger.log_activity(
                user_name, LOG_KATEGORIE,
                f"Export účastníků kurzu do PDF {t['nazev']} ({t['misto']} {_fmt_datum(t['datum'])})")

        # Dialog visí mimo _panel — _obnov() volá _panel.refresh(), který by ho
        # jinak smazal i s otevřenou prezenčkou (zrušení/zápis ji zavíral).
        with ui.context.client.layout, ui.dialog() as dlg, ui.card().classes('p-0 gap-0').style(
                'min-width: 1100px; max-width: 1400px; max-height: 90vh; overflow-y: auto'):

            # --- hlavička: údaje se doplňují automaticky z termínu ---
            # stejná barva jako dlaždice: TOP = barva firmy, JIP = barva místa konání
            od, do = (FIRMY_STYL.get((t.get('firma') or '').upper(), _FIRMA_DEFAULT)
                      if t['typ'] == 'top' else _styl_mista(t['misto']))
            with ui.column().classes('w-full p-6 gap-1 text-white').style(
                    f'background: linear-gradient(135deg, {od} 0%, {do} 100%)'):
                with ui.row().classes('w-full items-center gap-3'):
                    ui.icon('assignment_ind', size='2rem')
                    ui.label('Prezenční listina').classes('text-2xl font-extrabold')
                    ui.element('div').classes('flex-1')
                    ui.button('Export do Excelu', icon='download', on_click=_export_prezencky).props(
                        'flat no-caps color=white').classes('font-bold')
                    ui.button('Export do PDF', icon='picture_as_pdf', on_click=_export_pdf).props(
                        'flat no-caps color=white').classes('font-bold')
                    ui.button(icon='close', on_click=dlg.close).props('flat round color=white')
                with ui.row().classes('items-center gap-6 mt-1'):
                    for ikona, text in (('place', t['misto']),
                                        ('event', _fmt_datum(t['datum'])),
                                        ('restaurant_menu', t['nazev'] or '—'),
                                        ('person', t['lektor'] or 'lektor neurčen')):
                        with ui.row().classes('items-center gap-1'):
                            ui.icon(ikona, size='1.1rem').classes('opacity-80')
                            ui.label(str(text)).classes('text-sm font-bold')
                if t.get('firma'):
                    ui.label(f"Firemní kurz {t['firma']} · kapacita {t.get('kapacita') or '—'} míst").classes(
                        'text-xs font-bold opacity-90')
                if zruseny_termin:
                    ui.label('TERMÍN JE ZRUŠEN – zápis není možný.').classes(
                        'text-xs font-black bg-white/20 px-2 py-0.5 rounded mt-1 w-fit')

            # --- formulář nového zápisu ---
            if editovat:
                with ui.column().classes('w-full px-6 py-4 gap-2 bg-gray-50 border-b'):
                    ui.label('Přidat pozvaného zákazníka').classes(
                        'text-xs font-black text-gray-500 uppercase tracking-wide')
                    # každý řádek = jeden zápis; „+1“ přidá kopii posledního řádku
                    # (jméno a podpis zůstanou prázdné – jde o jinou osobu)
                    radky = []
                    odebrat_radek = {}  # id(r) -> funkce, která řádek odebere
                    tlacitko_pocet = []  # tlačítko „+N“ (vzniká až pod řádky)
                    # rezervace navíc přidané Shift + kolečkem – bez vlastního řádku,
                    # vzniknou až při Zapsat jako kopie posledního řádku bez jména
                    navic = [0]
                    radky_box = ui.column().classes('w-full gap-2')

                    def _obnov_pocet():
                        # číslo v tlačítku = počet zapisovaných osob (řádky + rezervace navíc)
                        if tlacitko_pocet:
                            tlacitko_pocet[0].text = f'+{len(radky) + navic[0]}'

                    def _novy_radek(vzor=None):
                        v = vzor or {}
                        with radky_box, ui.row().classes('w-full items-center gap-2') as radek:
                            r = {
                                'pobocka_klic': ui.select(
                                    {k: n for k, n in POBOCKY}, label='Pobočka *',
                                    value=v.get('pobocka_klic') or app.storage.user.get(posledni_pobocka_key, 'praha')
                                ).props('outlined dense').classes('w-40'),
                                'oz': ui.input('OZ (kdo zve) *', value=v.get('oz', '')).props(
                                    'outlined dense').classes('w-40'),
                                'ico': ui.input('IČO zákazníka *', value=v.get('ico', '')).props(
                                    'outlined dense').classes('w-32'),
                                'provozovna': ui.input('Provozovna', value=v.get('provozovna', '')).props(
                                    'outlined dense').classes('flex-1 min-w-40'),
                                'zakaznik': ui.input('Jméno zákazníka').props(
                                    'outlined dense' + (' autofocus' if vzor else '')).classes('flex-1 min-w-48'),
                                'funkce': ui.input('Funkce *', value=v.get('funkce', '')).props(
                                    'outlined dense').classes('w-40'),
                                'telefon': ui.input('Telefon', value=v.get('telefon', '')).props(
                                    'outlined dense').classes('w-36'),
                                'podpis': ui.checkbox('Podpis'),
                            }
                            radky.append(r)

                            def _odeber():
                                radky.remove(r)
                                odebrat_radek.pop(id(r), None)
                                radek.delete()
                                _obnov_pocet()
                            odebrat_radek[id(r)] = _odeber
                            _obnov_pocet()
                            # první řádek nejde odebrat; neviditelné tlačítko drží zarovnání sloupců
                            ui.button(icon='close', on_click=_odeber).props(
                                'flat round dense size=sm color=grey').tooltip('Odebrat řádek').classes(
                                '' if vzor else 'invisible')

                    _novy_radek()

                    async def _pridej():
                        # se jménem = plný zápis; bez jména = rezervace místa (stačí pobočka,
                        # zbytek si autor doplní později – nikdo jiný ji upravit nemůže)
                        povinne = (('Pobočka', 'pobocka_klic'), ('OZ', 'oz'), ('IČO', 'ico'),
                                   ('Funkce', 'funkce'))
                        chybi = []
                        for i, r in enumerate(radky, 1):
                            jen_rezervace = not str(r['zakaznik'].value or '').strip()
                            pole = [lbl for lbl, k in (povinne[:1] if jen_rezervace else povinne)
                                    if not str(r[k].value or '').strip()]
                            if pole:
                                chybi.append((f'řádek {i} – ' if len(radky) > 1 else '') + ', '.join(pole))
                        if chybi:
                            ui.notify('Vyplňte: ' + '; '.join(chybi), type='warning')
                            return
                        zaznamy = [{'termin_id': t['id'], 'zapsal': user_name,
                                    'pobocka_klic': r['pobocka_klic'].value, 'podpis': bool(r['podpis'].value),
                                    **{k: str(r[k].value or '').strip() for k in
                                       ('oz', 'ico', 'provozovna', 'zakaznik', 'funkce', 'telefon')}}
                                   for r in radky]
                        # rezervace navíc = kopie posledního řádku bez jména a podpisu
                        zaznamy += [{**zaznamy[-1], 'zakaznik': '', 'podpis': False}
                                    for _ in range(navic[0])]
                        volno = _volna_mista(t, ref['data'])
                        if volno is not None and volno < len(zaznamy):
                            ui.notify('Kurz je plně obsazen.' if volno <= 0 else
                                      f'Volná místa: {volno}, zapisujete {len(zaznamy)}.', type='negative')
                            return
                        if not await asyncio.to_thread(_zapis_prihlasky, zaznamy):
                            ui.notify('Zápis se nepodařilo uložit.', type='negative')
                            return
                        app.storage.user[posledni_pobocka_key] = zaznamy[0]['pobocka_klic']
                        pobocky = dict.fromkeys(POBOCKY_NAZVY.get(z['pobocka_klic'], z['pobocka_klic'])
                                                for z in zaznamy)
                        jmena = [z['zakaznik'] for z in zaznamy if z['zakaznik']]
                        rezervaci = len(zaznamy) - len(jmena)
                        popis = ', '.join(jmena + ([f'rezervace {rezervaci} míst'] if rezervaci else []))
                        intranet_logger.log_activity(
                            user_name, LOG_KATEGORIE,
                            f"Zápis na kurz {t['nazev']} ({t['misto']} {_fmt_datum(t['datum'])}): "
                            f"{popis} – {', '.join(pobocky)}")
                        if rezervaci:
                            ui.notify(f'Rezervováno míst: {rezervaci}. Jména a údaje doplníte přes '
                                      f'tlačítko Upravit – nikdo jiný je měnit nemůže.', type='positive')
                        radky.clear()
                        odebrat_radek.clear()
                        radky_box.clear()
                        navic[0] = 0
                        _novy_radek()
                        await _obnov()

                    def _kopie_radku():
                        _novy_radek({k: el.value for k, el in radky[-1].items()})

                    def _kolecko(e):
                        # Shift + kolečko nahoru/dolů = přidat/ubrat rezervaci navíc – řádky
                        # nepřibývají, místa se zapíšou až po kliknutí na Zapsat
                        if e.args > 0:
                            navic[0] += 1
                        elif navic[0] > 0:
                            navic[0] -= 1
                        _obnov_pocet()

                    with ui.row().classes('w-full items-center justify-end gap-2'):
                        ui.label('Bez jména zákazníka se místo jen zarezervuje – údaje doplníte později '
                                 'a upravovat je smíte jen vy.').classes('text-xs text-gray-400 mr-auto')
                        tlacitko_pocet.append(ui.button(
                            f'+{len(radky)}', on_click=_kopie_radku).props('outline color=primary').classes(
                            'font-bold').tooltip(
                            'Počet zapisovaných osob. Klik = další osoba (zkopíruje poslední řádek, '
                            'stačí doplnit jméno). Shift + kolečko myši nahoru/dolů přidává/ubírá '
                            'rezervace bez jména – zapíšou se až po kliknutí na Zapsat.'
                        ).on('wheel', _kolecko, js_handler='''(e) => {
                            if (!e.shiftKey) return;
                            e.preventDefault();
                            // se Shiftem prohlížeč často posílá vodorovný posun (deltaX místo deltaY)
                            const d = e.deltaY || e.deltaX;
                            if (d) emit(d < 0 ? 1 : -1);
                        }'''))
                        ui.button('Zapsat', icon='person_add', on_click=_pridej).props(
                            'unelevated color=primary').classes('font-bold')

            # --- tabulka zapsaných ---
            @ui.refreshable
            def _seznam():
                data = ref['data']
                aktivni = [p for p in data if p['stav'] == 'aktivni']
                volno = _volna_mista(t, data)
                with ui.row().classes('w-full items-center gap-3 px-6 pt-4'):
                    ui.label(f'Zapsáno: {len(aktivni)}').classes(
                        'text-sm font-black text-gray-700 bg-gray-100 px-3 py-1 rounded-full')
                    if volno is not None:
                        cls = 'bg-red-100 text-red-700' if volno <= 0 else 'bg-green-100 text-green-700'
                        ui.label(f'Volná místa: {volno}').classes(
                            f'text-sm font-black px-3 py-1 rounded-full {cls}')
                    zrusenych = len(data) - len(aktivni)
                    skryt = bool(app.storage.user.get(skryt_zrusene_key, False))
                    if zrusenych:
                        ui.label(f'Zrušeno: {zrusenych}').classes(
                            'text-sm font-bold text-gray-400 bg-gray-50 px-3 py-1 rounded-full')

                        def _prepni_zrusene():
                            app.storage.user[skryt_zrusene_key] = not skryt
                            _seznam.refresh()
                        ui.button('Zobrazit zrušené' if skryt else 'Skrýt zrušené',
                                  icon='visibility' if skryt else 'visibility_off',
                                  on_click=_prepni_zrusene).props(
                            'flat dense no-caps color=grey-7').classes('ml-auto')

                if skryt:
                    data = aktivni

                if not data:
                    if zrusenych:
                        with ui.column().classes('w-full items-center py-12 gap-2'):
                            ui.icon('visibility_off', size='3rem', color='grey-4')
                            ui.label('Všechny zápisy jsou zrušené a skryté.').classes('text-gray-400')
                        return
                    with ui.column().classes('w-full items-center py-12 gap-2'):
                        ui.icon('groups', size='3rem', color='grey-4')
                        ui.label('Zatím se nikdo nezapsal.').classes('text-gray-400')
                    return

                with ui.column().classes('w-full p-6 pt-3 gap-0'):
                    hlav = [('Pobočka', 'w-32'), ('OZ', 'w-24'), ('IČO', 'w-28'),
                            ('Provozovna', 'w-52'),
                            ('Jméno zákazníka', 'flex-1 min-w-48'), ('Funkce', 'w-40'),
                            ('Telefon', 'w-32'), ('Podpis', 'w-20'), ('', 'w-24')]
                    with ui.row().classes('w-full items-center gap-2 px-2 py-2 bg-gray-100 rounded-t'):
                        for text, sirka in hlav:
                            ui.label(text).classes(
                                f'{sirka} text-[11px] font-black text-gray-500 uppercase tracking-wide')
                    for p in data:
                        _radek_prezence(p)

            def _radek_prezence(p):
                zruseno = p['stav'] == 'zruseno'
                zaklad = 'text-sm text-gray-400 line-through' if zruseno else 'text-sm text-gray-700'
                anonym = not (p['zakaznik'] or '').strip()
                rezervace = bool(p.get('rezervace'))
                # cizí rezervaci smí měnit jen ten, kdo ji zapsal (i když už má jméno)
                cizi = rezervace and p['zapsal'] != user_name
                muze = editovat and not cizi
                with ui.row().classes(
                        'w-full items-center gap-2 px-2 py-1.5 border-b border-gray-100 '
                        + ('bg-gray-50' if zruseno else 'hover:bg-blue-50')):
                    ui.label(POBOCKY_NAZVY.get(p['pobocka_klic'], p['pobocka_klic'])).classes(
                        f'w-32 {zaklad} font-bold')
                    ui.label(p['oz'] or '—').classes(f'w-24 {zaklad}')
                    ui.label(p['ico'] or '—').classes(f'w-28 {zaklad} font-mono')
                    ui.label(p['provozovna'] or '—').classes(f'w-52 {zaklad} truncate').tooltip(
                        p['provozovna'] or '')
                    if anonym and rezervace:
                        ui.label('Rezervováno').classes('flex-1 min-w-48 text-sm italic text-indigo-500')
                    elif anonym:
                        with ui.row().classes('flex-1 min-w-48 items-center gap-1'):
                            ui.label('K doplnění').classes('text-sm italic text-amber-600')
                            ui.icon('info', size='0.9rem', color='amber').tooltip(
                                'Místo z původní tabulky – jméno zákazníka zatím nebylo zapsáno.')
                    else:
                        ui.label(p['zakaznik']).classes(f'flex-1 min-w-48 {zaklad} font-semibold')
                    ui.label(p['funkce'] or '—').classes(f'w-40 {zaklad}')
                    ui.label(p['telefon'] or '—').classes(f'w-32 {zaklad}')

                    async def _podpis(e, pid=p['id']):
                        if not await asyncio.to_thread(_prepni_podpis, pid, e.value, user_name):
                            ui.notify('Podpis se nepodařilo uložit.', type='negative')

                    with ui.element('div').classes('w-20'):
                        if zruseno:
                            ui.icon('block', color='grey-5')
                        else:
                            ui.checkbox(value=bool(p['podpis']), on_change=_podpis).props(
                                f'dense {"" if muze else "disable"}').tooltip(
                                'Prezence – lze zaškrtnout i v den kurzu nebo později.')

                    with ui.row().classes('w-24 items-center gap-1 justify-end'):
                        if zruseno:
                            kdy = p['zruseno_kdy'].strftime('%d.%m.%Y') if p['zruseno_kdy'] else ''
                            ui.icon('history', size='1rem', color='grey-5').tooltip(
                                f"Zrušil: {p['zrusil'] or '?'} {kdy}")
                            if muze:
                                ui.button(icon='undo', on_click=lambda _, pid=p['id']: _zmen(pid, 'aktivni')
                                          ).props('flat dense round size=sm color=grey').tooltip('Obnovit zápis')
                        elif muze:
                            ui.button(icon='edit', on_click=lambda _, pp=p: _otevri_editaci(pp)
                                      ).props('flat dense round size=sm color=grey').tooltip(
                                'Doplnit údaje' if anonym and rezervace else 'Upravit')
                            ui.button(icon='cancel', on_click=lambda _, pid=p['id']: _zmen(pid, 'zruseno')
                                      ).props('flat dense round size=sm color=red').tooltip('Zrušit zápis')
                        elif editovat:  # cizí rezervace
                            ui.icon('lock', size='1rem', color='grey-5').tooltip(
                                f"Rezervace – upravovat smí jen {p['zapsal']}.")
                            if je_spravce:
                                ui.button(icon='cancel', on_click=lambda _, pid=p['id']: _zmen(pid, 'zruseno')
                                          ).props('flat dense round size=sm color=red').tooltip(
                                    f"Zrušit rezervaci ({p['zapsal']}) – jako správce")

            async def _zmen(pid, stav):
                if not await asyncio.to_thread(_zmen_stav_prihlasky, pid, stav, user_name, je_spravce):
                    ui.notify('Změnu se nepodařilo uložit.', type='negative')
                    return
                intranet_logger.log_activity(
                    user_name, LOG_KATEGORIE,
                    f"{'Zrušen' if stav == 'zruseno' else 'Obnoven'} zápis na kurz "
                    f"{t['nazev']} ({t['misto']} {_fmt_datum(t['datum'])})")
                ui.notify('Zápis zrušen.' if stav == 'zruseno' else 'Zápis obnoven.', type='positive')
                await _obnov()

            def _otevri_editaci(p):
                with ui.dialog() as edlg, ui.card().classes('p-6 gap-3').style('min-width: 520px'):
                    ui.label('Úprava zápisu').classes('text-lg font-extrabold text-gray-800')
                    e_pob = ui.select({k: n for k, n in POBOCKY}, label='Pobočka',
                                      value=p['pobocka_klic']).props('outlined dense').classes('w-full')
                    e_oz = ui.input('OZ (kdo zve)', value=p['oz'] or '').props('outlined dense').classes('w-full')
                    e_ico = ui.input('IČO zákazníka', value=p['ico'] or '').props('outlined dense').classes('w-full')
                    e_prov = ui.input('Provozovna', value=p['provozovna'] or '').props('outlined dense').classes('w-full')
                    e_jm = ui.input('Jméno zákazníka', value=p['zakaznik'] or '').props('outlined dense').classes('w-full')
                    e_fce = ui.input('Funkce', value=p['funkce'] or '').props('outlined dense').classes('w-full')
                    e_tel = ui.input('Telefon', value=p['telefon'] or '').props('outlined dense').classes('w-full')
                    e_pod = ui.checkbox('Podpis', value=bool(p['podpis']))

                    async def _uloz():
                        ok = await asyncio.to_thread(_uloz_prihlasku, {
                            'pobocka_klic': e_pob.value, 'oz': str(e_oz.value).strip(),
                            'ico': str(e_ico.value).strip(), 'provozovna': str(e_prov.value).strip(),
                            'zakaznik': str(e_jm.value).strip(),
                            'funkce': str(e_fce.value).strip(), 'telefon': str(e_tel.value).strip(),
                            'podpis': e_pod.value}, p['id'], user_name)
                        if not ok:
                            ui.notify('Uložení se nepodařilo.', type='negative')
                            return
                        intranet_logger.log_activity(
                            user_name, LOG_KATEGORIE,
                            f"Úprava zápisu na kurz {t['nazev']} ({t['misto']} {_fmt_datum(t['datum'])})")
                        edlg.close()
                        await _obnov()

                    with ui.row().classes('w-full justify-end gap-2 pt-1'):
                        ui.button('Zavřít', on_click=edlg.close).props('flat')
                        ui.button('Uložit', icon='save', on_click=_uloz).props('unelevated color=primary')
                edlg.open()

            async def _obnov():
                ref['data'] = await asyncio.to_thread(_nacti_prihlasky, t['id'])
                _seznam.refresh()
                _panel.refresh()

            _seznam()
        dlg.on('hide', dlg.delete)
        dlg.open()

    # =================================================================
    # DIALOG: TERMÍN KURZU (jen správce)
    # =================================================================
    async def _otevri_email_dialog():
        """Náhled e-mailu o nových/upravených termínech a jeho ruční rozeslání."""
        terminy = await asyncio.to_thread(_nacti_neohlasene_terminy)
        if terminy is None:
            ui.notify('Termíny se nepodařilo načíst.', type='negative')
            return
        if not terminy:
            ui.notify('Žádné nové ani upravené termíny k odeslání.')
            _panel.refresh()
            return
        prijemci = await asyncio.to_thread(intranet_data.ziskej_emaily_s_pravem,
                                           'gastrokurzy_zapisovatel') or []
        with ui.dialog() as dlg, ui.card().classes('p-6 gap-3').style('min-width: 640px; max-width: 900px'):
            ui.label('Odeslat změny e-mailem').classes('text-lg font-extrabold text-gray-800')
            ui.label(f'Příjemci: {len(prijemci)} (všichni s právem zapisovat do Gastrokurzů)'
                     if prijemci else 'Nikdo nemá právo zapisovat do Gastrokurzů – není komu poslat.'
                     ).classes('text-xs ' + ('text-gray-500' if prijemci else 'text-red-600 font-bold'))
            ui.label(f'Předmět: {_predmet_souhrnu(terminy)}').classes('text-sm font-bold text-gray-700')
            ui.label(_text_souhrnu(terminy)) \
                .classes('text-xs font-mono whitespace-pre-wrap bg-gray-50 border rounded p-3 w-full') \
                .style('max-height: 55vh; overflow-y: auto')

            async def _odeslat():
                tl_odeslat.disable()
                odeslano, celkem = await asyncio.to_thread(_odesli_souhrn, terminy, user_name)
                if odeslano:
                    ui.notify(f'E-mail odeslán {odeslano} z {celkem} příjemců.', type='positive')
                    dlg.close()
                    _panel.refresh()
                else:
                    ui.notify('E-mail se nepodařilo odeslat (SMTP?) – změny zůstávají k odeslání.',
                              type='negative')
                    tl_odeslat.enable()

            async def _jen_oznacit():
                if await asyncio.to_thread(_oznac_jako_ohlasene, terminy):
                    intranet_logger.log_activity(
                        user_name, LOG_KATEGORIE,
                        f"Změny označeny jako ohlášené bez e-mailu: {len(terminy)} termínů")
                    ui.notify('Označeno – tyto změny se už nebudou nabízet k odeslání.')
                    dlg.close()
                    _panel.refresh()
                else:
                    ui.notify('Označení se nezdařilo.', type='negative')

            with ui.row().classes('w-full items-center gap-2 pt-2'):
                ui.button('Neposílat, jen označit', on_click=_jen_oznacit) \
                    .props('flat no-caps color=grey-7') \
                    .tooltip('Např. po opravě překlepu – změny se už nebudou nabízet k odeslání')
                ui.element('div').classes('flex-1')
                ui.button('Zavřít', on_click=dlg.close).props('flat')
                tl_odeslat = ui.button('Odeslat', icon='send', on_click=_odeslat) \
                    .props('no-caps color=primary').classes('font-bold')
                if not prijemci:
                    tl_odeslat.disable()
        dlg.open()

    def _otevri_import_dialog():
        """Nalití kurzů i účastníků z Excelu (prezenční listina v řádcích)."""
        with ui.dialog() as dlg, ui.card().classes('p-6 gap-3').style('min-width: 560px'):
            ui.label('Import kurzů z Excelu').classes('text-lg font-extrabold text-gray-800')
            ui.label('Hlavička: místo, datum, název kurzu, pobočka, OZ, IČO, provozovna, '
                     'jméno, funkce, telefon, podpis. Termín se páruje podle místa, data a '
                     'názvu – existující se jen doplní o účastníky, stejný člověk se nezdvojí.'
                     ).classes('text-xs text-gray-500')
            stav = ui.label('').classes('text-sm font-bold text-gray-700')

            async def _nahraj(e):
                stav.set_text('Importuji…')
                raw = await e.file.read()          # NiceGUI 3.x: FileUpload.read() je async
                vysl = await asyncio.to_thread(_import_excel, raw)
                if vysl.get('chyba'):
                    stav.set_text(vysl['chyba'])
                    ui.notify(vysl['chyba'], type='negative')
                    return
                zprava = (f"nové termíny: {vysl['nove']}, doplněné: {vysl['doplnene']}, "
                          f"účastníků: {vysl['osob']}, duplicit přeskočeno: {vysl['duplicit']}"
                          + (f", vadných řádků: {vysl['spatnych']}" if vysl['spatnych'] else ''))
                stav.set_text(f'Hotovo – {zprava}')
                intranet_logger.log_activity(user_name, LOG_KATEGORIE,
                                             f"Import z Excelu – {zprava}")
                ui.notify('Import hotov.', type='positive')
                _panel.refresh()

            ui.upload(label='Vybrat soubor (.xlsx)', on_upload=_nahraj, auto_upload=True,
                      max_file_size=20_000_000).props('accept=".xlsx" flat dense') \
                .classes('w-full')

            # --- Návrh kurzů: náhled → potvrzení ---
            ui.separator().classes('my-2')
            ui.label('Import Návrhu kurzů').classes('text-base font-extrabold text-gray-800')
            ui.label('Soubor KURZY_…_termíny_a_přehled_kurzů: datum ve sloupci, název kurzu '
                     'v 1. řádku, pod ním Pobočka + poč. Každé místo se zapíše jako „K doplnění" '
                     '(jméno pak doplní OZ). Import jen dorovná počty podle Excelu, nic nemaže – '
                     'jde ho pustit znovu po každé úpravě tabulky.').classes('text-xs text-gray-500')
            navrh = {'raw': None}

            @ui.refreshable
            def _nahled(vysl=None, hotovo=False):
                if not vysl:
                    return
                if vysl.get('chyba'):
                    ui.label(vysl['chyba']).classes('text-sm font-bold text-red-600')
                    return
                ui.label(('Zapsáno' if hotovo else 'Náhled') +
                         f" – termínů: {vysl['terminu']}, nových: {vysl['nove']}, "
                         f"doplněných: {vysl['doplnene']}, míst k doplnění: +{vysl['mist']}"
                         ).classes('text-sm font-bold ' + ('text-green-700' if hotovo else 'text-gray-700'))
                with ui.scroll_area().classes('w-full h-56 border rounded'):
                    for r in vysl['radky']:
                        ui.label(r).classes('text-xs text-gray-700')
                    for r in vysl['poznamky']:
                        ui.label(f'⚠ {r}').classes('text-xs text-amber-700')
                    if not vysl['radky'] and not vysl['poznamky']:
                        ui.label('Intranet už odpovídá Excelu – není co doplnit.') \
                            .classes('text-xs text-gray-500')
                if not hotovo and (vysl['radky'] or vysl['nove']):
                    ui.button('Zapsat do intranetu', icon='save', on_click=_zapis_navrh) \
                        .props('unelevated color=primary')

            async def _nahraj_navrh(e):
                navrh['raw'] = await e.file.read()
                _nahled.refresh(await asyncio.to_thread(_import_navrh, navrh['raw'], False))

            async def _zapis_navrh():
                if navrh.get('bezi'):       # dvojklik → dva souběžné zápisy = zdvojená místa
                    return
                navrh['bezi'] = True
                try:
                    vysl = await asyncio.to_thread(_import_navrh, navrh['raw'], True)
                finally:
                    navrh['bezi'] = False
                _nahled.refresh(vysl, not vysl.get('chyba'))
                if vysl.get('chyba'):
                    ui.notify(vysl['chyba'], type='negative')
                    return
                intranet_logger.log_activity(
                    user_name, LOG_KATEGORIE,
                    f"Import Návrhu kurzů – nové termíny: {vysl['nove']}, doplněné: "
                    f"{vysl['doplnene']}, míst k doplnění: {vysl['mist']}")
                ui.notify('Návrh kurzů naimportován.', type='positive')
                _panel.refresh()

            ui.upload(label='Import Návrhu kurzů (.xlsx)', on_upload=_nahraj_navrh,
                      auto_upload=True, max_file_size=20_000_000) \
                .props('accept=".xlsx" flat dense').classes('w-full')
            _nahled()
            with ui.row().classes('w-full justify-end pt-2'):
                ui.button('Zavřít', on_click=dlg.close).props('flat')
        dlg.open()

    def _otevri_termin_dialog(t=None, typ='standard'):
        novy = t is None
        t = t or {}
        with ui.dialog() as dlg, ui.card().classes('p-6 gap-3').style('min-width: 560px'):
            ui.label('Nový kurz' if novy else 'Úprava termínu').classes(
                'text-lg font-extrabold text-gray-800')
            i_typ = ui.toggle({'standard': 'Kurz JIP', 'top': 'Kurz dodavatel'}, value=typ
                              ).props('no-caps dense unelevated color=deep-purple')
            with ui.row().classes('w-full gap-2'):
                i_misto = ui.select(MISTA, label='Místo konání *',
                                    value=t.get('misto', 'Praha')).props('outlined dense').classes('flex-1')
                i_datum = ui.input('Datum (DD.MM.RRRR) *',
                                   value=_fmt_datum(t.get('datum')) if t.get('datum') else ''
                                   ).props('outlined dense').classes('flex-1')
                with i_datum:
                    with ui.menu().props('no-parent-event') as menu:
                        ui.date(mask='DD.MM.YYYY').bind_value(i_datum)
                        ui.button('Zavřít', on_click=menu.close).props('flat dense')
                    with i_datum.add_slot('append'):
                        ui.icon('event').on('click', menu.open).classes('cursor-pointer')
            i_nazev = ui.input('Název kurzu *', value=t.get('nazev') or '',
                               autocomplete=_nazvy_kurzu()).props('outlined dense').classes('w-full')
            with ui.row().classes('w-full gap-2'):
                i_lektor = ui.input('Lektor', value=t.get('lektor') or '').props(
                    'outlined dense').classes('flex-1')
                i_kapacita = ui.number('Kapacita (prázdné = neomezená)', value=t.get('kapacita'),
                                       min=0, format='%.0f').props('outlined dense').classes('flex-1')
            i_firma = ui.input('Firma (ORKLA / UNILEVER / NESTLE) *',
                               value=t.get('firma') or '').props('outlined dense').classes('w-full')
            i_firma.bind_visibility_from(i_typ, 'value', lambda v: v == 'top')
            i_stav = ui.select({'aktivni': 'Aktivní', 'zruseno': 'Zrušený termín'}, label='Stav',
                               value=t.get('stav', 'aktivni')).props('outlined dense').classes('w-full')
            i_pozn = ui.textarea('Poznámka', value=t.get('poznamka') or '').props(
                'outlined dense autogrow').classes('w-full')

            async def _uloz():
                datum = _parse_datum(i_datum.value)
                if not str(i_nazev.value or '').strip():
                    ui.notify('Vyplňte název kurzu.', type='warning')
                    return
                if not datum and i_stav.value != 'zruseno':
                    ui.notify('Vyplňte datum ve tvaru DD.MM.RRRR.', type='warning')
                    return
                data = {'typ': i_typ.value, 'misto': i_misto.value, 'datum': datum,
                        'nazev': str(i_nazev.value).strip(),
                        'firma': (str(i_firma.value or '').strip().upper() or None
                                  if i_typ.value == 'top' else None),
                        'lektor': str(i_lektor.value or '').strip() or None,
                        'kapacita': int(i_kapacita.value) if i_kapacita.value else None,
                        'stav': i_stav.value,
                        'poznamka': str(i_pozn.value or '').strip() or None}
                tid = await asyncio.to_thread(_uloz_termin, data, t.get('id'))
                if not tid:
                    ui.notify('Uložení se nepodařilo.', type='negative')
                    return
                intranet_logger.log_activity(
                    user_name, LOG_KATEGORIE,
                    f"{'Založen' if novy else 'Upraven'} termín {data['nazev']} "
                    f"({data['misto']} {_fmt_datum(datum)})")
                dlg.close()
                ui.notify('Termín uložen.', type='positive')
                _panel.refresh()

            async def _smaz():
                if not await asyncio.to_thread(_smaz_termin, t['id']):
                    ui.notify('Smazání se nepodařilo.', type='negative')
                    return
                intranet_logger.log_activity(
                    user_name, LOG_KATEGORIE,
                    f"Smazán termín {t.get('nazev')} ({t.get('misto')} {_fmt_datum(t.get('datum'))})")
                dlg.close()
                ui.notify('Termín smazán.', type='positive')
                _panel.refresh()

            with ui.row().classes('w-full justify-end gap-2 pt-2'):
                if not novy:
                    ui.button('Smazat', icon='delete', on_click=_smaz).props('flat color=red').classes('mr-auto')
                ui.button('Zavřít', on_click=dlg.close).props('flat')
                ui.button('Uložit', icon='save', on_click=_uloz).props('unelevated color=primary')
        dlg.open()

    # =================================================================
    # KARTY TERMÍNŮ
    # =================================================================
    def _chipy_pobocek(t):
        """Pobočky s počty lidí – to, co v Excelu stálo ve sloupci kurzu."""
        if not t['pobocky']:
            ui.label('zatím nikdo').classes('text-xs italic text-gray-400')
            return
        for klic, pocet in t['pobocky']:
            with ui.row().classes(
                    'items-center gap-1 bg-white border border-gray-200 rounded-full px-2 py-0.5'):
                ui.label(POBOCKY_NAZVY.get(klic, klic)).classes('text-[11px] font-bold text-gray-600')
                ui.label(str(pocet)).classes(
                    'text-[11px] font-black text-white bg-rose-500 rounded-full px-1.5')

    def _karta_velka(t):
        """Nadcházející termín – velká dlaždice (styl TOP kurzů)."""
        zruseno = t['stav'] == 'zruseno'
        je_top = t['typ'] == 'top'
        od, do = (FIRMY_STYL.get((t['firma'] or '').upper(), _FIRMA_DEFAULT)
                  if je_top else _styl_mista(t['misto']))
        volno = _volna_mista(t, None)
        obsazeno_pct = min(100, round(t['pocet'] / t['kapacita'] * 100)) if t['kapacita'] else 0
        dnu = (t['datum'] - datetime.date.today()).days
        odpocet = 'DNES' if dnu == 0 else ('ZÍTRA' if dnu == 1 else f'ZA {dnu} DNÍ')
        karta = ui.card().classes(
            'p-0 gap-0 overflow-hidden cursor-pointer transition-all '
            'hover:-translate-y-1 hover:shadow-2xl'
            + (' opacity-60' if zruseno else '')).style('width: 340px; border-radius: 18px')
        with karta:
            with ui.column().classes('w-full p-4 gap-1 text-white').style(
                    f'background: linear-gradient(135deg, {od} 0%, {do} 100%)'):
                with ui.row().classes('w-full items-center gap-2 no-wrap'):
                    ui.icon('workspace_premium' if je_top else 'restaurant_menu',
                            size='1.4rem').classes('opacity-90')
                    ui.label(t['firma'] or 'KURZ JIP').classes(
                        'text-lg font-black tracking-wide truncate')
                    ui.element('div').classes('flex-1')
                    ui.label(odpocet).classes(
                        'text-[10px] font-black bg-white/25 px-2 py-0.5 rounded-full whitespace-nowrap')
                ui.label(t['nazev'] or 'Volný termín').classes(
                    'text-sm font-bold opacity-95 leading-snug')
                with ui.row().classes('items-center gap-3 pt-1'):
                    with ui.row().classes('items-center gap-1'):
                        ui.icon('event', size='0.9rem').classes('opacity-80')
                        ui.label(_fmt_datum(t['datum'])).classes('text-xs font-bold')
                    with ui.row().classes('items-center gap-1 bg-white rounded-full px-2 py-0.5').style(
                            f"color: {_styl_mista(t['misto'])[1]}"):
                        ui.icon('place', size='0.9rem')
                        ui.label(t['misto']).classes('text-xs font-bold')
                    if t['lektor']:
                        with ui.row().classes('items-center gap-1'):
                            ui.icon('person', size='0.9rem').classes('opacity-80')
                            ui.label(t['lektor']).classes('text-xs font-bold truncate')
                if zruseno:
                    ui.label('ZRUŠENO').classes(
                        'text-[10px] font-black bg-white/25 px-2 py-0.5 rounded-full w-fit')
            with ui.column().classes('w-full px-4 py-3 gap-2 bg-white'):
                with ui.row().classes('w-full items-end gap-2'):
                    ui.label(str(t['pocet'])).classes('text-3xl font-black text-gray-800 leading-none')
                    ui.label(f"/ {t['kapacita'] or '∞'} míst").classes(
                        'text-sm font-bold text-gray-400 pb-0.5')
                    ui.element('div').classes('flex-1')
                    if volno is not None:
                        cls = 'bg-red-100 text-red-700' if volno <= 0 else 'bg-green-100 text-green-700'
                        ui.label(f'volno {volno}').classes(
                            f'text-xs font-black px-2 py-1 rounded-full {cls}')
                if t['kapacita']:
                    with ui.element('div').classes('w-full h-2 bg-gray-100 rounded-full overflow-hidden'):
                        ui.element('div').classes('h-full rounded-full').style(
                            f'width: {obsazeno_pct}%; background: linear-gradient(90deg, {od}, {do})')
                with ui.row().classes('w-full items-center gap-1 flex-wrap'):
                    _chipy_pobocek(t)
                with ui.row().classes('w-full items-center gap-1 pt-1'):
                    ui.button('Prezenční listina', icon='assignment_ind',
                              on_click=lambda _, tt=t: _otevri_prezencku(tt)
                              ).props('flat dense no-caps').classes('text-xs font-bold flex-1')
                    if je_spravce:
                        ui.button(icon='edit',
                                  on_click=lambda _, tt=t: _otevri_termin_dialog(tt, tt['typ'])
                                  ).props('flat dense round color=grey')
        karta.on('dblclick', lambda _, tt=t: _otevri_prezencku(tt))

    def _karta_terminu(t):
        zruseno = t['stav'] == 'zruseno'
        je_top = t['typ'] == 'top'
        volno = _volna_mista(t, None)
        karta = ui.card().classes(
            'w-full p-0 gap-0 overflow-hidden transition-all cursor-pointer '
            + ('opacity-60 ' if zruseno else 'hover:shadow-lg '))
        with karta:
            with ui.row().classes('w-full items-stretch gap-0 no-wrap'):
                # datum – barva místa konání, kurz dodavatele si drží barvu firmy
                sloupec = ui.column().classes(
                    'items-center justify-center px-4 py-3 gap-0 min-w-28'
                    + (' bg-gray-300' if zruseno else ''))
                if not zruseno:
                    od, do = (FIRMY_STYL.get((t['firma'] or '').upper(), _FIRMA_DEFAULT)
                              if je_top else _styl_mista(t['misto']))
                    sloupec.style(f'background: linear-gradient(135deg, {od} 0%, {do} 100%)')
                with sloupec:
                    if t['datum']:
                        ui.label(f"{t['datum'].day:02d}.{t['datum'].month:02d}.").classes(
                            'text-xl font-black text-white leading-tight')
                        ui.label(str(t['datum'].year)).classes('text-[10px] font-bold text-white/80')
                    else:
                        ui.label('—').classes('text-xl font-black text-white')
                    ui.tooltip(f"Lektor: {t['lektor']}" if t['lektor'] else 'Lektor neurčen')

                with ui.column().classes('flex-1 px-4 py-3 gap-1 justify-center min-w-0'):
                    with ui.row().classes('items-center gap-2 no-wrap'):
                        ui.label(t['nazev'] or 'Volný termín').classes(
                            'text-base font-extrabold text-gray-800 truncate'
                            + (' line-through' if zruseno else ''))
                        if je_top:
                            od, do = FIRMY_STYL.get((t['firma'] or '').upper(), _FIRMA_DEFAULT)
                            ui.label(t['firma'] or 'DODAVATEL').classes(
                                'text-[10px] font-black text-white px-2 py-0.5 rounded-full '
                                'whitespace-nowrap').style(f'background: {od}')
                        if zruseno:
                            ui.label('ZRUŠENO').classes(
                                'text-[10px] font-black text-red-600 bg-red-100 px-2 py-0.5 rounded-full')
                    with ui.row().classes('items-center gap-2 flex-wrap'):
                        with ui.row().classes('items-center gap-1'):
                            barva_mista = _styl_mista(t['misto'])[1]
                            ui.icon('place', size='0.9rem').style(f'color: {barva_mista}')
                            ui.label(t['misto']).classes('text-xs font-bold').style(f'color: {barva_mista}')
                        if t['lektor']:
                            with ui.row().classes('items-center gap-1'):
                                ui.icon('person', size='0.9rem', color='grey-6')
                                ui.label(t['lektor']).classes('text-xs font-bold text-gray-500')
                        _chipy_pobocek(t)

                with ui.column().classes('items-end justify-center px-4 py-3 gap-1 bg-gray-50 min-w-40'):
                    with ui.row().classes('items-center gap-1'):
                        ui.icon('groups', size='1.1rem', color='grey-7')
                        ui.label(str(t['pocet'])).classes('text-xl font-black text-gray-800')
                        if t['kapacita']:
                            ui.label(f"/ {t['kapacita']}").classes('text-sm font-bold text-gray-400')
                    if volno is not None:
                        cls = 'text-red-600' if volno <= 0 else 'text-green-600'
                        ui.label(f'volno: {volno}').classes(f'text-[11px] font-black {cls}')
                    if t['pocet_zrusenych']:
                        ui.label(f"zrušeno: {t['pocet_zrusenych']}").classes(
                            'text-[10px] font-bold text-gray-400')
                    with ui.row().classes('items-center gap-1'):
                        ui.button(icon='assignment_ind',
                                  on_click=lambda _, tt=t: _otevri_prezencku(tt)
                                  ).props('flat dense round color=primary').tooltip('Prezenční listina')
                        if je_spravce:
                            ui.button(icon='edit',
                                      on_click=lambda _, tt=t: _otevri_termin_dialog(tt, tt['typ'])
                                      ).props('flat dense round color=grey').tooltip('Upravit termín')
        karta.on('dblclick', lambda _, tt=t: _otevri_prezencku(tt))

    # =================================================================
    # HLAVNÍ PANEL
    # =================================================================
    @ui.refreshable
    def _panel():
        ref = {'std': None, 'top': None, 'ucastnici': []}

        async def _nacti():
            ref['std'] = await asyncio.to_thread(_nacti_terminy, 'standard')
            ref['top'] = await asyncio.to_thread(_nacti_terminy, 'top')
            if filtr['pohled'] == 'ucastnici':      # celá datová sada jen když se zobrazuje
                ref['ucastnici'] = await asyncio.to_thread(_nacti_vsechny_prihlasky)
            _obsah.refresh()

        # =============================================================
        # KALENDÁŘNÍ NÁHLED
        # =============================================================
        def _barva_terminu(t):
            """Barva podle místa konání; TOP kurz má vlastní fialovou."""
            if t['typ'] == 'top':
                return '#7C3AED'
            return _styl_mista(t['misto'])[1]

        def _bunka_dne(datum, terminy_dne, aktualni_mesic):
            dnes = datetime.date.today()
            mimo = datum.month != aktualni_mesic
            vikend = datum.weekday() >= 5
            je_dnes = datum == dnes

            if mimo:
                with ui.card().classes(
                        'min-h-[110px] p-1.5 rounded-lg bg-gray-50 border border-gray-100 overflow-hidden'):
                    ui.label(str(datum.day)).classes('text-xs text-gray-300 font-bold')
                return

            bg = 'bg-rose-50' if je_dnes else ('bg-gray-50' if vikend else 'bg-white')
            ram = 'border-2 border-rose-400' if je_dnes else 'border border-gray-100'
            with ui.card().classes(f'min-h-[110px] p-1.5 rounded-lg {bg} {ram} '
                                   'overflow-hidden hover:shadow-md transition-shadow'):
                with ui.row().classes('w-full items-center gap-1 mb-1 no-wrap'):
                    cislo = ('text-xs font-black text-rose-700' if je_dnes
                             else ('text-xs text-gray-400' if vikend else 'text-xs font-bold text-gray-600'))
                    ui.label(str(datum.day)).classes(cislo)
                    ui.element('div').classes('flex-1')
                    if terminy_dne:
                        ui.label(str(sum(t['pocet'] for t in terminy_dne))).classes(
                            'text-[9px] font-black text-white bg-gray-400 rounded-full px-1.5'
                        ).tooltip('Přihlášených celkem')

                for t in terminy_dne[:3]:
                    barva = _barva_terminu(t)
                    zruseno = t['stav'] == 'zruseno'
                    volno = _volna_mista(t, None)
                    prvek = ui.element('div').classes(
                        'w-full px-1 py-0.5 rounded text-[10px] font-bold leading-tight '
                        'cursor-pointer mb-0.5 hover:opacity-80'
                    ).style(f'background:{barva}20; border-left:3px solid {barva}; color:{barva}'
                            + ('; text-decoration:line-through; opacity:.55' if zruseno else ''))
                    with prvek:
                        ui.label(t['nazev'] or 'Volný termín').classes(
                            'block whitespace-normal break-words line-clamp-2')
                        popis = [t['misto'], f"Přihlášeno: {t['pocet']}"]
                        if t.get('lektor'):
                            popis.insert(1, f"Lektor: {t['lektor']}")
                        if t.get('firma'):
                            popis.insert(0, f"TOP kurz {t['firma']}")
                        if volno is not None:
                            popis.append(f"Volná místa: {volno}")
                        if zruseno:
                            popis.append('TERMÍN ZRUŠEN')
                        ui.tooltip('\n'.join([t['nazev'] or 'Volný termín'] + popis))
                    prvek.on('click', lambda _, tt=t: _otevri_prezencku(tt))

                if len(terminy_dne) > 3:
                    ui.label(f'+{len(terminy_dne) - 3} další').classes(
                        'text-[9px] font-bold text-gray-400')

        def _kalendar(terminy):
            rok, mesic = kal['rok'], kal['mesic']
            podle_dne = {}
            for t in terminy:
                if t['datum']:
                    podle_dne.setdefault(t['datum'], []).append(t)

            prvni = datetime.date(rok, mesic, 1)
            posledni = datetime.date(rok, mesic, calendar.monthrange(rok, mesic)[1])
            zacatek = prvni - datetime.timedelta(days=prvni.weekday())
            konec = posledni + datetime.timedelta(days=6 - posledni.weekday())
            pocet_tydnu = ((konec - zacatek).days + 1) // 7
            v_mesici = [t for t in terminy if t['datum'] and t['datum'].month == mesic
                        and t['datum'].year == rok]

            def _posun(delta):
                m, r = kal['mesic'] + delta, kal['rok']
                if m < 1:
                    m, r = 12, r - 1
                elif m > 12:
                    m, r = 1, r + 1
                kal.update(rok=r, mesic=m)
                _obsah.refresh()

            with ui.row().classes('w-full items-center gap-2 mb-3'):
                ui.button(icon='chevron_left', on_click=lambda: _posun(-1)).props('flat round dense')
                ui.label(f'{_MESICE[mesic].capitalize()} {rok}').classes(
                    'text-xl font-black text-gray-800 min-w-48 text-center')
                ui.button(icon='chevron_right', on_click=lambda: _posun(1)).props('flat round dense')
                ui.button('Dnes', on_click=lambda: (
                    kal.update(rok=datetime.date.today().year, mesic=datetime.date.today().month),
                    _obsah.refresh())).props('flat dense no-caps').classes('text-xs font-bold')
                ui.element('div').classes('flex-1')
                ui.label(f'{len(v_mesici)} termínů · {sum(t["pocet"] for t in v_mesici)} přihlášených'
                         ).classes('text-xs font-bold text-gray-400')
                for popis, barva in (('Praha', _styl_mista('Praha')[1]), ('Ostrava', _styl_mista('Ostrava')[1]),
                                     ('TOP kurz', '#7C3AED')):
                    with ui.row().classes('items-center gap-1'):
                        ui.element('div').classes('w-3 h-3 rounded').style(f'background:{barva}')
                        ui.label(popis).classes('text-[11px] font-bold text-gray-500')

            mrizka = 'display:grid; grid-template-columns:repeat(7, minmax(0, 1fr)); gap:3px;'
            with ui.element('div').classes('w-full mb-1').style(mrizka):
                for dn in ('Po', 'Út', 'St', 'Čt', 'Pá', 'So', 'Ne'):
                    ui.label(dn).classes('text-center text-xs font-bold text-gray-500 py-1')
            with ui.element('div').classes('w-full').style(mrizka):
                for i in range(pocet_tydnu * 7):
                    datum = zacatek + datetime.timedelta(days=i)
                    _bunka_dne(datum, sorted(podle_dne.get(datum, []),
                                             key=lambda t: (t['typ'] != 'top', t['misto'])), mesic)

        def _soupis_ucastniku(zaznamy):
            """Celá datová sada přihlášek – bez proklikávání jednotlivých kurzů."""
            sloupce = [
                {'name': 'datum', 'label': 'Datum', 'field': 'datum', 'align': 'left'},
                {'name': 'kurz', 'label': 'Kurz', 'field': 'kurz', 'align': 'left', 'sortable': True},
                {'name': 'misto', 'label': 'Místo', 'field': 'misto', 'align': 'left', 'sortable': True},
                {'name': 'firma', 'label': 'Firma', 'field': 'firma', 'align': 'left', 'sortable': True},
                {'name': 'pobocka', 'label': 'Pobočka', 'field': 'pobocka', 'align': 'left', 'sortable': True},
                {'name': 'oz', 'label': 'OZ', 'field': 'oz', 'align': 'left', 'sortable': True},
                {'name': 'ico', 'label': 'IČO', 'field': 'ico', 'align': 'left'},
                {'name': 'provozovna', 'label': 'Provozovna', 'field': 'provozovna', 'align': 'left'},
                {'name': 'zakaznik', 'label': 'Zákazník', 'field': 'zakaznik', 'align': 'left', 'sortable': True},
                {'name': 'funkce', 'label': 'Funkce', 'field': 'funkce', 'align': 'left'},
                {'name': 'telefon', 'label': 'Telefon', 'field': 'telefon', 'align': 'left'},
                {'name': 'podpis', 'label': 'Podpis', 'field': 'podpis', 'align': 'center'},
                {'name': 'stav', 'label': 'Stav', 'field': 'stav', 'align': 'left', 'sortable': True},
            ]
            radky = [{
                'id': r['id'],
                'datum': _fmt_datum(r['datum']),
                'kurz': r['nazev'] or '-',
                'misto': r['misto'] or '-',
                'firma': r['firma'] or ('dodavatel' if r['typ'] == 'top' else 'JIP'),
                'pobocka': POBOCKY_NAZVY.get(r['pobocka_klic'], r['pobocka_klic']) or '-',
                'oz': r['oz'] or '-',
                'ico': r['ico'] or '-',
                'provozovna': r['provozovna'] or '-',
                'zakaznik': r['zakaznik'] or '-',
                'funkce': r['funkce'] or '-',
                'telefon': r['telefon'] or '-',
                'podpis': '✓' if r['podpis'] else '-',
                'stav': 'Zrušeno' if r['stav'] == 'zruseno' else 'Aktivní',
            } for r in zaznamy]

            with ui.row().classes('w-full items-center gap-3 mb-3'):
                ui.icon('groups', color='primary').classes('text-3xl')
                ui.label('Účastníci – kompletní data').classes(
                    'text-2xl font-black text-gray-800')
                ui.label(f'{len(radky)} zápisů').classes('text-xs font-bold text-gray-400 pt-2')
                ui.element('div').classes('flex-1')
                hledani = ui.input(placeholder='Hledat v datech…').props(
                    'outlined dense clearable').classes('w-72')
            if not radky:
                with ui.column().classes('w-full items-center py-12 gap-2'):
                    ui.icon('inbox', size='3rem', color='grey-4')
                    ui.label('Zatím nikdo není zapsaný.').classes('text-gray-400')
                return
            tabulka = ui.table(columns=sloupce, rows=radky, row_key='id',
                               pagination=50).classes('w-full').props('dense flat bordered')
            hledani.on_value_change(lambda e: tabulka.set_filter(e.value or ''))

        @ui.refreshable
        def _obsah():
            if ref['std'] is None:
                with ui.column().classes('w-full items-center py-16'):
                    ui.spinner(size='3rem', color='primary')
                return

            if filtr['pohled'] == 'ucastnici':
                _soupis_ucastniku(ref['ucastnici'])
                return

            if filtr['pohled'] == 'kalendar':
                # V kalendáři se proběhlé termíny nikdy neskrývají — listuje se měsíci.
                vse = _filtruj(ref['std'], i_stare=True) + _filtruj(ref['top'], i_stare=True)
                _kalendar(vse)
                return

            # --- všechny kurzy (JIP i dodavatelské) v jednom seznamu, po měsících ---
            std = sorted(_filtruj(ref['std']) + _filtruj(ref['top']),
                         key=lambda t: (t['datum'] is None, t['datum'] or datetime.date.min,
                                        t['misto'] or '', t['nazev'] or ''))
            # Nejbližší týden nahoře velkými dlaždicemi – v seznamu dole zůstávají také.
            dnes = datetime.date.today()
            blizke = [t for t in std if t['datum'] and 0 <= (t['datum'] - dnes).days <= 7]
            if blizke:
                with ui.row().classes('w-full items-center gap-2 mb-3'):
                    ui.icon('local_fire_department', color='deep-orange').classes('text-2xl')
                    ui.label('Nejbližší kurzy').classes('text-xl font-black text-gray-800')
                with ui.row().classes('w-full gap-4 flex-wrap mb-8'):
                    for t in blizke:
                        _karta_velka(t)

            with ui.row().classes('w-full items-center gap-2 mb-3'):
                ui.element('div').classes('flex-1')
                ui.button('Export do Excelu', icon='download',
                          on_click=lambda: _export_obdobi(std)
                          ).props('outline dense no-caps color=green-8').classes('font-bold') \
                    .tooltip('Kurzy podle filtru (místo, typ, období) i s účastníky')
                if je_spravce:
                    cekajici = sum(1 for t in (ref['std'] or []) + (ref['top'] or [])
                                   if t['souhrn_verze'] is None or t['souhrn_verze'] < t['verze'])
                    with ui.button('Odeslat změny', icon='forward_to_inbox',
                                   on_click=_otevri_email_dialog
                                   ).props('outline dense no-caps color=orange-9'
                                           if cekajici else 'outline dense no-caps color=grey-6'
                                           ).classes('font-bold') \
                            .tooltip('Rozeslat zapisovatelům e-mail o nových a upravených termínech'):
                        if cekajici:
                            ui.badge(str(cekajici), color='orange-9').props('floating')
                    ui.button('Import z Excelu', icon='upload_file',
                              on_click=_otevri_import_dialog
                              ).props('outline dense no-caps color=grey-8').classes('font-bold')
                    ui.button('Nový kurz', icon='add',
                              on_click=lambda: _otevri_termin_dialog(None, 'standard')
                              ).props('outline dense no-caps color=primary').classes('font-bold')
            if not std and not blizke:
                with ui.column().classes('w-full items-center py-12 gap-2'):
                    ui.icon('event_busy', size='3rem', color='grey-4')
                    ui.label('Žádné termíny neodpovídají filtru.').classes('text-gray-400')
                return

            posledni = object()
            for t in std:
                mesic = _fmt_mesic(t['datum'])
                if mesic != posledni:
                    posledni = mesic
                    with ui.row().classes('w-full items-center gap-2 mt-4 mb-1'):
                        ui.label(mesic).classes(
                            'text-sm font-black text-gray-500 uppercase tracking-wider')
                        ui.element('div').classes('flex-1 h-px bg-gray-200')
                _karta_terminu(t)

        def _filtruj(terminy, i_stare=False):
            dnes = datetime.date.today()
            hledej = filtr['hledej'].strip().lower()
            od = _parse_datum(filtr['od'])
            do = _parse_datum(filtr['do'])
            out = []
            for t in terminy or []:
                if filtr['misto'] != 'vse' and t['misto'] != filtr['misto']:
                    continue
                if filtr['typ'] != 'vse' and t['typ'] != filtr['typ']:
                    continue
                if od or do:            # zvolené období má přednost před "skrýt proběhlé"
                    if not t['datum']:
                        continue
                    if od and t['datum'] < od:
                        continue
                    if do and t['datum'] > do:
                        continue
                elif not i_stare and filtr['skryt_stare'] and t['datum'] and t['datum'] < dnes:
                    continue
                if hledej and hledej not in ' '.join(
                        str(t.get(k) or '') for k in ('nazev', 'lektor', 'firma', 'misto')).lower():
                    continue
                out.append(t)
            return out

        async def _export_obdobi(terminy):
            """Kurzy právě zobrazené v seznamu (období + filtry) i s účastníky do Excelu."""
            if not terminy:
                ui.notify('Filtru neodpovídá žádný kurz.', type='warning')
                return
            od, do = _parse_datum(filtr['od']), _parse_datum(filtr['do'])
            if od or do:
                popis = [f"Období {_fmt_datum(od) if od else '…'} – {_fmt_datum(do) if do else '…'}"]
            else:
                popis = ['Nadcházející kurzy' if filtr['skryt_stare'] else 'Všechny kurzy']
            if filtr['misto'] != 'vse':
                popis.append(filtr['misto'])
            if filtr['typ'] != 'vse':
                popis.append('kurzy JIP' if filtr['typ'] == 'standard' else 'kurzy dodavatel')
            if filtr['hledej'].strip():
                popis.append(f"hledáno „{filtr['hledej'].strip()}“")
            popis.append(f'{len(terminy)} kurzů · export {_fmt_datum(datetime.date.today())}')
            prihlasky = await asyncio.to_thread(_nacti_vsechny_prihlasky)
            obsah = await asyncio.to_thread(_export_xlsx, terminy, prihlasky,
                                            'Gastrokurzy – přehled kurzů a účastníků',
                                            ' · '.join(popis))
            nazev = (_nazev_souboru('gastrokurzy', od or 'zacatek', do or 'konec') if od or do
                     else _nazev_souboru('gastrokurzy', datetime.date.today()))
            ui.download.content(obsah, nazev, XLSX_MIME)
            intranet_logger.log_activity(user_name, LOG_KATEGORIE,
                                         f"Export kurzů do Excelu ({' · '.join(popis)})")

        # --- hlavička modulu ---
        with ui.row().classes('w-full items-center gap-3 mb-1'):
            ui.icon('soup_kitchen', color='primary').classes('text-3xl')
            ui.label('Gastrokurzy').classes('text-3xl font-extrabold text-gray-800')
            ui.element('div').classes('flex-1')
            if not muze_zapisovat:
                ui.label('Přístup jen pro čtení').classes(
                    'text-xs font-bold text-gray-400 italic')
        ui.label('Dvojklik na termín otevře prezenční listinu. Najetím na datum se zobrazí lektor.'
                 if filtr['pohled'] == 'seznam' else
                 'Klik na kurz v kalendáři otevře prezenční listinu. Najetím myši se zobrazí detail.'
                 ).classes('text-xs text-gray-400 mb-4')

        def _zmena(_=None):
            _obsah.refresh()

        with ui.row().classes('w-full items-center gap-2 mb-6'):
            if filtr['pohled'] != 'ucastnici':      # soupis dat má vlastní hledání
                ui.select({'vse': 'Praha i Ostrava', **{m: m for m in MISTA}}, value='vse',
                          on_change=lambda e: (filtr.update(misto=e.value), _zmena())
                          ).props('outlined dense').classes('w-48')
                ui.select({'vse': 'Všechny kurzy', 'top': 'Kurzy dodavatel', 'standard': 'Kurzy JIP'},
                          value=filtr['typ'],
                          on_change=lambda e: (filtr.update(typ=e.value), _zmena())
                          ).props('outlined dense').classes('w-44')
                ui.input(placeholder='Hledat kurz nebo lektora…',
                         on_change=lambda e: (filtr.update(hledej=e.value or ''), _zmena())
                         ).props('outlined dense clearable').classes('w-72')
                with ui.row().classes('items-center gap-1 no-wrap'):
                    ui.label('Období').classes('text-xs font-bold text-gray-400')
                    ui.input(value=filtr['od'],
                             on_change=lambda e: (filtr.update(od=e.value or ''), _zmena())
                             ).props('type=date outlined dense').classes('w-36')
                    ui.label('–').classes('text-xs font-bold text-gray-400')
                    ui.input(value=filtr['do'],
                             on_change=lambda e: (filtr.update(do=e.value or ''), _zmena())
                             ).props('type=date outlined dense').classes('w-36')
            if filtr['pohled'] == 'seznam':     # v kalendáři se listuje po měsících
                ui.checkbox('Skrýt proběhlé', value=filtr['skryt_stare'],
                            on_change=lambda e: (filtr.update(skryt_stare=e.value), _zmena()))
            ui.element('div').classes('flex-1')
            with ui.row().classes('items-center gap-0 bg-gray-100 rounded-lg p-0.5'):
                for kod, popis, ikona in (('seznam', 'Seznam', 'view_list'),
                                          ('kalendar', 'Kalendář', 'calendar_month'),
                                          ('ucastnici', 'Účastníci', 'table_view')):
                    akt = filtr['pohled'] == kod
                    with ui.row().classes(
                            'items-center gap-1 px-3 py-1 rounded-md cursor-pointer transition-colors '
                            + ('bg-white text-rose-600 shadow-sm' if akt
                               else 'text-gray-500 hover:text-gray-700')
                    ).on('click', lambda _, k=kod: (filtr.update(pohled=k), _panel.refresh())):
                        ui.icon(ikona, size='1.1rem')
                        ui.label(popis).classes('text-xs font-bold')

        _obsah()
        ui.timer(0, _nacti, once=True)

    _panel()


def _volna_mista(t, prihlasky):
    """Volná místa, nebo None u kurzu bez kapacity."""
    if not t.get('kapacita'):
        return None
    if prihlasky is None:
        obsazeno = t.get('pocet', 0)
    else:
        obsazeno = sum(1 for p in prihlasky if p['stav'] == 'aktivni')
    return max(0, int(t['kapacita']) - int(obsazeno))
