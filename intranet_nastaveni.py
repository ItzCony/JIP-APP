from nicegui import ui, app
import intranet_data
import intranet_logger
import asyncio
import datetime
import re


def vykresli_nastaveni_portalu(user_name):
    with ui.column().classes('w-full px-4 md:px-8 xl:px-12 py-6 bg-gray-50/30 min-h-screen gap-6'):

        # --- HLAVIČKA ---
        with ui.row().classes('w-full justify-between items-end border-b border-gray-200 pb-4'):
            with ui.column().classes('gap-1'):
                ui.label('Konzole Administrátora').classes('text-4xl font-black text-gray-900 tracking-tight')
                ui.label('Globální správa a konfigurace celého portálu').classes('text-lg text-gray-500 font-bold')
            ui.icon('settings_applications', size='3rem', color='gray-300')

        # --- ZÁLOŽKY ---
        with ui.tabs().classes('w-full') as tabs:
            tab_moduly      = ui.tab('Moduly',      icon='view_module')
            tab_narozeniny  = ui.tab('Narozeniny',  icon='cake')
            tab_email       = ui.tab('E-mail',      icon='email')

        with ui.tab_panels(tabs, value=tab_moduly).classes('w-full'):

            # =========================================================
            # ZÁLOŽKA 2: MODULY
            # =========================================================
            with ui.tab_panel(tab_moduly):
                with ui.column().classes('w-full gap-8'):

                    # --- Viditelnost modulů ---
                    with ui.card().classes('w-full p-6 xl:p-8 shadow-sm bg-white rounded-2xl border-l-[10px] border-purple-500 hover:shadow-md transition-shadow'):
                        nastaveni_mod = intranet_data.nacti_nastaveni_intranetu()
                        with ui.column().classes('gap-1 mb-6'):
                            ui.label('Viditelnost modulů a globální funkce').classes('text-2xl font-bold text-gray-800')
                            ui.label('Tyto přepínače okamžitě skryjí nebo zobrazí dané moduly v levém menu pro celou firmu.').classes('text-sm text-gray-500')

                        def toggle_nast(klic, e, nazev_log):
                            n = intranet_data.nacti_nastaveni_intranetu()
                            n[klic] = e.value
                            intranet_data.uloz_nastaveni_intranetu(n)
                            intranet_logger.log_activity(user_name, "Přepnutí modulu", f"{nazev_log}: {'ZAPNUTO' if e.value else 'VYPNUTO'}")
                            try:
                                nova_verze = app.storage.general.get('nastaveni_verze', 0) + 1
                                app.storage.general['nastaveni_verze'] = nova_verze
                                # Uložíme vlastní verzi — timer na naší stránce nás pak nepřesměruje
                                app.storage.user['nastaveni_verze_vlastni'] = nova_verze
                            except Exception:
                                pass
                            ui.notify('Uloženo. Změna se projeví všem uživatelům do 10 sekund.', type='positive')

                        with ui.grid(columns=1).classes('w-full gap-4 md:grid-cols-2 xl:grid-cols-4'):
                            ui.switch('Modul Aprovia (Finance)', value=nastaveni_mod.get('finance_zapnuty', True), on_change=lambda e: toggle_nast('finance_zapnuty', e, 'Aprovia')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800')
                            ui.switch('Modul Zkouškový Kvíz', value=nastaveni_mod.get('kviz_zapnuty', True), on_change=lambda e: toggle_nast('kviz_zapnuty', e, 'Kvíz')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800')
                            ui.switch('Modul Veletrh', value=nastaveni_mod.get('veletrh_zapnuty', True), on_change=lambda e: toggle_nast('veletrh_zapnuty', e, 'Veletrh')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800')
                            ui.switch('Modul Značky JIP', value=nastaveni_mod.get('znacky_zapnuty', True), on_change=lambda e: toggle_nast('znacky_zapnuty', e, 'Značky JIP')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800')
                            ui.switch('E-maily: Modul Značky JIP', value=nastaveni_mod.get('znacky_emaily_zapnuty', True), on_change=lambda e: toggle_nast('znacky_emaily_zapnuty', e, 'E-maily Značky JIP')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('Modul Plánování směn', value=nastaveni_mod.get('smeny_zapnuty', True), on_change=lambda e: toggle_nast('smeny_zapnuty', e, 'Plánování směn')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🚬 Modul Plánogram tabáku', value=nastaveni_mod.get('planogram_zapnuty', True), on_change=lambda e: toggle_nast('planogram_zapnuty', e, 'Plánogram tabáku')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🍽️ Modul Ochutnávky MO a CC', value=nastaveni_mod.get('ochutnavky_zapnuty', True), on_change=lambda e: toggle_nast('ochutnavky_zapnuty', e, 'Ochutnávky MO a CC')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('⚖️ Modul Sankce', value=nastaveni_mod.get('sankce_zapnuty', True), on_change=lambda e: toggle_nast('sankce_zapnuty', e, 'Sankce')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🎉 Modul Společenský večer', value=nastaveni_mod.get('spolvecer_zapnuty', True), on_change=lambda e: toggle_nast('spolvecer_zapnuty', e, 'Společenský večer')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🪪 Modul Vizitky a podpisy', value=nastaveni_mod.get('vizitky_zapnuty', True), on_change=lambda e: toggle_nast('vizitky_zapnuty', e, 'Vizitky a podpisy')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🏷️ Modul Cenopřípad', value=nastaveni_mod.get('cenopripad_zapnuty', True), on_change=lambda e: toggle_nast('cenopripad_zapnuty', e, 'Cenopřípad')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('📝 Modul Formuláře ASM', value=nastaveni_mod.get('asm_zapnuty', True), on_change=lambda e: toggle_nast('asm_zapnuty', e, 'Formuláře ASM')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🔍 Modul Lupou na obchod', value=nastaveni_mod.get('lupa_zapnuty', True), on_change=lambda e: toggle_nast('lupa_zapnuty', e, 'Lupou na obchod')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('🗓️ Modul Schůzky s vedoucími', value=nastaveni_mod.get('schuzky_zapnuty', True), on_change=lambda e: toggle_nast('schuzky_zapnuty', e, 'Schůzky s vedoucími')).classes('w-full p-4 bg-purple-50 rounded-xl border border-purple-100 font-bold text-gray-800 xl:col-span-2')
                            ui.switch('⏰ Přesčasy (Evidence absencí)', value=nastaveni_mod.get('presczasy_zapnuty', True), on_change=lambda e: toggle_nast('presczasy_zapnuty', e, 'Přesčasy')).classes('w-full p-4 bg-orange-50 rounded-xl border border-orange-100 font-bold text-gray-800 xl:col-span-4')



            # =========================================================
            # ZÁLOŽKA 3: NAROZENINY
            # =========================================================
            with ui.tab_panel(tab_narozeniny):
                # datetime je již importováno na začátku souboru
                _dt_nar = datetime
                with ui.column().classes('w-full gap-8'):

                    # --- Modul on/off ---
                    with ui.card().classes('w-full p-6 xl:p-8 shadow-sm bg-white rounded-2xl border-l-[10px] border-pink-400 hover:shadow-md transition-shadow'):
                        nast_nar = intranet_data.nacti_nastaveni_intranetu()
                        with ui.row().classes('w-full justify-between items-center mb-4'):
                            with ui.column().classes('gap-1'):
                                ui.label('Modul Narozeniny').classes('text-2xl font-bold text-gray-800')
                                ui.label(
                                    'Vedoucí oddělení (právo „Hlavní vedoucí: Oddělení") vidí narozeniny svých lidí. '
                                    'Právo „Narozeniny – Všechna oddělení" zpřístupní celou firmu.'
                                ).classes('text-sm text-gray-500')

                            def toggle_narozeniny_modul(e):
                                n = intranet_data.nacti_nastaveni_intranetu()
                                n['narozeniny_zapnuty'] = e.value
                                intranet_data.uloz_nastaveni_intranetu(n)
                                intranet_logger.log_activity(user_name, 'Narozeniny', f"Modul {'ZAPNUT' if e.value else 'VYPNUT'}")
                                ui.notify('Uloženo. Projeví se po obnovení stránky.', type='info')

                            ui.switch(
                                value=nast_nar.get('narozeniny_zapnuty', True),
                                on_change=toggle_narozeniny_modul
                            ).props('color=pink').classes('mt-1')

                    # --- E-mailová přání ---
                    with ui.card().classes('w-full p-6 xl:p-8 shadow-sm bg-white rounded-2xl border-l-[10px] border-pink-300 hover:shadow-md transition-shadow'):
                        nast_nar2 = intranet_data.nacti_nastaveni_intranetu()

                        with ui.row().classes('w-full justify-between items-center mb-6'):
                            with ui.column().classes('gap-1'):
                                ui.label('E-mailová přání').classes('text-2xl font-bold text-gray-800')
                                ui.label(
                                    'V zadaný čas budou narozeninářům automaticky odeslána přání na jejich e-mail. '
                                    'Dostupné proměnné pro personalizaci: {jmeno}, {prijmeni}, {jmeno_cele}'
                                ).classes('text-sm text-gray-500')

                            def toggle_narozeniny_email(e):
                                n = intranet_data.nacti_nastaveni_intranetu()
                                n['narozeniny_email_zapnuty'] = e.value
                                intranet_data.uloz_nastaveni_intranetu(n)
                                intranet_logger.log_activity(user_name, 'Narozeniny', f"E-maily {'ZAPNUTY' if e.value else 'VYPNUTY'}")
                                ui.notify('Uloženo.', type='info')

                            ui.switch(
                                'ODESÍLAT PŘÁNÍ',
                                value=nast_nar2.get('narozeniny_email_zapnuty', True),
                                on_change=toggle_narozeniny_email
                            ).props('color=pink').classes('font-bold text-pink-700 bg-pink-50 px-4 py-2 rounded-xl border border-pink-100')

                        inp_cas = ui.input(
                            'Čas odeslání (HH:MM)',
                            value=nast_nar2.get('narozeniny_email_cas', '09:00')
                        ).classes('w-full max-w-xs mb-6').props('outlined mask="##:##" fill-mask')

                        inp_predmet = ui.input(
                            'Předmět e-mailu',
                            value=nast_nar2.get('narozeniny_email_predmet', 'Všechno nejlepší k narozeninám! 🎂')
                        ).classes('w-full mb-4').props('outlined')

                        inp_text = ui.textarea(
                            'Text e-mailu',
                            value=nast_nar2.get(
                                'narozeniny_email_text',
                                'Milý/á {jmeno},\n\n'
                                'v tento zvláštní den ti přejeme vše nejlepší k narozeninám! 🎂\n'
                                'Přejeme ti pevné zdraví, spoustu štěstí a mnoho krásných chvil.\n\n'
                                'S přáním vše nejlepšího,\n'
                                'Váš tým'
                            )
                        ).classes('w-full mb-6').props('outlined rows=8')

                        def uloz_narozeniny_email():
                            cas_val = inp_cas.value.strip()
                            try:
                                _dt_nar.datetime.strptime(cas_val, '%H:%M')
                            except ValueError:
                                return ui.notify('Neplatný formát času! Použijte HH:MM (např. 09:00)', type='negative')
                            n = intranet_data.nacti_nastaveni_intranetu()
                            n['narozeniny_email_cas'] = cas_val
                            n['narozeniny_email_predmet'] = inp_predmet.value
                            n['narozeniny_email_text'] = inp_text.value
                            intranet_data.uloz_nastaveni_intranetu(n)
                            intranet_logger.log_activity(user_name, 'Narozeniny', 'Uloženo nastavení e-mailů')
                            ui.notify('Nastavení narozenin uloženo.', type='positive')

                        async def otestovat_narozeniny():
                            uloz_narozeniny_email()
                            nastaveni_test = intranet_data.nacti_nastaveni_intranetu()
                            ui.notify('Odesílám testovací přání... Zkontrolujte terminál.', type='info', icon='hourglass_empty')
                            import intranet_narozeniny as _nar
                            dnes_test = _dt_nar.date.today()
                            await asyncio.to_thread(_nar._odesli_narozeninove_emaily, dnes_test, nastaveni_test)
                            ui.notify('Hotovo — zkontrolujte terminál a e-mailové schránky.', type='positive')

                        with ui.row().classes('w-full justify-end gap-4 border-t border-gray-100 pt-6'):
                            ui.button('Testovat (odeslat dnes)', icon='send', on_click=otestovat_narozeniny).classes('bg-pink-500 hover:bg-pink-600 text-white font-bold px-8 h-12 shadow-sm rounded-xl')
                            ui.button('Uložit nastavení', icon='save', on_click=uloz_narozeniny_email).classes('bg-gray-800 hover:bg-black text-white font-bold px-8 h-12 shadow-sm rounded-xl')

                    # --- Upozornění na kulatiny ---
                    with ui.card().classes('w-full p-6 xl:p-8 shadow-sm bg-white rounded-2xl border-l-[10px] border-purple-400 hover:shadow-md transition-shadow'):
                        nast_kul = intranet_data.nacti_nastaveni_intranetu()

                        with ui.column().classes('gap-1 mb-5'):
                            ui.label('Upozornění na kulatiny').classes('text-2xl font-bold text-gray-800')
                            ui.label(
                                'E-mailové adresy, na které se zasílá upozornění na kulaté narozeniny (30, 40, 50 …). '
                                'Na tyto adresy chodí automatické upozornění 2 dny před termínem kulatin '
                                '(v čase odesílání přání) a předvyplní se i v dialogu ručního tlačítka '
                                '„Upozornit e-mailem", kde je lze ještě upravit či doplnit.'
                            ).classes('text-sm text-gray-500')

                        # Migrace: starý jednotlivý klíč → nový plurálový seznam
                        _kul_raw = nast_kul.get('narozeniny_kulatiny_emaily') or nast_kul.get('narozeniny_kulatiny_email', '')
                        _kul_vychozi = [c.strip() for c in re.split(r'[,;\s]+', str(_kul_raw).strip()) if c.strip()]

                        import intranet_narozeniny
                        sel_kulatiny_email = ui.select(
                            options=intranet_narozeniny._nabidka_adresatu(_kul_vychozi),
                            value=list(_kul_vychozi),
                            multiple=True,
                            with_input=True,
                            new_value_mode='add-unique',
                            label='Výchozí příjemci upozornění na kulatiny',
                        ).classes('w-full max-w-md').props('outlined use-chips')
                        sel_kulatiny_email.tooltip('Začněte psát jméno nebo e-mail — našeptávač nabídne uživatele. '
                                                   'Enter přidá adresáta (lze zadat i adresu mimo systém).')

                        def uloz_kulatiny_email():
                            adresati = [a.strip() for a in (sel_kulatiny_email.value or []) if a and a.strip()]
                            n = intranet_data.nacti_nastaveni_intranetu()
                            n['narozeniny_kulatiny_emaily'] = ', '.join(adresati)
                            # Zpětná kompatibilita: starý klíč nese první adresu
                            n['narozeniny_kulatiny_email'] = adresati[0] if adresati else ''
                            intranet_data.uloz_nastaveni_intranetu(n)
                            intranet_logger.log_activity(user_name, 'Narozeniny', f"Kulatiny – výchozí příjemci: {', '.join(adresati) or '(žádní)'}")
                            ui.notify('Uloženo.', type='positive')

                        with ui.row().classes('w-full justify-end border-t border-gray-100 pt-5 mt-2'):
                            ui.button('Uložit', icon='save', on_click=uloz_kulatiny_email).classes('bg-gray-800 hover:bg-black text-white font-bold px-8 h-12 shadow-sm rounded-xl')

            # =========================================================
            # ZÁLOŽKA 5: E-MAIL
            # =========================================================
            with ui.tab_panel(tab_email):
                with ui.column().classes('w-full gap-8'):

                    with ui.card().classes('w-full p-6 xl:p-8 shadow-sm bg-white rounded-2xl border-l-[10px] border-teal-500 hover:shadow-md transition-shadow'):
                        nastaveni_mail = intranet_data.nacti_nastaveni_intranetu()

                        with ui.row().classes('w-full justify-between items-center mb-6'):
                            with ui.column().classes('gap-1'):
                                ui.label('SMTP a IMAP Server (Notifikace)').classes('text-2xl font-bold text-gray-800')
                                ui.label('Konfigurace poštovního klienta pro automatické rozesílání schválených dovolených a faktur.').classes('text-sm text-gray-500')

                            def toggle_emaily(e):
                                n = intranet_data.nacti_nastaveni_intranetu(); n['emaily_zapnuty'] = e.value; intranet_data.uloz_nastaveni_intranetu(n)
                                intranet_logger.log_activity(user_name, "Systém", f"E-maily globálně {'ZAPNUTY' if e.value else 'VYPNUTY'}")
                                ui.notify('Stav e-mailů uložen.', type='info')
                            ui.switch('POVOLIT ODESÍLÁNÍ', value=nastaveni_mail.get('emaily_zapnuty', True), on_change=toggle_emaily).classes('font-black text-teal-700 bg-teal-50 px-4 py-2 rounded-xl border border-teal-100')

                        with ui.grid(columns=1).classes('w-full gap-6 md:grid-cols-2 lg:grid-cols-4 mb-6'):
                            smtp_server = ui.input('SMTP Server', value=nastaveni_mail.get('smtp_server', '')).classes('w-full bg-white').props('outlined')
                            smtp_port   = ui.number('SMTP Port', value=nastaveni_mail.get('smtp_port', 465)).classes('w-full bg-white').props('outlined')
                            smtp_user   = ui.input('Přihlašovací e-mail', value=nastaveni_mail.get('smtp_user', '')).classes('w-full bg-white').props('outlined')
                            smtp_pass   = ui.input('Heslo', password=True, value=nastaveni_mail.get('smtp_password', '')).classes('w-full bg-white').props('outlined')

                        with ui.row().classes('w-full items-center gap-4 mb-2'):
                            app_url_input = ui.input(
                                'URL aplikace (proklik v e-mailech)',
                                value=nastaveni_mail.get('app_url', ''),
                                placeholder='https://intranet.mojejipka.cz',
                            ).classes('flex-1 bg-white').props('outlined')
                            ui.label('Vyplňte adresu, na kterou budou odkazovat e-maily o absencích.').classes('text-xs text-gray-400')

                        def ulozit_email():
                            n = intranet_data.nacti_nastaveni_intranetu()
                            n['smtp_server'] = smtp_server.value; n['smtp_port'] = smtp_port.value; n['smtp_user'] = smtp_user.value; n['smtp_password'] = smtp_pass.value
                            n['app_url'] = app_url_input.value.strip()
                            intranet_data.uloz_nastaveni_intranetu(n)
                            intranet_logger.log_activity(user_name, "Systém", "Uloženo nové SMTP nastavení")
                            ui.notify('SMTP uloženo.', type='positive')

                        async def otestovat_spojeni():
                            if not smtp_user.value or not smtp_pass.value or not smtp_server.value: return ui.notify('Vyplňte všechny údaje!', type='warning')
                            ulozit_email()
                            ui.notify('Testuji odesílání... Zkontrolujte Server Terminál pro detaily.', type='info', icon='hourglass_empty')
                            def _send_test():
                                import intranet_emaily
                                return intranet_emaily.odesli_upozorneni_email(smtp_user.value, "✅ ÚSPĚCH: Test spojení z Intranetu", "Nastavení funguje.")
                            try:
                                if await asyncio.to_thread(_send_test): ui.notify('✅ E-mail úspěšně odeslán!', type='positive')
                                else: ui.notify('❌ Chyba při odesílání. Koukněte do logů.', type='negative')
                            except Exception as e: ui.notify(f'Chyba: {e}', type='negative')

                        with ui.row().classes('w-full justify-end gap-4 border-t border-gray-100 pt-6'):
                            ui.button('Otestovat spojení', icon='send', on_click=otestovat_spojeni).classes('bg-teal-600 hover:bg-teal-700 text-white font-bold px-8 h-12 shadow-sm rounded-xl')
                            ui.button('Uložit nastavení', icon='save', on_click=ulozit_email).classes('bg-gray-800 hover:bg-black text-white font-bold px-8 h-12 shadow-sm rounded-xl')
