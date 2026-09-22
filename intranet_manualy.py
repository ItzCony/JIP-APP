"""Manuály — online čtečka firemních manuálů nahraných jako DOCX.

DOCX se NEpřevádí při každém zobrazení. Jednorázově se rozloží na kapitoly
(kapitola = nadpis úrovně 2) a uloží se jako JSON vedle vytažených obrázků:

    Manualy_Data/manual.json      kapitoly: nadpis, HTML, čistý text pro hledání
    Manualy_Data/obrazky/         obrázky pojmenované otiskem obsahu
    Manualy_Data/verze/           zálohy JSONu před každým zásahem (rollback)
    Manualy_Data/<původní>.docx   originál — leží na disku, NEservíruje se

Čtečka pak jen bere hotové HTML z RAM cache (klíč = mtime JSONu), takže
listování i hledání jsou okamžité a nezatěžují DB — modul žádnou tabulku nemá.

Vzhled drží stylopis: Wordova kaskáda stylů se čte přímo z XML dokumentu
(styles.xml + theme1.xml) a překlopí se na CSS třídy (`w-nadpis1`), které
jdou s manuálem v JSONu do hlavičky stránky. Odstavec si tak nese jen jméno
stylu, ne inline formát — python-docx totiž vrací poslední vrstvu kaskády,
takže nadpis s velikostí u rodiče vycházel prázdný a padal na tovární <h1>.

Hledání je bez diakritiky a bez ohledu na velikost písmen („zasoba" najde
„Zásoby"). Nalezené výrazy se zvýrazňují až v zobrazeném HTML, a to mimo
značky, aby se atributy `src`/`class` nerozbily.

Správce edituje kapitolu přímo v aplikaci (tužka → WYSIWYG → Uložit úpravy).
Před každým zápisem se odloží kopie JSONu do verze/, takže nepovedená úprava
se dá vrátit. Každý zásah jde do logu (kategorie „Manuály").

Manuál se nedá stáhnout — servírují se jen obrázky, ne zdrojový DOCX.

Práva: manualy_ctenar (čte), manualy_admin (edituje, nahrává, vrací verze).
"""

from nicegui import ui, app
import intranet_data
import intranet_logger
import intranet_static
import asyncio
import datetime
import hashlib
import html as _html
import io
import json
import os
import re
import shutil
import unicodedata
import urllib.parse
import zipfile

from fastapi.responses import HTMLResponse, PlainTextResponse
from lxml import etree

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

NAZEV = 'Manuály'
MANUALY_DIR = 'Manualy_Data'
OBRAZKY_DIR = os.path.join(MANUALY_DIR, 'obrazky')
VERZE_DIR = os.path.join(MANUALY_DIR, 'verze')
JSON_SOUBOR = os.path.join(MANUALY_DIR, 'manual.json')
URL_OBRAZKY = '/manualy_prilohy'
# Kolik starých podob manuálu držet stranou pro návrat.
POCET_VERZI = 2

os.makedirs(OBRAZKY_DIR, exist_ok=True)
# Servíruje se JEN podadresář s obrázky — zdrojový DOCX ani JSON nesmí jít
# stáhnout. Cesta v uloženém HTML (`/manualy_prilohy/obrazky/…`) zůstává stejná.
intranet_static.chranene_soubory(URL_OBRAZKY + '/obrazky', OBRAZKY_DIR)

PRAVA_CTENI = ('vse', 'manualy_ctenar', 'manualy_admin')
PRAVA_SPRAVA = ('vse', 'manualy_admin')

# Kapitola = nadpis téhle úrovně a výš. Hlouběji zanořené nadpisy zůstávají
# uvnitř kapitoly jako kotvy pro rychlý skok.
UROVEN_KAPITOLY = 2


def smi_cist(vsechna_prava) -> bool:
    return bool(set(PRAVA_CTENI) & set(vsechna_prava))


def smi_spravovat(vsechna_prava) -> bool:
    return bool(set(PRAVA_SPRAVA) & set(vsechna_prava))


# ============================================================
# ==                  Převod DOCX → HTML                    ==
# ============================================================

# Styly obsahu, který Word vygeneroval sám (toc 1…toc 9). Vlastní obsah
# skládáme z nadpisů, tenhle by byl jen mrtvý seznam s čísly stránek.
_STYL_TOC = re.compile(r'^toc\s*\d+$', re.I)
_STYL_NADPIS = re.compile(r'^(?:Heading|Nadpis)\s*(\d+)$', re.I)

# Jmenné prostory, které čteme přímo z XML (python-docx je přes svoje objekty
# nevydá celé — velikost ani barvu zděděnou po rodiči nezná).
_NS_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_NS_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def _wq(tag: str) -> str:
    return f'{{{_NS_W}}}{tag}'


def _aq(tag: str) -> str:
    return f'{{{_NS_A}}}{tag}'


def _hodnota(el, vychozi=None):
    """Obsah atributu w:val."""
    if el is None:
        return vychozi
    return el.get(_wq('val'), vychozi)


def _zapnuto(el) -> bool | None:
    """Přepínač typu <w:b/> — chybí = nic neříká, w:val="0" = vypnuto."""
    if el is None:
        return None
    return _hodnota(el, '1') not in ('0', 'false', 'off')


def _odstin(rgb: str, shade: str | None, tint: str | None) -> str:
    """Barva z tématu zesvětlená (tint) nebo ztmavená (shade), jak počítá Word."""
    try:
        r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return rgb
    if shade:
        k = int(shade, 16) / 255
        r, g, b = (int(x * k) for x in (r, g, b))
    elif tint:
        k = int(tint, 16) / 255
        r, g, b = (int(255 - (255 - x) * k) for x in (r, g, b))
    return f'{r:02x}{g:02x}{b:02x}'


# Zvýrazňovač z Wordu nese jméno barvy, ne kód.
_ZVYRAZNENI = {
    'yellow': '#ffff00', 'green': '#00ff00', 'cyan': '#00ffff', 'magenta': '#ff00ff',
    'blue': '#0000ff', 'red': '#ff0000', 'darkBlue': '#000080', 'darkCyan': '#008080',
    'darkGreen': '#008000', 'darkMagenta': '#800080', 'darkRed': '#800000',
    'darkYellow': '#808000', 'darkGray': '#808080', 'lightGray': '#c0c0c0', 'black': '#000000',
}

# Nadpis, který v souboru nemá vlastní velikost, ji bere z vestavěné definice
# Wordu — ta v DOCX není. Bez téhle tabulky by takový nadpis splynul s textem.
# ponytail: čísla ze současné šablony Office (Aptos); starší šablony mají
# nadpisy o 2–4 pt menší, rozdíl je vzhledový, ne funkční.
_VESTAVENE_NADPISY = {
    1: ('20pt', '#0f4761'), 2: ('16pt', '#0f4761'), 3: ('14pt', '#0f4761'),
    4: ('12pt', '#0f4761'), 5: ('11pt', '#0f4761'), 6: ('11pt', '#595959'),
}


