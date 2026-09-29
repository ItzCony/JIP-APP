from nicegui import ui, app, run, background_tasks
import datetime
import os
import re
import json
import time
import queue
import sqlite3
import atexit
import asyncio
import threading
import sys
import traceback
from contextlib import closing
import intranet_data
from intranet_ui_utils import refreshable_na_klienta
from nicegui.element import Element

# ==========================================
# --- ÚLOŽIŠTĚ AUDIT LOGU ---
# ==========================================
# Záznamy drží lokální SQLite databáze. Nezávisí na MySQL, takže se zapisuje
# i ve chvíli, kdy je hlavní DB nedostupná — a právě tehdy chceme stopu mít.
DB_FILE = "audit_log.db"
UCHOVAT_DNI = 365          # starší záznamy maže průběžná údržba

# Starý textový log. Do DB se naimportuje při startu; nový kód do něj píše už
# jen nouzově, když selže zápis do SQLite (další start ho zase naimportuje).
STARY_LOG = "activity.log"
ARCHIV_DIR = "Exporty_Logy"

# ==========================================
# --- KONZOLOVÉ PŘÍKAZY AUDIT KONZOLE ---
# ==========================================
# Flag čte web_main.py po návratu z ui.run(): graceful shutdown už proběhl
# (všechny on_shutdown hooky vč. bezpečného uzavření DB poolu), takže True
# znamená „nahraď běžící proces novým startem" přes os.execv.
RESTART_POZADOVAN = False

# Registr příkazů — slouží zároveň jako zdroj pro našeptávač ve vyhledávacím poli.
PRIKAZY = {
    '/reboot': 'Bezpečně vypne a znovu spustí celou aplikaci (uzavře DB spojení)',
    '/dark-mode on': 'Zapne tmavý režim (testovací fáze) pro tvůj účet',
    '/dark-mode off': 'Vypne tmavý režim pro tvůj účet',
    '/modul': 'Otevře přepínač modulů portálu (zapnutí / vypnutí)',
}

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')

# ==========================================
# --- IDENTIFIKACE KLIENTA (IP + ZAŘÍZENÍ) ---
# ==========================================
def popis_zarizeni(user_agent: str, je_brave: bool = False) -> str:
    """Z hlavičky User-Agent vyrobí čitelný popis typu 'Chrome 120 · Windows'.

    Obsahuje i hlavní verzi prohlížeče (pokud ji lze z User-Agentu vyčíst).
    `je_brave` = výsledek klientské detekce (Brave posílá stejný User-Agent
    jako Chrome, takže ze serveru ho jinak nelze rozeznat — viz zjisti_brave()).
    """
    if not user_agent:
        return 'Brave' if je_brave else ''
    ua = user_agent.lower()

    # Operační systém
    if 'windows' in ua:
        os_nazev = 'Windows'
    elif 'android' in ua:
        os_nazev = 'Android'
    elif 'iphone' in ua or 'ipad' in ua or 'ios' in ua:
        os_nazev = 'iOS'
    elif 'mac os' in ua or 'macintosh' in ua:
        os_nazev = 'macOS'
    elif 'linux' in ua:
        os_nazev = 'Linux'
    else:
        os_nazev = ''

    # Prohlížeč + token pro verzi (pořadí je důležité — Edge/Opera obsahují i 'chrome'/'safari')
    if 'edg' in ua:
        prohlizec, verze_token = 'Edge', r'edg(?:e|a|ios)?/(\d+)'
    elif 'opr' in ua or 'opera' in ua:
        prohlizec, verze_token = 'Opera', r'opr/(\d+)'
    elif 'samsungbrowser' in ua:
        prohlizec, verze_token = 'Samsung Internet', r'samsungbrowser/(\d+)'
    elif 'chrome' in ua or 'crios' in ua:
        prohlizec, verze_token = 'Chrome', r'(?:chrome|crios)/(\d+)'
    elif 'firefox' in ua or 'fxios' in ua:
        prohlizec, verze_token = 'Firefox', r'(?:firefox|fxios)/(\d+)'
    elif 'safari' in ua:
        prohlizec, verze_token = 'Safari', r'version/(\d+)'
    else:
        prohlizec, verze_token = '', ''

    # Hlavní verze (Brave hlásí verzi přes Chrome/NNN, proto čteme před přepisem názvu)
    verze = ''
    if verze_token:
        m = re.search(verze_token, ua)
        if m:
            verze = m.group(1)

    # Brave se maskuje za Chrome — pokud to klientská detekce potvrdila, přepíšeme.
    if je_brave and prohlizec in ('Chrome', ''):
        prohlizec = 'Brave'

    prohlizec_full = f'{prohlizec} {verze}'.strip() if prohlizec else ''
    casti = [c for c in (prohlizec_full, os_nazev) if c]
    return ' · '.join(casti)

async def zjisti_brave(timeout: float = 2.0) -> bool:
    """Zeptá se prohlížeče přes JS, zda jde o Brave (`navigator.brave.isBrave()`).

    Brave kvůli ochraně proti fingerprintingu posílá identický User-Agent jako
    Chrome, takže serverová detekce není možná. Musí se volat v kontextu
    připojeného klienta (např. v obsluze přihlášení). Při chybě vrací False.
    """
    try:
        vysledek = await ui.run_javascript(
            'if (navigator.brave && navigator.brave.isBrave) { return await navigator.brave.isBrave(); } return false;',
            timeout=timeout)
        return bool(vysledek)
    except Exception:
        return False

def ziskej_klienta_info(client, je_brave: bool = False) -> tuple:
    """Z NiceGUI klienta vytáhne (IP, popis_zařízení).

    Respektuje proxy hlavičky (X-Forwarded-For / X-Real-IP) — důležité,
    pokud aplikace běží za reverzní proxy (nginx apod.).
    `je_brave` = výsledek klientské detekce (viz zjisti_brave()).
    """
    ip = ''
    device = ''
    try:
        req = getattr(client, 'request', None)
        if req is not None:
            xff = req.headers.get('x-forwarded-for', '')
            if xff:
                ip = xff.split(',')[0].strip()
            if not ip:
                ip = (req.headers.get('x-real-ip', '') or '').strip()
            device = popis_zarizeni(req.headers.get('user-agent', ''), je_brave=je_brave)
        else:
            device = popis_zarizeni('', je_brave=je_brave)
        if not ip:
            ip = getattr(client, 'ip', '') or ''
    except Exception:
        pass
    # IPv6 loopback i mapované localhost zkrátíme na čitelnou formu
    if ip in ('::1', '127.0.0.1', '::ffff:127.0.0.1'):
        ip = 'localhost'
    elif ip.startswith('::ffff:'):
        ip = ip[7:]
    return ip, device


