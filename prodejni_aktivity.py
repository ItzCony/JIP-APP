"""
Modul Prodejní aktivity — NiceGUI implementace pro Moje JIPka.
Nahrazuje sdílený Excel „Nastavení a vyhodnocení prodejních aktivit".

Práva:
    prodej_akt_ctenar       – čtení záznamu, export CSV
    prodej_akt_zadavatel    – zakládá, edituje pole nákupčího
    prodej_akt_ucetni       – edituje 3 účetní sloupce
    prodej_akt_ao           – edituje sloupec Stav AO
    prodej_akt_schvalovatel – vše, vč. stavu aktivity a mazání
"""
from __future__ import annotations

import csv
import datetime
import io

from nicegui import ui
import intranet_data
import intranet_logger

# ── Číselníky ─────────────────────────────────────────────────────────────────

_VSECHNA_PRAVA_MODULU = {
    'prodej_akt_ctenar', 'prodej_akt_zadavatel',
    'prodej_akt_ucetni', 'prodej_akt_ao', 'prodej_akt_schvalovatel',
}

AKTIVITA_VOLBY = [
    '0 - budoucí',
    '1 - aktivní',
    '2 - ukončeno-vyhodnotit',
    '3 - vyhodnocená',
    '7 - vyúčtování odsouhlaseno',
    '9 - hotovo',
]

NAKUPCI_VOLBY = ['TK', 'IK', 'TS', 'IT', 'LJ', 'JV', 'KS', 'HK', 'MP', 'LM', 'ZZ', 'FK']

# ── Schema polí ───────────────────────────────────────────────────────────────
# (klic_db, role_write_token, typ, povinny, volby|None, label)
SCHEMA: list[tuple] = [
    ('aktivita',            'schvalovatel', 'enum',   True,  AKTIVITA_VOLBY,
     'Aktivita (stav)'),
    ('nakupci',             'zadavatel',    'enum',   True,  NAKUPCI_VOLBY,
     'Nákupčí'),
    ('dodavatel',           'zadavatel',    'text',   True,  None,
     'Dodavatel'),
    ('nazev_akce',          'zadavatel',    'text',   True,  None,
     'Název akce'),
    ('jak_vyuctovano',      'zadavatel',    'enum',   True,
     ['ODD na cenu zpětně na provozovny', 'ODD na cenu zpětně na centrálu',
      'ODD na vydané zásoby', 'Faktura centrálně za marketing',
      'Fakturovat dodavateli po skončení akce', 'z vlastní marže - nebude ODD'],
     'Jak bude vyúčtováno'),
    ('reseni_provozovna',   'zadavatel',    'enum',   False,
     ['příjemka od dodavatele', 'příjemka z centrály', 'záměna leták bonusy'],
     'Řešení na provozovně'),
    ('termin_nakupu_od',    'zadavatel',    'date',   False, None,
     'Termín nákupů od'),
    ('termin_nakupu_do',    'zadavatel',    'date',   False, None,
     'Termín nákupů do'),
    ('termin_prodeju_od',   'zadavatel',    'date',   False, None,
     'Termín prodejů od'),
    ('termin_prodeju_do',   'zadavatel',    'date',   False, None,
     'Termín prodejů do'),
    ('zasoby',              'zadavatel',    'enum',   False, ['ano', 'ne'],
     'Zásoby'),
    ('nakupci_kontrola',    'zadavatel',    'enum',   False, ['ano', 'ne'],
     'Nákupčí kontrola před odesláním dat'),
    ('info_aktivita',       'zadavatel',    'text',   False, None,
     'Informace k aktivitě'),
    ('info_pobocky',        'zadavatel',    'text',   False, None,
     'Informace pro pobočky'),
    ('castka_bez_dph',      'ucetni',       'text',   False, None,
     'Celkem částka bez DPH (Kč)'),
    ('cislo_dokladu',       'ucetni',       'text',   False, None,
     'Číslo dokladu dodavatele'),
    ('proouctoval_prov',    'ucetni',       'text',   False, None,
     'Vyúčtování proúčtoval provozovnám'),
    ('kompenzovano',        'zadavatel',    'text',   False, None,
     'Kompenzováno'),
    ('kompenzace_mj_kc',    'zadavatel',    'text',   False, None,
     'Kompenzace bude v MJ/Kč'),
    ('kompenzace_kc_za_mj', 'zadavatel',    'number', False, None,
     'Pokud kompenzace v Kč – kolik Kč/MJ'),
    ('kod_zbozi',           'zadavatel',    'text',   False, None,
     'Kód zboží'),
    ('nazev_zbozi',         'zadavatel',    'text',   False, None,
     'Název zboží'),
    ('stav_ao',             'ao',           'text',   False, None,
     'Stav AO'),
]

SCHEMA_DICT: dict = {
    s[0]: {'role_write': s[1], 'typ': s[2], 'povinny': s[3], 'volby': s[4], 'label': s[5]}
    for s in SCHEMA
}

# ── Pomocné funkce oprávnění ───────────────────────────────────────────────────

def _ma_pristup(prava: list) -> bool:
    return bool(set(prava) & (_VSECHNA_PRAVA_MODULU | {'vse'}))

def _je_schvalovatel(prava: list) -> bool:
    return 'prodej_akt_schvalovatel' in prava or 'vse' in prava

def _muze_psat_pole(prava: list, klic: str) -> bool:
    if _je_schvalovatel(prava):
        return True
    role_write = SCHEMA_DICT[klic]['role_write']
    return f'prodej_akt_{role_write}' in prava

def _muze_zalozit(prava: list) -> bool:
    return bool(set(prava) & {'prodej_akt_zadavatel', 'prodej_akt_schvalovatel', 'vse'})

def _muze_smazat(prava: list) -> bool:
    return bool(set(prava) & {'prodej_akt_schvalovatel', 'vse'})

# ── Databázové funkce ─────────────────────────────────────────────────────────

