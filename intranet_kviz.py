from nicegui import ui, app, Client, run, background_tasks
import pandas as pd
import os
import re
import math
import time
import json
import zipfile
import asyncio
from openpyxl.styles import PatternFill, Alignment
from openpyxl.worksheet.table import Table, TableStyleInfo

from datetime import datetime, timedelta

import intranet_data
import intranet_logger
import intranet_session
from intranet_ui_utils import refreshable_na_klienta

dokoncene_testy = []
SOUBOR_NASTAVENI = 'nastaveni_kvizu.json'

# ─── výchozí nastavení jedné skupiny (OZ nebo ASM) ───────────────────────────
_SKUPINA_DEFAULT = {
    'pocet_otazek': 20,
    'casovy_limit_minuty': 30,
    'min_procento_uspesnosti': 80,
    'okruh_1_nazev': 'Okruh 1', 'okruh_1_pocet': 20, 'okruh_1_zapnut': True,
    'okruh_2_nazev': 'Okruh 2', 'okruh_2_pocet': 0,  'okruh_2_zapnut': False,
    'okruh_3_nazev': 'Okruh 3', 'okruh_3_pocet': 0,  'okruh_3_zapnut': False,
}

config = {
    'aktivni_skupina': 'oz',   # globální nastavení — který kvíz dostanou studenti
    # globální: celá obrazovka, ukončení testu při jejím opuštění, blokace kláves,
    # hlídání kliknutí mimo test (rudá tečka v monitoru)
    'kontrolni_mechanismus': True,
    'oz':  {**_SKUPINA_DEFAULT},
    'asm': {**_SKUPINA_DEFAULT},
}

# ─── načtení uloženého nastavení (s migrací ze starého flat formátu) ──────────
if os.path.exists(SOUBOR_NASTAVENI):
    try:
        with open(SOUBOR_NASTAVENI, 'r', encoding='utf-8') as f:
            nacteno = json.load(f)
        if 'oz' in nacteno and 'asm' in nacteno:
            # nový vnořený formát
            config['aktivni_skupina'] = nacteno.get('aktivni_skupina', 'oz')
            config['kontrolni_mechanismus'] = bool(nacteno.get('kontrolni_mechanismus', True))
            for typ in ('oz', 'asm'):
                for k in _SKUPINA_DEFAULT:
                    config[typ][k] = nacteno[typ].get(k, _SKUPINA_DEFAULT[k])
        elif 'pocet_otazek' in nacteno:
            # starý plochý formát — přenes do oz
            for k in _SKUPINA_DEFAULT:
                if k in nacteno:
                    config['oz'][k] = nacteno[k]
    except Exception:
        pass



def _kontrola_zapnuta() -> bool:
    """Kontrolní mechanismus (nastavení kvízu). Přepnout ho jde jen bez běžících
    testů (viz _pisici_test), takže se běžícímu testu pravidla nikdy nezmění."""
    return bool(config.get('kontrolni_mechanismus', True))


def _pisici_test() -> str:
    """Kdo má rozpracovaný test — stejný seznam jako Live Monitor, včetně těch,
    kdo jsou dočasně bez spojení (test jim běží dál). '' = nikdo."""
    jmena = [str(z.get('jmeno') or '?') for z in ROZPRACOVANE.values()]
    return ', '.join(jmena[:5]) + (f' a {len(jmena) - 5} další' if len(jmena) > 5 else '')


# ─── soubory s otázkami pro každou skupinu ───────────────────────────────────
SOUBORY_OKRUHU = {
    'oz':  ['otazky.csv',     'otazky2.csv',     'otazky3.csv'],
    'asm': ['otazky_asm.csv', 'otazky2_asm.csv', 'otazky3_asm.csv'],
}


def get_max_questions(soubor):
    if not os.path.exists(soubor): return 0
    try:
        with open(soubor, 'r', encoding='utf-8', errors='ignore') as f:
            return max(0, sum(1 for line in f if line.strip()) - 1)
    except: return 0


def _nacti_csv(soubor):
    try: df = pd.read_csv(soubor, sep=';', encoding='utf-8')
    except UnicodeDecodeError: df = pd.read_csv(soubor, sep=';', encoding='cp1250')
    if len(df.columns) == 6: df.columns = ['OTÁZKA', 'A', 'B', 'C', 'D', 'správná odpověď']
    return df


def uloz_nastaveni(typ):
    """Uloží nastavení kvízu pro danou skupinu ('oz' nebo 'asm')."""
    c = config[typ]
    try:
        c['pocet_otazek']            = int(c['pocet_otazek'] or 1)
        c['casovy_limit_minuty']     = int(c['casovy_limit_minuty'] or 1)
        c['min_procento_uspesnosti'] = int(c['min_procento_uspesnosti'] or 0)
        c['okruh_1_pocet']           = int(c['okruh_1_pocet'] or 0)
        c['okruh_2_pocet']           = int(c['okruh_2_pocet'] or 0)
        c['okruh_3_pocet']           = int(c['okruh_3_pocet'] or 0)
        soucet = sum(
            c[f'okruh_{i}_pocet']
            for i in (1, 2, 3)
            if c.get(f'okruh_{i}_zapnut', i == 1)
        )
        if soucet != c['pocet_otazek']:
            ui.notify(
                f'Součet otázek z okruhů ({soucet}) se nerovná celkovému počtu '
                f'({c["pocet_otazek"]})! Opravte nastavení.',
                type='negative', position='top', timeout=6000)
            return
        intranet_data.zapis_json_atomicky(SOUBOR_NASTAVENI, config, indent=4)
        label = 'ASM' if typ == 'asm' else 'OZ'
        ui.notify(f'Nastavení {label} bylo úspěšně uloženo!', type='positive', position='top')
    except Exception as e:
        ui.notify(f'Chyba při ukládání: {e}', type='negative', position='top')


def nacti_otazky(typ='oz', pocet_otazek=20):
    """Načte otázky pro danou skupinu ('oz' nebo 'asm'). Bez UI — běží ve vlákně
    (run.io_bound), vrací (otázky, chybová hláška nebo None)."""
    c       = config[typ]
    soubory = SOUBORY_OKRUHU[typ]
    zapnute = [c.get('okruh_1_zapnut', True), c.get('okruh_2_zapnut', False), c.get('okruh_3_zapnut', False)]
    pocty   = [
        int(c['okruh_1_pocet'] or 0) if zapnute[0] else 0,
        int(c['okruh_2_pocet'] or 0) if zapnute[1] else 0,
        int(c['okruh_3_pocet'] or 0) if zapnute[2] else 0,
    ]
    soucet = sum(pocty)
    pouzit_okruhy = soucet > 0 and soucet == int(pocet_otazek)

    if pouzit_okruhy:
        vsechny = []
        for i, (soubor, pocet) in enumerate(zip(soubory, pocty), 1):
            if pocet <= 0:
                continue
            if not os.path.exists(soubor):
                return [], f'Chybí soubor {soubor} pro Okruh {i}! Nahrajte jej v administraci.'
            df = _nacti_csv(soubor)
            if len(df) < pocet:
                return [], f'Okruh {i}: soubor má jen {len(df)} otázek, požadováno {pocet}.'
            vsechny.extend(df.sample(n=pocet).to_dict('records'))
        import random; random.shuffle(vsechny)
        return vsechny, None
    else:
        soubor = soubory[0]
        if not os.path.exists(soubor):
            return [], f'Chybí soubor {soubor}! Nahrajte jej v administraci.'
        df = _nacti_csv(soubor)
        pocet_int = int(min(pocet_otazek, len(df)))
        return df.sample(n=pocet_int).to_dict('records'), None


# ─── rozpracované testy ─────────────────────────────────────────────────────
# Běžící test patří uživateli, ne otevřené stránce: výpadek Wi-Fi, F5 ani restart
# serveru ho nezruší — po otevření kvízu pokračuje, kde skončil. Čas běží i mimo
# stránku (konec_ts je absolutní), jinak by šel test „pozastavit". Záznam vzniká
# startem, na disk se ukládá po každé odpovědi a odevzdáním mizí. Klíče začínající
# '_' platí jen za běhu (na disk se neukládají).
SLOZKA_ROZPRACOVANYCH = 'Kviz_rozpracovane'
ROZPRACOVANE: dict[str, dict] = {}
# Výsledky odevzdané, když uživatel neměl kvíz otevřený (vypršel mu čas) —
# ukážou se mu jednou při příštím otevření. Jen v paměti.
NEVIDENE_VYSLEDKY: dict[str, dict] = {}
# Jak dlouho server drží stránku kvízu po výpadku spojení (ui.run má 15 s).
# Výpadek Wi-Fi nebo krátké uspání notebooku tak projde bez znovunačtení stránky.
KVIZ_RECONNECT_S = 180
# Rezerva, než test s vypršelým časem odevzdá úklid — živá stránka to stihne sama.
_UKLID_REZERVA_S = 15
# Zavřená stránka na celé obrazovce ukončí test až po této lhůtě. Do té doby ho
# může převzít nová stránka (znovunačtení po výpadku spojení se tak netrestá).
_ODCHOD_LHUTA_S = 10


def _klic_testu(user_id, host_token) -> str:
    """Host podle odkazu (1 odkaz = 1 člověk), ostatní podle účtu."""
    return re.sub(r'[^0-9A-Za-z_-]', '_', f'h{host_token}' if host_token else f'u{user_id}')


def _cesta_zaznamu(klic: str) -> str:
    return os.path.join(SLOZKA_ROZPRACOVANYCH, f'{klic}.json')


def _uloz_zaznam(zaznam: dict) -> None:
    try:
        os.makedirs(SLOZKA_ROZPRACOVANYCH, exist_ok=True)
        intranet_data.zapis_json_atomicky(
            _cesta_zaznamu(zaznam['klic']),
            {k: v for k, v in zaznam.items() if not k.startswith('_')}, default=str)
    except Exception as e:
        print(f"[kvíz] Rozpracovaný test {zaznam.get('klic')} se nepodařilo uložit: {e}")


def _nacti_rozpracovane() -> None:
    """Po startu serveru vrátí rozpracované testy z disku do paměti."""
    if not os.path.isdir(SLOZKA_ROZPRACOVANYCH):
        return
    for nazev in os.listdir(SLOZKA_ROZPRACOVANYCH):
        if nazev.startswith('.') or not nazev.endswith('.json'):
            continue  # .tmp_* = nedokončený atomický zápis
        try:
            with open(os.path.join(SLOZKA_ROZPRACOVANYCH, nazev), encoding='utf-8') as f:
                zaznam = json.load(f)
            zaznam['_odpojeno_ts'] = time.time()  # po restartu nemá nikdo spojení
            ROZPRACOVANE.setdefault(zaznam['klic'], zaznam)
        except Exception as e:
            print(f'[kvíz] Rozpracovaný test {nazev} nelze načíst: {e}')


def _zbyva_s(zaznam: dict) -> int:
    return max(0, math.ceil(zaznam['konec_ts'] - time.time()))


def _host_rozpracovaneho_testu(host_token: str) -> dict | None:
    """Host smí rozpracovaný test dopsat i po skončení platnosti odkazu."""
    z = ROZPRACOVANE.get(_klic_testu(None, host_token))
    if not z:
        return None
    return {'user_id': z['user_id'], 'jmeno': z['jmeno'], 'krestni': z['host_krestni'],
            'prijmeni': z['host_prijmeni'], 'skupina': z['typ']}


def _mimo_s(zaznam: dict, ted: float) -> int:
    """Celková doba mimo test včetně právě probíhajícího odchodu."""
    od = zaznam.get('_mimo_ts')
    return int(zaznam.get('mimo_s', 0) + (ted - od if od else 0))


def _mimo_text(opusteni: int, mimo_s: int, ted_mimo: bool = False) -> str:
    if ted_mimo:
        return f'právě mimo test ({opusteni}×, {mimo_s} s)'
    return f'{opusteni}× · {mimo_s} s' if opusteni else '—'


# Řádek monitoru: rudá tečka + podbarvení u toho, kdo během testu klikl mimo
# test (tečka bliká, dokud je mimo právě teď) nebo test přerušil.
_SLOT_DOHLED = r'''
<q-tr :props="props" :class="props.row.poruseni ? 'bg-red-1' : ''">
  <q-td v-for="col in props.cols" :key="col.name" :props="props"
        :class="props.row.poruseni && (col.name === 'mimo' || col.value === 'PŘERUŠENO') ? 'text-red-9 text-weight-bold' : ''">
    <span v-if="col.name === 'jmeno' && props.row.poruseni"
          :class="props.row.ted_mimo ? 'kviz-tecka kviz-tecka-ted' : 'kviz-tecka'"></span>{{ col.value }}
  </q-td>
</q-tr>
'''


def _vyhodnot(zaznam: dict, automaticky: bool, preruseno: bool = False) -> dict:
    ted = time.time()
    celkem = len(zaznam['otazky'])
    proc = (zaznam['body'] / celkem) * 100 if celkem > 0 else 0
    m, s = divmod(max(0, int(min(ted, zaznam['konec_ts']) - zaznam['zacatek_ts'])), 60)
    if preruseno:
        stav = 'PŘERUŠENO'  # opustil celou obrazovku → test končí s tím, co stihl
    else:
        stav = 'ČAS VYPRŠEL' if ted >= zaznam['konec_ts'] else 'DOKONČENO'
    return {
        'jmeno': zaznam['jmeno'], 'stav': stav,
        'body': zaznam['body'], 'celkem': celkem, 'proc': proc, 'm': m, 's': s,
        'min_procento': zaznam['min_procento'], 'historie': zaznam['historie_odpovedi'],
        'automaticky': automaticky, 'cas': ted,
        'opusteni': zaznam.get('opusteni', 0), 'mimo_s': _mimo_s(zaznam, ted),
    }


