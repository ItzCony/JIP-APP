import sys
sys.setrecursionlimit(3000)   # NiceGUI/inspect potřebuje více prostoru pro komplexní render stromy

import asyncio
import os
from nicegui import ui, app
from starlette.middleware.gzip import GZipMiddleware

# ── GZip komprese HTTP odpovědí ───────────────────────────────────────────────
# Textový obsah (SVG mapy pater ~4,8 MB → ~0,45 MB, initial HTML, JSON) se
# komprimuje za letu; malé odpovědi (<1 kB) a WebSocket provoz se nedotýkají.
# compresslevel=6: u velkých souborů skoro stejný výsledek jako 9 za zlomek CPU.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

import intranet_data
import intranet_static
import intranet_logger
import intranet_obsah
import intranet_finance
import intranet_kviz
import odstavka
import intranet
import intranet_oidc  # registruje /auth/login a /auth/callback (no-op bez env)
import intranet_exporty
import intranet_jobs
import znackyjip
import znacky_provoz
import prodejni_aktivity
import intranet_narozeniny
import intranet_vysledky
import intranet_spolvecer
import intranet_gastrokurzy

# Ladění `TypeError: u.match is not a function` (Quasar QIcon dostal ne-string):
# JIP_ICON_AUDIT=1 vypíše viníka do logu. Bez proměnné se modul neimportuje.
if os.environ.get('JIP_ICON_AUDIT') == '1':
    import intranet_icon_audit  # noqa: F401  (registruje app.on_connect)

# ── Bezpečnostní HTTP hlavičky + ochrana statických příloh ───────────────────
# Cesty servírované přes app.add_static_files, kam uživatelé NAHRÁVAJÍ soubory.
# U nich nutíme nebezpečné (inline-spustitelné) typy ke stažení, aby přes přímou
# URL nešlo spustit uložené XSS. Obrázky a PDF zůstávají inline (např. náhledy).
_UPLOAD_PREFIXY = (
    '/prilohy_predpoptavky', '/tisk_nakup', '/prilohy_nakup', '/faktury_soubory',
    '/faktury_exporty_soubory', '/znacky_foto', '/znacky_provoz_foto',
    '/kom_prilohy', '/spolvecer_prilohy', '/planogram_fotos', '/ukol_prilohy',
    '/projekt_prilohy', '/narozeniny_podpisy_static', '/sankce_prilohy',
    '/asm_oz_prilohy',
)
_NEBEZPECNE_PRIPONY = (
    '.html', '.htm', '.xhtml', '.shtml', '.svg', '.xml', '.js', '.mjs',
    '.mht', '.mhtml', '.swf', '.htc', '.eml',
)


@app.middleware('http')
async def _bezpecnostni_hlavicky(request, call_next):
    response = await call_next(request)
    # Globální obranné hlavičky (clickjacking, MIME-sniffing, únik referreru).
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    path = request.url.path
    if path.lower().endswith(_NEBEZPECNE_PRIPONY) and any(
        path.startswith(p) for p in _UPLOAD_PREFIXY
    ):
        # attachment → prohlížeč soubor stáhne místo vykreslení (žádný XSS běh).
        response.headers['Content-Disposition'] = 'attachment'
    return response


async def _nastav_exception_handler():
    """
    Potlačí neškodné výjimky vzniklé při odpojení prohlížeče nebo
    při pokusu timeru spustit callback na již smazaném UI elementu.
    """
    loop = asyncio.get_running_loop()

    def _handler(loop, context):
        exc = context.get('exception')
        # Normální odpojení prohlížeče (zavření záložky, navigace)
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        # Timer se pokusil spustit po smazání rodičovského elementu
        # (např. po refresh stránky) — zcela neškodné
        if isinstance(exc, RuntimeError) and 'parent slot' in str(exc):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


async def bg_periodicky_cleanup():
    """Každých 30 minut vyčistí vypršelé reset kódy a zastaralé záznamy v cache práv."""
    await asyncio.sleep(1800)
    while True:
        try:
            intranet_data._vycistit_reset_kody()
            intranet_data.vycistit_stare_cache_prava()
            intranet_data.vycistit_stare_login_pokusy()
        except Exception as e:
            print(f"[bg_periodicky_cleanup] Chyba: {e}")
        await asyncio.sleep(1800)