def inicializace_db():
    """Vytvoří tabulky, pokud neexistují."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prod_aktivity (
                id                  INT AUTO_INCREMENT PRIMARY KEY,
                aktivita            VARCHAR(60),
                nakupci             VARCHAR(20),
                dodavatel           VARCHAR(200),
                nazev_akce          VARCHAR(300),
                jak_vyuctovano      VARCHAR(150),
                reseni_provozovna   VARCHAR(100),
                termin_nakupu_od    DATE,
                termin_nakupu_do    DATE,
                termin_prodeju_od   DATE,
                termin_prodeju_do   DATE,
                zasoby              VARCHAR(10),
                nakupci_kontrola    VARCHAR(10),
                info_aktivita       TEXT,
                info_pobocky        TEXT,
                castka_bez_dph      VARCHAR(500),
                cislo_dokladu       VARCHAR(150),
                proouctoval_prov    VARCHAR(300),
                kompenzovano        VARCHAR(300),
                kompenzace_mj_kc    VARCHAR(100),
                kompenzace_kc_za_mj DECIMAL(12,2),
                kod_zbozi           VARCHAR(300),
                nazev_zbozi         VARCHAR(300),
                stav_ao             TEXT,
                rucni_uzavreni      TINYINT(1) NOT NULL DEFAULT 0,
                vytvoril            VARCHAR(150),
                vytvoreno           DATETIME,
                upraveno            DATETIME
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prod_aktivity_audit (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                aktivita_id INT,
                uzivatel    VARCHAR(150),
                akce        VARCHAR(20),
                pole        VARCHAR(100),
                stara_hodnota TEXT,
                nova_hodnota  TEXT,
                cas         DATETIME,
                INDEX idx_aktid (aktivita_id)
            )
        """)
        # Migrace: pokud sloupec existuje jako DECIMAL, převést na VARCHAR
        try:
            cur.execute("""
                ALTER TABLE prod_aktivity
                MODIFY COLUMN castka_bez_dph VARCHAR(500)
            """)
        except Exception:
            pass  # Sloupec už je VARCHAR nebo tabulka neexistuje
        # Migrace: příznak ručního uzavření (1 = uzavřel člověk, auto-reverze 2→1 ho nesmí otevřít)
        try:
            cur.execute("""
                ALTER TABLE prod_aktivity
                ADD COLUMN rucni_uzavreni TINYINT(1) NOT NULL DEFAULT 0
            """)
        except Exception:
            pass  # Sloupec už existuje nebo tabulka neexistuje
        conn.commit()
    except Exception as e:
        print(f"[prodejni_aktivity.inicializace_db] {e}")
    finally:
        cur.close()
        conn.close()


def _uloz_audit(aktivita_id: int, uzivatel: str, akce: str,
                pole: str | None = None, stara=None, nova=None):
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO prod_aktivity_audit
               (aktivita_id, uzivatel, akce, pole, stara_hodnota, nova_hodnota, cas)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (aktivita_id, uzivatel, akce, pole,
             str(stara) if stara is not None else None,
             str(nova)  if nova  is not None else None,
             datetime.datetime.now()),
        )
        conn.commit()
    except Exception as e:
        print(f"[prodejni_aktivity._uloz_audit] {e}")
    finally:
        cur.close()
        conn.close()


def _nacti_radky(filtr: dict, prava: list) -> list[dict]:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    kde, params = [], []

    if filtr.get('aktivita'):
        kde.append('aktivita = %s');        params.append(filtr['aktivita'])
    if filtr.get('nakupci'):
        kde.append('nakupci = %s');         params.append(filtr['nakupci'])
    if filtr.get('dodavatel'):
        kde.append('LOWER(dodavatel) LIKE %s'); params.append(f"%{filtr['dodavatel'].lower()}%")
    if filtr.get('nazev_akce'):
        kde.append('LOWER(nazev_akce) LIKE %s'); params.append(f"%{filtr['nazev_akce'].lower()}%")
    if filtr.get('od'):
        kde.append('(termin_nakupu_od >= %s OR termin_prodeju_od >= %s)')
        params += [filtr['od'], filtr['od']]
    if filtr.get('do'):
        kde.append('(termin_nakupu_do <= %s OR termin_prodeju_do <= %s)')
        params += [filtr['do'], filtr['do']]

    sql = "SELECT * FROM prod_aktivity"
    if kde:
        sql += " WHERE " + " AND ".join(kde)
    sql += " ORDER BY id DESC"

    try:
        cur.execute(sql, params)
        radky = cur.fetchall()
        smazat_ok = _muze_smazat(prava)

        # ── Auto-update stavu podle termínů ──
        #   '1 - aktivní'             → '2 - ukončeno-vyhodnotit'  když termín uplynul
        #   '2 - ukončeno-vyhodnotit' → '1 - aktivní'             když je termín zase platný
        # Reverze 2→1 se provede JEN když aktivita má budoucí termín – ruční uzavření
        # bez termínu (oba _do prázdné) tím nepřepíšeme.
        dnes = datetime.date.today()
        ids_prekcroceno = []   # 1 → 2
        ids_obnoveno    = []   # 2 → 1
        for r in radky:
            nakup_do  = r.get('termin_nakupu_do')
            prodej_do = r.get('termin_prodeju_do')
            ma_termin = (isinstance(nakup_do,  datetime.date) or
                         isinstance(prodej_do, datetime.date))
            terminy_uplynuly = (
                (isinstance(nakup_do,  datetime.date) and nakup_do  < dnes) or
                (isinstance(prodej_do, datetime.date) and prodej_do < dnes)
            )
            stav = r.get('aktivita')
            rucni = bool(r.get('rucni_uzavreni'))
            if stav == '1 - aktivní' and terminy_uplynuly:
                r['aktivita'] = '2 - ukončeno-vyhodnotit'
                ids_prekcroceno.append(r['id'])
            elif (stav == '2 - ukončeno-vyhodnotit' and not rucni
                    and ma_termin and not terminy_uplynuly):
                r['aktivita'] = '1 - aktivní'
                ids_obnoveno.append(r['id'])

        now = datetime.datetime.now()
        if ids_prekcroceno:
            ph = ', '.join(['%s'] * len(ids_prekcroceno))
            cur.execute(
                f"UPDATE prod_aktivity "
                f"SET aktivita='2 - ukončeno-vyhodnotit', rucni_uzavreni=0, upraveno=%s "
                f"WHERE id IN ({ph})",
                [now] + ids_prekcroceno,
            )
        if ids_obnoveno:
            ph = ', '.join(['%s'] * len(ids_obnoveno))
            cur.execute(
                f"UPDATE prod_aktivity "
                f"SET aktivita='1 - aktivní', rucni_uzavreni=0, upraveno=%s "
                f"WHERE id IN ({ph})",
                [now] + ids_obnoveno,
            )
        if ids_prekcroceno or ids_obnoveno:
            conn.commit()

        for r in radky:
            r['_muze_smazat'] = smazat_ok

            # Červené zvýraznění: termín uplynul a aktivita ještě není uzavřena (stav 0–2)
            nakup_do  = r.get('termin_nakupu_do')
            prodej_do = r.get('termin_prodeju_do')
            terminy_uplynuly = (
                (isinstance(nakup_do,  datetime.date) and nakup_do  < dnes) or
                (isinstance(prodej_do, datetime.date) and prodej_do < dnes)
            )
            stav = r.get('aktivita', '')
            r['_prekcrocen'] = terminy_uplynuly and not any(
                stav.startswith(s) for s in ('3', '7', '9')
            )

            # Datum objekty → YYYY-MM-DD (ISO, pro date picker i tabulkové :format)
            for klic in ('termin_nakupu_od', 'termin_nakupu_do',
                         'termin_prodeju_od', 'termin_prodeju_do'):
                v = r.get(klic)
                r[klic] = v.strftime('%Y-%m-%d') if isinstance(v, datetime.date) else (v or '')
            # Datetime → řetězec
            for klic in ('vytvoreno', 'upraveno'):
                v = r.get(klic)
                r[klic] = v.strftime('%d.%m.%Y %H:%M') if isinstance(v, datetime.datetime) else (v or '')
            # Decimal → float
            for klic in ('kompenzace_kc_za_mj',):
                v = r.get(klic)
                r[klic] = float(v) if v is not None else None
            # None → prázdný řetězec (JSON-safe)
            for k, v in r.items():
                if v is None:
                    r[k] = ''
        return radky
    except Exception as e:
        print(f"[prodejni_aktivity._nacti_radky] {e}")
        return []
    finally:
        cur.close()
        conn.close()