class _Stylopis:
    """Wordova kaskáda stylů přečtená z XML → hotové CSS třídy.

    Word skládá vzhled odstavce ze tří vrstev: docDefaults → řetěz basedOn →
    vlastní styl. python-docx vrací jen poslední vrstvu, takže nadpis, který
    má velikost zapsanou u rodiče (nebo font jen přes téma), vyjde prázdný —
    a v prohlížeči zbude tovární vzhled <h1>. Tady se kaskáda rozřeší celá a
    výsledek se uloží jako jedna CSS třída na styl (`w-nadpis1`), ne jako
    inline styl u každého odstavce.
    """

    def __init__(self, zdroj: zipfile.ZipFile):
        self.fonty_tematu = {}
        self.barvy_tematu = {}
        self._xml = {}          # styleId → element stylu
        self._typ = {}          # styleId → 'paragraph' | 'character'
        self._rodic = {}        # styleId → basedOn
        self._cache = {}
        self._vychozi_rpr = None
        self._vychozi_ppr = None
        self.vychozi_odstavec = 'Normal'
        self.tridy = {}         # w-… → deklarace (jen styly, které dokument použil)
        self._nacti_tema(zdroj)
        self._nacti_styly(zdroj)

    # -------- vstupní XML --------
    def _nacti_tema(self, zdroj: zipfile.ZipFile) -> None:
        """Fonty a barvy schématu — styly na ně odkazují jen jménem role."""
        jmeno = next((n for n in zdroj.namelist()
                      if n.startswith('word/theme/') and n.endswith('.xml')), None)
        if not jmeno:
            return
        try:
            koren = etree.fromstring(zdroj.read(jmeno))
        except etree.XMLSyntaxError:
            return
        for role, znacka in (('major', 'majorFont'), ('minor', 'minorFont')):
            skupina = koren.find('.//' + _aq(znacka))
            if skupina is None:
                continue
            latinka = skupina.find(_aq('latin'))
            pismo = latinka.get('typeface') if latinka is not None else None
            if pismo:
                # Word píše majorHAnsi / majorBidi / majorEastAsia — všechny
                # míří na stejnou hlavičkovou sadu.
                for pripona in ('HAnsi', 'Ascii', 'Bidi', 'EastAsia'):
                    self.fonty_tematu[role + pripona] = pismo
        schema = koren.find('.//' + _aq('clrScheme'))
        for prvek in (schema if schema is not None else []):
            barva = prvek.find(_aq('srgbClr'))
            if barva is not None and barva.get('val'):
                self.barvy_tematu[etree.QName(prvek).localname] = barva.get('val')
                continue
            system = prvek.find(_aq('sysClr'))
            if system is not None and system.get('lastClr'):
                self.barvy_tematu[etree.QName(prvek).localname] = system.get('lastClr')
        # Aliasy, kterými se na schéma odkazuje styl (text1 = dk1 …).
        for alias, zdrojova in (('text1', 'dk1'), ('text2', 'dk2'), ('dark1', 'dk1'),
                                ('dark2', 'dk2'), ('background1', 'lt1'), ('light1', 'lt1'),
                                ('background2', 'lt2'), ('light2', 'lt2'),
                                ('hyperlink', 'hlink'), ('followedHyperlink', 'folHlink')):
            if zdrojova in self.barvy_tematu:
                self.barvy_tematu.setdefault(alias, self.barvy_tematu[zdrojova])

    def _nacti_styly(self, zdroj: zipfile.ZipFile) -> None:
        try:
            koren = etree.fromstring(zdroj.read('word/styles.xml'))
        except (KeyError, etree.XMLSyntaxError):
            return
        vychozi = koren.find(_wq('docDefaults'))
        if vychozi is not None:
            rpr = vychozi.find(_wq('rPrDefault'))
            ppr = vychozi.find(_wq('pPrDefault'))
            self._vychozi_rpr = rpr.find(_wq('rPr')) if rpr is not None else None
            self._vychozi_ppr = ppr.find(_wq('pPr')) if ppr is not None else None
        for styl in koren.findall(_wq('style')):
            kod = styl.get(_wq('styleId'))
            if not kod:
                continue
            self._xml[kod] = styl
            self._typ[kod] = styl.get(_wq('type'), 'paragraph')
            self._rodic[kod] = _hodnota(styl.find(_wq('basedOn')))
            if (styl.get(_wq('default')) in ('1', 'true')
                    and styl.get(_wq('type')) == 'paragraph'):
                self.vychozi_odstavec = kod

    # -------- kaskáda --------
    def _z_rpr(self, rpr, cil: dict) -> None:
        """Písmo: velikost, font, barva, řezy, zvýraznění."""
        if rpr is None:
            return
        velikost = _hodnota(rpr.find(_wq('sz')))
        if velikost:
            try:
                cil['font-size'] = f'{int(velikost) / 2:g}pt'
            except ValueError:
                pass
        fonty = rpr.find(_wq('rFonts'))
        if fonty is not None:
            pismo = (fonty.get(_wq('ascii'))
                     or self.fonty_tematu.get(fonty.get(_wq('asciiTheme'), ''))
                     or fonty.get(_wq('hAnsi'))
                     or self.fonty_tematu.get(fonty.get(_wq('hAnsiTheme'), '')))
            if pismo:
                cil['font-family'] = f"'{pismo}'"
        barva = self._barva(rpr.find(_wq('color')))
        if barva:
            cil['color'] = barva
        tucne = _zapnuto(rpr.find(_wq('b')))
        if tucne is not None:
            cil['font-weight'] = '700' if tucne else '400'
        kurziva = _zapnuto(rpr.find(_wq('i')))
        if kurziva is not None:
            cil['font-style'] = 'italic' if kurziva else 'normal'
        ozdoby = []
        if _zapnuto(rpr.find(_wq('u'))) and _hodnota(rpr.find(_wq('u'))) != 'none':
            ozdoby.append('underline')
        if _zapnuto(rpr.find(_wq('strike'))):
            ozdoby.append('line-through')
        if ozdoby:
            cil['text-decoration'] = ' '.join(ozdoby)
        if _zapnuto(rpr.find(_wq('caps'))):
            cil['text-transform'] = 'uppercase'
        if _zapnuto(rpr.find(_wq('smallCaps'))):
            cil['font-variant'] = 'small-caps'
        zvyrazneni = _hodnota(rpr.find(_wq('highlight')))
        if zvyrazneni and zvyrazneni != 'none':
            cil['background-color'] = _ZVYRAZNENI.get(zvyrazneni, zvyrazneni)
        stin = rpr.find(_wq('shd'))
        if stin is not None and stin.get(_wq('fill')) not in (None, 'auto', 'FFFFFF'):
            cil['background-color'] = '#' + stin.get(_wq('fill')).lower()

    def _z_ppr(self, ppr, cil: dict) -> None:
        """Odstavec: zarovnání, odsazení, mezery, řádkování."""
        if ppr is None:
            return
        zarovnani = _hodnota(ppr.find(_wq('jc')))
        if zarovnani:
            cil['text-align'] = {'both': 'justify', 'start': 'left', 'end': 'right',
                                 'distribute': 'justify'}.get(zarovnani, zarovnani)
        odsazeni = ppr.find(_wq('ind'))
        if odsazeni is not None:
            vlevo = odsazeni.get(_wq('left')) or odsazeni.get(_wq('start'))
            if vlevo:
                try:
                    if int(vlevo) > 0:
                        cil['margin-left'] = f'{int(vlevo) / 20:g}pt'
                except ValueError:
                    pass
        mezery = ppr.find(_wq('spacing'))
        if mezery is not None:
            for atribut, vlastnost in (('before', 'margin-top'), ('after', 'margin-bottom')):
                hodnota = mezery.get(_wq(atribut))
                if hodnota:
                    try:
                        cil[vlastnost] = f'{int(hodnota) / 20:g}pt'
                    except ValueError:
                        pass
            radek = mezery.get(_wq('line'))
            if radek and mezery.get(_wq('lineRule'), 'auto') == 'auto':
                try:
                    cil['line-height'] = f'{int(radek) / 240:.2f}'
                except ValueError:
                    pass
        stin = ppr.find(_wq('shd'))
        if stin is not None and stin.get(_wq('fill')) not in (None, 'auto', 'FFFFFF'):
            cil['background-color'] = '#' + stin.get(_wq('fill')).lower()

    def _barva(self, el) -> str:
        if el is None:
            return ''
        tema = el.get(_wq('themeColor'))
        if tema and tema in self.barvy_tematu:
            return '#' + _odstin(self.barvy_tematu[tema], el.get(_wq('themeShade')),
                                 el.get(_wq('themeTint'))).lower()
        hodnota = _hodnota(el)
        if hodnota and hodnota != 'auto':
            return '#' + hodnota.lower()
        return ''

    def vlastnosti(self, kod: str, jen_vlastni: bool = False) -> dict:
        """Rozřešený vzhled stylu. `jen_vlastni` vynechá docDefaults — znakový
        styl smí přebít jen to, co sám (nebo jeho rodič) opravdu nastavuje."""
        klic = (kod, jen_vlastni)
        if klic in self._cache:
            return self._cache[klic]
        self._cache[klic] = {}          # zarážka proti zacyklení basedOn
        vysledek = {}
        if not jen_vlastni:
            self._z_rpr(self._vychozi_rpr, vysledek)
            self._z_ppr(self._vychozi_ppr, vysledek)
        retez = []
        chod, pojistka = kod, 0
        while chod and chod in self._xml and pojistka < 20:
            retez.append(chod)
            chod, pojistka = self._rodic.get(chod), pojistka + 1
        for predek in reversed(retez):     # od praotce k listu
            styl = self._xml[predek]
            self._z_ppr(styl.find(_wq('pPr')), vysledek)
            self._z_rpr(styl.find(_wq('rPr')), vysledek)
        if not jen_vlastni:
            uroven = self.uroven_nadpisu(kod)
            if uroven and 'font-size' not in vysledek:
                velikost, barva = _VESTAVENE_NADPISY[min(uroven, 6)]
                vysledek['font-size'] = velikost
                vysledek.setdefault('color', barva)
                vysledek.setdefault('font-weight', '700')
        self._cache[klic] = vysledek
        return vysledek

    def uroven_nadpisu(self, kod: str) -> int:
        """Úroveň nadpisu podle w:outlineLvl — funguje i v české šabloně, kde
        se styl nejmenuje Heading."""
        styl = self._xml.get(kod)
        if styl is None:
            return 0
        jmeno = _hodnota(styl.find(_wq('name')), '') or ''
        shoda = _STYL_NADPIS.match(jmeno.strip())
        if shoda:
            return int(shoda.group(1))
        if jmeno.strip().lower() in ('title', 'název', 'nazev'):
            return 1
        chod, pojistka = kod, 0
        while chod and chod in self._xml and pojistka < 20:
            ppr = self._xml[chod].find(_wq('pPr'))
            uroven = _hodnota(ppr.find(_wq('outlineLvl'))) if ppr is not None else None
            if uroven is not None:
                try:
                    cislo = int(uroven) + 1
                except ValueError:
                    return 0
                return cislo if cislo <= 9 else 0
            chod, pojistka = self._rodic.get(chod), pojistka + 1
        return 0

    # -------- výstup --------
    def trida(self, kod: str, znakovy: bool = False) -> str:
        """Jméno CSS třídy stylu; deklarace se cestou zaznamená do stylopisu."""
        if not kod or kod not in self._xml:
            return ''
        vlastnosti = self.vlastnosti(kod, jen_vlastni=znakovy)
        if not vlastnosti:
            return ''
        jmeno = 'w-' + (re.sub(r'[^a-z0-9]+', '-', _odstran_diakritiku(kod).lower()).strip('-')
                        or 'styl')
        self.tridy[jmeno] = ';'.join(f'{k}:{v}' for k, v in sorted(vlastnosti.items()))
        return jmeno

    def css_tela(self) -> str:
        """Vzhled běžného odstavce — sedne na celou čtečku, ať se neopakuje
        u každého <p>."""
        vlastnosti = self.vlastnosti(self.vychozi_odstavec)
        vlastnosti.pop('margin-top', None)
        vlastnosti.pop('margin-bottom', None)
        return ';'.join(f'{k}:{v}' for k, v in sorted(vlastnosti.items()))


