"""Mikulášský vzhled přihlašovací obrazovky — zimní noc se sněžením, srpkem měsíce (na něm
čert chytá na lízátko), padajícími hvězdami, krystaly vloček a třpytkami; zasněžený les,
vesnička s kostelíkem a čertí oči ve tmě. Nahoře jedlové větve s ozdobami v rozích a světelný
řetěz s koulemi, perníčky a ochranovskými hvězdami. V popředí Mikuláš s berlou a košem
nadílky, čert vykukující z pytle, anděl s hvězdou, lucerny, zvonek na stuze, ozdobený
stromek, sněhulák, sáňky s dárky a krmelec se srnkou a zajícem; popředím táhne čert
pytel brambor JIP a kolem skotačí čertík s vidlemi. Na polích formuláře sníh s rampouchy. Kdo jezdí myší, najde vtípky: Mikuláš se zeptá „Byl jsi hodný?", čert vyskočí
s „Bubu!", čert na měsíci se ptá „Kdo zlobil?", sněhulákovi nadskočí hrnec, anděl rozsype
hvězdičky, zvonek zacinká, srnka zvedne hlavu, čert
s pytlem se zastaví („Brambory pro zlobivé!") a nadílka i dárky poskočí.

Zapíná se sám 1.–6. prosince (`je_mikulas()`), mimo ně se nepošle ani řádek.
Audit konzole ho přepíná ručně: /mikulas on|off vynutí stav bez ohledu na
datum, /mikulas auto vrátí řízení datu (`nastav_rezim()`). Kdyby byl zapnutý
zároveň halloween, má přednost halloween (rozhoduje intranet.py).
Jen inline SVG + CSS animace, žádné soubory ani JS navíc. Styly jsou scoped
na `body.mikulas`, kterou nasazuje až `vykresli_dekorace()` na přihlašovací
stránce — bránu změny hesla ani hostovský vstup do kvízu to nezasáhne.
"""
import math
import random
from datetime import date
from urllib.parse import quote

from nicegui import ui

import intranet_data

# Klíč v nastavení portálu (nastaveni_intranetu.json): True/False = ruční
# přepínač z audit konzole, klíč chybí = automatika podle data.
_KLIC_NASTAVENI = 'mikulas'


def je_mikulas(dnes: date | None = None) -> bool:
    """Ruční přepínač z konzole má přednost; jinak běží 1.–6. prosince
    (Mikuláš chodí večer 5. 12.) a 7. prosince se sám vypne."""
    rucne = intranet_data.nacti_nastaveni_intranetu().get(_KLIC_NASTAVENI)
    if isinstance(rucne, bool):
        return rucne
    dnes = dnes or date.today()
    return dnes.month == 12 and dnes.day <= 6


def nastav_rezim(zapnout: bool | None) -> None:
    """/mikulas on|off|auto — True/False vynutí stav, None vrátí automatiku."""
    n = intranet_data.nacti_nastaveni_intranetu()
    if zapnout is None:
        n.pop(_KLIC_NASTAVENI, None)
    else:
        n[_KLIC_NASTAVENI] = bool(zapnout)
    intranet_data.uloz_nastaveni_intranetu(n)


# ===== Drobné tvary =====

def _hvezda5(r: float = 10, r2: float = 4.2) -> str:
    """Pěticípá hvězda se středem v (0, 0)."""
    body = []
    for i in range(10):
        u = math.radians(-90 + 36 * i)
        rr = r if i % 2 == 0 else r2
        body.append(f'{rr * math.cos(u):.1f} {rr * math.sin(u):.1f}')
    return 'M' + 'L'.join(body) + 'Z'


_HVEZDA5 = _hvezda5()


def _srst(cx: float, cy: float, r: float, zuby: int = 22, hloubka: float = 3) -> str:
    """Chlupatý obrys (čertova hlava): kruh se zoubky."""
    body = []
    for i in range(zuby * 2):
        u = 2 * math.pi * i / (zuby * 2)
        rr = r + (hloubka if i % 2 == 0 else 0)
        body.append(f'{cx + rr * math.cos(u):.1f} {cy + rr * math.sin(u):.1f}')
    return 'M' + 'L'.join(body) + 'Z'


# ===== Jedlové větve v horních rozích (s ozdobami) =====

def _bezier2(p0, c, p1, t: float):
    """Bod (x, y) a směrový úhel kvadratické Bézierovy křivky v parametru t."""
    s = 1 - t
    x = s * s * p0[0] + 2 * t * s * c[0] + t * t * p1[0]
    y = s * s * p0[1] + 2 * t * s * c[1] + t * t * p1[1]
    return x, y, math.atan2(s * (c[1] - p0[1]) + t * (p1[1] - c[1]), s * (c[0] - p0[0]) + t * (p1[0] - c[0]))


def _krivka(x0: float, y0: float, uhel: float, delka: float, ohyb: float):
    """Větev jako kvadratická křivka: start, kontrolní bod (prohnutí kolmo ke směru), konec."""
    u = math.radians(uhel)
    p1 = (x0 + delka * math.cos(u), y0 + delka * math.sin(u))
    return (x0, y0), ((x0 + p1[0]) / 2 - ohyb * math.sin(u), (y0 + p1[1]) / 2 + ohyb * math.cos(u)), p1


# Hlavní větve z levého horního rohu: (x, y, úhel, délka, prohnutí). Pravý roh je zrcadlo.
_VETVE = tuple(_krivka(*v) for v in ((-24, 4, 3, 380, 34), (-22, 18, 27, 270, 26), (-14, -6, 58, 215, -16)))


def _jehlici() -> str:
    """Jedlové větve v rohu jako <g id="mkVetev"> (pevné semínko => pořád stejné): stonky,
    jehličí ve dvou odstínech a sníh na horní straně větví."""
    rnd = random.Random(5)
    vetve = []
    for k in _VETVE:
        delka = math.dist(k[0], k[2])
        vetve.append((k, delka, 1.0))
        for i, t in enumerate((.12, .23, .34, .45, .56, .67, .78, .88)):
            x, y, a = _bezier2(*k, t)
            strana = 1 if i % 2 else -1
            d = delka * (.36 - .22 * t)
            vetve.append((_krivka(x, y, math.degrees(a) + strana * 40, d, 5), d, .8))
    stonky = tmave = svetle = snih = ''
    for (p0, c, p1), delka, m in vetve:
        stonky += f'M{p0[0]:.0f} {p0[1]:.0f}Q{c[0]:.0f} {c[1]:.0f} {p1[0]:.0f} {p1[1]:.0f}'
        n = max(4, int(delka / 3.2))
        for j in range(1, n + 1):
            t = j / n
            x, y, a = _bezier2(p0, c, p1, t)
            dj = (15 - 6 * t) * m * rnd.uniform(.85, 1.1)
            for strana in (-1, 1):
                b = a + strana * math.radians(rnd.uniform(46, 58))
                tmave += f'M{x:.0f} {y:.0f}l{dj * math.cos(b):.0f} {dj * math.sin(b):.0f}'
                if j % 2:
                    b = a + strana * math.radians(34)
                    svetle += f'M{x:.0f} {y:.0f}l{dj * .7 * math.cos(b):.0f} {dj * .7 * math.sin(b):.0f}'
            if j % 4 == 2 and t < .88 and rnd.random() < .7:
                # chomáč sněhu na horní straně větve (mezi chomáči zůstává zelená)
                nx, ny = math.sin(a), -math.cos(a)
                if ny > 0:
                    nx, ny = -nx, -ny
                sx, sy = x + nx * 2, y + ny * 2
                w = dj * rnd.uniform(.55, .85)
                dx, dy = math.cos(a) * w, math.sin(a) * w
                v = rnd.uniform(9, 13)
                snih += (f'M{sx - dx:.0f} {sy - dy:.0f}C{sx - dx * .6 + nx * v:.0f} {sy - dy * .6 + ny * v:.0f} '
                         f'{sx + dx * .6 + nx * v:.0f} {sy + dy * .6 + ny * v:.0f} {sx + dx:.0f} {sy + dy:.0f}'
                         f'Q{sx + nx * 2:.0f} {sy + ny * 2 + 1.5:.0f} {sx - dx:.0f} {sy - dy:.0f}Z')
    return ('<g id="mkVetev" stroke-linecap="round" fill="none">'
            f'<path d="{stonky}" stroke="#4a3220" stroke-width="2.6"/>'
            f'<path d="{tmave}" stroke="#0e3a2b" stroke-width="2.6"/>'
            f'<path d="{svetle}" stroke="#3d8757" stroke-width="1.8"/>'
            f'<path d="{snih}" fill="#eef4fd" stroke="none"/></g>')


def _bod_vetve(i: int, t: float):
    x, y, _ = _bezier2(*_VETVE[i], t)
    return x, y


def _koule_telo(x: float, y: float, r: float, barva: str) -> str:
    """Skleněná koule (barva = Cervena/Zlata/Modra/Zelena): lesk, odlesk a pruh teček."""
    return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="url(#mkKoule{barva})"/>'
            f'<path d="M{x - r:.1f} {y:.1f}Q{x:.1f} {y + r * .4:.1f} {x + r:.1f} {y:.1f}" stroke="#fff" '
            f'stroke-opacity=".6" stroke-width="{r * .13:.1f}" stroke-dasharray="{r * .02:.2f} {r * .26:.1f}" '
            'stroke-linecap="round" fill="none"/>'
            f'<ellipse cx="{x - r * .38:.1f}" cy="{y - r * .38:.1f}" rx="{r * .3:.1f}" ry="{r * .17:.1f}" fill="#fff" '
            f'opacity=".75" transform="rotate(-40 {x - r * .38:.1f} {y - r * .38:.1f})"/>')


def _houp(x: float, y: float, zp: float, obsah: str) -> str:
    """Ozdoba zavěšená na větvi v bodě (x, y) — houpe se kolem úchytu."""
    return (f'<g class="mk-houp" style="transform-origin:{x:.0f}px {y:.0f}px;animation-delay:-{zp}s">'
            f'{obsah}</g>')


def _koule_na_vetvi(i: int, t: float, nit: float, r: float, barva: str, zp: float) -> str:
    x, y = _bod_vetve(i, t)
    return _houp(x, y, zp, f'<path d="M{x:.0f} {y:.0f}V{y + nit:.0f}" stroke="#e6cf8f" stroke-width="1.2"/>'
                           f'<rect x="{x - r * .3:.1f}" y="{y + nit - 1.5:.1f}" width="{r * .6:.1f}" '
                           f'height="{r * .3 + 2:.1f}" rx="1" fill="url(#mkZlato)"/>'
                           + _koule_telo(x, y + nit + r, r, barva))


def _siska(i: int, t: float, nit: float, zp: float) -> str:
    x, y = _bod_vetve(i, t)
    cy = y + nit + 10
    supiny = ''.join(f'M{x - 5.5 + abs(k - 2) * .8:.1f} {cy - 6 + k * 3.4:.1f}'
                     f'q{5.5 - abs(k - 2) * .8:.1f} 3 {11 - abs(k - 2) * 1.6:.1f} 0' for k in range(5))
    return _houp(x, y, zp, f'<path d="M{x:.0f} {y:.0f}V{y + nit:.0f}" stroke="#e6cf8f" stroke-width="1.2"/>'
                           f'<ellipse cx="{x:.0f}" cy="{cy:.0f}" rx="6.5" ry="10.5" fill="#6b4423"/>'
                           f'<path d="{supiny}" stroke="#b07a42" stroke-width="1.6" fill="none"/>'
                           f'<path d="M{x - 5:.0f} {cy - 8:.0f}q5 -3 10 0" stroke="#fff" stroke-width="2.4" '
                           'fill="none" stroke-linecap="round"/>')


def _hulka_na_vetvi(i: int, t: float) -> str:
    """Cukrová hůlka zaháknutá obloukem přes větev."""
    x, y = _bod_vetve(i, t)
    d = f'M{x + 6:.0f} {y + 28:.0f}V{y + 6:.0f}a6 6 0 0 0 -12 0'
    return (f'<path d="{d}" stroke="#fff" stroke-width="4" fill="none" stroke-linecap="round"/>'
            f'<path d="{d}" stroke="#d62828" stroke-width="4" fill="none" stroke-dasharray="3 3.5"/>')


def _masle(x: float, y: float, s: float = 1.0) -> str:
    return (f'<g transform="translate({x} {y}) scale({s})">'
            '<path d="M0 0L-9 24L-4 21L-1 26L2 3ZM0 0L11 22L6 21L4 26L-1 3Z" fill="#a8121c"/>'
            '<path d="M0 0C-8 -14 -24 -12 -21 -2C-19 6 -8 4 0 0ZM0 0C8 -14 24 -12 21 -2C19 6 8 4 0 0Z" fill="#d42a35"/>'
            '<path d="M-3 -2C-9 -8 -16 -8 -17 -3M3 -2C9 -8 16 -8 17 -3" stroke="#ff6b74" stroke-width="1.6" '
            'fill="none" stroke-linecap="round"/><ellipse rx="4" ry="4.6" fill="#b5151f"/></g>')


# Ozdoby na větvích: (větev, t na větvi, …). Pravý roh je zrcadlený, ozdoby má jiné.
_ROH_L = (_hulka_na_vetvi(1, .3) + _koule_na_vetvi(0, .48, 20, 11, 'Cervena', 0) + _siska(0, .68, 6, 1.1)
          + _koule_na_vetvi(1, .62, 14, 9, 'Zlata', .7) + _koule_na_vetvi(2, .68, 12, 10, 'Modra', 1.6)
          + _koule_na_vetvi(0, .86, 26, 8, 'Zelena', 2.2) + _masle(34, 24))
_ROH_P = (_koule_na_vetvi(0, .42, 14, 10, 'Zlata', .4) + _hulka_na_vetvi(0, .64)
          + _koule_na_vetvi(1, .55, 22, 11, 'Cervena', 1.3) + _siska(2, .62, 5, .9)
          + _koule_na_vetvi(0, .82, 18, 9, 'Modra', 2) + _koule_na_vetvi(2, .9, 10, 7, 'Zelena', .2)
          + _masle(34, 24))


def _smrk(x: float, y: float, h: float) -> str:
    """Zasněžený smrk se špičkou nad (x, y-h): tři patra větví, na každém čepice sněhu."""
    w = h * .4
    strom = snih = ''
    for horni, dolni, sirka in ((0, .45, .55), (.24, .72, .78), (.48, 1, 1)):
        yt, yb, s = y - h + horni * h, y - h * .06 - h + dolni * h * .94 + h * .06, w * sirka
        strom += f'M{x:g} {yt:.1f}L{x + s:.1f} {yb:.1f}H{x - s:.1f}Z'
        v = yb - yt
        snih += (f'M{x:g} {yt:.1f}L{x + s * .5:.1f} {yt + v * .5:.1f}'
                 f'Q{x + s * .25:.1f} {yt + v * .4:.1f} {x:g} {yt + v * .47:.1f}'
                 f'Q{x - s * .25:.1f} {yt + v * .4:.1f} {x - s * .5:.1f} {yt + v * .5:.1f}Z'
                 f'M{x - s:.1f} {yb:.1f}Q{x - s * .5:.1f} {yb - 3:.1f} {x:g} {yb - 1.5:.1f}'
                 f'Q{x + s * .5:.1f} {yb - 3:.1f} {x + s:.1f} {yb:.1f}Z')
    return (f'<rect x="{x - 2:g}" y="{y - h * .08:.1f}" width="4" height="{h * .08:.1f}" fill="#20180f"/>'
            f'<path d="{strom}" fill="#14283a"/><path d="{snih}" fill="#e3ecf8" fill-opacity=".92"/>')


def _snih_strecha(x0: float, x1: float, y: float) -> str:
    """Sněhová převis na okapu: řada chomáčků od x0 do x1 na výšce y."""
    d = f'M{x0:g} {y:g}'
    kroky = max(2, round((x1 - x0) / 7))
    s = (x1 - x0) / kroky
    for i in range(kroky):
        a = x0 + i * s
        d += f'Q{a + s / 2:.1f} {y + 4.5:.1f} {a + s:.1f} {y:g}'
    return d


# ===== Nebe: hvězdy, srpek měsíce, padající hvězdy =====
# Všechno leží mimo střední sloupec s formulářem (± 210 px od středu), ať nic neleze pod text.

def _hvezdy(pocet: int = 26) -> str:
    """Hvězdy v horním pásu nebe (pevné semínko => pořád stejná obloha). Nad formulářem
    jen úplně nahoře, kolem měsíce žádné — ten je přezáří."""
    rnd = random.Random(56)
    hvezdy = []
    while len(hvezdy) < pocet:
        x, y = rnd.uniform(1.5, 98.5), rnd.uniform(3, 97)   # % pásu nebe
        if (27 < x < 73 and y > 25) or (74 < x < 95 and y < 55):
            continue
        r = rnd.choice((1.5, 2, 2, 2.5, 3))
        hvezdy.append(f'<i style="left:{x:.1f}%;top:{y:.1f}%;width:{r}px;height:{r}px;'
                      f'--t:{rnd.uniform(1.8, 4.2):.1f}s;--zp:-{rnd.uniform(0, 4):.1f}s"></i>')
    return '<div class="mk-hvezdy">' + ''.join(hvezdy) + '</div>'


_MRAK = ('<svg class="mk-mrak {}" viewBox="0 0 160 40"><path d="M12 36Q2 36 5 28Q8 21 18 22Q20 13 32 14'
         'Q39 5 53 9Q63 2 75 10Q87 6 93 15Q105 11 111 19Q125 17 129 25Q143 23 147 31Q151 36 143 36Z"/></svg>')

