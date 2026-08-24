from nicegui import ui
import intranet_data
import intranet_logger
import datetime
from intranet_ui_utils import refreshable_na_klienta

# =========================================================
# INICIALIZACE DATABÁZE PRO HELPDESK
# =========================================================
def inicializace_helpdesk_db():
    conn = intranet_data.get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor(dictionary=True)
        # Tabulka pro samotné tikety
        cur.execute("""
            CREATE TABLE IF NOT EXISTS helpdesk_tikety (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                jmeno_zadavatele VARCHAR(255),
                predmet VARCHAR(255),
                popis TEXT,
                stav VARCHAR(50) DEFAULT 'Nové',
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)

        # AKTUALIZACE DB: Přidání sloupce pro přiřazenou osobu
        cur.execute("SHOW COLUMNS FROM helpdesk_tikety LIKE 'prirazeno_jmeno'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE helpdesk_tikety ADD COLUMN prirazeno_jmeno VARCHAR(255) DEFAULT 'Všichni'")

        # Tabulka pro komentáře uvnitř tiketů
        cur.execute("""
            CREATE TABLE IF NOT EXISTS helpdesk_komentare (
                id INT AUTO_INCREMENT PRIMARY KEY,
                tiket_id INT,
                user_id INT,
                jmeno_autora VARCHAR(255),
                text TEXT,
                vytvoreno DATETIME DEFAULT CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Chyba při inicializaci DB Helpdesku: {e}")
    finally:
        conn.close()

# =========================================================
# HLAVNÍ VYKRESLOVACÍ FUNKCE
# =========================================================
@refreshable_na_klienta
def vykresli_helpdesk(user_id, user_name, vsechna_prava):
    inicializace_helpdesk_db()

    is_admin = 'vse' in vsechna_prava or 'admin_helpdesk' in vsechna_prava
    is_admin_delete = 'vse' in vsechna_prava or 'helpdesk_mazani' in vsechna_prava

    # Jeden SQL dotaz místo načtení všech uživatelů + kontroly pouze přímých práv
    admini_jmena = intranet_data.ziskej_uzivatele_s_pravem('admin_helpdesk', 'vse', pouze_jmena=True)
    admini_options = {'Všichni': 'Obecný požadavek'}
    for jmeno in admini_jmena:
        admini_options[jmeno] = jmeno

    with ui.row().classes('w-full items-center justify-between mb-8'):
        with ui.column().classes('gap-1'):
            ui.label('Helpdesk').classes('text-4xl font-extrabold text-gray-800')
            ui.label('Zde můžete hlásit technické problémy nebo podávat návrhy na zlepšení.').classes('text-gray-500 text-sm')

        def novy_tiket_dialog():
            with ui.dialog() as dlg, ui.card().classes('w-full max-w-2xl p-6 rounded-xl'):
                ui.label('Nový požadavek').classes('text-2xl font-bold mb-4 text-blue-800')

                priradit_komu = ui.select(admini_options, value='Všichni', label='Směřovat na (Řešitel)').classes('w-full mb-4 bg-white').props('outlined')
                predmet = ui.input('Předmět (stručně)').classes('w-full mb-4 bg-white').props('outlined')
                popis = ui.textarea('Detailní popis problému nebo návrhu').classes('w-full mb-6 bg-white').props('outlined rows=6')

                def odeslat():
                    if not predmet.value or not popis.value:
                        return ui.notify('Vyplňte prosím předmět i popis!', type='warning')

                    conn = intranet_data.get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("INSERT INTO helpdesk_tikety (user_id, jmeno_zadavatele, predmet, popis, prirazeno_jmeno) VALUES (%s, %s, %s, %s, %s)",
                                    (user_id, user_name, predmet.value.strip(), popis.value.strip(), priradit_komu.value))
                        conn.commit(); cur.close(); conn.close()

                        intranet_logger.log_activity(user_name, "Helpdesk", f"Vytvořen nový tiket (Pro: {priradit_komu.value}): {predmet.value[:30]}...")
                        ui.notify('Požadavek byl úspěšně odeslán.', type='positive', position='top')
                        dlg.close()
                        vykresli_helpdesk.refresh()
                    else:
                        ui.notify('Chyba databáze.', type='negative')

                with ui.row().classes('w-full justify-between'):
                    ui.button('Zrušit', on_click=dlg.close).classes('bg-gray-400 text-white font-bold px-6')
                    ui.button('Odeslat požadavek', on_click=odeslat).classes('bg-blue-600 text-white font-bold px-6 shadow-md')
            dlg.open()

        ui.button('Nový požadavek', icon='add_circle', on_click=novy_tiket_dialog).classes('bg-green-600 hover:bg-green-700 text-white font-bold h-12 px-6 shadow-md rounded-xl')

    # NAČTENÍ DAT
    conn = intranet_data.get_db_connection()
    tikety = []
    if conn:
        cur = conn.cursor(dictionary=True)
        if is_admin:
            cur.execute("SELECT * FROM helpdesk_tikety ORDER BY CASE WHEN stav='Nové' THEN 1 WHEN stav='Řeší se' THEN 2 WHEN stav='Vyřešeno' THEN 3 ELSE 4 END, vytvoreno DESC")
        else:
            cur.execute("SELECT * FROM helpdesk_tikety WHERE user_id = %s ORDER BY vytvoreno DESC", (user_id,))
        tikety = cur.fetchall()
        cur.close(); conn.close()

    if not tikety:
        with ui.card().classes('w-full p-12 items-center justify-center bg-gray-50 border border-gray-200 border-dashed rounded-xl'):
            ui.icon('support_agent', size='4rem', color='gray-400').classes('mb-4')
            ui.label('Zatím zde nejsou žádné požadavky.').classes('text-xl text-gray-500 font-bold')
        return

    # VYKRESLENÍ SEZNAMU TIKETŮ
    barvy_stavu = {
        'Nové': 'bg-red-100 text-red-800 border-red-300',
        'Řeší se': 'bg-orange-100 text-orange-800 border-orange-300',
        'Vyřešeno': 'bg-green-100 text-green-800 border-green-300',
        'Uzavřeno': 'bg-gray-200 text-gray-700 border-gray-400'
    }

    def detail_tiketu(t):
        with ui.dialog() as dlg, ui.card().classes('w-full max-w-4xl p-0 rounded-xl overflow-hidden bg-gray-100 max-h-[90vh] flex flex-col'):
            # Hlavička tiketu
            with ui.column().classes('w-full p-6 bg-white border-b border-gray-200 shrink-0 shadow-sm z-10'):
                with ui.row().classes('w-full justify-between items-start mb-2'):
                    with ui.column().classes('gap-0 flex-1'):
                        ui.label(t['predmet']).classes('text-2xl font-black text-gray-800 line-clamp-2')

                        prirazeno_komu = t.get('prirazeno_jmeno') or 'Všichni'
                        with ui.row().classes('items-center gap-2 mt-1'):
                            ui.label('Zodpovídá:').classes('text-xs text-gray-500 uppercase font-bold')

                            if is_admin:
                                def zmenit_resitele(e):
                                    c = intranet_data.get_db_connection()
                                    cur = c.cursor()
                                    cur.execute("UPDATE helpdesk_tikety SET prirazeno_jmeno = %s WHERE id = %s", (e.value, t['id']))
                                    c.commit(); cur.close(); c.close()
                                    intranet_logger.log_activity(user_name, "Helpdesk", f"Tiket #{t['id']} přeřazen na: {e.value}")
                                    ui.notify(f'Přeřazeno na: {e.value}', type='info')
                                    vykresli_helpdesk.refresh()

                                ui.select(admini_options, value=prirazeno_komu, on_change=zmenit_resitele).classes('w-48 bg-white').props('dense outlined rounded size=sm')
                            else:
                                ui.label(prirazeno_komu).classes('text-sm text-blue-600 font-bold')

                    b_stav = barvy_stavu.get(t['stav'], 'bg-gray-100 text-gray-800 border-gray-300')
                    ui.label(t['stav']).classes(f'px-3 py-1 rounded-full text-xs font-bold border uppercase tracking-wider mt-1 {b_stav}')

                with ui.row().classes('w-full gap-4 text-sm text-gray-500 mb-4 mt-2'):
                    ui.label(f"Zadal(a): {t['jmeno_zadavatele']}").classes('font-bold')
                    ui.label(f"Vytvořeno: {t['vytvoreno'].strftime('%d.%m.%Y %H:%M')}")

                ui.label('Zadání požadavku:').classes('font-bold text-gray-700 text-xs uppercase')
                ui.label(t['popis']).classes('text-gray-800 bg-gray-50 p-4 rounded-lg border border-gray-100 mt-1 whitespace-pre-wrap')

                # Tlačítka pro adminy (Změna stavu + Mazání)
                if is_admin or is_admin_delete:
                    with ui.row().classes('w-full gap-2 mt-4 pt-4 border-t border-gray-100 items-center'):

                        if is_admin:
                            ui.label('Změnit stav:').classes('font-bold text-gray-600 text-sm mr-2')
                            def zmenit_stav(novy_stav):
                                c = intranet_data.get_db_connection()
                                cur = c.cursor()
                                cur.execute("UPDATE helpdesk_tikety SET stav = %s WHERE id = %s", (novy_stav, t['id']))
                                c.commit(); cur.close(); c.close()
                                intranet_logger.log_activity(user_name, "Helpdesk", f"Změněn stav tiketu #{t['id']} na: {novy_stav}")
                                ui.notify(f'Stav změněn na {novy_stav}', type='info')
                                dlg.close(); vykresli_helpdesk.refresh()

                            ui.button('Nové', on_click=lambda: zmenit_stav('Nové')).props('size=sm outline color=red')
                            ui.button('Řeší se', on_click=lambda: zmenit_stav('Řeší se')).props('size=sm outline color=orange')
                            ui.button('Vyřešeno', on_click=lambda: zmenit_stav('Vyřešeno')).props('size=sm outline color=green')
                            ui.button('Uzavřeno', on_click=lambda: zmenit_stav('Uzavřeno')).props('size=sm outline color=grey')

                        if is_admin_delete:
                            def smazat_tiket():
                                with ui.dialog() as d_del, ui.card().classes('p-6 rounded-xl'):
                                    ui.label(f'Opravdu smazat tiket #{t["id"]}?').classes('font-bold text-xl text-red-600 mb-4')
                                    ui.label('Tato akce trvale smaže tiket i celou jeho historii konverzace.').classes('mb-4 text-gray-700')
                                    def on_yes():
                                        c = intranet_data.get_db_connection()
                                        cur = c.cursor()
                                        cur.execute("DELETE FROM helpdesk_komentare WHERE tiket_id = %s", (t['id'],))
                                        cur.execute("DELETE FROM helpdesk_tikety WHERE id = %s", (t['id'],))
                                        c.commit(); cur.close(); c.close()
                                        intranet_logger.log_activity(user_name, "Helpdesk", f"Trvale smazán tiket #{t['id']}")
                                        ui.notify('Tiket byl úspěšně smazán.', type='positive')
                                        d_del.close(); dlg.close(); vykresli_helpdesk.refresh()
                                    with ui.row().classes('w-full justify-between'):
                                        ui.button('Zrušit', on_click=d_del.close).classes('bg-gray-400 text-white font-bold')
                                        ui.button('Smazat', on_click=on_yes).classes('bg-red-600 text-white font-bold shadow-md')
                                d_del.open()

                            ui.button('SMAZAT', icon='delete', on_click=smazat_tiket).props('size=sm color=red').classes('ml-auto shadow-sm font-bold')

            # --- KOMENTÁŘE: ROLOVACÍ KARTY (EXPANSION PANELS) ---
            komentare_container = ui.column().classes('w-full p-6 gap-4 overflow-y-auto grow bg-gray-50')

            def nacti_a_vykresli_komentare():
                komentare_container.clear()
                c = intranet_data.get_db_connection()
                komentare = []
                if c:
                    cur = c.cursor(dictionary=True)
                    cur.execute("SELECT * FROM helpdesk_komentare WHERE tiket_id = %s ORDER BY vytvoreno ASC", (t['id'],))
                    komentare = cur.fetchall()
                    cur.close(); c.close()

                with komentare_container:
                    if not komentare:
                        ui.label('K tomuto požadavku zatím nebyly přidány žádné komentáře.').classes('text-gray-500 italic text-center w-full mt-4')
                    else:
                        for k in komentare:
                            je_muj = k['user_id'] == user_id

                            # Texty a ikony
                            jmeno_kdo = 'Můj komentář' if je_muj else f"Odpověď: {k['jmeno_autora']}"
                            ikona = 'person' if je_muj else 'support_agent'

                            # Přesné barvy hlavičky (Modrá pro mě, Šedá pro ostatní)
                            barva_hlavicky = 'bg-blue-50 text-blue-800' if je_muj else 'bg-gray-200 text-gray-800'

                            # Vytvoření karty - overflow-hidden zaručí, že barva hlavičky nepřeteče přes zakulacené rohy
                            with ui.expansion(
                                    f"{jmeno_kdo} • {k['vytvoreno'].strftime('%d.%m.%Y v %H:%M')}",
                                    icon=ikona,
                                    value=True
                            ).classes('w-full bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden').props(f'header-class="{barva_hlavicky} font-bold"'):

                                # Tělo komentáře
                                ui.label(k['text']).classes('p-4 text-gray-800 whitespace-pre-wrap text-base')

            nacti_a_vykresli_komentare()

            # Psaní nového komentáře
            if t['stav'] != 'Uzavřeno':
                with ui.row().classes('w-full p-4 bg-white border-t border-gray-200 shrink-0 items-end gap-2 shadow-[0_-4px_6px_-1px_rgba(0,0,0,0.05)] z-10'):
                    novy_komentar = ui.textarea('Přidat novou odpověď...').classes('flex-1 bg-gray-50 rounded-xl').props('outlined autogrow')
                    def odeslat_komentar():
                        txt = novy_komentar.value.strip() if novy_komentar.value else ""
                        if not txt: return
                        c = intranet_data.get_db_connection()
                        cur = c.cursor()
                        cur.execute("INSERT INTO helpdesk_komentare (tiket_id, user_id, jmeno_autora, text) VALUES (%s, %s, %s, %s)",
                                    (t['id'], user_id, user_name, txt))

                        if is_admin and t['user_id'] != user_id and t['stav'] == 'Nové':
                            cur.execute("UPDATE helpdesk_tikety SET stav = 'Řeší se' WHERE id = %s", (t['id'],))
                        c.commit(); cur.close(); c.close()

                        novy_komentar.value = ''
                        nacti_a_vykresli_komentare()
                        if is_admin and t['stav'] == 'Nové': vykresli_helpdesk.refresh()

                    ui.button(icon='send', on_click=odeslat_komentar).classes('bg-blue-600 text-white h-14 w-14 rounded-xl shadow-md')
            else:
                with ui.row().classes('w-full p-4 bg-gray-200 border-t border-gray-300 shrink-0 justify-center z-10'):
                    ui.label('🔒 Tento tiket je uzavřen. Pro další dotazy prosím vytvořte nový požadavek.').classes('text-gray-600 font-bold text-sm')

            ui.button(icon='close', on_click=dlg.close).props('flat round').classes('absolute top-4 right-4 text-gray-400 hover:text-red-500 bg-white shadow-sm z-20')

        dlg.open()

    # Vykreslení karet tiketů do mřížky
    with ui.grid(columns=1).classes('w-full gap-4 lg:grid-cols-2'):
        for t in tikety:
            barva = barvy_stavu.get(t['stav'], 'bg-gray-100 text-gray-800 border-gray-300')
            opacita = 'opacity-60' if t['stav'] == 'Uzavřeno' else 'opacity-100'

            with ui.card().classes(f'w-full p-5 cursor-pointer hover:shadow-lg transition-shadow bg-white rounded-xl border border-gray-100 {opacita}').on('click', lambda tz=t: detail_tiketu(tz)):
                with ui.row().classes('w-full justify-between items-start mb-2'):
                    ui.label(t['predmet']).classes('font-bold text-lg text-gray-800 line-clamp-1 flex-1')
                    ui.label(t['stav']).classes(f'px-2 py-0.5 rounded text-[10px] font-bold uppercase border whitespace-nowrap ml-2 {barva}')

                ui.label(t['popis']).classes('text-sm text-gray-500 line-clamp-2 mb-4 h-10')

                with ui.row().classes('w-full justify-between items-center text-xs font-bold border-t border-gray-50 pt-2'):
                    ui.label(f"🙋‍♂️ {t['jmeno_zadavatele']}").classes('text-gray-400')

                    prirazeno_text = t.get('prirazeno_jmeno') or 'Všichni'
                    barva_prirazeni = 'text-blue-500' if prirazeno_text == user_name else 'text-gray-400'
                    ui.label(f"👉 Na: {prirazeno_text}").classes(barva_prirazeni)

                    ui.label(f"🕒 {t['vytvoreno'].strftime('%d.%m.%Y %H:%M')}").classes('text-gray-400')