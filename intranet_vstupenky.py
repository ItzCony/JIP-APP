"""Vstupenky Veletrh — dlaždice ve Formulářích ASM.

Žadatel zadá IČO zákazníka své pobočky; z Kontaktních údajů VO se dotáhne jméno,
dodací (K.) údaje, prodejní kanál a typ plátce, z Lupy na obchod obrat za
dostupné měsíce roku ROK_OBRATU (vypnutá Lupa → pole šedé). Odeslaná žádost jde
Kontrole vstupenek (e-mail s odkazem přímo na žádost), ta ji schválí, nebo
zamítne — žadateli pak přijde e-mail s výsledkem. Žadatel vidí jen své žádosti.

Práva: vstupenky_zadatel, vstupenky_kontrola (+ 'vse').
Hlavní administrátor (iduser=1) vidí všechny žádosti a smí je trvale smazat.
"""
from __future__ import annotations

import asyncio

from nicegui import app, background_tasks, ui

import intranet_data
import intranet_emaily
import intranet_logger

PRAVO_ZADATEL = 'vstupenky_zadatel'
PRAVO_KONTROLA = 'vstupenky_kontrola'
ROK_OBRATU = 2026        # obrat z Lupy se sčítá za dostupné měsíce tohoto roku
MAX_POCET = 50
NA_STRANU = 25           # žádostí na stránku v seznamu
VAROVANI_POBOCKA = 'Zadané IČO neodpovídá Vaší pobočce'

_STAVY = {
    'ceka':      ('Čeká na kontrolu', 'orange'),
    'schvaleno': ('Schváleno', 'green'),
    'zamitnuto': ('Zamítnuto', 'red'),
}


def je_zadatel(prava) -> bool:
    return 'vse' in prava or PRAVO_ZADATEL in prava


def je_kontrola(prava) -> bool:
    return 'vse' in prava or PRAVO_KONTROLA in prava


def vidi(prava) -> bool:
    """Dlaždice patří pod Formuláře ASM → řídí ji i jejich přepínač modulu."""
    if not (je_zadatel(prava) or je_kontrola(prava)):
        return False
    return bool(intranet_data.nacti_nastaveni_intranetu().get('asm_zapnuty', True))


# =========================================================
# DATABÁZE (blokující — volat přes asyncio.to_thread)
# =========================================================
_DB_HOTOVO = False


