from nicegui import ui, app, events
import os, uuid, datetime, shutil, asyncio, urllib.parse, unicodedata, gc, zipfile
import intranet_data, intranet_logger
import intranet_static
import intranet_emaily
import intranet_jobs
from intranet_ui_utils import refreshable_na_klienta
try: import fitz; HAS_FITZ = True
except ImportError: HAS_FITZ = False

# ==========================================
# 📂 SLOŽKY A ROUTOVÁNÍ PRO OBA MODULY
# ==========================================
TISK_DIR, PRILOHY_DIR = 'Tisk_Nakup', 'Prilohy_Nakup'
UPLOAD_DIR, EXPORT_DIR = 'Faktury_Uploads', 'Exporty_Faktury'
for d in [TISK_DIR, PRILOHY_DIR, UPLOAD_DIR, EXPORT_DIR]: os.makedirs(d, exist_ok=True)

PREDPOPTAVKY_DIR = 'Prilohy_Predpoptavky'
os.makedirs(PREDPOPTAVKY_DIR, exist_ok=True)
intranet_static.chranene_soubory('/prilohy_predpoptavky', PREDPOPTAVKY_DIR)

PREDPOPTAVKY_KATEGORIE = ['IT / Elektronika', 'Kancelářské potřeby', 'Nábytek / Vybavení', 'Kuchyň / Občerstvení', 'Nástroje / Hardware', 'Hygienické potřeby', 'Marketing / Tisk', 'Ostatní']
PP_STAV_BARVA = {'Koncept': 'grey', 'Čeká na schválení': 'blue', 'Vráceno k doplnění': 'orange', 'Zamítnuto': 'red', 'Schváleno': 'green', 'Převedeno': 'purple'}
PP_STAV_IKONA = {'Koncept': 'edit_note', 'Čeká na schválení': 'hourglass_top', 'Vráceno k doplnění': 'undo', 'Zamítnuto': 'cancel', 'Schváleno': 'check_circle', 'Převedeno': 'swap_horiz'}

intranet_static.chranene_soubory('/tisk_nakup', TISK_DIR)
intranet_static.chranene_soubory('/prilohy_nakup', PRILOHY_DIR)
intranet_static.chranene_soubory('/faktury_soubory', UPLOAD_DIR)
intranet_static.chranene_soubory('/faktury_exporty_soubory', EXPORT_DIR)

# ==========================================
# 🚀 CENTRÁLNÍ CACHE A RYCHLÉ NAČÍTÁNÍ
# ==========================================
SCHVALOVATELE_FAKTURY = {'data': [], 'last_update': None}
CACHE_FAKTURY = {'data': [], 'last_update': None}
CACHE_NAKUP = {'data': [], 'last_update': None}
CACHE_BADGE = {'hodnota': 0, 'last_update': None}  # 5 min TTL — jen pro číslo v menu
CACHE_PREDPOPTAVKY = {'data': [], 'last_update': None}
_FINANCE_DB_INICIALIZOVANA = False  # Guard: CREATE TABLE + ALTER TABLE jen jednou za běh procesu

def vynut_obnovu_faktur(): CACHE_FAKTURY['last_update'] = None; CACHE_BADGE['last_update'] = None
def vynut_obnovu_nakupu(): CACHE_NAKUP['last_update'] = None; CACHE_BADGE['last_update'] = None
def vynut_obnovu_predpoptavek(): CACHE_PREDPOPTAVKY['last_update'] = None

def posli_emaily_schvovatelum(schvalovatel_ids: list, typ: str, cislo: str, nazev: str):
    """
    Odešle e-mailové upozornění každému schvalovateli (dle ID), že má co ke schválení.
    schvalovatel_ids: seznam int ID uživatelů (None hodnoty se přeskočí)
    """
    try:
        conn = get_db_utf8()
        if not conn:
            return
        cur = None
        emaily = []
        try:
            cur = conn.cursor(dictionary=True)
            for uid in schvalovatel_ids:
                if not uid:
                    continue
                cur.execute("SELECT email FROM user WHERE iduser = %s", (uid,))
                row = cur.fetchone()
                if row and row.get('email'):
                    emaily.append(row['email'])
        finally:
            if cur: cur.close()
            if conn: conn.close()

        predmet = f"[Intranet] Máte novou položku ke schválení: {cislo}"
        text = (
            f"Dobrý den,\n\n"
            f"byl/a jste přiřazen/a jako schvalovatel pro {typ.lower()}:\n\n"
            f"  Číslo: {cislo}\n"
            f"  Název: {nazev}\n\n"
            f"Přihlaste se prosím do intranetu a položku schvalte nebo zamítněte.\n\n"
            f"Tato zpráva byla odeslána automaticky systémem MojeJIPka."
        )
        for email in emaily:
            try:
                intranet_emaily.odesli_upozorneni_email(email, predmet, text)
            except Exception as e:
                print(f"[Finance] Chyba při odesílání e-mailu schvalovateli {email}: {e}")
    except Exception as e:
        print(f"[Finance] Chyba v posli_emaily_schvovatelum: {e}")


def posli_email_zadateli(zadavatel_id, udalost: str, cislo: str, nazev: str, duvod: str = ''):
    """Pošle e-mail žadateli (dle user_id) o vývoji jeho objednávky / faktury.

    udalost: 'schvalena_objednavka' | 'zamitnuta_objednavka' |
             'faktura_vlozena' | 'schvalena_faktura' | 'zamitnuta_faktura' |
             'zadano_ucetnictvi'
    """
    try:
        if not zadavatel_id:
            return
        conn = get_db_utf8()
        if not conn:
            return
        email = None
        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT email FROM user WHERE iduser = %s", (zadavatel_id,))
            row = cur.fetchone()
            if row:
                email = row.get('email')
        finally:
            if cur: cur.close()
            conn.close()
        if not email:
            return

        texty = {
            'schvalena_objednavka': (f"Objednávka {cislo} byla schválena",
                                     f"Vaše objednávka byla schválena.\n\n  Číslo: {cislo}\n  Název: {nazev}\n\nMůžete vyčkat na dodání a následně nahrát fakturu."),
            'zamitnuta_objednavka': (f"Objednávka {cislo} byla zamítnuta",
                                     f"Vaše objednávka byla zamítnuta.\n\n  Číslo: {cislo}\n  Název: {nazev}\n  Důvod: {duvod}\n\nObjednávku můžete upravit a odeslat znovu ke schválení."),
            'faktura_vlozena': (f"K objednávce {cislo} byla vložena faktura",
                                f"K objednávce {cislo} ({nazev}) byla vložena faktura ke schválení."),
            'schvalena_faktura': (f"Faktura u objednávky {cislo} byla schválena",
                                  f"Faktura u objednávky {cislo} ({nazev}) byla schválena."),
            'zamitnuta_faktura': (f"Faktura u objednávky {cislo} byla zamítnuta",
                                  f"Faktura u objednávky {cislo} ({nazev}) byla zamítnuta.\n\n  Důvod: {duvod}\n\nFakturu můžete opravit a odeslat znovu ke schválení."),
            'zadano_ucetnictvi': (f"Faktura k objednávce {cislo} předána do účetnictví",
                                  f"Faktura k objednávce {cislo} ({nazev}) byla předána do účetnictví k zadání."),
        }
        predmet_text = texty.get(udalost)
        if not predmet_text:
            return
        predmet = f"[Intranet] {predmet_text[0]}"
        text = f"Dobrý den,\n\n{predmet_text[1]}\n\nTato zpráva byla odeslána automaticky systémem MojeJIPka."
        try:
            intranet_emaily.odesli_upozorneni_email(email, predmet, text)
        except Exception as e:
            print(f"[Finance] Chyba při odesílání e-mailu žadateli {email}: {e}")
    except Exception as e:
        print(f"[Finance] Chyba v posli_email_zadateli: {e}")

# UTF-8 je nastaveno na úrovni poolu (charset v MySQLConnectionPool), SET NAMES není potřeba
def get_db_utf8():
    return intranet_data.get_db_connection()

def _bez_admina(schv):
    """Aprovia: "Hlavní administrátor" se nikde nesmí objevit mezi schvalovateli (case-insensitive)."""
    if not isinstance(schv, dict): return schv
    return {k: v for k, v in schv.items() if 'administrátor' not in (v or '').lower()}

def get_schvalovatele_faktury_fast():
    now = datetime.datetime.now()
    if SCHVALOVATELE_FAKTURY['last_update'] is None or (now - SCHVALOVATELE_FAKTURY['last_update']).total_seconds() > 28800:
        # {id: jmeno_cele} dict — id jako hodnota pro select, jméno jako label
        schv = intranet_data.ziskej_uzivatele_s_pravem(
            'faktury_seznam_schvalit', 'faktury_admin', 'vse', pouze_jmena=False
        )
        schv = _bez_admina(schv)
        SCHVALOVATELE_FAKTURY['data'] = dict(sorted(schv.items(), key=lambda x: x[1])) if schv else {}
        SCHVALOVATELE_FAKTURY['last_update'] = now
    return SCHVALOVATELE_FAKTURY['data']

def ziskej_nakup_schvalovatele_live():
    # Jeden SQL dotaz místo iterace přes všechny uživatele (a kontroly pouze přímých práv)
    schv = intranet_data.ziskej_uzivatele_s_pravem('nakup_schvalit', 'nakup_admin', 'vse')
    return _bez_admina(schv)

def nacti_vsechny_faktury_rychle():
    now = datetime.datetime.now()
    if CACHE_FAKTURY['last_update'] is None or (now - CACHE_FAKTURY['last_update']).total_seconds() > 60:
        conn = get_db_utf8()
        if conn:
            cursor = None
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute("SELECT * FROM faktury ORDER BY datum_zadani DESC LIMIT 2000")
                res = cursor.fetchall()
                for row in res:
                    if row.get('datum_zadani'): row['datum_zadani'] = row['datum_zadani'].strftime('%Y-%m-%d %H:%M')
                    if row.get('datum_schvaleni'): row['datum_schvaleni'] = row['datum_schvaleni'].strftime('%Y-%m-%d %H:%M')
                    if 'castka' in row: row['castka'] = float(row['castka'] or 0)
                CACHE_FAKTURY['data'] = res
            except Exception as e:
                print(f"Chyba načtení faktur: {e}")
                CACHE_FAKTURY['data'] = []
            finally:
                if cursor: cursor.close()
                if conn: conn.close()
        else:
            CACHE_FAKTURY['data'] = []
        CACHE_FAKTURY['last_update'] = now
    return CACHE_FAKTURY['data']

def nacti_vsechny_nakupy_rychle():
    now = datetime.datetime.now()
    if CACHE_NAKUP['last_update'] is None or (now - CACHE_NAKUP['last_update']).total_seconds() > 60:
        conn = get_db_utf8()
        if conn:
            cur = None
            try:
                cur = conn.cursor(dictionary=True)
                cur.execute("SELECT * FROM nakup_proces ORDER BY vytvoreno DESC LIMIT 2000")
                CACHE_NAKUP['data'] = cur.fetchall()
                CACHE_NAKUP['last_update'] = now
            except Exception:
                CACHE_NAKUP['data'] = []
            finally:
                if cur: cur.close()
                if conn: conn.close()
        else: CACHE_NAKUP['data'] = []
    return CACHE_NAKUP['data']

# ==========================================
# 🔔 VÝPOČET PUNTÍKU PRO HLAVNÍ MENU INTRANETU
# ==========================================
def ziskej_badge_pocet_rychle(user_id, user_name, vsechna_prava):
    """COUNT(*) dotazy místo načítání 2000 řádků — pro číslo v menu po přihlášení.
    Cache 5 minut; invaliduje se voláním vynut_obnovu_faktur() / vynut_obnovu_nakupu()."""
    now = datetime.datetime.now()
    if CACHE_BADGE['last_update'] is not None and (now - CACHE_BADGE['last_update']).total_seconds() < 300:
        return CACHE_BADGE['hodnota']
    try:
        inicializace_financi_db()
        is_admin_n = 'vse' in vsechna_prava
        is_admin_f = 'vse' in vsechna_prava
        muze_schv_n = is_admin_n or 'nakup_schvalit' in vsechna_prava
        muze_schv_f = is_admin_f or 'faktury_seznam_schvalit' in vsechna_prava
        celkem = 0
        conn = get_db_utf8()
        if not conn:
            return 0
        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            if muze_schv_n:
                if is_admin_n:
                    cur.execute("SELECT COUNT(*) AS cnt FROM nakup_proces WHERE stav = 'Čeká na schválení'")
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM nakup_proces WHERE stav = 'Čeká na schválení' "
                        "AND (schvalovatel_1 = %s OR schvalovatel_2 = %s OR schvalovatel_3 = %s)",
                        (user_id, user_id, user_id)
                    )
                celkem += (cur.fetchone() or {}).get('cnt', 0)
            if muze_schv_f:
                if is_admin_f:
                    cur.execute("SELECT COUNT(*) AS cnt FROM faktury WHERE stav IN ('Čeká', 'Ke schválení')")
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM faktury WHERE stav IN ('Čeká', 'Ke schválení') "
                        "AND (schvalovatel_id_1 = %s OR schvalovatel_id_2 = %s OR schvalovatel_id_3 = %s "
                        "     OR schvalovatel = %s OR schvalovatel_2 = %s OR schvalovatel_3 = %s)",
                        (user_id, user_id, user_id, user_name, user_name, user_name)
                    )
                celkem += (cur.fetchone() or {}).get('cnt', 0)
        finally:
            if cur: cur.close()
            conn.close()
        CACHE_BADGE['hodnota'] = celkem
        CACHE_BADGE['last_update'] = now
        return celkem
    except Exception as e:
        print(f"[badge] Chyba výpočtu badge: {e}")
        return 0

def ziskej_celkovy_pocet_ke_schvaleni(user_id, user_name, vsechna_prava):
    try:
        inicializace_financi_db()

        is_admin_n = 'vse' in vsechna_prava
        is_admin_f = 'vse' in vsechna_prava
        muze_schv_n = is_admin_n or 'nakup_schvalit' in vsechna_prava
        muze_schv_f = is_admin_f or 'faktury_seznam_schvalit' in vsechna_prava

        celkem = 0
        if muze_schv_n:
            nakupy = nacti_vsechny_nakupy_rychle()
            celkem += sum(1 for d in nakupy if d['stav'] == 'Čeká na schválení' and (is_admin_n or user_id in (d['schvalovatel_1'], d['schvalovatel_2'], d['schvalovatel_3'])))
        if muze_schv_f:
            faktury = nacti_vsechny_faktury_rychle()
            celkem += sum(1 for f in faktury if f.get('stav') in ['Čeká', 'Ke schválení'] and (is_admin_f or user_id in [f.get('schvalovatel_id_1'), f.get('schvalovatel_id_2'), f.get('schvalovatel_id_3')] or user_name in [f.get('schvalovatel'), f.get('schvalovatel_2'), f.get('schvalovatel_3')]))
        return celkem
    except Exception as e:
        print(f"Ochrana proti pádu menu při výpočtu notifikací financí: {e}")
        return 0

# ==========================================
# 🛠 POMOCNÉ FUNKCE A INICIALIZACE DB
# ==========================================
def vynut_pouze_cisla(e):
    if e.value:
        ciste = ''.join(c for c in str(e.value) if c.isdigit() or c in ' ,.-')
        if ciste != str(e.value):
            e.sender.value = ciste

def vynut_pouze_integer(e):
    # Povolí pouze číslice (datový typ integer) — bez mezer, čárek, teček.
    if e.value:
        ciste = ''.join(c for c in str(e.value) if c.isdigit())
        if ciste != str(e.value):
            e.sender.value = ciste

def zaved_formatovani_castky(prvek):
    prvek.classes('jen-castka')  # client-side blok znaků (jen číslice + mezera/čárka/tečka)
    def on_blur(e, el=prvek):
        v = parse_czk(el.value)
        if v > 0: el.value = f"{v:,.2f}".replace(',', ' ').replace('.', ',')
        else: el.value = '0' if v == 0 else ''
    prvek.on('blur', on_blur)
    prvek.on_value_change(vynut_pouze_cisla)

def propoj_castky_celkem(vstupy: list, celkem):
    """Napojí automatický výpočet součtu DPH základů → pole celkem (read-only)."""
    celkem.props(add='readonly')
    celkem.classes(remove='border-blue-400', add='border-blue-300 bg-blue-50 cursor-default')

    def prepocitej(_=None):
        total = sum(parse_czk(v.value) for v in vstupy)
        celkem.value = f"{total:,.2f}".replace(',', ' ').replace('.', ',') if total != 0 else "0"

    for v in vstupy:
        v.on_value_change(prepocitej)
    prepocitej()

