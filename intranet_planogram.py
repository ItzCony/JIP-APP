# intranet_planogram.py
# ═══════════════════════════════════════════════════════════════════════════════
#   Plánogram tabákových výrobků – CC / MO
# ═══════════════════════════════════════════════════════════════════════════════
import os
import json
import uuid
import base64
import asyncio
import threading
from datetime import datetime

import intranet_data
import intranet_notifikace
from nicegui import ui

# ─────────────────────────────────────────────────────────────────────────────
# KONSTANTY
# ─────────────────────────────────────────────────────────────────────────────
PERM_ADMIN   = 'planogram_admin'
PERM_PRISTUP = 'planogram_pristup'
FOTO_DIR     = 'planogram_fotos'

CELL_W = 158   # px – šířka políčka  (63 × 2,5)
CELL_H = 173   # px – výška políčka  (69 × 2,5)
GRID_S = 35    # sloupce hlavního gridu
GRID_V = 25    # řádky hlavního gridu
SUB_S  = 20    # sloupce vnořeného gridu (pobočka)
SUB_V  = 6     # řádky vnořeného gridu

LAYOUTS = [
    {'id': 'OHU',   'nazev': 'OHU',             'popis': 'Pokladní stojany nad pokladnami'},
    {'id': 'MONEW', 'nazev': 'dřevěnka MO NEW', 'popis': 'Placy za pokladnami'},
    {'id': 'CCNEW', 'nazev': 'dřevěnka CC NEW', 'popis': 'Placy za pokladnami'},
    {'id': 'MOOLD', 'nazev': 'dřevěnka MO OLD', 'popis': 'Placy za pokladnami'},
    {'id': 'CCOLD', 'nazev': 'dřevěnka CC OLD', 'popis': 'Placy za pokladnami'},
]

PALETA = [
    '#ffffff', '#fffbeb', '#fef3c7', '#fde68a', '#fcd34d', '#fbbf24',
    '#fed7aa', '#fdba74', '#fb923c', '#f97316', '#ea580c',
    '#fecaca', '#fca5a5', '#f87171', '#ef4444', '#dc2626',
    '#d1fae5', '#a7f3d0', '#6ee7b7', '#34d399', '#10b981', '#059669',
    '#bfdbfe', '#93c5fd', '#60a5fa', '#3b82f6', '#2563eb', '#1d4ed8',
    '#e9d5ff', '#c4b5fd', '#a78bfa', '#8b5cf6', '#7c3aed', '#6d28d9',
    '#fce7f3', '#fbcfe8', '#f9a8d4', '#f472b6', '#ec4899', '#db2777',
    '#e5e7eb', '#d1d5db', '#9ca3af', '#6b7280', '#374151', '#1f2937',
]

_DB_INIT = False


