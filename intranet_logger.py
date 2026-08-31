from nicegui import ui, app
import datetime
import os
import re
import csv
import io
import zipfile
import asyncio
import threading
from collections import deque
import sys
import traceback
import intranet_monitor
import intranet_data
from intranet_ui_utils import refreshable_na_klienta

LOG_FILE = "activity.log"
EXPORT_DIR = "Exporty_Logy"
os.makedirs(EXPORT_DIR, exist_ok=True)

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
    '/moduly': 'Vypíše aktuální stav všech modulů portálu',
}


def _prikazy_modulu():
    """Konkrétní varianty „/modul <alias> on|off" do našeptávače konzole.

    Registr modulů žije v intranet_data.MODULY — odsud se jen odvozují aliasy,
    aby seznam nebyl druhou kopií, která se s Nastavením portálu rozejde.
    """
    return [f'/modul {klic.removesuffix("_zapnuty")} {stav}'
            for klic in intranet_data.MODULY for stav in ('on', 'off')]

# ==========================================
# --- GLOBÁLNÍ PAMĚŤ PRO LOGY (RAM CACHE) ---
# ==========================================
LOG_CACHE = deque(maxlen=500)
CACHE_INITIALIZED = False
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')

def je_skryty_radek(raw: str) -> bool:
    """Řádek logu patří skrytému účtu (admin) — nesmí se nikde zobrazit.

    Kategorie logu je jméno uživatele, proto ji porovnáme. Zprávy ale nesou
    i e-mail (přihlášení, odhlášení), a ten identitu prozradí i tehdy, když
    se jméno neshoduje (např. DB nedostupná). Filtrujeme i historické řádky,
    které vznikly dřív, než se admin přestal zapisovat.
    """
    if intranet_data.je_skryty_ucet(jmeno=_parse_log_radek(raw)[1]):
        return True
    return any(intranet_data.je_skryty_ucet(email=e) for e in _EMAIL_RE.findall(raw))

def init_log_cache():
    """Načte posledních 500 řádků z disku do RAM při prvním startu"""
    global CACHE_INITIALIZED
    if CACHE_INITIALIZED: return

    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines[-500:]:
                    if line.strip() and not je_skryty_radek(line):
                        LOG_CACHE.append(line.strip())
        except Exception: pass
    CACHE_INITIALIZED = True

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
def _fyzicky_zapis_na_disk(log_entry):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"Chyba zápisu logu: {e}")

POVOLENE_UROVNE = {
    "Přihlášení", "Odhlášení", "Chyba přihlášení",
    "Podání žádosti", "Schválení žádosti", "Zamítnutí žádosti", "Stornování žádosti",
    "Export docházky", "Export účetní", "Export faktur", "IKOS Export",
    "Export práv", "Úkolovník", "Výsledky poboček",
    "Záloha DB", "Obnova DB",
    "Přepnutí modulu",
    "Autom. záloha faktur",
    "Odeslání e-mailu",
    "Znacky JIP",
    "Společenský večer",
    "Vizitky",
    "Lupa",
    "Schůzky",
}

# Strukturovaná metadata (IP / zařízení) připojujeme na konec řádku do ⟦…⟧.
# Tím zůstává formát zpětně kompatibilní — starší řádky bez metadat se parsují stejně.
_META_RE = re.compile(r'\s*⟦([^⟧]*)⟧\s*$')

def log_activity(kategorie, uroven, zprava, ip=None, device=None):
    if uroven not in POVOLENE_UROVNE:
        return
    # Skrytý admin a servisní účet se do logu nezapisují vůbec.
    if intranet_data.je_skryty_ucet(jmeno=kategorie):
        return
    init_log_cache()
    cas = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    log_entry = f"[{cas}] [{kategorie}] [{uroven}] {zprava}"

    meta_casti = []
    if ip:
        meta_casti.append(f"ip={ip}")
    if device:
        meta_casti.append(f"dev={device}")
    if meta_casti:
        log_entry += " ⟦" + " | ".join(meta_casti) + "⟧"

    LOG_CACHE.append(log_entry)

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _fyzicky_zapis_na_disk, log_entry)
    except RuntimeError:
        threading.Thread(target=_fyzicky_zapis_na_disk, args=(log_entry,)).start()

def get_logs(limit=500):
    init_log_cache()
    return list(LOG_CACHE)[-limit:]