def proved_automaticky_export_faktur(manualni_datum=None):
    try:
        nyni = datetime.datetime.now()
        cilove_datum = manualni_datum if manualni_datum else (nyni - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

        faktury_vsechny = nacti_vsechny_faktury_rychle()
        k_exportu = [
            f for f in faktury_vsechny
            if f.get('stav') == 'Schváleno'
            and str(f.get('datum_schvaleni', '')).startswith(cilove_datum)
        ]

        if not k_exportu:
            return False, "Žádná data pro export."

        # Cílová složka — respektuje nastavení, jinak výchozí EXPORT_DIR
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
        slozka = nastaveni.get('faktury_export_slozka', '').strip() or EXPORT_DIR
        os.makedirs(slozka, exist_ok=True)

        jmeno_zipu = f"Export_Schvalenych_Faktur_{cilove_datum}.zip"
        cesta_zip = os.path.join(slozka, jmeno_zipu)

        with zipfile.ZipFile(cesta_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in k_exportu:
                zdrojovy_soubor = f.get('soubor_schvaleny') or f.get('soubor_original')
                if zdrojovy_soubor and os.path.exists(zdrojovy_soubor):
                    nazev_souboru = f"{f.get('dodavatel', 'Neznamy')}_{f.get('cislo_faktury', 'Nezname')}.pdf"
                    nazev_souboru = "".join(c for c in nazev_souboru if c.isalnum() or c in " ._-").strip()
                    zf.write(zdrojovy_soubor, arcname=nazev_souboru)

        return True, f"Export uložen: {cesta_zip}"

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Chyba při automatickém exportu faktur: {e}")
        return False, f"Chyba: {e}"

# ==========================================
# ⏰ BACKGROUND SMYČKA: AUTOMATICKÁ ZÁLOHA FAKTUR
# ==========================================
async def bg_faktury_export():
    """Každou minutu zkontroluje, zda nastal nastavený čas exportu faktur."""
    _DNY = ['Pondělí', 'Úterý', 'Středa', 'Čtvrtek', 'Pátek', 'Sobota', 'Neděle']
    posledni_spusteni = None
    await asyncio.sleep(60)
    while True:
        try:
            nastaveni = intranet_data.nacti_nastaveni_intranetu()
            if not nastaveni.get('faktury_export_zapnuty', False):
                await asyncio.sleep(60)
                continue

            cas_str = nastaveni.get('faktury_export_cas', '').strip()
            if not cas_str:
                await asyncio.sleep(60)
                continue

            nyni = datetime.datetime.now()
            try:
                cas = datetime.datetime.strptime(cas_str, '%H:%M').time()
            except ValueError:
                await asyncio.sleep(60)
                continue

            if not (nyni.hour == cas.hour and nyni.minute == cas.minute):
                await asyncio.sleep(60)
                continue

            if posledni_spusteni == nyni.date():
                await asyncio.sleep(60)
                continue

            frekvence = nastaveni.get('faktury_export_frekvence', 'Denně')
            spustit = False
            if frekvence == 'Denně':
                spustit = True
            elif frekvence == 'Týdně':
                den = nastaveni.get('faktury_export_den_tydne', 'Pondělí')
                if den in _DNY and nyni.weekday() == _DNY.index(den):
                    spustit = True
            elif frekvence == 'Měsíčně':
                den_m = int(nastaveni.get('faktury_export_den_mesice', 1))
                if nyni.day == den_m:
                    spustit = True

            if spustit:
                posledni_spusteni = nyni.date()
                status, msg = await asyncio.to_thread(proved_automaticky_export_faktur)
                stav = "OK" if status else "CHYBA"
                print(f"[{nyni.strftime('%H:%M:%S')}] Automatická záloha faktur [{stav}]: {msg}")
                intranet_logger.log_activity("Systém", "Autom. záloha faktur", f"[{stav}] {msg}")
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Chyba bg_faktury_export: {e}")
        await asyncio.sleep(60)

# Automatický export faktur není součástí dokumentu — background úloha se nespouští.

def inicializace_financi_db():
    global _FINANCE_DB_INICIALIZOVANA
    if _FINANCE_DB_INICIALIZOVANA: return
    # Aprovia: tabulka faktur + ADITIVNÍ migrace starých práv na 3 role
    # (faktury_uzivatel→nakup_uzivatel, nakup_admin→nakup_schvalit/nakup_uzivatel).
    # Současné UI legacy funkce uloz_fakturu/ziskej_vsechny_faktury nevolá, proto
    # migraci spouštíme zde — jinak držitelé starých práv neuvidí dlaždici Aprovia.
    try:
        intranet_data.inicializace_faktur_db()
        intranet_data.vymazat_cache_prav()  # migrace mohla přidat práva → ať se přečtou hned
    except Exception as _e: print(f"[Finance] inicializace_faktur_db: {_e}")
    conn = get_db_utf8()
    if not conn: return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nakup_proces (
                id INT AUTO_INCREMENT PRIMARY KEY, cislo_akce VARCHAR(20) UNIQUE, typ VARCHAR(20), nazev_akce VARCHAR(255),
                user_id INT, zadavatel VARCHAR(100), dodavatel VARCHAR(255), misto_plneni VARCHAR(255), polozky TEXT, doplnujici_info TEXT,
                reakce_dodavatele TEXT NULL, termin_zadani DATE, termin_dodani DATE, realny_termin_dokonceni DATE NULL,
                stav VARCHAR(100) DEFAULT 'Rozpracováno', schvalovatel_1 INT NULL, schvaleno_1 DATETIME NULL, schvalovatel_2 INT NULL,
                schvaleno_2 DATETIME NULL, schvalovatel_3 INT NULL, schvaleno_3 DATETIME NULL, soubor_priloha VARCHAR(500) DEFAULT '',
                duvod_zamitnuti VARCHAR(255) DEFAULT '', opakena_zadost BOOLEAN DEFAULT FALSE, odeslano_dodavateli DATETIME NULL,
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        cur.execute("CREATE TABLE IF NOT EXISTS nakup_logy (id INT AUTO_INCREMENT PRIMARY KEY, akce_id INT, uzivatel VARCHAR(100), text TEXT, datum DATETIME DEFAULT CURRENT_TIMESTAMP) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

        # Přidání rozpadu DPH a platby do tabulky nákupů
        nove_sloupce_nakup = [
            ("soubor_priloha", "VARCHAR(500) DEFAULT ''"),
            ("castka_21", "DECIMAL(12,2) DEFAULT 0.00"),
            ("castka_12", "DECIMAL(12,2) DEFAULT 0.00"),
            ("castka_0", "DECIMAL(12,2) DEFAULT 0.00"),
            ("castka_celkem", "DECIMAL(12,2) DEFAULT 0.00"),
            ("platba", "VARCHAR(50) DEFAULT 'Fakturou'"),
            ("korekce_ceny_puvodni", "DECIMAL(12,2) DEFAULT NULL"),
            ("korekce_ceny_castka", "DECIMAL(12,2) DEFAULT NULL"),
            ("korekce_ceny_duvod", "TEXT DEFAULT NULL"),
            ("korekce_ceny_datum", "DATETIME DEFAULT NULL"),
            ("korekce_ceny_uzivatel", "VARCHAR(100) DEFAULT NULL"),
        ]
        for col, definice in nove_sloupce_nakup:
            try: cur.execute(f"ALTER TABLE nakup_proces ADD COLUMN {col} {definice}")
            except Exception: pass

        # Faktury alter tabulky
        nove_sloupce_faktury = [
            ("cislo_objednavky", "VARCHAR(100) DEFAULT ''"), ("castka_21", "DECIMAL(12,2) DEFAULT 0.00"), ("castka_12", "DECIMAL(12,2) DEFAULT 0.00"),
            ("castka_0", "DECIMAL(12,2) DEFAULT 0.00"), ("schvalovatel_2", "VARCHAR(100) DEFAULT ''"), ("schvalovatel_3", "VARCHAR(100) DEFAULT ''"),
            ("schvaleno_1", "DATETIME NULL"), ("schvaleno_2", "DATETIME NULL"), ("schvaleno_3", "DATETIME NULL"),
            ("duvod_zamitnuti", "VARCHAR(255) DEFAULT ''"), ("opakena_zadost", "BOOLEAN DEFAULT FALSE"),
            ("dic", "VARCHAR(50) DEFAULT ''"), ("bankovni_ucet", "VARCHAR(100) DEFAULT ''"), ("datum_vystaveni", "DATE NULL"), ("platba", "VARCHAR(50) DEFAULT 'Převodem'"),
            # 3NF: FK sloupce pro zadavatele a schvalovatele
            ("zadavatel_id", "INT NULL"), ("schvalovatel_id_1", "INT NULL"), ("schvalovatel_id_2", "INT NULL"), ("schvalovatel_id_3", "INT NULL"),
        ]
        for col, definice in nove_sloupce_faktury:
            try: cur.execute(f"ALTER TABLE faktury ADD COLUMN {col} {definice}")
            except Exception: pass

        # 3NF migrace: naplnit ID sloupce z existujících jmenných hodnot
        try: cur.execute("UPDATE faktury f INNER JOIN user u ON CONCAT(u.name, ' ', u.surname) = f.zadavatel SET f.zadavatel_id = u.iduser WHERE f.zadavatel_id IS NULL AND f.zadavatel IS NOT NULL AND f.zadavatel != ''")
        except Exception: pass
        try: cur.execute("UPDATE faktury f INNER JOIN user u ON CONCAT(u.name, ' ', u.surname) = f.schvalovatel SET f.schvalovatel_id_1 = u.iduser WHERE f.schvalovatel_id_1 IS NULL AND f.schvalovatel IS NOT NULL AND f.schvalovatel != ''")
        except Exception: pass
        try: cur.execute("UPDATE faktury f INNER JOIN user u ON CONCAT(u.name, ' ', u.surname) = f.schvalovatel_2 SET f.schvalovatel_id_2 = u.iduser WHERE f.schvalovatel_id_2 IS NULL AND f.schvalovatel_2 IS NOT NULL AND f.schvalovatel_2 != ''")
        except Exception: pass
        try: cur.execute("UPDATE faktury f INNER JOIN user u ON CONCAT(u.name, ' ', u.surname) = f.schvalovatel_3 SET f.schvalovatel_id_3 = u.iduser WHERE f.schvalovatel_id_3 IS NULL AND f.schvalovatel_3 IS NOT NULL AND f.schvalovatel_3 != ''")
        except Exception: pass
        # Naplnit dodavatel_firma z existujících faktur
        try: cur.execute("INSERT IGNORE INTO dodavatel_firma (ico, nazev) SELECT DISTINCT ico, dodavatel FROM faktury WHERE ico IS NOT NULL AND ico != '' AND dodavatel IS NOT NULL AND dodavatel != ''")
        except Exception: pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS predpoptavky (
                id INT AUTO_INCREMENT PRIMARY KEY,
                cislo VARCHAR(20) UNIQUE,
                nazev VARCHAR(255) NOT NULL,
                oddeleni VARCHAR(100) DEFAULT '',
                kategorie VARCHAR(100) DEFAULT '',
                mnozstvi VARCHAR(50) DEFAULT '',
                jednotka VARCHAR(50) DEFAULT 'ks',
                cena_odhad DECIMAL(12,2) DEFAULT NULL,
                potreba_do DATE DEFAULT NULL,
                oduvodneni TEXT,
                soubor_priloha VARCHAR(500) DEFAULT '',
                zadavatel_id INT,
                zadavatel_jmeno VARCHAR(200) DEFAULT '',
                schvalovatel_id INT DEFAULT NULL,
                stav VARCHAR(50) DEFAULT 'Koncept',
                komentar_schvalovatele TEXT,
                datum_vytvoreni DATETIME DEFAULT CURRENT_TIMESTAMP,
                datum_zmeny DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                poptavka_cislo VARCHAR(20) DEFAULT ''
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)

        conn.commit()
        _FINANCE_DB_INICIALIZOVANA = True
    except Exception: pass
    finally:
        if cur: cur.close()
        if conn: conn.close()

def generuj_cislo_akce():
    conn = get_db_utf8()
    if not conn: return f"{datetime.datetime.now().year}99999"
    cur = None
    try:
        # Z-3: Použijeme transakci s FOR UPDATE pro atomické generování čísla zakázky
        conn.autocommit = False
        cur = conn.cursor()
        rok = datetime.datetime.now().year
        cur.execute("SELECT MAX(cislo_akce) FROM nakup_proces WHERE cislo_akce LIKE %s FOR UPDATE", (str(rok) + '%',))
        m = cur.fetchone()[0]
        cislo = str(int(m) + 1) if m else f"{rok}00001"
        conn.commit()
        return cislo
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return f"{datetime.datetime.now().year}99999"
    finally:
        if cur: cur.close()
        if conn:
            try:
                conn.autocommit = True
            except Exception:
                pass
            conn.close()

def generuj_cislo_predpoptavky():
    conn = get_db_utf8()
    if not conn: return f"{datetime.datetime.now().year}99999"
    cur = None
    try:
        cur = conn.cursor()
        rok = datetime.datetime.now().year
        cur.execute("SELECT MAX(CAST(cislo AS UNSIGNED)) FROM predpoptavky WHERE cislo LIKE %s", (str(rok) + '%',))
        m = cur.fetchone()[0]
        return str(int(m) + 1) if m else f"{rok}00001"
    except Exception: return f"{datetime.datetime.now().year}99999"
    finally:
        if cur: cur.close()
        if conn: conn.close()

def nacti_predpoptavky_cache():
    now = datetime.datetime.now()
    if CACHE_PREDPOPTAVKY['last_update'] is None or (now - CACHE_PREDPOPTAVKY['last_update']).total_seconds() > 30:
        conn = get_db_utf8()
        if conn:
            cur = None
            try:
                cur = conn.cursor(dictionary=True)
                cur.execute("""SELECT p.*, CONCAT(u.name,' ',u.surname) AS schvalovatel_jmeno
                               FROM predpoptavky p LEFT JOIN user u ON p.schvalovatel_id=u.iduser
                               ORDER BY p.datum_vytvoreni DESC LIMIT 1000""")
                data = cur.fetchall()
                for row in data:
                    for col in ['datum_vytvoreni','datum_zmeny']:
                        if row.get(col): row[col] = row[col].strftime('%d.%m.%Y %H:%M')
                    if row.get('potreba_do'): row['potreba_do'] = str(row['potreba_do'])
                    if row.get('cena_odhad') is not None: row['cena_odhad'] = float(row['cena_odhad'] or 0)
                CACHE_PREDPOPTAVKY['data'] = data
            except Exception as e:
                print(f"Chyba načtení předpoptávek: {e}")
                CACHE_PREDPOPTAVKY['data'] = []
            finally:
                if cur: cur.close()
                if conn: conn.close()
        else: CACHE_PREDPOPTAVKY['data'] = []
        CACHE_PREDPOPTAVKY['last_update'] = now
    return CACHE_PREDPOPTAVKY['data']

def _posli_email_pp(prijemce_id, predmet_suffix, cislo, nazev, zadavatel, extra=''):
    try:
        conn = get_db_utf8()
        if not conn: return
        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT email FROM user WHERE iduser = %s", (prijemce_id,))
            row = cur.fetchone()
            if not row or not row.get('email'): return
            email = row['email']
        finally:
            if cur: cur.close()
            if conn: conn.close()
        predmet = f"[Intranet] Předpoptávka PPQ-{cislo}: {predmet_suffix}"
        text = (f"Dobrý den,\n\nPředpoptávka PPQ-{cislo} — {nazev}\nZadavatel: {zadavatel}\n\n"
                f"{extra}\nPřihlaste se do intranetu pro zobrazení detailu.\n\nMoje JIPka")
        intranet_emaily.odesli_upozorneni_email(email, predmet, text)
    except Exception as e:
        print(f"[PP email] Chyba: {e}")

def pridej_log_nakup(akce_id, uzivatel, text):
    conn = get_db_utf8()
    if conn:
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO nakup_logy (akce_id, uzivatel, text) VALUES (%s, %s, %s)", (akce_id, uzivatel, text))
            conn.commit()
        except Exception: pass
        finally:
            if cur: cur.close()
            if conn: conn.close()

def vygeneruj_tiskove_pdf_nakup(d):
    # ZJIŠTĚNÍ SPRÁVNÉHO TYPU DOKUMENTU PŘÍMO PRO HLAVIČKU
    typ_dokumentu = d.get('typ', 'Poptávka')

    # Formátování data vystavení
    datum_vystaveni = d.get('termin_zadani', '')
    if isinstance(datum_vystaveni, datetime.date) or isinstance(datum_vystaveni, datetime.datetime):
        datum_vystaveni = datum_vystaveni.strftime('%d.%m.%Y')

    # Přidání standardní věty u objednávky
    doplnujici_text = str(d.get('doplnujici_info', '')).replace(chr(10), '<br>')
    if typ_dokumentu == 'Objednávka':
        if doplnujici_text: doplnujici_text += "<br><br>"
        doplnujici_text += "<strong>Prosíme o písemné potvrzení přijetí této objednávky.</strong>"

    html = f"""
    <html>
    <head>
        <meta charset='utf-8'>
        <title>{typ_dokumentu} {d.get('cislo_akce', '')}</title>
        <style>
            body {{font-family: Arial, sans-serif; padding: 40px; color: #333;}} 
            .header {{display: flex; justify-content: space-between; border-bottom: 2px solid #E30613; padding-bottom: 20px;}} 
            .info-box {{background: #f9fafb; padding: 15px; border-radius: 8px; margin: 20px 0;}} 
            .items {{width: 100%; border-collapse: collapse;}} 
            .items th, .items td {{border: 1px solid #ddd; padding: 12px;}} 
            .items th {{background: #2A3547; color: white;}}
        </style>
    </head>
    <body onload='window.print()'>
        <div class='header'>
            <div>
                <h1>{typ_dokumentu.upper()}</h1>
                <p>Číslo: <strong>{d.get('cislo_akce', '')}</strong></p>
                <p>Vystaveno: <strong>{datum_vystaveni}</strong></p>
            </div>
            <div style='text-align: right;'>
                <h2 style='margin-bottom: 5px;'>JIP Východočeská a.s.</h2>
                <p style='margin-top: 0; color: #555;'>
                    Hradišťská 407<br>
                    533 52 Staré Hradiště<br>
                    Česká republika<br>
                    IČ: 27464822
                </p>
                <br>
                <p>Zadavatel: {d.get('zadavatel', '')}</p>
            </div>
        </div>
        <div class='info-box'>
            <p><strong>Dodavatel:</strong> {d.get('dodavatel', '')}</p>
            <p><strong>Název akce:</strong> {d.get('nazev_akce', '')}</p>
            <p><strong>Místo plnění:</strong> {d.get('misto_plneni', '')}</p>
            <p><strong>Termín dodání / realizace:</strong> {d.get('termin_dodani') or 'Dle dohody'}</p>
            <p><strong>Platební podmínky:</strong> {d.get('platba', 'Fakturou')}</p>
        </div>
        <h3>Specifikace požadavku:</h3>
        <table class='items'>
            <tr><th>Položky</th></tr>
            <tr><td>{str(d.get('polozky', '')).replace(chr(10), '<br>')}</td></tr>
        </table>
        <br>
        <p><strong>Doplňující informace:</strong><br>{doplnujici_text}</p>
    </body>
    </html>
    """

    # UNIKÁTNÍ NÁZEV PROTI VYROVNÁVACÍ PAMĚTI PROHLÍŽEČE
    prefix = "OBJ" if typ_dokumentu == 'Objednávka' else "POP"
    jmeno_souboru = f"Tisk_{prefix}_{d.get('cislo_akce', 'neznama')}.html"
    cesta = os.path.join(TISK_DIR, jmeno_souboru)

    with open(cesta, "w", encoding="utf-8") as f:
        f.write(html)

    timestamp = int(datetime.datetime.now().timestamp())
    return f"/tisk_nakup/{jmeno_souboru}?v={timestamp}"

def render_semafor(stav, is_faktura=False, z_data=None):
    if is_faktura:
        ma_pp = False
        kroky = ['Zadáno', 'Schvalování', 'Účetnictví (Hotovo)']
        idx = 0
        if stav in ['Ke schválení', 'Čeká', 'Zamítnuto']: idx = 1
        elif stav == 'Schváleno': idx = 2
    else:
        ma_pp = False
        kroky = ['Koncept', 'Schvalování', 'Schváleno', 'Faktura', 'Účetnictví']
        idx = 0
        if stav in ['Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení']: idx = 0
        elif stav in ['Čeká na schválení', 'Zamítnuto']: idx = 1
        elif stav == 'Schváleno': idx = 2
        elif stav == 'Faktura schválena': idx = 3
        elif stav == 'Uzavřeno': idx = 4

    def ukaz_detail_kroku(krok_nazev, krok_idx):
        if not z_data: return
        with ui.dialog() as dlg_krok, ui.card().classes('w-full max-w-2xl p-6 rounded-xl bg-white max-h-[85vh] overflow-y-auto'):
            ui.label(f'Detail fáze: {krok_nazev}').classes('text-2xl font-bold mb-4 text-blue-900 border-b pb-2 w-full')

            if is_faktura:
                if krok_idx == 0:
                    ui.label(f"Zadáno uživatelem: {z_data.get('zadavatel', '')}").classes('font-bold text-gray-700')
                    ui.label(f"Datum zadání: {z_data.get('datum_zadani', '')}").classes('text-gray-600')
                    if z_data.get('soubor_original'):
                        ui.button('Zobrazit nahranou fakturu (PDF)', icon='attach_file', on_click=lambda: ui.navigate.to(f"/faktury_soubory/{urllib.parse.quote(os.path.basename(z_data['soubor_original']))}", new_tab=True)).classes('mt-4 bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold w-full h-12 shadow-sm rounded-xl')

                elif krok_idx == 1:
                    ui.label('Průběh schvalování:').classes('font-bold text-gray-700 mb-2')
                    for i, c_nm, c_tm in [(1, 'schvalovatel', 'schvaleno_1'), (2, 'schvalovatel_2', 'schvaleno_2'), (3, 'schvalovatel_3', 'schvaleno_3')]:
                        jm = z_data.get(c_nm)
                        if jm:
                            cas = z_data.get(c_tm)
                            if cas: ui.label(f"✅ {jm} (Schváleno: {cas})").classes('text-green-600 font-bold mb-1 text-sm')
                            else: ui.label(f"⏳ {jm} (Čeká se)").classes('text-orange-600 font-bold mb-1 text-sm')
                    if z_data.get('duvod_zamitnuti'):
                        ui.label(f"ZAMÍTNUTO: {z_data.get('duvod_zamitnuti')}").classes('text-red-600 font-bold mt-4 bg-red-50 p-3 rounded w-full border border-red-200')

                elif krok_idx == 2:
                    if z_data.get('stav') == 'Schváleno':
                        ui.label(f"Schváleno a uzavřeno dne: {z_data.get('datum_schvaleni', '')}").classes('font-bold text-green-700 text-lg')
                        if z_data.get('soubor_schvaleny'):
                            ui.button('Otevřít ORAZÍTKOVANOU fakturu (PDF)', icon='verified', on_click=lambda: ui.navigate.to(f"/faktury_soubory/{urllib.parse.quote(os.path.basename(z_data['soubor_schvaleny']))}", new_tab=True)).classes('mt-4 bg-green-600 hover:bg-green-700 text-white font-bold w-full h-14 shadow-lg rounded-xl')
                    else:
                        ui.label('Zatím nebylo schváleno.').classes('text-gray-500 italic')

            else: # NÁKUP
                eff_idx = krok_idx - 1 if ma_pp else krok_idx

                if krok_idx == 0 and ma_pp:
                    # --- Krok Předpoptávka ---
                    pp_info = str(z_data.get('doplnujici_info', ''))
                    ui.label('Původ z předpoptávky').classes('font-bold text-purple-800 text-lg mb-3')
                    for radek in [r.strip() for r in pp_info.split('\n') if r.strip()]:
                        if radek.startswith('Zdroj předpoptávky:'):
                            val = radek.replace('Zdroj předpoptávky:', '').strip()
                            ui.label(val).classes('font-bold text-purple-700 bg-purple-50 p-3 rounded-lg w-full border border-purple-200 mb-2 text-sm')
                        else:
                            ui.label(radek).classes('text-sm text-gray-700 bg-gray-50 p-2 rounded w-full border border-gray-100 mb-1')

                elif eff_idx == 0:
                    ui.label(f"Založil(a): {z_data.get('zadavatel', '')}").classes('font-bold text-gray-800 text-lg mb-4')
                    ui.label('Požadované položky:').classes('text-xs text-gray-500 font-bold uppercase tracking-wider')
                    ui.label(z_data.get('polozky', '')).classes('bg-gray-50 p-4 rounded-lg w-full whitespace-pre-wrap text-sm mb-4 border border-gray-200')

                    if z_data.get('doplnujici_info') and not ma_pp:
                        ui.label('Doplňující informace zadavatele:').classes('text-xs text-gray-500 font-bold uppercase tracking-wider')
                        ui.label(z_data.get('doplnujici_info')).classes('bg-blue-50 p-3 rounded-lg w-full text-sm mb-4 text-blue-800 border border-blue-100 whitespace-pre-wrap')

                    if z_data.get('soubor_priloha'):
                        ui.button('Otevřít původní přiložený soubor / nabídku', icon='attach_file', on_click=lambda: ui.navigate.to(f"/prilohy_nakup/{urllib.parse.quote(os.path.basename(z_data['soubor_priloha']))}", new_tab=True)).classes('bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold w-full h-12 shadow-sm rounded-xl mt-2')

                elif eff_idx == 1:
                    ui.label('Průběh schvalování:').classes('font-bold text-gray-700 mb-4 text-lg')
                    live_schv = ziskej_nakup_schvalovatele_live()
                    for i in range(1, 4):
                        sid = z_data.get(f'schvalovatel_{i}')
                        if sid:
                            cas = z_data.get(f'schvaleno_{i}')
                            jm = live_schv.get(sid, 'Neznámý')
                            if cas: ui.label(f"✅ {jm} (Schváleno: {cas.strftime('%d.%m.%Y %H:%M') if isinstance(cas, datetime.datetime) else cas})").classes('text-green-600 font-bold mb-2 text-sm')
                            else: ui.label(f"⏳ {jm} (Čeká se)").classes('text-orange-600 font-bold mb-2 text-sm')

                    if z_data.get('duvod_zamitnuti'):
                        ui.label(f"ZAMÍTNUTO: {z_data.get('duvod_zamitnuti')}").classes('text-red-600 font-bold mt-4 bg-red-50 p-3 rounded w-full border border-red-200')

                elif eff_idx == 2:
                    if z_data.get('odeslano_dodavateli'):
                        ui.label(f"Odesláno dodavateli dne: {z_data['odeslano_dodavateli'].strftime('%d.%m.%Y %H:%M') if isinstance(z_data['odeslano_dodavateli'], datetime.datetime) else z_data['odeslano_dodavateli']}").classes('font-bold text-blue-700 text-lg mb-4')
                    else:
                        ui.label('Nebylo zaznamenáno přesné datum odeslání dodavateli.').classes('text-gray-500 italic mb-4')

                    if z_data.get('reakce_dodavatele'):
                        ui.label('Vyjádření dodavatele / Poznámky:').classes('text-xs text-gray-500 font-bold uppercase tracking-wider')
                        ui.label(z_data.get('reakce_dodavatele')).classes('bg-yellow-50 p-4 rounded-lg w-full whitespace-pre-wrap text-sm border border-yellow-200 mt-1')
                    else:
                        ui.label('Žádné poznámky k dodavateli nebyly vyplněny.').classes('text-gray-500 text-sm')

                elif eff_idx == 3:
                    if z_data.get('realny_termin_dokonceni'):
                        ui.label(f"Zboží / Služba dodána dne: {z_data['realny_termin_dokonceni'].strftime('%d.%m.%Y') if isinstance(z_data['realny_termin_dokonceni'], (datetime.datetime, datetime.date)) else z_data['realny_termin_dokonceni']}").classes('font-black text-purple-700 text-xl')
                    else:
                        ui.label('Zatím nebylo v systému označeno jako fyzicky dodáno.').classes('text-gray-500 italic')

                elif eff_idx == 4:
                    faktury = nacti_vsechny_faktury_rychle()
                    nalezena_fa = next((f for f in faktury if f.get('cislo_objednavky') == z_data['cislo_akce']), None)

                    if z_data.get('stav') == 'Uzavřeno':
                        ui.label('Nákupní proces je kompletně uzavřen.').classes('font-black text-gray-800 text-xl mb-4')
                        if nalezena_fa:
                            with ui.card().classes('w-full p-4 bg-green-50 border border-green-200 shadow-sm'):
                                ui.label('Spárovaná faktura nalezena:').classes('text-xs text-green-700 font-bold uppercase tracking-wider mb-1')
                                ui.label(f"Číslo faktury: {nalezena_fa.get('cislo_faktury')}").classes('font-bold text-gray-900')
                                ui.label(f"Částka bez DPH: {formatuj_castku(nalezena_fa.get('castka'))} Kč").classes('font-bold text-gray-900')

                                if nalezena_fa.get('soubor_schvaleny'):
                                    ui.button('Otevřít orazítkovanou fakturu', icon='receipt_long', on_click=lambda: ui.navigate.to(f"/faktury_soubory/{urllib.parse.quote(os.path.basename(nalezena_fa['soubor_schvaleny']))}", new_tab=True)).classes('mt-4 bg-green-600 hover:bg-green-700 text-white font-bold w-full h-12 rounded-xl')
                        else:
                            ui.label('Faktura spárovaná s tímto nákupem nebyla v databázi nalezena.').classes('text-gray-500 italic')
                    else:
                        if nalezena_fa:
                            ui.label('Čeká se na schválení faktury.').classes('font-black text-orange-500 text-xl mb-4')
                            with ui.card().classes('w-full p-4 bg-orange-50 border border-orange-200 shadow-sm'):
                                ui.label('Zpracovávaná faktura:').classes('text-xs text-orange-700 font-bold uppercase tracking-wider mb-1')
                                ui.label(f"Číslo faktury: {nalezena_fa.get('cislo_faktury')}").classes('font-bold text-gray-900')
                                ui.label(f"Částka bez DPH: {formatuj_castku(nalezena_fa.get('castka'))} Kč").classes('font-bold text-gray-900')

                                if nalezena_fa.get('soubor_original'):
                                    ui.button('Otevřít nahranou fakturu', icon='receipt_long', on_click=lambda: ui.navigate.to(f"/faktury_soubory/{urllib.parse.quote(os.path.basename(nalezena_fa['soubor_original']))}", new_tab=True)).classes('mt-4 bg-orange-500 hover:bg-orange-600 text-white font-bold w-full h-12 rounded-xl')
                        else:
                            ui.label('Nákup čeká na propojení s přijatou fakturou.').classes('text-gray-500 italic')

            ui.button('Zavřít detail', on_click=dlg_krok.close).classes('mt-8 w-full bg-gray-500 hover:bg-gray-600 text-white font-bold h-12 rounded-xl')
        dlg_krok.open()

    with ui.row().classes('w-full justify-between items-center bg-white border border-gray-200 p-4 rounded-xl mb-6 shadow-sm flex-wrap'):
        for i, krok in enumerate(kroky):
            if stav == 'Uzavřeno': color = 'bg-gray-200 text-gray-800 border-gray-400 hover:bg-gray-300'
            elif stav == 'Zamítnuto' and i == 1: color = 'bg-red-600 text-white border-red-700 shadow-[0_0_15px_rgba(220,38,38,0.5)] hover:bg-red-500'
            elif i < idx: color = 'bg-green-500 text-white border-green-600 hover:bg-green-400'
            elif i == idx: color = 'bg-blue-500 text-white border-blue-600 hover:bg-blue-400' if is_faktura else 'bg-red-500 text-white border-red-600 hover:bg-red-400'
            else: color = 'bg-gray-100 text-gray-400 border-gray-200 hover:bg-gray-200'

            with ui.row().classes('items-center gap-2 cursor-pointer transition-transform hover:scale-105').on('click', lambda name=krok, id_k=i: ukaz_detail_kroku(name, id_k)).tooltip(f'Klikněte pro zobrazení detailů fáze: {krok}'):
                ui.label(str(i+1)).classes(f'w-8 h-8 rounded-full flex items-center justify-center font-black border-2 transition-colors {color}')
                ui.label(krok).classes(f"font-bold text-xs sm:text-sm uppercase tracking-wider {'text-gray-800' if i <= idx else 'text-gray-400'}")

            if i < len(kroky) - 1:
                ui.icon('arrow_forward_ios', color='gray-300', size='sm').classes('mx-2 hidden sm:block')

def parse_czk(val):
    try: return float(str(val).replace(' ', '').replace('\xa0', '').replace(',', '.'))
    except: return 0.0

def formatuj_castku(hodnota):
    try: return f"{float(hodnota):,.2f}".replace(',', ' ').replace('.', ',')
    except: return "0,00"

def rychly_format_data(datum_str):
    if not datum_str: return ""
    d_str = str(datum_str).strip()
    if len(d_str) >= 10:
        r, m, d = d_str[0:4], d_str[5:7], d_str[8:10]
        if r.isdigit() and m.isdigit() and d.isdigit(): return f"{d}.{m}.{r}"
    return d_str[:10]

def odstran_diakritiku(text):
    try: return ''.join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
    except: return str(text)

def smaz_fakturu_z_db(f_id):
    conn = get_db_utf8()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT soubor_original, soubor_schvaleny FROM faktury WHERE id = %s", (f_id,))
        row = cursor.fetchone()
        if row:
            for klic in ['soubor_original', 'soubor_schvaleny']:
                if row.get(klic) and os.path.exists(row[klic]):
                    try: os.remove(row[klic])
                    except: pass
        cursor.execute("DELETE FROM faktury WHERE id = %s", (f_id,))
        conn.commit()
        return True
    except: return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def smaz_nakup_z_db(akce_id):
    conn = get_db_utf8()
    if not conn: return False
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT cislo_akce, soubor_priloha FROM nakup_proces WHERE id = %s", (akce_id,))
        row = cursor.fetchone()
        if row and row.get('soubor_priloha') and os.path.exists(row['soubor_priloha']):
            try: os.remove(row['soubor_priloha'])
            except: pass

        # Pokud tato poptávka vznikla z předpoptávky, smazat i předpoptávku
        if row and row.get('cislo_akce'):
            cursor.execute(
                "DELETE FROM predpoptavky WHERE poptavka_cislo=%s",
                (row['cislo_akce'],)
            )
            vynut_obnovu_predpoptavek()

        cursor.execute("DELETE FROM nakup_logy WHERE akce_id = %s", (akce_id,))
        cursor.execute("DELETE FROM nakup_proces WHERE id = %s", (akce_id,))

        conn.commit()
        return True
    except: return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def orazitkuj_pdf(cesta_in, cesta_out, jmeno_schvalovatele, pozice='Vpravo nahoře'):
    doc = None
    try:
        if not HAS_FITZ: shutil.copy(cesta_in, cesta_out); return False
        doc = fitz.open(cesta_in)
        page = doc[0]
        pw, ph = page.rect.width, page.rect.height
        sw, sh = 270, 80; margin = 30
        if pozice == 'Vpravo nahoře': x0, y0 = pw - sw - margin, margin
        elif pozice == 'Vlevo dole': x0, y0 = margin, ph - sh - margin
        elif pozice == 'Vpravo dole': x0, y0 = pw - sw - margin, ph - sh - margin
        else: x0, y0 = margin, margin

        page.draw_rect(fitz.Rect(x0, y0, x0 + sw, y0 + sh), color=(0.8, 0, 0), width=3)
        page.insert_text((x0 + 10, y0 + 30), "SCHVALENO", fontsize=24, color=(0.8, 0, 0), fontname="hebo")
        page.insert_text((x0 + 10, y0 + 55), odstran_diakritiku(f"Schvalil : {jmeno_schvalovatele}"), fontsize=12, color=(0.8, 0, 0), fontname="helv")
        page.insert_text((x0 + 10, y0 + 70), f"Datum : {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}", fontsize=12, color=(0.8, 0, 0), fontname="helv")
        doc.save(cesta_out, garbage=4, deflate=True, clean=True)
        return True
    except:
        try: shutil.copy(cesta_in, cesta_out)
        except: pass
        return False
    finally:
        if doc: doc.close()
        gc.collect()

# ==========================================
# 📊 NOVÁ FUNKCE: CENOVÁ HISTORIE PRODUKTU
# ==========================================
def ukaz_historii_produktu(hledany_produkt):
    hledany = str(hledany_produkt).strip().lower()
    if not hledany:
        ui.notify('Zadejte název produktu pro zobrazení historie.', type='warning')
        return

    vsechny_nakupy = nacti_vsechny_nakupy_rychle()
    nalezene = []

    for n in vsechny_nakupy:
        if n['stav'] in ['Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení', 'Zamítnuto']: continue
        polozky_txt = str(n.get('polozky', ''))
        if hledany in polozky_txt.lower():
            lines = polozky_txt.split('\n')
            match_lines = [l.strip('• ') for l in lines if hledany in l.lower()]
            if match_lines:
                nalezene.append({
                    'datum': n['vytvoreno'],
                    'cislo_akce': n['cislo_akce'],
                    'typ': n['typ'],
                    'dodavatel': n['dodavatel'],
                    'zaznam_polozky': "\n".join(match_lines),
                    'soubor': n.get('soubor_priloha', ''),
                    'popis': n.get('doplnujici_info', ''),
                    'reakce': n.get('reakce_dodavatele', '')
                })

    with ui.dialog() as dlg, ui.card().classes('w-full max-w-4xl p-6 bg-white rounded-xl max-h-[85vh] overflow-y-auto min-w-0'):
        ui.label(f'Cenová historie pro: {hledany_produkt}').classes('text-2xl font-bold mb-4 text-blue-900')
        if not nalezene:
            ui.label('Nebyla nalezena žádná historie schválených nákupů pro tento produkt.').classes('italic text-gray-500')
        else:
            for item in sorted(nalezene, key=lambda x: x['datum'], reverse=True):
                with ui.card().classes('w-full mb-4 p-0 border border-blue-100 overflow-hidden shadow-sm'):
                    with ui.row().classes('w-full bg-blue-50 p-3 justify-between items-center border-b border-blue-100 flex-nowrap'):
                        with ui.column().classes('gap-0 flex-1 min-w-0 pr-2'):
                            ui.label(f"{item['datum'].strftime('%d.%m.%Y')} | {item['dodavatel']}").classes('font-bold text-blue-800 truncate w-full')
                            ui.label(f"{item['typ']} #{item['cislo_akce']}").classes('text-[10px] text-gray-500 uppercase tracking-widest font-bold')

                        if item['soubor']:
                            ui.button('Otevřít přiložený soubor', icon='attach_file', on_click=lambda u=item['soubor']: ui.navigate.to(f"/prilohy_nakup/{urllib.parse.quote(os.path.basename(u))}", new_tab=True)).props('flat dense size=sm').classes('text-blue-600 bg-white shadow-sm px-3 py-1 rounded font-bold shrink-0')

                    with ui.column().classes('w-full p-4 gap-2 min-w-0'):
                        ui.label('Zakoupené položky (odpovídající hledání):').classes('text-[10px] font-bold text-gray-500 uppercase')
                        ui.label(item['zaznam_polozky']).classes('font-bold text-gray-900 bg-white p-2 rounded border border-gray-200 w-full break-words whitespace-normal')

                        if item['reakce']:
                            ui.label('Vyjádření dodavatele:').classes('text-[10px] font-bold text-gray-500 uppercase mt-2')
                            ui.label(item['reakce']).classes('text-sm text-gray-700 bg-yellow-50 p-2 rounded border border-yellow-200 w-full break-words whitespace-normal italic')

                        if item['popis']:
                            ui.label('Doplňující informace:').classes('text-[10px] font-bold text-gray-500 uppercase mt-2')
                            ui.label(item['popis']).classes('text-sm text-gray-600 w-full break-words whitespace-normal')

        ui.button('Zavřít historii', on_click=dlg.close).classes('mt-2 w-full bg-gray-500 hover:bg-gray-600 text-white font-bold h-12 rounded-xl')
    dlg.open()


# ==========================================
# 💼 HLAVNÍ SUPER-MODUL NÁKUP A FAKTURACE
# ==========================================
@refreshable_na_klienta
def vykresli_finance(user_id, user_name, vsechna_prava):
    inicializace_financi_db()

    ma_vse = 'vse' in vsechna_prava
    # Modul Aprovia – 3 role dle dokumentu
    is_admin_n = ma_vse                                                   # superadmin vidí vše
    muze_zadat_n = ma_vse or 'nakup_uzivatel' in vsechna_prava            # Uživatel (zakládá objednávky, nahrává faktury)
    muze_schv_n = ma_vse or 'nakup_schvalit' in vsechna_prava             # Schvalovatel objednávek

    is_admin_f = ma_vse
    muze_zadat_f = muze_zadat_n                                           # fakturu k případu nahrává žadatel = Uživatel
    muze_schv_f = ma_vse or 'faktury_seznam_schvalit' in vsechna_prava    # Schvalovatel faktur (+ Faktury k zadání)
    muze_export_f = False

    if 'finance_state' not in app.storage.user: app.storage.user['finance_state'] = {}
    state = app.storage.user['finance_state']
    _def_tab = ('nakup_nova' if muze_zadat_n else ('nakup_schvalovani' if muze_schv_n else 'faktury_schvalovani'))
    state.setdefault('finance_vnitrni_tab', _def_tab)
    for k, v in [('temp_upload_path', None), ('vybrana_faktura_id', None), ('filter_zadavatel', 'Všichni'),
                 ('filter_stav', 'Všechny'), ('filter_od', ''), ('filter_do', ''), ('filter_cena_od', None),
                 ('filter_cena_do', None), ('pozice_razitka', 'Vpravo nahoře'), ('show_details', False)]:
        state.setdefault(k, v)

    def force_refresh_all():
        vynut_obnovu_faktur(); vynut_obnovu_nakupu()
        vnitrni_menu.refresh(); obsah_panelu.refresh()

    # Client-side blok znaků: .jen-cislo = pouze číslice (integer),
    # .jen-castka = číslice + mezera/čárka/tečka (double). beforeinput chytí
    # i vložení (paste). Guard, ať se listener navěsí jen jednou.
    ui.add_head_html('''
        <script>
        if (!window.__aproviaNumGuard) {
            window.__aproviaNumGuard = true;
            document.addEventListener('beforeinput', function(ev){
                var t = ev.target;
                if(!t || !t.closest) return;
                var d = ev.data;
                if(d == null) return;  // mazání, navigace apod.
                if(t.closest('.jen-cislo'))  { if(/[^0-9]/.test(d))        ev.preventDefault(); }
                else if(t.closest('.jen-castka')){ if(/[^0-9 .,]/.test(d)) ev.preventDefault(); }
            }, true);
        }
        </script>
    ''')

    ui.label('Aprovia').classes('text-4xl font-extrabold text-blue-900 mb-6')

    # CSS grid – šířka levého sloupce je vždy pevná, obsah vpravo ji nemůže ovlivnit
    with ui.element('div').style(
        'display:grid;grid-template-columns:280px 1fr;gap:24px;'
        'width:100%;min-height:600px;align-items:stretch'
    ):
        # --- TMAVÉ LEVÉ MENU ---
        with ui.element('div').style(
            'background:#0f172a;border-radius:1rem;box-shadow:0 4px 24px rgba(0,0,0,.3);'
            'padding:16px;display:flex;flex-direction:column;gap:8px;'
            'width:280px;box-sizing:border-box'
        ):
            @ui.refreshable
            def vnitrni_menu():
                tab = state.get('finance_vnitrni_tab')

                n_ceka = sum(1 for d in nacti_vsechny_nakupy_rychle() if d['stav'] == 'Čeká na schválení' and (is_admin_n or user_id in (d['schvalovatel_1'], d['schvalovatel_2'], d['schvalovatel_3']))) if muze_schv_n else 0
                f_ceka = sum(1 for f in nacti_vsechny_faktury_rychle() if f.get('stav') in ['Čeká', 'Ke schválení'] and (is_admin_f or user_id in [f.get('schvalovatel_id_1'), f.get('schvalovatel_id_2'), f.get('schvalovatel_id_3')] or user_name in [f.get('schvalovatel'), f.get('schvalovatel_2'), f.get('schvalovatel_3')])) if muze_schv_f else 0
                f_ucetni = sum(1 for f in nacti_vsechny_faktury_rychle() if f.get('stav') == 'Schváleno') if muze_schv_f else 0

                def menu_btn(tid, icon, label, color, badge=0):
                    active = (tab == tid)
                    bg = 'bg-blue-600 text-white shadow-lg' if active else 'hover:bg-[#1e293b] text-gray-400 hover:text-white'
                    ic = 'text-white' if active else color

                    def on_menu_click(e, t=tid):
                        state['finance_vnitrni_tab'] = t
                        if t == 'faktury_nova':
                            state.pop('temp_prefill', None)
                        force_refresh_all()

                    with ui.row().classes(f'w-full items-center px-4 py-3 rounded-xl cursor-pointer transition-colors no-wrap {bg}').on('click', on_menu_click):
                        ui.icon(icon, size='sm').classes(ic)
                        ui.label(label).classes('font-bold text-sm ml-4 truncate')
                        if badge > 0: ui.badge(str(badge), color='red').classes('ml-auto font-bold')

                if muze_zadat_n or muze_schv_n:
                    ui.label('🛒 OBJEDNÁVKY').classes('text-[10px] font-bold text-gray-500 uppercase tracking-widest px-2 mb-2 mt-2')
                    if muze_zadat_n:
                        menu_btn('nakup_nova', 'add_circle', 'Nová objednávka', 'text-green-400')
                        menu_btn('nakup_koncepty', 'edit_note', 'Mé koncepty', 'text-blue-400')
                        menu_btn('nakup_objednavky', 'shopping_cart', 'Moje objednávky', 'text-orange-400')
                    if muze_schv_n: menu_btn('nakup_schvalovani', 'fact_check', 'Schvalování objednávek', 'text-yellow-400', n_ceka)

                if muze_zadat_n or muze_schv_f:
                    ui.label('🧾 FAKTURY').classes('text-[10px] font-bold text-gray-500 uppercase tracking-widest px-2 mt-6 mb-2')
                    if muze_zadat_f:
                        menu_btn('faktury_nova', 'note_add', 'Nová faktura', 'text-green-400')
                    if muze_zadat_n:
                        menu_btn('faktury_moje', 'receipt_long', 'Moje faktury', 'text-blue-400')
                    if muze_schv_f:
                        menu_btn('faktury_schvalovani', 'dashboard', 'Faktury ke schválení', 'text-red-400', f_ceka)
                        menu_btn('faktury_ucetnictvi', 'account_balance', 'Faktury k zadání', 'text-purple-400', f_ucetni)

            vnitrni_menu()

        # --- PRAVÝ OBSAHOVÝ PANEL ---
        with ui.column().classes('w-full min-w-0 m-0 p-0 overflow-x-hidden'):
            @ui.refreshable
            def obsah_panelu():
                tab = state.get('finance_vnitrni_tab')

                # ==========================================
                # NÁKUP: NOVÁ AKCE
                # ==========================================
                if tab == 'nakup_nova' and muze_zadat_n:
                    # Úprava existujícího konceptu z "Mé koncepty" → načteme jeho data,
                    # jinak nový formulář = nový koncept
                    _edit_kid = state.pop('edit_koncept_id', None)
                    _edit_data = None
                    if _edit_kid:
                        _edit_data = next((x for x in nacti_vsechny_nakupy_rychle() if x['id'] == _edit_kid), None)
                    state['rozpracovany_koncept_id'] = (_edit_data['id'] if _edit_data else None)
                    def _ed(key, default=''):
                        if _edit_data is None: return default
                        v = _edit_data.get(key)
                        return default if v in (None, '') else v
                    def _ed_castka(key):
                        v = parse_czk(_edit_data.get(key, 0)) if _edit_data else 0
                        return f"{v:,.2f}".replace(',', ' ').replace('.', ',') if v > 0 else "0"
                    with ui.card().classes('w-full max-w-4xl p-8 shadow-md rounded-xl bg-white border-t-4 border-blue-500') as _form_card:
                        ui.label('Úprava konceptu' if _edit_data else 'Vytvoření nové objednávky').classes('text-2xl font-bold mb-6 text-gray-800')
                        _koncept_banner = ui.row().classes('w-full justify-center mb-6')
                        with _koncept_banner:
                            with ui.row().classes('bg-green-100 border border-green-300 rounded-xl px-4 py-2 items-center gap-2 shadow-sm'):
                                ui.icon('check_circle', color='green')
                                ui.label('Formulář uložen jako koncept').classes('text-sm text-green-700 font-medium')
                        _koncept_banner.set_visibility(False)
                        _kb = {'shown': False}
                        def _show_koncept_banner():
                            if _kb['shown']: return
                            _kb['shown'] = True
                            _koncept_banner.set_visibility(True)
                            ui.timer(3.0, lambda: _koncept_banner.set_visibility(False), once=True)
                        _form_card.on('input', lambda e: _show_koncept_banner())
                        _pp_k = state.pop('pp_konverze', None)
                        if _pp_k:
                            with ui.row().classes('w-full bg-purple-50 border border-purple-200 rounded-xl p-3 mb-4 items-center gap-2'):
                                ui.icon('swap_horiz', color='purple')
                                ui.label(f"Předvyplněno z předpoptávky PPQ-{_pp_k.get('pp_cislo','')}").classes('text-sm font-bold text-purple-700')
                        # Modul dle dokumentu pracuje pouze s objednávkami.
                        class _TypAkce:
                            value = 'Objednávka'
                        typ_akce = _TypAkce()
                        ocek_cislo = _ed('cislo_akce') or generuj_cislo_akce()

                        with ui.row().classes('w-full gap-4 mb-6 border-b border-gray-100 pb-6'):
                            inp_o = ui.input('Číslo objednávky', value=ocek_cislo).classes('flex-1 bg-gray-50').props('outlined disable readonly font-bold text-blue-900')
                            ui.input('Zadavatel', value=user_name).classes('flex-[1.5] bg-gray-50').props('outlined disable readonly')

                        with ui.row().classes('w-full gap-4 mb-4'):
                            nazev = ui.input('Název akce', value=_ed('nazev_akce') or (_pp_k.get('nazev','') if _pp_k else '')).classes('flex-[2]').props('outlined bg-white')

                        with ui.row().classes('w-full gap-4 mb-4'):
                            dodavatel = ui.input('Adresát / Dodavatel', value=_ed('dodavatel')).classes('flex-[2]').props('outlined bg-white')
                            misto = ui.input('Místo plnění', value=_ed('misto_plneni')).classes('flex-1').props('outlined bg-white')

                        _termin_v = _edit_data.get('termin_dodani') if _edit_data else None
                        if hasattr(_termin_v, 'strftime'): _termin_v = _termin_v.strftime('%Y-%m-%d')
                        _platba_v = _ed('platba', 'Fakturou')
                        with ui.row().classes('w-full gap-4 mb-6 items-center'):
                            date_dodani = ui.input('Termín realizace a dokončení', value=str(_termin_v) if _termin_v else '').classes('flex-1 bg-white').props('outlined type=date')
                            platba_nakup = ui.select(['Fakturou', 'Hotově', 'Převodem', 'Kartou'], value=_platba_v if _platba_v in ['Fakturou', 'Hotově', 'Převodem', 'Kartou'] else 'Fakturou', label='Platební podmínky').classes('flex-1 bg-white').props('outlined')

                        ui.label('Specifikace položek k nákupu *').classes('font-bold text-gray-700 text-sm mt-2 mb-1')
                        pol_kont = ui.column().classes('w-full gap-2 mb-2 min-w-0')
                        pol_data = []

                        def pridej_radek_polozky(mnoz_v='1', jedn_v='ks', prod_v='', cena_v=''):
                            with pol_kont:
                                r = ui.row().classes('w-full gap-3 items-center flex-wrap sm:flex-nowrap min-w-0')
                                with r:
                                    mnoz = ui.input('Počet', value=mnoz_v).classes('w-20 bg-white border-2 border-blue-400').props('outlined dense').on_value_change(vynut_pouze_cisla)
                                    jedn = ui.select(['ks', 'bal', 'm', 'm2', 'kg', 't', 'hod', 'kpl'], value=jedn_v if jedn_v in ['ks', 'bal', 'm', 'm2', 'kg', 't', 'hod', 'kpl'] else 'ks', label='MJ').classes('w-20 bg-white').props('outlined dense')
                                    prod = ui.input('Produkt / Služba', value=prod_v).classes('flex-1 min-w-[150px] bg-white').props('outlined dense')
                                    cena = ui.input('Cena za MJ (bez DPH)', value=cena_v).classes('w-32 bg-white border-2 border-blue-400').props('outlined dense').tooltip('Zadejte částku bez DPH. Lze oddělovat tisíce mezerou.').on_value_change(vynut_pouze_cisla)

                                    def on_blur_mnoz(e, el=mnoz):
                                        v = parse_czk(el.value)
                                        el.value = f"{v:g}".replace('.', ',') if v > 0 else '1'
                                    mnoz.on('blur', on_blur_mnoz)

                                    def on_blur_cena(e, el=cena):
                                        v = parse_czk(el.value)
                                        if v > 0: el.value = f"{v:,.2f}".replace(',', ' ').replace('.', ',')
                                        else: el.value = ''
                                    cena.on('blur', on_blur_cena)

                                    ui.button(icon='delete', color='red', on_click=lambda: (pol_kont.remove(r), pol_data.remove((mnoz, jedn, prod, cena)))).props('flat dense').classes('mt-1 shrink-0')

                                    pol_data.append((mnoz, jedn, prod, cena))

                        def _parse_polozka_radek(line):
                            m, j, p, c = '1', 'ks', line.strip(), ''
                            if "(Cena:" in line and "Kč)" in line:
                                try:
                                    c_str = line.split("(Cena:")[1].split("Kč)")[0].strip()
                                    cval = parse_czk(c_str)
                                    c = f"{cval:,.2f}".replace(',', ' ').replace('.', ',') if cval > 0 else ''
                                    p = line.split("(Cena:")[0].strip()
                                except: pass
                            if p.startswith('•'):
                                pts = p[1:].strip().split('-', 1)
                                if len(pts) == 2:
                                    mj = pts[0].strip().split(' ', 1)
                                    if len(mj) == 2:
                                        m = mj[0].strip(); j = mj[1].strip(); p = pts[1].strip()
                                    else: p = pts[1].strip()
                                else: p = pts[0].strip()
                            return m, j, p, c

                        if _edit_data and str(_edit_data.get('polozky') or '').strip():
                            for line in str(_edit_data['polozky']).split('\n'):
                                if not line.strip(): continue
                                _m, _j, _p, _c = _parse_polozka_radek(line)
                                pridej_radek_polozky(_m, _j, _p, _c)
                        elif _pp_k:
                            with pol_kont:
                                r = ui.row().classes('w-full gap-3 items-center flex-wrap sm:flex-nowrap min-w-0')
                                with r:
                                    mnoz_v = str(_pp_k.get('mnozstvi','1')) or '1'
                                    jedn_v = _pp_k.get('jednotka','ks') or 'ks'
                                    cena_v = f"{float(_pp_k['cena_odhad']):,.2f}".replace(',',' ').replace('.',',') if _pp_k.get('cena_odhad') else ''
                                    mnoz = ui.input('Počet', value=mnoz_v).classes('w-20 bg-white border-2 border-blue-400').props('outlined dense')
                                    jedn = ui.select(['ks','bal','m','m2','kg','t','hod','kpl'], value=jedn_v if jedn_v in ['ks','bal','m','m2','kg','t','hod','kpl'] else 'ks', label='MJ').classes('w-20 bg-white').props('outlined dense')
                                    prod = ui.input('Produkt / Služba', value=_pp_k.get('nazev','')).classes('flex-1 min-w-[150px] bg-white').props('outlined dense')
                                    cena = ui.input('Cena za MJ (bez DPH)', value=cena_v).classes('w-32 bg-white border-2 border-blue-400').props('outlined dense')
                                    ui.button(icon='delete', on_click=r.delete).props('flat round dense color=red')
                                    pol_data.append((mnoz, jedn, prod, cena))
                        else:
                            pridej_radek_polozky()
                        ui.button('Přidat další položku', icon='add', on_click=pridej_radek_polozky).props('outline size=sm').classes('mb-6')

                        # PŘIDANÝ ROZPAD DPH UŽ V ZAKLÁDÁNÍ
                        ui.label('Celkem v Kč bez DPH').classes('font-bold text-gray-700 text-sm mt-4 mb-2')
                        with ui.row().classes('w-full gap-4 mb-6 flex-wrap'):
                            n_c21 = ui.input('Základ 21 %', value=_ed_castka('castka_21')).classes('flex-1 min-w-[100px] bg-white').props('outlined dense').tooltip('Zadejte částku bez DPH')
                            zaved_formatovani_castky(n_c21)

                            n_c12 = ui.input('Základ 12 %', value=_ed_castka('castka_12')).classes('flex-1 min-w-[100px] bg-white').props('outlined dense').tooltip('Zadejte částku bez DPH')
                            zaved_formatovani_castky(n_c12)

                            n_c0 = ui.input('Základ 0 %', value=_ed_castka('castka_0')).classes('flex-1 min-w-[100px] bg-white').props('outlined dense').tooltip('Zadejte částku bez DPH')
                            zaved_formatovani_castky(n_c0)

                            n_castka = ui.input('CELKEM BEZ DPH', value=_ed_castka('castka_celkem')).classes('flex-[1.5] min-w-[150px] font-bold bg-white border-2 border-blue-400').props('outlined dense').tooltip('Součet základů DPH — vypočítáno automaticky')
                            propoj_castky_celkem([n_c21, n_c12, n_c0], n_castka)

                        doplnujici = ui.textarea('Další informace k e-mailu / PDF', value=_ed('doplnujici_info') or (_pp_k.get('oduvodneni','') if _pp_k else '')).classes('w-full mb-4 bg-white').props('outlined rows=2')

                        state['temp_create_upload_path'] = None
                        async def nahrat_prilohu_create(e):
                            cesta = os.path.join(PRILOHY_DIR, f"nova_{uuid.uuid4().hex[:8]}_{getattr(e, 'name', 'priloha.pdf')}")
                            try:
                                obsah = None; zdroj = None
                                for attr in ['content', 'file', 'stream', 'data', 'file_obj']:
                                    if hasattr(e, attr):
                                        val = getattr(e, attr)
                                        if hasattr(val, 'read'): zdroj = val; break
                                if not zdroj:
                                    for attr in dir(e):
                                        val = getattr(e, attr)
                                        if hasattr(val, 'read'): zdroj = val; break
                                if zdroj:
                                    cteni = zdroj.read()
                                    if asyncio.iscoroutine(cteni): obsah = await cteni
                                    else: obsah = cteni
                                if obsah:
                                    with open(cesta, 'wb') as f: f.write(obsah)
                                    state['temp_create_upload_path'] = cesta
                                    ui.notify('Příloha nahrána', type='positive')
                                else: ui.notify('Nelze načíst data souboru.', type='negative')
                            except Exception as err: ui.notify(f'Chyba: {err}', type='negative')

                        _upload_create = ui.upload(on_upload=nahrat_prilohu_create, auto_upload=True, max_file_size=10_000_000).classes('hidden')
                        with ui.row().classes('items-center gap-2 mb-6'):
                            ui.button(icon='add', on_click=lambda: _upload_create.run_method('pickFiles')).props('round dense size=sm color=primary').tooltip('Přiložit přílohu / PDF (volitelné)')
                            ui.label('Přiložit přílohu / PDF (volitelné)').classes('text-sm text-gray-600')

                        async def ulozit_akci(stav_akce):
                            if not nazev.value or not dodavatel.value: return ui.notify('Vyplňte Název a Dodavatele!', type='warning')
                            seznam_txt = ""
                            for m, j, p, c in pol_data:
                                if p.value and str(p.value).strip():
                                    c_val = parse_czk(c.value)
                                    ctx = f" (Cena: {f'{c_val:,.2f}'.replace(',', ' ').replace('.', ',')} Kč)" if c_val > 0 else ""
                                    seznam_txt += f"• {m.value} {j.value} - {p.value.strip()}{ctx}\n"
                            if not seznam_txt.strip(): return ui.notify('Zadejte alespoň jeden produkt!', type='warning')
                            _kid = state.get('rozpracovany_koncept_id')
                            _typ = typ_akce.value; _nazev = nazev.value; _dodavatel = dodavatel.value
                            _misto = misto.value; _doplnujici = doplnujici.value
                            _termin = date_dodani.value or None; _priloha = state.get('temp_create_upload_path', '')
                            _platba = platba_nakup.value
                            _d21 = parse_czk(n_c21.value); _d12 = parse_czk(n_c12.value)
                            _d0 = parse_czk(n_c0.value); _d_celk = parse_czk(n_castka.value)
                            def _db():
                                c_conn = get_db_utf8(); cur = None; new_id = _kid; cislo = ''
                                try:
                                    cur = c_conn.cursor()
                                    if _kid:
                                        cur.execute("UPDATE nakup_proces SET nazev_akce=%s, dodavatel=%s, misto_plneni=%s, polozky=%s, doplnujici_info=%s, termin_dodani=%s, stav=%s, soubor_priloha=%s, castka_21=%s, castka_12=%s, castka_0=%s, castka_celkem=%s, platba=%s WHERE id=%s", (_nazev, _dodavatel, _misto, seznam_txt, _doplnujici, _termin, stav_akce, _priloha, _d21, _d12, _d0, _d_celk, _platba, _kid))
                                        cur.execute("SELECT cislo_akce FROM nakup_proces WHERE id=%s", (_kid,))
                                        r = cur.fetchone(); cislo = (r[0] if r else '')
                                    else:
                                        cislo = generuj_cislo_akce()
                                        cur.execute("INSERT INTO nakup_proces (cislo_akce, typ, nazev_akce, user_id, zadavatel, dodavatel, misto_plneni, polozky, doplnujici_info, termin_zadani, termin_dodani, stav, soubor_priloha, castka_21, castka_12, castka_0, castka_celkem, platba) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (cislo, _typ, _nazev, user_id, user_name, _dodavatel, _misto, seznam_txt, _doplnujici, datetime.date.today().strftime('%Y-%m-%d'), _termin, stav_akce, _priloha, _d21, _d12, _d0, _d_celk, _platba))
                                        new_id = cur.lastrowid
                                    c_conn.commit()
                                finally:
                                    if cur: cur.close()
                                    if c_conn: c_conn.close()
                                return new_id, cislo
                            akce_id, _nove_cislo = await asyncio.to_thread(_db)
                            pridej_log_nakup(akce_id, user_name, f"Uloženo jako koncept (Stav: {stav_akce})")
                            ui.notify(f'Uloženo jako koncept pod číslem {_nove_cislo}.', type='positive')
                            state['temp_create_upload_path'] = None
                            state['rozpracovany_koncept_id'] = None
                            state['finance_vnitrni_tab'] = 'nakup_koncepty'
                            force_refresh_all()

                        actions_row = ui.row().classes('w-full justify-between items-center mt-4 border-t pt-4')

                        def refresh_actions(e=None):
                            actions_row.clear()
                            with actions_row:
                                ui.button('Uložit změny konceptu' if _edit_data else 'Uložit koncept', icon='save', on_click=lambda: ulozit_akci('Připraveno ke schválení')).classes('bg-blue-600 hover:bg-blue-700 text-white font-bold h-12 px-8 shadow-md ml-auto')

                        refresh_actions()

                        # ── Auto-ukládání rozpracovaného konceptu (dle dokumentu) ──
                        async def _autosave_koncept(_=None):
                            if not (nazev.value or '').strip():
                                return
                            seznam_txt = ""
                            for m, j, p, c in pol_data:
                                if p.value and str(p.value).strip():
                                    c_val = parse_czk(c.value)
                                    ctx = f" (Cena: {f'{c_val:,.2f}'.replace(',', ' ').replace('.', ',')} Kč)" if c_val > 0 else ""
                                    seznam_txt += f"• {m.value} {j.value} - {p.value.strip()}{ctx}\n"
                            a_nazev = nazev.value; a_dod = dodavatel.value or ''; a_misto = misto.value or ''
                            a_dopl = doplnujici.value or ''; a_termin = date_dodani.value or None; a_platba = platba_nakup.value
                            a21 = parse_czk(n_c21.value); a12 = parse_czk(n_c12.value); a0 = parse_czk(n_c0.value); acelk = parse_czk(n_castka.value)
                            def _db():
                                c = get_db_utf8(); cur = None
                                try:
                                    cur = c.cursor()
                                    kid = state.get('rozpracovany_koncept_id')
                                    if kid:
                                        cur.execute("UPDATE nakup_proces SET nazev_akce=%s, dodavatel=%s, misto_plneni=%s, polozky=%s, doplnujici_info=%s, termin_dodani=%s, platba=%s, castka_21=%s, castka_12=%s, castka_0=%s, castka_celkem=%s WHERE id=%s AND stav='Rozpracováno'", (a_nazev, a_dod, a_misto, seznam_txt, a_dopl, a_termin, a_platba, a21, a12, a0, acelk, kid))
                                    else:
                                        cislo = generuj_cislo_akce()
                                        cur.execute("INSERT INTO nakup_proces (cislo_akce, typ, nazev_akce, user_id, zadavatel, dodavatel, misto_plneni, polozky, doplnujici_info, termin_zadani, termin_dodani, stav, platba, castka_21, castka_12, castka_0, castka_celkem) VALUES (%s, 'Objednávka', %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Rozpracováno', %s, %s, %s, %s, %s)", (cislo, a_nazev, user_id, user_name, a_dod, a_misto, seznam_txt, a_dopl, datetime.date.today().strftime('%Y-%m-%d'), a_termin, a_platba, a21, a12, a0, acelk))
                                        state['rozpracovany_koncept_id'] = cur.lastrowid
                                    c.commit()
                                finally:
                                    if cur: cur.close()
                                    if c: c.close()
                            await asyncio.to_thread(_db)

                        for _el in (nazev, dodavatel, misto, doplnujici):
                            _el.on('blur', _autosave_koncept)

                # ==========================================
                # NÁKUP: VÝPISY A DETAILY
                # ==========================================
                elif tab in ['nakup_koncepty', 'nakup_objednavky', 'nakup_schvalovani']:
                    titulky = {'nakup_koncepty': 'Mé koncepty', 'nakup_objednavky': 'Moje objednávky', 'nakup_schvalovani': 'Objednávky ke schválení'}
                    ui.label(titulky[tab]).classes('text-2xl font-bold mb-2 text-gray-800')

                    data = []
                    vsechny_nak = nacti_vsechny_nakupy_rychle()
                    for d in vsechny_nak:
                        if tab == 'nakup_koncepty' and d['user_id'] == user_id and d['stav'] in ('Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení'): data.append(d)
                        elif tab == 'nakup_objednavky' and d['user_id'] == user_id and d['stav'] not in ('Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení'): data.append(d)
                        elif tab == 'nakup_schvalovani' and d['stav'] == 'Čeká na schválení' and (is_admin_n or user_id in (d['schvalovatel_1'], d['schvalovatel_2'], d['schvalovatel_3'])): data.append(d)

                    if not data:
                        ui.label('Zatím nemáte žádné záznamy v této kategorii.').classes('text-gray-500 italic mt-4')
                    else:
                        with ui.column().classes('w-full gap-3 mt-4'):
                            for d in data:
                                bg, bdr = 'bg-white', 'border-gray-200'
                                if 'Zamítnuto' in d['stav']: bg, bdr = 'bg-red-50', 'border-red-400'
                                elif 'Uzavřeno' in d['stav']: bg, bdr = 'bg-gray-100', 'border-gray-400'
                                elif 'Schváleno' in d['stav']: bg, bdr = 'bg-green-50', 'border-green-400'

                                def otevri_detail(akce_id=d['id']):
                                    with ui.dialog().props('maximized transition-show="slide-up"') as dlg:
                                        dlg.on('hide', obsah_panelu.refresh)

                                        with ui.card().classes('w-full h-full p-0 bg-gray-50 flex-col'):
                                            @ui.refreshable
                                            def d_obsah():
                                                dd = next((x for x in nacti_vsechny_nakupy_rychle() if x['id'] == akce_id), None)
                                                if not dd:
                                                    dlg.close()
                                                    return

                                                live_schv = ziskej_nakup_schvalovatele_live()
                                                with ui.row().classes('w-full bg-white p-6 border-b justify-between items-center sticky top-0 z-50 shadow-sm'):
                                                    with ui.column().classes('gap-0'):
                                                        ui.label(f"{dd['typ'].upper()} #{dd['cislo_akce']}").classes('text-sm font-bold text-blue-600')
                                                        ui.label(dd['nazev_akce']).classes('text-3xl font-black')

                                                        with ui.row().classes('gap-6 mt-2'):
                                                            with ui.row().classes('items-center gap-1'):
                                                                ui.icon('person', size='sm', color='gray-500')
                                                                ui.label(f"Žadatel: {dd.get('zadavatel', 'Neznámý')}").classes('text-sm font-bold text-gray-700')
                                                            with ui.row().classes('items-center gap-1'):
                                                                ui.icon('store', size='sm', color='gray-500')
                                                                ui.label(f"Dodavatel: {dd.get('dodavatel', 'Neznámý')}").classes('text-sm font-bold text-gray-700')

                                                    with ui.row().classes('gap-4'):
                                                        if dd['stav'] not in ('Rozpracováno', 'Připraveno ke schválení'):
                                                            ui.button('Stáhnout / Tisk PDF', icon='print', on_click=lambda: ui.navigate.to(vygeneruj_tiskove_pdf_nakup(dd), new_tab=True)).classes('bg-gray-800 text-white font-bold')
                                                        ui.button('Zavřít', icon='close', on_click=dlg.close).classes('bg-gray-300 text-gray-800 font-bold')

                                                with ui.row().classes('w-full p-6 gap-6 items-start h-full overflow-y-auto no-wrap'):
                                                    with ui.column().classes('flex-[2] gap-4'):
                                                        render_semafor(dd['stav'], False, dd)
                                                        if dd.get('opakena_zadost'): ui.label('⚠️ OPAKOVANÁ ŽÁDOST').classes('w-full bg-red-600 text-white font-black text-center py-2 rounded-xl text-lg')

                                                        if dd['typ'] == 'Objednávka' and dd['stav'] in ('Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení'):
                                                            with ui.card().classes('w-full p-6 shadow-sm border-t-4 border-green-500 bg-white'):
                                                                ui.label('Úprava a odeslání objednávky').classes('text-xl font-bold mb-4')

                                                                def _upravit_koncept(_=None, _id=akce_id):
                                                                    state['edit_koncept_id'] = _id
                                                                    state['finance_vnitrni_tab'] = 'nakup_nova'
                                                                    dlg.close()
                                                                    force_refresh_all()
                                                                ui.button('Upravit celý koncept ve formuláři', icon='edit', on_click=_upravit_koncept).props('outline').classes('w-full mb-4 text-blue-700 font-bold')
                                                                pk = ui.column().classes('w-full gap-2 mb-6')
                                                                n_pol = []

                                                                for line in str(dd['polozky']).split('\n'):
                                                                    if not line.strip(): continue
                                                                    m, j, p, c = 1.0, 'ks', line.strip(), 0.0

                                                                    if "(Cena:" in line and "Kč)" in line:
                                                                        try:
                                                                            c_str = line.split("(Cena:")[1].split("Kč)")[0].strip()
                                                                            c = parse_czk(c_str)
                                                                            p = line.split("(Cena:")[0].strip()
                                                                        except: pass
                                                                    if p.startswith('•'):
                                                                        pts = p[1:].strip().split('-', 1)
                                                                        if len(pts)==2:
                                                                            mj = pts[0].strip().split(' ', 1)
                                                                            if len(mj)==2:
                                                                                try:
                                                                                    m = parse_czk(mj[0])
                                                                                    j = mj[1].strip()
                                                                                    p = pts[1].strip()
                                                                                except: pass
                                                                            else: p = pts[1].strip()
                                                                        else: p = pts[0].strip()

                                                                    with pk:
                                                                        with ui.row().classes('w-full gap-3 no-wrap items-center'):
                                                                            i_m = ui.input('Počet', value=f"{m:g}".replace('.', ',')).classes('w-20 bg-white border-2 border-blue-400').props('outlined dense').on_value_change(vynut_pouze_cisla)
                                                                            i_j = ui.select(['ks', 'bal', 'm', 'm2', 'kg', 't', 'hod', 'kpl'], value=j).classes('w-20 bg-gray-50').props('outlined dense disable readonly')
                                                                            i_p = ui.input('Produkt', value=p).classes('flex-1 bg-gray-50').props('outlined dense disable readonly')

                                                                            c_val = f"{c:,.2f}".replace(',', ' ').replace('.', ',') if c > 0 else ''
                                                                            i_c = ui.input('Cena za MJ (bez DPH)', value=c_val).classes('w-32 bg-white border-2 border-blue-400').props('outlined dense').tooltip('Zadejte částku bez DPH. Lze použít mezery a desetinnou čárku.').on_value_change(vynut_pouze_cisla)

                                                                            def on_blur_mnoz_edit(e, el=i_m):
                                                                                v = parse_czk(el.value)
                                                                                el.value = f"{v:g}".replace('.', ',') if v > 0 else '1'
                                                                            i_m.on('blur', on_blur_mnoz_edit)

                                                                            def on_blur_cena_edit(e, el=i_c):
                                                                                v = parse_czk(el.value)
                                                                                if v > 0: el.value = f"{v:,.2f}".replace(',', ' ').replace('.', ',')
                                                                                else: el.value = ''
                                                                            i_c.on('blur', on_blur_cena_edit)

                                                                            n_pol.append((i_m, i_j, i_p, i_c))

                                                                ui.label('Celkem v Kč bez DPH').classes('font-bold text-sm mt-4 mb-2')
                                                                with ui.row().classes('w-full gap-4 mb-6'):
                                                                    v21 = parse_czk(dd.get('castka_21', 0))
                                                                    e_c21 = ui.input('Základ 21 %', value=f"{v21:,.2f}".replace(',', ' ').replace('.', ',') if v21 > 0 else "0").classes('flex-1 bg-white').props('outlined dense')
                                                                    zaved_formatovani_castky(e_c21)

                                                                    v12 = parse_czk(dd.get('castka_12', 0))
                                                                    e_c12 = ui.input('Základ 12 %', value=f"{v12:,.2f}".replace(',', ' ').replace('.', ',') if v12 > 0 else "0").classes('flex-1 bg-white').props('outlined dense')
                                                                    zaved_formatovani_castky(e_c12)

                                                                    v0 = parse_czk(dd.get('castka_0', 0))
                                                                    e_c0 = ui.input('Základ 0 %', value=f"{v0:,.2f}".replace(',', ' ').replace('.', ',') if v0 > 0 else "0").classes('flex-1 bg-white').props('outlined dense')
                                                                    zaved_formatovani_castky(e_c0)

                                                                    vc = parse_czk(dd.get('castka_celkem', 0))
                                                                    e_castka = ui.input('CELKEM BEZ DPH', value=f"{vc:,.2f}".replace(',', ' ').replace('.', ',') if vc > 0 else "0").classes('flex-[1.5] font-bold bg-white border-2 border-blue-400').props('outlined dense').tooltip('Součet základů DPH — vypočítáno automaticky')
                                                                    propoj_castky_celkem([e_c21, e_c12, e_c0], e_castka)

                                                                state['temp_upload_path'] = None
                                                                async def nahrat_prilohu(e):
                                                                    cesta = os.path.join(PRILOHY_DIR, f"{dd['cislo_akce']}_{getattr(e, 'name', 'priloha.pdf')}")
                                                                    try:
                                                                        obsah = None; zdroj = None
                                                                        for attr in ['content', 'file', 'stream', 'data', 'file_obj']:
                                                                            if hasattr(e, attr):
                                                                                val = getattr(e, attr)
                                                                                if hasattr(val, 'read'): zdroj = val; break
                                                                        if not zdroj:
                                                                            for attr in dir(e):
                                                                                val = getattr(e, attr)
                                                                                if hasattr(val, 'read'): zdroj = val; break
                                                                        if zdroj:
                                                                            cteni = zdroj.read()
                                                                            if asyncio.iscoroutine(cteni): obsah = await cteni
                                                                            else: obsah = cteni
                                                                        if obsah:
                                                                            with open(cesta, 'wb') as f: f.write(obsah)
                                                                            state['temp_upload_path'] = cesta
                                                                            ui.notify('Příloha nahrána', type='info')
                                                                        else: ui.notify('Nelze načíst data souboru.', type='negative')
                                                                    except Exception as err: ui.notify(f'Chyba: {err}', type='negative')

                                                                _upload_obj = ui.upload(on_upload=nahrat_prilohu, auto_upload=True, max_file_size=10_000_000).classes('hidden')
                                                                with ui.row().classes('items-center gap-2 mb-4 mt-2'):
                                                                    ui.button(icon='add', on_click=lambda: _upload_obj.run_method('pickFiles')).props('round dense size=sm color=primary').tooltip('Přiložit objednávku (volitelné)')
                                                                    ui.label('Přiložit objednávku (volitelné)').classes('text-sm text-gray-600')

                                                                ui.label('Schvalovatelé (Povinné):').classes('font-bold text-sm mb-2')
                                                                s1 = ui.select(live_schv, label='1. Schvalovatel').classes('w-full mb-1 bg-white').props('outlined dense')
                                                                s2 = ui.select(live_schv, label='2. Schvalovatel', clearable=True).classes('w-full mb-1 bg-white').props('outlined dense')
                                                                s3 = ui.select(live_schv, label='3. Schvalovatel', clearable=True).classes('w-full mb-4 bg-white').props('outlined dense')

                                                                async def odeslat_schvaleni():
                                                                    if not s1.value: return ui.notify('Vyberte 1. schvalovatele!', type='warning')
                                                                    txt = ""
                                                                    for im, ij, ip, ic in n_pol:
                                                                        c_val = parse_czk(ic.value)
                                                                        ctx = f" (Cena: {f'{c_val:,.2f}'.replace(',', ' ').replace('.', ',')} Kč)" if c_val > 0 else ""
                                                                        txt += f"• {im.value} {ij.value} - {ip.value.strip()}{ctx}\n"
                                                                    _txt = txt; _soubor = state['temp_upload_path'] or dd.get('soubor_priloha','')
                                                                    _s1 = s1.value; _s2 = s2.value; _s3 = s3.value
                                                                    _d21 = parse_czk(e_c21.value); _d12 = parse_czk(e_c12.value)
                                                                    _d0 = parse_czk(e_c0.value); _d_celk = parse_czk(e_castka.value)
                                                                    def _db():
                                                                        c = get_db_utf8(); cr = None
                                                                        try:
                                                                            cr = c.cursor()
                                                                            cr.execute("UPDATE nakup_proces SET stav='Čeká na schválení', polozky=%s, soubor_priloha=%s, schvalovatel_1=%s, schvalovatel_2=%s, schvalovatel_3=%s, castka_21=%s, castka_12=%s, castka_0=%s, castka_celkem=%s WHERE id=%s", (_txt, _soubor, _s1, _s2, _s3, _d21, _d12, _d0, _d_celk, akce_id))
                                                                            c.commit()
                                                                        finally:
                                                                            if cr: cr.close()
                                                                            if c: c.close()
                                                                    await asyncio.to_thread(_db)
                                                                    pridej_log_nakup(akce_id, user_name, "Odesláno ke schválení.")
                                                                    vynut_obnovu_nakupu()
                                                                    force_refresh_all()
                                                                    ui.notify('Odesláno ke schválení!', type='positive')
                                                                    _cislo = dd.get('cislo_akce', '')
                                                                    _nazev = dd.get('nazev_akce', '')
                                                                    await asyncio.to_thread(posli_emaily_schvovatelum, [_s1, _s2, _s3], 'Objednávku', _cislo, _nazev)

                                                                ui.button('Odeslat ke schválení', icon='send', on_click=odeslat_schvaleni).classes('bg-green-600 text-white font-bold w-full h-12 shadow-lg')

                                                        if dd['stav'] != 'Uzavřeno' and dd['stav'] not in ['Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení']:
                                                            with ui.card().classes('w-full p-6 shadow-sm border border-blue-200 bg-blue-50/30 rounded-xl mt-4'):

                                                                if dd['stav'] == 'Zamítnuto' and (dd['zadavatel'] == user_name or is_admin_n):
                                                                    ui.label(f"Důvod zamítnutí: {dd.get('duvod_zamitnuti', '')}").classes('bg-red-100 text-red-800 p-3 rounded font-bold mb-4 w-full break-words whitespace-normal')
                                                                    async def n_naprava():
                                                                        def _db():
                                                                            c = get_db_utf8(); cr = None
                                                                            try:
                                                                                cr = c.cursor()
                                                                                cr.execute("UPDATE nakup_proces SET stav='Návrh objednávky', opakena_zadost=1, schvaleno_1=NULL, schvaleno_2=NULL, schvaleno_3=NULL WHERE id=%s", (akce_id,))
                                                                                c.commit()
                                                                            finally:
                                                                                if cr: cr.close()
                                                                                if c: c.close()
                                                                        await asyncio.to_thread(_db)
                                                                        pridej_log_nakup(akce_id, user_name, "Zadavatel otevřel objednávku k nápravě.")
                                                                        vynut_obnovu_nakupu()
                                                                        d_obsah.refresh()
                                                                        ui.notify('Objednávka otevřena k úpravě — upravte a odešlete znovu.', type='info')
                                                                    ui.button('Upravit a odeslat znovu', icon='replay', on_click=n_naprava).classes('bg-orange-500 text-white font-bold w-full')

                                                                elif dd['stav'] == 'Čeká na schválení':
                                                                    for i in range(1, 4):
                                                                        sid = dd[f'schvalovatel_{i}']
                                                                        if sid:
                                                                            cas = dd[f'schvaleno_{i}']
                                                                            jm = live_schv.get(sid, 'Neznámý')
                                                                            if cas: ui.label(f"✅ {jm} (Schváleno: {cas.strftime('%d.%m.%Y %H:%M') if isinstance(cas, datetime.datetime) else cas})").classes('text-green-600 font-bold mb-1 text-xs')
                                                                            else:
                                                                                ui.label(f"⏳ {jm} (Čeká se)").classes('text-orange-600 font-bold mb-1 text-xs')
                                                                                if sid == user_id or is_admin_n:
                                                                                    async def n_schv(idx=i):
                                                                                        _zadavatel_id = dd.get('user_id'); _cislo = dd.get('cislo_akce', ''); _nazev = dd.get('nazev_akce', '')
                                                                                        hotovo_flag = {'v': False}
                                                                                        def _db():
                                                                                            c = get_db_utf8(); cr = None
                                                                                            try:
                                                                                                cr = c.cursor(dictionary=True)
                                                                                                if idx not in (1, 2, 3):
                                                                                                    raise ValueError(f"Neplatný index schválení: {idx}")
                                                                                                cr.execute(f"UPDATE nakup_proces SET schvaleno_{idx}=NOW() WHERE id=%s", (akce_id,))
                                                                                                cr.execute("SELECT schvaleno_1, schvaleno_2, schvaleno_3, schvalovatel_1, schvalovatel_2, schvalovatel_3 FROM nakup_proces WHERE id=%s", (akce_id,))
                                                                                                row = cr.fetchone()
                                                                                                hotovo = all(not (row[f'schvalovatel_{j}'] and not row[f'schvaleno_{j}']) for j in range(1, 4))
                                                                                                if hotovo: cr.execute("UPDATE nakup_proces SET stav='Schváleno' WHERE id=%s", (akce_id,))
                                                                                                hotovo_flag['v'] = hotovo
                                                                                                c.commit()
                                                                                            finally:
                                                                                                if cr: cr.close()
                                                                                                if c: c.close()
                                                                                        await asyncio.to_thread(_db)
                                                                                        pridej_log_nakup(akce_id, user_name, f"Schváleno (Krok {idx})")
                                                                                        if hotovo_flag['v']:
                                                                                            await asyncio.to_thread(posli_email_zadateli, _zadavatel_id, 'schvalena_objednavka', _cislo, _nazev)
                                                                                        vynut_obnovu_nakupu()
                                                                                        d_obsah.refresh()
                                                                                        ui.notify('Schváleno', type='positive')

                                                                                    def n_zam():
                                                                                        with ui.dialog() as zd, ui.card().classes('p-6 rounded-xl max-w-sm'):
                                                                                            duv = ui.input('Důvod zamítnutí').classes('w-full mb-4 bg-white')
                                                                                            async def z_potvrd():
                                                                                                _duv = (duv.value or '').strip()
                                                                                                if not _duv: return ui.notify('Uveďte důvod zamítnutí!', type='warning')
                                                                                                _zadavatel_id = dd.get('user_id'); _cislo = dd.get('cislo_akce', ''); _nazev = dd.get('nazev_akce', '')
                                                                                                def _db():
                                                                                                    c = get_db_utf8(); cr = None
                                                                                                    try:
                                                                                                        cr = c.cursor()
                                                                                                        cr.execute("UPDATE nakup_proces SET stav='Zamítnuto', duvod_zamitnuti=%s WHERE id=%s", (_duv, akce_id))
                                                                                                        c.commit()
                                                                                                    finally:
                                                                                                        if cr: cr.close()
                                                                                                        if c: c.close()
                                                                                                await asyncio.to_thread(_db)
                                                                                                pridej_log_nakup(akce_id, user_name, f"Zamítnuto: {_duv}")
                                                                                                await asyncio.to_thread(posli_email_zadateli, _zadavatel_id, 'zamitnuta_objednavka', _cislo, _nazev, _duv)
                                                                                                zd.close()
                                                                                                vynut_obnovu_nakupu()
                                                                                                d_obsah.refresh()
                                                                                                ui.notify('Zamítnuto', type='negative')
                                                                                            ui.button('Zamítnout', color='red', on_click=z_potvrd).classes('w-full font-bold h-12')
                                                                                        zd.open()
                                                                                    with ui.row().classes('w-full gap-2 mt-2 mb-4'):
                                                                                        ui.button('Schválit', color='green', on_click=n_schv).classes('flex-1 font-bold h-10')
                                                                                        ui.button('Zamítnout', color='red', on_click=n_zam).classes('flex-1 font-bold h-10').props('flat')

                                                                elif dd['stav'] == 'Schváleno' and (dd['zadavatel'] == user_name or is_admin_n):
                                                                    ui.label('Objednávka schválena. Jakmile od dodavatele přijde faktura, nahrajte ji k objednávce.').classes('text-sm text-gray-600 mb-3')
                                                                    def do_faktur():
                                                                        state['temp_prefill'] = {
                                                                            'cislo_akce': dd['cislo_akce'],
                                                                            'zadavatel': dd['zadavatel'],
                                                                            'dodavatel': dd['dodavatel'],
                                                                            'popis': f"{dd['nazev_akce']}\n\nČíslo objednávky JIP: {dd['cislo_akce']}",
                                                                            'castka_21': dd.get('castka_21', 0),
                                                                            'castka_12': dd.get('castka_12', 0),
                                                                            'castka_0': dd.get('castka_0', 0),
                                                                            'castka': dd.get('castka_celkem', 0),
                                                                            'platba': dd.get('platba', 'Fakturou')
                                                                        }
                                                                        state['finance_vnitrni_tab'] = 'faktury_nova'
                                                                        dlg.close()
                                                                        force_refresh_all()
                                                                    ui.button('Nahrát fakturu k objednávce', icon='receipt_long', on_click=do_faktur).classes('bg-purple-600 hover:bg-purple-700 text-white font-bold w-full h-14 shadow-lg')

                                                                elif dd['stav'] == 'Faktura schválena':
                                                                    ui.label('Faktura k objednávce byla schválena a čeká na zadání do účetnictví.').classes('text-sm font-bold text-purple-700')
                                                                else:
                                                                    ui.label(f"Stav objednávky: {dd['stav']}").classes('text-sm font-bold text-gray-600')

                                                        muze_smazat = is_admin_n or (dd['zadavatel'] == user_name and dd['stav'] in ['Rozpracováno', 'Návrh objednávky', 'Připraveno ke schválení', 'Čeká na schválení'])
                                                        if muze_smazat:
                                                            def akce_smazat_nakup():
                                                                with ui.dialog() as dlg_del, ui.card().classes('p-6 rounded-xl w-full max-w-sm'):
                                                                    ui.label('Smazání konceptu').classes('text-xl font-bold mb-4 text-red-600')
                                                                    ui.label('Opravdu nenávratně smazat tento koncept včetně přiložených souborů? Tento krok nelze vrátit.').classes('mb-6 text-gray-700')
                                                                    async def potvrdit():
                                                                        dlg_del.close()
                                                                        if await asyncio.to_thread(smaz_nakup_z_db, akce_id):
                                                                            intranet_logger.log_activity(user_name, "Nákup", f"Smazán nákupní požadavek č. {dd['cislo_akce']}")
                                                                            vynut_obnovu_nakupu()
                                                                            ui.notify('Nákupní požadavek byl úspěšně smazán.', type='info')
                                                                            dlg.close()
                                                                            force_refresh_all()
                                                                        else:
                                                                            ui.notify('Chyba při mazání!', type='negative')
                                                                    with ui.row().classes('w-full justify-between'):
                                                                        ui.button('Zrušit', on_click=dlg_del.close).classes('bg-gray-400 text-white font-bold')
                                                                        ui.button('Smazat koncept', on_click=potvrdit).classes('bg-red-600 text-white font-bold shadow-md')
                                                                dlg_del.open()

                                                            ui.button('Smazat koncept', icon='delete', color='red', on_click=akce_smazat_nakup).props('flat').classes('w-full mt-4 text-[13px] text-gray-500 hover:text-red-500 bg-red-50 h-10 rounded-lg font-bold')

                                                    with ui.column().classes('flex-1'):
                                                        with ui.card().classes('w-full p-4 bg-gray-800 text-gray-300 rounded-xl shadow-inner max-h-[500px] overflow-y-auto'):
                                                            ui.label('Historie a logování').classes('text-white font-bold text-lg mb-4 border-b border-gray-600 pb-2 w-full')
                                                            _logy = []
                                                            _lc = get_db_utf8()
                                                            _lcr = None
                                                            try:
                                                                _lcr = _lc.cursor(dictionary=True)
                                                                _lcr.execute("SELECT uzivatel, text FROM nakup_logy WHERE akce_id=%s ORDER BY datum DESC LIMIT 200", (akce_id,))
                                                                _logy = _lcr.fetchall()
                                                            except Exception: pass
                                                            finally:
                                                                if _lcr: _lcr.close()
                                                                if _lc: _lc.close()
                                                            for log in _logy:
                                                                with ui.row().classes('w-full border-l-2 border-gray-500 pl-2 mb-2'):
                                                                    ui.label(log['uzivatel']).classes('text-[11px] font-bold text-blue-400')
                                                                    ui.label(log['text']).classes('text-xs text-gray-200 w-full break-words whitespace-normal')
                                            d_obsah()
                                    dlg.open()

                                with ui.card().classes(f'w-full p-4 {bg} border-l-4 {bdr} shadow-sm flex-row justify-between items-center cursor-pointer').on('click', lambda a=d['id']: otevri_detail(a)):
                                    with ui.column().classes('gap-0 items-start flex-1 min-w-0 pr-2'):
                                        ui.label(f"{d['cislo_akce']} | {d['nazev_akce']}").classes('font-bold text-lg text-gray-800 break-words whitespace-normal')

                                        with ui.row().classes('gap-2 items-center mt-1 flex-wrap w-full'):
                                            ui.label(f"Žadatel: {d.get('zadavatel', 'Neznámý')}").classes('text-sm text-blue-700 font-bold break-words whitespace-normal')
                                            ui.label('|').classes('text-gray-300 text-sm')
                                            ui.label(f"Dodavatel: {d.get('dodavatel', 'Neznámý')}").classes('text-sm text-gray-600 font-bold break-words whitespace-normal')

                                        termin = d.get('termin_dodani')
                                        realny_termin = d.get('realny_termin_dokonceni')

                                        if realny_termin:
                                            ui.label(f"✅ Splněno: {rychly_format_data(realny_termin)}").classes('text-[11px] font-bold text-green-700 mt-1')
                                        elif termin and d['stav'] not in ['Uzavřeno', 'Zamítnuto', 'Rozpracováno', 'Připraveno ke schválení']:
                                            try:
                                                t_date = termin if isinstance(termin, datetime.date) else datetime.datetime.strptime(str(termin)[:10], '%Y-%m-%d').date()
                                                dnes = datetime.date.today()
                                                rozdil = (t_date - dnes).days

                                                if rozdil < 0:
                                                    ui.label(f"⚠️ Zpoždění: {abs(rozdil)} dní (Termín: {rychly_format_data(termin)})").classes('text-[11px] font-bold text-red-600 mt-1 bg-red-50 px-2 py-0.5 rounded border border-red-200 inline-block')
                                                elif rozdil == 0:
                                                    ui.label("⏳ Termín je DNES!").classes('text-[11px] font-bold text-orange-600 mt-1 bg-orange-50 px-2 py-0.5 rounded border border-orange-200 inline-block')
                                                else:
                                                    ui.label(f"🕒 Zbývá dnů: {rozdil} (Termín: {rychly_format_data(termin)})").classes('text-[11px] font-bold text-blue-600 mt-1')
                                            except Exception:
                                                ui.label(f"Termín: {rychly_format_data(termin)}").classes('text-[11px] text-gray-500 mt-1')
                                        elif termin:
                                            ui.label(f"Termín: {rychly_format_data(termin)}").classes('text-[11px] text-gray-500 mt-1')

                                    stav_k = d['stav']

                                    z_stav = stav_k
                                    if stav_k == 'Připraveno ke schválení': z_stav = 'Připraveno'

                                    if stav_k == 'Připraveno ke schválení': s_bg, s_tx, s_bd = 'bg-indigo-100', 'text-indigo-800', 'border-indigo-300'
                                    elif stav_k in ['Rozpracováno', 'Návrh objednávky']: s_bg, s_tx, s_bd = 'bg-gray-100', 'text-gray-700', 'border-gray-300'
                                    elif 'Zamítnuto' in stav_k: s_bg, s_tx, s_bd = 'bg-red-100', 'text-red-800', 'border-red-300'
                                    elif stav_k == 'Čeká na schválení': s_bg, s_tx, s_bd = 'bg-yellow-100', 'text-yellow-800', 'border-yellow-400'
                                    elif 'Schváleno' in stav_k: s_bg, s_tx, s_bd = 'bg-green-100', 'text-green-800', 'border-green-400'
                                    elif 'Uzavřeno' in stav_k: s_bg, s_tx, s_bd = 'bg-gray-200', 'text-gray-800', 'border-gray-400'
                                    else: s_bg, s_tx, s_bd = 'bg-white', 'text-gray-700', 'border-gray-200'

                                    with ui.column().classes('items-end gap-2 shrink-0'):
                                        ui.label(z_stav).classes(f'px-4 py-1.5 {s_bg} {s_tx} border {s_bd} rounded-full text-xs font-black uppercase tracking-wider shadow-sm text-center')

                                        if stav_k not in ('Rozpracováno', 'Připraveno ke schválení'):
                                            btn_text = 'Stáhnout poptávku' if d['typ'] == 'Poptávka' else 'Stáhnout objednávku'
                                            ui.button(btn_text, icon='download').on('click.stop', lambda e, doc=d: ui.navigate.to(vygeneruj_tiskove_pdf_nakup(doc), new_tab=True)).props('flat size=sm').classes('bg-blue-50 text-blue-700 hover:bg-blue-100 font-bold rounded-lg')

                # ==========================================
                # FAKTURY: ZADAT NOVOU NEBO OPRAVIT
                # ==========================================
                elif tab == 'faktury_nova' and muze_zadat_f:
                    prefill = state.get('temp_prefill', {})
                    je_nakup = bool(prefill.get('cislo_akce'))
                    edit_id = prefill.get('edit_id')

                    with ui.card().classes(f"w-full max-w-2xl p-8 shadow-md rounded-2xl border-t-8 {'border-orange-500 bg-orange-50' if edit_id else ('border-green-500 bg-green-50' if je_nakup else 'border-blue-500 bg-white')}"):
                        if edit_id: ui.label('Opravujete dříve zamítnutou fakturu').classes('text-xl font-bold mb-4 text-orange-800')
                        elif je_nakup: ui.label(f"Faktura k objednávce č. {prefill.get('cislo_akce', '')}").classes('text-xl font-bold mb-4 text-green-800')
                        else: ui.label('Zadat novou fakturu ručně').classes('text-2xl font-bold mb-6 text-gray-800')

                        dost_schv = get_schvalovatele_faktury_fast()  # {id: jmeno} dict
                        vych_schv = next(iter(dost_schv), None)  # první ID nebo None
                        _id_na_jmeno = dost_schv  # alias pro přehlednost
                        _jmeno_na_id = {v: k for k, v in dost_schv.items()}

                        c_obj = ui.input('Číslo objednávky', value=prefill.get('cislo_akce', '')).classes('hidden' if not je_nakup else 'w-full mb-4 bg-white').props('outlined dense readonly' if je_nakup else 'outlined dense')

                        with ui.row().classes('w-full gap-4 mb-4'):
                            dodavatel_bg = 'bg-gray-50' if je_nakup and not edit_id else 'bg-white'
                            dodavatel = ui.input('Dodavatel *', value=prefill.get('dodavatel', '')).classes(f'flex-[2] {dodavatel_bg}').props('outlined dense' + (' readonly' if je_nakup and not edit_id else ''))
                            ico = ui.input('IČO', value=prefill.get('ico', '')).classes('flex-1 bg-white').props('outlined dense')
                            dic = ui.input('DIČ', value=prefill.get('dic', '')).classes('flex-1 bg-white').props('outlined dense')

                        with ui.row().classes('w-full gap-4 mb-4 items-center'):
                            cislo_fa = ui.input('Číslo faktury dodavatele *', value=prefill.get('cislo_faktury', '')).classes('flex-[1.5] bg-white jen-cislo').props('outlined dense inputmode=numeric').on_value_change(vynut_pouze_integer)
                            vs = ui.input('Variabilní symbol', value=prefill.get('vs', '')).classes('flex-1 bg-white jen-cislo').props('outlined dense inputmode=numeric').on_value_change(vynut_pouze_integer)

                            b_ucet_full = str(prefill.get('bankovni_ucet', ''))
                            b_cislo = b_ucet_full.split('/')[0] if '/' in b_ucet_full else b_ucet_full
                            b_kod = b_ucet_full.split('/')[1] if '/' in b_ucet_full else None

                            with ui.row().classes('flex-[1.5] no-wrap items-center gap-1'):
                                cislo_uctu = ui.input('Číslo účtu', value=b_cislo).classes('flex-[2] bg-white jen-cislo').props('outlined dense inputmode=numeric').on_value_change(vynut_pouze_integer)
                                ui.label('/').classes('text-2xl font-bold text-gray-400 pb-1')
                                banky_seznam = {
                                    '0100': '0100 (KB)', '0300': '0300 (ČSOB)', '0600': '0600 (MONETA)',
                                    '0800': '0800 (ČS)', '2010': '2010 (Fio)', '2700': '2700 (UniCredit)',
                                    '3030': '3030 (Air Bank)', '5500': '5500 (Raiffeisen)', '6210': '6210 (mBank)',
                                    '2070': '2070 (Trinity)', '2250': '2250 (Creditas)', '8150': '8150 (J&T)'
                                }
                                if b_kod and b_kod not in banky_seznam: banky_seznam[b_kod] = b_kod
                                kod_banky = ui.select(banky_seznam, label='Kód', value=b_kod, with_input=True, new_value_mode='add-unique').classes('flex-1 bg-white min-w-[100px]').props('outlined dense')

                        with ui.row().classes('w-full gap-4 mb-4'):
                            datum_vystaveni = ui.input('Datum vystavení *', value=prefill.get('datum_vystaveni', datetime.date.today().strftime('%Y-%m-%d'))).classes('flex-1 bg-white').props('type=date outlined dense')
                            duzp = ui.input('Datum D.U.Z.P. *', value=prefill.get('duzp', datetime.date.today().strftime('%Y-%m-%d'))).classes('flex-1 bg-white').props('type=date outlined dense')
                            splatnost = ui.input('Splatnost *', value=prefill.get('splatnost', '')).classes('flex-1 bg-white').props('type=date outlined dense')
                            platba = ui.select(['Převodem', 'Hotově', 'Fakturou', 'Kartou'], value=prefill.get('platba', 'Fakturou' if je_nakup else 'Převodem'), label='Platba').classes('flex-1 bg-white').props('outlined dense')

                        ui.label('Celkem v Kč bez DPH').classes('font-bold text-sm mt-4 mb-2')
                        with ui.row().classes('w-full gap-4 mb-4'):
                            val21 = parse_czk(prefill.get('castka_21', 0))
                            c21 = ui.input('Základ 21 % (Kč bez DPH)', value=f"{val21:,.2f}".replace(',', ' ').replace('.', ',') if val21 > 0 else "0").classes('flex-1 bg-white').props('outlined dense').tooltip('Zadejte částku bez DPH')
                            zaved_formatovani_castky(c21)

                            val12 = parse_czk(prefill.get('castka_12', 0))
                            c12 = ui.input('Základ 12 % (Kč bez DPH)', value=f"{val12:,.2f}".replace(',', ' ').replace('.', ',') if val12 > 0 else "0").classes('flex-1 bg-white').props('outlined dense').tooltip('Zadejte částku bez DPH')
                            zaved_formatovani_castky(c12)

                            val0 = parse_czk(prefill.get('castka_0', 0))
                            c0  = ui.input('Základ 0 % (Kč bez DPH)', value=f"{val0:,.2f}".replace(',', ' ').replace('.', ',') if val0 > 0 else "0").classes('flex-1 bg-white').props('outlined dense').tooltip('Zadejte částku bez DPH')
                            zaved_formatovani_castky(c0)

                            valcelk = parse_czk(prefill.get('castka_21', 0)) + parse_czk(prefill.get('castka_12', 0)) + parse_czk(prefill.get('castka_0', 0))
                            if valcelk == 0: valcelk = parse_czk(prefill.get('castka', 0))
                            castka = ui.input('CELKEM BEZ DPH', value=f"{valcelk:,.2f}".replace(',', ' ').replace('.', ',') if valcelk > 0 else "0").classes('flex-[1.5] font-bold bg-white border-2 border-blue-400').props('outlined dense').tooltip('Součet základů DPH — vypočítáno automaticky')
                            propoj_castky_celkem([c21, c12, c0], castka)

                        popis = ui.textarea('Popis (Předmět fakturace)', value=prefill.get('popis', '')).classes('w-full mb-4 bg-white').props('outlined dense rows=2')

                        ui.label('Schvalovatelé *').classes('font-bold text-sm mb-2')

                        def _prefill_schv(col_id, col_name):
                            """Vrátí ID pro select: preferuje ID sloupec, fallback přes jméno."""
                            vid = prefill.get(col_id)
                            if vid and vid in dost_schv: return vid
                            return _jmeno_na_id.get(str(prefill.get(col_name, '')).strip())

                        v1 = _prefill_schv('schvalovatel_id_1', 'schvalovatel') or vych_schv
                        v2 = _prefill_schv('schvalovatel_id_2', 'schvalovatel_2')
                        v3 = _prefill_schv('schvalovatel_id_3', 'schvalovatel_3')

                        with ui.row().classes('w-full gap-4 mb-4'):
                            schv1 = ui.select(dost_schv, value=v1, label='1. Schvalovatel').classes('flex-1 bg-white').props('outlined dense')
                            schv2 = ui.select(dost_schv, value=v2, label='2. Schvalovatel', clearable=True).classes('flex-1 bg-white').props('outlined dense')
                            schv3 = ui.select(dost_schv, value=v3, label='3. Schvalovatel', clearable=True).classes('flex-1 bg-white').props('outlined dense')

                        if edit_id and prefill.get('soubor_original'): ui.label(f"Zatím nahráno: {os.path.basename(prefill['soubor_original'])}").classes('text-xs text-blue-600 font-bold mb-1')

                        async def on_upload(e):
                            cesta = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{getattr(e, 'name', 'fa.pdf')}")
                            try:
                                obsah = None; zdroj = None
                                for attr in ['content', 'file', 'stream', 'data', 'file_obj']:
                                    if hasattr(e, attr):
                                        val = getattr(e, attr)
                                        if hasattr(val, 'read'): zdroj = val; break
                                if not zdroj:
                                    for attr in dir(e):
                                        val = getattr(e, attr)
                                        if hasattr(val, 'read'): zdroj = val; break
                                if zdroj:
                                    cteni = zdroj.read()
                                    if asyncio.iscoroutine(cteni): obsah = await cteni
                                    else: obsah = cteni
                                if obsah:
                                    with open(cesta, 'wb') as f: f.write(obsah)
                                    state['temp_upload_path'] = cesta
                                    ui.notify('PDF nahráno.', type='positive')
                                else: ui.notify('Nelze načíst data souboru.', type='negative')
                            except Exception as err: ui.notify(f'Chyba nahrávání: {err}', type='negative')

                        _upload_fa = ui.upload(on_upload=on_upload, auto_upload=True, max_file_size=10_000_000).classes('hidden')
                        _fa_upload_label = 'Nahrát PDF' if edit_id else 'Přiložit fakturu (PDF) *'
                        with ui.row().classes('items-center gap-2 mb-6'):
                            ui.button(icon='add', on_click=lambda: _upload_fa.run_method('pickFiles')).props('round dense size=sm color=primary').tooltip(_fa_upload_label)
                            ui.label(_fa_upload_label).classes('text-sm text-gray-600')

                        async def uloz_fakturu():
                            if not dodavatel.value or not cislo_fa.value or not str(castka.value).strip() or not schv1.value: return ui.notify('Vyplňte povinná pole!', type='warning')
                            if not state.get('temp_upload_path') and not edit_id: return ui.notify('Nahrajte PDF!', type='warning')
                            c_dph = parse_czk(castka.value); d21 = parse_czk(c21.value); d12 = parse_czk(c12.value); d0 = parse_czk(c0.value)
                            if c_dph <= 0: return ui.notify('Částka musí být > 0!', type='negative')

                            for ex in nacti_vsechny_faktury_rychle():
                                if edit_id and ex['id'] == edit_id: continue
                                if str(ex.get('cislo_faktury', '')).strip().upper() == str(cislo_fa.value).strip().upper(): return ui.notify('Faktura s tímto číslem již existuje!', type='negative')

                            def zapis_db():
                                fin_banka = f"{cislo_uctu.value.strip()}/{kod_banky.value.strip()}" if (kod_banky.value and cislo_uctu.value) else (cislo_uctu.value.strip() if cislo_uctu.value else '')

                                c = get_db_utf8()
                                cr = None
                                try:
                                    cr = c.cursor()
                                    fs = state.get('temp_upload_path') or prefill.get('soubor_original', '')
                                    _s1_jmeno = _id_na_jmeno.get(schv1.value, '')
                                    _s2_jmeno = _id_na_jmeno.get(schv2.value, '') if schv2.value else ''
                                    _s3_jmeno = _id_na_jmeno.get(schv3.value, '') if schv3.value else ''
                                    if ico.value and ico.value.strip() and dodavatel.value and dodavatel.value.strip():
                                        try: cr.execute("INSERT IGNORE INTO dodavatel_firma (ico, nazev) VALUES (%s, %s)", (ico.value.strip(), dodavatel.value.strip()))
                                        except Exception: pass
                                    if edit_id:
                                        cr.execute("UPDATE faktury SET cislo_objednavky=%s, dodavatel=%s, ico=%s, dic=%s, variabilni_symbol=%s, bankovni_ucet=%s, cislo_faktury=%s, datum_vystaveni=%s, duzp=%s, splatnost=%s, platba=%s, castka=%s, castka_21=%s, castka_12=%s, castka_0=%s, popis=%s, schvalovatel=%s, schvalovatel_2=%s, schvalovatel_3=%s, schvalovatel_id_1=%s, schvalovatel_id_2=%s, schvalovatel_id_3=%s, soubor_original=%s, stav='Ke schválení', opakena_zadost=1, schvaleno_1=NULL, schvaleno_2=NULL, schvaleno_3=NULL WHERE id=%s", (c_obj.value, dodavatel.value, ico.value, dic.value, vs.value, fin_banka, cislo_fa.value.strip(), str(datum_vystaveni.value), str(duzp.value), str(splatnost.value), platba.value, c_dph, d21, d12, d0, popis.value, _s1_jmeno, _s2_jmeno, _s3_jmeno, schv1.value, schv2.value or None, schv3.value or None, fs, edit_id))
                                    else:
                                        cr.execute("INSERT INTO faktury (id, cislo_objednavky, zadavatel, zadavatel_id, dodavatel, ico, dic, variabilni_symbol, bankovni_ucet, cislo_faktury, datum_vystaveni, duzp, splatnost, platba, castka, castka_21, castka_12, castka_0, popis, schvalovatel, schvalovatel_2, schvalovatel_3, schvalovatel_id_1, schvalovatel_id_2, schvalovatel_id_3, soubor_original, stav, datum_zadani) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Ke schválení', NOW())", (str(uuid.uuid4()), c_obj.value, user_name, user_id, dodavatel.value, ico.value, dic.value, vs.value, fin_banka, cislo_fa.value.strip(), str(datum_vystaveni.value), str(duzp.value), str(splatnost.value), platba.value, c_dph, d21, d12, d0, popis.value, _s1_jmeno, _s2_jmeno, _s3_jmeno, schv1.value, schv2.value or None, schv3.value or None, fs))
                                    c.commit()
                                finally:
                                    if cr: cr.close()
                                    if c: c.close()

                            await asyncio.to_thread(zapis_db)
                            intranet_logger.log_activity(user_name, "Faktury", f"Odeslána faktura č. {cislo_fa.value}")
                            state['temp_upload_path'] = None; state.pop('temp_prefill', None)
                            ui.notify('Odesláno ke schválení!', type='positive')
                            state['finance_vnitrni_tab'] = 'faktury_moje'
                            force_refresh_all()
                            _fa_cislo = cislo_fa.value
                            _fa_dodavatel = dodavatel.value
                            _fa_schv_ids = [schv1.value, schv2.value or None, schv3.value or None]
                            await asyncio.to_thread(posli_emaily_schvovatelum, _fa_schv_ids, 'Fakturu', _fa_cislo, _fa_dodavatel)

                        ui.button('ULOŽIT A ODESLAT ZNOVU' if edit_id else 'ODESLAT KE SCHVÁLENÍ', on_click=uloz_fakturu).classes(f"w-full text-white font-bold h-14 rounded-xl text-lg shadow-md {'bg-orange-500 hover:bg-orange-600' if edit_id else 'bg-blue-600 hover:bg-blue-700'}")

                # ==========================================
                # FAKTURY: MOJE A SCHVALOVÁNÍ (SDÍLENÝ KÓD)
                # ==========================================
                elif tab in ['faktury_moje', 'faktury_schvalovani']:
                    je_schv = (tab == 'faktury_schvalovani')
                    if je_schv and not muze_schv_f: ui.label('Odepřeno.'); return
                    if not je_schv and not muze_zadat_f: ui.label('Odepřeno.'); return

                    ui.label('Dashboard Schvalovatele' if je_schv else 'Moje odeslané faktury').classes('text-2xl font-bold mb-4 text-gray-800')

                    vsechny_f = nacti_vsechny_faktury_rychle()
                    dnes = datetime.date.today()
                    zobrazit = []

                    for f in vsechny_f:
                        f['_castka_fmt'] = formatuj_castku(f.get('castka'))
                        f['_zadani_fmt'] = rychly_format_data(f.get('datum_zadani'))
                        f['_splatnost_fmt'] = rychly_format_data(f.get('splatnost'))
                        try: sdt = datetime.datetime.strptime(str(f.get('splatnost', '')), '%Y-%m-%d').date()
                        except: sdt = dnes
                        f['_je_po'] = (sdt < dnes and f.get('stav') in ['Čeká', 'Ke schválení'])
                        f['_stav_txt'] = f"Po splatnosti ({(dnes - sdt).days} dní)" if f['_je_po'] else f.get('stav', '')
                        if 'Po splat' in f['_stav_txt']: f['_bg'] = 'bg-red-500'
                        elif f['_stav_txt'] in ['Čeká', 'Ke schválení']: f['_bg'] = 'bg-orange-500'
                        elif f['_stav_txt'] == 'Schváleno': f['_bg'] = 'bg-green-500'
                        else: f['_bg'] = 'bg-gray-500'

                        _je_zadavatel = (f.get('zadavatel_id') == user_id) or (f.get('zadavatel') == user_name)
                        _je_schvalovatel = is_admin_f or user_id in [f.get('schvalovatel_id_1'), f.get('schvalovatel_id_2'), f.get('schvalovatel_id_3')] or user_name in [f.get('schvalovatel'), f.get('schvalovatel_2'), f.get('schvalovatel_3')]
                        if not je_schv and _je_zadavatel: zobrazit.append(f)
                        elif je_schv:
                            if _je_schvalovatel:
                                if state['filter_zadavatel'] != 'Všichni' and f.get('zadavatel') != state['filter_zadavatel']: continue
                                st = state.get('filter_stav', 'Všechny')
                                if st != 'Všechny':
                                    if st == 'Po splatnosti' and not f['_je_po']: continue
                                    elif st != 'Po splatnosti' and f.get('stav') not in [st, 'Ke schválení']:
                                        if not (st == 'Čeká' and f.get('stav') in ['Čeká', 'Ke schválení']): continue

                                datum_vyst = str(f.get('datum_vystaveni', ''))[:10] if f.get('datum_vystaveni') else ''
                                if state.get('filter_od') and (not datum_vyst or datum_vyst < state['filter_od']): continue
                                if state.get('filter_do') and (not datum_vyst or datum_vyst > state['filter_do']): continue

                                zobrazit.append(f)

                    if je_schv:
                        zobrazit.sort(key=lambda x: (0 if x.get('stav') in ['Čeká', 'Ke schválení'] else 1, x.get('datum_zadani') or ''))

                        with ui.row().classes('w-full gap-4 mb-6 items-stretch'):
                            with ui.card().classes('flex-1 bg-orange-500 text-white p-4 shadow-md rounded-xl items-center text-center'):
                                ui.label(str(sum(1 for x in zobrazit if x.get('stav') in ['Čeká', 'Ke schválení']))).classes('text-4xl font-black mb-1')
                                ui.label('Čeká na schválení').classes('text-xs font-bold uppercase tracking-wider opacity-90')
                            with ui.card().classes('flex-1 bg-green-500 text-white p-4 shadow-md rounded-xl items-center text-center'):
                                ui.label(str(sum(1 for x in zobrazit if x.get('stav') == 'Schváleno'))).classes('text-4xl font-black mb-1')
                                ui.label('Schváleno celkem').classes('text-xs font-bold uppercase tracking-wider opacity-90')
                            with ui.card().classes('flex-[1.5] bg-blue-900 text-white p-4 shadow-md rounded-xl items-center text-center'):
                                ui.label(f"{formatuj_castku(sum(float(x.get('castka',0)) for x in zobrazit))} Kč").classes('text-4xl font-black mb-1 text-blue-200')
                                ui.label('Kč bez DPH celkem (Z aktuálně zobrazených faktur)').classes('text-xs font-bold uppercase tracking-wider opacity-80')

                        with ui.row().classes('w-full bg-white p-4 rounded-xl shadow-sm border border-gray-100 mb-6 items-center gap-4 flex-wrap'):
                            ui.select({'Všichni': 'Všichni žadatelé', **{z: z for z in sorted(set(f.get('zadavatel', '') for f in vsechny_f if f.get('zadavatel')))}}, value=state.get('filter_zadavatel', 'Všichni')).on_value_change(lambda e: state.update(filter_zadavatel=e.value)).classes('w-48 bg-gray-50').props('dense outlined')
                            ui.select({'Všechny': 'Všechny stavy', 'Čeká': 'Čeká na schválení', 'Schváleno': 'Schválené', 'Zamítnuto': 'Zamítnuté', 'Po splatnosti': 'Po splatnosti'}, value=state.get('filter_stav', 'Všechny')).on_value_change(lambda e: state.update(filter_stav=e.value)).classes('w-48 bg-gray-50').props('dense outlined')

                            ui.input('Datum od', value=state.get('filter_od', '')).on_value_change(lambda e: state.update(filter_od=e.value)).classes('w-32 bg-gray-50').props('type=date dense outlined clearable')
                            ui.input('Datum do', value=state.get('filter_do', '')).on_value_change(lambda e: state.update(filter_do=e.value)).classes('w-32 bg-gray-50').props('type=date dense outlined clearable')

                            ui.button('Filtrovat', on_click=obsah_panelu.refresh).classes('bg-blue-600 text-white font-bold ml-auto')
                            ui.button(icon='close', color='red', on_click=lambda: (state.update({'filter_zadavatel': 'Všichni', 'filter_stav': 'Všechny', 'filter_od': '', 'filter_do': ''}), obsah_panelu.refresh())).props('flat round')

                    # ----------------------------
                    # ROZLOŽENÍ SEZNAM A DETAIL
                    # ----------------------------
                    with ui.row().classes('w-full no-wrap gap-6 items-stretch mb-8').style('height: 85vh; min-height: 800px;'):
                        with ui.column().classes('w-[420px] max-w-[420px] shrink-0 h-full bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden flex flex-col no-wrap'):
                            ui.label(f'Zobrazené faktury ({len(zobrazit)})').classes('font-bold text-gray-800 text-sm uppercase p-4 border-b bg-gray-50 w-full shrink-0')

                            with ui.column().classes('w-full flex-1 overflow-y-auto p-3 gap-3'):
                                if not zobrazit: ui.label('Žádné faktury.').classes('text-gray-400 italic text-center w-full py-8')

                                def select_faktura(fid):
                                    state['vybrana_faktura_id'] = fid; state['show_details'] = True; obsah_panelu.refresh()

                                for f in zobrazit[:50]:
                                    iss = (f['id'] == state.get('vybrana_faktura_id'))
                                    bgk = 'bg-blue-50 border-blue-300 ring-1 ring-blue-500' if iss else 'bg-white border-gray-100 hover:bg-gray-50'
                                    with ui.card().classes(f'w-full p-4 shadow-none border rounded-xl cursor-pointer transition-colors {bgk}').on('click', lambda id=f['id']: select_faktura(id)):
                                        with ui.row().classes('w-full justify-between items-start mb-1'):
                                            with ui.column().classes('gap-0 flex-1 overflow-hidden pr-2'):
                                                ui.label(f.get('dodavatel', '')).classes('font-bold text-gray-800 truncate w-full')
                                                ui.label(f"FA: {f.get('cislo_faktury', '')} | Žadatel: {f.get('zadavatel', 'Neznámý')}").classes('text-xs text-gray-500 truncate w-full font-bold')
                                            ui.label(f"{f['_castka_fmt']} Kč").classes('font-black text-gray-900 shrink-0')
                                        with ui.row().classes('w-full justify-between items-center mt-3'):
                                            ui.label(f"Splatnost: {f['_splatnost_fmt']}" if f.get('stav') in ['Čeká', 'Ke schválení'] else f"Zadáno: {f['_zadani_fmt']}").classes('text-[11px] text-gray-500')
                                            ui.label(f['_stav_txt']).classes(f"text-white text-[10px] px-2 py-0.5 rounded font-bold uppercase tracking-wider {f['_bg']}")

                            # DETAIL DOLE VLEVO
                            ld = ui.column().classes('w-full shrink-0 bg-blue-50/20 overflow-y-auto transition-all').style('max-height: 60%;')

                        # PRAVÁ STRANA - PDF A INFO
                        pravy_panel = ui.column().classes('flex-1 min-w-0 w-full h-full p-0 m-0 overflow-hidden no-wrap flex flex-col')

                        vybrana = next((x for x in zobrazit if x['id'] == state.get('vybrana_faktura_id')), None)
                        if not vybrana:
                            with pravy_panel:
                                with ui.column().classes('w-full h-full items-center justify-center bg-white rounded-2xl border'):
                                    ui.icon('receipt_long', size='4rem', color='gray-300').classes('mb-4')
                                    ui.label('Vyberte fakturu vlevo.').classes('text-xl font-bold text-gray-400')
                        else:
                            sb = vybrana.get('soubor_schvaleny') if vybrana.get('stav') == 'Schváleno' and vybrana.get('soubor_schvaleny') else vybrana.get('soubor_original')
                            url_sb = f"/faktury_soubory/{urllib.parse.quote(os.path.basename(sb))}" if sb else ""

                            # DETAIL VLEVO DOLE
                            if state.get('show_details'):
                                ld.classes(add='p-5 border-t border-blue-100')
                                with ld:
                                    with ui.row().classes('w-full justify-between mb-1'):
                                        ui.label('Detaily faktury').classes('text-xs font-bold text-blue-800/60 uppercase tracking-widest')
                                        ui.button(icon='close', on_click=lambda: (state.update({'show_details': False}), obsah_panelu.refresh())).props('flat dense size=sm padding=none').classes('text-gray-500 hover:text-red-500')

                                    render_semafor(vybrana.get('stav'), True, vybrana)
                                    if vybrana.get('opakena_zadost'): ui.label('⚠️ OPAKOVANÁ ŽÁDOST').classes('w-full bg-red-600 text-white font-black text-center py-1 rounded text-xs mb-2')

                                    txt_info = f"FA: {vybrana.get('cislo_faktury')} | VS: {vybrana.get('variabilni_symbol')} | IČO: {vybrana.get('ico')}"
                                    if vybrana.get('dic'): txt_info += f" | DIČ: {vybrana.get('dic')}"
                                    if vybrana.get('bankovni_ucet'): txt_info += f" | Účet: {vybrana.get('bankovni_ucet')}"
                                    if vybrana.get('cislo_objednavky'): txt_info += f" | OBJ: {vybrana.get('cislo_objednavky')}"
                                    ui.label(txt_info).classes('text-xs font-bold text-gray-800 w-full mb-1 break-words whitespace-normal')

                                    ui.label(f"Vystaveno: {rychly_format_data(vybrana.get('datum_vystaveni'))} | D.U.Z.P.: {rychly_format_data(vybrana.get('duzp'))} | Splatnost: {vybrana['_splatnost_fmt']} | {vybrana.get('platba', 'Převodem')}").classes('text-xs font-bold text-gray-800 w-full mb-3 break-words whitespace-normal')

                                    with ui.row().classes('w-full gap-2 mb-2 flex-wrap'):
                                        for lbl, val in [('21%', vybrana.get('castka_21',0)), ('12%', vybrana.get('castka_12',0)), ('0%', vybrana.get('castka_0',0))]:
                                            ui.label(f"DPH {lbl}: {formatuj_castku(val)}").classes('bg-white px-2 py-1 border rounded text-[10px] font-bold')

                                    ui.label(vybrana.get('popis', '')).classes('text-xs text-gray-700 bg-white p-2 rounded border w-full mb-2')

                                    # PÁROVÁNÍ S OBJEDNÁVKOU
                                    c_obj = vybrana.get('cislo_objednavky')
                                    if c_obj:
                                        n_akce = next((x for x in nacti_vsechny_nakupy_rychle() if x['cislo_akce'] == c_obj), None)
                                        if n_akce:
                                            # Použijeme primárně nová pole pro výpočet ceny nákupu
                                            o_cena = float(n_akce.get('castka_celkem', 0) or 0)

                                            # Zpětná kompatibilita pro staré objednávky
                                            if o_cena == 0:
                                                for l in str(n_akce['polozky']).split('\n'):
                                                    if not l.strip(): continue
                                                    m, c = 1.0, 0.0
                                                    if "(Cena:" in l and "Kč)" in l:
                                                        c_str = l.split("(Cena:")[1].split("Kč)")[0].strip()
                                                        c = parse_czk(c_str)
                                                    if l.startswith('•'):
                                                        pts = l[1:].strip().split('-',1)
                                                        if len(pts)==2:
                                                            mj = pts[0].strip().split(' ',1)
                                                            if len(mj)==2:
                                                                m = parse_czk(mj[0])
                                                    o_cena += (m * c)

                                            f_cena = parse_czk(vybrana.get('castka_21',0)) + parse_czk(vybrana.get('castka_12',0)) + parse_czk(vybrana.get('castka_0',0))
                                            if f_cena == 0: f_cena = parse_czk(vybrana.get('castka',0))

                                            is_ok = (round(f_cena, 2) == round(o_cena, 2))

                                            def ukaz_nahled_nakupu(n_dict):
                                                with ui.dialog() as dlg_nahled, ui.card().classes('w-full max-w-3xl p-6 rounded-xl bg-white max-h-[85vh] overflow-y-auto min-w-0'):
                                                    with ui.row().classes('w-full justify-between items-start border-b border-gray-100 pb-4 mb-4'):
                                                        with ui.column().classes('gap-1 min-w-0'):
                                                            ui.label(f"{n_dict['typ'].upper()} #{n_dict['cislo_akce']}").classes('text-blue-600 font-bold text-sm tracking-widest')
                                                            ui.label(n_dict['nazev_akce']).classes('text-2xl font-black text-gray-800 break-words whitespace-normal')

                                                            with ui.row().classes('items-center gap-2 mt-1 bg-gray-50 p-2 rounded-lg border border-gray-200 flex-wrap'):
                                                                ui.icon('person', size='sm', color='gray-600')
                                                                ui.label(f"Poptával/Založil: {n_dict.get('zadavatel', 'Neznámý')}").classes('text-gray-800 font-bold text-sm break-words whitespace-normal')

                                                            ui.label(f"Dodavatel: {n_dict['dodavatel']}").classes('text-gray-600 text-sm mt-2 break-words whitespace-normal')
                                                        ui.button(icon='close', on_click=dlg_nahled.close).props('flat round').classes('text-gray-500 bg-gray-100 hover:bg-gray-200 shrink-0')

                                                    with ui.row().classes('w-full gap-4 mb-4 flex-wrap'):
                                                        ui.label(f"Termín: {n_dict['termin_dodani'] or 'Neurčeno'}").classes('font-bold text-xs bg-gray-100 text-gray-700 px-3 py-1 rounded-full')

                                                        zobrazovany_stav = 'Čeká se na schválení faktury' if vybrana.get('stav') in ['Čeká', 'Ke schválení'] else n_dict['stav']
                                                        ui.label(f"Stav: {zobrazovany_stav}").classes('font-bold text-xs bg-blue-100 text-blue-800 px-3 py-1 rounded-full')

                                                    ui.label('Schválené položky:').classes('font-bold text-gray-700 mb-2')
                                                    ui.label(n_dict['polozky']).classes('w-full bg-blue-50/50 p-4 rounded-lg text-sm break-words whitespace-normal border border-blue-100 mb-4 text-gray-800')

                                                    vc_nahled = parse_czk(n_dict.get('castka_celkem', 0))
                                                    if vc_nahled > 0:
                                                        ui.label('Rozpad schválené ceny (bez DPH):').classes('font-bold text-gray-700 mb-1')
                                                        with ui.row().classes('w-full gap-2 mb-4 flex-wrap'):
                                                            for lbl, val in [('21%', n_dict.get('castka_21',0)), ('12%', n_dict.get('castka_12',0)), ('0%', n_dict.get('castka_0',0))]:
                                                                if parse_czk(val) > 0:
                                                                    ui.label(f"DPH {lbl}: {formatuj_castku(val)}").classes('bg-gray-100 px-2 py-1 border rounded text-[10px] font-bold')
                                                            ui.label(f"CELKEM: {formatuj_castku(vc_nahled)} Kč").classes('bg-green-50 text-green-800 px-2 py-1 border border-green-200 rounded text-[11px] font-black')

                                                    if n_dict.get('doplnujici_info'):
                                                        ui.label('Doplňující informace od zadavatele:').classes('font-bold text-gray-700 mb-2')
                                                        ui.label(n_dict['doplnujici_info']).classes('w-full bg-gray-50 p-4 rounded-lg text-sm break-words whitespace-normal border border-gray-200 mb-4 text-gray-700')

                                                    if n_dict.get('reakce_dodavatele'):
                                                        ui.label('Vyjádření dodavatele / Poznámky:').classes('font-bold text-gray-700 mb-2')
                                                        ui.label(n_dict['reakce_dodavatele']).classes('w-full bg-yellow-50 p-4 rounded-lg text-sm break-words whitespace-normal border border-yellow-200 mb-4 text-gray-800')

                                                    if n_dict.get('soubor_priloha'):
                                                        url_priloha = f"/prilohy_nakup/{urllib.parse.quote(os.path.basename(n_dict['soubor_priloha']))}"
                                                        ui.button('Otevřít přiloženou objednávku (PDF)', icon='open_in_new', on_click=lambda: ui.navigate.to(url_priloha, new_tab=True)).classes('bg-blue-600 hover:bg-blue-700 text-white font-bold w-full h-12 shadow-md mt-4 rounded-xl')

                                                dlg_nahled.open()

                                            _ma_korekci = bool(n_akce.get('korekce_ceny_duvod'))
                                            _border_col = 'border-amber-400 bg-amber-50/50' if _ma_korekci else ('border-green-500 bg-green-50/50' if is_ok else 'border-red-500 bg-red-50/50')
                                            with ui.card().classes(f"w-full p-4 mt-2 rounded-xl border-l-4 {_border_col}"):
                                                with ui.row().classes('w-full justify-between items-center mb-2 border-b border-gray-200 pb-2'):
                                                    ui.label('Kontrola proti objednávce').classes(f"font-bold text-[11px] uppercase tracking-wider {'text-amber-800' if _ma_korekci else ('text-green-800' if is_ok else 'text-red-800')}")
                                                    ui.button('Náhled objednávky', icon='zoom_in', on_click=lambda n=n_akce: ukaz_nahled_nakupu(n)).props('flat dense size=sm padding=xs').classes('text-blue-600 hover:bg-blue-100 rounded')

                                                # Varování o korekci ceny
                                                if _ma_korekci:
                                                    kor_datum = n_akce.get('korekce_ceny_datum')
                                                    kor_datum_str = kor_datum.strftime('%d.%m.%Y %H:%M') if isinstance(kor_datum, datetime.datetime) else str(kor_datum or '')[:16]
                                                    with ui.card().classes('w-full p-3 bg-amber-100 border border-amber-300 rounded-lg mb-3'):
                                                        ui.label('⚠ Pozor — byl proveden zásah do potvrzené ceny objednávky').classes('text-amber-800 font-black text-[11px] uppercase tracking-wide mb-1')
                                                        ui.label(f"Důvod: {n_akce['korekce_ceny_duvod']}").classes('text-amber-900 text-xs font-semibold break-words whitespace-normal')
                                                        ui.label(f"Původní cena: {formatuj_castku(n_akce.get('korekce_ceny_puvodni', 0))} Kč  →  Nová cena: {formatuj_castku(n_akce.get('korekce_ceny_castka', 0))} Kč").classes('text-amber-700 text-[10px] font-bold mt-1')
                                                        ui.label(f"Provedl: {n_akce.get('korekce_ceny_uzivatel','')}  |  {kor_datum_str}").classes('text-amber-600 text-[10px] mt-1')

                                                with ui.row().classes('w-full justify-between items-center flex-wrap gap-2'):
                                                    ui.label(f"Schválený Nákup (bez DPH): {formatuj_castku(o_cena)} Kč").classes('text-[11px] font-bold text-gray-700')
                                                    ui.label(f"Faktura (bez DPH): {formatuj_castku(f_cena)} Kč").classes('text-[11px] font-bold text-gray-700')

                                                if o_cena == 0:
                                                    ui.label('❕ Původní nákup neměl vyplněné ceny').classes('text-orange-600 font-bold text-[10px] mt-1 break-words whitespace-normal')
                                                elif not is_ok:
                                                    rozdil = f_cena - o_cena
                                                    text_rozdilu = f"dražší o {formatuj_castku(rozdil)}" if rozdil > 0 else f"levnější o {formatuj_castku(abs(rozdil))}"
                                                    ui.label(f'⚠ UPOZORNĚNÍ: Faktura je {text_rozdilu} Kč!').classes('text-red-600 font-bold text-[11px] mt-2 bg-white px-2 py-1 rounded border border-red-200 inline-block shadow-sm break-words whitespace-normal')
                                                else:
                                                    ui.label('✅ Fakturovaná částka přesně odpovídá.').classes('text-green-700 font-bold text-[11px] mt-2 inline-block break-words whitespace-normal')

                                    # SCHVALOVACÍ SEKCE
                                    _je_zadavatel_vybrane = (vybrana.get('zadavatel_id') == user_id) or (vybrana.get('zadavatel') == user_name)
                                    if je_schv and vybrana.get('stav') in ['Čeká', 'Ke schválení']:
                                        pending = []
                                        muzu_schvalit = None
                                        for i, c_nm, c_tm, c_id in [(1, 'schvalovatel', 'schvaleno_1', 'schvalovatel_id_1'), (2, 'schvalovatel_2', 'schvaleno_2', 'schvalovatel_id_2'), (3, 'schvalovatel_3', 'schvaleno_3', 'schvalovatel_id_3')]:
                                            jm = vybrana.get(c_nm)
                                            if jm:
                                                cas = vybrana.get(c_tm)
                                                if cas: ui.label(f"✅ {jm}").classes('text-green-600 font-bold mb-1 text-xs')
                                                else:
                                                    ui.label(f"⏳ {jm} (Čeká se)").classes('text-orange-600 font-bold mb-1 text-xs')
                                                    pending.append((i, c_tm, jm))
                                                    _je_muj = jm == user_name or vybrana.get(c_id) == user_id or is_admin_f
                                                    if not muzu_schvalit and _je_muj: muzu_schvalit = (i, c_tm, jm)

                                        if muzu_schvalit:
                                            t_idx, t_col, t_jm = muzu_schvalit
                                            is_final = (len(pending) == 1)

                                            def akce_schvalit():
                                                f_id, p_pdf = vybrana['id'], vybrana.get('soubor_original')
                                                _zad_id = vybrana.get('zadavatel_id')
                                                _cislo = vybrana.get('cislo_objednavky') or vybrana.get('cislo_faktury', '')
                                                _nazev = vybrana.get('dodavatel', '')
                                                with ui.dialog() as ds, ui.card().classes('p-6 rounded-xl max-w-sm'):
                                                    ui.label('Schválit fakturu?').classes('text-xl font-bold mb-4 text-green-600')
                                                    async def on_yes():
                                                        ds.close()
                                                        d_ted = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                                        def db_fin():
                                                            c = get_db_utf8()
                                                            cr = None
                                                            try:
                                                                cr = c.cursor()
                                                                cr.execute(f"UPDATE faktury SET {t_col}=%s, stav='Schváleno', soubor_schvaleny=%s, datum_schvaleni=%s WHERE id=%s", (d_ted, p_pdf, d_ted, f_id))
                                                                if vybrana.get('cislo_objednavky'): cr.execute("UPDATE nakup_proces SET stav='Faktura schválena' WHERE cislo_akce=%s", (vybrana['cislo_objednavky'],))
                                                                c.commit()
                                                            finally:
                                                                if cr: cr.close()
                                                                if c: c.close()

                                                        def db_par():
                                                            c = get_db_utf8()
                                                            cr = None
                                                            try:
                                                                cr = c.cursor()
                                                                cr.execute(f"UPDATE faktury SET {t_col}=%s WHERE id=%s", (d_ted, f_id))
                                                                c.commit()
                                                            finally:
                                                                if cr: cr.close()
                                                                if c: c.close()

                                                        if is_final:
                                                            await asyncio.to_thread(db_fin)
                                                            await asyncio.to_thread(posli_email_zadateli, _zad_id, 'schvalena_faktura', _cislo, _nazev)
                                                            ui.notify('Faktura schválena — přesouvá se do „Faktury k zadání“.', type='positive')
                                                        else:
                                                            await asyncio.to_thread(db_par)
                                                            ui.notify('Částečně schváleno.', type='positive')

                                                        intranet_logger.log_activity(user_name, "Faktury", f"Schválena FA {vybrana.get('cislo_faktury')}")
                                                        force_refresh_all()
                                                    ui.button('Schválit', color='green', on_click=on_yes).classes('w-full')
                                                ds.open()

                                            def akce_zamitnout():
                                                with ui.dialog() as dz, ui.card().classes('p-6 rounded-xl max-w-sm'):
                                                    ds = ui.select(['Nesouhlasím s cenou', 'Chybí informace', 'Jiné'], label='Důvod').classes('w-full mb-2')
                                                    dt = ui.input('Poznámka').classes('w-full mb-4')
                                                    async def on_z():
                                                        if not ds.value: return ui.notify('Zvolte důvod!', type='warning')
                                                        txt = f"{ds.value}: {dt.value}".strip()
                                                        _zad_id = vybrana.get('zadavatel_id')
                                                        _cislo = vybrana.get('cislo_objednavky') or vybrana.get('cislo_faktury', '')
                                                        _nazev = vybrana.get('dodavatel', '')
                                                        d_ted = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                                        def db_z():
                                                            c = get_db_utf8()
                                                            cr = None
                                                            try:
                                                                cr = c.cursor()
                                                                cr.execute("UPDATE faktury SET stav='Zamítnuto', datum_schvaleni=%s, duvod_zamitnuti=%s WHERE id=%s", (d_ted, txt, vybrana['id']))
                                                                c.commit()
                                                            finally:
                                                                if cr: cr.close()
                                                                if c: c.close()
                                                        await asyncio.to_thread(db_z)
                                                        await asyncio.to_thread(posli_email_zadateli, _zad_id, 'zamitnuta_faktura', _cislo, _nazev, txt)
                                                        ui.notify('Zamítnuto.', type='negative'); dz.close(); force_refresh_all()
                                                    ui.button('Zamítnout', color='red', on_click=on_z).classes('w-full')
                                                dz.open()

                                            with ui.row().classes('w-full gap-2 no-wrap mt-4'):
                                                j_btn = str(t_jm).split()[0].upper() if t_jm else 'OK'
                                                ui.button(f'SCHVÁLIT: {j_btn}', color='green', icon='check', on_click=akce_schvalit).classes('flex-1 text-[10px] px-1')
                                                ui.button('ZAMÍTNOUT', color='red', icon='close', on_click=akce_zamitnout).classes('flex-1 text-[10px] px-1').props('flat')

                                    elif not je_schv and vybrana.get('stav') == 'Zamítnuto' and _je_zadavatel_vybrane:
                                        ui.label(f"Důvod: {vybrana.get('duvod_zamitnuti')}").classes('text-xs text-red-600 font-bold w-full mt-2')
                                        def naprava():
                                            state['temp_prefill'] = {
                                                'edit_id': vybrana['id'], 'cislo_akce': vybrana.get('cislo_objednavky', ''), 'zadavatel': vybrana.get('zadavatel', ''),
                                                'dodavatel': vybrana.get('dodavatel', ''), 'ico': vybrana.get('ico', ''), 'dic': vybrana.get('dic', ''),
                                                'cislo_faktury': vybrana.get('cislo_faktury', ''), 'vs': vybrana.get('variabilni_symbol', ''), 'bankovni_ucet': vybrana.get('bankovni_ucet', ''),
                                                'datum_vystaveni': str(vybrana.get('datum_vystaveni', ''))[:10] if vybrana.get('datum_vystaveni') else '',
                                                'duzp': str(vybrana.get('duzp', ''))[:10], 'splatnost': str(vybrana.get('splatnost', ''))[:10], 'platba': vybrana.get('platba', 'Převodem'),
                                                'castka_21': vybrana.get('castka_21', 0), 'castka_12': vybrana.get('castka_12', 0), 'castka_0': vybrana.get('castka_0', 0),
                                                'castka': vybrana.get('castka', 0), 'popis': vybrana.get('popis', ''),
                                                'schvalovatel': vybrana.get('schvalovatel', ''), 'schvalovatel_id_1': vybrana.get('schvalovatel_id_1'),
                                                'schvalovatel_2': vybrana.get('schvalovatel_2', ''), 'schvalovatel_id_2': vybrana.get('schvalovatel_id_2'),
                                                'schvalovatel_3': vybrana.get('schvalovatel_3', ''), 'schvalovatel_id_3': vybrana.get('schvalovatel_id_3'),
                                                'soubor_original': vybrana.get('soubor_original', '')
                                            }
                                            state['finance_vnitrni_tab'] = 'faktury_nova'
                                            force_refresh_all()
                                        ui.button('Upravit a odeslat znovu', icon='edit', on_click=naprava).classes('bg-orange-500 hover:bg-orange-600 text-white w-full mt-2 text-xs h-8 rounded-lg shadow-sm')

                                    # TLAČÍTKO SMAZAT (pro adminy, nebo zadavatele)
                                    muze_smazat = is_admin_f or (_je_zadavatel_vybrane and vybrana.get('stav') in ['Čeká', 'Ke schválení', 'Zamítnuto'])
                                    if muze_smazat:
                                        def akce_smazat():
                                            f_id = vybrana['id']
                                            with ui.dialog() as dlg_del, ui.card().classes('p-6 rounded-xl w-full max-w-sm'):
                                                ui.label('Smazání faktury').classes('text-xl font-bold mb-4 text-red-600')
                                                ui.label('Opravdu smazat včetně PDF? Tento krok nelze vrátit.').classes('mb-6 text-gray-700')
                                                async def potvrdit():
                                                    dlg_del.close()
                                                    smazano = await asyncio.to_thread(smaz_fakturu_z_db, f_id)
                                                    if smazano:
                                                        intranet_logger.log_activity(user_name, "Faktury", f"Smazána faktura ze systému (Číslo: {vybrana.get('cislo_faktury')})")
                                                        vynut_obnovu_faktur()
                                                        ui.notify('Smazáno.', type='info')
                                                        state['vybrana_faktura_id'] = None
                                                        state['show_details'] = False
                                                        force_refresh_all()
                                                    else:
                                                        ui.notify('Chyba při mazání!', type='negative')
                                                with ui.row().classes('w-full justify-between'):
                                                    ui.button('Zrušit', on_click=dlg_del.close).classes('bg-gray-400 text-white font-bold')
                                                    ui.button('Smazat', on_click=potvrdit).classes('bg-red-600 text-white font-bold shadow-md')
                                            dlg_del.open()

                                        ui.button('Smazat fakturu', icon='delete', color='red', on_click=akce_smazat).props('flat').classes('w-full mt-4 text-[11px] text-gray-500 hover:text-red-500 bg-red-50 h-8 rounded-lg')

                            else:
                                ld.classes(add='p-2 border-t border-blue-100 justify-center')
                                with ld: ui.button('Zobrazit detaily k vybrané faktuře', icon='expand_less', on_click=lambda: (state.update({'show_details': True}), obsah_panelu.refresh())).props('flat dense size=sm').classes('w-full text-blue-600 font-bold')

                            with pravy_panel:
                                with ui.column().classes('w-full h-full bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden flex flex-col no-wrap'):
                                    with ui.row().classes('w-full bg-white p-6 border-b border-gray-100 justify-between items-center shrink-0'):
                                        with ui.row().classes('items-center gap-4'):
                                            ui.icon('description', size='2rem', color='blue-600')
                                            with ui.column().classes('gap-0'):
                                                ui.label(vybrana.get('dodavatel', '')).classes('text-2xl font-black text-gray-900')
                                                ui.label(f"Zadal(a): {vybrana.get('zadavatel', '')}").classes('text-sm font-bold text-gray-500')
                                        with ui.row().classes('items-center gap-6'):
                                            with ui.column().classes('items-end gap-0'):
                                                ui.label('Celkem k úhradě').classes('text-xs text-gray-500 font-bold uppercase')
                                                ui.label(f"{vybrana['_castka_fmt']} Kč").classes('text-2xl font-black text-blue-600')
                                            if url_sb: ui.button(icon='open_in_new', on_click=lambda u=url_sb: ui.navigate.to(u, new_tab=True)).props('flat round').classes('text-gray-400 hover:text-blue-600')
                                    with ui.column().classes('flex-1 p-0 w-full h-full overflow-hidden bg-gray-200'):
                                        if url_sb: ui.element('iframe').props(f'src="{url_sb}?v={int(datetime.datetime.now().timestamp())}#view=FitH" type="application/pdf"').classes('w-full h-full border-0 rounded-2xl')

                # ==========================================
                # FAKTURY: K ZADÁNÍ DO ÚČETNICTVÍ
                # ==========================================
                elif tab == 'faktury_ucetnictvi' and muze_schv_f:
                    ui.label('Faktury k zadání do účetnictví').classes('text-2xl font-bold mb-2 text-gray-800')
                    ui.label('Schválené faktury připravené k zaúčtování. Otevřete fakturu, zkontrolujte ji a zadejte do účetnictví.').classes('text-sm text-gray-500 mb-4')

                    fa_ucet = [f for f in nacti_vsechny_faktury_rychle() if f.get('stav') == 'Schváleno']
                    if not fa_ucet:
                        ui.label('Žádné faktury nečekají na zadání do účetnictví.').classes('text-gray-500 italic mt-4')
                    else:
                        vyb_id = state.get('ucetni_vybrana_id')
                        if vyb_id not in [f['id'] for f in fa_ucet]:
                            vyb_id = fa_ucet[0]['id']; state['ucetni_vybrana_id'] = vyb_id
                        with ui.element('div').style('display:grid;grid-template-columns:340px 1fr;gap:16px;width:100%;min-height:600px'):
                            with ui.column().classes('w-full gap-2 min-w-0'):
                                for f in fa_ucet:
                                    akt = (f['id'] == vyb_id)
                                    bgk = 'bg-blue-50 border-blue-400' if akt else 'bg-white border-gray-200'
                                    def vyber(fid=f['id']):
                                        state['ucetni_vybrana_id'] = fid; obsah_panelu.refresh()
                                    with ui.card().classes(f'w-full p-3 shadow-none border rounded-xl cursor-pointer {bgk}').on('click', vyber):
                                        ui.label(f.get('dodavatel', '')).classes('font-bold text-gray-800 text-sm')
                                        ui.label(f"Obj.: {f.get('cislo_objednavky') or '—'} · {formatuj_castku(f.get('castka', 0))} Kč").classes('text-xs text-gray-500')
                                        ui.label(f"Zadal(a): {f.get('zadavatel', '')}").classes('text-[11px] text-gray-400')
                            vyb = next((f for f in fa_ucet if f['id'] == vyb_id), None)
                            with ui.column().classes('w-full min-w-0'):
                                if vyb:
                                    sb = vyb.get('soubor_schvaleny') or vyb.get('soubor_original')
                                    url_sb = f"/faktury_soubory/{urllib.parse.quote(os.path.basename(sb))}" if sb else ''
                                    with ui.row().classes('w-full justify-between items-center bg-white p-4 rounded-2xl border border-gray-100 mb-2 no-wrap'):
                                        with ui.column().classes('gap-0 min-w-0'):
                                            ui.label(vyb.get('dodavatel', '')).classes('text-xl font-black text-gray-900 truncate')
                                            ui.label(f"Objednávka: {vyb.get('cislo_objednavky') or '—'} · Celkem {formatuj_castku(vyb.get('castka', 0))} Kč").classes('text-sm text-gray-500')
                                        async def zadat_ucetnictvi(v=vyb):
                                            _zad_id = v.get('zadavatel_id'); _cislo = v.get('cislo_objednavky') or v.get('cislo_faktury', ''); _nazev = v.get('dodavatel', '')
                                            def _db():
                                                c = get_db_utf8(); cr = None
                                                try:
                                                    cr = c.cursor()
                                                    cr.execute("UPDATE faktury SET stav='Zaúčtováno' WHERE id=%s", (v['id'],))
                                                    if v.get('cislo_objednavky'): cr.execute("UPDATE nakup_proces SET stav='Uzavřeno' WHERE cislo_akce=%s", (v['cislo_objednavky'],))
                                                    c.commit()
                                                finally:
                                                    if cr: cr.close()
                                                    if c: c.close()
                                            await asyncio.to_thread(_db)
                                            await asyncio.to_thread(posli_email_zadateli, _zad_id, 'zadano_ucetnictvi', _cislo, _nazev)
                                            intranet_logger.log_activity(user_name, "Faktury", f"Zadáno do účetnictví: {v.get('cislo_faktury')}")
                                            state['ucetni_vybrana_id'] = None
                                            ui.notify('Faktura zadána do účetnictví. Objednávka uzavřena.', type='positive')
                                            force_refresh_all()
                                        ui.button('Zadat do účetnictví', icon='account_balance', on_click=zadat_ucetnictvi).classes('bg-purple-600 hover:bg-purple-700 text-white font-bold h-12 px-6 shadow-lg shrink-0')
                                    with ui.column().classes('w-full bg-gray-200 rounded-2xl overflow-hidden').style('height:70vh'):
                                        if url_sb: ui.element('iframe').props(f'src="{url_sb}?v={int(datetime.datetime.now().timestamp())}#view=FitH" type="application/pdf"').classes('w-full h-full border-0')
                                        else: ui.label('K faktuře není přiložen PDF soubor.').classes('p-6 text-gray-500')

            obsah_panelu()