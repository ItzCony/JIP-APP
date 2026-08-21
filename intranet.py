from nicegui import ui, app, background_tasks, Client
from fastapi.responses import HTMLResponse
import html as _html
import intranet_data
import intranet_static
import intranet_obsah
import intranet_veletrh
import intranet_logger
import intranet_monitor
import intranet_helpdesk
import intranet_kviz
import intranet_nastaveni
import intranet_finance
import znackyjip
import znacky_provoz
import prodejni_aktivity
import intranet_narozeniny
import intranet_smeny
import intranet_notifikace
import intranet_komunikace
import intranet_planogram
import intranet_ochutnavky
import intranet_ukolovnik
import intranet_vysledky
import intranet_sankce
import intranet_cenopripad
import intranet_asm
import intranet_vizitky
import intranet_spolvecer
import intranet_lupa

import time
import asyncio
import contextvars
import re
import intranet_emaily
import intranet_session
import intranet_oidc
import intranet_2fa


def _odhlas_vycisti_relaci():
    """Vyprázdní uživatelskou relaci při odhlášení, ale ZACHOVÁ token
    důvěryhodného zařízení (2FA "Pamatovat toto zařízení") — jinak by se po
    každém odhlášení musel kód zadávat znovu. Důvěru ruší až expirace,
    vypnutí 2FA nebo "Odhlásit ze všech zařízení"."""
    _duvera = app.storage.user.get('totp_duvera_token')
    _email = app.storage.user.get('login_email')  # "Zapamatovat si mě" (jen e-mail, nikdy heslo)
    _oidc = app.storage.user.get('oidc_hint')     # účet pro login_hint (jen e-mail)
    app.storage.user.clear()
    if _duvera:
        app.storage.user['totp_duvera_token'] = _duvera
    if _email:
        app.storage.user['login_email'] = _email
    if _oidc:
        app.storage.user['oidc_hint'] = _oidc


# Statické soubory — whitelist konkrétních povolených souborů.
# ZÁMĚRNĚ nepoužíváme add_static_files('.'), aby nebyly dostupné
# konfigurační soubory s hesly (nastaveni_intranetu.json atd.)
import os as _os
_STATIC_WHITELIST = [
    'auto.svg', 'auto.png',
    'prizemi.svg', 'druhe_patro.svg', 'suteren.svg',
    'prizemi_podklad.png',
    'pozadi.jpg',
    'jip-logo.png', 'logo.png',
    'pruh_kviz.png',
    'favicon.ico',
]
for _sf in _STATIC_WHITELIST:
    if _os.path.exists(_sf):
        app.add_static_file(local_file=_sf, url_path=f'/static/{_sf}')
# Nahrané soubory servírujeme jen přihlášeným (viz intranet_static) — add_static_files
# je posílá komukoli, kdo uhodne jméno souboru.
for _dir in ('planogram_fotos', 'ochutnavky_prilohy', 'ukol_prilohy',
             'projekt_prilohy', 'spolvecer_prilohy', 'asm_prirucka'):
    intranet_static.chranene_soubory(f'/{_dir}', _dir)

# Evidence aktivních relací + „chytré" automatické odhlašování je v intranet_session
# (GLOBAL_ACTIVE_SESSIONS, počítání živých připojení, odložené odhlášení po zavření
# prohlížeče). Drží se pravidlo 1 účet = 1 aktivní relace.

# --- Vzhled přihlašovací obrazovky (varianta „tmavá cinematic") ---
# Scoped na body.prihlaseni-pozadi => aktivní JEN na přihlašovací stránce, jinde inertní.
# Používá se na přihlašovací obrazovce.
_LOGIN_CSS = '''
            /* ===== Přihlášení: tmavý cinematic vzhled ===== */
            .login-wrap { width: 100%; max-width: 400px; }
            /* POZOR: ui.image = q-img (poměr stran přes padding wrapper).
               Musí se zadat WIDTH, jinak se šířka spočte na 0 a logo zmizí.
               logo.png je 1000x475 => 122px šířka ≈ 58px výška. */
            .login-logo { width: 122px !important; height: auto; filter: drop-shadow(0 6px 18px rgba(0,0,0,.7)); }
            .login-title { color: #fff; font-size: 27px; font-weight: 800; letter-spacing: -.4px; }
            .login-sub { color: #9ba7b8; font-size: 13.5px; }
            .login-foot { color: #79839a; font-size: 11.5px; letter-spacing: .2px; }

            /* Průhledná skleněná pole (Quasar outlined + dark) */
            body.prihlaseni-pozadi .q-field--outlined .q-field__control {
                background: rgba(255,255,255,0.07);
                backdrop-filter: blur(9px);
                -webkit-backdrop-filter: blur(9px);
                border-radius: 12px;
                height: 54px;
                transition: background .18s ease;
            }
            body.prihlaseni-pozadi .q-field--outlined .q-field__control:before {
                border: 1.5px solid rgba(255,255,255,0.16);
                border-radius: 12px;
            }
            body.prihlaseni-pozadi .q-field--outlined:hover .q-field__control:before {
                border-color: rgba(255,255,255,0.32);
            }
            /* Červený rámeček POUZE při zaměření pole (jinak Quasar drží :after průhledné) */
            body.prihlaseni-pozadi .q-field--outlined .q-field__control:after {
                border-radius: 12px;
            }
            body.prihlaseni-pozadi .q-field--outlined.q-field--highlighted .q-field__control:after {
                border: 2px solid #ff5a63;
            }
            body.prihlaseni-pozadi .q-field--focused .q-field__control {
                background: rgba(255,255,255,0.11);
                box-shadow: 0 0 0 4px rgba(227,6,19,0.20);
            }
            body.prihlaseni-pozadi .q-field__native,
            body.prihlaseni-pozadi .q-field__input { color: #fff !important; }
            body.prihlaseni-pozadi .q-field__label { color: #8d99ab !important; }
            body.prihlaseni-pozadi .q-field--focused .q-field__label { color: #ff8189 !important; }
            body.prihlaseni-pozadi .q-field__prepend .q-icon,
            body.prihlaseni-pozadi .q-field__append .q-icon { color: rgba(255,255,255,.55); }
            /* Autofill prohlížeče nesmí udělat obdélník s vlastním pozadím.
               Trik: pozadí autofillu se ořízne na tvar písmen (background-clip: text),
               takže po poli nezůstane žádná plocha. Box-shadow trik se ZÁMĚRNĚ nepoužívá —
               kreslil by neprůhledný obdélník jen přes <input>, ne přes celé glass pole. */
            body.prihlaseni-pozadi input:-webkit-autofill,
            body.prihlaseni-pozadi input:-webkit-autofill:hover,
            body.prihlaseni-pozadi input:-webkit-autofill:focus,
            body.prihlaseni-pozadi input:-webkit-autofill:active {
                -webkit-background-clip: text !important;
                background-clip: text !important;
                -webkit-text-fill-color: #fff !important;
                caret-color: #fff !important;
                transition: background-color 9999s ease-in-out 0s;
            }

            /* Zapamatovat si mě + odkaz na zapomenuté heslo */
            body.prihlaseni-pozadi .login-chk .q-checkbox__label { color: #b6c0ce; font-size: 13.5px; font-weight: 600; }
            body.prihlaseni-pozadi .login-chk .q-checkbox__inner { color: #b6c0ce; }
            body.prihlaseni-pozadi .login-chk .q-checkbox__inner--truthy { color: #E30613; }
            .login-link { color: #ff8189 !important; font-size: 13.5px; font-weight: 700; }
            .login-link:hover { background: rgba(255,120,130,0.12) !important; }

            /* Hlavní tlačítko */
            .login-btn {
                height: 54px; border-radius: 12px;
                background: linear-gradient(135deg, #E30613, #b00510) !important;
                color: #fff !important; font-weight: 800; letter-spacing: .4px;
                transition: filter .18s ease, box-shadow .18s ease;
            }
            .login-btn:hover { filter: brightness(1.08); box-shadow: 0 10px 30px rgba(227,6,19,0.5); }

            /* Dialog zapomenutého hesla drží světlý vzhled (čitelnost) */
            body.prihlaseni-pozadi .q-dialog .q-field--outlined .q-field__control { background: #fff; backdrop-filter: none; }
            body.prihlaseni-pozadi .q-dialog .q-field__native,
            body.prihlaseni-pozadi .q-dialog .q-field__input { color: #111827 !important; }
            body.prihlaseni-pozadi .q-dialog .q-field__label { color: #6b7280 !important; }

            /* Mezistránka „Přihlašuji se…" — stejné pozadí jako přihlašovací obrazovka.
               POZOR: uvnitř q-dialogu nelze spoléhat na body::before/::after (dialog je
               nad nimi), proto se fotka i cinematic overlay kreslí znovu na kontejneru.
               position: absolute (ne fixed) — dialog je maximized, kryje celý viewport. */
            .login-loading { position: relative; background: #0a0e16; overflow: hidden; }
            .login-loading::before {
                content: ''; position: absolute; inset: 0;
                background-image: url("/static/pozadi.jpg");
                background-size: cover;
                background-position: center center;
                background-repeat: no-repeat;
                filter: grayscale(0.35) contrast(1.05);
                z-index: 0;
            }
            .login-loading::after {
                content: ''; position: absolute; inset: 0;
                background: radial-gradient(ellipse at 50% 45%,
                            rgba(10,14,22,0.55) 0%, rgba(8,11,18,0.93) 72%);
                z-index: 1; pointer-events: none;
            }
            .login-loading > * { position: relative; z-index: 2; }
            .login-loading-sub { color: #9ba7b8 !important; }
            .login-loading-name { color: #fff !important; text-shadow: 0 6px 24px rgba(0,0,0,.6); }
            /* Brána vynucené změny hesla — stejný glass jako přihlašovací pole */
            body.prihlaseni-pozadi .gate-card {
                background: rgba(255,255,255,0.07) !important;
                backdrop-filter: blur(9px);
                -webkit-backdrop-filter: blur(9px);
                border: 1.5px solid rgba(255,255,255,0.16);
                border-radius: 18px;
                box-shadow: 0 20px 60px rgba(0,0,0,.55);
                color: #fff;
            }
            body.prihlaseni-pozadi .gate-btn-secondary {
                background: rgba(255,255,255,0.10) !important;
                color: #d7dde6 !important;
                border: 1px solid rgba(255,255,255,0.18);
            }
            body.prihlaseni-pozadi .gate-btn-secondary:hover {
                background: rgba(255,255,255,0.18) !important;
            }
'''

def _obrazovka_vynucene_zmeny_hesla(user_id, user_email, user_name):
    """Fullscreen brána pro nový účet / heslo nastavené adminem.
    Žádný header, drawer ani taby – jediná cesta dál je změna hesla, jediná cesta ven je odhlášení."""
    ui.page_title('Změna hesla – Moje JIPka')
    ui.query('body').classes(add='prihlaseni-pozadi', remove='intranet-pozadi')

    with ui.column().classes('w-full h-screen items-center justify-center p-4'):
        with ui.card().classes('gate-card login-wrap p-8'):
            ui.icon('key', size='4rem', color='orange-500').classes('mb-3 self-center')
            ui.label('Změna hesla je povinná').classes('login-title text-center mb-1')
            ui.label(f'Vítejte, {user_name}! Před prvním použitím si prosím nastavte vlastní heslo.').classes('login-sub text-center mb-3')
            ui.label('Heslo musí mít min. 8 znaků, velké i malé písmeno a číslici.').classes('login-sub text-center mb-5')

            h1_inp = ui.input('Nové heslo', password=True, password_toggle_button=True).classes('w-full').props('outlined rounded')
            h2_inp = ui.input('Potvrďte heslo', password=True, password_toggle_button=True).classes('w-full mt-2').props('outlined rounded')
            chyba_lbl = ui.label('').classes('text-red-500 text-sm min-h-[1.2rem]')

            async def ulozit():
                h1, h2 = h1_inp.value, h2_inp.value
                if not intranet_data.heslo_je_silne(h1):
                    chyba_lbl.set_text('Heslo nesplňuje požadavky na složitost.')
                    return
                if h1 != h2:
                    chyba_lbl.set_text('Hesla se neshodují.')
                    return
                ok = await asyncio.to_thread(intranet_data.nastav_heslo_a_zrus_priznak, user_id, h1)
                if not ok:
                    chyba_lbl.set_text('Nepodařilo se změnit heslo. Zkuste to znovu.')
                    return
                intranet_logger.log_activity(user_name, "Přihlášení", "Vynucená změna hesla dokončena")
                ui.notify('Heslo bylo úspěšně změněno.', type='positive', position='top')
                ui.navigate.to('/')

            h2_inp.on('keydown.enter', ulozit)

            with ui.row().classes('w-full gap-3 mt-2'):
                def odhlasit():
                    _odhlas_vycisti_relaci()
                    ui.navigate.to('/')
                ui.button('Odhlásit', on_click=odhlasit).props('flat no-caps unelevated').classes('flex-1 gate-btn-secondary font-bold h-12 rounded-xl')
                ui.button('Uložit heslo', on_click=ulozit).props('no-caps unelevated').classes('flex-1 login-btn')

