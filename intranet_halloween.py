"""Halloweenský vzhled přihlašovací obrazovky — noční scéna s úplňkem, netopýry,
hvězdami, blesky, strašidelným domem, hřbitůvkem, mlhou, duchy a očima ve tmě;
v popředí dýně, pavučiny, čarodějnice na koštěti, svíčky, kostlivci za dýněmi a vosk stékající
po polích formuláře. Kdo jezdí myší, najde vtípky: dýně se zachichotá, duch
houkne „Bú!" a pavouk uteče nahoru po vlákně.

Zapíná se sám celý říjen (`je_halloween()`), mimo něj se nepošle ani řádek.
Audit konzole ho přepíná ručně: /halloween on|off vynutí stav bez ohledu na
datum, /halloween auto vrátí řízení datu (`nastav_rezim()`).
Jen inline SVG + CSS animace, žádné soubory ani JS navíc. Styly jsou scoped
na `body.halloween`, kterou nasazuje až `vykresli_dekorace()` na přihlašovací
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
_KLIC_NASTAVENI = 'halloween'


def je_halloween(dnes: date | None = None) -> bool:
    """Ruční přepínač z konzole má přednost; jinak běží celý říjen
    a 1. listopadu se sám vypne."""
    rucne = intranet_data.nacti_nastaveni_intranetu().get(_KLIC_NASTAVENI)
    if isinstance(rucne, bool):
        return rucne
    return (dnes or date.today()).month == 10


def nastav_rezim(zapnout: bool | None) -> None:
    """/halloween on|off|auto — True/False vynutí stav, None vrátí automatiku."""
    n = intranet_data.nacti_nastaveni_intranetu()
    if zapnout is None:
        n.pop(_KLIC_NASTAVENI, None)
    else:
        n[_KLIC_NASTAVENI] = bool(zapnout)
    intranet_data.uloz_nastaveni_intranetu(n)


def _pavucina() -> str:
    """Cesta rohové pavučiny: paprsky z rohu (0,0) a mezi nimi prověšená vlákna."""
    uhly = [math.radians(u) for u in (0, 14, 29, 45, 61, 76, 90)]
    d = ''.join(f'M0 0L{300 * math.cos(u):.0f} {300 * math.sin(u):.0f}' for u in uhly)
    for r in (34, 66, 102, 142, 186, 236):
        d += f'M{r} 0'
        for u0, u1 in zip(uhly, uhly[1:]):
            s = (u0 + u1) / 2   # kontrolní bod blíž k rohu => vlákno se prověsí
            d += (f'Q{r * .84 * math.cos(s):.1f} {r * .84 * math.sin(s):.1f} '
                  f'{r * math.cos(u1):.1f} {r * math.sin(u1):.1f}')
    return d


# Dílky dýně (cx, cy, rx, ry) ve viewBoxu 200×176 — vnější první, prostřední navrch
_DYNE_ELIPSY = ((54, 112, 48, 57), (146, 112, 48, 57), (78, 110, 44, 62), (122, 110, 44, 62), (100, 109, 40, 64))


def _vrch_dyne(x: float) -> float:
    """Horní obrys dýně v bodě x — tam se jí drží prsty kostlivce."""
    return min(cy - ry * math.sqrt(1 - ((x - cx) / rx) ** 2) for cx, cy, rx, ry in _DYNE_ELIPSY if abs(x - cx) < rx)


def _dyne(trida: str, za: str = '', pred: str = '') -> str:
    """Dýně; `za` se kreslí za ni, `pred` před ni (svg přetéká viewBox, takže smí i nad ni)."""
    elipsy = ''.join(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}"/>' for cx, cy, rx, ry in _DYNE_ELIPSY)
    return f'''<svg class="hw-dyne {trida}" viewBox="0 0 200 176">{za}
      <path d="M93 47C90 34 95 22 106 12L115 19C107 26 104 35 107 48Z" fill="url(#hwStonek)"/>
      <g fill="url(#hwDyne)" stroke="#6e2a03" stroke-opacity=".5" stroke-width="2">{elipsy}</g>
      <ellipse cx="68" cy="84" rx="8" ry="20" fill="#fff" opacity=".13" transform="rotate(22 68 84)"/>
      <path class="hw-tvar" fill="url(#hwTvar)" stroke="#5c2200" stroke-width="2.5"
            stroke-linejoin="round" filter="url(#hwZar)"
            d="M58 100H88L76 75ZM112 100H142L124 75ZM93 118H107L100 106Z
               M50 124Q62 133 78 134V144H90V136Q100 137 110 136V144H122V134Q138 133 150 124
               Q140 154 106 160V151H94V160Q60 154 50 124Z"/>{pred}
    </svg>'''


def _svicka(vosk: int, zpozdeni: float) -> str:
    """Svíčka; `vosk` = výška vosku ve viewBoxu, `zpozdeni` rozhodí plápolání svíček vůči sobě."""
    return f'''<svg class="hw-svicka" viewBox="0 0 40 {56 + vosk}" style="--zp:-{zpozdeni}s">
      <circle class="hw-svatozar" cx="20" cy="30" r="34" fill="url(#hwSvatozar)"/>
      <path class="hw-plamen" d="M20 8C27 22 29 33 20 46C11 33 13 22 20 8Z" fill="url(#hwPlamen)"/>
      <path d="M20 44V53" stroke="#2a1a10" stroke-width="2" stroke-linecap="round"/>
      <rect x="6" y="52" width="28" height="{vosk}" rx="2" fill="url(#hwVosk)"/>
      <path fill="#fbf2dc" d="M6 52H34V62Q34 66 31.5 66Q29 66 29 62V58Q26.5 59 24 58V71Q24 75 21.5 75
            Q19 75 19 71V58Q15.5 59 12 58V64Q12 67 10 67Q8 67 8 64V58Q7 58 6 58Z"/>
      <ellipse cx="20" cy="53" rx="14" ry="3" fill="#fff8e8"/>
    </svg>'''


def _kost(x1: float, y1: float, x2: float, y2: float, t: float) -> str:
    """Kost z bodu do bodu tloušťky `t`, na obou koncích kloubní hrbolky."""
    dl = math.hypot(x2 - x1, y2 - y1)
    nx, ny = (y1 - y2) / dl * t * .42, (x2 - x1) / dl * t * .42
    hrbolky = ''.join(f'<circle cx="{x + s * nx:.1f}" cy="{y + s * ny:.1f}" r="{t * .5:.1f}"/>'
                      for x, y in ((x1, y1), (x2, y2)) for s in (-1, 1))
    return f'<path d="M{x1:g} {y1:g}L{x2:g} {y2:g}" stroke-width="{t:g}"/>{hrbolky}'


# Lebka se středem mezi očima v (0,0); spodní čelist (hw-celist) cvaká zuby
_LEBKA = '''<path class="hw-lebka" d="M0-30C17-30 28-18 28-2C28 7 24 12 20 15L19 22H-19L-20 15C-24 12-28 7-28-2
        C-28-18-17-30 0-30Z"/>
  <g class="hw-tma"><ellipse cx="-11" cy="0" rx="8" ry="8.6"/><ellipse cx="11" cy="0" rx="8" ry="8.6"/>
    <path d="M0 7L-3.6 14Q0 16 3.6 14Z"/><rect x="-17" y="17.5" width="34" height="10" rx="2"/></g>
  <g class="hw-oko" filter="url(#hwZar)"><circle cx="-10.5" cy="1" r="2.4"/><circle cx="10.5" cy="1" r="2.4"/></g>
  <rect class="hw-lebka" x="-15" y="18" width="30" height="5" rx="1"/>
  <path class="hw-zuby" d="M-10 18v5M-5 18v5M0 18v5M5 18v5M10 18v5"/>
  <g class="hw-celist"><path class="hw-lebka" d="M-19 22.5H19L18 28C17 35 9 39 0 39C-9 39-17 35-18 28Z"/>
    <path class="hw-zuby" d="M-14 27H14M-9.5 22.5v4.5M-4.75 22.5v4.5M0 22.5v4.5M4.75 22.5v4.5M9.5 22.5v4.5"/></g>'''


def _kostlivec_mava() -> str:
    """Kostlivec za velkou dýní (kreslí se za ni): nad ni kouká lebka, ramena a horní žebra,
    pravou rukou mává — předloktí se otáčí v lokti, který je v počátku své skupiny."""
    obratle = ''.join(f'<rect x="-4" y="{y}" width="8" height="5.2" rx="1.8"/>' for y in range(39, 150, 7))
    zebra = ''.join(f'<path d="M{-3 * s} {y:g}C{-.75 * w * s:g} {y - 6:g} {-(w + 2) * s:g} {y + 2:g} '
                    f'{-(w - 3) * s:g} {y + 12:g}"/>'
                    for i, w in enumerate((23, 27, 28.5, 26.5)) for y in (56 + 11.5 * i,) for s in (1, -1))
    return f'''<g transform="translate(54 -28.5) scale(1.25)">
      <g class="hw-kost">{obratle}<path d="M0 52L-27 48M0 52L27 48" stroke-width="4.2"/>
        <rect x="-3.2" y="54" width="6.4" height="30" rx="3"/><g fill="none" stroke-width="3.6">{zebra}</g>
        {_kost(-29, 50, -32, 90, 5.5)}{_kost(29, 50, 46, 22, 5.5)}</g>
      <g transform="translate(46 22)"><g class="hw-mava hw-kost">{_kost(-2, -1, 5, -35, 3.2)}{_kost(2, 1, 10, -34, 3.2)}
        <ellipse cx="8" cy="-40" rx="5.5" ry="5"/>
        <path d="M4-42L0-55M7-44L6-59M10-44L12-58M13-41L18-52M3-37L-4-44" fill="none" stroke-width="2.4"/></g></g>
      {_LEBKA}
    </g>'''


def _kostlivec_vykuk() -> tuple[str, str]:
    """Lebka za střední dýní, která občas vykoukne, a prsty, které se přitom drží jejího okraje.
    Vrací (kresba za dýní, kresba před dýní)."""
    xs = (24, 31, 38, 74, 81, 88)
    prsty = ''.join(f'<path d="M{x} {t - 6:.1f}Q{x + 2} {t:.1f} {x + .5:g} {t + 7:.1f}"/>'
                    for x in xs for t in (_vrch_dyne(x),))
    klouby = ''.join(f'<circle cx="{x}" cy="{_vrch_dyne(x) - 5:.1f}" r="3.3"/>' for x in xs)
    return (f'<g transform="translate(56 34)"><g class="hw-vykuk">{_LEBKA}</g></g>',
            f'<g class="hw-kost hw-prsty"><g fill="none" stroke-width="5">{prsty}</g>{klouby}</g>')


# Vosk ze svíček na polích formuláře: kousky SVG v pozadí pseudo-elementu pole, formulář zůstává
# netknutý. Souřadnice kousku: horní hrana pole y=6, okraj pole x=6 (pseudo-element pole o 6 px
# přesahuje). Pravý roh je zrcadlo levého. Kapky = (střed x, půl šířky, konec y); nad textem pole
# končí nejdéle 8 px pod hranou, aby nezakryly popisek.
_VY = 11   # spodní okraj voskové vrstvy (5 px pod hranou pole)
_LESK = 'fill="none" stroke="#fff" stroke-opacity=".8" stroke-width="1.1" stroke-linecap="round"'


def _vosk_url(obsah: str, styl: str = '', sirka: int = 96, zrcadlit: bool = False) -> str:
    """Kousek vosku jako CSS url() s inline SVG — animace běží uvnitř obrázku."""
    z = f' transform="matrix(-1 0 0 1 {sirka} 0)"' if zrcadlit else ''
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{sirka}" height="72"><style>{styl}'
           '@media (prefers-reduced-motion:reduce){*{animation:none!important}}</style>'
           '<linearGradient id="v" gradientUnits="userSpaceOnUse" x2="0" y1="2" y2="52">'
           '<stop stop-color="#fffaf0"/><stop offset=".22" stop-color="#f6ead0"/>'
           f'<stop offset="1" stop-color="#d4b884"/></linearGradient><g fill="url(#v)"{z}>{obsah}</g></svg>')
    return 'url("data:image/svg+xml,' + quote(svg, safe=" =:/.,-()!*;'") + '")'


def _kapky(kapky) -> tuple[str, str]:
    """Spodní okraj vosku zprava doleva: dlouhé kapky se zúženým krčkem a baňkou, krátké jako boule.
    Vrací (kus cesty, lesky na baňkách)."""
    d = lesk = ''
    y = _VY
    for c, h, k in sorted(kapky, reverse=True):
        n = .75 * h   # polovina šířky krčku
        if k >= y + 4 + 2.1 * h:
            d += (f'L{c + h + 4:g} {y}C{c + h + 1:g} {y} {c + n:g} {y + 2} {c + n:g} {y + 4}V{k - 2.1 * h:g}'
                  f'C{c + n:g} {k - 1.5 * h:g} {c + h:g} {k - 1.6 * h:g} {c + h:g} {k - h:g}'
                  f'A{h:g} {h:g} 0 0 1 {c - h:g} {k - h:g}'
                  f'C{c - h:g} {k - 1.6 * h:g} {c - n:g} {k - 1.5 * h:g} {c - n:g} {k - 2.1 * h:g}'
                  f'V{y + 4}C{c - n:g} {y + 2} {c - h - 1:g} {y} {c - h - 4:g} {y}')
            lesk += f'<ellipse cx="{c - .35 * h:g}" cy="{k - 1.3 * h:g}" rx="{.3 * h:g}" ry="{.45 * h:g}"/>'
        else:
            d += (f'L{c + h + 3:g} {y}C{c + h:g} {y} {c + h:g} {k:g} {c:g} {k:g}'
                  f'C{c - h:g} {k:g} {c - h:g} {y} {c - h - 3:g} {y}')
    return d, f'<g fill="#fff" fill-opacity=".75">{lesk}</g>'


def _vosk_roh(konec: int, kapky, bok: float, perioda: float = 0, zpozdeni: float = 0,
              zrcadlit: bool = False) -> str:
    """Vosk přes horní roh pole: v rohu nejtlustší, po hraně se ztenčí do x=`konec`; kapky
    (jen mezi rohem a `konec` - 18) a stékání po boku délky `bok`. S `perioda` stékání pomalu
    teče, odkápne a zase se natahuje."""
    d_kapky, lesk = _kapky(kapky)
    k1, k2 = 18 + (konec - 38) * .35, 18 + (konec - 38) * .7
    yb = 12.7 + bok   # střed baňky stékání (začíná na y=16)
    tridy = ('class="s" ', 'class="b"') if perioda else ('', '')
    obsah = (f'<path d="M3.2 28C3.2 24 2.5 22 2.5 18A15.5 15.5 0 0 1 18 2.5C{k1:g} 1.8 {k2:g} 3.2 {konec - 20} 3'
             f'C{konec - 9} 3 {konec - 4} 4.2 {konec} 6.4C{konec - 5} 8.4 {konec - 12} 10.6 {konec - 18} {_VY}'
             f'{d_kapky}L18 {_VY}A7 7 0 0 0 11 18C11 21 9.8 23 9.8 28Z"/>{lesk}'
             f'<path d="M4 18A14 14 0 0 1 18 4C{k1:g} 3.3 {k2:g} 4.6 {konec - 21} 4.3" {_LESK}/>'
             f'<rect {tridy[0]}x="3.2" y="16" width="6.6" height="{bok:g}" rx="3.3"/><g {tridy[1]}>'
             f'<circle cx="6.5" cy="{yb:g}" r="4.4"/>'
             f'<ellipse cx="5" cy="{yb - 1.6:g}" rx="1.2" ry="1.7" fill="#fff" fill-opacity=".75"/></g>')
    styl = ''
    if perioda:
        obsah += f'<circle class="f" cx="6.5" cy="{yb:g}" r="3"/>'
        a, p = f' {perioda:g}s ease-in -{zpozdeni:g}s infinite', f'translateY(-{bok * .55:.1f}px)'
        styl = (f'.s{{transform-box:fill-box;transform-origin:50% 0;animation:s{a}}}.b{{animation:b{a}}}'
                f'.f{{opacity:0;animation:f{a}}}'
                '@keyframes s{0%{transform:scaleY(.45)}80%{transform:none}85%,to{transform:scaleY(.45)}}'
                f'@keyframes b{{0%{{transform:{p}}}80%{{transform:none}}85%,to{{transform:{p}}}}}'
                '@keyframes f{0%,79%{opacity:0;transform:none}80%{opacity:1;transform:none}'
                '97%,to{opacity:0;transform:translateY(24px)}}')
    return _vosk_url(obsah, styl, zrcadlit=zrcadlit)


def _vosk_stred(kapky, s: int = 130) -> str:
    """Louže vosku na horní hraně pole — uprostřed tlustá, ke krajům se ztenčí; kapky mezi 24 a s-24."""
    d_kapky, lesk = _kapky(kapky)
    return _vosk_url(
        f'<path d="M6 6.4C12 3.4 18 2.6 26 2.6C{s * .4:g} 1.8 {s * .62:g} 3.4 {s - 26} 2.8C{s - 18} 2.8 {s - 12} 3.4 '
        f'{s - 6} 6.4C{s - 11} 8.4 {s - 18} 10.6 {s - 24} {_VY}{d_kapky}L24 {_VY}C18 10.6 11 8.4 6 6.4Z"/>{lesk}'
        f'<path d="M14 4.6C18 3.6 22 3.4 26 3.4C{s * .4:g} 2.6 {s * .62:g} 4.2 {s - 24} 3.6" {_LESK}/>', sirka=s)


# Koště letí násadou napřed (doprava) — metla je vlevo. Na násadě sedí čarodějnice; přečnívá
# nad viewBox (.hw svg má overflow: visible), takže se nemění rozměr koštěte ani zarovnání jiskřiček.
# Plášť a vlasy jsou za koštětem, sukně s nohou, tělo a hlava před ním.
# Čarodějnice je zvětšená kolem místa, kde sedí na ose násady (186, 47) – ruka a noha zůstanou na koštěti.
_CAROD_ZVETSENI = 'translate(186 47) scale(1.4) translate(-186 -47)'
_KOSTE = f'''<svg class="hw-koste" viewBox="0 0 300 100">
  <g transform="{_CAROD_ZVETSENI}">
  <path class="hw-carod-plast" fill="#2b1846" stroke="#cbb8ff" stroke-opacity=".35" stroke-width="1.2"
        stroke-linejoin="round" d="M188 4C170 -1 150 4 122 1L131 11L119 19L134 25L127 34C148 34 164 30 178 30Z"/>
  <path class="hw-carod-vlasy" fill="#ff8a2a" d="M189 -9C178 -12 168 -5 157 -8Q164 -1 157 5Q168 5 165 12Q176 7 187 6Z"/>
  </g>
  <path fill="url(#hwSlama)" d="M121 44C95 40 60 30 22 18Q13 20 16 28Q9 36 14 44Q7 52 13 60Q7 68 15 76
        Q11 86 24 90C60 77 95 65 121 58Z"/>
  <path d="M120 46L24 22M120 49L16 37M120 52L13 52M120 55L15 68M120 57L24 86"
        stroke="#7d5718" stroke-width="1.3" opacity=".55"/>
  <path d="M120 47L290 36Q297 39 292 44L120 56Z" fill="url(#hwNasada)"/>
  <rect x="110" y="40" width="13" height="21" rx="3" fill="#5b2a86"/>
  <path d="M110 46H123M110 55H123" stroke="#2d1245" stroke-width="1.5"/>
  <g transform="{_CAROD_ZVETSENI}">
  <path fill="url(#hwSaty)" d="M171 27H187Q194 40 201 55L194 53L190 59L183 54L176 60L170 53L162 57Q164 40 171 27Z"/>
  <g class="hw-carod-noha">
    <path d="M191 55L189 72" stroke="#3a1f5c" stroke-width="5.5" stroke-linecap="round"/>
    <path d="M191 55L189 72" stroke="#ff9a2e" stroke-width="5.5" stroke-dasharray="2.4 2.4"/>
    <path fill="#1c1228" d="M184 69H193Q197 73 203 72Q208 71 207 66Q212 72 205 77H185Z"/>
  </g>
  <path fill="url(#hwSaty)" d="M171 29Q173 13 182 4L193 6Q191 18 188 29Z"/>
  <path d="M188 9Q199 21 204 39" fill="none" stroke="#6a3aa6" stroke-width="6" stroke-linecap="round"/>
  <circle cx="205" cy="41.5" r="4" fill="url(#hwKuze)"/>
  <circle cx="194" cy="-4" r="8.5" fill="url(#hwKuze)"/>
  <path d="M201 -7L210 -2L201 -1Z" fill="#86c255"/>
  <circle cx="198" cy="-6" r="1.3" fill="#1c1228"/>
  <path d="M196.5 1.5Q199.5 4 202.5 1" fill="none" stroke="#1c1228" stroke-width="1.1" stroke-linecap="round"/>
  <path fill="url(#hwKlobouk)" stroke="#cbb8ff" stroke-opacity=".45" stroke-width="1"
        d="M182 -12C184 -26 178 -38 166 -46C180 -42 192 -32 200 -14Z"/>
  <path d="M182.4 -14L199.2 -16L197.6 -20.5L183 -19Z" fill="#ff9a2e"/>
  <rect x="188.5" y="-20.2" width="4" height="4.2" rx=".6" transform="rotate(-7 190.5 -18)"
        fill="none" stroke="#ffd23f" stroke-width="1.1"/>
  <ellipse cx="191" cy="-12" rx="15.5" ry="3.2" transform="rotate(-7 191 -12)" fill="#3b1f63"
           stroke="#cbb8ff" stroke-opacity=".45" stroke-width="1"/>
  </g>
</svg>'''

_HVEZDA = 'M0-6L1.4-1.4L6 0L1.4 1.4L0 6L-1.4 1.4L-6 0L-1.4-1.4Z'

# Jiskřivá stopa za metlou koštěte
_JISKRY = f'''<svg class="hw-jiskry" viewBox="0 0 90 60" fill="#ffd27a">
  <path d="{_HVEZDA}" transform="translate(80 30)"/>
  <circle cx="64" cy="44" r="1.8"/>
  <path d="{_HVEZDA}" transform="translate(52 20) scale(.7)"/>
  <circle cx="36" cy="38" r="1.4"/>
  <path d="{_HVEZDA}" transform="translate(20 28) scale(.5)"/>
  <circle cx="6" cy="40" r="1"/>
</svg>'''

# ===== Noční scéna: měsíc s netopýry, hvězdy, blesk, dům a hroby, mlha, duchové, oči =====
# Všechno leží mimo střední sloupec s formulářem (± 210 px od středu), ať nic neleze pod text.

# Netopýr zepředu; křídla mávají kolem ramen (60, 30)
_NETOPYR = '''<svg viewBox="0 0 120 60">
  <g class="hw-kridla">
    <path d="M57 27C47 20 37 12 27 11C17 10 9 14 2 22Q10 23 15 31Q22 27 30 35Q38 30 45 37Q51 34 57 35Z"/>
    <path d="M63 27C73 20 83 12 93 11C103 10 111 14 118 22Q110 23 105 31Q98 27 90 35Q82 30 75 37Q69 34 63 35Z"/>
  </g>
  <ellipse cx="60" cy="31" rx="6.5" ry="9"/><circle cx="60" cy="21" r="5.5"/>
  <path d="M56 18L54.5 9.5L59.5 15.5ZM64 18L65.5 9.5L60.5 15.5Z"/>
  <circle class="hw-netopyr-oko" cx="58" cy="21" r="1"/><circle class="hw-netopyr-oko" cx="62" cy="21" r="1"/>
</svg>'''

# Hejno vylétá z měsíce a mizí za okrajem obrazovky:
# cíl (x, y) vůči středu měsíce, konečná velikost, start [s]
_HEJNO = (('-40vw', '-27vh', 1, 1.2), ('-25vw', '-31vh', .75, 1.7), ('23vw', '-27vh', .9, 2.4),
          ('-11vw', '-32vh', .6, 3.1), ('26vw', '7vh', .8, 3.9))
# Netopýři kroužící kolem měsíce: poloosy elipsy, půlperioda [s], fáze [s]
_KROUZI = (('8.5em', '2.6em', 3.2, -1), ('10.5em', '3.4em', 4.1, -3.3))

_MRAK = ('<svg class="hw-mrak {}" viewBox="0 0 160 40"><path d="M12 36Q2 36 5 28Q8 21 18 22Q20 13 32 14'
         'Q39 5 53 9Q63 2 75 10Q87 6 93 15Q105 11 111 19Q125 17 129 25Q143 23 147 31Q151 36 143 36Z"/></svg>')

# Úplněk s krátery; přes kotouč táhnou mraky, kolem kroužkují netopýři a čas od času vylétne hejno
_MESIC = (
    '<div class="hw-mesic"><svg class="hw-mesic-kotouc" viewBox="0 0 100 100">'
    '<circle cx="50" cy="50" r="50" fill="url(#hwMesic)"/><g fill="#bfa673" fill-opacity=".3">'
    '<circle cx="34" cy="38" r="9"/><circle cx="62" cy="62" r="12"/><circle cx="60" cy="27" r="5"/>'
    '<circle cx="31" cy="68" r="6"/><circle cx="77" cy="43" r="3.5"/></g></svg>'
    + _MRAK.format('hw-m1') + _MRAK.format('hw-m2')
    + ''.join(f'<div class="hw-krouzi" style="--rx:{rx};--ry:{ry};--t:{t}s;--d:{d}s"><div>'
              f'<div class="hw-netopyr-k">{_NETOPYR}</div></div></div>' for rx, ry, t, d in _KROUZI)
    + ''.join(f'<div class="hw-netopyr" style="--x:{x};--y:{y};--s:{s};--d:{d}s">{_NETOPYR}</div>'
              for x, y, s, d in _HEJNO)
    + '</div>')


def _hvezdy(pocet: int = 22) -> str:
    """Hvězdy v horním pásu nebe (pevné semínko => pořád stejná obloha). Nad formulářem
    jen úplně nahoře, kolem měsíce žádné — ten je přezáří."""
    rnd = random.Random(31)
    hvezdy = []
    while len(hvezdy) < pocet:
        x, y = rnd.uniform(1.5, 98.5), rnd.uniform(3, 97)   # % pásu nebe
        if (27 < x < 73 and y > 25) or (66 < x < 97 and y < 80):
            continue
        r = rnd.choice((1.5, 2, 2, 2.5, 3))
        hvezdy.append(f'<i style="left:{x:.1f}%;top:{y:.1f}%;width:{r}px;height:{r}px;'
                      f'--t:{rnd.uniform(1.8, 4.2):.1f}s;--zp:-{rnd.uniform(0, 4):.1f}s"></i>')
    return '<div class="hw-hvezdy">' + ''.join(hvezdy) + '</div>'


def _oci(x: str, y: str, barva: str, perioda: float, zpozdeni: float, sirka: float = 2.6) -> str:
    """Svítící oči ve tmě: občas se vynoří, mrknou, rozhlédnou se a zase zmizí.
    Zornice jsou oříznuté tvarem oka (#hwDulky), ať při rozhlížení nevylézají ven."""
    return (f'<div class="hw-tma-oci" style="left:{x};top:{y};width:{sirka}em;--barva:{barva};'
            f'--t:{perioda}s;--zp:-{zpozdeni}s"><svg viewBox="0 0 40 12"><g class="hw-mrk">'
            '<path d="M2 6Q8 .5 14 6Q8 11.5 2 6ZM26 6Q32 .5 38 6Q32 11.5 26 6Z"/>'
            '<g clip-path="url(#hwDulky)"><g class="hw-zornice">'
            '<ellipse cx="8" cy="6" rx="1.1" ry="3.2"/><ellipse cx="32" cy="6" rx="1.1" ry="3.2"/>'
            '</g></g></g></svg></div>')


# Duch s mávající ručkou; tělo chytá myš (najetí => „Bú!")
_DUCH = '''<svg class="hw-duch-svg" viewBox="0 0 100 124">
  <path class="hw-duch-ruka" d="M84 58Q98 52 99 66Q91 70 84 68Z"/>
  <path class="hw-duch-telo" d="M50 6C27 6 14 24 14 50V102Q20 116 28 104Q35 115 42 104Q50 117 58 104
        Q65 115 72 104Q80 116 86 102V50C86 24 73 6 50 6ZM16 60Q2 58 2 70Q9 74 16 70Z"/>
  <g class="hw-duch-oci"><ellipse cx="39" cy="44" rx="5.5" ry="7.5"/><ellipse cx="61" cy="44" rx="5.5" ry="7.5"/></g>
  <circle cx="41" cy="41" r="1.8" fill="#fff"/><circle cx="63" cy="41" r="1.8" fill="#fff"/>
  <ellipse class="hw-duch-pusa" cx="50" cy="64" rx="5" ry="6.5"/>
  <g fill="#ff9ec1" fill-opacity=".45"><ellipse cx="30" cy="57" rx="5" ry="3"/><ellipse cx="70" cy="57" rx="5" ry="3"/></g>
</svg>'''

# Strašidelný dům na pahorku: okna blikají jako od svíček, půdní okénko občas zhasne.
# Pahorek se kreslí první — zdi (dotažené do y=160) pak zakryjí jeho světlý lem.
_DUM = '''<svg class="hw-dum" viewBox="0 0 220 170">
  <path class="hw-kopec" d="M0 170C24 150 60 141 108 141S196 150 220 170Z"/>
  <path d="M37 160L39 78L61 76L63 160Z"/>
  <path d="M30 81L46 28Q48 20 55 15Q51 25 52 32L69 79Z"/>
  <path d="M61 160V94H158V160Z"/>
  <path d="M53 98L110 48L167 98Z"/>
  <path d="M133 70L134 50L145 49L146 80Z"/>
  <path d="M156 160V116H194V160Z"/>
  <path d="M150 119L175 96L200 119Z"/>
  <path class="hw-lem" d="M30 81L46 28Q48 20 55 15M110 48L167 98M175 96L200 119M134 50L145 49"/>
  <g filter="url(#hwZar)">
    <path class="hw-okno" style="--zp:-.4s" d="M45 100V92Q50 85 55 92V100Z"/>
    <circle class="hw-okno hw-zhasina" cx="110" cy="80" r="6"/>
    <rect class="hw-okno" style="--zp:-.9s" x="74" y="104" width="13" height="15"/>
    <rect class="hw-okno" style="--zp:-2.3s" x="132" y="104" width="13" height="15"/>
    <rect class="hw-okno" style="--zp:-1.6s" x="74" y="126" width="13" height="14"/>
    <path class="hw-okno" style="--zp:-1.2s" d="M103 160V132Q110 123 117 132V160Z"/>
    <rect class="hw-okno" style="--zp:-2.8s" x="169" y="124" width="11" height="12"/>
  </g>
  <path class="hw-pricle" d="M80.5 104V119M74 111.5H87M138.5 104V119M132 111.5H145M80.5 126V140M74 133H87
        M174.5 124V136M169 130H180M110 74V86M104 80H116"/>
</svg>'''

# Hřbitůvek: náhrobky zapíchnuté v pahorku (ten se kreslí až po nich a zakryje jim paty)
_HROBY = '''<svg class="hw-hroby" viewBox="0 0 200 110">
  <g class="hw-hrob">
    <path transform="rotate(-7 54 92)" d="M38 100V58Q38 42 54 42Q70 42 70 58V100Z"/>
    <path transform="rotate(5 109 90)" d="M105 100V54H95V46H105V36H113V46H123V54H113V100Z"/>
    <path transform="rotate(9 150 94)" d="M139 102V72Q139 62 150 62Q161 62 161 72V102Z"/>
  </g>
  <text class="hw-rip" transform="rotate(-7 54 92)" x="54" y="66" text-anchor="middle">RIP</text>
  <path d="M0 110C26 92 62 84 100 84S174 92 200 110Z"/>
</svg>'''

# Pruh země přes celou šířku (natahuje se, lem drží tloušťku díky non-scaling-stroke)
_ZEME = '''<svg class="hw-zeme" viewBox="0 0 1000 60" preserveAspectRatio="none">
  <path d="M0 60V30C70 20 140 16 210 22S340 38 430 39S570 31 650 25S810 13 890 17S965 26 1000 22V60Z"/>
  <path class="hw-zeme-lem" d="M0 30C70 20 140 16 210 22S340 38 430 39S570 31 650 25S810 13 890 17S965 26 1000 22"/>
</svg>'''

_HTML = f'''
<svg width="0" height="0" style="position:absolute">
  <defs>
    <radialGradient id="hwDyne" cx="45%" cy="38%" r="72%">
      <stop offset="0" stop-color="#ffb04a"/><stop offset=".55" stop-color="#f0780f"/>
      <stop offset="1" stop-color="#963a05"/>
    </radialGradient>
    <radialGradient id="hwTvar" cx="50%" cy="55%" r="70%">
      <stop offset="0" stop-color="#fff7c7"/><stop offset=".5" stop-color="#ffd23f"/>
      <stop offset="1" stop-color="#ff8c00"/>
    </radialGradient>
    <linearGradient id="hwStonek" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#86a346"/><stop offset="1" stop-color="#3a5317"/>
    </linearGradient>
    <filter id="hwZar" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="2.5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <radialGradient id="hwPlamen" cx="50%" cy="70%" r="60%">
      <stop offset="0" stop-color="#fff"/><stop offset=".3" stop-color="#fff2a8"/>
      <stop offset=".65" stop-color="#ffb12e"/><stop offset="1" stop-color="#ff6a00" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="hwSvatozar">
      <stop offset="0" stop-color="#ffc35a" stop-opacity=".55"/>
      <stop offset="1" stop-color="#ffc35a" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="hwVosk" x1="0" x2="1">
      <stop offset="0" stop-color="#bba57a"/><stop offset=".35" stop-color="#f7ecd2"/>
      <stop offset=".7" stop-color="#e6d3ab"/><stop offset="1" stop-color="#a48d62"/>
    </linearGradient>
    <linearGradient id="hwSlama" x1="1" x2="0">
      <stop offset="0" stop-color="#b98a2e"/><stop offset="1" stop-color="#f0cf72"/>
    </linearGradient>
    <linearGradient id="hwNasada" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#94602f"/><stop offset="1" stop-color="#5a3214"/>
    </linearGradient>
    <!-- Šaty čarodějnice: userSpaceOnUse, ať tělo a sukně navazují bez švu -->
    <linearGradient id="hwSaty" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="62">
      <stop offset="0" stop-color="#7d4cc0"/><stop offset="1" stop-color="#44206e"/>
    </linearGradient>
    <radialGradient id="hwKuze" cx="40%" cy="35%" r="70%">
      <stop offset="0" stop-color="#c8f095"/><stop offset="1" stop-color="#86c255"/>
    </radialGradient>
    <linearGradient id="hwKlobouk" x1="0" x2="1">
      <stop offset="0" stop-color="#5b2f8f"/><stop offset="1" stop-color="#2e1650"/>
    </linearGradient>
    <radialGradient id="hwLebka" cx="42%" cy="34%" r="72%">
      <stop offset="0" stop-color="#fbf7ec"/><stop offset=".65" stop-color="#e6dcc3"/>
      <stop offset="1" stop-color="#bfae8c"/>
    </radialGradient>
    <radialGradient id="hwMesic" cx="40%" cy="36%" r="70%">
      <stop offset="0" stop-color="#fffef6"/><stop offset=".55" stop-color="#f6eac4"/>
      <stop offset="1" stop-color="#d8c18a"/>
    </radialGradient>
    <linearGradient id="hwMrak" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#7a68a2"/><stop offset=".45" stop-color="#3a2d58" stop-opacity=".95"/>
      <stop offset="1" stop-color="#1c1430" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="hwDuch" cx="42%" cy="30%" r="78%">
      <stop offset="0" stop-color="#fff"/><stop offset=".6" stop-color="#eeeafa"/>
      <stop offset="1" stop-color="#c3bae3"/>
    </radialGradient>
    <clipPath id="hwDulky"><path d="M2 6Q8 .5 14 6Q8 11.5 2 6ZM26 6Q32 .5 38 6Q32 11.5 26 6Z"/></clipPath>
  </defs>
</svg>
<div class="hw-blesk"></div>
{_hvezdy()}
{_MESIC}
{_ZEME}{_HROBY}{_DUM}
<div class="hw-mlha hw-mlha-zadni"></div>
{_oci('5vw', '55vh', '#d4ff4f', 15, 2)}{_oci('25vw', '68vh', '#ffb43a', 19, 9, 2.2)}{_oci('92vw', '38vh', '#d4ff4f', 17, 13)}
<div class="hw-duch hw-d1"><div class="hw-duch-vznasej">{_DUCH}<div class="hw-bu">Bú!</div></div></div>
<div class="hw-duch hw-d2"><div class="hw-duch-vznasej">{_DUCH}<div class="hw-bu">Bú!</div></div></div>
<svg class="hw-pavucina hw-l" viewBox="0 0 300 300"><path d="{_pavucina()}"/></svg>
<svg class="hw-pavucina hw-p" viewBox="0 0 300 300"><path d="{_pavucina()}"/></svg>
<svg class="hw-pavouk" viewBox="0 0 60 300">
  <path class="hw-nit" d="M30 0V238"/>
  <path class="hw-nohy" d="M26 252Q13 238 5 246M26 256Q10 248 3 258M26 260Q11 262 5 272M27 263Q15 272 11 283
        M34 252Q47 238 55 246M34 256Q50 248 57 258M34 260Q49 262 55 272M33 263Q45 272 49 283"/>
  <ellipse class="hw-telo" cx="30" cy="247" rx="8.5" ry="10.5"/>
  <circle class="hw-telo" cx="30" cy="261" r="5.5"/>
  <circle class="hw-oci" cx="28" cy="262.5" r="1.1"/><circle class="hw-oci" cx="32" cy="262.5" r="1.1"/>
</svg>
<div class="hw-let"><div class="hw-houpej">{_JISKRY}{_KOSTE}</div></div>
<div class="hw-let hw-zpet"><div class="hw-houpej">{_JISKRY}{_KOSTE}</div></div>
<div class="hw-skupina hw-l">
  <div class="hw-zar"></div>
  {_dyne('hw-velka', _kostlivec_mava())}{_dyne('hw-mala')}{_svicka(64, .4)}
</div>
<div class="hw-skupina hw-p">
  <div class="hw-zar"></div>
  {_svicka(110, .9)}{_svicka(80, .1)}{_svicka(48, 1.3)}{_dyne('hw-stredni', *_kostlivec_vykuk())}
</div>
<div class="hw-mlha hw-mlha-predni"></div>
'''

# Pole e-mailu a hesla mají každé jiné rozložení vosku (a vlastní tempo odkapávání)
_VOSK_CSS = f'''
/* Vosk ze svíček stéká po polích formuláře — jen přihlašovací formulář, kliknutí propouští */
body.halloween .login-wrap .q-field__inner::after {{
    content: ''; position: absolute; left: -6px; right: -6px; top: -6px; height: 72px;
    pointer-events: none; z-index: 1;
    filter: drop-shadow(0 0 .6px rgba(150,110,50,.9)) drop-shadow(0 1.5px 1.2px rgba(0,0,0,.45));
    background: {_vosk_roh(86, [(30, 3.2, 20), (44, 2.4, 15.5), (60, 2.2, 14)], 30, 7.5)} left top / 96px 72px no-repeat,
        {_vosk_stred([(36, 2.2, 14), (56, 2, 13.5), (78, 3.4, 27), (95, 2.4, 17)])} 70% 0 / 130px 72px no-repeat,
        {_vosk_roh(72, [(32, 2.6, 24), (48, 2, 14)], 16, zrcadlit=True)} right top / 96px 72px no-repeat;
}}
body.halloween .login-wrap .q-field ~ .q-field .q-field__inner::after {{
    background: {_vosk_roh(60, [(32, 2.6, 17)], 12)} left top / 96px 72px no-repeat,
        {_vosk_stred([(36, 2.6, 19), (60, 2, 13.5), (84, 3, 23), (96, 2, 14.5)])} 38% 0 / 130px 72px no-repeat,
        {_vosk_roh(90, [(30, 2.4, 16.5), (50, 3, 25), (66, 2, 14)], 34, 9, 3, zrcadlit=True)} right top / 96px 72px no-repeat;
}}
'''

CSS = '''
/* ===== Halloween na přihlašovací obrazovce (jen říjen, viz intranet_halloween.py) ===== */
/* Fotka do studeného měsíčního svitu, nad ní fialová noc a zespodu oranžová záře od dýní */
body.halloween {
    --hw-foto: grayscale(.65) sepia(.3) hue-rotate(210deg) saturate(1.3) contrast(1.05) brightness(.6);
    --hw-noc: radial-gradient(ellipse 75% 45% at 50% 108%, rgba(255,115,20,.26), rgba(255,115,20,0) 70%),
              radial-gradient(ellipse at 50% 45%, rgba(32,16,56,.55) 0%, rgba(12,6,26,.95) 72%);
}
body.prihlaseni-pozadi.halloween::before { filter: var(--hw-foto) !important; }
body.prihlaseni-pozadi.halloween::after { background: var(--hw-noc) !important; }
/* Mezistránka „Přihlašuji se…" kreslí pozadí sama (je v dialogu nad body) — stejně ztmavit */
body.halloween .login-loading::before { filter: var(--hw-foto); }
body.halloween .login-loading::after { background: var(--hw-noc); }
/* Dekorace leží pod formulářem a nikdy nechytají kliknutí */
body.halloween .login-wrap { position: relative; z-index: 1; }
body.halloween .hw {
    position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
    font-size: clamp(8px, .85vw, 16px);   /* 1em = měřítko všech dekorací */
}
.hw svg { display: block; overflow: visible; }

/* Oranžová místo červené. Quasar .bg-primary / .text-primary mají !important v CSS
   vrstvě, která přebije i naše !important — tlačítko a odkazy se proto barví přes
   proměnnou --q-primary, kterou ty třídy čtou. */
body.halloween .login-wrap { --q-primary: #ff9a3c; }
body.halloween .login-btn { --q-primary: linear-gradient(135deg, #ffa53a, #e8620e); }
body.halloween .login-btn .q-btn__content { color: #241003; }
body.halloween .login-btn:hover { box-shadow: 0 10px 30px rgba(255,130,20,.45); }
body.halloween .login-link:hover { background: rgba(255,154,60,.12) !important; }
body.halloween .login-title { text-shadow: 0 0 24px rgba(255,140,30,.35), 0 2px 10px rgba(0,0,0,.6); }
/* Pole a checkbox jen ve formuláři — světlé dialogy (zapomenuté heslo, 2FA) se nemění */
body.halloween .login-wrap .q-field--outlined.q-field--highlighted .q-field__control:after { border-color: #ff9a3c; }
body.halloween .login-wrap .q-field--focused .q-field__control { box-shadow: 0 0 0 4px rgba(255,130,20,.22); }
body.halloween .login-wrap .q-field--focused .q-field__label { color: #ffb36b !important; }
body.halloween .login-chk .q-checkbox__inner--truthy .q-checkbox__bg { background: #ff9a3c; border-color: #ff9a3c; }

/* Pavučiny v horních rozích + pavouk na vlákně */
.hw-pavucina { position: absolute; top: 0; width: 17em; fill: none;
               stroke: rgba(225,230,240,.38); stroke-width: 1.1; }
.hw-pavucina.hw-l { left: 0; }
.hw-pavucina.hw-p { right: 0; transform: scaleX(-1); }
.hw-pavouk { position: absolute; top: -7em; left: 10em; width: 4em; transform-origin: 50% 0;
             animation: hw-pavouk 7s ease-in-out infinite; }
.hw-nit { stroke: rgba(230,235,245,.45); stroke-width: 1; }
.hw-nohy { fill: none; stroke: rgba(205,210,222,.6); stroke-width: 1.7; stroke-linecap: round; }
.hw-telo { fill: #26222d; stroke: rgba(255,255,255,.35); stroke-width: .8; }
.hw-oci { fill: #ffae42; }

/* Dýně a svíčky dole v rozích */
.hw-skupina { position: absolute; bottom: -.4em; display: flex; align-items: flex-end; gap: .6em; }
.hw-skupina.hw-l { left: 3vw; }
.hw-skupina.hw-p { right: 3vw; }
.hw-zar { position: absolute; left: -15%; right: -15%; bottom: -2em; height: 9em;
          background: radial-gradient(closest-side, rgba(255,140,30,.38), rgba(255,140,30,0));
          animation: hw-svit 2.6s ease-in-out infinite; }
.hw-dyne { position: relative; width: 12em; filter: drop-shadow(0 .4em 1.2em rgba(0,0,0,.55)); }
.hw-dyne.hw-mala { width: 7.5em; }
.hw-dyne.hw-stredni { width: 10em; }
.hw-tvar { animation: hw-svit 2.6s ease-in-out infinite; }
.hw-svicka { position: relative; width: 2em; }
.hw-plamen { transform-box: fill-box; transform-origin: 50% 90%;
             animation: hw-plamen 1.7s ease-in-out infinite var(--zp, 0s); }
.hw-svatozar { animation: hw-svit 2.2s ease-in-out infinite var(--zp, 0s); }

/* Dvě košťata: bližší letí doleva doprava, vzdálenější (menší, výš) zpátky */
.hw-let { position: absolute; top: 13vh; left: 0; animation: hw-let 24s linear infinite; }
.hw-houpej { display: flex; align-items: center; animation: hw-houpej 2.8s ease-in-out infinite alternate; }
/* Klobouk přečnívá nad koště úměrně jeho velikosti – proto část odsazení v em */
.hw-let.hw-zpet { top: calc(3vh + 2.6em); opacity: .7; animation-name: hw-let-zpet; }
.hw-zpet .hw-houpej { font-size: .6em; flex-direction: row-reverse; animation-name: hw-houpej-zpet; }
.hw-zpet svg { transform: scaleX(-1); }
.hw-koste { width: 15em; }
/* Čarodějnici vlaje plášť a vlasy, houpe nohou */
.hw-carod-plast, .hw-carod-vlasy {
    transform-box: fill-box; transform-origin: 100% 15%;
    animation: hw-vlaje .5s ease-in-out infinite alternate;
}
.hw-carod-vlasy { animation-duration: .38s; }
.hw-carod-noha {
    transform-box: view-box; transform-origin: 191px 54px;
    animation: hw-houpe-nohou 1.3s ease-in-out infinite alternate;
}
.hw-jiskry { width: 4.5em; margin-right: -.5em; }
.hw-jiskry > * { animation: hw-trpyt 1.4s ease-in-out infinite; }
.hw-jiskry > :nth-child(2n) { animation-delay: -.7s; }
.hw-jiskry > :nth-child(3n) { animation-delay: -.35s; }

/* Kostlivci za dýněmi: levý mává a cvaká zuby, pravý občas vykoukne a chytí se dýně */
.hw-kost { fill: #e9e0c9; stroke: #e9e0c9; stroke-linecap: round; }
.hw-lebka { fill: url(#hwLebka); }
.hw-tma { fill: #211a27; }
.hw-zuby { fill: none; stroke: #211a27; stroke-width: .9; }
.hw-oko { fill: #ffae42; animation: hw-svit 2.4s ease-in-out infinite; }
.hw-mava { animation: hw-mava 1.3s ease-in-out infinite alternate; }
.hw-celist { animation: hw-celist 3.6s ease-in-out infinite; }
.hw-vykuk { animation: hw-vykuk 11s ease-in-out infinite; }
.hw-prsty { animation: hw-prsty 11s ease-in-out infinite; }

/* ===== Noční scéna — všechno mimo střední sloupec s formulářem ===== */
/* Blesk: jednou za 23 s dvakrát krátce ozáří nebe (≤ 3 záblesky za sekundu, WCAG 2.3.1) */
.hw-blesk { position: absolute; inset: 0; opacity: 0;
            background: radial-gradient(ellipse 85% 75% at 72% 0%, rgba(205,215,255,.5),
                        rgba(160,170,235,.12) 55%, rgba(160,170,235,0) 80%);
            animation: hw-blesk 23s linear -8s infinite; }
/* Hvězdy jen v horním pásu nebe */
.hw-hvezdy { position: absolute; left: 0; top: 0; width: 100%; height: 40vh; }
.hw-hvezdy i { position: absolute; border-radius: 50%; background: #fff; box-shadow: 0 0 4px 1px rgba(210,220,255,.6);
               animation: hw-trpyt var(--t) ease-in-out var(--zp) infinite; }
/* Úplněk vpravo nahoře: svatozář dýchá, přes kotouč táhnou mraky, kolem lítají netopýři */
.hw-mesic { position: absolute; right: 18vw; top: 15vh; width: 24em; height: 24em; margin: -12em -12em 0 0; }
.hw-mesic::before { content: ''; position: absolute; inset: 0; border-radius: 50%;
                    background: radial-gradient(closest-side, rgba(255,236,190,.4) 42%,
                                rgba(255,236,190,.13) 64%, rgba(255,236,190,0));
                    animation: hw-dech 6s ease-in-out infinite alternate; }
.hw-mesic-kotouc { position: absolute; left: 27%; top: 27%; width: 46%;
                   filter: drop-shadow(0 0 1.4em rgba(255,240,200,.5)); }
.hw-mrak { position: absolute; left: 50%; top: 47%; width: 15em; margin-left: -7.5em; fill: url(#hwMrak);
           opacity: 0; filter: blur(1.2px); animation: hw-mrak 34s linear infinite; }
.hw-mrak.hw-m2 { top: 33%; width: 10em; margin-left: -5em; animation-duration: 26s; animation-delay: -14s; }
/* Netopýr je na tmavém nebi tmavý — světlejší lem a slabá záře ho od noci oddělí */
.hw-netopyr, .hw-krouzi { position: absolute; left: 50%; top: 50%; fill: #1a1226;
                          stroke: rgba(215,200,255,.55); stroke-width: 1.6;
                          filter: drop-shadow(0 0 .35em rgba(200,185,255,.45)); }
.hw-netopyr { width: 7em; margin: -1.75em 0 0 -3.5em; animation: hw-vylet 15s ease-in-out var(--d) infinite both; }
.hw-krouzi { animation: hw-kx var(--t) ease-in-out var(--d) infinite alternate; }
.hw-krouzi > div { animation: hw-ky var(--t) ease-in-out calc(var(--d) - var(--t) / 2) infinite alternate; }
.hw-netopyr-k { width: 3.4em; margin: -.85em 0 0 -1.7em; }
.hw-kridla { transform-origin: 60px 30px; animation: hw-mavani .24s ease-in-out infinite alternate; }
.hw-netopyr-oko { fill: #ff7a3a; stroke: none; }
/* Pahorek se strašidelným domem vpravo, hřbitůvek vlevo — siluety na pruhu země */
.hw-zeme { position: absolute; left: 0; bottom: 0; width: 100%; height: 5.5em; fill: #0b0713; }
.hw-zeme-lem, .hw-kopec, .hw-lem { stroke: rgba(190,170,245,.22); stroke-width: 1.2; }
.hw-zeme-lem, .hw-lem { fill: none; }
.hw-zeme-lem { vector-effect: non-scaling-stroke; }
.hw-dum, .hw-hroby { position: absolute; bottom: 0; fill: #0b0713; }
.hw-dum { left: calc(50% + 210px + 1em); width: 15em; }
.hw-hroby { right: calc(50% + 210px + 1em); width: 14em; }
.hw-okno { fill: #ffb547; animation: hw-svit 3.2s ease-in-out var(--zp, 0s) infinite; }
.hw-okno.hw-zhasina { animation: hw-zhasina 13s linear infinite; }
.hw-pricle { fill: none; stroke: #0b0713; stroke-width: 1.8; }
.hw-hrob { fill: #1c1528; stroke: rgba(190,170,245,.25); stroke-width: 1.2; }
.hw-rip { fill: rgba(255,255,255,.2); font-size: 10px; font-weight: 800; letter-spacing: 1px; }
/* Mlha se válí po zemi — dlaždice na šířku okna, posun o jednu dlaždici = plynulá smyčka */
.hw-mlha { position: absolute; left: 0; bottom: -2em; width: 200vw; height: 16em;
           background-size: 50% 100%; background-repeat: repeat-x; animation: hw-mlha 70s linear infinite; }
.hw-mlha-zadni { background-image:
    radial-gradient(ellipse 24% 42% at 24% 64%, rgba(200,190,235,.32), rgba(200,190,235,0) 70%),
    radial-gradient(ellipse 18% 32% at 52% 74%, rgba(200,190,235,.26), rgba(200,190,235,0) 70%),
    radial-gradient(ellipse 26% 46% at 79% 60%, rgba(200,190,235,.32), rgba(200,190,235,0) 70%); }
.hw-mlha-predni { bottom: -2.5em; height: 9em; animation-duration: 46s; animation-direction: reverse;
    background-image:
    radial-gradient(ellipse 22% 45% at 18% 60%, rgba(215,205,245,.24), rgba(215,205,245,0) 70%),
    radial-gradient(ellipse 25% 40% at 61% 66%, rgba(215,205,245,.2), rgba(215,205,245,0) 70%); }
/* Svítící oči ve tmě: vynoří se, mrknou, rozhlédnou se a zmizí */
.hw-tma-oci { position: absolute; opacity: 0; animation: hw-oci-tma var(--t) ease-in-out var(--zp) infinite; }
.hw-tma-oci svg { filter: drop-shadow(0 0 .35em var(--barva)); }
.hw-tma-oci path { fill: var(--barva); }
.hw-mrk { transform-origin: 20px 6px; animation: hw-mrk 4.7s ease-in-out var(--zp) infinite; }
.hw-zornice { fill: #170c05; animation: hw-rozhled 7.5s ease-in-out var(--zp) infinite; }
/* Duchové: vznášejí se, průsvitně blednou a občas houknou „Bú!" */
.hw-duch { position: absolute; width: 7em; }
.hw-duch.hw-d1 { left: 11vw; top: 35vh; }
.hw-duch.hw-d2 { left: calc(50% + 210px + 8em); top: 57vh; width: 4.6em; }
.hw-duch-vznasej { position: relative; animation: hw-vznasej 4.4s ease-in-out infinite alternate; }
.hw-d2 .hw-duch-vznasej { animation-duration: 3.6s; animation-delay: -2s; }
.hw-duch-svg { filter: drop-shadow(0 0 .9em rgba(185,195,255,.5)); animation: hw-prusvit 9s ease-in-out infinite;
               transition: scale .35s cubic-bezier(.3,1.6,.5,1); }
.hw-d2 .hw-duch-svg { animation-delay: -4.5s; }
.hw-duch-telo, .hw-duch-ruka { fill: url(#hwDuch); }
.hw-duch-ruka { transform-origin: 85px 63px; animation: hw-ruka 1.1s ease-in-out infinite alternate; }
.hw-duch-oci, .hw-duch-pusa { fill: #241731; }
.hw-duch-pusa { transform-box: fill-box; transform-origin: 50% 50%; transition: transform .3s; }
.hw-bu { position: absolute; left: 80%; top: -16%; padding: .3em .6em .35em; border-radius: 1em 1em 1em .2em;
         background: #fff; color: #3b1c5a; font-size: 1.25em; font-weight: 900; line-height: 1; white-space: nowrap;
         box-shadow: 0 .25em .9em rgba(0,0,0,.35); transform-origin: 0 100%; opacity: 0;
         animation: hw-bu 12s ease-in-out infinite; }
.hw-d2 .hw-bu { font-size: 1em; animation-delay: -6s; }
/* Schované vtípky pro myš — kurzor chytají jen tyhle tvary, zbytek vrstvy kliky propouští */
.hw-duch-telo, .hw-duch-ruka, .hw-dyne > *, .hw-pavouk .hw-telo, .hw-pavouk .hw-nohy { pointer-events: visiblePainted; }
.hw-duch:hover .hw-duch-svg { scale: 1.15; animation: none; opacity: 1; }
.hw-duch:hover .hw-duch-pusa { transform: scale(1.35, 1.7); }
.hw-duch:hover .hw-bu { animation: none; opacity: 1; }
.hw-dyne { transform-origin: 50% 100%; }
.hw-dyne:hover { animation: hw-chichot .28s ease-in-out 6 alternate; }
.hw-dyne:hover .hw-celist { animation: hw-celist-smich .14s linear infinite alternate; }
.hw-pavouk { transition: translate 2s ease-in-out 1.2s; }
.hw-pavouk:hover { translate: 0 -9em; transition: translate .4s cubic-bezier(.2,.9,.3,1); }

@keyframes hw-pavouk { 0%, 100% { transform: translateY(0) rotate(0); }
                       50% { transform: translateY(4em) rotate(1.5deg); } }
@keyframes hw-svit { 0%, 100% { opacity: 1; } 25% { opacity: .78; } 45% { opacity: .95; }
                     70% { opacity: .7; } 85% { opacity: .9; } }
@keyframes hw-plamen { 0%, 100% { transform: scale(1, 1) skewX(0); }
                       20% { transform: scale(.94, 1.06) skewX(2deg); }
                       40% { transform: scale(1.04, .95) skewX(-2deg); }
                       60% { transform: scale(.97, 1.08) skewX(1deg); }
                       80% { transform: scale(1.02, .97) skewX(-1deg); } }
@keyframes hw-let { 0% { transform: translateX(-25em); }
                    60%, 100% { transform: translateX(calc(100vw + 5em)); } }
@keyframes hw-let-zpet { 0%, 40% { transform: translateX(calc(100vw + 5em)); }
                         100% { transform: translateX(-25em); } }
@keyframes hw-houpej { from { transform: translateY(-.6em) rotate(-5deg); }
                       to { transform: translateY(.6em) rotate(-1deg); } }
@keyframes hw-houpej-zpet { from { transform: translateY(-.6em) rotate(5deg); }
                            to { transform: translateY(.6em) rotate(1deg); } }
@keyframes hw-trpyt { 0%, 100% { opacity: .2; } 50% { opacity: 1; } }
@keyframes hw-vlaje { to { transform: scaleX(.9) skewY(-6deg); } }
@keyframes hw-houpe-nohou { from { transform: rotate(-7deg); } to { transform: rotate(9deg); } }
@keyframes hw-mava { from { transform: rotate(-18deg); } to { transform: rotate(12deg); } }
@keyframes hw-celist { 0%, 50%, 62%, 74%, 86%, 100% { transform: none; } 56%, 68%, 80% { transform: translateY(3.5px); } }
@keyframes hw-vykuk { 0%, 28%, 90%, 100% { transform: translateY(72px); } 38%, 44%, 78%, 82% { transform: none; }
                      50%, 56% { transform: rotate(-9deg); } 66%, 72% { transform: rotate(8deg); } }
@keyframes hw-prsty { 0%, 20%, 93%, 100% { opacity: 0; } 26%, 88% { opacity: 1; } }
@keyframes hw-blesk { 0%, 60%, 61.6%, 62.6%, 64.4%, 100% { opacity: 0; } 60.5% { opacity: .9; } 63.2% { opacity: .55; } }
@keyframes hw-dech { from { opacity: .75; transform: scale(.96); } to { opacity: 1; transform: scale(1.04); } }
@keyframes hw-mrak { 0% { transform: translateX(16em); opacity: 0; } 12%, 86% { opacity: .9; }
                     100% { transform: translateX(-17em); opacity: 0; } }
@keyframes hw-vylet {
    0% { transform: translate(0, 0) scale(.12); opacity: 0; }
    2% { opacity: 1; }
    8% { transform: translate(calc(var(--x) * .18), calc(var(--y) * .1 + 1.6em)) scale(calc(var(--s) * .3)); }
    16% { transform: translate(calc(var(--x) * .42), calc(var(--y) * .38 - 1.2em)) scale(calc(var(--s) * .55)); }
    24% { transform: translate(calc(var(--x) * .7), calc(var(--y) * .64 + 1em)) scale(calc(var(--s) * .8)); }
    32% { transform: translate(var(--x), var(--y)) scale(var(--s)); opacity: 1; }
    32.1%, 100% { transform: translate(var(--x), var(--y)) scale(var(--s)); opacity: 0; } }
@keyframes hw-mavani { from { transform: scaleY(1); } to { transform: scaleY(-.45); } }
@keyframes hw-kx { from { transform: translateX(calc(var(--rx) * -1)); } to { transform: translateX(var(--rx)); } }
@keyframes hw-ky { from { transform: translateY(calc(var(--ry) * -1)) scale(.55); opacity: .65; }
                   to { transform: translateY(var(--ry)) scale(1); opacity: 1; } }
@keyframes hw-zhasina { 0%, 60%, 85.5%, 100% { opacity: 1; } 60.5%, 85% { opacity: .06; } }
@keyframes hw-mlha { to { transform: translateX(-100vw); } }
@keyframes hw-oci-tma { 0%, 42%, 100% { opacity: 0; } 50%, 90% { opacity: 1; } }
@keyframes hw-mrk { 0%, 90%, 96%, 100% { transform: scaleY(1); } 93% { transform: scaleY(.08); } }
@keyframes hw-rozhled { 0%, 22%, 100% { transform: translateX(0); } 30%, 52% { transform: translateX(-2.4px); }
                        60%, 82% { transform: translateX(2.4px); } }
@keyframes hw-vznasej { from { transform: translateY(-.8em) rotate(-4deg); } to { transform: translateY(.8em) rotate(3deg); } }
@keyframes hw-prusvit { 0%, 100% { opacity: .93; } 50% { opacity: .5; } }
@keyframes hw-ruka { from { transform: rotate(-22deg); } to { transform: rotate(14deg); } }
@keyframes hw-bu { 0%, 72%, 100% { opacity: 0; transform: scale(.3); } 76%, 90% { opacity: 1; transform: scale(1); }
                   94% { opacity: 0; transform: scale(.85); } }
@keyframes hw-chichot { from { transform: translateY(0) rotate(-2deg); }
                        to { transform: translateY(-.35em) rotate(2.5deg) scale(1.03, .97); } }
@keyframes hw-celist-smich { to { transform: translateY(3.5px); } }

@media (prefers-reduced-motion: reduce) {
    .hw *, .hw-mesic::before { animation: none !important; transition: none !important; }
    .hw-let, .hw-netopyr, .hw-krouzi, .hw-blesk { display: none; }
}
/* Úzký displej: formulář zabírá skoro celou šířku — duchové, oči, dům a hroby by lezli pod text */
@media (max-width: 760px) {
    .hw-duch, .hw-tma-oci, .hw-dum, .hw-hroby { display: none; }
    .hw-hvezdy { height: 20vh; }
}
''' + _VOSK_CSS


def vykresli_dekorace() -> None:
    """Nasadí na body třídu `halloween` a vykreslí dekorace (fixní vrstva pod formulářem).
    CSS (`CSS`) musí být v hlavičce stránky už předem — po připojení klienta ji NiceGUI nemění."""
    ui.query('body').classes(add='halloween')
    ui.html(_HTML, sanitize=False).classes('hw').props('aria-hidden=true')