def _nacti_audit(aktivita_id: int) -> list[dict]:
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM prod_aktivity_audit WHERE aktivita_id=%s ORDER BY id DESC LIMIT 200",
            (aktivita_id,)
        )
        radky = cur.fetchall()
        for r in radky:
            v = r.get('cas')
            if isinstance(v, datetime.datetime):
                r['cas'] = v.strftime('%d.%m.%Y %H:%M:%S')
        return radky
    except Exception as e:
        print(f"[prodejni_aktivity._nacti_audit] {e}")
        return []
    finally:
        cur.close()
        conn.close()


# ── Dialog: Nová aktivita / Úprava ────────────────────────────────────────────

def _dialog_formular(prava: list, user_name: str,
                     predvyplnit: dict | None, po_ulozeni):
    """
    Otevře dialog pro nový záznam (predvyplnit=None) nebo úpravu (predvyplnit=dict).
    po_ulozeni() se zavolá po úspěšném uložení (obvykle _tabulka.refresh).
    """
    je_nova = predvyplnit is None
    vstupni_pole: dict = {}   # klic → NiceGUI widget

    with ui.dialog().props('maximized') as dlg:
        # Karta vyplní celou výšku dialogu — flex sloupec, aby scroll dostal zbylé místo
        with ui.card().classes('w-full h-full max-w-4xl mx-auto flex flex-col rounded-none shadow-none bg-white p-0 overflow-hidden'):

            # ── Fixní hlavička ────────────────────────────────────────────
            with ui.column().classes('w-full px-6 pt-5 pb-3 shrink-0 border-b border-gray-200'):
                ui.label('Nová aktivita' if je_nova else 'Úprava aktivity') \
                  .classes('text-2xl font-extrabold text-blue-900')
                chyba_lbl = ui.label('').classes('text-red-500 text-sm min-h-[1.4em] mt-1')

            # ── Scrollovatelný obsah — plain div, kolečko myši funguje kdekoliv ──
            with ui.element('div').classes('flex-1 w-full overflow-y-auto').style('min-height:0'):
                with ui.column().classes('w-full gap-4 px-6 py-4'):
                    for klic, role_write, typ, povinny, volby, label in SCHEMA:
                        muze_psat = _muze_psat_pole(prava, klic)

                        # Aktivita při zakládání: jen schvalovatel ji nastavuje ručně,
                        # ostatní dostanou skrytou default hodnotu '1 - aktivní'
                        if klic == 'aktivita' and je_nova and not _je_schvalovatel(prava):
                            vstupni_pole[klic] = None   # None = použít default
                            continue

                        # Výchozí hodnota pole
                        predval = '' if je_nova else str(predvyplnit.get(klic) or '')

                        lbl_full = label + (' *' if povinny and muze_psat else '')

                        if typ == 'enum' and volby:
                            w = ui.select(
                                options=[''] + list(volby),
                                label=lbl_full,
                                value=predval,
                            ).classes('w-full')
                            if not muze_psat:
                                w.props('disable')
                            vstupni_pole[klic] = w

                        elif typ == 'date':
                            w = ui.input(label=lbl_full, value=predval) \
                                  .props('type=date').classes('w-full')
                            if not muze_psat:
                                w.props('disable')
                            vstupni_pole[klic] = w

                        elif typ == 'number':
                            num_val = float(predval) if predval not in ('', None) else None
                            w = ui.number(label=lbl_full, value=num_val, format='%.2f') \
                                  .classes('w-full')
                            if not muze_psat:
                                w.props('disable')
                            vstupni_pole[klic] = w

                        else:  # text / textarea
                            if klic == 'castka_bez_dph':
                                w = ui.input(label=lbl_full, value=predval,
                                             placeholder='např. 1000 250.50 800') \
                                      .classes('w-full') \
                                      .props('pattern="[0-9 .;,]*"')

                                def _filtruj_castku(e, field=None):
                                    import re
                                    if field is None:
                                        return
                                    ocisteno = re.sub(r'[^0-9 .;,]', '', str(e.args or ''))
                                    if ocisteno != str(e.args or ''):
                                        field.set_value(ocisteno)

                                _w_castka = w
                                w.on('update:model-value',
                                     lambda e, f=_w_castka: _filtruj_castku(e, f))
                            else:
                                w = ui.textarea(label=lbl_full, value=predval) \
                                      .props('rows=2 autogrow').classes('w-full')
                            if not muze_psat:
                                w.props('disable')
                            vstupni_pole[klic] = w

            # ── Fixní patička — def uloz() musí být PŘED tlačítkem ───────
            def uloz():
                chyba_lbl.set_text('')

                # Sestav dict hodnot z widgetů
                hodnoty: dict = {}
                for klic, *_ in SCHEMA:
                    w = vstupni_pole.get(klic)
                    if w is None:
                        hodnoty[klic] = None   # None = default handled below
                    else:
                        val = w.value
                        if isinstance(val, float) and SCHEMA_DICT[klic]['typ'] == 'number':
                            hodnoty[klic] = val if val != 0.0 else None
                        else:
                            hodnoty[klic] = val if val != '' else None

                # Validace pole castka_bez_dph — jen čísla, mezery, středníky, tečky, čárky
                import re as _re
                castka_val = hodnoty.get('castka_bez_dph') or ''
                if castka_val and not _re.fullmatch(r'[0-9][0-9 .;,]*', castka_val.strip()):
                    chyba_lbl.set_text('Pole „Celkem částka bez DPH" smí obsahovat jen čísla oddělená mezerou nebo středníkem.')
                    return

                # Validace povinných polí (jen těch, které uživatel smí vyplnit)
                chybejici = []
                for klic, role_write, typ, povinny, volby, label in SCHEMA:
                    if not povinny:
                        continue
                    if klic == 'aktivita' and je_nova and not _je_schvalovatel(prava):
                        continue   # aktivita má default
                    if not _muze_psat_pole(prava, klic):
                        continue
                    v = hodnoty.get(klic)
                    if v is None or str(v).strip() == '':
                        chybejici.append(label)

                if chybejici:
                    chyba_lbl.set_text('Povinná pole: ' + ', '.join(chybejici))
                    return

                conn = intranet_data.get_db_connection()
                if not conn:
                    ui.notify('Nepodařilo se připojit k databázi.', type='negative')
                    return

                cur = conn.cursor()
                try:
                    now = datetime.datetime.now()

                    if je_nova:
                        if hodnoty.get('aktivita') is None:
                            hodnoty['aktivita'] = '1 - aktivní'
                        # Při zakládání už ve stavu '2' = ruční uzavření (auto-reverze ho neotevře)
                        hodnoty['rucni_uzavreni'] = (
                            1 if hodnoty['aktivita'] == '2 - ukončeno-vyhodnotit' else 0)

                        sloupce = list(hodnoty.keys())
                        vals    = list(hodnoty.values())
                        sloupce += ['vytvoril', 'vytvoreno', 'upraveno']
                        vals    += [user_name, now, now]
                        ph = ', '.join(['%s'] * len(vals))
                        cur.execute(
                            f"INSERT INTO prod_aktivity ({', '.join(sloupce)}) VALUES ({ph})",
                            vals,
                        )
                        new_id = cur.lastrowid
                        conn.commit()
                        _uloz_audit(new_id, user_name, 'vytvoreni',
                                    nova=str({k: v for k, v in hodnoty.items() if v}))
                        intranet_logger.log_activity(
                            user_name, 'Prodejní aktivity',
                            f'Nová aktivita #{new_id}: {hodnoty.get("nazev_akce","?")}')
                        ui.notify(f'Aktivita #{new_id} uložena.', type='positive')

                    else:
                        rid = int(predvyplnit['id'])
                        zmeny: dict = {}
                        for klic in hodnoty:
                            if not _muze_psat_pole(prava, klic):
                                continue
                            stara_str = str(predvyplnit.get(klic) or '')
                            nova_str  = str(hodnoty[klic] or '')
                            if stara_str != nova_str:
                                zmeny[klic] = (stara_str, nova_str)

                        if zmeny:
                            set_sql = ', '.join([f'{k} = %s' for k in zmeny]) + ', upraveno = %s'
                            vals = [v[1] if v[1] != '' else None for v in zmeny.values()] + [now]
                            # Ruční změna stavu → příznak ručního uzavření.
                            # '2 - ukončeno-vyhodnotit' = uzavřel člověk (auto-reverze ho neotevře);
                            # jakýkoli jiný stav = příznak zpět na 0.
                            if 'aktivita' in zmeny:
                                set_sql += ', rucni_uzavreni = %s'
                                vals.append(1 if zmeny['aktivita'][1] == '2 - ukončeno-vyhodnotit' else 0)
                            vals.append(rid)
                            cur.execute(f"UPDATE prod_aktivity SET {set_sql} WHERE id = %s", vals)
                            conn.commit()
                            for k, (s, n) in zmeny.items():
                                _uloz_audit(rid, user_name, 'uprava', pole=k, stara=s, nova=n)
                            intranet_logger.log_activity(
                                user_name, 'Prodejní aktivity', f'Upravena aktivita #{rid}')
                        ui.notify('Uloženo.', type='positive')

                    dlg.close()
                    po_ulozeni()

                except Exception as e:
                    ui.notify(f'Chyba při ukládání: {e}', type='negative')
                    print(f"[prodejni_aktivity.uloz] {e}")
                finally:
                    cur.close()
                    conn.close()

            with ui.row().classes('w-full justify-end gap-3 px-6 py-4 shrink-0 border-t border-gray-200 bg-white'):
                ui.button('Zrušit', on_click=dlg.close) \
                  .classes('bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold')
                ui.button('Uložit', icon='save', on_click=uloz) \
                  .classes('bg-blue-600 hover:bg-blue-700 text-white font-bold')

    dlg.open()


