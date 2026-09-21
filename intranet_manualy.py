"""Manuály — online čtečka firemních manuálů nahraných jako DOCX.

DOCX se NEpřevádí při každém zobrazení. Jednorázově se rozloží na kapitoly
(kapitola = nadpis úrovně 2) a uloží se jako JSON vedle vytažených obrázků:

    Manualy_Data/manual.json      kapitoly: nadpis, HTML, čistý text pro hledání
    Manualy_Data/obrazky/         obrázky pojmenované otiskem obsahu
    Manualy_Data/<původní>.docx   originál ke stažení

Čtečka pak jen bere hotové HTML z RAM cache (klíč = mtime JSONu), takže
listování i hledání jsou okamžité a nezatěžují DB — modul žádnou tabulku nemá.

Hledání je bez diakritiky a bez ohledu na velikost písmen („zasoba" najde
„Zásoby"). Nalezené výrazy se zvýrazňují až v zobrazeném HTML, a to mimo
značky, aby se atributy `src`/`class` nerozbily.

Práva: manualy_ctenar (čte), manualy_admin (nahrává novou verzi).
"""

from nicegui import ui, app
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
import unicodedata

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

NAZEV = 'Manuály'
MANUALY_DIR = 'Manualy_Data'
OBRAZKY_DIR = os.path.join(MANUALY_DIR, 'obrazky')
JSON_SOUBOR = os.path.join(MANUALY_DIR, 'manual.json')
URL_OBRAZKY = '/manualy_prilohy'

os.makedirs(OBRAZKY_DIR, exist_ok=True)
# Obrázky i originál DOCX jen pro přihlášené (stejně jako přílohy ASM).
intranet_static.chranene_soubory(URL_OBRAZKY, MANUALY_DIR)

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
_STYL_NADPIS = re.compile(r'^Heading\s*(\d+)$', re.I)

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

    def __init__(self, doc: Document):
        self.doc = doc
        self.part = doc.part
        self.cislovani = self._nacti_cislovani()
        self.obrazky_ulozeny = 0

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
                zvyrazneni = rpr.find(qn('w:highlight'))
                if zvyrazneni is not None and zvyrazneni.get(qn('w:val')) not in (None, 'none'):
                    text = f'<span class="m-zvyr">{text}</span>'
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

    # -------- tabulky --------
    def _tabulka_html(self, t: Table) -> str:
        radky = []
        for i, radek in enumerate(t.rows):
            bunky = []
            for bunka in radek.cells:
                vnitrek = ''.join(f'<p>{self._vnitrek_odstavce(p)}</p>'
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
    prevodnik = _Prevodnik(doc)

    kapitoly = []
    aktualni = None
    otevreny_seznam = []   # zásobník (úroveň, značka) kvůli zanoření
    poradi_kotev = 0

    def nova_kapitola(nadpis: str, uroven: int):
        nonlocal poradi_kotev
        poradi_kotev += 1
        return {'id': _slug(nadpis, poradi_kotev), 'nadpis': nadpis, 'uroven': uroven,
                'cast': '', 'kotvy': [], 'html': [], 'text': []}

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
        try:
            styl = p.style.name or ''
        except Exception:
            styl = ''
        if _STYL_TOC.match(styl):
            continue

        vnitrek = prevodnik._vnitrek_odstavce(p)
        if not vnitrek:
            continue

        shoda = _STYL_NADPIS.match(styl)
        uroven = int(shoda.group(1)) if shoda else (1 if styl == 'Title' else 0)
        cisty = re.sub(r'<[^>]+>', '', vnitrek).strip()

        if uroven and cisty:
            zavri_seznamy()
            if uroven <= UROVEN_KAPITOLY:
                aktualni = nova_kapitola(cisty, uroven)
                kapitoly.append(aktualni)
                continue
            # Podnadpis uvnitř kapitoly → kotva do bočního panelu.
            if aktualni is None:
                aktualni = nova_kapitola(cisty, uroven)
                kapitoly.append(aktualni)
                continue
            poradi_kotev += 1
            kotva = _slug(cisty, poradi_kotev)
            aktualni['kotvy'].append({'id': kotva, 'nadpis': cisty, 'uroven': uroven})
            stupen = min(uroven, 6)
            aktualni['html'].append(f'<h{stupen} id="{kotva}" class="m-nadpis">{vnitrek}</h{stupen}>')
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
            aktualni['html'].append(f'<li>{vnitrek}</li>')
        else:
            zavri_seznamy()
            aktualni['html'].append(f'<p>{vnitrek}</p>')
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
            'kotvy': k['kotvy'], 'html': ''.join(k['html']),
            'text': text, 'hledej': _odstran_diakritiku(text + ' ' + k['nadpis']).lower(),
        })

    return {
        'nazev': os.path.splitext(jmeno_souboru)[0],
        'soubor': jmeno_souboru,
        'nahrano': datetime.datetime.now().strftime('%d.%m.%Y %H:%M'),
        'kapitoly': hotove,
    }