def _uzavri_test(klic: str, automaticky: bool = False, preruseno: bool = False,
                 zarizeni: dict | None = None) -> tuple[dict, dict] | None:
    """Synchronní část odevzdání — jediné místo, kde test končí (tlačítko,
    vypršení času, úklid, opuštění celé obrazovky). Vrací (záznam, vyhodnocení),
    nebo None, když test mezitím odevzdal někdo jiný (druhé okno / úklid)."""
    zaznam = ROZPRACOVANE.pop(klic, None)  # pop = pojistka proti dvojímu odevzdání
    if zaznam is None:
        return None
    v = _vyhodnot(zaznam, automaticky, preruseno)
    try:
        os.remove(_cesta_zaznamu(klic))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[kvíz] Rozpracovaný test {klic} nelze smazat: {e}')
    intranet_session.uvolni_relaci(zaznam.get('_login_token'))
    dokoncene_testy.insert(0, {'jmeno': v['jmeno'], 'cas_odevzdani': time.strftime('%H:%M:%S'), 'vysledek': v['stav'], 'skore': f"{v['body']}/{v['celkem']}", 'uspesnost': f"{v['proc']:.2f}%",
                               'mimo': _mimo_text(v['opusteni'], v['mimo_s']), 'poruseni': preruseno or v['opusteni'] > 0})
    souhrn = f"{v['stav']}, {v['body']}/{v['celkem']} ({v['proc']:.2f} %), trvání {v['m']}m {v['s']}s"
    if v['opusteni']:
        souhrn += f", mimo test {v['opusteni']}× ({v['mimo_s']} s)"
    if preruseno:
        zprava = f"Varování: test ukončen – opuštění celé obrazovky: {souhrn}"
    else:
        kdo = ' automaticky (kvíz neměl otevřený)' if automaticky else ''
        zprava = f"Test odevzdán{kdo}: {souhrn}"
    intranet_logger.log_activity(v['jmeno'], 'Kvíz', zprava, **(zarizeni or {}))
    return zaznam, v


def _zapis_vysledek(zaznam: dict, v: dict) -> bool:
    """Excel do Vysledky_Web + MySQL. Blokující — volat přes run.io_bound."""
    slozka = "Vysledky_Web"
    os.makedirs(slozka, exist_ok=True)
    cesta = os.path.join(slozka, f"Vysledek_{v['jmeno']}_{time.strftime('%H-%M-%S')}.xlsx")
    try:
        souhrn = {"Jméno": v['jmeno'], "Stav": v['stav'], "Úspěšnost": f"{v['proc']:.2f}%", "Bodů": f"{v['body']}/{v['celkem']}", "Trvání": f"{v['m']}m {v['s']}s"}
        if _kontrola_zapnuta() or v['opusteni']:
            souhrn["Mimo test"] = f"{v['opusteni']}× ({v['mimo_s']} s)"
        df_s = pd.DataFrame([souhrn])
        with pd.ExcelWriter(cesta, engine='openpyxl') as w:
            df_s.to_excel(w, sheet_name='Souhrn', index=False)
            if v['historie']: pd.DataFrame(v['historie']).to_excel(w, sheet_name='Analýza', index=False)
            w.sheets['Souhrn']['B2'].fill = PatternFill(start_color="FF00FF00" if v['proc'] >= v['min_procento'] else "00E02319", fill_type="solid")
    except Exception: pass

    if not intranet_data.nacti_mysql().get("enabled"):
        return True
    anonymni_host = zaznam['user_id'] is None
    return intranet_data.uloz_vysledek_kvizu(
        zaznam['user_id'],
        v['stav'],
        f"{v['proc']:.2f}%",
        f"{v['body']}/{v['celkem']}",
        f"{v['m']}m {v['s']}s",
        v['historie'],
        host_jmeno=zaznam['host_krestni'] if anonymni_host else None,
        host_prijmeni=zaznam['host_prijmeni'] if anonymni_host else None,
        # Vazba na zadavatele odkazu; u interního uživatele zůstane None.
        pristup_token=zaznam['pristup_token'],
    )


async def _uloz_vysledek(zaznam: dict, v: dict) -> bool:
    """Uloží výsledek mimo event loop — Excel + MySQL by při hromadném odevzdání
    zdržely všechny připojené. Selhání zapíše do audit logu."""
    try:
        ok = bool(await run.io_bound(_zapis_vysledek, zaznam, v))
    except Exception as e:
        print(f"[kvíz] Výsledek {v['jmeno']} se nepodařilo uložit: {e}")
        ok = False
    if not ok:
        intranet_logger.log_activity(v['jmeno'], 'Kvíz', f"Chyba: výsledek testu se nepodařilo uložit do databáze ({v['stav']}, {v['body']}/{v['celkem']}, {v['proc']:.2f} %). Záloha je ve Vysledky_Web.")
    return ok


async def bg_uklid_testu() -> None:
    """Úloha na pozadí (web_main.py): po startu vrátí rozpracované testy z disku
    a pak odevzdává ty, kterým vypršel čas a žádná stránka je neodevzdala sama
    (uživatel kvíz zavřel nebo nemá spojení)."""
    _nacti_rozpracovane()
    while True:
        try:
            ted = time.time()
            for klic in [k for k, z in ROZPRACOVANE.items() if ted >= z['konec_ts'] + _UKLID_REZERVA_S]:
                if (uzavreno := _uzavri_test(klic, automaticky=True)):
                    NEVIDENE_VYSLEDKY[klic] = uzavreno[1]
                    await _uloz_vysledek(*uzavreno)
            for klic in [k for k, v in NEVIDENE_VYSLEDKY.items() if ted - v['cas'] > 12 * 3600]:
                NEVIDENE_VYSLEDKY.pop(klic, None)
        except Exception as e:
            print(f'[kvíz] Úklid rozpracovaných testů selhal: {e}')
        await asyncio.sleep(15)

def _nav_na_prehled():
    with ui.dialog().props('maximized persistent transition-show="fade" transition-hide="fade"') as _dlg_back:
        with ui.column().classes('w-full h-full items-center justify-center bg-gray-50 m-0 p-0'):
            ui.spinner('dots', size='5rem', color='blue').classes('mb-4')
            ui.label('Otevírám modul').classes('text-sm font-bold text-gray-500 uppercase tracking-widest')
            ui.label('Přehled').classes('text-3xl font-black text-blue-900')
    _dlg_back.open()
    ui.timer(0.2, lambda: ui.navigate.to('/'), once=True)

def _zakladni_url() -> str:
    """Absolutní adresa portálu (JIPKA_APP_URL, výchozí ostrý portál) – stejná jako v e-mailech ostatních modulů."""
    return intranet_data.APP_URL

def kviz_host_odkaz(token: str) -> str:
    return f"{_zakladni_url()}/kviz/host/{token}"

@ui.page('/kviz/host/{token}')
def kviz_host_page(token: str):
    """Vstup zkoušeného bez účtu v intranetu. Token jen uloží do session a pošle na /kviz."""
    if app.storage.user.get('user_id'):
        # Přihlášený uživatel jde svou vlastní cestou, token se nespotřebuje.
        ui.navigate.to('/kviz')
        return
    if _host_rozpracovaneho_testu(token):
        # Rozpracovaný test pokračuje vždy — i když odkazu mezitím skončila platnost.
        app.storage.user['kviz_host_token'] = token
        ui.navigate.to('/kviz')
        return

    # Host vidí stejnou tmavou obrazovku jako přihlášení do intranetu — je to první
    # (a často jediná) stránka systému, kterou uvidí. Import je až tady: intranet
    # importuje tenhle modul, takže na úrovni modulu by vznikl kruh.
    import intranet
    ui.add_head_html('<style>' + intranet.css_prihlasovaci_obrazovky() + '</style>')
    ui.query('body').classes(add='prihlaseni-pozadi', remove='intranet-pozadi kviz-pozadi')
    ui.page_title('Zkouškový kvíz – Moje JIPka')

    pristup = intranet_data.over_kviz_pristup(token)
    with ui.column().classes('w-full h-screen items-center justify-center p-4'):
        with ui.card().classes('gate-card login-wrap p-8 items-center'):
            ui.image('/static/logo.png').classes('login-logo mb-6')

            if not pristup:
                # Odkaz rozeslaný předem není chyba — řekneme, odkdy platí.
                stav = intranet_data.stav_kviz_pristupu(token)
                if stav and stav['stav'] == 'ceka':
                    od, do = stav['platnost_od'], stav['platnost_do']
                    ui.icon('schedule', size='3rem', color='orange-400').classes('mb-3')
                    ui.label('Test zatím nezačal').classes('login-title text-center mb-1')
                    ui.label(f"Kvíz bude dostupný {od:%d.%m.%Y} od {od:%H:%M} do {do:%H:%M}.")\
                        .classes('login-sub text-center mb-1')
                    ui.label('Odkaz si uschovejte a otevřete ho znovu v uvedený čas.')\
                        .classes('login-sub text-center')
                    return
                ui.icon('link_off', size='3rem', color='red-400').classes('mb-3')
                ui.label('Odkaz je neplatný').classes('login-title text-center mb-1')
                ui.label('Odkaz byl zrušen, nebo mu vypršela platnost. '
                         'Požádejte o nový osobu, která test zadává.').classes('login-sub text-center')
                return
            app.storage.user['kviz_host_token'] = token

            if pristup['anonymni'] and not pristup['jmeno']:
                # Odkaz nikomu nepatří — jméno pod výsledkem určí ten, kdo přijde.
                ui.label('Představte se').classes('login-title text-center mb-1')
                ui.label('Pod tímto jménem bude uložen váš výsledek testu.').classes('login-sub text-center mb-5')
                i_jmeno = ui.input(label='Jméno').props('outlined').classes('w-full')
                i_prijmeni = ui.input(label='Příjmení').props('outlined').classes('w-full')

                def _potvrd():
                    if not (i_jmeno.value or '').strip() or not (i_prijmeni.value or '').strip():
                        ui.notify('Vyplňte jméno i příjmení.', type='negative', position='top'); return
                    if not intranet_data.uloz_jmeno_hosta(token, i_jmeno.value, i_prijmeni.value):
                        ui.notify('Odkaz mezitím propadl. Požádejte o nový.', type='negative', position='top'); return
                    ui.navigate.to('/kviz')

                ui.button('POKRAČOVAT NA TEST', on_click=_potvrd).classes('w-full mt-5 h-12 text-lg font-bold')
                i_prijmeni.on('keydown.enter', _potvrd)
                return

            ui.label(f"Vítejte, {pristup['jmeno']}").classes('login-title text-center mb-1')
            ui.label('Otevírám zkouškový kvíz…').classes('login-sub text-center mb-4')
            ui.spinner(size='xl')
            ui.timer(1.0, lambda: ui.navigate.to('/kviz'), once=True)