# ── Dialog: Historie změn ─────────────────────────────────────────────────────

def _dialog_audit_historie(aktivita_id: int, nazev: str):
    radky = _nacti_audit(aktivita_id)
    with ui.dialog() as dlg, \
         ui.card().classes('w-full max-w-3xl p-6 rounded-2xl shadow-2xl'):
        ui.label(f'Historie změn — #{aktivita_id}: {nazev}') \
          .classes('text-xl font-extrabold text-blue-900 mb-4')

        if not radky:
            ui.label('Žádné záznamy.').classes('text-gray-400 italic py-4')
        else:
            with ui.scroll_area().style('max-height:60vh').classes('w-full'):
                for r in radky:
                    akce  = r.get('akce', '')
                    barva = {'vytvoreni': 'green', 'uprava': 'blue', 'smazani': 'red'}.get(akce, 'grey')
                    with ui.row().classes('w-full items-start gap-3 py-2 border-b border-gray-100'):
                        ui.badge(akce or '?', color=barva).classes('shrink-0 mt-1 capitalize')
                        with ui.column().classes('flex-1 min-w-0 gap-0'):
                            ui.label(f"{r.get('cas','')}  ·  {r.get('uzivatel','')}") \
                              .classes('text-xs text-gray-400 font-mono')
                            if r.get('pole'):
                                pole_label = SCHEMA_DICT.get(r['pole'], {}).get('label', r['pole'])
                                ui.label(
                                    f"{pole_label}:  "
                                    f"{r.get('stara_hodnota') or '—'}  →  "
                                    f"{r.get('nova_hodnota') or '—'}"
                                ).classes('text-sm text-gray-700 break-words')

        ui.button('Zavřít', on_click=dlg.close) \
          .classes('mt-4 bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold self-end')
    dlg.open()


# ── Import z Excelu ───────────────────────────────────────────────────────────

