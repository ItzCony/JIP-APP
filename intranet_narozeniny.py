# ==========================================
# MODUL NAROZENINY — CSV databanka
# ==========================================
from nicegui import ui, app, events
import intranet_data
import intranet_static
import intranet_emaily
import intranet_logger
import asyncio
import datetime
import csv
import html as _html
import io
import json
import os
import unicodedata
from urllib.parse import quote as _url_quote

EMAIL_DOMENA = 'jip-napoje.cz'

DATA_FILE        = 'narozeniny_data.json'
PODPISY_DIR      = 'narozeniny_podpisy'
PODPISY_EXTS     = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
PODPISY_URL_BASE = '/narozeniny_podpisy_static'

os.makedirs(PODPISY_DIR, exist_ok=True)
# Náhled podpisů v administraci. Do odchozích e-mailů se podpisy vkládají jako
# inline přílohy (cid:), takže omezení na přihlášené nic nerozbije.
intranet_static.chranene_soubory(PODPISY_URL_BASE, PODPISY_DIR)


def _ziskej_podpisy() -> list[str]:
    """Vrátí seřazený seznam cest k obrázkům podpisů."""
    try:
        return sorted(
            os.path.join(PODPISY_DIR, f)
            for f in os.listdir(PODPISY_DIR)
            if os.path.splitext(f)[1].lower() in PODPISY_EXTS
        )
    except Exception:
        return []


def _smazat_podpis(cesta: str):
    try:
        os.remove(cesta)
    except Exception as e:
        print(f'[narozeniny] Nelze smazat podpis {cesta}: {e}')


def _img_url(cesta: str) -> str:
    """Vrátí URL servírovanou přes app.add_static_files, s percent-encoding pro non-ASCII znaky."""
    return f'{PODPISY_URL_BASE}/{_url_quote(os.path.basename(cesta))}'

MESICE_CZ = [
    '', 'Leden', 'Únor', 'Březen', 'Duben', 'Květen', 'Červen',
    'Červenec', 'Srpen', 'Září', 'Říjen', 'Listopad', 'Prosinec'
]

# ==========================================
# HELPERS
# ==========================================