# ==========================================
# --- GLOBÁLNÍ ZACHYTÁVÁNÍ CHYB PYTHONU ---
# ==========================================
def globalni_zachytavac_chyb(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    chyba_text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    log_activity("Systémový Pád", "Chyba", f"Neočekávaná chyba aplikace:\n{chyba_text}")
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

def aktivuj_globalni_logovani():
    sys.excepthook = globalni_zachytavac_chyb

# ==========================================
# --- PARSOVÁNÍ A POMOCNÍCI ---
# ==========================================

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

def _relativni_cas(dt: datetime.datetime) -> str:
    """Vrátí čitelný relativní čas, např. 'před 3 min'."""
    if not dt:
        return ''
    s = (datetime.datetime.now() - dt).total_seconds()
    if s < 0:
        return 'právě teď'
    if s < 60:
        return 'právě teď'
    if s < 3600:
        return f'před {int(s // 60)} min'
    if s < 86400:
        return f'před {int(s // 3600)} h'
    dny = int(s // 86400)
    return 'včera' if dny == 1 else f'před {dny} dny'

# Barvy levého borderu + barevné chemy pro badge
_SEVERITY_SCHEMA = {
    'chyba':   ('border-red-500',    'bg-red-100 text-red-700'),
    'zamitnut':('border-red-400',    'bg-red-100 text-red-700'),
    'warning': ('border-amber-400',  'bg-amber-100 text-amber-700'),
    'export':  ('border-blue-400',   'bg-blue-100 text-blue-700'),
    'prihlaseni': ('border-emerald-400', 'bg-emerald-100 text-emerald-700'),
    'schvaleni':  ('border-emerald-400', 'bg-emerald-100 text-emerald-700'),
    'odhlas':  ('border-slate-400',  'bg-slate-100 text-slate-600'),
    'zaloha':  ('border-violet-400', 'bg-violet-100 text-violet-700'),
    'storno':  ('border-orange-400', 'bg-orange-100 text-orange-700'),
    'default': ('border-gray-300',   'bg-gray-100 text-gray-600'),
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

# ==========================================
# --- PŘEHLED PŘIHLÁŠENÝCH UŽIVATELŮ ---
# ==========================================
# Barvy avatarů — deterministicky podle e-mailu, ať má člověk pořád stejnou.
_AVATAR_PALETTE = [
    ('bg-sky-100',     'text-sky-700'),
    ('bg-violet-100',  'text-violet-700'),
    ('bg-emerald-100', 'text-emerald-700'),
    ('bg-amber-100',   'text-amber-700'),
    ('bg-rose-100',    'text-rose-700'),
    ('bg-teal-100',    'text-teal-700'),
    ('bg-indigo-100',  'text-indigo-700'),
    ('bg-orange-100',  'text-orange-700'),
]


def _avatar_styl(email: str):
    """(bg třída, text třída) pro avatar — stejný e-mail = vždy stejná barva."""
    idx = sum(ord(z) for z in (email or '?')) % len(_AVATAR_PALETTE)
    return _AVATAR_PALETTE[idx]


def _inicialy(jmeno: str) -> str:
    """'Jan Novák' → 'JN', 'admin' → 'AD'."""
    casti = [c for c in (jmeno or '').replace('.', ' ').split() if c]
    if not casti:
        return '?'
    if len(casti) == 1:
        return casti[0][:2].upper()
    return (casti[0][0] + casti[-1][0]).upper()


def _ikona_zarizeni(os_nazev: str) -> str:
    """Material ikona podle operačního systému z popisu zařízení."""
    o = (os_nazev or '').lower()
    if 'android' in o or 'ios' in o:
        return 'smartphone'
    if 'windows' in o:
        return 'desktop_windows'
    if 'mac' in o:
        return 'laptop_mac'
    if 'linux' in o:
        return 'terminal'
    return 'devices'


def _posledni_aktivita_map() -> dict:
    """kategorie logu (= jméno uživatele, lower) → čas jeho posledního záznamu.

    Kategorie zapisuje ``log_activity`` jako ``user_name``, takže poslední řádek
    dané kategorie je zároveň poslední aktivitou uživatele. Bereme z RAM cache,
    žádné čtení disku navíc.
    """
    out: dict = {}
    for raw in reversed(get_logs(500)):
        cas, kat, _uroven, _msg, _ip, _dev = _parse_log_radek(raw)
        klic = (kat or '').strip().lower()
        if klic and klic not in out:
            dt = _parse_cas(cas)
            if dt:
                out[klic] = dt
    return out


def _aktivni_relace() -> list:
    """Aktivní uživatelé + stav živého připojení + poslední aktivita z logu.

    ``intranet_session`` importuje tento modul, proto import až uvnitř funkce.
    """
    import intranet_session
    posledni = _posledni_aktivita_map()
    out = []
    for zaznam in intranet_monitor.ziskej_aktivni():
        u = dict(zaznam)
        token = intranet_session.GLOBAL_ACTIVE_SESSIONS.get(u['email'])
        u['zive'] = bool(token) and intranet_session.ZIVE_PRIPOJENI.get(token, 0) > 0
        dt = posledni.get((u['jmeno'] or '').strip().lower())
        u['aktivita'] = _relativni_cas(dt) if dt else ''
        out.append(u)
    return out


def _pocet_relaci(n: int) -> str:
    """České skloňování: 1 relace / 2–4 relace / 5+ relací."""
    if n == 1:
        return '1 relace'
    if 2 <= n <= 4:
        return f'{n} relace'
    return f'{n} relací'


def _severity(uroven: str, zprava: str):
    t = (uroven + ' ' + zprava).lower()
    if any(k in t for k in ('chyb', 'error', 'exception', 'kritick', 'pad')):
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

# Skupiny pro rychlé filtrování (chip filtr v UI)
_FILTR_SKUPINY = {
    'vse':        None,
    'prihlaseni': {'prihlaseni'},
    'pristup':    {'chyba', 'zamitnut'},   # bezpečnost: chyby + zamítnutí + chyby přihlášení
    'zmeny':      {'schvaleni', 'storno', 'zaloha'},
    'export':     {'export'},
}


# ==========================================
# --- UŽIVATELSKÉ ROZHRANÍ (UI) ---
# ==========================================

@refreshable_na_klienta
def vykresli_logy(user_name, vsechna_prava):
    if 'vse' not in vsechna_prava and 'admin_logy' not in vsechna_prava:
        ui.label('Přístup odepřen. Tuto sekci mohou vidět pouze administrátoři.').classes('text-2xl font-bold text-red-600')
        return

    # Stav filtrů — sdílený mezi vstupy a automatickým refreshem
    stav_filtru = {'hledat': '', 'skupina': 'vse', 'diagnostika': False}

    # ── TOOLBAR ────────────────────────────────────────────────
    with ui.element('div').classes(
        'w-full flex items-center justify-between gap-4 flex-wrap '
        'px-5 py-3 mb-0 bg-gray-100 rounded-t-2xl border border-gray-200'
    ):
        with ui.row().classes('gap-3 items-center'):
            ui.icon('security', size='sm', color='gray-500')
            ui.label('Audit Log').classes('text-sm font-black tracking-wide text-gray-600')
            ui.label('Forenzní záznam aktivity · IP · zařízení').classes('text-[11px] text-gray-400 hidden sm:block')

        with ui.row().classes('gap-2 items-center'):
            # ── Tlačítko přihlášených uživatelů ──────────────────
            def _otevrit_prihlasene():
                # 'podpis' = seřazená množina (e-mail, živý) — seznam se přestaví
                # jen při reálné změně, jinak se patchují pouze časové popisky.
                stav: dict = {'podpis': None, 'labels': {}, 'hledat': '', 'rezim': 'vse'}

                try:
                    _muj_email = str(app.storage.user.get('user_email', '')).lower()
                except Exception:
                    _muj_email = ''

                def _filtruj(relace):
                    q = stav['hledat']
                    rez = stav['rezim']
                    out = []
                    for u in relace:
                        if rez == 'zivi' and not u['zive']:
                            continue
                        if rez == 'cekajici' and u['zive']:
                            continue
                        if q and q not in f"{u['jmeno']} {u['email']} {u.get('ip', '')}".lower():
                            continue
                        out.append(u)
                    return out

                def _odhlas_uzivatele(email, jmeno):
                    """Potvrzovací dialog → admin vynutí odhlášení uživatele."""
                    import intranet_session
                    with ui.dialog() as dlg_o, ui.card().classes('p-6 rounded-2xl w-full max-w-md'):
                        ui.label('Odhlásit uživatele?').classes('text-xl font-bold text-red-600 mb-2')
                        ui.label(f'„{jmeno}" bude odhlášen a přesměrován na přihlašovací obrazovku. '
                                 f'Relace se ukončí do ~30 sekund.').classes('text-sm text-gray-600 mb-5')
                        def _potvrd():
                            intranet_session.vynut_odhlaseni(email)
                            log_activity(user_name, "Odhlášení",
                                         f"Administrátor vynutil odhlášení uživatele: {jmeno} ({email})")
                            dlg_o.close()
                            ui.notify(f'Uživatel „{jmeno}" byl odhlášen.', type='positive')
                            stav['podpis'] = None       # vynutí přestavbu seznamu
                            _tick()
                        with ui.row().classes('w-full justify-between'):
                            ui.button('Zrušit', on_click=dlg_o.close).classes('bg-gray-200 text-gray-700 font-bold px-6')
                            ui.button('Odhlásit', icon='logout', on_click=_potvrd).classes(
                                'bg-red-600 hover:bg-red-700 text-white font-bold px-6')
                    dlg_o.open()

                def _prazdny_stav(je_filtr: bool):
                    with ui.element('div').classes(
                        'flex flex-col items-center justify-center gap-3 py-12 text-gray-400 w-full'
                    ):
                        ui.icon('search_off' if je_filtr else 'person_off', size='2.5rem')
                        ui.label('Filtru neodpovídá žádná relace' if je_filtr
                                 else 'Žádný přihlášený uživatel').classes('text-sm')

                def _sestav_seznam(relace, je_filtr: bool):
                    """Přestaví seznam — volá se jen při změně složení nebo filtru."""
                    stav['labels'] = {}
                    seznam_kontejner.clear()
                    with seznam_kontejner:
                        if not relace:
                            _prazdny_stav(je_filtr)
                            return
                        for u in relace:
                            je_ja = u['email'].lower() == _muj_email
                            bg, fg = _avatar_styl(u['email'])
                            prohlizec, os_nazev = _rozdel_zarizeni(u.get('device', ''))
                            with ui.element('div').classes(
                                'flex items-center gap-4 px-6 py-3 w-full hover:bg-gray-50 transition-colors'
                            ):
                                # Stavová tečka — živé připojení vs. čekání na reconnect
                                ui.element('span').classes(
                                    'w-2.5 h-2.5 rounded-full shrink-0 ' +
                                    ('bg-emerald-500 animate-pulse' if u['zive'] else 'bg-gray-300')
                                ).tooltip('Živé připojení' if u['zive'] else 'Čeká na obnovení spojení')
                                # Avatar s iniciálami
                                with ui.element('div').classes(
                                    f'w-10 h-10 rounded-full {bg} flex items-center justify-center shrink-0'
                                ):
                                    ui.label(_inicialy(u['jmeno'])).classes(f'text-xs font-black {fg}')
                                # Identita
                                with ui.element('div').classes('flex flex-col flex-1 min-w-0'):
                                    with ui.element('div').classes('flex items-center gap-2 min-w-0'):
                                        ui.label(u['jmeno']).classes('text-sm font-semibold text-gray-800 truncate')
                                        if je_ja:
                                            ui.label('vy').classes(
                                                'bg-emerald-100 text-emerald-700 text-[10px] font-bold '
                                                'px-1.5 py-0.5 rounded shrink-0')
                                    ui.label(u['email']).classes('text-xs text-gray-400 truncate')
                                    if not u['zive']:
                                        ui.label('čeká na obnovení spojení').classes(
                                            'text-[10px] text-amber-600 font-semibold')
                                # Zařízení + IP
                                with ui.element('div').classes('hidden md:flex flex-col shrink-0 w-44 min-w-0'):
                                    with ui.element('div').classes('flex items-center gap-1.5 min-w-0'):
                                        ui.icon(_ikona_zarizeni(os_nazev), size='14px', color='gray-400')
                                        ui.label(prohlizec or '—').classes('text-xs text-gray-600 truncate')
                                        if os_nazev:
                                            ui.label(os_nazev).classes('text-[10px] text-gray-400 truncate')
                                    with ui.element('div').classes('flex items-center gap-1.5 min-w-0'):
                                        ui.icon('lan', size='12px', color='gray-300')
                                        ui.label(u.get('ip') or '—').classes(
                                            'text-[11px] text-gray-400 font-mono truncate')
                                # Časová osa relace
                                with ui.element('div').classes('flex flex-col items-end shrink-0 w-32'):
                                    ui.label(f'od {u["od"]}').classes('text-[11px] text-gray-400 font-mono')
                                    trvani_lbl = ui.label(u['trvani']).classes('text-xs font-bold text-emerald-600')
                                    akt_lbl = ui.label(u['aktivita'] or 'bez aktivity').classes('text-[10px] text-gray-400')
                                stav['labels'][u['email']] = (trvani_lbl, akt_lbl)
                                # Vynucené odhlášení (nikdy ne sebe sama)
                                if je_ja:
                                    ui.element('div').classes('w-[104px] shrink-0')
                                else:
                                    ui.button('Odhlásit', icon='logout',
                                              on_click=lambda _e, em=u['email'], jm=u['jmeno']: _odhlas_uzivatele(em, jm)) \
                                        .props('flat dense no-caps size=sm') \
                                        .classes('text-red-500 hover:text-red-700 shrink-0 w-[104px]')

                def _tick():
                    relace = _aktivni_relace()
                    zivych = sum(1 for u in relace if u['zive'])
                    ceka = len(relace) - zivych
                    online_lbl.set_text(f'{zivych} online')
                    ceka_lbl.set_text(f'{ceka} čeká na spojení')
                    ceka_lbl.set_visibility(bool(ceka))
                    patka_lbl.set_text(_pocet_relaci(len(relace)))

                    filtr = _filtruj(relace)
                    podpis = tuple(sorted((u['email'], u['zive']) for u in filtr))
                    if podpis != stav['podpis']:
                        stav['podpis'] = podpis
                        _sestav_seznam(filtr, je_filtr=bool(relace) and not filtr)
                    else:
                        for u in filtr:
                            par = stav['labels'].get(u['email'])
                            if par:
                                par[0].set_text(u['trvani'])
                                par[1].set_text(u['aktivita'] or 'bez aktivity')

                def _nastav(klic, hodnota):
                    stav[klic] = hodnota
                    stav['podpis'] = None       # filtr se změnil → přestav seznam
                    _tick()

                with ui.dialog() as dlg_uzivatele:
                    # Pevné okno 820 x 600 px; na malém displeji se smrskne na viewport.
                    with ui.card().classes(
                        'p-0 rounded-2xl overflow-hidden bg-white flex flex-col'
                    ).style('width:820px; max-width:92vw; height:600px; max-height:85vh'):
                        # Hlavička
                        with ui.element('div').classes(
                            'flex items-center gap-3 px-6 py-4 w-full bg-gradient-to-r from-emerald-600 to-teal-600'
                        ):
                            ui.icon('groups', color='white', size='sm')
                            ui.label('Aktuálně přihlášení').classes('text-white font-bold text-base flex-1')
                            online_lbl = ui.label('0 online').classes(
                                'bg-white text-emerald-700 text-xs font-bold px-2.5 py-1 rounded-full whitespace-nowrap')
                            ceka_lbl = ui.label('').classes(
                                'bg-amber-100 text-amber-800 text-xs font-bold px-2.5 py-1 rounded-full whitespace-nowrap')
                            ui.button(icon='close', on_click=dlg_uzivatele.close).props('flat round dense color=white')
                        # Filtry
                        with ui.element('div').classes(
                            'flex items-center gap-3 px-6 py-3 w-full flex-wrap bg-gray-50 border-b border-gray-200'
                        ):
                            ui.input(placeholder='Hledat jméno, e-mail nebo IP…',
                                     on_change=lambda e: _nastav('hledat', (e.value or '').strip().lower())) \
                                .props('dense outlined clearable debounce=200').classes('flex-1 min-w-[220px]')
                            ui.toggle({'vse': 'Vše', 'zivi': 'Připojení', 'cekajici': 'Čekající'}, value='vse',
                                      on_change=lambda e: _nastav('rezim', e.value or 'vse')) \
                                .props('dense unelevated no-caps toggle-color=teal-7')
                        # Seznam
                        seznam_kontejner = ui.element('div').classes(
                            'flex flex-col divide-y divide-gray-100 w-full flex-1 overflow-y-auto')
                        # Patička
                        with ui.element('div').classes(
                            'flex items-center justify-between px-6 py-3 w-full bg-gray-50 border-t border-gray-100'
                        ):
                            patka_lbl = ui.label('').classes('text-xs text-gray-500 font-semibold')
                            ui.label('aktualizace každé 2 s').classes('text-[11px] text-gray-400')

                _tick()
                _tick_timer = ui.timer(2.0, _tick)
                dlg_uzivatele.on('hide', lambda _: _tick_timer.cancel())
                dlg_uzivatele.open()

            with ui.button(icon='people', on_click=_otevrit_prihlasene).props('flat round size=sm').classes(
                    'text-emerald-600 hover:text-emerald-800').tooltip('Přihlášení uživatelé'):
                _prihlaseni_pocet = ui.badge('0', color='emerald').classes('text-[10px] font-bold').props('floating')

            def _obnov_pocet():
                _prihlaseni_pocet.set_text(str(len(intranet_monitor.ziskej_aktivni())))

            _obnov_pocet()
            ui.timer(10.0, _obnov_pocet)
            # ─────────────────────────────────────────────────────

            def _sber_parsed():
                """Vrátí seznam rozparsovaných záznamů (nejnovější první)."""
                out = []
                for raw in get_logs(500):
                    cas, kat, urov, msg, ip, device = _parse_log_radek(raw)
                    out.append((cas, kat, urov, msg, ip, device))
                return out

            def stahnout_log():
                if os.path.exists(LOG_FILE):
                    cesta_zip = os.path.join(EXPORT_DIR, f"Zaloha_Logu_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.zip")
                    # Celý soubor, ale bez řádků skrytého admina (proto writestr,
                    # ne write — zabalený originál by je vynesl ven).
                    with open(LOG_FILE, 'r', encoding='utf-8') as f:
                        obsah = ''.join(r for r in f if not je_skryty_radek(r))
                    with zipfile.ZipFile(cesta_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                        zf.writestr("activity.log", obsah)
                    log_activity(user_name, "Export", "Uživatel stáhnul historii Audit Logu (.zip)")
                    ui.download(cesta_zip)
                    ui.notify('Logy staženy.', type='positive')
                else:
                    ui.notify('Soubor logů je prázdný.', type='warning')

            def stahnout_csv():
                """Export aktuálně načtených záznamů jako strukturované CSV (Excel-friendly)."""
                parsed = _sber_parsed()
                if not parsed:
                    ui.notify('Žádné záznamy k exportu.', type='warning')
                    return
                buf = io.StringIO()
                buf.write('﻿')  # BOM pro správné UTF-8 v Excelu
                w = csv.writer(buf, delimiter=';')
                w.writerow(['Čas', 'Uživatel / Kategorie', 'Událost', 'Závažnost', 'IP adresa', 'Zařízení', 'Zpráva'])
                for cas, kat, urov, msg, ip, device in parsed:
                    sev = _severity(urov, msg)
                    w.writerow([cas, kat, urov, sev, ip, device, msg])
                cesta = os.path.join(EXPORT_DIR, f"AuditLog_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv")
                with open(cesta, 'w', encoding='utf-8', newline='') as f:
                    f.write(buf.getvalue())
                log_activity(user_name, "Export", "Uživatel exportoval Audit Log do CSV")
                ui.download(cesta)
                ui.notify('CSV exportováno.', type='positive')

            def vymazat_log():
                with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl w-full max-w-md'):
                    ui.label('Vymazat celý log?').classes('text-xl font-bold text-red-600 mb-3')
                    ui.label('Tato akce je nevratná — všechny záznamy budou trvale odstraněny.').classes('text-sm text-gray-600 mb-5')
                    def on_potvrdit():
                        LOG_CACHE.clear()
                        try:
                            open(LOG_FILE, 'w').close()
                        except Exception:
                            pass
                        log_activity(user_name, "Systém", "Uživatel nenávratně vymazal historii logů")
                        dlg.close()
                        vykresli_logy.refresh()
                        ui.notify('Logy vymazány.', type='info')
                    with ui.row().classes('w-full justify-between'):
                        ui.button('Zrušit', on_click=dlg.close).classes('bg-gray-200 text-gray-700 font-bold px-6')
                        ui.button('Vymazat', icon='delete_forever', on_click=on_potvrdit).classes(
                            'bg-red-600 hover:bg-red-700 text-white font-bold px-6')
                dlg.open()

            def _prepni_diagnostiku():
                stav_filtru['diagnostika'] = not stav_filtru['diagnostika']
                zap = stav_filtru['diagnostika']
                if zap:
                    diag_btn.classes(remove='text-gray-400', add='text-blue-600 bg-blue-100')
                else:
                    diag_btn.classes(remove='text-blue-600 bg-blue-100', add='text-gray-400')
                diag_btn.tooltip('Vypnout diagnostický pohled' if zap else 'Diagnostický pohled (sloupec Prohlížeč)')
                prekresli_zahlavi()
                update_logs()

            diag_btn = ui.button(icon='view_column', on_click=_prepni_diagnostiku).props('flat round size=sm').classes(
                'text-gray-400 hover:text-blue-600').tooltip('Diagnostický pohled (sloupec Prohlížeč)')

            ui.button(icon='grid_on', on_click=stahnout_csv).props('flat round size=sm').classes(
                'text-gray-400 hover:text-emerald-600').tooltip('Export do CSV')
            ui.button(icon='download', on_click=stahnout_log).props('flat round size=sm').classes(
                'text-gray-400 hover:text-gray-700').tooltip('Stáhnout celý log (.zip)')
            ui.button(icon='delete_forever', on_click=vymazat_log).props('flat round size=sm').classes(
                'text-gray-400 hover:text-red-500').tooltip('Vymazat log')

    # ── STATISTICKÝ PRUH ───────────────────────────────────────
    with ui.element('div').classes(
        'w-full grid grid-cols-2 sm:grid-cols-4 gap-px bg-gray-200 border-x border-gray-200'
    ):
        def _stat_box(ikona, barva):
            with ui.element('div').classes('bg-white px-4 py-2.5 flex items-center gap-3'):
                with ui.element('div').classes(f'w-8 h-8 rounded-lg flex items-center justify-center bg-{barva}-100 shrink-0'):
                    ui.icon(ikona, size='16px', color=f'{barva}-600')
                with ui.element('div').classes('flex flex-col min-w-0'):
                    hodnota = ui.label('0').classes('text-base font-black text-gray-800 leading-none')
                    popis = ui.label('').classes('text-[10px] text-gray-400 uppercase tracking-wide')
            return hodnota, popis

        stat_celkem_val, stat_celkem_lbl = _stat_box('receipt_long', 'slate')
        stat_login_val, stat_login_lbl = _stat_box('login', 'emerald')
        stat_fail_val, stat_fail_lbl = _stat_box('block', 'red')
        stat_err_val, stat_err_lbl = _stat_box('warning', 'amber')

    # ── FILTRAČNÍ LIŠTA ────────────────────────────────────────
    with ui.element('div').classes(
        'w-full flex items-center justify-between gap-3 flex-wrap '
        'px-5 py-2.5 bg-white border-x border-t border-gray-200'
    ):
        hledat_input = ui.input(placeholder='Hledat: uživatel, IP, událost, zpráva…  ·  „/" pro příkazy',
                                autocomplete=list(PRIKAZY) + _prikazy_modulu()) \
            .props('dense clearable outlined debounce=200 input-class=text-sm') \
            .classes('w-full sm:w-80')

        with ui.row().classes('gap-1 items-center') as chip_row:
            _chip_def = [
                ('vse', 'Vše', 'list'),
                ('prihlaseni', 'Přihlášení', 'login'),
                ('pristup', 'Bezpečnost', 'verified_user'),
                ('zmeny', 'Změny', 'edit'),
                ('export', 'Exporty', 'download'),
            ]
            chip_refs = {}
            def _vyber_skupinu(klic):
                stav_filtru['skupina'] = klic
                for k, btn in chip_refs.items():
                    if k == klic:
                        btn.classes(remove='bg-gray-100 text-gray-500', add='bg-blue-600 text-white')
                    else:
                        btn.classes(remove='bg-blue-600 text-white', add='bg-gray-100 text-gray-500')
                update_logs()
            for klic, popisek, ikona in _chip_def:
                zaklad = 'bg-blue-600 text-white' if klic == 'vse' else 'bg-gray-100 text-gray-500'
                b = ui.button(popisek, icon=ikona, on_click=lambda k=klic: _vyber_skupinu(k)) \
                    .props('flat dense no-caps size=sm') \
                    .classes(f'rounded-full px-3 text-[11px] font-bold {zaklad}')
                chip_refs[klic] = b

    # ── ZÁHLAVÍ SLOUPCŮ ────────────────────────────────────────
    # Klasický pohled vs. diagnostický.
    # Diagnostika navíc: datum a čas zvlášť + sloupce „Prohlížeč" (vč. verze) a „Systém".
    _GRID_KLASIK = 'grid-template-columns: 170px 180px 150px 140px 1fr'
    _GRID_DIAG   = 'grid-template-columns: 110px 90px 165px 140px 130px 160px 105px 1fr'

    def _aktualni_grid():
        return _GRID_DIAG if stav_filtru['diagnostika'] else _GRID_KLASIK

    zahlavi_box = ui.element('div').classes('w-full')

    def prekresli_zahlavi():
        zahlavi_box.clear()
        with zahlavi_box:
            with ui.element('div').classes(
                'w-full grid px-5 py-2 gap-2 '
                'bg-gray-50 border-x border-gray-200 '
                'text-[10px] font-black tracking-widest text-gray-400 uppercase'
            ).style(_aktualni_grid()):
                if stav_filtru['diagnostika']:
                    ui.label('Datum')
                    ui.label('Čas')
                else:
                    ui.label('Čas')
                ui.label('Uživatel / Kategorie')
                ui.label('Událost')
                ui.label('IP adresa')
                if stav_filtru['diagnostika']:
                    ui.label('Prohlížeč')
                    ui.label('Systém')
                ui.label('Zpráva')

    prekresli_zahlavi()

    # ── ZÁZNAMY ───────────────────────────────────────────────
    log_container = ui.element('div').classes(
        'w-full h-[60vh] min-h-[420px] overflow-y-auto '
        'bg-white border border-gray-200 rounded-b-2xl'
    )

    _SEV_DOT = {
        'chyba':      'bg-red-400',
        'zamitnut':   'bg-red-400',
        'warning':    'bg-amber-400',
        'storno':     'bg-orange-400',
        'prihlaseni': 'bg-emerald-400',
        'schvaleni':  'bg-emerald-400',
        'odhlas':     'bg-gray-300',
        'export':     'bg-blue-400',
        'zaloha':     'bg-violet-400',
        'default':    'bg-gray-300',
    }
    _SEV_MSG = {
        'chyba':   'text-red-700 font-medium',
        'warning': 'text-amber-700',
        'default': 'text-gray-700',
    }

    def _aktualizuj_statistiky(parsed):
        dnes = datetime.date.today()
        celkem = len(parsed)
        login_dnes = 0
        fail_dnes = 0
        err_dnes = 0
        for cas, kat, urov, msg, ip, device in parsed:
            dt = _parse_cas(cas)
            je_dnes = (dt is not None and dt.date() == dnes)
            sev = _severity(urov, msg)
            if not je_dnes:
                continue
            if sev == 'prihlaseni':
                login_dnes += 1
            if 'chyba přihlášení' in urov.lower() or 'chyba přihlášení' in (urov + msg).lower():
                fail_dnes += 1
            elif sev in ('chyba', 'zamitnut'):
                err_dnes += 1
        stat_celkem_val.set_text(str(celkem))
        stat_celkem_lbl.set_text('Záznamů celkem')
        stat_login_val.set_text(str(login_dnes))
        stat_login_lbl.set_text('Přihlášení dnes')
        stat_fail_val.set_text(str(fail_dnes))
        stat_fail_lbl.set_text('Selhání dnes')
        stat_err_val.set_text(str(err_dnes))
        stat_err_lbl.set_text('Chyby dnes')

    def update_logs():
        parsed = _sber_parsed()
        _aktualizuj_statistiky(parsed)

        # Filtrování
        hledany = (stav_filtru['hledat'] or '').strip().lower()
        skupina = stav_filtru['skupina']
        povolene_sev = _FILTR_SKUPINY.get(skupina)

        filtr = []
        for cas, kat, urov, msg, ip, device in parsed:
            sev = _severity(urov, msg)
            if povolene_sev is not None and sev not in povolene_sev:
                continue
            if hledany:
                haystack = f'{cas} {kat} {urov} {msg} {ip} {device}'.lower()
                if hledany not in haystack:
                    continue
            filtr.append((cas, kat, urov, msg, ip, device, sev))

        log_container.clear()
        with log_container:
            if not filtr:
                with ui.element('div').classes('flex flex-col items-center justify-center h-48 gap-3'):
                    ui.icon('search_off' if (hledany or skupina != 'vse') else 'inbox', size='3rem', color='gray-300')
                    ui.label('Žádné odpovídající záznamy' if (hledany or skupina != 'vse') else 'Žádné záznamy') \
                        .classes('text-gray-400 text-sm')
                return

            for i, (cas, kat, urov, msg, ip, device, sev) in enumerate(filtr):
                dot_cls = _SEV_DOT.get(sev, _SEV_DOT['default'])
                msg_cls = _SEV_MSG.get(sev, _SEV_MSG['default'])
                kat_cls = _kat_color(kat) if kat else 'bg-gray-100 text-gray-500'
                row_bg  = 'bg-white' if i % 2 == 0 else 'bg-gray-50'
                _, urov_badge_cls = _SEVERITY_SCHEMA.get(sev, _SEVERITY_SCHEMA['default'])

                with ui.element('div').classes(
                    f'w-full grid items-start px-5 py-2 gap-2 {row_bg} '
                    f'border-b border-gray-100 hover:bg-blue-50/50 transition-colors duration-75'
                ).style(_aktualni_grid()):

                    # Čas + tečka závažnosti + relativní čas v tooltipu.
                    # V diagnostickém pohledu rozdělíme na sloupec Datum a Čas.
                    dt = _parse_cas(cas)
                    if stav_filtru['diagnostika']:
                        datum_str, _, cas_str = cas.partition(' ')
                        with ui.element('div').classes('flex items-center gap-2'):
                            ui.element('div').classes(f'w-2 h-2 rounded-full flex-shrink-0 {dot_cls}')
                            datum_lbl = ui.label(datum_str).classes('text-[11px] text-gray-400 font-mono whitespace-nowrap')
                            if dt:
                                datum_lbl.tooltip(_relativni_cas(dt))
                        ui.label(cas_str).classes('text-[11px] text-gray-500 font-mono whitespace-nowrap pt-0.5')
                    else:
                        with ui.element('div').classes('flex items-center gap-2'):
                            ui.element('div').classes(f'w-2 h-2 rounded-full flex-shrink-0 {dot_cls}')
                            cas_lbl = ui.label(cas).classes('text-[11px] text-gray-400 font-mono whitespace-nowrap')
                            if dt:
                                cas_lbl.tooltip(_relativni_cas(dt))

                    # Uživatel / kategorie jako badge
                    with ui.element('div').classes('flex items-start pt-0.5 min-w-0'):
                        if kat:
                            ui.label(kat).classes(
                                f'text-[10px] font-bold px-2 py-0.5 rounded-full {kat_cls} truncate max-w-full')

                    # Událost
                    with ui.element('div').classes('flex items-start pt-0.5'):
                        if urov:
                            ui.label(urov).classes(
                                f'text-[10px] font-bold px-1.5 py-0.5 rounded {urov_badge_cls} whitespace-nowrap')

                    # IP adresa (+ zařízení v tooltipu — jen v klasickém pohledu)
                    with ui.element('div').classes('flex items-center gap-1.5 pt-0.5 min-w-0'):
                        if ip:
                            ui.icon('public', size='13px', color='gray-400')
                            ip_lbl = ui.label(ip).classes('text-[11px] text-gray-500 font-mono truncate')
                            if device and not stav_filtru['diagnostika']:
                                ip_lbl.tooltip(device)
                        else:
                            ui.label('—').classes('text-[11px] text-gray-300')

                    # Prohlížeč + Systém — samostatné sloupce jen v diagnostickém pohledu
                    if stav_filtru['diagnostika']:
                        prohlizec_str, system_str = _rozdel_zarizeni(device)
                        # Prohlížeč (vč. verze)
                        with ui.element('div').classes('flex items-center gap-1.5 pt-0.5 min-w-0'):
                            if prohlizec_str:
                                ui.icon('web', size='13px', color='gray-400')
                                ui.label(prohlizec_str).classes('text-[11px] text-gray-500 truncate')
                            else:
                                ui.label('—').classes('text-[11px] text-gray-300')
                        # Systém
                        with ui.element('div').classes('flex items-center gap-1.5 pt-0.5 min-w-0'):
                            if system_str:
                                ui.icon('computer', size='13px', color='gray-400')
                                ui.label(system_str).classes('text-[11px] text-gray-500 truncate')
                            else:
                                ui.label('—').classes('text-[11px] text-gray-300')

                    # Zpráva
                    ui.label(msg).classes(
                        f'text-[11px] {msg_cls} break-words leading-snug min-w-0')

    # Vstupy filtru → překreslení (hodnoty začínající „/" jsou příkazy, nefiltrují)
    def _on_hledat():
        hodnota = hledat_input.value or ''
        if hodnota.strip().startswith('/'):
            return  # režim příkazu — neaplikovat jako filtr, čeká se na Enter
        stav_filtru['hledat'] = hodnota
        update_logs()
    hledat_input.on_value_change(_on_hledat)

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

    def _stav_modulu():
        nast = intranet_data.nacti_nastaveni_intranetu()
        with ui.dialog() as dlg, ui.card().classes('p-6 gap-2 max-w-lg'):
            ui.label('Stav modulů portálu').classes('text-lg font-bold mb-1')
            for klic, (popisek, _) in intranet_data.MODULY.items():
                zapnuty = nast.get(klic, True)
                with ui.row().classes('items-center gap-2 w-full'):
                    ui.icon('check_circle' if zapnuty else 'cancel', size='xs',
                            color='green-600' if zapnuty else 'red-600')
                    ui.label(klic.removesuffix('_zapnuty')).classes('text-xs font-mono w-40 text-gray-700')
                    ui.label(popisek).classes('text-xs text-gray-500 flex-1')
            ui.button('Zavřít', on_click=dlg.close).props('flat no-caps color=grey-8').classes('self-end mt-2')
        dlg.open()

    def _prepni_modul_prikazem(alias, zapnout):
        # Konzoli vidí i právo admin_logy, ale moduly patří pod „Nastavení portálu"
        # (právo mysql). Bez téhle kontroly by příkaz obešel oprávnění z UI.
        if 'vse' not in vsechna_prava and 'mysql' not in vsechna_prava:
            log_activity(user_name, 'Přepnutí modulu',
                         f'ODMÍTNUTO — příkaz /modul {alias} {"on" if zapnout else "off"} '
                         f'bez práva „Nastavení portálu"')
            ui.notify('Nemáte oprávnění měnit moduly portálu.', type='negative')
            return
        klic = f'{alias}_zapnuty'
        if klic not in intranet_data.MODULY:
            ui.notify(f'Neznámý modul: {alias}. Seznam vypíše /moduly.', type='negative')
            return
        import intranet_nastaveni  # lazy — intranet_nastaveni importuje tenhle modul
        intranet_nastaveni.prepni_modul(klic, zapnout, user_name)
        ui.notify(f'{intranet_data.MODULY[klic][0]} — {"ZAPNUTO" if zapnout else "VYPNUTO"}. '
                  'Změna se projeví všem uživatelům do 10 sekund.', type='positive')

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
        elif prikaz == '/moduly':
            _stav_modulu()
        elif (m := re.fullmatch(r'/modul\s+([\w-]+)\s+(on|off)', prikaz)):
            _prepni_modul_prikazem(m.group(1), m.group(2) == 'on')
        else:
            ui.notify(f'Neznámý příkaz: {prikaz}', type='negative')
    hledat_input.on('keydown.enter', _zpracuj_prikaz)

    update_logs()
    ui.timer(3.0, update_logs)