def _dialog_import_excel(prava: list, user_name: str, po_importu):
    """Hromadný import aktivit z .xlsx souboru."""
    try:
        import openpyxl
    except ImportError:
        ui.notify('Chybí knihovna openpyxl. Spusťte: pip install openpyxl', type='negative')
        return

    _nactene: list = []  # parsované řádky, sdíleno přes closures
    _refs: dict   = {}   # reference na tlačítko importu (definované až v patičce)

    # Mapování: název sloupce (lowercase) nebo DB klíč → klic_db
    _label_map: dict = {}
    for klic, _, _, _, _, label in SCHEMA:
        _label_map[label.lower()] = klic
        _label_map[klic.lower()]  = klic

    # Aliasy pro nestandardní hlavičky (např. z původního Excel souboru)
    _ALIASY = {
        'nákupčí kontrola před odesláním dat (bude kontrolovat?)': 'nakupci_kontrola',
        'informace k aktivitě/informace co se posílá pro pobočky': 'info_pobocky',
        'celkem částka bez dph':  'castka_bez_dph',
        'číslo dokladu dodavatele': 'cislo_dokladu',
        'vyúčtování proúčtoval provozovnám': 'proouctoval_prov',
        'pokud kompenzace v kč (zadat kolik kč za mj). pokud v mj, nezadávat nic.': 'kompenzace_kc_za_mj',
        'název akce': 'nazev_akce',
    }
    for alias, klic in _ALIASY.items():
        _label_map[alias] = klic

    with ui.dialog().props('maximized') as dlg:
        with ui.card().classes(
            'w-full h-full max-w-5xl mx-auto flex flex-col '
            'rounded-none shadow-none bg-white p-0 overflow-hidden'
        ):
            # ── Hlavička ──────────────────────────────────────────────────
            with ui.column().classes('w-full px-6 pt-5 pb-3 shrink-0 border-b border-gray-200'):
                ui.label('📥 Import z Excelu').classes('text-2xl font-extrabold text-blue-900')
                ui.label(
                    'Nahrajte .xlsx soubor — první řádek musí být hlavička (název sloupce nebo DB klíč).'
                ).classes('text-sm text-gray-500 mt-1')
                stav_lbl = ui.label('').classes('text-sm min-h-[1.4em] mt-1')

            # ── Scrollovatelný obsah ───────────────────────────────────────
            with ui.element('div').classes('flex-1 w-full overflow-y-auto').style('min-height:0'):
                with ui.column().classes('w-full gap-5 px-6 py-4'):

                    # Nápověda
                    with ui.expansion('Nápověda — přehled sloupců', icon='help_outline').classes(
                        'w-full border border-gray-200 rounded-xl'
                    ):
                        with ui.element('div').classes('p-4'):
                            ui.label(
                                '* = povinné pole  |  Jako hlavičku v Excelu použijte název sloupce nebo DB klíč.'
                            ).classes('text-xs text-gray-400 mb-3')
                            with ui.element('div').classes('grid gap-x-8 gap-y-0.5').style(
                                'grid-template-columns: 24px 1fr 1fr'
                            ):
                                for lbl_h in ('', 'Název sloupce', 'DB klíč'):
                                    ui.label(lbl_h).classes('text-[10px] font-black text-gray-400 uppercase tracking-wider pb-1')
                                for klic, _, _, povinny, _, label in SCHEMA:
                                    ui.label('*' if povinny else '').classes('text-red-400 text-xs font-bold text-center')
                                    ui.label(label).classes('text-xs text-gray-700')
                                    ui.label(klic).classes('text-xs font-mono text-blue-500')

                    # Upload widget
                    nahled_kontejner = ui.column().classes('w-full gap-3')

                    async def on_upload(e):
                        _nactene.clear()
                        nahled_kontejner.clear()
                        stav_lbl.classes(replace='text-sm min-h-[1.4em] mt-1 text-gray-500')
                        stav_lbl.set_text('Zpracovávám soubor…')
                        if _refs.get('importovat_btn'):
                            _refs['importovat_btn'].set_visibility(False)

                        try:
                            # Kompatibilní čtení obsahu souboru napříč verzemi NiceGUI:
                            #   e.content  – NiceGUI ≥ 1.3 (BytesIO)
                            #   e.file     – starší NiceGUI (starlette UploadFile, async read)
                            if hasattr(e, 'content') and e.content is not None:
                                soubor = e.content
                            elif hasattr(e, 'file') and e.file is not None:
                                raw = await e.file.read()
                                soubor = io.BytesIO(raw)
                            else:
                                dostupne = [a for a in dir(e) if not a.startswith('_')]
                                stav_lbl.classes(replace='text-sm min-h-[1.4em] mt-1 text-red-600')
                                stav_lbl.set_text(f'Chyba: nepodařilo se načíst obsah souboru. Atributy: {dostupne}')
                                return

                            wb = openpyxl.load_workbook(soubor, data_only=True)
                            ws = wb.active
                            vsechny_radky = list(ws.iter_rows(values_only=True))

                            if not vsechny_radky:
                                stav_lbl.classes(replace='text-sm min-h-[1.4em] mt-1 text-red-500')
                                stav_lbl.set_text('Soubor je prázdný.')
                                return

                            # Namapuj hlavičku
                            header = [
                                str(h).strip().lower() if h is not None else ''
                                for h in vsechny_radky[0]
                            ]
                            col_map: dict = {}  # index sloupce → klic_db
                            nezname: list = []
                            for i, h in enumerate(header):
                                if h in _label_map:
                                    col_map[i] = _label_map[h]
                                elif h:
                                    nezname.append(h)

                            if not col_map:
                                stav_lbl.classes(replace='text-sm min-h-[1.4em] mt-1 text-red-600 font-semibold')
                                stav_lbl.set_text('Žádný sloupec nebylo možné namapovat. Zkontrolujte hlavičku.')
                                return

                            # Parsuj datové řádky
                            chyby: list = []
                            for row_i, vals in enumerate(vsechny_radky[1:], start=2):
                                # Přeskoč prázdné řádky
                                if all(v is None or str(v).strip() == '' for v in vals):
                                    continue
                                parsovany: dict = {}
                                for col_i, klic in col_map.items():
                                    val = vals[col_i] if col_i < len(vals) else None
                                    typ = SCHEMA_DICT[klic]['typ']
                                    if val is None or str(val).strip() == '':
                                        parsovany[klic] = None
                                        continue
                                    if typ == 'date':
                                        if isinstance(val, (datetime.date, datetime.datetime)):
                                            parsovany[klic] = (
                                                val.date() if isinstance(val, datetime.datetime) else val
                                            )
                                        else:
                                            s = str(val).strip()
                                            parsed_d = None
                                            for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%Y/%m/%d'):
                                                try:
                                                    parsed_d = datetime.datetime.strptime(s, fmt).date()
                                                    break
                                                except ValueError:
                                                    pass
                                            if parsed_d:
                                                parsovany[klic] = parsed_d
                                            else:
                                                chyby.append(f'Ř.{row_i}: nelze parsovat datum „{val}" ({klic})')
                                                parsovany[klic] = None
                                    elif typ == 'number':
                                        try:
                                            parsovany[klic] = float(
                                                str(val).replace(',', '.').replace(' ', '').replace(' ', '')
                                            )
                                        except ValueError:
                                            chyby.append(f'Ř.{row_i}: nelze parsovat číslo „{val}" ({klic})')
                                            parsovany[klic] = None
                                    else:
                                        parsovany[klic] = str(val).strip()
                                _nactene.append(parsovany)

                            # ── Zobraz výsledek ───────────────────────────
                            with nahled_kontejner:
                                if nezname:
                                    with ui.element('div').classes(
                                        'flex items-start gap-2 p-3 bg-amber-50 border border-amber-200 rounded-lg'
                                    ):
                                        ui.icon('info', color='amber-600', size='sm')
                                        ui.label(
                                            f'Ignorované sloupce (neznámá hlavička): {", ".join(nezname)}'
                                        ).classes('text-xs text-amber-700')

                                if chyby:
                                    with ui.expansion(
                                        f'⚠️ Upozornění při parsování ({len(chyby)})', icon='warning'
                                    ).classes('w-full border border-amber-200 rounded-lg bg-amber-50'):
                                        with ui.element('div').classes('p-3 flex flex-col gap-1'):
                                            for c in chyby[:30]:
                                                ui.label(c).classes('text-xs text-amber-700 font-mono')

                                # Náhled tabulky
                                mapped_klice = list(dict.fromkeys(col_map.values()))  # zachová pořadí
                                preview_cols = [
                                    {'name': k, 'label': SCHEMA_DICT[k]['label'], 'field': k, 'align': 'left'}
                                    for k in mapped_klice
                                ]
                                preview_rows = [
                                    {k: (str(r[k]) if r.get(k) is not None else '') for k in mapped_klice}
                                    for r in _nactene[:10]
                                ]

                                with ui.element('div').classes('flex items-center justify-between'):
                                    ui.label(
                                        f'Náhled — {len(_nactene)} řádků k importu ({len(col_map)} sloupců):'
                                    ).classes('text-sm font-bold text-gray-700')
                                    if len(_nactene) > 10:
                                        ui.label(f'zobrazeno prvních 10').classes('text-xs text-gray-400 italic')

                                ui.table(columns=preview_cols, rows=preview_rows) \
                                  .classes('w-full rounded-xl') \
                                  .props('flat bordered dense')

                            stav_lbl.classes(replace='text-sm min-h-[1.4em] mt-1 text-green-700 font-semibold')
                            stav_lbl.set_text(f'✅ Soubor načten — {len(_nactene)} řádků připraveno.')
                            if _refs.get('importovat_btn'):
                                _refs['importovat_btn'].set_visibility(True)

                        except Exception as ex:
                            stav_lbl.classes(replace='text-sm min-h-[1.4em] mt-1 text-red-600')
                            stav_lbl.set_text(f'Chyba při čtení souboru: {ex}')
                            print(f'[prodejni_aktivity.import_excel] {ex}')

                    ui.upload(
                        label='Vyberte .xlsx soubor',
                        on_upload=on_upload,
                        auto_upload=True,
                        max_file_size=10_000_000,
                    ).props('accept=.xlsx flat bordered').classes('w-full')

            # ── Patička ───────────────────────────────────────────────────
            def importovat():
                if not _nactene:
                    ui.notify('Nejsou žádná data k importu.', type='warning')
                    return

                conn = intranet_data.get_db_connection()
                if not conn:
                    ui.notify('Nepodařilo se připojit k databázi.', type='negative')
                    return

                now     = datetime.datetime.now()
                uspesne = 0
                chyby_db: list = []
                cur = conn.cursor()
                try:
                    for i, radek in enumerate(_nactene, 1):
                        if not radek.get('aktivita'):
                            radek['aktivita'] = '1 - aktivní'
                        sloupce = list(radek.keys())
                        vals    = list(radek.values())
                        sloupce += ['vytvoril', 'vytvoreno', 'upraveno']
                        vals    += [user_name, now, now]
                        ph = ', '.join(['%s'] * len(vals))
                        try:
                            cur.execute(
                                f"INSERT INTO prod_aktivity ({', '.join(sloupce)}) VALUES ({ph})",
                                vals,
                            )
                            new_id = cur.lastrowid
                            _uloz_audit(new_id, user_name, 'vytvoreni',
                                        nova=f'Import z Excelu, řádek {i}')
                            uspesne += 1
                        except Exception as ex:
                            chyby_db.append(f'Řádek {i}: {ex}')

                    conn.commit()
                    intranet_logger.log_activity(
                        user_name, 'Prodejní aktivity',
                        f'Import z Excelu — importováno {uspesne} aktivit',
                    )
                    if chyby_db:
                        ui.notify(
                            f'Import dokončen: {uspesne} ok, {len(chyby_db)} chyb.',
                            type='warning',
                        )
                    else:
                        ui.notify(f'Importováno {uspesne} aktivit.', type='positive')
                    dlg.close()
                    po_importu()
                except Exception as ex:
                    ui.notify(f'Chyba při importu: {ex}', type='negative')
                finally:
                    cur.close()
                    conn.close()

            with ui.row().classes(
                'w-full justify-end gap-3 px-6 py-4 shrink-0 border-t border-gray-200 bg-white'
            ):
                ui.button('Zrušit', on_click=dlg.close) \
                  .classes('bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold')
                _refs['importovat_btn'] = ui.button(
                    'Importovat', icon='upload', on_click=importovat,
                ).classes('bg-green-600 hover:bg-green-700 text-white font-bold')
                _refs['importovat_btn'].set_visibility(False)

    dlg.open()


