"""
Sdílené UI pomůcky nezávislé na doménových modulech.

Hlavní obsah: `bezpecny_timer` — náhrada `ui.timer` odolná vůči pádu

    RuntimeError: The parent slot of the element has been deleted.

Proč vzniká
-----------
`ui.timer` je sám o sobě UI element. Když se přerenderuje `@ui.refreshable`
blok, ve kterém timer vznikl, je timer element smazán — ale jeho smyčka
v NiceGUI běží dál. Při dalším tiknutí sáhne na `self.parent_slot`, ten na
smazaném elementu vyhodí výjimku, a ta skončí jako traceback v journalu.

Podstatné je, že výjimka padá JEŠTĚ PŘED spuštěním callbacku (NiceGUI vstupuje
do kontextu slotu příkazem `with self._get_context():`). Žádná kontrola uvnitř
callbacku tedy pádu zabránit nemůže — timer musí být uhlídán zvenčí.

Co dělá `bezpecny_timer`
------------------------
* Nevytváří žádný UI element, takže není co smazat → chyba nemůže nastat.
* Před každým tiknutím ověří, že rodičovský element žije. Jakmile je pryč
  (typicky po refreshi), smyčka se sama tiše ukončí.
* Ticky jsou striktně sekvenční — další začne až po dokončení předchozího.
  U async callbacků delších než interval tím odpadá překrývání běhů.
* Callback pouští ve slotu, ve kterém byl timer založen, takže `ui.notify`,
  `.refresh()` i tvorba elementů fungují stejně jako u `ui.timer`.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Callable, Optional

from nicegui import context, app, ui

# Po jak dlouhé nepřetržité nepřítomnosti WebSocket spojení timer vzdáme.
# Krátké výpadky (reconnect, uspaný notebook) tick pouze přeskočí.
_MAX_ODPOJENI_S = 600.0


def _aktualni_slot():
    """Slot, ve kterém právě probíhá vykreslování (napříč verzemi NiceGUI)."""
    try:
        return context.slot          # NiceGUI ≥ 2.0
    except AttributeError:           # pragma: no cover — starší větev
        return context.get_slot()


class BezpecnyTimer:
    """Drop-in náhrada `ui.timer` pro použití uvnitř refreshable bloků."""

    def __init__(
        self,
        interval: float,
        callback: Callable[[], Any],
        *,
        once: bool = False,
        active: bool = True,
        immediate: bool = True,
        popis: str = '',
    ) -> None:
        self.interval = interval
        self.callback = callback
        self.once = once
        self.active = active
        self.immediate = immediate
        self.popis = popis or getattr(callback, '__name__', 'timer')

        slot = _aktualni_slot()
        self._slot = slot
        self._rodic = slot.parent
        self._klient = slot.parent.client
        self._zrusen = False
        self._odpojen_od: Optional[float] = None

        self._task = asyncio.create_task(self._smycka())
        # Bez držení reference by task mohl posbírat GC ještě před doběhnutím.
        _BEZICI_TASKY.add(self._task)
        self._task.add_done_callback(_BEZICI_TASKY.discard)

    # ── veřejné API kompatibilní s ui.timer ──────────────────────────────────

    def cancel(self) -> None:
        self._zrusen = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def activate(self) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    @property
    def bezi(self) -> bool:
        return not self._zrusen and self._task is not None and not self._task.done()

    # ── vnitřek ──────────────────────────────────────────────────────────────

    def _lze_pokracovat(self) -> bool:
        """False → rodič je pryč nebo klient dlouhodobě mimo; smyčka končí."""
        if self._zrusen:
            return False
        try:
            if self._rodic.is_deleted:
                return False
        except Exception:
            return False
        if self._klient.has_socket_connection:
            self._odpojen_od = None
            return True
        # Klient zrovna nemá socket — dáme mu čas na reconnect.
        nyni = time.monotonic()
        if self._odpojen_od is None:
            self._odpojen_od = nyni
        return (nyni - self._odpojen_od) < _MAX_ODPOJENI_S

    async def _tik(self) -> bool:
        """Jedno spuštění callbacku. False → smyčku ukončit."""
        if not self._lze_pokracovat():
            return False
        if not self.active or not self._klient.has_socket_connection:
            return True  # přeskočeno, ale smyčka pokračuje
        try:
            with self._slot:
                vysledek = self.callback()
                if inspect.isawaitable(vysledek):
                    await vysledek
        except asyncio.CancelledError:
            raise
        except RuntimeError as e:
            # Element zmizel v průběhu callbacku — přesně ten případ, kvůli
            # kterému helper existuje. Tiše končíme.
            if 'has been deleted' in str(e):
                return False
            print(f'[bezpecny_timer:{self.popis}] {type(e).__name__}: {e}')
        except Exception as e:
            print(f'[bezpecny_timer:{self.popis}] {type(e).__name__}: {e}')
        return True

    async def _smycka(self) -> None:
        try:
            if self.once:
                await asyncio.sleep(self.interval)
                await self._tik()
                return
            if not self.immediate:
                await asyncio.sleep(self.interval)
            while True:
                zacatek = time.monotonic()
                if not await self._tik():
                    return
                # Interval se počítá od začátku ticku; když callback trval déle,
                # jde se rovnou na další kolo (bez překrývání).
                zbyva = self.interval - (time.monotonic() - zacatek)
                await asyncio.sleep(max(0.0, zbyva))
        except asyncio.CancelledError:
            pass


_BEZICI_TASKY: set[asyncio.Task] = set()


def bezpecny_timer(
    interval: float,
    callback: Callable[[], Any],
    *,
    once: bool = False,
    active: bool = True,
    immediate: bool = True,
    popis: str = '',
) -> BezpecnyTimer:
    """
    Náhrada `ui.timer` pro timery uvnitř `@ui.refreshable` bloků a dialogů.

    Použití je shodné s `ui.timer`:

        bezpecny_timer(3.0, obnov_data)
        bezpecny_timer(0.5, spust_jednou, once=True)

    Vrácený objekt má `cancel()`, `activate()` i `deactivate()`.
    """
    return BezpecnyTimer(
        interval, callback,
        once=once, active=active, immediate=immediate, popis=popis,
    )


# ───────────────────────────── Refreshable na klienta ─────────────────────────
# @ui.refreshable použitý jako dekorátor NA ÚROVNI MODULU vyrobí JEDEN objekt
# už při importu a ten je sdílený všemi připojenými klienty: jeho .targets drží
# sloty všech relací a NiceGUI je při .refresh() nefiltruje podle klienta
# (jediný filtr `target.instance` slouží pro @ui.refreshable_method ve třídách
# a u modulové funkce je vždy None). Jedno kliknutí tedy překreslí i cizí
# prohlížeče — a protože se cizí slot renderuje v request contextvaru
# klikajícího klienta, přečte si app.storage.user CIZÍ data.
#
# Tenhle wrapper drží jeden refreshable objekt na klienta v app.storage.client,
# která zaniká spolu s klientem (žádný leak). Volací místa se nemění: objekt je
# volatelný a .refresh je vázaná metoda, takže ho lze i dál předávat jako
# callback — klienta si zjistí až ve chvíli volání.


class _RefreshableNaKlienta:
    """Per-klientská náhrada @ui.refreshable pro funkce na úrovni modulu."""

    __slots__ = ('_func', '_klic')

    def __init__(self, func: Callable) -> None:
        self._func = func
        self._klic = f'_rf::{func.__module__}.{func.__qualname__}'

    def _rf(self):
        """Refreshable objekt patřící PRÁVĚ TOMUTO klientovi (líně vyrobený).

        Mimo klientský kontext (background task, startup) vrací None — tam není
        co překreslovat a pád by jen shodil úlohu.
        """
        try:
            uloziste = app.storage.client
        except Exception:
            return None
        rf = uloziste.get(self._klic)
        if rf is None:
            rf = ui.refreshable(self._func)
            uloziste[self._klic] = rf
        return rf

    def __call__(self, *args: Any, **kwargs: Any):
        rf = self._rf()
        if rf is None:      # vykreslení bez klienta nedává smysl — ať je to vidět
            raise RuntimeError(f'{self._klic}: volání mimo klientský kontext')
        return rf(*args, **kwargs)

    def refresh(self, *args: Any, **kwargs: Any):
        rf = self._rf()
        if rf is None:
            return None
        return rf.refresh(*args, **kwargs)

    def __getattr__(self, jmeno: str) -> Any:
        """Zbytek API `ui.refreshable` (targets, prune, func, ...) z klientova objektu.

        Volající kód (např. ruční per-klient refresh bez clear()) tak funguje
        beze změny. Mimo klientský kontext není z čeho brát → AttributeError.
        """
        rf = self._rf()
        if rf is None:
            raise AttributeError(f'{self._klic}: {jmeno} mimo klientský kontext')
        return getattr(rf, jmeno)

    def __repr__(self) -> str:
        return f'<refreshable_na_klienta {self._klic}>'


def refreshable_na_klienta(func: Callable) -> _RefreshableNaKlienta:
    """Náhrada @ui.refreshable pro funkce definované na úrovni modulu.

    Bez ní si dva současně přihlášení uživatelé přepisují navzájem pohled,
    protože .refresh() jednoho překreslí sloty všech ostatních.
    """
    return _RefreshableNaKlienta(func)


# ------------------------- Přepínání hlavních tabů -------------------------
# Dřív: `ui.tabs().bind_value(app.storage.user, 'intranet_tab')`. Zdrojem je
# dict (app.storage.user), který není bindable property → NiceGUI ho musel
# pollovat každých 100 ms v `binding._refresh_step` ("binding propagation for
# N active links took ... s"). Tady se místo pollingu zapisuje hodnota rovnou:
# storage + set_value na elementu, obojí v kontextu kliknutí.

def zaregistruj_prepinac_tabu(fn: Callable[[Any], None]) -> None:
    """Uloží per-klient funkci, která umí přepnout hlavní tab intranetu."""
    try:
        app.storage.client['_prepinac_tabu'] = fn
    except Exception:
        pass  # mimo klientský kontext (startup, background task)


def slouc_historii(historie, nazev: str, max_polozek: int = 6):
    """Nejnovější první, bez duplicit, oříznuto na max_polozek. Čistá funkce kvůli testu."""
    if not isinstance(historie, list):
        historie = []
    return [nazev] + [k for k in historie if k != nazev][:max_polozek - 1]


def zapis_naposledy(nazev: str) -> None:
    """Historie modulů pro pás „Naposledy použité“ na nástěnce."""
    try:
        app.storage.user['prehled_naposledy'] = slouc_historii(
            app.storage.user.get('prehled_naposledy', []), nazev)
    except Exception:
        pass  # mimo klientský kontext — historie není kritická


def prepni_tab(nazev: str) -> None:
    """Přepne hlavní tab intranetu — zápis do storage + přímé nastavení elementu."""
    app.storage.user['intranet_tab'] = nazev
    zapis_naposledy(nazev)
    try:
        fn = app.storage.client.get('_prepinac_tabu')
    except Exception:
        fn = None
    if fn is not None:
        fn(nazev)


def prekryv_kolecko(popis: str, procenta: bool = True):
    """Šedý překryv s kolečkem uprostřed. Vrací (dialog, kruh, popisek).
    procenta=False → kolečko se točí (délka operace není známá)."""
    with ui.dialog().props('persistent') as dlg, \
            ui.card().classes('bg-transparent shadow-none items-center gap-2'):
        kruh = ui.circular_progress(value=0, max=100, size='98px', show_value=False) \
            .props('thickness=0.2 color=primary track-color=grey-5')
        if not procenta:
            kruh.props('indeterminate')
        with kruh:
            popisek = ui.label('0 %' if procenta else '') \
                .classes('absolute-center text-lg font-bold')
        ui.label(popis).classes('text-white').style('font-size: 1.25rem')
    return dlg, kruh, popisek


# ------------------------- Kopírování a vkládání v AG Gridu -------------------------
# Jako v Excelu — AG Grid Community nemá výběr rozsahu ani schránku (jen Enterprise).
# Výběr: klik/tažení myší, Shift+klik, Shift(+Ctrl)+šipky, Ctrl+A; Ctrl+C → TSV (Excel
# vloží do buněk; po Ctrl+A i s hlavičkou), bez výběru zaměřená buňka. Zápis přes
# textarea+execCommand — navigator.clipboard na http (intranet bez TLS) nefunguje.
# Gridu stačí třída `KOPIE_CLASS`, `KOPIE_COLDEF` v defaultColDef (sloupec s vlastními
# cellClassRules si pravidlo přidá sám) a `ui.run_javascript(KOPIE_JS)`. Výběr žije na
# `api.__cpSel` (řádky = zobrazený index, tj. po řazení/filtru; sloupce = colId).
# Ctrl+V jen s třídou `KOPIE_VKLADANI_CLASS`: schránku vloží prohlížeč do dočasné textarey
# a grid dostane událost 'vlozeni' {text, radky: id řádků od levého horního rohu výběru
# (bez výběru od zaměřené buňky) dolů, sloupce: colId od něj doprava, vyska + sirka
# výběru} — zapíše ji server: grid.on('vlozeni', …). V editované buňce klávesy nechytá.
KOPIE_CLASS = "cp-kopie"
KOPIE_VKLADANI_CLASS = "cp-vkladani"
KOPIE_COLDEF = {"cellClassRules": {":cp-sel": (
    "(p) => { const s = p.api.__cpSel; return !!s && !p.node.rowPinned && p.rowIndex >= s.r1"
    " && p.rowIndex <= s.r2 && s.cols.includes(p.column.getColId()); }")}}
KOPIE_JS = r"""
(() => {
  if (window.__cpKopie) return;
  window.__cpKopie = true;
  const st = document.createElement('style');
  st.textContent = '.cp-kopie .ag-cell.cp-sel { background-color: rgba(37,99,235,.16); }';
  document.head.appendChild(st);
  const apiGridu = (el) => {
    const root = el && el.closest ? el.closest('.cp-kopie') : null;
    const comp = root && getElement(root);
    return (comp && comp.api) || null;
  };
  const sloupce = (api) => api.getAllDisplayedColumns();
  // Buňka v DOM → {r: zobrazený index řádku, c: pořadí zobrazeného sloupce}
  const pozice = (api, cell) => {
    const row = cell.closest('.ag-row');
    const r = row ? parseInt(row.getAttribute('row-index'), 10) : NaN;
    const c = sloupce(api).findIndex((k) => k.getColId() === cell.getAttribute('col-id'));
    return Number.isNaN(r) || c < 0 ? null : {r, c};
  };
  const nastav = (api, a, h, vse) => {   // a = kotva, h = druhý roh; a=null výběr zruší
    api.__cpSel = a && {a, h, vse: !!vse, r1: Math.min(a.r, h.r), r2: Math.max(a.r, h.r),
      cols: sloupce(api).slice(Math.min(a.c, h.c), Math.max(a.c, h.c) + 1).map((k) => k.getColId())};
    api.refreshCells();   // cellClassRules přepočítá i bez force; force by překreslil i ikony pod myší
  };
  const kopiruj = (api, cell) => {
    let s = api.__cpSel;
    if (!s) {
      const p = pozice(api, cell);
      if (!p) return;
      s = {r1: p.r, r2: p.r, cols: [sloupce(api)[p.c].getColId()]};
    }
    const txt = (v) => String(v == null ? '' : v).replace(/\s*[\t\r\n]+\s*/g, ' ').trim();
    const radky = s.vse ? [s.cols.map((id) =>
      txt(api.getDisplayNameForColumn(api.getColumn(id), 'header'))).join('\t')] : [];
    for (let r = s.r1; r <= s.r2; r++) {
      const node = api.getDisplayedRowAtIndex(r);
      if (node) radky.push(s.cols.map((id) =>
        txt(api.getCellValue({rowNode: node, colKey: id, useFormatter: true}))).join('\t'));
    }
    if (!radky.length) return;
    const data = radky.join('\r\n');
    let ok = false;
    const onCopy = (e) => { e.clipboardData.setData('text/plain', data); e.preventDefault(); ok = true; };
    const ta = document.createElement('textarea');
    ta.value = data;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    document.addEventListener('copy', onCopy, true);
    try { document.execCommand('copy'); } catch (e) {}
    document.removeEventListener('copy', onCopy, true);
    ta.remove();
    cell.focus({preventScroll: true});   // šipky dál chodí po gridu
    if (!ok && navigator.clipboard) { navigator.clipboard.writeText(data); ok = true; }
    const jedna = radky.length === 1 && s.cols.length === 1;
    Quasar.Notify.create({position: 'bottom', timeout: 1500, message: !ok
      ? 'Kopírování selhalo.'
      : 'Zkopírováno: ' + (jedna ? (data.length > 40 ? data.slice(0, 40) + '…' : data)
                                 : radky.length + ' ř. × ' + s.cols.length + ' sl.')});
  };
  // Ctrl+V: prohlížeč vloží schránku do dočasné textarey (číst navigator.clipboard na http
  // nejde); text a cílové řádky/sloupce dostane server jako událost 'vlozeni'.
  const vloz = (api, cell) => {
    const s = api.__cpSel;
    const p = s ? {r: s.r1, c: sloupce(api).findIndex((k) => k.getColId() === s.cols[0])} : pozice(api, cell);
    if (!p || p.c < 0) return;
    const ta = document.createElement('textarea');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
    document.body.appendChild(ta);
    let hotovo = false;
    const konec = (text) => {
      if (hotovo) return;
      hotovo = true;
      ta.remove();
      cell.focus({preventScroll: true});
      if (!text) return;
      if (text.length > 100000) {
        Quasar.Notify.create({type: 'warning', message: 'Ve schránce je toho na vložení moc.'});
        return;
      }
      const radky = [];
      for (let r = p.r; r < api.getDisplayedRowCount(); r++) radky.push(api.getDisplayedRowAtIndex(r).id);
      getElement(cell.closest('.cp-kopie')).$emit('vlozeni', {text, radky,
        sloupce: sloupce(api).slice(p.c).map((k) => k.getColId()),
        vyska: s ? s.r2 - s.r1 + 1 : 1, sirka: s ? s.cols.length : 1});
    };
    ta.addEventListener('paste', (e) => { e.preventDefault(); konec(e.clipboardData.getData('text/plain')); });
    ta.focus();
    setTimeout(() => konec(ta.value), 500);   // pojistka, kdyby událost paste nepřišla
  };
  let tah = null;   // grid, ve kterém se právě táhne myší
  document.addEventListener('mousedown', (ev) => {
    tah = null;
    const cell = ev.button === 0 && ev.target.closest   // v editované buňce patří myš inputu
      ? ev.target.closest('.cp-kopie .ag-cell:not(.ag-cell-inline-editing)') : null;
    const api = cell && apiGridu(cell);
    const p = api && pozice(api, cell);
    if (!p) return;
    let a = p;
    if (ev.shiftKey) {   // Shift+klik: od kotvy výběru, jinak od dosud zaměřené buňky
      const s = api.__cpSel, f = api.getFocusedCell();
      const fc = f && f.column ? sloupce(api).indexOf(f.column) : -1;
      a = s ? s.a : (fc >= 0 ? {r: f.rowIndex, c: fc} : p);
    }
    nastav(api, a, p);
    tah = api;
  }, true);
  document.addEventListener('mouseover', (ev) => {
    if (!tah) return;
    if (!(ev.buttons & 1)) { tah = null; return; }
    const cell = ev.target.closest ? ev.target.closest('.cp-kopie .ag-cell') : null;
    const s = tah.__cpSel, p = cell && apiGridu(cell) === tah && pozice(tah, cell);
    if (s && p && (p.r !== s.h.r || p.c !== s.h.c)) nastav(tah, s.a, p);
  });
  document.addEventListener('mouseup', () => { tah = null; });
  const SIPKY = {arrowup: [-1, 0], arrowdown: [1, 0], arrowleft: [0, -1], arrowright: [0, 1]};
  document.addEventListener('keydown', (ev) => {
    const ae = document.activeElement;
    // při editaci buňky (fokus v jejím inputu) patří Ctrl+C/V/A a Shift+šipky inputu
    const cell = ae && ae.closest ? ae.closest('.cp-kopie .ag-cell:not(.ag-cell-inline-editing)') : null;
    const api = cell && apiGridu(cell);
    if (!api || ev.altKey) return;
    const ctrl = ev.ctrlKey || ev.metaKey, k = (ev.key || '').toLowerCase(), sipka = SIPKY[k];
    const stop = () => { ev.preventDefault(); ev.stopPropagation(); };
    if (ctrl && !ev.shiftKey && (ev.code === 'KeyC' || k === 'c')) {
      stop(); kopiruj(api, cell);
    } else if (ctrl && !ev.shiftKey && (ev.code === 'KeyV' || k === 'v')) {
      if (!cell.closest('.cp-vkladani')) return;
      ev.stopPropagation();   // bez preventDefault: výchozí akce vloží schránku do textarey z vloz()
      vloz(api, cell);
    } else if (ctrl && !ev.shiftKey && (ev.code === 'KeyA' || k === 'a')) {
      stop();
      const n = api.getDisplayedRowCount();
      if (n) nastav(api, {r: 0, c: 0}, {r: n - 1, c: sloupce(api).length - 1}, true);
    } else if (sipka && ev.shiftKey) {   // Shift+šipka rozšíří výběr, s Ctrl až na kraj
      stop();
      const s = api.__cpSel, p = s ? s.h : pozice(api, cell);
      if (!p) return;
      const kam = (x, d, max) => Math.min(max, Math.max(0, !d ? x : ctrl ? (d < 0 ? 0 : max) : x + d));
      const h = {r: kam(p.r, sipka[0], api.getDisplayedRowCount() - 1),
                 c: kam(p.c, sipka[1], sloupce(api).length - 1)};
      nastav(api, s ? s.a : p, h);
      api.ensureIndexVisible(h.r);
      api.ensureColumnVisible(sloupce(api)[h.c]);
    } else if (api.__cpSel && (sipka || ['tab', 'pageup', 'pagedown', 'home', 'end', 'escape'].includes(k))) {
      nastav(api, null);   // běžný posun zaměřené buňky výběr zruší (jako v Excelu)
    }
  }, true);
})();
"""