def _parsuj_datum(dn_str):
    """Vrátí datetime.date nebo None — akceptuje YYYY-MM-DD i DD.MM.YYYY."""
    if not dn_str:
        return None
    s = str(dn_str).strip()
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ('%d.%m.%Y', '%d.%m.%y', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def _birthday_tuple(dn_str):
    d = _parsuj_datum(dn_str)
    return (d.month, d.day) if d else None

def _dni_do_narozenin(dn_str):
    bt = _birthday_tuple(dn_str)
    if bt is None:
        return 9999
    today = datetime.date.today()
    try:
        bday = datetime.date(today.year, bt[0], bt[1])
    except ValueError:
        return 9999
    if bday < today:
        try:
            bday = datetime.date(today.year + 1, bt[0], bt[1])
        except ValueError:
            return 9999
    return (bday - today).days

def _vek(dn_str):
    d = _parsuj_datum(dn_str)
    if not d:
        return None
    t = datetime.date.today()
    return t.year - d.year - ((t.month, t.day) < (d.month, d.day))

def _datum_cz(dn_str):
    """Datum ve formátu DD.MM.YYYY pro zobrazení."""
    d = _parsuj_datum(dn_str)
    return d.strftime('%d.%m.%Y') if d else (dn_str or '—')

def _je_kulatiny(vek) -> bool:
    """Vrátí True pokud věk je kulatý (násobek 10, min. 10)."""
    return vek is not None and vek >= 10 and vek % 10 == 0


def _odstran_diakritiku(s: str) -> str:
    """'Březina' → 'Brezina', 'Čermáková' → 'Cermakova'."""
    nfkd = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def _vygeneruj_email(jmeno: str, prijmeni: str) -> str:
    """'Petr', 'Březina' → 'petr.brezina@jip-napoje.cz'. Vrátí '' pokud chybí jméno nebo příjmení."""
    def _norm(s: str) -> str:
        s = _odstran_diakritiku(s).lower().strip()
        casti = [''.join(ch for ch in cast if ch.isalnum()) for cast in s.split()]
        return '.'.join(c for c in casti if c)
    j = _norm(jmeno)
    p = _norm(prijmeni)
    if not j or not p:
        return ''
    return f'{j}.{p}@{EMAIL_DOMENA}'


def _norm_jmeno(s: str) -> str:
    """Normalizace pro porovnání jména/příjmení: bez diakritiky, lowercase, sloučené mezery."""
    if not s:
        return ''
    bez_dia = _odstran_diakritiku(s).lower().strip()
    return ' '.join(bez_dia.split())


def _parsuj_cislo_jip(s: str) -> tuple[str, int | None]:
    """
    Rozpadne identifikátor typu 'OZ000001' na (příznak, osobní číslo).
    Příznak = úvodní písmena, os. číslo = zbytek převedený na int (zahodí vodící nuly).
    'OZ000001' → ('OZ', 1) · 'JV010002' → ('JV', 10002) · 'I010156' → ('I', 10156).
    Pokud zbytek není čistě číselný (např. 'I10S0130'), vrátí (priznak, None).
    """
    s = (s or '').strip()
    if not s:
        return ('', None)
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    priznak = s[:i]
    rest = s[i:]
    if not rest or not rest.isdigit():
        return (priznak, None)
    try:
        return (priznak, int(rest))
    except ValueError:
        return (priznak, None)


def _neni_v_zam_pomeru(s: str) -> bool:
    """True pokud hodnota sloupce 'je_v_zam_pomeru' jednoznačně říká NE (0/ne/false).
    Prázdná/chybějící hodnota → False (řádek se neblokuje)."""
    v = (s or '').strip().lower()
    if not v:
        return False
    return v in {'0', '0.0', 'ne', 'n', 'false', 'no', 'nepracuje'}


# ==========================================
# DATA — JSON soubor
# ==========================================

def _system_lookup() -> tuple[dict[tuple[str, int], dict], dict[str, dict]]:
    """Vrátí dvě mapy uživatelů ze systému na jejich data (udata):

    - podle_klice: (PŘÍZNAK, os. číslo) → udata
    - podle_emailu: email_lower → udata  (fallback pro starší záznamy)

    Os. číslo v systému = id uživatele (iduser) — stejná konvence jako při importu.
    Prázdné mapy znamenají DB nedostupná → volající NEfiltruje (fail-open)."""
    podle_klice: dict[tuple[str, int], dict] = {}
    podle_emailu: dict[str, dict] = {}
    try:
        for em, ud in intranet_data.ziskej_vsechny_uzivatele().items():
            em_l = (em or '').strip().lower()
            if em_l:
                podle_emailu[em_l] = ud
            pn = (ud.get('priznak_nazev') or '').strip().upper()
            oc = ud.get('id')
            if pn and oc not in (None, ''):
                try:
                    podle_klice[(pn, int(oc))] = ud
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        print(f'[narozeniny] _system_lookup: {e}')
    return podle_klice, podle_emailu


def _najdi_udata(p: dict, podle_klice: dict, podle_emailu: dict) -> dict | None:
    """Najde systémová data uživatele k osobě z databanky — příznak + os. číslo,
    fallback e-mail (stejné párování jako nacti_data). None = v systému není."""
    pn = (p.get('priznak') or '').strip().upper()
    oc = p.get('osobni_cislo')
    if pn and oc not in (None, ''):
        try:
            ud = podle_klice.get((pn, int(oc)))
            if ud is not None:
                return ud
        except (TypeError, ValueError):
            pass
    return podle_emailu.get((p.get('email') or '').strip().lower())

def nacti_data() -> list:
    """Načte seznam lidí z narozeniny_data.json a nechá jen ty, kdo jsou v systému
    a mají aktivní účet. Párování dle příznaku + osobního čísla (u starších záznamů
    fallback na e-mail). Kdo v systému není, nebo má je_aktivni = 0 (deaktivovaný),
    se nezobrazí. Při nedostupné DB se nefiltruje (fail-open)."""
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            seznam = json.load(f)
    except Exception as e:
        print(f'[narozeniny] Chyba načítání: {e}')
        return []

    podle_klice, podle_emailu = _system_lookup()
    if not podle_klice and not podle_emailu:
        return seznam  # DB nedostupná — nefiltruj, ať nezmizí všichni

    vysledek = []
    for p in seznam:
        # udata: nalezen a aktivní → zobrazit; nalezen deaktivovaný nebo
        # v systému vůbec není → skrýt.
        ud = _najdi_udata(p, podle_klice, podle_emailu)
        if ud is not None and ud.get('aktivni'):
            vysledek.append(p)
    return vysledek

def uloz_data(seznam: list):
    """Uloží seznam lidí do narozeniny_data.json."""
    global _CACHE_POCET_DNES
    _CACHE_POCET_DNES = {'hodnota': 0, 'datum': None}  # invalidace po změně databáze
    try:
        intranet_data.zapis_json_atomicky(DATA_FILE, seznam, indent=2)
    except Exception as e:
        print(f'[narozeniny] Chyba ukládání: {e}')

def importuj_csv(obsah_bytes: bytes):
    """
    Parsuje CSV (bytes) → (seznam_dict, chyby).
    Hledá sloupce: cislo_jip (např. 'OZ000001'), jmeno, prijmeni, datum_narozeni.

    Při importu se hlídá, že všechny čtyři údaje (příznak + os. číslo + jméno + příjmení)
    sedí s uživatelem v systému. K importu se nabídnou pouze osoby, kde všechny
    čtyři údaje souhlasí. E-mail se přebírá ze systému.
    Toleruje ; i , jako oddělovač, UTF-8 i CP1250.
    """
    errors = []
    result = []
    # Tichá deduplikace – v CSV bývá stejný člověk uveden vícekrát (různé pracovní poměry).
    videno_emaily: set[str] = set()

    # Dekódování
    try:
        text = obsah_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = obsah_bytes.decode('cp1250', errors='replace')

    # Detekce oddělovače
    first_line = text.split('\n')[0]
    delim = ';' if first_line.count(';') >= first_line.count(',') else ','

    reader = csv.DictReader(io.StringIO(text), delimiter=delim)

    # Načti uživatele ze systému a postav lookup (PŘÍZNAK, OS_ČÍSLO) → (email, udata).
    # Os. číslo v systému = id uživatele (iduser), tak je vedené i v UI „Osobní číslo: …".
    uzivatele_lookup: dict[tuple[str, int], tuple[str, dict]] = {}
    try:
        vsichni = intranet_data.ziskej_vsechny_uzivatele()
        for em, ud in vsichni.items():
            pn = (ud.get('priznak_nazev') or '').strip()
            oc = ud.get('id')
            if not pn or oc in (None, ''):
                continue
            try:
                uzivatele_lookup[(pn.upper(), int(oc))] = (em, ud)
            except (TypeError, ValueError):
                continue
    except Exception as ex:
        print(f'[narozeniny] Nelze načíst uživatele z DB: {ex}')

    # Normalizační mapa názvů sloupců
    ALIASY = {
        'cislo_jip':      ['cislojip', 'cislo_jip', 'osobni_cislo', 'os_cislo',
                           'employee_id', 'empl_id', 'id_zamestnance', 'cislo_zamestnance'],
        'jmeno':          ['jmeno', 'first_name', 'firstname', 'krestni'],
        'prijmeni':       ['prijmeni', 'last_name', 'lastname', 'surname'],
        'datum_narozeni': ['datum_narozeni', 'datum_narozenin', 'narozeniny', 'datum',
                           'birthdate', 'birth_date', 'date_of_birth', 'dob'],
        'je_v_zam_pomeru': ['je_v_zam_pomeru', 'v_zam_pomeru', 'zam_pomer',
                            'zamestnanecky_pomer', 'v_pracovnim_pomeru', 'is_active', 'active'],
    }

    def norm(s):
        # Sloučí 'DatumNarozeni', 'datum_narozeni', 'Datum narozeni', 'datum-narozeni' na 'datumnarozeni'
        if not s:
            return ''
        return s.strip().lower().replace(' ', '').replace('-', '').replace('_', '')

    def get_field(row, klic):
        for alias in ALIASY[klic]:
            alias_n = norm(alias)
            for col in row:
                if norm(col) == alias_n:
                    return str(row[col] or '').strip()
        return ''

    for i, row in enumerate(reader, start=2):
        cislo_jip = get_field(row, 'cislo_jip')
        jmeno     = get_field(row, 'jmeno')
        prijmeni  = get_field(row, 'prijmeni')
        datum     = get_field(row, 'datum_narozeni')
        je_v_zam  = get_field(row, 'je_v_zam_pomeru')

        # Přeskočit prázdné řádky
        if not any([cislo_jip, jmeno, prijmeni, datum, je_v_zam]):
            continue

        # Kdo není v zaměstnaneckém poměru (je_v_zam_pomeru = 0), se neimportuje.
        if _neni_v_zam_pomeru(je_v_zam):
            errors.append(
                f'Řádek {i}: přeskočeno — {jmeno} {prijmeni} ({cislo_jip}): '
                f'není v zaměstnaneckém poměru (je_v_zam_pomeru = 0)'
            )
            continue

        if not cislo_jip:
            errors.append(f'Řádek {i}: chybí osobní číslo (CisloJIP) – {jmeno} {prijmeni}')
            continue
        if not jmeno or not prijmeni:
            errors.append(f'Řádek {i}: chybí jméno nebo příjmení ({cislo_jip})')
            continue

        # Rozpad CisloJIP na příznak + os. číslo
        priznak_csv, os_cislo_csv = _parsuj_cislo_jip(cislo_jip)
        if not priznak_csv or os_cislo_csv is None:
            errors.append(
                f'Řádek {i}: přeskočeno — {jmeno} {prijmeni}: nelze rozpoznat příznak '
                f'a osobní číslo z „{cislo_jip}"'
            )
            continue

        # 1. Shoda příznaku + os. čísla
        match = uzivatele_lookup.get((priznak_csv.upper(), os_cislo_csv))
        if not match:
            errors.append(
                f'Řádek {i}: přeskočeno — {jmeno} {prijmeni} ({cislo_jip}): '
                f'v systému není uživatel s příznakem „{priznak_csv}" a os. číslem {os_cislo_csv}'
            )
            continue
        email_db, udata = match

        # 2. Shoda jména a příjmení (bez diakritiky, case-insensitive)
        sys_jmeno    = udata.get('jmeno', '') or ''
        sys_prijmeni = udata.get('prijmeni', '') or ''
        if _norm_jmeno(jmeno) != _norm_jmeno(sys_jmeno) or _norm_jmeno(prijmeni) != _norm_jmeno(sys_prijmeni):
            errors.append(
                f'Řádek {i}: přeskočeno — {jmeno} {prijmeni} ({cislo_jip}): '
                f'jméno/příjmení nesedí se systémem ({sys_jmeno} {sys_prijmeni})'
            )
            continue

        # 3. datum narození musí být vyplněné a platné
        if not datum:
            errors.append(
                f'Řádek {i}: přeskočeno — {jmeno} {prijmeni} ({cislo_jip}) '
                f'nemá vyplněné datum narození'
            )
            continue
        d = _parsuj_datum(datum)
        if not d:
            errors.append(
                f'Řádek {i}: přeskočeno — {jmeno} {prijmeni} ({cislo_jip}) '
                f'má neplatné datum narození „{datum}"'
            )
            continue

        if email_db in videno_emaily:
            continue  # tentýž uživatel už importován z dřívějšího řádku
        videno_emaily.add(email_db)

        result.append({
            'jmeno':          sys_jmeno or jmeno,
            'prijmeni':       sys_prijmeni or prijmeni,
            'jmeno_cele':     f'{sys_jmeno or jmeno} {sys_prijmeni or prijmeni}'.strip(),
            'datum_narozeni': d.isoformat(),
            'email':          email_db,
            # Klíč pro párování se systémem při zobrazení (kontrola aktivity účtu).
            'priznak':        (udata.get('priznak_nazev') or priznak_csv).strip().upper(),
            'osobni_cislo':   udata.get('id'),
        })

    return result, errors

def vzor_csv() -> bytes:
    """Vrátí obsah vzorového CSV souboru jako bytes."""
    return (
        'CisloJIP;Jmeno;Prijmeni;DatumNarozeni;je_v_zam_pomeru\n'
        'OZ000001;Jana;Nováková;1985-06-15;1\n'
        'JV010002;Petr;Březina;15.03.1990;1\n'
        'OZ000009;Karel;Odešlý;1979-11-02;0\n'
    ).encode('utf-8')


# ==========================================
# VEŘEJNÉ FUNKCE (volají je jiné moduly)
# ==========================================

_CACHE_POCET_DNES = {'hodnota': 0, 'datum': None}  # platná po celý kalendářní den

def ziskej_pocet_narozenin_dnes(vsechna_prava=None) -> int:
    """Počet narozeninářů dnes v CSV databance. Cache platná celý den."""
    global _CACHE_POCET_DNES
    today = datetime.date.today()
    if _CACHE_POCET_DNES['datum'] == today:
        return _CACHE_POCET_DNES['hodnota']
    pocet = sum(
        1 for p in nacti_data()
        if _birthday_tuple(p.get('datum_narozeni', '')) == (today.month, today.day)
    )
    _CACHE_POCET_DNES = {'hodnota': pocet, 'datum': today}
    return pocet


# ==========================================
# BACKGROUND TASK — odesílání e-mailů
# ==========================================
_POSLEDNI_EMAIL_DATUM = None

KULATINY_PREDSTIH_DNI = 2   # auto-upozornění na kulatiny se posílá X dní předem

async def bg_narozeniny_emaily():
    """Každou minutu zkontroluje, zda má odeslat přání k narozeninám
    a automatická upozornění na blížící se kulatiny (2 dny předem)."""
    global _POSLEDNI_EMAIL_DATUM
    await asyncio.sleep(120)
    while True:
        try:
            nastaveni = intranet_data.nacti_nastaveni_intranetu()
            if nastaveni.get('narozeniny_zapnuty', True):
                cas = nastaveni.get('narozeniny_email_cas', '09:00').strip()
                nyni = datetime.datetime.now()
                dnes = nyni.date()
                if nyni.strftime('%H:%M') == cas and _POSLEDNI_EMAIL_DATUM != dnes:
                    _POSLEDNI_EMAIL_DATUM = dnes
                    if nastaveni.get('narozeniny_email_zapnuty', True):
                        await asyncio.to_thread(_odesli_narozeninove_emaily, dnes, nastaveni)
                    await asyncio.to_thread(_odesli_kulatiny_upozorneni, dnes, nastaveni)
        except Exception as e:
            print(f'[bg_narozeniny_emaily] Chyba: {e}')
        await asyncio.sleep(60)

def _sestav_html_email(text: str, podpisy: list[str]) -> tuple[str, list]:
    """
    Převede plain-text šablonu na HTML a přidá inline podpisy.
    Vrátí (html_obsah, inline_soubory) kde inline_soubory = [(cid, cesta), ...].
    """
    bezpecny_text = _html.escape(text).replace('\n', '<br>')

    if podpisy:
        imgs = ''.join(
            f'<img src="cid:podpis_{i}" style="height:70px; margin-right:24px; vertical-align:middle;">'
            for i in range(len(podpisy))
        )
        podpisy_blok = f'<div style="margin-top:28px; padding-top:16px; border-top:1px solid #eee;">{imgs}</div>'
        inline = [(f'podpis_{i}', cesta) for i, cesta in enumerate(podpisy)]
    else:
        podpisy_blok = ''
        inline = []

    html = (
        '<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;">'
        f'<p style="line-height:1.7;">{bezpecny_text}</p>'
        f'{podpisy_blok}'
        '</body></html>'
    )
    return html, inline


def _odesli_narozeninove_emaily(dnes, nastaveni):
    """Odešle přání každé osobě v databance, které jsou dnes narozeniny."""
    predmet_tpl = nastaveni.get(
        'narozeniny_email_predmet',
        'Všechno nejlepší k narozeninám! 🎂'
    )
    text_tpl = nastaveni.get(
        'narozeniny_email_text',
        'Milý/á {jmeno},\n\n'
        'v tento zvláštní den ti přejeme vše nejlepší k narozeninám! 🎂\n'
        'Přejeme ti pevné zdraví, spoustu štěstí a mnoho krásných chvil.\n\n'
        'S přáním vše nejlepšího,\n'
        'Váš tým'
    )
    podpisy = _ziskej_podpisy()
    podle_klice, podle_emailu = _system_lookup()
    for p in nacti_data():
        bt = _birthday_tuple(p.get('datum_narozeni', ''))
        if not (bt and bt == (dnes.month, dnes.day)):
            continue
        email = p.get('email', '')
        if not email:
            continue
        ud = _najdi_udata(p, podle_klice, podle_emailu)
        if ud is not None and not ud.get('email_narozeniny', True):
            intranet_logger.log_activity(
                'Systém', 'Narozeniny',
                f'Přání neodesláno: {p.get("jmeno_cele", email)} ({email}) — vypnuto v osobním nastavení'
            )
            continue
        predmet = (predmet_tpl
                   .replace('{jmeno}',      p.get('jmeno', ''))
                   .replace('{prijmeni}',   p.get('prijmeni', ''))
                   .replace('{jmeno_cele}', p.get('jmeno_cele', '')))
        text = (text_tpl
                .replace('{jmeno}',      p.get('jmeno', ''))
                .replace('{prijmeni}',   p.get('prijmeni', ''))
                .replace('{jmeno_cele}', p.get('jmeno_cele', '')))
        html_obsah, inline = _sestav_html_email(text, podpisy)
        ok = intranet_emaily.odesli_html_email(email, predmet, html_obsah, inline)
        intranet_logger.log_activity(
            'Systém', 'Narozeniny',
            f'Přání odesláno: {p.get("jmeno_cele", email)} ({email}) — {"OK" if ok else "CHYBA"}'
        )


def _odesli_kulatiny_upozorneni(dnes, nastaveni):
    """Automatické upozornění na kulatiny KULATINY_PREDSTIH_DNI dní předem —
    adresátům z nastavení (narozeniny_kulatiny_emaily), NE oslavenci.
    Příklad: kulatiny 10. 7. → upozornění odejde 8. 7. Volá se 1× denně
    z bg_narozeniny_emaily ve stejný čas jako přání."""
    prijemci = [p for p in _rozdel_emaily(
        nastaveni.get('narozeniny_kulatiny_emaily')
        or nastaveni.get('narozeniny_kulatiny_email')
        or intranet_data.nacti_smtp().get('smtp_user', '')) if '@' in p]
    if not prijemci:
        return
    cil = dnes + datetime.timedelta(days=KULATINY_PREDSTIH_DNI)
    cil_str = f'{cil.day:02d}.{cil.month:02d}.{cil.year}'
    for p in nacti_data():
        bt = _birthday_tuple(p.get('datum_narozeni', ''))
        if not (bt and bt == (cil.month, cil.day)):
            continue
        narozen = _parsuj_datum(p.get('datum_narozeni', ''))
        vek = cil.year - narozen.year if narozen else None
        if not _je_kulatiny(vek):
            continue
        jmeno_cele = p.get('jmeno_cele',
                           f"{p.get('jmeno', '')} {p.get('prijmeni', '')}".strip())
        predmet = (f'Blížící se kulatiny: {jmeno_cele} oslaví za '
                   f'{KULATINY_PREDSTIH_DNI} dny {vek}. narozeniny ({cil_str})')
        text = (
            'Upozornění na blížící se kulatiny\n\n'
            f'Osoba:    {jmeno_cele}\n'
            f'Věk:      {vek} let (kulatiny)\n'
            f'Kdy:      za {KULATINY_PREDSTIH_DNI} dny — {cil_str}\n\n'
            'Toto je automatická zpráva z portálu Moje JIPka (modul Narozeniny).'
        )
        for prijemce in prijemci:
            ok = intranet_emaily.odesli_upozorneni_email(prijemce, predmet, text)
            intranet_logger.log_activity(
                'Systém', 'Narozeniny',
                f'Auto-upozornění na kulatiny: {jmeno_cele} ({vek} let, {cil_str}) '
                f'→ {prijemce} — {"OK" if ok else "CHYBA"}'
            )


# ==========================================
# UI — hlavní sekce
# ==========================================

@ui.refreshable
def vykresli_narozeniny(user_id, user_name, vsechna_prava):
    nastaveni = intranet_data.nacti_nastaveni_intranetu()
    nazev     = nastaveni.get('dlazdice_7', 'Narozeniny')
    je_admin  = 'vse' in vsechna_prava or 'narozeniny_sprava' in vsechna_prava

    with ui.column().classes('w-full gap-4'):
        ui.label(f'🎂 {nazev}').classes('text-4xl font-extrabold text-gray-800 mb-2')

        if je_admin:
            with ui.tabs().classes('w-full border-b border-gray-200') as tabs:
                ui.tab('prehled',  label='📅 Přehled narozenin')
                ui.tab('databaze', label='📋 Správa databáze')
                ui.tab('podpisy',  label='✍️ Podpisy v e-mailu')

            with ui.tab_panels(tabs, value='prehled').classes('w-full pt-4'):
                with ui.tab_panel('prehled'):
                    _tab_prehled()
                with ui.tab_panel('databaze'):
                    _tab_databaze()
                with ui.tab_panel('podpisy'):
                    _tab_podpisy()
        else:
            _tab_prehled()


# ------------------------------------------
# TAB: Přehled narozenin
# ------------------------------------------

@ui.refreshable
def _tab_prehled():
    seznam = nacti_data()

    if not seznam:
        with ui.card().classes('w-full p-12 text-center bg-gray-50 rounded-2xl border border-gray-100'):
            ui.label('📂').classes('text-6xl mb-4')
            ui.label('Databáze je prázdná').classes('text-xl text-gray-500 font-bold')
            ui.label('Přejděte do záložky „Správa databáze" a nahrajte CSV soubor.').classes('text-sm text-gray-400 mt-2')
        return

    obohaceni = []
    for p in seznam:
        dn  = p.get('datum_narozeni', '')
        bt  = _birthday_tuple(dn)
        if not bt:
            continue
        obohaceni.append({**p, 'bt': bt, 'days_until': _dni_do_narozenin(dn), 'vek': _vek(dn)})

    obohaceni.sort(key=lambda x: x['days_until'])
    dnesni    = [p for p in obohaceni if p['days_until'] == 0]
    zitrejsi  = [p for p in obohaceni if p['days_until'] == 1]
    nejblizsi = [p for p in obohaceni if p['days_until'] > 1][:5]

    # ── Dnešní ───────────────────────────────────────────
    if dnesni:
        with ui.card().classes('w-full p-6 bg-pink-50 border-2 border-pink-300 rounded-2xl shadow-md mb-2'):
            with ui.row().classes('items-center gap-3 mb-4'):
                ui.label('🎉').classes('text-4xl')
                ui.label('Dnešní narozeniny').classes('text-2xl font-extrabold text-pink-700')
            for p in dnesni:
                with ui.card().classes('w-full p-4 mb-2 bg-white rounded-xl border border-pink-200 shadow-sm'):
                    with ui.row().classes('w-full items-center gap-4'):
                        ui.label('🎂').classes('text-3xl w-10 text-center shrink-0')
                        with ui.column().classes('flex-1 gap-0 min-w-0'):
                            ui.label(p['jmeno_cele']).classes('text-lg font-bold text-gray-900')
                            ui.label(p['email']).classes('text-sm text-gray-400 truncate')
                        with ui.column().classes('items-end gap-1 shrink-0'):
                            if p['vek'] is not None:
                                ui.badge(f'{p["vek"]} let', color='pink').classes('text-base px-3 py-1 font-bold')
                            if _je_kulatiny(p['vek']):
                                ui.badge(f'🎊 {p["vek"]}. kulatiny!', color='purple').classes('text-xs font-bold')
                            ui.label(_datum_cz(p['datum_narozeni'])).classes('text-xs text-gray-400')
                    if _je_kulatiny(p['vek']):
                        def _otevri_kulatiny_dnes(osoba=p, vek=p['vek']):
                            _dialog_kulatiny_email(osoba, vek)
                        ui.button('Upozornit e-mailem', icon='mail', on_click=_otevri_kulatiny_dnes).props(
                            'flat size=sm color=purple'
                        ).classes('mt-2 self-end')

    # ── Zítřejší ─────────────────────────────────────────
    if zitrejsi:
        with ui.card().classes('w-full p-6 bg-orange-50 border border-orange-200 rounded-2xl shadow-sm mb-2'):
            with ui.row().classes('items-center gap-3 mb-4'):
                ui.label('🌅').classes('text-3xl')
                ui.label('Zítra slaví narozeniny').classes('text-xl font-bold text-orange-700')
            for p in zitrejsi:
                bt      = p['bt']
                vek_zit = p['vek'] + 1 if p['vek'] is not None else None
                with ui.row().classes('w-full items-center gap-4 py-3 border-b border-orange-100'):
                    ui.label('👤').classes('text-xl w-8 text-center shrink-0')
                    with ui.column().classes('flex-1 min-w-0 gap-0'):
                        ui.label(p['jmeno_cele']).classes('font-semibold text-gray-800 truncate')
                        ui.label(p['email']).classes('text-xs text-gray-400 truncate')
                    ui.label(f'{bt[1]:02d}.{bt[0]:02d}.').classes('text-gray-600 font-mono text-sm w-16 text-right shrink-0')
                    if vek_zit is not None:
                        ui.label(f'{vek_zit} let').classes('text-xs text-gray-500 w-14 text-right shrink-0')
                    if _je_kulatiny(vek_zit):
                        ui.badge(f'🎊 Kulatiny', color='purple').classes('text-xs shrink-0')
                        def _otevri_kulatiny_zit(osoba=p, vek=vek_zit):
                            _dialog_kulatiny_email(osoba, vek)
                        ui.button(icon='mail', on_click=_otevri_kulatiny_zit).props(
                            'flat round size=sm color=purple'
                        ).tooltip('Upozornit e-mailem na kulatiny').classes('shrink-0')
                    ui.badge('Zítra', color='orange').classes('text-xs shrink-0')

    # ── Nejbližších 5 ────────────────────────────────────
    if nejblizsi:
        with ui.card().classes('w-full p-6 bg-white rounded-2xl shadow-sm border border-gray-100'):
            ui.label('📅 Nejbližší narozeniny').classes('text-xl font-bold text-gray-700 mb-3')
            for p in nejblizsi:
                bt      = p['bt']
                days    = p['days_until']
                vek_bud = p['vek'] + 1 if p['vek'] is not None else None
                zbyvaji_label = f'Za {days} dní' if days <= 14 else MESICE_CZ[bt[0]]
                zbyvaji_color = 'blue' if days <= 14 else 'grey'
                with ui.row().classes('w-full items-center gap-4 py-3 border-b border-gray-50'):
                    ui.label('👤').classes('text-xl w-8 text-center shrink-0')
                    with ui.column().classes('flex-1 min-w-0 gap-0'):
                        ui.label(p['jmeno_cele']).classes('font-semibold text-gray-800 truncate')
                        ui.label(p['email']).classes('text-xs text-gray-400 truncate')
                    ui.label(f'{bt[1]:02d}.{bt[0]:02d}.').classes('text-gray-600 font-mono text-sm w-16 text-right shrink-0')
                    if vek_bud is not None:
                        ui.label(f'{vek_bud} let').classes('text-xs text-gray-500 w-14 text-right shrink-0')
                    if _je_kulatiny(vek_bud):
                        ui.badge(f'🎊 Kulatiny', color='purple').classes('text-xs shrink-0')
                        def _otevri_kulatiny_nb(osoba=p, vek=vek_bud):
                            _dialog_kulatiny_email(osoba, vek)
                        ui.button(icon='mail', on_click=_otevri_kulatiny_nb).props(
                            'flat round size=sm color=purple'
                        ).tooltip('Upozornit e-mailem na kulatiny').classes('shrink-0')
                    ui.badge(zbyvaji_label, color=zbyvaji_color).classes('text-xs shrink-0')

    # ── Prázdný stav ─────────────────────────────────────
    if not dnesni and not zitrejsi and not nejblizsi:
        with ui.card().classes('w-full p-12 text-center bg-gray-50 rounded-2xl border border-gray-100'):
            ui.label('😢').classes('text-6xl mb-4')
            ui.label('Žádné blížící se narozeniny').classes('text-xl text-gray-500 font-bold')
            ui.label(f'V databance je {len(seznam)} osob, ale žádná nemá narozeniny v nejbližších dnech.')\
              .classes('text-sm text-gray-400 mt-2')


# ------------------------------------------
# TAB: Správa databáze
# ------------------------------------------

@ui.refreshable
def _tab_databaze():
    seznam = nacti_data()

    # ── Upload ───────────────────────────────────────────
    with ui.card().classes('w-full p-6 bg-blue-50 border border-blue-200 rounded-2xl mb-4'):
        with ui.row().classes('items-center gap-2 mb-3'):
            ui.icon('upload_file', size='1.8rem').classes('text-blue-700')
            ui.label('Nahrát CSV soubor').classes('text-xl font-bold text-blue-800')

        ui.html(f'''
            <p class="text-sm text-blue-700 mb-1">
                <b>Povinné sloupce:</b>
                <code class="bg-blue-100 px-1 rounded">CisloJIP</code>,
                <code class="bg-blue-100 px-1 rounded">Jmeno</code>,
                <code class="bg-blue-100 px-1 rounded">Prijmeni</code>,
                <code class="bg-blue-100 px-1 rounded">DatumNarozeni</code>
            </p>
            <p class="text-sm text-blue-700 mt-1">
                🔒 Importováni budou <b>pouze lidé, u kterých se v systému shoduje
                příznak, osobní číslo, jméno i příjmení</b>. Identifikátor v sloupci
                <code class="bg-blue-100 px-1 rounded">CisloJIP</code> se rozdělí na
                úvodní písmena (příznak, např. <code>OZ</code>) a číselnou část (osobní číslo).
            </p>
            <p class="text-sm text-blue-700 mt-1">
                📧 E-mail se přebírá z účtu v systému (sloupec <code>email</code> v CSV se ignoruje).
                Osoby bez vyplněného data narození nebo bez odpovídajícího uživatele v systému
                se přeskočí a zobrazí ve varováních.
            </p>
            <p class="text-sm text-blue-700 mt-1">
                👔 Sloupec <code class="bg-blue-100 px-1 rounded">je_v_zam_pomeru</code>:
                <code>1</code> = v zaměstnaneckém poměru (importuje se),
                <code>0</code> = není (přeskočí se). Navíc se při zobrazení nezobrazí ani ti,
                kdo mají v systému deaktivovaný účet (<code>is_active = 0</code>).
            </p>
            <p class="text-sm text-blue-600 mt-1">
                Oddělovač: <code>;</code> nebo <code>,</code>&ensp;|&ensp;
                Datum: <code>YYYY-MM-DD</code> nebo <code>DD.MM.RRRR</code>&ensp;|&ensp;
                Kódování: UTF-8 nebo Windows-1250
            </p>
        ''')

        ui.button(
            'Stáhnout vzor CSV', icon='download',
            on_click=lambda: ui.download(vzor_csv(), 'vzor_narozeniny.csv')
        ).props('flat size=sm').classes('text-blue-600 mt-2 mb-4')

        preview  = {'data': None}
        info_box = ui.column().classes('w-full')

        def _uloz_import():
            if not preview.get('data'):
                return
            uloz_data(preview['data'])
            pocet = len(preview['data'])
            ui.notify(f'✅ Uloženo {pocet} záznamů.', type='positive')
            intranet_logger.log_activity('Správce', 'Narozeniny', f'Importováno {pocet} osob z CSV')
            preview['data'] = None
            btn_uloz.classes(add='hidden')
            _tab_databaze.refresh()
            _tab_prehled.refresh()

        btn_uloz = ui.button('💾 Uložit a nahradit celou databázi', icon='save',
                             on_click=_uloz_import).classes('mt-3 bg-blue-600 text-white font-bold hidden')

        async def zpracuj_upload(e: events.UploadEventArguments):
            raw = await e.file.read()
            data, chyby = importuj_csv(raw)
            preview['data'] = data

            info_box.clear()
            with info_box:
                if chyby:
                    with ui.card().classes('w-full p-4 bg-yellow-50 border border-yellow-200 rounded-xl mb-3'):
                        ui.label(f'⚠️ {len(chyby)} varování:').classes('font-bold text-yellow-800 mb-1')
                        for ch in chyby[:8]:
                            ui.label(f'• {ch}').classes('text-sm text-yellow-700')
                        if len(chyby) > 8:
                            ui.label(f'… a dalších {len(chyby) - 8}').classes('text-xs text-yellow-600')
                if data:
                    ui.label(f'✅ Soubor „{e.file.name}" — {len(data)} platných záznamů').classes(
                        'text-green-700 font-bold')
                    btn_uloz.classes(remove='hidden')
                else:
                    ui.label('❌ Soubor neobsahuje žádné platné záznamy.').classes('text-red-600 font-bold')
                    btn_uloz.classes(add='hidden')

        ui.upload(
            label='Vyberte soubor (.csv)',
            on_upload=zpracuj_upload,
            auto_upload=True,
            max_files=1,
            max_file_size=10_000_000,
        ).props('accept=".csv,text/csv" flat bordered').classes('w-full')

    # ── Existující databáze ──────────────────────────────
    if not seznam:
        with ui.card().classes('w-full p-10 text-center bg-gray-50 rounded-2xl border border-gray-100'):
            ui.label('📂').classes('text-5xl mb-3')
            ui.label('Databáze je prázdná — nahrajte CSV soubor výše.').classes('text-gray-500')
        return

    with ui.card().classes('w-full p-6 bg-white rounded-2xl shadow-sm border border-gray-100'):
        with ui.row().classes('w-full items-center justify-between mb-4'):
            ui.label(f'👥 Databáze — {len(seznam)} osob').classes('text-xl font-bold text-gray-700')
            ui.button('🗑️ Smazat celou databázi', on_click=_dialog_smazat_vse,
                      color='red').props('flat size=sm')

        seznam_sorted = sorted(seznam, key=lambda p: _dni_do_narozenin(p.get('datum_narozeni', '')))

        with ui.column().classes('w-full gap-0'):
            for p in seznam_sorted:
                dias = _dni_do_narozenin(p.get('datum_narozeni', ''))
                vek  = _vek(p.get('datum_narozeni', ''))

                if dias == 0:
                    badge_txt, badge_col = '🎂 Dnes!', 'pink'
                elif dias == 1:
                    badge_txt, badge_col = '🌅 Zítra', 'orange'
                elif dias <= 14:
                    badge_txt, badge_col = f'Za {dias} dní', 'blue'
                elif dias == 9999:
                    badge_txt, badge_col = '— bez data', 'grey'
                else:
                    bt = _birthday_tuple(p.get('datum_narozeni', ''))
                    badge_txt = MESICE_CZ[bt[0]] if bt else '—'
                    badge_col = 'grey'

                with ui.row().classes('w-full items-center gap-3 py-3 border-b border-gray-100'):
                    ui.label('👤').classes('text-lg w-7 text-center shrink-0 text-gray-400')
                    with ui.column().classes('flex-1 min-w-0 gap-0'):
                        ui.label(p.get('jmeno_cele', '—')).classes('font-semibold text-gray-800 truncate')
                        ui.label(p.get('email', '')).classes('text-xs text-gray-400 truncate')
                    ui.label(_datum_cz(p.get('datum_narozeni', ''))).classes(
                        'text-sm text-gray-500 font-mono w-24 text-right shrink-0')
                    if vek is not None:
                        ui.label(f'{vek} let').classes('text-xs text-gray-400 w-10 text-right shrink-0')
                    ui.badge(badge_txt, color=badge_col).classes('text-xs shrink-0')


# ------------------------------------------
# TAB: Podpisy v e-mailu
# ------------------------------------------

@ui.refreshable
def _tab_podpisy():
    podpisy = _ziskej_podpisy()

    with ui.card().classes('w-full p-6 bg-blue-50 border border-blue-200 rounded-2xl mb-4'):
        with ui.row().classes('items-center gap-2 mb-3'):
            ui.icon('draw', size='1.8rem').classes('text-blue-700')
            ui.label('Nahrát podpis').classes('text-xl font-bold text-blue-800')

        ui.label(
            'Nahrajte obrázky podpisů (PNG, JPG…), které se automaticky přidají na konec každého narozeninového e-mailu. '
            'Pořadí odpovídá abecednímu seřazení názvů souborů.'
        ).classes('text-sm text-blue-700 mb-4')

        async def zpracuj_podpis(e: events.UploadEventArguments):
            # K-4: Sanitizace názvu souboru — zabraňuje path traversal
            safe_name = os.path.basename(e.file.name)
            ext = os.path.splitext(safe_name)[1].lower()
            if ext not in PODPISY_EXTS:
                ui.notify(f'Nepodporovaný formát ({ext}). Použijte PNG, JPG nebo WebP.', type='negative')
                return
            raw = await e.file.read()
            cesta = os.path.join(PODPISY_DIR, safe_name)
            with open(cesta, 'wb') as f:
                f.write(raw)
            ui.notify(f'Podpis „{safe_name}" uložen.', type='positive')
            intranet_logger.log_activity('Správce', 'Narozeniny', f'Nahrán podpis: {safe_name}')
            _tab_podpisy.refresh()

        ui.upload(
            label='Vyberte soubor (.png, .jpg, .jpeg, .webp)',
            on_upload=zpracuj_podpis,
            auto_upload=True,
            max_files=1,
            max_file_size=10_000_000,
        ).props('accept=".png,.jpg,.jpeg,.gif,.webp" flat bordered').classes('w-full')

    if not podpisy:
        with ui.card().classes('w-full p-10 text-center bg-gray-50 rounded-2xl border border-gray-100'):
            ui.label('✍️').classes('text-5xl mb-3')
            ui.label('Žádné podpisy — nahrajte obrázky výše.').classes('text-gray-500')
        return

    def _otevri_nahled():
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
        smtp_user   = intranet_data.nacti_smtp().get('smtp_user') or 'intranet@firma.cz'
        predmet_tpl = nastaveni.get('narozeniny_email_predmet', 'Všechno nejlepší k narozeninám! 🎂')
        text_tpl = nastaveni.get(
            'narozeniny_email_text',
            'Milý/á {jmeno},\n\n'
            'v tento zvláštní den ti přejeme vše nejlepší k narozeninám! 🎂\n'
            'Přejeme ti pevné zdraví, spoustu štěstí a mnoho krásných chvil.\n\n'
            'S přáním vše nejlepšího,\n'
            'Váš tým'
        )
        vzor    = {'jmeno': 'Jana', 'prijmeni': 'Nováková', 'jmeno_cele': 'Jana Nováková'}
        predmet = predmet_tpl.replace('{jmeno}', vzor['jmeno']).replace('{prijmeni}', vzor['prijmeni']).replace('{jmeno_cele}', vzor['jmeno_cele'])
        text    = text_tpl   .replace('{jmeno}', vzor['jmeno']).replace('{prijmeni}', vzor['prijmeni']).replace('{jmeno_cele}', vzor['jmeno_cele'])
        d = datetime.date.today()
        dnes_str = f'{d.day}. {d.month}. {d.year}'

        with ui.dialog().props('maximized=false') as dlg, \
             ui.card().classes('p-0 overflow-hidden rounded-2xl shadow-2xl').style('width:660px; max-width:95vw;'):

            # ── Lišta okna (imitace e-mailového klienta) ──────
            with ui.element('div').style(
                'background:#404040; padding:10px 16px; display:flex; align-items:center; gap:10px;'
            ):
                for col in ['#ff5f57', '#ffbd2e', '#28c840']:
                    ui.element('div').style(f'width:13px; height:13px; border-radius:50%; background:{col};')
                ui.element('div').style('flex:1;')
                ui.element('div').style(
                    'background:#555; color:#ccc; font-size:12px; padding:3px 16px; border-radius:4px; font-family:monospace;'
                ).text = 'Doručená pošta'
                ui.element('div').style('flex:1;')

            # ── Panel předmětu ─────────────────────────────────
            with ui.element('div').style(
                'background:#f6f6f6; padding:16px 24px; border-bottom:1px solid #e0e0e0;'
            ):
                ui.element('div').style(
                    'font-size:20px; font-weight:700; color:#111; font-family:Arial,sans-serif; margin-bottom:2px;'
                ).text = predmet

            # ── Meta řádky (Od / Komu / Datum) ────────────────
            with ui.element('div').style(
                'background:#fafafa; padding:12px 24px 10px; border-bottom:1px solid #ebebeb;'
                'display:flex; align-items:flex-start; gap:14px;'
            ):
                # Avatar
                ui.element('div').style(
                    'width:40px; height:40px; border-radius:50%; background:#6366f1; color:#fff;'
                    'display:flex; align-items:center; justify-content:center; font-weight:700;'
                    'font-size:16px; font-family:Arial; flex-shrink:0; margin-top:2px;'
                ).text = smtp_user[0].upper() if smtp_user else 'J'

                with ui.element('div').style('flex:1; min-width:0;'):
                    ui.html(
                        f'<div style="font-family:Arial,sans-serif;">'
                        f'<span style="font-weight:600; color:#111; font-size:14px;">{_html.escape(smtp_user)}</span>'
                        f'&nbsp;<span style="color:#888; font-size:13px;">&lt;{_html.escape(smtp_user)}&gt;</span>'
                        f'</div>'
                        f'<div style="font-size:12px; color:#999; margin-top:2px; font-family:Arial,sans-serif;">'
                        f'Komu: <b style="color:#555;">Jana Nováková</b> &nbsp;·&nbsp; {_html.escape(dnes_str)}'
                        f'</div>'
                    )

            # ── Tělo e-mailu ───────────────────────────────────
            with ui.element('div').style(
                'background:#ffffff; padding:28px 32px 24px; font-family:Arial,sans-serif;'
            ):
                bezpecny = _html.escape(text).replace('\n', '<br>')
                ui.html(
                    f'<div style="font-size:14px; line-height:1.9; color:#222;">{bezpecny}</div>'
                )

                if podpisy:
                    ui.element('hr').style('border:none; border-top:1px solid #ebebeb; margin:24px 0 20px;')
                    with ui.row().classes('gap-4 items-end flex-wrap'):
                        for c in podpisy:
                            ui.image(os.path.abspath(c)).props('fit=contain').style(
                                'height:60px; width:180px; background:#ffffff;'
                            )

            # ── Dolní lišta ────────────────────────────────────
            with ui.element('div').style(
                'background:#f6f6f6; border-top:1px solid #e0e0e0; padding:10px 20px;'
                'display:flex; justify-content:space-between; align-items:center;'
            ):
                ui.html(
                    '<span style="font-size:11px; color:#aaa; font-family:Arial,sans-serif;">'
                    'Šablonu e-mailu upravíte v <b>Nastavení → Narozeniny</b></span>'
                )
                ui.button('Zavřít', on_click=dlg.close).props('flat size=sm').classes('text-gray-500')

        dlg.open()

    with ui.card().classes('w-full p-6 bg-white rounded-2xl shadow-sm border border-gray-100'):
        with ui.row().classes('w-full items-center justify-between mb-4'):
            ui.label(f'✍️ Uložené podpisy — {len(podpisy)} ks').classes('text-xl font-bold text-gray-700')
            ui.button('Náhled e-mailu', icon='visibility', on_click=_otevri_nahled).classes(
                'bg-indigo-600 text-white font-semibold px-4 h-10 rounded-xl shadow-sm'
            )

        ui.label('Náhled patičky:').classes('text-sm text-gray-500 mb-2')
        with ui.row().classes('gap-4 items-end flex-wrap p-4 rounded-xl border border-gray-200 mb-6').style('background:#f0f0f0;'):
            for c in podpisy:
                abs_c = os.path.abspath(c)
                ui.image(abs_c).props('fit=contain').style(
                    'height:64px; width:200px; background:#ffffff; border:1px solid #ddd; border-radius:6px;'
                )

        ui.label('Správa souborů:').classes('text-sm text-gray-500 mb-2')
        for cesta in podpisy:
            nazev = os.path.basename(cesta)
            with ui.row().classes('w-full items-center gap-4 py-2 border-b border-gray-100'):
                ui.image(os.path.abspath(cesta)).props('fit=contain').style(
                    'height:48px; width:100px; background:#ffffff; border:1px solid #eee; border-radius:4px; flex-shrink:0;'
                )
                ui.label(nazev).classes('flex-1 text-sm text-gray-700 truncate')
                def _smazat(c=cesta, n=nazev):
                    _smazat_podpis(c)
                    ui.notify(f'Podpis „{n}" smazán.', type='warning')
                    intranet_logger.log_activity('Správce', 'Narozeniny', f'Smazán podpis: {n}')
                    _tab_podpisy.refresh()
                ui.button(icon='delete', on_click=_smazat).props('flat round color=red size=sm')


def _rozdel_emaily(s) -> list[str]:
    """Rozdělí řetězec na seznam e-mailů (oddělené čárkou, středníkem, mezerou nebo novým řádkem)."""
    if not s:
        return []
    if isinstance(s, (list, tuple)):
        casti = []
        for cast in s:
            casti.extend(_rozdel_emaily(cast))
        return casti
    import re
    casti = re.split(r'[,;\s]+', str(s).strip())
    # Deduplikace se zachováním pořadí
    videno, vysledek = set(), []
    for c in casti:
        c = c.strip()
        if c and c.lower() not in videno:
            videno.add(c.lower())
            vysledek.append(c)
    return vysledek


def _nabidka_adresatu(vychozi: list[str]) -> dict[str, str]:
    """Sestaví našeptávač adresátů: e-maily všech uživatelů systému + výchozí adresáty.

    Vrací mapu e-mail → popisek („Jméno Příjmení (e-mail)"), seřazenou podle popisku.
    """
    options: dict[str, str] = {}
    try:
        vsichni = intranet_data.ziskej_vsechny_uzivatele()
        for em, ud in vsichni.items():
            em = (em or '').strip()
            if not em or '@' not in em:
                continue
            cele = f"{(ud.get('jmeno') or '').strip()} {(ud.get('prijmeni') or '').strip()}".strip()
            options[em] = f'{cele} ({em})' if cele else em
    except Exception as e:
        print(f'[narozeniny] Nelze načíst uživatele pro našeptávač: {e}')
    # Výchozí adresáti musí být v nabídce, aby se zobrazili jako chip i s popiskem.
    for em in vychozi:
        options.setdefault(em, em)
    return dict(sorted(options.items(), key=lambda kv: kv[1].lower()))


def _dialog_kulatiny_email(osoba: dict, vek: int):
    """Otevře dialog pro zaslání upozornění na kulatiny konkrétnímu uživateli.

    Umožňuje zvolit více adresátů — upozornění se odešle každému zvlášť.
    """
    nastaveni      = intranet_data.nacti_nastaveni_intranetu()
    # Výchozí adresáti: nový plurálový klíč → starý jednotlivý klíč → SMTP účet
    vychozi_emaily = _rozdel_emaily(
        nastaveni.get('narozeniny_kulatiny_emaily')
        or nastaveni.get('narozeniny_kulatiny_email')
        or intranet_data.nacti_smtp().get('smtp_user', '')
    )
    jmeno_cele     = osoba.get('jmeno_cele', f"{osoba.get('jmeno','')} {osoba.get('prijmeni','')}".strip())
    datum_str      = _datum_cz(osoba.get('datum_narozeni', ''))
    bt             = _birthday_tuple(osoba.get('datum_narozeni', ''))
    kdy_str        = f'{bt[1]:02d}.{bt[0]:02d}.' if bt else '—'

    with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl shadow-xl').style('width:460px; max-width:95vw;'):
        with ui.row().classes('items-center gap-3 mb-4'):
            ui.label('🎊').classes('text-3xl')
            ui.label(f'Upozornění na kulatiny').classes('text-xl font-bold text-purple-700')

        with ui.card().classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-200 mb-4'):
            ui.label(jmeno_cele).classes('font-bold text-gray-900 text-base')
            ui.label(f'slaví {vek}. narozeniny · {datum_str}').classes('text-sm text-gray-500')

        ui.label('Odeslat upozornění adresátům:').classes('text-sm font-semibold text-gray-700 mb-1')
        email_select = ui.select(
            options=_nabidka_adresatu(vychozi_emaily),
            value=list(vychozi_emaily),
            multiple=True,
            with_input=True,
            new_value_mode='add-unique',
        ).classes('w-full').props('outlined dense use-chips')
        email_select.tooltip('Začněte psát jméno nebo e-mail — našeptávač nabídne uživatele. '
                             'Enter přidá adresáta (lze zadat i adresu mimo systém).')
        ui.label('Začněte psát — našeptávač nabídne uživatele; Enter přidá dalšího adresáta.').classes(
            'text-xs text-gray-400 mt-1'
        )

        ui.label('Vlastní poznámka (volitelné):').classes('text-sm font-semibold text-gray-700 mt-3 mb-1')
        poznamka_input = ui.textarea(
            placeholder='Např.: Připravte dárek, organizujte oslavu…',
        ).classes('w-full').props('outlined dense rows=3')

        status_box = ui.row().classes('w-full')

        async def _odeslat():
            prijemci = _rozdel_emaily(email_select.value)
            prijemci = [p for p in prijemci if '@' in p]
            if not prijemci:
                ui.notify('Zadejte alespoň jednu platnou e-mailovou adresu.', type='warning')
                return

            poznamka = poznamka_input.value.strip()
            predmet  = f'Kulatiny: {jmeno_cele} slaví {vek}. narozeniny ({kdy_str})'
            text     = (
                f'Upozornění na kulatiny\n\n'
                f'Osoba:    {jmeno_cele}\n'
                f'Věk:      {vek} let\n'
                f'Datum:    {datum_str}\n'
            )
            if poznamka:
                text += f'\nPoznámka:\n{poznamka}\n'

            # Zachytíme klienta — notify po awaitu jinak může spadnout na 'slot deleted'.
            klient = ui.context.client

            uspesne, neuspesne = [], []
            for prijemce in prijemci:
                ok = await asyncio.to_thread(
                    intranet_emaily.odesli_upozorneni_email, prijemce, predmet, text
                )
                (uspesne if ok else neuspesne).append(prijemce)

            if uspesne:
                intranet_logger.log_activity(
                    'Uživatel', 'Narozeniny',
                    f'Upozornění na kulatiny {jmeno_cele} ({vek} let) odesláno → '
                    f'{", ".join(uspesne)}'
                )

            try:
                with klient:
                    if uspesne and not neuspesne:
                        ui.notify(
                            f'E-mail odeslán {len(uspesne)} adresátům.'
                            if len(uspesne) > 1 else f'E-mail odeslán na {uspesne[0]}.',
                            type='positive'
                        )
                        dlg.close()
                    elif uspesne and neuspesne:
                        ui.notify(
                            f'Odesláno {len(uspesne)} z {len(uspesne) + len(neuspesne)} '
                            f'adresátům. Neúspěšní: {", ".join(neuspesne)}',
                            type='warning'
                        )
                    else:
                        status_box.clear()
                        with status_box:
                            ui.label('Odeslání selhalo — zkontrolujte SMTP nastavení.').classes(
                                'text-red-600 text-sm font-semibold'
                            )
            except Exception as e:
                print(f'[narozeniny] notify po odeslání selhalo: {e}')

        with ui.row().classes('gap-3 justify-end w-full mt-4'):
            ui.button('Zrušit', on_click=dlg.close).props('flat')
            ui.button('Odeslat upozornění', icon='send', on_click=_odeslat).classes(
                'bg-purple-600 text-white font-semibold px-4 h-10 rounded-xl'
            )

    dlg.open()


def _dialog_smazat_vse():
    with ui.dialog() as dlg, ui.card().classes('p-6 min-w-[320px]'):
        ui.label('Smazat celou databázi?').classes('text-xl font-bold text-red-700 mb-2')
        ui.label('Tato akce je nevratná. Všechny záznamy budou trvale odstraněny.').classes(
            'text-gray-600 mb-5')
        with ui.row().classes('gap-3 justify-end w-full'):
            ui.button('Zrušit', on_click=dlg.close).props('flat')
            def potvrdit():
                uloz_data([])
                dlg.close()
                ui.notify('Databáze narozenin smazána.', type='warning')
                intranet_logger.log_activity('Správce', 'Narozeniny', 'Databáze smazána')
                _tab_databaze.refresh()
                _tab_prehled.refresh()
            ui.button('Smazat vše', color='red', on_click=potvrdit)
    dlg.open()