# Srpek měsíce (kotouč s maskou) se září; přes něj pomalu táhnou dva mráčky
_MESIC = (
    '<div class="mk-mesic"><svg class="mk-mesic-srpek" viewBox="0 0 100 100">'
    '<circle cx="50" cy="50" r="46" fill="url(#mkMesic)" mask="url(#mkSrp)"/>'
    '<g fill="#cdb67a" fill-opacity=".35" mask="url(#mkSrp)">'
    '<circle cx="22" cy="52" r="6"/><circle cx="34" cy="76" r="4.5"/><circle cx="30" cy="30" r="3"/></g></svg>'
    + _MRAK.format('mk-m1') + _MRAK.format('mk-m2'))   # </div> doplní _HTML (čert + bublina)

# Padající hvězdy: výchozí bod, perioda, zpoždění
_METEORY = (('14vh', '34vw', 13, 3), ('7vh', '80vw', 17, 11), ('22vh', '22vw', 21, 17))


def _meteory() -> str:
    return ''.join(f'<div class="mk-meteor" style="top:{t};left:{l};--t:{p}s;--zp:-{z}s"></div>'
                   for t, l, p, z in _METEORY)


# ===== Sněžení: dvě vrstvy (vzadu drobné a pomalé, vpředu velké, rozmazané a rychlejší) =====

def _snih(pocet: int, semeno: int, trida: str, r: tuple, t: tuple) -> str:
    """Vločky padají (i) a zároveň se vlní do stran (i::before) — dvě nezávislé animace."""
    rnd = random.Random(semeno)
    vlocky = []
    for _ in range(pocet):
        velikost, doba = rnd.uniform(*r), rnd.uniform(*t)
        vlocky.append(f'<i style="left:{rnd.uniform(0, 100):.1f}%;width:{velikost:.1f}px;height:{velikost:.1f}px;'
                      f'--t:{doba:.1f}s;--zp:-{rnd.uniform(0, doba):.1f}s;'
                      f'--k:{rnd.uniform(.5, 2.2):.1f}em;--s:{rnd.uniform(2.4, 5):.1f}s"></i>')
    return f'<div class="mk-snih {trida}">' + ''.join(vlocky) + '</div>'


# ===== Zem a vesnička =====

# Zadní kopec: tři kubické úseky (x 0–1000, y 0–100 ve viewBoxu vrstvy 12em vysoké).
# Les na něm stojí přesně na hřebeni, proto se výška počítá ze stejných bodů.
_ZADNI_KOPEC = (((0, 34), (110, 12), (240, 20), (360, 36)), ((360, 36), (480, 52), (600, 14), (740, 20)),
                ((740, 20), (860, 25), (940, 42), (1000, 34)))


