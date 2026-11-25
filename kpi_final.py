# pip install pandas numpy xlsxwriter pyxlsb openpyxl
# Převzato z KPI_Final.py (Serhii). Aplikace ho spouští jako samostatný proces
# (intranet_kpi.py) — nikdy ho neimportovat, kód běží už při importu. Oproti
# originálu jen: cestu ke konfiguraci a k logu lze předat proměnnými KPI_CONFIG / KPI_LOG.
import configparser
import glob
import logging
import os
import re
import sys
import time
import unicodedata
from logging.handlers import RotatingFileHandler

t_start = time.perf_counter()

# ---------- лог: весь ход обработки -> logs/KPI_Final.log (ротация 2 МБ x 5) + консоль ----------
LOG_FILE = (os.environ.get("KPI_LOG")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "KPI_Final.log"))
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_handlers = [RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")]
if sys.stdout is not None:  # pythonw консоли не имеет
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", handlers=_handlers)
logging.captureWarnings(True)  # предупреждения pandas тоже в лог
log = logging.getLogger("KPI")
sys.excepthook = lambda *exc_info: log.critical("Neošetřená chyba — report nebyl vytvořen", exc_info=exc_info)


def fail(msg):
    """Ошибка входных данных: сообщение в лог, выход с кодом 1 без трейсбэка."""
    log.error(msg)
    return SystemExit(1)


log.info("===== Start: %s (Python %s) =====", os.path.abspath(__file__), sys.version.split()[0])

# импорт после настройки лога — отсутствующая библиотека тоже попадёт в лог
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import xlsxwriter  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from openpyxl.utils import column_index_from_string  # noqa: E402
from xlsxwriter.utility import xl_col_to_name  # noqa: E402

# ---------- cesty: KPI_Final.ini vedle skriptu; chybí/prázdná hodnota = výchozí ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("KPI_CONFIG") or os.path.join(SCRIPT_DIR, "KPI_Final.ini")
_cfg = configparser.ConfigParser(interpolation=None)  # bez interpolace, aby šlo psát %USERPROFILE%
try:
    _cfg.read(CONFIG_FILE, encoding="utf-8-sig")         # -sig: Poznámkový blok může uložit BOM
except configparser.Error as e:
    raise fail(f"Chyba v konfiguraci {CONFIG_FILE}: {e}")
_paths = _cfg["cesty"] if _cfg.has_section("cesty") else {}


def _resolve(p):
    """%PROMENNE% a ~ se rozbalí, relativní cesta je vůči složce skriptu."""
    return os.path.normpath(os.path.join(SCRIPT_DIR, os.path.expandvars(os.path.expanduser(p.strip()))))


def cfg_path(name, default):
    v = _paths.get(name, "").strip()
    return _resolve(v) if v else default


def cfg_dirs(name, default):
    """Víc složek oddělených středníkem."""
    v = _paths.get(name, "").strip()
    return tuple(_resolve(p) for p in v.split(";") if p.strip()) if v else default


log.info("Konfigurace: %s", CONFIG_FILE if os.path.isfile(CONFIG_FILE) else "nenalezena, výchozí cesty")
folder = cfg_path("data", SCRIPT_DIR)
out_path = cfg_path("vystup", os.path.join(folder, "souhrn_python.xlsx"))
karty_sheet = "Karty k 02.10.2026"   # если такого листа нет, будет взят первый

ENCODING = "cp1250"
HEADER_ROW = 4
ID_COLS = {"IČO", "Kód", "Číslo Obj.", "K2", "Klíč"}
OZNSIT_KEEP = {"benzina", "euro", "mol", "omv", "shell"}

KEEP_MODE = "contains"
K2_PATTERN = r"[09SW-]"
K2_EXACT = {"0", "9", "S", "-", "W"}
USE_IFERROR = False
NEW_COL = "Nedodáno dodavatel"
ND_TEXT = "nedodáno dodavatel"

# --- подтягивание "Nedodáno dodavatel" ---
ND_FILE_MASK = "Nedod*VO*dodavatel*.xlsx"
ND_SHEET_START = "4.data"
LK_KEY = 0
LK_RET = 2
ONLY_LOW_COVERAGE = True
KEY_STRIP_LEADING_ZEROS = False

# --- итоговая строка и разделение по OznSit ---
SPLIT_DIR = cfg_path("dodavatele", os.path.join(folder, "Dodavately"))
SPLIT_FILES = {"BENZINA": "KPI_Benzina", "EURO": "KPI_EUROOIL", "OMV": "KPI_OMV",
               "SHELL": "KPI_SHELL", "MOL": "KPI_MOL"}
TOTAL_PCT_AS_RATIO = True
SPLIT_DROP_FILES = {"SHELL", "MOL", "OMV", "EURO"}
DROP_COLS = ("A", "R", "S")
TOTAL_LABEL = "Celkem"

# === AUTOCHECK === конфиг
ND_MAX_NA_SHARE = 0.10          # максимум доля строк без Nedodáno (FAIL)
K2_MAX_NOT_FOUND_SHARE = 0.05   # максимум доля строк без K2 (WARN)
CS_MAX_MISSING_SHARE = 0.05     # Benzina: максимум доля строк без ČS (WARN)
SHELL_MAX_DROP_SHARE = 0.20     # SHELL: максимум доля строк, отсеянных из-за отсутствия SHELL (WARN)
SHELL_MAX_JMENO_NA_SHARE = 0.02 # SHELL: максимум доля строк без K Jméno 2 (WARN)
SUM_REL_TOL = 1e-9              # относительная погрешность при сверке сумм
# Klíč — вычисляемый (формула), в SLK его нет; Středisko в SLK без диакритики -> сравнение через plain()
REQUIRED_COLS = {"IČO", "Kód", "Číslo Obj.", "K2", "OznSit", "Popis",
                 "Středisko", "Požad.MJ", "Dodáno MJ"}
# Ключ — буква в ГОТОВОМ файле.
# A: Klíč (вычисляемый)
# R: Datum dod.
# S: Nedodáno dodavatel
DROP_EXPECT = {
    "A": None,                      # Klíč (формула) — в columns его нет
    "R": "Datum dod.",              # реальное имя из вашего готового файла
    "S": "Nedodáno dodavatel",      # реальное имя из вашего готового файла
}
AUTOCHECK = []                  # list of (name, value, status); status: OK / WARN / FAIL
STATS = {}                      # счётчики, собираемые по ходу работы
FILES = []                      # реестр созданных файлов для итоговых сверок
DROPPED_IDS = []                # индексы строк, осознанно не попавших ни в один файл (отсев по SHELL)


def add_check(name, value, status):
    AUTOCHECK.append((name, value, status))
    if status != "OK":
        (log.error if status == "FAIL" else log.warning)(f"[AUTOCHECK] {status}: {name}: {value}")


# --- KPI_SHELL: K Jméno 2 / SHELL ---
SHELL_KEY = "SHELL"                                   # для какого файла из SPLIT_FILES
KONTAKTY_MASK = "Kontaktn*daje*.xlsb"                # Kontaktní údaje ... .xlsb (берётся самый свежий по дате изменения)
KONTAKTY_DIRS = cfg_dirs("kontakty", (folder,))
KONTAKTY_SHEET_START = "Kontaktni udaje"             # лист 'Kontaktní údaje k ...', иначе первый
KONTAKTY_KEY, KONTAKTY_RET = 0, 3                    # $A:$D, 4-й столбец
SHELL_LIST_MASK = "SHELL na prota*.xlsx"             # SHELL na protažení výsledovkou.xlsx
SHELL_LIST_DIRS = cfg_dirs("shell_seznam", (os.path.dirname(folder), folder))  # výchozí: nadřazená složka dat; složka dat
SHELL_LIST_SHEET = "Sheet1"                          # если такого листа нет, будет взят первый
SHELL_SRC_LETTER = "C"                               # SVYHLEDAT(C2; ...) — буква в готовом файле KPI_SHELL
SHELL_INSERT_BEFORE = "Číslo Obj."
SHELL_COL_JMENO = "K Jméno 2"
SHELL_COL_SHELL = "SHELL"
SHELL_JMENO_NUMBER_ONLY = True                       # "8167 Pravá", "8144 od 1.4." -> "8167", "8144" (как в ручном файле)
SHELL_RENAME = {NEW_COL: "Poznámka"}

# --- KPI_BENZINA: ČS из "Kontaktní údaje - Změna OZ" ---
BENZ_KEY = "BENZINA"
BENZ_SUBDIR = "KPI_Benzina"                       # подпапка внутри SPLIT_DIR
BENZ_FILE_PREFIX = "KPI_Benzina"                  # KPI_Benzina_XX_YYYY.xlsx
BENZ_REF_MASK = "*Změna OZ*.xlsb"                 # надежная маска для файла "Kontaktní údaje - Změna OZ..."
BENZ_REF_DIRS = cfg_dirs("benzina_kontakty", KONTAKTY_DIRS)  # výchozí: stejně jako kontakty
BENZ_REF_SHEET_START = "Kontaktni"                # лист 'Kontaktní údaje ...', иначе первый
BENZ_IČO_COL = 0                                  # A: IČO (0-based)
BENZ_CS_COL = 3                                   # D: K.jméno 2 ("255 EDI") -> ČS (0-based)
BENZ_INSERT_BEFORE = "Číslo Obj."                 # столбец вставляется ПЕРЕД ним
BENZ_NEW_COL = "ČS"

# --- KPI_Benzina: Gastro (Ano / Ano 5% / Ne) по Kód из ceníků JIP BNA ---
GASTRO_COL = "Gastro"
GASTRO_INSERT_BEFORE = "Tabak"                    # Gastro встаёт сразу после Popis (перед Tabak)
GASTRO_DIRS = cfg_dirs("ceniky_gastro", (folder,))           # výchozí: složka dat
# (метка, маска файла) — порядок = порядок проверки; дата в названии не учитывается
GASTRO_FILES = (("CODO", "Cen*JIP BNA*CODO*.xlsx"),
                ("DOFO", "Cen*JIP BNA*DOFO*.xlsx"))
GASTRO_SHEET_LOG = "Logistika SC_CR"              # найден -> "Ano"
GASTRO_SHEET_OST = "Ostatní"                      # найден (если не нашёлся в Logistika) -> "Ano 5%"
GASTRO_KEY_COL = 0                                # $A:$A (0-based)
GASTRO_YES, GASTRO_YES5, GASTRO_NO = "Ano", "Ano 5%", "Ne"

# --- KPI_Benzina: разделение по Gastro на два файла ---
BENZ_KEEP_COMBINED = True                         # True = дополнительно оставлять общий KPI_Benzina_XX_YYYY.xlsx
BENZ_GASTRO_SPLIT = (                             # (суффикс файла, какие значения Gastro попадают в файл)
    ("Gastro", (GASTRO_YES, GASTRO_YES5)),        # KPI_Benzina_Gastro_XX_YYYY.xlsx : Ano и Ano 5%
    ("SHOP", (GASTRO_NO,)),                       # KPI_Benzina_SHOP_XX_YYYY.xlsx   : Ne
)


# ---------- вспомогательные функции ----------
def plain(s):
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


def key(v):
    if pd.isna(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def make_key(st, kod):
    k = key(kod)
    if KEY_STRIP_LEADING_ZEROS and k.isascii() and k.isdigit():
        k = str(int(k))
    return key(st)[:3] + k


def col_idx(cols, name):
    p = plain(name)
    for i, c in enumerate(cols):
        if plain(c) == p:
            return i
    raise fail(f"Sloupec '{name}' nenalezen. Dostupné: {cols}")


def find_latest(dirs, mask):
    found = [p for d in dirs for p in glob.glob(os.path.join(d, mask))
             if not os.path.basename(p).startswith("~$")]
    if not found:
        raise fail(f"Soubor podle masky {mask} nenalezen v: {', '.join(dirs)}")
    return max(found, key=os.path.getmtime)


def convert(v):
    if v.startswith('"'):
        return v[1:-1].replace('""', '"') if v.endswith('"') and len(v) > 1 else v[1:]
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def parse_slk(path):
    cells, x, y = {}, 1, 1
    with open(path, encoding=ENCODING, errors="replace") as f:
        for line in f:
            if not line.startswith("C;"):
                continue
            for p in line.rstrip("\r\n").replace(";;", "\x00").split(";")[1:]:
                if not p:
                    continue
                c, v = p[0], p[1:].replace("\x00", ";")
                if c == "X":
                    x = int(v)
                elif c == "Y":
                    y = int(v)
                elif c == "K":
                    cells[(y, x)] = convert(v)
    return cells


# ---------- 1. чтение SLK-файлов ----------
log.info("--- Krok 1. Čtení SLK souborů ---")
log.info(f"Složka s daty: {folder}; hlavní soubor: {out_path}; samostatné soubory: {SPLIT_DIR}")
if not os.path.isdir(folder):
    raise fail(f"Složka s daty neexistuje: {folder} — nastavte 'data' v {CONFIG_FILE}")
files = sorted(glob.glob(os.path.join(folder, "*.SLK")))
log.info(f"Nalezeno souborů: {len(files)}")
if not files:
    raise fail(f"SLK soubory nenalezeny v {folder}")

karty_candidates = glob.glob(os.path.join(folder, "Karty k*.xlsb"))
if not karty_candidates:
    raise fail(f"Soubor Karty k*.xlsb nenalezen v {folder}")
karty_path = max(karty_candidates, key=os.path.getmtime)
log.info(f"Karty: {karty_path}")

nd_candidates = [p for p in glob.glob(os.path.join(folder, ND_FILE_MASK))
                 if not os.path.basename(p).startswith("~$")]
if not nd_candidates:
    raise fail(f"Soubor podle masky {ND_FILE_MASK} nenalezen v {folder}")
nd_path = max(nd_candidates, key=os.path.getmtime)
log.info(f"Nedodávky: {nd_path}")

if SHELL_KEY in SPLIT_FILES:
    kontakty_path = find_latest(KONTAKTY_DIRS, KONTAKTY_MASK)
    shell_list_path = find_latest(SHELL_LIST_DIRS, SHELL_LIST_MASK)
    log.info(f"Kontaktní údaje: {kontakty_path}")
    log.info(f"SHELL seznam: {shell_list_path}")

frames = []
columns, n = None, None
for i, path in enumerate(files):
    t0 = time.perf_counter()
    cells = parse_slk(path)

    if i == 0:
        n = max(x for (y, x) in cells if y == HEADER_ROW)
        columns = [str(cells.get((HEADER_ROW, x), f"col{x}")).strip() for x in range(1, n + 1)]

    rows = {}
    for (y, x), v in cells.items():
        if y > HEADER_ROW and x <= n:
            rows.setdefault(y, [None] * n)[x - 1] = v
    data = [rows[y] for y in sorted(rows)]
    frames.append(pd.DataFrame(data, columns=columns))
    log.info(f"{os.path.basename(path)}: {len(data)} řádků, {time.perf_counter() - t0:.2f} s")

result = pd.concat(frames, ignore_index=True).dropna(how="all")
log.info(f"Načtení všech souborů: {time.perf_counter() - t_start:.2f} s (celkem řádků: {len(result)})")

# ---------- 2. удаляем столбцы "Z toho akce" ----------
log.info('--- Krok 2. Odstranění sloupců "Z toho akce" ---')
keep = [i for i, c in enumerate(columns) if not plain(c).startswith("z toho ak")]
dropped = [columns[i] for i in range(len(columns)) if i not in keep]
result = result.iloc[:, keep]
columns = list(result.columns)
log.info(f"Odstraněné sloupce: {dropped}")

# === AUTOCHECK === обязательные заголовки (сравнение через plain(), чтобы не зависеть от диакритики/регистра)
# Это единственная проверка, которая останавливает скрипт: без этих столбцов дальше работать нельзя.
_columns_plain = {plain(c) for c in columns}
_missing = {c for c in REQUIRED_COLS if plain(c) not in _columns_plain}
if _missing:
    raise fail(
        f"[AUTOCHECK] V SLK chybí očekávané sloupce: {_missing}. "
        f"Dostupné: {columns}"
    )
AUTOCHECK.append(("Povinné záhlaví", "všechna jsou přítomna", "OK"))

# ---------- 3. фильтр по OznSit ----------
log.info("--- Krok 3. Filtr podle OznSit ---")
oz_i = col_idx(columns, "OznSit")
mask = result.iloc[:, oz_i].astype(str).str.strip().str.lower().isin(OZNSIT_KEEP)
before = len(result)
result = result[mask].reset_index(drop=True)
log.info(f"Filtr OznSit: {before} -> {len(result)} řádků")
log.info("OznSit podle hodnot:\n%s", result.iloc[:, oz_i].value_counts().to_string())

# ---------- типы: числа -> числа ----------
log.info("--- Krok: číselné sloupce -> čísla ---")
for i, name in enumerate(columns):
    s = result.iloc[:, i]
    nn = s.dropna()
    if len(nn) and nn.map(lambda v: isinstance(v, (int, float))).all():
        s = pd.to_numeric(s)
        result.isetitem(i, s)

# ---------- 4. подтягиваем K2 из Karty ----------
log.info("--- Krok 4. Doplnění K2 z Karty ---")
t_k = time.perf_counter()
try:
    karty = pd.read_excel(karty_path, engine="pyxlsb", sheet_name=karty_sheet, header=None)
except ValueError:
    karty = pd.read_excel(karty_path, engine="pyxlsb", sheet_name=0, header=None)
if karty.shape[1] < 9:
    raise fail(f"Karty má méně než 9 sloupců ({karty.shape[1]}), pro Tabak je potřeba sloupec I")
karty = karty.iloc[:, :9].iloc[::-1]
karty_keys = karty.iloc[:, 0].map(key)
lookup = dict(zip(karty_keys, karty.iloc[:, 3]))
lookup_tabak = dict(zip(karty_keys, karty.iloc[:, 8]))

kod_i = col_idx(columns, "Kód")
k2_i = col_idx(columns, "K2")
new_k2 = result.iloc[:, kod_i].map(key).map(lookup)
not_found = int(new_k2.isna().sum())
result.isetitem(k2_i, new_k2)
log.info(f"K2 doplněno z Karty za {time.perf_counter() - t_k:.2f} s, nenalezeno: {not_found} řádků")

# === AUTOCHECK === доля не найденных K2
_k2_share = not_found / max(len(result), 1)
_status = "OK" if _k2_share <= K2_MAX_NOT_FOUND_SHARE else "WARN"
AUTOCHECK.append(("K2 nenalezeno, %", f"{_k2_share:.1%} (limit {K2_MAX_NOT_FOUND_SHARE:.0%})", _status))
if _status != "OK":
    log.warning(f"[AUTOCHECK] WARN: K2 nenalezeno u {_k2_share:.1%} řádků – nad limitem, zkontrolujte Karty/Kód")

pop_i = col_idx(columns, "Popis")
st_i = col_idx(columns, "Středisko")
assert k2_i == 8 and pop_i == 7, f"Neočekávané pořadí sloupců: K2={k2_i}, Popis={pop_i}"
log.info("Sloupce I..P: %s", ["Popis"] + columns[8:15])


def num(i):
    return pd.to_numeric(result.iloc[:, i], errors="coerce")


def copy_rows(m):
    for dst, src in ((9, 10), (11, 12)):
        d, s = num(dst), num(src)
        result.isetitem(dst, d.where(~m, s))
        result.isetitem(src, s)


def vykryti(mi=11, ni=12):
    M, N = num(mi).fillna(0), num(ni).fillna(0)
    err = M == 0
    return N / M.where(~err), err


# ---------- 5. фильтр по K2 ----------
log.info("--- Krok 5. Filtr podle K2 ---")
k2s = result.iloc[:, k2_i].map(key).str.upper()
if KEEP_MODE == "contains":
    m = k2s.str.contains(K2_PATTERN, regex=True)
else:
    m = k2s.isin(K2_EXACT)
log.info(f"Řádků odpovídajících filtru K2: {int(m.sum())} z {len(result)}")
copy_rows(m)

# ---------- 6. Popis начинается с "*" ----------
log.info('--- Krok 6. Popis začíná "*" ---')
star = result.iloc[:, pop_i].map(key).str.startswith("*")
for i in (11, 12):
    result.isetitem(i, num(i).mask(star, 0.01))
log.info(f"Popis začíná '*': {int(star.sum())} řádků -> M a N = 0,01")

# ---------- 7. %vykrytí < 0 или > 1 ----------
log.info("--- Krok 7. %vykrytí < 0 nebo > 1 ---")
r, err = vykryti()
m = (~err) & ((r < 0) | (r > 1))
log.info(f"%vykrytí <0 nebo >1: {int(m.sum())} řádků")
copy_rows(m)

# ---------- 8. %vykrytí = #DĚLENÍ NULOU! ----------
log.info("--- Krok 8. %vykrytí = #DĚLENÍ NULOU! ---")
r, err = vykryti()
log.info(f"%vykrytí = #DĚLENÍ NULOU!: {int(err.sum())} řádků")
copy_rows(err)

# ---------- 9. оставшиеся #DĚLENÍ NULOU! -> удалить строки ----------
log.info("--- Krok 9. Zbylé #DĚLENÍ NULOU! -> smazání řádků ---")
r, err = vykryti()
before = len(result)
result = result[~err].reset_index(drop=True)
log.info(f"Smazané řádky s #DĚLENÍ NULOU!: {before - len(result)}, zbývá {len(result)}")
# ВАЖНО: дальше result имеет RangeIndex 0..N-1 — на этом строятся сверки по индексам (AUTOCHECK)
assert result.index.equals(pd.RangeIndex(len(result)))

# ---------- 10. столбец Tabak ----------
log.info("--- Krok 10. Sloupec Tabak ---")
kod_str = result.iloc[:, kod_i].map(key)
tabak = kod_str.map(lookup_tabak)
log.info(f"Tabak: nenalezeno v Karty: {int(tabak.isna().sum())} řádků")
tabak = tabak.astype(object)
tabak[kod_str.str.startswith("5")] = "ANO"
tabak[tabak != "ANO"] = "NE"
log.info(f"Tabak: ANO = {int((tabak == 'ANO').sum())}, NE = {int((tabak == 'NE').sum())}")
result.insert(pop_i + 1, "Tabak", tabak)

# ---------- 11. столбец "Nedodáno dodavatel" ----------
log.info('--- Krok 11. Sloupec "Nedodáno dodavatel" ---')
result[NEW_COL] = None
columns = list(result.columns)
PK, DK, VY, ND = 12, 13, 14, 15
log.info("Kontrola sloupců: %s | poslední: %s", [columns[i] for i in (PK, DK, VY, ND)], columns[-3:])

# ---------- 12. подтягивание данных для Nedodáno dodavatel ----------
log.info("--- Krok 12. Doplnění dat pro Nedodáno dodavatel ---")
t_nd = time.perf_counter()
xl = pd.ExcelFile(nd_path)
sheet = next((s for s in xl.sheet_names if plain(s).startswith(plain(ND_SHEET_START))), None)
if sheet is None:
    raise fail(f"List '{ND_SHEET_START}...' nenalezen. Dostupné listy: {xl.sheet_names}")
ref = xl.parse(sheet, header=None)
if ref.shape[1] <= max(LK_KEY, LK_RET):
    raise fail(f"List má {ref.shape[1]} sloupců, ale LK_KEY={LK_KEY}, LK_RET={LK_RET}")
ref = ref.iloc[::-1]
ref_vals = ref.iloc[:, LK_RET].fillna(0)
lk = dict(zip(ref.iloc[:, LK_KEY].map(key), ref_vals))

r, err = vykryti(PK, DK)
low = (r < 0.9).values
log.info(f"%vykrytí < 0,9: {int(low.sum())} z {len(result)} řádků")

row_keys = pd.Series([make_key(s, k) for s, k in zip(result.iloc[:, st_i], result.iloc[:, kod_i])], index=result.index)
found = row_keys.isin(lk.keys()).values
vals = row_keys.map(lk).astype(object)
scope = low if ONLY_LOW_COVERAGE else np.ones(len(result), dtype=bool)
vals[~scope] = None
notfound = scope & ~found
log.info(f"Nedodáno dodavatel: nalezeno {int((scope & found).sum())}, nenalezeno (#N/A): {int(notfound.sum())}")

# === AUTOCHECK === доля #N/A в Nedodáno (теперь не обрывает скрипт, а фиксируется в Kontrola)
_na_share = float(notfound.sum()) / max(len(result), 1)
add_check("Nedodáno #N/A, %",
          f"{int(notfound.sum())} z {len(result)} = {_na_share:.1%} (limit {ND_MAX_NA_SHARE:.0%})",
          "OK" if _na_share <= ND_MAX_NA_SHARE else "FAIL")

neg = (pd.to_numeric(vals, errors="coerce") < 0).values
cleared = int((scope & found & ~neg).sum()) + int(notfound.sum())
final = pd.Series([None] * len(result), dtype=object)
final[neg] = ND_TEXT
result[NEW_COL] = final.values
log.info(f"Nahrazeno na '{ND_TEXT}': {int(neg.sum())}, vymazáno jiných hodnot: {cleared} "
      f"(řádky se nemažou, celkem {len(result)}; {time.perf_counter() - t_nd:.2f} s)")

# === AUTOCHECK === дубли в источнике: Středisko + Kód + Číslo Obj. (только при непустом Číslo Obj.)
# Пустой Číslo Obj. — нормальная ситуация (не заказ), такие строки не считаются.
_obj_c = col_idx(columns, "Číslo Obj.")
_obj = result.iloc[:, _obj_c].map(key)
_has_obj = (_obj != "").values
_dup_key = (result.iloc[:, st_i].map(key) + "|" + result.iloc[:, kod_i].map(key) + "|" + _obj)
_dup_rows = int(_dup_key[_has_obj].duplicated(keep=False).sum())
add_check("Duplicity ve zdroji (Středisko+Kód+Číslo Obj.)",
          f"{_dup_rows} řádků (řádky s prázdným Číslo Obj. se nepočítají: {int((~_has_obj).sum())})",
          "OK" if _dup_rows == 0 else "WARN")

# ---------- 13. запись + форматирование ----------
log.info("--- Krok 13. Zápis + formátování ---")
t_write = time.perf_counter()
col_st = xl_col_to_name(st_i + 1)
col_kod = xl_col_to_name(kod_i + 1)
FORMULA_KLIC = not KEY_STRIP_LEADING_ZEROS
oz_i = col_idx(columns, "OznSit")
SUM_IDX = [col_idx(columns, "Požad.MJ"), col_idx(columns, "Dodáno MJ"), PK, DK, ND]


def letter_to_idx(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def sheet_col_name(c):
    return "Klíč" if c == 0 else columns[c - 1]


def out_col_index(letter, drop=()):
    """Индекс в columns того столбца, который в готовом файле (после удаления drop) стоит на букве letter."""
    drop_idx = {letter_to_idx(x) for x in drop}
    kept = [c for c in range(len(columns) + 1) if c not in drop_idx]
    c = kept[letter_to_idx(letter)]
    if c == 0:
        raise fail(f"Sloupec {letter} je ve výsledném souboru Klíč (vzorec), jako klíč se nehodí")
    return c - 1


# расчёт типов колонок и ширины
col_kind, col_width = {}, {}
for i, name in enumerate(columns):
    s = result.iloc[:, i]
    col_width[i] = max(min(max(s.astype(str).str.len().max() if len(s) else 8, len(name)) + 2, 45), 10)
    kind = None
    if i in SUM_IDX:
        kind = "num0"         # целые числа с разделителем тысяч
    elif i == VY:
        kind = "ratio2"       # проценты
    elif name == NEW_COL:
        kind = "num0"         # Nedodáno целое с разделителем
    elif name.lower().startswith("datum"):
        kind = "date"
    elif pd.api.types.is_numeric_dtype(s) and name not in ID_COLS:
        nn = s.dropna()
        kind = "num0" if (nn % 1 == 0).all() else "dec"
    col_kind[i] = kind


def _add_kontrola_sheet(wb, checks):
    """Лист Kontrola в той же книге (ничего не затирает)."""
    ws = wb.add_worksheet("Kontrola")
    hdr = wb.add_format({'bold': True, 'border': 1})
    fmt = {
        "OK":   wb.add_format({'bg_color': '#C6EFCE', 'border': 1}),
        "WARN": wb.add_format({'bg_color': '#FFEB9C', 'border': 1}),
        "FAIL": wb.add_format({'bg_color': '#FFC7CE', 'border': 1}),
    }
    ws.write(0, 0, "Kontrola", hdr)
    ws.write(0, 1, "Hodnota", hdr)
    ws.write(0, 2, "Stav", hdr)
    for r_, (name, value, status) in enumerate(checks, start=1):
        ws.write(r_, 0, name, fmt[status])
        ws.write(r_, 1, str(value), fmt[status])
        ws.write(r_, 2, status, fmt[status])
    ws.set_column(0, 0, 50)
    ws.set_column(1, 1, 80)
    ws.set_column(2, 2, 10)


def write_book(df, path, drop=(), total_label=None, extra_cols=(), extra_before=None, rename=None,
               full_border_cols=(), num_cols=(), kontrola=None):
    """extra_cols: [(название, список значений)] — вставляются (только значения) перед столбцом extra_before.
    rename: {старое название: новое} — меняется только заголовок в файле.
    full_border_cols: столбцы, где рамка и текстовый формат ставятся во все ячейки, включая пустые.
    num_cols: столбцы, которые нужно писать числовым форматом (без зеленых треугольников).
    kontrola: список проверок -> добавляется лист "Kontrola" в эту же книгу.
    Возвращает {заголовок: буква столбца} для контроля."""
    n = len(df)
    last = n + 1
    tot_xl = last + 1
    tot0 = last
    rename = rename or {}

    # === AUTOCHECK === DROP_COLS должны указывать на ожидаемые имена (ошибка конфигурации — останавливаем)
    for _letter, _expected in DROP_EXPECT.items():
        if _letter not in drop:
            continue
        if _expected is None:
            continue
        try:
            _idx = out_col_index(_letter, drop)
        except SystemExit:
            log.error(f"[AUTOCHECK] DROP_COLS: chybná konfigurace sloupce {_letter}")
            raise
        _actual = columns[_idx]
        if plain(_actual) != plain(_expected):
            raise fail(
                f"[AUTOCHECK] DROP_COLS: {_letter} = '{_actual}', očekáváno '{_expected}'. "
                f"Zkontrolujte DROP_EXPECT."
            )

    drop_idx = {letter_to_idx(x) for x in drop}
    keep = [c for c in range(len(columns) + 1) if c not in drop_idx]
    pos = {c: j for j, c in enumerate(keep)}
    for need in [PK + 1, DK + 1, VY + 1, ND + 1] + [i + 1 for i in SUM_IDX]:
        assert need in pos, f"sloupec {sheet_col_name(need)} nelze odstranit: je potřeba pro součty/vzorce"

    # --- вставка дополнительных столбцов: всё, что правее точки вставки, сдвигается ---
    extra_pos = []
    for ec in extra_cols:
        before = ec[2] if len(ec) > 2 else extra_before
        b = col_idx(columns, before) + 1
        assert b in pos, f"sloupec {before} je odstraněn — není kam vložit nový sloupec"
        ins = pos[b]
        pos = {c: (j + 1 if j >= ins else j) for c, j in pos.items()}
        extra_pos = [q + 1 if q >= ins else q for q in extra_pos]
        extra_pos.append(ins)
    ncols = len(keep) + len(extra_cols)
    Ln = lambda c: xl_col_to_name(pos[c])

    mv = pd.to_numeric(df.iloc[:, PK], errors="coerce").fillna(0)
    nv = pd.to_numeric(df.iloc[:, DK], errors="coerce").fillna(0)

    with xlsxwriter.Workbook(path) as wb:
        ws = wb.add_worksheet("Souhrn")

        border_opts = {'border': 1, 'border_color': '#000000'}
        base_bold = {'bold': True, **border_opts}

        hdr = wb.add_format(base_bold)
        f_tot = {
            "blank": wb.add_format(base_bold),
            "num0": wb.add_format({**base_bold, "num_format": "#,##0"}),
            "ratio2": wb.add_format({**base_bold, "num_format": "0.00%"})
        }

        f_int = wb.add_format({"num_format": "#,##0", **border_opts})
        f_dec = wb.add_format({"num_format": "#,##0.00", **border_opts})
        f_num0 = wb.add_format({"num_format": "#,##0", **border_opts})
        f_ratio2 = wb.add_format({"num_format": "0.00%", **border_opts})
        f_date = wb.add_format({"num_format": "dd.mm.yyyy", "align": "center", **border_opts})
        f_text = wb.add_format(border_opts)
        f_extra_num = wb.add_format({"num_format": "0", "align": "left", **border_opts})

        fmt_of = {"int": f_int, "dec": f_dec, "num0": f_num0, "ratio2": f_ratio2, "date": f_date, None: f_text}

        def put(row, col, val, fmt):
            if pd.notna(val):
                if isinstance(val, (int, float, np.number)):
                    ws.write_number(row, col, val, fmt)
                else:
                    ws.write_string(row, col, str(val), fmt)

        for c in keep:
            ws.set_column(pos[c], pos[c], col_width[0 if c == 0 else c - 1])

        if 0 in pos:
            ws.write(0, pos[0], "Klíč", hdr)

        for i, name in enumerate(columns):
            if (i + 1) in pos:
                ws.write(0, pos[i + 1], rename.get(name, name), hdr)

        if 0 in pos:
            kst, kkod = Ln(st_i + 1), Ln(kod_i + 1)
            key_vals = [make_key(s, k) for s, k in zip(df.iloc[:, st_i], df.iloc[:, kod_i])]
            for rr, v in enumerate(key_vals, start=1):
                if FORMULA_KLIC:
                    ws.write_formula(rr, pos[0], f"=CONCATENATE(LEFT({kst}{rr + 1},3),{kkod}{rr + 1})", f_text, v)
                else:
                    ws.write_string(rr, pos[0], v, f_text)

        for i, name in enumerate(columns):
            if (i + 1) in pos:
                full = name in full_border_cols
                col_fmt = f_text if full else fmt_of[col_kind[i]]
                p = pos[i + 1]
                for rr, val in enumerate(df.iloc[:, i].tolist(), start=1):
                    if full and pd.isna(val):
                        ws.write_blank(rr, p, None, col_fmt)
                    else:
                        put(rr, p, val, col_fmt)

        for ec, p in zip(extra_cols, extra_pos):
            name, values = ec[0], ec[1]
            ws.write(0, p, name, hdr)
            lens = [len(str(v)) for v in values if pd.notna(v)]
            ws.set_column(p, p, max(min(max(max(lens, default=0), len(name)) + 2, 45), 10))
            for rr, val in enumerate(values, start=1):
                if pd.isna(val):
                    ws.write_blank(rr, p, None, f_text)
                elif isinstance(val, (int, float, np.number)):
                    ws.write_number(rr, p, val, f_extra_num)
                else:
                    ws.write_string(rr, p, str(val), f_text)

        L_pk, L_dk = Ln(PK + 1), Ln(DK + 1)
        ratio = (nv / mv.where(mv != 0)).fillna(0).tolist()
        diff = (nv - mv).tolist()
        for rr in range(1, n + 1):
            x = rr + 1
            f_o = f"=IFERROR({L_dk}{x}/{L_pk}{x},0)" if USE_IFERROR else f"={L_dk}{x}/{L_pk}{x}"
            ws.write_formula(rr, pos[VY + 1], f_o, f_ratio2, ratio[rr - 1])
            ws.write_formula(rr, pos[ND + 1], f"={L_dk}{x}-{L_pk}{x}", f_num0, diff[rr - 1])

        for c in keep:
            ws.write_blank(tot0, pos[c], None, f_tot["blank"])
        for p in extra_pos:
            ws.write_blank(tot0, p, None, f_tot["blank"])
        if total_label:
            ws.write_string(tot0, 0, total_label, f_tot["blank"])

        for i in SUM_IDX:
            Lc = Ln(i + 1)
            cached = (float(nv.sum() - mv.sum()) if i == ND else float(pd.to_numeric(df.iloc[:, i], errors="coerce").sum()))
            ws.write_formula(tot0, pos[i + 1], f"=SUBTOTAL(9,{Lc}2:{Lc}{last})", f_tot["num0"], cached)

        if TOTAL_PCT_AS_RATIO:
            f_vy = f"=IFERROR({L_dk}{tot_xl}/{L_pk}{tot_xl},0)"
            cached = float(nv.sum() / mv.sum()) if mv.sum() else 0.0
            ws.write_formula(tot0, pos[VY + 1], f_vy, f_tot["ratio2"], cached)
        else:
            ws.write_formula(tot0, pos[VY + 1], f"={L_dk}{tot_xl}-{L_pk}{tot_xl}", f_tot["num0"], float(nv.sum() - mv.sum()))

        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, n, ncols - 1)

        if kontrola is not None:
            _add_kontrola_sheet(wb, kontrola)

    letters = {("Klíč" if c == 0 else rename.get(columns[c - 1], columns[c - 1])): xl_col_to_name(p)
               for c, p in pos.items()}
    letters.update({ec[0]: xl_col_to_name(p) for ec, p in zip(extra_cols, extra_pos)})
    return letters


# ---------- 13a. (основной файл пишется В КОНЦЕ, вместе с листом Kontrola) ----------


# ---------- 13b. справочники для KPI_SHELL ----------
def load_shell_refs():
    t0 = time.perf_counter()
    xk = pd.ExcelFile(kontakty_path, engine="pyxlsb")
    ks = next((s for s in xk.sheet_names if plain(s).startswith(plain(KONTAKTY_SHEET_START))), xk.sheet_names[0])
    kont = xk.parse(ks, header=None)
    if kont.shape[1] <= max(KONTAKTY_KEY, KONTAKTY_RET):
        raise fail(f"List '{ks}' má jen {kont.shape[1]} sloupců, je potřeba sloupec D")
    kont = kont.iloc[::-1]
    kont_map = dict(zip(kont.iloc[:, KONTAKTY_KEY].map(key), kont.iloc[:, KONTAKTY_RET]))
    kont_map.pop("", None)

    xs = pd.ExcelFile(shell_list_path)
    ss = SHELL_LIST_SHEET if SHELL_LIST_SHEET in xs.sheet_names else xs.sheet_names[0]
    sl = xs.parse(ss, header=None).iloc[::-1, 0]
    shell_map = {key(v).casefold(): v for v in sl if key(v)}
    log.info(f"Číselníky SHELL: Kontaktní údaje ('{ks}') {len(kont_map)} klíčů, "
          f"SHELL seznam ('{ss}') {len(shell_map)} hodnot, {time.perf_counter() - t0:.2f} s")
    return kont_map, shell_map


def jmeno_number(v):
    if pd.isna(v):
        return v
    m = re.match(r"\s*(\d+)(?!\d)", key(v))
    return m.group(1) if m else v


def prepare_shell(part, drop, refs):
    """Возвращает (part без строк без SHELL, extra, ok) — ok = булева маска оставленных строк исходного part."""
    kont_map, shell_map = refs
    src_i = out_col_index(SHELL_SRC_LETTER, drop)
    log.info(f"{SHELL_KEY}: klíč pro K Jméno 2 — sloupec {SHELL_SRC_LETTER} = '{columns[src_i]}'")

    jmeno = part.iloc[:, src_i].map(key).map(kont_map)
    if SHELL_JMENO_NUMBER_ONLY:
        raw = jmeno.copy()
        jmeno = jmeno.map(jmeno_number)
        fixed = raw.notna() & (raw.map(key) != jmeno.map(key))
        if fixed.any():
            pairs = sorted({(key(a), key(b)) for a, b in zip(raw[fixed], jmeno[fixed])})
            log.info(f"{SHELL_KEY}: K Jméno 2 zkráceno na číslo v {int(fixed.sum())} řádcích: "
                  + ", ".join(f"'{a}' -> '{b}'" for a, b in pairs))
    shell = jmeno.map(lambda v: shell_map.get(key(v).casefold()))
    ok = shell.notna().values
    STATS["shell_jmeno_na"] = int(jmeno.isna().sum())
    log.info(f"{SHELL_KEY}: K Jméno 2 nenalezeno (#N/A): {int(jmeno.isna().sum())}; "
          f"SHELL nedoplněn: {int((~ok).sum())} -> řádky smazány, zbývá {int(ok.sum())} z {len(part)}")

    part = part[ok].reset_index(drop=True)
    extra = [(SHELL_COL_JMENO, jmeno[ok].tolist()), (SHELL_COL_SHELL, shell[ok].tolist())]
    return part, dict(extra_cols=extra, extra_before=SHELL_INSERT_BEFORE, rename=SHELL_RENAME,
                      full_border_cols=(NEW_COL,)), ok


# ---------- 13c. справочник для KPI_Benzina ----------
def norm_ico(v):
    return key(v).upper().replace(" ", "").replace("\xa0", "")


def cs_clean(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    s = key(v)
    if not s:
        return None
    m = re.match(r"^\s*(\d+)\s+([^\W\d_]+)", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m = re.match(r"^\s*(\d+)(?!\d)", s)
    if m:
        return m.group(1)
    return s


def load_benz_refs():
    t0 = time.perf_counter()
    path = find_latest(BENZ_REF_DIRS, BENZ_REF_MASK)
    xb = pd.ExcelFile(path, engine="pyxlsb")
    sh = next((s for s in xb.sheet_names if plain(s).startswith(plain(BENZ_REF_SHEET_START))),
              xb.sheet_names[0])
    ref = xb.parse(sh, header=None)
    need = max(BENZ_IČO_COL, BENZ_CS_COL)
    if ref.shape[1] <= need:
        raise fail(f"List '{sh}' má jen {ref.shape[1]} sloupců, potřeba je alespoň {need + 1}")

    exact, loose = {}, {}
    for i, c in zip(ref.iloc[:, BENZ_IČO_COL], ref.iloc[:, BENZ_CS_COL]):
        k = norm_ico(i)
        if not k or k == norm_ico("IČO"):
            continue
        exact.setdefault(k, c)
        loose.setdefault(k.lstrip("0"), c)

    log.info(f"BENZINA: číselník '{os.path.basename(path)}' ('{sh}') – {len(exact)} IČO, "
             f"{time.perf_counter() - t0:.2f} s")
    log.info(f"BENZINA: ukázka klíčů číselníku: {list(exact.items())[:5]}")
    return exact, loose


def read_key_set(xf, sheet_name):
    sh = next((x for x in xf.sheet_names if plain(x) == plain(sheet_name)), None)
    if sh is None:
        sh = next((x for x in xf.sheet_names if plain(x).startswith(plain(sheet_name))), None)
    if sh is None:
        raise fail(f"List '{sheet_name}' nenalezen. Dostupné listy: {xf.sheet_names}")
    col = xf.parse(sh, header=None, usecols=[GASTRO_KEY_COL], dtype=object).iloc[:, 0]
    return {key(v) for v in col if key(v)}, sh


def load_gastro_refs():
    t0 = time.perf_counter()
    refs = []
    for tag, mask in GASTRO_FILES:
        path = find_latest(GASTRO_DIRS, mask)
        xf = pd.ExcelFile(path)
        kody_log, s1 = read_key_set(xf, GASTRO_SHEET_LOG)
        ost, s2 = read_key_set(xf, GASTRO_SHEET_OST)
        log.info(f"GASTRO {tag}: '{os.path.basename(path)}' – '{s1}': {len(kody_log)} kódů, "
                 f"'{s2}': {len(ost)} kódů; ukázka: {sorted(kody_log)[:3]}")
        refs.append((tag, kody_log, ost))
    log.info(f"GASTRO: číselníky načteny za {time.perf_counter() - t0:.2f} s")
    return refs


def make_gastro(kods, refs):
    res = pd.Series([None] * len(kods), index=kods.index, dtype=object)
    for tag, kody_log, ost in refs:
        m1 = res.isna() & kods.isin(kody_log)
        res[m1] = GASTRO_YES
        m2 = res.isna() & kods.isin(ost)
        res[m2] = GASTRO_YES5
        log.info(f"GASTRO {tag}: '{GASTRO_YES}' +{int(m1.sum())}, '{GASTRO_YES5}' +{int(m2.sum())}")
        if m1.sum() + m2.sum() == 0:
            log.warning(f"GASTRO {tag}: žádná shoda – zkontrolujte GASTRO_KEY_COL a formát kódů")
    res[res.isna()] = GASTRO_NO
    log.info(
        f"GASTRO celkem: {GASTRO_YES} = {int((res == GASTRO_YES).sum())}, "
        f"{GASTRO_YES5} = {int((res == GASTRO_YES5).sum())}, {GASTRO_NO} = {int((res == GASTRO_NO).sum())}")
    return res


def subset_extra(extra, mask):
    idx = np.flatnonzero(np.asarray(mask))
    cols = [(ec[0], [ec[1][i] for i in idx]) + tuple(ec[2:]) for ec in extra["extra_cols"]]
    return {**extra, "extra_cols": cols}


def prepare_benz(part, refs, gastro_refs):
    exact, loose = refs
    ico_i = col_idx(columns, "IČO")
    icos = part.iloc[:, ico_i].map(norm_ico)

    def look(k):
        if k in exact:
            return exact[k]
        return loose.get(k.lstrip("0"))

    raw = icos.map(look)
    cs = raw.map(cs_clean).astype(object)

    miss = int(cs.isna().sum())
    STATS["benz_n"] = len(part)
    STATS["benz_cs_miss"] = miss
    log.info(f"{BENZ_KEY}: ČS nenalezeno/prázdné: {miss} z {len(part)}")
    if miss:
        bad = sorted(set(icos[cs.isna()]))[:10]
        log.info(f"{BENZ_KEY}: ukázka IČO bez ČS: {bad}")
    log.info(f"{BENZ_KEY}: ukázka nalezených ČS: {cs.dropna().unique()[:5].tolist()}")

    gastro = make_gastro(part.iloc[:, kod_i].map(key), gastro_refs)
    STATS["gastro_counts"] = {str(k): int(v) for k, v in gastro.value_counts(dropna=False).items()}

    extra = [(BENZ_NEW_COL, [None if pd.isna(v) else v for v in cs]),
             (GASTRO_COL, gastro.tolist(), GASTRO_INSERT_BEFORE)]
    return part, dict(extra_cols=extra, extra_before=BENZ_INSERT_BEFORE)


def register(label, path, part, ids, letters, role):
    """role: 'primary' — файл входит в разбиение; 'mirror' — дублирующий общий файл (Benzina combined)."""
    FILES.append(dict(label=label, path=path, df=part, ids=np.asarray(ids, dtype=int),
                      letters=letters, role=role, group=label.split("/")[0]))


# ---------- 14. отдельные файлы по значениям столбца C ----------
log.info("--- Krok 14. Samostatné soubory podle hodnot sloupce C ---")
os.makedirs(SPLIT_DIR, exist_ok=True)
oz_vals = result.iloc[:, oz_i].astype(str).str.strip().str.upper()

today = pd.Timestamp.today()
stamp = f"{today.month:02d}_{today.year}"

for oz, fname in SPLIT_FILES.items():
    t_f = time.perf_counter()
    mask_oz = (oz_vals == oz).values
    part = result[mask_oz].reset_index(drop=True)
    ids = result.index[mask_oz].to_numpy()          # индексы строк в result (для сверок)
    if part.empty:
        log.warning(f"{oz}: žádné řádky, soubor {fname}.xlsx nebyl vytvořen")
        continue

    drop = DROP_COLS if oz in SPLIT_DROP_FILES else ()
    label = TOTAL_LABEL if oz in SPLIT_DROP_FILES else None

    extra = {}

    if oz == BENZ_KEY:
        subdir = os.path.join(SPLIT_DIR, BENZ_SUBDIR)
        os.makedirs(subdir, exist_ok=True)
        path = os.path.join(subdir, f"{BENZ_FILE_PREFIX}_{stamp}.xlsx")

        part, extra = prepare_benz(part, load_benz_refs(), load_gastro_refs())

        gastro_vals = pd.Series(next(ec[1] for ec in extra["extra_cols"] if ec[0] == GASTRO_COL))
        covered_mask = pd.Series(False, index=gastro_vals.index)
        covered = 0
        for suffix, allowed in BENZ_GASTRO_SPLIT:
            m = gastro_vals.isin(allowed).values
            covered_mask |= pd.Series(m, index=gastro_vals.index)
            covered += int(m.sum())
            if not m.any():
                log.info(f"{oz}: pro {suffix} ({', '.join(allowed)}) nejsou řádky, soubor nevytvořen")
                continue
            sub_path = os.path.join(subdir, f"{BENZ_FILE_PREFIX}_{suffix}_{stamp}.xlsx")
            sub_part = part[m].reset_index(drop=True)
            letters = write_book(sub_part, sub_path, drop=drop, total_label=label, **subset_extra(extra, m))
            log.info(f"{oz}: {suffix} ({', '.join(allowed)}): {len(sub_part)} řádků -> {sub_path}")
            register(f"{oz}/{suffix}", sub_path, sub_part, ids[m], letters, "primary")
        (log.info if covered == len(part) else log.warning)(
            f"{oz}: rozdělení podle {GASTRO_COL}: rozděleno {covered} z {len(part)} řádků"
            + ("" if covered == len(part) else " – POZOR: část řádků nepatří do žádného souboru"))

        # === AUTOCHECK === Gastro-split покрывает все строки Benzina (не обрывает скрипт)
        lost = int((~covered_mask).sum())
        if lost:
            lost_vals = gastro_vals[~covered_mask].value_counts().to_dict()
            add_check("Benzina: Gastro-split pokrývá všechny řádky",
                      f"ztraceno {lost} z {len(gastro_vals)} (hodnoty Gastro: {lost_vals})", "FAIL")
        else:
            add_check("Benzina: Gastro-split pokrývá všechny řádky",
                      f"{len(gastro_vals)} z {len(gastro_vals)}", "OK")

        if not BENZ_KEEP_COMBINED:
            log.info(f"{oz}: souhrnný soubor se nevytváří (BENZ_KEEP_COMBINED = False)")
            continue

        letters = write_book(part, path, drop=drop, total_label=label, **extra)
        log.info(f"{oz}: {len(part)} řádků -> {path} ({time.perf_counter() - t_f:.2f} s); "
                 f"{letters[BENZ_NEW_COL]} = {BENZ_NEW_COL}, {letters[GASTRO_COL]} = {GASTRO_COL} (hodnoty); "
                 f"{letters[BENZ_INSERT_BEFORE]} = {BENZ_INSERT_BEFORE}, {letters['Popis']} = Popis")
        register(f"{oz}/combined", path, part, ids, letters, "mirror")
        continue

    path = os.path.join(SPLIT_DIR, fname + ".xlsx")
    if oz == SHELL_KEY:
        n_before = len(part)
        part, extra, ok = prepare_shell(part, drop, load_shell_refs())
        DROPPED_IDS.extend(ids[~ok].tolist())       # осознанно отсеянные строки
        ids = ids[ok]
        STATS["shell_before"] = n_before
        STATS["shell_dropped"] = int((~ok).sum())
        if part.empty:
            log.warning(f"{oz}: po filtru podle SHELL nezůstaly žádné řádky, soubor {fname}.xlsx nebyl vytvořen")
            continue

    letters = write_book(part, path, drop=drop, total_label=label, **extra)

    msg = f"{oz}: {len(part)} řádků -> {path} ({time.perf_counter() - t_f:.2f} s)"
    if drop:
        dropped = ", ".join(f"{x} ({sheet_col_name(letter_to_idx(x))})" for x in drop)
        msg += f"; odstraněny sloupce {dropped}; v řádku součtů A = {TOTAL_LABEL}"
    if extra:
        new_names = [ec[0] for ec in extra["extra_cols"]] + list(extra["rename"].values())
        msg += "; " + ", ".join(f"{letters[nm]} = {nm}" for nm in new_names)
        msg += f", {letters[SHELL_INSERT_BEFORE]} = {SHELL_INSERT_BEFORE}"
    log.info(msg)
    register(oz, path, part, ids, letters, "primary")

log.info(f"Hlavní soubor bez filtru podle sloupce C: {out_path} ({len(result)} řádků) – zapíše se po kontrolách")


# =====================================================================
# === AUTOCHECK === итоговые сверки (после создания всех файлов)
# =====================================================================
def _close(a, b):
    return abs(a - b) <= SUM_REL_TOL * max(1.0, abs(a), abs(b))


def _col_sum(df, i):
    return float(pd.to_numeric(df.iloc[:, i], errors="coerce").sum())


_all_idx = result.index.to_numpy()
_primary = [f for f in FILES if f["role"] == "primary"]
_mirror = [f for f in FILES if f["role"] == "mirror"]
_dropped = np.array(DROPPED_IDS, dtype=int)

# --- 1. каждая строка попала ровно в один файл (или осознанно отсеяна) ---
_cat = np.concatenate([f["ids"] for f in _primary]) if _primary else np.array([], dtype=int)
_uniq, _cnt = np.unique(_cat, return_counts=True)
_overlap = _uniq[_cnt > 1]
add_check("Překryvy mezi soubory",
          f"{len(_overlap)} řádků je ve více než jednom souboru" if len(_overlap) else "žádné",
          "OK" if len(_overlap) == 0 else "FAIL")

_accounted = np.union1d(_uniq, _dropped)
_unacc = np.setdiff1d(_all_idx, _accounted)
if len(_unacc):
    _by_oz = oz_vals.iloc[_unacc].value_counts().to_dict()
    _val = f"ztraceno {len(_unacc)} z {len(_all_idx)} (podle OznSit: {_by_oz})"
else:
    _val = (f"v souborech {len(_uniq)} + vyřazeno kvůli SHELL {len(_dropped)} = {len(_uniq) + len(_dropped)} "
            f"z {len(_all_idx)}")
add_check("Všechny řádky započteny (soubory + vyřazené SHELL)", _val, "OK" if len(_unacc) == 0 else "FAIL")

# --- 2. Benzina: общий файл = Gastro + SHOP ---
for f in _mirror:
    _parts = [p for p in _primary if p["group"] == f["group"]]
    _u = np.sort(np.concatenate([p["ids"] for p in _parts])) if _parts else np.array([], dtype=int)
    _same = np.array_equal(np.sort(f["ids"]), _u)
    add_check(f"{f['group']}: souhrnný soubor = součet částí",
              f"{len(f['ids'])} řádků v souhrnném, {len(_u)} v částech", "OK" if _same else "FAIL")

# --- 3. содержимое каждого файла = источник (Kód, Číslo Obj., суммы) ---
_kod_c = col_idx(columns, "Kód")
_obj_c = col_idx(columns, "Číslo Obj.")
_sum_cols = {columns[i]: i for i in (col_idx(columns, "Požad.MJ"), col_idx(columns, "Dodáno MJ"), PK, DK)}
_bad_files = []
for f in FILES:
    src, p = result.loc[f["ids"]], f["df"]
    prob = []
    if len(src) != len(p):
        prob.append(f"{len(p)} řádků místo {len(src)}")
    else:
        for nm, ci in (("Kód", _kod_c), ("Číslo Obj.", _obj_c)):
            if src.iloc[:, ci].map(key).tolist() != p.iloc[:, ci].map(key).tolist():
                prob.append(f"{nm} se liší od zdroje")
        for nm, ci in _sum_cols.items():
            if not _close(_col_sum(src, ci), _col_sum(p, ci)):
                prob.append(f"součet {nm}")
    if prob:
        _bad_files.append(f"{f['label']}: {', '.join(prob)}")
add_check("Obsah souborů = zdroj (Kód, Číslo Obj., součty)",
          "shoduje se ve všech souborech" if not _bad_files else "; ".join(_bad_files),
          "OK" if not _bad_files else "FAIL")

# --- 4. суммы: файлы + отсеянные = целое ---
_bad_sum = []
for nm, ci in _sum_cols.items():
    whole = _col_sum(result, ci)
    parts_sum = sum(_col_sum(f["df"], ci) for f in _primary)
    drop_sum = _col_sum(result.loc[_dropped], ci) if len(_dropped) else 0.0
    if not _close(whole, parts_sum + drop_sum):
        _bad_sum.append(f"{nm}: celek {whole:,.2f}, soubory {parts_sum:,.2f} + vyřazené {drop_sum:,.2f}")
add_check("Součty: soubory + vyřazené SHELL = celek",
          ", ".join(_sum_cols) + (" – sedí" if not _bad_sum else " – rozdíl: " + "; ".join(_bad_sum)),
          "OK" if not _bad_sum else "FAIL")

# --- 5. пороги по справочникам ---
if "benz_n" in STATS:
    _s = STATS["benz_cs_miss"] / max(STATS["benz_n"], 1)
    add_check("Benzina: ČS nenalezeno, %",
              f"{STATS['benz_cs_miss']} z {STATS['benz_n']} = {_s:.1%} (limit {CS_MAX_MISSING_SHARE:.0%})",
              "OK" if _s <= CS_MAX_MISSING_SHARE else "WARN")

if "gastro_counts" in STATS:
    _gc = STATS["gastro_counts"]
    _unexpected = set(_gc) - {GASTRO_YES, GASTRO_YES5, GASTRO_NO}
    _yes = _gc.get(GASTRO_YES, 0) + _gc.get(GASTRO_YES5, 0)
    _st = "OK"
    _note = f"{GASTRO_YES} {_gc.get(GASTRO_YES, 0)}, {GASTRO_YES5} {_gc.get(GASTRO_YES5, 0)}, {GASTRO_NO} {_gc.get(GASTRO_NO, 0)}"
    if _unexpected:
        _st, _note = "FAIL", _note + f"; neočekávané hodnoty: {sorted(_unexpected)}"
    elif _yes == 0:
        _st, _note = "FAIL", _note + "; žádná shoda s číselníky"
    add_check("Benzina: hodnoty Gastro", _note, _st)

if "shell_before" in STATS:
    _s = STATS["shell_dropped"] / max(STATS["shell_before"], 1)
    add_check("SHELL: vyřazeno kvůli chybějícímu SHELL, %",
              f"{STATS['shell_dropped']} z {STATS['shell_before']} = {_s:.1%} (limit {SHELL_MAX_DROP_SHARE:.0%})",
              "OK" if _s <= SHELL_MAX_DROP_SHARE else "WARN")
    _s = STATS.get("shell_jmeno_na", 0) / max(STATS["shell_before"], 1)
    add_check("SHELL: K Jméno 2 nenalezeno, %",
              f"{STATS.get('shell_jmeno_na', 0)} z {STATS['shell_before']} = {_s:.1%} (limit {SHELL_MAX_JMENO_NA_SHARE:.0%})",
              "OK" if _s <= SHELL_MAX_JMENO_NA_SHARE else "WARN")


# --- 6. проверка записанных xlsx ---
def verify_xlsx(path, n_data, letters):
    """Открывает записанный файл и проверяет: число строк, заголовки, формулы итоговой строки."""
    if not os.path.exists(path):
        return ["soubor nevytvořen"]
    probs = []
    wb = load_workbook(path, read_only=True)
    try:
        ws = wb["Souhrn"] if "Souhrn" in wb.sheetnames else wb.worksheets[0]
        hdr = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        hdr = {h for h in hdr if h is not None}
        if hdr != set(letters):
            probs.append(f"záhlaví: navíc {sorted(hdr - set(letters))}, chybí {sorted(set(letters) - hdr)}")
        expected_rows = n_data + 2                      # шапка + данные + итоговая строка
        rows = ws.max_row
        if rows != expected_rows:
            rows = sum(1 for _ in ws.iter_rows(values_only=True))
        if rows != expected_rows:
            probs.append(f"{rows} řádků místo {expected_rows}")
        tot = next(ws.iter_rows(min_row=expected_rows, max_row=expected_rows, values_only=True), ())
        for h in (columns[PK], columns[DK]):
            ci = column_index_from_string(letters[h]) - 1
            v = tot[ci] if ci < len(tot) else None
            if not (isinstance(v, str) and v.startswith("=SUBTOTAL(9,")):
                probs.append(f"v řádku součtů chybí u '{h}' vzorec SUBTOTAL")
    finally:
        wb.close()
    return probs


_bad_write = []
for f in FILES:
    pr = verify_xlsx(f["path"], len(f["df"]), f["letters"])
    if pr:
        _bad_write.append(f"{f['label']}: {'; '.join(pr)}")
add_check("Zapsané soubory (řádky, záhlaví, součty)",
          f"zkontrolováno {len(FILES)} souborů, bez chyb" if not _bad_write else "; ".join(_bad_write),
          "OK" if not _bad_write else "FAIL")

# ---------- 15. запись основного файла вместе с листом Kontrola ----------
main_letters = write_book(result, out_path, kontrola=AUTOCHECK)
log.info(f"Zapsán {out_path}: {len(result)} datových řádků + řádek součtů (řádek {len(result) + 2}) "
         f"+ list Kontrola ({len(AUTOCHECK)} kontrol)")

_main_probs = verify_xlsx(out_path, len(result), main_letters)
if _main_probs:
    log.error(f"[AUTOCHECK] FAIL: hlavní soubor: {'; '.join(_main_probs)}")
else:
    log.info("Hlavní soubor zkontrolován: řádky, záhlaví, součty – OK")

# ---------- 16. сводка ----------
log.info("===== AUTOCHECK =====")
_LEVEL = {"OK": log.info, "WARN": log.warning, "FAIL": log.error}
for _name, _value, _status in AUTOCHECK:
    _LEVEL.get(_status, log.info)(f"  [{_status}] {_name}: {_value}")
_n_fail = sum(1 for x in AUTOCHECK if x[2] == "FAIL") + (1 if _main_probs else 0)
_n_warn = sum(1 for x in AUTOCHECK if x[2] == "WARN")
(log.error if _n_fail else log.warning if _n_warn else log.info)(
    f"Celkem: FAIL = {_n_fail}, WARN = {_n_warn}, OK = {len(AUTOCHECK) - _n_fail - _n_warn}")

t_end = time.perf_counter()
log.info(f"Zápis a formátování: {t_end - t_write:.2f} s")
log.info(f"Hotovo. Celková doba běhu: {t_end - t_start:.2f} s")
if _n_fail:
    log.error(f"Report dokončen s {_n_fail} chybami kontrol (FAIL) – viz list Kontrola")
    sys.exit(1)