# ═══════════════════════════════════════════════════════════════════════════════
# DB INICIALIZACE
# ═══════════════════════════════════════════════════════════════════════════════
def _init_db():
    global _DB_INIT
    if _DB_INIT:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planogram_fochy (
                id          VARCHAR(36)  PRIMARY KEY,
                layout_id   VARCHAR(20)  NOT NULL,
                radek       INT          NOT NULL,
                sloupec     INT          NOT NULL,
                nazev       VARCHAR(200),
                popis       TEXT,
                barva       VARCHAR(20)  DEFAULT '#ffffff',
                foto_cesta  VARCHAR(500),
                merge_sirka INT          DEFAULT 1,
                merge_vyska INT          DEFAULT 1,
                UNIQUE KEY  uk_lp (layout_id, radek, sloupec),
                INDEX       idx_layout (layout_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planogram_vnorene (
                id       VARCHAR(36)  PRIMARY KEY,
                foch_id  VARCHAR(36)  NOT NULL,
                nazev    VARCHAR(200) NOT NULL,
                INDEX    idx_foch (foch_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planogram_vnorene_fochy (
                id          VARCHAR(36)  PRIMARY KEY,
                vnorena_id  VARCHAR(36)  NOT NULL,
                radek       INT          NOT NULL,
                sloupec     INT          NOT NULL,
                nazev       VARCHAR(200),
                popis       TEXT,
                barva       VARCHAR(20)  DEFAULT '#ffffff',
                foto_cesta  VARCHAR(500),
                UNIQUE KEY  uk_vp (vnorena_id, radek, sloupec),
                INDEX       idx_v (vnorena_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planogram_komentare (
                id         VARCHAR(36)  PRIMARY KEY,
                layout_id  VARCHAR(20)  NOT NULL,
                user_id    INT          NOT NULL,
                user_jmeno VARCHAR(200),
                text       TEXT         NOT NULL,
                datum      DATETIME     DEFAULT CURRENT_TIMESTAMP,
                INDEX      idx_layout (layout_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        conn.commit()
        _DB_INIT = True
    except Exception as e:
        print(f'[Planogram] DB init: {e}')
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FUNKCE
# ═══════════════════════════════════════════════════════════════════════════════
def _nacti_fochy(layout_id):
    """Vrátí ({(r,c): dict}, set foch_id s vnořeným menu)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}, set()
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM planogram_fochy WHERE layout_id = %s", (layout_id,))
        fochy = {(f['radek'], f['sloupec']): f for f in cur.fetchall()}
        foch_ids = [f['id'] for f in fochy.values()]
        vnorene_ids = set()
        if foch_ids:
            ph = ','.join(['%s'] * len(foch_ids))
            cur.execute(
                f"SELECT DISTINCT foch_id FROM planogram_vnorene WHERE foch_id IN ({ph})",
                foch_ids)
            vnorene_ids = {r['foch_id'] for r in cur.fetchall()}
        return fochy, vnorene_ids
    except Exception as e:
        print(f'[Planogram] _nacti_fochy: {e}')
        return {}, set()
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_vnorene_fochy(vnorena_id):
    """Vrátí {(r,c): dict} pro vnořené menu pobočky."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM planogram_vnorene_fochy WHERE vnorena_id = %s", (vnorena_id,))
        return {(f['radek'], f['sloupec']): f for f in cur.fetchall()}
    except Exception as e:
        print(f'[Planogram] _nacti_vnorene_fochy: {e}')
        return {}
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_pobocky(foch_id):
    """Vrátí seznam vnořených menu (poboček) pro foch."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM planogram_vnorene WHERE foch_id = %s ORDER BY nazev",
            (foch_id,))
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _nacti_komentare(layout_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM planogram_komentare WHERE layout_id=%s ORDER BY datum DESC LIMIT 100",
            (layout_id,))
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _uloz_foch(layout_id, radek, sloupec, nazev, popis,
               barva, foto_cesta, merge_sirka, merge_vyska):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM planogram_fochy WHERE layout_id=%s AND radek=%s AND sloupec=%s",
            (layout_id, radek, sloupec))
        row = cur.fetchone()
        if row:
            fid = row[0]
            cur.execute("""
                UPDATE planogram_fochy
                SET nazev=%s, popis=%s, barva=%s, foto_cesta=%s,
                    merge_sirka=%s, merge_vyska=%s
                WHERE id=%s
            """, (nazev or None, popis or None, barva or '#ffffff',
                  foto_cesta or None, max(1, merge_sirka or 1), max(1, merge_vyska or 1), fid))
        else:
            fid = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO planogram_fochy
                    (id, layout_id, radek, sloupec, nazev, popis,
                     barva, foto_cesta, merge_sirka, merge_vyska)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (fid, layout_id, radek, sloupec, nazev or None, popis or None,
                  barva or '#ffffff', foto_cesta or None,
                  max(1, merge_sirka or 1), max(1, merge_vyska or 1)))
        conn.commit()
        return fid
    except Exception as e:
        print(f'[Planogram] _uloz_foch: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _smaz_foch(layout_id, radek, sloupec):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, foto_cesta FROM planogram_fochy "
            "WHERE layout_id=%s AND radek=%s AND sloupec=%s",
            (layout_id, radek, sloupec))
        foch = cur.fetchone()
        if not foch:
            return
        fid = foch['id']
        _smazat_foto_disk(foch.get('foto_cesta'))
        cur = conn.cursor()
        # Smazat vnořená menu a jejich fochy
        cur.execute("SELECT id FROM planogram_vnorene WHERE foch_id=%s", (fid,))
        for (vid,) in cur.fetchall():
            _smaz_vsechny_vnorene_fochy(cur, vid)
        cur.execute("DELETE FROM planogram_vnorene WHERE foch_id=%s", (fid,))
        cur.execute("DELETE FROM planogram_fochy WHERE id=%s", (fid,))
        conn.commit()
    except Exception as e:
        print(f'[Planogram] _smaz_foch: {e}')
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _smaz_vsechny_vnorene_fochy(cur, vnorena_id):
    cur.execute(
        "SELECT foto_cesta FROM planogram_vnorene_fochy WHERE vnorena_id=%s",
        (vnorena_id,))
    for (cesta,) in cur.fetchall():
        _smazat_foto_disk(cesta)
    cur.execute("DELETE FROM planogram_vnorene_fochy WHERE vnorena_id=%s", (vnorena_id,))


def _swap_fochy(layout_id, r1, c1, r2, c2):
    """Prohodí dvě políčka v layoutu (drag & drop)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM planogram_fochy "
            "WHERE layout_id=%s AND radek=%s AND sloupec=%s",
            (layout_id, r1, c1))
        f1 = cur.fetchone()
        cur.execute(
            "SELECT * FROM planogram_fochy "
            "WHERE layout_id=%s AND radek=%s AND sloupec=%s",
            (layout_id, r2, c2))
        f2 = cur.fetchone()
        if not f1 and not f2:
            return True
        cur = conn.cursor()
        if f1 and f2:
            cur.execute(
                "UPDATE planogram_fochy SET radek=-1,sloupec=-1 WHERE id=%s",
                (f1['id'],))
            cur.execute(
                "UPDATE planogram_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r1, c1, f2['id']))
            cur.execute(
                "UPDATE planogram_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r2, c2, f1['id']))
        elif f1:
            cur.execute(
                "UPDATE planogram_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r2, c2, f1['id']))
        else:
            cur.execute(
                "UPDATE planogram_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r1, c1, f2['id']))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Planogram] _swap_fochy: {e}')
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _swap_vnorene_fochy(vnorena_id, r1, c1, r2, c2):
    """Prohodí dvě políčka ve vnořeném menu (drag & drop)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM planogram_vnorene_fochy "
            "WHERE vnorena_id=%s AND radek=%s AND sloupec=%s",
            (vnorena_id, r1, c1))
        f1 = cur.fetchone()
        cur.execute(
            "SELECT * FROM planogram_vnorene_fochy "
            "WHERE vnorena_id=%s AND radek=%s AND sloupec=%s",
            (vnorena_id, r2, c2))
        f2 = cur.fetchone()
        if not f1 and not f2:
            return True
        cur = conn.cursor()
        if f1 and f2:
            cur.execute(
                "UPDATE planogram_vnorene_fochy SET radek=-1,sloupec=-1 WHERE id=%s",
                (f1['id'],))
            cur.execute(
                "UPDATE planogram_vnorene_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r1, c1, f2['id']))
            cur.execute(
                "UPDATE planogram_vnorene_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r2, c2, f1['id']))
        elif f1:
            cur.execute(
                "UPDATE planogram_vnorene_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r2, c2, f1['id']))
        else:
            cur.execute(
                "UPDATE planogram_vnorene_fochy SET radek=%s,sloupec=%s WHERE id=%s",
                (r1, c1, f2['id']))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Planogram] _swap_vnorene_fochy: {e}')
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _uloz_vnoreny_foch(vnorena_id, radek, sloupec, nazev, popis, barva, foto_cesta):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM planogram_vnorene_fochy "
            "WHERE vnorena_id=%s AND radek=%s AND sloupec=%s",
            (vnorena_id, radek, sloupec))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE planogram_vnorene_fochy
                SET nazev=%s, popis=%s, barva=%s, foto_cesta=%s
                WHERE vnorena_id=%s AND radek=%s AND sloupec=%s
            """, (nazev or None, popis or None, barva or '#ffffff',
                  foto_cesta or None, vnorena_id, radek, sloupec))
        else:
            cur.execute("""
                INSERT INTO planogram_vnorene_fochy
                    (id, vnorena_id, radek, sloupec, nazev, popis, barva, foto_cesta)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (str(uuid.uuid4()), vnorena_id, radek, sloupec,
                  nazev or None, popis or None, barva or '#ffffff', foto_cesta or None))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Planogram] _uloz_vnoreny_foch: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _smaz_vnoreny_foch(vnorena_id, radek, sloupec):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT foto_cesta FROM planogram_vnorene_fochy "
            "WHERE vnorena_id=%s AND radek=%s AND sloupec=%s",
            (vnorena_id, radek, sloupec))
        row = cur.fetchone()
        if row:
            _smazat_foto_disk(row[0])
        cur.execute(
            "DELETE FROM planogram_vnorene_fochy "
            "WHERE vnorena_id=%s AND radek=%s AND sloupec=%s",
            (vnorena_id, radek, sloupec))
        conn.commit()
    except Exception as e:
        print(f'[Planogram] _smaz_vnoreny_foch: {e}')
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _vytvor_pobocku(foch_id, nazev):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return None
    cur = None
    try:
        cur = conn.cursor()
        vid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO planogram_vnorene (id, foch_id, nazev) VALUES (%s,%s,%s)",
            (vid, foch_id, nazev))
        conn.commit()
        return vid
    except Exception as e:
        print(f'[Planogram] _vytvor_pobocku: {e}')
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _smaz_pobocku(vnorena_id):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        _smaz_vsechny_vnorene_fochy(cur, vnorena_id)
        cur.execute("DELETE FROM planogram_vnorene WHERE id=%s", (vnorena_id,))
        conn.commit()
    except Exception as e:
        print(f'[Planogram] _smaz_pobocku: {e}')
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _pridej_komentar(layout_id, user_id, user_jmeno, text):
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO planogram_komentare (id, layout_id, user_id, user_jmeno, text)
            VALUES (%s,%s,%s,%s,%s)
        """, (str(uuid.uuid4()), layout_id, user_id, user_jmeno, text))
        conn.commit()
        return True
    except Exception as e:
        print(f'[Planogram] _pridej_komentar: {e}')
        return False
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _smazat_foto_disk(cesta):
    if cesta and os.path.exists(cesta):
        try:
            os.remove(cesta)
        except Exception:
            pass


def _uloz_foto_disk(soubor_nazev, obsah_bytes):
    os.makedirs(FOTO_DIR, exist_ok=True)
    ext = os.path.splitext(soubor_nazev)[1].lower() or '.jpg'
    cesta = os.path.join(FOTO_DIR, f'{uuid.uuid4()}{ext}')
    try:
        with open(cesta, 'wb') as f:
            f.write(obsah_bytes)
        return cesta
    except Exception as e:
        print(f'[Planogram] _uloz_foto_disk: {e}')
        return None


def _foto_url(cesta):
    if not cesta:
        return None
    return f'/planogram_fotos/{os.path.basename(cesta)}'


def _absorbovane(fochy, sirka, vyska):
    """Vrátí set (r,c) pohlcených slučováním."""
    absorbed = set()
    for (r, c), f in fochy.items():
        mw = max(1, f.get('merge_sirka') or 1)
        mh = max(1, f.get('merge_vyska') or 1)
        if mw > 1 or mh > 1:
            for dr in range(mh):
                for dc in range(mw):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < vyska and 0 <= nc < sirka:
                        absorbed.add((nr, nc))
    return absorbed


def _fmt_datum(dt):
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt[:16]
    return dt.strftime('%d.%m.%Y %H:%M')


def _notifikuj_pristupove(user_name):
    """Rozešle notifikaci všem uživatelům s přístupem k plánogramu."""
    try:
        admin_ids = intranet_data.ziskej_uzivatele_s_pravem(PERM_ADMIN, 'vse')
        user_ids  = intranet_data.ziskej_uzivatele_s_pravem(PERM_PRISTUP, 'vse')
        vsichni   = set(admin_ids + user_ids)
        for uid in vsichni:
            intranet_notifikace.pridej(
                uid,
                f'📋 {user_name} aktualizoval/a Plánogram tabákových výrobků. '
                f'Zkontrolujte rozmístění a upravte dle potřeby.',
                'info')
    except Exception as e:
        print(f'[Planogram] _notifikuj_pristupove: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# SDÍLENÉ DIALOGY – edit políčka (admin) / detail (uživatel) / vnořené menu
# ═══════════════════════════════════════════════════════════════════════════════
def _build_edit_dialog(je_admin):
    """
    Vytvoří sdílené dialogy pro celý modul.
    Vrátí (dlg_edit, dlg_detail, dlg_pobocky, dlg_sub, ctx, fn_otevrit_edit,
            fn_otevrit_detail, fn_otevrit_pobocky).
    """
    ctx = {
        'layout_id': None, 'r': 0, 'c': 0, 'foch': None,
        'vnorena_id': None, 'vr': 0, 'vc': 0, 'vfoch': None,
        'foto_pending': {'bytes': None, 'nazev': None},
        'vfoto_pending': {'bytes': None, 'nazev': None},
        'refresh_grid': None,   # volatelný callback pro obnovu gridu
        'refresh_sub': None,    # volatelný callback pro obnovu sub-gridu
    }

    # ── Dialog: Detail políčka (read-only) ────────────────────────────────────
    with ui.dialog().classes('!max-w-md w-full') as dlg_detail:
        with ui.card().classes('w-full p-5'):
            det_nazev = ui.label('').classes('text-lg font-bold text-gray-800 mb-1')
            det_popis = ui.label('').classes('text-sm text-gray-600 whitespace-pre-wrap mb-3')
            det_foto  = ui.image('').classes(
                'w-full rounded-lg max-h-52 object-contain mb-3')
            det_foto.set_visibility(False)
            with ui.row().classes('w-full justify-end'):
                ui.button('Zavřít', on_click=dlg_detail.close) \
                    .props('flat').classes('text-gray-500')

    def _otevrit_detail(foch):
        det_nazev.set_text(foch.get('nazev') or '—')
        det_popis.set_text(foch.get('popis') or '')
        url = _foto_url(foch.get('foto_cesta'))
        if url:
            det_foto.set_source(url)
            det_foto.set_visibility(True)
        else:
            det_foto.set_visibility(False)
        dlg_detail.open()

    # ── Dialog: Výběr pobočky (vnořené menu – výběr pro uživatele) ───────────
    with ui.dialog().classes('!max-w-sm w-full') as dlg_pobocky:
        with ui.card().classes('w-full p-5'):
            ui.label('Vyberte pobočku').classes('text-xl font-bold text-gray-800 mb-3')

            @ui.refreshable
            def _seznam_pobocek():
                foch = ctx.get('foch')
                if not foch:
                    return
                pobocky = _nacti_pobocky(foch['id'])
                if not pobocky:
                    ui.label('Žádné pobočky.').classes('text-sm text-gray-400 italic')
                    return
                for p in pobocky:
                    ui.button(
                        p['nazev'],
                        on_click=lambda _p=p: (
                            dlg_pobocky.close(),
                            _otevrit_sub_dialog(_p, je_admin)
                        )
                    ).classes('w-full bg-blue-50 hover:bg-blue-100 text-blue-800 '
                               'font-bold mb-2 rounded-lg')

            _seznam_pobocek()
            with ui.row().classes('w-full justify-end mt-2'):
                ui.button('Zavřít', on_click=dlg_pobocky.close) \
                    .props('flat').classes('text-gray-500')

    def _otevrit_pobocky(foch):
        ctx['foch'] = foch
        _seznam_pobocek.refresh()
        dlg_pobocky.open()

    _zoom_sub    = {'factor': 1.0}
    _ZOOM_SUB_KR = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
                    1.1, 1.25, 1.5, 1.75, 2.0]

    def _zoom_sub_out():
        idx = _ZOOM_SUB_KR.index(
            min(_ZOOM_SUB_KR, key=lambda x: abs(x - _zoom_sub['factor'])))
        if idx > 0:
            _zoom_sub['factor'] = _ZOOM_SUB_KR[idx - 1]
            _zoom_bar_sub.refresh()
            _sub_grid_area.refresh()

    def _zoom_sub_in():
        idx = _ZOOM_SUB_KR.index(
            min(_ZOOM_SUB_KR, key=lambda x: abs(x - _zoom_sub['factor'])))
        if idx < len(_ZOOM_SUB_KR) - 1:
            _zoom_sub['factor'] = _ZOOM_SUB_KR[idx + 1]
            _zoom_bar_sub.refresh()
            _sub_grid_area.refresh()

    def _zoom_sub_reset():
        _zoom_sub['factor'] = 1.0
        _zoom_bar_sub.refresh()
        _sub_grid_area.refresh()

    # ── Fullscreen stránka: Vnořené menu (sub-grid pobočky) ──────────────────
    with ui.dialog().props('maximized transition-show=slide-up transition-hide=slide-down') as dlg_sub:
        with ui.column().classes('w-full h-full bg-white gap-0'):

            async def _nahled_pdf_sub():
                vnorena_id = ctx.get('vnorena_id')
                if not vnorena_id:
                    return
                vfochy_pdf = _nacti_vnorene_fochy(vnorena_id)
                vabs_pdf   = _absorbovane(vfochy_pdf, SUB_S, SUB_V)
                nazev_sub  = sub_title.text if hasattr(sub_title, 'text') else 'Pobočka'
                html = _build_planogram_html(
                    titulek   = nazev_sub,
                    popis_str = 'Rozmístění výrobků v pobočce',
                    fochy     = vfochy_pdf,
                    absorbed  = vabs_pdf,
                    sirka     = SUB_S,
                    vyska     = SUB_V,
                    show_foto = True,
                )
                html_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')
                await ui.run_javascript(f'''
                    var b64  = "{html_b64}";
                    var bin  = atob(b64);
                    var arr  = new Uint8Array(bin.length);
                    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                    var blob = new Blob([arr], {{type:"text/html;charset=utf-8"}});
                    var url  = URL.createObjectURL(blob);
                    var w    = window.open(url, "_blank");
                    if (!w) alert("Povolte prosím vyskakovací okna pro tisk/export.");
                ''')

            # Záhlaví
            with ui.row().classes(
                'w-full items-center justify-between '
                'px-6 py-4 border-b border-gray-200 flex-shrink-0 bg-white'
            ):
                with ui.column().classes('gap-0'):
                    sub_title = ui.label('').classes('text-2xl font-black text-blue-900')
                    ui.label('Rozmístění výrobků v pobočce').classes('text-sm text-gray-500')
                with ui.row().classes('items-center gap-2'):
                    ui.button(icon='picture_as_pdf', on_click=_nahled_pdf_sub) \
                        .props('flat round') \
                        .classes('text-red-500 hover:bg-red-50') \
                        .tooltip('Tisk / Export do PDF')

                    @ui.refreshable
                    def _zoom_bar_sub():
                        is_min = _zoom_sub['factor'] <= _ZOOM_SUB_KR[0]
                        is_max = _zoom_sub['factor'] >= _ZOOM_SUB_KR[-1]
                        with ui.row().classes('items-center gap-0 '
                                              'border border-gray-200 rounded-lg overflow-hidden'):
                            ui.button(icon='remove', on_click=_zoom_sub_out) \
                                .props('flat dense') \
                                .classes('h-8 w-8 rounded-none ' +
                                         ('text-gray-200 cursor-default' if is_min
                                          else 'text-gray-500 hover:bg-gray-100'))
                            ui.label(f'{int(_zoom_sub["factor"] * 100)} %') \
                                .classes('text-xs font-bold text-gray-600 w-12 text-center '
                                         'cursor-pointer select-none border-x border-gray-200 h-8 '
                                         'flex items-center justify-center') \
                                .style('display:flex;align-items:center;justify-content:center') \
                                .on('click', lambda: _zoom_sub_reset()) \
                                .tooltip('Resetovat na 100 %')
                            ui.button(icon='add', on_click=_zoom_sub_in) \
                                .props('flat dense') \
                                .classes('h-8 w-8 rounded-none ' +
                                         ('text-gray-200 cursor-default' if is_max
                                          else 'text-gray-500 hover:bg-gray-100'))

                    _zoom_bar_sub()

                    ui.button(icon='arrow_back', text='Zpět', on_click=dlg_sub.close) \
                        .props('flat').classes('text-gray-600 font-bold')

            # Oblast gridu – centrovaná, scrollovatelná při menším okně
            with ui.element('div').style(
                'flex:1;overflow:auto;padding:24px;'
                'display:flex;align-items:center;justify-content:center;'
            ):
                @ui.refreshable
                def _sub_grid_area():
                    vnorena_id = ctx.get('vnorena_id')
                    if not vnorena_id:
                        return
                    vfochy = _nacti_vnorene_fochy(vnorena_id)
                    vabs   = _absorbovane(vfochy, SUB_S, SUB_V)

                    def _drop_sub(from_r, from_c, to_r, to_c):
                        if _swap_vnorene_fochy(vnorena_id, from_r, from_c, to_r, to_c):
                            _sub_grid_area.refresh()
                        else:
                            ui.notify('Přesunutí se nezdařilo.', type='negative')

                    z = _zoom_sub['factor']
                    _render_grid(
                        fochy       = vfochy,
                        absorbed    = vabs,
                        vnorene_ids = set(),
                        sirka       = SUB_S,
                        vyska       = SUB_V,
                        je_admin    = je_admin,
                        on_click    = lambda r, c, f, a='main': _klik_sub(r, c, f, a),
                        on_drop     = _drop_sub if je_admin else None,
                        cell_w      = max(20, int(CELL_W * z)),
                        cell_h      = max(20, int(CELL_H * z)),
                        show_foto   = True,
                    )

                ctx['refresh_sub'] = _sub_grid_area.refresh
                _sub_grid_area()

    # ── Dialog: Edit políčka v sub-gridu ─────────────────────────────────────
    with ui.dialog().classes('!max-w-lg w-full') as dlg_sub_edit:
        with ui.card().classes('w-full p-5 gap-2'):
            sub_edit_title = ui.label('').classes('text-lg font-bold text-gray-800 mb-1')
            se_nazev = ui.input('Název výrobku').classes('w-full').props('outlined')
            se_popis  = ui.textarea('Popis / poznámka').classes('w-full').props('outlined rows=3')
            ui.label('Barva').classes('text-xs font-bold text-gray-600 mt-1')
            se_barva_ref = {'val': '#ffffff'}
            with ui.row().classes('flex-wrap gap-1'):
                for _b in PALETA:
                    def _set_sb(b=_b):
                        se_barva_ref['val'] = b
                        se_barva_prev.style(
                            f'width:100%;height:14px;border-radius:4px;'
                            f'border:1px solid #e5e7eb;background:{b}')
                    ui.element('div') \
                        .style(f'background:{_b};width:18px;height:18px;border-radius:3px;'
                               f'cursor:pointer;border:1px solid rgba(0,0,0,.12)') \
                        .on('click', _set_sb).tooltip(_b)
            se_barva_prev = ui.element('div').style(
                'width:100%;height:14px;border-radius:4px;border:1px solid #e5e7eb;background:#ffffff')

            ui.label('Fotografie').classes('text-xs font-bold text-gray-600 mt-1')
            se_foto_lbl = ui.label('').classes('text-xs text-green-600 italic')
            se_foto_smazat = {'flag': False}

            async def _se_foto_upload(e):
                ctx['vfoto_pending']['bytes'] = await e.file.read()
                ctx['vfoto_pending']['nazev'] = e.file.name
                se_foto_lbl.set_text(f'📎 {e.file.name[:40]}')

            with ui.row().classes('items-center gap-2'):
                ui.upload(auto_upload=True, on_upload=_se_foto_upload) \
                    .props('accept="image/*" label="Nahrát foto"').classes('flex-1')
                ui.button('🗑️ Smazat foto', on_click=lambda: (
                    se_foto_smazat.__setitem__('flag', True),
                    se_foto_lbl.set_text('Fotka bude smazána.')
                )).props('flat dense').classes('text-red-500 text-xs')

            async def _ulozit_sub():
                try:
                    vnorena_id = ctx['vnorena_id']
                    vr, vc     = ctx['vr'], ctx['vc']
                    # Fotka
                    stara_cesta = (ctx.get('vfoch') or {}).get('foto_cesta')
                    foto_cesta  = stara_cesta
                    if se_foto_smazat['flag']:
                        _smazat_foto_disk(foto_cesta)
                        foto_cesta = None
                        se_foto_smazat['flag'] = False
                    if ctx['vfoto_pending']['bytes']:
                        nf = await asyncio.to_thread(
                            _uloz_foto_disk,
                            ctx['vfoto_pending']['nazev'],
                            ctx['vfoto_pending']['bytes'])
                        if nf:
                            foto_cesta = nf
                        else:
                            ui.notify('Nepodařilo se uložit fotku na disk.', type='negative')
                        ctx['vfoto_pending']['bytes'] = None
                    _uloz_vnoreny_foch(vnorena_id, vr, vc,
                                       se_nazev.value or None,
                                       se_popis.value or None,
                                       se_barva_ref['val'],
                                       foto_cesta)
                    dlg_sub_edit.close()
                    ui.notify('Políčko uloženo.', type='positive')
                    if ctx.get('refresh_sub'):
                        ctx['refresh_sub']()
                except Exception as ex:
                    ui.notify(f'Chyba při ukládání: {ex}', type='negative', timeout=8000)
                    print(f'[Planogram] _ulozit_sub chyba: {ex}')

            async def _smazat_sub():
                _smaz_vnoreny_foch(ctx['vnorena_id'], ctx['vr'], ctx['vc'])
                dlg_sub_edit.close()
                if ctx.get('refresh_sub'):
                    ctx['refresh_sub']()

            with ui.row().classes('w-full justify-between mt-3'):
                ui.button('🗑️ Smazat', on_click=_smazat_sub) \
                    .props('flat').classes('text-red-500')
                with ui.row().classes('gap-2'):
                    ui.button('Zrušit', on_click=dlg_sub_edit.close) \
                        .props('flat').classes('text-gray-500')
                    ui.button('💾 Uložit', on_click=_ulozit_sub) \
                        .classes('bg-blue-600 text-white font-bold')

    def _klik_sub(r, c, foch, action='main'):
        # Oko – náhled (foto + popis), funguje pro admina i čtenáře
        if action == 'detail':
            if foch and (foch.get('popis') or foch.get('foto_cesta')):
                _otevrit_detail(foch)
            return
        if not je_admin:
            if foch and (foch.get('popis') or foch.get('foto_cesta')):
                _otevrit_detail(foch)
            return
        ctx['vr']   = r
        ctx['vc']   = c
        ctx['vfoch'] = foch
        ctx['vfoto_pending']['bytes'] = None
        barva = (foch.get('barva') or '#ffffff') if foch else '#ffffff'
        se_nazev.set_value((foch.get('nazev') or '') if foch else '')
        se_popis.set_value((foch.get('popis') or '') if foch else '')
        se_barva_ref['val'] = barva
        se_barva_prev.style(
            f'width:100%;height:14px;border-radius:4px;'
            f'border:1px solid #e5e7eb;background:{barva}')
        se_foto_lbl.set_text('')
        se_foto_smazat['flag'] = False
        sub_edit_title.set_text(f'Úprava pole [{r+1}, {c+1}]')
        dlg_sub_edit.open()

    def _otevrit_sub_dialog(pobocka, _je_admin):
        ctx['vnorena_id'] = pobocka['id']
        ctx['refresh_sub'] = _sub_grid_area.refresh
        sub_title.set_text(f'Pobočka: {pobocka["nazev"]}')
        _sub_grid_area.refresh()
        dlg_sub.open()

    # ── Dialog: Edit hlavního políčka (admin) ─────────────────────────────────
    with ui.dialog().classes('!max-w-xl w-full') as dlg_edit:
        with ui.card().classes('w-full p-6 gap-2'):
            edit_title = ui.label('').classes('text-lg font-bold text-gray-800 mb-1')

            ed_nazev = ui.input('Název výrobku').classes('w-full').props('outlined')
            ed_popis  = ui.textarea('Popis / poznámka').classes('w-full').props('outlined rows=3')

            # Barva
            ui.label('Barva pozadí').classes('text-xs font-bold text-gray-600 mt-1')
            ed_barva_ref = {'val': '#ffffff', '_preview_refresh': lambda: None}
            with ui.row().classes('flex-wrap gap-1'):
                for _b in PALETA:
                    def _set_eb(b=_b):
                        ed_barva_ref['val'] = b
                        ed_barva_prev.style(
                            f'width:100%;height:14px;border-radius:4px;'
                            f'border:1px solid #e5e7eb;background:{b}')
                        ed_barva_ref['_preview_refresh']()
                    ui.element('div') \
                        .style(f'background:{_b};width:18px;height:18px;border-radius:3px;'
                               f'cursor:pointer;border:1px solid rgba(0,0,0,.12)') \
                        .on('click', _set_eb).tooltip(_b)
            ed_barva_prev = ui.element('div').style(
                'width:100%;height:14px;border-radius:4px;border:1px solid #e5e7eb;background:#ffffff')

            # Sloučení
            ui.label('Sloučení buněk').classes('text-xs font-bold text-gray-600 mt-2')
            with ui.row().classes('gap-4 items-start'):
                ed_merge_w = ui.number('Šířka (sloupce)', min=1, max=GRID_S,
                                       value=1).classes('w-36').props('outlined dense')
                ed_merge_h = ui.number('Výška (řádky)',   min=1, max=GRID_V,
                                       value=1).classes('w-36').props('outlined dense')

            # Živý náhled sloučení – jeden ui.html element, obsah se mění přes set_content()
            _merge_prev_el = ui.html('').style('display:block;margin-top:6px;')

            def _build_merge_html():
                w    = max(1, min(int(ed_merge_w.value or 1), 10))
                h    = max(1, min(int(ed_merge_h.value or 1), 6))
                barva = ed_barva_ref.get('val', '#ffffff')
                CTX  = 1          # řádky/sloupce kontextu okolo
                SZ   = '22px'     # velikost kontextové buňky
                cols = w + CTX * 2
                rows = h + CTX * 2

                td_ctx = (
                    f'style="width:{SZ};height:{SZ};'
                    f'background:#f1f5f9;border:1px solid #cbd5e1;"'
                )
                td_merge = (
                    f'style="width:{w * 22}px;height:{h * 22}px;'
                    f'background:{barva};border:2px solid #1d4ed8;'
                    f'text-align:center;vertical-align:middle;'
                    f'font-size:.6rem;font-weight:800;color:#1e293b;"'
                )

                rows_html = ''
                for pr in range(rows):
                    tds = ''
                    for pc in range(cols):
                        in_merge = (CTX <= pr < CTX + h and CTX <= pc < CTX + w)
                        anchor   = (pr == CTX and pc == CTX)
                        if anchor:
                            tds += f'<td rowspan="{h}" colspan="{w}" {td_merge}>{w}&times;{h}</td>'
                        elif in_merge:
                            pass   # pohlceno rowspan/colspan
                        else:
                            tds += f'<td {td_ctx}></td>'
                    rows_html += f'<tr>{tds}</tr>'

                return (
                    f'<table style="border-collapse:collapse;'
                    f'background:#e2e8f0;border-radius:4px;'
                    f'padding:2px;border:2px solid #e2e8f0;">'
                    f'{rows_html}</table>'
                )

            def _refresh_merge_preview():
                _merge_prev_el.set_content(_build_merge_html())

            ed_barva_ref['_preview_refresh'] = _refresh_merge_preview
            _refresh_merge_preview()
            ed_merge_w.on_value_change(lambda _: _refresh_merge_preview())
            ed_merge_h.on_value_change(lambda _: _refresh_merge_preview())

            # Fotka
            ui.label('Fotografie').classes('text-xs font-bold text-gray-600 mt-2')
            ed_foto_lbl = ui.label('').classes('text-xs text-green-600 italic')
            ed_foto_smazat = {'flag': False}

            async def _ed_foto_upload(e):
                ctx['foto_pending']['bytes'] = await e.file.read()
                ctx['foto_pending']['nazev'] = e.file.name
                ed_foto_lbl.set_text(f'📎 {e.file.name[:40]}')

            with ui.row().classes('items-center gap-2'):
                ui.upload(auto_upload=True, on_upload=_ed_foto_upload) \
                    .props('accept="image/*" label="Nahrát foto"').classes('flex-1')
                ui.button('🗑️ Smazat foto', on_click=lambda: (
                    ed_foto_smazat.__setitem__('flag', True),
                    ed_foto_lbl.set_text('Fotka bude smazána.')
                )).props('flat dense').classes('text-red-500 text-xs')

            # Vnořená menu
            ui.separator().classes('my-2')
            ui.label('Vnořená menu — pobočky').classes('text-xs font-bold text-gray-600')

            @ui.refreshable
            def _pobocky_v_editoru():
                foch = ctx.get('foch')
                if not foch:
                    ui.label('Uložte políčko nejprve, poté přidávejte pobočky.') \
                        .classes('text-xs text-gray-400 italic')
                    return
                pobocky = _nacti_pobocky(foch['id'])
                if not pobocky:
                    ui.label('Žádné pobočky.').classes('text-xs text-gray-400 italic mb-1')
                for p in pobocky:
                    with ui.row().classes('items-center gap-2 w-full'):
                        ui.label(p['nazev']).classes('text-sm text-gray-700 flex-1')
                        ui.button(
                            icon='open_in_new',
                            on_click=lambda _p=p: _otevrit_sub_dialog(_p, je_admin)
                        ).props('flat round dense').tooltip('Otevřít fochy pobočky')
                        ui.button(
                            icon='delete',
                            on_click=lambda _p=p: (
                                _smaz_pobocku(_p['id']),
                                _pobocky_v_editoru.refresh(),
                                ui.notify('Pobočka smazána.', type='positive')
                            )
                        ).props('flat round dense color=red').tooltip('Smazat pobočku')

            _pobocky_v_editoru()

            with ui.row().classes('items-center gap-2 mt-1'):
                inp_pobocka = ui.input('Název nové pobočky', placeholder='např. Pardubice') \
                    .classes('flex-1').props('outlined dense')

                def _pridat_pobocku():
                    foch  = ctx.get('foch')
                    nazev = (inp_pobocka.value or '').strip()
                    if not nazev:
                        ui.notify('Zadejte název pobočky.', type='warning')
                        return
                    if not foch:
                        ui.notify('Nejprve uložte políčko.', type='warning')
                        return
                    _vytvor_pobocku(foch['id'], nazev)
                    inp_pobocka.set_value('')
                    _pobocky_v_editoru.refresh()
                    ui.notify(f'Pobočka „{nazev}" přidána.', type='positive')

                ui.button('➕', on_click=_pridat_pobocku) \
                    .props('flat dense').classes('text-blue-600 font-bold')

            # Uložit / Smazat
            async def _ulozit_edit():
                try:
                    layout_id  = ctx['layout_id']
                    r, c       = ctx['r'], ctx['c']
                    stara_cesta = (ctx.get('foch') or {}).get('foto_cesta')
                    foto_cesta  = stara_cesta
                    if ed_foto_smazat['flag']:
                        _smazat_foto_disk(foto_cesta)
                        foto_cesta = None
                        ed_foto_smazat['flag'] = False
                    if ctx['foto_pending']['bytes']:
                        nf = await asyncio.to_thread(
                            _uloz_foto_disk,
                            ctx['foto_pending']['nazev'],
                            ctx['foto_pending']['bytes'])
                        if nf:
                            foto_cesta = nf
                        else:
                            ui.notify('Nepodařilo se uložit fotku na disk.', type='negative')
                        ctx['foto_pending']['bytes'] = None
                    fid = _uloz_foch(
                        layout_id, r, c,
                        nazev       = ed_nazev.value or None,
                        popis       = ed_popis.value or None,
                        barva       = ed_barva_ref['val'],
                        foto_cesta  = foto_cesta,
                        merge_sirka = int(ed_merge_w.value or 1),
                        merge_vyska = int(ed_merge_h.value or 1),
                    )
                    # Aktualizovat ctx['foch']['id'] pro případ přidávání pobočky ihned
                    if fid and (not ctx.get('foch') or not ctx['foch'].get('id')):
                        ctx['foch'] = {'id': fid, 'nazev': ed_nazev.value,
                                       'foto_cesta': foto_cesta}
                        _pobocky_v_editoru.refresh()
                    dlg_edit.close()
                    ui.notify('Políčko uloženo.', type='positive')
                    if ctx.get('refresh_grid'):
                        ctx['refresh_grid']()
                except Exception as ex:
                    ui.notify(f'Chyba při ukládání: {ex}', type='negative', timeout=8000)
                    print(f'[Planogram] _ulozit_edit chyba: {ex}')

            async def _smazat_edit():
                _smaz_foch(ctx['layout_id'], ctx['r'], ctx['c'])
                dlg_edit.close()
                if ctx.get('refresh_grid'):
                    ctx['refresh_grid']()

            with ui.row().classes('w-full justify-between mt-3'):
                ui.button('🗑️ Smazat', on_click=_smazat_edit) \
                    .props('flat').classes('text-red-500')
                with ui.row().classes('gap-2'):
                    ui.button('Zrušit', on_click=dlg_edit.close) \
                        .props('flat').classes('text-gray-500')
                    ui.button('💾 Uložit', on_click=_ulozit_edit) \
                        .classes('bg-blue-600 text-white font-bold')

    def _otevrit_edit(layout_id, r, c, foch):
        ctx['layout_id'] = layout_id
        ctx['r']   = r
        ctx['c']   = c
        ctx['foch'] = foch
        ctx['foto_pending']['bytes'] = None
        ed_foto_smazat['flag'] = False
        barva = (foch.get('barva') or '#ffffff') if foch else '#ffffff'
        ed_nazev.set_value((foch.get('nazev') or '') if foch else '')
        ed_popis.set_value((foch.get('popis') or '') if foch else '')
        ed_barva_ref['val'] = barva
        ed_barva_prev.style(
            f'width:100%;height:14px;border-radius:4px;'
            f'border:1px solid #e5e7eb;background:{barva}')
        ed_merge_w.set_value((foch.get('merge_sirka') or 1) if foch else 1)
        ed_merge_h.set_value((foch.get('merge_vyska') or 1) if foch else 1)
        ed_foto_lbl.set_text('')
        inp_pobocka.set_value('')
        _pobocky_v_editoru.refresh()
        edit_title.set_text(f'Úprava políčka [{r+1}, {c+1}]')
        dlg_edit.open()

    return ctx, _otevrit_edit, _otevrit_detail, _otevrit_pobocky


# ═══════════════════════════════════════════════════════════════════════════════
# GRID RENDERER
# ═══════════════════════════════════════════════════════════════════════════════
def _render_grid(fochy, absorbed, vnorene_ids, sirka, vyska,
                 je_admin, on_click, show_foto=False,
                 cell_w=None, cell_h=None, fill=False, on_drop=None):
    """
    Vykreslí CSS grid.
    Architektura:
      outer  – NiceGUI div: scroll-kontejner + posluchač CustomEvent
      grid   – NiceGUI div: display:grid přes .style() (čistý DOM, bez Vue)
      buňky  – injektovány přes ui.run_javascript() jako PŘÍMÉ potomky grid,
               onclick/onmouseenter FUNGUJÍ (vkládají se mimo Vue v-html).
    """
    # overflow-x:auto  → vodorovný posuvník (svislý záměrně není – výška je pevná)
    # flex-shrink:0     → zabraňuje deformaci gridu flex/grid layoutem nadřazených prvků
    if fill:
        outer_style = (
            'flex:1;display:flex;'
            'overflow:hidden;'
            'border:1px solid #e2e8f0;'
            'border-radius:8px;'
            'min-width:0;min-height:0;'
        )
    else:
        outer_style = (
            'display:flex;'
            'overflow-x:auto;'
            'overflow-y:visible;'
            'flex-shrink:0;'
            'border:1px solid #e2e8f0;'
            'border-radius:8px;'
        )
    outer = ui.element('div').classes('w-full').style(outer_style)
    cid   = outer.id                          # DOM id kontejneru = c{cid}

    def _handle(e):
        args   = e.args if isinstance(e.args, dict) else {}
        detail = args.get('detail') or {}
        if not isinstance(detail, dict):
            return
        r      = int(detail.get('r', -1))
        c      = int(detail.get('c', -1))
        action = str(detail.get('action') or 'main')
        if 0 <= r < vyska and 0 <= c < sirka:
            on_click(r, c, fochy.get((r, c)), action)

    outer.on('pg_click', _handle, ['detail'])

    if on_drop and je_admin:
        def _handle_drop(e):
            args   = e.args if isinstance(e.args, dict) else {}
            detail = args.get('detail') or {}
            if not isinstance(detail, dict):
                return
            from_r = int(detail.get('from_r', -1))
            from_c = int(detail.get('from_c', -1))
            to_r   = int(detail.get('to_r',   -1))
            to_c   = int(detail.get('to_c',   -1))
            if (0 <= from_r < vyska and 0 <= from_c < sirka and
                    0 <= to_r < vyska and 0 <= to_c < sirka and
                    (from_r, from_c) != (to_r, to_c)):
                on_drop(from_r, from_c, to_r, to_c)
        outer.on('pg_drop', _handle_drop, ['detail'])

    cw = cell_w if cell_w else CELL_W
    ch = cell_h if cell_h else CELL_H

    if fill:
        col_template = f'repeat({sirka},minmax(40px,1fr))'
        header_style = (
            f'display:grid;grid-template-columns:{col_template};'
            f'gap:1px;background:#e2e8f0;'
            f'width:100%;flex-shrink:0;'
        )
        grid_style = (
            f'display:grid;'
            f'grid-template-columns:{col_template};'
            f'grid-template-rows:repeat({vyska},minmax(60px,1fr));'
            f'gap:1px;background:#cbd5e1;'
            f'width:100%;flex:1;min-height:0;'
        )
    else:
        col_template = f'repeat({sirka},{cw}px)'
        header_style = (
            f'display:grid;grid-template-columns:{col_template};'
            f'gap:1px;background:#e2e8f0;'
            f'width:fit-content;margin:0 auto;'
        )
        grid_style = (
            f'display:grid;'
            f'grid-template-columns:{col_template};'
            f'grid-template-rows:repeat({vyska},{ch}px);'
            f'gap:1px;background:#cbd5e1;'
            f'width:fit-content;margin:0 auto;'
        )

    # Wrapper: záhlaví + grid ve sloupci
    wrap_style = (
        'display:flex;flex-direction:column;'
        + ('width:100%;height:100%;' if fill else 'margin:0 auto;flex-shrink:0;')
    )
    with outer:
        wrap = ui.element('div').style(wrap_style)
    with wrap:
        hdr  = ui.element('div').style(header_style)
        grid = ui.element('div').style(grid_style)
    hid = hdr.id
    gid = grid.id                             # DOM id gridu = c{gid}

    # Záhlaví sloupců — čísla 1…sirka
    header_cells = ''.join(
        f'<div style="text-align:center;font-size:0.33rem;font-weight:700;'
        f'color:#64748b;padding:3px 0;background:#f8fafc;">{i + 1}</div>'
        for i in range(sirka)
    )

    # ── Lepivý spodní posuvník (sticky scrollbar) – jen v normálním režimu ──────
    # V fill režimu grid nepřetéká → scrollbar není potřeba.
    if not fill:
        sticky_bar = ui.element('div').style(
            'position:sticky;bottom:0;z-index:10;'
            'overflow-x:auto;overflow-y:hidden;'
            'height:16px;width:100%;'
            'background:#f1f5f9;border-top:1px solid #e2e8f0;'
            'flex-shrink:0;'
        )
        with sticky_bar:
            sticky_inner = ui.element('div').style('height:1px;width:100%;')
        sb_id = sticky_bar.id
        si_id = sticky_inner.id

    # ── Sestavení HTML buněk (čistý string, žádné NiceGUI elementy) ──────────
    cells = []
    for r in range(vyska):
        for c in range(sirka):
            if (r, c) in absorbed:
                continue

            foch  = fochy.get((r, c))
            barva = (foch.get('barva') or '#ffffff') if foch else '#ffffff'
            nazev = (foch.get('nazev') or '') if foch else ''
            mw    = max(1, (foch.get('merge_sirka') or 1)) if foch else 1
            mh    = max(1, (foch.get('merge_vyska') or 1)) if foch else 1

            has_foto    = bool(foch and foch.get('foto_cesta'))
            has_vnorene = bool(foch and foch.get('id') in vnorene_ids)
            has_popis   = bool(foch and foch.get('popis'))
            has_nahled  = has_foto or has_popis          # eye icon – foto nebo popis
            has_detail  = has_nahled or has_vnorene
            is_empty    = not bool(foch and (nazev or has_foto or has_popis or has_vnorene))

            clickable = je_admin or has_detail
            cursor    = 'pointer' if clickable else 'default'
            border    = '1px solid #e2e8f0' if is_empty else f'1px solid {barva}'

            justify = 'flex-start' if (show_foto and has_foto) else 'center'
            cell_style = (
                f'background:{barva};'
                f'grid-column:{c+1}/span {mw};'
                f'grid-row:{r+1}/span {mh};'
                f'display:flex;flex-direction:column;'
                f'align-items:center;justify-content:{justify};'
                f'position:relative;overflow:hidden;'
                f'cursor:{cursor};border:{border};'
                f'padding:4px 2px 2px;box-sizing:border-box;'
            )

            # Pomocná funkce – onclick pro ikonu (stopPropagation + CustomEvent s action)
            def _icon_click(action, r=r, c=c):
                return (
                    f"event.stopPropagation();"
                    f"document.getElementById('c{cid}')"
                    f".dispatchEvent(new CustomEvent('pg_click',"
                    f"{{'bubbles':false,'detail':{{'r':{r},'c':{c},'action':'{action}'}}}}))"
                )

            # Cell body onclick – action='main' (pro admina = edit, pro čtenáře = auto)
            onclick = ''
            if clickable:
                onclick = (
                    f" onclick=\"if(window._pgDragging){{window._pgDragging=false;return;}}"
                    f"document.getElementById('c{cid}')"
                    f".dispatchEvent(new CustomEvent('pg_click',"
                    f"{{'bubbles':false,'detail':{{'r':{r},'c':{c},'action':'main'}}}}))\""
                )

            drag_attrs = ''
            if je_admin and on_drop:
                drag_attrs = (
                    ' draggable="true"'
                    f' ondragstart="window._pgFrom={{r:{r},c:{c}}};event.dataTransfer.effectAllowed=\'move\';this.style.opacity=\'0.4\'"'
                    ' ondragend="if(window._pgDragging){this.style.opacity=\'0\'}else{this.style.opacity=\'\'};window._pgDragging=false"'
                    ' ondragover="event.preventDefault();event.dataTransfer.dropEffect=\'move\';this.style.outline=\'2px solid #2563eb\'"'
                    ' ondragleave="this.style.outline=\'\'"'
                    f' ondrop="event.preventDefault();this.style.outline=\'\';if(window._pgFrom){{window._pgDragging=true;document.getElementById(\'c{cid}\').dispatchEvent(new CustomEvent(\'pg_drop\',{{bubbles:false,detail:{{from_r:window._pgFrom.r,from_c:window._pgFrom.c,to_r:{r},to_c:{c}}}}}));window._pgFrom=null}}"'
                )

            inner = ''
            if show_foto and has_foto:
                # Foto + text pod ním (layout pobočky)
                url = _foto_url(foch['foto_cesta'])
                inner += (
                    f'<img src="{url}" style="'
                    f'width:90%;flex:1;min-height:0;max-height:75%;'
                    f'object-fit:contain;margin-bottom:3px;flex-shrink:1">'
                )
                if nazev:
                    esc = (nazev
                           .replace('&', '&amp;').replace('<', '&lt;')
                           .replace('>', '&gt;').replace('"', '&quot;'))
                    inner += (
                        f'<span style="font-size:0.70rem;line-height:1.2;text-align:center;'
                        f'word-break:break-word;overflow:hidden;'
                        f'padding:0 3px;color:#1e293b;font-weight:600;flex-shrink:0">{esc}</span>'
                    )
            elif nazev:
                esc = (nazev
                       .replace('&', '&amp;').replace('<', '&lt;')
                       .replace('>', '&gt;').replace('"', '&quot;'))
                fsize = '0.70rem' if show_foto else '0.64rem'
                inner += (
                    f'<span style="font-size:{fsize};line-height:1.25;text-align:center;'
                    f'word-break:break-word;overflow:hidden;max-height:60%;'
                    f'padding:0 4px;color:#1e293b;font-weight:700">{esc}</span>'
                )

            # Ikony – každá má vlastní onclick s action, pointer-events:auto
            # Kontejner pg-ic má pointer-events:auto + stopPropagation na pozadí
            # Buňky s pobočkami: pravý klik otevírá pobočky (+ potlačí výchozí menu)
            icons = ''
            if has_vnorene:
                # Pouze vizuální indikátor – pravý klik na buňku obsluhuje pobočky
                icons += (
                    f'<i style="font-size:1.5rem;color:#2563eb;font-style:normal;'
                    f'pointer-events:none" title="Pobočky (pravé tlačítko)">&#9783;</i>'
                )
            if has_nahled:
                icons += (
                    f'<i style="font-size:1.5rem;color:#059669;font-style:normal;'
                    f'cursor:pointer;pointer-events:auto"'
                    f' title="Náhled"'
                    f' onclick="{_icon_click("detail")}">&#128065;</i>'
                )
            if je_admin:
                icons += (
                    f'<i style="font-size:1.5rem;color:#6b7280;font-style:normal;'
                    f'cursor:pointer;pointer-events:auto"'
                    f' title="Upravit"'
                    f' onclick="{_icon_click("edit")}">&#9998;</i>'
                )

            # oncontextmenu: potlač výchozí menu; buňky s pobočkami navíc pošlou event
            if has_vnorene:
                contextmenu = (
                    f' oncontextmenu="event.preventDefault();event.stopPropagation();'
                    f'document.getElementById(\'c{cid}\')'
                    f'.dispatchEvent(new CustomEvent(\'pg_click\','
                    f'{{bubbles:false,detail:{{r:{r},c:{c},action:\'pobocky\'}}}}))"'
                )
            else:
                contextmenu = ' oncontextmenu="event.preventDefault()"'

            hover_attrs = ''
            if icons:
                inner += (
                    '<div class="pg-ic" style="position:absolute;top:4px;right:4px;'
                    'display:none;gap:3px;pointer-events:auto"'
                    ' onclick="event.stopPropagation()"'
                    ' ondragstart="event.stopPropagation()">'
                    + icons + '</div>'
                )
                hover_attrs = (
                    " onmouseenter=\"var i=this.querySelector('.pg-ic');"
                    "if(i)i.style.display='flex'\""
                    " onmouseleave=\"var i=this.querySelector('.pg-ic');"
                    "if(i)i.style.display='none'\""
                )

            cells.append(
                f'<div style="{cell_style}"{onclick}{contextmenu}{hover_attrs}{drag_attrs}>{inner}</div>'
            )

    # ── Injekce buněk přes JS přímo do DOM (mimo Vue) ────────────────────────
    # document.createElement('div') + innerHTML → tmpl.childNodes → appendChild
    # zachová onclick/onmouseenter atributy jako živé event handlery.
    # Zároveň propojí lepivý posuvník (sticky_bar) s outer scroll kontejnerem.
    cells_json  = json.dumps(''.join(cells))
    header_json = json.dumps(header_cells)
    if fill:
        # Fill režim: injekce záhlaví + buněk, bez scrollbar logiky
        ui.run_javascript(f'''
            (function inject() {{
                var h = document.getElementById('c{hid}');
                var g = document.getElementById('c{gid}');
                if (!h || !g) {{ setTimeout(inject, 60); return; }}
                var th = document.createElement('div');
                th.innerHTML = {header_json};
                while (th.firstChild) h.appendChild(th.firstChild);
                var tmp = document.createElement('div');
                tmp.innerHTML = {cells_json};
                while (tmp.firstChild) g.appendChild(tmp.firstChild);
            }})();
        ''')
    else:
        ui.run_javascript(f'''
            (function inject() {{
                var h   = document.getElementById('c{hid}');
                var g   = document.getElementById('c{gid}');
                var box = document.getElementById('c{cid}');
                var bar = document.getElementById('c{sb_id}');
                var inn = document.getElementById('c{si_id}');
                if (!h || !g || !box || !bar || !inn) {{ setTimeout(inject, 60); return; }}

                // Injekce záhlaví sloupců
                var th = document.createElement('div');
                th.innerHTML = {header_json};
                while (th.firstChild) h.appendChild(th.firstChild);

                // Injekce buněk
                var tmp = document.createElement('div');
                tmp.innerHTML = {cells_json};
                while (tmp.firstChild) g.appendChild(tmp.firstChild);

                // Schovat nativní scrollbar na box (scroll funkce zůstane)
                box.style.scrollbarWidth = 'none';
                box.style.msOverflowStyle = 'none';
                var st = document.createElement('style');
                st.textContent = '#c{cid}::-webkit-scrollbar{{display:none}}';
                document.head.appendChild(st);

                // Synchronizace posuvníku
                setTimeout(function() {{
                    var sw = box.scrollWidth;
                    var cw = box.clientWidth;
                    if (sw > cw) {{
                        inn.style.width = sw + 'px';
                        var lock = false;
                        bar.addEventListener('scroll', function() {{
                            if (!lock) {{ lock = true; box.scrollLeft = bar.scrollLeft; lock = false; }}
                        }});
                        box.addEventListener('scroll', function() {{
                            if (!lock) {{ lock = true; bar.scrollLeft = box.scrollLeft; lock = false; }}
                        }});
                    }} else {{
                        bar.style.display = 'none';
                    }}
                }}, 120);
            }})();
        ''')


# ═══════════════════════════════════════════════════════════════════════════════
# HLAVNÍ UI
# ═══════════════════════════════════════════════════════════════════════════════
# PDF EXPORT – sestaví HTML stránku s tabulkou plánogramu
# ═══════════════════════════════════════════════════════════════════════════════
def _foto_base64_data_url(cesta):
    """Přečte fotku z disku a vrátí data: URL pro vložení do HTML. None při chybě."""
    if not cesta or not os.path.exists(cesta):
        return None
    try:
        ext  = os.path.splitext(cesta)[1].lower().lstrip('.')
        mime = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png',
                'gif': 'gif', 'webp': 'webp'}.get(ext, 'jpeg')
        with open(cesta, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
        return f'data:image/{mime};base64,{data}'
    except Exception:
        return None


def _build_planogram_html(titulek, popis_str, fochy, absorbed,
                           sirka=None, vyska=None, show_foto=False):
    """
    Vrátí kompletní HTML string pro tisk/PDF.
    Vizuálně 1:1 shodný s gridu v prohlížeči – stejné rozměry buněk,
    fonty, barvy, mezery a záhlaví řádků/sloupců.
    Parametry sirka/vyska: výchozí GRID_S/GRID_V pro hlavní grid,
                           nebo SUB_S/SUB_V pro sub-grid pobočky.
    show_foto: True = zobrazit fotky (sub-grid), False = jen barva+text (hlavní grid)
    """
    vytisteno = datetime.now().strftime('%d.%m.%Y %H:%M')
    lay_nazev = titulek

    sirka = (sirka if sirka is not None else GRID_S)
    vyska = (vyska if vyska is not None else GRID_V)
    cw    = CELL_W   # 158 px
    ch    = CELL_H   # 173 px
    gap   = 1        # px mezera mezi buňkami (background #cbd5e1)

    # ── Záhlaví sloupců – přesná kopie _render_grid ──────────────────────────
    row_num_w = 28  # px – šířka sloupce s čísly řádků
    hdr_divs = ''.join(
        f'<div style="text-align:center;font-size:0.65rem;font-weight:700;'
        f'color:#64748b;padding:3px 0;background:#f8fafc;'
        f'width:{cw}px;flex-shrink:0">{i + 1}</div>'
        for i in range(sirka)
    )
    hdr_html = (
        f'<div style="display:flex;flex-direction:row;gap:{gap}px;'
        f'background:#e2e8f0;width:fit-content">'
        f'{hdr_divs}</div>'
    )

    # ── Buňky gridu ──────────────────────────────────────────────────────────
    cells_html = ''
    for r in range(vyska):
        for c in range(sirka):
            if (r, c) in absorbed:
                continue

            foch    = fochy.get((r, c))
            barva   = (foch.get('barva') or '#ffffff') if foch else '#ffffff'
            nazev_b = (foch.get('nazev') or '')        if foch else ''
            cesta   = (foch.get('foto_cesta') or '')   if foch else ''
            mw = max(1, (foch.get('merge_sirka') or 1)) if foch else 1
            mh = max(1, (foch.get('merge_vyska') or 1)) if foch else 1

            has_foto = bool(show_foto and cesta)
            is_empty = not bool(foch and (nazev_b or has_foto))
            border   = '1px solid #e2e8f0' if is_empty else f'1px solid {barva}'
            justify  = 'flex-start' if has_foto else 'center'

            cell_style = (
                f'background:{barva};'
                f'grid-column:{c + 1}/span {mw};'
                f'grid-row:{r + 1}/span {mh};'
                f'display:flex;flex-direction:column;'
                f'align-items:center;justify-content:{justify};'
                f'overflow:hidden;border:{border};'
                f'padding:4px 2px 2px;box-sizing:border-box;'
            )

            inner = ''
            if has_foto:
                # Foto + text pod ním – přesná kopie show_foto=True větve
                data_url = _foto_base64_data_url(cesta)
                if data_url:
                    inner += (
                        f'<img src="{data_url}" style="'
                        f'width:90%;flex:1;min-height:0;max-height:75%;'
                        f'object-fit:contain;margin-bottom:3px;flex-shrink:1">'
                    )
                if nazev_b:
                    esc = (nazev_b
                           .replace('&', '&amp;').replace('<', '&lt;')
                           .replace('>', '&gt;'))
                    inner += (
                        f'<span style="font-size:0.72rem;line-height:1.2;'
                        f'text-align:center;word-break:break-word;overflow:hidden;'
                        f'padding:0 3px;color:#1e293b;font-weight:600;'
                        f'flex-shrink:0">{esc}</span>'
                    )
            elif nazev_b:
                esc = (nazev_b
                       .replace('&', '&amp;').replace('<', '&lt;')
                       .replace('>', '&gt;'))
                fsize = '0.70rem' if show_foto else '0.64rem'
                inner = (
                    f'<span style="font-size:{fsize};line-height:1.25;'
                    f'text-align:center;word-break:break-word;overflow:hidden;'
                    f'max-height:60%;padding:0 4px;color:#1e293b;'
                    f'font-weight:700">{esc}</span>'
                )

            cells_html += f'<div style="{cell_style}">{inner}</div>'

    # ── Čísla řádků (vlevo od gridu) ─────────────────────────────────────────
    row_nums_html = ''.join(
        f'<div style="height:{ch}px;display:flex;align-items:center;'
        f'justify-content:flex-end;padding-right:6px;'
        f'font-size:0.65rem;font-weight:700;color:#64748b;'
        f'flex-shrink:0;box-sizing:border-box">{r + 1}</div>'
        for r in range(vyska)
    )
    row_nums_col = (
        f'<div style="display:flex;flex-direction:column;gap:{gap}px;'
        f'width:{row_num_w}px;flex-shrink:0;background:#f8fafc;'
        f'margin-top:{gap}px">'
        f'{row_nums_html}</div>'
    )

    grid_w = sirka * cw + (sirka - 1) * gap
    grid_h = vyska * ch + (vyska - 1) * gap

    grid_div = (
        f'<div style="display:grid;'
        f'grid-template-columns:repeat({sirka},{cw}px);'
        f'grid-template-rows:repeat({vyska},{ch}px);'
        f'gap:{gap}px;background:#cbd5e1;'
        f'width:{grid_w}px;height:{grid_h}px;flex-shrink:0">'
        f'{cells_html}</div>'
    )

    total_w = row_num_w + grid_w

    html = f'''<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<title>{lay_nazev}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;
   -webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{font-family:Arial,Helvetica,sans-serif;color:#1e293b;background:#fff}}
.top{{padding:12px 16px 10px}}
h1{{font-size:16px;font-weight:900;color:#1e3a5f;margin-bottom:3px}}
.sub{{font-size:10px;color:#6b7280}}
.btn{{background:#1d4ed8;color:#fff;border:none;padding:7px 18px;border-radius:6px;
      font-size:11px;font-weight:700;cursor:pointer;margin-bottom:10px;
      display:inline-block}}
.wrap{{padding:0 16px 24px;overflow:auto}}
.grid-outer{{display:flex;flex-direction:column;width:{total_w}px}}
.grid-body{{display:flex;flex-direction:row}}
@media print{{
  .btn{{display:none}}
  .wrap{{padding:0;overflow:visible}}
  @page{{size:{total_w + 48}px {grid_h + 120}px;margin:16px}}
}}
</style>
</head>
<body>
<div class="top">
  <button class="btn" onclick="window.print()">🖨️ Tisk / Uložit jako PDF</button>
  <h1>{lay_nazev}</h1>
  <p class="sub">{popis_str} &nbsp;·&nbsp; Vytištěno: {vytisteno}</p>
</div>
<div class="wrap">
  <div class="grid-outer">
    <div style="margin-left:{row_num_w}px">{hdr_html}</div>
    <div class="grid-body">
      {row_nums_col}
      {grid_div}
    </div>
  </div>
</div>
</body>
</html>'''
    return html


# ═══════════════════════════════════════════════════════════════════════════════
@ui.refreshable
def vykresli_planogram(user_id, user_name, vsechna_prava):
    _init_db()
    os.makedirs(FOTO_DIR, exist_ok=True)

    je_admin   = 'vse' in vsechna_prava or PERM_ADMIN in vsechna_prava
    ma_pristup = je_admin or PERM_PRISTUP in vsechna_prava

    if not ma_pristup:
        with ui.column().classes('w-full items-center py-24 gap-4'):
            ui.icon('lock').classes('text-5xl text-gray-300')
            ui.label('Nemáte přístup k plánogramu tabákových výrobků.') \
                .classes('text-xl text-gray-500')
        return

    # Sdílené dialogy (jednou pro celý modul)
    ctx, fn_edit, fn_detail, fn_pobocky = _build_edit_dialog(je_admin)

    with ui.column().classes('w-full gap-0'):

        # Záhlaví
        with ui.row().classes('w-full items-center justify-between '
                              'px-6 py-4 border-b border-gray-200 flex-shrink-0'):
            with ui.column().classes('gap-0'):
                ui.label('Plánogram tabákových výrobků') \
                    .classes('text-2xl font-black text-blue-900')
                ui.label('CC / MO — rozmístění výrobků na stojánkách a dřevěnkách') \
                    .classes('text-sm text-gray-500')
            if je_admin:
                ui.button(
                    icon='send', text='Rozeslat aktualizaci',
                    on_click=lambda: (
                        threading.Thread(
                            target=_notifikuj_pristupove, args=(user_name,),
                            daemon=True).start(),
                        ui.notify(
                            '✅ Notifikace rozeslána všem uživatelům s přístupem.',
                            type='positive')
                    )
                ).classes('bg-blue-600 text-white font-bold shadow-md px-5')

        # Záložky layoutů
        with ui.tabs(value='OHU').classes(
                'w-full bg-white border-b border-gray-200 px-4 flex-shrink-0') as tabs:
            for lay in LAYOUTS:
                ui.tab(lay['id'], label=lay['nazev'])

        with ui.tab_panels(tabs, value='OHU').classes('w-full'):
            for lay in LAYOUTS:
                with ui.tab_panel(lay['id']).classes('p-0'):
                    _vykresli_layout(lay, user_id, user_name, je_admin,
                                     ctx, fn_edit, fn_detail, fn_pobocky)


# ─────────────────────────────────────────────────────────────────────────────
def _vykresli_layout(lay, user_id, user_name, je_admin,
                     ctx, fn_edit, fn_detail, fn_pobocky):
    layout_id = lay['id']

    # Zoom stav – uchováváme mimo refreshable, aby přežil refresh gridu
    _zoom = {'factor': 1.0}
    _ZOOM_KROKY = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
                   1.1, 1.25, 1.5, 1.75, 2.0]

    @ui.refreshable
    def _grid():
        fochy, vnorene_ids = _nacti_fochy(layout_id)
        absorbed = _absorbovane(fochy, GRID_S, GRID_V)

        def _klik(r, c, foch, action='main'):
            # Ikona Náhled (👁) – vždy otevře detail dialog, i pro admina
            if action == 'detail':
                if foch and (foch.get('popis') or foch.get('foto_cesta')):
                    fn_detail(foch)
                return
            # Ikona Pobočky
            if action == 'pobocky':
                if foch:
                    fn_pobocky(foch)
                return
            # Ikona Upravit (tužka) – jen admin
            if action == 'edit':
                if je_admin:
                    ctx['refresh_grid'] = _grid.refresh
                    fn_edit(layout_id, r, c, foch)
                return
            # Klik na tělo buňky (action == 'main')
            if je_admin:
                ctx['refresh_grid'] = _grid.refresh
                fn_edit(layout_id, r, c, foch)
            else:
                if foch:
                    if foch.get('id') in vnorene_ids:
                        fn_pobocky(foch)
                    elif foch.get('popis') or foch.get('foto_cesta'):
                        fn_detail(foch)

        def _drop(from_r, from_c, to_r, to_c):
            if _swap_fochy(layout_id, from_r, from_c, to_r, to_c):
                _grid.refresh()
            else:
                ui.notify('Přesunutí se nezdařilo.', type='negative')

        z = _zoom['factor']
        _render_grid(
            fochy       = fochy,
            absorbed    = absorbed,
            vnorene_ids = vnorene_ids,
            sirka       = GRID_S,
            vyska       = GRID_V,
            je_admin    = je_admin,
            on_click    = _klik,
            on_drop     = _drop if je_admin else None,
            cell_w      = max(20, int(CELL_W * z)),
            cell_h      = max(20, int(CELL_H * z)),
        )

    @ui.refreshable
    def _komentare():
        komentare = _nacti_komentare(layout_id)
        if not komentare:
            ui.label('Zatím žádné komentáře.') \
                .classes('text-sm text-gray-400 italic')
            return
        with ui.column().classes('w-full gap-2'):
            for k in komentare:
                with ui.row().classes('gap-2 items-start'):
                    ui.label(k['user_jmeno'] or '?') \
                        .classes('text-xs font-bold text-blue-700 w-32 flex-shrink-0')
                    ui.label(k['text']) \
                        .classes('text-sm text-gray-700 flex-1 whitespace-pre-wrap')
                    ui.label(_fmt_datum(k['datum'])) \
                        .classes('text-xs text-gray-400 flex-shrink-0')

    def _zoom_out():
        idx = _ZOOM_KROKY.index(
            min(_ZOOM_KROKY, key=lambda x: abs(x - _zoom['factor'])))
        if idx > 0:
            _zoom['factor'] = _ZOOM_KROKY[idx - 1]
            _zoom_bar.refresh()
            _grid.refresh()

    def _zoom_in():
        idx = _ZOOM_KROKY.index(
            min(_ZOOM_KROKY, key=lambda x: abs(x - _zoom['factor'])))
        if idx < len(_ZOOM_KROKY) - 1:
            _zoom['factor'] = _ZOOM_KROKY[idx + 1]
            _zoom_bar.refresh()
            _grid.refresh()

    def _zoom_reset():
        _zoom['factor'] = 1.0
        _zoom_bar.refresh()
        _grid.refresh()

    async def _nahled_pdf():
        fochy_pdf, _ = _nacti_fochy(layout_id)
        absorbed_pdf  = _absorbovane(fochy_pdf, GRID_S, GRID_V)
        html     = _build_planogram_html(
            titulek   = f'Plánogram – {lay["nazev"]}',
            popis_str = lay['popis'],
            fochy     = fochy_pdf,
            absorbed  = absorbed_pdf,
        )
        html_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')
        await ui.run_javascript(f'''
            var b64  = "{html_b64}";
            var bin  = atob(b64);
            var arr  = new Uint8Array(bin.length);
            for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            var blob = new Blob([arr], {{type:"text/html;charset=utf-8"}});
            var url  = URL.createObjectURL(blob);
            var w    = window.open(url, "_blank");
            if (!w) alert("Povolte prosím vyskakovací okna pro tisk/export.");
        ''')

    with ui.column().classes('w-full gap-4 p-5'):

        # Popis layoutu + zoom ovládání + PDF
        with ui.row().classes('items-center justify-between gap-2 flex-wrap'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('info_outline').classes('text-blue-400 text-xl')
                ui.label(lay['popis']).classes(
                    'text-sm font-semibold text-blue-800 '
                    'bg-blue-50 border border-blue-200 px-4 py-2 rounded-lg')

            with ui.row().classes('items-center gap-2'):
                # PDF tlačítko
                ui.button(icon='picture_as_pdf', on_click=_nahled_pdf) \
                    .props('flat round') \
                    .classes('text-red-500 hover:bg-red-50') \
                    .tooltip('Tisk / Export do PDF')

            # Zoom tlačítka (refreshable kvůli aktualizaci procent)
            @ui.refreshable
            def _zoom_bar():
                is_min = _zoom['factor'] <= _ZOOM_KROKY[0]
                is_max = _zoom['factor'] >= _ZOOM_KROKY[-1]
                with ui.row().classes('items-center gap-0 '
                                      'border border-gray-200 rounded-lg overflow-hidden'):
                    ui.button(icon='remove', on_click=_zoom_out) \
                        .props('flat dense') \
                        .classes('h-8 w-8 rounded-none ' +
                                 ('text-gray-200 cursor-default' if is_min
                                  else 'text-gray-500 hover:bg-gray-100'))
                    ui.label(f'{int(_zoom["factor"] * 100)} %') \
                        .classes('text-xs font-bold text-gray-600 w-12 text-center '
                                 'cursor-pointer select-none border-x border-gray-200 h-8 '
                                 'flex items-center justify-center') \
                        .style('display:flex;align-items:center;justify-content:center') \
                        .on('click', lambda: _zoom_reset()) \
                        .tooltip('Resetovat na 100 %')
                    ui.button(icon='add', on_click=_zoom_in) \
                        .props('flat dense') \
                        .classes('h-8 w-8 rounded-none ' +
                                 ('text-gray-200 cursor-default' if is_max
                                  else 'text-gray-500 hover:bg-gray-100'))

            _zoom_bar()

        if je_admin:
            ui.label('Klikněte na políčko pro úpravu. Přetáhněte barvu pro označení.') \
                .classes('text-xs text-gray-400 -mt-2')

        # Grid
        _grid()

        ui.separator().classes('my-2')

        # Komentáře
        with ui.column().classes('w-full gap-2'):
            ui.label('Komentáře').classes(
                'font-bold text-xs text-gray-500 uppercase tracking-widest')
            _komentare()

            # Přidat komentář
            with ui.row().classes('w-full gap-2 items-center mt-1'):
                kom_input = ui.input(
                    placeholder='Přidejte komentář…'
                ).classes('flex-1').props('outlined dense')

                def _odeslat_kom(_lid=layout_id):
                    text = (kom_input.value or '').strip()
                    if not text:
                        return
                    _pridej_komentar(_lid, user_id, user_name, text)
                    kom_input.set_value('')
                    _komentare.refresh()

                kom_input.on('keydown.enter', lambda e, fn=_odeslat_kom: fn())
                ui.button(icon='send', on_click=_odeslat_kom) \
                    .props('flat round').classes('text-blue-500')