async def vykresli_kompletni_intranet(client: Client, aktivni_tab='prehled'):
    ui.page_title('Moje JIPka')

    # --- Tmavý režim (persistence přes app.storage.user) ---
    # ui.dark_mode() přepne Quasar body--dark => Quasar komponenty (karty, tabulky,
    # inputy, color= props) zčernají nativně. Tailwind bg-*/text-* třídy zůstávají
    # (řeší fáze F2 – globální CSS override). Volba se ukládá per-user.
    # POZN.: Tmavý režim je zatím v testovací fázi — přepínač v horním panelu byl
    # odebrán; zapíná/vypíná se pouze příkazem /dark-mode on|off v audit konzoli.
    # Zde už jen aplikujeme uloženou volbu při sestavení stránky.
    try:
        _tmavy_ulozeny = bool(app.storage.user.get('dark_mode', False))
    except Exception:
        _tmavy_ulozeny = False
    ui.dark_mode(value=_tmavy_ulozeny)

    # --- F2: globální CSS override Tailwind tříd pod tmavým režimem ---
    # Scoped na body.body--dark => aktivní JEN když je dark zapnutý (jinak inertní).
    # Remapuje ~40 nejčastějších bg-/text-/border- tříd (pokrytí ~80 % ploch) BEZ
    # sahání na 5140 inline .classes() v Pythonu. Dlouhý ocas (inline hex, sémantika,
    # SVG/mapy, kontrast) = fáze F3. !important kvůli výhře nad Tailwind utilitami.
    ui.add_head_html(r'''
    <style>
      @layer overrides {
      /* pozadí aplikace (bije body.intranet-pozadi !important, vyšší specificita) */
      body.body--dark, body.body--dark.intranet-pozadi, body.body--dark #q-app { background-color:#0f172a !important; }
      body.body--dark #_jip_preload { background:#0f172a !important; }
      /* povrchy: karty / panely / řádky */
      body.body--dark .bg-white, body.body--dark .bg-gray-50 { background-color:#1e293b !important; }
      body.body--dark .bg-gray-100 { background-color:#263449 !important; }
      body.body--dark .bg-gray-200 { background-color:#334155 !important; }
      body.body--dark .bg-gray-400 { background-color:#475569 !important; }
      /* text: tmavý -> světlý (text-white ZÁMĚRNĚ neměníme, drží na barevných tlačítkách) */
      body.body--dark .text-black, body.body--dark .text-gray-900,
      body.body--dark .text-gray-800, body.body--dark .text-gray-700 { color:#e2e8f0 !important; }
      body.body--dark .text-gray-600, body.body--dark .text-gray-500 { color:#94a3b8 !important; }
      body.body--dark .text-gray-400, body.body--dark .text-gray-300 { color:#64748b !important; }
      /* okraje */
      body.body--dark .border-gray-100, body.body--dark .border-gray-200,
      body.body--dark .border-gray-300 { border-color:#334155 !important; }
      body.body--dark .border-blue-100, body.body--dark .border-blue-200 { border-color:rgba(59,130,246,.35) !important; }
      body.body--dark .border-amber-200 { border-color:rgba(245,158,11,.35) !important; }
      /* sémantické světlé tinty pozadí -> tmavé tinty (drží odstín) */
      body.body--dark .bg-blue-50 { background-color:rgba(59,130,246,.14) !important; }
      body.body--dark .bg-blue-100 { background-color:rgba(59,130,246,.22) !important; }
      body.body--dark .bg-red-50 { background-color:rgba(239,68,68,.14) !important; }
      body.body--dark .bg-green-50 { background-color:rgba(34,197,94,.14) !important; }
      body.body--dark .bg-green-100 { background-color:rgba(34,197,94,.22) !important; }
      body.body--dark .bg-amber-50 { background-color:rgba(245,158,11,.14) !important; }
      body.body--dark .bg-indigo-50 { background-color:rgba(99,102,241,.14) !important; }
      /* sémantický text: zesvětlit pro čitelnost na tmavé */
      body.body--dark .text-red-700, body.body--dark .text-red-600,
      body.body--dark .text-red-500, body.body--dark .text-red-400 { color:#f87171 !important; }
      body.body--dark .text-green-800, body.body--dark .text-green-700,
      body.body--dark .text-green-600, body.body--dark .text-emerald-700 { color:#4ade80 !important; }
      body.body--dark .text-blue-900, body.body--dark .text-blue-800,
      body.body--dark .text-blue-700, body.body--dark .text-blue-600,
      body.body--dark .text-blue-500, body.body--dark .text-indigo-700,
      body.body--dark .text-indigo-600, body.body--dark .text-slate-800 { color:#60a5fa !important; }
      body.body--dark .text-amber-700, body.body--dark .text-orange-700,
      body.body--dark .text-orange-600 { color:#fbbf24 !important; }
      /* hover stavy (barevné saturované hovery jako hover:bg-blue-700 ponecháváme) */
      body.body--dark .hover\:bg-gray-50:hover, body.body--dark .hover\:bg-gray-100:hover { background-color:#334155 !important; }
      body.body--dark .hover\:bg-gray-300:hover { background-color:#475569 !important; }
      }
    </style>
    ''')

    # --- F3: rozsireni dark pokryti na dalsi pouzite Tailwind color tridy ---
    # Generovano z Tailwind palety pro realne pouzite NEpokryte tridy; overeno
    # tinycss2 (62 pravidel/0 chyb) + co-occurrence (0 kolizi jasny-bg x svetly-text).
    ui.add_head_html(r'''
    <style>
      @layer overrides {
      /* F4: opacity varianty bg-white (glassmorphism) -> tmave pruhledne, zachova backdrop-blur */
      body.body--dark .bg-white\/95 { background-color:rgba(30,41,59,0.95) !important; }
      body.body--dark .bg-white\/90 { background-color:rgba(30,41,59,0.90) !important; }
      body.body--dark .bg-white\/85 { background-color:rgba(30,41,59,0.85) !important; }
      body.body--dark .bg-white\/80 { background-color:rgba(30,41,59,0.80) !important; }
      body.body--dark .bg-white\/75 { background-color:rgba(30,41,59,0.75) !important; }
      body.body--dark .bg-white\/70 { background-color:rgba(30,41,59,0.70) !important; }
      body.body--dark .bg-white\/60 { background-color:rgba(30,41,59,0.60) !important; }
      body.body--dark .bg-white\/50 { background-color:rgba(30,41,59,0.50) !important; }
      /* svetly highlight banner (#FFFFCC) -> tmavy text i v dark (jinak text-gray-800 zesvetla = necitelne) */
      body.body--dark .oz-titul-banner{ color:#1f2937 !important; }
      /* F3: rozsireni pokryti dark rezimu na dalsi pouzite Tailwind color tridy */
      body.body--dark .border-gray-400, body.body--dark .border-gray-50, body.body--dark .border-slate-100, body.body--dark .border-slate-200, body.body--dark .border-slate-400 { border-color:#334155 !important; }
      body.body--dark .text-purple-500, body.body--dark .text-purple-600, body.body--dark .text-purple-700, body.body--dark .text-purple-800, body.body--dark .text-purple-900 { color:#c084fc !important; }
      body.body--dark .text-cyan-500, body.body--dark .text-cyan-600, body.body--dark .text-cyan-700, body.body--dark .text-cyan-900 { color:#22d3ee !important; }
      body.body--dark .text-teal-500, body.body--dark .text-teal-600, body.body--dark .text-teal-700, body.body--dark .text-teal-800 { color:#2dd4bf !important; }
      body.body--dark .text-yellow-600, body.body--dark .text-yellow-700, body.body--dark .text-yellow-800, body.body--dark .text-yellow-900 { color:#facc15 !important; }
      body.body--dark .text-amber-500, body.body--dark .text-amber-600, body.body--dark .text-amber-800, body.body--dark .text-amber-900 { color:#fbbf24 !important; }
      body.body--dark .border-emerald-100, body.body--dark .border-emerald-200, body.body--dark .border-emerald-300 { border-color:rgba(16,185,129,.38) !important; }
      body.body--dark .border-purple-100, body.body--dark .border-purple-200, body.body--dark .border-purple-300 { border-color:rgba(168,85,247,.38) !important; }
      body.body--dark .border-yellow-100, body.body--dark .border-yellow-200, body.body--dark .border-yellow-300 { border-color:rgba(234,179,8,.38) !important; }
      body.body--dark .border-pink-100, body.body--dark .border-pink-200, body.body--dark .border-pink-300 { border-color:rgba(236,72,153,.38) !important; }
      body.body--dark .border-red-100, body.body--dark .border-red-200, body.body--dark .border-red-300 { border-color:rgba(239,68,68,.38) !important; }
      body.body--dark .border-orange-100, body.body--dark .border-orange-200, body.body--dark .border-orange-300 { border-color:rgba(249,115,22,.38) !important; }
      body.body--dark .border-green-100, body.body--dark .border-green-200, body.body--dark .border-green-300 { border-color:rgba(34,197,94,.38) !important; }
      body.body--dark .border-indigo-100, body.body--dark .border-indigo-200, body.body--dark .border-indigo-300 { border-color:rgba(99,102,241,.38) !important; }
      body.body--dark .text-emerald-500, body.body--dark .text-emerald-600, body.body--dark .text-emerald-800 { color:#34d399 !important; }
      body.body--dark .text-violet-500, body.body--dark .text-violet-700, body.body--dark .text-violet-800 { color:#a78bfa !important; }
      body.body--dark .text-rose-500, body.body--dark .text-rose-600, body.body--dark .text-rose-700 { color:#fb7185 !important; }
      body.body--dark .bg-slate-100, body.body--dark .bg-slate-50 { background-color:#1e293b !important; }
      body.body--dark .border-teal-100, body.body--dark .border-teal-200 { border-color:rgba(20,184,166,.38) !important; }
      body.body--dark .border-amber-100, body.body--dark .border-amber-300 { border-color:rgba(245,158,11,.38) !important; }
      body.body--dark .text-green-500, body.body--dark .text-green-900 { color:#4ade80 !important; }
      body.body--dark .text-indigo-500, body.body--dark .text-indigo-800 { color:#818cf8 !important; }
      body.body--dark .text-slate-500, body.body--dark .text-slate-600 { color:#94a3b8 !important; }
      body.body--dark .text-red-800, body.body--dark .text-red-900 { color:#f87171 !important; }
      body.body--dark .text-orange-500, body.body--dark .text-orange-800 { color:#fb923c !important; }
      body.body--dark .bg-gray-300 { background-color:#334155 !important; }
      body.body--dark .bg-lime-100 { background-color:rgba(132,204,22,.18) !important; }
      body.body--dark .bg-violet-50 { background-color:rgba(139,92,246,.12) !important; }
      body.body--dark .bg-violet-100 { background-color:rgba(139,92,246,.18) !important; }
      body.body--dark .bg-sky-100 { background-color:rgba(14,165,233,.18) !important; }
      body.body--dark .bg-emerald-50 { background-color:rgba(16,185,129,.12) !important; }
      body.body--dark .bg-emerald-100 { background-color:rgba(16,185,129,.18) !important; }
      body.body--dark .bg-purple-50 { background-color:rgba(168,85,247,.12) !important; }
      body.body--dark .bg-purple-100 { background-color:rgba(168,85,247,.18) !important; }
      body.body--dark .bg-teal-50 { background-color:rgba(20,184,166,.12) !important; }
      body.body--dark .bg-teal-100 { background-color:rgba(20,184,166,.18) !important; }
      body.body--dark .bg-teal-200 { background-color:rgba(20,184,166,.26) !important; }
      body.body--dark .bg-yellow-50 { background-color:rgba(234,179,8,.12) !important; }
      body.body--dark .bg-yellow-100 { background-color:rgba(234,179,8,.18) !important; }
      body.body--dark .bg-pink-50 { background-color:rgba(236,72,153,.12) !important; }
      body.body--dark .bg-pink-100 { background-color:rgba(236,72,153,.18) !important; }
      body.body--dark .bg-red-100 { background-color:rgba(239,68,68,.18) !important; }
      body.body--dark .bg-rose-50 { background-color:rgba(244,63,94,.12) !important; }
      body.body--dark .bg-amber-100 { background-color:rgba(245,158,11,.18) !important; }
      body.body--dark .bg-orange-50 { background-color:rgba(249,115,22,.12) !important; }
      body.body--dark .bg-orange-100 { background-color:rgba(249,115,22,.18) !important; }
      body.body--dark .bg-green-200 { background-color:rgba(34,197,94,.26) !important; }
      body.body--dark .bg-blue-200 { background-color:rgba(59,130,246,.26) !important; }
      body.body--dark .bg-cyan-50 { background-color:rgba(6,182,212,.12) !important; }
      body.body--dark .bg-cyan-100 { background-color:rgba(6,182,212,.18) !important; }
      body.body--dark .bg-indigo-100 { background-color:rgba(99,102,241,.18) !important; }
      body.body--dark .border-lime-200 { border-color:rgba(132,204,22,.38) !important; }
      body.body--dark .border-violet-200 { border-color:rgba(139,92,246,.38) !important; }
      body.body--dark .border-sky-200 { border-color:rgba(14,165,233,.38) !important; }
      body.body--dark .border-fuchsia-200 { border-color:rgba(217,70,239,.38) !important; }
      body.body--dark .border-rose-200 { border-color:rgba(244,63,94,.38) !important; }
      body.body--dark .border-blue-300 { border-color:rgba(59,130,246,.38) !important; }
      body.body--dark .border-cyan-200 { border-color:rgba(6,182,212,.38) !important; }
      body.body--dark .text-sky-700 { color:#38bdf8 !important; }
      body.body--dark .text-lime-700 { color:#a3e635 !important; }
      body.body--dark .text-slate-700 { color:#e2e8f0 !important; }
      body.body--dark .text-pink-700 { color:#f472b6 !important; }
      }
    </style>
    ''')
    ui.add_head_html(r'''
    <style>
      /* F5: AG-Grid Excel-repro tabulky (vysledky) - v dark modu Quartz delal svetly text na
         natvrdo svetlych/pastelovych/bilych bunkach => necitelne cislice. Vynutime svetlou paletu
         bunek i v dark. UNLAYERED zamerne (prebije ag-grid theme, ktery je v @layer). Inline
         cellStyle barvy zustanou (inline > tato CSS): tmava hlavicka #404040+bily text,
         cervena/zelena delta, zelene/broskvove/modre pastely. */
      body.body--dark .tn-grid .ag-cell, body.body--dark .oz-grid .ag-cell,
      body.body--dark .obop-grid .ag-cell, body.body--dark .ao-grid .ag-cell,
      body.body--dark .prehled-grid .ag-cell, body.body--dark .komentare-grid .ag-cell{
        background-color:#ffffff; color:#1f2937;
      }
      body.body--dark .tn-grid .ag-header-cell, body.body--dark .oz-grid .ag-header-cell,
      body.body--dark .obop-grid .ag-header-cell, body.body--dark .ao-grid .ag-header-cell,
      body.body--dark .prehled-grid .ag-header-cell, body.body--dark .komentare-grid .ag-header-cell{
        background-color:#e5e7eb; color:#1f2937;
      }
    </style>
    ''')

    # Přidáme preloader ihned do initial HTML — jestliže je uživatel přihlášený,
    # browser dostane spinner ještě před tím, než se navázaným WebSocketem dokreslí UI.
    # Pro nepřihlášené (login stránka) preloader nevytváříme vůbec.
    try:
        _je_prihlasen = bool(app.storage.user.get('user_id'))
    except Exception:
        _je_prihlasen = False

    if _je_prihlasen:
        ui.add_head_html('''
        <style>
            /* Stejné pozadí jako přihlašovací obrazovka (fotka + cinematic overlay).
               Fotka je na ::before kvůli filtru — na elementu samotném by grayscale
               dědily i spinner a text. Děti proto potřebují position + z-index. */
            #_jip_preload {
                position: fixed; inset: 0;
                background: #0a0e16;
                overflow: hidden;
                z-index: 99998;
                display: flex; flex-direction: column; align-items: center; justify-content: center;
                transition: opacity 0.28s ease;
            }
            #_jip_preload::before {
                content: ''; position: absolute; inset: 0;
                background-image: url("/static/pozadi.jpg");
                background-size: cover;
                background-position: center center;
                background-repeat: no-repeat;
                filter: grayscale(0.35) contrast(1.05);
                z-index: 0;
            }
            #_jip_preload::after {
                content: ''; position: absolute; inset: 0;
                background: radial-gradient(ellipse at 50% 45%,
                            rgba(10,14,22,0.55) 0%, rgba(8,11,18,0.93) 72%);
                z-index: 1; pointer-events: none;
            }
            #_jip_preload > * { position: relative; z-index: 2; }
            #_jip_preload.out { opacity: 0; pointer-events: none; }
            @keyframes _jip_spin { to { transform: rotate(360deg); } }
        </style>
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                var el = document.createElement('div');
                el.id = '_jip_preload';
                el.innerHTML =
                    '<div style="width:48px;height:48px;border:5px solid rgba(255,255,255,0.18);'
                    + 'border-top-color:#E30613;'
                    + 'border-radius:50%;animation:_jip_spin .75s linear infinite"></div>'
                    + '<p style="margin:20px 0 0;color:#9ba7b8;font:600 12px/1 system-ui,sans-serif;'
                    + 'letter-spacing:.1em;text-transform:uppercase">Načítám přehled\u2026</p>';
                document.body.appendChild(el);
            });
            function _jipRemovePreloader() {
                var el = document.getElementById('_jip_preload');
                if (!el) return;
                el.classList.add('out');
                setTimeout(function() { if (el && el.parentNode) el.parentNode.removeChild(el); }, 300);
            }
        </script>
        ''')

    ui.add_head_html('''
        <style>
            /* Okamžité nastavení pozadí přes CSS ještě před spuštěním JS – eliminuje bílý záblesk */
            html, body { background-color: #f3f4f6; }

            body.prihlaseni-pozadi {
                margin: 0 !important;
                padding: 0 !important;
                overflow: hidden !important;
                width: 100% !important;
                height: 100% !important;
                background: transparent !important;
            }
            /* Pozadí jako fixní pseudo-element — vždy přesně pokrývá viewport, žádné přetékání */
            body.prihlaseni-pozadi::before {
                content: '' !important;
                position: fixed !important;
                inset: 0 !important;
                background-image: url("/static/pozadi.jpg") !important;
                background-size: cover !important;
                background-position: center center !important;
                background-repeat: no-repeat !important;
                filter: grayscale(0.35) contrast(1.05) !important;
                z-index: -2 !important;
            }
            /* Tmavý cinematic overlay nad fotkou */
            body.prihlaseni-pozadi::after {
                content: '' !important;
                position: fixed !important;
                inset: 0 !important;
                background: radial-gradient(ellipse at 50% 45%,
                            rgba(10,14,22,0.55) 0%, rgba(8,11,18,0.93) 72%) !important;
                z-index: -1 !important;
                pointer-events: none !important;
            }
            body.prihlaseni-pozadi #q-app {
                height: 100% !important;
                width: 100% !important;
                overflow: hidden !important;
                background: transparent !important;
            }
''' + _LOGIN_CSS + '''
            body.intranet-pozadi {
                margin: 0 !important;
                padding: 0 !important;
                overflow: auto !important;
                background-image: none !important;
                background-color: #f3f4f6 !important;
                min-height: 100vh !important;
            }
            .q-table__container {
                max-width: 100vw;
                overflow-x: auto;
                min-height: 20em;
            }
            /* 🚗 ANIMACE AUTÍČKA */
            @keyframes driveRight { 
                0% { left: -300px; } 
                100% { left: 100vw; } 
            }
            .car-container { 
                position: fixed; 
                top: 50%; 
                transform: translateY(-50%); 
                left: -300px; 
                z-index: 9999; 
                pointer-events: none; 
                display: none; 
                background: transparent !important; 
                box-shadow: none !important; 
                border: none !important; 
            }
            .car-container img { 
                background: transparent !important; 
            }
            .car-driving {
                display: block !important;
                animation: driveRight 1.5s ease-in-out forwards;
            }
        </style>
    ''')

    try:
        await client.connected()
    except Exception:
        return

    try:
        user_id = app.storage.user.get('user_id')
        user_email = str(app.storage.user.get('user_email', '')).lower()
        user_name = app.storage.user.get('user_name')
        muj_token = app.storage.user.get('login_token')
    except AssertionError:
        user_id = None
        user_email = ''
        user_name = None
        muj_token = None

    if muj_token:
        # Eviduj živé připojení této karty (single-session + „chytré" odhlašování
        # po zavření prohlížeče). on_connect/on_disconnect se navěsí uvnitř.
        intranet_session.registruj_pripojeni(client, muj_token, user_email, user_name)

    if user_id:
        # Single-session: pokud se na účet přihlásil někdo jiný (jako aktuální je
        # veden novější token), tuto starší relaci odhlásíme. Odhlašování po
        # zavření prohlížeče řeší intranet_session (přes připojení navěšená výše).
        def kontrola_relace():
            if not client.has_socket_connection:
                return
            vynuceno = intranet_session.ma_vynuceno_odhlaseni(user_email)
            if vynuceno or intranet_session.je_nahrazena(user_email, muj_token):
                try:
                    _odhlas_vycisti_relaci()
                    app.storage.user['odhlaseni_admin' if vynuceno else 'vykopnut_duplicita'] = True
                    ui.navigate.to('/')
                except Exception:
                    pass
        if client.has_socket_connection:
            ui.timer(30.0, kontrola_relace)  # 5s → 30s: snížení overhead WebSocket zpráv

    vsechna_prava = []
    if user_id:
        loop = asyncio.get_running_loop()
        prava_raw = await loop.run_in_executor(None, intranet_data.ziskej_prava_uzivatele, user_id)
        if not client.has_socket_connection:
            return
        vsechna_prava = [p.lower() for p in prava_raw]

    ma_vse = "vse" in vsechna_prava
    dostupne_taby = ['prehled', 'nastaveni', 'helpdesk']

    if ma_vse or "dochazka_admin" in vsechna_prava or "dochazka_export" in vsechna_prava or "dochazka_zadosti" in vsechna_prava or any(p.startswith('schvalovat_') for p in vsechna_prava) or any(p.startswith('hlavni_vedouci_') for p in vsechna_prava) or any(p.startswith('slozka_') for p in vsechna_prava):
        dostupne_taby.append('dochazka')
    if ma_vse or "vystup_vse" in vsechna_prava or "vystup_osobni" in vsechna_prava:
        dostupne_taby.append('vystup')
    if ma_vse or "uzivatele" in vsechna_prava:
        dostupne_taby.append('uzivatele')
    if ma_vse or "mysql" in vsechna_prava:
        dostupne_taby.append('mysql')
    if ma_vse or "veletrh_admin" in vsechna_prava or "veletrh_uzivatel" in vsechna_prava or "veletrh_komentator" in vsechna_prava or "veletrh_pristup" in vsechna_prava:
        dostupne_taby.append('veletrh')

    nastaveni = await asyncio.get_running_loop().run_in_executor(None, intranet_data.nacti_nastaveni_intranetu)
    if not client.has_socket_connection:
        return

    # --- Sledování změn nastavení (pro okamžitý reload při změně modulů adminem) ---
    try:
        _init_nastaveni_verze = app.storage.general.get('nastaveni_verze', 0)
    except Exception:
        _init_nastaveni_verze = 0

    # Mapování tab klíče → klíč nastavení modulu
    _TAB_MODUL_KLIC = {
        'finance':       'finance_zapnuty',
        'kviz':          'kviz_zapnuty',
        'veletrh':       'veletrh_zapnuty',
        'znacky':        'znacky_zapnuty',
        'znacky_provoz': 'znacky_provoz_zapnuty',
        'smeny':         'smeny_zapnuty',
        'planogram':     'planogram_zapnuty',
        'ochutnavky':    'ochutnavky_zapnuty',
        'narozeniny':    'narozeniny_zapnuty',
        'prod_akt':      'prod_akt_zapnuty',
        'ukolovnik':     'ukolovnik_zapnuty',
        'vysledky':      'vysledky_zapnuty',
        'sankce':        'sankce_zapnuty',
        'spolvecer':     'spolvecer_zapnuty',
        'vizitky':       'vizitky_zapnuty',
        'cenopripad':    'cenopripad_zapnuty',
        'asm':           'asm_zapnuty',
        'lupa':          'lupa_zapnuty',

    }

    def kontrola_nastaveni_verze():
        if not client.has_socket_connection:
            return
        try:
            global_verze = app.storage.general.get('nastaveni_verze', 0)
            if global_verze == _init_nastaveni_verze:
                return  # žádná změna
            # Přeskočíme akci pro toho, kdo změnu sám provedl
            vlastni_verze = app.storage.user.get('nastaveni_verze_vlastni', _init_nastaveni_verze)
            if global_verze == vlastni_verze:
                return

            # Zjisti, zda je uživatel na záložce modulu, který byl právě vypnut
            nova_nast     = intranet_data.nacti_nastaveni_intranetu()
            aktualni_tab  = app.storage.user.get('intranet_tab', 'prehled')
            klic_modulu   = _TAB_MODUL_KLIC.get(aktualni_tab)

            if klic_modulu and not nova_nast.get(klic_modulu, True):
                # Modul byl vypnut a uživatel je na něm — přesměruj na přehled
                app.storage.user['intranet_tab'] = 'prehled'
                ui.notify(
                    'Byl jste přesměrován z technických důvodů.',
                    type='warning', position='top', timeout=5000,
                )
                ui.navigate.to('/')
            else:
                # Jiná změna nastavení — tiše potvrdit verzi, bez reloadu
                app.storage.user['nastaveni_verze_vlastni'] = global_verze
        except Exception:
            pass

    if client.has_socket_connection:
        # 5 s stačí — jde jen o reload při vypnutí modulu adminem; 1 Hz per klient
        # zbytečně zatěžovala event loop při desítkách připojených uživatelů.
        ui.timer(5.0, kontrola_nastaveni_verze)

    ma_prava_finance = ma_vse or any(p in vsechna_prava for p in ['nakup_uzivatel', 'nakup_schvalit', 'faktury_seznam_schvalit'])
    has_finance = ma_prava_finance and nastaveni.get('finance_zapnuty', True)

    if has_finance:
        dostupne_taby.append('finance')

    if (ma_vse or "znacky_uzivatel" in vsechna_prava or "znacky_spravce" in vsechna_prava) and nastaveni.get('znacky_zapnuty', True):
        dostupne_taby.append('znacky')
    if (ma_vse or "znacky_provoz_uzivatel" in vsechna_prava or "znacky_provoz_spravce" in vsechna_prava) and nastaveni.get('znacky_provoz_zapnuty', True):
        dostupne_taby.append('znacky_provoz')
    _ma_prod_akt = bool(
        set(vsechna_prava) & {
            'prodej_akt_ctenar', 'prodej_akt_zadavatel', 'prodej_akt_ucetni',
            'prodej_akt_ao', 'prodej_akt_schvalovatel', 'vse',
        }
    )
    if _ma_prod_akt and nastaveni.get('prod_akt_zapnuty', True):
        dostupne_taby.append('prod_akt')
    ma_narozeniny = (
        ma_vse or
        "narozeniny_sprava" in vsechna_prava or
        "narozeniny_zobrazeni" in vsechna_prava
    ) and nastaveni.get('narozeniny_zapnuty', True)
    if ma_narozeniny:
        dostupne_taby.append('narozeniny')
    if ma_vse or "admin_logy" in vsechna_prava:
        dostupne_taby.append('logy')
    if ma_vse or "admin_server" in vsechna_prava:
        dostupne_taby.append('server')

    # Plánování směn: admin nebo uživatel s přímým pravem nebo vedoucí oddělení, které má skupinu
    _ma_smeny_pravo = (ma_vse or 'smeny_admin' in vsechna_prava
                       or 'smeny_zobrazit' in vsechna_prava
                       or 'smeny_vedouci' in vsechna_prava)
    if not _ma_smeny_pravo:
        # zkusíme, jestli je vedoucí oddělení, které má skupinu
        _ma_smeny_pravo = any(p.startswith('hlavni_vedouci_') for p in vsechna_prava)
    if _ma_smeny_pravo and nastaveni.get('smeny_zapnuty', True):
        dostupne_taby.append('smeny')

    if nastaveni.get('komunikace_zapnuty', True):
        dostupne_taby.append('komunikace')

    _ma_planogram = (
        'vse' in vsechna_prava or
        'planogram_admin' in vsechna_prava or
        'planogram_pristup' in vsechna_prava
    )
    if _ma_planogram and nastaveni.get('planogram_zapnuty', True):
        dostupne_taby.append('planogram')

    _ma_ochutnavky = (
        'vse' in vsechna_prava or
        'ochutnavky_admin' in vsechna_prava or
        'ochutnavky_pristup' in vsechna_prava
    )
    if _ma_ochutnavky and nastaveni.get('ochutnavky_zapnuty', True):
        dostupne_taby.append('ochutnavky')

    _ma_ukolovnik = (
        ma_vse or
        'ukolovnik_admin' in vsechna_prava or
        any(p.startswith('ukolovnik_') for p in vsechna_prava)
    )
    if _ma_ukolovnik and nastaveni.get('ukolovnik_zapnuty', True):
        dostupne_taby.append('ukolovnik')

    _ma_vysledky = (
        ma_vse or
        'vysledky_ao' in vsechna_prava or
        'vysledky_ucetni' in vsechna_prava or
        'vysledky_ucetni_bezna' in vsechna_prava or
        'vysledky_majitel' in vsechna_prava or
        any(p.startswith('vysledky_pobocka_') for p in vsechna_prava)
    )
    if _ma_vysledky and nastaveni.get('vysledky_zapnuty', True):
        dostupne_taby.append('vysledky')

    _ma_sankce = (
        ma_vse or
        'sankce_analytik' in vsechna_prava or
        'sankce_ucetni' in vsechna_prava or
        'sankce_nakup' in vsechna_prava or
        'sankce_ctenar' in vsechna_prava
    )
    if _ma_sankce and nastaveni.get('sankce_zapnuty', True):
        dostupne_taby.append('sankce')

    _ma_spolvecer = (
        ma_vse or
        'spolvecer_ctenar' in vsechna_prava or
        'spolvecer_schvalovatel' in vsechna_prava or
        any(p.startswith('spolvecer_organizator_') for p in vsechna_prava)
    )
    if _ma_spolvecer and nastaveni.get('spolvecer_zapnuty', True):
        dostupne_taby.append('spolvecer')

    # Žadatelem je automaticky každý přihlášený uživatel → modul je dostupný všem (když je zapnutý).
    if nastaveni.get('vizitky_zapnuty', True):
        dostupne_taby.append('vizitky')


    _ma_cenopripad = (
        ma_vse or
        any(p in vsechna_prava for p in (
            'cenopripad_zadatel_nakup', 'cenopripad_zadatel_obchod',
            'cenopripad_zadatel_letaky', 'cenopripad_office_letaky',
            'cenopripad_ctenar_letaky',
            'cenopripad_office_nakup', 'cenopripad_office_obchod',
            'cenopripad_spravce_nakup', 'cenopripad_spravce',
            'cenopripad_spravce_bez_emailu', 'cenopripad_vkladatel',
            'cenopripad_zobrazeni_oddeleni')) or
        # Schvalovatel – oddělení: dynamické právo cp_schval_odd_<oddělení>
        any(p.startswith('cp_schval_odd_') for p in vsechna_prava)
    )
    if _ma_cenopripad and nastaveni.get('cenopripad_zapnuty', True):
        dostupne_taby.append('cenopripad')

    _ma_asm = (
        ma_vse or
        any(p in vsechna_prava for p in (
            'asm_zadatel', 'asm_office', 'asm_spravce',
            'asm_spravce_bez_emailu', 'asm_vkladatel'))
    )
    # Schůzky s vedoucími jsou dlaždice uvnitř Formulářů ASM — kdo má právo jen
    # na ně, musí záložku vidět taky (uvnitř pak uvidí jen tu jednu dlaždici).
    _ma_schuzky = (
        ma_vse or
        any(p in vsechna_prava for p in
            ('schuzky_zadatel', 'schuzky_vedouci', 'schuzky_spravce'))
    )
    if ((_ma_asm and nastaveni.get('asm_zapnuty', True))
            or (_ma_schuzky and nastaveni.get('schuzky_zapnuty', True))):
        dostupne_taby.append('asm')

    # Lupa: přístup se z práv odvodit nedá — rozhoduje spárování ASM s příjmením
    # uživatele a vedení oddělení. Ptáme se modulu.
    if nastaveni.get('lupa_zapnuty', True) and intranet_lupa.ma_pristup(user_id, vsechna_prava):
        dostupne_taby.append('lupa')

    if not user_id:
        ui.query('body').classes(add='prihlaseni-pozadi', remove='intranet-pozadi')
        with ui.column().classes('w-full h-screen items-center justify-center m-0 py-8 px-4 overflow-y-auto'):
            try:
                if app.storage.user.get('vykopnut_duplicita'):
                    ui.notify('Byli jste odhlášeni, protože se na tento účet právě přihlásil někdo jiný na jiném zařízení.', type='negative', position='top', timeout=10000)
                    app.storage.user.pop('vykopnut_duplicita', None)
            except AssertionError:
                pass

            try:
                if app.storage.user.get('odhlaseni_necinnost'):
                    ui.notify('Byli jste automaticky odhlášeni z důvodu nečinnosti.', type='warning', position='top', timeout=8000)
                    app.storage.user.pop('odhlaseni_necinnost', None)
            except AssertionError:
                pass

            try:
                if app.storage.user.get('odhlaseni_admin'):
                    ui.notify('Byli jste odhlášeni administrátorem.', type='warning', position='top', timeout=8000)
                    app.storage.user.pop('odhlaseni_admin', None)
            except AssertionError:
                pass

            car_img = ui.image('/static/auto.svg').classes('car-container w-64')

            # Předvyplnění e-mailu z „Zapamatovat si mě" (ukládá se JEN e-mail, nikdy heslo)
            try:
                _zapamatovany_email = str(app.storage.user.get('login_email') or '')
            except Exception:
                _zapamatovany_email = ''

            with ui.column().classes('login-wrap items-center gap-0 mx-auto'):
                ui.image('/static/logo.png').classes('login-logo mb-6')
                ui.label('Vítejte v Moje JIPka').classes('login-title text-center')
                ui.label('Přihlaste se firemním účtem, nebo os. číslem').classes('login-sub text-center mt-1 mb-6')

                # Firemní účet (OIDC) je primární cesta: tlačítko nahoře, heslový formulář
                # schovaný za odkazem. Bez env konfigurace (`je_zapnuto()` False) se nic
                # nevykreslí a stránka vypadá přesně jako dřív.
                _oidc_on = intranet_oidc.je_zapnuto()

                def _ukaz_heslo():
                    heslo_box.set_visibility(True)
                    heslo_odkaz.set_visibility(False)

                if _oidc_on:
                    ui.button('Přihlásit se firemním účtem (MS365)', icon='business',
                              on_click=lambda: ui.navigate.to('/auth/login')) \
                        .props('no-caps unelevated').classes('login-btn w-full')
                    heslo_odkaz = ui.button('Přihlásit se heslem', on_click=_ukaz_heslo) \
                        .props('flat dense no-caps').classes('login-link mt-3 mb-2')
                    _oidc_chyba = app.storage.user.pop('oidc_chyba', '')
                    if _oidc_chyba:
                        ui.timer(0.1, lambda z=_oidc_chyba: ui.notify(z, type='warning', position='top', timeout=8000), once=True)

                heslo_box = ui.column().classes('w-full gap-0')
                with heslo_box:
                    email_input = ui.input('E-mail nebo osobní číslo', value=_zapamatovany_email) \
                        .classes('w-full mb-3').props('outlined dark type=text autocomplete=username name=email id=login-email')
                    with email_input.add_slot('prepend'):
                        ui.icon('mail_outline')

                    heslo_input = ui.input('Heslo', password=True, password_toggle_button=True) \
                        .classes('w-full mb-1').props('outlined dark autocomplete=current-password name=password id=login-password')
                    with heslo_input.add_slot('prepend'):
                        ui.icon('lock_outline')

                    # Řádek „zapamatovat + zapomenuté heslo" a hlavní tlačítko se vykreslí až
                    # níže (po definici obslužných funkcí) do tohoto kontejneru, aby pořadí
                    # na obrazovce odpovídalo návrhu.
                    akce_box = ui.column().classes('w-full gap-0')

                if _oidc_on:
                    heslo_box.set_visibility(False)

                async def zkusit_prihlaseni():
                    email = email_input.value.strip().lower()
                    heslo = heslo_input.value

                    # Přihlášení osobním číslem s příznakem (JV323514) → přeložíme na
                    # e-mail. Nenajde-li se, jde původní hodnota dál a spadne na běžnou
                    # chybu (útočník tak nezjistí, které číslo existuje).
                    if '@' not in email:
                        email = await asyncio.get_running_loop().run_in_executor(
                            None, intranet_data.email_z_osobniho_cisla, email) or email

                    id_u, jmeno_u, msg = await asyncio.get_running_loop().run_in_executor(
                        None, intranet_data.overit_prihlaseni, email, heslo)

                    async def dokonci_prihlaseni():
                        if app.storage.user.get('auto_zapnuto', True):
                            car_img.classes(add='car-driving')

                        # „Zapamatovat si mě" — ukládáme POUZE e-mail do relace uživatele
                        # (přežije odhlášení, viz _odhlas_vycisti_relaci). Heslo nikdy a nikam.
                        try:
                            if zapamatovat.value:
                                app.storage.user['login_email'] = email
                            else:
                                app.storage.user.pop('login_email', None)
                        except Exception:
                            pass

                        intranet_session.zahaj_relaci(id_u, email, jmeno_u)

                        with ui.dialog().props('maximized persistent transition-show="fade" transition-hide="fade"') as loading_dlg:
                            with ui.column().classes('login-loading w-full h-full items-center justify-center m-0 p-0'):
                                ui.spinner('dots', size='5rem', color='white').classes('mb-6')
                                ui.label('Přihlašuji se').classes('login-loading-sub text-sm font-bold uppercase tracking-widest mb-2')
                                ui.label(f'Vítejte, {jmeno_u}!').classes('login-loading-name text-3xl font-black')
                        loading_dlg.open()

                        _cil = f'/{aktivni_tab}' if aktivni_tab != 'prehled' else '/'
                        ui.timer(0.8, lambda: ui.navigate.to(_cil), once=True)

                        # Logování až PO zobrazení dialogu a naplánování navigace —
                        # JS detekce Brave (timeout až 2 s) nesmí zdržet přihlášení.
                        _je_brave = await intranet_logger.zjisti_brave()
                        _ip, _zarizeni = intranet_logger.ziskej_klienta_info(client, je_brave=_je_brave)
                        intranet_session.zaznamenej_klienta(email, jmeno_u, _ip, _zarizeni)

                    if id_u:
                        # 2FA: relace vznikne až po ověření kódu z autentifikační aplikace
                        ma_2fa = await asyncio.get_running_loop().run_in_executor(
                            None, intranet_2fa.ma_aktivni_2fa, id_u)
                        if not ma_2fa:
                            await dokonci_prihlaseni()
                        else:
                            # Zapamatované zařízení (2FA "Pamatovat toto zařízení"):
                            # platný token v prohlížeči → TOTP kód se nevyžaduje.
                            _duvera_token = app.storage.user.get('totp_duvera_token')
                            if _duvera_token and await asyncio.get_running_loop().run_in_executor(
                                    None, intranet_2fa.je_zarizeni_duveryhodne, id_u, _duvera_token):
                                intranet_logger.log_activity(jmeno_u, "Přihlášení", f"Přihlášení na zapamatovaném zařízení bez 2FA kódu (E-mail: {email})")
                                await dokonci_prihlaseni()
                                return
                            with ui.dialog().props('persistent') as dlg_2fa, \
                                    ui.card().classes('p-6 sm:p-8 rounded-2xl w-full max-w-sm shadow-2xl mx-4'):
                                ui.icon('verified_user', size='4rem', color='blue-500').classes('mb-3 self-center')
                                ui.label('Dvoufaktorové ověření').classes('text-2xl font-black text-center text-gray-800 mb-1')
                                ui.label('Zadejte 6místný kód z autentifikační aplikace, nebo jeden ze záložních kódů.').classes('text-gray-800 text-sm text-center mb-5')
                                kod_2fa = ui.input('Ověřovací kód').classes('w-full mb-3 text-lg') \
                                    .props('outlined rounded autofocus inputmode=numeric autocomplete=one-time-code')
                                zapamatovat_2fa = ui.checkbox('Pamatovat toto zařízení (30 dní)') \
                                    .classes('w-full text-sm text-gray-700 mb-2 self-start')

                                async def overit_kod_2fa():
                                    ok, zprava = await asyncio.get_running_loop().run_in_executor(
                                        None, intranet_2fa.overit_2fa_kod, id_u, kod_2fa.value)
                                    if ok:
                                        dlg_2fa.close()
                                        # Zapamatovat zařízení: token uložíme do prohlížeče (relace),
                                        # hash do DB — příště se na něm 2FA kód přeskočí.
                                        if zapamatovat_2fa.value:
                                            _tok = await asyncio.get_running_loop().run_in_executor(
                                                None, intranet_2fa.zaregistruj_duveryhodne_zarizeni, id_u, None)
                                            if _tok:
                                                app.storage.user['totp_duvera_token'] = _tok
                                        if zprava:
                                            ui.notify(zprava, type='warning', position='top', timeout=8000)
                                        await dokonci_prihlaseni()
                                        return
                                    if zprava and str(zprava).startswith('ZAMCEN:'):
                                        dlg_2fa.close()
                                        try:
                                            zb = int(str(zprava).split(':', 1)[1])
                                            ui.notify(f'Příliš mnoho chybných kódů. Zkuste to za {max(1, (zb + 59) // 60)} minut.', type='negative', position='top')
                                        except Exception:
                                            ui.notify('Příliš mnoho chybných kódů. Zkuste to později.', type='negative', position='top')
                                    else:
                                        ui.notify(zprava or 'Neplatný ověřovací kód.', type='negative', position='top')
                                    intranet_logger.log_activity(jmeno_u, "Chyba přihlášení", f"Neplatný 2FA kód (E-mail: {email})")

                                kod_2fa.on('keydown.enter', overit_kod_2fa)
                                with ui.row().classes('w-full justify-end gap-3'):
                                    ui.button('Zrušit', on_click=dlg_2fa.close).props('flat no-caps').classes('text-gray-600 font-semibold')
                                    ui.button('Ověřit', on_click=overit_kod_2fa).classes('bg-blue-600 hover:bg-blue-700 text-white font-bold px-6')
                            dlg_2fa.open()
                    else:
                        # K-6: Nejdřív okamžitá odezva uživateli, logování (JS roundtrip) až po ní
                        if msg and str(msg).startswith('ZAMCEN:'):
                            try:
                                zbyvaji_s = int(str(msg).split(':', 1)[1])
                                zbyvaji_min = max(1, (zbyvaji_s + 59) // 60)
                                ui.notify(f'Účet je dočasně zablokován. Zkuste to za {zbyvaji_min} minut.', type='negative', position='top')
                            except Exception:
                                ui.notify('Účet je dočasně zablokován. Zkuste to později.', type='negative', position='top')
                        else:
                            ui.notify(msg, type='negative', position='top')
                        _je_brave_fail = await intranet_logger.zjisti_brave()
                        _ip_fail, _zar_fail = intranet_logger.ziskej_klienta_info(client, je_brave=_je_brave_fail)
                        intranet_logger.log_activity("Neznámý uživatel", "Chyba přihlášení", f"Pokus o přihlášení na e-mail: {email}", ip=_ip_fail, device=_zar_fail)

                heslo_input.on('keydown.enter', zkusit_prihlaseni)

                ui.run_javascript("""
                    setTimeout(function() {
                        var fields = document.querySelectorAll('.q-field');
                        fields.forEach(function(field) {
                            var label = field.querySelector('.q-field__label');
                            if (!label) return;
                            var txt = label.textContent.toLowerCase();
                            var inp = field.querySelector('input');
                            if (!inp) return;
                            if (txt.includes('mail') || txt.includes('adresa')) {
                                inp.setAttribute('autocomplete', 'email');
                                inp.setAttribute('name', 'email');
                                inp.setAttribute('type', 'email');
                                inp.setAttribute('id', 'login-email');
                            } else if (txt.includes('heslo') || inp.type === 'password') {
                                inp.setAttribute('autocomplete', 'current-password');
                                inp.setAttribute('name', 'password');
                                inp.setAttribute('id', 'login-password');
                            }
                        });
                    }, 300);
                """)

                async def ukaz_zapomenute_heslo():
                    stav = {'krok': 1, 'email': ''}

                    with ui.dialog() as dlg, ui.card().classes('p-6 sm:p-8 rounded-2xl w-full max-w-sm shadow-2xl mx-4'):

                        @ui.refreshable
                        def krok_obsah():

                            if stav['krok'] == 1:
                                ui.icon('lock_reset', size='4rem', color='blue-500').classes('mb-3 self-center')
                                ui.label('Obnovení hesla').classes('text-2xl font-black text-center text-gray-800 mb-1')
                                ui.label('Zadejte e-mail vašeho účtu. Zašleme vám ověřovací kód.').classes('text-gray-800 text-sm text-center mb-5')

                                email_inp = ui.input('E-mailová adresa').classes('w-full').props('outlined rounded')
                                chyba_lbl = ui.label('').classes('text-red-500 text-sm min-h-[1.2rem]')

                                async def odeslat_kod():
                                    em = email_inp.value.strip().lower()
                                    if not em:
                                        chyba_lbl.set_text('Zadejte e-mail.')
                                        return
                                    kod = await asyncio.to_thread(intranet_data.vygeneruj_reset_kod, em)
                                    # Vždy zobrazujeme stejnou zprávu — zabraňujeme email enumeration (K-7)
                                    if kod is None:
                                        stav['email'] = em
                                        stav['krok'] = 2
                                        krok_obsah.refresh()
                                        return
                                    predmet = 'Obnova hesla — ověřovací kód'
                                    text = (
                                        f'Dobrý den,\n\n'
                                        f'byl vyžádán reset hesla pro váš účet ({em}).\n\n'
                                        f'Váš ověřovací kód: {kod}\n\n'
                                        f'Kód je platný 15 minut. Pokud jste reset hesla nevyžádali, ignorujte tento e-mail.\n\n'
                                        f'Moje JIPka'
                                    )
                                    await asyncio.to_thread(intranet_emaily.odesli_upozorneni_email, em, predmet, text)
                                    intranet_logger.log_activity(em, "Reset hesla", "Odeslán ověřovací kód")
                                    stav['email'] = em
                                    stav['krok'] = 2
                                    krok_obsah.refresh()

                                email_inp.on('keydown.enter', odeslat_kod)
                                with ui.row().classes('w-full gap-3 mt-2'):
                                    ui.button('Zrušit', on_click=dlg.close).classes('flex-1 bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold h-12 rounded-xl')
                                    ui.button('Odeslat kód', on_click=odeslat_kod, icon='send').classes('flex-1 bg-blue-600 hover:bg-blue-700 text-white font-bold h-12 rounded-xl')

                            elif stav['krok'] == 2:
                                ui.icon('mark_email_read', size='4rem', color='green-500').classes('mb-3 self-center')
                                ui.label('Zadejte ověřovací kód').classes('text-2xl font-black text-center text-gray-800 mb-1')
                                ui.label(f'Kód jsme odeslali na {stav["email"]}').classes('text-gray-800 text-sm text-center mb-5')

                                kod_inp = ui.input('6místný kód').classes('w-full').props('outlined rounded maxlength=6')
                                chyba_lbl = ui.label('').classes('text-red-500 text-sm min-h-[1.2rem]')

                                async def overit_kod():
                                    ok = await asyncio.to_thread(intranet_data.overit_reset_kod, stav['email'], kod_inp.value)
                                    if not ok:
                                        chyba_lbl.set_text('Neplatný nebo vypršelý kód. Zkuste to znovu.')
                                        return
                                    stav['krok'] = 3
                                    krok_obsah.refresh()

                                async def znovu_odeslat():
                                    kod = await asyncio.to_thread(intranet_data.vygeneruj_reset_kod, stav['email'])
                                    if kod:
                                        predmet = 'Obnova hesla — nový ověřovací kód'
                                        text = (
                                            f'Dobrý den,\n\n'
                                            f'Váš nový ověřovací kód: {kod}\n\n'
                                            f'Kód je platný 15 minut.\n\n'
                                            f'Moje JIPka'
                                        )
                                        await asyncio.to_thread(intranet_emaily.odesli_upozorneni_email, stav['email'], predmet, text)
                                        chyba_lbl.set_text('')
                                        ui.notify('Nový kód byl odeslán.', type='positive', position='top')

                                kod_inp.on('keydown.enter', overit_kod)
                                with ui.row().classes('w-full gap-3 mt-2'):
                                    ui.button('Zpět', on_click=lambda: [stav.update({'krok': 1}), krok_obsah.refresh()]).classes('flex-1 bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold h-12 rounded-xl')
                                    ui.button('Ověřit kód', on_click=overit_kod, icon='check').classes('flex-1 bg-blue-600 hover:bg-blue-700 text-white font-bold h-12 rounded-xl')
                                ui.button('Kód nedorazil? Odeslat znovu', on_click=znovu_odeslat).props('flat').classes('w-full text-xs text-gray-400 hover:text-blue-500 mt-1')

                            elif stav['krok'] == 3:
                                ui.icon('key', size='4rem', color='orange-500').classes('mb-3 self-center')
                                ui.label('Nové heslo').classes('text-2xl font-black text-center text-gray-800 mb-1')
                                ui.label('Heslo musí mít min. 8 znaků, velké i malé písmeno a číslici.').classes('text-gray-800 text-sm text-center mb-5')

                                heslo1_inp = ui.input('Nové heslo', password=True, password_toggle_button=True).classes('w-full').props('outlined rounded')
                                heslo2_inp = ui.input('Potvrďte heslo', password=True, password_toggle_button=True).classes('w-full mt-2').props('outlined rounded')
                                chyba_lbl = ui.label('').classes('text-red-500 text-sm min-h-[1.2rem]')

                                async def ulozit_heslo():
                                    h1, h2 = heslo1_inp.value, heslo2_inp.value
                                    if not intranet_data.heslo_je_silne(h1):
                                        chyba_lbl.set_text('Heslo nesplňuje požadavky na složitost.')
                                        return
                                    if h1 != h2:
                                        chyba_lbl.set_text('Hesla se neshodují.')
                                        return
                                    ok = await asyncio.to_thread(intranet_data.zmen_heslo_emailem, stav['email'], h1)
                                    if not ok:
                                        chyba_lbl.set_text('Nepodařilo se změnit heslo. Zkuste to znovu.')
                                        return
                                    predmet = 'Heslo bylo změněno'
                                    text = (
                                        f'Dobrý den,\n\n'
                                        f'heslo vašeho účtu ({stav["email"]}) bylo právě úspěšně změněno.\n\n'
                                        f'Pokud jste tuto změnu neprovedli, kontaktujte neprodleně správce systému.\n\n'
                                        f'Moje JIPka'
                                    )
                                    await asyncio.to_thread(intranet_emaily.odesli_upozorneni_email, stav['email'], predmet, text)
                                    intranet_logger.log_activity(stav['email'], "Reset hesla", "Heslo úspěšně změněno")
                                    dlg.close()
                                    ui.notify('Heslo bylo úspěšně změněno. Můžete se přihlásit.', type='positive', position='top')

                                heslo2_inp.on('keydown.enter', ulozit_heslo)
                                with ui.row().classes('w-full gap-3 mt-2'):
                                    ui.button('Zrušit', on_click=dlg.close).classes('flex-1 bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold h-12 rounded-xl')
                                    ui.button('Uložit heslo', on_click=ulozit_heslo, icon='save').classes('flex-1 bg-green-600 hover:bg-green-700 text-white font-bold h-12 rounded-xl')

                        krok_obsah()
                    dlg.open()

                # Řádek s volbami + hlavní tlačítko (vykresleno do kontejneru nad formulářem)
                with akce_box:
                    with ui.row().classes('w-full items-center justify-between mt-2 mb-4 flex-nowrap'):
                        zapamatovat = ui.checkbox('Zapamatovat si mě', value=bool(_zapamatovany_email)) \
                            .classes('login-chk').props('dense color=red-7')
                        ui.button('Zapomněli jste heslo?', on_click=ukaz_zapomenute_heslo) \
                            .props('flat dense no-caps').classes('login-link')

                    ui.button('Přihlásit se', on_click=zkusit_prihlaseni).props('no-caps unelevated').classes('login-btn w-full')

                # Patička je mimo `heslo_box` i `akce_box` — musí zůstat vidět i tehdy,
                # když je heslový formulář schovaný.
                with ui.row().classes('w-full justify-center items-center mt-7 gap-0'):
                    ui.label('JIP východočeská, a.s.').classes('login-foot')

    else:
        # Brána: nový účet / heslo nastavené adminem → nic jiného se nevykreslí, dokud si heslo nezmění
        nutna, dni_hesla = await asyncio.to_thread(intranet_data.stav_hesla, user_id)
        if nutna:
            _obrazovka_vynucene_zmeny_hesla(user_id, user_email, user_name)
            # Preloader se schovává až na konci funkce — tady odcházíme dřív, schovat ho ručně
            if _je_prihlasen:
                ui.run_javascript('if(typeof _jipRemovePreloader==="function") _jipRemovePreloader();')
            return

        # Připomínka staršího hesla — jen upozornění, max 1× denně
        if dni_hesla is not None and dni_hesla >= intranet_data.HESLO_MAX_DNI:
            if app.storage.user.get('heslo_pripominka_den') != time.strftime('%Y-%m-%d'):
                app.storage.user['heslo_pripominka_den'] = time.strftime('%Y-%m-%d')
                ui.notify(f'Heslo jste neměnili {dni_hesla} dní. Změnit si ho můžete v Osobním nastavení účtu.',
                          type='warning', position='top', timeout=12000, close_button='OK')

        ui.query('body').classes(add='intranet-pozadi', remove='prihlaseni-pozadi')

        if aktivni_tab != 'prehled' and aktivni_tab in dostupne_taby:
            app.storage.user['intranet_tab'] = aktivni_tab
        elif app.storage.user.get('intranet_tab') not in dostupne_taby:
            app.storage.user['intranet_tab'] = 'prehled'

        with ui.header(elevated=True).classes('bg-blue-800 items-center justify-between px-2 sm:px-4 py-2 flex-wrap gap-2'):
            with ui.row().classes('items-center gap-2 sm:gap-4'):
                ui.button(on_click=lambda: drawer.toggle(), icon='menu').props('flat color=white dense')
                ui.label('Moje JIPka').classes('text-xl sm:text-2xl font-bold')
            with ui.row().classes('items-center gap-2 sm:gap-4 w-full sm:w-auto justify-end'):
                ui.label(f'{user_name}').classes('text-sm sm:text-lg font-medium truncate max-w-[150px] sm:max-w-none')

                # ── ZVONEČEK ─────────────────────────────────────────────
                @ui.refreshable
                async def _notif_badge():
                    # DB dotaz ve vlákně — běží při každém načtení stránky a pak
                    # à 30 s per klient, nesmí blokovat event loop.
                    pocet = await asyncio.to_thread(intranet_notifikace.pocet_neprectenych, user_id)
                    if pocet > 0:
                        ui.badge(str(pocet), color='red').props('floating')

                # Seznam notifikací se načítá až při otevření zvonečku (a při
                # každém dalším otevření znovu) — page load ho nedotahuje
                # zbytečně a otevřený panel je vždy čerstvý.
                _notif_otevren = {'v': False}

                with ui.button(icon='notifications').props('flat round color=white dense'):
                    await _notif_badge()
                    with ui.menu().classes(
                        'w-96 p-0 overflow-hidden shadow-2xl rounded-2xl border border-gray-100'
                    ) as _notif_menu:
                        @ui.refreshable
                        async def _notif_panel():
                            if not _notif_otevren['v']:
                                return
                            with ui.row().classes('w-full justify-center py-6') as _nacitani:
                                ui.spinner('dots', size='2rem', color='primary')
                            notifs       = await asyncio.to_thread(intranet_notifikace.ziskej, user_id, 25)
                            _nacitani.delete()
                            neprectenych = sum(1 for n in notifs if not n['precteno'])

                            # záhlaví panelu
                            with ui.row().classes(
                                'w-full justify-between items-center px-4 py-3 '
                                'bg-gray-50 border-b border-gray-200'
                            ):
                                ui.label('Upozornění').classes('font-bold text-gray-800 text-sm')
                                if neprectenych > 0:
                                    async def _vse_precist():
                                        await asyncio.to_thread(
                                            intranet_notifikace.oznac_vse_precteno, user_id)
                                        _notif_badge.refresh()
                                        _notif_panel.refresh()
                                    ui.label('Označit vše jako přečtené').classes(
                                        'text-xs text-blue-500 hover:text-blue-700 '
                                        'cursor-pointer select-none'
                                    ).on('click', _vse_precist)

                            # obsah — prázdný stav
                            if not notifs:
                                with ui.column().classes('w-full items-center py-10 gap-2'):
                                    ui.label('🔔').classes('text-4xl')
                                    ui.label('Žádná upozornění').classes(
                                        'text-sm text-gray-400 font-medium')
                            else:
                                import datetime as _dtm
                                _now = _dtm.datetime.now()

                                def _rel_cas(dt):
                                    """Relativní čas: 'před 5 min', 'před 2 h',
                                    'včera 14:30', jinak datum."""
                                    if dt.date() == _now.date():
                                        _min = max(0, int((_now - dt).total_seconds() // 60))
                                        if _min < 1:
                                            return 'právě teď'
                                        if _min < 60:
                                            return f'před {_min} min'
                                        return f'před {_min // 60} h'
                                    if dt.date() == _now.date() - _dtm.timedelta(days=1):
                                        return f"včera {dt.strftime('%H:%M')}"
                                    return dt.strftime('%d.%m. %H:%M')

                                def _skupina(dt):
                                    if dt is None:
                                        return 'Starší'
                                    if dt.date() == _now.date():
                                        return 'Dnes'
                                    if dt.date() == _now.date() - _dtm.timedelta(days=1):
                                        return 'Včera'
                                    return 'Starší'

                                # světlé pozadí kolečka dle typu notifikace
                                BARVY_BG = {'success': '#dcfce7', 'error': '#fee2e2',
                                            'warning': '#ffedd5', 'info': '#dbeafe'}

                                _minula_skupina = None
                                with ui.scroll_area().classes('w-full').style('max-height:420px'):
                                    for n in notifs:
                                        typ     = n.get('typ', 'info')
                                        barva   = intranet_notifikace.BARVY.get(typ, '#3b82f6')
                                        bg_kruh = BARVY_BG.get(typ, '#dbeafe')
                                        cas_v   = n.get('created_at')
                                        cas_s   = _rel_cas(cas_v) if cas_v else ''

                                        # Emoji z textu vytáhneme do barevného
                                        # kolečka, text zůstane čistý. Když text
                                        # emoji nemá, spadne to na ikonu typu.
                                        text  = n['text']
                                        emoji = intranet_notifikace.IKONY.get(typ, 'ℹ️')
                                        _casti = text.split(' ', 1)
                                        if (len(_casti) == 2 and _casti[0]
                                                and not any(c.isalnum() for c in _casti[0])):
                                            emoji, text = _casti[0], _casti[1]

                                        skupina = _skupina(cas_v)
                                        if skupina != _minula_skupina:
                                            _minula_skupina = skupina
                                            ui.label(skupina).classes(
                                                'w-full px-4 pt-3 pb-1 text-[11px] '
                                                'font-semibold uppercase tracking-wider '
                                                'text-gray-400')

                                        async def _precist(nid=n['id']):
                                            await asyncio.to_thread(
                                                intranet_notifikace.oznac_precteno, nid, user_id)
                                            _notif_badge.refresh()
                                            _notif_panel.refresh()

                                        _bg = 'bg-blue-50/40' if not n['precteno'] else ''
                                        _prouzek = barva if not n['precteno'] else 'transparent'
                                        with ui.row().classes(
                                            f'w-full items-start gap-3 px-4 py-3 '
                                            f'border-b border-gray-100 {_bg} '
                                            f'hover:bg-gray-50 transition-colors cursor-default'
                                        ).style(f'border-left:3px solid {_prouzek}'):
                                            with ui.element('div').style(
                                                f'width:32px;height:32px;border-radius:50%;'
                                                f'background:{bg_kruh};flex-shrink:0;'
                                                f'display:flex;align-items:center;'
                                                f'justify-content:center'
                                            ):
                                                ui.label(emoji).style('font-size:15px')
                                            with ui.column().classes('flex-1 min-w-0 gap-0.5'):
                                                ui.label(text).classes(
                                                    'text-sm leading-snug ' + (
                                                        'text-gray-800 font-medium'
                                                        if not n['precteno']
                                                        else 'text-gray-600'))
                                                ui.label(cas_s).classes('text-xs text-gray-400')
                                            if not n['precteno']:
                                                ui.button(
                                                    icon='done', on_click=_precist
                                                ).props('flat round dense').classes(
                                                    'shrink-0 mt-1 text-gray-300 '
                                                    'hover:text-green-500'
                                                ).style('font-size:14px')

                        await _notif_panel()

                def _notif_pri_otevreni():
                    _notif_otevren['v'] = True
                    _notif_panel.refresh()
                _notif_menu.on('show', _notif_pri_otevreni)

                ui.timer(30, _notif_badge.refresh)
                # ─────────────────────────────────────────────────────────

                ui.button(icon='settings', on_click=lambda: app.storage.user.update({'intranet_tab': 'nastaveni'})) \
                    .props('flat round color=white dense') \
                    .tooltip('Osobní nastavení')

                def odhlasit():
                    intranet_session.odhlas_rucne(user_email, muj_token)
                    intranet_monitor.odeber_prihlaseni(user_email)
                    _ip_out = app.storage.user.get('login_ip') or intranet_logger.ziskej_klienta_info(client)[0]
                    _zar_out = app.storage.user.get('login_device') or ''
                    intranet_logger.log_activity(user_name, "Odhlášení", "Uživatel se odhlásil ze systému", ip=_ip_out, device=_zar_out)
                    _odhlas_vycisti_relaci()
                    ui.navigate.to('/')

                ui.button('Odhlásit', on_click=odhlasit).classes('bg-red-500 hover:bg-red-600 shadow-sm text-xs sm:text-sm px-2 sm:px-4 py-1')

        try:
            with ui.left_drawer(elevated=True, value=True).classes('bg-gray-50 border-r border-gray-200 overflow-x-hidden') as drawer:
                with ui.tabs().bind_value(app.storage.user, 'intranet_tab').props('vertical active-color="primary" indicator-color="primary"').classes('w-full text-left gap-2 pt-4 px-4') as tabs:
                    ui.tab('prehled', label='📊  Přehled').classes('justify-start text-lg text-gray-800')
                    ui.tab('helpdesk', label='🎧  Helpdesk').classes('hidden')

                    tab_dochazka = tab_vystup = tab_uzivatele = tab_mysql = tab_veletrh = tab_logy = tab_server = tab_finance = tab_znacky = tab_znacky_provoz = tab_narozeniny = tab_smeny = tab_komunikace = tab_planogram = tab_ochutnavky = tab_prod_akt = tab_ukolovnik = tab_vysledky = tab_sankce = tab_spolvecer = tab_vizitky = None

                    if ma_vse or "dochazka_admin" in vsechna_prava or "dochazka_export" in vsechna_prava or "dochazka_zadosti" in vsechna_prava or any(p.startswith('schvalovat_') for p in vsechna_prava) or any(p.startswith('hlavni_vedouci_') for p in vsechna_prava) or any(p.startswith('slozka_') for p in vsechna_prava):
                        tab_dochazka = ui.tab('dochazka', label='📅  Evidence absencí').classes('justify-start text-lg text-gray-800')

                    if has_finance:
                        tab_finance = ui.tab('finance', label='💼  Aprovia').classes('justify-start text-lg text-gray-800')

                        async def _nacti_finance_badge(tab=tab_finance):
                            pocet = await asyncio.get_running_loop().run_in_executor(
                                None, intranet_finance.ziskej_badge_pocet_rychle, user_id, user_name, vsechna_prava
                            )
                            if client.has_socket_connection and pocet > 0:
                                tab.props(f'label="💼  Aprovia ({pocet})"')

                        ui.timer(0.1, _nacti_finance_badge, once=True)

                    if ma_vse or "vystup_vse" in vsechna_prava or "vystup_osobni" in vsechna_prava:
                        tab_vystup = ui.tab('vystup', label='📈  Výstup Kvíz').classes('justify-start text-lg text-gray-800')
                    if (ma_vse or "veletrh_admin" in vsechna_prava or "veletrh_uzivatel" in vsechna_prava or "veletrh_komentator" in vsechna_prava or "veletrh_pristup" in vsechna_prava) and nastaveni.get('veletrh_zapnuty', True):
                        tab_veletrh = ui.tab('veletrh', label='🎪  Veletrh').classes('justify-start text-lg text-gray-800')
                    if (ma_vse or "znacky_uzivatel" in vsechna_prava or "znacky_spravce" in vsechna_prava) and nastaveni.get('znacky_zapnuty', True):
                        tab_znacky = ui.tab('znacky', label='🏷️  Privátní značky JIP').classes('justify-start text-lg text-gray-800')
                    if (ma_vse or "znacky_provoz_uzivatel" in vsechna_prava or "znacky_provoz_spravce" in vsechna_prava) and nastaveni.get('znacky_provoz_zapnuty', True):
                        tab_znacky_provoz = ui.tab('znacky_provoz', label='🏭  Hlas Provozu').classes('justify-start text-lg text-gray-800')
                    if 'prod_akt' in dostupne_taby:
                        tab_prod_akt = ui.tab('prod_akt', label='📋  Prodejní aktivity').classes('justify-start text-lg text-gray-800')
                    if ma_narozeniny:
                        tab_narozeniny = ui.tab('narozeniny', label='🎂  Narozeniny').classes('justify-start text-lg text-gray-800')
                    if 'smeny' in dostupne_taby:
                        tab_smeny = ui.tab('smeny', label='⌨️  Plánování směn').classes('justify-start text-lg text-gray-800')
                    if 'komunikace' in dostupne_taby:
                        tab_komunikace = ui.tab('komunikace', label='💬  Komunikační portál').classes('justify-start text-lg text-gray-800')
                    if 'planogram' in dostupne_taby:
                        tab_planogram = ui.tab('planogram', label='🚬  Plánogram tabáku').classes('justify-start text-lg text-gray-800')
                    if 'ochutnavky' in dostupne_taby:
                        tab_ochutnavky = ui.tab('ochutnavky', label='🍽️  Ochutnávky MO a CC').classes('justify-start text-lg text-gray-800')
                    if 'ukolovnik' in dostupne_taby:
                        tab_ukolovnik = ui.tab('ukolovnik', label='📋  Porady a úkoly').classes('justify-start text-lg text-gray-800')
                    if 'vysledky' in dostupne_taby:
                        tab_vysledky = ui.tab('vysledky', label='📊  Výsledky poboček').classes('justify-start text-lg text-gray-800')
                    if 'sankce' in dostupne_taby:
                        tab_sankce = ui.tab('sankce', label='⚖️  Sankce').classes('justify-start text-lg text-gray-800')
                    if 'spolvecer' in dostupne_taby:
                        tab_spolvecer = ui.tab('spolvecer', label='🎉  Společenský večer').classes('justify-start text-lg text-gray-800')
                    if 'vizitky' in dostupne_taby:
                        tab_vizitky = ui.tab('vizitky', label='🪪  Vizitky a podpisy').classes('justify-start text-lg text-gray-800')
                    if 'cenopripad' in dostupne_taby:
                        tab_cenopripad = ui.tab('cenopripad', label='🏷️  Cenopřípad').classes('justify-start text-lg text-gray-800')
                    if 'asm' in dostupne_taby:
                        tab_asm = ui.tab('asm', label='📝  Formuláře ASM').classes('justify-start text-lg text-gray-800')
                    if 'lupa' in dostupne_taby:
                        tab_lupa = ui.tab('lupa', label='🔍  Lupou na obchod').classes('justify-start text-lg text-gray-800')

                    if ma_vse or "uzivatele" in vsechna_prava:
                        tab_uzivatele = ui.tab('uzivatele', label='👥  Správa uživatelů').classes('justify-start text-lg text-gray-800')

                    if ma_vse or "admin_logy" in vsechna_prava:
                        tab_logy = ui.tab('logy', label='📜  Audit Log').classes('justify-start text-lg text-gray-800')
                    if ma_vse or "admin_server" in vsechna_prava:
                        tab_server = ui.tab('server', label='🖥️  Monitor serveru').classes('justify-start text-lg text-gray-800')

                    if ma_vse or "mysql" in vsechna_prava:
                        tab_mysql = ui.tab('mysql', label='🗄️  Nastavení portálu').classes('justify-start text-lg text-gray-800')

                with ui.column().classes('w-full px-4 pt-8 pb-6 gap-4 items-center'):
                    ui.image('/static/jip-logo.png').classes('w-32 opacity-100')

            with ui.column().classes('w-full min-h-screen p-2 sm:p-8'):
                # Veletrh klávesové zkratky — ui.keyboard musí být na úrovni stránky, ne uvnitř tab_panel
                from nicegui import events as _events
                def _veletrh_kbd(e: _events.KeyEventArguments):
                    if not e.action.keydown:
                        return
                    if app.storage.user.get('intranet_tab') != 'veletrh':
                        return
                    key_raw = str(e.key).lower()
                    if e.modifiers.ctrl and not e.modifiers.alt and key_raw == 'z':
                        intranet_veletrh.dispatch_kbd(user_id, 'ctrl+z')
                        return
                    if e.modifiers.ctrl and not e.modifiers.alt and key_raw in ('c', 'v'):
                        intranet_veletrh.dispatch_kbd(user_id, 'ctrl+' + key_raw)
                        return
                    if e.modifiers.ctrl or e.modifiers.alt or e.modifiers.shift:
                        return
                    intranet_veletrh.dispatch_kbd(user_id, key_raw)
                ui.keyboard(on_key=_veletrh_kbd, ignore=['input', 'select', 'textarea'])

                # ── Lazy loading tab panelů ───────────────────────────────────────
                # Okamžitě se vykreslí jen přehled a tab, na kterém uživatel byl naposled.
                # Každý další tab se vykreslí až při prvním kliknutí → výrazně kratší přihlášení.
                _aktivni_start  = app.storage.user.get('intranet_tab', 'prehled')
                _rendered_tabs: set = set()
                _tab_containers: dict = {}  # tab_name → ui.column placeholder

                # Mapa tab → render funkce (jen existující taby)
                _RENDER_FNS: dict = {
                    'nastaveni':    lambda: intranet_obsah.vykresli_osobni_nastaveni(user_id, user_email, user_name),
                    'helpdesk':     lambda: intranet_helpdesk.vykresli_helpdesk(user_id, user_name, vsechna_prava),
                }
                if tab_dochazka:    _RENDER_FNS['dochazka']     = lambda: intranet_obsah.vykresli_dochazku(user_id, user_name, vsechna_prava)
                if tab_finance:     _RENDER_FNS['finance']       = lambda: intranet_finance.vykresli_finance(user_id, user_name, vsechna_prava)
                if tab_vystup:      _RENDER_FNS['vystup']        = lambda: intranet_kviz.vykresli_vystup_kviz(user_name, vsechna_prava)
                if tab_uzivatele:   _RENDER_FNS['uzivatele']     = lambda: intranet_obsah.vykresli_spravu_uzivatelu(user_email, user_name, vsechna_prava)
                if tab_mysql:       _RENDER_FNS['mysql']         = lambda: intranet_nastaveni.vykresli_nastaveni_portalu(user_name)
                if tab_veletrh:     _RENDER_FNS['veletrh']       = lambda: intranet_veletrh.vykresli_veletrh(user_id, user_name, vsechna_prava)
                if tab_znacky:      _RENDER_FNS['znacky']        = lambda: znackyjip.vykresli_znacky(user_id, user_name, vsechna_prava)
                if tab_znacky_provoz: _RENDER_FNS['znacky_provoz'] = lambda: znacky_provoz.vykresli(user_id, user_name, vsechna_prava)
                if tab_prod_akt:    _RENDER_FNS['prod_akt']      = lambda: prodejni_aktivity.vykresli(user_id, user_name, vsechna_prava)
                if tab_narozeniny:  _RENDER_FNS['narozeniny']    = lambda: intranet_narozeniny.vykresli_narozeniny(user_id, user_name, vsechna_prava)
                if 'smeny'      in dostupne_taby: _RENDER_FNS['smeny']      = lambda: intranet_smeny.vykresli_smeny(user_id, user_name, vsechna_prava)
                if 'komunikace' in dostupne_taby: _RENDER_FNS['komunikace'] = lambda: intranet_komunikace.vykresli_komunikaci(user_id, user_name, vsechna_prava)
                if 'planogram'  in dostupne_taby: _RENDER_FNS['planogram']  = lambda: intranet_planogram.vykresli_planogram(user_id, user_name, vsechna_prava)
                if 'ochutnavky' in dostupne_taby: _RENDER_FNS['ochutnavky'] = lambda: intranet_ochutnavky.vykresli_ochutnavky(user_id, user_name, vsechna_prava)
                if 'ukolovnik'  in dostupne_taby: _RENDER_FNS['ukolovnik']  = lambda: intranet_ukolovnik.vykresli_ukolovnik(user_id, user_name, vsechna_prava)
                if 'vysledky'   in dostupne_taby: _RENDER_FNS['vysledky']   = lambda: intranet_vysledky.vykresli_vysledky(user_id, user_name, vsechna_prava)
                if 'sankce'     in dostupne_taby:
                    _RENDER_FNS['sankce']     = lambda: intranet_sankce.vykresli_sankce(user_id, user_name, vsechna_prava)
                    # Globální handler mazání řádků registrujeme JIŽ TEĎ (při stavbě
                    # stránky, před odesláním do prohlížeče). ui.on() věší listener na
                    # persistentní client.layout; kdyby se to dělalo až při líném
                    # renderu tabu (background task, po flushi), klient hlásí
                    # „Event listeners changed after initial definition".
                    intranet_sankce._zaregistruj_mazani_radku(user_name, vsechna_prava)
                if 'spolvecer'  in dostupne_taby: _RENDER_FNS['spolvecer']  = lambda: intranet_spolvecer.vykresli(user_id, user_name, vsechna_prava)
                if 'vizitky'    in dostupne_taby: _RENDER_FNS['vizitky']    = lambda: intranet_vizitky.vykresli_vizitky(user_id, user_name, user_email, vsechna_prava)
                if 'cenopripad' in dostupne_taby: _RENDER_FNS['cenopripad'] = lambda: intranet_cenopripad.vykresli_cenopripad(user_id, user_name, vsechna_prava)
                if 'asm'        in dostupne_taby: _RENDER_FNS['asm']        = lambda: intranet_asm.vykresli_asm(user_id, user_name, vsechna_prava)
                if 'lupa'       in dostupne_taby: _RENDER_FNS['lupa']       = lambda: intranet_lupa.vykresli_lupa(user_id, user_name, vsechna_prava)

                if tab_logy:        _RENDER_FNS['logy']          = lambda: intranet_logger.vykresli_logy(user_name, vsechna_prava)
                if tab_server:      _RENDER_FNS['server']        = lambda: intranet_monitor.vykresli_monitor(vsechna_prava)

                def _vykresli_tab(tab_name: str):
                    """Zavolá render funkci pro daný tab a označí ho jako vykreslený.

                    Async render funkce (moduly s DB dotazy přes to_thread, např.
                    ukolovník) vrací coroutine — dokreslí se na pozadí; kontejner
                    tabu je nutné uvnitř tasku znovu aktivovat (slot stack je
                    per-task)."""
                    if tab_name in _rendered_tabs:
                        return
                    _rendered_tabs.add(tab_name)
                    fn = _RENDER_FNS.get(tab_name)
                    if not fn or tab_name not in _tab_containers:
                        return
                    kontejner = _tab_containers[tab_name]
                    with kontejner:
                        vysledek = fn()
                    if asyncio.iscoroutine(vysledek):
                        async def _dokresli(v=vysledek, k=kontejner):
                            with k:
                                await v
                        background_tasks.create(_dokresli())

                with ui.tab_panels(tabs, value=_aktivni_start).bind_value(app.storage.user, 'intranet_tab').classes('w-full bg-transparent p-0'):
                    # Přehled — vždy okamžitě
                    with ui.tab_panel('prehled'):
                        intranet_obsah.vykresli_prehled(user_id, user_name, vsechna_prava)
                    _rendered_tabs.add('prehled')

                    # Ostatní taby — placeholder; obsah se doplní při prvním otevření
                    for _tn in list(_RENDER_FNS.keys()):
                        with ui.tab_panel(_tn):
                            _tab_containers[_tn] = ui.column().classes('w-full')

                # Kontext stránky zachycený při buildu — obsahuje request contextvar,
                # bez kterého app.storage.user uvnitř render funkcí spadne.
                # Handler změny tabu totiž může běžet MIMO UI kontext (změna přišlá
                # z bindingu, např. tlačítko ⚙️ zapisující do app.storage.user).
                _page_ctx = contextvars.copy_context()

                def _vykresli_tab_v_kontextu(tab_name: str):
                    try:
                        _page_ctx.run(_vykresli_tab, tab_name)
                    except RuntimeError:
                        # kontext je právě aktivní (nemělo by nastat) → přímé volání
                        _vykresli_tab(tab_name)

                # Okamžitě vykreslíme i ten tab, na kterém uživatel byl naposled
                # (běžíme uvnitř page builderu — kontext je tu přirozeně)
                if _aktivni_start != 'prehled':
                    _vykresli_tab(_aktivni_start)

                # Lazy render bez pollingu: tab se dokreslí při změně hodnoty tabů.
                # Zachytí kliknutí na tab i programové přepnutí přes binding.
                tabs.on_value_change(lambda e: _vykresli_tab_v_kontextu(e.value))

            # Automatické odhlášení po nečinnosti: výchozí 10 minut natvrdo.
            # Individuální nastavení může čas změnit, ale nikdy nevypnout (0/None → 10 min).
            osobni_ao = await asyncio.get_running_loop().run_in_executor(None, intranet_data.ziskej_osobni_auto_odhlaseni, user_id) if user_id else None
            minuty_necinnost = int(osobni_ao) if osobni_ao and int(osobni_ao) > 0 else 10
            ui.run_javascript(f'(function(){{var t={minuty_necinnost*60000};var timer;function r(){{clearTimeout(timer);timer=setTimeout(function(){{window.location.href="/logout";}},t);}}["mousemove","mousedown","keypress","scroll","touchstart","click"].forEach(function(e){{document.addEventListener(e,r,true);}});r();}})()')

            # Celé UI je vykresleno — schovat preloader (pokud byl zobrazen)
            if _je_prihlasen:
                ui.run_javascript('if(typeof _jipRemovePreloader==="function") _jipRemovePreloader();')
        except Exception as e:
            # I při chybě preloader schováme, aby nezůstal viset
            if _je_prihlasen:
                try:
                    ui.run_javascript('if(typeof _jipRemovePreloader==="function") _jipRemovePreloader();')
                except Exception:
                    pass

@ui.page('/')
async def route_prehled(client: Client):
    await vykresli_kompletni_intranet(client, 'prehled')

@ui.page('/veletrh')
async def route_veletrh(client: Client):
    await vykresli_kompletni_intranet(client, 'veletrh')

@ui.page('/finance')
async def route_finance(client: Client):
    await vykresli_kompletni_intranet(client, 'finance')

@ui.page('/dochazka')
async def route_dochazka(client: Client):
    await vykresli_kompletni_intranet(client, 'dochazka')

@ui.page('/logy')
async def route_logy(client: Client):
    await vykresli_kompletni_intranet(client, 'logy')

@ui.page('/server')
async def route_server(client: Client):
    await vykresli_kompletni_intranet(client, 'server')

@ui.page('/helpdesk')
async def route_helpdesk(client: Client):
    await vykresli_kompletni_intranet(client, 'helpdesk')

@ui.page('/smeny')
async def route_smeny(client: Client):
    await vykresli_kompletni_intranet(client, 'smeny')

@ui.page('/komunikace')
async def route_komunikace(client: Client):
    await vykresli_kompletni_intranet(client, 'komunikace')

@ui.page('/planogram')
async def route_planogram(client: Client):
    await vykresli_kompletni_intranet(client, 'planogram')

@ui.page('/ochutnavky')
async def route_ochutnavky(client: Client):
    await vykresli_kompletni_intranet(client, 'ochutnavky')

@ui.page('/znacky')
async def route_znacky(client: Client):
    await vykresli_kompletni_intranet(client, 'znacky')

@ui.page('/provoz')
async def route_znacky_provoz(client: Client):
    await vykresli_kompletni_intranet(client, 'znacky_provoz')

@ui.page('/prod_akt')
async def route_prod_akt(client: Client):
    await vykresli_kompletni_intranet(client, 'prod_akt')

@ui.page('/sankce')
async def route_sankce(client: Client):
    await vykresli_kompletni_intranet(client, 'sankce')

@ui.page('/spolvecer')
async def route_spolvecer(client: Client):
    await vykresli_kompletni_intranet(client, 'spolvecer')

@ui.page('/vizitky')
async def route_vizitky(client: Client):
    await vykresli_kompletni_intranet(client, 'vizitky')

@ui.page('/cenopripad')
async def route_cenopripad(client: Client):
    await vykresli_kompletni_intranet(client, 'cenopripad')

@ui.page('/lupa')
async def route_lupa(client: Client):
    await vykresli_kompletni_intranet(client, 'lupa')

@ui.page('/asm')
async def route_asm(client: Client, pripad: str = ''):
    # Deep-link z e-mailu (/asm?pripad=<id>) → po vykreslení se otevře rovnou detail případu.
    if pripad:
        try:
            app.storage.user['asm_deep_pripad'] = int(pripad)
        except (TypeError, ValueError):
            pass
    await vykresli_kompletni_intranet(client, 'asm')

@ui.page('/logout')
def route_logout(client: Client):
    try:
        user_email = str(app.storage.user.get('user_email', '')).lower()
        user_name = app.storage.user.get('user_name') or 'Neznámý'
        muj_token = app.storage.user.get('login_token')
        intranet_session.odhlas_rucne(user_email, muj_token)
        if user_name and app.storage.user.get('user_id'):
            intranet_monitor.odeber_prihlaseni(user_email)
            _ip_to = app.storage.user.get('login_ip') or intranet_logger.ziskej_klienta_info(client)[0]
            _zar_to = app.storage.user.get('login_device') or ''
            intranet_logger.log_activity(user_name, "Odhlášení", "Automatické odhlášení z důvodu nečinnosti", ip=_ip_to, device=_zar_to)
        _odhlas_vycisti_relaci()
        app.storage.user['odhlaseni_necinnost'] = True
    except Exception:
        pass
    ui.navigate.to('/')

if __name__ in {"__main__", "__mp_main__"}:
    try:
        if intranet_data.nacti_mysql().get("enabled"):
            intranet_data.get_db_connection()
    except Exception:
        pass
