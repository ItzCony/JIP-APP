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
        """Refreshable objekt patřící PRÁVĚ TOMUTO klientovi (líně vyrobený)."""
        rf = app.storage.client.get(self._klic)
        if rf is None:
            rf = ui.refreshable(self._func)
            app.storage.client[self._klic] = rf
        return rf

    def __call__(self, *args: Any, **kwargs: Any):
        return self._rf()(*args, **kwargs)

    def refresh(self, *args: Any, **kwargs: Any):
        return self._rf().refresh(*args, **kwargs)

    def __repr__(self) -> str:
        return f'<refreshable_na_klienta {self._klic}>'


def refreshable_na_klienta(func: Callable) -> _RefreshableNaKlienta:
    """Náhrada @ui.refreshable pro funkce definované na úrovni modulu.

    Bez ní si dva současně přihlášení uživatelé přepisují navzájem pohled,
    protože .refresh() jednoho překreslí sloty všech ostatních.
    """
    return _RefreshableNaKlienta(func)