def _kopec_y(x: float) -> float:
    """Výška zadního kopce (jednotky viewBoxu) nad bodem x (0–1000)."""
    for p0, p1, p2, p3 in _ZADNI_KOPEC:
        if x <= p3[0]:
            lo, hi = 0.0, 1.0
            for _ in range(24):
                t = (lo + hi) / 2
                bx = (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t * t * p2[0] + t ** 3 * p3[0]
                lo, hi = (t, hi) if bx < x else (lo, t)
            return (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t * t * p2[1] + t ** 3 * p3[1]
    return _ZADNI_KOPEC[-1][3][1]


_ZEME_ZADNI = ('<svg class="mk-zeme" viewBox="0 0 1000 100" preserveAspectRatio="none"><path d="M0 34'
               + ''.join(f'C{p1[0]} {p1[1]} {p2[0]} {p2[1]} {p3[0]} {p3[1]}' for _, p1, p2, p3 in _ZADNI_KOPEC)
               + 'V100H0Z" fill="url(#mkZemeZadni)"/></svg>')
_ZEME_PREDNI = ('<svg class="mk-zeme" viewBox="0 0 1000 100" preserveAspectRatio="none">'
                '<path d="M0 70C150 52 320 60 480 74C640 86 800 60 1000 66V100H0Z" fill="url(#mkZeme)"/></svg>')

_SMRCEK = ('<g id="mkSmrcek"><rect x="18" y="62" width="4" height="8" fill="#121c2c"/>'
           '<path d="M20 0L30 20H25L35 40H29L40 63H0L11 40H5L15 20H10Z" fill="#17304a"/>'
           '<path d="M20 0L25.5 11Q20 8.5 14.5 11ZM10 20Q20 15 30 20L28 23Q20 18.5 12 23ZM5 40Q20 34 35 40L33 43'
           'Q20 37.5 7 43ZM0 63Q20 57 40 63L38 65Q20 60 2 65Z" fill="#cfdcf0"/></g>')


def _les() -> str:
    """Řada zasněžených smrčků po hřebeni zadního kopce."""
    rnd = random.Random(31)
    stromy, x = [], rnd.uniform(0, 2)
    while x < 100:
        w = rnd.uniform(1.5, 3.2)
        dole = 12 - .12 * _kopec_y(x * 10) - w * .3
        stromy.append(f'<svg class="mk-les-strom" viewBox="0 0 40 70" style="left:{x:.1f}%;bottom:{dole:.2f}em;'
                      f'width:{w:.1f}em"><use href="#mkSmrcek"/></svg>')
        x += rnd.uniform(1.6, 3.8)
    return '<div class="mk-les">' + ''.join(stromy) + '</div>'

# Chaloupky: okna svítí (jedno občas zhasne), z komína se kouří
_DYM = ''.join(f'<circle class="mk-dym" cx="{{x}}" cy="{{y}}" r="{r}" style="animation-delay:-{z}s"/>'
               for r, z in ((4, 0), (5, 1.3), (4.5, 2.6)))

_CHALOUPKY = f'''<svg class="mk-vesnice" viewBox="0 0 270 140">
  {_smrk(16, 140, 62)}
  <g fill="#c9d6ea" fill-opacity=".55">{_DYM.format(x=104, y=44)}</g>
  <rect x="98" y="46" width="12" height="20" fill="#3a2a2a"/>
  <path d="M96 48Q104 41 112 48Z" fill="#eef4fc"/>
  <rect x="40" y="82" width="80" height="58" fill="#252b42"/>
  <path d="M40 98H120M40 114H120M40 130H120" stroke="#1b2033" stroke-width="2"/>
  <path d="M30 86L80 44L130 86Z" fill="#1a2034"/>
  <path d="M27 87L80 40L133 87Z" fill="url(#mkSnihStrecha)"/>
  <path d="{_snih_strecha(27, 133, 87)}" fill="#dfe8f6"/>
  <g filter="url(#mkZar)" fill="#ffc861">
    <rect x="50" y="98" width="18" height="17" rx="1.5"/>
    <rect class="mk-okno-zhasina" x="94" y="98" width="18" height="17" rx="1.5"/>
    <path d="M80 70a5 5 0 0 1 0 -10a5 5 0 0 1 0 10Z" fill="#ffd98a"/>
  </g>
  <path d="M59 98V115M50 106.5H68M103 98V115M94 106.5H112" stroke="#3b2b20" stroke-width="2"/>
  <path d="M48 116H70M92 116H114" stroke="#eef4fc" stroke-width="3" stroke-linecap="round"/>
  <rect x="74" y="114" width="14" height="26" rx="1" fill="#3b2b20"/>
  {_smrk(140, 140, 50)}
  <g fill="#c9d6ea" fill-opacity=".5">{_DYM.format(x=226, y=70)}</g>
  <rect x="221" y="72" width="10" height="16" fill="#3a2a2a"/>
  <path d="M219 74Q226 68 233 74Z" fill="#eef4fc"/>
  <rect x="166" y="102" width="62" height="38" fill="#232840"/>
  <path d="M158 105L197 74L236 105Z" fill="#1a2034"/>
  <path d="M156 106L197 71L238 106Z" fill="url(#mkSnihStrecha)"/>
  <path d="{_snih_strecha(156, 238, 106)}" fill="#dfe8f6"/>
  <g filter="url(#mkZar)" fill="#ffc861">
    <rect x="176" y="114" width="15" height="14" rx="1.5"/><rect x="204" y="114" width="15" height="14" rx="1.5"/>
  </g>
  <path d="M183.5 114V128M176 121H191M211.5 114V128M204 121H219" stroke="#3b2b20" stroke-width="1.8"/>
  {_smrk(256, 140, 44)}
</svg>'''

# Kostelík s cibulovou věží, ciferníkem a rozsvícenými okny
_KOSTEL = f'''<svg class="mk-kostel" viewBox="0 0 190 210">
  <rect x="64" y="130" width="96" height="80" fill="#2b3350"/>
  <path d="M58 134L112 98L166 134Z" fill="#1a2034"/>
  <path d="M55 135L112 95L169 135Z" fill="url(#mkSnihStrecha)"/>
  <path d="{_snih_strecha(55, 169, 135)}" fill="#dfe8f6"/>
  <g filter="url(#mkZar)" fill="#ffc861">
    <path d="M80 182V160a6 6 0 0 1 12 0V182Z"/><path d="M106 182V160a6 6 0 0 1 12 0V182Z"/>
    <path class="mk-okno-zhasina" style="animation-delay:-5s" d="M132 182V160a6 6 0 0 1 12 0V182Z"/>
  </g>
  <rect x="30" y="80" width="34" height="130" fill="#323b5a"/>
  <path d="M30 80V210" stroke="#252c45" stroke-width="2"/>
  <g filter="url(#mkZar)" fill="#ffc861">
    <path d="M40 210V193a7 7 0 0 1 14 0V210Z" fill="#ffb84a"/>
    <path d="M42 104V94a5 5 0 0 1 10 0V104Z"/>
  </g>
  <circle cx="47" cy="124" r="8" fill="#e9e2c8"/>
  <path d="M47 124V118.5M47 124L51 126" stroke="#2b3350" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M26 82H68C68 72 60 66 56 60C62 54 60 44 47 36C34 44 32 54 38 60C34 66 26 72 26 82Z" fill="#34416a"/>
  <path d="M47 36C40 40 36 46 37 52Q42 46 47 45Q52 46 57 52C58 46 54 40 47 36Z" fill="#e3ecf8"/>
  <path d="M25 84Q27 78 32 79H62Q67 78 69 84Q63 87 58 84Q52 88 47 84Q42 88 36 84Q31 87 25 84Z" fill="#e3ecf8"/>
  <path d="M47 36V16M41.5 21H52.5" stroke="#f0c24a" stroke-width="2.4" stroke-linecap="round"/>
  {_smrk(176, 210, 66)}
</svg>'''


def _oci(x: str, y: str, barva: str, perioda: float, zpozdeni: float, sirka: float = 2.6) -> str:
    """Čertí oči ve tmě: občas se vynoří, mrknou, rozhlédnou se a zase zmizí.
    Zornice jsou oříznuté tvarem oka (#mkDulky), ať při rozhlížení nevylézají ven."""
    return (f'<div class="mk-tma-oci" style="left:{x};top:{y};width:{sirka}em;--barva:{barva};'
            f'--t:{perioda}s;--zp:-{zpozdeni}s"><svg viewBox="0 0 40 12"><g class="mk-mrk">'
            '<path d="M2 4Q9 2 14 7Q8 11.5 2 4ZM38 4Q31 2 26 7Q32 11.5 38 4Z"/>'
            '<g clip-path="url(#mkDulky)"><g class="mk-zornice">'
            '<ellipse cx="8.5" cy="6.5" rx="1.1" ry="2.8"/><ellipse cx="31.5" cy="6.5" rx="1.1" ry="2.8"/>'
            '</g></g></g></svg></div>')


# ===== Postavy =====

# Mikuláš: bílá alba, červený pluviál se zlatým lemem, mitra, berla; pravou rukou mává.
# Najetí myší => kývne hlavou a zeptá se.
_MIKULAS = '''<svg class="mk-mikulas-svg" viewBox="0 0 170 280">
  <path d="M30 278V60" stroke="#c9961f" stroke-width="6.5" stroke-linecap="round"/>
  <path d="M28.6 270V64" stroke="#ffe39a" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M30 64V44C30 24 54 20 57 36C59 47 47 51 43 43C40 37 46 33 49 37"
        fill="none" stroke="#e2ae2c" stroke-width="6" stroke-linecap="round"/>
  <path d="M30 60V44C30 27 51 24 54 36" fill="none" stroke="#fff0bd" stroke-width="1.5" stroke-linecap="round"/>
  <path d="M71 140L60 278H110L99 140Z" fill="url(#mkAlba)"/>
  <ellipse cx="74" cy="277" rx="10" ry="4" fill="#3a2418"/><ellipse cx="96" cy="277" rx="10" ry="4" fill="#3a2418"/>
  <path d="M84 106C62 106 46 116 40 136L22 264Q20 278 34 278H64L74 144Z" fill="url(#mkPlast)"/>
  <path d="M86 106C108 106 124 116 130 136L148 264Q150 278 136 278H106L96 144Z" fill="url(#mkPlast)"/>
  <path d="M74 144L64 277M96 144L106 277" stroke="#f0c24a" stroke-width="6"/>
  <path d="M74 144L64 277M96 144L106 277" stroke="#fff3c0" stroke-width="1.4" stroke-dasharray="2 6"/>
  <path d="M24 263Q21 277 34 277H64M106 277H136Q149 277 146 263" stroke="#f0c24a" stroke-width="5" fill="none"/>
  <path d="M85 160V184M77 168H93" stroke="#f0c24a" stroke-width="4.5" stroke-linecap="round"/>
  <path d="M46 122C36 132 26 150 24 168L40 172C42 156 50 142 58 134Z" fill="url(#mkPlast)"/>
  <path d="M23 167L41 172" stroke="#f3f6fb" stroke-width="6" stroke-linecap="round"/>
  <ellipse cx="31" cy="178" rx="7.5" ry="7" fill="#fff"/>
  <g class="mk-ruka">
    <path d="M112 120C126 116 138 106 145 92L158 100C150 116 136 130 120 136Z" fill="url(#mkPlast)"/>
    <path d="M144 90L159 100" stroke="#f3f6fb" stroke-width="6" stroke-linecap="round"/>
    <ellipse cx="155" cy="84" rx="7.5" ry="9" fill="#fff"/>
    <ellipse cx="147.5" cy="88" rx="3" ry="4.5" fill="#f1f4f9" transform="rotate(-30 147.5 88)"/>
  </g>
  <g class="mk-hlava">
    <circle cx="65" cy="76" r="7.5" fill="#eef2f8"/><circle cx="105" cy="76" r="7.5" fill="#eef2f8"/>
    <circle cx="85" cy="80" r="19" fill="url(#mkPlet)"/>
    <path d="M64 80C59 100 62 122 70 138C74 147 80 155 85 150C90 155 96 147 100 138C108 122 111 100 106 80
             C100 94 93 98 85 98C77 98 70 94 64 80Z" fill="url(#mkVousy)"/>
    <path d="M74 108Q76 122 73 134M85 104V142M96 108Q94 122 97 134" stroke="#d3dbe7" stroke-width="1.5"
          fill="none" stroke-linecap="round"/>
    <path d="M85 90C80 85 70 86 67 94C72 96 80 95 85 92C90 95 98 96 103 94C100 86 90 85 85 90Z" fill="#fff"/>
    <ellipse cx="85" cy="86" rx="4.4" ry="3.7" fill="#ec9f7e"/>
    <circle cx="73" cy="85" r="4" fill="#f08a7a" opacity=".45"/><circle cx="97" cy="85" r="4" fill="#f08a7a" opacity=".45"/>
    <path d="M74 78Q77.5 74.5 81 78M89 78Q92.5 74.5 96 78" stroke="#3b2618" stroke-width="2.2"
          fill="none" stroke-linecap="round"/>
    <path d="M71 71Q76 67 81 70M89 70Q94 67 99 71" stroke="#fff" stroke-width="3.6" stroke-linecap="round" fill="none"/>
    <path d="M64 64L63 38C64 24 74 13 85 6C96 13 106 24 107 38L106 64Z" fill="url(#mkMitra)"
          stroke="#e2b23a" stroke-width="2.5" stroke-linejoin="round"/>
    <path d="M85 8V58" stroke="#e8bb45" stroke-width="5"/>
    <path d="M85 25V45M77 32H93" stroke="#c8202b" stroke-width="4" stroke-linecap="round"/>
    <rect x="62" y="56" width="46" height="11" rx="3" fill="url(#mkZlato)"/>
    <circle cx="73" cy="61.5" r="2.4" fill="#c8202b"/><circle cx="85" cy="61.5" r="2.6" fill="#2a7a4a"/>
    <circle cx="97" cy="61.5" r="2.4" fill="#c8202b"/>
  </g>
</svg>'''

# Koš s nadílkou: pomeranče, jablko, ořechy, bonbon, perníkové srdce a cukrová hůlka
_KOS = '''<svg class="mk-kos-svg" viewBox="0 0 120 96">
  <path d="M16 44C16 2 104 2 104 44" fill="none" stroke="#7d4a1e" stroke-width="6"/>
  <path d="M16 44C16 4 104 4 104 44" fill="none" stroke="#b07a3e" stroke-width="2"/>
  <g class="mk-nadilka">
    <g><path d="M20 42V20a6 6 0 0 1 12 0" stroke="#fff" stroke-width="4.5" fill="none" stroke-linecap="round"/>
       <path d="M20 42V20a6 6 0 0 1 12 0" stroke="#d62828" stroke-width="4.5" fill="none" stroke-dasharray="3 3"/></g>
    <g><circle cx="38" cy="37" r="11" fill="url(#mkPomeranc)"/><path d="M38 26q4-5 9-3q-4 4-9 3z" fill="#3f8f3a"/></g>
    <g><circle cx="60" cy="33" r="11" fill="url(#mkJablko)"/><path d="M60 23q1-5 4-6" stroke="#5a3a1a" stroke-width="2" fill="none"/></g>
    <g><ellipse cx="48" cy="44" rx="6.5" ry="5.5" fill="#a3743f"/><path d="M48 39v10" stroke="#7a5228" stroke-width="1"/></g>
    <g transform="translate(78 29) rotate(-20)"><ellipse rx="7" ry="4.6" fill="#e63946"/>
       <path d="M-7 0l-6-4.5v9zM7 0l6-4.5v9z" fill="#e63946"/><path d="M-2.5-4.2v8.4M1.5-4.6v9.2" stroke="#fff" stroke-width="1.3"/></g>
    <g><circle cx="84" cy="40" r="10" fill="url(#mkPomeranc)"/></g>
    <g><path d="M100 28c-3-6-12-3-10 3 1 5 10 10 10 10s9-5 10-10c2-6-7-9-10-3z" fill="#a0612b"/>
       <path d="M100 31c-2-3-7-2-6 2 1 3 6 6 6 6s5-3 6-6c1-4-4-5-6-2z" fill="none" stroke="#fff" stroke-width="1.1"
             stroke-dasharray="1.6 1.6"/></g>
  </g>
  <path d="M6 44H114L102 90Q100 95 94 95H26Q20 95 18 90Z" fill="url(#mkKos)"/>
  <path d="M10 56H110M13 68H107M16 80H104" stroke="#5e3410" stroke-width="2.2" opacity=".5"/>
  <path d="M26 47L31 94M43 47L46 94M60 47V94M77 47L74 94M94 47L89 94" stroke="#5e3410" stroke-width="1.6" opacity=".4"/>
  <rect x="3" y="40" width="114" height="9" rx="4.5" fill="#c58945" stroke="#7d4a1e" stroke-width="1.5"/>
  <path d="M8 41Q30 36 52 41Q70 37 90 41Q104 37 113 41" stroke="#eef4fc" stroke-width="3" fill="none" stroke-linecap="round"/>
</svg>'''


def _retez(x0: float, x1: float, y: float, prohnuti: float = 5) -> str:
    """Čertův řetěz přes pytel: střídají se články naplocho a nastojato."""
    clanky = []
    n = int((x1 - x0) // 8)
    for i in range(n + 1):
        x = x0 + i * 8
        yy = y + prohnuti * math.sin(math.pi * i / n)
        if i % 2 == 0:
            clanky.append(f'<ellipse cx="{x:g}" cy="{yy:.1f}" rx="5.5" ry="3.3"/>')
        else:
            clanky.append(f'<ellipse cx="{x:g}" cy="{yy:.1f}" rx="5" ry="1.1"/>')
    return f'<g fill="none" stroke="#8d96a8" stroke-width="2.2">{"".join(clanky)}</g>'


# Čert vykukuje z pytle: celý je oříznutý ústím pytle (#mkUsti), takže když se schová, trčí jen
# rohy. Ruce na okraji pytle jsou zvlášť (kreslí se přes přední lem). Z díry v boku mrská ocas.
_CERT_HLAVA = f'''<g class="mk-cert">
    <path d="M54 98C54 82 66 72 82 72C98 72 110 82 110 98Z" fill="url(#mkSrst)"/>
    <path d="M65 44C55 36 52 26 56 14C59 25 66 31 73 35Z" fill="url(#mkRoh)"/>
    <path d="M99 44C109 36 112 26 108 14C105 25 98 31 91 35Z" fill="url(#mkRoh)"/>
    <path d="M60 54L43 47L55 66Z" fill="#8d2f1d"/><path d="M104 54L121 47L109 66Z" fill="#8d2f1d"/>
    <path d="{_srst(82, 58, 23, 20, 3.2)}" fill="url(#mkSrst)"/>
    <ellipse cx="82" cy="63" rx="17" ry="15" fill="url(#mkTvar)"/>
    <ellipse cx="75" cy="57" rx="5" ry="6" fill="#fff3c4"/><ellipse cx="89" cy="57" rx="5" ry="6" fill="#fff3c4"/>
    <g class="mk-cert-zornice"><circle cx="76" cy="58" r="2.6" fill="#1a0f0a"/><circle cx="90" cy="58" r="2.6" fill="#1a0f0a"/></g>
    <path d="M68 49L80 53M96 49L84 53" stroke="#1a0f0a" stroke-width="2.8" stroke-linecap="round"/>
    <ellipse cx="82" cy="64" rx="3.6" ry="2.7" fill="#6e2414"/>
    <path d="M70 68Q82 81 94 68Q82 72 70 68Z" fill="#3a0d08"/>
    <path class="mk-jazyk" d="M79 72C78 81 80 89 84 91C88 89 87 81 85 72Z" fill="#e0405a"/>
    <path d="M74 69.2L76 73.4L77.6 69.8ZM90 69.2L88 73.4L86.4 69.8Z" fill="#fff"/>
  </g>'''

_CERT_RUCE = '''<g class="mk-cert">
    <path d="M47 92C44 82 50 76 57 79L62 92Z" fill="url(#mkSrst)"/>
    <path d="M47 82l-3-3M51 79l-1-4M56 78l1-4" stroke="#f1e3c2" stroke-width="1.6" stroke-linecap="round"/>
    <path d="M117 92C120 82 114 76 107 79L102 92Z" fill="url(#mkSrst)"/>
    <path d="M117 82l3-3M113 79l1-4M108 78l-1-4" stroke="#f1e3c2" stroke-width="1.6" stroke-linecap="round"/>
  </g>'''

_PYTEL = f'''<svg class="mk-pytel-svg" viewBox="0 0 190 200">
  <g class="mk-ocas">
    <path d="M150 150C166 146 172 132 168 120C166 110 172 104 178 106" stroke="#21150f" stroke-width="4.5"
          fill="none" stroke-linecap="round"/>
    <path d="M176 107L190 98L186 114Z" fill="#21150f"/>
  </g>
  <path d="M48 84C30 92 12 112 8 140C4 170 14 196 40 196H124C150 196 160 170 156 140C152 112 134 92 116 84Z"
        fill="url(#mkPytel)"/>
  <path d="M30 120Q34 150 28 182M58 104Q64 150 58 194M100 104Q96 150 102 194M134 120Q130 150 138 182"
        stroke="#6d4a22" stroke-width="1.5" fill="none" opacity=".5"/>
  <path d="M32 146h24v22h-24z" fill="#a87c45" stroke="#5e4220" stroke-width="1.5" stroke-dasharray="3 2"/>
  <ellipse cx="150" cy="150" rx="4" ry="5.5" fill="#3a2612"/>
  <ellipse cx="82" cy="84" rx="34" ry="10" fill="#2a1a0c"/>
  <path d="M48 84A34 10 0 0 1 116 84" stroke="#c9a26a" stroke-width="3" fill="none"/>
  <g clip-path="url(#mkUsti)">{_CERT_HLAVA}</g>
  <path d="M48 84A34 10 0 0 0 116 84" stroke="#d8b37a" stroke-width="4" fill="none"/>
  <path d="M114 88C124 98 118 112 128 120C134 125 132 132 126 134" stroke="#e0c58e" stroke-width="3"
        fill="none" stroke-linecap="round"/>
  <g clip-path="url(#mkUsti)">{_CERT_RUCE}</g>
  {_retez(16, 148, 128, 18)}
</svg>'''

# Anděl s hvězdou na hůlce: vznáší se a mává křídly; najetí myší => rozsype hvězdičky
_KRIDLO = ('<path d="M54 64C42 42 20 32 6 40C12 46 8 50 3 54C12 58 12 62 6 66C16 70 20 74 16 80'
           'C30 80 44 74 54 72Z" fill="url(#mkKridlo)"/>'
           '<path d="M50 66C38 58 26 54 14 54M50 69C40 66 30 66 20 68M48 72C40 72 32 74 26 77" '
           'stroke="#c3d2ec" stroke-width="1.2" fill="none" stroke-linecap="round"/>')

_ANDEL = f'''<svg class="mk-andel-svg" viewBox="0 0 120 140">
  <g class="mk-kridlo-l">{_KRIDLO}</g>
  <g class="mk-kridlo-p"><g transform="matrix(-1 0 0 1 120 0)">{_KRIDLO}</g></g>
  <path d="M52 58C46 78 38 104 32 128Q60 138 88 128C82 104 74 78 68 58Z" fill="url(#mkSaty)"/>
  <path d="M33 127Q60 137 87 127" stroke="#f0c24a" stroke-width="3" fill="none"/>
  <path d="M49 77Q60 81 71 77" stroke="#f0c24a" stroke-width="2.6" fill="none"/>
  <path d="M52 62C46 68 44 76 46 84" stroke="#eef3fb" stroke-width="8" stroke-linecap="round" fill="none"/>
  <circle cx="46" cy="87" r="4" fill="url(#mkPlet)"/>
  <path d="M68 62C76 64 84 60 88 52" stroke="#eef3fb" stroke-width="8" stroke-linecap="round" fill="none"/>
  <path d="M89 50L98 30" stroke="#e2ae2c" stroke-width="2.2" stroke-linecap="round"/>
  <circle cx="89" cy="50" r="4" fill="url(#mkPlet)"/>
  <g transform="translate(99 26)"><path class="mk-hulka" d="{_hvezda5(8, 3.4)}" fill="#ffd34d" stroke="#fff3c0" stroke-width=".8"/></g>
  <circle cx="60" cy="42" r="15" fill="url(#mkVlasy)"/>
  <circle cx="47" cy="50" r="5" fill="url(#mkVlasy)"/><circle cx="73" cy="50" r="5" fill="url(#mkVlasy)"/>
  <circle cx="60" cy="44" r="11.5" fill="url(#mkPlet)"/>
  <path d="M48 42C48 32 56 28 62 29C70 30 74 36 72 42C68 36 64 38 60 35C56 39 52 38 48 42Z" fill="url(#mkVlasy)"/>
  <path d="M54 46Q56 44 58 46M62 46Q64 44 66 46" stroke="#4a3020" stroke-width="1.5" fill="none" stroke-linecap="round"/>
  <circle cx="52.5" cy="49.5" r="2.4" fill="#f08a7a" opacity=".45"/><circle cx="67.5" cy="49.5" r="2.4" fill="#f08a7a" opacity=".45"/>
  <path d="M57.5 50.5Q60 53 62.5 50.5" stroke="#b5523e" stroke-width="1.3" fill="none" stroke-linecap="round"/>
  <ellipse class="mk-svatozar" cx="60" cy="25" rx="12" ry="3.6" fill="none" stroke="#ffd96b" stroke-width="2.6"/>
</svg>'''

# Hvězdičky, které anděl rozsype při najetí myší: cíl letu (x, y)
_JISKRICKY = (('-3.5em', '-2.5em'), ('2.5em', '-3em'), ('4em', '0'), ('3em', '3em'),
              ('-1em', '3.5em'), ('-4em', '1.5em'), ('0', '-4.5em'), ('-2.5em', '-0.5em'))

# Zvonek na červené stuze visí z horního okraje a houpe se; najetí myší => zacinká
_ZVONEK = '''<svg class="mk-zvonek-svg" viewBox="0 0 60 300">
  <path d="M30 0V238" stroke="#c8202b" stroke-width="3.5"/>
  <g class="mk-zvonek-telo">
    <circle class="mk-srdce" cx="30" cy="288" r="4.6" fill="#a8761a"/>
    <path d="M30 240C19 240 14 250 13 262C12 272 9 277 5 281H55C51 277 48 272 47 262C46 250 41 240 30 240Z" fill="url(#mkZvon)"/>
    <rect x="3" y="279" width="54" height="6" rx="3" fill="#d9a321"/>
    <path d="M21 252C18 258 17 266 17 274" stroke="#fff6cf" stroke-width="2.4" stroke-linecap="round" fill="none" opacity=".8"/>
    <path d="M30 238C22 228 10 230 12 238C14 246 24 244 30 238ZM30 238C38 228 50 230 48 238C46 246 36 244 30 238Z" fill="#d42a35"/>
    <path d="M30 238L22 254M30 238L38 254" stroke="#d42a35" stroke-width="4" stroke-linecap="round"/>
    <circle cx="30" cy="238" r="3.4" fill="#a8121c"/>
  </g>
</svg>'''


def _lucerna(sirka: float, zpozdeni: float) -> str:
    """Lucerna se svíčkou; plamen se třese, sklo hřeje."""
    return (f'<svg class="mk-lucerna" style="width:{sirka}em" viewBox="0 0 50 100">'
            '<circle cx="25" cy="7" r="4.5" fill="none" stroke="#2b3140" stroke-width="2.5"/>'
            '<path d="M8 24L25 11L42 24Z" fill="#2b3140"/>'
            '<rect x="11" y="24" width="28" height="56" rx="2" fill="url(#mkSklo)"/>'
            '<rect x="21" y="60" width="8" height="18" rx="1.5" fill="#f6efe0"/>'
            f'<g class="mk-plamen" style="animation-delay:-{zpozdeni}s">'
            '<ellipse cx="25" cy="52" rx="4" ry="7.5" fill="#ffc94a"/>'
            '<ellipse cx="25" cy="54" rx="1.8" ry="3.6" fill="#fff8de"/></g>'
            '<rect x="11" y="24" width="28" height="56" rx="2" fill="none" stroke="#2b3140" stroke-width="3"/>'
            '<path d="M11 42H39" stroke="#2b3140" stroke-width="1.6"/>'
            '<path d="M6 80H44L41 88H9Z" fill="#2b3140"/>'
            '<path d="M7 24Q9 17 17 14Q25 8 33 14Q41 17 43 24Q39 21 35 24Q31 20 26 23Q21 19 16 23Q11 21 7 24Z" fill="#eef4fc"/>'
            '</svg>')


_ZAVEJ = '''<svg class="mk-zavej" viewBox="0 0 1000 40" preserveAspectRatio="none">
  <path d="M0 18C60 6 120 10 170 16C230 24 280 8 340 12C400 16 430 26 500 24C570 22 620 10 690 14C760 18 800 26
           870 18C920 12 960 10 1000 16V40H0Z" fill="url(#mkZavej)"/>
</svg>'''

# ===== Světelný řetěz přes horní okraj a ozdoby, které z něj visí =====
_BARVY_ZAROVEK = ('#ff5252', '#ffd34d', '#4fc3f7', '#7ee081')


def _girlanda() -> str:
    """Řetěz v šesti obloucích přes celou šířku (vrstva 6em vysoká, x v ‰ šířky, y/10 = em).
    Kotvy oblouků jsou na y=3 (0,3em), nejnižší místa na y=42 (4,2em)."""
    d, zarovky = '', []
    for k in range(6):
        a, b = k * 1000 / 6, (k + 1) * 1000 / 6
        c = ((a + b) / 2, 81)
        d += f'M{a:.0f} 3Q{c[0]:.0f} 81 {b:.0f} 3'
        for t in (.14, .32, .5, .68, .86):
            x, y, _ = _bezier2((a, 3), c, (b, 3), t)
            i = len(zarovky)
            zarovky.append(f'<i style="left:{x / 10:.1f}%;top:{y / 10:.2f}em;--b:{_BARVY_ZAROVEK[i % 4]};'
                           f'animation-delay:-{i % 3 * .6:.1f}s"></i>')
    return ('<div class="mk-girlanda"><svg viewBox="0 0 1000 60" preserveAspectRatio="none">'
            f'<path d="{d}" fill="none" stroke="#1d2430" stroke-width="1.8" vector-effect="non-scaling-stroke"/>'
            '</svg>' + ''.join(zarovky) + '</div>')


def _zaves(x: str, top: float, nit: float, sirka: float, svg: str, zp: float, trida: str = '') -> str:
    """Ozdoba na niti (top/nit/sirka v em); houpe se kolem horního konce niti."""
    return (f'<div class="mk-zaves {trida}" style="left:{x};top:{top}em;width:{sirka}em;--t:{2.6 + zp % 1.4:.1f}s;'
            f'animation-delay:-{zp}s"><div class="mk-zaves-nit" style="height:{nit}em"></div>{svg}</div>')


_KOULE_SVG = ('<svg viewBox="-25 -30 50 56"><circle cx="0" cy="-29" r="2.4" fill="none" stroke="#e6cf8f" '
              'stroke-width="1"/><rect x="-5" y="-27" width="10" height="7" rx="1.5" fill="url(#mkZlato)"/>{}</svg>')


def _hvezda_ochranovska() -> str:
    """Ochranovská (moravská) hvězda: osm hrotů vpředu, osm menších za nimi; každý hrot má
    osvětlenou a stinnou stěnu."""
    s = ('<svg viewBox="-50 -54 100 104"><circle cx="0" cy="-50" r="3" fill="none" stroke="#e6cf8f" '
         'stroke-width="1.2"/>')
    for r_hrot, r_pata, posun, svetla, tmava in ((36, 12, 22.5, '#f2c14e', '#b9801a'),
                                                (46, 14, 0, '#fff5c2', '#f2b53a')):
        sv = tm = ''
        for k in range(8):
            a = math.radians(k * 45 + posun - 90)
            hx, hy = r_hrot * math.cos(a), r_hrot * math.sin(a)
            sv += f'M0 0L{r_pata * math.cos(a - .42):.1f} {r_pata * math.sin(a - .42):.1f}L{hx:.1f} {hy:.1f}Z'
            tm += f'M0 0L{r_pata * math.cos(a + .42):.1f} {r_pata * math.sin(a + .42):.1f}L{hx:.1f} {hy:.1f}Z'
        s += f'<path d="{sv}" fill="{svetla}"/><path d="{tm}" fill="{tmava}"/>'
    return s + '<circle r="16" fill="url(#mkZareHvezdy)"/></svg>'


_HVEZDA_OCHR = _hvezda_ochranovska()

# Perníkové srdce s polevou
_SRDCE = 'M0 -20C-8 -34 -34 -32 -34 -10C-34 6 -16 20 0 34C16 20 34 6 34 -10C34 -32 8 -34 0 -20Z'
_KVITEK = ''.join(f'<circle cx="{6.5 * math.cos(math.radians(k * 72 - 90)):.1f}" '
                  f'cy="{4 + 6.5 * math.sin(math.radians(k * 72 - 90)):.1f}" r="3"/>' for k in range(5))
_SRDCE_SVG = ('<svg viewBox="-38 -42 76 80">'
              '<path d="M0 -41V-20" stroke="#d42a35" stroke-width="2.4"/>'
              f'<path d="{_SRDCE}" fill="url(#mkPernik)"/>'
              f'<path d="{_SRDCE}" transform="scale(.8)" fill="none" stroke="#fff" stroke-width="2.8" '
              'stroke-dasharray=".1 4.4" stroke-linecap="round"/>'
              f'<path d="{_SRDCE}" transform="scale(.6)" fill="none" stroke="#fff" stroke-width="1.6"/>'
              f'<g fill="#fff">{_KVITEK}</g><circle cx="0" cy="4" r="3" fill="#d42a35"/>'
              '<circle cx="-17" cy="-12" r="2.2" fill="#3fa35a"/><circle cx="17" cy="-12" r="2.2" fill="#3fa35a"/>'
              f'{_masle(0, -22, .42)}</svg>')

# Perníková hvězda s tečkovanou polevou
_HVEZDA_PERNIK_SVG = ('<svg viewBox="-32 -36 64 66"><circle cx="0" cy="-33" r="2.4" fill="none" stroke="#e6cf8f" '
                      f'stroke-width="1"/><path d="{_hvezda5(30, 14)}" fill="url(#mkPernik)" stroke-linejoin="round"/>'
                      f'<path d="{_hvezda5(23, 10.5)}" fill="none" stroke="#fff" stroke-width="2.4" '
                      'stroke-dasharray=".1 3.6" stroke-linecap="round"/>'
                      '<circle r="4.5" fill="#fff"/><circle r="2.5" fill="#d42a35"/></svg>')

# Visí z větví (velké po stranách) a z řetězu (z kotev oblouků na 0,3em a z jejich dna na 4,2em)
_ZAVESY = (_zaves('5%', 6, 16, 6.5, _SRDCE_SVG, .3, 'mk-zaves-velka')
           + _zaves('94%', 6, 12, 6, _HVEZDA_OCHR, 1.1, 'mk-zaves-velka mk-zaves-zari')
           + _zaves('25%', 4.2, 9, 4.6, _HVEZDA_OCHR, .8, 'mk-zaves-zari')
           + _zaves('33.3%', .3, 6, 3.2, _KOULE_SVG.format(_koule_telo(0, 0, 22, 'Cervena')), 1.9)
           + _zaves('41.7%', 4.2, 1.8, 3.4, _HVEZDA_PERNIK_SVG, 2.4)
           + _zaves('50%', .3, 3.4, 2.8, _KOULE_SVG.format(_koule_telo(0, 0, 22, 'Zlata')), .5)
           + _zaves('58.3%', 4.2, 1.8, 3.6, _SRDCE_SVG, 1.4)
           + _zaves('66.7%', .3, 6.5, 3.2, _KOULE_SVG.format(_koule_telo(0, 0, 22, 'Modra')), 2.8)
           + _zaves('75%', 4.2, 3, 3, _KOULE_SVG.format(_koule_telo(0, 0, 22, 'Zelena')), 1.2))


# ===== Velké sněhové krystaly na obloze (šest ramen s postranními výhonky) =====

def _krystal_def() -> str:
    d = ''
    for k in range(6):
        a = math.radians(k * 60 - 90)
        ux, uy = math.cos(a), math.sin(a)
        d += f'M0 0L{46 * ux:.1f} {46 * uy:.1f}'
        for r, delka in ((15, 12), (27, 11), (37, 7)):
            for s in (-1, 1):
                b = a + s * math.radians(60)
                d += f'M{r * ux:.1f} {r * uy:.1f}l{delka * math.cos(b):.1f} {delka * math.sin(b):.1f}'
    d += 'M' + 'L'.join(f'{8 * math.cos(math.radians(k * 60 - 90)):.1f} {8 * math.sin(math.radians(k * 60 - 90)):.1f}'
                        for k in range(6)) + 'Z'
    return ('<g id="mkKrystal" fill="none" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{d}" stroke="#bcd6ff" stroke-opacity=".35" stroke-width="7"/>'
            f'<path d="{d}" stroke="#f2f8ff" stroke-width="2.6"/></g>')


# (left, top, šířka em, perioda otočky s) — jen mimo sloupec s formulářem
_KRYSTALY = (('3vw', '58vh', 3.4, 46), ('24vw', '57vh', 2.6, 38), ('28.5vw', '41vh', 3.2, 55),
             ('9vw', '46vh', 2.2, 33), ('67.5vw', '33vh', 3, 42), ('74vw', '49vh', 4.4, 60),
             ('88vw', '50vh', 2.6, 35), ('80vw', '37vh', 2, 29))


def _krystaly() -> str:
    return ''.join(f'<svg class="mk-krystal" viewBox="-50 -50 100 100" style="left:{x};top:{y};width:{w}em;'
                   f'--t:{t}s"><use href="#mkKrystal"/></svg>' for x, y, w, t in _KRYSTALY)


def _trpytky() -> str:
    """Čtyřcípé třpytky v bocích (vlevo a vpravo od formuláře) — vynoří se, zatočí a zhasnou."""
    rnd = random.Random(77)
    out = []
    for x1, x2, y1, y2 in ((2, 30, 34, 66), (64, 98, 28, 64)):
        for _ in range(10):
            out.append(f'<i style="left:{rnd.uniform(x1, x2):.1f}vw;top:{rnd.uniform(y1, y2):.1f}vh;'
                       f'--s:{rnd.uniform(.7, 1.6):.1f}em;--t:{rnd.uniform(2.5, 5):.1f}s;--zp:-{rnd.uniform(0, 5):.1f}s"></i>')
    return '<div class="mk-trpytky">' + ''.join(out) + '</div>'


# ===== Čert na měsíci: sedí ve srpku, kope nohama a chytá na lízátko =====
_MESIC_CERT = f'''<svg class="mk-mesic-cert" viewBox="0 0 100 100">
  <g class="mk-mc-vlasec">
    <path d="M106 34V118" stroke="rgba(230,235,245,.75)" stroke-width=".6"/>
    <path d="M106 118v6" stroke="#f4f0e6" stroke-width="1.2"/>
    <circle cx="106" cy="129" r="5.5" fill="#ff5d73"/>
    <path d="M102 129a4 4 0 0 1 8 0a3 3 0 0 1 -6 0a2 2 0 0 1 4 0" stroke="#fff" stroke-width="1.1" fill="none"/>
  </g>
  <g class="mk-mc-ocas">
    <path d="M54 72Q40 78 42 88Q44 97 35 99" stroke="#21150f" stroke-width="2" fill="none" stroke-linecap="round"/>
    <path d="M37 96L28 100L36 102Z" fill="#21150f"/>
  </g>
  <g class="mk-mc-noha1"><path d="M56 71Q49 82 53 93" stroke="#2a1a12" stroke-width="4.4" fill="none"
     stroke-linecap="round"/><ellipse cx="53.5" cy="95" rx="3" ry="2" fill="#0e0806"/></g>
  <g class="mk-mc-noha2"><path d="M63 71Q60 83 65 92" stroke="#2a1a12" stroke-width="4.4" fill="none"
     stroke-linecap="round"/><ellipse cx="65.5" cy="94" rx="3" ry="2" fill="#0e0806"/></g>
  <ellipse cx="59.5" cy="63" rx="7.5" ry="10" fill="url(#mkSrst)"/>
  <path d="M62 69L106 34" stroke="#6b4423" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M54 58Q58 68 66 65M65 57Q70 60 70 62" stroke="#2a1a12" stroke-width="3" fill="none" stroke-linecap="round"/>
  <path d="M54 43Q49 37 51 30Q53 36 57 40Z" fill="url(#mkRoh)"/><path d="M65 43Q70 37 68 30Q66 36 62 40Z" fill="url(#mkRoh)"/>
  <path d="M53 47L47 44L52 51Z" fill="#8d2f1d"/><path d="M66 47L72 44L67 51Z" fill="#8d2f1d"/>
  <path d="{_srst(59.5, 48, 8, 14, 1.6)}" fill="url(#mkSrst)"/>
  <ellipse cx="59.5" cy="50" rx="5.8" ry="5" fill="url(#mkTvar)"/>
  <ellipse cx="57.4" cy="48.5" rx="1.6" ry="1.9" fill="#fff3c4"/><ellipse cx="61.6" cy="48.5" rx="1.6" ry="1.9" fill="#fff3c4"/>
  <circle cx="57.8" cy="49" r=".9" fill="#1a0f0a"/><circle cx="62" cy="49" r=".9" fill="#1a0f0a"/>
  <path d="M56 52.6Q59.5 56 63 52.6" stroke="#3a0d08" stroke-width="1" fill="none" stroke-linecap="round"/>
</svg>'''


# ===== Dole pod formulářem: sněhulák, sáňky s dárky, krmelec se srnkou a zajícem =====
_SNEHULAK = '''<svg class="mk-snehulak-svg" viewBox="0 0 100 150">
  <ellipse cx="52" cy="146" rx="40" ry="5" fill="rgba(20,30,60,.3)"/>
  <path d="M86 26L90 124" stroke="#7a5228" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M90 118C82 128 78 140 78 148H102C102 140 98 128 90 118Z" fill="#c79b45"/>
  <path d="M83 134L82 147M90 130V148M97 134L98 147" stroke="#9c7428" stroke-width="1.2"/>
  <rect x="85" y="116" width="10" height="5" rx="1.5" fill="#8a2a1a"/>
  <circle cx="50" cy="116" r="31" fill="url(#mkSnehulak)"/>
  <circle cx="50" cy="72" r="22" fill="url(#mkSnehulak)"/>
  <path d="M30 66L10 52M16 56L12 46M16 56L6 58" stroke="#5a3a1e" stroke-width="2.4" stroke-linecap="round" fill="none"/>
  <path d="M70 66L88 60" stroke="#5a3a1e" stroke-width="2.4" stroke-linecap="round"/>
  <g fill="#24262c"><circle cx="50" cy="66" r="2.6"/><circle cx="50" cy="77" r="2.6"/><circle cx="50" cy="88" r="2.6"/>
     <circle cx="50" cy="104" r="3"/><circle cx="50" cy="118" r="3"/></g>
  <circle cx="50" cy="36" r="16" fill="url(#mkSnehulak)"/>
  <path d="M34 50Q50 58 66 50L67 55Q50 64 33 55Z" fill="#d42a35"/>
  <path d="M40 51.5l-1 5M47 53.5l-.5 5.5M54 53.5l.5 5.5M61 51.5l1 5" stroke="#fff" stroke-width="2"/>
  <g class="mk-sala"><path d="M58 55L64 78L57 79L53 57Z" fill="#c01d28"/>
     <path d="M56 64L61.5 63M57.5 71L63 70" stroke="#fff" stroke-width="2"/>
     <path d="M57.5 79l-1 4M60.5 78.6l0 4M63.5 78l1 4" stroke="#c01d28" stroke-width="1.5"/></g>
  <circle cx="44" cy="32" r="2.3" fill="#24262c"/><circle cx="56" cy="32" r="2.3" fill="#24262c"/>
  <circle cx="44.7" cy="31.3" r=".7" fill="#fff"/><circle cx="56.7" cy="31.3" r=".7" fill="#fff"/>
  <circle cx="39" cy="39" r="3" fill="#f6a3a3" opacity=".5"/><circle cx="61" cy="39" r="3" fill="#f6a3a3" opacity=".5"/>
  <path d="M50 35.5L70 39.5L50 40.5Z" fill="#f07a1a"/><path d="M56 37.5l1 2M62 38.5l.6 1.6" stroke="#c4560c" stroke-width=".8"/>
  <g fill="#24262c"><circle cx="42" cy="43.5" r="1.3"/><circle cx="46" cy="45.5" r="1.3"/><circle cx="50" cy="46.2" r="1.3"/>
     <circle cx="54" cy="45.5" r="1.3"/><circle cx="58" cy="43.5" r="1.3"/></g>
  <g class="mk-snehulak-klobouk">
    <path d="M64 10Q73 10 73 16Q73 21 65 20" stroke="#a8121c" stroke-width="2.2" fill="none"/>
    <path d="M35 23L37 6H63L65 23Z" fill="#c8202b"/>
    <rect x="33" y="20" width="34" height="5" rx="2" fill="#a8121c"/><rect x="35" y="4" width="30" height="4" rx="2" fill="#a8121c"/>
    <g fill="#fff"><circle cx="42" cy="13" r="1.8"/><circle cx="50" cy="16" r="1.8"/><circle cx="58" cy="12" r="1.8"/></g>
    <path d="M36 5Q42 -1 50 2.5Q58 -1 64 5Z" fill="#fff"/>
  </g>
</svg>'''

_SANKY = '''<svg class="mk-sanky-svg" viewBox="0 0 132 90">
  <ellipse cx="64" cy="86" rx="54" ry="4" fill="rgba(20,30,60,.3)"/>
  <g class="mk-darek"><rect x="22" y="30" width="34" height="30" rx="2" fill="#c8202b"/>
     <rect x="36" y="30" width="6" height="30" fill="#f0c24a"/><rect x="22" y="42" width="34" height="6" fill="#f0c24a"/>
     <path d="M22 31Q30 27 38 30Q46 27 56 31V33H22Z" fill="#eef4fc"/></g>
  <g class="mk-darek"><rect x="58" y="38" width="30" height="22" rx="2" fill="#2a7a4a"/>
     <rect x="70" y="38" width="6" height="22" fill="#e9eef6"/><rect x="58" y="46" width="30" height="5" fill="#e9eef6"/>
     <path d="M73 38C66 30 62 37 73 38C84 37 80 30 73 38Z" fill="#e9eef6"/></g>
  <g class="mk-darek"><rect x="40" y="12" width="22" height="18" rx="2" fill="#2f6fd0"/>
     <rect x="49" y="12" width="4" height="18" fill="#ffd34d"/>
     <path d="M51 12C44 4 41 10 51 12C61 10 58 4 51 12Z" fill="#ffd34d"/></g>
  <rect x="12" y="60" width="90" height="7" rx="2" fill="#b07a3e"/>
  <path d="M30 60V67M50 60V67M70 60V67M90 60V67" stroke="#8a5a28" stroke-width="1.4"/>
  <path d="M12 61H102" stroke="#eef4fc" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M24 67V77M50 67V77M76 67V77M94 67V77" stroke="#7a4f22" stroke-width="3"/>
  <path d="M6 78H98C110 78 116 72 116 62C116 56 110 56 108 60" stroke="#5a3a1e" stroke-width="3.6" fill="none"
        stroke-linecap="round"/>
  <path d="M112 64C126 70 124 84 132 86" stroke="#d8b37a" stroke-width="1.6" fill="none"/>
</svg>'''


def _seno() -> str:
    rnd = random.Random(8)
    return ''.join(f'M{rnd.uniform(140, 192):.0f} {rnd.uniform(58, 88):.0f}l{rnd.uniform(-6, 6):.0f} '
                   f'{rnd.uniform(-5, 5):.0f}' for _ in range(28))


_KRMELEC = f'''<svg class="mk-krmelec-svg" viewBox="0 0 240 134">
  <ellipse cx="124" cy="130" rx="112" ry="4.5" fill="rgba(20,30,60,.3)"/>
  <rect x="122" y="40" width="6" height="90" fill="#4f3218"/><rect x="204" y="40" width="6" height="90" fill="#4f3218"/>
  <path d="M128 54H204L196 92H136Z" fill="url(#mkSeno)"/>
  <path d="{_seno()}" stroke="#f3dc8f" stroke-width="1.2" fill="none"/>
  <path d="M136 54V92M146 54V92M156 54V92M166 54V92M176 54V92M186 54V92M196 54V92" stroke="#5a3a1e" stroke-width="2"/>
  <path d="M132 54Q134 44 138 52M142 54Q146 42 150 53M156 54Q160 45 163 53M170 54Q173 43 178 52M184 54Q188 46 191 53
           M196 54Q199 47 201 53" stroke="#e8c96a" stroke-width="1.6" fill="none"/>
  <rect x="120" y="50" width="92" height="5" rx="1.5" fill="#6e4824"/>
  <rect x="124" y="92" width="84" height="9" rx="1.5" fill="#6e4824"/>
  <path d="M126 92H206" stroke="#eef4fc" stroke-width="3" stroke-linecap="round"/>
  <path d="M114 48L166 16L218 48Z" fill="#3e2714"/>
  <path d="M106 45L166 8L226 45Z" fill="url(#mkSnihStrecha)"/>
  <path d="{_snih_strecha(106, 226, 45)}" fill="#dfe8f6"/>
  <path d="M110 131Q122 121 134 131ZM196 131Q208 120 220 131Z" fill="#e6eef9"/>
  <g class="mk-zajic">
    <ellipse cx="220" cy="122" rx="11" ry="8" fill="#9b8a76"/>
    <ellipse cx="211" cy="123" rx="4" ry="3.5" fill="#f2ece2"/>
    <g class="mk-zajic-usi"><ellipse cx="227" cy="104" rx="2.2" ry="8" fill="#a8977f" transform="rotate(-12 227 104)"/>
       <ellipse cx="232" cy="104" rx="2.2" ry="8" fill="#9b8a76" transform="rotate(10 232 104)"/></g>
    <circle cx="230" cy="115" r="6" fill="#a8977f"/>
    <circle cx="232.5" cy="113.5" r="1.2" fill="#1a1410"/><circle cx="235.6" cy="116.5" r="1.1" fill="#d78a8a"/>
    <ellipse cx="226" cy="129" rx="4.5" ry="1.8" fill="#8d7c68"/>
  </g>
  <path d="M36 92L32 108L35 126M46 95L47 110L44 126M78 95L79 126M86 93L89 126" stroke="#5e3a1e" stroke-width="4.2"
        fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <g fill="#24170e"><circle cx="35" cy="127" r="2.4"/><circle cx="44" cy="127" r="2.4"/><circle cx="79" cy="127" r="2.4"/>
     <circle cx="89" cy="127" r="2.4"/></g>
  <ellipse cx="60" cy="86" rx="31" ry="15" fill="url(#mkSrna)"/>
  <path d="M40 94Q62 104 84 94Q62 98 40 94Z" fill="#d8b48a" opacity=".7"/>
  <ellipse cx="33" cy="82" rx="7" ry="9.5" fill="#f6eddc"/><ellipse cx="29" cy="76" rx="2.6" ry="3.6" fill="#6e4423"/>
  <g class="mk-srna-hlava">
    <path d="M76 80Q86 66 101 58L110 66Q98 75 92 92Z" fill="url(#mkSrna)"/>
    <ellipse cx="99" cy="70" rx="3.2" ry="5" transform="rotate(-50 99 70)" fill="#f6eddc"/>
    <g class="mk-ucho"><ellipse cx="103" cy="47" rx="3.4" ry="8.5" transform="rotate(-28 103 47)" fill="#7a4a24"/>
       <ellipse cx="103" cy="47.5" rx="1.8" ry="5.5" transform="rotate(-28 103 47.5)" fill="#e7c8a5"/></g>
    <ellipse cx="109" cy="47" rx="3.2" ry="8" transform="rotate(8 109 47)" fill="#6e4423"/>
    <ellipse cx="112" cy="60" rx="11.5" ry="7.5" transform="rotate(32 112 60)" fill="url(#mkSrna)"/>
    <ellipse cx="120" cy="66" rx="4" ry="3.2" transform="rotate(32 120 66)" fill="#3a2414"/>
    <path d="M114.5 66Q116.5 70 121 70.5" stroke="#f6eddc" stroke-width="1.6" fill="none"/>
    <circle cx="110" cy="57" r="1.8" fill="#1a0f0a"/><circle cx="110.6" cy="56.4" r=".6" fill="#fff"/>
    <path d="M121 68L130 70M121 69L128 74M120 70L126 76" stroke="#e8c96a" stroke-width="1.2" stroke-linecap="round"/>
  </g>
</svg>'''


# ===== Ozdobený smrk vpravo dole (za pytlem s čertem) =====

def _strom() -> str:
    """Pět pater jehličí se sněhovými obloučky, světýlka, koule a hvězda na špičce."""
    rnd = random.Random(21)
    patra = ((24, 74, 30), (48, 114, 44), (78, 154, 57), (108, 194, 69), (140, 234, 80))
    jehli = snih = svetla = koule = ''
    for p, (yt, yb, w) in enumerate(patra):
        jehli += f'M80 {yt}L{80 + w} {yb}'
        for i in range(5):
            x0, x1 = 80 + w - 2 * w * i / 5, 80 + w - 2 * w * (i + 1) / 5
            xm = (x0 + x1) / 2
            jehli += f'Q{xm:.1f} {yb + 8} {x1:.1f} {yb}'
            snih += f'M{x0:.1f} {yb}Q{xm:.1f} {yb + 8} {x1:.1f} {yb}Q{xm:.1f} {yb + 2.5} {x0:.1f} {yb}Z'
        jehli += 'Z'
        yr = yb - (yb - yt) * .32
        pul = w * (yr - yt) / (yb - yt) * .85
        for k in range(6):
            s = k / 5
            svetla += (f'<circle class="l{(k + p) % 3}" cx="{80 - pul + 2 * pul * s:.1f}" '
                       f'cy="{yr + 6 * math.sin(math.pi * s) - 4 * s + 2:.1f}" r="2.6" '
                       f'fill="{_BARVY_ZAROVEK[(k + p) % 4]}"/>')
        for _ in range(2 if p < 2 else 3):
            y = rnd.uniform(yt + (yb - yt) * .5, yb - 6)
            sirka = w * (y - yt) / (yb - yt) * .75
            koule += _koule_telo(rnd.uniform(80 - sirka, 80 + sirka), y, rnd.choice((4.5, 5.5)),
                                 rnd.choice(('Cervena', 'Zlata', 'Modra', 'Zelena')))
    return (f'<svg class="mk-strom" viewBox="0 0 160 262">'
            '<rect x="72" y="232" width="16" height="28" fill="#4a2f18"/>'
            f'<path d="{jehli}" fill="url(#mkStrom)"/><path d="{snih}" fill="#eef4fd"/>'
            f'{koule}<g filter="url(#mkZar)">{svetla}</g>'
            f'<g transform="translate(80 22)" filter="url(#mkZar)"><path class="mk-strom-hvezda" '
            f'd="{_hvezda5(15, 6.5)}" fill="#ffd34d" stroke="#fff3c0" stroke-width="1"/></g></svg>')


# ===== Průvod popředím: čert táhne pytel brambor JIP, za ním skotačí čertík s vidlemi =====

def _chlupy(cx: float, cy: float, rx: float, ry: float, zuby: int = 24, hloubka: float = 2.2) -> str:
    """Chlupatý obrys elipsy (čertí tělo): elipsa se zoubky."""
    body = []
    for i in range(zuby * 2):
        u = math.pi * i / zuby
        k = hloubka if i % 2 == 0 else 0
        body.append(f'{cx + (rx + k) * math.cos(u):.1f} {cy + (ry + k) * math.sin(u):.1f}')
    return 'M' + 'L'.join(body) + 'Z'


def _brambora(x: float, y: float, rx: float, ry: float, uhel: float = 0) -> str:
    return (f'<g transform="rotate({uhel} {x} {y})"><ellipse cx="{x}" cy="{y}" rx="{rx}" ry="{ry}" '
            'fill="url(#mkBrambora)"/><g fill="#7a4e1e" opacity=".75">'
            f'<circle cx="{x - rx * .45:.1f}" cy="{y - ry * .15:.1f}" r=".9"/>'
            f'<circle cx="{x + rx * .35:.1f}" cy="{y + ry * .35:.1f}" r=".8"/>'
            f'<circle cx="{x + rx * .15:.1f}" cy="{y - ry * .5:.1f}" r=".7"/></g></g>')


# Tkaní juty: čárkované řádky a sloupce uvnitř těla pytle
_JUTA = ('<path d="' + ''.join(f'M{x0} {y}H{x1}' for y, x0, x1 in ((84, 90, 134), (94, 80, 144), (104, 76, 148),
                                                                  (116, 73, 151), (128, 73, 151), (136, 78, 146)))
         + ''.join(f'M{x} {y0}V{y1}' for x, y0, y1 in ((84, 86, 136), (96, 77, 139), (128, 77, 139), (140, 86, 136)))
         + '" stroke="#6e4c22" stroke-opacity=".3" stroke-width=".9" stroke-dasharray="1.4 1.8"/>')

# Pytel kreslený na stojato (dno uprostřed v 112,140); v průvodu je nakloněný k čertovi o 14°
_PYTEL_JIP = (
    '<path d="M74 138C64 118 68 92 86 80C92 76 100 74 102 70H122C124 74 132 76 138 80C156 92 160 118 150 138'
    'C138 143 86 143 74 138Z" fill="url(#mkJuta)"/>' + _JUTA
    + '<path d="M138 80C156 92 160 118 150 138C146 140 140 141 134 141C145 120 147 96 138 80Z" fill="#5a3a16" opacity=".3"/>'
    '<path d="M86 80C76 90 72 104 74 118" stroke="#f2dcae" stroke-opacity=".35" stroke-width="3" fill="none" '
    'stroke-linecap="round"/>'
    # záplata
    '<g transform="rotate(-10 83 117)"><rect x="77" y="112" width="12" height="10" rx="1" fill="#a88148"/>'
    '<rect x="78.5" y="113.5" width="9" height="7" fill="none" stroke="#f0dfb4" stroke-width=".7" '
    'stroke-dasharray="1.5 1.5"/></g>'
    # potisk JIP (žlutý ovál, modrý nápis, červené kuličky a lístek jako na logu)
    '<g opacity=".95"><ellipse cx="112" cy="104" rx="25" ry="13.5" fill="#ffe000" stroke="#1d3f95" stroke-width="1.2"/>'
    '<g fill="#e3202a"><circle cx="93.5" cy="102" r="2.5"/><circle cx="97" cy="98" r="2.1"/>'
    '<circle cx="97.5" cy="106" r="2.1"/></g>'
    '<text x="114" y="110.5" text-anchor="middle" font-family="Arial Black, Arial, Helvetica, sans-serif" '
    'font-size="17" font-weight="900" font-style="italic" fill="#1d3f95" stroke="#fff" stroke-width="1" '
    'paint-order="stroke">JIP</text>'
    '<path d="M125 112Q131 105 136 108Q132 114 125 112Z" fill="#3aa142"/></g>'
    '<text x="112" y="129" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="6.2" '
    'font-weight="800" letter-spacing=".9" fill="#4a2e10" opacity=".75">BRAMBORY</text>'
    # hrdlo, otevřený vršek s bramborami, provaz
    '<path d="M102 70C104 66 104 64 103 61H121C120 64 120 66 122 70Z" fill="#9a7440"/>'
    '<path d="M107 70L107.5 62M112 70V61M117 70L116.5 62" stroke="#6e4c22" stroke-width=".8"/>'
    '<ellipse cx="112" cy="50" rx="18" ry="5" fill="#4e3418"/>'
    '<g class="mk-pr-brambora">' + _brambora(100, 40, 6, 4.8, -15) + '</g>'
    + _brambora(102, 47, 8, 6, -20) + _brambora(121, 46, 8.5, 6.2, 15) + _brambora(112, 40, 8.2, 6, 5)
    + _brambora(128, 50, 5.5, 4.2, 30)
    + '<path d="M94 50Q112 57 130 50C131 56 127 60 121 61H103C97 60 93 56 94 50Z" fill="url(#mkJuta)" '
    'stroke="#7a5628" stroke-width=".6"/>'
    '<path d="M100 65.5Q112 69.5 124 65.5" stroke="#d9b56a" stroke-width="3" fill="none" stroke-linecap="round"/>'
    '<circle cx="124" cy="65.5" r="2.4" fill="#c9a058"/>')

# Řetěz kolem pasu a jeho volný konec (houpe se)
_RETEZ_PAS = ''.join(f'<ellipse cx="{242 + i * 4.3:.1f}" cy="{91 + i * 1.3:.1f}" rx="2.6" ry="1.5" '
                     f'transform="rotate({15 if i % 2 else -15} {242 + i * 4.3:.1f} {91 + i * 1.3:.1f})"/>'
                     for i in range(7))
_RETEZ_KONEC = ''.join(f'<ellipse cx="{243 - i * 1.6:.1f}" cy="{97 + i * 4.6:.1f}" rx="{1.5 if i % 2 else 2.4}" '
                       f'ry="{2.6 if i % 2 else 1.5}"/>' for i in range(5))

_PRUVOD_SVG = f'''<svg class="mk-pruvod-svg" viewBox="-8 0 310 150">
  <ellipse cx="114" cy="141" rx="46" ry="4" fill="rgba(20,30,60,.3)"/>
  <ellipse cx="253" cy="141" rx="24" ry="3.5" fill="rgba(20,30,60,.3)"/>
  <ellipse cx="34" cy="141" rx="13" ry="2.6" fill="rgba(20,30,60,.3)"/>
  <g class="mk-ct">
    <g fill="#eef4fd"><circle class="mk-pr-prasan" cx="72" cy="137" r="3.6"/>
       <circle class="mk-pr-prasan" cx="78" cy="139" r="2.8" style="animation-delay:-.4s"/>
       <circle class="mk-pr-prasan" cx="68" cy="138" r="2.4" style="animation-delay:-.75s"/></g>
    <path d="M142 70.6Q200 80 262 58" stroke="#c9a058" stroke-width="2.6" fill="none" stroke-linecap="round"/>
    <g class="mk-pb-tah"><g transform="rotate(14 112 140)">{_PYTEL_JIP}</g></g>
    <g class="mk-ct-noha2"><path d="M249 100Q244 118 248 136" stroke="#1e120c" stroke-width="7.5" fill="none"
       stroke-linecap="round"/><path d="M242 135h11l-.5 4.5h-11Z" fill="#0a0605"/></g>
    <g class="mk-ct-bob">
      <g class="mk-ct-ocas"><path d="M240 100C228 112 222 124 212 120C204 117 206 108 200 106" stroke="#2a1a12"
         stroke-width="3" fill="none" stroke-linecap="round"/><path d="M203 109L194 104L203 100Z" fill="#2a1a12"/></g>
      <g transform="rotate(14 252 102)">
        <path d="{_chlupy(255, 80, 16, 24)}" fill="url(#mkSrst)"/>
        <ellipse cx="259" cy="86" rx="8" ry="13" fill="#5a3a26" opacity=".45"/>
      </g>
    </g>
    <g class="mk-ct-noha1"><path d="M255 100Q252 118 256 136" stroke="#2a1a12" stroke-width="7.5" fill="none"
       stroke-linecap="round"/><path d="M250 135h11l-.5 4.5h-11Z" fill="#0e0806"/>
       <path d="M255.5 135.5v4" stroke="#2a1a12" stroke-width=".8"/></g>
    <g class="mk-ct-bob">
      <g fill="none" stroke="#9aa4b0" stroke-width="1.2">{_RETEZ_PAS}
        <g class="mk-ct-retez">{_RETEZ_KONEC}</g></g>
      <path d="M262 63Q264 80 270 81Q276 80 279 68" stroke="#2a1a12" stroke-width="6.5" fill="none" stroke-linecap="round"/>
      <path d="M257 57Q270 51 280 66M280 70Q284 80 280 88" stroke="#c9a058" stroke-width="2.6" fill="none"
            stroke-linecap="round"/>
      <circle cx="279.5" cy="67.5" r="4.5" fill="#1e120c"/>
      <path d="{_srst(273, 43, 14, 20, 2.2)}" fill="url(#mkSrst)"/>
      <path d="M264 33Q255 22 260 11Q262 22 270 29ZM281 31Q288 20 284 9Q280 21 275 29Z" fill="url(#mkRoh)"/>
      <path d="M261 46L251 41L259 51Z" fill="#8d2f1d"/>
      <ellipse cx="277" cy="46" rx="10" ry="9" fill="url(#mkTvar)"/>
      <ellipse cx="273.5" cy="43.5" rx="2.3" ry="2.8" fill="#fff3c4"/><ellipse cx="281.5" cy="43.5" rx="2.3" ry="2.8" fill="#fff3c4"/>
      <circle cx="274.6" cy="44.2" r="1.2" fill="#1a0f0a"/><circle cx="282.6" cy="44.2" r="1.2" fill="#1a0f0a"/>
      <path d="M270 39.5L276 41.2M286 39.5L280 41.2" stroke="#1a0f0a" stroke-width="1.4" stroke-linecap="round"/>
      <ellipse cx="285.5" cy="47.5" rx="2.2" ry="1.6" fill="#9a3a24"/>
      <g class="mk-ct-jazyk"><path d="M276 52.5C275 60 280 63 278 69C277 73 282 75 284 71C286 65 282 59 283 52.5Z"
         fill="#d6283a"/><path d="M279.5 54C279.5 60 282 63 281 69" stroke="#a3121f" stroke-width=".8" fill="none"/></g>
      <path d="M271 50.5Q278 56 286 50" stroke="#3a0d08" stroke-width="1.3" fill="none" stroke-linecap="round"/>
    </g>
  </g>
  <g class="mk-ck"><g class="mk-ck-trasa"><g class="mk-ck-skok"><g class="mk-ck-salto">
    <g class="mk-ck-ocas"><path d="M26 124Q10 130 9 116Q8 104 0 101" stroke="#7a1d10" stroke-width="2.4" fill="none"
       stroke-linecap="round"/><path d="M-4 104L0 93L5 103Z" fill="#7a1d10"/></g>
    <g class="mk-ck-noha1"><path d="M30 126L28.5 137" stroke="#5e170c" stroke-width="3.8" stroke-linecap="round"/>
       <ellipse cx="28" cy="138.6" rx="3.4" ry="1.7" fill="#1a0806"/></g>
    <g class="mk-ck-noha2"><path d="M38 126L39.5 137" stroke="#5e170c" stroke-width="3.8" stroke-linecap="round"/>
       <ellipse cx="40" cy="138.6" rx="3.4" ry="1.7" fill="#1a0806"/></g>
    <ellipse cx="34" cy="117" rx="10.5" ry="12" fill="url(#mkCertik)"/>
    <ellipse cx="35.5" cy="119" rx="6" ry="7.5" fill="#f08a6a" opacity=".35"/>
    <g class="mk-ck-ruka"><path d="M27 110L18 101" stroke="#9a2a18" stroke-width="3.4" stroke-linecap="round"/>
       <circle cx="17.5" cy="100.5" r="3" fill="#9a2a18"/></g>
    <g class="mk-ck-vidle"><g transform="rotate(6 47 108)"><path d="M47 132V76" stroke="#6b4423" stroke-width="2.2"
       stroke-linecap="round"/><path d="M41 76H53M41 76V66M47 76V64M53 76V66" stroke="#9aa3ad" stroke-width="1.8"
       stroke-linecap="round" fill="none"/><path d="M39.5 67L41 61.5L42.5 67ZM45.5 65L47 59.5L48.5 65ZM51.5 67L53 61.5L54.5 67Z"
       fill="#c3cad2"/></g></g>
    <path d="M41 111L46 108.5" stroke="#9a2a18" stroke-width="3.4" stroke-linecap="round"/>
    <circle cx="47" cy="108" r="3.2" fill="#9a2a18"/>
    <path d="{_srst(34, 96, 10, 16, 1.8)}" fill="url(#mkCertik)"/>
    <path d="M27.5 89Q22 81 24.5 75Q27 82 31.5 86ZM40.5 89Q46 81 43.5 75Q41 82 36.5 86Z" fill="url(#mkRoh)"/>
    <path d="M24.5 97L18 94.5L23.5 100.5ZM43.5 97L50 94.5L44.5 100.5Z" fill="#b5321e"/>
    <ellipse cx="34.5" cy="98.5" rx="7.6" ry="6.6" fill="url(#mkTvar)"/>
    <ellipse cx="31" cy="96.8" rx="2.5" ry="3" fill="#fff"/><ellipse cx="38" cy="96.8" rx="2.5" ry="3" fill="#fff"/>
    <circle cx="32" cy="97.4" r="1.4" fill="#1a0f0a"/><circle cx="39" cy="97.4" r="1.4" fill="#1a0f0a"/>
    <circle cx="32.5" cy="96.7" r=".45" fill="#fff"/><circle cx="39.5" cy="96.7" r=".45" fill="#fff"/>
    <path d="M28.5 92.8L32.5 94M40.5 92.8L36.5 94" stroke="#4a0c06" stroke-width="1.2" stroke-linecap="round"/>
    <path d="M30 101.3Q34.5 106 39.5 101.3Q34.5 103.2 30 101.3Z" fill="#5a0d08"/>
    <path d="M32.6 102.2l.8 1.5l.8-1.3Z" fill="#fff"/>
    <circle cx="28.6" cy="101" r="1.6" fill="#ff7a6a" opacity=".5"/><circle cx="40.4" cy="101" r="1.6" fill="#ff7a6a" opacity=".5"/>
  </g></g></g></g>
</svg>'''

_PRUVOD = f'<div class="mk-pruvod">{_PRUVOD_SVG}<div class="mk-bublina">Brambory pro zlobivé!</div></div>'


_HTML = f'''
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <linearGradient id="mkPlast" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ec3a43"/><stop offset=".6" stop-color="#c01722"/><stop offset="1" stop-color="#84070f"/>
    </linearGradient>
    <linearGradient id="mkAlba" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#d8e0ee"/><stop offset=".5" stop-color="#fff"/><stop offset="1" stop-color="#cfd9e8"/>
    </linearGradient>
    <linearGradient id="mkZlato" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffe9a3"/><stop offset=".5" stop-color="#f0c24a"/><stop offset="1" stop-color="#b8841c"/>
    </linearGradient>
    <linearGradient id="mkMitra" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#e3e8f2"/><stop offset=".45" stop-color="#fff"/><stop offset="1" stop-color="#d1d9e8"/>
    </linearGradient>
    <radialGradient id="mkPlet" cx="42%" cy="38%" r="70%">
      <stop offset="0" stop-color="#ffe3cf"/><stop offset="1" stop-color="#f0b393"/>
    </radialGradient>
    <linearGradient id="mkVousy" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#dbe2ec"/>
    </linearGradient>
    <radialGradient id="mkMesic" cx="35%" cy="45%" r="70%">
      <stop offset="0" stop-color="#fffdf0"/><stop offset=".6" stop-color="#f8ecc4"/><stop offset="1" stop-color="#dcc58c"/>
    </radialGradient>
    <mask id="mkSrp" maskUnits="userSpaceOnUse" x="0" y="0" width="100" height="100">
      <rect width="100" height="100" fill="#fff"/><circle cx="70" cy="36" r="40" fill="#000"/>
    </mask>
    <linearGradient id="mkMrak" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#7d8fb8"/><stop offset=".45" stop-color="#3b4b72" stop-opacity=".95"/>
      <stop offset="1" stop-color="#1a2440" stop-opacity="0"/>
    </linearGradient>
    <clipPath id="mkDulky"><path d="M2 4Q9 2 14 7Q8 11.5 2 4ZM38 4Q31 2 26 7Q32 11.5 38 4Z"/></clipPath>
    <linearGradient id="mkPytel" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#c99c60"/><stop offset=".6" stop-color="#9a6d38"/><stop offset="1" stop-color="#5f4019"/>
    </linearGradient>
    <radialGradient id="mkSrst" cx="45%" cy="35%" r="75%">
      <stop offset="0" stop-color="#4d362b"/><stop offset="1" stop-color="#160d0a"/>
    </radialGradient>
    <radialGradient id="mkTvar" cx="45%" cy="38%" r="70%">
      <stop offset="0" stop-color="#dc6c4c"/><stop offset="1" stop-color="#8a2c1a"/>
    </radialGradient>
    <linearGradient id="mkRoh" x1="0" y1="1" x2="0" y2="0">
      <stop offset="0" stop-color="#f1e3c2"/><stop offset="1" stop-color="#7d6c50"/>
    </linearGradient>
    <clipPath id="mkUsti"><path d="M0 0H190V84H116A34 10 0 0 1 48 84H0Z"/></clipPath>
    <linearGradient id="mkKos" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#c88a48"/><stop offset="1" stop-color="#7a4a1f"/>
    </linearGradient>
    <radialGradient id="mkPomeranc" cx="38%" cy="35%" r="70%">
      <stop offset="0" stop-color="#ffc56b"/><stop offset=".6" stop-color="#f68b1f"/><stop offset="1" stop-color="#b85a0b"/>
    </radialGradient>
    <radialGradient id="mkJablko" cx="38%" cy="35%" r="70%">
      <stop offset="0" stop-color="#ff7b7b"/><stop offset=".6" stop-color="#d42a2a"/><stop offset="1" stop-color="#8c1010"/>
    </radialGradient>
    <radialGradient id="mkSklo" cx="50%" cy="52%" r="60%">
      <stop offset="0" stop-color="#fff4c8" stop-opacity=".95"/><stop offset=".5" stop-color="#ffbf55" stop-opacity=".7"/>
      <stop offset="1" stop-color="#a1500f" stop-opacity=".45"/>
    </radialGradient>
    <linearGradient id="mkKridlo" x1="1" y1="0" x2="0" y2="0">
      <stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#cbd9f2"/>
    </linearGradient>
    <linearGradient id="mkSaty" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#dbe5f6"/><stop offset=".5" stop-color="#fff"/><stop offset="1" stop-color="#cfdbef"/>
    </linearGradient>
    <radialGradient id="mkVlasy" cx="45%" cy="35%" r="70%">
      <stop offset="0" stop-color="#ffe7a0"/><stop offset="1" stop-color="#e3a536"/>
    </radialGradient>
    <linearGradient id="mkZvon" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#b8861a"/><stop offset=".35" stop-color="#ffe08a"/><stop offset="1" stop-color="#a87414"/>
    </linearGradient>
    <linearGradient id="mkSnihStrecha" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f4f8ff"/><stop offset="1" stop-color="#b3c3df"/>
    </linearGradient>
    <linearGradient id="mkZeme" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#b9c9e3"/><stop offset="1" stop-color="#5d7098"/>
    </linearGradient>
    <linearGradient id="mkZemeZadni" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5f739c"/><stop offset="1" stop-color="#25324f"/>
    </linearGradient>
    <linearGradient id="mkZavej" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#eef4fd"/><stop offset="1" stop-color="#a3b6d6"/>
    </linearGradient>
    <radialGradient id="mkKouleCervena" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="#ff9a9a"/><stop offset=".55" stop-color="#d42a35"/><stop offset="1" stop-color="#7a0910"/>
    </radialGradient>
    <radialGradient id="mkKouleZlata" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="#fff3c0"/><stop offset=".55" stop-color="#f0c24a"/><stop offset="1" stop-color="#9c6a10"/>
    </radialGradient>
    <radialGradient id="mkKouleModra" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="#b4e2ff"/><stop offset=".55" stop-color="#2f7fd6"/><stop offset="1" stop-color="#12376e"/>
    </radialGradient>
    <radialGradient id="mkKouleZelena" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="#b4f2c2"/><stop offset=".55" stop-color="#2e9a54"/><stop offset="1" stop-color="#0f4d28"/>
    </radialGradient>
    <radialGradient id="mkPernik" cx="40%" cy="35%" r="75%">
      <stop offset="0" stop-color="#d39456"/><stop offset=".6" stop-color="#a0612b"/><stop offset="1" stop-color="#6e3a14"/>
    </radialGradient>
    <radialGradient id="mkZareHvezdy">
      <stop offset="0" stop-color="#fff" stop-opacity=".95"/><stop offset=".45" stop-color="#fff8d8" stop-opacity=".7"/>
      <stop offset="1" stop-color="#fff3b0" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="mkSnehulak" cx="38%" cy="32%" r="75%">
      <stop offset="0" stop-color="#fff"/><stop offset=".6" stop-color="#e6eef9"/><stop offset="1" stop-color="#b3c4e0"/>
    </radialGradient>
    <linearGradient id="mkSrna" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#b47c45"/><stop offset="1" stop-color="#7a4a24"/>
    </linearGradient>
    <linearGradient id="mkSeno" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ecd27c"/><stop offset="1" stop-color="#b88f3c"/>
    </linearGradient>
    <linearGradient id="mkStrom" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2f7a4e"/><stop offset="1" stop-color="#0f3a28"/>
    </linearGradient>
    {_jehlici()}{_krystal_def()}{_SMRCEK}
    <radialGradient id="mkJuta" cx="40%" cy="40%" r="70%">
      <stop offset="0" stop-color="#dcbb80"/><stop offset=".6" stop-color="#b08848"/><stop offset="1" stop-color="#7e5a28"/>
    </radialGradient>
    <radialGradient id="mkBrambora" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="#eec684"/><stop offset=".6" stop-color="#c08a44"/><stop offset="1" stop-color="#865a26"/>
    </radialGradient>
    <radialGradient id="mkCertik" cx="40%" cy="32%" r="75%">
      <stop offset="0" stop-color="#e0603f"/><stop offset=".6" stop-color="#a92c18"/><stop offset="1" stop-color="#5a1208"/>
    </radialGradient>
    <filter id="mkZar" x="-1" y="-1" width="3" height="3">
      <feGaussianBlur stdDeviation="2.5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
</svg>
{_hvezdy()}
{_meteory()}
{_MESIC}{_MESIC_CERT}<div class="mk-bublina">Kdo zlobil?</div></div>
{_krystaly()}{_trpytky()}
{_snih(55, 7, 'mk-snih-zadni', (1.5, 3.2), (10, 17))}
{_ZEME_ZADNI}{_les()}{_ZEME_PREDNI}{_CHALOUPKY}{_KOSTEL}
{_strom()}
{_oci('19vw', '66vh', '#ff5a36', 15, 2)}{_oci('33vw', '30vh', '#ffb43a', 19, 9, 2.2)}{_oci('69vw', '63vh', '#ff5a36', 17, 13)}
<div class="mk-zem-postava mk-snehulak">{_SNEHULAK}<div class="mk-bublina">Brr, to je zima!</div></div>
<div class="mk-zem-postava mk-sanky">{_SANKY}</div>
<div class="mk-zem-postava mk-krmelec">{_KRMELEC}</div>
{_PRUVOD}
<div class="mk-andel"><div class="mk-andel-vznasej">{_ANDEL}
  <div class="mk-jiskricky">{''.join(f'<i style="--x:{x};--y:{y};animation-delay:{i * .04:.2f}s"></i>'
                                     for i, (x, y) in enumerate(_JISKRICKY))}</div>
</div></div>
{_girlanda()}
{_ZAVESY}
<div class="mk-zvonek">{_ZVONEK}<div class="mk-bublina">Cink, cink!</div></div>
<svg class="mk-vetev mk-l" viewBox="0 0 360 260"><use href="#mkVetev"/>{_ROH_L}</svg>
<svg class="mk-vetev mk-p" viewBox="0 0 360 260"><use href="#mkVetev"/>{_ROH_P}</svg>
<div class="mk-skupina mk-l">
  <div class="mk-zar"></div>
  <div class="mk-postava mk-mikulas">{_MIKULAS}<div class="mk-bublina">Byl jsi hodný?</div></div>
  <div class="mk-postava mk-kos">{_KOS}</div>
  {_lucerna(3.4, .4)}
</div>
<div class="mk-skupina mk-p">
  <div class="mk-zar"></div>
  {_lucerna(4.2, .9)}{_lucerna(3.2, 1.6)}
  <div class="mk-postava mk-pytel">{_PYTEL}<div class="mk-bublina">Bubu!</div></div>
</div>
{_ZAVEJ}
{_snih(20, 8, 'mk-snih-predni', (4, 7.5), (6, 10))}
'''


# ===== Sníh s rampouchy na polích formuláře =====
# Vrstva začíná 14 px nad polem (horní hrana pole je v obrázku na y=_SY) a je 80 px vysoká.
_SY = 14


def _hrana(x: float, roh: bool) -> float:
    """y horní hrany pole v obrázku sněhu; `roh` = kus začíná zaobleným rohem pole
    (poloměr 12 px, levý bok pole je na x=6)."""
    if roh and x < 18:
        return _SY + 12 - math.sqrt(max(0.0, 144 - (18 - x) ** 2))
    return _SY


def _tloustka(x: float, profil) -> float:
    """Tloušťka sněhu na x — lineárně mezi body profilu [(x, t)]."""
    for (x0, t0), (x1, t1) in zip(profil, profil[1:]):
        if x0 <= x <= x1:
            return t0 + (t1 - t0) * (x - x0) / (x1 - x0)
    return 0.0


def _snih_url(obsah: str, styl: str, sirka: int, zrcadlit: bool) -> str:
    """Kousek sněhu jako CSS url() s inline SVG — kapky a třpyt běží uvnitř obrázku."""
    z = f' transform="matrix(-1 0 0 1 {sirka} 0)"' if zrcadlit else ''
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{sirka}" height="80"><style>{styl}'
           '@media (prefers-reduced-motion:reduce){*{animation:none!important}}</style>'
           '<linearGradient id="s" gradientUnits="userSpaceOnUse" x2="0" y1="2" y2="18">'
           '<stop stop-color="#fff"/><stop offset=".55" stop-color="#f3f7fe"/><stop offset="1" stop-color="#c9d7ee"/>'
           '</linearGradient><linearGradient id="r" x2="0" y2="1"><stop stop-color="#f2f8ff" stop-opacity=".95"/>'
           '<stop offset="1" stop-color="#a9c8ec" stop-opacity=".5"/></linearGradient>'
           f'<g{z}>{obsah}</g></svg>')
    return 'url("data:image/svg+xml,' + quote(svg, safe=" =:/.,-()!*;'") + '")'


def _snih_kus(sirka: int, profil, rampouchy, roh: bool = False, zrcadlit: bool = False, semeno: int = 1) -> str:
    """Čepice sněhu na horní hraně pole: nahoře chomáčky, tloušťka podle `profil` (na koncích 0),
    pod ní rampouchy (x, délka, polovina šířky, perioda kapky [s] nebo 0, zpoždění [s])."""
    rnd = random.Random(semeno)
    x0, x1 = profil[0][0], profil[-1][0]
    body = [x0]
    while body[-1] < x1 - 12:
        body.append(body[-1] + rnd.uniform(8, 12))
    body.append(x1)

    def horni(x):
        return _hrana(x, roh) - _tloustka(x, profil)

    def dolni(x):
        return _hrana(x, roh) + min(2.5, _tloustka(x, profil))

    d = f'M{x0 - (1.5 if roh else 0):g} {dolni(x0) + (4 if roh else 0):.1f}L{x0:g} {horni(x0):.1f}'
    for a, b in zip(body, body[1:]):
        t = _tloustka((a + b) / 2, profil)
        vyboul = min(3.2, .4 * (b - a)) * min(1, t / 2)
        d += f'Q{(a + b) / 2:.1f} {min(horni(a), horni(b)) - vyboul:.1f} {b:.1f} {horni(b):.1f}'
    x = x1
    while x > x0:
        x = max(x0, x - 3)
        d += f'L{x:.1f} {dolni(x):.1f}'
    d += 'Z'

    led = kapky = styl = ''
    for i, (c, delka, w, perioda, zpozdeni) in enumerate(rampouchy):
        y0 = _hrana(c, roh) + 1
        led += (f'M{c - w:g} {y0:.1f}Q{c - w * .35:.1f} {y0 + delka * .55:.1f} {c:g} {y0 + delka:.1f}'
                f'Q{c + w * .35:.1f} {y0 + delka * .55:.1f} {c + w:g} {y0:.1f}Z')
        if perioda:
            kapky += f'<circle class="k{i}" cx="{c:g}" cy="{y0 + delka + 1.6:.1f}" r="1.6"/>'
            styl += f'.k{i}{{animation:k {perioda:g}s ease-in -{zpozdeni:g}s infinite}}'
    if kapky:
        styl = ('circle{transform-box:fill-box;transform-origin:50% 0}' + styl +
                '@keyframes k{0%{transform:scale(0);opacity:1}70%{transform:scale(1)}'
                '76%{transform:scale(1);opacity:1}100%{transform:translateY(40px) scale(.7);opacity:0}}')
    # třpyt: pár jiskřiček na hřbetu sněhu
    trpyt = ''
    for j in range(3):
        x = rnd.uniform(x0 + 8, x1 - 8)
        if _tloustka(x, profil) > 3:
            trpyt += (f'<circle class="t" cx="{x:.1f}" cy="{horni(x) + 2:.1f}" r="1" '
                      f'style="animation-delay:-{rnd.uniform(0, 3):.1f}s"/>')
    if trpyt:
        styl += ('.t{animation:t 3s ease-in-out infinite;opacity:0}'
                 '@keyframes t{0%,70%,100%{opacity:0}80%{opacity:1}}')
    obsah = (f'<path d="{led}" fill="url(#r)"/><path d="{d}" fill="url(#s)"/>'
             f'<g fill="#d6ebff">{kapky}</g><g fill="#fff">{trpyt}</g>')
    return _snih_url(obsah, styl, sirka, zrcadlit)


# Pole e-mailu a hesla mají každé jiné rozložení sněhu a rampouchů (a vlastní tempo kapání).
# Rampouchy nad začátkem pole (x 18–75 v levém kusu) nejsou — tam sedí popisek pole.
_SNIH_CSS = f'''
/* Sníh s rampouchy na polích formuláře — jen přihlašovací formulář, kliknutí propouští */
body.mikulas .login-wrap .q-field__inner::after {{
    content: ''; position: absolute; left: -6px; right: -6px; top: -14px; height: 80px;
    pointer-events: none; z-index: 1;
    filter: drop-shadow(0 1px 1px rgba(30,50,90,.45));
    background: {_snih_kus(110, [(5, 4), (12, 9), (40, 8.5), (72, 6), (108, 0)],
                           [(9, 8, 2.4, 0, 0), (82, 12, 2.6, 4.6, 1), (96, 6, 1.8, 0, 0)], roh=True, semeno=3)}
                left top / 110px 80px no-repeat,
        {_snih_kus(150, [(0, 0), (22, 6), (70, 8.5), (120, 5.5), (150, 0)],
                   [(28, 7, 2, 0, 0), (47, 14, 3, 5.4, 2.5), (66, 9, 2.2, 0, 0), (92, 13, 2.6, 6.2, 4),
                    (110, 6, 1.8, 0, 0)], semeno=5)} 64% 0 / 150px 80px no-repeat,
        {_snih_kus(110, [(5, 5), (12, 10), (46, 7.5), (80, 4), (108, 0)],
                   [(10, 15, 3, 5, 3.4), (27, 9, 2.2, 0, 0), (45, 6, 1.8, 0, 0), (68, 11, 2.4, 0, 0)],
                   roh=True, zrcadlit=True, semeno=7)} right top / 110px 80px no-repeat;
}}
body.mikulas .login-wrap .q-field ~ .q-field .q-field__inner::after {{
    background: {_snih_kus(110, [(5, 3), (12, 8), (50, 7), (90, 3), (108, 0)],
                           [(8, 10, 2.6, 5.8, 2), (86, 9, 2.2, 0, 0)], roh=True, semeno=11)}
                left top / 110px 80px no-repeat,
        {_snih_kus(150, [(0, 0), (25, 7), (75, 9), (125, 5), (150, 0)],
                   [(34, 10, 2.4, 0, 0), (52, 6, 1.8, 0, 0), (74, 15, 3, 4.8, 1.2), (100, 8, 2, 0, 0),
                    (118, 12, 2.4, 6.6, 3.7)], semeno=13)} 36% 0 / 150px 80px no-repeat,
        {_snih_kus(110, [(5, 4), (12, 9), (50, 8), (85, 4), (108, 0)],
                   [(9, 11, 2.6, 0, 0), (32, 14, 2.8, 5.2, .6), (56, 7, 2, 0, 0), (74, 9, 2.2, 0, 0)],
                   roh=True, zrcadlit=True, semeno=17)} right top / 110px 80px no-repeat;
}}
'''

CSS = '''
/* ===== Mikuláš na přihlašovací obrazovce (1.–6. prosince, viz intranet_mikulas.py) ===== */
/* Fotka do studeného zimního svitu, nad ní modrá noc a zespodu teplá záře od luceren */
body.mikulas {
    --mk-foto: grayscale(.7) sepia(.2) hue-rotate(180deg) saturate(1.5) contrast(1.05) brightness(.55);
    --mk-noc: radial-gradient(ellipse 75% 40% at 50% 110%, rgba(255,196,110,.2), rgba(255,196,110,0) 70%),
              radial-gradient(ellipse at 50% 42%, rgba(18,34,72,.5) 0%, rgba(4,10,28,.95) 72%);
}
body.prihlaseni-pozadi.mikulas::before { filter: var(--mk-foto) !important; }
body.prihlaseni-pozadi.mikulas::after { background: var(--mk-noc) !important; }
/* Mezistránka „Přihlašuji se…" kreslí pozadí sama (je v dialogu nad body) — stejně ztmavit */
body.mikulas .login-loading::before { filter: var(--mk-foto); }
body.mikulas .login-loading::after { background: var(--mk-noc); }
/* Dekorace leží pod formulářem a nikdy nechytají kliknutí */
body.mikulas .login-wrap { position: relative; z-index: 1; }
body.mikulas .mk {
    position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
    font-size: clamp(8px, .85vw, 16px);   /* 1em = měřítko všech dekorací */
}
.mk svg { display: block; overflow: visible; }

/* Zlatá pro odkazy a pole, tlačítko mikulášsky červené se zlatým lemem. Quasar .bg-primary /
   .text-primary mají !important v CSS vrstvě, která přebije i naše !important — barví se
   proto přes proměnnou --q-primary, kterou ty třídy čtou. */
body.mikulas .login-wrap { --q-primary: #ffd27a; }
body.mikulas .login-btn { --q-primary: linear-gradient(135deg, #f0343c, #a80d17);
                          box-shadow: inset 0 0 0 1.5px rgba(255,214,130,.6), 0 8px 24px rgba(150,8,18,.45); }
body.mikulas .login-btn .q-btn__content { color: #fff; }
body.mikulas .login-btn:hover { box-shadow: inset 0 0 0 1.5px rgba(255,214,130,.85), 0 10px 30px rgba(255,180,80,.35); }
body.mikulas .login-link:hover { background: rgba(255,210,122,.12) !important; }
body.mikulas .login-title { text-shadow: 0 0 24px rgba(255,220,150,.35), 0 2px 10px rgba(0,0,0,.6); }
/* Pole a checkbox jen ve formuláři — světlé dialogy (zapomenuté heslo, 2FA) se nemění */
body.mikulas .login-wrap .q-field--outlined.q-field--highlighted .q-field__control:after { border-color: #ffd27a; }
body.mikulas .login-wrap .q-field--focused .q-field__control { box-shadow: 0 0 0 4px rgba(255,210,122,.2); }
body.mikulas .login-wrap .q-field--focused .q-field__label { color: #ffe2a6 !important; }
body.mikulas .login-chk .q-checkbox__inner--truthy .q-checkbox__bg { background: #e3262f; border-color: #e3262f; }

/* Nebe: hvězdy, srpek měsíce s mráčky, padající hvězdy */
.mk-hvezdy { position: absolute; left: 0; right: 0; top: 0; height: 45vh; }
.mk-hvezdy i { position: absolute; border-radius: 50%; background: #fff;
               box-shadow: 0 0 6px rgba(220,230,255,.9); animation: mk-trpyt var(--t) ease-in-out var(--zp) infinite; }
.mk-mesic { position: absolute; top: 11vh; right: 11vw; width: 10em; height: 10em; }
.mk-mesic::before { content: ''; position: absolute; inset: -60%; border-radius: 50%;
                    background: radial-gradient(closest-side, rgba(255,240,200,.28), rgba(255,240,200,0));
                    animation: mk-svit 6s ease-in-out infinite; }
.mk-mesic-srpek { position: relative; width: 100%; filter: drop-shadow(0 0 .9em rgba(255,236,180,.6)); }
.mk-mrak { position: absolute; fill: url(#mkMrak); opacity: .85; }
.mk-mrak.mk-m1 { width: 10em; left: -5em; top: 4.6em; animation: mk-mrak 34s ease-in-out infinite alternate; }
.mk-mrak.mk-m2 { width: 7em; left: 3em; top: 1.2em; opacity: .6; animation: mk-mrak 27s ease-in-out -9s infinite alternate-reverse; }
.mk-meteor { position: absolute; width: 9em; height: 2px; border-radius: 2px; opacity: 0;
             background: linear-gradient(90deg, #fff, rgba(200,220,255,.6) 20%, rgba(200,220,255,0));
             box-shadow: 0 0 6px rgba(255,255,255,.7); transform-origin: 0 50%;
             animation: mk-meteor var(--t) ease-in var(--zp) infinite; }

/* Sněžení */
.mk-snih { position: absolute; inset: 0; }
.mk-snih i { position: absolute; top: -12px; animation: mk-pad var(--t) linear var(--zp) infinite; }
.mk-snih i::before { content: ''; position: absolute; inset: 0; border-radius: 50%; background: #fff;
                     animation: mk-vlnit var(--s) ease-in-out var(--zp) infinite alternate; }
.mk-snih-zadni i::before { opacity: .6; }
.mk-snih-predni i::before { opacity: .85; filter: blur(1.2px); box-shadow: 0 0 6px rgba(255,255,255,.6); }

/* Zasněžená zem, chaloupky a kostelík */
.mk-zeme { position: absolute; left: 0; right: 0; bottom: 0; width: 100%; height: 12em; }
.mk-vesnice { position: absolute; left: 17vw; bottom: 3.6em; width: 19em; }
.mk-kostel { position: absolute; right: 20.5vw; bottom: 4em; width: 13em; }
.mk-okno-zhasina { animation: mk-okno 13s steps(1) infinite; }
.mk-dym { transform-box: fill-box; transform-origin: center; animation: mk-dym 4s ease-out infinite; }

/* Čertí oči ve tmě */
.mk-tma-oci { position: absolute; opacity: 0; animation: mk-oci-tma var(--t) ease-in-out var(--zp) infinite; }
.mk-tma-oci svg { filter: drop-shadow(0 0 .35em var(--barva)); }
.mk-tma-oci path { fill: var(--barva); }
.mk-tma-oci ellipse { fill: #1a0500; }
.mk-mrk { transform-box: fill-box; transform-origin: center; animation: mk-mrk 3.7s ease-in-out infinite; }
.mk-zornice { animation: mk-rozhled 7s ease-in-out var(--zp) infinite; }

/* Anděl s hvězdou */
.mk-andel { position: absolute; left: 13vw; top: 31vh; width: 10em; }
.mk-andel-vznasej { position: relative; animation: mk-vznasej 3.6s ease-in-out infinite alternate; }
.mk-andel-svg { filter: drop-shadow(0 0 1em rgba(210,225,255,.45)); transition: filter .3s; }
.mk-kridlo-l, .mk-kridlo-p { transform-box: view-box; }
.mk-kridlo-l { transform-origin: 54px 66px; animation: mk-mav-l 1.1s ease-in-out infinite alternate; }
.mk-kridlo-p { transform-origin: 66px 66px; animation: mk-mav-p 1.1s ease-in-out infinite alternate; }
.mk-svatozar { animation: mk-svit 2.4s ease-in-out infinite; }
.mk-hulka { transform-box: fill-box; transform-origin: center; animation: mk-hulka 2.2s ease-in-out infinite; }
.mk-jiskricky { position: absolute; left: 82.5%; top: 18.5%; }
.mk-jiskricky i { position: absolute; width: .9em; height: .9em; margin: -.45em; background: #ffd96b; opacity: 0;
                  clip-path: polygon(50% 0, 61% 39%, 100% 50%, 61% 61%, 50% 100%, 39% 61%, 0 50%, 39% 39%); }

/* Jedlové větve s ozdobami v horních rozích */
.mk-vetev { position: absolute; top: 0; width: 26em; filter: drop-shadow(0 .4em .6em rgba(0,0,0,.5)); }
.mk-vetev.mk-l { left: 0; }
.mk-vetev.mk-p { right: 0; transform: scaleX(-1); }
.mk-houp { transform-box: view-box; animation: mk-houp 3.2s ease-in-out infinite alternate; }

/* Světelný řetěz přes horní okraj a ozdoby na nitích */
.mk-girlanda { position: absolute; left: 0; right: 0; top: 0; height: 6em; }
.mk-girlanda svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.mk-girlanda i { position: absolute; width: .55em; height: .82em; margin: .16em 0 0 -.275em;
                 border-radius: 50% 50% 50% 50% / 40% 40% 60% 60%; background: var(--b);
                 box-shadow: 0 0 .6em .12em var(--b); animation: mk-blik 1.8s steps(1) infinite; }
.mk-girlanda i::before { content: ''; position: absolute; left: .13em; right: .13em; top: -.2em; height: .26em;
                         border-radius: .06em; background: #39404c; }
.mk-zaves { position: absolute; translate: -50% 0; transform-origin: 50% 0;
            animation: mk-houpej var(--t) ease-in-out infinite alternate; }
.mk-zaves-nit { width: 1.5px; margin: 0 auto -2px; background: linear-gradient(rgba(240,215,150,.35), rgba(240,215,150,.85)); }
.mk-zaves svg { width: 100%; filter: drop-shadow(0 .3em .5em rgba(0,0,0,.45)); }
.mk-zaves-zari svg { filter: drop-shadow(0 0 .9em rgba(255,214,110,.8)); }

/* Velké sněhové krystaly */
.mk-krystal { position: absolute; opacity: .65; filter: drop-shadow(0 0 .3em rgba(200,225,255,.9));
              animation: mk-toc var(--t) linear infinite; }

.mk-trpytky i { position: absolute; width: var(--s); height: var(--s); background: #fff6d0; opacity: 0;
                clip-path: polygon(50% 0, 58% 42%, 100% 50%, 58% 58%, 50% 100%, 42% 58%, 0 50%, 42% 42%);
                animation: mk-trpytka var(--t) ease-in-out var(--zp) infinite; }

/* Čert na měsíci */
.mk-mesic-cert { position: absolute; inset: 0; width: 100%; height: 100%; }
.mk-mc-noha1, .mk-mc-noha2, .mk-mc-ocas, .mk-mc-vlasec { transform-box: view-box; }
.mk-mc-noha1 { transform-origin: 56px 72px; animation: mk-kopej 1.4s ease-in-out infinite alternate; }
.mk-mc-noha2 { transform-origin: 63px 72px; animation: mk-kopej 1.4s ease-in-out -.7s infinite alternate; }
.mk-mc-ocas { transform-origin: 54px 72px; animation: mk-ocas 2s ease-in-out infinite alternate; }
.mk-mc-vlasec { transform-origin: 106px 34px; animation: mk-houpej 2.6s ease-in-out infinite alternate; }
.mk-mesic .mk-bublina { left: auto; right: 104%; bottom: 46%; translate: 0 0; transform-origin: 100% 50%; }
.mk-mesic .mk-bublina::after { left: 100%; top: 50%; translate: -1px -50%;
                               border-top-color: transparent; border-left-color: #fffdf6; }

/* Les na zadním kopci, ozdobený smrk vpravo */
.mk-les { position: absolute; left: 0; right: 0; bottom: 0; height: 12em; }
.mk-les-strom { position: absolute; translate: -50% 0; opacity: .92; }
.mk-strom { position: absolute; right: -1.5em; bottom: 1.4em; width: 16em; filter: drop-shadow(0 .4em 1em rgba(0,0,0,.5)); }
.mk-strom .l0, .mk-strom .l1, .mk-strom .l2 { animation: mk-blik 1.6s steps(1) infinite; }
.mk-strom .l1 { animation-delay: -.53s; }
.mk-strom .l2 { animation-delay: -1.07s; }
.mk-strom-hvezda { transform-box: fill-box; transform-origin: center; animation: mk-hulka 3s ease-in-out infinite; }

/* Sněhulák, sáňky s dárky, krmelec se srnkou a zajícem */
.mk-zem-postava { position: absolute; bottom: 1.4em; }
.mk-zem-postava > svg { filter: drop-shadow(0 .3em .6em rgba(0,0,0,.45)); }
.mk-snehulak { left: 33.5vw; width: 7.5em; }
.mk-sanky { left: 41vw; width: 10em; bottom: 1.2em; }
.mk-krmelec { left: 50.5vw; width: 18em; bottom: 1.3em; }
.mk-snehulak .mk-bublina { left: 92%; bottom: 62%; translate: 0 0; transform-origin: 0 50%; }
.mk-snehulak .mk-bublina::after { left: auto; right: 100%; top: 50%; translate: 1px -50%;
                                  border-top-color: transparent; border-right-color: #fffdf6; }
.mk-snehulak-klobouk { transform-box: view-box; transform-origin: 50px 24px; transition: transform .3s cubic-bezier(.3,1.6,.5,1); }
.mk-sala { transform-box: view-box; transform-origin: 57px 55px; animation: mk-sala 1.8s ease-in-out infinite alternate; }
.mk-srna-hlava { transform-box: view-box; transform-origin: 86px 84px; animation: mk-zere 9s ease-in-out infinite; }
.mk-ucho, .mk-zajic-usi { transform-box: fill-box; transform-origin: 50% 100%; animation: mk-ucho 5s ease-in-out infinite; }
.mk-zajic-usi { animation-duration: 3.3s; animation-delay: -1s; }

/* Průvod popředím: čert s pytlem brambor JIP a čertík s vidlemi */
.mk-pruvod { position: absolute; left: 24vw; bottom: .7em; width: 28em; animation: mk-pr-chuze 42s linear -8s infinite; }
.mk-pruvod > svg { display: block; width: 100%; overflow: visible; filter: drop-shadow(0 .3em .6em rgba(0,0,0,.45)); }
.mk-pruvod .mk-bublina { left: 88%; bottom: 96%; }
.mk-ct-noha1, .mk-ct-noha2, .mk-ct-bob, .mk-ct-ocas, .mk-ct-retez, .mk-ct-jazyk, .mk-pb-tah, .mk-ck-trasa, .mk-ck-skok,
.mk-ck-salto, .mk-ck-noha1, .mk-ck-noha2, .mk-ck-ocas, .mk-ck-ruka, .mk-ck-vidle { transform-box: view-box; }
.mk-ct-noha1 { transform-origin: 255px 100px; animation: mk-pr-krok .45s ease-in-out infinite alternate; }
.mk-ct-noha2 { transform-origin: 249px 100px; animation: mk-pr-krok .45s ease-in-out -.45s infinite alternate; }
.mk-ct-bob { animation: mk-pr-bob .45s ease-in-out infinite; }
.mk-ct-ocas { transform-origin: 240px 100px; animation: mk-pr-ocas 1.3s ease-in-out infinite alternate; }
.mk-ct-retez { transform-origin: 244px 95px; animation: mk-houp .9s ease-in-out infinite alternate; }
.mk-ct-jazyk { transform-origin: 279px 53px; animation: mk-pr-jazyk .9s ease-in-out infinite alternate; }
.mk-pb-tah { transform-origin: 150px 140px; animation: mk-pr-tah .45s ease-in-out infinite; }
.mk-pr-brambora, .mk-pr-prasan { transform-box: fill-box; transform-origin: center; }
.mk-pr-brambora { animation: mk-pr-brambora 3.4s ease-in-out infinite; }
.mk-pr-prasan { animation: mk-pr-prasan 1.1s ease-out infinite; }
.mk-ck-trasa { animation: mk-pr-trasa 7s ease-in-out infinite; }
.mk-ck-skok { animation: mk-pr-skok .3s ease-out infinite alternate; }
.mk-ck-salto { transform-origin: 34px 112px; animation: mk-pr-salto 7s ease-in-out infinite; }
.mk-ck-noha1 { transform-origin: 30px 126px; animation: mk-pr-krok .15s linear infinite alternate; }
.mk-ck-noha2 { transform-origin: 38px 126px; animation: mk-pr-krok .15s linear -.15s infinite alternate; }
.mk-ck-ocas { transform-origin: 26px 124px; animation: mk-pr-ocas .4s ease-in-out infinite alternate; }
.mk-ck-ruka { transform-origin: 27px 110px; animation: mk-pr-mava .35s ease-in-out infinite alternate; }
.mk-ck-vidle { transform-origin: 47px 108px; animation: mk-pr-vidle .6s ease-in-out infinite alternate; }

/* Zvonek na stuze */
.mk-zvonek { position: absolute; top: -8em; left: 11em; width: 3.6em; transform-origin: 50% 0;
             animation: mk-houpej 4.5s ease-in-out infinite alternate; }
.mk-zvonek-svg { filter: drop-shadow(0 .3em .6em rgba(0,0,0,.45)); }
.mk-zvonek-telo { transform-box: view-box; transform-origin: 30px 238px; }
.mk-srdce { transform-box: fill-box; transform-origin: center; }
.mk-zvonek .mk-bublina { left: 125%; bottom: 7%; translate: 0 0; transform-origin: 0 50%; }
.mk-zvonek .mk-bublina::after { left: auto; right: 100%; top: 50%; translate: 1px -50%;
                                border-top-color: transparent; border-right-color: #fffdf6; }

/* Mikuláš s košem, čert v pytli a lucerny dole v rozích */
.mk-skupina { position: absolute; bottom: .9em; display: flex; align-items: flex-end; gap: .5em; }
.mk-skupina.mk-l { left: 2.5vw; }
.mk-skupina.mk-p { right: 2.5vw; }
.mk-zar { position: absolute; left: -15%; right: -15%; bottom: -2em; height: 10em;
          background: radial-gradient(closest-side, rgba(255,190,90,.3), rgba(255,190,90,0));
          animation: mk-svit 2.8s ease-in-out infinite; }
.mk-postava { position: relative; }
.mk-postava > svg { filter: drop-shadow(0 .4em 1em rgba(0,0,0,.5)); }
.mk-mikulas { width: 11em; }
.mk-kos { width: 7.5em; margin-left: -2.2em; margin-bottom: -.3em; }
.mk-pytel { width: 13em; margin-bottom: -.2em; }
.mk-lucerna { position: relative; filter: drop-shadow(0 0 .9em rgba(255,190,90,.6)); margin-bottom: -.2em; }
.mk-plamen { transform-box: fill-box; transform-origin: 50% 100%; animation: mk-plamen 1.6s ease-in-out infinite alternate; }
.mk-ruka { transform-box: view-box; transform-origin: 116px 128px; animation: mk-mavani 5s ease-in-out infinite; }
.mk-hlava { transform-box: view-box; transform-origin: 85px 104px; }
.mk-cert { transform-box: view-box; transform-origin: 82px 96px; animation: mk-vykuk 9s ease-in-out infinite; }
.mk-cert-zornice { animation: mk-cert-oci 9s ease-in-out infinite; }
.mk-jazyk { transform-box: fill-box; transform-origin: 50% 0; }
.mk-ocas { transform-box: view-box; transform-origin: 150px 150px; animation: mk-ocas 1.4s ease-in-out infinite alternate; }

/* Přední závěj přes nohy postav */
.mk-zavej { position: absolute; left: 0; right: 0; bottom: -.3em; width: 100%; height: 2.6em; }

/* Bubliny s vtípky */
.mk-bublina { position: absolute; left: 50%; bottom: 100%; translate: -50% 0; padding: .45em .85em; border-radius: 1em;
              background: #fffdf6; color: #8a0f18; font-family: inherit; font-weight: 800; font-size: 1.3em;
              line-height: 1.1; white-space: nowrap; box-shadow: 0 .3em 1em rgba(0,0,0,.4);
              opacity: 0; scale: .3; transform-origin: 50% 100%;
              transition: opacity .18s, scale .3s cubic-bezier(.3,1.7,.5,1); }
.mk-bublina::after { content: ''; position: absolute; left: 50%; top: 100%; translate: -50% -1px;
                     border: .45em solid transparent; border-top-color: #fffdf6; }
.mk-mikulas .mk-bublina { left: 74%; bottom: 99%; }
.mk-pytel .mk-bublina { left: 44%; bottom: 93%; }

/* Schované vtípky — kurzor chytají jen tyto tvary, zbytek vrstvy kliky propouští */
.mk-mikulas-svg *, .mk-kos-svg *, .mk-pytel-svg *, .mk-andel-svg *, .mk-zvonek-telo *, .mk-zaves svg *,
.mk-houp *, .mk-mesic-cert *, .mk-snehulak-svg *, .mk-sanky-svg *, .mk-krmelec-svg *,
.mk-pruvod-svg * { pointer-events: visiblePainted; }
.mk-snehulak:hover .mk-bublina, .mk-mesic:hover .mk-bublina { opacity: 1; scale: 1; }
.mk-snehulak:hover .mk-snehulak-klobouk { transform: translateY(-10px) rotate(-14deg); }
.mk-sanky:hover .mk-darek, .mk-krmelec:hover .mk-zajic { animation: mk-hop .22s ease-out 4 alternate; }
.mk-sanky:hover .mk-darek:nth-child(3) { animation-delay: .08s; }
.mk-krmelec:hover .mk-srna-hlava { animation: none; transform: rotate(-16deg); transition: transform .4s; }
.mk-mesic:hover .mk-mc-noha1, .mk-mesic:hover .mk-mc-noha2 { animation-duration: .22s; }
.mk-zaves:hover, .mk-houp:hover { animation-duration: .5s; }
.mk-pruvod:hover .mk-bublina { opacity: 1; scale: 1; }
.mk-pruvod:hover, .mk-pruvod:hover .mk-ct * { animation-play-state: paused; }
.mk-pruvod:hover .mk-ct-jazyk { animation: mk-pr-jazyk .12s ease-in-out infinite alternate; }
.mk-mikulas:hover .mk-bublina, .mk-pytel:hover .mk-bublina, .mk-zvonek:hover .mk-bublina { opacity: 1; scale: 1; }
.mk-mikulas:hover .mk-hlava { animation: mk-kyvni .6s ease-in-out 2; }
.mk-pytel:hover .mk-cert { animation: none; transform: translateY(-8px); transition: transform .25s cubic-bezier(.2,1.6,.4,1); }
.mk-pytel:hover .mk-jazyk { animation: mk-jazyk .15s ease-in-out infinite alternate; }
.mk-pytel:hover .mk-ocas { animation-duration: .25s; }
.mk-andel:hover .mk-andel-svg { filter: drop-shadow(0 0 1.4em rgba(255,226,140,.95)); }
.mk-andel:hover .mk-jiskricky i { animation: mk-jiskra 1s ease-out forwards; }
.mk-zvonek:hover .mk-zvonek-telo { animation: mk-cink .22s ease-in-out infinite; }
.mk-zvonek:hover .mk-srdce { animation: mk-srdce .11s linear infinite alternate; }
.mk-kos:hover .mk-nadilka > g:not([transform]) { animation: mk-hop .22s ease-out 4 alternate; }
.mk-kos:hover .mk-nadilka > g:nth-child(2n) { animation-delay: .08s; }

@keyframes mk-trpytka { 0%, 100% { opacity: 0; transform: scale(.3) rotate(0); }
                        50% { opacity: 1; transform: scale(1) rotate(45deg); } }
@keyframes mk-blik { 50% { opacity: .3; } }
@keyframes mk-houp { from { transform: rotate(-6deg); } to { transform: rotate(6deg); } }
@keyframes mk-toc { to { rotate: 360deg; } }
@keyframes mk-kopej { from { transform: rotate(-12deg); } to { transform: rotate(14deg); } }
@keyframes mk-sala { from { transform: rotate(-6deg) skewY(2deg); } to { transform: rotate(9deg) skewY(-3deg); } }
@keyframes mk-zere { 0%, 45%, 100% { transform: rotate(0); } 52%, 72% { transform: rotate(-15deg); } 80% { transform: rotate(0); } }
@keyframes mk-ucho { 0%, 86%, 100% { transform: rotate(0); } 90% { transform: rotate(-18deg); } 94% { transform: rotate(7deg); } }
@keyframes mk-pr-chuze { from { translate: calc(-24vw - 29em) 0; } 86%, to { translate: calc(76vw + 1em) 0; } }
@keyframes mk-pr-krok { from { transform: rotate(-22deg); } to { transform: rotate(22deg); } }
@keyframes mk-pr-bob { 50% { transform: translateY(-2.5px); } }
@keyframes mk-pr-ocas { from { transform: rotate(-10deg); } to { transform: rotate(12deg); } }
@keyframes mk-pr-jazyk { from { transform: rotate(-8deg); } to { transform: rotate(10deg); } }
@keyframes mk-pr-tah { 50% { transform: rotate(-2.5deg) translateY(-1px); } }
@keyframes mk-pr-prasan { from { opacity: .9; transform: translate(0, 0) scale(.5); }
                          to { opacity: 0; transform: translate(-16px, -8px) scale(1.7); } }
@keyframes mk-pr-brambora { 0%, 60% { transform: translateY(0) rotate(0); } 72% { transform: translateY(-20px) rotate(170deg); }
                            84%, 100% { transform: translateY(0) rotate(360deg); } }
@keyframes mk-pr-trasa { 0%, 30% { transform: translate(0, 0); } 36% { transform: translate(52px, -128px); }
                         42%, 66% { transform: translate(102px, -100px); } 73% { transform: translate(52px, -132px); }
                         80%, 100% { transform: translate(0, 0); } }
@keyframes mk-pr-salto { 0%, 30% { transform: rotate(0); } 42%, 66% { transform: rotate(360deg); } 80%, 100% { transform: rotate(0); } }
@keyframes mk-pr-skok { from { transform: translateY(0); } to { transform: translateY(-8px); } }
@keyframes mk-pr-mava { from { transform: rotate(-25deg); } to { transform: rotate(20deg); } }
@keyframes mk-pr-vidle { from { transform: rotate(-16deg); } to { transform: rotate(14deg); } }
@keyframes mk-pad { to { transform: translateY(calc(100vh + 30px)); } }
@keyframes mk-vlnit { from { transform: translateX(calc(var(--k) * -1)); } to { transform: translateX(var(--k)); } }
@keyframes mk-trpyt { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }
@keyframes mk-svit { 0%, 100% { opacity: .75; } 50% { opacity: 1; } }
@keyframes mk-mrak { from { transform: translateX(-2.5em); } to { transform: translateX(2.5em); } }
@keyframes mk-meteor { 0%, 88% { opacity: 0; transform: rotate(-20deg) translateX(0); }
                       90% { opacity: 1; } 96%, 100% { opacity: 0; transform: rotate(-20deg) translateX(-30vw); } }
@keyframes mk-okno { 0%, 45%, 72%, 100% { opacity: 1; } 46%, 71% { opacity: .12; } }
@keyframes mk-dym { 0% { opacity: 0; transform: translate(0, 0) scale(.5); } 15% { opacity: .6; }
                    100% { opacity: 0; transform: translate(12px, -42px) scale(2); } }
@keyframes mk-oci-tma { 0%, 42%, 100% { opacity: 0; } 50%, 90% { opacity: 1; } }
@keyframes mk-mrk { 0%, 90%, 96%, 100% { transform: scaleY(1); } 93% { transform: scaleY(.08); } }
@keyframes mk-rozhled { 0%, 22%, 100% { transform: translateX(0); } 30%, 52% { transform: translateX(-2.4px); }
                        60%, 82% { transform: translateX(2.4px); } }
@keyframes mk-vznasej { from { transform: translateY(-.8em) rotate(-3deg); } to { transform: translateY(.8em) rotate(3deg); } }
@keyframes mk-mav-l { from { transform: rotate(10deg); } to { transform: rotate(-14deg); } }
@keyframes mk-mav-p { from { transform: rotate(-10deg); } to { transform: rotate(14deg); } }
@keyframes mk-hulka { 0%, 100% { transform: scale(1) rotate(0); } 50% { transform: scale(1.2) rotate(18deg); } }
@keyframes mk-jiskra { 0% { opacity: 0; transform: translate(0, 0) scale(.2); } 20% { opacity: 1; }
                       100% { opacity: 0; transform: translate(var(--x), var(--y)) scale(1) rotate(120deg); } }
@keyframes mk-houpej { from { transform: rotate(-4deg); } to { transform: rotate(4deg); } }
@keyframes mk-cink { 0%, 100% { transform: rotate(0); } 25% { transform: rotate(-18deg); } 75% { transform: rotate(18deg); } }
@keyframes mk-srdce { from { transform: translateX(-4px); } to { transform: translateX(4px); } }
@keyframes mk-plamen { 0% { transform: scale(.9, 1) skewX(-4deg); } 50% { transform: scale(1.06, 1.12) skewX(2deg); }
                       100% { transform: scale(.96, .92) skewX(-1deg); } }
@keyframes mk-mavani { 0%, 55%, 90%, 100% { transform: rotate(0); } 62%, 76% { transform: rotate(-16deg); }
                       69%, 83% { transform: rotate(7deg); } }
@keyframes mk-kyvni { 0%, 100% { transform: rotate(0); } 35% { transform: rotate(-6deg) translateY(1.5px); }
                      70% { transform: rotate(4deg); } }
@keyframes mk-vykuk { 0%, 50%, 100% { transform: translateY(46px); } 57%, 63% { transform: translateY(0); }
                      68% { transform: translateY(0) rotate(-7deg); } 75% { transform: translateY(0) rotate(6deg); }
                      80% { transform: translateY(0); } 86% { transform: translateY(46px); } }
@keyframes mk-cert-oci { 0%, 60%, 100% { transform: translateX(0); } 66%, 70% { transform: translateX(-2px); }
                         74%, 78% { transform: translateX(2px); } }
@keyframes mk-jazyk { from { transform: scaleY(1) rotate(-8deg); } to { transform: scaleY(1.2) rotate(8deg); } }
@keyframes mk-ocas { from { transform: rotate(-10deg); } to { transform: rotate(14deg); } }
@keyframes mk-hop { to { transform: translateY(-7px); } }

@media (prefers-reduced-motion: reduce) {
    .mk *, .mk *::before, .mk-mesic::before { animation: none !important; transition: none !important; }
    .mk-snih, .mk-meteor { display: none; }
}
/* Úzký displej: formulář zabírá skoro celou šířku — anděl, oči, vesnička a koš by lezli pod text */
@media (max-width: 760px) {
    .mk .mk-andel, .mk .mk-tma-oci, .mk .mk-vesnice, .mk .mk-kostel, .mk .mk-kos, .mk .mk-krystal,
    .mk .mk-zem-postava, .mk .mk-trpytky { display: none; }
    .mk-hvezdy { height: 20vh; }
}
''' + _SNIH_CSS


def vykresli_dekorace() -> None:
    """Nasadí na body třídu `mikulas` a vykreslí dekorace (fixní vrstva pod formulářem).
    CSS (`CSS`) musí být v hlavičce stránky už předem — po připojení klienta ji NiceGUI nemění."""
    ui.query('body').classes(add='mikulas')
    ui.html(_HTML, sanitize=False).classes('mk').props('aria-hidden=true')
