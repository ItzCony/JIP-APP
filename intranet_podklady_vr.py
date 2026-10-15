"""Podklady VŘ — dlaždice ve Formulářích ASM.

PDF podklady s platností od–do: náhled přímo v intranetu, tisk a stažení.
Archiv = ručně archivované dokumenty + ty, kterým skončila platnost.
Šablony opakovaně podepisovaných dokumentů: text se značkou {datum} → PDF
k tisku; podepsaný sken se pak nahraje jako nový podklad.

Práva: podklady_vr_ctenar (náhled, tisk, stažení), podklady_vr_spravce (vše).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import tempfile
import uuid
from types import SimpleNamespace

from nicegui import app, ui, background_tasks
from nicegui.storage import request_contextvar

import intranet_2fa
import intranet_data
import intranet_emaily
import intranet_logger
import intranet_oidc
import intranet_session
import intranet_static
from intranet_manualy import _ocisti_html

PRAVO_CTENAR = 'podklady_vr_ctenar'
PRAVO_SPRAVCE = 'podklady_vr_spravce'
_PRAVA_CTENI = ('vse', PRAVO_CTENAR, PRAVO_SPRAVCE)

ADRESAR = 'podklady_vr'
MAX_BYTU = 50 * 1024 * 1024          # skeny podepsaných dokumentů bývají velké
ZNACKA_DATA = '{datum}'
KONCI_DNI = 30                       # od kdy hlásit „Platnost N dní"

# Citlivé údaje → druhá brána: kód 2FA, nebo znovu přes Entra ID (M365).
BRANA = 'podklady_vr'


def brana_odemcena() -> bool:
    return intranet_session.brana_odemcena(BRANA)


def _log(user_name, zprava, udalost='Podklady VŘ', client=None):
    """Audit log vč. IP a zařízení — jde o citlivé dokumenty."""
    try:
        ip, device = intranet_logger.ziskej_klienta_info(client or ui.context.client)
    except Exception:
        ip = device = None
    intranet_logger.log_activity(user_name, udalost, zprava, ip=ip, device=device)


def _smi_soubory() -> bool:
    """Administrátor (právo `vse`) bránu nepotřebuje."""
    if brana_odemcena() or intranet_static.ma_pravo('vse'):
        return True
    # Práva má, ale bránu neodemkl — přeposlaný odkaz nebo vypršelé ověření.
    try:
        req = request_contextvar.get()
        klient = SimpleNamespace(request=req, ip=req.client.host if req and req.client else '')
        _log(app.storage.user.get('user_name', ''),
             'Odepřen přístup k souboru bez druhého ověření', client=klient)
    except Exception:
        pass
    return False


# Soubory jen pro držitele práv s odemčenou bránou — jména jsou sice náhodná,
# ale odkaz se dá přeposlat.
intranet_static.chranene_soubory('/' + ADRESAR, ADRESAR, pravo=_PRAVA_CTENI,
                                 kontrola=_smi_soubory)


def je_spravce(prava) -> bool:
    return 'vse' in prava or PRAVO_SPRAVCE in prava


def vidi(prava) -> bool:
    """Dlaždice patří pod Formuláře ASM → řídí ji i jejich přepínač modulu."""
    if not any(p in prava for p in _PRAVA_CTENI):
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
            CREATE TABLE IF NOT EXISTS podklady_vr (
                id INT AUTO_INCREMENT PRIMARY KEY,
                nazev VARCHAR(255) NOT NULL,
                poznamka TEXT,
                soubor_nazev VARCHAR(255) NOT NULL,
                soubor_cesta VARCHAR(500) NOT NULL,
                platnost_od DATE NOT NULL,
                platnost_do DATE NOT NULL,
                archivovano TINYINT(1) NOT NULL DEFAULT 0,
                nahral VARCHAR(255),
                nahrano DATETIME DEFAULT CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        # Upozornění na konec platnosti: komu (JSON seznam iduser), kolik dní předem,
        # a k jakému `platnost_do` už odešlo — prodloužení platnosti ho tím samo znovu nastaví.
        for _col, _typ in (('upozornit_komu', 'TEXT'),
                           ('upozornit_dni', 'INT'),
                           ('upozorneno_k', 'DATE')):
            cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='podklady_vr' AND "
                        "COLUMN_NAME=%s", (_col,))
            if cur.fetchone()[0] == 0:
                cur.execute(f"ALTER TABLE podklady_vr ADD COLUMN {_col} {_typ}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS podklady_vr_sablony (
                id INT AUTO_INCREMENT PRIMARY KEY,
                nazev VARCHAR(255) NOT NULL,
                obsah MEDIUMTEXT,
                upravil VARCHAR(255),
                upraveno DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()
        cur.close()
        _DB_HOTOVO = True
    except Exception as e:
        print(f'[podklady_vr] inicializace_db: {e}')
    finally:
        conn.close()


def _sql(dotaz, parametry=(), vysledek=None):
    """Jeden dotaz ve vlastním spojení. vysledek: 'vse' | 'jeden' | None (= commit, vrací lastrowid)."""
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
        return cur.lastrowid
    finally:
        conn.close()


def nacti_dokumenty() -> list:
    return _sql("SELECT * FROM podklady_vr ORDER BY nazev, platnost_do DESC", vysledek='vse')


def uloz_soubor(jmeno: str, obsah: bytes) -> tuple:
    """PDF pod náhodným jménem. Kontrola na serveru (`accept` v prohlížeči je jen nápověda).
    Vrací (původní_název, cesta)."""
    jmeno = os.path.basename(jmeno or '').strip() or 'podklad.pdf'
    if os.path.splitext(jmeno)[1].lower() != '.pdf' or b'%PDF' not in obsah[:1024]:
        raise ValueError('Nahrávat lze jen PDF.')
    if len(obsah) > MAX_BYTU:
        raise ValueError(f'Soubor je větší než {MAX_BYTU // (1024 * 1024)} MB.')
    os.makedirs(ADRESAR, exist_ok=True)
    cesta = os.path.join(ADRESAR, f'{uuid.uuid4().hex}.pdf')
    with open(cesta, 'wb') as f:
        f.write(obsah)
    return jmeno, cesta


def _smaz_soubor(cesta):
    try:
        if cesta and os.path.isfile(cesta):
            os.remove(cesta)
    except Exception as e:
        print(f'[podklady_vr] smazání {cesta}: {e}')


def prijemci_upozorneni(d) -> list:
    """iduser příjemců upozornění z řádku dokumentu ([] = vypnuto)."""
    try:
        return [int(x) for x in json.loads(d.get('upozornit_komu') or '[]')]
    except (TypeError, ValueError):
        return []


def _komu_json(komu) -> str | None:
    return json.dumps(sorted({int(x) for x in komu})) if komu else None


def pridej_dokument(nazev, poznamka, soubor_nazev, cesta, od, do, kdo,
                    komu=(), dni=None) -> int:
    return _sql("INSERT INTO podklady_vr (nazev, poznamka, soubor_nazev, soubor_cesta, "
                "platnost_od, platnost_do, nahral, upozornit_komu, upozornit_dni) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (nazev, poznamka, soubor_nazev, cesta, od, do, kdo,
                 _komu_json(komu), dni if komu else None))


def uprav_dokument(dok_id, nazev, poznamka, od, do, komu=(), dni=None):
    """Změna příjemců nebo předstihu upozornění ho znovu nastaví (i když už odešlo) —
    jinak by nově přidaný člověk upozornění nedostal. Samotná změna názvu ho nechá být."""
    komu_, dni_ = _komu_json(komu), (dni if komu else None)
    stary = _sql("SELECT upozornit_komu, upozornit_dni FROM podklady_vr WHERE id=%s",
                 (dok_id,), vysledek='jeden') or {}
    znovu = (stary.get('upozornit_komu'), stary.get('upozornit_dni')) != (komu_, dni_)
    _sql("UPDATE podklady_vr SET nazev=%s, poznamka=%s, platnost_od=%s, platnost_do=%s, "
         "upozornit_komu=%s, upozornit_dni=%s"
         + (", upozorneno_k=NULL" if znovu else "") + " WHERE id=%s",
         (nazev, poznamka, od, do, komu_, dni_, dok_id))


# ---------- upozornění na konec platnosti (automat na pozadí) ----------
def k_upozorneni() -> list:
    """Nearchivované podklady, kterým nastal termín upozornění a pro aktuální
    `platnost_do` ještě neodešlo. Po konci platnosti už se neposílá."""
    return _sql("SELECT * FROM podklady_vr WHERE archivovano=0 AND upozornit_dni IS NOT NULL "
                "AND upozornit_komu IS NOT NULL AND platnost_do >= CURDATE() "
                "AND DATE_SUB(platnost_do, INTERVAL upozornit_dni DAY) <= CURDATE() "
                "AND (upozorneno_k IS NULL OR upozorneno_k <> platnost_do)", vysledek='vse')


def _zamluv_upozorneni(dok_id) -> bool:
    """Atomicky označí upozornění za odeslané. True = tenhle proces ho má poslat
    (druhý běh procesu / restart tentýž řádek už nezabere)."""
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE podklady_vr SET upozorneno_k=platnost_do WHERE id=%s "
                    "AND (upozorneno_k IS NULL OR upozorneno_k <> platnost_do)", (dok_id,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _uvolni_upozorneni(dok_id):
    """Odeslání selhalo → zkusit znovu při dalším průchodu."""
    _sql("UPDATE podklady_vr SET upozorneno_k=NULL WHERE id=%s", (dok_id,))


def kandidati_upozorneni() -> dict:
    """{iduser: 'Jméno (e-mail)'} — aktivní uživatelé s přístupem k Podkladům VŘ
    (právo přímé, z role i z oddělení) a s vyplněným e-mailem."""
    s_pristupem = intranet_data.ziskej_uzivatele_s_pravem(*_PRAVA_CTENI)
    emaily = {u['id']: e for e, u in intranet_data.ziskej_vsechny_uzivatele().items()}
    return dict(sorted(((i, f'{jmeno} ({emaily[i]})') for i, jmeno in s_pristupem.items()
                        if emaily.get(i)), key=lambda kv: kv[1].lower()))


def _prijemci_emailu(ids) -> list:
    """[(e-mail, je_správce)] AKTIVNÍCH uživatelů, kteří k podkladům STÁLE mají přístup —
    kdo ho mezitím ztratil nebo byl deaktivován, se tiše vynechá (citlivé údaje)."""
    s_pristupem = set(intranet_data.ziskej_uzivatele_s_pravem(*_PRAVA_CTENI))
    ids = [i for i in ids if i in s_pristupem]
    if not ids:
        return []
    spravci = set(intranet_data.ziskej_uzivatele_s_pravem('vse', PRAVO_SPRAVCE))
    radky = _sql("SELECT iduser, email FROM user WHERE is_active=1 AND email IS NOT NULL "
                 f"AND email<>'' AND iduser IN ({','.join(['%s'] * len(ids))})",
                 tuple(ids), vysledek='vse')
    return [(r['email'], r['iduser'] in spravci) for r in radky]


def _text_upozorneni(d, spravce=False) -> tuple:
    zbyva = (d['platnost_do'] - datetime.date.today()).days
    kdy = 'dnes' if zbyva == 0 else f'za {zbyva} {"den" if zbyva == 1 else "dny" if zbyva < 5 else "dní"}'
    predmet = f'Končí platnost podkladu „{d["nazev"]}“ ({_dat(d["platnost_do"])})'
    radky = [f'Platnost podkladu „{d["nazev"]}“ končí {kdy} — {_dat(d["platnost_do"])}.', '']
    if (d.get('poznamka') or '').strip():
        radky += [f'Poznámka: {d["poznamka"].strip()}', '']
    radky.append('Podklad najdete v intranetu: Formuláře ASM → Podklady VŘ.')
    if intranet_data.APP_URL:
        radky.append(f'{intranet_data.APP_URL}/asm')
    if spravce:     # nahrávat smí jen správce — čtenáři by věta k ničemu nebyla
        radky += ['', 'Novou verzi nahrajete v menu ⋮ u podkladu → „Nahrát novou verzi“. '
                       'Název, poznámka i nastavení upozornění se převezmou.']
    return predmet, '\n'.join(radky)


def odesli_upozorneni() -> int:
    """Jeden průchod automatu. Vrací počet odeslaných upozornění."""
    inicializace_db()
    odeslano = 0
    for d in k_upozorneni():
        prijemci = _prijemci_emailu(prijemci_upozorneni(d))
        if not prijemci or not _zamluv_upozorneni(d['id']):
            continue
        ok = [e for e, spravce in prijemci
              if intranet_emaily.odesli_upozorneni_email(e, *_text_upozorneni(d, spravce))]
        if not ok:
            _uvolni_upozorneni(d['id'])
            continue
        odeslano += 1
        intranet_logger.log_activity('Systém', 'Podklady VŘ',
                                     f"Upozornění na konec platnosti „{d['nazev']}“ "
                                     f"({_dat(d['platnost_do'])}) → {', '.join(ok)}")
    return odeslano


async def bg_upozorneni():
    """Kontrola každých 30 min od 6:00 (e-maily ráno, ne o půlnoci). Odeslání hlídá
    `upozorneno_k` v DB, takže restart ani více průchodů za den nic nezdvojí."""
    await asyncio.sleep(120)
    while True:
        try:
            if datetime.datetime.now().hour >= 6:
                n = await asyncio.to_thread(odesli_upozorneni)
                if n:
                    print(f'[podklady_vr] odesláno upozornění: {n}')
        except Exception as e:
            print(f'[podklady_vr] bg_upozorneni: {e}')
        await asyncio.sleep(1800)


def nastav_archiv(dok_id, archivovano: bool):
    _sql("UPDATE podklady_vr SET archivovano=%s WHERE id=%s", (int(archivovano), dok_id))


def smaz_dokument(dok_id):
    d = _sql("SELECT soubor_cesta FROM podklady_vr WHERE id=%s", (dok_id,), vysledek='jeden')
    _sql("DELETE FROM podklady_vr WHERE id=%s", (dok_id,))
    if d:
        _smaz_soubor(d['soubor_cesta'])


def nacti_sablony() -> list:
    return _sql("SELECT * FROM podklady_vr_sablony ORDER BY nazev", vysledek='vse')


def uloz_sablonu(sab_id, nazev, obsah, kdo):
    obsah = _ocisti_html(obsah)
    if sab_id:
        _sql("UPDATE podklady_vr_sablony SET nazev=%s, obsah=%s, upravil=%s WHERE id=%s",
             (nazev, obsah, kdo, sab_id))
    else:
        _sql("INSERT INTO podklady_vr_sablony (nazev, obsah, upravil) VALUES (%s,%s,%s)",
             (nazev, obsah, kdo))


def smaz_sablonu(sab_id):
    _sql("DELETE FROM podklady_vr_sablony WHERE id=%s", (sab_id,))


def v_archivu(d, dnes=None) -> bool:
    return bool(d['archivovano']) or d['platnost_do'] < (dnes or datetime.date.today())


# =========================================================
# ŠABLONA → PDF
# =========================================================
_PDF_ZAMEK = None


async def sablona_na_pdf(obsah_html: str, datum: datetime.date) -> bytes:
    """HTML šablony s dosazeným datem → PDF A4 (Playwright/Chromium)."""
    global _PDF_ZAMEK
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError('Na serveru chybí Playwright (pip install playwright; '
                           f'playwright install chromium). Detail: {e}')
    telo = (obsah_html or '').replace(ZNACKA_DATA, datum.strftime('%d.%m.%Y'))
    stranka = ('<!doctype html><html><head><meta charset="utf-8"><style>'
               'body{font-family:Arial,sans-serif;font-size:11pt;line-height:1.45;color:#111}'
               'table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px 6px}'
               'img{max-width:100%}'
               '</style></head><body>' + telo + '</body></html>')
    if _PDF_ZAMEK is None:
        _PDF_ZAMEK = asyncio.Semaphore(1)      # jedno Chromium naráz (paměť serveru)
    async with _PDF_ZAMEK:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception as e:
                raise RuntimeError(f'Nepodařilo se spustit Chromium ({e}). '
                                   'Spusťte na serveru: playwright install chromium')
            try:
                page = await browser.new_page(java_script_enabled=False)
                # Šablonu píše člověk — ze serveru nikam nesahá (obrázky jen vložené data:).
                await page.route('**/*', lambda r: r.abort())
                await page.set_content(stranka, wait_until='load')
                return await page.pdf(format='A4', print_background=True,
                                      margin={'top': '20mm', 'right': '20mm',
                                              'bottom': '20mm', 'left': '20mm'})
            finally:
                await browser.close()


async def _smaz_pozdeji(cesta, za_s=300):
    await asyncio.sleep(za_s)
    _smaz_soubor(cesta)


def _stahni_pdf(obsah: bytes, jmeno: str):
    """Přes HTTP, ne websocket (ui.download.content padá nad ~1 MB na limitu engine.io)."""
    fd, cesta = tempfile.mkstemp(suffix='.pdf')
    with os.fdopen(fd, 'wb') as f:
        f.write(obsah)
    ui.download.file(cesta, jmeno)
    background_tasks.create(_smaz_pozdeji(cesta), name='podklady-vr-tmp')


# =========================================================
# UI
# =========================================================
def _dat(d) -> str:
    return d.strftime('%d.%m.%Y') if d else ''


def _parsuj_datum(text):
    try:
        return datetime.datetime.strptime((text or '').strip(), '%d.%m.%Y').date()
    except ValueError:
        return None


def _dni(n: int) -> str:
    """1 den, 2–4 dny, 0/5+ dní."""
    return f"{n} {'den' if n == 1 else 'dny' if 2 <= n <= 4 else 'dní'}"


def _stav(d, dnes) -> tuple:
    """(text, tailwind pozadí) štítku platnosti — vzhled jako stav faktury v Aprovii."""
    if d['archivovano']:
        return 'Archivováno', 'bg-gray-400'
    if d['platnost_do'] < dnes:
        return f"Skončila ({_dni((dnes - d['platnost_do']).days)})", 'bg-red-500'
    if d['platnost_od'] > dnes:
        return f"Platí od {_dat(d['platnost_od'])}", 'bg-blue-500'
    zbyva = (d['platnost_do'] - dnes).days
    if zbyva <= KONCI_DNI:
        return (f'Platnost {_dni(zbyva)}' if zbyva else 'Končí dnes'), 'bg-orange-500'
    return 'Platný', 'bg-green-600'


def _stitek(d, dnes):
    text, bg = _stav(d, dnes)
    ui.label(text).classes(f'text-white text-[10px] px-2 py-0.5 rounded font-bold uppercase '
                           f'tracking-wider whitespace-nowrap {bg}')


def _zavri_a_smaz(dlg):
    """Dialog po zavření zahodit — jinak se v klientovi hromadí."""
    dlg.on_value_change(lambda e: None if e.value else dlg.delete())


# =========================================================
# DRUHÁ BRÁNA
# =========================================================
def _po_odemceni():
    # import tady: intranet_asm importuje tento modul
    from intranet_asm import _refresh
    _refresh()


async def _vykresli_branu(user_id, user_name):
    chyba = app.storage.user.pop('brana_chyba', '')
    if chyba:
        ui.timer(0.1, lambda: ui.notify(chyba, type='negative', position='top', timeout=8000), once=True)

    ma_2fa = await asyncio.to_thread(intranet_2fa.ma_aktivni_2fa, user_id)
    m365 = intranet_oidc.je_zapnuto()

    with ui.column().classes('w-full items-center py-10'):
        with ui.card().classes('w-full max-w-md p-8 items-center rounded-2xl'):
            ui.icon('lock', size='3rem', color='teal-8').classes('mb-2')
            ui.label('Podklady VŘ — druhé ověření').classes('text-xl font-bold text-gray-800 text-center mb-4')

            if ma_2fa:
                kod = ui.input('Kód z ověřovací aplikace nebo záložní kód') \
                    .props('outlined autofocus autocomplete=one-time-code').classes('w-full')

                async def _over():
                    ok, zprava = await asyncio.to_thread(intranet_2fa.overit_2fa_kod, user_id, kod.value)
                    if ok:
                        intranet_session.odemkni_branu(BRANA)
                        _log(user_name, f'Odemčeno kódem 2FA ({BRANA})', 'Druhé ověření')
                        if zprava:
                            ui.notify(zprava, type='warning', position='top', timeout=8000)
                        _po_odemceni()
                        return
                    kod.value = ''
                    if zprava and str(zprava).startswith('ZAMCEN:'):
                        try:
                            zb = int(str(zprava).split(':', 1)[1])
                        except ValueError:
                            zb = 60
                        zprava = f'Příliš mnoho chybných kódů. Zkuste to za {max(1, (zb + 59) // 60)} min.'
                    _log(user_name, f'Neplatný kód 2FA ({BRANA})', 'Druhé ověření')
                    ui.notify(zprava or 'Neplatný ověřovací kód.', type='negative', position='top')

                kod.on('keydown.enter', _over)
                ui.button('Ověřit', icon='verified_user', on_click=_over) \
                    .props('unelevated no-caps color=teal-8').classes('w-full mt-2')

            if m365:
                if ma_2fa:
                    with ui.row().classes('w-full items-center gap-2 my-3'):
                        ui.separator().classes('flex-1')
                        ui.label('nebo').classes('text-xs text-gray-400')
                        ui.separator().classes('flex-1')
                ui.button('Ověřit přes Microsoft 365', icon='business',
                          on_click=lambda: ui.navigate.to(f'/auth/login?brana={BRANA}')) \
                    .props('outline no-caps color=teal-8').classes('w-full')

            if not ma_2fa:
                def _do_nastaveni():
                    app.storage.user['intranet_tab'] = 'nastaveni'
                    ui.navigate.to('/')

                ui.label('Pokud se nepřihlašujete firemním účtem, zapněte si dvoufaktorové '
                         'ověření (2FA) v Osobním nastavení účtu.' if m365 else
                         'Přístup vyžaduje dvoufaktorové ověření (2FA). Zapněte si ho '
                         'v Osobním nastavení účtu.') \
                    .classes('text-sm text-gray-600 text-center mt-4')
                ui.button('Osobní nastavení', icon='settings', on_click=_do_nastaveni) \
                    .props('flat no-caps color=grey-8').classes('mt-1')

            ui.label(f'Ověření platí {intranet_session.BRANA_PLATNOST_S // 60} minut '
                     'nebo do odhlášení.').classes('text-xs text-gray-400 mt-4')


async def vykresli_podklady(user_id, user_name, vsechna_prava):
    # import tady: intranet_asm importuje tento modul
    from intranet_asm import _date_input

    if 'vse' not in vsechna_prava:          # administrátor bránu nepotřebuje
        if not brana_odemcena():
            await _vykresli_branu(user_id, user_name)
            return
        # Ověření vypršelo při otevřené stránce → zpět na bránu (soubory už stejně nepůjdou).
        ui.timer(30, lambda: None if brana_odemcena() else _po_odemceni())

    await asyncio.to_thread(inicializace_db)
    spravce = je_spravce(vsechna_prava)
    stav = {'pohled': 'platne', 'hledat': '', 'vybrany': None, 'zobrazeny': None}

    def _vyber(d):
        stav['vybrany'] = d['id']
        _seznam.refresh()

    # ---------- nahrání / úprava ----------
    async def _dialog_dokument(d=None, nazev='', predloha=None):
        """d = úprava existujícího; predloha = nová verze (nové PDF, přebírá název,
        poznámku a upozornění z původního podkladu); jinak čisté nahrání."""
        uprava = d is not None
        zdroj = d or predloha       # odkud předvyplnit
        nahrany = {}            # {'jmeno', 'cesta'} — PDF uložené, ale ještě nezapsané v DB
        dnes = datetime.date.today()
        kandidati = await asyncio.to_thread(kandidati_upozorneni)
        with ui.dialog() as dlg, ui.card().classes('w-[34rem] max-w-full gap-3'):
            ui.label('Upravit podklad' if uprava else
                     'Nahrát novou verzi' if predloha else 'Nahrát podklad') \
                .classes('text-lg font-bold text-gray-800')
            f_nazev = ui.input('Název', value=zdroj['nazev'] if zdroj else nazev) \
                .props('outlined dense').classes('w-full')
            f_pozn = ui.textarea('Poznámka', value=(zdroj.get('poznamka') or '') if zdroj else '') \
                .props('outlined dense autogrow').classes('w-full')
            with ui.row().classes('w-full no-wrap gap-3'):
                with ui.column().classes('flex-1'):
                    f_od = _date_input('Platnost od',
                                       _dat(d['platnost_od']) if uprava else _dat(dnes))
                with ui.column().classes('flex-1'):
                    f_do = _date_input('Platnost do', _dat(d['platnost_do']) if uprava else '')
            if not uprava:
                soubor_lbl = ui.label('Zatím nevybráno žádné PDF.') \
                    .classes('text-sm text-gray-500')

                async def _nahraj(e):
                    try:
                        jmeno, cesta = await asyncio.to_thread(
                            uloz_soubor, e.file.name, await e.file.read())
                    except ValueError as ex:
                        ui.notify(str(ex), type='negative')
                        upl.reset()
                        return
                    except Exception as ex:
                        ui.notify(f'Soubor se nepodařilo uložit: {ex}', type='negative')
                        upl.reset()
                        return
                    _smaz_soubor(nahrany.get('cesta'))      # vybral jiné PDF
                    nahrany.update(jmeno=jmeno, cesta=cesta)
                    soubor_lbl.set_text(f'📄 {jmeno}')
                    soubor_lbl.classes(replace='text-sm text-emerald-700 font-medium')
                    if not (f_nazev.value or '').strip():
                        f_nazev.value = os.path.splitext(jmeno)[0]
                    upl.reset()

                upl = ui.upload(label=f'Vybrat PDF (max {MAX_BYTU // (1024 * 1024)} MB)',
                                on_upload=_nahraj, auto_upload=True, max_file_size=MAX_BYTU) \
                    .props('accept=".pdf" flat dense color=blue-grey').classes('w-full')

            # Upozornění e-mailem před koncem platnosti (jen lidé s přístupem k podkladům).
            ui.label('Upozornění na konec platnosti').classes('text-sm font-bold text-gray-700 mt-1')
            with ui.row().classes('w-full no-wrap gap-3 items-start'):
                f_komu = ui.select(kandidati, multiple=True, with_input=True, label='Komu poslat e-mail',
                                   value=[i for i in (prijemci_upozorneni(zdroj) if zdroj else [])
                                          if i in kandidati]) \
                    .props('use-chips outlined dense').classes('flex-1')
                f_dni = ui.number('Dní předem', value=(zdroj.get('upozornit_dni') if zdroj else None) or 30,
                                  min=1, max=365, precision=0) \
                    .props('outlined dense').classes('w-28')
            # Původní verze už je nahrazená → z „Platné“ pryč (v Archivu zůstane).
            f_arch = ui.checkbox('Archivovat původní verzi', value=True) \
                if predloha and not predloha.get('archivovano') else None
            chyba = ui.label('').classes('text-sm text-red-600 font-medium')

            async def _uloz():
                nazev_ = (f_nazev.value or '').strip()
                od, do = _parsuj_datum(f_od.value), _parsuj_datum(f_do.value)
                if not nazev_ or not od or not do:
                    chyba.set_text('Vyplňte název a obě data platnosti (DD.MM.RRRR).')
                    return
                if do < od:
                    chyba.set_text('„Platnost do" je dřív než „Platnost od".')
                    return
                if not uprava and not nahrany:
                    chyba.set_text('Vyberte PDF soubor.')
                    return
                komu = list(f_komu.value or [])
                dni = f_dni.value
                if komu and (dni is None or not 1 <= int(dni) <= 365):
                    chyba.set_text('Počet dní předem musí být 1–365.')
                    return
                dni = int(dni) if komu else None
                pozn = (f_pozn.value or '').strip()
                try:
                    if uprava:
                        await asyncio.to_thread(uprav_dokument, d['id'], nazev_, pozn, od, do,
                                                komu, dni)
                    else:
                        await asyncio.to_thread(pridej_dokument, nazev_, pozn, nahrany['jmeno'],
                                                nahrany['cesta'], od, do, user_name, komu, dni)
                        nahrany.clear()             # už patří záznamu, po zavření nemazat
                except Exception as ex:
                    ui.notify(f'Uložení se nezdařilo: {ex}', type='negative')
                    _log(user_name, f"Chyba při ukládání podkladu „{nazev_}“: {ex}")
                    return
                archivovano = False
                if f_arch is not None and f_arch.value:
                    try:
                        await asyncio.to_thread(nastav_archiv, predloha['id'], True)
                        archivovano = True
                    except Exception as ex:
                        ui.notify(f'Nová verze je uložená, ale původní se nepodařilo '
                                  f'archivovat: {ex}', type='warning')
                akce = ('Upraven podklad' if uprava else
                        'Nahrána nová verze podkladu' if predloha else 'Nahrán podklad')
                _log(user_name, f"{akce} „{nazev_}“ ({_dat(od)} – {_dat(do)})"
                                + (f", upozornění {dni} dní předem → "
                                   f"{', '.join(kandidati[i].split(' (')[0] for i in komu)}"
                                   if komu else '')
                                + (f"; původní verze ({_dat(predloha['platnost_do'])}) archivována"
                                   if archivovano else ''))
                ui.notify('Uloženo.', type='positive')
                dlg.close()
                _seznam.refresh()

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                ui.button('Uložit', icon='save', on_click=_uloz) \
                    .props('unelevated no-caps color=teal-8')

        def _po_zavreni(e):
            if not e.value:
                _smaz_soubor(nahrany.get('cesta'))      # nahrál, ale neuložil
                dlg.delete()
        dlg.on_value_change(_po_zavreni)
        dlg.open()

    async def _archivuj(d, archivovat: bool):
        try:
            await asyncio.to_thread(nastav_archiv, d['id'], archivovat)
        except Exception as ex:
            ui.notify(f'Nezdařilo se: {ex}', type='negative')
            _log(user_name, f"Chyba při archivaci podkladu „{d['nazev']}“: {ex}")
            return
        _log(user_name, f"{'Archivován' if archivovat else 'Obnoven z archivu'} "
                        f"podklad „{d['nazev']}“")
        _seznam.refresh()

    def _potvrd(text, akce):
        with ui.dialog() as dlg, ui.card().classes('gap-4'):
            ui.label(text).classes('text-base')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')

                async def _ano():
                    dlg.close()
                    await akce()
                ui.button('Smazat', icon='delete', on_click=_ano) \
                    .props('unelevated no-caps color=red-7')
        _zavri_a_smaz(dlg)
        dlg.open()

    def _smazat(d):
        async def _akce():
            try:
                await asyncio.to_thread(smaz_dokument, d['id'])
            except Exception as ex:
                ui.notify(f'Smazání se nezdařilo: {ex}', type='negative')
                _log(user_name, f"Chyba při mazání podkladu „{d['nazev']}“: {ex}")
                return
            _log(user_name, f"Smazán podklad „{d['nazev']}“ ({d['soubor_nazev']})")
            stav['vybrany'] = None
            _seznam.refresh()
        _potvrd(f"Opravdu trvale smazat „{d['nazev']}“ i s PDF? "
                "Pro zachování historie raději archivujte.", _akce)

    # ---------- šablony ----------
    def _dialog_sablona(s=None):
        with ui.dialog().props('maximized') as dlg, \
                ui.card().classes('w-full h-full flex flex-col no-wrap gap-3 p-4'):
            with ui.row().classes('w-full items-center gap-2'):
                ui.icon('edit_document').classes('text-emerald-700 text-2xl')
                ui.label('Upravit šablonu' if s else 'Nová šablona').classes('text-lg font-bold')
                ui.space()
                ui.button(icon='close', on_click=dlg.close).props('flat round')
            f_nazev = ui.input('Název šablony', value=s['nazev'] if s else '') \
                .props('outlined dense').classes('w-full')
            ui.label(f'Na místo data, které se pokaždé mění, napište {ZNACKA_DATA} — při '
                     'vytvoření PDF se nahradí zvoleným datem. Text lze vložit i z Wordu.') \
                .classes('text-sm text-gray-500')
            editor = ui.editor(value=(s.get('obsah') or '') if s else
                               f'<p></p><p>V Praze dne {ZNACKA_DATA}</p>') \
                .props(':toolbar="[[\'bold\',\'italic\',\'underline\',\'strike\'],'
                       '[\'left\',\'center\',\'right\',\'justify\'],'
                       '[\'unordered\',\'ordered\',\'hr\'],[\'removeFormat\'],[\'undo\',\'redo\']]"') \
                .classes('w-full flex-1 overflow-auto')

            async def _uloz():
                nazev_ = (f_nazev.value or '').strip()
                if not nazev_:
                    ui.notify('Vyplňte název šablony.', type='warning')
                    return
                try:
                    await asyncio.to_thread(uloz_sablonu, s['id'] if s else None,
                                            nazev_, editor.value, user_name)
                except Exception as ex:
                    ui.notify(f'Uložení se nezdařilo: {ex}', type='negative')
                    _log(user_name, f"Chyba při ukládání šablony „{nazev_}“: {ex}")
                    return
                _log(user_name, f"{'Upravena' if s else 'Založena'} šablona „{nazev_}“")
                dlg.close()
                _seznam.refresh()

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                ui.button('Uložit šablonu', icon='save', on_click=_uloz) \
                    .props('unelevated no-caps color=teal-8')
        _zavri_a_smaz(dlg)
        dlg.open()

    def _dialog_pdf(s):
        with ui.dialog() as dlg, ui.card().classes('w-96 gap-3'):
            ui.label(f"PDF ze šablony „{s['nazev']}“").classes('text-lg font-bold')
            f_dat = _date_input('Datum v dokumentu', _dat(datetime.date.today()))

            async def _vytvor():
                datum = _parsuj_datum(f_dat.value)
                if not datum:
                    ui.notify('Zadejte datum ve tvaru DD.MM.RRRR.', type='warning')
                    return
                tl.props('loading')
                try:
                    pdf = await sablona_na_pdf(s.get('obsah'), datum)
                except Exception as ex:
                    ui.notify(f'PDF se nepodařilo vytvořit: {ex}', type='negative', multi_line=True)
                    _log(user_name, f"Chyba při tvorbě PDF ze šablony „{s['nazev']}“: {ex}")
                    return
                finally:
                    tl.props(remove='loading')
                jmeno = f"{s['nazev']} {_dat(datum)}.pdf".replace('/', '-').replace('\\', '-')
                _stahni_pdf(pdf, jmeno)
                _log(user_name, f"Staženo PDF ze šablony „{s['nazev']}“ k {_dat(datum)}")
                dlg.close()

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Zrušit', on_click=dlg.close).props('flat no-caps')
                tl = ui.button('Vytvořit PDF', icon='picture_as_pdf', on_click=_vytvor) \
                    .props('unelevated no-caps color=teal-8')
        _zavri_a_smaz(dlg)
        dlg.open()

    def _smazat_sablonu(s):
        async def _akce():
            try:
                await asyncio.to_thread(smaz_sablonu, s['id'])
            except Exception as ex:
                ui.notify(f'Smazání se nezdařilo: {ex}', type='negative')
                _log(user_name, f"Chyba při mazání šablony „{s['nazev']}“: {ex}")
                return
            _log(user_name, f"Smazána šablona „{s['nazev']}“")
            _seznam.refresh()
        _potvrd(f"Opravdu smazat šablonu „{s['nazev']}“?", _akce)

    # ---------- seznamy ----------
    def _hleda(*texty) -> bool:
        h = stav['hledat'].strip().lower()
        return not h or any(h in (t or '').lower() for t in texty)

    def _vykresli_sablony(sablony):
        with ui.row().classes('w-full items-center mb-2'):
            ui.label('Opakovaně podepisované dokumenty: vytvořte PDF k datu, vytiskněte, '
                     'nechte podepsat a sken nahrajte jako nový podklad.') \
                .classes('text-sm text-gray-500')
            ui.space()
            ui.button('Nová šablona', icon='add', on_click=lambda: _dialog_sablona()) \
                .props('outline no-caps color=teal-8')
        sablony = [s for s in sablony if _hleda(s['nazev'])]
        if not sablony:
            ui.label('Žádné šablony.').classes('text-gray-400 py-10 w-full text-center')
            return
        for s in sablony:
            with ui.card().classes('w-full px-4 py-3'):
                with ui.row().classes('w-full items-center gap-3 no-wrap'):
                    ui.icon('description', size='1.8rem').classes('text-emerald-600')
                    with ui.column().classes('gap-0 flex-1 min-w-0'):
                        ui.label(s['nazev']).classes('font-semibold text-gray-800 truncate')
                        ui.label(f"Upraveno {s['upraveno']:%d.%m.%Y %H:%M} · {s.get('upravil') or ''}"
                                 if s.get('upraveno') else '').classes('text-xs text-gray-500')
                    ui.button('Vytvořit PDF', icon='picture_as_pdf',
                              on_click=lambda s=s: _dialog_pdf(s)) \
                        .props('unelevated no-caps color=teal-8')
                    ui.button('Nahrát podepsaný', icon='upload_file',
                              on_click=lambda s=s: _dialog_dokument(nazev=s['nazev'])) \
                        .props('outline no-caps color=grey-8')
                    ui.button(icon='edit', on_click=lambda s=s: _dialog_sablona(s)) \
                        .props('flat round color=grey-7').tooltip('Upravit šablonu')
                    ui.button(icon='delete', on_click=lambda s=s: _smazat_sablonu(s)) \
                        .props('flat round color=red-6').tooltip('Smazat šablonu')

    def _karta(d, vybrana: bool, dnes):
        """Karta vlevo — stejná jako faktura v Aprovii (klik = náhled vpravo)."""
        bgk = ('bg-blue-50 border-blue-300 ring-1 ring-blue-500' if vybrana
               else 'bg-white border-gray-100 hover:bg-gray-50')
        with ui.card().classes(f'w-full p-4 shadow-none border rounded-xl cursor-pointer '
                               f'transition-colors {bgk}').on('click', lambda d=d: _vyber(d)):
            with ui.row().classes('w-full justify-between items-start no-wrap mb-1'):
                with ui.column().classes('gap-0 flex-1 overflow-hidden pr-2'):
                    ui.label(d['nazev']).classes('font-bold text-gray-800 truncate w-full')
                    ui.label(d.get('poznamka') or d['soubor_nazev']) \
                        .classes('text-xs text-gray-500 truncate w-full font-bold')
            with ui.row().classes('w-full justify-end items-center no-wrap mt-3'):
                _stitek(d, dnes)

    def _panel_nahledu(d):
        """Pravá strana — hlavička s platností a akcemi, pod ní PDF přes celou výšku."""
        url = f"/{ADRESAR}/{os.path.basename(d['soubor_cesta'])}"
        existuje = os.path.isfile(d['soubor_cesta'])

        with ui.column().classes('w-full h-full bg-white rounded-2xl shadow-sm border border-gray-100 '
                                 'overflow-hidden flex flex-col no-wrap gap-0'):
            with ui.row().classes('w-full bg-white p-6 border-b border-gray-100 justify-between '
                                  'items-center no-wrap gap-4 shrink-0'):
                with ui.row().classes('items-center gap-4 no-wrap min-w-0'):
                    ui.icon('description', size='2rem', color='blue-600').classes('shrink-0')
                    with ui.column().classes('gap-0 min-w-0'):
                        ui.label(d['nazev']).classes('text-2xl font-black text-gray-900 truncate w-full')
                        if d.get('nahral'):
                            ui.label(f"Nahrál: {d['nahral']}") \
                                .classes('text-sm font-bold text-gray-500 truncate w-full')
                with ui.row().classes('items-center gap-4 no-wrap shrink-0'):
                    with ui.column().classes('items-end gap-1'):
                        ui.label('Platnost').classes('text-xs text-gray-500 font-bold uppercase')
                        ui.label(f"{_dat(d['platnost_od'])} – {_dat(d['platnost_do'])}") \
                            .classes('text-2xl font-black text-blue-600 whitespace-nowrap')
                    # Tisk a stažení nabízí lišta PDF prohlížeče pod hlavičkou.
                    if spravce:
                        with ui.row().classes('items-center gap-1 no-wrap'):
                            with ui.button(icon='more_vert').props('flat round') \
                                    .classes('text-gray-500').tooltip('Správa'):
                                with ui.menu():
                                    ui.menu_item('Upravit', on_click=lambda: _dialog_dokument(d))
                                    ui.menu_item('Nahrát novou verzi',
                                                 on_click=lambda: _dialog_dokument(predloha=d))
                                    if d['archivovano']:
                                        ui.menu_item('Obnovit z archivu',
                                                     on_click=lambda: _archivuj(d, False))
                                    else:
                                        ui.menu_item('Archivovat', on_click=lambda: _archivuj(d, True))
                                    ui.menu_item('Smazat', on_click=lambda: _smazat(d)) \
                                        .classes('text-red-600')
            with ui.column().classes('flex-1 p-0 w-full overflow-hidden bg-gray-200'):
                if existuje:
                    # navpanes=0 — postranní panel s miniaturami stránek je ve výchozím stavu skrytý.
                    ui.element('iframe') \
                        .props(f'src="{url}#navpanes=0&view=FitH" type="application/pdf" '
                               f'title="Náhled dokumentu"') \
                        .classes('w-full h-full border-0')
                else:
                    with ui.column().classes('w-full h-full items-center justify-center'):
                        ui.icon('error_outline', size='3rem', color='red-300')
                        ui.label('Soubor na serveru chybí.').classes('text-lg font-bold text-gray-500')

    def _vykresli_dokumenty(dokumenty, archiv: bool):
        dnes = datetime.date.today()
        vyber = [d for d in dokumenty if v_archivu(d, dnes) == archiv
                 and _hleda(d['nazev'], d.get('poznamka'), d['soubor_nazev'])]
        if archiv:
            vyber.sort(key=lambda d: d['platnost_do'], reverse=True)
        vybrany = next((d for d in vyber if d['id'] == stav['vybrany']), None)
        # Zobrazení logujeme při změně dokumentu, ne při každém překreslení (hledání, úpravy).
        if (vybrany or {}).get('id') != stav['zobrazeny']:
            stav['zobrazeny'] = vybrany and vybrany['id']
            if vybrany:
                _log(user_name, f"Zobrazen podklad „{vybrany['nazev']}“ ({vybrany['soubor_nazev']})")

        with ui.row().classes('w-full no-wrap gap-6 items-stretch mb-8') \
                .style('height: 85vh; min-height: 600px;'):
            # LEVÁ STRANA — seznam
            with ui.column().classes('w-[420px] max-w-[420px] shrink-0 h-full bg-white rounded-2xl '
                                     'shadow-sm border border-gray-100 overflow-hidden flex flex-col '
                                     'no-wrap gap-0'):
                ui.label(f"{'Archiv' if archiv else 'Platné podklady'} ({len(vyber)})") \
                    .classes('font-bold text-gray-800 text-sm uppercase p-4 border-b bg-gray-50 '
                             'w-full shrink-0')
                with ui.column().classes('w-full flex-1 overflow-y-auto p-3 gap-3 no-wrap'):
                    if not vyber:
                        ui.label('Archiv je prázdný.' if archiv else 'Žádné platné podklady.') \
                            .classes('text-gray-400 italic text-center w-full py-8')
                    for d in vyber:
                        _karta(d, d is vybrany, dnes)
            # PRAVÁ STRANA — náhled PDF
            with ui.column().classes('flex-1 min-w-0 h-full p-0 m-0 overflow-hidden no-wrap'):
                if vybrany:
                    _panel_nahledu(vybrany)
                else:
                    with ui.column().classes('w-full h-full items-center justify-center bg-white '
                                             'rounded-2xl border'):
                        ui.icon('description', size='4rem', color='gray-300').classes('mb-4')
                        ui.label('Vyberte podklad vlevo.').classes('text-xl font-bold text-gray-400')

    @ui.refreshable
    async def _seznam():
        try:
            if stav['pohled'] == 'sablony':
                _vykresli_sablony(await asyncio.to_thread(nacti_sablony))
            else:
                _vykresli_dokumenty(await asyncio.to_thread(nacti_dokumenty),
                                    archiv=stav['pohled'] == 'archiv')
        except Exception as ex:
            ui.label(f'Podklady se nepodařilo načíst: {ex}').classes('text-red-600')

    def _prepni(hodnota):
        stav['pohled'] = hodnota
        _seznam.refresh()

    def _hledej(text):
        stav['hledat'] = text or ''
        _seznam.refresh()

    with ui.row().classes('w-full items-center gap-3 mb-4'):
        moznosti = {'platne': 'Platné', 'archiv': 'Archiv'}
        if spravce:
            moznosti['sablony'] = 'Šablony'
        ui.toggle(moznosti, value='platne', on_change=lambda e: _prepni(e.value)) \
            .props('no-caps unelevated toggle-color=teal-8')
        ui.input(placeholder='Hledat…', on_change=lambda e: _hledej(e.value)) \
            .props('dense outlined clearable debounce=300').classes('w-64')
        ui.space()
        if spravce:
            ui.button('Nahrát podklad', icon='upload_file', on_click=lambda: _dialog_dokument()) \
                .props('unelevated no-caps color=teal-8')
    with ui.column().classes('w-full gap-2'):
        await _seznam()