# Staré obrázky (Word 2003 a starší vložení) sedí ve VML; python-docx tenhle
# jmenný prostor v mapě nemá, takže ho píšeme natvrdo.
_VML_IMAGEDATA = '{urn:schemas-microsoft-com:vml}imagedata'


def _odstran_diakritiku(text: str) -> str:
    return ''.join(z for z in unicodedata.normalize('NFKD', text)
                   if not unicodedata.combining(z))


def _slug(text: str, poradi: int) -> str:
    zaklad = re.sub(r'[^a-z0-9]+', '-', _odstran_diakritiku(text).lower()).strip('-')
    return f'k{poradi}-{zaklad[:60]}' if zaklad else f'k{poradi}'


class _Prevodnik:
    """Jeden průchod dokumentem. Drží rels kvůli obrázkům a odkazům."""

    def __init__(self, doc: Document, stylopis: _Stylopis):
        self.doc = doc
        self.part = doc.part
        self.stylopis = stylopis
        self.cislovani = self._nacti_cislovani()
        self.obrazky_ulozeny = 0

    # -------- styl odstavce a běhu --------
    def _kod_stylu(self, p: Paragraph) -> str:
        """styleId z pPr — jméno stylu se v každém jazyce Wordu liší, kód ne."""
        ppr = p._p.find(qn('w:pPr'))
        kod = _hodnota(ppr.find(qn('w:pStyle'))) if ppr is not None else None
        return kod or self.stylopis.vychozi_odstavec

    def _prime_css(self, rpr) -> list:
        """Formát nastavený rukou u konkrétního běhu — ten v žádné třídě není."""
        prime = {}
        self.stylopis._z_rpr(rpr, prime)
        # Řezy řeší <strong>/<em>/<u>/<s> níž, ať se HTML dá dál editovat.
        for klic in ('font-weight', 'font-style', 'text-decoration'):
            prime.pop(klic, None)
        return [f'{k}:{v}' for k, v in sorted(prime.items())]

    # -------- číslování seznamů --------
    def _nacti_cislovani(self) -> dict:
        """(numId, ilvl) → formát odrážky. Rozhoduje jen o <ul> vs <ol>."""
        mapa = {}
        try:
            koren = self.part.numbering_part.element
        except Exception:
            return mapa
        abstraktni = {}
        for an in koren.findall(qn('w:abstractNum')):
            aid = an.get(qn('w:abstractNumId'))
            for lvl in an.findall(qn('w:lvl')):
                ilvl = lvl.get(qn('w:ilvl'))
                fmt = lvl.find(qn('w:numFmt'))
                abstraktni[(aid, ilvl)] = fmt.get(qn('w:val')) if fmt is not None else 'decimal'
        for num in koren.findall(qn('w:num')):
            nid = num.get(qn('w:numId'))
            odkaz = num.find(qn('w:abstractNumId'))
            if odkaz is None:
                continue
            aid = odkaz.get(qn('w:val'))
            for (a, ilvl), fmt in abstraktni.items():
                if a == aid:
                    mapa[(nid, ilvl)] = fmt
        return mapa

    def _seznam_info(self, p: Paragraph):
        """Vrátí (úroveň, je_odrážkový) nebo None, když odstavec není v seznamu."""
        numpr = p._p.find(qn('w:pPr'))
        numpr = numpr.find(qn('w:numPr')) if numpr is not None else None
        if numpr is None:
            return None
        nid_el = numpr.find(qn('w:numId'))
        ilvl_el = numpr.find(qn('w:ilvl'))
        if nid_el is None:
            return None
        nid = nid_el.get(qn('w:val'))
        ilvl = ilvl_el.get(qn('w:val')) if ilvl_el is not None else '0'
        fmt = self.cislovani.get((nid, ilvl), 'bullet')
        try:
            uroven = int(ilvl)
        except (TypeError, ValueError):
            uroven = 0
        return uroven, fmt == 'bullet'

    # -------- obrázky --------
    def _uloz_obrazek(self, rid: str) -> str | None:
        try:
            obrazek = self.part.related_parts[rid]
            data = obrazek.blob
        except Exception:
            return None
        pripona = os.path.splitext(str(getattr(obrazek, 'partname', '')))[1].lower() or '.png'
        if pripona not in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.emf', '.wmf'):
            pripona = '.png'
        jmeno = hashlib.sha1(data).hexdigest()[:20] + pripona
        cesta = os.path.join(OBRAZKY_DIR, jmeno)
        if not os.path.exists(cesta):
            with open(cesta, 'wb') as f:
                f.write(data)
            self.obrazky_ulozeny += 1
        return jmeno

    def _obrazek_html(self, element) -> str:
        """Obrázek z w:drawing (moderní) i w:pict (starý VML)."""
        kusy = []
        for blip in element.findall('.//' + qn('a:blip')):
            rid = blip.get(qn('r:embed')) or blip.get(qn('r:link'))
            if not rid:
                continue
            jmeno = self._uloz_obrazek(rid)
            if not jmeno:
                continue
            # Šířka z dokumentu (EMU → px), ať obrázek nepřeteče sloupec.
            sirka = ''
            ext = element.find('.//' + qn('wp:extent'))
            if ext is not None and ext.get('cx'):
                try:
                    px = int(int(ext.get('cx')) / 9525)
                    if px > 0:
                        sirka = f'max-width:min(100%,{px}px);'
                except (TypeError, ValueError):
                    pass
            kusy.append(f'<img src="{URL_OBRAZKY}/obrazky/{jmeno}" loading="lazy" '
                        f'style="{sirka}" alt="">')
        for data in element.findall('.//' + _VML_IMAGEDATA):
            rid = data.get(qn('r:id'))
            jmeno = self._uloz_obrazek(rid) if rid else None
            if jmeno:
                kusy.append(f'<img src="{URL_OBRAZKY}/obrazky/{jmeno}" loading="lazy" alt="">')
        return ''.join(kusy)

    # -------- běhy textu --------
    def _run_html(self, r) -> str:
        obrazky = ''
        for tag in ('w:drawing', 'w:pict', 'w:object'):
            for el in r.findall(qn(tag)):
                obrazky += self._obrazek_html(el)

        kusy = []
        for el in r.iter():
            if el.tag == qn('w:t'):
                kusy.append(_html.escape(el.text or ''))
            elif el.tag == qn('w:tab'):
                kusy.append(' ')
            elif el.tag in (qn('w:br'), qn('w:cr')):
                kusy.append('<br>')
        text = ''.join(kusy)
        if not text.strip() and not obrazky:
            return ''

        if text:
            rpr = r.find(qn('w:rPr'))
            # Znakový styl → třída, ruční formát → inline. Nic se nedopočítává
            # ze stylu odstavce; ten sedí na <p> a dědí se v prohlížeči sám.
            trida = self.stylopis.trida(_hodnota(rpr.find(qn('w:rStyle'))) if rpr is not None
                                        else None, znakovy=True)
            css = self._prime_css(rpr)
            if trida or css:
                atributy = f' class="{trida}"' if trida else ''
                atributy += f' style="{";".join(css)}"' if css else ''
                text = f'<span{atributy}>{text}</span>'
            if rpr is not None:
                def zapnuto(tag):
                    el = rpr.find(qn(tag))
                    return el is not None and el.get(qn('w:val')) not in ('0', 'false', 'none')
                vert = rpr.find(qn('w:vertAlign'))
                if vert is not None and vert.get(qn('w:val')) == 'superscript':
                    text = f'<sup>{text}</sup>'
                elif vert is not None and vert.get(qn('w:val')) == 'subscript':
                    text = f'<sub>{text}</sub>'
                if zapnuto('w:b'):
                    text = f'<strong>{text}</strong>'
                if zapnuto('w:i'):
                    text = f'<em>{text}</em>'
                if zapnuto('w:u'):
                    text = f'<u>{text}</u>'
                if zapnuto('w:strike'):
                    text = f'<s>{text}</s>'
        return obrazky + text

    def _odkaz_html(self, hl) -> str:
        vnitrek = ''.join(self._run_html(r) for r in hl.findall(qn('w:r')))
        if not vnitrek:
            return ''
        rid = hl.get(qn('r:id'))
        cil = ''
        if rid:
            try:
                cil = self.part.rels[rid].target_ref
            except Exception:
                cil = ''
        if not cil or cil.startswith('file:') or cil.startswith('\\\\'):
            # Odkazy do sítě/na disk v prohlížeči stejně nefungují — jen text.
            return f'<span class="m-cesta">{vnitrek}</span>'
        return (f'<a href="{_html.escape(cil, quote=True)}" target="_blank" '
                f'rel="noopener">{vnitrek}</a>')

    def _vnitrek_odstavce(self, p: Paragraph) -> str:
        kusy = []
        for el in p._p.iterchildren():
            if el.tag == qn('w:r'):
                kusy.append(self._run_html(el))
            elif el.tag == qn('w:hyperlink'):
                kusy.append(self._odkaz_html(el))
        return ''.join(kusy).strip()

    def _atributy_odstavce(self, p: Paragraph) -> str:
        """Třída ze stylu + inline jen to, co je u odstavce nastavené rukou."""
        trida = self.stylopis.trida(self._kod_stylu(p))
        prime = {}
        self.stylopis._z_ppr(p._p.find(qn('w:pPr')), prime)
        atributy = f' class="{trida}"' if trida else ''
        if prime:
            atributy += ' style="' + ';'.join(f'{k}:{v}' for k, v in sorted(prime.items())) + '"'
        return atributy

    # -------- tabulky --------
    def _tabulka_html(self, t: Table) -> str:
        radky = []
        for i, radek in enumerate(t.rows):
            bunky = []
            for bunka in radek.cells:
                vnitrek = ''.join(f'<p{self._atributy_odstavce(p)}>{self._vnitrek_odstavce(p)}</p>'
                                  for p in bunka.paragraphs if self._vnitrek_odstavce(p))
                znacka = 'th' if i == 0 else 'td'
                bunky.append(f'<{znacka}>{vnitrek or "&nbsp;"}</{znacka}>')
            radky.append('<tr>' + ''.join(bunky) + '</tr>')
        if not radky:
            return ''
        return ('<div class="m-tbl-obal"><table class="m-tbl">'
                f'<thead>{radky[0]}</thead><tbody>{"".join(radky[1:])}</tbody>'
                '</table></div>')


