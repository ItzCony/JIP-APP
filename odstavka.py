from nicegui import ui, app

# Odkomentujte, pokud byste chtěli použít reálné logo JIP místo ikony
# app.add_static_files('/static', '.')

# Z-5: Route '/' definována pouze při přímém spuštění tohoto souboru,
# aby nedošlo ke konfliktu s routami v intranet.py při importu.
if __name__ in {"__main__", "__mp_main__"}:
    @ui.page('/')
    def odstavka_stranka():
        ui.page_title('Odstávka systému | Moje JIPka')

        # Stylování pozadí a horní červené linky
        ui.add_head_html('''
            <style>
                body {
                    background-color: #f3f4f6;
                    /* Moderní tečkované pozadí */
                    background-image: radial-gradient(#d1d5db 1px, transparent 1px);
                    background-size: 24px 24px;
                    margin: 0;
                }
                .jip-card {
                    /* JIP Červená linka nahoře */
                    border-top: 6px solid #E30613;
                }
            </style>
        ''')

        with ui.column().classes('w-full h-screen items-center justify-center p-4'):

            with ui.card().classes('w-full max-w-3xl p-8 sm:p-12 items-center text-center shadow-2xl rounded-2xl bg-white jip-card'):

                # Zástupná ikona údržby (zde můžete dát ui.image('/static/logo.png').classes('w-48 mb-4') )
                ui.icon('engineering', size='6rem', color='blue-grey-2').classes('mb-4')

                # Nadpis (JIP Tmavá barva)
                ui.label('Probíhá údržba systému').classes('text-4xl sm:text-5xl font-extrabold mb-4 tracking-tight text-[#2A3547]')

                # Podnadpis
                ui.label(
                    'Omlouváme se, ale firemní portál je momentálně nedostupný '
                    'z důvodu plánované aktualizace a vylepšování sítě.'
                ).classes('text-lg sm:text-xl text-gray-500 mb-8 max-w-2xl leading-relaxed')

                # Odhadovaný čas (Červený štítek)
                with ui.row().classes('bg-red-50 text-[#E30613] px-6 py-3 rounded-xl font-bold mb-8 items-center gap-2 border border-red-100'):
                    ui.icon('schedule', size='sm')
                    ui.label('Předpokládaný návrat: Již brzy')

                # Kontaktní informace
                with ui.row().classes('gap-4 justify-center w-full flex-wrap'):
                    # Box 1: E-mail
                    with ui.column().classes('items-center p-6 bg-gray-50 rounded-xl flex-1 min-w-[200px] border border-gray-100 transition-colors hover:bg-gray-100 shadow-sm'):
                        ui.icon('email', size='md', color='grey-6').classes('mb-2')
                        ui.label('Technická podpora').classes('font-bold text-[#2A3547]')
                        ui.label('mojejipka@jip-napoje.cz').classes('text-sm text-[#E30613] font-bold mt-1')

                    # Box 2: Telefon
                    with ui.column().classes('items-center p-6 bg-gray-50 rounded-xl flex-1 min-w-[200px] border border-gray-100 transition-colors hover:bg-gray-100 shadow-sm'):
                        ui.icon('phone', size='md', color='grey-6').classes('mb-2')
                        ui.label('Nouzový Helpdesk').classes('font-bold text-[#2A3547]')
                        ui.label('+420 800 111 222').classes('text-sm text-[#E30613] font-bold mt-1')

                # Patička
                ui.label('Děkujeme za pochopení a vaši trpělivost.').classes('mt-10 text-xs sm:text-sm font-bold text-gray-400 uppercase tracking-widest')

    ui.run(title='Odstávka | Moje JIPka', port=8080)