def uloz_prevod(data: bytes, jmeno_souboru: str) -> dict:
    """Převede DOCX, uloží JSON i originál ke stažení a vyprázdní cache."""
    manual = preved_docx(data, jmeno_souboru)
    bezpecne_jmeno = re.sub(r'[^\w.\- ]', '_', jmeno_souboru) or 'manual.docx'
    with open(os.path.join(MANUALY_DIR, bezpecne_jmeno), 'wb') as f:
        f.write(data)
    manual['soubor'] = bezpecne_jmeno
    docasny = JSON_SOUBOR + '.tmp'
    with open(docasny, 'w', encoding='utf-8') as f:
        json.dump(manual, f, ensure_ascii=False)
    os.replace(docasny, JSON_SOUBOR)
    _CACHE.clear()
    return manual


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
    return _CACHE.get('data')


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


def vloz_css() -> None:
    """CSS modulu do hlavičky stránky — MUSÍ se volat při stavbě stránky.

    Tab Manuály se renderuje lazy až po připojení klienta; tam `ui.add_css`
    na starším NiceGUI (ostrý provoz běží 3.8) styl zahodí a obsah se vykreslí
    bez formátování. Proto hlavičku plní rozcestník v intranet.py.
    """
    ui.add_head_html(f'<style>{_CSS}</style>')


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