def _bloky(doc: Document):
    """Odstavce i tabulky v pořadí, v jakém jsou v dokumentu."""
    for ch in doc.element.body.iterchildren():
        if ch.tag == qn('w:p'):
            yield Paragraph(ch, doc)
        elif ch.tag == qn('w:tbl'):
            yield Table(ch, doc)


def preved_docx(data: bytes, jmeno_souboru: str) -> dict:
    """DOCX → struktura kapitol. Obrázky vysype do OBRAZKY_DIR."""
    doc = Document(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as balik:
        stylopis = _Stylopis(balik)
    prevodnik = _Prevodnik(doc, stylopis)

    kapitoly = []
    aktualni = None
    otevreny_seznam = []   # zásobník (úroveň, značka) kvůli zanoření
    poradi_kotev = 0

    def nova_kapitola(nadpis: str, uroven: int, trida: str = ''):
        nonlocal poradi_kotev
        poradi_kotev += 1
        return {'id': _slug(nadpis, poradi_kotev), 'nadpis': nadpis, 'uroven': uroven,
                'cast': '', 'kotvy': [], 'html': [], 'text': [], 'trida': trida}

    def zavri_seznamy(do_urovne: int = -1):
        while otevreny_seznam and otevreny_seznam[-1][0] > do_urovne:
            _, znacka = otevreny_seznam.pop()
            aktualni['html'].append(f'</{znacka}>')

    for blok in _bloky(doc):
        if isinstance(blok, Table):
            if aktualni is None:
                aktualni = nova_kapitola('Úvod', 1)
                kapitoly.append(aktualni)
            zavri_seznamy()
            html_tab = prevodnik._tabulka_html(blok)
            if html_tab:
                aktualni['html'].append(html_tab)
                for radek in blok.rows:
                    aktualni['text'].append(' '.join(b.text for b in radek.cells))
            continue

        p = blok
        kod_stylu = prevodnik._kod_stylu(p)
        try:
            jmeno_stylu = p.style.name or ''
        except Exception:
            jmeno_stylu = ''
        if _STYL_TOC.match(jmeno_stylu) or _STYL_TOC.match(kod_stylu):
            continue

        vnitrek = prevodnik._vnitrek_odstavce(p)
        if not vnitrek:
            continue

        atr_p = prevodnik._atributy_odstavce(p)
        uroven = stylopis.uroven_nadpisu(kod_stylu)
        cisty = re.sub(r'<[^>]+>', '', vnitrek).strip()

        if uroven and cisty:
            zavri_seznamy()
            trida_nadpisu = stylopis.trida(kod_stylu)
            if uroven <= UROVEN_KAPITOLY:
                aktualni = nova_kapitola(cisty, uroven, trida_nadpisu)
                kapitoly.append(aktualni)
                continue
            # Podnadpis uvnitř kapitoly → kotva do bočního panelu.
            if aktualni is None:
                aktualni = nova_kapitola(cisty, uroven, trida_nadpisu)
                kapitoly.append(aktualni)
                continue
            poradi_kotev += 1
            kotva = _slug(cisty, poradi_kotev)
            aktualni['kotvy'].append({'id': kotva, 'nadpis': cisty, 'uroven': uroven})
            stupen = min(uroven, 6)
            aktualni['html'].append(
                f'<h{stupen} id="{kotva}" class="m-nadpis {trida_nadpisu}">{vnitrek}</h{stupen}>')
            aktualni['text'].append(cisty)
            continue

        if aktualni is None:
            aktualni = nova_kapitola('Úvod', 1)
            kapitoly.append(aktualni)

        seznam = prevodnik._seznam_info(p)
        if seznam is not None:
            hloubka, odrazkovy = seznam
            znacka = 'ul' if odrazkovy else 'ol'
            zavri_seznamy(hloubka)
            if not otevreny_seznam or otevreny_seznam[-1][0] < hloubka:
                aktualni['html'].append(f'<{znacka}>')
                otevreny_seznam.append((hloubka, znacka))
            elif otevreny_seznam[-1][1] != znacka:
                aktualni['html'].append(f'</{otevreny_seznam.pop()[1]}>')
                aktualni['html'].append(f'<{znacka}>')
                otevreny_seznam.append((hloubka, znacka))
            aktualni['html'].append(f'<li{atr_p}>{vnitrek}</li>')
        else:
            zavri_seznamy()
            aktualni['html'].append(f'<p{atr_p}>{vnitrek}</p>')
        if cisty:
            aktualni['text'].append(cisty)

    if aktualni is not None:
        zavri_seznamy()

    # Nadpis bez vlastního textu je v těchhle dokumentech předěl mezi celky
    # („Reporty", „Sítě", „COGS"). Nedělá se z něj prázdná kapitola, jen
    # pojmenuje skupinu v obsahu. Úroveň nadpisu se na to spolehnout nedá —
    # ve sloučeném dokumentu jsou předěly jednou H1, jednou H2.
    cast = ''
    bez_predelu = []
    for k in kapitoly:
        telo = ''.join(k['html'])
        # Prázdný odstavec z Wordu je `<p><br></p>`, ne prázdný řetězec.
        if '<img' not in telo and not re.sub(r'<[^>]+>|&nbsp;|\s', '', telo):
            cast = k['nadpis']
            continue
        k['cast'] = cast
        bez_predelu.append(k)
    if bez_predelu:
        kapitoly = bez_predelu

    hotove = []
    for k in kapitoly:
        text = ' '.join(k['text'])
        hotove.append({
            'id': k['id'], 'nadpis': k['nadpis'], 'uroven': k['uroven'], 'cast': k['cast'],
            'trida': k.get('trida', ''), 'kotvy': k['kotvy'], 'html': ''.join(k['html']),
            'text': text, 'hledej': _odstran_diakritiku(text + ' ' + k['nadpis']).lower(),
        })

    return {
        'nazev': os.path.splitext(jmeno_souboru)[0],
        'soubor': jmeno_souboru,
        'nahrano': datetime.datetime.now().strftime('%d.%m.%Y %H:%M'),
        # Stylopis: Wordovy styly rozřešené na CSS třídy. Bez něj by kapitoly
        # spadly na tovární vzhled prohlížeče — proto se vozí s manuálem.
        'format': 2,
        'styly': stylopis.tridy,
        'css_telo': stylopis.css_tela(),
        'kapitoly': hotove,
    }


def _zapis_json(manual: dict) -> None:
    """Atomický zápis manuálu + zneplatnění cache."""
    docasny = JSON_SOUBOR + '.tmp'
    with open(docasny, 'w', encoding='utf-8') as f:
        json.dump(manual, f, ensure_ascii=False)
    os.replace(docasny, JSON_SOUBOR)
    _CACHE.clear()


def uloz_prevod(data: bytes, jmeno_souboru: str, user_name: str = '') -> dict:
    """Převede DOCX, uloží JSON i originál a vyprázdní cache."""
    manual = preved_docx(data, jmeno_souboru)
    bezpecne_jmeno = re.sub(r'[^\w.\- ]', '_', jmeno_souboru) or 'manual.docx'
    with open(os.path.join(MANUALY_DIR, bezpecne_jmeno), 'wb') as f:
        f.write(data)
    manual['soubor'] = bezpecne_jmeno
    zaloz_verzi(f'nahrání souboru {bezpecne_jmeno}')
    _zapis_json(manual)
    return manual


# ============================================================
# ==                    Verze a editace                     ==
# ============================================================

def seznam_verzi() -> list:
    """Odložené podoby manuálu, nejnovější první."""
    try:
        jmena = os.listdir(VERZE_DIR)
    except OSError:
        return []
    verze = []
    for jmeno in sorted(jmena, reverse=True):
        if not (jmeno.startswith('manual-') and jmeno.endswith('.json')):
            continue
        cesta = os.path.join(VERZE_DIR, jmeno)
        try:
            cas = datetime.datetime.fromtimestamp(os.path.getmtime(cesta))
        except OSError:
            continue
        popis = ''
        souhrn = os.path.splitext(cesta)[0] + '.txt'
        try:
            with open(souhrn, encoding='utf-8') as f:
                popis = f.read().strip()
        except OSError:
            pass
        verze.append({'jmeno': jmeno, 'cesta': cesta, 'popis': popis,
                      'cas': cas.strftime('%d.%m.%Y %H:%M:%S')})
    return verze


def zaloz_verzi(duvod: str) -> None:
    """Kopie současného JSONu stranou. Drží se jen POCET_VERZI posledních."""
    if not os.path.exists(JSON_SOUBOR):
        return
    os.makedirs(VERZE_DIR, exist_ok=True)
    # Mikrosekundy: dvě zálohy v téže sekundě (nahrání + hned úprava) se nepřepíšou.
    zaklad = datetime.datetime.now().strftime('manual-%Y%m%d-%H%M%S-%f')
    shutil.copy2(JSON_SOUBOR, os.path.join(VERZE_DIR, zaklad + '.json'))
    with open(os.path.join(VERZE_DIR, zaklad + '.txt'), 'w', encoding='utf-8') as f:
        f.write(f'Stav před: {duvod}')
    for stara in seznam_verzi()[POCET_VERZI:]:
        for cesta in (stara['cesta'], os.path.splitext(stara['cesta'])[0] + '.txt'):
            try:
                os.remove(cesta)
            except OSError:
                pass


def obnov_verzi(jmeno: str, user_name: str) -> dict | None:
    """Vrátí manuál do odložené podoby. Současný stav se předtím odloží taky."""
    cesta = os.path.join(VERZE_DIR, os.path.basename(jmeno))
    try:
        with open(cesta, encoding='utf-8') as f:
            manual = json.load(f)
    except (OSError, ValueError):
        return None
    zaloz_verzi(f'obnovení verze {os.path.basename(jmeno)}')
    _zapis_json(manual)
    intranet_logger.log_activity(user_name, 'Manuály',
                                 f'Obnovena starší verze manuálu ({os.path.basename(jmeno)})')
    return manual


_ZAKAZANE_ZNACKY = re.compile(r'(?is)<\s*(script|style|iframe|object|embed|form)\b[^>]*>.*?<\s*/\s*\1\s*>')
_ZAKAZANE_SAMOSTATNE = re.compile(r'(?is)<\s*/?\s*(script|style|iframe|object|embed|form|link|meta)\b[^>]*>')
_UDALOSTI = re.compile(r'''(?is)\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)''')
_JS_ODKAZ = re.compile(r'''(?is)\b(href|src)\s*=\s*("|')?\s*javascript:[^"'>\s]*("|')?''')


def _ocisti_html(html_text: str) -> str:
    """Editor je jen pro správce, přesto ven nepustíme skripty ani handlery."""
    cisty = _ZAKAZANE_ZNACKY.sub('', html_text or '')
    cisty = _ZAKAZANE_SAMOSTATNE.sub('', cisty)
    cisty = _UDALOSTI.sub('', cisty)
    return _JS_ODKAZ.sub('', cisty)


def uloz_kapitolu(manual: dict, index: int, html_text: str, user_name: str) -> None:
    """Přepíše kapitolu z editoru — se zálohou předchozí podoby a zápisem do logu."""
    kapitola = manual['kapitoly'][index]
    kapitola['html'] = _ocisti_html(html_text)
    text = _html.unescape(re.sub(r'<[^>]+>', ' ', kapitola['html']))
    text = re.sub(r'\s+', ' ', text).strip()
    kapitola['text'] = text
    kapitola['hledej'] = _odstran_diakritiku(text + ' ' + kapitola['nadpis']).lower()
    # Kotvy v panelu musí sedět na nadpisy, které v upraveném HTML zbyly.
    zbyle = set(re.findall(r'id="([^"]+)"', kapitola['html']))
    kapitola['kotvy'] = [k for k in kapitola.get('kotvy', []) if k['id'] in zbyle]
    manual['upraveno'] = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    manual['upravil'] = user_name
    zaloz_verzi(f"úprava kapitoly „{kapitola['nadpis']}“")
    _zapis_json(manual)
    intranet_logger.log_activity(user_name, 'Manuály',
                                 f"Upravena kapitola „{kapitola['nadpis']}“")


def _volne_id(kapitoly: list, nadpis: str) -> str:
    """Kotvy i odkazy stojí na id — nová kapitola nesmí sebrat cizí."""
    pouzita = {k.get('id') for k in kapitoly}
    poradi = len(kapitoly) + 1
    while _slug(nadpis, poradi) in pouzita:
        poradi += 1
    return _slug(nadpis, poradi)


def vytvor_kapitolu(manual: dict, po_indexu: int, nadpis: str,
                    cast: str, user_name: str) -> int:
    """Vloží prázdnou kapitolu za danou pozici. Vrací její index."""
    nadpis = (nadpis or '').strip()
    if not nadpis:
        raise ValueError('Kapitola musí mít nadpis.')
    kapitoly = manual['kapitoly']
    soused = kapitoly[po_indexu] if 0 <= po_indexu < len(kapitoly) else {}
    index = min(max(po_indexu + 1, 0), len(kapitoly))
    kapitoly.insert(index, {
        'id': _volne_id(kapitoly, nadpis),
        'nadpis': nadpis,
        'uroven': soused.get('uroven', UROVEN_KAPITOLY),
        'cast': (cast or '').strip(),
        # Nadpis drží formát sousední kapitoly, ať nová nevyčnívá z dokumentu.
        'trida': soused.get('trida', ''),
        'kotvy': [],
        'html': '',
        'text': '',
        'hledej': _odstran_diakritiku(nadpis).lower(),
    })
    manual['upraveno'] = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    manual['upravil'] = user_name
    zaloz_verzi(f'nová kapitola „{nadpis}“')
    _zapis_json(manual)
    intranet_logger.log_activity(user_name, 'Manuály',
                                 f'Vytvořena kapitola „{nadpis}“')
    return index


# ============================================================
# ==                    Načtení a hledání                   ==
# ============================================================

_CACHE = {}


def nacti_manual() -> dict | None:
    """Hotový manuál z RAM; z disku jen když se JSON od minule změnil."""
    try:
        mtime = os.path.getmtime(JSON_SOUBOR)
    except OSError:
        return None
    if _CACHE.get('mtime') != mtime:
        try:
            with open(JSON_SOUBOR, encoding='utf-8') as f:
                _CACHE['data'] = json.load(f)
            _CACHE['mtime'] = mtime
        except Exception as e:
            print(f'[manualy] JSON se nepodařilo načíst: {e}')
            return None
        _prevod_na_format2(_CACHE['data'])
    return _CACHE.get('data')


def _prevod_na_format2(manual: dict) -> None:
    """Manuál z verze bez stylopisu se přesype znovu z uloženého DOCX.

    Starý formát nesl vzhled jako inline `style=` u každého odstavce a po
    přechodu na třídy by zůstal bez formátu. Originál leží na disku, tak se
    převod zopakuje — jednou, při prvním načtení po aktualizaci.
    """
    if manual.get('format', 1) >= 2:
        return
    cesta = os.path.join(MANUALY_DIR, manual.get('soubor') or '')
    if not manual.get('soubor') or not os.path.exists(cesta):
        return
    try:
        with open(cesta, 'rb') as f:
            novy = preved_docx(f.read(), manual['soubor'])
    except Exception as e:
        print(f'[manualy] převod na nový formát selhal: {e}')
        return
    novy['upraveno'] = manual.get('upraveno', '')
    novy['upravil'] = manual.get('upravil', '')
    novy['nahrano'] = manual.get('nahrano', novy['nahrano'])
    zaloz_verzi('před převodem na formát se styly z Wordu')
    _zapis_json(novy)
    _CACHE['data'] = novy
    try:
        _CACHE['mtime'] = os.path.getmtime(JSON_SOUBOR)
    except OSError:
        _CACHE.pop('mtime', None)


# Písmena s diakritikou pro hledání „bez háčků" — `zasoba` musí najít `Zásoba`.
_VARIANTY = {}
for _z in 'áäčćďéěëíîľĺňóôöřŕšśťúůüýžźż':
    _VARIANTY.setdefault(_odstran_diakritiku(_z), set()).add(_z)


def _vzor_dotazu(dotaz: str) -> re.Pattern | None:
    kusy = []
    for znak in dotaz.strip():
        zaklad = _odstran_diakritiku(znak).lower()
        varianty = _VARIANTY.get(zaklad, set())
        if varianty or zaklad.isalnum():
            trida = ''.join(re.escape(z) for z in sorted({zaklad, *varianty}))
            kusy.append(f'[{trida}]' if len(trida) > 1 else re.escape(zaklad))
        elif znak.isspace():
            kusy.append(r'\s+')
        else:
            kusy.append(re.escape(znak))
    if not kusy:
        return None
    try:
        return re.compile(''.join(kusy), re.I)
    except re.error:
        return None


def hledej(manual: dict, dotaz: str, limit: int = 80) -> list:
    """Kapitoly obsahující dotaz + útržek okolí první shody."""
    dotaz = (dotaz or '').strip()
    if len(dotaz) < 2:
        return []
    jehla = _odstran_diakritiku(dotaz).lower()
    vzor = _vzor_dotazu(dotaz)
    vysledky = []
    for i, k in enumerate(manual['kapitoly']):
        pocet = k['hledej'].count(jehla)
        if not pocet:
            continue
        utrzek = ''
        if vzor:
            shoda = vzor.search(k['text'])
            if shoda:
                od = max(0, shoda.start() - 60)
                do = min(len(k['text']), shoda.end() + 90)
                utrzek = ('… ' if od else '') + k['text'][od:do] + (' …' if do < len(k['text']) else '')
        vysledky.append({'index': i, 'nadpis': k['nadpis'], 'pocet': pocet, 'utrzek': utrzek})
    vysledky.sort(key=lambda v: -v['pocet'])
    return vysledky[:limit]




def zvyrazni(html_text: str, dotaz: str) -> str:
    """Obalí shody do <mark>, ale jen v textu — nikdy uvnitř značky."""
    vzor = _vzor_dotazu(dotaz)
    if not vzor or len((dotaz or '').strip()) < 2:
        return html_text
    vysledek = []
    for kus in re.split(r'(<[^>]+>)', html_text):
        if kus.startswith('<'):
            vysledek.append(kus)
        else:
            vysledek.append(vzor.sub(lambda m: f'<mark class="m-nalez">{m.group(0)}</mark>', kus))
    return ''.join(vysledek)


# ============================================================
# ==                        UI                              ==
# ============================================================

URL_CTENI = '/manualy_cteni'

# Tělo manuálu se NEvykresluje v NiceGUI, ale jako samostatná HTML stránka
# v <iframe>. Ostrý provoz běží NiceGUI 3.8, kde se styl vložený do hlavičky
# lazy renderovaného tabu ztrácí a wordovské formáty zmizí; navíc do obsahu
# sahá Tailwind i Quasar reset. Samostatný dokument si CSS nese v sobě,
# takže vypadá stejně na 3.8 i 3.16 a nikdo mu do stylu nemluví.
_CSS_STRANKY = '''
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:18px 28px 64px;background:#fff;color:#1f2937;
 font-family:'Aptos','Segoe UI',Roboto,Arial,sans-serif;font-size:15px;line-height:1.65}
p{margin:0 0 .6rem}
ul,ol{margin:0 0 .7rem 1.4rem;padding-left:.6rem}
ul{list-style:disc}
ol{list-style:decimal}
li{margin:.15rem 0}
h1,h2,h3,h4,h5,h6{margin:1.4rem 0 .5rem;line-height:1.3;scroll-margin-top:14px}
body>h1:first-child,body>h2:first-child{margin-top:0}
.m-nadpis-nahradni{font-size:1.5rem;font-weight:700;color:#065f46}
img{display:block;margin:.8rem 0;max-width:100%;height:auto;border:1px solid #e5e7eb;
 border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
a{color:#1d4ed8}
.m-cesta{font-family:ui-monospace,monospace;font-size:.86em;background:#f1f5f9;
 padding:.05rem .3rem;border-radius:4px;word-break:break-all}
.m-zvyr{background:#fef08a;padding:0 .15rem}
.m-tbl-obal{overflow-x:auto;margin:.8rem 0}
table.m-tbl{border-collapse:collapse;font-size:13.5px;min-width:60%}
table.m-tbl th,table.m-tbl td{border:1px solid #cbd5e1;padding:.35rem .55rem;vertical-align:top}
table.m-tbl th{background:#ecfdf5;font-weight:700;text-align:left}
table.m-tbl tbody tr:nth-child(even){background:#f8fafc}
table.m-tbl p{margin:0}
mark.m-nalez{background:#fde047;color:inherit;border-radius:3px;padding:0 .1rem}
mark.m-nalez.m-aktivni{background:#fb923c;color:#fff}
#u-zvyr{position:absolute;display:none;gap:6px;padding:5px 8px;background:#fff;
 border:1px solid #cbd5e1;border-radius:999px;box-shadow:0 2px 10px rgba(0,0,0,.18);z-index:9999}
#u-zvyr button{width:20px;height:20px;padding:0;border-radius:50%;cursor:pointer;
 border:1px solid rgba(0,0,0,.25);font-size:12px;line-height:1;background:#fff}
#u-zvyr button:hover{outline:2px solid #065f46;outline-offset:1px}
'''

# Word si veze vlastní písmo a řádkování; čitelnost je přednější — přepíšeme je
# až za wordovským stylopisem, velikosti/tučnost/barvy nadpisů zůstávají.
_CSS_PISMO = '''
body,p,li,td,th,span,div,a,h1,h2,h3,h4,h5,h6{font-family:Arial,Helvetica,sans-serif !important}
body,p,li{line-height:1.9 !important}
p{margin:0 0 .75rem}
li{margin:.3rem 0}
td,th{line-height:1.6 !important}
h1,h2,h3,h4,h5,h6{line-height:1.35 !important}
.m-cesta{font-family:ui-monospace,monospace !important}
'''

# Odkazy pryč z rámu (jinak by cizí web nahradil manuál), skok na první nález
# a zvýrazňovač: táhnutím se vybere text, bublina nabídne barvu. Zvýraznění žije
# jen v DOMu téhle stránky — nikam se neukládá, po obnovení je pryč a vidí ho
# pouze ten, kdo si ho udělal.
_SKRIPT_STRANKY = '''
for(const a of document.links){const h=a.getAttribute('href')||'';
 if(h.startsWith('#'))continue;a.target='_blank';a.rel='noopener noreferrer';}
const n=document.querySelector('mark');
if(n)n.scrollIntoView({block:'center'});

const BARVY=[['#fde047','Žlutá'],['#86efac','Zelená'],['#93c5fd','Modrá'],['#f9a8d4','Růžová']];
const panel=document.createElement('div');
panel.id='u-zvyr';
function obarvi(barva){
 const v=document.getSelection();
 if(!v||v.isCollapsed)return;
 document.designMode='on';
 try{document.execCommand('styleWithCSS',false,true);
     document.execCommand('hiliteColor',false,barva);}catch(e){}
 document.designMode='off';
 v.removeAllRanges();
 panel.style.display='none';
}
for(const [barva,nazev] of BARVY){
 const b=document.createElement('button');
 b.style.background=barva;b.title=nazev+' — zvýraznění zmizí po obnovení stránky';
 b.addEventListener('mousedown',e=>e.preventDefault());
 b.addEventListener('click',()=>obarvi(barva));
 panel.appendChild(b);
}
const guma=document.createElement('button');
guma.textContent='⌫';guma.title='Zrušit zvýraznění výběru';
guma.addEventListener('mousedown',e=>e.preventDefault());
guma.addEventListener('click',()=>obarvi('transparent'));
panel.appendChild(guma);
document.body.appendChild(panel);

document.addEventListener('mouseup',e=>{
 if(panel.contains(e.target))return;
 const v=document.getSelection();
 if(!v||v.isCollapsed||!v.toString().trim()){panel.style.display='none';return;}
 const r=v.getRangeAt(0).getBoundingClientRect();
 panel.style.display='flex';
 const sirka=panel.offsetWidth;
 let x=r.left+window.scrollX+r.width/2-sirka/2;
 x=Math.max(6,Math.min(x,document.documentElement.clientWidth-sirka-6));
 let y=r.top+window.scrollY-panel.offsetHeight-8;
 if(y<window.scrollY+2)y=r.bottom+window.scrollY+8;
 panel.style.left=x+'px';panel.style.top=y+'px';
});
document.addEventListener('mousedown',e=>{
 if(!panel.contains(e.target))panel.style.display='none';
});
'''

_CSS = '''
.manual-editor { display: flex; flex-direction: column; min-height: 0; }
.manual-editor .q-editor__content { flex: 1; overflow-y: auto; }
'''


def vloz_css() -> None:
    """Drobné CSS editoru do hlavičky stránky (volá rozcestník v intranet.py).

    Vzhled manuálu na tomhle nestojí — ten si nese samostatná stránka kapitoly
    ve vlastním <style>. Tady zbývá jen rozměr editačního okna.
    """
    ui.add_head_html(f'<style>{_CSS}</style>')


def _css_stranky(manual: dict) -> str:
    """Wordovský stylopis + základ stránky, vše do <style> jednoho dokumentu."""
    kusy = [_CSS_STRANKY]
    telo = manual.get('css_telo')
    if telo:
        kusy.append(f'body{{{telo}}}')
    kusy += [f'.{jmeno}{{{vlastnosti}}}'
             for jmeno, vlastnosti in (manual.get('styly') or {}).items()]
    kusy.append(_CSS_PISMO)
    return '\n'.join(kusy)


def _css_editoru(manual: dict) -> str:
    """Týž stylopis pro editor — vkládá se do obsahu, ne do hlavičky stránky."""
    kusy = [f".q-editor__content{{{manual.get('css_telo') or ''}}}",
            '.q-editor__content img{max-width:100%;height:auto}',
            '.q-editor__content table{border-collapse:collapse}',
            '.q-editor__content td,.q-editor__content th'
            '{border:1px solid #cbd5e1;padding:.35rem .55rem}']
    kusy += [f'.{jmeno}{{{vlastnosti}}}'
             for jmeno, vlastnosti in (manual.get('styly') or {}).items()]
    # Ať editor ukazuje totéž písmo co čtečka, jinak by se po uložení vzhled hnul.
    kusy += ['.q-editor__content,.q-editor__content *'
             '{font-family:Arial,Helvetica,sans-serif !important}',
             '.q-editor__content p,.q-editor__content li{line-height:1.9 !important}']
    return '\n'.join(kusy)


def stranka_kapitoly(manual: dict, index: int, dotaz: str = '') -> str:
    """Jedna kapitola jako úplný HTML dokument pro <iframe>."""
    kapitola = manual['kapitoly'][index]
    nadpis = _html.escape(kapitola.get('nadpis') or '')
    trida = _html.escape(kapitola.get('trida') or 'm-nadpis-nahradni', quote=True)
    obsah = kapitola.get('html') or ''
    if dotaz:
        obsah = zvyrazni(obsah, dotaz)
    return ('<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{nadpis}</title><style>{_css_stranky(manual)}</style></head>'
            f'<body><h2 class="{trida}">{nadpis}</h2>{obsah}'
            f'<script>{_SKRIPT_STRANKY}</script></body></html>')


@app.get(URL_CTENI + '/{index}', include_in_schema=False, name='manualy_cteni')
def _cteni_kapitoly(index: int, q: str = '', v: str = ''):
    """Obsah manuálu mimo NiceGUI. Bez přihlášení a bez práva čtení 404 —
    ať se z odpovědi nedá vyčíst, co v manuálu je."""
    if not intranet_static.je_prihlasen():
        return PlainTextResponse('Nenalezeno', status_code=404)
    try:
        prava = [p.lower() for p in
                 intranet_data.ziskej_prava_uzivatele(app.storage.user.get('user_id'))]
    except Exception:
        prava = []
    if not smi_cist(prava):
        return PlainTextResponse('Nenalezeno', status_code=404)
    manual = nacti_manual()
    kapitoly = (manual or {}).get('kapitoly') or []
    if not kapitoly:
        return PlainTextResponse('Nenalezeno', status_code=404)
    index = max(0, min(index, len(kapitoly) - 1))
    return HTMLResponse(stranka_kapitoly(manual, index, q),
                        headers={'Cache-Control': 'no-store',
                                 'X-Frame-Options': 'SAMEORIGIN'})


def vykresli_manualy(user_id, user_name, vsechna_prava):
    """Vstupní bod modulu — volá se z rozcestníku intranetu."""
    if not smi_cist(vsechna_prava):
        ui.label('Na tento modul nemáte právo.').classes('text-red-600 p-4')
        return
    je_admin = smi_spravovat(vsechna_prava)
    manual = nacti_manual()
    if not manual or not manual.get('kapitoly'):
        _prazdno(je_admin, user_name)
        return

    kapitoly = manual['kapitoly']
    stav = {'index': 0, 'dotaz': '', 'kotva': '', 'otisk': 0}
    # Dialog vzniká ve slotu odesílatele; panel se překresluje, tak dialogy
    # věšíme na stabilní kotvu mimo něj, jinak zůstanou neviditelné.
    kotva_dialogu = ui.element('div')

    with ui.row().classes('w-full no-wrap gap-4 items-start'):

        # ---------- levý panel: hledání + části manuálu ----------
        with ui.column().classes('w-72 shrink-0 gap-2'):
            hledani = ui.input(placeholder='Hledat v manuálu…') \
                .props('dense outlined clearable debounce=300 '
                       'autocomplete=off bg-color=white') \
                .classes('w-full')
            hledani.on_value_change(lambda e: _hledani_zmena(e.value))
            with ui.card().classes('w-full p-0 overflow-hidden'):
                panel = ui.column().classes('w-full gap-0 p-0')

        # ---------- pravá část: lišta + stránka kapitoly v rámu ----------
        with ui.column().classes('flex-1 min-w-0 gap-2'):
            with ui.row().classes('w-full items-center no-wrap gap-2 '
                                  'rounded-lg bg-emerald-700 text-white px-3 py-1'):
                ui.icon('menu_book')
                nadpis_l = ui.label().classes('text-base font-bold truncate')
                ui.space()
                pozice_l = ui.label().classes('text-xs opacity-80 whitespace-nowrap')
                ui.button(icon='chevron_left', on_click=lambda: _skoc(-1)) \
                    .props('flat dense round color=white').tooltip('Předchozí kapitola')
                ui.button(icon='chevron_right', on_click=lambda: _skoc(1)) \
                    .props('flat dense round color=white').tooltip('Další kapitola')
                if je_admin:
                    ui.separator().props('vertical dark')
                    ui.button(icon='edit', on_click=lambda: _dialog_editace(stav['index'])) \
                        .props('flat dense round color=white').tooltip('Upravit kapitolu')
                    ui.button(icon='playlist_add', on_click=lambda: _dialog_nova_kapitola()) \
                        .props('flat dense round color=white').tooltip('Nová kapitola')
                    ui.button(icon='history', on_click=lambda: _dialog_verze(user_name)) \
                        .props('flat dense round color=white').tooltip('Historie verzí')
                    ui.button(icon='upload_file', on_click=lambda: _dialog_nahrani(user_name)) \
                        .props('flat dense round color=white').tooltip('Nahrát nový DOCX')
            telo = ui.column().classes('w-full gap-0')

    # ---------- vykreslování ----------

    def _vykresli_telo():
        kapitola = kapitoly[stav['index']]
        nadpis_l.set_text(kapitola.get('nadpis') or '')
        pozice_l.set_text(f"{stav['index'] + 1} / {len(kapitoly)}")
        adresa = f"{URL_CTENI}/{stav['index']}?v={stav['otisk']}"
        if stav['dotaz']:
            adresa += '&q=' + urllib.parse.quote(stav['dotaz'])
        if stav['kotva']:
            adresa += '#' + urllib.parse.quote(stav['kotva'])
        telo.clear()
        with telo:
            # Pozor: ui.html() prohání obsah přes DOMPurify a <iframe> zahodí
            # (prázdné tělo manuálu). Proto stavíme element napřímo.
            ui.element('iframe').props(f'src="{adresa}" title="Manuál"') \
                .style('width:100%;height:calc(100vh - 240px);min-height:420px;'
                       'border:1px solid #d1d5db;border-radius:8px;background:#fff')

    def _otevri(index: int, kotva: str = ''):
        stav['index'] = max(0, min(index, len(kapitoly) - 1))
        stav['kotva'] = kotva
        _vykresli_telo()
        _vykresli_panel()

    def _skoc(o: int):
        _otevri(stav['index'] + o)

    def _vykresli_panel():
        panel.clear()
        with panel:
            _panel_vysledky() if stav['dotaz'] else _panel_obsah()

    def _panel_obsah():
        casti = {}
        for i, k in enumerate(kapitoly):
            casti.setdefault(k.get('cast') or 'Manuál', []).append(i)
        with ui.row().classes('w-full items-center gap-2 px-3 py-2 bg-gray-50'):
            ui.icon('list').classes('text-emerald-700')
            ui.label('Obsah').classes('text-sm font-bold text-gray-700')
            ui.space()
            ui.label(f'{len(kapitoly)} kapitol').classes('text-xs text-gray-400')
        with ui.scroll_area().classes('w-full') \
                .style('height: calc(100vh - 300px); min-height: 360px'):
            for nazev, indexy in casti.items():
                with ui.expansion(nazev, value=stav['index'] in indexy) \
                        .classes('w-full text-sm') \
                        .props('dense dense-toggle switch-toggle-side'):
                    for i in indexy:
                        _radek_kapitoly(i)

    def _radek_kapitoly(i: int):
        aktivni = i == stav['index']
        trida = 'w-full rounded px-2 py-1 cursor-pointer text-xs '
        trida += ('bg-emerald-50 text-emerald-800 font-bold border-l-4 border-emerald-600'
                  if aktivni else 'text-gray-600 hover:bg-gray-100')
        radek = ui.label(kapitoly[i].get('nadpis') or '').classes(trida)
        radek.on('click', lambda i=i: _otevri(i))

    def _panel_vysledky():
        vysledky = hledej(manual, stav['dotaz'])
        with ui.row().classes('w-full items-center gap-2 px-3 py-2 bg-amber-50'):
            ui.icon('search').classes('text-amber-700')
            ui.label(f'Nalezeno: {len(vysledky)}').classes('text-sm font-bold text-gray-700')
            ui.space()
            ui.button(icon='close', on_click=lambda: (hledani.set_value(''),
                                                      _hledani_zmena(''))) \
                .props('flat dense round size=sm').tooltip('Zrušit hledání')
        if not vysledky:
            ui.label('Nic se nenašlo.').classes('text-xs text-gray-400 px-3 py-4')
            return
        with ui.scroll_area().classes('w-full') \
                .style('height: calc(100vh - 300px); min-height: 360px'):
            for v in vysledky:
                with ui.column().classes('w-full gap-0 px-2 py-1 rounded cursor-pointer '
                                         'hover:bg-gray-100') as polozka:
                    with ui.row().classes('w-full items-center no-wrap gap-1'):
                        ui.label(v['nadpis']).classes('text-xs font-bold text-emerald-800 truncate')
                        ui.space()
                        ui.label(str(v['pocet'])).classes('text-[10px] text-white bg-amber-500 '
                                                          'rounded-full px-1.5')
                    if v['utrzek']:
                        ui.label(v['utrzek']).classes('text-[11px] text-gray-500 leading-tight')
                polozka.on('click', lambda i=v['index']: _otevri(i))

    def _hledani_zmena(hodnota):
        dotaz = (hodnota or '').strip()
        if dotaz == stav['dotaz']:
            return
        stav['dotaz'] = dotaz
        if dotaz:
            nalezy = hledej(manual, dotaz)
            if nalezy:
                stav['index'] = nalezy[0]['index']
        stav['kotva'] = ''
        _vykresli_telo()
        _vykresli_panel()

    # ---------- správa ----------

    def _dialog_editace(index: int):
        kapitola = kapitoly[index]
        with kotva_dialogu, ui.dialog().props('maximized persistent') as dlg, \
                ui.card().classes('w-full h-full flex flex-col p-3 gap-2'):
            with ui.row().classes('w-full items-center gap-2'):
                ui.icon('edit').classes('text-emerald-700')
                ui.label(f"Úprava kapitoly: {kapitola.get('nadpis') or ''}") \
                    .classes('text-base font-bold')
                ui.space()
                ui.button(icon='close', on_click=dlg.close).props('flat dense round')
            editor = ui.editor(value=f'<style>{_css_editoru(manual)}</style>'
                                     + (kapitola.get('html') or '')) \
                .classes('w-full flex-1 manual-editor')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.label('Každá úprava se zapisuje do logu a zakládá novou verzi.') \
                    .classes('text-xs text-gray-400 self-center mr-auto')
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                ui.button('Uložit úpravy', icon='save', on_click=lambda: _uloz(editor, index, dlg)) \
                    .props('unelevated no-caps').classes('bg-emerald-600')
        dlg.open()

    def _uloz(editor, index: int, dlg):
        try:
            uloz_kapitolu(manual, index, editor.value, user_name)
        except Exception as chyba:
            ui.notify(f'Uložení se nepovedlo: {chyba}', type='negative')
            return
        dlg.close()
        stav['otisk'] += 1
        _otevri(index)
        ui.notify('Úpravy uloženy.', type='positive')

    def _dialog_nova_kapitola():
        soused = kapitoly[stav['index']]
        with kotva_dialogu, ui.dialog() as dlg, \
                ui.card().classes('w-[520px] max-w-[95vw] p-5 gap-3'):
            ui.label('Nová kapitola').classes('text-base font-bold')
            nadpis_i = ui.input('Nadpis kapitoly').props('outlined dense autofocus') \
                .classes('w-full')
            cast_i = ui.input('Část manuálu', value=soused.get('cast') or '') \
                .props('outlined dense').classes('w-full')
            ui.label(f"Vloží se hned za „{soused.get('nadpis') or ''}“.") \
                .classes('text-xs text-gray-400')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                ui.button('Vytvořit', icon='check',
                          on_click=lambda: _vytvor(nadpis_i, cast_i, dlg)) \
                    .props('unelevated no-caps').classes('bg-emerald-600')
        dlg.open()

    def _vytvor(nadpis_i, cast_i, dlg):
        try:
            index = vytvor_kapitolu(manual, stav['index'], nadpis_i.value,
                                    cast_i.value, user_name)
        except Exception as chyba:
            ui.notify(str(chyba), type='negative')
            return
        dlg.close()
        stav['otisk'] += 1
        _otevri(index)
        ui.notify('Kapitola vytvořena — teď ji můžete naplnit.', type='positive')
        _dialog_editace(index)

    def _dialog_verze(_user_name: str):
        verze = seznam_verzi()
        with kotva_dialogu, ui.dialog() as dlg, \
                ui.card().classes('w-[620px] max-w-[95vw] p-5 gap-3'):
            ui.label('Historie verzí').classes('text-base font-bold')
            ui.label(f'Drží se posledních {POCET_VERZI} podob manuálu. '
                     'Obnovením se současný stav také uloží jako verze.') \
                .classes('text-xs text-gray-400')
            if not verze:
                ui.label('Zatím není z čeho obnovovat.').classes('text-sm text-gray-500 py-4')
            for v in verze:
                with ui.row().classes('w-full items-center gap-2 border-b py-2'):
                    ui.icon('history').classes('text-gray-400')
                    with ui.column().classes('gap-0 min-w-0'):
                        ui.label(v['cas']).classes('text-sm font-bold')
                        ui.label(v['popis'] or 'bez popisu').classes('text-xs text-gray-500 truncate')
                    ui.space()
                    ui.button('Obnovit', icon='undo',
                              on_click=lambda _=None, jmeno=v['jmeno']: _obnov(jmeno)) \
                        .props('outline dense no-caps')
            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg.close).props('flat no-caps')
        dlg.open()

    def _obnov(jmeno: str):
        if obnov_verzi(jmeno, user_name) is None:
            ui.notify('Verzi se nepodařilo obnovit.', type='negative')
            return
        ui.notify('Verze obnovena.', type='positive')
        ui.navigate.reload()

    _vykresli_telo()
    _vykresli_panel()