def inicializace_db():
    global _DB_HOTOVO
    if _DB_HOTOVO:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS asm_vstupenky (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ico VARCHAR(40) NOT NULL,
                jmeno VARCHAR(255),
                k_jmeno VARCHAR(255),
                k_ulice VARCHAR(255),
                k_mesto VARCHAR(255),
                k_psc VARCHAR(40),
                prodejni_kanal VARCHAR(40),
                prodejni_kanal_popis VARCHAR(255),
                typ_platice VARCHAR(120),
                obrat_kc DOUBLE NULL,
                obrat_mesice VARCHAR(40),
                pocet INT NOT NULL DEFAULT 1,
                poznamka TEXT,
                zadatel_id INT NOT NULL,
                zadatel_jmeno VARCHAR(255),
                zadano DATETIME DEFAULT CURRENT_TIMESTAMP,
                stav VARCHAR(20) NOT NULL DEFAULT 'ceka',
                rozhodl_jmeno VARCHAR(255),
                rozhodnuto DATETIME NULL,
                duvod VARCHAR(1000),
                INDEX idx_zadatel (zadatel_id),
                INDEX idx_stav (stav)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
        _DB_HOTOVO = True
    except Exception as e:
        print(f'[vstupenky] inicializace_db: {e}')
    finally:
        conn.close()


def _sql(dotaz, parametry=(), vysledek=None):
    """Jeden dotaz ve vlastním spojení.
    vysledek: 'vse' | 'jeden' | None (= commit, vrací (lastrowid, rowcount))."""
    conn = intranet_data.get_db_connection()
    if not conn:
        raise RuntimeError('Databáze není dostupná.')
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(dotaz, parametry)
        if vysledek == 'vse':
            return cur.fetchall()
        if vysledek == 'jeden':
            return cur.fetchone()
        conn.commit()
        return cur.lastrowid, cur.rowcount
    finally:
        conn.close()


def kanal_a_popis(prodejni_doba):
    """„034Q JIP Plzeň" → ("034Q", "JIP Plzeň")."""
    kanal, _, popis = str(prodejni_doba or '').strip().partition(' ')
    return kanal, popis.strip()


def lupa_zapnuta() -> bool:
    return bool(intranet_data.nacti_nastaveni_intranetu().get('lupa_zapnuty', True))


_MESICE = ('Leden', 'Únor', 'Březen', 'Duben', 'Květen', 'Červen', 'Červenec',
           'Srpen', 'Září', 'Říjen', 'Listopad', 'Prosinec')


def rozpad_obratu(ico):
    """[(měsíc, obrat_kc)] za ROK_OBRATU z Lupy na obchod.
    None = Lupa vypnutá nebo její data nejsou dostupná."""
    if not lupa_zapnuta():
        return None
    try:
        rows = _sql('SELECT mesic, SUM(obrat_kc) AS kc FROM lupa_obrat '
                    'WHERE ico=%s AND rok=%s GROUP BY mesic ORDER BY mesic',
                    (ico, ROK_OBRATU), 'vse')
    except Exception as e:
        print(f'[vstupenky] rozpad_obratu {ico}: {e}')
        return None
    return [(int(r['mesic']), float(r['kc'] or 0)) for r in rows]


def obrat_z_lupy(ico):
    """(obrat_kc, 'měsíce/rok') za ROK_OBRATU z Lupy na obchod.
    None = Lupa vypnutá nebo její data nejsou dostupná (pole pak zešedne)."""
    rozpad = rozpad_obratu(ico)
    if rozpad is None:
        return None
    if not rozpad:
        return 0.0, ''
    m = [mesic for mesic, _ in rozpad]
    obdobi = f'{m[0]}/{ROK_OBRATU}' if m[0] == m[-1] else f'{m[0]}–{m[-1]}/{ROK_OBRATU}'
    return sum(kc for _, kc in rozpad), obdobi


def dohledej(ico_vstup, user_id, prava):
    """IČ 10, která žadatel smí k zadanému IČO použít. Vrací (varianty, chyba).
    Zadané IČ 8 → všechna IČ 10 pod ním; plné IČ 10 → jen to jedno. Mimo 'vse'
    se nechají jen IČ 10 poboček uživatele (stejně jako u navýšení limitů)."""
    import intranet_asm   # tady: intranet_asm importuje tento modul
    ico = (ico_vstup or '').strip().upper()
    if not ico:
        return [], 'Zadejte IČO.'
    _, varianty = intranet_asm._kontakty_dle_ico8(ico[:8])
    if len(ico) > 8:
        varianty = [v for v in varianty if str(v.get('ico') or '').strip().upper() == ico]
    if not varianty:
        return [], 'IČO nebylo nalezeno v Kontaktních údajích VO.'
    if 'vse' not in prava:
        pobocky = intranet_asm._pobocky_uzivatele(user_id)
        if not pobocky:
            return [], ('Nemáte v profilu přiřazenou pobočku, IČO nelze ověřit. '
                        'Obraťte se na správce portálu.')
        # Řádek bez rozpoznatelného kódu pobočky se nefiltruje (jako v ostatních
        # formulářích ASM — _kod_pobocky vrací "").
        varianty = [v for v in varianty
                    if intranet_asm._kod_pobocky(v.get('prodejni_doba')) in ('', *pobocky)]
        if not varianty:
            return [], VAROVANI_POBOCKA
    return varianty, ''


def udaje_zakaznika(v) -> dict:
    kanal, popis = kanal_a_popis(v.get('prodejni_doba'))
    return {
        'ico': str(v.get('ico') or '').strip(),
        'jmeno': v.get('jmeno') or '',
        'k_jmeno': v.get('k_jmeno') or '',
        'k_ulice': v.get('k_ulice') or '',
        'k_mesto': v.get('k_mesto') or '',
        'k_psc': v.get('k_psc') or '',
        'prodejni_kanal': kanal,
        'prodejni_kanal_popis': popis,
        'typ_platice': v.get('typ_platice') or '',
    }


def uloz_zadost(z, obrat, pocet, poznamka, user_id, user_name) -> int:
    kc, mesice = obrat if obrat is not None else (None, '')
    lid, _ = _sql(
        'INSERT INTO asm_vstupenky (ico, jmeno, k_jmeno, k_ulice, k_mesto, k_psc, '
        'prodejni_kanal, prodejni_kanal_popis, typ_platice, obrat_kc, obrat_mesice, '
        'pocet, poznamka, zadatel_id, zadatel_jmeno) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (z['ico'], z['jmeno'], z['k_jmeno'], z['k_ulice'], z['k_mesto'], z['k_psc'],
         z['prodejni_kanal'], z['prodejni_kanal_popis'], z['typ_platice'], kc, mesice,
         pocet, poznamka or None, user_id, user_name))
    return lid


