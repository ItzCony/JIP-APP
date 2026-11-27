"""Analytické oddělení → Kontakty Nákupčí.

Živý náhled dvou XLSX souborů s kontakty (seznam vedoucích poboček a ASM,
číselník nákupčích podle sortimentu). Soubory leží ve složce KontaktyASM/ vedle
aplikace. Náhled se překreslí sám, jakmile se soubor na disku změní — po nahrání
nové verze v aplikaci i po přepsání souboru přímo ve složce — bez obnovení stránky.

List se nekopíruje buňku po buňce (excelové výšky řádků, zalamování a sytě
podbarvené buňky působily na webu nesourodě) — rozpozná se jeho stavba
a vykreslí se jednotná tabulka:
  • řádky nad záhlavím → nadpis karty; skryté řádky a sloupce se vynechají,
  • sloupec s daty bez nadpisu se připojí k nadpisu vlevo (Sortiment = kód + název),
  • e-mail a telefon se přidají k osobě ve sloupci vlevo (jméno a telefon, pod nimi
    e-mail) — z osmi sloupců zbudou čtyři a tabulka se vejde i na notebook,
  • adresa se zobrazí na dva řádky (ulice / PSČ a obec),
  • sloupec jen s nadpisem (MAPA nad obrázkem) zmizí; velké obrázky z listu se
    ukážou pod tabulkou s tímto nadpisem jako popiskem, malá loga se vynechají,
  • řídký sloupec bez nadpisu a osamocený dlouhý text → poznámka
    (červená výplň nebo „POZOR“ = upozornění),
  • opakující se úvodní hodnoty (pobočka, adresa, vedoucí…) se sloučí do skupiny,
  • výplň klíčového sloupce (barva regionu) → barevná tečka místo plné výplně,
  • jednotné písmo i výška řádků; delší text se zalomí, jen když by se tabulka
    jinak do okna nevešla.
"""
from __future__ import annotations

import asyncio
import base64
import colorsys
import datetime
import html
import io
import os
import re
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

import openpyxl
from openpyxl.styles.colors import COLOR_INDEX
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import column_index_from_string, get_column_letter
from nicegui import ui

import intranet_logger

SLOZKA = os.path.abspath('KontaktyASM')     # kořen vedle aplikace (jako Bonusy, KPI)
SOUBORY = [                                 # (název souboru na disku, popisek záložky)
    ('SEZNAM_vedoucí poboček, ASM.xlsx', 'Vedoucí poboček a ASM'),
    ('Aktuální číselník nákupčí sortiment.xlsx', 'Číselník nákupčí – sortiment'),
]
KONTROLA_S = 5                              # jak často hlídat změnu souborů na disku
MAX_RADKU = 2000                            # pojistka proti obřím listům v náhledu
DLOUHY_TEXT = 40                            # osamocený text v řádku od této délky = poznámka
ZALOMIT_OD = 24                             # sloupec s texty od této délky se smí zalomit
MIN_OBRAZEK = (320, 200)                    # menší obrázky (loga, ikony) se vynechají [px]

_cache: dict[str, tuple[float, list[tuple[str, str, dict]]]] = {}   # cesta → (mtime, [(list, html, popis)])
_cache_zamek = threading.Lock()


def _cesta(nazev: str) -> str:
    return os.path.join(SLOZKA, nazev)


def _mtime(cesta: str) -> float | None:
    try:
        return os.path.getmtime(cesta)
    except OSError:
        return None


# ---------------------------------------------------------------- barvy

_NS_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_HEX6 = re.compile(r'[0-9A-F]{6}')
_PORADI_MOTIVU = ('lt1', 'dk1', 'lt2', 'dk2', 'accent1', 'accent2', 'accent3',
                  'accent4', 'accent5', 'accent6', 'hlink', 'folHlink')   # theme=0 je lt1, 1 je dk1…


def _paleta_motivu(wb) -> list[str]:
    """Barvy motivu sešitu v pořadí, v jakém na ně odkazují buňky."""
    try:
        schema = ET.fromstring(wb.loaded_theme).find(f'.//{_NS_A}clrScheme')
    except Exception:
        return []
    if schema is None:
        return []
    barvy = {}
    for prvek in schema:
        for definice in prvek:                      # srgbClr val=… / sysClr lastClr=…
            hodnota = (definice.get('lastClr') or definice.get('val') or '').upper()
            if _HEX6.fullmatch(hodnota):
                barvy[prvek.tag.replace(_NS_A, '')] = hodnota
    return [barvy.get(k, '') for k in _PORADI_MOTIVU]