def _prazdno(je_admin: bool, user_name: str):
    with ui.column().classes('w-full items-center justify-center gap-3 py-20'):
        ui.icon('menu_book', size='4rem').classes('text-gray-300')
        ui.label('Zatím tu není žádný manuál.').classes('text-lg text-gray-500')
        if je_admin:
            ui.label('Nahrajte dokument ve formátu DOCX — rozdělí se na kapitoly '
                     'a zpřístupní k online čtení.').classes('text-sm text-gray-400 text-center')
            ui.button('Nahrát manuál (DOCX)', icon='upload_file',
                      on_click=lambda: _dialog_nahrani(user_name)) \
                .props('no-caps').classes('bg-emerald-600')
        else:
            ui.label('Až správce nahraje dokument, objeví se tady.').classes('text-sm text-gray-400')


def _dialog_nahrani(user_name: str):
    with ui.dialog() as dlg, ui.card().classes('w-[560px] max-w-[95vw] p-5 gap-3'):
        with ui.row().classes('w-full items-center gap-2'):
            ui.icon('upload_file', size='1.6rem').classes('text-emerald-600')
            ui.label('Nahrát novou verzi manuálu').classes('text-lg font-bold')
            ui.space()
            ui.button(icon='close', on_click=dlg.close).props('flat dense round')
        ui.label('Soubor .docx. Předchozí verze se nahradí — čtečka se přegeneruje '
                 'včetně obsahu, obrázků a vyhledávání. Podle velikosti to trvá '
                 'několik sekund.').classes('text-sm text-gray-600')
        prubeh = ui.column().classes('w-full gap-1')

        async def _nahrano(e):
            nahravani.set_visibility(False)
            prubeh.clear()
            with prubeh:
                ui.spinner('dots', size='1.6rem').classes('text-emerald-600')
                ui.label('Převádím dokument…').classes('text-sm text-gray-600')
            # NiceGUI 3.x: obsah i jméno sedí na e.file (FileUpload), read() je async.
            data = await e.file.read()
            jmeno = e.file.name
            try:
                manual = await asyncio.get_running_loop().run_in_executor(
                    None, uloz_prevod, data, jmeno, user_name)
            except Exception as chyba:
                prubeh.clear()
                with prubeh:
                    ui.label(f'Převod selhal: {chyba}').classes('text-sm text-red-600')
                nahravani.set_visibility(True)
                nahravani.reset()
                return
            intranet_logger.log_activity(
                user_name, 'Manuály',
                f"Nahrána nová verze manuálu: {jmeno} ({len(manual['kapitoly'])} kapitol)")
            prubeh.clear()
            with prubeh:
                ui.label(f"Hotovo — {len(manual['kapitoly'])} kapitol. "
                         'Čtečka se načte znovu.').classes('text-sm text-emerald-700')
            ui.timer(1.2, lambda: ui.navigate.reload(), once=True)

        nahravani = ui.upload(on_upload=_nahrano, max_files=1, auto_upload=True) \
            .props('accept=".docx" flat bordered').classes('w-full')
    dlg.open()


# ============================================================
# ==            Ruční převod z příkazové řádky              ==
# ============================================================

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Použití: python intranet_manualy.py <soubor.docx>')
        raise SystemExit(1)
    cesta = sys.argv[1]
    with open(cesta, 'rb') as f:
        obsah = f.read()
    vysledek = uloz_prevod(obsah, os.path.basename(cesta))
    print(f"Hotovo: {len(vysledek['kapitoly'])} kapitol → {JSON_SOUBOR}")