def nacti_zadosti(zadatel_id=None, stav=None) -> list:
    podm, par = [], []
    if zadatel_id is not None:
        podm.append('zadatel_id=%s')
        par.append(zadatel_id)
    if stav:
        podm.append('stav=%s')
        par.append(stav)
    where = f"WHERE {' AND '.join(podm)}" if podm else ''
    return _sql(f'SELECT * FROM asm_vstupenky {where} ORDER BY id DESC', tuple(par), 'vse')


def nacti_zadost(zid):
    return _sql('SELECT * FROM asm_vstupenky WHERE id=%s', (zid,), 'jeden')


def smaz_zadost(zid) -> bool:
    """Trvale smaže žádost (jen hlavní admin — kontroluje volající). False = už neexistuje."""
    _, n = _sql('DELETE FROM asm_vstupenky WHERE id=%s', (zid,))
    return n == 1


def rozhodni(zid, schvalit, kdo, duvod='') -> bool:
    """Schválí / zamítne čekající žádost. False = mezitím rozhodl někdo jiný."""
    _, n = _sql("UPDATE asm_vstupenky SET stav=%s, rozhodl_jmeno=%s, rozhodnuto=NOW(), "
                "duvod=%s WHERE id=%s AND stav='ceka'",
                ('schvaleno' if schvalit else 'zamitnuto', kdo, duvod or None, zid))
    return n == 1


# =========================================================
# E-MAILY (blokující — volat přes asyncio.to_thread)
# =========================================================
def _url(zid) -> str:
    u = getattr(intranet_data, 'APP_URL', '') or ''
    return f'{u}/asm?vstupenka={int(zid)}' if u else ''


def _kc(x) -> str:
    return f"{x:,.0f}".replace(',', ' ') + ' Kč'


# Obrat nad tuto hranici žadatel NEVIDÍ přesně — zobrazí se jen „> 1 Milion Kč"
# (celkem i jednotlivé měsíce). Kontrola vidí přesnou hodnotu (jako Office ve Formulářích ASM).
OBRAT_LIMIT = 1_000_000
OBRAT_LIMIT_TEXT = '> 1 Milion Kč'


def _obrat_kc(kc, maskovat) -> str:
    """Obrat v Kč; maskovat=True (žadatel) → nad OBRAT_LIMIT jen „> 1 Milion Kč"."""
    return OBRAT_LIMIT_TEXT if maskovat and kc > OBRAT_LIMIT else _kc(kc)


def _obrat_text(z, maskovat=False) -> str:
    if z.get('obrat_kc') is None:
        return 'nedostupný (Lupa na obchod)'
    if not z.get('obrat_mesice'):
        return f'bez obratu v roce {ROK_OBRATU}'
    return f"{_obrat_kc(z['obrat_kc'], maskovat)} ({z['obrat_mesice']})"


def _shrnuti(z) -> str:
    radky = [('IČO', z.get('ico')), ('Jméno', z.get('jmeno')), ('K Jméno', z.get('k_jmeno')),
             ('K Ulice', z.get('k_ulice')), ('K Město', z.get('k_mesto')), ('K PSČ', z.get('k_psc')),
             ('Prodejní kanál', z.get('prodejni_kanal')),
             ('Prodejní kanál – popis', z.get('prodejni_kanal_popis')),
             ('Typ plátce', z.get('typ_platice')), (f'Obrat {ROK_OBRATU}', _obrat_text(z)),
             ('Počet vstupenek', z.get('pocet')), ('Poznámka', z.get('poznamka')),
             ('Žadatel', z.get('zadatel_jmeno'))]
    return '\n'.join(f'{k}: {v}' for k, v in radky if v not in (None, ''))