# POZN.: guard je ZÁMĚRNĚ jen "__main__" (ne "__mp_main__"). Proces-pool
# (intranet_jobs) spouští worker-procesy, které re-importují tento modul jako
# "__mp_main__"; kdyby guard platil i pro ně, každý worker by se pokusil
# nastartovat vlastní server na portu 8080. App běží s reload=False, takže
# "__main__" plně stačí.
if __name__ == "__main__":
    # Tajemství pro podpis session cookie MUSÍ přijít z prostředí. Bez něj (nebo
    # s prázdným) server odmítne start — žádný natvrdo zapsaný default v kódu.
    _STORAGE_SECRET = os.environ.get('NICEGUI_SECRET', '').strip()
    if len(_STORAGE_SECRET) < 16:
        print("\n" + "=" * 72)
        print("  CHYBA STARTU: chybí silné NICEGUI_SECRET.")
        print("  Nastavte proměnnou prostředí NICEGUI_SECRET (min. 16 znaků),")
        print("  např.:  export NICEGUI_SECRET=\"$(python -c 'import secrets;print(secrets.token_urlsafe(48))')\"")
        print("=" * 72 + "\n")
        raise SystemExit(1)

    # Audit log (SQLite audit_log.db): schéma + jednorázový import starých activity.log
    # a archivů z Exporty_Logy, pak spustí zapisovací vlákno.
    intranet_logger.init_db()
    # Audit: pády, chyby v UI handlerech a každý krok (kliknutí / výběr) ve všech modulech.
    intranet_logger.aktivuj_globalni_logovani()
    app.on_startup(_nastav_exception_handler)
    # Proces-pool pro CPU-náročné úlohy (exporty, tisk, parsování uploadů).
    app.on_startup(intranet_jobs.init_pool)
    app.on_startup(lambda: asyncio.create_task(intranet_jobs.warmup()))
    app.on_shutdown(intranet_jobs.shutdown_pool)
    # Při zastavení služby (systemctl stop → SIGTERM → graceful shutdown) zavři
    # nečinná DB spojení, ať MySQL nehlásí „Aborted connection".
    app.on_shutdown(intranet_data.zavri_db_pool)
    app.on_startup(lambda: asyncio.create_task(bg_periodicky_cleanup()))
    # Cache docházky/absencí se plní na pozadí co 20 min — modul se kreslí z RAM.
    app.on_startup(lambda: asyncio.create_task(intranet_data.bg_obnova_cache()))
    app.on_startup(lambda: asyncio.create_task(znackyjip.bg_uzavreni_pripadu()))
    app.on_startup(lambda: asyncio.create_task(znacky_provoz.bg_uzavreni_pripadu()))
    app.on_startup(prodejni_aktivity.inicializace_db)
    app.on_startup(intranet_spolvecer.inicializace_db)
    # Gastrokurzy — tabulky + jednorázový import termínů z Excelu (jen když je prázdno).
    app.on_startup(intranet_gastrokurzy.inicializace_db)
    # Gastrokurzy — denní souhrn nových/upravených termínů (výchozí 12:00).
    app.on_startup(lambda: asyncio.create_task(intranet_gastrokurzy.bg_gastrokurzy_souhrn()))

    app.on_startup(lambda: asyncio.create_task(intranet_narozeniny.bg_narozeniny_emaily()))
    # Evidence OZ — ranní přepočet „Aktivní OZ" + kontrola proti kartám.
    import intranet_asm
    app.on_startup(lambda: asyncio.create_task(intranet_asm.bg_oz_denni()))
    # Nahrané soubory jen pro přihlášené (viz intranet_static)
    intranet_static.chranene_soubory('/znacky_foto', 'znacky_foto')
    intranet_static.chranene_soubory(znacky_provoz.foto_route, znacky_provoz.foto_dir)
    intranet_static.chranene_soubory('/kom_prilohy', 'kom_prilohy')
    intranet_static.chranene_soubory('/spolvecer_prilohy', 'spolvecer_prilohy')

    # Jak dlouho server drží klienta po výpadku spojení (F5, zavření karty,
    # blokující JS) — do té doby mu běží elementy i timery. NiceGUI z hodnoty
    # odvozuje i heartbeat socket.io (ping_interval 0,8×, ping_timeout 0,4×),
    # zablokovaný prohlížeč (PDF export mapy) tedy vydrží ~1,4×. 15 s = polovina
    # dřívějších 30 s, export má pořád ~21 s rezervu. Strop 30 s: musí zůstat
    # pod intranet_session.ODHLASENI_PO_S (35 s). Ladění: JIPKA_RECONNECT_TIMEOUT.
    try:
        _reconnect_s = float(os.environ.get('JIPKA_RECONNECT_TIMEOUT', '15'))
    except ValueError:
        _reconnect_s = 15.0
    _reconnect_s = min(max(_reconnect_s, 3.0), 30.0)

    try:
        ui.run(
            title="Moje JIPka",
            language="cs",
            port=8080,
            favicon="favicon.ico",
            storage_secret=_STORAGE_SECRET,
            reload=False,
            reconnect_timeout=_reconnect_s,  # viz komentář nad try
            # JIP_DEV_VUE=1 => nemíněný (dev) Vue/Quasar: chyby v konzoli hlásí jméno
            # komponenty a vadný prop místo minifikovaného `u.match is not a function`.
            # Jen pro ladění na testu — dev build je větší a pomalejší.
            prod_js=os.environ.get('JIP_DEV_VUE') != '1',
        )
    except KeyboardInterrupt:
        pass

    # Příkaz /reboot z audit konzole: ui.run() se vrátil až po graceful shutdownu
    # (všechny on_shutdown hooky vč. intranet_data.zavri_db_pool už proběhly),
    # takže je bezpečné nahradit běžící proces čerstvým startem. os.execv zachová
    # PID i proměnné prostředí (NICEGUI_SECRET), funguje i pod systemd.
    if intranet_logger.RESTART_POZADOVAN:
        print('[reboot] Restart aplikace vyžádán z audit konzole — spouštím znovu…')
        sys.stdout.flush()
        # os.execv nespouští atexit — frontu audit logu dopíšeme do DB ručně.
        intranet_logger.dokonci_zapis()
        os.execv(sys.executable, [sys.executable] + sys.argv)