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
kdo a kdy ho zrušil.

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

_MESICE = ['', 'leden', 'únor', 'březen', 'duben', 'květen', 'červen',
           'červenec', 'srpen', 'září', 'říjen', 'listopad', 'prosinec']

_db_init = False
_POSLEDNI_SOUHRN_DATUM = None   # datum posledního odeslaného denního souhrnu


# ==========================================
# POMOCNÉ FORMÁTOVÁNÍ
# ==========================================
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
        # Stopa o denním souhrnu — v DB, ne v RAM, aby restart serveru nezpůsobil
        # druhé odeslání téhož termínu. Verze se počítá, nesrovnává s časem:
        # `updated_at` má granularitu 1 s, takže úprava ve stejné sekundě jako
        # rozesílka by se ztratila. Termíny existující v okamžiku migrace se
        # označí za ohlášené (žádný mail zpětně za historii).
        for prikaz in (
            "ALTER TABLE gastrokurzy_termin ADD COLUMN verze INT NOT NULL DEFAULT 1",
            "ALTER TABLE gastrokurzy_termin ADD COLUMN souhrn_verze INT NULL",
            "ALTER TABLE gastrokurzy_termin ADD COLUMN souhrn_odeslan DATETIME NULL",
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


def _uloz_termin(data, termin_id=None):
    """INSERT/UPDATE termínu. Vrací id, nebo None při chybě."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = conn.cursor()
    try:
        if termin_id:
            cur.execute("""UPDATE gastrokurzy_termin
                  SET misto=%s, datum=%s, nazev=%s, firma=%s, lektor=%s,
                      kapacita=%s, stav=%s, poznamka=%s, verze = verze + 1
                  WHERE id=%s""",
                        (data['misto'], data['datum'], data['nazev'], data.get('firma'),
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


def _uloz_prihlasku(data, prihlaska_id=None):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        if prihlaska_id:
            cur.execute("""UPDATE gastrokurzy_prihlaska
                  SET pobocka_klic=%s, oz=%s, ico=%s, provozovna=%s, zakaznik=%s,
                      funkce=%s, telefon=%s, podpis=%s
                  WHERE id=%s""",
                        (data['pobocka_klic'], data['oz'], data['ico'], data['provozovna'],
                         data['zakaznik'], data['funkce'], data['telefon'],
                         int(data['podpis']), prihlaska_id))
        else:
            cur.execute("""INSERT INTO gastrokurzy_prihlaska
                  (termin_id, pobocka_klic, oz, ico, provozovna, zakaznik,
                   funkce, telefon, podpis, zapsal)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (data['termin_id'], data['pobocka_klic'], data['oz'], data['ico'],
                         data['provozovna'], data['zakaznik'], data['funkce'],
                         data['telefon'], int(data['podpis']), data['zapsal']))
        conn.commit()
        return True
    except Exception as e:
        print(f"[gastrokurzy] Uložení přihlášky: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def _zmen_stav_prihlasky(prihlaska_id, stav, user_name):
    """Zrušení zápisu (stav 'zruseno') nebo jeho obnovení ('aktivni')."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
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


def _prepni_podpis(prihlaska_id, hodnota):
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
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
# E-MAILY – denní souhrn nových a upravených termínů
# ==========================================
def _nacti_neohlasene_terminy():
    """Termíny, které ještě nebyly v souhrnu, nebo se od něj změnily.

    Kritérium je v datech (sloupec `souhrn_odeslan`), ne v časovém okně — restart
    serveru ani zmeškaná minuta časovače tedy nic neduplikuje ani neztratí.
    """
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT t.*,
                  SUM(p.stav = 'aktivni') AS pocet,
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
        return terminy
    except Exception as e:
        print(f"[gastrokurzy] Načtení nenahlášených termínů: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def _oznac_jako_ohlasene(terminy):
    """Uloží, KTERÁ verze termínu byla ohlášena (`souhrn_odeslan` = jeho `updated_at`
    z okamžiku sestavení mailu), ne kdy se mail poslal.

    Díky tomu se úprava provedená ve stejné sekundě jako rozesílka neztratí a
    naopak se nic neohlásí dvakrát. `updated_at = updated_at` potlačí
    ON UPDATE NOW(), jinak by se termín tímto zápisem sám označil za změněný.
    """
    if not terminy:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.executemany("""UPDATE gastrokurzy_termin
              SET souhrn_verze = %s, souhrn_odeslan = NOW(), updated_at = updated_at
              WHERE id = %s""",
                        [(t['verze'], t['id']) for t in terminy])
        conn.commit()
    except Exception as e:
        print(f"[gastrokurzy] Označení odeslaného souhrnu: {e}")
    finally:
        cur.close()
        conn.close()


def _blok_terminu(t):
    """Odstavec jednoho termínu v e-mailu."""
    radky = [f"Kurz:          {t['nazev'] or '(bez názvu)'}"]
    if t['typ'] == 'top' and t.get('firma'):
        radky.append(f"Firma:         {t['firma']}")
    radky.append(f"Místo konání:  {t['misto']}")
    radky.append(f"Datum:         {_fmt_datum(t['datum'])}")
    if t.get('lektor'):
        radky.append(f"Lektor:        {t['lektor']}")
    if t['volno'] is None:
        radky.append("Volná místa:   neomezeno")
    else:
        radky.append(f"Volná místa:   {t['volno']} z {t['kapacita']}")
    if t['stav'] == 'zruseno':
        radky.append("Stav:          TERMÍN ZRUŠEN")
    if t.get('poznamka'):
        radky.append(f"Poznámka:      {t['poznamka']}")
    return '\n'.join(radky)


def _text_souhrnu(terminy):
    """Tělo souhrnného e-mailu; None = není co poslat."""
    nove = [t for t in terminy if t['je_novy']]
    zmenene = [t for t in terminy if not t['je_novy']]
    if not nove and not zmenene:
        return None

    casti = ["Dobrý den,\n"]
    if nove:
        casti.append(f"v Gastrokurzech přibyly nové termíny ({len(nove)}):\n")
        casti.append('\n\n'.join(_blok_terminu(t) for t in nove))
    if zmenene:
        casti.append(f"\nUpravené termíny ({len(zmenene)}):\n")
        casti.append('\n\n'.join(_blok_terminu(t) for t in zmenene))
    casti.append("\nPřihlášky na kurzy zapisujte v modulu Gastrokurzy.")
    if intranet_data.APP_URL:
        casti.append(f"\nOtevřít v portálu: {intranet_data.APP_URL}/gastrokurzy")
    return '\n'.join(casti)


def _odesli_souhrn():
    """Sestaví a rozešle denní souhrn zapisovatelům. Blokující.

    Termín se označí za nahlášený jen při skutečném odeslání — když e-maily
    selžou, zůstane ve frontě na další den, místo aby tiše zmizel.
    """
    terminy = _nacti_neohlasene_terminy()
    text = _text_souhrnu(terminy)
    if not text:
        return
    prijemci = intranet_data.ziskej_emaily_s_pravem('gastrokurzy_zapisovatel')
    if not prijemci:
        print("[gastrokurzy] Souhrn: žádný příjemce s právem gastrokurzy_zapisovatel")
        return
    pocet_novych = sum(1 for t in terminy if t['je_novy'])
    predmet = (f"Gastrokurzy – {pocet_novych} nových termínů" if pocet_novych
               else "Gastrokurzy – upravené termíny")
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
        "Systém Gastrokurzy", LOG_KATEGORIE,
        f"Denní souhrn: {odeslano}/{len(prijemci)} příjemců, {len(terminy)} termínů")


async def bg_gastrokurzy_souhrn():
    """Jednou denně (výchozí 12:00) rozešle souhrn nových a upravených termínů.

    Hlídá „dnes už proběhlo" jen v RAM kvůli zbytečným dotazům; proti duplicitě
    po restartu chrání `souhrn_odeslan` v DB. Podmínka je `>= čas`, ne rovnost na
    minutu — restart přes poledne tedy souhrn dožene, místo aby ho přeskočil.
    """
    global _POSLEDNI_SOUHRN_DATUM
    await asyncio.sleep(120)
    while True:
        try:
            nastaveni = intranet_data.nacti_nastaveni_intranetu()
            if (nastaveni.get('gastrokurzy_zapnuty', True)
                    and nastaveni.get('gastrokurzy_email_zapnuty', True)):
                cas = str(nastaveni.get('gastrokurzy_email_cas', '12:00')).strip()
                nyni = datetime.datetime.now()
                if (nyni.strftime('%H:%M') >= cas
                        and _POSLEDNI_SOUHRN_DATUM != nyni.date()):
                    _POSLEDNI_SOUHRN_DATUM = nyni.date()
                    await asyncio.to_thread(_odesli_souhrn)
        except Exception as e:
            print(f"[bg_gastrokurzy_souhrn] Chyba: {e}")
        await asyncio.sleep(60)


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
    filtr = {'misto': 'vse', 'hledej': '', 'skryt_stare': True, 'pohled': 'seznam'}
    kal = {'rok': dnes_.year, 'mesic': dnes_.month}
    posledni_pobocka_key = f'gastrokurzy_pob_{user_id}'

    # =================================================================
    # DIALOG: PREZENČNÍ LISTINA
    # =================================================================
    async def _otevri_prezencku(t):
        prihlasky = await asyncio.to_thread(_nacti_prihlasky, t['id'])
        ref = {'data': prihlasky}
        zruseny_termin = t['stav'] == 'zruseno'
        editovat = muze_zapisovat and not zruseny_termin

        with ui.dialog() as dlg, ui.card().classes('p-0 gap-0').style(
                'min-width: 1100px; max-width: 1400px; max-height: 90vh; overflow-y: auto'):

            # --- hlavička: údaje se doplňují automaticky z termínu ---
            barva = 'from-rose-500 to-pink-600' if t['typ'] == 'standard' else 'from-violet-600 to-fuchsia-600'
            with ui.column().classes(f'w-full p-6 gap-1 bg-gradient-to-r {barva} text-white'):
                with ui.row().classes('w-full items-center gap-3'):
                    ui.icon('assignment_ind', size='2rem')
                    ui.label('Prezenční listina').classes('text-2xl font-extrabold')
                    ui.element('div').classes('flex-1')
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
                    with ui.row().classes('w-full items-center gap-2'):
                        pob_i = ui.select({k: n for k, n in POBOCKY}, label='Pobočka *',
                                          value=app.storage.user.get(posledni_pobocka_key, 'praha')
                                          ).props('outlined dense').classes('w-40')
                        oz_i = ui.input('OZ (kdo zve) *').props('outlined dense').classes('w-40')
                        ico_i = ui.input('IČO zákazníka *').props('outlined dense').classes('w-32')
                        prov_i = ui.input('Provozovna').props('outlined dense').classes('flex-1 min-w-40')
                        jm_i = ui.input('Jméno zákazníka *').props('outlined dense').classes('flex-1 min-w-48')
                        fce_i = ui.input('Funkce *').props('outlined dense').classes('w-40')
                        tel_i = ui.input('Telefon').props('outlined dense').classes('w-36')
                        pod_i = ui.checkbox('Podpis')

                        async def _pridej():
                            chybi = [lbl for lbl, el in (('Pobočka', pob_i), ('OZ', oz_i), ('IČO', ico_i),
                                                         ('Jméno', jm_i), ('Funkce', fce_i))
                                     if not str(el.value or '').strip()]
                            if chybi:
                                ui.notify('Vyplňte: ' + ', '.join(chybi), type='warning')
                                return
                            volno = _volna_mista(t, ref['data'])
                            if volno is not None and volno <= 0:
                                ui.notify('Kurz je plně obsazen.', type='negative')
                                return
                            ok = await asyncio.to_thread(_uloz_prihlasku, {
                                'termin_id': t['id'], 'pobocka_klic': pob_i.value,
                                'oz': str(oz_i.value).strip(), 'ico': str(ico_i.value).strip(),
                                'provozovna': str(prov_i.value or '').strip(),
                                'zakaznik': str(jm_i.value).strip(), 'funkce': str(fce_i.value).strip(),
                                'telefon': str(tel_i.value or '').strip(), 'podpis': pod_i.value,
                                'zapsal': user_name})
                            if not ok:
                                ui.notify('Zápis se nepodařilo uložit.', type='negative')
                                return
                            app.storage.user[posledni_pobocka_key] = pob_i.value
                            intranet_logger.log_activity(
                                user_name, LOG_KATEGORIE,
                                f"Zápis na kurz {t['nazev']} ({t['misto']} {_fmt_datum(t['datum'])}): "
                                f"{jm_i.value} – {POBOCKY_NAZVY.get(pob_i.value, pob_i.value)}")
                            for el in (oz_i, ico_i, prov_i, jm_i, fce_i, tel_i):
                                el.value = ''
                            pod_i.value = False
                            await _obnov()

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
                    if zrusenych:
                        ui.label(f'Zrušeno: {zrusenych}').classes(
                            'text-sm font-bold text-gray-400 bg-gray-50 px-3 py-1 rounded-full')

                if not data:
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
                with ui.row().classes(
                        'w-full items-center gap-2 px-2 py-1.5 border-b border-gray-100 '
                        + ('bg-gray-50' if zruseno else 'hover:bg-blue-50')):
                    ui.label(POBOCKY_NAZVY.get(p['pobocka_klic'], p['pobocka_klic'])).classes(
                        f'w-32 {zaklad} font-bold')
                    ui.label(p['oz'] or '—').classes(f'w-24 {zaklad}')
                    ui.label(p['ico'] or '—').classes(f'w-28 {zaklad} font-mono')
                    ui.label(p['provozovna'] or '—').classes(f'w-52 {zaklad} truncate').tooltip(
                        p['provozovna'] or '')
                    if anonym:
                        with ui.row().classes('flex-1 min-w-48 items-center gap-1'):
                            ui.label('K doplnění').classes('text-sm italic text-amber-600')
                            ui.icon('info', size='0.9rem', color='amber').tooltip(
                                'Místo z původní tabulky – jméno zákazníka zatím nebylo zapsáno.')
                    else:
                        ui.label(p['zakaznik']).classes(f'flex-1 min-w-48 {zaklad} font-semibold')
                    ui.label(p['funkce'] or '—').classes(f'w-40 {zaklad}')
                    ui.label(p['telefon'] or '—').classes(f'w-32 {zaklad}')

                    async def _podpis(e, pid=p['id']):
                        if not await asyncio.to_thread(_prepni_podpis, pid, e.value):
                            ui.notify('Podpis se nepodařilo uložit.', type='negative')

                    with ui.element('div').classes('w-20'):
                        if zruseno:
                            ui.icon('block', color='grey-5')
                        else:
                            ui.checkbox(value=bool(p['podpis']), on_change=_podpis).props(
                                f'dense {"" if editovat else "disable"}').tooltip(
                                'Prezence – lze zaškrtnout i v den kurzu nebo později.')

                    with ui.row().classes('w-24 items-center gap-1 justify-end'):
                        if zruseno:
                            kdy = p['zruseno_kdy'].strftime('%d.%m.%Y') if p['zruseno_kdy'] else ''
                            ui.icon('history', size='1rem', color='grey-5').tooltip(
                                f"Zrušil: {p['zrusil'] or '?'} {kdy}")
                            if editovat:
                                ui.button(icon='undo', on_click=lambda _, pid=p['id']: _zmen(pid, 'aktivni')
                                          ).props('flat dense round size=sm color=grey').tooltip('Obnovit zápis')
                        elif editovat:
                            ui.button(icon='edit', on_click=lambda _, pp=p: _otevri_editaci(pp)
                                      ).props('flat dense round size=sm color=grey').tooltip('Upravit')
                            ui.button(icon='cancel', on_click=lambda _, pid=p['id']: _zmen(pid, 'zruseno')
                                      ).props('flat dense round size=sm color=red').tooltip('Zrušit zápis')

            async def _zmen(pid, stav):
                if not await asyncio.to_thread(_zmen_stav_prihlasky, pid, stav, user_name):
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
                            'podpis': e_pod.value}, p['id'])
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
        dlg.open()

    # =================================================================
    # DIALOG: TERMÍN KURZU (jen správce)
    # =================================================================
    def _otevri_termin_dialog(t=None, typ='standard'):
        novy = t is None
        t = t or {}
        with ui.dialog() as dlg, ui.card().classes('p-6 gap-3').style('min-width: 560px'):
            ui.label(('Nový termín' if novy else 'Úprava termínu')
                     + (' – TOP kurz' if typ == 'top' else '')).classes(
                'text-lg font-extrabold text-gray-800')
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
            if typ == 'top':
                i_firma = ui.input('Firma (ORKLA / UNILEVER / NESTLE) *',
                                   value=t.get('firma') or '').props('outlined dense').classes('w-full')
            else:
                i_firma = None
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
                data = {'typ': typ, 'misto': i_misto.value, 'datum': datum,
                        'nazev': str(i_nazev.value).strip(),
                        'firma': str(i_firma.value).strip().upper() if i_firma else t.get('firma'),
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

    def _karta_terminu(t):
        zruseno = t['stav'] == 'zruseno'
        volno = _volna_mista(t, None)
        karta = ui.card().classes(
            'w-full p-0 gap-0 overflow-hidden transition-all cursor-pointer '
            + ('opacity-60 ' if zruseno else 'hover:shadow-lg '))
        with karta:
            with ui.row().classes('w-full items-stretch gap-0 no-wrap'):
                # datum – růžový sloupec jako v Excelu; hover ukáže lektora
                with ui.column().classes(
                        'items-center justify-center px-4 py-3 gap-0 min-w-28 '
                        + ('bg-gray-300' if zruseno else 'bg-rose-500')):
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
                        if zruseno:
                            ui.label('ZRUŠENO').classes(
                                'text-[10px] font-black text-red-600 bg-red-100 px-2 py-0.5 rounded-full')
                    with ui.row().classes('items-center gap-2 flex-wrap'):
                        with ui.row().classes('items-center gap-1'):
                            ui.icon('place', size='0.9rem', color='grey-6')
                            ui.label(t['misto']).classes('text-xs font-bold text-gray-500')
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

    def _karta_top(t):
        """TOP kurz – prémiová dlaždice (firemní kurz s hlídanou kapacitou)."""
        zruseno = t['stav'] == 'zruseno'
        od, do = FIRMY_STYL.get((t['firma'] or '').upper(), _FIRMA_DEFAULT)
        volno = _volna_mista(t, None)
        obsazeno_pct = 0
        if t['kapacita']:
            obsazeno_pct = min(100, round(t['pocet'] / t['kapacita'] * 100))
        karta = ui.card().classes(
            'p-0 gap-0 overflow-hidden cursor-pointer transition-all hover:-translate-y-1 hover:shadow-2xl'
            + (' opacity-60' if zruseno else '')).style('width: 340px; border-radius: 18px')
        with karta:
            with ui.column().classes('w-full p-4 gap-1 text-white relative').style(
                    f'background: linear-gradient(135deg, {od} 0%, {do} 100%)'):
                with ui.row().classes('w-full items-center gap-2 no-wrap'):
                    ui.icon('workspace_premium', size='1.4rem').classes('opacity-90')
                    ui.label(t['firma'] or 'TOP KURZ').classes(
                        'text-lg font-black tracking-wide truncate')
                    ui.element('div').classes('flex-1')
                    ui.label('TOP').classes(
                        'text-[10px] font-black bg-white/25 px-2 py-0.5 rounded-full')
                ui.label(t['nazev'] or '—').classes('text-sm font-bold opacity-95 leading-snug')
                with ui.row().classes('items-center gap-3 pt-1'):
                    with ui.row().classes('items-center gap-1'):
                        ui.icon('event', size='0.9rem').classes('opacity-80')
                        ui.label(_fmt_datum(t['datum'])).classes('text-xs font-bold')
                    with ui.row().classes('items-center gap-1'):
                        ui.icon('place', size='0.9rem').classes('opacity-80')
                        ui.label(t['misto']).classes('text-xs font-bold')
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
                        cls = ('bg-red-100 text-red-700' if volno <= 0
                               else 'bg-green-100 text-green-700')
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
                                  on_click=lambda _, tt=t: _otevri_termin_dialog(tt, 'top')
                                  ).props('flat dense round color=grey')
        karta.on('dblclick', lambda _, tt=t: _otevri_prezencku(tt))

    # =================================================================
    # HLAVNÍ PANEL
    # =================================================================
    @ui.refreshable
    def _panel():
        ref = {'std': None, 'top': None}

        async def _nacti():
            ref['std'] = await asyncio.to_thread(_nacti_terminy, 'standard')
            ref['top'] = await asyncio.to_thread(_nacti_terminy, 'top')
            _obsah.refresh()

        # =============================================================
        # KALENDÁŘNÍ NÁHLED
        # =============================================================
        def _barva_terminu(t):
            """Barva podle místa konání; TOP kurz má vlastní fialovou."""
            if t['typ'] == 'top':
                return '#7C3AED'
            return '#E11D48' if t['misto'] == 'Praha' else '#0E7490'

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
                for popis, barva in (('Praha', '#E11D48'), ('Ostrava', '#0E7490'), ('TOP kurz', '#7C3AED')):
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

        @ui.refreshable
        def _obsah():
            if ref['std'] is None:
                with ui.column().classes('w-full items-center py-16'):
                    ui.spinner(size='3rem', color='primary')
                return

            if filtr['pohled'] == 'kalendar':
                # V kalendáři se proběhlé termíny nikdy neskrývají — listuje se měsíci.
                vse = _filtruj(ref['std'], i_stare=True) + _filtruj(ref['top'], i_stare=True)
                _kalendar(vse)
                return

            # --- TOP kurzy ---
            top = _filtruj(ref['top'])
            with ui.row().classes('w-full items-center gap-2 mb-3'):
                ui.icon('workspace_premium', color='deep-purple').classes('text-2xl')
                ui.label('TOP kurzy').classes('text-xl font-black text-gray-800')
                ui.label('firemní kurzy s omezenou kapacitou').classes(
                    'text-xs font-bold text-gray-400 pt-1')
                ui.element('div').classes('flex-1')
                if je_spravce:
                    ui.button('Nový TOP kurz', icon='add',
                              on_click=lambda: _otevri_termin_dialog(None, 'top')
                              ).props('outline dense no-caps color=deep-purple').classes('font-bold')
            if top:
                with ui.row().classes('w-full gap-4 flex-wrap mb-8'):
                    for t in top:
                        _karta_top(t)
            else:
                ui.label('Žádné TOP kurzy neodpovídají filtru.').classes(
                    'text-sm italic text-gray-400 mb-8')

            # --- běžné kurzy, po měsících ---
            std = _filtruj(ref['std'])
            with ui.row().classes('w-full items-center gap-2 mb-3'):
                ui.icon('restaurant_menu', color='primary').classes('text-2xl')
                ui.label('Termíny kurzů').classes('text-xl font-black text-gray-800')
                ui.label(f'{len(std)} termínů').classes('text-xs font-bold text-gray-400 pt-1')
                ui.element('div').classes('flex-1')
                if je_spravce:
                    ui.button('Nový termín', icon='add',
                              on_click=lambda: _otevri_termin_dialog(None, 'standard')
                              ).props('outline dense no-caps color=primary').classes('font-bold')
            if not std:
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
            out = []
            for t in terminy or []:
                if filtr['misto'] != 'vse' and t['misto'] != filtr['misto']:
                    continue
                if not i_stare and filtr['skryt_stare'] and t['datum'] and t['datum'] < dnes:
                    continue
                if hledej and hledej not in ' '.join(
                        str(t.get(k) or '') for k in ('nazev', 'lektor', 'firma', 'misto')).lower():
                    continue
                out.append(t)
            return out

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
            ui.select({'vse': 'Praha i Ostrava', **{m: m for m in MISTA}}, value='vse',
                      on_change=lambda e: (filtr.update(misto=e.value), _zmena())
                      ).props('outlined dense').classes('w-48')
            ui.input(placeholder='Hledat kurz nebo lektora…',
                     on_change=lambda e: (filtr.update(hledej=e.value or ''), _zmena())
                     ).props('outlined dense clearable').classes('w-72')
            if filtr['pohled'] == 'seznam':     # v kalendáři se listuje po měsících
                ui.checkbox('Skrýt proběhlé', value=filtr['skryt_stare'],
                            on_change=lambda e: (filtr.update(skryt_stare=e.value), _zmena()))
            ui.element('div').classes('flex-1')
            with ui.row().classes('items-center gap-0 bg-gray-100 rounded-lg p-0.5'):
                for kod, popis, ikona in (('seznam', 'Seznam', 'view_list'),
                                          ('kalendar', 'Kalendář', 'calendar_month')):
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