def _posli(prijemci, predmet, text, zid):
    odkaz = _url(zid)
    if odkaz:
        text = f'{text}\n\nOtevřít žádost v portálu: {odkaz}'
    for p in dict.fromkeys(prijemci):
        if p and '@' in p:
            try:
                intranet_emaily.odesli_upozorneni_email(p, predmet, text)
            except Exception as e:
                print(f'[vstupenky] e-mail {p}: {e}')


def notifikuj_novou(zid):
    z = nacti_zadost(zid)
    if not z:
        return
    # Jen držitelé práva Kontrola — 'vse' (admin) se záměrně nepřidává.
    prijemci = intranet_data.ziskej_emaily_s_pravem(PRAVO_KONTROLA)
    _posli(prijemci, f"Nová žádost o vstupenky na veletrh — {z['jmeno'] or z['ico']}",
           f"{z['zadatel_jmeno']} žádá o vstupenky na veletrh JIP. Žádost čeká na "
           f"schválení.\n\n{_shrnuti(z)}", zid)


def notifikuj_rozhodnuti(zid):
    z = nacti_zadost(zid)
    if not z:
        return
    r = _sql('SELECT email FROM user WHERE iduser=%s', (z['zadatel_id'],), 'jeden')
    email = (r or {}).get('email') or ''
    zak = f"{z['jmeno'] or z['ico']} (IČO {z['ico']})"
    if z['stav'] == 'schvaleno':
        predmet = f"Vstupenky na veletrh schváleny — {z['jmeno'] or z['ico']}"
        text = (f"Vaše žádost o vstupenky na veletrh JIP pro zákazníka {zak} byla "
                f"schválena ({z['pocet']} ks). Schválil(a): {z['rozhodl_jmeno']}.")
    else:
        predmet = f"Žádost o vstupenky zamítnuta — {z['jmeno'] or z['ico']}"
        text = (f"Vaše žádost o vstupenky na veletrh JIP pro zákazníka {zak} byla "
                f"zamítnuta. Zamítl(a): {z['rozhodl_jmeno']}.")
        if z.get('duvod'):
            text += f"\nDůvod: {z['duvod']}"
    _posli([email], predmet, text, zid)


def _na_pozadi(fn, *args):
    """E-maily neblokují UI ani event loop."""
    background_tasks.create(asyncio.to_thread(fn, *args), name=f'vstupenky.{fn.__name__}')


# =========================================================
# UI
# =========================================================
def _dt(d) -> str:
    return d.strftime('%d.%m.%Y %H:%M') if d else ''


_POLE = (('jmeno', 'Jméno'), ('k_jmeno', 'K Jméno'), ('k_ulice', 'K Ulice'),
         ('k_mesto', 'K Město'), ('k_psc', 'K PSČ'), ('prodejni_kanal', 'Prodejní kanál'),
         ('prodejni_kanal_popis', 'Prodejní kanál – popis'), ('typ_platice', 'Typ plátce'))


