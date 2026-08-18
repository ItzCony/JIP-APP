"""
intranet_jobs.py — centrální dispečer výpočetních úloh.

Cíl:
  • io(fn, ...)  → blokující I/O (DB, soubory, SMTP) běží ve VLÁKNĚ → event-loop
                   zůstává volná a ostatní uživatelé nezamrznou, když někdo
                   nahrává / exportuje / tiskne.
  • cpu(fn, ...) → CPU-náročný výpočet (Excel/PDF/obrázky) běží v ODDĚLENÉM
                   PROCESU → využije další jádro a obejde GIL. Pokud proces-pool
                   není k dispozici NEBO funkce není picklovatelná (typicky
                   closure), spadne to BEZPEČNĚ zpět do (brzděného) vlákna, takže
                   úloha vždy doběhne stejně jako dřív.

Souběh těžkých úloh hlídá semafor (_SEM) — chrání RAM i DB connection-pool tím,
že najednou běží nejvýš _MAX_CPU těžkých úloh; zbytek se slušně zařadí do fronty.

──────────────────────────────────────────────────────────────────────────────
DŮLEŽITÉ (Windows / spawn):
  • Funkce i argumenty předané do cpu() musí být PICKLOVATELNÉ — tj. funkce na
    úrovni modulu (ne lokální closure) a běžná data (dict, list, str, bytes,
    cesty). Nelze předat DB spojení, otevřené soubory ani NiceGUI elementy.
  • Funkce běžící v procesu NEMÁ přístup k DB poolu hlavního procesu. Vzor je
    proto „data → stavba → doručení“: data načti přes io() v hlavním procesu a
    do cpu() pošli už jen čistá data; výstupem je cesta k souboru / bytes.
  • Worker-procesy re-importují web_main, proto MUSÍ být spouštěč serveru
    (ui.run) pod `if __name__ == "__main__":` (NE `__mp_main__`), jinak by se
    každý worker pokusil nastartovat vlastní server. To je ošetřeno ve web_main.
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import functools
import multiprocessing
import os
import pickle
import signal
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool


def _worker_init() -> None:
    """Inicializace worker-procesu poolu: IGNORUJ SIGINT (Ctrl+C).

    Bez toho na Windows i Linuxu dostane Ctrl+C celá skupina procesů a nečinné
    workery čekající ve frontě (`call_queue.get`) vypíšou matoucí
    `KeyboardInterrupt` traceback. Ctrl+C tak řeší jen hlavní proces; workery
    se korektně ukončí přes sentinel, který jim pool pošle při shutdownu."""
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError, AttributeError):
        pass


def _mp_context():
    """Bezpečná start-metoda procesů podle OS:
      • Windows  → 'spawn'      (jediná dostupná; čistý nový proces)
      • Linux/mac→ 'forkserver' (čistý fork z minimálního serveru)

    Holému 'fork' se VYHÝBÁME: produkce běží na Linuxu a tam by fork z procesu
    s běžící asyncio smyčkou, vlákny (jobs.io) a DB connection-poolem mohl
    zdědit zamčené zámky → zatuhlý worker. 'forkserver' i 'spawn' startují
    workery čistě, proto je nutný guard `__main__` ve web_main (workery
    re-importují hlavní modul). 'forkserver' importuje moduly jen jednou
    (v server-procesu), takže je na Linuxu levnější než 'spawn'.
    """
    try:
        if sys.platform.startswith("win"):
            return multiprocessing.get_context("spawn")
        return multiprocessing.get_context("forkserver")
    except (ValueError, OSError):
        return multiprocessing.get_context("spawn")


# ── Kolik jader vyhradit na těžké CPU úlohy ─────────────────────────────────
# Worker je „drahý“ (načte pandas/openpyxl + moduly + ~150–250 MB RAM), proto
# konzervativně. Lze přepsat proměnnou prostředí JIPKA_CPU_WORKERS.
def _vychozi_pocet_workeru() -> int:
    try:
        env = int(os.environ.get("JIPKA_CPU_WORKERS", "0"))
        if env > 0:
            return env
    except (ValueError, TypeError):
        pass
    jader = os.cpu_count() or 2
    return max(1, min(3, jader - 1))


_MAX_CPU: int = _vychozi_pocet_workeru()

CPU_POOL: ProcessPoolExecutor | None = None
_SEM: asyncio.Semaphore | None = None      # brzda souběhu těžkých úloh
_BEZICI: int = 0                           # počet právě běžících těžkých úloh
_FALLBACKU: int = 0                        # kolikrát se cpu() muselo vrátit k vláknu


def _je_worker() -> bool:
    """True, běžíme-li uvnitř child-procesu poolu — tam pool NEzakládáme."""
    return multiprocessing.parent_process() is not None


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(_MAX_CPU)
    return _SEM


# ── Životní cyklus poolu (volat z app.on_startup / on_shutdown) ─────────────
def init_pool() -> None:
    """Založí proces-pool. Bezpečné volat opakovaně; v child-procesu nedělá nic."""
    global CPU_POOL
    if _je_worker():
        return
    _sem()  # inicializuj semafor v aktuální event-loopě
    if CPU_POOL is None:
        try:
            ctx = _mp_context()
            CPU_POOL = ProcessPoolExecutor(
                max_workers=_MAX_CPU, mp_context=ctx, initializer=_worker_init,
            )
            print(f"[jobs] Proces-pool spuštěn: {_MAX_CPU} worker(ů) "
                  f"(start={ctx.get_start_method()}, jader: {os.cpu_count()}).")
        except Exception as e:
            CPU_POOL = None
            print(f"[jobs] Proces-pool se nepodařilo spustit ({e}); "
                  f"těžké úlohy poběží přes vlákna.")


def shutdown_pool() -> None:
    """Korektně ukončí proces-pool. Volat z app.on_shutdown."""
    global CPU_POOL
    if CPU_POOL is not None:
        try:
            CPU_POOL.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        CPU_POOL = None


async def warmup() -> None:
    """
    Předehřeje pool — spawne workery a re-importuje moduly na pozadí, aby první
    reálný export/tisk nečekal na start procesu. Bezpečné volat po startu.
    """
    if _je_worker():
        return
    try:
        if CPU_POOL is None:
            init_pool()
        if CPU_POOL is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(CPU_POOL, _noop)
            print("[jobs] Proces-pool předehřát.")
    except Exception as e:
        print(f"[jobs] Předehřátí poolu přeskočeno ({e}).")


def _noop() -> bool:
    """Triviální úloha pro předehřátí workeru."""
    return True


# ── Veřejné API ─────────────────────────────────────────────────────────────
async def io(fn, *args, brzda: bool = False, **kwargs):
    """
    Blokující I/O (DB dotaz, čtení/zápis souboru, SMTP, ZIP) → spustí ve VLÁKNĚ,
    takže event-loop zůstává volná pro ostatní uživatele.

    brzda=True → navíc se započítá do semaforu souběhu (pro velké/dlouhé úlohy,
    např. hromadné exporty, ať jich neběží příliš najednou).
    """
    target = functools.partial(fn, **kwargs) if kwargs else fn
    if not brzda:
        return await asyncio.to_thread(target, *args)

    global _BEZICI
    async with _sem():
        _BEZICI += 1
        try:
            return await asyncio.to_thread(target, *args)
        finally:
            _BEZICI -= 1


async def cpu(fn, *args):
    """
    CPU-náročný výpočet → ODDĚLENÝ PROCES (jiné jádro, obejde GIL). Vždy brzděno
    semaforem. fn i args musí být picklovatelné.

    Bezpečnostní síť: když pool není k dispozici, funkce není picklovatelná
    (closure) nebo worker spadne, úloha se DOJEDE VE VLÁKNĚ. Díky tomu je migrace
    bezriziková — nejhůř to běží jako dřív (ve vlákně), nikdy to nespadne kvůli
    pickle/procesu. Funkce by proto měly být idempotentní (zápis do temp/cesty).
    """
    global CPU_POOL, _BEZICI, _FALLBACKU
    async with _sem():
        _BEZICI += 1
        try:
            if CPU_POOL is None:
                init_pool()

            if CPU_POOL is not None:
                loop = asyncio.get_running_loop()
                try:
                    return await loop.run_in_executor(CPU_POOL, fn, *args)
                except BrokenProcessPool as e:
                    # Worker zemřel (OOM/crash) → obnov pool a dojeď ve vlákně
                    print(f"[jobs] Proces-pool se rozbil ({e}); restart + vlákno.")
                    shutdown_pool()
                    init_pool()
                    _FALLBACKU += 1
                    return await asyncio.to_thread(fn, *args)
                except (pickle.PicklingError, AttributeError, TypeError) as e:
                    # Nepicklovatelná funkce/args (typicky closure) → vlákno.
                    # (Pool zůstává živý — chyba nastala při serializaci vstupu.)
                    _FALLBACKU += 1
                    return await asyncio.to_thread(fn, *args)

            # Pool není → vlákno (pořád mimo event-loop, takže bez zámrzu)
            _FALLBACKU += 1
            return await asyncio.to_thread(fn, *args)
        finally:
            _BEZICI -= 1


# ── Diagnostika (pro monitor) ───────────────────────────────────────────────
def pocet_bezicich() -> int:
    """Počet právě běžících těžkých úloh."""
    return _BEZICI


def info() -> dict:
    return {
        "max_workers": _MAX_CPU,
        "pool_aktivni": CPU_POOL is not None,
        "bezici_ulohy": _BEZICI,
        "fallbacku_na_vlakno": _FALLBACKU,
        "jader": os.cpu_count(),
    }