@ui.page('/kviz', reconnect_timeout=KVIZ_RECONNECT_S)
def index_page(client: Client):

    ui.add_head_html('''
        <style>
            @media print {
                .skryt-pri-tisku { display: none !important; }
                body { background-color: white !important; background-image: none !important; }
                .q-dialog__backdrop { display: none !important; }
            }
            body.kviz-pozadi {
                margin: 0 !important;
                padding: 0 !important;
                overflow: hidden !important;
                background-image: url("/static/pozadi.jpg");
                background-size: cover;
                background-position: center;
                background-repeat: no-repeat;
                height: 100vh;
                width: 100vw;
            }
            /* Plynulý přechod při načtení kvízu */
            #q-app { animation: _kviz_fadein 0.3s ease-out; }
            @keyframes _kviz_fadein { from { opacity: 0; } to { opacity: 1; } }
            /* Gesto zpět na touchpadu ani tah dolů nesmí stránku s testem opustit/obnovit */
            html, body { overscroll-behavior: none; }
            /* Test mimo celou obrazovku: otázky skryté, místo nich výzva k návratu */
            .kviz-bezi .kviz-fs-vyzva { display: none !important; }
            body.kviz-bez-fs .kviz-bezi .kviz-fs-vyzva {
                display: flex !important; position: fixed; inset: 0; z-index: 6000;
                flex-direction: column; align-items: center; justify-content: center; gap: 20px;
                padding: 24px; text-align: center; background: #f8fafc;
                animation: _kviz_fadein .25s .6s both;  /* při běžném startu nebliká */
            }
            body.kviz-bez-fs .kviz-obsah-testu { visibility: hidden; }
            .kviz-kontrola { user-select: none; -webkit-user-select: none; }
            /* Monitor: rudá tečka u studenta, který klikl mimo test (bliká = je mimo právě teď) */
            .kviz-tecka { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
                          background: #dc2626; margin-right: 8px; vertical-align: middle; }
            .kviz-tecka-ted { animation: _kviz_blik 1s ease-in-out infinite; }
            @keyframes _kviz_blik { 50% { opacity: .2; } }
        </style>
        <script>
            // .kviz-bezi = na stránce běží test → displej zůstává zapnutý.
            // .kviz-kontrola = navíc kontrolní mechanismus (přepínač v nastavení kvízu):
            //  - test běží na celé obrazovce se zámkem klávesnice (Alt+Tab, Ctrl+W/T/N
            //    nefungují, Esc se musí podržet); odchod z ní (i zavřením stránky) test ukončí,
            //  - klik mimo stránku (jiné okno, aplikace, záložka) vidí správce v monitoru.
            // Zámek klávesnice i displeje fungují jen přes HTTPS.
            (() => {
                const bezi = () => !!document.querySelector('.kviz-bezi');
                const hlidat = () => !!document.querySelector('.kviz-kontrola');
                const posli = (typ) => { try { emitEvent('kviz_dohled', typ); } catch (e) {} };
                let veFs = false, odchazi = false, testBezel = false, mimo = false, mimoCasovac = null;

                // Celou obrazovku prohlížeč povolí jen v reakci na klik (Spustit / Pokračovat).
                document.addEventListener('click', (e) => {
                    if (e.target.closest && e.target.closest('.kviz-na-celou') && !document.fullscreenElement)
                        document.documentElement.requestFullscreen({ navigationUI: 'hide' }).catch(() => {});
                }, true);

                document.addEventListener('fullscreenchange', () => {
                    if (document.fullscreenElement) {
                        veFs = true;
                        if (navigator.keyboard && navigator.keyboard.lock) navigator.keyboard.lock().catch(() => {});
                    } else {
                        if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
                        if (veFs && hlidat() && !odchazi) posli('fs_off');
                        veFs = false;
                    }
                    obnov();
                });
                // Zavření stránky na celé obrazovce = odchod z testu. Server ho potvrdí až po lhůtě,
                // protože stejně vypadá i znovunačtení, které NiceGUI dělá sám po výpadku spojení.
                window.addEventListener('pagehide', () => {
                    if (veFs && hlidat() && !odchazi) posli('odchod');
                    odchazi = true;
                });
                window.addEventListener('pageshow', (e) => { if (e.persisted) odchazi = false; });

                // Klik mimo stránku (jiné okno, aplikace, záložka). Krátké probliknutí (< 1 s)
                // se nepočítá; při zavírání stránky časovač nedoběhne, takže ho nespustí ani reload.
                const odesel = () => {
                    if (!hlidat() || mimo || mimoCasovac) return;
                    mimoCasovac = setTimeout(() => {
                        mimoCasovac = null;
                        if (hlidat() && !mimo && !odchazi && (document.visibilityState === 'hidden' || !document.hasFocus())) {
                            mimo = true; posli('mimo');
                        }
                    }, 1000);
                };
                const vratil = () => {
                    if (document.visibilityState !== 'visible' || !document.hasFocus()) return;
                    clearTimeout(mimoCasovac); mimoCasovac = null;
                    if (mimo) { mimo = false; posli('zpet'); }
                };
                window.addEventListener('blur', odesel);
                window.addEventListener('focus', vratil);
                document.addEventListener('visibilitychange', () => document.visibilityState === 'hidden' ? odesel() : vratil());

                // Během testu: žádné obnovení, tisk, hledání, vývojářské nástroje, kopírování.
                const ZAKAZANE = ['r', 'w', 't', 'n', 'p', 's', 'u', 'f', 'g', 'c', 'x', 'a', 'l', 'k', 'e', 'o', 'j', 'h', 'd', 'i'];
                document.addEventListener('keydown', (e) => {
                    if (!hlidat()) return;
                    const k = (e.key || '').toLowerCase();
                    if (k === 'f5' || k === 'f12' || ((e.ctrlKey || e.metaKey) && ZAKAZANE.includes(k))
                        || (e.altKey && (k === 'arrowleft' || k === 'arrowright' || k === 'home'))) {
                        e.preventDefault(); e.stopPropagation();
                    }
                }, true);
                ['contextmenu', 'copy', 'cut', 'dragstart', 'selectstart'].forEach((t) =>
                    document.addEventListener(t, (e) => { if (hlidat()) e.preventDefault(); }, true));

                // Výzva k návratu na celou obrazovku, konec testu → ven z celé obrazovky, Wake Lock.
                let zamek = null, zadam = false, dalsiPokus = 0;
                async function obnov() {
                    const b = bezi(), h = hlidat();
                    document.body.classList.toggle('kviz-bez-fs', h && document.fullscreenEnabled && !document.fullscreenElement);
                    if (h) testBezel = true;
                    else if (testBezel || b) {
                        // Konec testu, nebo test běží s vypnutou kontrolou → pryč z celé obrazovky.
                        testBezel = false;
                        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
                    }
                    const drz = b && document.visibilityState === 'visible';
                    if (drz && !zamek && !zadam && 'wakeLock' in navigator && Date.now() >= dalsiPokus) {
                        zadam = true;
                        try {
                            const z = await navigator.wakeLock.request('screen');
                            z.addEventListener('release', () => { if (zamek === z) zamek = null; });
                            zamek = z;
                        } catch (e) { dalsiPokus = Date.now() + 30000; }
                        zadam = false;
                    } else if (!drz && zamek) {
                        const z = zamek; zamek = null; z.release().catch(() => {});
                    }
                }
                setInterval(obnov, 400);
                document.addEventListener('visibilitychange', obnov);
                // Server: test se nespustil → zpět z celé obrazovky.
                window.kvizDohled = { konec() { testBezel = true; obnov(); } };
            })();
        </script>
    ''')
    # remove: hostovská brána nechává na body tmavý režim s overflow:hidden, v testu by ořízl obsah
    ui.query('body').classes(add='kviz-pozadi', remove='prihlaseni-pozadi')

    user_id = app.storage.user.get('user_id')
    user_name = app.storage.user.get('user_name')
    je_host = False
    host_krestni = host_prijmeni = host_skupina = None

    if not user_id:
        # Zkoušený, který přišel přes časově omezený odkaz — nemá žádná práva a
        # zůstává uvnitř /kviz. Buď píše jménem přiřazeného uživatele, nebo je
        # anonymní (user_id zůstane None) a jméno si vyplnil na vstupní stránce.
        _token = app.storage.user.get('kviz_host_token')
        _host = intranet_data.over_kviz_pristup(_token)
        if not _host and _token:
            # Rozpracovaný test dopíše i po skončení platnosti odkazu.
            _host = _host_rozpracovaneho_testu(_token)
        if _host and _host['jmeno']:
            user_id, user_name, je_host = _host['user_id'], _host['jmeno'], True
            host_krestni, host_prijmeni = _host['krestni'], _host['prijmeni']
            host_skupina = _host.get('skupina')
        elif _token:
            # Chybí jméno, okno ještě nezačalo nebo už propadlo — brána to vysvětlí.
            ui.navigate.to(f'/kviz/host/{_token}')
            return

    if not user_id and not je_host:
        app.storage.user['redirect_to'] = '/kviz'
        with ui.column().classes('w-full max-w-2xl mx-auto items-center mt-32'):
            ui.label('🚫 Nejste přihlášen(a)').classes('text-5xl font-bold text-red-600 mb-6')
            ui.label('Přesměrovávám na bezpečné přihlášení...').classes('text-2xl text-gray-700 mb-6')
            ui.spinner(size='xl')
            ui.timer(1.5, _nav_na_prehled, once=True)
        return

    # Eviduj živé připojení i pro samostatnou stránku /kviz (single-session
    # + odhlašování po zavření prohlížeče), ať návrat sem nezpůsobí výpadek relace.
    _user_email = str(app.storage.user.get('user_email', '')).lower()
    _token = app.storage.user.get('login_token')
    if _token and not je_host:
        # Lhůta delší než reconnect_timeout kvízu — odhlášení nesmí předběhnout návrat.
        intranet_session.registruj_pripojeni(client, _token, _user_email, user_name,
                                             lhuta_s=KVIZ_RECONNECT_S + 20)

    # Host nedědí práva uživatele, jehož jménem test píše.
    _verze_prav_stranky = None
    if je_host:
        vsechna_prava = []
    else:
        _verze_prav_stranky = intranet_data.verze_prav(user_id)  # před čtením práv
        vsechna_prava, _je_aktivni = intranet_data.ziskej_prava_a_aktivitu(user_id)
        if not _je_aktivni:
            # Deaktivovaný / smazaný účet — relaci ukončí stráž na hlavní stránce.
            ui.navigate.to('/')
            return
    is_admin = 'vse' in vsechna_prava
    ma_pristup_k_vysledkum = "vystup_osobni" in vsechna_prava or "vystup_vse" in vsechna_prava or "vse" in vsechna_prava
    muze_generovat_hosty = is_admin or 'kviz_hoste' in vsechna_prava

    # Stejná podmínka jako dlaždice na přehledu – bez práva 'kviz' nejde kvíz otevřít
    # ani přímou URL. Host má přístup daný platným odkazem.
    if not je_host and not (is_admin or 'kviz' in vsechna_prava):
        with ui.column().classes('w-full max-w-2xl mx-auto items-center mt-32'):
            ui.label('🚫 NEMÁTE PŘÍSTUP').classes('text-5xl font-bold text-red-600 mb-6')
            ui.label('K modulu Zkouškový kvíz nemáte oprávnění.').classes('text-2xl text-gray-700 text-center')
            ui.button('Zpět na Přehled', on_click=_nav_na_prehled).classes('mt-10 bg-blue-600 text-white font-bold px-8 py-3 rounded-lg shadow-md')
        return

    # Skupina kvízu je řízena globálním nastavením administrátora; hostovský odkaz
    # si ale může nést vlastní (zadavatel ji volí při generování).
    kviz_typ = host_skupina or config.get('aktivni_skupina', 'oz')

    nastaveni = intranet_data.nacti_nastaveni_intranetu()

    # Modul kvízu je záměrně VYJMUT z automatického odhlašování po nečinnosti –
    # uživatel nesmí být odhlášen uprostřed vyplňování testu. Žádný idle-timer zde neběží.

    if not nastaveni.get('kviz_zapnuty', True) and not is_admin:
        with ui.column().classes('w-full max-w-2xl mx-auto items-center mt-32'):
            ui.label('🚫 MODUL JE VYPNUTÝ').classes('text-5xl font-bold text-orange-600 mb-6')
            ui.label('Zkouškový kvíz je momentálně administrátorem deaktivován.').classes('text-2xl text-gray-700 text-center')
            if not je_host:
                ui.button('Zpět na Přehled', on_click=_nav_na_prehled).classes('mt-10 bg-blue-600 text-white font-bold px-8 py-3 rounded-lg shadow-md')
        return

    # Stav stránky je jen pohled — běžící test žije v ROZPRACOVANE[klic], takže ho
    # výpadek spojení ani znovunačtení stránky nezruší.
    host_token = app.storage.user.get('kviz_host_token') if je_host else None
    login_token = None if je_host else _token
    klic = _klic_testu(user_id, host_token)
    state = {'jmeno': user_name, 'zobrazit_admin_panel': False, 'bezi_test': False,
             'vysledek': None, 'zobrazeny_index': None, 'nacitam': False}
    local_ui = {'cas_label': None, 'cas_vyzva': None}
    zarizeni = {'ip': client.ip,
                'device': intranet_logger.popis_zarizeni(client.request.headers.get('user-agent', ''))}

    def drz_relaci_testu(z: dict) -> None:
        """Během testu nesmí relaci zrušit automatické odhlášení (ani při delším výpadku)."""
        if not login_token:
            return
        if z.get('_login_token') not in (None, login_token):
            intranet_session.uvolni_relaci(z['_login_token'])  # mezitím se přihlásil znovu
        z['_login_token'] = login_token
        intranet_session.drz_relaci(login_token, _user_email, user_name, lhuta_s=KVIZ_RECONNECT_S + 20)

    def prevezmi_nevideny() -> dict | None:
        """Výsledek odevzdaný mimo tuto stránku. Odpojená stránka (třeba stará po
        znovunačtení, server ji drží až KVIZ_RECONNECT_S) si ho jen přečte; smaže ho
        až stránka, kterou uživatel opravdu vidí (pri_pripojeni / nové otevření)."""
        if client.has_socket_connection:
            return NEVIDENE_VYSLEDKY.pop(klic, None)
        return NEVIDENE_VYSLEDKY.get(klic)

    zaznam = ROZPRACOVANE.get(klic)
    if zaznam and _zbyva_s(zaznam) <= 0:
        # Čas vypršel, zatímco tu nebyl → odevzdej hned a ukaž výsledek.
        if (uzavreno := _uzavri_test(klic, automaticky=True)):
            state['vysledek'] = uzavreno[1]
            background_tasks.create(_uloz_vysledek(*uzavreno), name=f'kviz-ulozeni-{klic}')
    elif zaznam:
        # Rozpracovaný test → pokračuj tam, kde skončil (čas mezitím běžel).
        state['bezi_test'] = True
        drz_relaci_testu(zaznam)
        zaznam['_stranka'] = client.id  # test převzala tato stránka, signály té staré už neplatí
        if (mimo_od := zaznam.pop('_mimo_ts', None)):
            zaznam['mimo_s'] = zaznam.get('mimo_s', 0) + (time.time() - mimo_od)  # je zpátky
        odpojeno = zaznam.pop('_odpojeno_ts', None)
        vypadek = f' (výpadek spojení {int(time.time() - odpojeno)} s)' if odpojeno else ''
        s = _zbyva_s(zaznam)
        intranet_logger.log_activity(user_name, 'Kvíz', f"Varování: test obnoven po znovunačtení stránky{vypadek}, otázka {zaznam['aktualni_index'] + 1}/{len(zaznam['otazky'])}, zbývá {s // 60:02d}:{s % 60:02d}", **zarizeni)
    elif klic in NEVIDENE_VYSLEDKY:
        state['vysledek'] = NEVIDENE_VYSLEDKY.pop(klic)  # odevzdáno, když tu nebyl

    # Diagnostika výpadků: kdy spojení spadlo a jak dlouho trvalo, než se vrátilo.
    def pri_odpojeni() -> None:
        if state['bezi_test'] and (z := ROZPRACOVANE.get(klic)):
            z['_odpojeno_ts'] = time.time()

    def pri_pripojeni() -> None:
        if state['vysledek'] is not None and NEVIDENE_VYSLEDKY.get(klic) is state['vysledek']:
            NEVIDENE_VYSLEDKY.pop(klic, None)  # odevzdáno bez spojení, výsledek už vidí tady
        z = ROZPRACOVANE.get(klic)
        odpojeno = z.pop('_odpojeno_ts', None) if z else None
        if odpojeno and time.time() - odpojeno >= 3:
            intranet_logger.log_activity(user_name, 'Kvíz', f'Varování: výpadek spojení {int(time.time() - odpojeno)} s, test běží dál bez přerušení', **zarizeni)

    client.on_disconnect(pri_odpojeni)
    client.on_connect(pri_pripojeni)

    # Dohled nad testem — události posílá skript v hlavičce stránky:
    # 'fs_off' = opustil celou obrazovku → test končí hned,
    # 'odchod' = stránka se zavírá na celé obrazovce → test končí po lhůtě (viz níže),
    # 'mimo' / 'zpet' = klik mimo stránku a návrat → jen evidence pro monitor.
    def je_aktivni_stranka() -> bool:
        z = ROZPRACOVANE.get(klic)
        return state['bezi_test'] and z is not None and z.get('_stranka', client.id) == client.id

    async def pri_dohledu(e) -> None:
        typ = e.args[0] if isinstance(e.args, list) and e.args else e.args
        if not je_aktivni_stranka():
            return  # test skončil, nebo ho mezitím převzala novější stránka (znovunačtení)
        if not _kontrola_zapnuta():
            return  # kontrolní mechanismus je vypnutý
        z = ROZPRACOVANE[klic]
        ted = time.time()
        if typ == 'fs_off':
            await ukoncit_test(preruseno=True)
        elif typ == 'odchod':
            # Stejně vypadá i znovunačtení, které NiceGUI dělá sám po výpadku spojení.
            # Test proto skončí, jen když ho do lhůty nepřevezme nová stránka.
            await asyncio.sleep(_ODCHOD_LHUTA_S)
            if je_aktivni_stranka():
                await ukoncit_test(preruseno=True)
        elif typ == 'mimo' and not z.get('_mimo_ts'):
            z['_mimo_ts'] = ted
            z['opusteni'] = z.get('opusteni', 0) + 1
            _uloz_zaznam(z)
            intranet_logger.log_activity(user_name, 'Kvíz', f"Varování: klik mimo test (jiné okno / aplikace), {z['opusteni']}. opuštění, otázka {min(z['aktualni_index'] + 1, len(z['otazky']))}/{len(z['otazky'])}", **zarizeni)
        elif typ == 'zpet' and (od := z.pop('_mimo_ts', None)):
            z['mimo_s'] = z.get('mimo_s', 0) + (ted - od)
            _uloz_zaznam(z)

    ui.on('kviz_dohled', pri_dohledu)

    def prepni_admin_panel(zobrazit):
        state['zobrazit_admin_panel'] = zobrazit
        hlavni_rozhrani.refresh()

    # ── Správa hostovských přístupů (admin panel i samostatné právo kviz_hoste) ──
    def vykresli_spravu_pristupu():
        vsichni = intranet_data.ziskej_vsechny_uzivatele()
        kandidati = {}
        for email, u in vsichni.items():
            prava_u = set(str(u.get('prava') or '').split(','))
            if u.get('aktivni') and ('kviz' in prava_u or 'vse' in prava_u):
                kandidati[u['id']] = f"{u['jmeno_cele']} ({email})"

        ted = datetime.now().replace(second=0, microsecond=0)
        vyber = {'ids': [], 'od': ted.strftime('%Y-%m-%dT%H:%M'), 'trvani': 1.0,
                 'skupina': config.get('aktivni_skupina', 'oz')}
        # Okno se zadává jako začátek + délka; konec dopočítáváme, ať nejde zadat naopak.
        TRVANI = {0.5: '30 minut', 1.0: '1 hodina', 2.0: '2 hodiny', 3.0: '3 hodiny',
                  4.0: '4 hodiny', 8.0: '8 hodin', 24.0: '1 den', 72.0: '3 dny', 168.0: '7 dní'}

        with ui.card().classes('w-full p-6 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm mb-6'):
            ui.label('🎟️ Vygenerovat přístupy do kvízu').classes('text-2xl font-bold mb-4 text-purple-600')
            ui.label('Vyberte zkoušené a okno platnosti. Každý dostane vlastní odkaz, '
                     'kterým se do kvízu dostane bez přihlášení do intranetu.').classes('text-sm text-gray-500 mb-4')

            sel = ui.select(kandidati, multiple=True, with_input=True,
                            label='Zkoušení uživatelé').props('use-chips outlined').classes('w-full mb-4')
            sel.bind_value(vyber, 'ids')

            with ui.row().classes('w-full items-center gap-4 mb-4'):
                # Skupina se zapíše do odkazu — nezávisle na tom, co zrovna jede v intranetu.
                ui.label('Typ kvízu:').classes('font-bold text-gray-700')
                ui.toggle({'oz': '🧑‍🔧 OZ', 'asm': '👔 ASM'}, value=vyber['skupina'],
                          on_change=lambda e: vyber.update(skupina=e.value)).props('dense')

            with ui.row().classes('w-full items-center gap-4 mb-2'):
                i_od = ui.input(label='Platnost od').props('type=datetime-local outlined dense').classes('w-64')
                i_od.bind_value(vyber, 'od')
                ui.select(TRVANI, label='Jak dlouho platí').props('outlined dense').classes('w-48') \
                    .bind_value(vyber, 'trvani').on_value_change(lambda _: nahled.refresh())
                for popis, zacatek in (('teď', 0), ('zítra 9:00', 1), ('pondělí 9:00', None)):
                    def _start(d=zacatek):
                        zac = datetime.now().replace(second=0, microsecond=0)
                        if d is None:                       # nejbližší příští pondělí
                            zac = (zac + timedelta(days=7 - zac.weekday() or 7)).replace(hour=9, minute=0)
                        elif d:
                            zac = (zac + timedelta(days=d)).replace(hour=9, minute=0)
                        vyber['od'] = zac.strftime('%Y-%m-%dT%H:%M')
                        i_od.value = vyber['od']
                        nahled.refresh()
                    ui.button(popis, on_click=_start).props('flat dense').classes('text-blue-600')

            def _okno():
                """(od, do) z formuláře, nebo None když je okno nesmyslné."""
                try:
                    od = datetime.fromisoformat(vyber['od'])
                    hodin = float(vyber['trvani'] or 0)
                except (TypeError, ValueError):
                    ui.notify('Vyplňte platnost od a délku platnosti.', type='negative', position='top'); return None
                if hodin <= 0:
                    ui.notify('Vyberte, jak dlouho odkaz platí.', type='negative', position='top'); return None
                return od, od + timedelta(hours=hodin)

            @ui.refreshable
            def nahled():
                """Ať je vidět, co přesně se vygeneruje — hlavně u oken na příští dny."""
                try:
                    od = datetime.fromisoformat(vyber['od'])
                    do = od + timedelta(hours=float(vyber['trvani'] or 0))
                except (TypeError, ValueError):
                    ui.label('⚠️ Vyplňte začátek platnosti.').classes('text-sm text-orange-600 mb-4'); return
                popis = f"Odkazy budou platit {od:%d.%m.%Y %H:%M} – {do:%d.%m.%Y %H:%M}"
                if od > datetime.now():
                    popis += f' · do té doby zkoušený uvidí hlášku, že test začíná {od:%d.%m. v %H:%M}'
                ui.label(popis).classes('text-sm text-gray-600 mb-4')

            i_od.on('blur', lambda _: nahled.refresh())
            nahled()

            def _generuj():
                if not vyber['ids']:
                    ui.notify('Vyberte alespoň jednoho uživatele.', type='negative', position='top'); return
                okno = _okno()
                if not okno: return
                od, do = okno

                nove = intranet_data.vytvor_kviz_pristupy(vyber['ids'], od, do, user_id, vyber['skupina'])
                if not nove:
                    ui.notify('Přístupy se nepodařilo vytvořit.', type='negative', position='top'); return
                intranet_logger.log_activity(user_name, 'Kvíz', f'Vygeneroval {len(nove)} hostovských přístupů do kvízu')
                ui.notify(f'Vygenerováno {len(nove)} přístupů.', type='positive', position='top')
                seznam_pristupu.refresh()

            ui.button('VYGENEROVAT ODKAZY', on_click=_generuj).classes(
                'h-12 px-8 text-lg font-bold bg-blue-500 hover:bg-blue-600 text-white rounded-2xl shadow-md')

            # --- Odkazy pro zkoušené, kteří v intranetu účet vůbec nemají ---
            ui.separator().classes('my-6')
            ui.label('👤 Odkazy bez účtu (jméno vyplní zkoušený)').classes('text-lg font-bold text-purple-600 mb-1')
            ui.label('Odkazy nikomu nepatří. Kdo přijde, vyplní jméno a příjmení a to se uloží '
                     'k jeho výsledku. Platí stejné okno platnosti jako výše.').classes('text-sm text-gray-500 mb-3')
            with ui.row().classes('items-center gap-4'):
                i_pocet = ui.number(label='Počet odkazů', value=10, min=1, max=100, precision=0).props('outlined dense').classes('w-40')

                def _generuj_anonymni():
                    okno = _okno()
                    if not okno: return
                    od, do = okno
                    try: pocet = int(i_pocet.value or 0)
                    except (TypeError, ValueError): pocet = 0
                    if not 1 <= pocet <= 100:
                        ui.notify('Zadejte počet 1 až 100.', type='negative', position='top'); return

                    nove = intranet_data.vytvor_anonymni_pristupy(pocet, od, do, user_id, vyber['skupina'])
                    if not nove:
                        ui.notify('Přístupy se nepodařilo vytvořit.', type='negative', position='top'); return
                    intranet_logger.log_activity(user_name, 'Kvíz', f'Vygeneroval {len(nove)} anonymních přístupů do kvízu')
                    ui.notify(f'Vygenerováno {len(nove)} odkazů bez účtu.', type='positive', position='top')
                    seznam_pristupu.refresh()

                ui.button('VYGENEROVAT BEZ ÚČTU', on_click=_generuj_anonymni).classes(
                    'h-12 px-8 text-lg font-bold bg-purple-500 hover:bg-purple-600 text-white rounded-2xl shadow-md')

        # Admin vidí odkazy i výsledky ze všech přístupů, zadavatel jen ze svých.
        vlastnik = None if is_admin else user_id

        @ui.refreshable
        def seznam_pristupu():
            zaznamy = intranet_data.seznam_kviz_pristupu(vlastnik)
            if not zaznamy:
                ui.label('Zatím nebyly vygenerovány žádné přístupy.').classes('text-gray-500 italic'); return

            # Odkaz na okno, které teprve začne, se rozesílá předem — patří mezi kopírovatelné.
            odkazy = [kviz_host_odkaz(z['token']) for z in zaznamy if z['je_platny'] or z.get('ceka')]
            if odkazy:
                ui.button(f'📋 Kopírovat všechny platné odkazy ({len(odkazy)})',
                          on_click=lambda: ui.clipboard.write('\n'.join(odkazy))).props('flat').classes('text-blue-600 mb-2')

            for z in zaznamy:
                url = kviz_host_odkaz(z['token'])
                platny = bool(z['je_platny'])
                ceka = bool(z.get('ceka'))
                barva = 'border-green-400' if platny else ('border-orange-400' if ceka else 'border-gray-300 opacity-60')
                with ui.row().classes(f'w-full items-center gap-3 border-l-4 {barva} bg-gray-50 rounded-lg px-3 py-2 mb-2 no-wrap'):
                    with ui.column().classes('gap-0 w-64 shrink-0'):
                        if z['name'] and z['surname']:
                            popis = f"{z['name']} {z['surname']}"
                        else:
                            popis = '👤 bez účtu – jméno nevyplněno'
                        ui.label(popis).classes('font-bold text-gray-800' if z['name'] else 'font-bold text-gray-500 italic')
                        ui.label(f"{z['platnost_od']:%d.%m. %H:%M} – {z['platnost_do']:%d.%m. %H:%M}")\
                            .classes('text-xs ' + ('text-orange-600 font-bold' if ceka else 'text-gray-500'))
                    ui.label((z.get('skupina') or '—').upper()).classes('text-xs font-bold w-12 shrink-0 text-purple-700')
                    if z.get('vysledek_uspesnost'):
                        hotovo = (z.get('vysledek_stav') or '').upper().startswith('DOKON')
                        ui.label(f"{'✅' if hotovo else '⚠️'} {z['vysledek_uspesnost']}")\
                            .classes('text-xs w-24 shrink-0 font-bold ' + ('text-green-700' if hotovo else 'text-orange-600'))\
                            .tooltip(z.get('vysledek_stav') or '')
                    else:
                        if z['pouzito_at']: stav_txt = '✅ použito'
                        elif ceka: stav_txt = '🕒 čeká'
                        elif platny: stav_txt = '⏳ nepoužito'
                        else: stav_txt = '⌛ propadlé'
                        ui.label(stav_txt).classes('text-xs w-24 shrink-0')
                    ui.input(value=url).props('readonly outlined dense').classes('flex-1')
                    ui.button(icon='content_copy', on_click=lambda u=url: ui.clipboard.write(u)).props('flat dense').tooltip('Kopírovat odkaz')
                    def _zrus(pid=z['id']):
                        intranet_data.zrus_kviz_pristup(pid, vlastnik)
                        seznam_pristupu.refresh()
                    ui.button(icon='delete', on_click=_zrus).props('flat dense color=negative').tooltip('Zrušit přístup')

        with ui.card().classes('w-full p-6 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm mb-6'):
            ui.label('📜 Vygenerované přístupy').classes('text-2xl font-bold mb-4 text-purple-600')
            seznam_pristupu()

        @ui.refreshable
        def vysledky_hostu():
            data = intranet_data.vysledky_kviz_pristupu(vlastnik)
            if not data:
                ui.label('Z vygenerovaných odkazů zatím nikdo test neodevzdal.').classes('text-gray-500 italic'); return
            rows = [{'jmeno': f"{(z['name'] or '?')} {(z['surname'] or '')}".strip() + (' 👤' if z['anonymni'] else ''),
                     'skupina': (z['skupina'] or '—').upper(),
                     'stav': z['stav_testu'], 'uspesnost': z['uspesnost'], 'body': z['body'],
                     'trvani': z['doba_trvani'], 'datum': f"{z['datum']:%d.%m.%Y %H:%M}"} for z in data]
            sloupce = [{'name': 'jmeno', 'label': 'Jméno', 'field': 'jmeno', 'align': 'left', 'sortable': True},
                       {'name': 'skupina', 'label': 'Typ', 'field': 'skupina', 'align': 'center'},
                       {'name': 'stav', 'label': 'Stav testu', 'field': 'stav', 'align': 'center'},
                       {'name': 'uspesnost', 'label': 'Úspěšnost', 'field': 'uspesnost', 'align': 'center', 'sortable': True},
                       {'name': 'body', 'label': 'Body', 'field': 'body', 'align': 'center'},
                       {'name': 'trvani', 'label': 'Trvání', 'field': 'trvani', 'align': 'center'},
                       {'name': 'datum', 'label': 'Odevzdáno', 'field': 'datum', 'align': 'center', 'sortable': True}]
            ui.table(columns=sloupce, rows=rows, pagination=20).classes('w-full')

            async def _export():
                def _soubor():
                    df = pd.DataFrame([{'Jméno': z['name'] or '', 'Příjmení': z['surname'] or '',
                                        'Anonymní': 'ano' if z['anonymni'] else '',
                                        'Typ kvízu': (z['skupina'] or '—').upper(), 'Stav': z['stav_testu'],
                                        'Úspěšnost': z['uspesnost'], 'Body': z['body'],
                                        'Trvání': z['doba_trvani'], 'Odevzdáno': z['datum']} for z in data])
                    kdo = {z['id']: (z['name'] or '', z['surname'] or '', z['datum']) for z in data}
                    odpovedi = []
                    conn = intranet_data.get_db_connection()
                    if conn and kdo:
                        cur = conn.cursor(dictionary=True)
                        cur.execute(f"SELECT vysledek_id, poradi, otazka, tvoje_volba, spravna_odpoved, hodnoceni "
                                    f"FROM zaznamy_odpovedi WHERE vysledek_id IN ({','.join(['%s'] * len(kdo))}) "
                                    f"ORDER BY vysledek_id, poradi", tuple(kdo))
                        for o in cur.fetchall():
                            j, p, d = kdo[o['vysledek_id']]
                            odpovedi.append({'Jméno': j, 'Příjmení': p, 'Odevzdáno': d,
                                             'Pořadí v testu': o['poradi'], 'Otázka': o['otazka'],
                                             'Tvoje volba': o['tvoje_volba'], 'Správná odpověď': o['spravna_odpoved'],
                                             'Hodnocení': o['hodnoceni']})
                        cur.close(); conn.close()
                    os.makedirs('Exporty_Kviz', exist_ok=True)
                    cesta = os.path.join('Exporty_Kviz', f"Hoste_Kviz_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
                    with pd.ExcelWriter(cesta, engine='openpyxl') as w:
                        df.to_excel(w, sheet_name='Výsledky', index=False)
                        if odpovedi: pd.DataFrame(odpovedi).to_excel(w, sheet_name='Analýza', index=False)
                    return cesta
                try:
                    ui.download(await asyncio.to_thread(_soubor))
                    intranet_logger.log_activity(user_name, 'Kvíz', 'Stažen export výsledků hostovských přístupů')
                except Exception as e:
                    ui.notify(f'Chyba při exportu: {e}', type='negative', position='top')

            with ui.row().classes('w-full justify-end gap-3 mt-4'):
                ui.button('🔄 Obnovit', on_click=vysledky_hostu.refresh).props('flat').classes('text-blue-600')
                ui.button('📥 Export do Excelu', on_click=_export).classes(
                    'bg-green-600 hover:bg-green-700 text-white font-bold px-6 shadow-md')

        with ui.card().classes('w-full p-6 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm mb-6'):
            ui.label('📊 Výsledky z odkazů' if is_admin else '📊 Výsledky z mých odkazů')\
                .classes('text-2xl font-bold mb-4 text-purple-600')
            ui.label('Testy odevzdané přes hostovské odkazy. Jméno s 👤 vyplnil zkoušený sám na vstupní stránce.')\
                .classes('text-sm text-gray-500 mb-4')
            vysledky_hostu()

    @ui.refreshable
    def hlavni_rozhrani():
        state['zobrazeny_index'] = local_ui['cas_label'] = local_ui['cas_vyzva'] = None  # nastaví je jen obrazovka testu
        zarovnani = 'justify-start pt-12' if state.get('zobrazit_admin_panel') else 'justify-center'

        with ui.column().classes(f'w-full max-w-6xl mx-auto items-center {zarovnani} h-screen p-4 overflow-y-auto'):

            if state['zobrazit_admin_panel'] and is_admin:
                with ui.row().classes('w-full justify-between items-center mb-6 pb-4 border-b-2 border-gray-200 dark:border-gray-700 skryt-pri-tisku'):
                    with ui.card().classes('p-2 bg-white/90 backdrop-blur-sm rounded-xl'):
                        ui.label('⚙️ Správa Kvízu').classes('text-4xl font-extrabold text-gray-800')
                    ui.button('Zpět na test', on_click=lambda: prepni_admin_panel(False)).classes('bg-blue-500 hover:bg-blue-600 text-lg rounded-2xl shadow-md')

                with ui.row().classes('w-full items-start no-wrap skryt-pri-tisku pb-10'):
                    with ui.card().classes('w-64 shrink-0 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm mr-6 p-2'):
                        with ui.tabs().props('vertical active-color="primary" indicator-color="primary"').classes('w-full text-left') as tabs:
                            tab_monitor = ui.tab('📡 Live Monitor').classes('justify-start')
                            tab_parametry = ui.tab('⚙️ Nastavení').classes('justify-start')
                            tab_otazky = ui.tab('📝 Otázky (CSV)').classes('justify-start')
                            tab_soubory = ui.tab('📁 Excel Výstupy').classes('justify-start')
                            tab_pristupy = ui.tab('🎟️ Přístupy hostů').classes('justify-start')

                    with ui.column().classes('flex-1 w-full'):
                        with ui.tab_panels(
                            tabs,
                            value=state.get('admin_aktivni_tab', tab_monitor)
                        ).classes('w-full bg-transparent p-0').on_value_change(lambda e: state.update(admin_aktivni_tab=e.value)):

                            with ui.tab_panel(tab_pristupy):
                                vykresli_spravu_pristupu()

                            with ui.tab_panel(tab_monitor):
                                with ui.card().classes('w-full p-6 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm mb-6'):
                                    ui.label('▶ Aktuálně píšící studenti').classes('text-2xl font-bold mb-4 text-purple-600')
                                    monitor_kontejner = ui.column().classes('w-full')
                                    def obnov_monitor():
                                        monitor_kontejner.clear()

                                        aktualni_cas = time.time()
                                        # Rozpracované testy zůstávají i bez spojení (dopíší se po návratu).
                                        rozpracovane = list(ROZPRACOVANE.values())

                                        with monitor_kontejner:
                                            if not rozpracovane:
                                                ui.label('Zatím nikdo nedělá test.').classes('text-gray-500 italic')
                                            else:
                                                sloupce = [{'name': 'jmeno', 'label': 'Jméno studenta', 'field': 'jmeno', 'align': 'left'}, {'name': 'otazka', 'label': 'Aktuální otázka', 'field': 'otazka', 'align': 'center'}, {'name': 'cas', 'label': 'Zbývající čas', 'field': 'cas', 'align': 'center'}, {'name': 'spojeni', 'label': 'Spojení', 'field': 'spojeni', 'align': 'center'}]
                                                if (kontrola := _kontrola_zapnuta()):
                                                    sloupce.append({'name': 'mimo', 'label': 'Mimo test', 'field': 'mimo', 'align': 'center'})
                                                data = []
                                                for i, st in enumerate(rozpracovane):
                                                    m, s = divmod(_zbyva_s(st), 60)
                                                    odpojeno = st.get('_odpojeno_ts')
                                                    spojeni = f'⚠️ bez spojení {int(aktualni_cas - odpojeno)} s' if odpojeno else 'v pořádku'
                                                    ted_mimo, opusteni = bool(st.get('_mimo_ts')), st.get('opusteni', 0)
                                                    data.append({'id': i, 'jmeno': st['jmeno'], 'otazka': f"{min(st['aktualni_index'] + 1, len(st['otazky']))} / {len(st['otazky'])}", 'cas': f"{m:02d}:{s:02d}", 'spojeni': spojeni,
                                                                 'mimo': _mimo_text(opusteni, _mimo_s(st, aktualni_cas), ted_mimo), 'poruseni': opusteni > 0, 'ted_mimo': ted_mimo})
                                                tabulka = ui.table(columns=sloupce, rows=data).classes('w-full')
                                                if kontrola:
                                                    tabulka.add_slot('body', _SLOT_DOHLED)
                                    ui.timer(3.0, obnov_monitor)

                                with ui.card().classes('w-full p-6 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm'):
                                    ui.label('✅ Dokončené testy').classes('text-2xl font-bold mb-4 text-green-600')
                                    dokoncene_kontejner = ui.column().classes('w-full')
                                    def obnov_dokoncene():
                                        dokoncene_kontejner.clear()
                                        with dokoncene_kontejner:
                                            if not dokoncene_testy: ui.label('Zatím nikdo nedokončil test.').classes('text-gray-500 italic')
                                            else:
                                                cols_dokonceno = [{'name': 'jmeno', 'label': 'Jméno', 'field': 'jmeno', 'align': 'left'}, {'name': 'cas', 'label': 'Odevzdáno', 'field': 'cas_odevzdani', 'align': 'center'}, {'name': 'uspesnost', 'label': 'Úspěšnost', 'field': 'uspesnost', 'align': 'center'}, {'name': 'skore', 'label': 'Skóre', 'field': 'skore', 'align': 'center'}, {'name': 'stav', 'label': 'Stav testu', 'field': 'vysledek', 'align': 'center'}]
                                                if (kontrola := _kontrola_zapnuta()):
                                                    cols_dokonceno.append({'name': 'mimo', 'label': 'Mimo test', 'field': 'mimo', 'align': 'center'})
                                                radky = [dict(r, id=i) for i, r in enumerate(dokoncene_testy)]
                                                tabulka = ui.table(columns=cols_dokonceno, rows=radky).classes('w-full')
                                                if kontrola:
                                                    tabulka.add_slot('body', _SLOT_DOHLED)
                                    ui.timer(5.0, obnov_dokoncene)

                            with ui.tab_panel(tab_parametry):
                                with ui.card().classes('w-full p-8 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm'):

                                    # ── AKTUÁLNÍ SKUPINA (globální přepínač) ─
                                    with ui.card().classes('w-full p-5 mb-6 border-2 border-blue-300 rounded-2xl bg-blue-50'):
                                        with ui.row().classes('items-center gap-4 flex-wrap'):
                                            with ui.column().classes('gap-1'):
                                                ui.label('Aktuální skupina').classes('text-lg font-extrabold text-blue-800')

                                            def _zmen_aktivni_skupinu(hodnota):
                                                config['aktivni_skupina'] = hodnota
                                                try:
                                                    intranet_data.zapis_json_atomicky(SOUBOR_NASTAVENI, config, indent=4)
                                                    ui.notify(f'Aktivní skupina změněna na {hodnota.upper()}', type='positive', position='top')
                                                except Exception as _e:
                                                    ui.notify(f'Chyba při ukládání: {_e}', type='negative', position='top')
                                                hlavni_rozhrani.refresh()

                                            ui.toggle(
                                                {'oz': '🧑‍💼 OZ', 'asm': '👔 ASM'},
                                                value=config.get('aktivni_skupina', 'oz'),
                                                on_change=lambda e: _zmen_aktivni_skupinu(e.value)
                                            ).classes('font-extrabold text-lg')

                                    # ── KONTROLNÍ MECHANISMUS (globální přepínač) ─
                                    with ui.card().classes('w-full p-5 mb-6 border-2 border-red-200 rounded-2xl bg-red-50'):
                                        with ui.row().classes('w-full items-center justify-between gap-4 no-wrap'):
                                            with ui.column().classes('gap-1'):
                                                ui.label('Kontrolní mechanismus').classes('text-lg font-extrabold text-red-800')
                                                ui.label('Test přes celou obrazovku, jejím opuštěním test končí. Blokace kláves a kopírování, '
                                                         'kliknutí mimo test uvidíte v Live Monitoru (rudá tečka). Přepnout lze, jen když nikdo nepíše test.') \
                                                    .classes('text-sm text-red-900')
                                                blokace_kontroly = ui.label().classes('text-sm font-bold text-red-700')

                                            def _zmen_kontrolu(hodnota):
                                                if bool(hodnota) == _kontrola_zapnuta():
                                                    return  # přepínač se jen srovnal s uloženým stavem
                                                if (pisici := _pisici_test()):
                                                    # Pojistka: test mohl začít těsně před kliknutím (zámek se obnovuje po 2 s).
                                                    prepinac_kontroly.value = _kontrola_zapnuta()
                                                    ui.notify(f'Nelze přepnout – test právě píše: {pisici}', type='warning', position='top')
                                                    return
                                                config['kontrolni_mechanismus'] = bool(hodnota)
                                                stav = 'zapnut' if hodnota else 'vypnut'
                                                try:
                                                    intranet_data.zapis_json_atomicky(SOUBOR_NASTAVENI, config, indent=4)
                                                    ui.notify(f'Kontrolní mechanismus {stav}', type='positive', position='top')
                                                except Exception as _e:
                                                    ui.notify(f'Chyba při ukládání: {_e}', type='negative', position='top')
                                                intranet_logger.log_activity(user_name, 'Kvíz', f'Kontrolní mechanismus {stav}', **zarizeni)

                                            prepinac_kontroly = ui.switch(value=_kontrola_zapnuta(), on_change=lambda e: _zmen_kontrolu(e.value)) \
                                                .props('color=red size=xl').classes('shrink-0')

                                            def _hlidej_prepinac():
                                                pisici = _pisici_test()
                                                prepinac_kontroly.set_enabled(not pisici)
                                                blokace_kontroly.set_text(f'🔒 Nelze přepnout – test právě píše: {pisici}')
                                                blokace_kontroly.set_visibility(bool(pisici))
                                                if prepinac_kontroly.value != _kontrola_zapnuta():
                                                    prepinac_kontroly.value = _kontrola_zapnuta()  # přepnul jiný správce

                                            _hlidej_prepinac()
                                            ui.timer(2.0, _hlidej_prepinac)

                                    # ── toggle pro editaci nastavení (ASM / OZ)
                                    if 'admin_kviz_typ' not in state:
                                        state['admin_kviz_typ'] = 'oz'

                                    def _prepni_admin_typ(hodnota):
                                        state['admin_kviz_typ'] = hodnota
                                        hlavni_rozhrani.refresh()

                                    with ui.row().classes('items-center gap-4 mb-6'):
                                        ui.label('Skupina:').classes('font-bold text-gray-700 text-lg')
                                        ui.toggle(
                                            {'oz': '🧑‍💼 OZ', 'asm': '👔 ASM'},
                                            value=state.get('admin_kviz_typ', 'oz'),
                                            on_change=lambda e: _prepni_admin_typ(e.value)
                                        ).classes('font-bold')

                                    edit_typ = state.get('admin_kviz_typ', 'oz')
                                    c = config[edit_typ]
                                    soubory_skupiny = SOUBORY_OKRUHU[edit_typ]
                                    barva_skupiny = 'text-blue-700' if edit_typ == 'oz' else 'text-indigo-700'

                                    ui.label(f'Celkový počet otázek v testu ({edit_typ.upper()}):').classes(f'text-lg font-medium {barva_skupiny}')
                                    with ui.row().classes('w-full items-center mb-8'):
                                        _sl = ui.slider(value=c.get('pocet_otazek', 1), min=1, max=500, step=1).classes('w-2/3')
                                        ui.number(min=1, max=500, step=1, format='%d').classes('w-24 ml-4 bg-white').bind_value(_sl, 'value')
                                        _sl.on_value_change(lambda e: c.update(pocet_otazek=e.value))

                                    ui.separator().classes('mb-6')
                                    ui.label('Rozdělení otázek do okruhů').classes(f'text-xl font-bold {barva_skupiny} mb-1')
                                    ui.label('Součet otázek z okruhů musí odpovídat celkovému počtu výše.').classes('text-sm text-gray-500 mb-4')

                                    soucet_label = ui.label().classes('text-base font-semibold mb-4')

                                    def aktualizuj_soucet_label(et=edit_typ, cc=c, sl=soucet_label):
                                        try:
                                            s = sum([
                                                int(cc['okruh_1_pocet'] or 0) if cc.get('okruh_1_zapnut', True)  else 0,
                                                int(cc['okruh_2_pocet'] or 0) if cc.get('okruh_2_zapnut', False) else 0,
                                                int(cc['okruh_3_pocet'] or 0) if cc.get('okruh_3_zapnut', False) else 0,
                                            ])
                                            celkem = int(cc['pocet_otazek'] or 1)
                                            ok = s == celkem
                                            sl.set_text(f'Součet zapnutých okruhů: {s}  {"✅ Odpovídá" if ok else f"⚠️ Neodpovídá celkovému počtu {celkem}"}')
                                            sl.classes(replace='text-base font-semibold mb-4 ' + ('text-green-600' if ok else 'text-red-600'))
                                        except Exception: pass

                                    for idx, (nazev_key, pocet_key, zapnut_key, soubor) in enumerate([
                                        ('okruh_1_nazev', 'okruh_1_pocet', 'okruh_1_zapnut', soubory_skupiny[0]),
                                        ('okruh_2_nazev', 'okruh_2_pocet', 'okruh_2_zapnut', soubory_skupiny[1]),
                                        ('okruh_3_nazev', 'okruh_3_pocet', 'okruh_3_zapnut', soubory_skupiny[2]),
                                    ], 1):
                                        max_ok = get_max_questions(soubor)
                                        soubor_stav = f'✅ {max_ok} otázek' if max_ok > 0 else '❌ soubor nenáhrán'
                                        zapnut = c.get(zapnut_key, idx == 1)
                                        with ui.card().classes('w-full p-4 mb-3 border border-gray-200 rounded-xl ' + ('bg-white' if zapnut else 'bg-gray-100 opacity-70')):
                                            with ui.row().classes('w-full items-center gap-4 mb-2'):
                                                tog = ui.switch(f'Okruh {idx}', value=zapnut).classes('font-bold text-gray-700')
                                                _inp = ui.input(placeholder=f'Název okruhu {idx}', value=c.get(nazev_key, '')).classes('flex-1 bg-white')
                                                _inp.set_enabled(zapnut)
                                                _inp.on_value_change(lambda e, k=nazev_key: c.update({k: e.value}))
                                                ui.badge(soubor_stav, color='green' if max_ok > 0 else 'red').classes('shrink-0')
                                            with ui.row().classes('w-full items-center gap-3'):
                                                ui.label('Počet losovaných otázek:').classes('text-sm text-gray-600 w-48 shrink-0')
                                                _num = ui.number(min=0, max=max(max_ok, 500), step=1, format='%d', value=c.get(pocet_key, 0)) \
                                                    .classes('w-24 bg-white').on('update:model-value', lambda _: aktualizuj_soucet_label())
                                                _num.set_enabled(zapnut)
                                                _num.on_value_change(lambda e, k=pocet_key: c.update({k: e.value}))
                                                tog.on_value_change(lambda e, i=_inp, n=_num, k=zapnut_key: (c.update({k: e.value}),
                                                                                                            i.set_enabled(e.value),
                                                                                                            n.set_enabled(e.value),
                                                                                                            aktualizuj_soucet_label()))

                                    aktualizuj_soucet_label()
                                    ui.separator().classes('my-6')

                                    ui.label(f'Časový limit testu — {edit_typ.upper()} (min):').classes(f'text-lg font-medium {barva_skupiny}')
                                    with ui.row().classes('w-full items-center mb-6'):
                                        _sl = ui.slider(value=c.get('casovy_limit_minuty', 1), min=1, max=120, step=1).classes('w-2/3 text-blue-500')
                                        ui.number(min=1, max=120, step=1, format='%d').classes('w-24 ml-4 bg-white').bind_value(_sl, 'value')
                                        _sl.on_value_change(lambda e: c.update(casovy_limit_minuty=e.value))
                                    ui.label(f'Minimální úspěšnost — {edit_typ.upper()} (%):').classes(f'text-lg font-medium {barva_skupiny}')
                                    with ui.row().classes('w-full items-center mb-6'):
                                        _sl = ui.slider(value=c.get('min_procento_uspesnosti', 0), min=0, max=100, step=1).classes('w-2/3 text-green-500')
                                        ui.number(min=0, max=100, step=1, format='%d').classes('w-24 ml-4 bg-white').bind_value(_sl, 'value')
                                        _sl.on_value_change(lambda e: c.update(min_procento_uspesnosti=e.value))
                                    ui.button(
                                        f'💾 Uložit nastavení {edit_typ.upper()}',
                                        on_click=lambda t=edit_typ: uloz_nastaveni(t)
                                    ).classes('w-64 h-12 mt-4 bg-green-500 hover:bg-green-600 text-white font-bold rounded-2xl shadow-md')

                            with ui.tab_panel(tab_otazky):
                                with ui.card().classes('w-full p-8 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm'):
                                    ui.label('📝 Nahrání souborů s otázkami').classes('text-2xl font-bold mb-2 text-blue-600')
                                    ui.label('Každý okruh má vlastní soubor CSV. Starý soubor bude přepsán. Změny se projeví v dalším testu.').classes('mb-6 text-gray-500')

                                    async def _nahrat_do_souboru(e, cilovy_soubor):
                                        nazev = getattr(e, 'name', '')
                                        if nazev and not nazev.lower().endswith('.csv'):
                                            ui.notify('CHYBA: Lze nahrávat POUZE soubory ve formátu .csv!', type='negative', position='top')
                                            return
                                        try:
                                            obsah = None
                                            zdroj = getattr(e, 'content', None) or getattr(e, 'file', None) or getattr(e, 'stream', None)
                                            if not zdroj:
                                                for attr_name in dir(e):
                                                    attr_val = getattr(e, attr_name)
                                                    if hasattr(attr_val, 'read'):
                                                        zdroj = attr_val; break
                                            if zdroj and hasattr(zdroj, 'read'):
                                                cteni = zdroj.read()
                                                obsah = await cteni if asyncio.iscoroutine(cteni) else cteni
                                            if obsah is None:
                                                return ui.notify('CHYBA VERZE: Nelze načíst data souboru.', type='negative', position='top')
                                            with open(cilovy_soubor, 'wb') as f: f.write(obsah)
                                            ui.notify(f'Soubor {cilovy_soubor} byl úspěšně nahrán!', type='positive', position='top')
                                        except Exception as err:
                                            ui.notify(f'Chyba: {err}', type='negative', position='top')

                                    def _vykresli_okruhy_skupiny(typ, barva_nadpisu, nazev_skupiny):
                                        soubory_sk = SOUBORY_OKRUHU[typ]
                                        c_sk = config[typ]
                                        with ui.expansion(
                                            f'{nazev_skupiny} — okruhy otázek',
                                            icon='folder_open'
                                        ).classes(f'w-full border border-gray-200 rounded-xl mb-4 font-bold {barva_nadpisu}').props('default-opened'):
                                            for idx, (soubor, nazev_key) in enumerate([
                                                (soubory_sk[0], 'okruh_1_nazev'),
                                                (soubory_sk[1], 'okruh_2_nazev'),
                                                (soubory_sk[2], 'okruh_3_nazev'),
                                            ], 1):
                                                max_ok = get_max_questions(soubor)
                                                nazev_okruhu = c_sk.get(nazev_key, f'Okruh {idx}')
                                                stav = f'✅ Nahrán — {max_ok} otázek' if max_ok > 0 else '❌ Soubor nenáhrán'
                                                barva_stavu = 'text-green-600' if max_ok > 0 else 'text-red-500'
                                                with ui.card().classes('w-full p-5 mb-3 border border-gray-200 rounded-xl bg-gray-50'):
                                                    with ui.row().classes('w-full items-center justify-between mb-3'):
                                                        ui.label(f'Okruh {idx} — {nazev_okruhu}').classes('text-lg font-bold text-gray-800')
                                                        ui.label(stav).classes(f'text-sm font-semibold {barva_stavu}')
                                                    ui.label(f'Soubor: {soubor}').classes('text-xs text-gray-400 mb-2')
                                                    ui.upload(
                                                        on_upload=lambda e, s=soubor: _nahrat_do_souboru(e, s),
                                                        label=f'Vyberte CSV pro Okruh {idx}',
                                                        auto_upload=True
                                                    ).props('accept=".csv"').classes('max-w-md w-full')

                                    _vykresli_okruhy_skupiny('oz',  'text-blue-700',   '🧑‍💼 OZ')
                                    _vykresli_okruhy_skupiny('asm', 'text-indigo-700', '👔 ASM')

                            with ui.tab_panel(tab_soubory):
                                with ui.card().classes('w-full p-8 shadow-xl rounded-2xl border border-gray-100 bg-white/95 backdrop-blur-sm'):
                                    slozka = "Vysledky_Web"
                                    os.makedirs(slozka, exist_ok=True)
                                    vybrane_soubory = set()
                                    def checkbox_zmena(e, soubor): vybrane_soubory.add(soubor) if e.value else vybrane_soubory.discard(soubor)
                                    def smazat_vybrane():
                                        if not vybrane_soubory: return ui.notify('Nejprve zaškrtněte testy k vymazání.', type='warning', position='top')
                                        for f in vybrane_soubory:
                                            try: os.remove(os.path.join(slozka, f))
                                            except Exception: pass
                                        ui.notify('Vybrané testy byly smazány.', type='positive', position='top'); obnovit_seznam_souboru()
                                    def stahnout_vybrane():
                                        if not vybrane_soubory: return ui.notify('Nejprve zaškrtněte testy ke stažení.', type='warning', position='top')
                                        zip_path = os.path.join(slozka, "Vybrane_testy_export.zip")
                                        try:
                                            with zipfile.ZipFile(zip_path, 'w') as zf:
                                                for f in vybrane_soubory:
                                                    cesta = os.path.join(slozka, f)
                                                    if os.path.exists(cesta): zf.write(cesta, arcname=f)
                                            ui.download(zip_path); ui.notify('Stahování ZIP archivu začalo.', type='positive', position='top')
                                        except Exception as e: ui.notify(f'Chyba ZIP: {e}', type='negative', position='top')
                                    def vytisknout_web(cesta_k_souboru):
                                        try:
                                            df_s = pd.read_excel(cesta_k_souboru, sheet_name='Souhrn')
                                            df_a = pd.read_excel(cesta_k_souboru, sheet_name='Analýza')
                                            with ui.dialog() as print_dlg, ui.card().classes('w-full max-w-4xl bg-white text-black p-8'):
                                                ui.label('Zkušební protokol - Výsledek testu').classes('text-3xl font-bold mb-6 text-center')
                                                cols_s = [{'name': c, 'label': c, 'field': c, 'align': 'left'} for c in df_s.columns]
                                                ui.table(columns=cols_s, rows=df_s.to_dict('records')).classes('w-full mb-8 shadow-sm')
                                                ui.label('Analýza odpovědí').classes('text-2xl font-bold mb-4')
                                                cols_a = [{'name': c, 'label': c, 'field': c, 'align': 'left'} for c in df_a.columns]
                                                ui.table(columns=cols_a, rows=df_a.to_dict('records')).classes('w-full mb-8 shadow-sm')
                                                with ui.row().classes('w-full justify-between mt-4 skryt-pri-tisku'):
                                                    ui.button('Zavřít náhled', on_click=print_dlg.close).classes('bg-gray-500 hover:bg-gray-600 text-white')
                                                    ui.button('🖨️ Fyzicky Vytisknout', on_click=lambda: ui.run_javascript('window.print()')).classes('bg-orange-500 hover:bg-orange-600 text-white font-bold px-8')
                                            print_dlg.open()
                                        except Exception as e: ui.notify(f'Chyba při přípravě tisku: {e}', type='negative')

                                    with ui.row().classes('w-full items-center justify-between mb-6 pb-4 border-b border-gray-200'):
                                        ui.label('📁 Odevzdané testy').classes('text-2xl font-bold text-blue-600')
                                        with ui.row().classes('space-x-4'):
                                            ui.button('🔄 Obnovit', color='blue', on_click=lambda: obnovit_seznam_souboru()).classes('font-bold shadow-md')
                                            ui.button('🗑️ Smazat', color='red', on_click=smazat_vybrane).classes('font-bold shadow-md')
                                            ui.button('📦 Stáhnout (ZIP)', color='green', on_click=stahnout_vybrane).classes('font-bold shadow-md')

                                    kontejner_souboru = ui.column().classes('w-full space-y-2')
                                    def obnovit_seznam_souboru():
                                        vybrane_soubory.clear(); kontejner_souboru.clear()
                                        with kontejner_souboru:
                                            soubory = [f for f in os.listdir(slozka) if f.startswith('Vysledek_') and f.endswith('.xlsx')]
                                            if not soubory: ui.label('Zatím nebyly odevzdány žádné testy.').classes('text-gray-500 italic text-lg')
                                            else:
                                                for f in sorted(soubory, reverse=True)[:50]:
                                                    cesta_k_souboru = os.path.join(slozka, f)
                                                    with ui.row().classes('w-full items-center justify-between border-b border-gray-100 p-2 hover:bg-gray-50'):
                                                        with ui.row().classes('items-center space-x-4'):
                                                            ui.checkbox(on_change=lambda e, f_name=f: checkbox_zmena(e, f_name))
                                                            ui.label(f"📄 {f}").classes('text-lg text-gray-700 font-medium')
                                                        with ui.row().classes('space-x-2'):
                                                            ui.button('🖨️ Tisk', on_click=lambda c=cesta_k_souboru: vytisknout_web(c)).classes('bg-orange-500 text-white px-3 py-1 shadow-sm hover:bg-orange-600')
                                                            ui.button('📥 Excel', on_click=lambda c=cesta_k_souboru: ui.download(c)).classes('bg-blue-500 text-white px-3 py-1 shadow-sm hover:bg-blue-600')
                                    obnovit_seznam_souboru()

            # --- A2) SPRÁVA PŘÍSTUPŮ BEZ PLNÝCH ADMIN PRÁV ---
            elif state['zobrazit_admin_panel'] and muze_generovat_hosty:
                with ui.row().classes('w-full justify-between items-center mb-6 pb-4 border-b-2 border-gray-200 dark:border-gray-700 skryt-pri-tisku'):
                    with ui.card().classes('p-2 bg-white/90 backdrop-blur-sm rounded-xl'):
                        ui.label('🎟️ Přístupy do Kvízu').classes('text-4xl font-extrabold text-gray-800')
                    ui.button('Zpět na test', on_click=lambda: prepni_admin_panel(False)).classes('bg-blue-500 hover:bg-blue-600 text-lg rounded-2xl shadow-md')
                with ui.column().classes('w-full skryt-pri-tisku pb-10'):
                    vykresli_spravu_pristupu()

            # --- B) BĚŽÍCÍ TEST ---
            elif state['bezi_test'] and (zaznam := ROZPRACOVANE.get(klic)) and zaznam['aktualni_index'] < len(zaznam['otazky']):
                idx, otazky = zaznam['aktualni_index'], zaznam['otazky']
                state['zobrazeny_index'] = idx
                # Znamení pro skript v hlavičce: .kviz-bezi = běží test (displej zapnutý),
                # .kviz-kontrola = kontrolní mechanismus (celá obrazovka, dohled, blokace).
                kontrola = _kontrola_zapnuta()
                with ui.column().classes('w-full max-w-4xl mt-12 skryt-pri-tisku kviz-bezi' + (' kviz-kontrola' if kontrola else '')):
                    if kontrola:
                        # Mimo celou obrazovku jsou otázky skryté a místo nich je tahle výzva (viz CSS).
                        with ui.element('div').classes('kviz-fs-vyzva'):
                            ui.label('Test běží na celé obrazovce').classes('text-4xl font-extrabold text-gray-800')
                            ui.label('Čas testu běží dál. Pro pokračování se vraťte na celou obrazovku.').classes('text-xl text-gray-600')
                            local_ui['cas_vyzva'] = ui.label().classes('text-3xl font-bold tracking-widest text-blue-600')
                            ui.button('POKRAČOVAT V TESTU').classes('kviz-na-celou h-16 px-10 text-2xl font-bold bg-blue-500 hover:bg-blue-600 text-white rounded-2xl shadow-lg')
                            ui.label('Opuštění celé obrazovky během testu test ukončí.').classes('text-base font-semibold text-red-600')
                    with ui.column().classes('w-full kviz-obsah-testu'):
                        with ui.row().classes('w-full justify-between items-center mb-8 border-b-2 pb-4 border-gray-200'):
                            ui.label(f'Otázka {idx + 1} z {len(otazky)}').classes('text-2xl text-gray-600 font-bold')
                            local_ui['cas_label'] = ui.label().classes('text-3xl font-bold tracking-widest')
                            aktualizuj_zobrazeni_casu(zaznam)

                        aktualni_q = otazky[idx]
                        ui.label(aktualni_q['OTÁZKA']).classes('text-4xl font-extrabold mb-10 text-center text-gray-800 w-full skryt-pri-tisku')
                        moznosti = {'A': f"A)  {aktualni_q['A']}", 'B': f"B)  {aktualni_q['B']}", 'C': f"C)  {aktualni_q['C']}", 'D': f"D)  {aktualni_q['D']}"}
                        # Rozkliknutá odpověď patří k testu, ne ke stránce → přežije i znovunačtení.
                        ui.radio(moznosti, value=zaznam.get('vybrana_odpoved')).classes('text-2xl mb-12 space-y-6 w-full ml-12 text-gray-800 font-medium skryt-pri-tisku').on_value_change(lambda e: zaznam.update(vybrana_odpoved=e.value))

                        text_tlacitka = 'Odevzdat test' if idx == len(otazky) - 1 else 'Další otázka'
                        ui.button(text_tlacitka, on_click=lambda: dalsi_otazka(idx)).classes('w-[25rem] h-20 text-3xl font-bold bg-blue-500 hover:bg-blue-600 text-white rounded-xl shadow-lg mx-auto block skryt-pri-tisku')

            # --- C) VÝSLEDKY PO TESTU ---
            elif state['vysledek'] is not None:
                with ui.column().classes('w-full max-w-4xl items-center mt-12 skryt-pri-tisku'):
                    ui.label('ZPRACOVÁNÍ TESTU DOKONČENO').classes('text-4xl font-extrabold mb-8 text-gray-800 skryt-pri-tisku')
                    v = state['vysledek']
                    if v['stav'] == 'ČAS VYPRŠEL':
                        ui.label('Čas vypršel – test byl odevzdán automaticky.').classes('text-xl text-gray-500 mb-4 skryt-pri-tisku')
                    elif v['stav'] == 'PŘERUŠENO':
                        ui.label('Test byl ukončen – opustil(a) jste celou obrazovku.').classes('text-xl font-bold text-red-600 mb-4 skryt-pri-tisku')
                    celkem, uspesnost, min_usp = v['celkem'], v['proc'], v['min_procento']
                    barva = 'text-green-500' if uspesnost >= min_usp else 'text-red-600'
                    stav_text = "ÚSPĚŠNĚ SPLNĚNO" if uspesnost >= min_usp else "NESPLNĚNO"

                    ui.label(stav_text).classes(f'text-5xl font-black mb-4 {barva} skryt-pri-tisku')
                    ui.label(f'Úspěšnost: {uspesnost:.2f} % (Požadováno {min_usp} %)').classes(f'text-4xl font-bold mb-4 {barva} skryt-pri-tisku')
                    ui.label(f'Získané body: {v["body"]} z {celkem}').classes('text-3xl mb-12 text-gray-600 font-bold skryt-pri-tisku')

                    with ui.dialog().props('maximized transition-show="slide-up" transition-hide="slide-down"') as vysledky_dlg:
                        with ui.card().classes('w-full h-full max-w-full m-0 p-8 bg-white overflow-y-auto'):
                            ui.label('Zkušební protokol - Výsledek testu').classes('text-4xl font-bold mb-6 text-center text-gray-800')

                            df_s = [{"Jméno": v['jmeno'], "Stav": v['stav'], "Úspěšnost": f"{v['proc']:.2f}%", "Bodů": f"{v['body']}/{celkem}", "Trvání": f"{v['m']}m {v['s']}s"}]
                            cols_s = [{'name': c, 'label': c, 'field': c, 'align': 'left'} for c in df_s[0].keys()]
                            ui.table(columns=cols_s, rows=df_s).classes('w-full mb-8 shadow-sm text-lg')

                            ui.label('Analýza odpovědí').classes('text-3xl font-bold mb-4 text-gray-800')
                            if v['historie']:
                                cols_a = [{'name': c, 'label': c, 'field': c, 'align': 'left'} for c in v['historie'][0].keys()]
                                ui.table(columns=cols_a, rows=v['historie']).classes('w-full mb-8 shadow-sm text-base')

                            with ui.row().classes('w-full justify-center mt-8 skryt-pri-tisku gap-4'):
                                ui.button('Zavřít výsledky', on_click=vysledky_dlg.close).classes('bg-gray-500 hover:bg-gray-600 text-white px-8 h-14 text-lg font-bold shadow-md')
                                ui.button('🖨️ Vytisknout', on_click=lambda: ui.run_javascript('window.print()')).classes('bg-orange-500 hover:bg-orange-600 text-white font-bold px-8 h-14 text-lg shadow-md ml-4')

                    with ui.row().classes('gap-4 mt-8'):
                        if not je_host:
                            ui.button('Zpět na Přehled', on_click=_nav_na_prehled).classes('bg-blue-600 hover:bg-blue-700 text-white font-bold h-16 px-8 text-xl shadow-md')

                        if ma_pristup_k_vysledkum:
                            ui.button('📊 Zobrazit mé výsledky', on_click=vysledky_dlg.open).classes('bg-green-600 hover:bg-green-700 text-white font-bold h-16 px-8 text-xl shadow-md')

            else:
                with ui.column().classes('w-full max-w-3xl items-center mt-20 skryt-pri-tisku'):
                    try: ui.image('logo.png').classes('w-64 mb-6 skryt-pri-tisku')
                    except Exception: pass

                    ui.label('Testovací systém JIP').classes('text-3xl font-bold text-blue-600 mb-8 text-center skryt-pri-tisku')

                    ui.label(f'Přihlášen(a): {state["jmeno"]}').classes('text-2xl font-bold mb-4 text-gray-700 skryt-pri-tisku')
                    skupina_label = '👔 ASM' if kviz_typ == 'asm' else '🧑‍💼 OZ'
                    ui.label(f'Skupina: {skupina_label}').classes('text-base font-semibold text-gray-500 mb-2 skryt-pri-tisku')
                    ui.label(f'Čeká vás {config[kviz_typ]["pocet_otazek"]} otázek. Na vypracování máte {config[kviz_typ]["casovy_limit_minuty"]} minut.').classes('text-lg text-gray-500 mb-8 skryt-pri-tisku')

                    with ui.row().classes('gap-4 items-center'):
                        if not je_host:
                            ui.button('Zpět na Přehled', on_click=_nav_na_prehled).classes('h-20 text-xl font-bold bg-gray-500 hover:bg-gray-600 text-white shadow-md rounded-2xl skryt-pri-tisku')
                        ui.button('SPUSTIT TEST', on_click=spustit_test).classes(('kviz-na-celou ' if _kontrola_zapnuta() else '') + 'w-[25rem] h-20 text-3xl font-bold bg-blue-500 hover:bg-blue-600 text-white shadow-xl rounded-2xl skryt-pri-tisku')

                    if is_admin:
                        ui.button('⚙️ Správa Kvízu', on_click=lambda: prepni_admin_panel(True)).classes('mt-12 w-64 h-12 text-lg bg-gray-800 hover:bg-gray-700 text-white shadow-md rounded-2xl skryt-pri-tisku')
                    elif muze_generovat_hosty:
                        ui.button('🎟️ Přístupy do Kvízu', on_click=lambda: prepni_admin_panel(True)).classes('mt-12 w-64 h-12 text-lg bg-gray-800 hover:bg-gray-700 text-white shadow-md rounded-2xl skryt-pri-tisku')

    def aktualizuj_zobrazeni_casu(zaznam: dict) -> None:
        s = _zbyva_s(zaznam)
        if local_ui['cas_vyzva']:
            local_ui['cas_vyzva'].set_text(f'Zbývá ⏱ {s // 60:02d}:{s % 60:02d}')
        if local_ui['cas_label']:
            local_ui['cas_label'].set_text(f'⏱ {s // 60:02d}:{s % 60:02d}')
            local_ui['cas_label'].classes(replace='text-3xl font-bold tracking-widest text-red-600' if s <= 600 else 'text-3xl font-bold tracking-widest text-blue-600')

    async def tik_casovace():
        """Zbývající čas se počítá z konec_ts — po výpadku spojení nebo uspání
        notebooku tak sedí hned po návratu."""
        if not state['bezi_test']:
            return
        zaznam = ROZPRACOVANE.get(klic)
        if zaznam is None:
            # Test mezitím odevzdalo jiné okno nebo úklid po vypršení času.
            state['bezi_test'] = False
            state['vysledek'] = prevezmi_nevideny()
            if state['vysledek'] is None:
                ui.notify('Test už byl odevzdán v jiném okně.', type='info', position='top')
            hlavni_rozhrani.refresh()
        elif _zbyva_s(zaznam) <= 0 or zaznam['aktualni_index'] >= len(zaznam['otazky']):
            if _zbyva_s(zaznam) <= 0:
                ui.notify('Čas vypršel! Odevzdáno.', type='negative')
            await ukoncit_test()
        elif state['zobrazeny_index'] != zaznam['aktualni_index']:
            hlavni_rozhrani.refresh()  # odpověď z jiného okna / obnovený test
        else:
            aktualizuj_zobrazeni_casu(zaznam)

    ui.timer(1.0, tik_casovace)

    # --- Hlídání změn práv (obdoba kontrola_prav v intranet.py). Obrazovka s výsledkem
    # žije jen v této stránce, takže reload by ji zahodil: probíhající test se nepřeruší
    # nikdy, výsledek jen při ztrátě přístupu. Odložená změna se projeví při dalším
    # otevření stránky.
    if not je_host and user_id:
        _KVIZ_PRAVA = {'vse', 'kviz', 'kviz_hoste', 'vystup_osobni', 'vystup_vse'}
        _prava_kvizu = {p.lower() for p in vsechna_prava} & _KVIZ_PRAVA
        _stav_prav = {'verze': _verze_prav_stranky}

        async def kontrola_prav_kvizu():
            if state.get('bezi_test') or not client.has_socket_connection:
                return
            verze = intranet_data.verze_prav(user_id)
            if verze == _stav_prav['verze']:
                return
            try:
                vysledek = await asyncio.get_running_loop().run_in_executor(
                    None, intranet_data.prava_pro_kontrolu, user_id)
            except Exception:
                return
            if vysledek is None or state.get('bezi_test') or not client.has_socket_connection:
                return  # výpadek DB / test mezitím začal → zkusí se znovu
            _stav_prav['verze'] = verze
            prava_raw, je_aktivni = vysledek
            nova = {p.lower() for p in prava_raw} & _KVIZ_PRAVA
            if je_aktivni and nova == _prava_kvizu:
                return  # změna se kvízu netýká
            ztrata_pristupu = not je_aktivni or not ({'vse', 'kviz'} & nova)
            if state.get('vysledek') and not ztrata_pristupu:
                return
            # Reload vyřeší vše: „NEMÁTE PŘÍSTUP", deaktivovaný účet → '/', nová tlačítka.
            ui.navigate.reload()

        ui.timer(5.0, kontrola_prav_kvizu)

    async def spustit_test():
        if state['nacitam'] or state['bezi_test']:
            return  # dvojklik
        if klic not in ROZPRACOVANE:
            cfg = config[kviz_typ]
            state['nacitam'] = True
            try:
                # Čtení CSV mimo event loop — 20 lidí naráz by jinak zdrželo všechny.
                otazky, chyba = await run.io_bound(nacti_otazky, kviz_typ, int(cfg['pocet_otazek']))
            except Exception as e:
                otazky, chyba = [], f'Otázky se nepodařilo načíst: {e}'
            finally:
                state['nacitam'] = False
            if not otazky and klic not in ROZPRACOVANE:
                ui.notify(chyba or 'Pro test nejsou k dispozici žádné otázky.', type='negative', position='top')
                ui.run_javascript('window.kvizDohled && window.kvizDohled.konec()')
                return
            if klic not in ROZPRACOVANE:  # během načítání ho nespustilo jiné okno
                ted = time.time()
                ROZPRACOVANE[klic] = {
                    'klic': klic, 'jmeno': state['jmeno'], 'user_id': user_id, 'typ': kviz_typ,
                    'host_krestni': host_krestni, 'host_prijmeni': host_prijmeni,
                    # Vazba na zadavatele odkazu; u interního uživatele zůstane None.
                    'pristup_token': host_token,
                    'otazky': otazky, 'aktualni_index': 0, 'body': 0,
                    'historie_odpovedi': [], 'vybrana_odpoved': None,
                    'zacatek_ts': ted, 'konec_ts': ted + int(cfg['casovy_limit_minuty']) * 60,
                    'min_procento': int(cfg['min_procento_uspesnosti']),
                }
                _uloz_zaznam(ROZPRACOVANE[klic])
                intranet_logger.log_activity(state['jmeno'], 'Kvíz', f"Test spuštěn ({kviz_typ.upper()}): {len(otazky)} otázek, limit {cfg['casovy_limit_minuty']} min", **zarizeni)
        # Nový test, nebo pokračování testu spuštěného v jiném okně.
        drz_relaci_testu(ROZPRACOVANE[klic])
        ROZPRACOVANE[klic]['_stranka'] = client.id  # dohled poslouchá jen tuto stránku
        state.update(bezi_test=True, vysledek=None)
        hlavni_rozhrani.refresh()

    async def dalsi_otazka(idx: int):
        zaznam = ROZPRACOVANE.get(klic)
        if zaznam is None or zaznam['aktualni_index'] != idx or _zbyva_s(zaznam) <= 0:
            # Dvojklik, odpověď z jiného okna, test už odevzdaný nebo po limitu
            # (odpověď po limitu se nepočítá) — srovná to časovač.
            return await tik_casovace()
        v = zaznam.get('vybrana_odpoved')
        if not v: return ui.notify('Vyberte odpověď!', type='warning', position='top')
        aktualni_q = zaznam['otazky'][idx]
        spravna = str(aktualni_q['správná odpověď']).strip().upper()
        if v == spravna: zaznam['body'] += 1; hodnoceni = "Správně"
        else: hodnoceni = "Špatně"

        zaznam['historie_odpovedi'].append({"Pořadí v testu": idx + 1, "Otázka": aktualni_q['OTÁZKA'], "Tvoje volba": f"{v}: {aktualni_q.get(v, '')}", "Hodnocení": hodnoceni, "Správná odpověď": "-" if v == spravna else f"{spravna}: {aktualni_q.get(spravna, '')}"})
        zaznam['vybrana_odpoved'], zaznam['aktualni_index'] = None, idx + 1

        if zaznam['aktualni_index'] < len(zaznam['otazky']):
            _uloz_zaznam(zaznam)  # každá odpověď hned na disk — přežije i restart serveru
            hlavni_rozhrani.refresh()
        else:
            await ukoncit_test()

    async def ukoncit_test(preruseno: bool = False):
        if not state['bezi_test']:
            return  # už se odevzdává (souběh tlačítka, časovače a dohledu)
        state['bezi_test'] = False
        uzavreno = _uzavri_test(klic, preruseno=preruseno, zarizeni=zarizeni)
        if uzavreno is None:
            # Mezitím odevzdalo jiné okno nebo úklid.
            state['vysledek'] = prevezmi_nevideny()
            hlavni_rozhrani.refresh()
            return
        zaznam, state['vysledek'] = uzavreno
        if not client.has_socket_connection:
            # Odevzdáno bez spojení — kdyby se stránka musela znovu načíst, ukáže se výsledek tam.
            NEVIDENE_VYSLEDKY[klic] = state['vysledek']
        hlavni_rozhrani.refresh()
        if not await _uloz_vysledek(zaznam, state['vysledek']):
            try:
                ui.notify('Výsledek se nepodařilo uložit do databáze. Nahlaste to prosím zadavateli testu.',
                          type='negative', position='top', timeout=0, close_button='OK')
            except Exception:
                pass  # stránka mezitím zanikla; chyba je v audit logu

    hlavni_rozhrani()


@refreshable_na_klienta
def vykresli_vystup_kviz(user_name, vsechna_prava):
    if "vse" not in vsechna_prava and "vystup_vse" not in vsechna_prava and "vystup_osobni" not in vsechna_prava:
        ui.label('Přístup odepřen').classes('text-red-500 text-xl')
        return

    muze_mazat = "vse" in vsechna_prava or "vystup_vse" in vsechna_prava

    with ui.row().classes('w-full justify-between items-center mb-8'):
        ui.label('Výsledky znalostního testu').classes('text-4xl font-extrabold text-gray-800')

        async def export_kviz_excel():
            ui.notify('Generuji Excel export, malý moment...', type='info', icon='hourglass_empty')
            def _export():
                conn = intranet_data.get_db_connection()
                if not conn: raise ValueError('Nelze se připojit k databázi.')
                cursor = conn.cursor(dictionary=True)
                if "vse" in vsechna_prava or "vystup_vse" in vsechna_prava:
                    cursor.execute("SELECT COALESCE(u.name, v.host_jmeno) AS Jméno, COALESCE(u.surname, v.host_prijmeni) AS Příjmení, v.stav_testu AS Stav, v.uspesnost AS Úspěšnost, v.body AS Body, v.doba_trvani AS Trvání, v.datum AS Odevzdáno FROM vysledky_kvizu v LEFT JOIN user u ON v.user_iduser = u.iduser ORDER BY v.datum DESC")
                else:
                    u_id = next((u['id'] for u in intranet_data.ziskej_vsechny_uzivatele().values() if f"{u['jmeno']} {u['prijmeni']}" == user_name), None)
                    cursor.execute("SELECT u.name AS Jméno, u.surname AS Příjmení, v.stav_testu AS Stav, v.uspesnost AS Úspěšnost, v.body AS Body, v.doba_trvani AS Trvání, v.datum AS Odevzdáno FROM vysledky_kvizu v JOIN user u ON v.user_iduser = u.iduser WHERE v.user_iduser = %s ORDER BY v.datum DESC", (u_id,))
                data = cursor.fetchall()
                cursor.close()
                conn.close()

                if not data: raise ValueError('Žádná data k exportu.')
                df = pd.DataFrame(data)
                if 'Odevzdáno' in df.columns: df['Odevzdáno'] = pd.to_datetime(df['Odevzdáno']).dt.tz_localize(None)
                os.makedirs("Exporty_Kviz", exist_ok=True)
                cesta = os.path.join("Exporty_Kviz", f"Export_Kviz_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
                with pd.ExcelWriter(cesta, engine='openpyxl') as writer:
                    df.to_excel(writer, sheet_name='Výsledky', index=False)
                return cesta

            try:
                cesta = await asyncio.to_thread(_export)
                ui.download(cesta)
                intranet_logger.log_activity(user_name, "Kvíz", "Stažen hromadný export výsledků do Excelu")
                ui.notify('Excel úspěšně stažen.', type='positive', position='top')
            except ValueError as ve: ui.notify(str(ve), type='warning', position='top')
            except Exception as e: ui.notify(f'Chyba při exportu: {e}', type='negative', position='top')

        ui.button('📥 Exportovat vše do Excelu', on_click=export_kviz_excel).classes('bg-green-600 hover:bg-green-700 text-white font-bold h-[3.5rem] px-6 shadow-md')

    conn = intranet_data.get_db_connection()
    if not conn:
        ui.label('Chyba databáze').classes('text-red-500 text-xl')
        return

    cursor = conn.cursor(dictionary=True)
    if "vse" in vsechna_prava or "vystup_vse" in vsechna_prava:
        cursor.execute("SELECT v.id, COALESCE(u.name, v.host_jmeno) AS name, COALESCE(u.surname, v.host_prijmeni) AS surname, v.stav_testu, v.uspesnost, v.body, v.doba_trvani, v.datum FROM vysledky_kvizu v LEFT JOIN user u ON v.user_iduser = u.iduser ORDER BY v.datum DESC")
    else:
        u_id = next((u['id'] for u in intranet_data.ziskej_vsechny_uzivatele().values() if f"{u['jmeno']} {u['prijmeni']}" == user_name), None)
        cursor.execute("SELECT v.id, u.name, u.surname, v.stav_testu, v.uspesnost, v.body, v.doba_trvani, v.datum FROM vysledky_kvizu v JOIN user u ON v.user_iduser = u.iduser WHERE v.user_iduser = %s ORDER BY v.datum DESC", (u_id,))

    vysledky = cursor.fetchall()
    cursor.close(); conn.close()

    if not vysledky:
        ui.label('Zatím nejsou k dispozici žádné výsledky.').classes('text-gray-500 italic text-xl')
        return

    with ui.card().classes('w-full p-8 shadow-md bg-white rounded-xl'):
        def ukaz_detail(vid):
            conn = intranet_data.get_db_connection()
            if not conn: return
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM zaznamy_odpovedi WHERE vysledek_id = %s ORDER BY poradi", (vid,))
            odpovedi = cursor.fetchall()
            cursor.close(); conn.close()

            with ui.dialog() as dlg, ui.card().classes('w-full max-w-4xl p-6 rounded-xl max-h-[80vh] overflow-y-auto'):
                ui.label('Detail odpovědí').classes('text-2xl font-bold mb-4')
                if not odpovedi: ui.label('Detail odpovědí není k dispozici.').classes('text-gray-500 italic')
                else:
                    for o in odpovedi:
                        barva = 'bg-green-50 border-green-200' if o['hodnoceni'] == 'Správně' else 'bg-red-50 border-red-200'
                        ikona = '✅' if o['hodnoceni'] == 'Správně' else '❌'
                        with ui.card().classes(f'w-full p-4 mb-2 shadow-sm border {barva}'):
                            ui.label(f"Otázka {o['poradi']}: {o['otazka']}").classes('font-bold text-lg mb-2')
                            ui.label(f"{ikona} Vaše volba: {o['tvoje_volba']}").classes('mb-1')
                            if o['hodnoceni'] != 'Správně': ui.label(f"👉 Správná odpověď: {o['spravna_odpoved']}").classes('font-bold text-green-700')
                ui.button('Zavřít', on_click=dlg.close).classes('mt-4 bg-gray-500 text-white font-bold w-full')
            dlg.open()

        def smazat_test(vid):
            with ui.dialog() as dlg, ui.card().classes('p-6 rounded-xl w-full max-w-sm'):
                ui.label('Smazání výsledku').classes('text-xl font-bold mb-4 text-red-600')
                ui.label('Opravdu chcete tento výsledek nenávratně smazat?').classes('mb-6 text-gray-700')
                def potvrdit():
                    conn = intranet_data.get_db_connection()
                    if conn:
                        try:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM zaznamy_odpovedi WHERE vysledek_id = %s", (vid,))
                            cursor.execute("DELETE FROM vysledky_kvizu WHERE id = %s", (vid,))
                            conn.commit(); cursor.close(); conn.close()
                            intranet_logger.log_activity(user_name, "Kvíz", f"Smazán výsledek testu (ID: {vid})")
                            ui.notify('Výsledek smazán.', type='positive', position='top')
                            dlg.close(); vykresli_vystup_kviz.refresh()
                        except Exception as e: ui.notify(f'Chyba při mazání: {e}', type='negative', position='top')
                with ui.row().classes('w-full justify-between'):
                    ui.button('Zrušit', on_click=dlg.close).classes('bg-gray-400 text-white font-bold')
                    ui.button('Smazat', on_click=potvrdit).classes('bg-red-600 text-white font-bold shadow-md')
            dlg.open()

        for v in vysledky:
            barva = 'border-green-500 bg-green-50' if v['stav_testu'] == 'Úspěšný' else 'border-red-500 bg-red-50'
            with ui.row().classes(f'w-full justify-between items-center p-4 mb-4 border-l-8 shadow-sm rounded {barva}'):
                with ui.column().classes('gap-1'):
                    ui.label(f"{v['name']} {v['surname']}").classes('font-bold text-xl text-gray-800')
                    ui.label(f"Odevzdáno: {v['datum'].strftime('%d.%m.%Y %H:%M:%S')}").classes('text-sm text-gray-600')
                with ui.column().classes('items-end gap-1'):
                    ui.label(f"{v['uspesnost']} ({v['body']})").classes('font-black text-2xl')
                    ui.label(f"Trvání: {v['doba_trvani']}").classes('text-sm text-gray-600')

                with ui.row().classes('gap-2'):
                    ui.button('Detail chyb', icon='search', on_click=lambda vid=v['id']: ukaz_detail(vid)).classes('bg-blue-500 hover:bg-blue-600 text-white shadow-md')
                    async def stahni_excel(vid=v['id'], v_data=v):
                        ui.notify('Generuji Excel export...', type='info')
                        def _zpracuj_excel():
                            conn = intranet_data.get_db_connection()
                            cursor = conn.cursor(dictionary=True)
                            cursor.execute("SELECT otazka AS `Otázka`, tvoje_volba AS `Tvoje volba`, spravna_odpoved AS `Správná odpověď` FROM zaznamy_odpovedi WHERE vysledek_id = %s ORDER BY poradi", (vid,))
                            odpovedi = cursor.fetchall()
                            cursor.close(); conn.close()
                            os.makedirs("Exporty_Kviz", exist_ok=True)
                            bezpecne_jmeno = f"{v_data['name']}_{v_data['surname']}".replace(" ", "_")
                            format_data = v_data['datum'].strftime('%Y%m%d_%H%M%S')
                            cesta = os.path.join("Exporty_Kviz", f"Vysledek_{bezpecne_jmeno}_{format_data}.xlsx")
                            df_s = pd.DataFrame([{"Jméno": f"{v_data['name']} {v_data['surname']}", "Stav": v_data['stav_testu'], "Úspěšnost": v_data['uspesnost'], "Bodů": v_data['body'], "Trvání": v_data['doba_trvani']}])
                            with pd.ExcelWriter(cesta, engine='openpyxl') as w:
                                df_s.to_excel(w, sheet_name='Souhrn', index=False)
                                # Souhrn: šířka sloupců podle nejdelšího textu (celé jméno), tisk A4 na šířku jako Analýza
                                ws_s = w.sheets['Souhrn']
                                for sloupec in ws_s.columns:
                                    ws_s.column_dimensions[sloupec[0].column_letter].width = max(len(str(c.value or '')) for c in sloupec) * 1.2 + 2
                                ws_s.page_setup.orientation = ws_s.ORIENTATION_LANDSCAPE
                                ws_s.page_setup.paperSize = ws_s.PAPERSIZE_A4
                                if odpovedi:
                                    pd.DataFrame(odpovedi).to_excel(w, sheet_name='Analýza', index=False)
                                    # Tisk: A4 na šířku, všechny sloupce na 1 stránku šířky, dlouhé texty se zalamují
                                    ws = w.sheets['Analýza']
                                    for sloupec, sirka in zip('ABC', (60, 35, 35)):
                                        ws.column_dimensions[sloupec].width = sirka
                                    zalom = Alignment(wrap_text=True, vertical='top')
                                    for radek in ws.iter_rows():
                                        for bunka in radek: bunka.alignment = zalom
                                    # Formát tabulky vč. hlavičky: pruhované sudé/liché řádky (drží i po řazení/filtru)
                                    tabulka = Table(displayName="Analyza", ref=ws.dimensions)
                                    tabulka.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
                                    ws.add_table(tabulka)
                                    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
                                    ws.page_setup.paperSize = ws.PAPERSIZE_A4
                                    ws.sheet_properties.pageSetUpPr.fitToPage = True
                                    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
                                    ws.print_title_rows = '1:1'
                                barva_fill = "FF00FF00" if v_data['stav_testu'] == 'Úspěšný' else "00E02319"
                                w.sheets['Souhrn']['B2'].fill = PatternFill(start_color=barva_fill, fill_type="solid")
                            return cesta
                        try:
                            cesta_k_souboru = await asyncio.to_thread(_zpracuj_excel)
                            ui.download(cesta_k_souboru)
                            intranet_logger.log_activity(user_name, "Kvíz", f"Stažen detailní export výsledku pro: {v_data['name']} {v_data['surname']}")
                        except Exception as e: ui.notify(f'Chyba při tvorbě Excelu: {e}', type='negative')
                    ui.button('Excel', icon='download', on_click=stahni_excel).classes('bg-green-600 hover:bg-green-700 text-white shadow-md')
                    if muze_mazat: ui.button('Smazat', icon='delete', color='red', on_click=lambda vid=v['id']: smazat_test(vid)).classes('shadow-md')