async def vykresli_vstupenky(user_id, user_name, vsechna_prava, deep_id=None):
    await asyncio.to_thread(inicializace_db)
    # Hlavní administrátor (iduser=1) vidí vždy všechny žádosti a smí je trvale mazat.
    hlavni_admin = user_id == intranet_data.SKRYTY_ADMIN_ID
    zadatel = je_zadatel(vsechna_prava)
    kontrola = je_kontrola(vsechna_prava) or hlavni_admin
    maskovat = not kontrola   # obrat nad 1 mil. vidí přesně jen Kontrola
    stav = {'sekce': app.storage.user.get('vstupenky_sekce')
            or ('kontrola' if kontrola else 'moje'),
            'filtr': '' if hlavni_admin else 'ceka', 'strana': 1}
    if stav['sekce'] == 'kontrola' and not kontrola:
        stav['sekce'] = 'moje'
    if stav['sekce'] == 'moje' and not zadatel:
        stav['sekce'] = 'kontrola'

    # Dialogy bydlí mimo obnovovaný seznam — refresh je nesmaže pod rukama.
    host = ui.element('div')

    # ── Rozpad obratu po měsících (tlačítko „i" u obratu) ──
    async def _rozpad(ico):
        rozpad = await asyncio.to_thread(rozpad_obratu, ico)
        with host, ui.dialog() as dlg, ui.card().classes('min-w-[340px]'):
            ui.label(f'Obrat {ROK_OBRATU} po měsících').classes('text-lg font-bold')
            ui.label(f'IČO {ico} · aktuální data z Lupy na obchod') \
                .classes('text-xs text-gray-500')
            if rozpad is None:
                ui.label('Obrat z Lupy není dostupný.').classes('text-gray-400 italic py-2')
            elif not rozpad:
                ui.label(f'Bez obratu v roce {ROK_OBRATU}.').classes('text-gray-400 italic py-2')
            else:
                with ui.grid(columns=2).classes('w-full gap-x-8 gap-y-1 mt-2'):
                    for mesic, kc in rozpad:
                        ui.label(_MESICE[mesic - 1]).classes('text-gray-700')
                        ui.label(_obrat_kc(kc, maskovat)) \
                            .classes('text-right text-gray-800 tabular-nums')
                ui.separator()
                with ui.row().classes('w-full justify-between'):
                    ui.label('Celkem').classes('font-semibold')
                    ui.label(_obrat_kc(sum(kc for _, kc in rozpad), maskovat)) \
                        .classes('font-semibold tabular-nums')
            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg.close).props('flat')
        dlg.on('hide', dlg.delete)
        dlg.open()

    # ── Detail žádosti (+ schválení / zamítnutí pro Kontrolu) ──
    async def _detail(zid):
        z = await asyncio.to_thread(nacti_zadost, zid)
        # Žadatel smí jen své žádosti — kontrolujeme na serveru, ne jen v seznamu.
        if not z or not (kontrola or z['zadatel_id'] == user_id):
            ui.notify('Žádost nenalezena nebo k ní nemáte přístup.', type='warning')
            return
        with host, ui.dialog() as dlg, ui.card().classes('w-[720px] max-w-full'):
            with ui.grid(columns=2).classes('w-full gap-x-6 gap-y-2'):
                for k, label in (('ico', 'IČO'), *_POLE):
                    with ui.column().classes('gap-0'):
                        ui.label(label).classes('text-xs text-gray-500')
                        ui.label(str(z.get(k) or '—')).classes('text-gray-800 font-medium')
                with ui.column().classes('gap-0'):
                    ui.label(f'Obrat {ROK_OBRATU} (Lupa)').classes('text-xs text-gray-500')
                    with ui.row().classes('items-center gap-1 no-wrap'):
                        ui.label(_obrat_text(z, maskovat)).classes(
                            'font-medium ' + ('text-gray-400' if z.get('obrat_kc') is None
                                              else 'text-gray-800'))
                        if z.get('obrat_mesice'):
                            ui.button(icon='info', on_click=lambda: _rozpad(z['ico'])) \
                                .props('flat round dense size=sm color=primary') \
                                .tooltip('Rozpad obratu po měsících')
                with ui.column().classes('gap-0'):
                    ui.label('Počet vstupenek').classes('text-xs text-gray-500')
                    ui.label(str(z['pocet'])).classes('text-gray-800 font-medium')
            if z.get('poznamka'):
                ui.label('Poznámka').classes('text-xs text-gray-500 mt-2')
                ui.label(z['poznamka']).classes('text-gray-800 whitespace-pre-wrap')
            if z['stav'] != 'ceka':
                ui.separator()
                ui.label(f"Rozhodl(a): {z['rozhodl_jmeno']} · {_dt(z['rozhodnuto'])}") \
                    .classes('text-sm text-gray-600')
                if z.get('duvod'):
                    ui.label(f"Důvod: {z['duvod']}").classes('text-sm text-red-700')

            duvod_w = None
            if kontrola and z['stav'] == 'ceka':
                duvod_w = ui.textarea('Důvod zamítnutí (nepovinné)') \
                    .props('outlined autogrow').classes('w-full mt-2')

            async def _rozhodni(schvalit):
                duvod = '' if schvalit else (duvod_w.value or '').strip()
                ok = await asyncio.to_thread(rozhodni, zid, schvalit, user_name, duvod)
                if not ok:
                    ui.notify('O žádosti už mezitím rozhodl někdo jiný.', type='warning')
                else:
                    _na_pozadi(notifikuj_rozhodnuti, zid)
                    intranet_logger.log_activity(
                        user_name, 'Schválení žádosti' if schvalit else 'Zamítnutí žádosti',
                        f"Vstupenky Veletrh: žádost č. {zid} ({z['ico']})")
                    ui.notify('Žádost schválena.' if schvalit else 'Žádost zamítnuta.',
                              type='positive' if schvalit else 'info')
                dlg.close()
                _seznam.refresh()

            def _smazat():
                with host, ui.dialog() as potvrd, ui.card():
                    ui.label(f"Trvale smazat žádost pro {z['k_jmeno'] or z['jmeno'] or z['ico']} "
                             f"(IČO {z['ico']})?").classes('font-semibold')
                    ui.label('Tuto akci nelze vrátit.').classes('text-sm text-gray-500')

                    async def _ano():
                        if not hlavni_admin:   # pojistka i na serveru
                            return
                        ok = await asyncio.to_thread(smaz_zadost, zid)
                        if ok:
                            intranet_logger.log_activity(
                                user_name, 'Smazání žádosti',
                                f"Vstupenky Veletrh: žádost č. {zid} ({z['ico']})")
                            ui.notify('Žádost smazána.', type='positive')
                        else:
                            ui.notify('Žádost už neexistuje.', type='warning')
                        potvrd.close()
                        dlg.close()
                        _seznam.refresh()

                    with ui.row().classes('w-full justify-end gap-2 mt-2'):
                        ui.button('Zrušit', on_click=potvrd.close).props('flat')
                        ui.button('Smazat', icon='delete', on_click=_ano) \
                            .props('color=negative')
                potvrd.on('hide', potvrd.delete)
                potvrd.open()

            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                if hlavni_admin:
                    ui.button('Smazat', icon='delete', on_click=_smazat) \
                        .props('flat color=negative')
                    ui.space()
                ui.button('Zavřít', on_click=dlg.close).props('flat')
                if duvod_w is not None:
                    ui.button('Zamítnout', icon='block',
                              on_click=lambda: _rozhodni(False)).props('color=negative')
                    ui.button('Schválit', icon='check',
                              on_click=lambda: _rozhodni(True)).props('color=positive')
        dlg.on('hide', dlg.delete)
        dlg.open()

    # ── Nová žádost ──
    def _nova():
        sv = {'varianty': [], 'vybrany': None, 'obrat': None}
        with host, ui.dialog() as dlg, ui.card().classes('w-[720px] max-w-full'):
            ui.label('Nová žádost o vstupenky na veletrh').classes('text-xl font-bold')
            ico_w = ui.input('IČO *').props('outlined dense autofocus').classes('w-56')
            varovani = ui.label('').classes('text-sm text-red-700 font-semibold')
            varovani.set_visibility(False)
            ic10_w = ui.select({}, label='IČ 10 (provozovna)').props('outlined dense') \
                .classes('w-full')
            ic10_w.set_visibility(False)
            pole = {}
            with ui.grid(columns=2).classes('w-full gap-3'):
                for k, label in _POLE:
                    pole[k] = ui.input(label).props('outlined dense readonly')
                obrat_w = ui.input(f'Obrat {ROK_OBRATU} (Lupa na obchod)') \
                    .props('outlined dense readonly')
                with obrat_w.add_slot('append'):
                    obrat_info = ui.button(
                        icon='info',
                        on_click=lambda: sv['vybrany'] and _rozpad(sv['vybrany']['ico'])) \
                        .props('flat round dense size=sm color=primary') \
                        .tooltip('Rozpad obratu po měsících')
                obrat_info.set_visibility(False)
                pocet_w = ui.number('Počet vstupenek *', value=1, min=1, max=MAX_POCET,
                                    precision=0).props('outlined dense')
            pozn_w = ui.textarea('Poznámka').props('outlined autogrow').classes('w-full')

            def _vypln(v):
                sv['vybrany'] = v
                z = udaje_zakaznika(v) if v else {}
                for k, w in pole.items():
                    w.value = z.get(k, '')

            def _ukaz_obrat(o):
                sv['obrat'] = o
                obrat_info.set_visibility(bool(o and o[1]))
                if o is None:
                    obrat_w.value = ('Modul Lupa na obchod je vypnutý' if not lupa_zapnuta()
                                     else 'Obrat z Lupy není dostupný')
                    obrat_w.props('disable')
                else:
                    obrat_w.props(remove='disable')
                    obrat_w.value = (f'{_obrat_kc(o[0], maskovat)} ({o[1]})' if o[1]
                                     else f'Bez obratu v roce {ROK_OBRATU}')

            async def _vyber_ic10(e=None):
                v = next((x for x in sv['varianty'] if x['ico'] == ic10_w.value), None)
                _vypln(v)
                if v:
                    _ukaz_obrat(await asyncio.to_thread(obrat_z_lupy, v['ico']))

            def _vycisti(chyba=''):
                sv['varianty'] = []
                varovani.text = chyba
                varovani.set_visibility(bool(chyba))
                ic10_w.set_visibility(False)
                _vypln(None)
                obrat_w.props(remove='disable')
                obrat_w.value = ''
                sv['obrat'] = None
                obrat_info.set_visibility(False)

            async def _dotahni():
                zadane = (ico_w.value or '').strip()
                if len(zadane) < 8:          # IČO ještě není dopsané
                    _vycisti()
                    return
                varianty, chyba = await asyncio.to_thread(
                    dohledej, zadane, user_id, vsechna_prava)
                if (ico_w.value or '').strip() != zadane:
                    return                   # mezitím přepsáno — výsledek je pro staré IČO
                if chyba:
                    _vycisti(chyba)
                    ui.notify(chyba, type='warning', position='top')
                    return
                sv['varianty'] = varianty
                varovani.set_visibility(False)
                ic10_w.set_options({
                    v['ico']: f"{v['ico']} — {v.get('k_jmeno') or v.get('jmeno') or ''}"
                              f"{', ' + v['k_mesto'] if v.get('k_mesto') else ''}"
                    for v in varianty})
                ic10_w.set_visibility(len(varianty) > 1)
                prvni = next((v for v in varianty if v['ico'].upper() == zadane.upper()),
                             varianty[0])
                if ic10_w.value != prvni['ico']:
                    ic10_w.set_value(prvni['ico'])   # spustí _vyber_ic10
                else:
                    await _vyber_ic10()

            async def _po_zmene_ico(_e):
                # Dotažení až po krátké pauze v psaní, ne na každý znak.
                sv['seq'] = moje = sv.get('seq', 0) + 1
                await asyncio.sleep(0.5)
                if sv['seq'] == moje:
                    await _dotahni()

            ico_w.on_value_change(_po_zmene_ico)
            ic10_w.on_value_change(_vyber_ic10)

            async def _odeslat():
                # Pobočku ověř znovu na serveru z aktuálního IČO — vstup se mohl
                # po dotažení změnit.
                varianty, chyba = await asyncio.to_thread(
                    dohledej, ico_w.value, user_id, vsechna_prava)
                v = sv['vybrany']
                if chyba or not v or v['ico'] not in {x['ico'] for x in varianty}:
                    ui.notify(chyba or 'Zadejte IČO a počkejte na dotažení údajů.',
                              type='warning', position='top')
                    return
                try:
                    pocet = int(pocet_w.value or 0)
                except (TypeError, ValueError):
                    pocet = 0
                if not 1 <= pocet <= MAX_POCET:
                    ui.notify(f'Počet vstupenek musí být 1–{MAX_POCET}.', type='warning')
                    return
                z = udaje_zakaznika(next(x for x in varianty if x['ico'] == v['ico']))
                zid = await asyncio.to_thread(uloz_zadost, z, sv['obrat'], pocet,
                                              (pozn_w.value or '').strip(), user_id, user_name)
                _na_pozadi(notifikuj_novou, zid)
                intranet_logger.log_activity(user_name, 'Podání žádosti',
                                             f"Vstupenky Veletrh: žádost č. {zid} ({z['ico']})")
                ui.notify('Žádost odeslána ke kontrole.', type='positive', position='top')
                dlg.close()
                stav['sekce'] = 'moje'
                _seznam.refresh()

            with ui.row().classes('w-full justify-end gap-2 mt-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat')
                ui.button('Odeslat žádost', icon='send', on_click=_odeslat) \
                    .props('color=primary')
        dlg.on('hide', dlg.delete)
        dlg.open()

    # ── Seznam ──
    def _prepni(sekce=None, filtr=None, strana=None):
        if sekce:
            stav['sekce'] = sekce
            app.storage.user['vstupenky_sekce'] = sekce
        if filtr is not None:
            stav['filtr'] = filtr
        stav['strana'] = strana or 1   # změna sekce/filtru → zpět na 1. stránku
        _seznam.refresh()

    @ui.refreshable
    async def _seznam():
        moje = stav['sekce'] == 'moje'
        with ui.row().classes('w-full items-center gap-3 mb-4'):
            if zadatel and kontrola:
                ui.toggle({'moje': 'Moje žádosti', 'kontrola': 'Žádosti o vstupenky'},
                          value=stav['sekce'], on_change=lambda e: _prepni(sekce=e.value)) \
                    .props('no-caps rounded unelevated toggle-color=pink')
            else:
                ui.label('Moje žádosti' if moje else 'Žádosti o vstupenky') \
                    .classes('text-lg font-semibold text-gray-700')
            if not moje:
                ui.toggle({'ceka': 'Čeká', 'schvaleno': 'Schválené',
                           'zamitnuto': 'Zamítnuté', '': 'Vše'},
                          value=stav['filtr'], on_change=lambda e: _prepni(filtr=e.value)) \
                    .props('no-caps rounded unelevated')
            ui.space()
            if zadatel:
                ui.button('Nová žádost', icon='add', on_click=_nova) \
                    .props('color=pink no-caps unelevated')

        try:
            rows = await asyncio.to_thread(
                nacti_zadosti, user_id if moje else None, None if moje else stav['filtr'])
        except Exception as e:
            ui.label(f'Žádosti nelze načíst: {e}').classes('text-red-600')
            return
        if not rows:
            ui.label('Žádné žádosti.').classes('text-gray-400 italic py-8')
            return
        # Karty ve stylu Cenopřípadu: barevný okraj dle stavu, název, badge, meta, datum.
        okraje = {'ceka': 'border-orange-400', 'schvaleno': 'border-green-400',
                  'zamitnuto': 'border-red-400'}
        stran = (len(rows) + NA_STRANU - 1) // NA_STRANU
        strana = min(max(stav['strana'], 1), stran)
        with ui.column().classes('w-full gap-2'):
            for z in rows[(strana - 1) * NA_STRANU:strana * NA_STRANU]:
                popis, barva = _STAVY.get(z['stav'], (z['stav'], 'grey'))
                with ui.card().classes(
                        f"w-full border-l-4 {okraje.get(z['stav'], 'border-gray-400')} "
                        'hover:bg-gray-50 cursor-pointer transition-colors p-3 rounded-lg') \
                        .on('click', lambda z=z: _detail(z['id'])):
                    with ui.row().classes('w-full items-center gap-3 no-wrap'):
                        ui.label(f"{z['k_jmeno'] or z['jmeno'] or '—'} ({z['ico']})") \
                            .classes('font-semibold text-gray-800 flex-1')
                        ui.badge(popis, color=barva).props('outline')
                        meta = [z['prodejni_kanal'] or '', f"{z['pocet']} ks"]
                        if not moje:
                            meta.insert(0, z['zadatel_jmeno'] or '')
                        ui.label(' · '.join(m for m in meta if m)) \
                            .classes('text-xs text-gray-500')
                        ui.label(_dt(z['zadano'])).classes('text-xs text-gray-400')
        if stran > 1:
            with ui.row().classes('w-full items-center justify-center gap-3 mt-2'):
                ui.pagination(1, stran, direction_links=True, value=strana,
                              on_change=lambda e: _prepni(strana=e.value)) \
                    .props('max-pages=7 boundary-numbers')
                ui.label(f'{len(rows)} žádostí').classes('text-xs text-gray-400')

    await _seznam()
    if deep_id:
        ui.timer(0.25, lambda: _detail(deep_id), once=True)