_CSS = '''
.manual-obsah { font-size: 15px; line-height: 1.65; color: #1f2937; }
.manual-obsah p { margin: 0 0 .6rem 0; }
.manual-obsah ul, .manual-obsah ol { margin: 0 0 .7rem 1.4rem; padding-left: .6rem; }
.manual-obsah ul { list-style: disc; }
.manual-obsah ol { list-style: decimal; }
.manual-obsah li { margin: .15rem 0; }
.manual-obsah h3, .manual-obsah h4, .manual-obsah h5, .manual-obsah h6 {
  font-weight: 700; color: #065f46; margin: 1.5rem 0 .5rem 0; scroll-margin-top: 1rem;
  border-bottom: 1px solid #d1fae5; padding-bottom: .25rem; }
.manual-obsah h3 { font-size: 1.28rem; }
.manual-obsah h4 { font-size: 1.12rem; color: #0f766e; }
.manual-obsah h5, .manual-obsah h6 { font-size: 1rem; color: #334155; border-bottom: none; }
.manual-obsah img { display: block; margin: .8rem 0; border: 1px solid #e5e7eb;
  border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.07); max-width: 100%; height: auto; }
.manual-obsah a { color: #1d4ed8; text-decoration: underline; }
.manual-obsah .m-cesta { font-family: ui-monospace, monospace; font-size: .86em;
  background: #f1f5f9; padding: .05rem .3rem; border-radius: 4px; word-break: break-all; }
.manual-obsah .m-zvyr { background: #fef08a; padding: 0 .15rem; }
.manual-obsah .m-tbl-obal { overflow-x: auto; margin: .8rem 0; }
.manual-obsah table.m-tbl { border-collapse: collapse; font-size: 13.5px; min-width: 60%; }
.manual-obsah table.m-tbl th, .manual-obsah table.m-tbl td {
  border: 1px solid #cbd5e1; padding: .35rem .55rem; vertical-align: top; }
.manual-obsah table.m-tbl th { background: #ecfdf5; font-weight: 700; text-align: left; }
.manual-obsah table.m-tbl tbody tr:nth-child(even) { background: #f8fafc; }
.manual-obsah table.m-tbl p { margin: 0; }
.manual-obsah mark.m-nalez { background: #fde047; color: inherit; border-radius: 3px; padding: 0 .1rem; }
.manual-obsah mark.m-nalez.m-aktivni { background: #fb923c; color: #fff; }
.manual-toc-polozka { cursor: pointer; border-left: 3px solid transparent; }
.manual-toc-polozka:hover { background: #f1f5f9; }
.manual-toc-aktivni { background: #ecfdf5 !important; border-left-color: #059669 !important; }
'''


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
    stav = {'index': 0, 'dotaz': '', 'vysledky': [], 'rezim': 'obsah'}
    # Handlery v panelu mažou vlastní element (panel.clear()) → ui.run_javascript
    # by pak hledalo klienta přes slot smazaného rodiče. Držíme klienta natvrdo.
    klient = ui.context.client

    # ---------- hlavička ----------
    with ui.row().classes('w-full items-center gap-3 px-4 pt-3 pb-2 flex-nowrap'):
        ui.icon('menu_book', size='2rem').classes('text-emerald-600 shrink-0')
        with ui.column().classes('gap-0 min-w-0 flex-1'):
            ui.label(manual.get('nazev') or NAZEV).classes(
                'text-2xl font-bold text-gray-800 leading-tight truncate')
            ui.label(f"{len(kapitoly)} kapitol · aktualizováno {manual.get('nahrano', '—')}").classes(
                'text-xs text-gray-500')
        if manual.get('soubor'):
            ui.button('Originál', icon='download',
                      on_click=lambda: ui.download(f"{URL_OBRAZKY}/{manual['soubor']}")) \
                .props('flat dense no-caps').classes('text-gray-600').tooltip('Stáhnout zdrojový DOCX')
        if je_admin:
            ui.button('Nová verze', icon='upload_file',
                      on_click=lambda: _dialog_nahrani(user_name)) \
                .props('outline dense no-caps').classes('text-emerald-700')

    with ui.row().classes('w-full flex-nowrap gap-0 px-2 pb-2 items-stretch'):
        # ---------- levý panel: hledání + obsah ----------
        with ui.column().classes('w-[320px] shrink-0 gap-2 pr-2'):
            hledani = ui.input(placeholder='Hledat v manuálu…') \
                .props('outlined dense clearable autocomplete=off') \
                .classes('w-full')
            with hledani.add_slot('prepend'):
                ui.icon('search').classes('text-gray-400')
            pocitadlo = ui.label('').classes('text-xs text-gray-500 px-1 min-h-[1rem]')

            panel = ui.column().classes(
                'w-full gap-0 overflow-y-auto border border-gray-200 rounded bg-white') \
                .style('height: calc(100vh - 230px);')

        # ---------- pravý panel: text kapitoly ----------
        with ui.column().classes('flex-1 min-w-0 gap-0'):
            with ui.card().classes('w-full flex-1 p-0 shadow-sm border border-gray-200 gap-0'):
                with ui.row().classes(
                        'w-full items-center gap-2 px-4 py-2 border-b border-gray-200 '
                        'bg-gray-50 flex-nowrap sticky top-0 z-10'):
                    tlac_zpet = ui.button(icon='chevron_left', on_click=lambda: _skoc(-1)) \
                        .props('flat dense round').tooltip('Předchozí kapitola')
                    tlac_vpred = ui.button(icon='chevron_right', on_click=lambda: _skoc(1)) \
                        .props('flat dense round').tooltip('Další kapitola')
                    with ui.column().classes('gap-0 min-w-0 flex-1'):
                        popis_cast = ui.label('').classes(
                            'text-[11px] uppercase tracking-wider text-emerald-700 font-semibold truncate')
                        popis_nadpis = ui.label('').classes(
                            'text-base font-bold text-gray-800 leading-tight truncate')
                    popis_strana = ui.label('').classes('text-xs text-gray-500 shrink-0 pl-2')

                telo = ui.column().classes('w-full overflow-y-auto px-6 py-4 gap-0') \
                    .style('height: calc(100vh - 292px);')

                with ui.row().classes('w-full items-center justify-between gap-2 px-4 py-2 '
                                      'border-t border-gray-200 bg-gray-50 flex-nowrap'):
                    tlac_zpet2 = ui.button('Předchozí', icon='arrow_back',
                                           on_click=lambda: _skoc(-1)) \
                        .props('flat dense no-caps').classes('text-gray-700')
                    nahoru = ui.button('Nahoru', icon='vertical_align_top',
                                       on_click=lambda: klient.run_javascript(
                                           f'getElement({telo.id}).scrollTo({{top:0,behavior:"smooth"}})')) \
                        .props('flat dense no-caps').classes('text-gray-500')
                    # icon-right MUSÍ nést název ikony: holý flag => Quasar pošle do
                    # QIcon name=true a Vue spadne na `u.match is not a function`.
                    tlac_vpred2 = ui.button('Další', on_click=lambda: _skoc(1)) \
                        .props('flat dense no-caps icon-right=arrow_forward').classes('text-gray-700')

    # ---------- vykreslování ----------
    def _vykresli_telo():
        k = kapitoly[stav['index']]
        telo.clear()
        with telo:
            ui.html(f'<h2 class="text-2xl font-bold text-emerald-800 mb-3">'
                    f'{_html.escape(k["nadpis"])}</h2>')
            obsah = zvyrazni(k['html'], stav['dotaz']) if stav['dotaz'] else k['html']
            ui.html(f'<div class="manual-obsah">{obsah}</div>').classes('w-full')
        popis_cast.set_text(k.get('cast') or manual.get('nazev') or '')
        popis_nadpis.set_text(k['nadpis'])
        popis_strana.set_text(f"{stav['index'] + 1} / {len(kapitoly)}")
        for t in (tlac_zpet, tlac_zpet2):
            t.set_enabled(stav['index'] > 0)
        for t in (tlac_vpred, tlac_vpred2):
            t.set_enabled(stav['index'] < len(kapitoly) - 1)
        klient.run_javascript(f'getElement({telo.id}).scrollTo({{top:0}})')

    def _otevri(index: int, kotva: str = ''):
        stav['index'] = max(0, min(index, len(kapitoly) - 1))
        _vykresli_telo()
        _vykresli_panel()
        if kotva:
            klient.run_javascript(
                f'setTimeout(()=>document.getElementById("{kotva}")'
                f'?.scrollIntoView({{behavior:"smooth",block:"start"}}), 120)')
        elif stav['dotaz']:
            klient.run_javascript(
                f'setTimeout(()=>getElement({telo.id}).querySelector("mark.m-nalez")'
                f'?.scrollIntoView({{behavior:"smooth",block:"center"}}), 120)')

    def _skoc(o: int):
        _otevri(stav['index'] + o)

    def _vykresli_panel():
        panel.clear()
        with panel:
            if stav['rezim'] == 'hledani':
                _panel_vysledky()
            else:
                _panel_obsah()

    def _panel_obsah():
        posledni_cast = None
        for i, k in enumerate(kapitoly):
            cast = k.get('cast') or ''
            if cast != posledni_cast:
                posledni_cast = cast
                if cast:
                    ui.label(cast).classes(
                        'w-full px-3 pt-3 pb-1 text-[11px] font-bold uppercase '
                        'tracking-wider text-gray-500 bg-gray-50 sticky top-0 z-10')
            aktivni = i == stav['index']
            trida = ('manual-toc-polozka w-full px-3 py-1.5 text-sm text-gray-700 '
                     'border-b border-gray-100')
            if aktivni:
                trida += ' manual-toc-aktivni font-semibold text-emerald-800'
            radek = ui.label(f"{i + 1}. {k['nadpis']}").classes(trida)
            radek.on('click', lambda _, idx=i: _otevri(idx))
            if aktivni and k.get('kotvy'):
                with ui.column().classes('w-full gap-0 bg-emerald-50/40 pb-1'):
                    for kotva in k['kotvy']:
                        odsazeni = 4 + (kotva['uroven'] - 3) * 3
                        pod = ui.label('› ' + kotva['nadpis']).classes(
                            f'manual-toc-polozka w-full pl-{min(odsazeni, 12)} pr-2 py-1 '
                            'text-xs text-gray-600')
                        pod.on('click', lambda _, kid=kotva['id'], idx=i: _otevri(idx, kid))

    def _panel_vysledky():
        if not stav['vysledky']:
            with ui.column().classes('w-full items-center py-8 gap-2'):
                ui.icon('search_off', size='2rem').classes('text-gray-300')
                ui.label('Nic nenalezeno').classes('text-sm text-gray-500')
            return
        for v in stav['vysledky']:
            aktivni = v['index'] == stav['index']
            trida = ('manual-toc-polozka w-full px-3 py-2 gap-0 border-b border-gray-100')
            if aktivni:
                trida += ' manual-toc-aktivni'
            with ui.column().classes(trida) as polozka:
                with ui.row().classes('w-full items-center gap-2 flex-nowrap'):
                    ui.label(v['nadpis']).classes(
                        'text-sm font-semibold text-gray-800 flex-1 min-w-0 truncate')
                    ui.label(str(v['pocet'])).classes(
                        'text-[10px] font-bold text-white bg-emerald-600 '
                        'rounded-full px-1.5 py-0.5 shrink-0')
                if v['utrzek']:
                    ui.label(v['utrzek']).classes('text-xs text-gray-500 leading-snug line-clamp-2')
            polozka.on('click', lambda _, idx=v['index']: _otevri(idx))

    # ---------- hledání ----------
    def _hledej(_=None):
        dotaz = (hledani.value or '').strip()
        stav['dotaz'] = dotaz
        if len(dotaz) < 2:
            stav['rezim'] = 'obsah'
            stav['vysledky'] = []
            pocitadlo.set_text('')
            _vykresli_telo()
            _vykresli_panel()
            return
        stav['vysledky'] = hledej(manual, dotaz)
        stav['rezim'] = 'hledani'
        celkem = sum(v['pocet'] for v in stav['vysledky'])
        pocitadlo.set_text(
            f"{celkem}× ve {len(stav['vysledky'])} kapitolách" if celkem else 'Žádná shoda')
        if stav['vysledky']:
            _otevri(stav['vysledky'][0]['index'])
        else:
            _vykresli_telo()
            _vykresli_panel()

    # Debounce: hledá se až když uživatel na chvíli přestane psát.
    casovac = {'t': None}

    def _odlozene_hledani(_=None):
        if casovac['t']:
            casovac['t'].cancel()
        casovac['t'] = ui.timer(0.35, _hledej, once=True)

    hledani.on_value_change(_odlozene_hledani)
    hledani.on('keydown.enter', _hledej)

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
                    None, uloz_prevod, data, jmeno)
            except Exception as chyba:
                prubeh.clear()
                with prubeh:
                    ui.label(f'Převod selhal: {chyba}').classes('text-sm text-red-600')
                nahravani.set_visibility(True)
                nahravani.reset()
                return
            intranet_logger.log_activity(
                user_name, 'Manuály – nahrání',
                f"{jmeno} ({len(manual['kapitoly'])} kapitol)")
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