# ==========================================
# --- ZÁPIS LOGŮ ---
# ==========================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS zaznamy (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cas       TEXT NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS', místní čas
    uzivatel  TEXT NOT NULL DEFAULT '',    -- kdo akci provedl (dřív „kategorie")
    udalost   TEXT NOT NULL DEFAULT '',    -- typ akce, např. Přihlášení (dřív „úroveň")
    zavaznost TEXT NOT NULL DEFAULT 'default',
    zprava    TEXT NOT NULL DEFAULT '',
    ip        TEXT NOT NULL DEFAULT '',
    prohlizec TEXT NOT NULL DEFAULT '',
    system    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_zaznamy_cas       ON zaznamy(cas);
CREATE INDEX IF NOT EXISTS ix_zaznamy_uzivatel  ON zaznamy(uzivatel);
CREATE INDEX IF NOT EXISTS ix_zaznamy_udalost   ON zaznamy(udalost);
CREATE INDEX IF NOT EXISTS ix_zaznamy_zavaznost ON zaznamy(zavaznost);
CREATE TABLE IF NOT EXISTS meta (klic TEXT PRIMARY KEY, hodnota TEXT);
"""

_INSERT = ("INSERT INTO zaznamy (cas, uzivatel, udalost, zavaznost, zprava, ip, prohlizec, system) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")

_fronta: queue.Queue = queue.Queue()
_KONEC = object()                 # signál zapisovači: dopiš frontu a skonči
_zapisovac: threading.Thread | None = None
_init_zamek = threading.Lock()
_db_pripravena = False

# Nejvyšší id v DB. Zapisovač ho průběžně obnovuje (i o zápisy z jiných
# procesů), UI podle něj levně pozná, že přibyly nové záznamy.
POSLEDNI_ID = 0


def _pylower(s) -> str:
    return (s or '').strip().lower()


def _skryty_email(text) -> bool:
    return any(intranet_data.je_skryty_ucet(email=e) for e in _EMAIL_RE.findall(text or ''))


def _spojeni() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 10000')
    # SQLite lower() zná jen ASCII — české znaky převádí Python.
    conn.create_function('pylower', 1, _pylower, deterministic=True)
    conn.create_function('skryty_email', 1, _skryty_email)
    return conn


def init_db():
    """Založí schéma, naimportuje staré textové logy a spustí zapisovač.

    Volá se při startu (web_main) a pojistně i při prvním zápisu/čtení.
    Idempotentní, bezpečné z více vláken.
    """
    global _db_pripravena, POSLEDNI_ID
    if _db_pripravena:
        return
    with _init_zamek:
        if _db_pripravena:
            return
        try:
            with closing(_spojeni()) as conn:
                conn.execute('PRAGMA journal_mode = WAL')
                conn.executescript(_SCHEMA)
                _importuj_stare_logy(conn)
                _udrzba(conn)
                POSLEDNI_ID = conn.execute('SELECT COALESCE(MAX(id), 0) FROM zaznamy').fetchone()[0]
        except Exception as e:
            print(f"[audit] Inicializace DB selhala: {e}")
        # I po chybě: zapisovač při neúspěchu ukládá nouzově do textového souboru.
        _db_pripravena = True
        _spust_zapisovac()


def _spust_zapisovac():
    global _zapisovac
    if _zapisovac is not None and _zapisovac.is_alive():
        return
    _zapisovac = threading.Thread(target=_smycka_zapisu, name='audit-log-zapis', daemon=True)
    _zapisovac.start()


def _smycka_zapisu():
    """Jediné vlákno, které do DB zapisuje. Co se ve frontě nasbírá, jde jednou transakcí."""
    global POSLEDNI_ID
    conn = None
    posledni_udrzba = time.monotonic()
    while True:
        try:
            davka = [_fronta.get(timeout=2.0)]
        except queue.Empty:
            davka = []
        while True:
            try:
                davka.append(_fronta.get_nowait())
            except queue.Empty:
                break
        konec = any(p is _KONEC for p in davka)
        zaznamy = [p for p in davka if p is not _KONEC]
        try:
            if conn is None:
                conn = _spojeni()
            if zaznamy:
                with conn:
                    conn.executemany(_INSERT, zaznamy)
            # Obnovujeme i bez vlastního zápisu — zachytí zápisy z worker procesů.
            POSLEDNI_ID = conn.execute('SELECT COALESCE(MAX(id), 0) FROM zaznamy').fetchone()[0]
            if time.monotonic() - posledni_udrzba > 86400:
                posledni_udrzba = time.monotonic()
                _udrzba(conn)
        except Exception as e:
            if zaznamy:
                print(f"[audit] Zápis do DB selhal ({e}) — ukládám nouzově do {STARY_LOG}")
                _nouzovy_zapis(zaznamy)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
        for _ in davka:
            _fronta.task_done()
        if konec:
            break
    if conn is not None:
        conn.close()


def dokonci_zapis(timeout: float = 3.0):
    """Dopíše frontu do DB. Volat před os.execv (restart), který nespouští atexit."""
    if _zapisovac is None or not _zapisovac.is_alive():
        return
    _fronta.put(_KONEC)
    _zapisovac.join(timeout)


atexit.register(dokonci_zapis)


def _nouzovy_zapis(zaznamy):
    """Když SQLite selže, záznam nesmí zmizet — uložíme ho ve starém textovém formátu."""
    try:
        with open(STARY_LOG, 'a', encoding='utf-8') as f:
            for cas, uzivatel, udalost, _zav, zprava, ip, prohlizec, system in zaznamy:
                try:
                    cas = datetime.datetime.strptime(cas, '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M:%S')
                except ValueError:
                    pass
                # Textový log je řádkový — víceřádková zpráva by rozbila import.
                zprava = ' ⏎ '.join(zprava.splitlines())
                radek = f"[{cas}] [{uzivatel}] [{udalost}] {zprava}"
                meta = []
                if ip:
                    meta.append(f"ip={ip}")
                zarizeni = ' · '.join(x for x in (prohlizec, system) if x)
                if zarizeni:
                    meta.append(f"dev={zarizeni}")
                if meta:
                    radek += " ⟦" + " | ".join(meta) + "⟧"
                f.write(radek + "\n")
    except Exception as e:
        print(f"[audit] Nouzový zápis selhal: {e}")


def _udrzba(conn):
    """Smaže záznamy starší než UCHOVAT_DNI."""
    hranice = (datetime.datetime.now() - datetime.timedelta(days=UCHOVAT_DNI)).strftime('%Y-%m-%d %H:%M:%S')
    with conn:
        smazano = conn.execute('DELETE FROM zaznamy WHERE cas < ?', (hranice,)).rowcount
    if smazano:
        print(f"[audit] Údržba: smazáno {smazano} záznamů starších než {UCHOVAT_DNI} dní.")


def log_activity(kategorie, uroven, zprava, ip=None, device=None):
    """Zapíše záznam do audit logu. Neblokuje — zápis obstará vlákno na pozadí.

    kategorie = kdo (obvykle jméno uživatele), uroven = typ události,
    device = 'Prohlížeč · Systém'.
    """
    # Skrytý admin a servisní účet se do logu nezapisují vůbec.
    if intranet_data.je_skryty_ucet(jmeno=kategorie):
        return
    init_db()
    zprava = '' if zprava is None else str(zprava)
    prohlizec, system = _rozdel_zarizeni(device or '')
    _fronta.put((
        datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        str(kategorie or ''), str(uroven), _severity(uroven, zprava),
        zprava, str(ip or ''), prohlizec, system,
    ))
    _spust_zapisovac()

# ==========================================
# --- IMPORT STARÝCH TEXTOVÝCH LOGŮ ---
# ==========================================
# Strukturovaná metadata (IP / zařízení) byla na konci řádku v ⟦…⟧.
_META_RE = re.compile(r'\s*⟦([^⟧]*)⟧\s*$')
_ZACATEK_RADKU_RE = re.compile(r'^\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\] ')


def _parse_log_radek(log: str):
    """Rozparsuje '[čas] [kategorie] [úroveň] zpráva ⟦ip=… | dev=…⟧'.

    Vrací (čas, kategorie, úroveň, zpráva, ip, zařízení).
    Metadata (IP/zařízení) jsou volitelná — starší řádky je nemají.
    """
    ip = ''
    device = ''
    m = _META_RE.search(log)
    if m:
        for kus in m.group(1).split('|'):
            kus = kus.strip()
            if kus.startswith('ip='):
                ip = kus[3:].strip()
            elif kus.startswith('dev='):
                device = kus[4:].strip()
        log = log[:m.start()]

    try:
        cas  = log[1 : log.index(']')]
        zb   = log[log.index(']') + 3:]
        kat  = zb[: zb.index(']')]
        zb   = zb[zb.index(']') + 3:]
        urov = zb[: zb.index(']')]
        msg  = zb[zb.index(']') + 2:]
        return cas, kat, urov, msg, ip, device
    except Exception:
        return '', '', '', log, ip, device


def _zaznamy_ze_souboru(cesta: str) -> list:
    """Převede starý textový log na n-tice pro _INSERT. Víceřádkové zprávy (traceback) slepí."""
    bloky = []
    with open(cesta, encoding='utf-8', errors='replace') as f:
        for radek in f:
            radek = radek.rstrip('\r\n')
            if _ZACATEK_RADKU_RE.match(radek) or not bloky:
                bloky.append(radek)
            else:
                bloky[-1] += '\n' + radek
    out = []
    for blok in bloky:
        blok = blok.rstrip()
        if not blok:
            continue
        cas, kat, urov, msg, ip, device = _parse_log_radek(blok)
        dt = _parse_cas(cas)
        if dt is None:
            continue
        if intranet_data.je_skryty_ucet(jmeno=kat) or _skryty_email(blok):
            continue
        prohlizec, system = _rozdel_zarizeni(device)
        out.append((dt.strftime('%Y-%m-%d %H:%M:%S'), kat, urov, _severity(urov, msg),
                    msg, ip, prohlizec, system))
    return out


def _importuj_stare_logy(conn):
    """Jednorázově převede activity.log a archivy Exporty_Logy/activity_*.log do DB.

    Soubory se nemažou (forenzní stopa). Každý archiv se importuje právě jednou —
    evidence je v tabulce meta; unikátní klíč zároveň brání dvojímu importu
    ze souběžně startujících procesů (druhý insert selže a transakce se vrátí).
    """
    os.makedirs(ARCHIV_DIR, exist_ok=True)
    if os.path.exists(STARY_LOG) and os.path.getsize(STARY_LOG) > 0:
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        cil = os.path.join(ARCHIV_DIR, f'activity_{ts}.log')
        n = 1
        while os.path.exists(cil):
            n += 1
            cil = os.path.join(ARCHIV_DIR, f'activity_{ts}_{n}.log')
        try:
            os.replace(STARY_LOG, cil)
        except FileNotFoundError:
            pass  # souběžně startující proces ho už přesunul

    hotove = {r[0] for r in conn.execute("SELECT klic FROM meta WHERE klic LIKE 'import:%'")}
    for nazev in sorted(os.listdir(ARCHIV_DIR)):
        if not (nazev.startswith('activity_') and nazev.endswith('.log')):
            continue
        klic = f'import:{nazev}'
        if klic in hotove:
            continue
        try:
            zaznamy = _zaznamy_ze_souboru(os.path.join(ARCHIV_DIR, nazev))
            with conn:
                conn.executemany(_INSERT, zaznamy)
                conn.execute('INSERT INTO meta (klic, hodnota) VALUES (?, ?)', (klic, str(len(zaznamy))))
            print(f"[audit] Naimportováno {len(zaznamy)} záznamů z {nazev}.")
        except sqlite3.IntegrityError:
            pass  # mezitím naimportoval jiný proces
        except Exception as e:
            print(f"[audit] Import {nazev} selhal: {e}")

# ==========================================
# --- ČTENÍ (FILTRY, STRÁNKOVÁNÍ) ---
# ==========================================
# Text, ve kterém hledá fulltext. Datum i ve tvaru DD.MM.YYYY, jak ho uživatel vidí.
_HLEDANY_TEXT = ("pylower(uzivatel || ' ' || udalost || ' ' || zprava || ' ' || ip || ' ' || "
                 "prohlizec || ' ' || system || ' ' || strftime('%d.%m.%Y %H:%M:%S', cas))")


def _skryte_ucty():
    """(jména, e-maily) skrytých účtů, lowercase — stejný zdroj jako intranet_data.je_skryty_ucet."""
    if intranet_data._SKRYTE_UCTY is not None:
        return intranet_data._SKRYTE_UCTY
    return intranet_data._nacti_skryte_ucty()


def _podminky(filtr: dict, navic: list | None = None):
    """filtr → (' WHERE …', parametry). Skryté účty se odfiltrují vždy."""
    podm, par = [], []
    jmena, emaily = _skryte_ucty()
    if jmena:
        podm.append(f"pylower(uzivatel) NOT IN ({', '.join('?' * len(jmena))})")
        par += sorted(jmena)
    if emaily:
        # instr() je levné předsítko; přesnou shodu celé adresy ověří Python.
        podm.append('NOT ((' + ' OR '.join(['instr(lower(zprava), ?) > 0'] * len(emaily))
                    + ') AND skryty_email(zprava))')
        par += sorted(emaily)
    for sloupec, klic in (('uzivatel', 'uzivatele'), ('udalost', 'udalosti'), ('zavaznost', 'zavaznosti')):
        hodnoty = list(filtr.get(klic) or [])
        if hodnoty:
            podm.append(f"{sloupec} IN ({', '.join('?' * len(hodnoty))})")
            par += hodnoty
    if filtr.get('od'):
        podm.append('cas >= ?')
        par.append(f"{filtr['od']} 00:00:00")
    if filtr.get('do'):
        podm.append('cas <= ?')
        par.append(f"{filtr['do']} 23:59:59")
    hledany = _pylower(filtr.get('hledat'))
    if hledany:
        podm.append(f'instr({_HLEDANY_TEXT}, ?) > 0')
        par.append(hledany)
    for sql, hodnota in (navic or []):
        podm.append(sql)
        par.append(hodnota)
    return (' WHERE ' + ' AND '.join(podm)) if podm else '', par


def nacti_stranku(filtr: dict, strana: int = 1, na_stranku: int = 50):
    """→ (záznamy jako dicty, celkový počet). Nejnovější první."""
    init_db()
    where, par = _podminky(filtr)
    with closing(_spojeni()) as conn:
        celkem = conn.execute(f'SELECT COUNT(*) FROM zaznamy{where}', par).fetchone()[0]
        radky = conn.execute(
            f'SELECT * FROM zaznamy{where} ORDER BY cas DESC, id DESC LIMIT ? OFFSET ?',
            par + [na_stranku, (max(strana, 1) - 1) * na_stranku]).fetchall()
    return [dict(r) for r in radky], celkem


def pocet_novych(filtr: dict, od_id: int) -> int:
    """Kolik záznamů odpovídajících filtru přibylo s id vyšším než od_id."""
    init_db()
    where, par = _podminky(filtr, [('id > ?', od_id)])
    with closing(_spojeni()) as conn:
        return conn.execute(f'SELECT COUNT(*) FROM zaznamy{where}', par).fetchone()[0]


def moznosti_filtru():
    """→ (uživatelé, události) vyskytující se v logu, abecedně."""
    init_db()
    where, par = _podminky({})
    with closing(_spojeni()) as conn:
        uzivatele = [r[0] for r in conn.execute(f'SELECT DISTINCT uzivatel FROM zaznamy{where}', par) if r[0]]
        udalosti = [r[0] for r in conn.execute(f'SELECT DISTINCT udalost FROM zaznamy{where}', par) if r[0]]
    return sorted(uzivatele, key=str.lower), sorted(udalosti, key=str.lower)

# ==========================================
# --- GLOBÁLNÍ ZACHYTÁVÁNÍ CHYB PYTHONU ---
# ==========================================
def _popis_chyby(e: BaseException) -> str:
    """Jednořádkový popis výjimky: typ, text a místo vzniku (soubor:řádek funkce).
    Plný traceback dál vypisuje NiceGUI / Python na stderr."""
    tb = traceback.extract_tb(e.__traceback__)
    misto = f' @ {os.path.basename(tb[-1].filename)}:{tb[-1].lineno} {tb[-1].name}' if tb else ''
    return f'{type(e).__name__}: {e}{misto}'

def globalni_zachytavac_chyb(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log_activity("Systémový Pád", "Chyba", f"Neočekávaná chyba aplikace: {_popis_chyby(exc_value)}")
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

# ==========================================
# --- LOGOVÁNÍ KAŽDÉHO KROKU V UI ---
# ==========================================
# Úrovně kroků — UI je nebarví jako chybu, i když text tlačítka obsahuje „chyb…".
UROVNE_KROKU = ('Krok', 'Otevření modulu')
# Změny hodnot logujeme jen u výběrových prvků. Textová pole (input/textarea)
# vynechána záměrně: logovala by se každá napsaná hodnota vč. hesel a 2FA kódů.
_PRVKY_S_HODNOTOU = (ui.select, ui.checkbox, ui.switch, ui.toggle, ui.radio, ui.date, ui.time)

def log_krok(uroven, zprava, client=None):
    """Zapíše krok za aktuálně přihlášeného uživatele (+ IP / zařízení z klienta)."""
    try:
        jmeno = app.storage.user.get('user_name') or 'Nepřihlášený'
    except Exception:
        jmeno = 'Systém'  # mimo UI kontext (background task, startup)
    ip, device = ziskej_klienta_info(client) if client is not None else (None, None)
    log_activity(jmeno, uroven, zprava, ip=ip, device=device)

def _popis_prvku(el) -> str:
    """Čitelný název prvku: text / label / první text uvnitř (vč. tooltipu) / ikona."""
    t = (getattr(el, 'text', '') or el.props.get('label')
         or next((d.text for d in el.descendants() if getattr(d, 'text', '')), '')
         or el.props.get('icon') or type(el).__name__)
    return ' '.join(str(t).split())[:80]

def _zaloguj_udalost(el, msg: dict) -> None:
    """Kliknutí nebo změna výběru → záznam „Krok" s modulem, kde se stal."""
    listener = el._event_listeners.get(msg.get('listener_id'))
    if listener is None:
        return
    typ = listener.type.split('.')[0]
    if typ == 'click':
        zprava = f'Kliknutí: {_popis_prvku(el)}'
    elif typ == 'update:modelValue' and isinstance(el, _PRVKY_S_HODNOTOU):
        hodnota = el.value
        moznosti = getattr(el, 'options', None)
        if isinstance(moznosti, dict) and not isinstance(hodnota, list):
            hodnota = moznosti.get(hodnota, hodnota)
        zprava = f'{_popis_prvku(el)} → {" ".join(str(hodnota).split())[:80]}'
    else:
        return
    # Prvek může mít víc listenerů stejného typu (on_click + .on('click')) —
    # prohlížeč pošle zprávu za každý, logujeme jen za první.
    prvni = next((l.id for l in el._event_listeners.values() if l.type.split('.')[0] == typ), None)
    if prvni != listener.id:
        return
    try:
        u = app.storage.user
        modul = u.get('intranet_tab') if u.get('user_id') else 'přihlašování'
    except Exception:
        modul = '-'
    log_krok('Krok', f'[{modul or "-"}] {zprava}', el.client)

def _zaloguj_vyjimku(e: Exception) -> None:
    """Chyby v obsluze tlačítek, timerů a tasků (app.on_exception) do auditu."""
    if isinstance(e, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return  # běžné odpojení prohlížeče
    if isinstance(e, RuntimeError) and 'parent slot' in str(e):
        return  # timer smazaného elementu — neškodné (viz web_main)
    log_krok('Chyba', _popis_chyby(e))

def aktivuj_globalni_logovani():
    """Záchyt pádů, chyb v UI handlerech a logování každého kroku ve všech modulech.

    Hook sedí na Element._handle_event, kudy projde každá událost z prohlížeče,
    takže moduly nemusí nic volat samy.
    """
    # ponytail: Element._handle_event je interní API NiceGUI — po upgradu pusť test_audit_kroky.py
    if getattr(Element._handle_event, '_audit', False):
        return  # už aktivní — dvojí obalení = dvojí záznamy
    sys.excepthook = globalni_zachytavac_chyb
    app.on_exception(_zaloguj_vyjimku)
    puvodni = Element._handle_event

    def _handle_event(self, msg):
        puvodni(self, msg)
        try:
            _zaloguj_udalost(self, msg)
        except Exception as e:  # logování nesmí shodit obsluhu události
            print(f"Chyba logování kroku: {e}")

    _handle_event._audit = True
    Element._handle_event = _handle_event

# ==========================================
# --- POMOCNÍCI ---
# ==========================================

def _rozdel_zarizeni(device: str):
    """'Brave 149 · Windows' → ('Brave 149', 'Windows'). Tolerantní k chybějícím částem."""
    if not device:
        return '', ''
    casti = device.split(' · ')
    if len(casti) >= 2:
        return casti[0].strip(), ' · '.join(casti[1:]).strip()
    return casti[0].strip(), ''

def _parse_cas(cas: str):
    """'DD.MM.YYYY HH:MM:SS' → datetime nebo None."""
    try:
        return datetime.datetime.strptime(cas, "%d.%m.%Y %H:%M:%S")
    except Exception:
        return None

def _dt_iso(cas: str):
    """'YYYY-MM-DD HH:MM:SS' (formát v DB) → datetime nebo None."""
    try:
        return datetime.datetime.strptime(cas, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def _relativni_cas(dt: datetime.datetime) -> str:
    """Vrátí čitelný relativní čas, např. 'před 3 min'."""
    if not dt:
        return ''
    s = (datetime.datetime.now() - dt).total_seconds()
    if s < 60:
        return 'právě teď'
    if s < 3600:
        return f'před {int(s // 60)} min'
    if s < 86400:
        return f'před {int(s // 3600)} h'
    dny = int(s // 86400)
    return 'včera' if dny == 1 else f'před {dny} dny'

def _pocet(n: int, jedna: str, dve_az_ctyri: str, pet: str) -> str:
    """České skloňování podle počtu: 1 záznam, 3 záznamy, 5 záznamů."""
    tvar = jedna if n == 1 else dve_az_ctyri if 2 <= n <= 4 else pet
    return f"{n:,}".replace(',', ' ') + f" {tvar}"

# Závažnost: klíč → (popisek, barva tečky, barvy badge)
_ZAVAZNOSTI = {
    'chyba':      ('Chyba',      'bg-red-500',     'bg-red-100 text-red-700'),
    'zamitnut':   ('Zamítnutí',  'bg-red-400',     'bg-red-100 text-red-700'),
    'warning':    ('Varování',   'bg-amber-400',   'bg-amber-100 text-amber-700'),
    'storno':     ('Storno',     'bg-orange-400',  'bg-orange-100 text-orange-700'),
    'prihlaseni': ('Přihlášení', 'bg-emerald-400', 'bg-emerald-100 text-emerald-700'),
    'schvaleni':  ('Schválení',  'bg-emerald-400', 'bg-emerald-100 text-emerald-700'),
    'odhlas':     ('Odhlášení',  'bg-gray-300',    'bg-slate-100 text-slate-600'),
    'export':     ('Export',     'bg-blue-400',    'bg-blue-100 text-blue-700'),
    'zaloha':     ('Záloha',     'bg-violet-400',  'bg-violet-100 text-violet-700'),
    'default':    ('Ostatní',    'bg-gray-300',    'bg-gray-100 text-gray-600'),
}

_KAT_PALETTE = [
    'bg-sky-100 text-sky-700',
    'bg-purple-100 text-purple-700',
    'bg-teal-100 text-teal-700',
    'bg-pink-100 text-pink-700',
    'bg-indigo-100 text-indigo-700',
    'bg-lime-100 text-lime-700',
    'bg-orange-100 text-orange-700',
    'bg-cyan-100 text-cyan-700',
]
_kat_color_cache: dict = {}

def _kat_color(kat: str) -> str:
    if kat not in _kat_color_cache:
        _kat_color_cache[kat] = _KAT_PALETTE[len(_kat_color_cache) % len(_KAT_PALETTE)]
    return _kat_color_cache[kat]

def _severity(uroven: str, zprava: str):
    if uroven in UROVNE_KROKU:
        return 'default'
    t = (uroven + ' ' + zprava).lower()
    if any(k in t for k in ('chyb', 'error', 'exception', 'kritick', 'pád')):
        return 'chyba'
    if any(k in t for k in ('varován', 'warning')):
        return 'warning'
    if 'zamítn' in t or 'zamitnut' in t:
        return 'zamitnut'
    if 'storn' in t:
        return 'storno'
    if 'schvál' in t or 'schvaleni' in t:
        return 'schvaleni'
    if 'přihlášen' in t or 'prihlaseni' in t:
        return 'prihlaseni'
    if 'odhlášen' in t or 'odhlaseni' in t:
        return 'odhlas'
    if 'export' in t or 'stažen' in t or 'stáhnul' in t:
        return 'export'
    if 'záloha' in t or 'obnova' in t or 'zaloha' in t:
        return 'zaloha'
    return 'default'


# ==========================================
# --- UŽIVATELSKÉ ROZHRANÍ (UI) ---
# ==========================================
# Sloupce: Datum, Čas, Uživatel, Událost, IP adresa, Prohlížeč, Systém, Zpráva
_GRID = 'grid-template-columns: 110px 90px 165px 140px 130px 160px 105px 1fr'
_NA_STRANKU = (50, 100, 200)


def _vykresli_detail(z: dict, filtruj_uzivatele, hledej):
    """Dialog se všemi poli záznamu. Vytvářet ve stabilním rodiči — řádky se překreslují."""
    popisek, dot_cls, badge_cls = _ZAVAZNOSTI.get(z['zavaznost'], _ZAVAZNOSTI['default'])
    dt = _dt_iso(z['cas'])
    with ui.dialog() as dlg, ui.card().classes('p-6 gap-3 w-full max-w-2xl'):
        with ui.row().classes('w-full items-center gap-3 no-wrap'):
            ui.element('div').classes(f'w-2.5 h-2.5 rounded-full flex-shrink-0 {dot_cls}')
            ui.label(z['udalost'] or '—').classes('text-lg font-bold text-gray-800')
            ui.label(popisek).classes(f'text-[10px] font-bold px-1.5 py-0.5 rounded {badge_cls}')
            ui.space()
            ui.label(f"#{z['id']}").classes('text-xs text-gray-400 font-mono')

        cas = f"{dt.strftime('%d.%m.%Y %H:%M:%S')}  ·  {_relativni_cas(dt)}" if dt else z['cas']
        with ui.element('div').classes('grid gap-x-4 gap-y-1.5 w-full').style('grid-template-columns: 90px 1fr'):
            for nazev, hodnota in (('Čas', cas), ('Uživatel', z['uzivatel']), ('IP adresa', z['ip']),
                                   ('Prohlížeč', z['prohlizec']), ('Systém', z['system'])):
                ui.label(nazev).classes('text-xs text-gray-400 pt-0.5')
                ui.label(hodnota or '—').classes(
                    'text-sm text-gray-800 break-all' if hodnota else 'text-sm text-gray-300')

        ui.label('Zpráva').classes('text-xs text-gray-400 mt-1')
        ui.label(z['zprava'] or '—').classes(
            'w-full text-xs font-mono text-gray-800 whitespace-pre-wrap break-words '
            'bg-gray-50 border border-gray-200 rounded-lg p-3 max-h-[40vh] overflow-y-auto')

        def _a_zavri(akce):
            dlg.close()
            akce()

        text = (f"[{cas}] [{z['uzivatel']}] [{z['udalost']}] {z['zprava']}"
                f"  (IP {z['ip'] or '—'}, {z['prohlizec'] or '—'} · {z['system'] or '—'})")
        with ui.row().classes('w-full gap-1 mt-1 items-center'):
            if z['uzivatel']:
                ui.button('Jen tento uživatel', icon='person',
                          on_click=lambda: _a_zavri(lambda: filtruj_uzivatele(z['uzivatel']))) \
                    .props('flat dense no-caps color=primary')
            if z['ip']:
                ui.button('Hledat tuto IP', icon='public',
                          on_click=lambda: _a_zavri(lambda: hledej(z['ip']))) \
                    .props('flat dense no-caps color=primary')
            ui.space()
            ui.button(icon='content_copy',
                      on_click=lambda: (ui.clipboard.write(text), ui.notify('Záznam zkopírován', type='positive'))) \
                .props('flat round dense color=grey-7').tooltip('Kopírovat záznam')
            ui.button('Zavřít', on_click=dlg.close).props('flat no-caps color=grey-8')
    dlg.on('hide', dlg.delete)   # otevírá se často — zavřený dialog nenecháváme viset v DOM
    dlg.open()


def _vykresli_radek(z: dict, i: int, novy: bool, on_click):
    popisek, dot_cls, badge_cls = _ZAVAZNOSTI.get(z['zavaznost'], _ZAVAZNOSTI['default'])
    msg_cls = {'chyba': 'text-red-700 font-medium', 'warning': 'text-amber-700'}.get(z['zavaznost'], 'text-gray-700')
    row_bg = 'bg-emerald-50' if novy else ('bg-white' if i % 2 == 0 else 'bg-gray-50')
    dt = _dt_iso(z['cas'])

    with ui.element('div').classes(
        f'w-full grid items-start px-5 py-2 gap-2 {row_bg} cursor-pointer '
        f'border-b border-gray-100 hover:bg-blue-50/50 transition-colors duration-75'
    ).style(_GRID).on('click', on_click):

        # Datum (+ tečka závažnosti, relativní čas v tooltipu) a Čas
        with ui.element('div').classes('flex items-center gap-2'):
            ui.element('div').classes(f'w-2 h-2 rounded-full flex-shrink-0 {dot_cls}').tooltip(popisek)
            datum_lbl = ui.label(dt.strftime('%d.%m.%Y') if dt else z['cas']) \
                .classes('text-[11px] text-gray-400 font-mono whitespace-nowrap')
            if dt:
                datum_lbl.tooltip(_relativni_cas(dt))
        ui.label(dt.strftime('%H:%M:%S') if dt else '') \
            .classes('text-[11px] text-gray-500 font-mono whitespace-nowrap pt-0.5')

        # Uživatel jako badge
        with ui.element('div').classes('flex items-start pt-0.5 min-w-0'):
            if z['uzivatel']:
                ui.label(z['uzivatel']).classes(
                    f"text-[10px] font-bold px-2 py-0.5 rounded-full {_kat_color(z['uzivatel'])} truncate max-w-full")

        # Událost
        with ui.element('div').classes('flex items-start pt-0.5 min-w-0'):
            if z['udalost']:
                ui.label(z['udalost']).classes(
                    f'text-[10px] font-bold px-1.5 py-0.5 rounded {badge_cls} truncate max-w-full')

        # IP adresa, Prohlížeč (vč. verze), Systém
        for hodnota, ikona, extra in ((z['ip'], 'public', ' font-mono'),
                                      (z['prohlizec'], 'web', ''),
                                      (z['system'], 'computer', '')):
            with ui.element('div').classes('flex items-center gap-1.5 pt-0.5 min-w-0'):
                if hodnota:
                    ui.icon(ikona, size='13px', color='gray-400')
                    ui.label(hodnota).classes(f'text-[11px] text-gray-500 truncate{extra}')
                else:
                    ui.label('—').classes('text-[11px] text-gray-300')

        # Zpráva — v řádku max. 2 řádky, celá je v detailu
        ui.label(z['zprava']).classes(f'text-[11px] {msg_cls} break-words leading-snug min-w-0 line-clamp-2')


@refreshable_na_klienta
def vykresli_logy(user_name, vsechna_prava):
    if 'vse' not in vsechna_prava and 'admin_logy' not in vsechna_prava:
        ui.label('Přístup odepřen. Tuto sekci mohou vidět pouze administrátoři.').classes('text-2xl font-bold text-red-600')
        return

    init_db()
    filtr = {'hledat': '', 'uzivatele': [], 'udalosti': [], 'od': None, 'do': None}
    stav = {
        'strana': 1, 'na_stranku': _NA_STRANKU[0], 'celkem': 0,
        'zaklad_id': 0,     # nejvyšší id v DB při posledním načtení — od něj se počítají „nové"
        'token': 0,         # pořadí požadavků; doběhnutý starší výsledek se zahodí
        'ticho': False,     # hromadné nastavování filtrů — nenačítat po každé změně
        'kontroluji': False,
    }
    koren = ui.element('div').classes('w-full')   # stabilní rodič pro dialogy

    # ── FILTRAČNÍ LIŠTA ────────────────────────────────────────
    with koren, ui.element('div').classes(
        'w-full flex items-center gap-2 flex-wrap '
        'px-5 py-2.5 bg-white rounded-t-2xl border-x border-t border-gray-200'
    ):
        hledat_input = ui.input(placeholder='Hledat: uživatel, IP, událost, zpráva…  ·  „/" pro příkazy',
                                autocomplete=list(PRIKAZY)) \
            .props('dense clearable outlined debounce=200 input-class=text-sm') \
            .classes('w-full sm:w-80')
        uzivatel_sel = ui.select([], multiple=True, label='Uživatel', with_input=True, clearable=True) \
            .props('dense outlined use-chips options-dense').classes('w-full sm:w-56')
        udalost_sel = ui.select([], multiple=True, label='Událost', with_input=True, clearable=True) \
            .props('dense outlined use-chips options-dense').classes('w-full sm:w-56')
        with ui.button('Období', icon='event').props('outline no-caps color=grey-8') as datum_btn:
            with ui.menu():
                datum = ui.date().props('range minimal first-day-of-week=1')
                with ui.row().classes('w-full justify-end px-2 pb-2'):
                    ui.button('Vymazat', on_click=lambda: datum.set_value(None)).props('flat dense no-caps')
        zrusit_btn = ui.button('Zrušit filtry', icon='filter_alt_off') \
            .props('flat dense no-caps color=grey-7')
        zrusit_btn.set_visibility(False)

        ui.space()
        nove_btn = ui.button(icon='arrow_upward').props('dense no-caps unelevated rounded color=primary') \
            .classes('text-xs px-3')
        nove_btn.set_visibility(False)
        with ui.row().classes('items-center gap-1.5') as zive_ind:
            ui.element('span').classes('w-2 h-2 rounded-full bg-emerald-500 animate-pulse')
            ui.label('Živě').classes('text-[11px] text-gray-500')
        zive_ind.tooltip('Nové záznamy se na první stránce objevují samy')
        souhrn_lbl = ui.label('').classes('text-xs text-gray-500 whitespace-nowrap')

    # ── ZÁHLAVÍ SLOUPCŮ ────────────────────────────────────────
    with koren, ui.element('div').classes(
        'w-full grid px-5 py-2 gap-2 '
        'bg-gray-50 border-x border-gray-200 '
        'text-[10px] font-black tracking-widest text-gray-400 uppercase'
    ).style(_GRID):
        for nazev in ('Datum', 'Čas', 'Uživatel', 'Událost',
                      'IP adresa', 'Prohlížeč', 'Systém', 'Zpráva'):
            ui.label(nazev)

    # ── ZÁZNAMY ───────────────────────────────────────────────
    with koren:
        log_container = ui.element('div').classes(
            'w-full h-[60vh] min-h-[420px] overflow-y-auto bg-white border-x border-t border-gray-200')
        with log_container, ui.element('div').classes('flex items-center justify-center h-48'):
            ui.spinner(size='lg', color='grey-5')

    # ── STRÁNKOVÁNÍ ───────────────────────────────────────────
    with koren, ui.element('div').classes(
        'w-full flex items-center justify-between gap-3 flex-wrap '
        'px-5 py-2 bg-white rounded-b-2xl border border-gray-200'
    ):
        rozsah_lbl = ui.label('').classes('text-xs text-gray-500')
        with ui.row().classes('items-center gap-3'):
            strankovani = ui.pagination(1, 1, direction_links=True, value=1) \
                .props('dense boundary-numbers max-pages=7 color=grey-8 active-color=primary')
            ui.select({n: f'{n} / stránka' for n in _NA_STRANKU}, value=stav['na_stranku'],
                      on_change=lambda e: _zmen_na_stranku(e.value)) \
                .props('dense borderless options-dense').classes('text-xs')

    # ── NAČÍTÁNÍ ──────────────────────────────────────────────
    def _filtr_aktivni() -> bool:
        return bool(filtr['hledat'].strip() or filtr['uzivatele'] or filtr['udalosti'] or filtr['od'])

    def _otevri_detail(z):
        with koren:
            _vykresli_detail(z, _filtruj_uzivatele, _hledej)

    async def nacti(auto: bool = False):
        stav['token'] += 1
        token = stav['token']
        predchozi_id = stav['zaklad_id']
        zaklad = POSLEDNI_ID
        vysledek = await run.io_bound(nacti_stranku, dict(filtr), stav['strana'], stav['na_stranku'])
        if vysledek is None or token != stav['token'] or log_container.is_deleted:
            return
        zaznamy, celkem = vysledek
        stran = max(1, -(-celkem // stav['na_stranku']))
        if stav['strana'] > stran:        # filtr nebo mazání zmenšily výsledek
            stav['strana'] = stran
            await nacti(auto)
            return

        stav['zaklad_id'], stav['celkem'] = zaklad, celkem
        strankovani.max = stran
        strankovani.value = stav['strana']
        strankovani.update()
        souhrn_lbl.set_text(_pocet(celkem, 'záznam', 'záznamy', 'záznamů'))
        prvni = (stav['strana'] - 1) * stav['na_stranku']
        rozsah_lbl.set_text(f'{prvni + 1}–{prvni + len(zaznamy)} z {celkem:,}'.replace(',', ' ')
                            if zaznamy else '')
        nove_btn.set_visibility(False)
        zive_ind.set_visibility(stav['strana'] == 1)
        zrusit_btn.set_visibility(_filtr_aktivni())

        log_container.clear()
        with log_container:
            if not zaznamy:
                aktivni = _filtr_aktivni()
                with ui.element('div').classes('flex flex-col items-center justify-center h-48 gap-3'):
                    ui.icon('search_off' if aktivni else 'inbox', size='3rem', color='gray-300')
                    ui.label('Žádné odpovídající záznamy' if aktivni else 'Žádné záznamy') \
                        .classes('text-gray-400 text-sm')
                return
            for i, z in enumerate(zaznamy):
                # Při živém doplnění zvýrazníme, co přibylo od minula.
                novy = auto and predchozi_id and z['id'] > predchozi_id
                _vykresli_radek(z, i, novy, lambda _, z=z: _otevri_detail(z))

    def _nacti_od_zacatku():
        if stav['ticho']:
            return
        stav['strana'] = 1
        background_tasks.create(nacti(), name='audit-log-nacti')

    async def _kontrola_novych():
        """Časovač: přibylo něco v DB? Na 1. stránce rovnou doplní, jinde nabídne tlačítko."""
        if stav['kontroluji'] or POSLEDNI_ID <= stav['zaklad_id']:
            return
        stav['kontroluji'] = True
        try:
            if stav['strana'] == 1:
                await nacti(auto=True)
                return
            n = await run.io_bound(pocet_novych, dict(filtr), stav['zaklad_id'])
            if n and not nove_btn.is_deleted:
                nove_btn.set_text(_pocet(n, 'nový záznam', 'nové záznamy', 'nových záznamů'))
                nove_btn.set_visibility(True)
        finally:
            stav['kontroluji'] = False

    async def _nacti_moznosti():
        vysledek = await run.io_bound(moznosti_filtru)
        if vysledek is None or uzivatel_sel.is_deleted:
            return
        for sel, moznosti in zip((uzivatel_sel, udalost_sel), vysledek):
            # Vybrané hodnoty musí zůstat mezi volbami, jinak select vyhodí ValueError.
            sel.set_options(sorted(set(moznosti) | set(sel.value or []), key=str.lower), value=sel.value)

    # ── OVLÁDÁNÍ FILTRŮ ───────────────────────────────────────
    def _on_hledat():
        hodnota = hledat_input.value or ''
        if hodnota.strip().startswith('/'):
            return  # režim příkazu — neaplikovat jako filtr, čeká se na Enter
        filtr['hledat'] = hodnota
        _nacti_od_zacatku()
    hledat_input.on_value_change(_on_hledat)

    def _on_select(klic, e):
        filtr[klic] = list(e.value or [])
        _nacti_od_zacatku()
    uzivatel_sel.on_value_change(lambda e: _on_select('uzivatele', e))
    udalost_sel.on_value_change(lambda e: _on_select('udalosti', e))
    for sel in (uzivatel_sel, udalost_sel):
        sel.on('popup-show', _nacti_moznosti)   # nově zalogovaní uživatelé / události

    def _on_datum(e):
        v = e.value
        if isinstance(v, dict):
            od, do = v.get('from'), v.get('to')
        elif isinstance(v, str) and v:
            od = do = v          # v režimu range vrací jeden den jako řetězec
        else:
            od = do = None
        filtr['od'], filtr['do'] = od, do
        if od:
            f = lambda d: datetime.datetime.strptime(d, '%Y-%m-%d').strftime('%d.%m.%Y')
            datum_btn.set_text(f(od) if od == do else f'{f(od)} – {f(do)}')
            datum_btn.props('color=primary')
        else:
            datum_btn.set_text('Období')
            datum_btn.props('color=grey-8')
        _nacti_od_zacatku()
    datum.on_value_change(_on_datum)

    def _zrus_filtry():
        stav['ticho'] = True
        try:
            hledat_input.value = ''
            uzivatel_sel.value = []
            udalost_sel.value = []
            datum.value = None
        finally:
            stav['ticho'] = False
        _nacti_od_zacatku()
    zrusit_btn.on_click(_zrus_filtry)

    def _filtruj_uzivatele(jmeno):
        if jmeno not in uzivatel_sel.options:
            uzivatel_sel.set_options(sorted([*uzivatel_sel.options, jmeno], key=str.lower))
        uzivatel_sel.value = [jmeno]

    def _hledej(text):
        hledat_input.value = text

    def _na_stranu(cislo):
        if cislo and cislo != stav['strana']:
            stav['strana'] = cislo
            background_tasks.create(nacti(), name='audit-log-nacti')
    strankovani.on_value_change(lambda e: _na_stranu(e.value))

    def _zmen_na_stranku(n):
        stav['na_stranku'] = n
        _nacti_od_zacatku()

    nove_btn.on_click(lambda: (strankovani.set_value(1)))

    # ── KONZOLOVÉ PŘÍKAZY (/reboot …) ──────────────────────────
    def _potvrd_reboot():
        with ui.dialog() as dlg, ui.card().classes('p-6 max-w-md gap-3'):
            with ui.row().classes('items-center gap-3'):
                ui.icon('restart_alt', size='md', color='red-600')
                ui.label('Restart aplikace').classes('text-lg font-bold')
            ui.label(
                'Opravdu restartovat celou aplikaci? Všichni uživatelé budou dočasně '
                'odpojeni. Databázová spojení budou bezpečně uzavřena a server se '
                'automaticky spustí znovu.'
            ).classes('text-sm text-gray-600 leading-snug')

            async def _proved_restart():
                global RESTART_POZADOVAN
                RESTART_POZADOVAN = True
                log_activity(user_name, 'Restart systému',
                             'Příkaz /reboot — vyžádán bezpečný restart aplikace z audit konzole')
                dlg.close()
                ui.notify('Aplikace se restartuje… Stránka se za chvíli obnoví sama.',
                          type='warning', position='top', timeout=5000)
                await asyncio.sleep(1.5)  # ať se notifikace a zápis logu stihnou doručit
                app.shutdown()            # graceful: proběhnou on_shutdown hooky (safe DB close)

            with ui.row().classes('w-full justify-end gap-2 mt-1'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps color=grey-8')
                ui.button('Restartovat', icon='restart_alt', on_click=_proved_restart) \
                    .props('unelevated no-caps color=negative')
        dlg.open()

    def _nastav_dark(zapnout: bool):
        # Tmavý režim je v testovací fázi — nemá přepínač v UI, přepíná se jen zde.
        # Volba je per-user (app.storage.user); po zápisu obnovíme stránku, aby se
        # uložený stav aplikoval při novém sestavení hlavičky (viz intranet.py).
        try:
            app.storage.user['dark_mode'] = bool(zapnout)
        except Exception:
            pass
        stav = 'zapnut' if zapnout else 'vypnut'
        log_activity(user_name, 'Tmavý režim',
                     f'Příkaz /dark-mode {"on" if zapnout else "off"} — '
                     f'tmavý režim {stav} z audit konzole')
        ui.notify(f'Tmavý režim {stav}. Stránka se obnovuje…',
                  type='info', position='top', timeout=2000)
        ui.navigate.reload()  # přímo, bez ui.timer — konzole si přestavuje slot (padal parent slot)

    def _muze_menit_moduly():
        # Konzoli vidí i právo admin_logy, ale moduly patří pod „Nastavení portálu"
        # (právo mysql). Bez téhle kontroly by konzole obešla oprávnění z UI.
        return 'vse' in vsechna_prava or 'mysql' in vsechna_prava

    def _barva_prepinace(zapnuty):
        return f'toggle-color={"green-7" if zapnuty else "red-7"}'

    def _prepni_modul(klic, zapnout, prepinac):
        if zapnout is None:
            return
        # Tlačítka jsou pro ostatní disabled, ale kontrola patří i na server.
        if not _muze_menit_moduly():
            log_activity(user_name, 'Přepnutí modulu',
                         f'ODMÍTNUTO — {klic} {"on" if zapnout else "off"} '
                         f'z audit konzole bez práva „Nastavení portálu"')
            ui.notify('Nemáte oprávnění měnit moduly portálu.', type='negative')
            return
        import intranet_nastaveni  # lazy — intranet_nastaveni importuje tenhle modul
        intranet_nastaveni.prepni_modul(klic, zapnout, user_name)
        prepinac.props(_barva_prepinace(zapnout))
        ui.notify(f'{intranet_data.MODULY[klic][0]} — {"ZAPNUTO" if zapnout else "VYPNUTO"}',
                  type='positive' if zapnout else 'warning', position='top')

    def _dialog_moduly():
        """/modul — přepínač modulů portálu.

        Každé kliknutí na Zapnuto/Vypnuto se uloží hned (prepni_modul), proto
        dialog nemá „Uložit", jen „Zavřít". Bez práva „Nastavení portálu" jde
        dialog jen číst.
        """
        muze_menit = _muze_menit_moduly()
        nast = intranet_data.nacti_nastaveni_intranetu()
        with ui.dialog() as dlg, ui.card().classes('p-6 gap-1 w-full max-w-xl'):
            ui.label('Moduly portálu').classes('text-lg font-bold')
            ui.label('Změna se uloží hned po kliknutí a všem uživatelům se projeví do 10 sekund.'
                     if muze_menit else
                     'Jen pro čtení. Moduly může měnit jen právo „Nastavení portálu".') \
                .classes('text-xs text-gray-500 mb-2')
            with ui.column().classes('w-full gap-0 max-h-[60vh] overflow-y-auto'):
                for klic, (popisek, _) in intranet_data.MODULY.items():
                    zapnuty = bool(nast.get(klic, True))
                    with ui.row().classes('items-center gap-3 w-full py-1.5 border-b border-gray-100 no-wrap'):
                        # Popisky v MODULY nesou emoji ikonu (kvůli Nastavení) — tady bez ní
                        ui.label(re.sub(r'^\W+', '', popisek)).classes('text-sm text-gray-800 flex-1')
                        prepinac = ui.toggle(
                            {True: 'Zapnuto', False: 'Vypnuto'}, value=zapnuty,
                            on_change=(lambda e, k=klic: _prepni_modul(k, e.value, e.sender))
                            if muze_menit else None,
                        ).props(f'dense no-caps unelevated {_barva_prepinace(zapnuty)}')
                        if not muze_menit:
                            prepinac.props('disable')
            ui.button('Zavřít', on_click=dlg.close).props('flat no-caps color=grey-8').classes('self-end mt-3')
        dlg.open()

    def _zpracuj_prikaz():
        prikaz = (hledat_input.value or '').strip()
        if not prikaz.startswith('/'):
            return
        hledat_input.value = ''
        if prikaz == '/reboot':
            _potvrd_reboot()
        elif prikaz == '/dark-mode on':
            _nastav_dark(True)
        elif prikaz == '/dark-mode off':
            _nastav_dark(False)
        elif prikaz in ('/modul', '/moduly'):
            _dialog_moduly()
        else:
            ui.notify(f'Neznámý příkaz: {prikaz}', type='negative')
    hledat_input.on('keydown.enter', _zpracuj_prikaz)


    with koren:
        ui.timer(3.0, _kontrola_novych)
    background_tasks.create(nacti(), name='audit-log-nacti')
    background_tasks.create(_nacti_moznosti(), name='audit-log-moznosti')