def _odstin(rgb: str, tint: float) -> str:
    """Excelový tint (−1…1) = posun světlosti v HLS (záporný ztmavuje, kladný zesvětluje)."""
    if not tint:
        return rgb
    h, l, s = colorsys.rgb_to_hls(*(int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    l = l * (1 + tint) if tint < 0 else l * (1 - tint) + tint
    return ''.join(f'{round(x * 255):02X}' for x in colorsys.hls_to_rgb(h, l, s))


def _barva(c, paleta: list[str]) -> str | None:
    """openpyxl Color (RGB, motiv i index) → 'RRGGBB'; None = bez barvy."""
    try:
        if c is None:
            return None
        if c.type == 'rgb' and isinstance(c.rgb, str) and len(c.rgb) == 8:
            if c.rgb == '00000000':
                return None
            rgb = c.rgb[2:]
        elif c.type == 'theme' and isinstance(c.theme, int) and c.theme < len(paleta):
            rgb = paleta[c.theme]
        elif c.type == 'indexed' and isinstance(c.indexed, int) and c.indexed < len(COLOR_INDEX):
            rgb = COLOR_INDEX[c.indexed][2:]
        else:
            return None
        if not isinstance(rgb, str) or not _HEX6.fullmatch(rgb.upper()):
            return None                             # jde do atributu style → jen čisté hex
        return _odstin(rgb.upper(), float(c.tint or 0))
    except Exception:                               # rozbitá barva nesmí shodit náhled
        return None


def _vypln(bunka, paleta: list[str]) -> str | None:
    """Barva plné výplně buňky; bílá a téměř bílá se bere jako žádná."""
    v = bunka.fill
    if v is None or v.fill_type != 'solid':
        return None
    rgb = _barva(v.fgColor, paleta)
    if rgb and min(int(rgb[i:i + 2], 16) for i in (0, 2, 4)) >= 0xF0:
        return None
    return rgb


def _cervena(rgb: str | None) -> bool:
    if not rgb:
        return False
    r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    return r >= 0xC0 and g <= 0x90 and b <= 0x90


def _pruhledna(rgb: str, alfa: float = .18) -> str:
    r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    return f'rgba({r},{g},{b},{alfa})'


# ---------------------------------------------------------------- text

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_MEZERY = re.compile(r'\s+')
_POMLCKA = re.compile(r'(?<=\S) -\s*(?=[^\W\d_])')     # „Praha - Bořanovice“ → „Praha – Bořanovice“
_TELEFON = re.compile(r'(?<![\d+])(\+?420)?\s?(\d{3})\s?(\d{3})\s?(\d{3})(?!\d)')
_TELEFON_V_TEXTU = re.compile(r'(\btel\.?:?\s*)(\d{3})\s?(\d{3})\s?(\d{3})(?!\d)', re.I)
_TELEFON_NADPIS = re.compile(r'\btel|mobil|phone', re.I)
_PORADI = re.compile(r'\d{1,4}\.?')
_ADRESA = re.compile(r'(.+?),\s*(\d{3} ?\d{2}\s+\S.*)')    # „Ulice 12, 533 52 Obec“
_UVOD_POZNAMKY = re.compile(r'([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]{3,}[!:])\s*')     # „POZOR:“ → tučně
_PRED_EMAILEM = re.compile(r'(?<=\d) (?=[\w.+-]+@)')
_ZLOM = re.compile(r'(;\s+|,\s+|\s+·\s+|\s+–\s+|/|\+)')        # kde se smí zalomit dlouhý text
_ZLOM_ADRESA = re.compile(r'(,\s+|\s+–\s+)')                   # adresa: ne uvnitř čísla 630/78


def _text(hodnota, oddelovac: str = ' · ') -> str:
    """Hodnota buňky → jednořádkový text (víc řádků v buňce se spojí oddělovačem)."""
    if hodnota is None:
        return ''
    if isinstance(hodnota, datetime.datetime):
        return hodnota.strftime('%d.%m.%Y' if hodnota.time() == datetime.time() else '%d.%m.%Y %H:%M')
    if isinstance(hodnota, datetime.date):
        return hodnota.strftime('%d.%m.%Y')
    if isinstance(hodnota, float):
        return str(int(hodnota)) if hodnota.is_integer() else str(hodnota).replace('.', ',')
    radky = (_MEZERY.sub(' ', r).strip() for r in str(hodnota).splitlines())
    return _POMLCKA.sub(' – ', oddelovac.join(r for r in radky if r))


def _telefony(t: str) -> str:
    """Telefonní čísla po trojicích: 702014406 → 702 014 406."""
    return _TELEFON.sub(lambda m: ('+420 ' if m.group(1) else '')
                        + f'{m.group(2)} {m.group(3)} {m.group(4)}', t)


def _html_textu(t: str) -> str:
    """Escapovaný text s klikacími e-maily."""
    kusy, pos = [], 0
    for m in _EMAIL.finditer(t):
        e = html.escape(m.group(0))
        kusy += [html.escape(t[pos:m.start()]), f'<a href="mailto:{e}">{e}</a>']
        pos = m.end()
    kusy.append(html.escape(t[pos:]))
    return ''.join(kusy)


def _html_zalomitelny(t: str, vzor: re.Pattern = None) -> str:
    """Text, který se smí zalomit jen mezi položkami (za ; , · – / +), nikdy uprostřed jména."""
    vysledek = []
    for i, kus in enumerate((vzor or _ZLOM).split(t)):
        if i % 2:                                   # oddělovač — za ním je místo pro zlom
            vysledek.append(html.escape(kus) + ('' if kus.endswith(' ') else '<wbr>'))
        elif kus:
            vysledek.append(f'<span class="nw">{_html_textu(kus)}</span>')
    return ''.join(vysledek)


def _html_nadpisu(t: str) -> str:
    """Nadpis sloupce — slovo s pomlčkou (E-mail) se nerozdělí na konci řádku."""
    return ' '.join(f'<span class="nw">{html.escape(s)}</span>' if '-' in s else html.escape(s)
                    for s in t.split(' '))


def _poznamka(t: str, varovani: bool) -> str:
    """Poznámka jako decentní pruh s ikonou (upozornění červeně)."""
    t = t[:1].upper() + t[1:]
    t = _TELEFON_V_TEXTU.sub(lambda m: f'{m.group(1)}{m.group(2)} {m.group(3)} {m.group(4)}', t)
    t = _PRED_EMAILEM.sub(' · ', t)
    uvod = ''
    if (m := _UVOD_POZNAMKY.match(t)):
        uvod, t = f'<b>{html.escape(m.group(1))}</b> ', t[m.end():]
    return (f'<div class="kasm-pozn{" warn" if varovani else ""}">'
            f'<i class="q-icon notranslate material-icons" aria-hidden="true">'
            f'{"warning" if varovani else "info"}</i>'
            f'<span>{uvod}{_html_textu(t)}</span></div>')


# ---------------------------------------------------------------- obrázky

_MIME = {'png': 'image/png', 'jpeg': 'image/jpeg', 'jpg': 'image/jpeg', 'gif': 'image/gif',
         'bmp': 'image/bmp', 'webp': 'image/webp'}
_EMU_NA_PX = 9525


def _obrazky(ws, sirky: dict[int, float], vysky: dict[int, float],
             skryte_sl: set[int], skryte_r: set[int]) -> list[tuple[int, int, str]]:
    """Velké obrázky listu → [(první sloupec, poslední sloupec, data URI)]."""
    def _sirka(c):
        return 0 if c in skryte_sl else sirky.get(c, 8.43) * 7 + 5

    def _vyska(r):
        return 0 if r in skryte_r else vysky.get(r, 15) * 4 / 3

    vysledek = []
    for obr in getattr(ws, '_images', []):
        mime = _MIME.get(str(getattr(obr, 'format', '') or '').lower())
        if not mime:
            continue
        try:
            a = obr.anchor
            od, do, ext = getattr(a, '_from', None), getattr(a, 'to', None), getattr(a, 'ext', None)
            if od is not None and do is not None:               # ukotvení na dvě buňky
                c1, c2 = od.col + 1, do.col + 1
                sirka = sum(_sirka(c) for c in range(c1, c2)) + (do.colOff - od.colOff) / _EMU_NA_PX
                vyska = (sum(_vyska(r) for r in range(od.row + 1, do.row + 1))
                         + (do.rowOff - od.rowOff) / _EMU_NA_PX)
            elif ext is not None:                               # jedna buňka / absolutní pozice
                c1 = c2 = od.col + 1 if od is not None else 0
                sirka, vyska = ext.width / _EMU_NA_PX, ext.height / _EMU_NA_PX
            else:
                c1 = c2 = 0
                sirka, vyska = obr.width, obr.height
            if sirka < MIN_OBRAZEK[0] or vyska < MIN_OBRAZEK[1]:
                continue
            data = base64.b64encode(obr._data()).decode('ascii')
        except Exception:                                       # nečitelný obrázek náhled neshodí
            continue
        vysledek.append((c1, c2, f'data:{mime};base64,{data}'))
    return vysledek


# ---------------------------------------------------------------- list → HTML

@dataclass
class _Bunka:
    text: str
    hodnota: object
    vypln: str | None       # 'RRGGBB' plné výplně (bez bílé)
    pismo: str | None       # 'RRGGBB' barvy písma


def _html_listu(ws, paleta: list[str]) -> tuple[str, dict]:
    """(html náhledu, popis pro editor řádků — viz _popis_listu)."""
    # ── rozměry, skryté sloupce a řádky
    skryte_sl: set[int] = set()
    sirky: dict[int, float] = {}
    for pismeno, d in ws.column_dimensions.items():
        try:
            od = d.min or column_index_from_string(pismeno)
        except ValueError:
            continue
        for c in range(od, min(d.max or od, od + 1000) + 1):
            if d.hidden:
                skryte_sl.add(c)
            if d.width:
                sirky[c] = d.width
    skryte_r = {r for r, d in ws.row_dimensions.items() if d.hidden}
    vysky = {r: d.ht for r, d in ws.row_dimensions.items() if d.ht}

    # ── skutečný rozsah dat (max_row/max_column bývá nafouknutý formátováním)
    posl_r = posl_c = 0
    for radek in ws.iter_rows():
        for b in radek:
            if b.value is not None and _text(b.value):
                posl_r, posl_c = max(posl_r, b.row), max(posl_c, b.column)
    obrazky = _obrazky(ws, sirky, vysky, skryte_sl, skryte_r)
    if not posl_r and not obrazky:
        return ('<div class="kasm"><div class="kasm-karta kasm-prazdny">List je prázdný.</div></div>',
                _popis_listu())
    oriznuto = posl_r > MAX_RADKU
    posl_r = min(posl_r, MAX_RADKU)

    # ── sloučené buňky (rozsah neořezaný — kvůli popiskům obrázků)
    sloucene: dict[tuple[int, int], tuple[int, int]] = {}
    kryte: set[tuple[int, int]] = set()
    for m in ws.merged_cells.ranges:
        if m.min_row > posl_r or m.min_col > posl_c:
            continue
        sloucene[(m.min_row, m.min_col)] = (m.max_row, m.max_col)
        for r in range(m.min_row, min(m.max_row, posl_r) + 1):
            for c in range(m.min_col, min(m.max_col, posl_c) + 1):
                if (r, c) != (m.min_row, m.min_col):
                    kryte.add((r, c))

    # ── mřížka viditelných neprázdných buněk
    radky = [r for r in range(1, posl_r + 1) if r not in skryte_r]
    sloupce = [c for c in range(1, posl_c + 1) if c not in skryte_sl]
    mrizka: dict[tuple[int, int], _Bunka] = {}
    if posl_r:
        for radek in ws.iter_rows(min_row=1, max_row=posl_r, max_col=posl_c):
            for b in radek:
                k = (b.row, b.column)
                if b.row in skryte_r or b.column in skryte_sl or k in kryte:
                    continue
                if (t := _text(b.value)):
                    mrizka[k] = _Bunka(t, b.value, _vypln(b, paleta),
                                       _barva(b.font.color, paleta) if b.font else None)

    def _v_radku(r):
        return [c for c in sloupce if (r, c) in mrizka]

    pocet = {r: len(_v_radku(r)) for r in radky}
    posledni = max((r for r in radky if pocet[r]), default=0)

    # ── záhlaví = první „široký“ textový řádek, pod kterým ještě něco je
    zahlavi = None
    nejvic = max(pocet.values(), default=0)
    for r in radky:
        if pocet[r] >= max(2, (nejvic + 1) // 2):
            textu = sum(isinstance(mrizka[(r, c)].hodnota, str) for c in _v_radku(r))
            if textu * 2 >= pocet[r] and r < posledni:
                zahlavi = r
            break

    titulky = []
    if zahlavi:
        for r in radky:
            if r >= zahlavi:
                break
            if pocet[r]:
                titulky.append((r, ' · '.join(mrizka[(r, c)].text for c in _v_radku(r))))

    nadpis: dict[int, str] = {}         # sloupec → vlastní text záhlaví
    vlastnik: dict[int, int] = {}       # sloupec → sloupec, jehož nadpis ho kryje (sloučené záhlaví)
    if zahlavi:
        for c in _v_radku(zahlavi):
            nadpis[c] = _text(mrizka[(zahlavi, c)].hodnota, ' ')
            for c2 in range(c, sloucene.get((zahlavi, c), (zahlavi, c))[1] + 1):
                vlastnik[c2] = c

    # ── datové řádky; osamocený dlouhý / široce sloučený text = poznámka
    data_r = [r for r in radky if (zahlavi is None or r > zahlavi) and pocet[r]]
    pozn_r = set()
    for r in data_r:
        if pocet[r] == 1:
            c = _v_radku(r)[0]
            konec = sloucene.get((r, c), (r, c))[1]
            if len(mrizka[(r, c)].text) >= DLOUHY_TEXT or konec - c >= 2:
                pozn_r.add(r)
    zaznam_r = [r for r in data_r if r not in pozn_r]

    # ── sloupce: datové / poznámkové (řídké bez nadpisu) / jen nadpis (popisek obrázku pod ním,
    #    jinak prázdný sloupec tabulky — třeba nově přidaný, zatím nevyplněný)
    obsazeno = {c: sum((r, c) in mrizka for r in zaznam_r) for c in sloupce}
    prah = max(2, round(len(zaznam_r) * .25))
    datove, pozn_sl, popisky = [], [], []
    for c in sloupce:
        if not obsazeno[c]:
            if c in nadpis:
                konec = sloucene.get((zahlavi, c), (zahlavi, c))[1]
                if any(c1 and c1 <= konec and c2 >= c for c1, c2, _ in obrazky):
                    popisky.append((nadpis[c], c, konec))
                else:
                    datove.append(c)
        elif zahlavi and c not in vlastnik and obsazeno[c] < prah:
            pozn_sl.append(c)
        else:
            datove.append(c)

    # ── druhy sloupců
    def _hodnoty(c):
        return [mrizka[(r, c)] for r in zaznam_r if (r, c) in mrizka]

    def _vetsina(c, test, podil=.8) -> bool:
        h = _hodnoty(c)
        return bool(h) and sum(1 for b in h if test(b.text)) >= podil * len(h)

    def _nadpis_sloupce(c):
        return nadpis.get(vlastnik.get(c, 0), '')

    poradi_sl = (datove[0] if datove and len(_hodnoty(datove[0])) >= 2
                 and _nadpis_sloupce(datove[0]).lower() in ('', '#', 'č', 'č.', 'p.č.', 'poř.')
                 and _vetsina(datove[0], _PORADI.fullmatch, 1) else None)
    if poradi_sl and ([int(b.text.rstrip('.')) for b in _hodnoty(poradi_sl)]
                      == list(range(1, len(zaznam_r) + 1))):
        datove.remove(poradi_sl)                  # čisté číslování 1, 2, 3… je v tabulce zbytečné
        poradi_sl = None
    ostatni = [c for c in datove if c != poradi_sl]
    emaily = {c for c in ostatni if _vetsina(c, _EMAIL.fullmatch)}
    telefony = {c for c in ostatni if c not in emaily and (
        _vetsina(c, _TELEFON.fullmatch)
        or (_TELEFON_NADPIS.search(_nadpis_sloupce(c)) and _vetsina(c, _TELEFON.search, .5)))}
    adresy = {c for c in ostatni if c not in emaily and c not in telefony
              and _vetsina(c, _ADRESA.fullmatch, .6)}
    cisla_sl = {c for c in ostatni if c not in telefony and all(
        isinstance(b.hodnota, (int, float)) and not isinstance(b.hodnota, bool) for b in _hodnoty(c))}
    zvlastni = emaily | telefony | adresy | cisla_sl

    # ── e-mail a telefon patří osobě ve sloupci vlevo → jedna buňka „jméno + telefon / e-mail“
    pripojeno: dict[int, list[int]] = {}
    zobrazene: list[int] = []
    for c in datove:
        zakl = zobrazene[-1] if zobrazene else None
        if (c in emaily or c in telefony) and zakl is not None and zakl != poradi_sl \
                and zakl not in zvlastni:
            pripojeno.setdefault(zakl, []).append(c)
        else:
            zobrazene.append(c)
    klic = next((c for c in zobrazene if c != poradi_sl), None)

    def _sloupce_bunky(c) -> tuple:
        return (c, *pripojeno.get(c, ()))

    def _text_sl(r, c) -> str:
        return mrizka[(r, c)].text if (r, c) in mrizka else ''

    zalomit = {c for c in zobrazene if c != poradi_sl and c not in pripojeno and c not in zvlastni
               and max((len(_text_sl(r, c)) for r in zaznam_r), default=0) >= ZALOMIT_OD}

    # ── barva klíčového sloupce (region) → tečka, pokud je obarvená většina záznamů
    vypln_klice = {r: mrizka[(r, klic)].vypln for r in zaznam_r if (r, klic) in mrizka}
    obarveno = sum(1 for v in vypln_klice.values() if v)
    tecky = obarveno > 0 and obarveno * 2 >= len(vypln_klice)

    # ── skupiny: navazující záznamy se stejnými úvodními hodnotami (pobočka, adresa…)
    def _obsah_sl(r, c) -> tuple:
        return tuple(mrizka[(r, x)].text if (r, x) in mrizka else '' for x in _sloupce_bunky(c))

    def _spolecne(r1, r2) -> int:
        if not zobrazene or (r1, zobrazene[0]) not in mrizka:
            return 0
        n = 0
        for c in zobrazene[:-1]:                    # poslední sloupec se nikdy neslučuje
            if _obsah_sl(r1, c) != _obsah_sl(r2, c):
                break
            n += 1
        return n

    polozky: list[tuple[str, object]] = []       # ('skupina', [řádky]) | ('pozn', řádek)
    for r in data_r:
        if r in pozn_r or not zobrazene:
            polozky.append(('pozn', r))
        elif polozky and polozky[-1][0] == 'skupina' and _spolecne(polozky[-1][1][-1], r):
            polozky[-1][1].append(r)
        else:
            polozky.append(('skupina', [r]))
    pata_r = []                                   # poznámky pod posledním záznamem → patička karty
    while polozky and polozky[-1][0] == 'pozn':
        pata_r.insert(0, polozky.pop()[1])

    def _osamocena(r) -> _Bunka:
        return mrizka[(r, _v_radku(r)[0])]

    def _je_varovani(b: _Bunka) -> bool:
        return (_cervena(b.vypln) or _cervena(b.pismo)
                or b.text.upper().startswith(('POZOR', 'UPOZORN', 'DŮLEŽIT', '!')))

    def _obsah_td(r, c) -> str:
        t = _text_sl(r, c)
        if c in telefony:
            t = _telefony(t)
        if c in pripojeno:                          # osoba: jméno + telefon, pod tím e-mail
            tel = [_telefony(mrizka[(r, x)].text) for x in pripojeno[c]
                   if x in telefony and (r, x) in mrizka]
            mail = [mrizka[(r, x)].text for x in pripojeno[c] if x in emaily and (r, x) in mrizka]
            r1 = ((f'<span class="jm">{_html_textu(t)}</span>' if t else '')
                  + (f'<span>{html.escape(" · ".join(tel))}</span>' if tel else ''))
            r2 = ' · '.join(_html_textu(e) for e in mail)
            return ('<div class="kasm-kont">' + (f'<div class="r1">{r1}</div>' if r1 else '')
                    + (f'<div class="r2">{r2}</div>' if r2 else '') + '</div>')
        if c in adresy and (m := _ADRESA.fullmatch(t)):
            return (f'<div class="wa"><div>{_html_zalomitelny(m.group(1), _ZLOM_ADRESA)}</div>'
                    f'<div class="r2">{_html_zalomitelny(m.group(2), _ZLOM_ADRESA)}</div></div>')
        return _html_zalomitelny(t) if c in zalomit else _html_textu(t)

    def _tridy(c, posl=None) -> list[str]:
        posl = posl or c
        return [t for t, plati in (('i', c == poradi_sl), ('n', c in cisla_sl),
                                   ('okl', c == zobrazene[0]), ('okp', posl == zobrazene[-1]))
                if plati]

    def _td(r, c, rowspan) -> str:
        b = mrizka.get((r, c))
        tridy = _tridy(c) + (['k'] if c == klic else [])
        obsah = _obsah_td(r, c)
        if c == klic and tecky:
            barva = vypln_klice.get(r)
            obsah = (f'<span class="kasm-tecka" style="background:#{barva}"></span>' if barva
                     else '<span class="kasm-tecka bez"></span>') + obsah
        if c in zalomit:
            obsah = f'<div class="w">{obsah}</div>'
        attr = f' class="{" ".join(tridy)}"' if tridy else ''
        if b and b.vypln and not (tecky and (c == klic or b.vypln == vypln_klice.get(r))):
            attr += f' style="background:{_pruhledna(b.vypln)}"'      # zvýrazněná buňka
        if rowspan > 1:
            attr += f' rowspan="{rowspan}"'
        return f'<td{attr}>{obsah}</td>'

    def _radek_poznamky(r, b: _Bunka) -> str:
        return (f'<tr class="kasm-rp" data-r="{r}"><td colspan="{len(zobrazene)}">'
                f'{_poznamka(b.text, _je_varovani(b))}</td></tr>')

    sdilene: dict[tuple[int, int], tuple[int, ...]] = {}  # (řádek, sloupec) → řádky jedné sloučené buňky

    def _skupina(rr: list[int]) -> str:
        spol = [0] + [_spolecne(rr[i - 1], rr[i]) for i in range(1, len(rr))]
        tr = []
        for i, r in enumerate(rr):
            bunky = []
            for j, c in enumerate(zobrazene):
                if spol[i] > j:                     # sloučeno s buňkou výš
                    continue
                n = 1
                while i + n < len(rr) and spol[i + n] > j:
                    n += 1
                if n > 1:                           # stejné hodnoty v náhledu sloučené do jedné buňky
                    for x in _sloupce_bunky(c):
                        for rx in rr[i:i + n]:
                            sdilene[(rx, x)] = tuple(rr[i:i + n])
                bunky.append(_td(r, c, n))
            tr.append(f'<tr data-r="{r}">' + ''.join(bunky) + '</tr>')
        tr += [_radek_poznamky(r, mrizka[(r, c)]) for r in rr for c in pozn_sl if (r, c) in mrizka]
        return '<tbody>' + ''.join(tr) + '</tbody>'

    # ── sestavení
    karta = []
    if titulky:
        karta.append('<div class="kasm-hlava">' + ''.join(
            f'<div class="{"kasm-podnadpis" if i else "kasm-nadpis"}" data-r="{r}">{_html_textu(t)}</div>'
            for i, (r, t) in enumerate(titulky)) + '</div>')
    if zobrazene and polozky:
        thead = ''
        if zahlavi:
            hlavicka: list[list] = []               # [text, colspan, první sloupec]
            for i, c in enumerate(zobrazene):
                v, pred = vlastnik.get(c), (zobrazene[i - 1] if i else None)
                if v == c:
                    hlavicka.append([nadpis[c], 1, c])
                elif hlavicka and pred is not None and (
                        (v is not None and vlastnik.get(pred) == v)
                        or (v is None and hlavicka[-1][0]
                            and sloupce.index(c) - sloupce.index(max(_sloupce_bunky(pred))) == 1)):
                    hlavicka[-1][1] += 1            # data bez nadpisu patří k nadpisu vlevo
                else:
                    hlavicka.append([nadpis.get(v, '') if v else '', 1, c])
            th = []
            for t, n, c in hlavicka:
                tridy = _tridy(c, zobrazene[zobrazene.index(c) + n - 1])
                attr = ((f' class="{" ".join(tridy)}"' if tridy else '')
                        + (f' colspan="{n}"' if n > 1 else ''))
                obsah = _html_nadpisu(t)
                if n == 1 and c in pripojeno:
                    # telefon a e-mail jsou v buňce osoby → jejich nadpisy ve stejném rozložení
                    tel, mail = (' · '.join(_html_nadpisu(nadpis.get(x, '')) for x in pripojeno[c] if x in druh)
                                 for druh in (telefony, emaily))
                    obsah = (f'<div class="kasm-kont"><div class="r1"><span>{obsah}</span>'
                             + (f'<span>{tel}</span>' if tel else '') + '</div>'
                             + (f'<div class="r2">{mail}</div>' if mail else '') + '</div>')
                th.append(f'<th{attr}>{obsah}</th>')
            thead = '<thead><tr>' + ''.join(th) + '</tr></thead>'
        telo = [_skupina(x) if druh == 'skupina'
                else '<tbody>' + _radek_poznamky(x, _osamocena(x)) + '</tbody>'
                for druh, x in polozky]
        husta = ' husta' if len(zobrazene) >= 5 else ''      # hodně sloupců → užší mezery
        karta.append(f'<div class="kasm-posuv"><table class="kasm-tab{husta}">' + thead + ''.join(telo)
                     + '</table></div>')
    pata = [f'<div data-r="{r}">{_poznamka(b.text, _je_varovani(b))}</div>'
            for r, b in zip(pata_r, map(_osamocena, pata_r))]
    if oriznuto:
        pata.append(_poznamka(f'Náhled ukazuje prvních {MAX_RADKU} řádků listu.', False))
    if pata:
        karta.append('<div class="kasm-pata">' + ''.join(pata) + '</div>')

    casti = ['<div class="kasm">']
    if karta:
        casti.append('<div class="kasm-karta">' + ''.join(karta) + '</div>')
    for c1, c2, src in obrazky:
        popisek = next((t for t, p1, p2 in popisky if c1 and p1 <= c2 and p2 >= c1), '')
        hlava = (f'<figcaption class="kasm-hlava"><div class="kasm-nadpis">{html.escape(popisek)}'
                 f'</div></figcaption>' if popisek else '')
        casti.append(f'<figure class="kasm-karta kasm-obr">{hlava}<div class="kasm-obr-telo">'
                     f'<img src="{src}" alt="{html.escape(popisek)}"></div></figure>')
    casti.append('</div>')
    obr_sl = {c for c1, c2, _ in obrazky if c1 for c in range(c1, c2 + 1)}
    sl_zaznamu = [c for c in sloupce if obsazeno[c] or (c in nadpis and c not in obr_sl)]
    return ''.join(casti), _popis_listu(zaznam_r, [(c, nadpis.get(vlastnik.get(c), '')) for c in sl_zaznamu],
                                         sloupce, sdilene)


def _popis_listu(zaznamy=(), sloupce=(), viditelne=(), sdilene=None) -> dict:
    """Co editor řádků potřebuje vědět o listu: řádky záznamů, jejich sloupce s nadpisy ze záhlaví
    (prázdný nadpis → „Sloupec C“, stejný nadpis víckrát → doplní se písmeno sloupce),
    viditelné sloupce (pro řádky mimo tabulku — titulky, poznámky) a hodnoty, které náhled
    sloučil přes víc řádků ((řádek, sloupec) → řádky)."""
    pocty = Counter(t for _, t in sloupce if t)
    nazvy = tuple((c, (f'{t} ({get_column_letter(c)})' if pocty[t] > 1 else t) if t
                   else f'Sloupec {get_column_letter(c)}') for c, t in sloupce)
    return {'zaznamy': frozenset(zaznamy), 'sloupce': nazvy, 'viditelne': tuple(viditelne),
            'sdilene': dict(sdilene or {})}


def _nacti(cesta: str) -> list[tuple[str, str, dict]]:
    """[(název listu, html, popis listu)] — s cache podle mtime, takže se parsuje jen po změně."""
    mt = _mtime(cesta)
    with _cache_zamek:
        hit = _cache.get(cesta)
        if hit and hit[0] == mt:
            return hit[1]
    wb = openpyxl.load_workbook(cesta, data_only=True)
    try:
        paleta = _paleta_motivu(wb)
        listy = [(ws.title, *_html_listu(ws, paleta)) for ws in wb.worksheets
                 if ws.sheet_state == 'visible']
    finally:
        wb.close()
    with _cache_zamek:
        _cache[cesta] = (mt, listy)
    return listy


# ---------------------------------------------------------------- úprava řádků
# Mění se jen hodnoty existujících buněk — řádky, sloupce, záhlaví, styly, sloučení a obrázky
# zůstanou, jak byly. Soubor se čte bez data_only, ukládá přes dočasný soubor a os.replace.

_zapis_zamek = threading.Lock()
_CISLO_VSTUP = re.compile(r'-?(?:\d+(?:[.,]\d*)?|[.,]\d+)')


class _Konflikt(Exception):
    """Soubor se od otevření formuláře změnil."""


def _typ(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 'bool'
    if isinstance(v, (int, float)):
        return 'cislo'
    if isinstance(v, (datetime.datetime, datetime.date)):
        return 'datum'
    if isinstance(v, datetime.time):
        return 'cas'
    return 'text'


def _do_editoru(v) -> str:
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'PRAVDA' if v else 'NEPRAVDA'
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v).replace('.', ',')
    if isinstance(v, datetime.datetime):
        return v.strftime('%d.%m.%Y' if v.time() == datetime.time() else '%d.%m.%Y %H:%M')
    if isinstance(v, datetime.date):
        return v.strftime('%d.%m.%Y')
    if isinstance(v, datetime.time):
        return v.strftime('%H:%M')
    return str(v)


def _z_editoru(t: str, typ: str | None):
    """Text z formuláře → hodnota buňky stejného typu, jaký buňka (nebo sloupec) měla.
    Co nejde převést, uloží se jako text."""
    t = ILLEGAL_CHARACTERS_RE.sub('', t).strip()
    if not t:
        return None
    if typ == 'cislo':
        n = re.sub(r'\s', '', t)
        if _CISLO_VSTUP.fullmatch(n) and not re.fullmatch(r'-?0\d+', n):    # úvodní nula = kód, ne číslo
            n = n.replace(',', '.')
            return int(n) if re.fullmatch(r'-?\d+', n) else float(n)
    elif typ == 'datum':
        d = re.sub(r'\s*\.\s*', '.', t)
        for f in ('%d.%m.%Y %H:%M', '%d.%m.%Y'):
            try:
                return datetime.datetime.strptime(d, f)
            except ValueError:
                pass
    elif typ == 'cas':
        try:
            return datetime.datetime.strptime(t, '%H:%M').time()
        except ValueError:
            pass
    elif typ == 'bool' and t.upper() in ('PRAVDA', 'NEPRAVDA'):
        return t.upper() == 'PRAVDA'
    return t


def _ma_vzorce(wb) -> bool:
    return any(b.data_type == 'f' for ws in wb.worksheets for radek in ws.iter_rows() for b in radek)


def _pole_radku(cesta: str, list_nazev: str, r: int, popis: dict) -> list[dict]:
    """Buňky řádku r pro formulář. Záznam = všechny sloupce tabulky (i prázdné buňky);
    jiný řádek (titulek, poznámka) = jen jeho neprázdné buňky."""
    wb = openpyxl.load_workbook(cesta)
    try:
        if _ma_vzorce(wb):
            raise ValueError('soubor obsahuje vzorce — po uložení by se ztratily jejich vypočtené '
                             'hodnoty. Uprav ho v Excelu a nahraj novou verzi.')
        ws = wb[list_nazev]
        rozsahy = {}                            # sloupec → sloučený rozsah, kam buňka (r, sloupec) patří
        for m in ws.merged_cells.ranges:
            if m.min_row <= r <= m.max_row:
                for c in range(m.min_col, m.max_col + 1):
                    rozsahy[c] = m
        zaznam = r in popis['zaznamy']
        sloupce = (popis['sloupce'] if zaznam
                   else [(c, f'Sloupec {get_column_letter(c)}') for c in popis['viditelne']])
        pole, videno = [], set()
        for c, nadpis in sloupce:
            m = rozsahy.get(c)
            kr, kc = (m.min_row, m.min_col) if m else (r, c)
            if (kr, kc) in videno:
                continue
            b = ws.cell(kr, kc)
            if not zaznam and not _text(b.value):
                continue
            videno.add((kr, kc))
            typ = _typ(b.value) or (Counter(t for x in popis['zaznamy']
                                            if (t := _typ(ws.cell(x, kc).value))).most_common(1)
                                    or [('text',)])[0][0]
            text = _do_editoru(b.value)
            bunky = [(b.coordinate, b.value)]
            napoveda = (f'Sloučená buňka {m.coord} — změna platí pro řádky {m.min_row}–{m.max_row}.'
                        if m and m.max_row > m.min_row else None)
            if not m and zaznam and (rr := popis['sdilene'].get((r, c))):
                dalsi = [ws.cell(x, c) for x in rr if x != r]
                bunky += [(d.coordinate, d.value) for d in dalsi if d.value == b.value]
                if len(bunky) > 1:
                    napoveda = (f'Stejná hodnota je v řádcích {", ".join(str(x) for x in rr)} '
                                f'— změna se zapíše do všech.')
            pole.append({
                'bunky': bunky, 'nadpis': nadpis, 'puvodni': b.value, 'text': text,
                'typ': typ, 'dlouhy': '\n' in text or len(text) > 70, 'napoveda': napoveda,
            })
        return pole
    finally:
        wb.close()


def _uloz_zmeny(cesta: str, list_nazev: str, zmeny: list[tuple[str, object, object]],
                mt: float | None) -> None:
    """zmeny = [(souřadnice, původní hodnota, nová hodnota)]. Když se soubor mezitím změnil
    (jiná úprava, nahraná verze), nic se neuloží a vyhodí se _Konflikt."""
    doc = cesta + '.uprava.tmp'
    with _zapis_zamek:
        if _mtime(cesta) != mt:
            raise _Konflikt
        try:
            wb = openpyxl.load_workbook(cesta)
            try:
                if _ma_vzorce(wb):
                    raise ValueError('soubor obsahuje vzorce')
                ws = wb[list_nazev]
                for sour, puvodni, nova in zmeny:
                    b = ws[sour]
                    if b.value != puvodni:
                        raise _Konflikt
                    b.value = nova
                    if isinstance(nova, str) and nova.startswith('='):
                        b.data_type = 's'           # text začínající „=“, ne vzorec
                wb.save(doc)
            finally:
                wb.close()
            os.replace(doc, cesta)
        finally:
            if os.path.exists(doc):
                os.remove(doc)


_CSS = '''
.kasm { --k-bg:#fff; --k-head:#f8fafc; --k-line:#e2e8f0; --k-line2:#eef2f6; --k-text:#334155;
        --k-strong:#0f172a; --k-muted:#64748b; --k-faint:#94a3b8; --k-hover:#f8fafc; --k-link:#2563eb;
        --k-info-bg:#f1f5f9; --k-info-fg:#334155; --k-warn-bg:#fef2f2; --k-warn-fg:#b91c1c;
        color:var(--k-text); }
body.body--dark .kasm { --k-bg:#1e293b; --k-head:#223047; --k-line:#334155; --k-line2:#2a384d;
        --k-text:#cbd5e1; --k-strong:#f1f5f9; --k-muted:#94a3b8; --k-faint:#64748b; --k-hover:#243349;
        --k-link:#60a5fa; --k-info-bg:#263449; --k-info-fg:#cbd5e1; --k-warn-bg:rgba(239,68,68,.14);
        --k-warn-fg:#fca5a5; }
.kasm-karta { background:var(--k-bg); border:1px solid var(--k-line); border-radius:12px; overflow:hidden;
              box-shadow:0 1px 2px rgba(15,23,42,.04); margin:0; }
.kasm-karta + .kasm-karta { margin-top:16px; }
.kasm-hlava { padding:14px 20px; border-bottom:1px solid var(--k-line); }
.kasm-nadpis { font-size:15px; line-height:22px; font-weight:600; color:var(--k-strong); }
.kasm-podnadpis { font-size:13px; line-height:20px; color:var(--k-muted); }
.kasm-posuv { overflow-x:auto; }
.kasm-tab { width:100%; border-collapse:separate; border-spacing:0; font-size:13px; line-height:20px;
            font-variant-numeric:tabular-nums; }
.kasm-tab th { padding:10px 12px; background:var(--k-head); border-bottom:1px solid var(--k-line);
               color:var(--k-muted); font-size:12px; line-height:16px; font-weight:600;
               text-align:left; vertical-align:bottom; }
.kasm-tab td { padding:10px 12px; white-space:nowrap; vertical-align:middle;
               border-top:1px solid var(--k-line2); }
.kasm-tab.husta th, .kasm-tab.husta td { padding-left:10px; padding-right:10px; }
.kasm-tab th.i, .kasm-tab td.i { width:1%; padding-right:0; text-align:right; color:var(--k-faint); }
.kasm-tab th.okl, .kasm-tab td.okl { padding-left:20px; }
.kasm-tab th.okp, .kasm-tab td.okp { padding-right:20px; }
.kasm-tab tbody > tr:first-child > td { border-top-color:var(--k-line); }
.kasm-tab tbody:first-of-type > tr:first-child > td { border-top:0; }
.kasm-tab tbody:hover > tr:not(.kasm-rp) > td { background:var(--k-hover); }
.kasm-tab td.k { font-weight:600; color:var(--k-strong); }
.kasm-tab .n { text-align:right; }
.kasm-tab .nw { white-space:nowrap; }
.kasm-tab .w { white-space:normal; min-width:9rem; max-width:28rem; }
.kasm-tab .wa { white-space:normal; min-width:8rem; }
.kasm-tab .r2 { font-size:12px; line-height:18px; color:var(--k-muted); }
.kasm-kont { max-width:24rem; }
.kasm-kont .r1 { display:flex; align-items:baseline; justify-content:space-between; gap:16px; }
.kasm-kont .jm { font-weight:500; color:var(--k-strong); }
.kasm-tab tr.kasm-rp > td { padding:0 20px 10px; border-top:0; white-space:normal; }
.kasm-tab tbody > tr.kasm-rp:first-child > td { padding-top:10px; border-top:1px solid var(--k-line); }
.kasm a { color:var(--k-link); text-decoration:none; }
.kasm a:hover { text-decoration:underline; }
.kasm-tecka { display:inline-block; width:10px; height:10px; margin-right:10px; border-radius:50%;
              vertical-align:-1px; box-shadow:inset 0 0 0 1px rgba(15,23,42,.18); }
.kasm-tecka.bez { box-shadow:inset 0 0 0 1.5px var(--k-line); }
.kasm-pozn { display:flex; gap:10px; align-items:flex-start; padding:8px 12px; border-radius:8px;
             font-size:13px; line-height:20px; background:var(--k-info-bg); color:var(--k-info-fg); }
.kasm-pozn.warn { background:var(--k-warn-bg); color:var(--k-warn-fg); }
.kasm-pozn .q-icon { font-size:18px; line-height:20px; flex:none; }
.kasm-pata { display:flex; flex-direction:column; gap:8px; padding:12px 20px;
             border-top:1px solid var(--k-line); }
.kasm-obr-telo { padding:16px 20px; }
.kasm-obr img { display:block; max-width:min(100%, 960px); height:auto; margin:0 auto; }
body.body--dark .kasm-obr img { background:#fff; border-radius:8px; padding:8px; }
.kasm-prazdny { padding:48px; text-align:center; color:var(--k-faint); }
tr.kasm-cil > td { box-shadow:inset 0 0 0 9999px rgba(37,99,235,.09); }
div.kasm-cil { border-radius:8px; box-shadow:0 0 0 2px rgba(37,99,235,.45); }
'''


# ---------------------------------------------------------------- UI

def vykresli(user_name: str):
    os.makedirs(SLOZKA, exist_ok=True)
    ui.add_css(_CSS)
    stav = {'mtimes': {}, 'list': {}}   # list: soubor → otevřený list (záložka)

    def _otisk():
        return {n: _mtime(_cesta(n)) for n, _ in SOUBORY}

    async def _nahraj(e, nazev: str, dialog):
        obsah = await e.file.read()
        if not obsah.startswith(b'PK'):
            ui.notify('Soubor není platné XLSX.', type='negative')
            return
        cil = _cesta(nazev)
        doc = cil + '.upload.tmp'
        try:
            # ověřit, že jde otevřít – z paměti (openpyxl odmítá soubory s příponou .tmp)
            openpyxl.load_workbook(io.BytesIO(obsah), read_only=True).close()
            with open(doc, 'wb') as f:
                f.write(obsah)
            with _zapis_zamek:
                os.replace(doc, cil)
        except Exception as ex:
            try:
                os.remove(doc)
            except OSError:
                pass
            ui.notify(f'Soubor nejde načíst: {ex}', type='negative')
            return
        dialog.close()
        intranet_logger.log_activity(user_name, 'Kontakty Nákupčí', f'nahrána nová verze: {nazev}')
        ui.notify(f'Nahráno: {nazev}', type='positive')
        _obsah.refresh()

    def _dialog_nahrat(nazev: str):
        with ui.dialog() as dlg, ui.card().classes('w-[480px]'):
            ui.label('Nahrát novou verzi').classes('text-lg font-semibold')
            ui.label(f'Nahradí soubor „{nazev}“.').classes('text-sm text-gray-500')
            ui.upload(label='Vybrat soubor (.xlsx)', auto_upload=True,
                      max_file_size=20_000_000,
                      on_upload=lambda e, n=nazev, d=dlg: _nahraj(e, n, d)) \
                .props('accept=".xlsx" flat dense').classes('w-full')
            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg.close).props('flat no-caps')
        dlg.open()

    async def _dialog_radku(nazev: str, list_nazev: str, r: int, popis: dict):
        cesta = _cesta(nazev)
        mt = _mtime(cesta)                  # před čtením — změna během úprav se pozná při uložení
        try:
            pole = await asyncio.to_thread(_pole_radku, cesta, list_nazev, r, popis)
        except Exception as ex:
            ui.notify(f'Řádek nejde upravit: {ex}', type='negative', multi_line=True)
            return
        if not pole:
            ui.notify('Řádek nemá žádné buňky k úpravě.', type='warning')
            return
        vstupy = []
        with ui.dialog() as dlg, ui.card().classes('w-[560px] max-w-full'):
            ui.label('Upravit řádek').classes('text-lg font-semibold')
            ui.label(f'{nazev} · list {list_nazev} · řádek {r}').classes('text-sm text-gray-500')
            with ui.column().classes('w-full gap-3 max-h-[65vh] overflow-y-auto py-1'):
                for p in pole:
                    vstup = (ui.textarea(p['nadpis'], value=p['text']).props('autogrow')
                             if p['dlouhy'] else ui.input(p['nadpis'], value=p['text']))
                    vstup.props('outlined dense' + (' hide-bottom-space' if not p['napoveda'] else ''))
                    if p['napoveda']:
                        vstup.props(f'hint="{html.escape(p["napoveda"])}"')
                    vstup.classes('w-full')
                    vstupy.append((p, vstup))

            async def _ulozit():
                zmeny = []
                for p, vstup in vstupy:
                    text = vstup.value or ''
                    if text == p['text']:
                        continue
                    nova = _z_editoru(text, p['typ'])
                    if nova != p['puvodni']:
                        zmeny += [(sour, puvodni, nova) for sour, puvodni in p['bunky']]
                if not zmeny:
                    dlg.close()
                    return
                ulozit.disable()
                try:
                    await asyncio.to_thread(_uloz_zmeny, cesta, list_nazev, zmeny, mt)
                except _Konflikt:
                    dlg.close()
                    ui.notify('Soubor se mezitím změnil — úprava se neuložila. Otevři řádek znovu.',
                              type='warning', multi_line=True)
                    _obsah.refresh()
                    return
                except PermissionError:
                    ulozit.enable()
                    ui.notify('Soubor nejde přepsat — nejspíš je otevřený v jiném programu.',
                              type='negative')
                    return
                except Exception as ex:
                    ulozit.enable()
                    ui.notify(f'Uložení selhalo: {ex}', type='negative', multi_line=True)
                    return
                dlg.close()
                intranet_logger.log_activity(
                    user_name, 'Kontakty Nákupčí',
                    f'upraven řádek {r} ({nazev} / {list_nazev}): ' + ', '.join(z[0] for z in zmeny))
                ui.notify('Uloženo', type='positive')
                _obsah.refresh()

            with ui.row().classes('w-full justify-end gap-2 pt-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                ulozit = ui.button('Uložit', icon='save', on_click=_ulozit).props('unelevated no-caps')
        dlg.on('hide', dlg.delete)
        dlg.open()

    def _list_html(nazev: str, list_nazev: str, obsah: str, popis: dict):
        # Pravé kliknutí (na mobilu dlouhé podržení) na řádek → nabídka „Upravit“. Mimo řádek,
        # na odkazu nebo s označeným textem zůstane nabídka prohlížeče (kopírování apod.).
        obal = ui.element('div').classes('w-full')
        el = f'getHtmlElement({obal.id})'
        zrus = f'{el}.querySelectorAll(".kasm-cil").forEach(x => x.classList.remove("kasm-cil"));'
        oznac = f'{el}._kasmCil?.classList.add("kasm-cil");'

        def _cil(zvyraznit: bool) -> str:
            return ('(e) => { const t = e.target.closest("[data-r]");'
                    ' if (!t || e.target.closest("a") || String(window.getSelection())) { e.stopPropagation(); return; }'
                    f' {el}._kasmCil = t; ' + (zrus + oznac if zvyraznit else '') + ' }')

        with obal:
            tabulka = ui.html(obsah, sanitize=False).classes('w-full')
            tabulka.on('contextmenu', js_handler=_cil(True))     # otevřená nabídka se jen přesune → přeznačit hned
            tabulka.on('touchstart', js_handler=_cil(False))     # dotyk může být jen scroll → označí až 'show'
            with ui.context_menu() as menu:
                ui.menu_item('Upravit').on(
                    'click', lambda e: _dialog_radku(nazev, list_nazev, int(e.args), popis),
                    js_handler=f'() => {{ const t = {el}._kasmCil; if (t) emit(Number(t.dataset.r)); }}')
            menu.on('show', js_handler=f'() => {{ {oznac} }}')
            menu.on('hide', js_handler=f'() => {{ {zrus} }}')

    def _soubor(nazev: str):
        cesta = _cesta(nazev)
        mt = _mtime(cesta)
        with ui.row().classes('w-full items-center gap-x-2 gap-y-1 mb-3 flex-wrap'):
            ui.space()
            if mt:
                ui.button('Stáhnout', icon='download',
                          on_click=lambda: ui.download.file(cesta, nazev)).props('flat no-caps')
            ui.button('Nahrát novou verzi', icon='upload',
                      on_click=lambda: _dialog_nahrat(nazev)).props('flat no-caps')
        if not mt:
            with ui.column().classes('items-center py-16 gap-2 w-full'):
                ui.icon('upload_file', size='3rem', color='grey-4')
                ui.label('Soubor zatím není nahraný.').classes('text-gray-400')
            return
        try:
            listy = _nacti(cesta)
        except Exception as ex:
            ui.label(f'Soubor nejde načíst: {ex}').classes('text-red-600')
            return
        if not listy:
            ui.label('Soubor nemá žádný viditelný list.').classes('text-gray-400')
        elif len(listy) == 1:
            _list_html(nazev, *listy[0])
        else:
            otevreny = stav['list'].get(nazev)
            otevreny = otevreny if otevreny in [jm for jm, *_ in listy] else listy[0][0]
            with ui.tabs(value=otevreny, on_change=lambda e: stav['list'].update({nazev: e.value})) \
                    .props('dense align=left no-caps').classes('text-gray-600') as t:
                for jm, *_ in listy:
                    ui.tab(jm)
            with ui.tab_panels(t, value=otevreny).classes('w-full bg-transparent'):
                for jm, obsah, popis in listy:
                    with ui.tab_panel(jm).classes('p-0 pt-3'):
                        _list_html(nazev, jm, obsah, popis)

    vybrana = {'tab': SOUBORY[0][0]}

    @ui.refreshable
    def _obsah():
        stav['mtimes'] = _otisk()
        with ui.tabs(value=vybrana['tab'],
                     on_change=lambda e: vybrana.update(tab=e.value)).props(
                'align=left active-color=primary indicator-color=primary no-caps'
        ).classes('w-full border-b border-gray-200') as tabs:
            for nazev, popisek in SOUBORY:
                ui.tab(nazev, label=popisek)
        with ui.tab_panels(tabs, value=vybrana['tab']).classes('w-full pt-4 bg-transparent'):
            for nazev, _ in SOUBORY:
                with ui.tab_panel(nazev).classes('p-0'):
                    _soubor(nazev)

    def _hlidej():
        if _otisk() != stav['mtimes']:
            _obsah.refresh()

    _obsah()
    ui.timer(KONTROLA_S, _hlidej)