# ── Export CSV ────────────────────────────────────────────────────────────────

def _export_csv(prava: list, filtr: dict | None = None):
    radky = _nacti_radky(filtr or {}, prava)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';')
    writer.writerow(
        ['#'] + [s[5] for s in SCHEMA] + ['Vytvořil', 'Vytvořeno', 'Upraveno']
    )
    for r in radky:
        row = [r.get('id', '')]
        for klic, *_ in SCHEMA:
            row.append(r.get(klic, '') or '')
        row += [r.get('vytvoril', ''), r.get('vytvoreno', ''), r.get('upraveno', '')]
        writer.writerow(row)

    content = b'\xef\xbb\xbf' + buf.getvalue().encode('utf-8')
    nazev = f'prodejni_aktivity_{datetime.date.today().strftime("%Y%m%d")}.csv'
    ui.download(content, nazev)


# ── Hlavní funkce ─────────────────────────────────────────────────────────────

def vykresli(user_id: int, user_name: str, vsechna_prava: list):
    inicializace_db()

    if not _ma_pristup(vsechna_prava):
        with ui.column().classes('w-full items-center py-24 gap-4'):
            ui.icon('lock', size='4rem', color='grey-4')
            ui.label('Nemáte přístup k modulu Prodejní aktivity.') \
              .classes('text-gray-500 text-lg')
        return

    # Stav filtrů — sdílený v closure (nevytváříme třídu)
    filtr: dict = {k: '' for k in ('aktivita', 'nakupci', 'dodavatel', 'nazev_akce', 'od', 'do')}

    # ── Záhlaví ───────────────────────────────────────────────────────────
    with ui.row().classes('w-full items-center gap-4 mb-2'):
        ui.label('📋 Prodejní aktivity').classes('text-3xl font-extrabold text-gray-800')

    ui.label('Nastavení a vyhodnocení prodejních aktivit') \
      .classes('text-sm text-gray-500 mb-4')

    # ── Panel filtrů ──────────────────────────────────────────────────────
    with ui.card().classes('w-full p-4 mb-4 bg-white rounded-xl shadow-sm border border-gray-100'):
        with ui.row().classes('w-full items-end gap-3 flex-wrap'):

            def _aktualizuj():
                filtr['aktivita']   = f_aktivita.value or ''
                filtr['nakupci']    = f_nakupci.value or ''
                filtr['dodavatel']  = f_dodavatel.value or ''
                filtr['nazev_akce'] = f_nazev.value or ''
                filtr['od']         = f_od.value or ''
                filtr['do']         = f_do.value or ''
                _tabulka.refresh()

            f_aktivita = ui.select(
                options=[''] + AKTIVITA_VOLBY,
                label='Stav', value='',
            ).classes('w-52')
            f_nakupci = ui.select(
                options=[''] + NAKUPCI_VOLBY,
                label='Nákupčí', value='',
            ).classes('w-36')
            f_dodavatel = ui.input(label='Dodavatel').classes('w-44')
            f_nazev     = ui.input(label='Název akce').classes('w-60')
            f_od = ui.input(label='Termín od').props('type=date').classes('w-44')
            f_do = ui.input(label='Termín do').props('type=date').classes('w-44')

            f_aktivita.on_value_change(lambda _: _aktualizuj())
            f_nakupci.on_value_change(lambda _: _aktualizuj())
            f_dodavatel.on_value_change(lambda _: _aktualizuj())
            f_nazev.on_value_change(lambda _: _aktualizuj())
            f_od.on_value_change(lambda _: _aktualizuj())
            f_do.on_value_change(lambda _: _aktualizuj())

            def reset_filtr():
                f_aktivita.set_value('')
                f_nakupci.set_value('')
                f_dodavatel.set_value('')
                f_nazev.set_value('')
                f_od.set_value('')
                f_do.set_value('')
                for k in filtr:
                    filtr[k] = ''
                _tabulka.refresh()

            ui.button(icon='clear', on_click=reset_filtr) \
              .props('flat round').classes('text-gray-400 hover:text-gray-700 self-end').tooltip('Resetovat filtry')

    # ── Akční tlačítka ────────────────────────────────────────────────────
    with ui.row().classes('w-full items-center gap-3 mb-4 flex-wrap'):
        if _muze_zalozit(vsechna_prava):
            ui.button(
                'Nová aktivita', icon='add',
                on_click=lambda: _dialog_formular(
                    vsechna_prava, user_name, None, _tabulka.refresh),
            ).classes('bg-green-600 hover:bg-green-700 text-white font-bold')

            ui.button(
                'Import z Excelu', icon='upload_file',
                on_click=lambda: _dialog_import_excel(
                    vsechna_prava, user_name, _tabulka.refresh),
            ).classes('bg-teal-600 hover:bg-teal-700 text-white font-bold')

        ui.button(
            'Export CSV', icon='download',
            on_click=lambda: _export_csv(vsechna_prava, filtr),
        ).classes('bg-gray-700 hover:bg-gray-800 text-white font-bold')

    # ── Refreshovatelná tabulka ───────────────────────────────────────────
    @ui.refreshable
    def _tabulka():
        radky = _nacti_radky(filtr, vsechna_prava)

        _fmt_datum = 'val => val ? new Date(val).toLocaleDateString("cs-CZ") : ""'
        _fmt_cislo = 'val => val !== "" && val !== null ? Number(val).toLocaleString("cs-CZ",{minimumFractionDigits:2}) : ""'

        columns = [
            {'name': 'akce',                'label': '',                                        'field': 'id',                  'align': 'center', 'sortable': False},
            {'name': 'id',                  'label': '#',                                       'field': 'id',                  'align': 'right',  'sortable': True},
            {'name': 'aktivita',            'label': 'Aktivita (stav)',                         'field': 'aktivita',            'align': 'left',   'sortable': True},
            {'name': 'nakupci',             'label': 'Nákupčí',                                 'field': 'nakupci',             'align': 'left',   'sortable': True},
            {'name': 'dodavatel',           'label': 'Dodavatel',                               'field': 'dodavatel',           'align': 'left',   'sortable': True},
            {'name': 'nazev_akce',          'label': 'Název akce',                              'field': 'nazev_akce',          'align': 'left',   'sortable': True},
            {'name': 'jak_vyuctovano',      'label': 'Jak bude vyúčtováno',                     'field': 'jak_vyuctovano',      'align': 'left',   'sortable': True},
            {'name': 'reseni_provozovna',   'label': 'Řešení na provozovně',                    'field': 'reseni_provozovna',   'align': 'left',   'sortable': True},
            {'name': 'termin_nakupu_od',    'label': 'Termín nákupů od',                        'field': 'termin_nakupu_od',    'align': 'center', 'sortable': True,  ':format': _fmt_datum},
            {'name': 'termin_nakupu_do',    'label': 'Termín nákupů do',                        'field': 'termin_nakupu_do',    'align': 'center', 'sortable': True,  ':format': _fmt_datum},
            {'name': 'termin_prodeju_od',   'label': 'Termín prodejů od',                       'field': 'termin_prodeju_od',   'align': 'center', 'sortable': True,  ':format': _fmt_datum},
            {'name': 'termin_prodeju_do',   'label': 'Termín prodejů do',                       'field': 'termin_prodeju_do',   'align': 'center', 'sortable': True,  ':format': _fmt_datum},
            {'name': 'zasoby',              'label': 'Zásoby',                                  'field': 'zasoby',              'align': 'center', 'sortable': True},
            {'name': 'nakupci_kontrola',    'label': 'Nákupčí kontrola',                        'field': 'nakupci_kontrola',    'align': 'center', 'sortable': True},
            {'name': 'info_aktivita',       'label': 'Informace k aktivitě',                    'field': 'info_aktivita',       'align': 'left',   'sortable': False},
            {'name': 'info_pobocky',        'label': 'Informace pro pobočky',                   'field': 'info_pobocky',        'align': 'left',   'sortable': False},
            {'name': 'castka_bez_dph',      'label': 'Částka bez DPH (Kč)',                     'field': 'castka_bez_dph',      'align': 'left',   'sortable': True},
            {'name': 'cislo_dokladu',       'label': 'Číslo dokladu',                           'field': 'cislo_dokladu',       'align': 'left',   'sortable': True},
            {'name': 'proouctoval_prov',    'label': 'Proúčtoval provozovnám',                  'field': 'proouctoval_prov',    'align': 'left',   'sortable': False},
            {'name': 'kompenzovano',        'label': 'Kompenzováno',                             'field': 'kompenzovano',        'align': 'left',   'sortable': False},
            {'name': 'kompenzace_mj_kc',    'label': 'Kompenzace MJ/Kč',                        'field': 'kompenzace_mj_kc',    'align': 'left',   'sortable': False},
            {'name': 'kompenzace_kc_za_mj', 'label': 'Kč/MJ',                                  'field': 'kompenzace_kc_za_mj', 'align': 'right',  'sortable': True,  ':format': _fmt_cislo},
            {'name': 'kod_zbozi',           'label': 'Kód zboží',                               'field': 'kod_zbozi',           'align': 'left',   'sortable': False},
            {'name': 'nazev_zbozi',         'label': 'Název zboží',                             'field': 'nazev_zbozi',         'align': 'left',   'sortable': False},
            {'name': 'stav_ao',             'label': 'Stav AO',                                 'field': 'stav_ao',             'align': 'left',   'sortable': False},
            {'name': 'vytvoril',            'label': 'Vytvořil',                                'field': 'vytvoril',            'align': 'left',   'sortable': True},
            {'name': 'upraveno',            'label': 'Upraveno',                                'field': 'upraveno',            'align': 'center', 'sortable': True},
        ]

        if not radky:
            with ui.column().classes('w-full items-center py-16 gap-3'):
                ui.icon('inbox', size='3rem', color='grey-4')
                ui.label('Žádné záznamy neodpovídají filtru.') \
                  .classes('text-gray-400 italic')
            return

        with ui.table(columns=columns, rows=radky, row_key='id') \
             .classes('w-full shadow-sm rounded-xl') \
             .style('max-width:100vw;overflow-x:auto;max-height:47em') as tbl:
            tbl.props('flat bordered dense :row-class="row => row._prekcrocen ? \'bg-red-50 text-red-900\' : \'\'"')

            # Slot: top — tlačítka pro horizontální posun (náhrada za scrollbar dole)
            tbl.add_slot('top', '''
                <div class="flex items-center gap-0.5 py-0.5 w-full">
                    <span class="text-xs text-grey-5 mr-1" style="white-space:nowrap">posun:</span>
                    <q-btn flat dense round icon="first_page" size="xs" color="dark"
                           @click="() => { var m = $el.closest('.q-table__container').querySelector('.q-table__middle'); if(m) m.scrollLeft = 0; }"
                           title="Začátek" />
                    <q-btn flat dense round icon="chevron_left" size="xs" color="dark"
                           @click="() => { var m = $el.closest('.q-table__container').querySelector('.q-table__middle'); if(m) m.scrollLeft -= 300; }"
                           title="Posun vlevo" />
                    <q-btn flat dense round icon="chevron_right" size="xs" color="dark"
                           @click="() => { var m = $el.closest('.q-table__container').querySelector('.q-table__middle'); if(m) m.scrollLeft += 300; }"
                           title="Posun vpravo" />
                    <q-btn flat dense round icon="last_page" size="xs" color="dark"
                           @click="() => { var m = $el.closest('.q-table__container').querySelector('.q-table__middle'); if(m) m.scrollLeft = 99999; }"
                           title="Konec" />
                </div>
            ''')

            # Slot: akce (edit, historie, smazat)
            tbl.add_slot('body-cell-akce', '''
                <q-td :props="props" class="text-center" style="white-space:nowrap;padding:2px 4px">
                    <q-btn flat dense round icon="edit"    color="primary" size="sm"
                           @click="$parent.$emit('uprav',    props.row)" />
                    <q-btn flat dense round icon="history" color="grey"    size="sm"
                           @click="$parent.$emit('historie', props.row)" />
                    <q-btn v-if="props.row._muze_smazat"
                           flat dense round icon="delete" color="negative" size="sm"
                           @click="$parent.$emit('smaz',    props.row)" />
                </q-td>
            ''')

            # Slot: stav aktivity — barevný badge
            tbl.add_slot('body-cell-aktivita', '''
                <q-td :props="props">
                    <q-badge
                        :color="props.value.startsWith('9') ? 'grey'        :
                                props.value.startsWith('7') ? 'indigo'      :
                                props.value.startsWith('3') ? 'teal'        :
                                props.value.startsWith('2') ? 'orange'      :
                                props.value.startsWith('1') ? 'green'       : 'blue-grey'"
                        :label="props.value"
                        style="white-space:nowrap"
                    />
                </q-td>
            ''')

            # ── Event handlery ────────────────────────────────────────────

            def _evt_uprav(e):
                _dialog_formular(vsechna_prava, user_name, e.args, _tabulka.refresh)

            def _evt_historie(e):
                r = e.args
                _dialog_audit_historie(r.get('id'), r.get('nazev_akce', ''))

            def _evt_smaz(e):
                r = e.args
                rid   = r.get('id')
                nazev = r.get('nazev_akce', '?')

                with ui.dialog() as potvrdit_dlg, \
                     ui.card().classes('p-6 rounded-2xl shadow-2xl max-w-md'):
                    ui.icon('warning', size='3rem', color='red').classes('self-center mb-2')
                    ui.label(f'Smazat aktivitu #{rid}?') \
                      .classes('text-xl font-bold text-red-700 text-center')
                    ui.label(nazev).classes('text-gray-600 text-center mb-4 text-sm')

                    with ui.row().classes('w-full justify-end gap-3 mt-2'):
                        ui.button('Zrušit', on_click=potvrdit_dlg.close) \
                          .classes('bg-gray-200 hover:bg-gray-300 text-gray-800 font-bold')

                        def _potvrdit(r_id=rid, r_nazev=nazev, d=potvrdit_dlg):
                            conn2 = intranet_data.get_db_connection()
                            if not conn2:
                                ui.notify('Chyba připojení k databázi.', type='negative')
                                return
                            cur2 = conn2.cursor()
                            try:
                                _uloz_audit(r_id, user_name, 'smazani')
                                cur2.execute("DELETE FROM prod_aktivity WHERE id=%s", (r_id,))
                                conn2.commit()
                                intranet_logger.log_activity(
                                    user_name, 'Prodejní aktivity',
                                    f'Smazána aktivita #{r_id}: {r_nazev}')
                                ui.notify(f'Aktivita #{r_id} smazána.', type='warning')
                                d.close()
                                _tabulka.refresh()
                            except Exception as ex:
                                ui.notify(f'Chyba: {ex}', type='negative')
                            finally:
                                cur2.close()
                                conn2.close()

                        ui.button('Smazat', icon='delete', on_click=_potvrdit) \
                          .classes('bg-red-600 hover:bg-red-700 text-white font-bold')

                potvrdit_dlg.open()

            tbl.on('uprav',    _evt_uprav)
            tbl.on('historie', _evt_historie)
            tbl.on('smaz',     _evt_smaz)

        ui.label(f'Celkem záznamů: {len(radky)}') \
          .classes('text-xs text-gray-400 mt-2 text-right')

    _tabulka()
