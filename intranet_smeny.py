from nicegui import ui, app
import intranet_data, intranet_logger
import datetime
import asyncio
import base64
import calendar as cal_lib

# ==========================================
# KONSTANTY
# ==========================================
BARVY_SMENY = [
    ('#2563eb', 'Modrá'),   ('#dc2626', 'Červená'), ('#15803d', 'Zelená'),
    ('#ea580c', 'Oranžová'),('#7c3aed', 'Fialová'), ('#0e7490', 'Tyrkysová'),
    ('#db2777', 'Růžová'),  ('#a16207', 'Zlatá'),
]

def _barva_uzivatele(user_id):
    """Deterministická barva zaměstnance z palety BARVY_SMENY."""
    return BARVY_SMENY[int(user_id) % len(BARVY_SMENY)][0]

_ABSENCE_STYL_MAP = {
    'dovolená':   ('#f59e0b', '🏖'),
    'lékař':      ('#0891b2', '🩺'),
    'nemoc':      ('#ef4444', '🤒'),
    'ošetřování': ('#8b5cf6', '👶'),
}

# Typy absencí viditelné řadovým členům oddělení; ostatní typy cizích absencí
# se řadovému členovi maskují jako obecná "Absence" (vedoucí vidí vše).
VEREJNE_TYPY_ABSENCI = {'dovolená', 'homeoffice'}

def _abs_styl(typ):
    t = (typ or '').lower()
    for klic, hodnota in _ABSENCE_STYL_MAP.items():
        if klic in t:
            return hodnota
    return ('#6b7280', '📋')

DNY = ['Po', 'Út', 'St', 'Čt', 'Pá', 'So', 'Ne']
MESICE = ['', 'Leden', 'Únor', 'Březen', 'Duben', 'Květen', 'Červen',
          'Červenec', 'Srpen', 'Září', 'Říjen', 'Listopad', 'Prosinec']
MESICE_2 = ['', 'ledna', 'února', 'března', 'dubna', 'května', 'června',
            'července', 'srpna', 'září', 'října', 'listopadu', 'prosince']

_DB_INIT = False

# ==========================================
# DB INICIALIZACE
# ==========================================
def _init_db():
    global _DB_INIT
    if _DB_INIT:
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    tabulky = [
        """CREATE TABLE IF NOT EXISTS smeny_skupiny (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nazev VARCHAR(100) NOT NULL UNIQUE,
            ikona VARCHAR(10) DEFAULT '📋',
            oddeleni_nazev VARCHAR(100) DEFAULT '',
            zapnuty BOOLEAN DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS smeny_smena (
            id INT AUTO_INCREMENT PRIMARY KEY,
            skupina_id INT NOT NULL,
            nazev VARCHAR(100) NOT NULL,
            datum DATE NOT NULL,
            cas_od TIME NOT NULL,
            cas_do TIME NOT NULL,
            popis TEXT,
            barva VARCHAR(20) DEFAULT '#3b82f6',
            created_by VARCHAR(100),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_sk_datum (skupina_id, datum),
            FOREIGN KEY (skupina_id) REFERENCES smeny_skupiny(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS smeny_prirazeni (
            id INT AUTO_INCREMENT PRIMARY KEY,
            smena_id INT NOT NULL,
            user_id INT NOT NULL,
            user_name VARCHAR(100),
            created_by VARCHAR(100),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_su (smena_id, user_id),
            INDEX idx_uid (user_id),
            FOREIGN KEY (smena_id) REFERENCES smeny_smena(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS smeny_uvazek (
            id INT AUTO_INCREMENT PRIMARY KEY,
            skupina_id INT NOT NULL,
            user_id INT NOT NULL,
            rezim VARCHAR(20) DEFAULT 'plny',
            uvazek_pct INT DEFAULT 100,
            updated_by VARCHAR(100),
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_sk_u (skupina_id, user_id),
            FOREIGN KEY (skupina_id) REFERENCES smeny_skupiny(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS smeny_fond_override (
            id INT AUTO_INCREMENT PRIMARY KEY,
            skupina_id INT NOT NULL,
            user_id INT NOT NULL,
            rok INT NOT NULL,
            mesic INT NOT NULL,
            fond_hodin DECIMAL(6,2) NOT NULL,
            duvod TEXT NOT NULL,
            updated_by VARCHAR(100),
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_fond (skupina_id, user_id, rok, mesic),
            FOREIGN KEY (skupina_id) REFERENCES smeny_skupiny(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    ]
    vsechny_ok = True
    try:
        cur = conn.cursor()
        for sql in tabulky:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                print(f'[Směny] DB init chyba: {e}')
                vsechny_ok = False
        if vsechny_ok:
            _DB_INIT = True
    except Exception as e:
        print(f'[Směny] DB init: {e}')
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ==========================================
# DB HELPERY
# ==========================================
def _skupiny_vsechny():
    conn = intranet_data.get_db_connection()
    if not conn: return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM smeny_skupiny ORDER BY nazev ASC")
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _ziskej_nebo_vytvor_skupinu(oddeleni_nazev):
    """Vrátí skupinu pro dané oddělení. Pokud neexistuje, automaticky ji vytvoří."""
    conn = intranet_data.get_db_connection()
    if not conn: return None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM smeny_skupiny WHERE oddeleni_nazev = %s", (oddeleni_nazev,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            "INSERT INTO smeny_skupiny (nazev, ikona, oddeleni_nazev, zapnuty) VALUES (%s, %s, %s, 1)",
            (oddeleni_nazev, '📋', oddeleni_nazev)
        )
        conn.commit()
        new_id = cur.lastrowid
        return {'id': new_id, 'nazev': oddeleni_nazev, 'ikona': '📋',
                'oddeleni_nazev': oddeleni_nazev, 'zapnuty': 1}
    except Exception as e:
        print(f'[Směny] _ziskej_nebo_vytvor_skupinu: {e}')
        return None
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _skupiny_pro_uzivatele(user_id, vsechna_prava):
    """
    Vrátí seznam skupin dostupných pro daného uživatele:
    - admin/smeny_admin  → všechna oddělení (může plánovat)
    - hlavni_vedouci_{odd} → jen oddělení kde je vedoucí (může plánovat)
    - smeny_vedouci      → oddělení, jejichž je členem (může plánovat)
    - ostatní            → oddělení, jejichž jsou členem (pouze zobrazení)
    """
    is_admin = 'vse' in vsechna_prava or 'smeny_admin' in vsechna_prava
    je_ved_smeny = 'smeny_vedouci' in vsechna_prava
    vsechna_oddeleni = intranet_data.ziskej_vsechna_oddeleni()
    viditelne_oddeleni = []

    if is_admin:
        viditelne_oddeleni = list(vsechna_oddeleni.keys())
    else:
        # hlavni_vedouci_{odd} → zobrazí a může plánovat konkrétní oddělení
        for dept_name in vsechna_oddeleni.keys():
            klic = f'hlavni_vedouci_{dept_name.lower()}'
            if klic in vsechna_prava:
                viditelne_oddeleni.append(dept_name)

        # smeny_vedouci → zobrazí a může plánovat vlastní oddělení (podle členství)
        if je_ved_smeny:
            for dept_name in vsechna_oddeleni.keys():
                if dept_name not in viditelne_oddeleni:
                    lide = _lide_skupiny(dept_name)
                    if user_id in lide:
                        viditelne_oddeleni.append(dept_name)
        elif not viditelne_oddeleni:
            # Řadový zaměstnanec – pouze zobrazení svých směn
            for dept_name in vsechna_oddeleni.keys():
                lide = _lide_skupiny(dept_name)
                if user_id in lide:
                    viditelne_oddeleni.append(dept_name)

    skupiny = []
    for dept_name in sorted(viditelne_oddeleni):
        sk = _ziskej_nebo_vytvor_skupinu(dept_name)
        if sk and sk.get('zapnuty', 1):
            skupiny.append(sk)
    return skupiny

def _je_vedouci(vsechna_prava, oddeleni_nazev):
    if 'vse' in vsechna_prava or 'smeny_admin' in vsechna_prava:
        return True
    if 'smeny_vedouci' in vsechna_prava:
        return True
    if not oddeleni_nazev:
        return False
    klic = f'hlavni_vedouci_{oddeleni_nazev.lower()}'
    return klic in vsechna_prava

def _lide_skupiny(oddeleni_nazev):
    """Vrátí {id: jmeno} dict aktivních lidí v oddělení."""
    conn = intranet_data.get_db_connection()
    if not conn: return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        if oddeleni_nazev:
            cur.execute(f"""
                SELECT u.iduser, CONCAT(u.name, ' ', u.surname) AS jmeno
                FROM user u
                JOIN department_To_user dtu ON u.iduser = dtu.user_iduser
                JOIN department d ON dtu.department_iddepartment = d.iddepartment
                WHERE d.name = %s AND u.is_active = 1
                  AND u.iduser <> {intranet_data.SKRYTY_ADMIN_ID}
                ORDER BY u.name, u.surname
            """, (oddeleni_nazev,))
        else:
            cur.execute(f"""
                SELECT iduser, CONCAT(name, ' ', surname) AS jmeno
                FROM user WHERE is_active = 1
                  AND iduser <> {intranet_data.SKRYTY_ADMIN_ID}
                ORDER BY name, surname
            """)
        return {r['iduser']: r['jmeno'] for r in cur.fetchall()}
    except Exception as e:
        print(f'[Směny] _lide_skupiny: {e}')
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _smeny_rozsah(skupina_id, datum_od_str, datum_do_str, user_id_filtr=None):
    """Vrátí dict {datum_str: [smena_dict, ...]} pro dané rozmezí dat."""
    conn = intranet_data.get_db_connection()
    if not conn: return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        if user_id_filtr:
            cur.execute("""
                SELECT s.id, s.nazev, s.datum, s.cas_od, s.cas_do, s.popis, s.barva,
                       GROUP_CONCAT(CONCAT(p.user_id, '\x01', IFNULL(p.user_name,'')) SEPARATOR '\x02') AS prirazeni
                FROM smeny_smena s
                JOIN smeny_prirazeni p ON s.id = p.smena_id
                WHERE s.skupina_id = %s AND s.datum BETWEEN %s AND %s AND p.user_id = %s
                GROUP BY s.id ORDER BY s.datum, s.cas_od
            """, (skupina_id, datum_od_str, datum_do_str, user_id_filtr))
        else:
            cur.execute("""
                SELECT s.id, s.nazev, s.datum, s.cas_od, s.cas_do, s.popis, s.barva,
                       GROUP_CONCAT(CONCAT(p.user_id, '\x01', IFNULL(p.user_name,'')) SEPARATOR '\x02') AS prirazeni
                FROM smeny_smena s
                LEFT JOIN smeny_prirazeni p ON s.id = p.smena_id
                WHERE s.skupina_id = %s AND s.datum BETWEEN %s AND %s
                GROUP BY s.id ORDER BY s.datum, s.cas_od
            """, (skupina_id, datum_od_str, datum_do_str))

        vysledek = {}
        for r in cur.fetchall():
            d = str(r['datum'])
            r['lide'] = []
            if r['prirazeni']:
                for cast in r['prirazeni'].split('\x02'):
                    if '\x01' in cast:
                        uid_s, jm = cast.split('\x01', 1)
                        try:
                            r['lide'].append({'user_id': int(uid_s), 'user_name': jm})
                        except Exception:
                            pass
            r['cas_od_fmt'] = _fmt_cas(r['cas_od'])
            r['cas_do_fmt'] = _fmt_cas(r['cas_do'])
            vysledek.setdefault(d, []).append(r)
        return vysledek
    except Exception as e:
        print(f'[Směny] _smeny_rozsah: {e}')
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _smeny_den_detail(skupina_id, datum_str):
    """Vrátí detail směn dne s přiřazenými osobami (agregované)."""
    conn = intranet_data.get_db_connection()
    if not conn: return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT s.id, s.nazev, s.datum, s.cas_od, s.cas_do, s.popis, s.barva,
                   p.user_id, p.user_name
            FROM smeny_smena s
            LEFT JOIN smeny_prirazeni p ON s.id = p.smena_id
            WHERE s.skupina_id = %s AND s.datum = %s
            ORDER BY s.cas_od, s.id, p.user_name
        """, (skupina_id, datum_str))
        smeny = {}
        for r in cur.fetchall():
            sid = r['id']
            if sid not in smeny:
                r['lide'] = []
                r['cas_od_fmt'] = _fmt_cas(r['cas_od'])
                r['cas_do_fmt'] = _fmt_cas(r['cas_do'])
                smeny[sid] = r
            if r['user_id']:
                smeny[sid]['lide'].append({'user_id': r['user_id'], 'user_name': r['user_name']})
        return list(smeny.values())
    except Exception as e:
        print(f'[Směny] _smeny_den_detail: {e}')
        return []
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _absence_rozsah(datum_od_str, datum_do_str, user_ids=None, maskovat_krome=None):
    """Vrátí dict {datum_str: [{user_id, user_name, typ}, ...]} pro schválené absence v rozsahu.

    maskovat_krome: user_id diváka-řadového člena — neveřejné typy cizích absencí
    se nahradí obecným typem 'Absence'. None = bez maskování (vedoucí)."""
    conn = intranet_data.get_db_connection()
    if not conn: return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        q = """
            SELECT lr.user_iduser, CONCAT(u.name, ' ', u.surname) AS jmeno,
                   t.name AS typ, lr.`from` AS od, lr.`to` AS do_
            FROM leaveRequest lr
            JOIN user u ON lr.user_iduser = u.iduser
            JOIN typeOfLeave t ON lr.typeOfLeave_idtypeOfLeave = t.idtypeOfLeave
            WHERE lr.leaveStatus_idleaveStatus = 2
              AND lr.`from` <= %s AND lr.`to` >= %s
        """
        params = [datum_do_str, datum_od_str]
        if user_ids:
            placeholders = ','.join('%s' for _ in user_ids)
            q += f" AND lr.user_iduser IN ({placeholders})"
            params.extend(user_ids)
        cur.execute(q, params)
        vysledek = {}
        d_od = datetime.date.fromisoformat(datum_od_str)
        d_do = datetime.date.fromisoformat(datum_do_str)
        for r in cur.fetchall():
            if maskovat_krome is not None and r['user_iduser'] != maskovat_krome \
                    and (r['typ'] or '').strip().lower() not in VEREJNE_TYPY_ABSENCI:
                r['typ'] = 'Absence'
            a_od = r['od'] if isinstance(r['od'], datetime.date) else datetime.date.fromisoformat(str(r['od']))
            a_do = r['do_'] if isinstance(r['do_'], datetime.date) else datetime.date.fromisoformat(str(r['do_']))
            d = max(a_od, d_od)
            konec = min(a_do, d_do)
            while d <= konec:
                vysledek.setdefault(str(d), []).append({
                    'user_id': r['user_iduser'],
                    'user_name': r['jmeno'],
                    'typ': r['typ'],
                })
                d += datetime.timedelta(days=1)
        return vysledek
    except Exception as e:
        print(f'[Směny] _absence_rozsah: {e}')
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _fmt_cas(t):
    """Bezpečně naformátuje MySQL TIME (timedelta nebo time) na HH:MM."""
    if not t:
        return '?'
    if isinstance(t, datetime.timedelta):
        s = int(t.total_seconds())
        return f'{s // 3600:02d}:{(s % 3600) // 60:02d}'
    return str(t)[:5]

def _fmt_cas_kratky(t):
    """Kompaktní čas pro úzké buňky: 07:00→'7', 15:30→'15:30'."""
    s = _fmt_cas(t)
    if s == '?' or ':' not in s:
        return s
    hh, mm = s.split(':')
    h = str(int(hh))
    return h if mm == '00' else f'{h}:{mm}'

def _mapa_barev(user_ids):
    """Vrátí {user_id: barva} – každému člověku jednoznačnou, dobře odlišitelnou
    barvu. Odstíny se rovnoměrně rozloží podle skutečného počtu osob, takže
    nesplývají ať je v oddělení libovolný počet lidí."""
    import colorsys
    serazene = sorted({int(u) for u in user_ids})
    n = max(1, len(serazene))
    mapa = {}
    for i, uid in enumerate(serazene):
        h = ((210 + i * 360.0 / n) % 360) / 360.0
        # střídání sytosti/světlosti zvyšuje kontrast mezi sousedy,
        # světlost držíme nízko, aby bílý text v buňce zůstal čitelný
        s = 0.70 if i % 2 == 0 else 0.85
        l = 0.46 if i % 2 == 0 else 0.39
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        mapa[uid] = f'#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}'
    return mapa

def _ziskat_tyzden(ref_datum):
    """Vrátí (pondeli, nedele) pro týden obsahující ref_datum."""
    weekday = ref_datum.weekday()
    pondeli = ref_datum - datetime.timedelta(days=weekday)
    nedele = pondeli + datetime.timedelta(days=6)
    return pondeli, nedele

# ==========================================
# PŘEPOČET HODIN – FOND, ÚVAZKY, DEN/NOC
# ==========================================
REZIM_PCT = {'plny': 100, 'polovicni': 50, 'castecny': None}
REZIM_NAZEV = {'plny': 'Plný', 'polovicni': 'Poloviční', 'castecny': 'Částečný'}

def _cas_na_hodiny(t):
    """Převede MySQL TIME (timedelta/time/str) na float hodin (0–24)."""
    if isinstance(t, datetime.timedelta):
        return t.total_seconds() / 3600.0
    if isinstance(t, datetime.time):
        return t.hour + t.minute / 60.0 + t.second / 3600.0
    s = str(t)[:5]
    try:
        h, m = s.split(':')
        return int(h) + int(m) / 60.0
    except Exception:
        return 0.0

def _delka_smeny_h(cas_od, cas_do):
    """Délka směny v hodinách; přesah přes půlnoc (konec <= začátek) ⇒ +24 h."""
    od = _cas_na_hodiny(cas_od)
    do = _cas_na_hodiny(cas_do)
    if do <= od:
        do += 24.0
    return round(do - od, 2)

def _hodiny_v_pasmu_den(cas_od, cas_do):
    """Počet hodin směny spadajících do denního pásma 6:00–18:00."""
    od = _cas_na_hodiny(cas_od)
    do = _cas_na_hodiny(cas_do)
    if do <= od:
        do += 24.0
    den = 0.0
    # Projdeme směnu po 15 minutách a počítáme čas v okně <6,18)
    t = od
    while t < do:
        hod = t % 24.0
        if 6.0 <= hod < 18.0:
            den += 0.25
        t += 0.25
    return den

def _typ_smeny(cas_od, cas_do):
    """'den' / 'noc' podle převahy hodin v pásmu 6–18 vs 18–6."""
    celkem = _delka_smeny_h(cas_od, cas_do)
    if celkem <= 0:
        return 'den'
    den_h = _hodiny_v_pasmu_den(cas_od, cas_do)
    return 'den' if den_h >= (celkem / 2.0) else 'noc'

def _mesicni_fond_zaklad(rok, mesic, h_den=8.0):
    """Fond plného úvazku: (Po–Pá mimo státní svátky) × h_den."""
    import intranet_obsah  # lazy – vyhneme se cyklickému importu
    try:
        svatky = intranet_obsah.ziskej_statni_svatky(rok, mesic)
    except Exception:
        svatky = {}
    pocet_dnu = cal_lib.monthrange(rok, mesic)[1]
    prac_dny = 0
    for den in range(1, pocet_dnu + 1):
        d = datetime.date(rok, mesic, den)
        if d.weekday() < 5 and den not in svatky:
            prac_dny += 1
    return round(prac_dny * h_den, 2)

def _uvazky_skupiny(skupina_id):
    """Vrátí {user_id: {'rezim','uvazek_pct'}}; bez záznamu = plný/100 doplní volající."""
    conn = intranet_data.get_db_connection()
    if not conn: return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT user_id, rezim, uvazek_pct FROM smeny_uvazek WHERE skupina_id=%s",
            (skupina_id,))
        return {r['user_id']: {'rezim': r['rezim'], 'uvazek_pct': r['uvazek_pct']}
                for r in cur.fetchall()}
    except Exception as e:
        print(f'[Směny] _uvazky_skupiny: {e}')
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _fond_overridy(skupina_id, rok, mesic):
    """Vrátí {user_id: {'fond_hodin','duvod'}} pro daný měsíc."""
    conn = intranet_data.get_db_connection()
    if not conn: return {}
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT user_id, fond_hodin, duvod FROM smeny_fond_override "
            "WHERE skupina_id=%s AND rok=%s AND mesic=%s",
            (skupina_id, rok, mesic))
        return {r['user_id']: {'fond_hodin': float(r['fond_hodin']), 'duvod': r['duvod']}
                for r in cur.fetchall()}
    except Exception as e:
        print(f'[Směny] _fond_overridy: {e}')
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _naplanovane_hodiny(skupina_id, rok, mesic):
    """Vrátí {user_id: {'naplanovano','den_h','noc_h'}} za měsíc dle přiřazených směn."""
    conn = intranet_data.get_db_connection()
    if not conn: return {}
    cur = None
    prvni = datetime.date(rok, mesic, 1)
    posledni = datetime.date(rok, mesic, cal_lib.monthrange(rok, mesic)[1])
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT p.user_id, s.cas_od, s.cas_do
            FROM smeny_smena s
            JOIN smeny_prirazeni p ON s.id = p.smena_id
            WHERE s.skupina_id=%s AND s.datum BETWEEN %s AND %s
        """, (skupina_id, str(prvni), str(posledni)))
        vysledek = {}
        for r in cur.fetchall():
            uid = r['user_id']
            delka = _delka_smeny_h(r['cas_od'], r['cas_do'])
            zaznam = vysledek.setdefault(uid, {'naplanovano': 0.0, 'den_h': 0.0, 'noc_h': 0.0})
            zaznam['naplanovano'] += delka
            if _typ_smeny(r['cas_od'], r['cas_do']) == 'den':
                zaznam['den_h'] += delka
            else:
                zaznam['noc_h'] += delka
        for z in vysledek.values():
            z['naplanovano'] = round(z['naplanovano'], 2)
            z['den_h'] = round(z['den_h'], 2)
            z['noc_h'] = round(z['noc_h'], 2)
        return vysledek
    except Exception as e:
        print(f'[Směny] _naplanovane_hodiny: {e}')
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def _napocet_mesic(skupina_id, rok, mesic, lide):
    """Řádky přepočtu na osobu pro daný měsíc (jen lidé oddělení)."""
    zaklad = _mesicni_fond_zaklad(rok, mesic)
    uvazky = _uvazky_skupiny(skupina_id)
    overidy = _fond_overridy(skupina_id, rok, mesic)
    naplan = _naplanovane_hodiny(skupina_id, rok, mesic)
    radky = []
    for uid, jmeno in lide.items():
        uv = uvazky.get(uid, {'rezim': 'plny', 'uvazek_pct': 100})
        pct = uv.get('uvazek_pct') or 100
        ov = overidy.get(uid)
        if ov:
            fond = round(ov['fond_hodin'], 2)
        else:
            fond = round(zaklad * pct / 100.0, 2)
        np = naplan.get(uid, {'naplanovano': 0.0, 'den_h': 0.0, 'noc_h': 0.0})
        rozdil = round(np['naplanovano'] - fond, 2)
        radky.append({
            'user_id': uid, 'jmeno': jmeno,
            'rezim': uv.get('rezim', 'plny'), 'uvazek_pct': pct,
            'fond': fond, 'fond_override': bool(ov),
            'duvod': ov['duvod'] if ov else '',
            'naplanovano': np['naplanovano'], 'den_h': np['den_h'], 'noc_h': np['noc_h'],
            'rozdil': rozdil, 'prescas': round(max(0.0, rozdil), 2),
        })
    radky.sort(key=lambda r: r['jmeno'])
    return radky, zaklad

def _fmt_h(x):
    """Naformátuje hodiny: bez zbytečných desetinných nul (8 / 7.5)."""
    return f'{x:.0f}' if abs(x - round(x)) < 0.01 else f'{x:.1f}'

# ==========================================
# TISK – HTML do nového okna
# ==========================================
async def _otevri_html_okno(html):
    """Otevře předané HTML v novém okně (přes blob URL) – pro tisk / PDF."""
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

def _rocni_kalendar_html(rok):
    """Obecný tisknutelný roční kalendář (12 měsíců, víkendy + svátky)."""
    import intranet_obsah
    vytisteno = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    mesice_html = ''
    for mesic in range(1, 13):
        try:
            svatky = intranet_obsah.ziskej_statni_svatky(rok, mesic)
        except Exception:
            svatky = {}
        hdr = ''.join(
            f'<th style="padding:3px 0;font-size:9px;font-weight:700;'
            f'color:{"#ef4444" if di >= 5 else "#6b7280"}">{dn}</th>'
            for di, dn in enumerate(DNY))
        radky = ''
        for tyden in cal_lib.monthcalendar(rok, mesic):
            bunky = ''
            for wi, den in enumerate(tyden):
                if den == 0:
                    bunky += '<td></td>'
                    continue
                d = datetime.date(rok, mesic, den)
                je_vik = d.weekday() >= 5
                je_sv = den in svatky
                if je_sv:
                    styl = 'background:#f3e8ff;color:#7c3aed;font-weight:700'
                elif je_vik:
                    styl = 'color:#ef4444'
                else:
                    styl = 'color:#374151'
                titl = f' title="{svatky[den]}"' if je_sv else ''
                bunky += (f'<td style="text-align:center;padding:2px 0;font-size:10px;'
                          f'{styl}"{titl}>{den}</td>')
            radky += f'<tr>{bunky}</tr>'
        mesice_html += (
            f'<div class="m"><div class="mh">{MESICE[mesic]}</div>'
            f'<table><thead><tr>{hdr}</tr></thead><tbody>{radky}</tbody></table></div>'
        )
    return f'''<!DOCTYPE html><html lang="cs"><head><meta charset="UTF-8">
<title>Plánovací kalendář {rok}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,Helvetica,sans-serif;color:#111827;padding:16px}}
h1{{font-size:20px;font-weight:900;color:#1e3a5f}}
.sub{{font-size:11px;color:#6b7280;margin-bottom:14px}}
.btn{{background:#1d4ed8;color:#fff;border:none;padding:8px 20px;border-radius:7px;
 font-size:12px;font-weight:700;cursor:pointer;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.m{{border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}}
.mh{{background:#1e3a5f;color:#fff;text-align:center;font-weight:700;font-size:12px;padding:5px}}
.m table{{width:100%;border-collapse:collapse}}
.m td{{width:14.28%}}
@media print{{.btn{{display:none}}@page{{size:A4 portrait;margin:8mm}}
 .grid{{grid-template-columns:repeat(3,1fr)}}}}
</style></head><body>
<button class="btn" onclick="window.print()">🖨️ Tisk / Uložit jako PDF</button>
<h1>Plánovací kalendář {rok}</h1>
<p class="sub">Vytištěno: {vytisteno}</p>
<div class="grid">{mesice_html}</div>
</body></html>'''

def _rocni_plan_html(skupina, rok, lide):
    """Roční plán směn oddělení: pro každý měsíc tabulka osoby × dny + souhrn."""
    import intranet_obsah
    vytisteno = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    # Každý člověk oddělení = jedna odlišitelná barva (přizpůsobí se počtu osob)
    barvy = _mapa_barev(lide.keys())
    conn = intranet_data.get_db_connection()
    # Mapa směn: {(user_id, datum_str): (znak, 'od-do', 'celý čas')}
    smeny_map = {}
    if conn:
        cur = None
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT p.user_id, s.datum, s.cas_od, s.cas_do
                FROM smeny_smena s JOIN smeny_prirazeni p ON s.id = p.smena_id
                WHERE s.skupina_id=%s AND YEAR(s.datum)=%s
            """, (skupina['id'], rok))
            for r in cur.fetchall():
                znak = 'D' if _typ_smeny(r['cas_od'], r['cas_do']) == 'den' else 'N'
                cas_kr = f'{_fmt_cas_kratky(r["cas_od"])}-{_fmt_cas_kratky(r["cas_do"])}'
                cas_pl = f'{_fmt_cas(r["cas_od"])} – {_fmt_cas(r["cas_do"])}'
                smeny_map[(r['user_id'], str(r['datum']))] = (znak, cas_kr, cas_pl)
        except Exception as e:
            print(f'[Směny] _rocni_plan_html: {e}')
        finally:
            if cur: cur.close()
            if conn: conn.close()

    mesice_html = ''
    for mesic in range(1, 13):
        try:
            svatky = intranet_obsah.ziskej_statni_svatky(rok, mesic)
        except Exception:
            svatky = {}
        pocet_dnu = cal_lib.monthrange(rok, mesic)[1]
        radky, zaklad = _napocet_mesic(skupina['id'], rok, mesic, lide)

        # Záhlaví dnů
        den_hlavicky = ''
        for den in range(1, pocet_dnu + 1):
            d = datetime.date(rok, mesic, den)
            je_vol = d.weekday() >= 5 or den in svatky
            barva = '#ef4444' if je_vol else '#6b7280'
            den_hlavicky += (f'<th style="color:{barva}">{den}<br>'
                             f'<span style="font-size:7px">{DNY[d.weekday()]}</span></th>')

        telo = ''
        for rad in radky:
            bunky = ''
            barva_os = barvy.get(rad['user_id'], '#6b7280')
            for den in range(1, pocet_dnu + 1):
                d = datetime.date(rok, mesic, den)
                ds = str(d)
                je_vol = d.weekday() >= 5 or den in svatky
                zaznam = smeny_map.get((rad['user_id'], ds))
                if zaznam:
                    znak, cas_kr, cas_pl = zaznam
                    if znak == 'D':
                        # Den = plná výplň osobní barvou, bílý text: typ + čas (D 7-15)
                        cell = (f'<span title="{cas_pl}" style="background:{barva_os};'
                                f'color:#fff;font-weight:700;font-size:6.5px">'
                                f'{znak}&nbsp;{cas_kr}</span>')
                    else:
                        # Noc = světlé pole s barevným prstencem (odstín zůstává čitelný,
                        # inset stín nemění velikost buňky)
                        cell = (f'<span title="{cas_pl}" style="background:#fff;'
                                f'color:{barva_os};font-weight:700;font-size:6.5px;'
                                f'box-shadow:inset 0 0 0 1.6px {barva_os}">'
                                f'{znak}&nbsp;{cas_kr}</span>')
                else:
                    cell = ''
                bg = 'background:#fef2f2;' if je_vol else ''
                bunky += f'<td style="{bg}">{cell}</td>'
            tecka = (f'<span style="display:inline-block;width:9px;height:9px;'
                     f'border-radius:50%;background:{barva_os};'
                     f'margin-right:4px;vertical-align:middle"></span>')
            telo += (
                f'<tr><td class="nm">{tecka}{rad["jmeno"]}</td>{bunky}'
                f'<td class="sum">{_fmt_h(rad["fond"])}</td>'
                f'<td class="sum">{_fmt_h(rad["naplanovano"])}</td>'
                f'<td class="sum" style="color:{"#dc2626" if rad["prescas"]>0 else "#9ca3af"}">'
                f'{_fmt_h(rad["prescas"])}</td></tr>'
            )
        if not radky:
            telo = f'<tr><td colspan="{pocet_dnu + 4}" class="empty">Žádní zaměstnanci</td></tr>'

        mesice_html += (
            f'<div class="mesic"><h2>{MESICE[mesic]} {rok}</h2>'
            f'<table><thead><tr><th class="nm">Zaměstnanec</th>{den_hlavicky}'
            f'<th class="sum">Fond</th><th class="sum">Napl.</th><th class="sum">Přesč.</th>'
            f'</tr></thead><tbody>{telo}</tbody></table></div>'
        )

    return f'''<!DOCTYPE html><html lang="cs"><head><meta charset="UTF-8">
<title>Roční plán směn {rok} — {skupina["nazev"]}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,Helvetica,sans-serif;color:#111827;padding:14px}}
h1{{font-size:18px;font-weight:900;color:#1e3a5f}}
.sub{{font-size:11px;color:#6b7280;margin-bottom:4px}}
.leg{{font-size:10px;color:#6b7280;margin-bottom:14px}}
.btn{{background:#1d4ed8;color:#fff;border:none;padding:8px 20px;border-radius:7px;
 font-size:12px;font-weight:700;cursor:pointer;margin-bottom:12px}}
.mesic{{margin-bottom:18px;page-break-inside:avoid}}
.mesic h2{{font-size:13px;color:#1e3a5f;margin-bottom:4px}}
.mesic h2 .zk{{font-size:9px;font-weight:400;color:#9ca3af;margin-left:8px}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
th,td{{border:1px solid #e5e7eb;text-align:center;font-size:8px;padding:0;height:16px}}
th{{background:#f9fafb;font-weight:700}}
td span{{display:block;border-radius:2px;line-height:14px;white-space:nowrap;
 overflow:hidden;letter-spacing:-.2px;transition:filter .12s ease}}
/* Najetí na řádek (= člověka): jeho směny zůstanou, směny ostatních ztmavnou.
   Dimování se spustí jen když najetý řádek nějakou směnu má (prázdný řádek nic netmaví). */
tbody:has(tr:hover td:not(.nm) span) tr:not(:hover) td:not(.nm) span{{filter:brightness(.55) saturate(.85)}}
.nm{{text-align:left;width:104px;font-size:9px;padding-left:4px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}}
.sum{{width:34px;font-weight:700;background:#f9fafb}}
.empty{{color:#9ca3af;font-style:italic;padding:8px}}
@media print{{.btn{{display:none}}@page{{size:A4 landscape;margin:6mm}}}}
</style></head><body>
<button class="btn" onclick="window.print()">🖨️ Tisk / Uložit jako PDF</button>
<h1>Roční plán směn {rok} — {skupina["nazev"]}</h1>
<p class="sub">Vytištěno: {vytisteno}</p>
<p class="leg">■ plná = denní · ▢ rámeček = noční · čas = od–do</p>
{mesice_html}
</body></html>'''

# ==========================================
# HLAVNÍ RENDER
# ==========================================
def vykresli_smeny(user_id, user_name, vsechna_prava):
    _init_db()
    is_admin = 'vse' in vsechna_prava or 'smeny_admin' in vsechna_prava
    dnes = datetime.date.today()

    stav = {
        'skupina':    None,
        'tyzden_ref': dnes,       # datetime.date — z toho se odvodí Po–Ne
        'mesic_ref':  dnes,       # datetime.date — z toho se odvodí 1. den měsíce
        'vybran_den': str(dnes),  # 'YYYY-MM-DD' nebo None
        'filtr_uid':  None,
        'pohled':     'tyden',    # 'tyden' | 'mesic'
        'prepocet_open': False,   # zůstane rozbalený i po překreslení
        'vyber_dny':  set(),      # SHIFT+klik – dny označené k hromadnému mazání
    }

    @ui.refreshable
    def render():
        if stav['skupina'] is None:
            _dashboard()
        else:
            _view()

    # ==========================================
    # DASHBOARD – dlaždice skupin
    # ==========================================
    def _dashboard():
        skupiny = _skupiny_pro_uzivatele(user_id, vsechna_prava)

        with ui.column().classes('w-full px-4 md:px-8 xl:px-12 py-8 gap-8 bg-gray-50/30 min-h-screen'):
            with ui.row().classes('w-full justify-between items-end border-b border-gray-200 pb-4'):
                with ui.column().classes('gap-1'):
                    ui.label('Plánování směn').classes('text-4xl font-black text-gray-900 tracking-tight')
                    ui.label('Vyberte oddělení, které chcete naplánovat').classes('text-lg text-gray-500')
                with ui.row().classes('items-center gap-3'):
                    ui.button(f'📅 Plánovací kalendář {dnes.year}',
                              on_click=lambda: _otevri_html_okno(_rocni_kalendar_html(dnes.year))).classes(
                        'bg-teal-600 hover:bg-teal-700 text-white font-bold px-6 h-11 rounded-xl shadow-sm')
                    if is_admin:
                        ui.button('⚙️ Nastavení ikon', on_click=_sprava_skupin_dialog).classes(
                            'bg-gray-700 hover:bg-gray-800 text-white font-bold px-6 h-11 rounded-xl shadow-sm')

            if not skupiny:
                with ui.column().classes('w-full items-center justify-center py-24 gap-4'):
                    ui.label('📅').classes('text-7xl')
                    ui.label('Nemáte přístup k žádnému oddělení pro plánování směn.').classes('text-xl text-gray-500')
                    ui.label('Přístup získáte přiřazením práva „Hlavní vedoucí" na oddělení nebo práva „Plánování směn".').classes(
                        'text-sm text-gray-400 text-center max-w-lg')
                return

            # Vedoucí jednoho oddělení → rovnou otevřít
            if len(skupiny) == 1 and not is_admin:
                stav['skupina']    = skupiny[0]
                stav['tyzden_ref'] = dnes
                stav['vybran_den'] = str(dnes)
                stav['filtr_uid']  = None
                _view()
                return

            with ui.row().classes('gap-8 flex-wrap'):
                for sk in skupiny:
                    def _otevri(s=sk):
                        stav['skupina']    = s
                        stav['tyzden_ref'] = dnes
                        stav['vybran_den'] = str(dnes)
                        stav['filtr_uid']  = None
                        stav['vyber_dny'].clear()
                        render.refresh()

                    with ui.card().classes(
                        'w-80 h-56 items-center justify-center shadow-lg hover:scale-105 '
                        'transition-transform duration-300 cursor-pointer bg-white rounded-2xl '
                        'border border-teal-100'
                    ).on('click', _otevri):
                        ui.label(sk.get('ikona', '📋')).classes('text-6xl mb-3')
                        ui.label(sk['nazev']).classes('text-2xl font-bold text-gray-800 mb-2 text-center')
                        ui.button('Otevřít plán', on_click=_otevri).classes(
                            'bg-teal-600 hover:bg-teal-700 text-white font-bold py-2 px-6 rounded-lg shadow-md')

    # ==========================================
    # NAVIGACE
    # ==========================================
    def _zpet():
        stav['skupina']    = None
        stav['vybran_den'] = str(dnes)
        stav['vyber_dny'].clear()
        render.refresh()

    def _prev_tyzden():
        stav['tyzden_ref'] -= datetime.timedelta(weeks=1)
        stav['vyber_dny'].clear()
        render.refresh()

    def _next_tyzden():
        stav['tyzden_ref'] += datetime.timedelta(weeks=1)
        stav['vyber_dny'].clear()
        render.refresh()

    def _tento_tyzden():
        stav['tyzden_ref'] = dnes
        stav['vybran_den'] = str(dnes)
        stav['vyber_dny'].clear()
        render.refresh()

    def _prev_mesic():
        ref = stav['mesic_ref']
        if ref.month == 1:
            stav['mesic_ref'] = ref.replace(year=ref.year - 1, month=12, day=1)
        else:
            stav['mesic_ref'] = ref.replace(month=ref.month - 1, day=1)
        stav['vyber_dny'].clear()
        render.refresh()

    def _next_mesic():
        ref = stav['mesic_ref']
        if ref.month == 12:
            stav['mesic_ref'] = ref.replace(year=ref.year + 1, month=1, day=1)
        else:
            stav['mesic_ref'] = ref.replace(month=ref.month + 1, day=1)
        stav['vyber_dny'].clear()
        render.refresh()

    def _tento_mesic():
        stav['mesic_ref'] = dnes
        stav['vybran_den'] = str(dnes)
        stav['vyber_dny'].clear()
        render.refresh()

    # ==========================================
    # POHLED KALENDÁŘE
    # ==========================================
    def _view():
        skupina = stav['skupina']
        oddeleni = skupina.get('oddeleni_nazev', '')
        je_ved = is_admin or _je_vedouci(vsechna_prava, oddeleni)
        lide   = _lide_skupiny(oddeleni) if je_ved else {}
        filtr  = stav['filtr_uid'] if je_ved else user_id
        pohled = stav['pohled']

        # ESC zavře kalendář (návrat na dashboard). Když je otevřený dialog,
        # ESC řeší Quasar (zavře dialog) a návrat se neprovede – kontrola .q-dialog.
        ui.timer(0.05, lambda: ui.run_javascript('''
            if (!window._smenyEscBound) {
                window._smenyEscBound = true;
                document.addEventListener('keydown', function(e) {
                    if (e.key !== 'Escape') return;
                    if (document.querySelector('.q-dialog')) return;
                    var btn = document.querySelector('.smeny-zpet-btn');
                    if (btn) btn.click();
                });
            }
        '''), once=True)

        # ── Hromadné mazání přes SHIFT+klik (jen vedoucí) ─────────────────
        def _zrus_vyber():
            stav['vyber_dny'].clear()
            render.refresh()

        def _otevri_mazani():
            if je_ved and stav['vyber_dny']:
                _dialog_mazat_oznacene(skupina, sorted(stav['vyber_dny']), lide)

        if je_ved:
            # Klávesa DEL nad označenými dny → dialog mazání.
            # ui.keyboard uvnitř tab_panelu nefunguje, proto (stejně jako ESC výše)
            # použijeme JS posluchač na úrovni dokumentu, který klikne na skryté tlačítko.
            # Skryté tlačítko zůstává v toku (ne position:fixed) – díky tomu má
            # offsetParent==null, jen když je celá záložka skrytá → DEL nereaguje
            # z jiné záložky.
            ui.button(on_click=_otevri_mazani).props('flat').classes(
                'smeny-del-btn').style(
                'width:0;height:0;min-width:0;padding:0;opacity:0;overflow:hidden')
            ui.timer(0.05, lambda: ui.run_javascript('''
                if (!window._smenyDelBound) {
                    window._smenyDelBound = true;
                    document.addEventListener('keydown', function(e) {
                        if (e.key !== 'Delete') return;
                        if (document.querySelector('.q-dialog')) return;
                        var t = document.activeElement;
                        if (t && ['INPUT','TEXTAREA','SELECT'].indexOf(t.tagName) >= 0) return;
                        var btn = document.querySelector('.smeny-del-btn');
                        if (!btn || btn.offsetParent === null) return;
                        btn.click();
                    });
                }
            '''), once=True)

        # Předpočítat navigační data pro oba pohledy
        pondeli, nedele = _ziskat_tyzden(stav['tyzden_ref'])
        prvni = stav['mesic_ref'].replace(day=1)
        posledni_den_m = cal_lib.monthrange(prvni.year, prvni.month)[1]
        posledni_m = prvni.replace(day=posledni_den_m)

        # Absence se zobrazují za celé oddělení – i řadovému zaměstnanci
        absence_lide = lide if lide else _lide_skupiny(oddeleni)
        lide_ids = list(absence_lide.keys()) if absence_lide else [user_id]
        maska = None if je_ved else user_id
        if pohled == 'tyden':
            data = _smeny_rozsah(skupina['id'], str(pondeli), str(nedele), filtr)
            absence_data = _absence_rozsah(str(pondeli), str(nedele), lide_ids, maskovat_krome=maska)
        else:
            data = _smeny_rozsah(skupina['id'], str(prvni), str(posledni_m), filtr)
            absence_data = _absence_rozsah(str(prvni), str(posledni_m), lide_ids, maskovat_krome=maska)

        # ── PDF / Tisk ────────────────────────────────────────────────────
        async def _nahled_pdf():
            vytisteno = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')

            # ── Společné CSS ──────────────────────────────────────────────
            css_base = '''
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#111827}
.top{padding:14px 16px 10px}
h1{font-size:17px;font-weight:900;color:#1e3a5f;margin-bottom:2px}
.sub{font-size:10px;color:#6b7280;margin-bottom:0}
.btn{background:#1d4ed8;color:#fff;border:none;padding:8px 20px;border-radius:7px;
     font-size:12px;font-weight:700;cursor:pointer;margin-bottom:10px;display:inline-block}
.shift{border-radius:3px;padding:3px 5px;margin-bottom:2px}
.s-time{font-weight:700;font-size:10px;white-space:nowrap}
.s-name{font-weight:600;font-size:10px}
/* Jména na směně se musí zalomit na víc řádků — jinak se dlouhý seznam
   ořízne a v tisku není vidět, kdo všechno na směně je. */
.s-ppl{font-size:9px;color:#4b5563;line-height:1.35;word-break:break-word}
.s-note{font-size:9px;color:#9ca3af;font-style:italic;
        line-height:1.35;word-break:break-word}
@media print{.btn{display:none}body{font-size:10px}
  @page{size:A4 landscape;margin:10mm}}
'''

            if pohled == 'tyden':
                # ── TÝDENNÍ pohled ────────────────────────────────────────
                if pondeli.month == nedele.month:
                    titulek = (f"{skupina['nazev']} — {pondeli.day}.–{nedele.day}. "
                               f"{MESICE_2[nedele.month]} {nedele.year}")
                else:
                    titulek = (f"{skupina['nazev']} — "
                               f"{pondeli.day}.{pondeli.month}. – "
                               f"{nedele.day}.{nedele.month}.{nedele.year}")
                dni_t = [pondeli + datetime.timedelta(days=i) for i in range(7)]

                cols_html = ''
                for datum in dni_t:
                    datum_str  = str(datum)
                    smeny_dne  = data.get(datum_str, [])
                    is_today   = (datum == dnes)
                    is_weekend = datum.weekday() >= 5

                    if is_today:
                        hdr_bg = 'background:#2563eb;color:#fff'
                        abbr_c = 'color:rgba(255,255,255,.8)'
                        num_c  = 'color:#fff'
                    elif is_weekend:
                        hdr_bg = 'background:#fff1f2'
                        abbr_c = 'color:#f87171'
                        num_c  = 'color:#ef4444'
                    else:
                        hdr_bg = 'background:#f9fafb'
                        abbr_c = 'color:#9ca3af'
                        num_c  = 'color:#374151'

                    shifts_html = ''
                    for sm in smeny_dne:
                        barva = sm.get('barva', '#3b82f6')
                        lide_list = ', '.join(
                            o['user_name'] for o in sm.get('lide', [])) or '—'
                        popis = sm.get('popis') or ''
                        shifts_html += (
                            f'<div class="shift" style="background:{barva}18;'
                            f'border-left:3px solid {barva}">'
                            f'<div class="s-time" style="color:{barva}">'
                            f'{sm["cas_od_fmt"]} – {sm["cas_do_fmt"]}</div>'
                            f'<div class="s-name">{sm["nazev"]}</div>'
                            f'<div class="s-ppl">{lide_list}</div>'
                            + (f'<div class="s-note">{popis}</div>' if popis else '')
                            + '</div>'
                        )

                    border_r = 'border-right:1px solid #e5e7eb;' if datum.weekday() < 6 else ''
                    cols_html += (
                        f'<div style="flex:1;min-width:0;{border_r}">'
                        f'<div style="padding:6px 4px 4px;text-align:center;'
                        f'border-bottom:1px solid #e5e7eb;{hdr_bg}">'
                        f'<div style="font-size:9px;font-weight:600;'
                        f'text-transform:uppercase;letter-spacing:.05em;{abbr_c}">'
                        f'{DNY[datum.weekday()]}</div>'
                        f'<div style="font-size:20px;font-weight:900;line-height:1;{num_c}">'
                        f'{datum.day}</div>'
                        + (f'<div style="font-size:8px;{abbr_c}">'
                           f'{MESICE[datum.month]}</div>' if datum.day == 1 else '')
                        + '</div>'
                        f'<div style="padding:4px 3px;min-height:120px">{shifts_html}</div>'
                        f'</div>'
                    )

                obsah_html = (
                    f'<div style="display:flex;border:1px solid #e5e7eb;'
                    f'border-radius:8px;overflow:hidden;margin:0 16px 16px">'
                    f'{cols_html}</div>'
                )

            else:
                # ── MĚSÍČNÍ pohled ────────────────────────────────────────
                titulek = f"{skupina['nazev']} — {MESICE[prvni.month]} {prvni.year}"
                tyzdny_pdf = cal_lib.monthcalendar(prvni.year, prvni.month)

                # Záhlaví dnů
                hdr_cells = ''
                for di, dn in enumerate(DNY):
                    color = '#ef4444' if di >= 5 else '#6b7280'
                    hdr_cells += (
                        f'<th style="text-align:center;padding:5px 2px;font-size:9px;'
                        f'font-weight:700;text-transform:uppercase;color:{color};'
                        f'border-right:{"1px solid #e5e7eb" if di<6 else "none"}">'
                        f'{dn}</th>'
                    )

                # Buňky mají pevnou MINIMÁLNÍ výšku a rostou dle obsahu — aby se
                # v tisku vešli VŠICHNI lidé na směně (dřív se kalendář natahoval
                # na výšku stránky a ořezával obsah na 2 jména + „+N další").
                cell_min_h = '72px'

                rows_html = ''
                for tyzden in tyzdny_pdf:
                    cells_html = ''
                    for wi, den_cislo in enumerate(tyzden):
                        border_r = 'border-right:1px solid #e5e7eb;' if wi < 6 else ''
                        td_common = (
                            f'vertical-align:top;{border_r}'
                            f'border-bottom:1px solid #e5e7eb;'
                            f'height:{cell_min_h};'
                        )
                        if den_cislo == 0:
                            cells_html += (
                                f'<td style="{td_common}background:#f9fafb"></td>'
                            )
                        else:
                            datum      = datetime.date(prvni.year, prvni.month, den_cislo)
                            datum_str  = str(datum)
                            is_today   = (datum == dnes)
                            is_weekend = datum.weekday() >= 5
                            smeny_dne  = data.get(datum_str, [])

                            if is_today:
                                cell_bg = '#eff6ff'
                            elif is_weekend:
                                cell_bg = '#fff1f2'
                            else:
                                cell_bg = '#fff'

                            num_c = '#ef4444' if is_weekend else '#374151'
                            if is_today:
                                num_html = (
                                    f'<div style="display:inline-flex;width:20px;height:20px;'
                                    f'border-radius:50%;background:#2563eb;color:#fff;'
                                    f'align-items:center;justify-content:center;'
                                    f'font-weight:900;font-size:10px;margin-bottom:2px">'
                                    f'{den_cislo}</div>'
                                )
                            else:
                                num_html = (
                                    f'<div style="font-size:10px;font-weight:700;'
                                    f'color:{num_c};margin-bottom:2px">{den_cislo}</div>'
                                )

                            shifts_html = ''
                            for sm in smeny_dne:
                                barva = sm.get('barva', '#3b82f6')
                                # Plná jména VŠECH lidí na směně, zalomená na víc řádků.
                                jmena = ', '.join(
                                    o['user_name'] for o in sm.get('lide', []))
                                shifts_html += (
                                    f'<div style="border-radius:2px;padding:1px 3px;'
                                    f'margin-bottom:1px;'
                                    f'background:{barva}18;border-left:2px solid {barva}">'
                                    f'<div style="font-size:9px;font-weight:600;'
                                    f'color:{barva};word-break:break-word;line-height:1.3">'
                                    f'{sm["cas_od_fmt"]} {sm["nazev"]}</div>'
                                    + (f'<div style="font-size:8px;color:#6b7280;'
                                       f'word-break:break-word;line-height:1.3">{jmena}</div>'
                                       if jmena else '')
                                    + '</div>'
                                )

                            cells_html += (
                                f'<td style="{td_common}padding:3px 4px;'
                                f'background:{cell_bg}">'
                                f'{num_html}{shifts_html}</td>'
                            )
                    rows_html += f'<tr>{cells_html}</tr>'

                # Tisk: řádek (týden) nedělit přes zlom stránky, ať se buňka
                # s mnoha jmény nerozpadne. Kalendář roste přirozeně dle obsahu
                # a případně přeteče na další stránku — všechna jména jsou vidět.
                css_mesic_print = '@media print{tr{break-inside:avoid}}'

                obsah_html = (
                    f'<style>{css_mesic_print}</style>'
                    f'<div class="cal-wrap" style="margin:0 16px 8px;'
                    f'border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">'
                    f'<table style="width:100%;border-collapse:collapse;'
                    f'table-layout:fixed">'
                    f'<thead><tr style="background:#f9fafb;'
                    f'border-bottom:1px solid #e5e7eb">'
                    f'{hdr_cells}</tr></thead>'
                    f'<tbody>{rows_html}</tbody>'
                    f'</table></div>'
                )

            html = f'''<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8">
<title>Plán směn — {titulek}</title>
<style>{css_base}</style></head>
<body>
<div class="top">
<button class="btn" onclick="window.print()">🖨️ Tisk / Uložit jako PDF</button>
<h1>Plán směn — {titulek}</h1>
<p class="sub">Vytištěno: {vytisteno}</p>
</div>
{obsah_html}
</body></html>'''

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

        with ui.column().classes('w-full min-h-screen bg-white'):

            # ── HEADER ────────────────────────────────────────────────────
            with ui.row().classes(
                'w-full items-center justify-between px-4 md:px-6 py-3 '
                'bg-white border-b border-gray-200 gap-3 flex-wrap shadow-sm'
            ):
                # Zpět + název oddělení
                with ui.row().classes('items-center gap-2'):
                    ui.button(icon='arrow_back', on_click=_zpet).props('flat round').classes('text-gray-500 smeny-zpet-btn')
                    ui.label(skupina.get('ikona', '📋')).classes('text-2xl leading-none')
                    ui.label(skupina['nazev']).classes('text-xl font-bold text-gray-800')

                # Navigace (týden nebo měsíc)
                with ui.row().classes('items-center gap-1'):
                    if pohled == 'tyden':
                        ui.button(icon='chevron_left',
                                  on_click=_prev_tyzden).props('flat round dense')
                        if pondeli.month == nedele.month and pondeli.year == nedele.year:
                            nav_label = (f'{pondeli.day}. – {nedele.day}. '
                                         f'{MESICE_2[nedele.month]} {nedele.year}')
                        elif pondeli.year == nedele.year:
                            nav_label = (f'{pondeli.day}. {MESICE_2[pondeli.month]} – '
                                         f'{nedele.day}. {MESICE_2[nedele.month]} {nedele.year}')
                        else:
                            nav_label = (f'{pondeli.day}. {MESICE_2[pondeli.month]} {pondeli.year} – '
                                         f'{nedele.day}. {MESICE_2[nedele.month]} {nedele.year}')
                        ui.label(nav_label).classes(
                            'text-sm font-bold text-gray-700 min-w-[210px] text-center')
                        ui.button(icon='chevron_right',
                                  on_click=_next_tyzden).props('flat round dense')
                        ui.button('Dnes', on_click=_tento_tyzden).classes(
                            'bg-gray-100 hover:bg-gray-200 text-gray-700 font-semibold '
                            'h-8 px-3 rounded-lg text-sm ml-1')
                    else:
                        ui.button(icon='chevron_left',
                                  on_click=_prev_mesic).props('flat round dense')
                        ui.label(f'{MESICE[prvni.month]} {prvni.year}').classes(
                            'text-sm font-bold text-gray-700 min-w-[160px] text-center')
                        ui.button(icon='chevron_right',
                                  on_click=_next_mesic).props('flat round dense')
                        ui.button('Tento měsíc', on_click=_tento_mesic).classes(
                            'bg-gray-100 hover:bg-gray-200 text-gray-700 font-semibold '
                            'h-8 px-3 rounded-lg text-sm ml-1')

                # Pravá část: filtr + přepínač pohledu + PDF
                with ui.row().classes('items-center gap-2 flex-wrap'):

                    # Filtr osoby (jen vedoucí/admin)
                    if je_ved and lide:
                        options = {'': 'Všichni'} | {str(k): v for k, v in lide.items()}
                        cur_val = str(stav['filtr_uid']) if stav['filtr_uid'] else ''
                        fil = ui.select(options, value=cur_val, label='Osoba').classes(
                            'w-44 bg-white').props('outlined dense clearable')
                        def _zmena_filtru(e):
                            stav['filtr_uid'] = int(e.value) if e.value else None
                            render.refresh()
                        fil.on_value_change(_zmena_filtru)

                    # Přepínač Týden / Měsíc
                    with ui.row().classes(
                        'gap-0 rounded-lg overflow-hidden border border-gray-200'
                    ):
                        ui.button('Týden',
                            on_click=lambda: (
                                stav['vyber_dny'].clear(),
                                stav.update({'pohled': 'tyden'}), render.refresh())
                        ).props('flat no-caps').classes(
                            'h-9 px-4 font-semibold rounded-none border-r border-gray-200 ' +
                            ('bg-blue-600 text-white'
                             if pohled == 'tyden'
                             else 'bg-white text-gray-600 hover:bg-gray-50'))
                        ui.button('Měsíc',
                            on_click=lambda: (
                                stav['vyber_dny'].clear(),
                                stav.update({'pohled': 'mesic'}), render.refresh())
                        ).props('flat no-caps').classes(
                            'h-9 px-4 font-semibold rounded-none ' +
                            ('bg-blue-600 text-white'
                             if pohled == 'mesic'
                             else 'bg-white text-gray-600 hover:bg-gray-50'))

                    # Roční plán směn oddělení (jen vedoucí)
                    if je_ved:
                        ui.button(f'📅 Roční plán {dnes.year}',
                                  on_click=lambda: _otevri_html_okno(
                                      _rocni_plan_html(skupina, dnes.year,
                                                       lide or _lide_skupiny(oddeleni)))).props('no-caps').classes(
                            'bg-teal-600 hover:bg-teal-700 text-white font-semibold '
                            'h-9 px-4 rounded-lg text-sm')

                    # PDF export
                    ui.button(icon='picture_as_pdf', on_click=_nahled_pdf) \
                        .props('flat round') \
                        .classes('text-red-500 hover:bg-red-50') \
                        .tooltip('Tisk / Export do PDF')

            # Přepočet hodin pro aktuálně zobrazený měsíc – spočítáme jednou
            # a použijeme pro pruh vytížení i pro spodní panel.
            if pohled == 'mesic':
                pc_rok, pc_mesic = prvni.year, prvni.month
            else:
                pc_rok, pc_mesic = stav['tyzden_ref'].year, stav['tyzden_ref'].month
            prepocet_lide = lide or _lide_skupiny(oddeleni)
            if je_ved:
                prepocet_radky, prepocet_zaklad = _napocet_mesic(
                    skupina['id'], pc_rok, pc_mesic, prepocet_lide)
            else:
                prepocet_radky, prepocet_zaklad = [], 0

            # ── PŘEHLED VYTÍŽENÍ (jen vedoucí, hned na první pohled) ──────
            if je_ved:
                _draw_fond_prehled(prepocet_radky, prepocet_zaklad, pc_rok, pc_mesic)

            # ── LIŠTA OZNAČENÝCH DNŮ (SHIFT+klik → hromadné mazání) ───────
            if je_ved and stav['vyber_dny']:
                poc = len(stav['vyber_dny'])
                den_slovo = 'den' if poc == 1 else ('dny' if poc < 5 else 'dní')
                with ui.row().classes(
                    'w-full items-center gap-3 px-4 md:px-5 py-2 '
                    'bg-red-50 border-y border-red-200'
                ):
                    ui.icon('check_box', size='sm').classes('text-red-500')
                    ui.label(f'Označeno {poc} {den_slovo} k mazání').classes(
                        'text-sm font-bold text-red-700')
                    ui.label('SHIFT + klik označí další den · klávesa DEL smaže').classes(
                        'text-[11px] text-red-400 hidden sm:block')
                    ui.space()
                    ui.button('Smazat směny', icon='delete',
                              on_click=_otevri_mazani).classes(
                        'bg-red-600 hover:bg-red-700 text-white font-bold '
                        'h-8 px-4 rounded-lg text-sm').props('no-caps')
                    ui.button('Zrušit výběr', on_click=_zrus_vyber).props(
                        'flat no-caps dense').classes('text-gray-500 text-sm')

            # ── TĚLO: KALENDÁŘ + PRAVÝ PANEL ─────────────────────────────
            with ui.row().classes('w-full flex-1 gap-0 items-start overflow-hidden'):

                with ui.element('div').classes('flex-1 min-w-0 overflow-x-auto p-3 md:p-4'):
                    if pohled == 'tyden':
                        _draw_weekly_calendar(data, je_ved, pondeli, skupina, lide, absence_data)
                    else:
                        _draw_monthly_calendar(data, je_ved, prvni, skupina, lide, absence_data)

                # Pravý panel – detail vybraného dne
                if stav['vybran_den']:
                    with ui.column().classes(
                        'w-80 xl:w-96 flex-shrink-0 border-l border-gray-200 '
                        'bg-white self-stretch min-h-[calc(100vh-58px)]'
                    ):
                        _draw_panel(skupina, je_ved, lide, absence_data)

            # ── PŘEPOČET HODIN (jen vedoucí) ─────────────────────────────
            if je_ved:
                _draw_prepocet(skupina, oddeleni, prepocet_lide,
                               pc_rok, pc_mesic, prepocet_radky, prepocet_zaklad)

    # ==========================================
    # TÝDENNÍ MŘÍŽKA
    # ==========================================
    def _draw_weekly_calendar(data, je_ved, pondeli, skupina, lide, absence_data=None):
        absence_data = absence_data or {}
        tydne = [pondeli + datetime.timedelta(days=i) for i in range(7)]

        with ui.row().classes('gap-0 border border-gray-200 rounded-xl overflow-hidden min-w-[560px]'):
            for i, datum in enumerate(tydne):
                datum_str  = str(datum)
                is_today   = (datum == dnes)
                is_selected = (stav['vybran_den'] == datum_str)
                is_oznacen = je_ved and datum_str in stav['vyber_dny']
                is_weekend = datum.weekday() >= 5
                smeny_dne  = data.get(datum_str, [])

                def _klik(e, d=datum_str):
                    shift = isinstance(e.args, dict) and e.args.get('shiftKey')
                    if je_ved and shift:
                        if d in stav['vyber_dny']:
                            stav['vyber_dny'].discard(d)
                        else:
                            stav['vyber_dny'].add(d)
                    else:
                        stav['vyber_dny'].clear()
                        stav['vybran_den'] = d
                    render.refresh()

                def _right_klik(d=datum_str):
                    if not je_ved:
                        return
                    if stav['vyber_dny']:        # je-li aktivní výběr → nabídni mazání
                        stav['vyber_dny'].add(d)
                        _dialog_mazat_oznacene(skupina, sorted(stav['vyber_dny']), lide)
                    else:
                        stav['vybran_den'] = d
                        _dialog_smena(skupina, d, lide, None)

                border_r = 'border-r border-gray-200' if i < 6 else ''

                # Pozadí sloupce
                if is_selected:
                    col_bg = 'bg-blue-50/50'
                elif is_weekend:
                    col_bg = 'bg-red-50/20'
                else:
                    col_bg = 'bg-white'

                # Označení k mazání = jemný červený rámeček
                vyber_ring = 'ring-2 ring-inset ring-red-400' if is_oznacen else ''

                with ui.column().classes(
                    f'flex-1 min-w-0 {col_bg} {border_r} {vyber_ring} '
                    f'cursor-pointer gap-0 select-none'
                ).on('click', _klik, ['shiftKey']).on('contextmenu.prevent', _right_klik):

                    # ── Záhlaví dne ──────────────────────────────────────
                    if is_today:
                        hdr_bg   = 'bg-blue-600'
                        abbr_cls = 'text-[11px] font-semibold text-blue-100 uppercase tracking-wide'
                        num_cls  = 'text-2xl font-black text-white leading-none'
                        mnth_cls = 'text-[9px] text-blue-200'
                    elif is_selected:
                        hdr_bg   = 'bg-blue-50 border-b-2 border-blue-400'
                        abbr_cls = 'text-[11px] font-semibold text-blue-500 uppercase tracking-wide'
                        num_cls  = 'text-2xl font-black text-blue-700 leading-none'
                        mnth_cls = 'text-[9px] text-blue-400'
                    elif is_weekend:
                        hdr_bg   = 'bg-red-50 border-b border-red-100'
                        abbr_cls = 'text-[11px] font-semibold text-red-400 uppercase tracking-wide'
                        num_cls  = 'text-2xl font-black text-red-500 leading-none'
                        mnth_cls = 'text-[9px] text-red-300'
                    else:
                        hdr_bg   = 'bg-gray-50 border-b border-gray-200'
                        abbr_cls = 'text-[11px] font-semibold text-gray-400 uppercase tracking-wide'
                        num_cls  = 'text-2xl font-black text-gray-700 leading-none'
                        mnth_cls = 'text-[9px] text-gray-400'

                    with ui.column().classes(
                        f'w-full items-center pt-2 pb-1.5 px-1 gap-0 {hdr_bg}'
                    ):
                        ui.label(DNY[datum.weekday()]).classes(abbr_cls)
                        ui.label(str(datum.day)).classes(num_cls)
                        # Název měsíce jen u 1. v měsíci
                        if datum.day == 1:
                            ui.label(MESICE[datum.month]).classes(mnth_cls)

                    # ── Bloky směn ────────────────────────────────────────
                    with ui.column().classes('w-full p-1 gap-0.5 min-h-[240px]'):
                        for sm in smeny_dne:
                            with ui.element('div').classes(
                                'w-full rounded p-1.5 mb-0.5 overflow-hidden bg-gray-50'
                            ).style('border-left:3px solid #cbd5e1'):
                                ui.label(f'{sm["cas_od_fmt"]} – {sm["cas_do_fmt"]}').classes(
                                    'text-[11px] font-bold text-gray-700 leading-tight block truncate')
                                ui.label(sm['nazev']).classes(
                                    'text-[10px] text-gray-500 leading-tight block truncate')
                                for os in sm.get('lide', [])[:2]:
                                    barva_os = _barva_uzivatele(os['user_id'])
                                    with ui.row().classes('items-center gap-0.5 flex-nowrap'):
                                        ui.element('div').style(
                                            f'width:7px;height:7px;border-radius:50%;'
                                            f'background:{barva_os};flex-shrink:0'
                                        )
                                        ui.label(os['user_name']).classes(
                                            'text-[10px] text-gray-500 leading-tight truncate')
                                extra = len(sm.get('lide', [])) - 2
                                if extra > 0:
                                    ui.label(f'+{extra} další').classes(
                                        'text-[10px] text-gray-300 italic leading-tight block')

                        # ── Absence dne ───────────────────────────────────
                        abses_dne = absence_data.get(datum_str, [])
                        if abses_dne:
                            with ui.column().classes(
                                'w-full mt-1 pt-1 border-t border-amber-100 gap-0.5'
                            ):
                                ui.label('Absence').classes(
                                    'text-[9px] font-bold text-amber-600 uppercase '
                                    'tracking-wider leading-none')
                                for ab in abses_dne:
                                    barva_ab, ikona_ab = _abs_styl(ab['typ'])
                                    with ui.row().classes(
                                        'items-center gap-0.5 flex-nowrap w-full'
                                    ).style(
                                        f'background:{barva_ab}14;border-radius:3px;'
                                        f'padding:1px 3px'
                                    ).tooltip(f'{ab["user_name"]} — {ab["typ"]}'):
                                        ui.label(ikona_ab).classes(
                                            'text-[10px] leading-none flex-shrink-0')
                                        ui.label(ab['user_name']).classes(
                                            'text-[10px] leading-tight truncate'
                                        ).style(f'color:{barva_ab}')

    # ==========================================
    # MĚSÍČNÍ MŘÍŽKA
    # ==========================================
    def _draw_monthly_calendar(data, je_ved, prvni_den, skupina, lide, absence_data=None):
        absence_data = absence_data or {}
        rok    = prvni_den.year
        mesic  = prvni_den.month
        tyzdny = cal_lib.monthcalendar(rok, mesic)
        # Odečteme: záhlaví směn (~58 px) + obal p-3 (24 px) + záhlaví dnů (~34 px) + border (2 px)
        row_h = f'calc((100vh - 118px) / {len(tyzdny)})'

        with ui.column().classes(
            'w-full gap-0 border border-gray-200 rounded-xl overflow-hidden '
            'min-w-[560px] flex flex-col'
        ).style('height:calc(100vh - 118px)'):

            # Záhlaví dnů
            with ui.row().classes(
                'w-full gap-0 bg-gray-50 border-b border-gray-200 flex-shrink-0'
            ):
                for den_nazev in DNY:
                    is_wknd = (DNY.index(den_nazev) >= 5)
                    ui.label(den_nazev).classes(
                        'flex-1 text-center py-2 text-xs font-bold uppercase tracking-wide '
                        + ('text-red-400' if is_wknd else 'text-gray-500'))

            # Týdny – každý dostane rovný díl zbývající výšky
            for tyzden in tyzdny:
                with ui.row().classes('w-full gap-0 flex-1 min-h-0'):
                    for wi, den_cislo in enumerate(tyzden):
                        border_r = 'border-r border-gray-100' if wi < 6 else ''

                        if den_cislo == 0:
                            # Den mimo měsíc
                            ui.element('div').classes(
                                f'flex-1 bg-gray-50/60 {border_r} border-b border-gray-100')
                        else:
                            datum      = datetime.date(rok, mesic, den_cislo)
                            datum_str  = str(datum)
                            is_today   = (datum == dnes)
                            is_sel     = (stav['vybran_den'] == datum_str)
                            is_oznacen = je_ved and datum_str in stav['vyber_dny']
                            is_weekend = datum.weekday() >= 5
                            smeny_dne  = data.get(datum_str, [])

                            if is_today:
                                cell_bg = 'bg-blue-50'
                            elif is_sel:
                                cell_bg = 'bg-indigo-50/40'
                            elif is_weekend:
                                cell_bg = 'bg-red-50/20'
                            else:
                                cell_bg = 'bg-white'

                            # Označení k mazání má přednost (jemný červený rámeček)
                            if is_oznacen:
                                ring = 'ring-2 ring-inset ring-red-400'
                            elif is_sel:
                                ring = 'ring-2 ring-inset ring-blue-300'
                            else:
                                ring = ''

                            def _klik_m(e, d=datum_str):
                                shift = isinstance(e.args, dict) and e.args.get('shiftKey')
                                if je_ved and shift:
                                    if d in stav['vyber_dny']:
                                        stav['vyber_dny'].discard(d)
                                    else:
                                        stav['vyber_dny'].add(d)
                                else:
                                    stav['vyber_dny'].clear()
                                    stav['vybran_den'] = d
                                render.refresh()

                            def _right_klik_m(d=datum_str):
                                if not je_ved:
                                    return
                                if stav['vyber_dny']:
                                    stav['vyber_dny'].add(d)
                                    _dialog_mazat_oznacene(
                                        skupina, sorted(stav['vyber_dny']), lide)
                                else:
                                    stav['vybran_den'] = d
                                    _dialog_smena(skupina, d, lide, None)

                            with ui.column().classes(
                                f'flex-1 {cell_bg} {border_r} {ring} '
                                f'border-b border-gray-100 cursor-pointer p-1 gap-0.5 '
                                f'overflow-hidden select-none hover:bg-blue-50/30 transition-colors'
                            ).on('click', _klik_m, ['shiftKey']).on('contextmenu.prevent', _right_klik_m):

                                # Číslo dne
                                if is_today:
                                    ui.label(str(den_cislo)).style(
                                        'width:22px;height:22px;min-width:22px;'
                                        'border-radius:50%;background:#2563eb;'
                                        'color:white;font-weight:900;font-size:11px;'
                                        'display:inline-flex;align-items:center;'
                                        'justify-content:center;margin-bottom:2px'
                                    )
                                else:
                                    ui.label(str(den_cislo)).classes(
                                        'text-xs font-semibold mb-0.5 ' +
                                        ('text-red-400' if is_weekend else 'text-gray-600'))

                                # Bloky směn (max 3 viditelné)
                                for sm in smeny_dne[:3]:
                                    barva = sm.get('barva', '#3b82f6')
                                    with ui.element('div').classes(
                                        'w-full rounded px-1 py-0.5 mb-0.5 overflow-hidden'
                                    ).style(
                                        f'background:{barva}1a;border-left:2px solid {barva}'
                                    ):
                                        ui.label(
                                            f'{sm["cas_od_fmt"]} {sm["nazev"]}'
                                        ).classes(
                                            'text-[10px] font-semibold truncate block leading-tight'
                                        ).style(f'color:{barva}')
                                        if sm.get('lide'):
                                            jmena = ', '.join(
                                                o['user_name'].split()[0]
                                                for o in sm['lide'][:2])
                                            ui.label(jmena).classes(
                                                'text-[9px] truncate block leading-tight text-gray-500')

                                extra = len(smeny_dne) - 3
                                if extra > 0:
                                    ui.label(f'+{extra} další').classes(
                                        'text-[9px] text-gray-400 italic leading-tight')

                                # Absence dne – kompaktní
                                abses_dne = absence_data.get(datum_str, [])
                                for ab in abses_dne[:2]:
                                    barva_ab, ikona_ab = _abs_styl(ab['typ'])
                                    with ui.row().classes(
                                        'items-center gap-0.5 flex-nowrap w-full'
                                    ).style(
                                        f'background:{barva_ab}14;border-radius:2px;'
                                        f'padding:0 2px'
                                    ).tooltip(f'{ab["user_name"]} — {ab["typ"]}'):
                                        ui.label(ikona_ab).classes(
                                            'text-[9px] leading-none flex-shrink-0')
                                        ui.label(ab['user_name'].split()[0]).classes(
                                            'text-[9px] leading-tight truncate'
                                        ).style(f'color:{barva_ab}')
                                extra_ab = len(abses_dne) - 2
                                if extra_ab > 0:
                                    ui.label(f'+{extra_ab} absence').classes(
                                        'text-[9px] text-amber-500 italic leading-tight')

    # ==========================================
    # PRAVÝ PANEL – detail dne
    # ==========================================
    def _draw_panel(skupina, je_ved, lide, absence_data=None):
        absence_data = absence_data or {}
        datum_str  = stav['vybran_den']
        datum      = datetime.date.fromisoformat(datum_str)
        is_weekend = datum.weekday() >= 5
        smeny      = _smeny_den_detail(skupina['id'], datum_str)

        # Seskupit podle názvu směny
        skupiny_smen: dict = {}
        for sm in smeny:
            skupiny_smen.setdefault(sm['nazev'], []).append(sm)

        with ui.column().classes('w-full h-full'):

            # ── Panel – záhlaví ───────────────────────────────────────────
            with ui.row().classes(
                'w-full justify-between items-center px-5 py-3 border-b border-gray-200'
            ):
                with ui.column().classes('gap-0'):
                    ui.label(f'{datum.day}. {MESICE_2[datum.month]}').classes(
                        'text-xl font-bold text-gray-800')
                    badges = []
                    if datum == dnes:
                        badges.append('Dnes')
                    badges.append(DNY[datum.weekday()])
                    ui.label(' · '.join(badges)).classes(
                        f'text-xs font-medium '
                        f'{"text-blue-500" if datum == dnes else "text-gray-400"}')

                with ui.row().classes('gap-1 items-center'):
                    if je_ved:
                        ui.button(
                            icon='add',
                            on_click=lambda: _dialog_smena(skupina, datum_str, lide, None)
                        ).props('flat round').classes(
                            'text-blue-600 bg-blue-50 hover:bg-blue-100 w-9 h-9')
                        ui.button(
                            icon='delete_sweep',
                            on_click=lambda: _dialog_hromadne_mazani(skupina, datum_str)
                        ).props('flat round').classes(
                            'text-red-500 bg-red-50 hover:bg-red-100 w-9 h-9')
                    def _zavre():
                        stav['vybran_den'] = None
                        render.refresh()
                    ui.button(icon='close', on_click=_zavre).props(
                        'flat round dense').classes('text-gray-400')

            # ── Absence dne ───────────────────────────────────────────────
            abses_dne = absence_data.get(datum_str, [])
            if abses_dne:
                with ui.column().classes(
                    'w-full px-5 py-2 bg-amber-50/60 border-b border-amber-200 gap-1'
                ):
                    ui.label('Absence').classes(
                        'text-[10px] font-bold text-amber-700 uppercase tracking-wider mb-0.5')
                    for ab in abses_dne:
                        barva, ikona = _abs_styl(ab['typ'])
                        with ui.row().classes('items-center gap-2 py-0.5'):
                            ui.element('div').style(
                                f'width:8px;height:8px;border-radius:50%;'
                                f'background:{barva};flex-shrink:0'
                            )
                            ui.label(ab['user_name']).classes(
                                'text-xs text-gray-700 flex-1 truncate')
                            ui.label(f'{ikona} {ab["typ"]}').classes(
                                'text-xs font-semibold flex-shrink-0'
                            ).style(f'color:{barva}')

            # ── Panel – obsah ─────────────────────────────────────────────
            if not skupiny_smen:
                with ui.column().classes('w-full items-center justify-center py-20 gap-3'):
                    ui.label('📭').classes('text-5xl')
                    ui.label('Žádné směny').classes('text-gray-400 font-medium')
                    if je_ved:
                        ui.label('Klikněte + pro přidání první směny').classes(
                            'text-xs text-gray-300 text-center max-w-[160px]')
                return

            with ui.column().classes('w-full gap-0'):
                for typ_nazev, typ_smeny in skupiny_smen.items():

                    # Záhlaví skupiny
                    with ui.row().classes(
                        'w-full justify-between items-center '
                        'px-5 py-2.5 bg-gray-50/80 border-b border-gray-100'
                    ):
                        ui.label(typ_nazev).classes('font-bold text-gray-800 text-sm')
                        if je_ved:
                            ui.button(
                                'Přidat směnu',
                                on_click=lambda tn=typ_nazev: _dialog_smena(
                                    skupina, datum_str, lide, None, predvyplnit_nazev=tn)
                            ).props('flat dense no-caps').classes(
                                'text-blue-500 text-xs font-semibold hover:text-blue-700')

                    # Řádky jednotlivých směn
                    for sm in typ_smeny:
                        with ui.row().classes(
                            'w-full items-start gap-2 px-5 py-2.5 '
                            'border-b border-gray-100 hover:bg-gray-50/60 transition-colors'
                        ):
                            ui.icon('lock_open', size='xs').classes(
                                'text-gray-200 mt-0.5 shrink-0')

                            with ui.column().classes('flex-1 gap-0.5 min-w-0'):
                                with ui.row().classes('items-center gap-1.5 flex-nowrap'):
                                    ui.label(f'{sm["cas_od_fmt"]} – {sm["cas_do_fmt"]}').classes(
                                        'text-sm font-semibold text-gray-700 leading-tight')
                                    je_den = _typ_smeny(sm['cas_od'], sm['cas_do']) == 'den'
                                    ui.label(('☀️ Den' if je_den else '🌙 Noc')).classes(
                                        'text-[10px] font-bold rounded px-1.5 py-0.5 leading-none ' +
                                        ('text-amber-700 bg-amber-100'
                                         if je_den else 'text-indigo-700 bg-indigo-100'))
                                if sm.get('lide'):
                                    for os in sm['lide']:
                                        barva_os = _barva_uzivatele(os['user_id'])
                                        with ui.row().classes('items-center gap-1.5'):
                                            ui.element('div').style(
                                                f'width:10px;height:10px;border-radius:50%;'
                                                f'background:{barva_os};flex-shrink:0'
                                            )
                                            ui.label(os['user_name']).classes(
                                                'text-xs text-gray-600 truncate')
                                else:
                                    ui.label('Nikdo nepřiřazen').classes(
                                        'text-xs text-gray-300 italic')
                                if sm.get('popis'):
                                    ui.label(sm['popis']).classes(
                                        'text-xs text-gray-400 italic mt-0.5 truncate')

                            with ui.row().classes('gap-0 shrink-0 -mr-1'):
                                if je_ved:
                                    ui.button(
                                        icon='mode_edit',
                                        on_click=lambda s=sm: _dialog_smena(skupina, datum_str, lide, s)
                                    ).props('flat round dense').classes(
                                        'text-gray-300 hover:text-blue-400')
                                    ui.button(
                                        icon='close',
                                        on_click=lambda s=sm: _smazat_smenu(s['id'])
                                    ).props('flat round dense').classes(
                                        'text-gray-300 hover:text-red-400')

    # ==========================================
    # PŘEHLED VYTÍŽENÍ – pruh pod hlavičkou (jen vedoucí)
    # ==========================================
    def _draw_fond_prehled(radky, zaklad, rok, mesic):
        """Kompaktní pruh: u koho kolik z fondu je zabráno (na první pohled)."""
        if not radky:
            return
        with ui.column().classes('w-full px-3 md:px-4 pt-3 pb-1 gap-1.5 '
                                 'border-b border-gray-100 bg-gray-50/40'):
            with ui.row().classes('w-full items-center gap-2'):
                ui.icon('insights').classes('text-teal-600 text-lg')
                ui.label(f'Vytížení — {MESICE[mesic]} {rok}').classes(
                    'text-xs font-bold text-gray-600 uppercase tracking-wide')
                ui.label('naplánováno / fond hodin').classes(
                    'text-[11px] text-gray-400')
            with ui.row().classes('w-full gap-2 flex-nowrap overflow-x-auto pb-1'):
                for rad in radky:
                    fond = rad['fond']
                    napl = rad['naplanovano']
                    zbyva = round(fond - napl, 2)
                    pct = min(100, (napl / fond * 100) if fond > 0 else 0)
                    over = rad['prescas'] > 0
                    if over:
                        bar_col = '#f97316'; txt_col = 'text-orange-600'
                    elif fond > 0 and zbyva <= fond * 0.1:
                        bar_col = '#f59e0b'; txt_col = 'text-amber-600'
                    else:
                        bar_col = '#14b8a6'; txt_col = 'text-teal-600'
                    with ui.column().classes(
                        'flex-shrink-0 w-44 bg-white border border-gray-200 rounded-xl '
                        'p-2.5 gap-1 shadow-sm'):
                        with ui.row().classes('items-center gap-1.5 flex-nowrap w-full'):
                            ui.element('div').style(
                                f'width:9px;height:9px;border-radius:50%;'
                                f'background:{_barva_uzivatele(rad["user_id"])};flex-shrink:0')
                            ui.label(rad['jmeno']).classes(
                                'text-xs font-semibold text-gray-700 truncate')
                        with ui.row().classes('items-baseline gap-1'):
                            ui.label(_fmt_h(napl)).classes(
                                f'text-lg font-black {txt_col} leading-none')
                            ui.label(f'/ {_fmt_h(fond)} h').classes('text-xs text-gray-400')
                        with ui.element('div').classes(
                            'w-full h-1.5 rounded-full bg-gray-100 overflow-hidden'):
                            ui.element('div').style(
                                f'width:{pct}%;height:100%;background:{bar_col};'
                                f'border-radius:9999px')
                        if over:
                            ui.label(f'+{_fmt_h(rad["prescas"])} h přesčas').classes(
                                'text-[11px] font-bold text-orange-600 leading-none')
                        else:
                            ui.label(f'zbývá {_fmt_h(zbyva)} h').classes(
                                'text-[11px] font-medium text-gray-500 leading-none')

    # ==========================================
    # PŘEPOČET HODIN – tabulka pod kalendářem
    # ==========================================
    def _draw_prepocet(skupina, oddeleni, lide, rok, mesic, radky=None, zaklad=None):
        if radky is None:
            radky, zaklad = _napocet_mesic(skupina['id'], rok, mesic, lide)

        def _bunka(text, sirka, classes=''):
            ui.label(text).classes(
                f'{sirka} text-center text-sm {classes}')

        with ui.column().classes('w-full px-3 md:px-4 pb-10'):
            exp = ui.expansion(
                f'Přepočet hodin — {MESICE[mesic]} {rok}', icon='schedule',
                value=stav['prepocet_open']
            ).classes(
                'w-full border border-gray-200 rounded-xl bg-white shadow-sm'
            ).props('header-class="text-base font-bold text-gray-800"')
            exp.on_value_change(lambda e: stav.update({'prepocet_open': e.value}))
            with exp:

                ui.label(
                    f'Fond plného úvazku v měsíci: {_fmt_h(zaklad)} h '
                    f'(pracovní dny mimo státní svátky × 8 h). '
                    f'Naplánované hodiny nad fond se počítají jako přesčas.'
                ).classes('text-xs text-gray-500 px-3 pt-2 pb-3')

                if not radky:
                    with ui.column().classes('w-full items-center py-8 gap-2'):
                        ui.label('👥').classes('text-4xl')
                        ui.label('V oddělení nejsou žádní zaměstnanci.').classes(
                            'text-sm text-gray-400')
                    return

                # Záhlaví tabulky
                with ui.row().classes(
                    'w-full items-center gap-1 px-3 py-2 bg-gray-50 '
                    'border-y border-gray-200 flex-nowrap'
                ):
                    ui.label('Zaměstnanec').classes(
                        'flex-1 min-w-[140px] text-xs font-bold text-gray-500 uppercase')
                    _bunka('Úvazek', 'w-32', 'text-xs font-bold text-gray-500 uppercase')
                    _bunka('Fond', 'w-20', 'text-xs font-bold text-gray-500 uppercase')
                    _bunka('Naplán.', 'w-20', 'text-xs font-bold text-gray-500 uppercase')
                    _bunka('Den', 'w-14', 'text-xs font-bold text-gray-500 uppercase')
                    _bunka('Noc', 'w-14', 'text-xs font-bold text-gray-500 uppercase')
                    _bunka('Rozdíl', 'w-20', 'text-xs font-bold text-gray-500 uppercase')
                    _bunka('Přesčas', 'w-20', 'text-xs font-bold text-gray-500 uppercase')

                sum_fond = sum_napl = sum_pres = 0.0
                for rad in radky:
                    sum_fond += rad['fond']
                    sum_napl += rad['naplanovano']
                    sum_pres += rad['prescas']
                    with ui.row().classes(
                        'w-full items-center gap-1 px-3 py-2 border-b border-gray-100 '
                        'hover:bg-gray-50/60 flex-nowrap'
                    ):
                        with ui.row().classes('flex-1 min-w-[140px] items-center gap-2'):
                            ui.element('div').style(
                                f'width:10px;height:10px;border-radius:50%;'
                                f'background:{_barva_uzivatele(rad["user_id"])};flex-shrink:0')
                            ui.label(rad['jmeno']).classes('text-sm text-gray-700 truncate')

                        # Úvazek – klikací chip
                        rezim_lbl = REZIM_NAZEV.get(rad['rezim'], 'Plný')
                        if rad['rezim'] == 'castecny':
                            rezim_lbl = f'Částečný {rad["uvazek_pct"]} %'
                        with ui.element('div').classes('w-32 flex justify-center'):
                            ui.button(
                                rezim_lbl,
                                on_click=lambda r=rad: _dialog_uvazek(
                                    skupina, r['user_id'], r['jmeno'])
                            ).props('flat dense no-caps').classes(
                                'text-xs font-semibold text-teal-700 bg-teal-50 '
                                'hover:bg-teal-100 rounded-lg px-2 h-7')

                        # Fond – klikací (override s odůvodněním)
                        with ui.element('div').classes('w-20 flex justify-center'):
                            fond_txt = _fmt_h(rad['fond'])
                            btn = ui.button(
                                (f'✎ {fond_txt}' if rad['fond_override'] else fond_txt),
                                on_click=lambda r=rad: _dialog_fond(
                                    skupina, r['user_id'], r['jmeno'], rok, mesic,
                                    zaklad, r)
                            ).props('flat dense no-caps').classes(
                                'text-sm font-semibold rounded-lg px-2 h-7 ' +
                                ('text-amber-700 bg-amber-50 hover:bg-amber-100'
                                 if rad['fond_override']
                                 else 'text-gray-700 hover:bg-gray-100'))
                            if rad['fond_override'] and rad['duvod']:
                                btn.tooltip(f'Ručně upraveno: {rad["duvod"]}')

                        _bunka(_fmt_h(rad['naplanovano']), 'w-20',
                               'font-semibold text-gray-700')
                        _bunka(_fmt_h(rad['den_h']), 'w-14', 'text-gray-500')
                        _bunka(_fmt_h(rad['noc_h']), 'w-14', 'text-indigo-500')

                        rozd = rad['rozdil']
                        _bunka(('+' if rozd > 0 else '') + _fmt_h(rozd), 'w-20',
                               'font-bold ' + ('text-green-600' if rozd >= 0 else 'text-red-500'))

                        if rad['prescas'] > 0:
                            with ui.element('div').classes('w-20 flex justify-center'):
                                ui.label('+' + _fmt_h(rad['prescas'])).classes(
                                    'text-sm font-bold text-orange-600 bg-orange-50 '
                                    'rounded-lg px-2')
                        else:
                            _bunka('—', 'w-20', 'text-gray-300')

                # Součtový řádek
                with ui.row().classes(
                    'w-full items-center gap-1 px-3 py-2.5 bg-gray-50 '
                    'border-t-2 border-gray-300 flex-nowrap'
                ):
                    ui.label('Celkem').classes(
                        'flex-1 min-w-[140px] text-sm font-bold text-gray-700')
                    _bunka('', 'w-32')
                    _bunka(_fmt_h(sum_fond), 'w-20', 'font-bold text-gray-700')
                    _bunka(_fmt_h(sum_napl), 'w-20', 'font-bold text-gray-700')
                    _bunka('', 'w-14')
                    _bunka('', 'w-14')
                    _bunka('', 'w-20')
                    _bunka(('+' + _fmt_h(sum_pres)) if sum_pres > 0 else '—', 'w-20',
                           'font-bold ' + ('text-orange-600' if sum_pres > 0 else 'text-gray-300'))

    # ==========================================
    # DIALOG – úvazek osoby
    # ==========================================
    def _dialog_uvazek(skupina, uid, jmeno):
        akt = _uvazky_skupiny(skupina['id']).get(uid, {'rezim': 'plny', 'uvazek_pct': 100})
        with ui.dialog() as dlg, \
             ui.card().classes('w-full max-w-md p-6 rounded-2xl gap-4'):
            with ui.row().classes('w-full justify-between items-center'):
                ui.label('Úvazek zaměstnance').classes('text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')
            ui.label(jmeno).classes('text-sm text-gray-500 font-bold -mt-2')

            rezim_sel = ui.radio(
                {'plny': 'Plný úvazek (100 %)',
                 'polovicni': 'Poloviční úvazek (50 %)',
                 'castecny': 'Částečný úvazek (vlastní %)'},
                value=akt.get('rezim', 'plny')
            ).props('color=teal').classes('w-full')

            pct_inp = ui.number(
                'Výše úvazku (%)',
                value=(akt.get('uvazek_pct', 75) if akt.get('rezim') == 'castecny' else 75),
                min=1, max=100, step=5
            ).classes('w-full bg-white').props('outlined suffix=%')
            pct_inp.bind_visibility_from(rezim_sel, 'value', lambda v: v == 'castecny')

            async def _uloz():
                rezim = rezim_sel.value
                if rezim == 'plny':
                    pct = 100
                elif rezim == 'polovicni':
                    pct = 50
                else:
                    try:
                        pct = int(pct_inp.value or 0)
                    except (TypeError, ValueError):
                        pct = 0
                    if pct < 1 or pct > 100:
                        return ui.notify('Zadejte úvazek 1–100 %.', type='warning')

                def _db():
                    c = intranet_data.get_db_connection()
                    cr = None
                    try:
                        cr = c.cursor()
                        cr.execute(
                            "INSERT INTO smeny_uvazek (skupina_id,user_id,rezim,uvazek_pct,updated_by) "
                            "VALUES (%s,%s,%s,%s,%s) "
                            "ON DUPLICATE KEY UPDATE rezim=VALUES(rezim),"
                            "uvazek_pct=VALUES(uvazek_pct),updated_by=VALUES(updated_by)",
                            (skupina['id'], uid, rezim, pct, user_name))
                        c.commit()
                    finally:
                        if cr: cr.close()
                        if c:  c.close()
                await asyncio.to_thread(_db)
                intranet_logger.log_activity(
                    user_name, 'Směny',
                    f'Úvazek: {jmeno} → {REZIM_NAZEV.get(rezim, rezim)} ({pct} %) [{skupina["nazev"]}]')
                dlg.close()
                ui.notify('Úvazek uložen.', type='positive')
                render.refresh()

            with ui.row().classes('w-full justify-end gap-3 mt-2'):
                ui.button('Zrušit', on_click=dlg.close).classes(
                    'bg-gray-200 text-gray-700 font-bold h-11 px-6 rounded-xl')
                ui.button('Uložit', on_click=_uloz).classes(
                    'bg-teal-600 hover:bg-teal-700 text-white font-bold h-11 px-6 rounded-xl')
        dlg.open()

    # ==========================================
    # DIALOG – ruční úprava fondu (s odůvodněním)
    # ==========================================
    def _dialog_fond(skupina, uid, jmeno, rok, mesic, zaklad, rad):
        vypocet = round(zaklad * (rad['uvazek_pct'] or 100) / 100.0, 2)
        with ui.dialog() as dlg, \
             ui.card().classes('w-full max-w-md p-6 rounded-2xl gap-4'):
            with ui.row().classes('w-full justify-between items-center'):
                ui.label('Úprava fondu hodin').classes('text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')
            ui.label(f'{jmeno} — {MESICE[mesic]} {rok}').classes(
                'text-sm text-gray-500 font-bold -mt-2')

            with ui.column().classes(
                'w-full bg-gray-50 rounded-xl p-3 gap-0.5 border border-gray-200'):
                ui.label(f'Vypočtený fond dle úvazku: {_fmt_h(vypocet)} h').classes(
                    'text-sm text-gray-600')
                ui.label(f'(fond plného úvazku {_fmt_h(zaklad)} h × {rad["uvazek_pct"]} %)').classes(
                    'text-xs text-gray-400')

            fond_inp = ui.number(
                'Nový fond hodin', value=rad['fond'], min=0, step=0.5
            ).classes('w-full bg-white').props('outlined suffix=h')
            duvod_inp = ui.textarea(
                'Odůvodnění úpravy *', value=rad['duvod'] if rad['fond_override'] else ''
            ).classes('w-full bg-white').props('outlined rows=2')

            async def _uloz():
                try:
                    fond = round(float(fond_inp.value), 2)
                except (TypeError, ValueError):
                    return ui.notify('Zadejte platný počet hodin.', type='warning')
                if fond < 0:
                    return ui.notify('Fond nemůže být záporný.', type='warning')
                duvod = (duvod_inp.value or '').strip()
                if not duvod:
                    return ui.notify('Vyplňte odůvodnění úpravy fondu.', type='warning')

                def _db():
                    c = intranet_data.get_db_connection()
                    cr = None
                    try:
                        cr = c.cursor()
                        cr.execute(
                            "INSERT INTO smeny_fond_override "
                            "(skupina_id,user_id,rok,mesic,fond_hodin,duvod,updated_by) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                            "ON DUPLICATE KEY UPDATE fond_hodin=VALUES(fond_hodin),"
                            "duvod=VALUES(duvod),updated_by=VALUES(updated_by)",
                            (skupina['id'], uid, rok, mesic, fond, duvod, user_name))
                        c.commit()
                    finally:
                        if cr: cr.close()
                        if c:  c.close()
                await asyncio.to_thread(_db)
                intranet_logger.log_activity(
                    user_name, 'Směny',
                    f'Úprava fondu: {jmeno} {MESICE[mesic]} {rok} → {_fmt_h(fond)} h '
                    f'({duvod}) [{skupina["nazev"]}]')
                dlg.close()
                ui.notify('Fond upraven.', type='positive')
                render.refresh()

            async def _vratit():
                def _db():
                    c = intranet_data.get_db_connection()
                    cr = None
                    try:
                        cr = c.cursor()
                        cr.execute(
                            "DELETE FROM smeny_fond_override "
                            "WHERE skupina_id=%s AND user_id=%s AND rok=%s AND mesic=%s",
                            (skupina['id'], uid, rok, mesic))
                        c.commit()
                    finally:
                        if cr: cr.close()
                        if c:  c.close()
                await asyncio.to_thread(_db)
                intranet_logger.log_activity(
                    user_name, 'Směny',
                    f'Fond vrácen na výpočet: {jmeno} {MESICE[mesic]} {rok} [{skupina["nazev"]}]')
                dlg.close()
                ui.notify('Fond vrácen na automatický výpočet.', type='info')
                render.refresh()

            with ui.row().classes('w-full justify-between items-center gap-3 mt-2'):
                if rad['fond_override']:
                    ui.button('↺ Vrátit na výpočet', on_click=_vratit).props('flat no-caps').classes(
                        'text-gray-500 hover:text-gray-700 text-sm')
                else:
                    ui.element('div')
                with ui.row().classes('gap-3'):
                    ui.button('Zrušit', on_click=dlg.close).classes(
                        'bg-gray-200 text-gray-700 font-bold h-11 px-6 rounded-xl')
                    ui.button('Uložit', on_click=_uloz).classes(
                        'bg-teal-600 hover:bg-teal-700 text-white font-bold h-11 px-6 rounded-xl')
        dlg.open()

    # ==========================================
    # DIALOG – přidat / upravit směnu
    # ==========================================
    def _dialog_smena(skupina, datum_str, lide, smena_edit=None, predvyplnit_nazev=''):
        import calendar as _cal
        je_edit   = smena_edit is not None
        datum_obj = datetime.date.fromisoformat(datum_str)

        # Sada vybraných dní — vždy obsahuje alespoň původní datum
        vybrany_dny = {datum_str}

        with ui.dialog() as dlg, \
             ui.card().classes('w-full max-w-lg p-6 rounded-2xl gap-4'):

            with ui.row().classes('w-full justify-between items-center'):
                ui.label('Upravit směnu' if je_edit else 'Nová směna').classes(
                    'text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')

            ui.label(f'📅 {datum_obj.strftime("%d.%m.%Y")} — {skupina["nazev"]}').classes(
                'text-sm text-gray-500 font-bold -mt-2')

            nazev_inp = ui.input(
                'Název směny *',
                value=smena_edit['nazev'] if je_edit else predvyplnit_nazev
            ).classes('w-full bg-white').props('outlined')

            with ui.row().classes('w-full gap-4'):
                cas_od_inp = ui.input(
                    'Začátek *',
                    value=smena_edit['cas_od_fmt'] if je_edit else '08:00'
                ).classes('flex-1 bg-white').props('outlined mask="##:##" fill-mask')
                cas_do_inp = ui.input(
                    'Konec *',
                    value=smena_edit['cas_do_fmt'] if je_edit else '16:00'
                ).classes('flex-1 bg-white').props('outlined mask="##:##" fill-mask')

            popis_inp = ui.textarea(
                'Poznámka',
                value=smena_edit.get('popis', '') if je_edit else ''
            ).classes('w-full bg-white').props('outlined rows=2')

            # ── Výběr více dní (pouze pro nové směny) ────────────────────────
            if not je_edit:
                nav = {'rok': datum_obj.year, 'mesic': datum_obj.month}

                with ui.expansion('Přidat na více dní', icon='date_range').classes(
                    'w-full border border-blue-100 rounded-xl bg-blue-50/30'
                ):
                    @ui.refreshable
                    def _vyber_dni():
                        rok, mes = nav['rok'], nav['mesic']
                        with ui.column().classes('w-full gap-1 p-1'):
                            with ui.row().classes('w-full justify-between items-center'):
                                def _prev_mes():
                                    if nav['mesic'] == 1: nav['mesic'] = 12; nav['rok'] -= 1
                                    else: nav['mesic'] -= 1
                                    _vyber_dni.refresh()
                                def _next_mes():
                                    if nav['mesic'] == 12: nav['mesic'] = 1; nav['rok'] += 1
                                    else: nav['mesic'] += 1
                                    _vyber_dni.refresh()
                                ui.button(icon='chevron_left', on_click=_prev_mes).props('flat round dense').classes('text-gray-500')
                                ui.label(f'{MESICE[mes]} {rok}').classes('font-bold text-gray-700 text-sm')
                                ui.button(icon='chevron_right', on_click=_next_mes).props('flat round dense').classes('text-gray-500')

                            with ui.row().classes('gap-0.5 justify-center'):
                                for zkr in DNY:
                                    ui.label(zkr).classes('w-9 text-center text-[10px] text-gray-400 font-bold')

                            for tyden in _cal.monthcalendar(rok, mes):
                                with ui.row().classes('gap-0.5 justify-center'):
                                    for den in tyden:
                                        if den == 0:
                                            ui.element('div').classes('w-9 h-9')
                                        else:
                                            ds = f'{rok}-{mes:02d}-{den:02d}'
                                            vybran     = ds in vybrany_dny
                                            je_puvodni = ds == datum_str

                                            def _toggle(d=ds, jp=je_puvodni):
                                                if d in vybrany_dny:
                                                    if not jp:
                                                        vybrany_dny.discard(d)
                                                else:
                                                    vybrany_dny.add(d)
                                                _vyber_dni.refresh()
                                                _info_dny.refresh()

                                            if je_puvodni:
                                                styl = 'bg-blue-600 text-white font-bold'
                                            elif vybran:
                                                styl = 'bg-blue-200 text-blue-800 font-bold'
                                            else:
                                                styl = 'bg-white text-gray-700 hover:bg-blue-50 border border-gray-200'
                                            ui.button(str(den), on_click=_toggle, color=None).classes(
                                                f'w-9 h-9 rounded-lg text-xs p-0 {styl}')

                            pocet = len(vybrany_dny)
                            ui.label(f'Vybráno: {pocet} {"den" if pocet == 1 else "dny" if pocet < 5 else "dní"}').classes(
                                f'text-xs {"text-blue-600 font-bold" if pocet > 1 else "text-gray-400"} text-center mt-1')

                    _vyber_dni()

            # Předvybrané osoby (u úpravy) + živý stav zaškrtnutí pro info panel
            vybrani = ({os['user_id'] for os in smena_edit.get('lide', [])}
                       if je_edit else set())
            prirazeni_sel = set(vybrani)

            # ── Info o vybraných dnech: absence + již existující směny ───────
            # Kolize u osob, které právě přiřazuješ (prirazeni_sel), se zvýrazní červeně.
            @ui.refreshable
            def _info_dny():
                dny = sorted(vybrany_dny)
                if not dny:
                    return
                d_min, d_max = dny[0], dny[-1]
                uids = list(lide.keys()) if lide else None
                try:
                    abs_map = _absence_rozsah(d_min, d_max, uids)
                except Exception:
                    abs_map = {}
                try:
                    sm_map = _smeny_rozsah(skupina['id'], d_min, d_max)
                except Exception:
                    sm_map = {}

                # Pro každý vybraný den sesbírej absence a (cizí) směny
                bloky = []
                ma_konflikt = False   # kolize konkrétně s přiřazovanou osobou
                for ds in dny:
                    d_obj   = datetime.date.fromisoformat(ds)
                    abs_dne = abs_map.get(ds, [])
                    sm_dne  = [s for s in sm_map.get(ds, [])
                               if not (je_edit and s['id'] == smena_edit['id'])]
                    if abs_dne or sm_dne:
                        if any(ab['user_id'] in prirazeni_sel for ab in abs_dne) or \
                           any(o['user_id'] in prirazeni_sel
                               for s in sm_dne for o in s.get('lide', [])):
                            ma_konflikt = True
                        bloky.append((d_obj, abs_dne, sm_dne))

                if not bloky:
                    with ui.row().classes(
                        'w-full items-center gap-2 px-3 py-2 rounded-xl '
                        'bg-emerald-50 border border-emerald-100'
                    ):
                        ui.icon('check_circle', size='xs').classes('text-emerald-500')
                        ui.label('Na vybrané dny nikdo nemá absenci ani jinou směnu.'
                                 ).classes('text-xs text-emerald-700')
                    return

                box_styl = ('bg-red-50 border-red-300' if ma_konflikt
                            else 'bg-amber-50/60 border-amber-200')
                with ui.column().classes(
                    f'w-full gap-1.5 px-3 py-2.5 rounded-xl border {box_styl}'
                ):
                    with ui.row().classes('items-center gap-1.5'):
                        if ma_konflikt:
                            ui.icon('error', size='xs').classes('text-red-600')
                            ui.label('Kolize s přiřazovanou osobou!').classes(
                                'text-[11px] font-bold text-red-700 uppercase tracking-wide')
                        else:
                            ui.icon('warning', size='xs').classes('text-amber-600')
                            ui.label('Na vybrané dny — pozor:').classes(
                                'text-[11px] font-bold text-amber-700 uppercase tracking-wide')
                    with ui.column().classes('w-full gap-1 max-h-44 overflow-y-auto'):
                        for d_obj, abs_dne, sm_dne in bloky:
                            ui.label(
                                f'{d_obj.day}. {MESICE_2[d_obj.month]} · {DNY[d_obj.weekday()]}'
                            ).classes('text-[11px] font-bold text-gray-500 mt-0.5')
                            for ab in abs_dne:
                                barva, ikona = _abs_styl(ab['typ'])
                                konf = ab['user_id'] in prirazeni_sel
                                with ui.row().classes(
                                    'items-center gap-1.5 flex-nowrap pl-1 w-full rounded'
                                    + (' px-1 bg-red-100' if konf else '')
                                ):
                                    ui.label(ikona).classes('text-xs leading-none flex-shrink-0')
                                    ui.label(ab['user_name']).classes(
                                        'text-xs truncate '
                                        + ('font-bold text-red-700' if konf else 'text-gray-700'))
                                    ui.label(ab['typ']).classes(
                                        'text-[10px] font-semibold px-1.5 py-0.5 rounded '
                                        'flex-shrink-0'
                                    ).style(f'color:{barva};background:{barva}1a')
                                    if konf:
                                        ui.label('přiřazujete').classes(
                                            'text-[10px] font-bold text-red-600 flex-shrink-0')
                            for s in sm_dne:
                                lide_s = s.get('lide', [])
                                konf   = any(o['user_id'] in prirazeni_sel for o in lide_s)
                                osoby  = ', '.join(o['user_name'] for o in lide_s) or 'nikdo přiřazen'
                                with ui.row().classes(
                                    'items-center gap-1.5 flex-nowrap pl-1 w-full rounded'
                                    + (' px-1 bg-red-100' if konf else '')
                                ).tooltip('Na tento den už existuje směna'):
                                    ui.label('📋').classes('text-xs leading-none flex-shrink-0')
                                    ui.label(f'{s["cas_od_fmt"]}–{s["cas_do_fmt"]}').classes(
                                        'text-xs font-semibold flex-shrink-0 '
                                        + ('text-red-700' if konf else 'text-gray-600'))
                                    ui.label(osoby).classes(
                                        'text-xs truncate '
                                        + ('font-semibold text-red-700' if konf else 'text-gray-500'))
                                    if konf:
                                        ui.label('už má směnu').classes(
                                            'text-[10px] font-bold text-red-600 flex-shrink-0')

            _info_dny()

            checkboxy = {}

            if lide:
                ui.label('Přiřadit osoby:').classes('text-sm font-bold text-gray-600 -mb-2')
                with ui.column().classes(
                    'w-full gap-0.5 h-44 shrink-0 overflow-y-auto border border-gray-200 '
                    'rounded-xl p-3 bg-gray-50'
                ):
                    for uid, jmeno in lide.items():
                        # Při zaškrtnutí osoby přepočítej info panel (zvýraznění kolizí)
                        def _zmena_osoby(e, u=uid):
                            if e.value:
                                prirazeni_sel.add(u)
                            else:
                                prirazeni_sel.discard(u)
                            _info_dny.refresh()
                        with ui.row().classes('items-center gap-1 py-0.5'):
                            ui.element('div').style(
                                f'width:10px;height:10px;border-radius:50%;'
                                f'background:{_barva_uzivatele(uid)};flex-shrink:0'
                            )
                            cb = ui.checkbox(
                                jmeno, value=(uid in vybrani), on_change=_zmena_osoby
                            ).classes('-ml-1')
                        checkboxy[uid] = cb

            async def _ulozit():
                if not nazev_inp.value.strip():
                    return ui.notify('Vyplňte název směny!', type='warning')
                try:
                    datetime.datetime.strptime(cas_od_inp.value, '%H:%M')
                    datetime.datetime.strptime(cas_do_inp.value, '%H:%M')
                except ValueError:
                    return ui.notify('Neplatný formát času — použijte HH:MM', type='warning')

                _nazev        = nazev_inp.value.strip()
                _od           = cas_od_inp.value
                _do           = cas_do_inp.value
                _popis        = popis_inp.value.strip()
                _barva        = '#94a3b8'
                _lide_vybrani = [
                    (uid, jm) for uid, jm in lide.items()
                    if checkboxy.get(uid) and checkboxy[uid].value
                ]
                _dny = sorted(vybrany_dny)

                def _db():
                    c  = intranet_data.get_db_connection()
                    cr = None
                    try:
                        cr = c.cursor()
                        if je_edit:
                            cr.execute(
                                "UPDATE smeny_smena SET nazev=%s,cas_od=%s,cas_do=%s,"
                                "popis=%s,barva=%s WHERE id=%s",
                                (_nazev, _od, _do, _popis, _barva, smena_edit['id'])
                            )
                            cr.execute(
                                "DELETE FROM smeny_prirazeni WHERE smena_id=%s",
                                (smena_edit['id'],)
                            )
                            smena_id = smena_edit['id']
                            for uid, jm in _lide_vybrani:
                                cr.execute(
                                    "INSERT IGNORE INTO smeny_prirazeni "
                                    "(smena_id,user_id,user_name,created_by) VALUES (%s,%s,%s,%s)",
                                    (smena_id, uid, jm, user_name)
                                )
                        else:
                            for ds in _dny:
                                cr.execute(
                                    "INSERT INTO smeny_smena "
                                    "(skupina_id,nazev,datum,cas_od,cas_do,popis,barva,created_by) "
                                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (skupina['id'], _nazev, ds,
                                     _od, _do, _popis, _barva, user_name)
                                )
                                smena_id = cr.lastrowid
                                for uid, jm in _lide_vybrani:
                                    cr.execute(
                                        "INSERT IGNORE INTO smeny_prirazeni "
                                        "(smena_id,user_id,user_name,created_by) VALUES (%s,%s,%s,%s)",
                                        (smena_id, uid, jm, user_name)
                                    )
                        c.commit()
                    finally:
                        if cr: cr.close()
                        if c:  c.close()

                await asyncio.to_thread(_db)
                pocet_dni = len(_dny)
                intranet_logger.log_activity(
                    user_name, 'Směny',
                    f'{"Upravena" if je_edit else f"Přidána ({pocet_dni} dní)"} směna: '
                    f'{_nazev} ({_dny[0]}{f" … {_dny[-1]}" if pocet_dni > 1 else ""}) [{skupina["nazev"]}]'
                )
                dlg.close()
                ui.notify(
                    f'Směna uložena pro {pocet_dni} {"den" if pocet_dni == 1 else "dny" if pocet_dni < 5 else "dní"}!',
                    type='positive'
                )
                render.refresh()

            with ui.row().classes('w-full justify-end gap-3 mt-2'):
                ui.button('Zrušit', on_click=dlg.close).classes(
                    'bg-gray-200 text-gray-700 font-bold h-11 px-6 rounded-xl')
                ui.button(
                    'Uložit změny' if je_edit else '✅ Vytvořit směnu',
                    on_click=_ulozit
                ).classes(
                    'bg-blue-600 hover:bg-blue-700 text-white font-bold h-11 px-6 rounded-xl shadow-sm')

        dlg.open()

    # ==========================================
    # HROMADNÉ MAZÁNÍ SMĚN (více dní)
    # ==========================================
    def _dialog_hromadne_mazani(skupina, datum_str):
        import calendar as _cal
        datum_obj = datetime.date.fromisoformat(datum_str)
        nav         = {'rok': datum_obj.year, 'mesic': datum_obj.month}
        vybrane_dny = set()   # ds vybraných dní
        mazat_ids   = set()   # id směn ke smazání
        cache_den   = {}      # ds -> [smena_dict, ...]

        def _smeny_dne(ds):
            if ds not in cache_den:
                cache_den[ds] = _smeny_den_detail(skupina['id'], ds)
            return cache_den[ds]

        # Předvyber den, ze kterého bylo mazání otevřeno (má-li směny)
        if _smeny_dne(datum_str):
            vybrane_dny.add(datum_str)
            for sm in _smeny_dne(datum_str):
                mazat_ids.add(sm['id'])

        async def _provest():
            ids = list(mazat_ids)
            if not ids:
                return
            def _db():
                c  = intranet_data.get_db_connection()
                cr = None
                try:
                    cr = c.cursor()
                    ph = ','.join(['%s'] * len(ids))
                    cr.execute(f"DELETE FROM smeny_smena WHERE id IN ({ph})", tuple(ids))
                    c.commit()
                finally:
                    if cr: cr.close()
                    if c:  c.close()
            await asyncio.to_thread(_db)
            intranet_logger.log_activity(
                user_name, 'Směny',
                f'Hromadně smazáno {len(ids)} směn [{skupina["nazev"]}]')
            dlg.close()
            ui.notify(
                f'Smazáno {len(ids)} {"směna" if len(ids) == 1 else "směny" if len(ids) < 5 else "směn"}.',
                type='positive')
            render.refresh()

        with ui.dialog() as dlg, \
             ui.card().classes('w-full max-w-lg p-6 rounded-2xl gap-3 '
                               'max-h-[88vh] overflow-y-auto'):

            with ui.row().classes('w-full justify-between items-center'):
                ui.label('Hromadné mazání směn').classes('text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')
            ui.label(f'{skupina["nazev"]} — vyberte dny a směny ke smazání').classes(
                'text-sm text-gray-500 -mt-2')

            @ui.refreshable
            def _obsah():
                rok, mes = nav['rok'], nav['mesic']
                mesic_smeny = _smeny_rozsah(
                    skupina['id'], f'{rok}-{mes:02d}-01',
                    f'{rok}-{mes:02d}-{_cal.monthrange(rok, mes)[1]:02d}')

                # ── Kalendář pro výběr dní ──
                with ui.column().classes('w-full gap-1'):
                    with ui.row().classes('w-full justify-between items-center'):
                        def _prev():
                            if nav['mesic'] == 1: nav['mesic'] = 12; nav['rok'] -= 1
                            else: nav['mesic'] -= 1
                            _obsah.refresh()
                        def _next():
                            if nav['mesic'] == 12: nav['mesic'] = 1; nav['rok'] += 1
                            else: nav['mesic'] += 1
                            _obsah.refresh()
                        ui.button(icon='chevron_left', on_click=_prev).props(
                            'flat round dense').classes('text-gray-500')
                        ui.label(f'{MESICE[mes]} {rok}').classes('font-bold text-gray-700 text-sm')
                        ui.button(icon='chevron_right', on_click=_next).props(
                            'flat round dense').classes('text-gray-500')

                    with ui.row().classes('gap-0.5 justify-center'):
                        for zkr in DNY:
                            ui.label(zkr).classes('w-9 text-center text-[10px] text-gray-400 font-bold')

                    for tyden in _cal.monthcalendar(rok, mes):
                        with ui.row().classes('gap-0.5 justify-center'):
                            for den in tyden:
                                if den == 0:
                                    ui.element('div').classes('w-9 h-9')
                                    continue
                                ds     = f'{rok}-{mes:02d}-{den:02d}'
                                pocet  = len(mesic_smeny.get(ds, []))
                                vybran = ds in vybrane_dny
                                if pocet == 0:
                                    # Den bez směn – nelze vybrat
                                    ui.label(str(den)).classes(
                                        'w-9 h-9 flex items-center justify-center '
                                        'text-xs text-gray-300')
                                    continue
                                def _toggle_den(d=ds):
                                    if d in vybrane_dny:
                                        vybrane_dny.discard(d)
                                        for s in _smeny_dne(d):
                                            mazat_ids.discard(s['id'])
                                    else:
                                        vybrane_dny.add(d)
                                        for s in _smeny_dne(d):
                                            mazat_ids.add(s['id'])
                                    _obsah.refresh()
                                styl = ('bg-red-500 text-white font-bold' if vybran
                                        else 'bg-blue-50 text-blue-700 hover:bg-red-50 '
                                             'border border-blue-200')
                                ui.button(str(den), on_click=_toggle_den, color=None).classes(
                                    f'w-9 h-9 rounded-lg text-xs p-0 {styl}')

                # ── Seznam směn ke smazání ──
                ui.separator().classes('my-1')
                if not vybrane_dny:
                    ui.label('Vyberte v kalendáři dny (modré = obsahují směny).').classes(
                        'text-xs text-gray-400 text-center py-3')
                else:
                    with ui.column().classes('w-full gap-1 max-h-56 overflow-y-auto'):
                        for ds in sorted(vybrane_dny):
                            smeny_d = _smeny_dne(ds)
                            d_obj   = datetime.date.fromisoformat(ds)
                            vice    = len(smeny_d) > 1
                            ui.label(
                                f'{d_obj.day}. {MESICE_2[d_obj.month]} · {DNY[d_obj.weekday()]}'
                                + ('  — vyberte které směny smazat:' if vice else '')
                            ).classes('text-xs font-bold text-gray-600 mt-1.5')
                            for sm in smeny_d:
                                osoby = ', '.join(o['user_name'] for o in sm['lide']) or 'nikdo'
                                def _toggle_sm(e, sid=sm['id']):
                                    if e.value: mazat_ids.add(sid)
                                    else:       mazat_ids.discard(sid)
                                    _obsah.refresh()
                                with ui.row().classes('items-center gap-2 pl-1 flex-nowrap'):
                                    ui.checkbox(
                                        value=sm['id'] in mazat_ids, on_change=_toggle_sm
                                    ).props('dense')
                                    ui.label(
                                        f'{sm["cas_od_fmt"]}–{sm["cas_do_fmt"]} · {sm["nazev"]}'
                                    ).classes('text-xs text-gray-700 shrink-0')
                                    ui.label(osoby).classes('text-xs text-gray-400 truncate')

                # ── Patička ──
                ui.separator().classes('my-1')
                pocet_mazat = len(mazat_ids)
                with ui.row().classes('w-full justify-end gap-3'):
                    ui.button('Zrušit', on_click=dlg.close).classes(
                        'bg-gray-200 text-gray-700 font-bold h-10 px-5 rounded-xl')
                    btn = ui.button(
                        f'Smazat ({pocet_mazat})', icon='delete', on_click=_provest
                    ).classes('bg-red-600 text-white font-bold h-10 px-5 rounded-xl')
                    if pocet_mazat == 0:
                        btn.props('disable')

            _obsah()

        dlg.open()

    # ==========================================
    # MAZÁNÍ OZNAČENÝCH DNŮ (SHIFT+klik / DEL / pravý klik) – po osobách
    # ==========================================
    def _dialog_mazat_oznacene(skupina, dny, lide):
        """Smaže směny ve dnech označených SHIFT+klikem.
           Má-li v daných dnech směny více lidí, umožní vybrat, kterým."""
        # Načti směny dotčených dní a seskup je po osobách (+ neobsazené)
        cache = {ds: _smeny_den_detail(skupina['id'], ds) for ds in dny}
        osoby = {}        # uid -> {'jmeno', 'polozky': [(ds, smena), ...]}
        neobsazene = []   # [(ds, smena), ...] – směny bez přiřazené osoby
        for ds in dny:
            for sm in cache[ds]:
                if sm.get('lide'):
                    for os in sm['lide']:
                        o = osoby.setdefault(
                            os['user_id'],
                            {'jmeno': os['user_name'], 'polozky': []})
                        o['polozky'].append((ds, sm))
                else:
                    neobsazene.append((ds, sm))

        if not osoby and not neobsazene:
            ui.notify('Ve vybraných dnech nejsou žádné směny ke smazání.', type='info')
            stav['vyber_dny'].clear()
            render.refresh()
            return

        vice_osob = len(osoby) > 1
        sel_osoby = set(osoby.keys())   # přednastaveno = všichni
        sel_neobs = {'v': True}

        def _sklon(n):
            return 'směna' if n == 1 else 'směny' if n < 5 else 'směn'

        def _pocet_mazat():
            n = sum(len(osoby[u]['polozky']) for u in sel_osoby)
            if sel_neobs['v']:
                n += len(neobsazene)
            return n

        async def _provest():
            assign   = [(sm['id'], u) for u in sel_osoby for (ds, sm) in osoby[u]['polozky']]
            affected = {sm['id'] for u in sel_osoby for (ds, sm) in osoby[u]['polozky']}
            orphan   = [sm['id'] for (ds, sm) in neobsazene] if sel_neobs['v'] else []
            if not assign and not orphan:
                return
            dlg.close()

            def _db():
                c  = intranet_data.get_db_connection()
                cr = None
                try:
                    cr = c.cursor()
                    # 1) odeber přiřazení vybraných osob
                    for sid, uid in assign:
                        cr.execute(
                            "DELETE FROM smeny_prirazeni WHERE smena_id=%s AND user_id=%s",
                            (sid, uid))
                    # 2) neobsazené směny smaž rovnou
                    if orphan:
                        ph = ','.join(['%s'] * len(orphan))
                        cr.execute(
                            f"DELETE FROM smeny_smena WHERE id IN ({ph})", tuple(orphan))
                    # 3) ukliď směny, které po odebrání zůstaly bez kohokoliv
                    if affected:
                        ph = ','.join(['%s'] * len(affected))
                        cr.execute(
                            f"DELETE FROM smeny_smena WHERE id IN ({ph}) AND NOT EXISTS "
                            f"(SELECT 1 FROM smeny_prirazeni p "
                            f"WHERE p.smena_id = smeny_smena.id)",
                            tuple(affected))
                    c.commit()
                finally:
                    if cr: cr.close()
                    if c:  c.close()

            await asyncio.to_thread(_db)
            poc = len(assign) + len(orphan)
            intranet_logger.log_activity(
                user_name, 'Směny',
                f'Smazáno {poc} směn ve {len(dny)} označených dnech [{skupina["nazev"]}]')
            ui.notify(f'Smazáno {poc} {_sklon(poc)}.', type='positive')
            stav['vyber_dny'].clear()
            render.refresh()

        with ui.dialog() as dlg, \
             ui.card().classes('w-full max-w-md p-6 rounded-2xl gap-3 '
                               'max-h-[88vh] overflow-y-auto'):

            with ui.row().classes('w-full justify-between items-center'):
                ui.label('Smazat směny').classes('text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')

            dny_txt = ', '.join(
                f'{datetime.date.fromisoformat(d).day}. {MESICE_2[datetime.date.fromisoformat(d).month]}'
                for d in dny)
            den_slovo = 'den' if len(dny) == 1 else 'dny' if len(dny) < 5 else 'dní'
            ui.label(f'{skupina["nazev"]} · {len(dny)} {den_slovo}: {dny_txt}').classes(
                'text-sm text-gray-500 -mt-2')

            @ui.refreshable
            def _telo():
                if vice_osob:
                    ui.label('V těchto dnech mají směny různí lidé — '
                             'vyberte, komu je smazat:').classes(
                        'text-xs font-semibold text-gray-600 mt-1')

                with ui.column().classes('w-full gap-0.5 max-h-72 overflow-y-auto'):
                    for uid in sorted(osoby, key=lambda u: osoby[u]['jmeno']):
                        o     = osoby[uid]
                        poc   = len(o['polozky'])
                        barva = _barva_uzivatele(uid)
                        def _tg(_e, u=uid):
                            sel_osoby.discard(u) if u in sel_osoby else sel_osoby.add(u)
                            _telo.refresh()
                        with ui.row().classes(
                            'items-center gap-2 w-full flex-nowrap '
                            'rounded-lg px-2 py-1 hover:bg-gray-50'
                        ):
                            ui.checkbox(value=uid in sel_osoby, on_change=_tg).props('dense')
                            ui.element('div').style(
                                f'width:10px;height:10px;border-radius:50%;'
                                f'background:{barva};flex-shrink:0')
                            ui.label(o['jmeno']).classes(
                                'text-sm text-gray-700 flex-1 truncate')
                            ui.label(f'{poc} {_sklon(poc)}').classes(
                                'text-xs text-gray-400 shrink-0')

                    if neobsazene:
                        def _tg_n(e):
                            sel_neobs['v'] = bool(e.value)
                            _telo.refresh()
                        with ui.row().classes(
                            'items-center gap-2 w-full flex-nowrap '
                            'rounded-lg px-2 py-1 hover:bg-gray-50'
                        ):
                            ui.checkbox(value=sel_neobs['v'], on_change=_tg_n).props('dense')
                            ui.icon('block', size='xs').classes('text-gray-300')
                            ui.label('Neobsazené směny').classes(
                                'text-sm text-gray-500 italic flex-1 truncate')
                            n = len(neobsazene)
                            ui.label(f'{n} {_sklon(n)}').classes(
                                'text-xs text-gray-400 shrink-0')

                ui.separator().classes('my-1')
                poc_m = _pocet_mazat()
                with ui.row().classes('w-full justify-end gap-3'):
                    ui.button('Zrušit', on_click=dlg.close).classes(
                        'bg-gray-200 text-gray-700 font-bold h-10 px-5 rounded-xl')
                    btn = ui.button(f'Smazat ({poc_m})', icon='delete',
                                    on_click=_provest).classes(
                        'bg-red-600 hover:bg-red-700 text-white font-bold '
                        'h-10 px-5 rounded-xl')
                    if poc_m == 0:
                        btn.props('disable')

            _telo()

        dlg.open()

    # ==========================================
    # SMAZAT SMĚNU
    # ==========================================
    async def _smazat_smenu(smena_id):
        with ui.dialog() as potvrz, \
             ui.card().classes('p-6 rounded-xl w-full max-w-sm'):
            ui.label('Smazat směnu?').classes('text-xl font-bold mb-2 text-red-700')
            ui.label(
                'Směna bude odstraněna včetně všech přiřazení. Tuto akci nelze vrátit.'
            ).classes('text-sm text-gray-600 mb-6')

            async def _provest():
                potvrz.close()
                def _db():
                    c  = intranet_data.get_db_connection()
                    cr = None
                    try:
                        cr = c.cursor()
                        cr.execute("DELETE FROM smeny_smena WHERE id=%s", (smena_id,))
                        c.commit()
                    finally:
                        if cr: cr.close()
                        if c:  c.close()
                await asyncio.to_thread(_db)
                intranet_logger.log_activity(user_name, 'Směny', f'Smazána směna id={smena_id}')
                ui.notify('Směna smazána.', type='info')
                render.refresh()

            with ui.row().classes('w-full justify-between'):
                ui.button('Zrušit', on_click=potvrz.close).classes(
                    'bg-gray-300 text-gray-700 font-bold h-10 px-5 rounded-xl')
                ui.button('Smazat', icon='delete', on_click=_provest).classes(
                    'bg-red-600 text-white font-bold h-10 px-5 rounded-xl')
        potvrz.open()

    # ==========================================
    # SPRÁVA SKUPIN (admin dialog)
    # ==========================================
    def _sprava_skupin_dialog():
        """Admin dialog: ikony a zapnutí/vypnutí oddělení v plánovači."""
        with ui.dialog() as dlg, \
             ui.card().classes('w-full max-w-xl p-6 rounded-2xl max-h-[85vh] overflow-y-auto'):

            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('Nastavení oddělení').classes('text-xl font-bold text-gray-800')
                ui.button(icon='close', on_click=dlg.close).props('flat round')
            ui.label(
                'Oddělení se přidávají automaticky podle struktury ve Správě uživatelů. '
                'Zde lze upravit ikonu nebo oddělení skrýt.'
            ).classes('text-xs text-gray-500 mb-4')

            @ui.refreshable
            def _seznam():
                vsechna_oddeleni = intranet_data.ziskej_vsechna_oddeleni()
                with ui.column().classes('w-full gap-2'):
                    for dept_name in sorted(vsechna_oddeleni.keys()):
                        sk = _ziskej_nebo_vytvor_skupinu(dept_name)
                        if not sk:
                            continue
                        zap = bool(sk.get('zapnuty', 1))
                        with ui.card().classes('w-full p-3 border border-gray-200 rounded-xl'):
                            with ui.row().classes('w-full justify-between items-center gap-3'):
                                ikona_inp = ui.input(value=sk.get('ikona', '📋')).classes(
                                    'w-16 bg-white text-center text-xl').props('outlined dense')
                                ui.label(dept_name).classes('font-bold text-gray-800 flex-1')
                                with ui.row().classes('gap-2 items-center'):
                                    async def _uloz_ikonu(s=sk, ii=ikona_inp):
                                        def _db():
                                            c  = intranet_data.get_db_connection()
                                            cr = None
                                            try:
                                                cr = c.cursor()
                                                cr.execute(
                                                    "UPDATE smeny_skupiny SET ikona=%s WHERE id=%s",
                                                    (ii.value.strip() or '📋', s['id'])
                                                )
                                                c.commit()
                                            finally:
                                                if cr: cr.close()
                                                if c:  c.close()
                                        await asyncio.to_thread(_db)
                                        ui.notify('Ikona uložena.', type='positive')
                                        render.refresh()
                                    ui.button(icon='save', on_click=_uloz_ikonu).props(
                                        'flat round dense').classes('text-blue-400 hover:text-blue-600')

                                    async def _toggle(s=sk):
                                        def _db():
                                            c  = intranet_data.get_db_connection()
                                            cr = None
                                            try:
                                                cr = c.cursor()
                                                cr.execute(
                                                    "UPDATE smeny_skupiny SET zapnuty=%s WHERE id=%s",
                                                    (0 if s.get('zapnuty', 1) else 1, s['id'])
                                                )
                                                c.commit()
                                            finally:
                                                if cr: cr.close()
                                                if c:  c.close()
                                        await asyncio.to_thread(_db)
                                        _seznam.refresh()
                                        render.refresh()
                                    ui.button(
                                        'Zobrazit' if not zap else 'Skrýt',
                                        on_click=_toggle
                                    ).classes(
                                        'font-bold text-xs h-8 px-3 rounded-lg '
                                        + ('bg-gray-100 text-gray-400' if not zap
                                           else 'bg-green-100 text-green-700')
                                    )

            _seznam()
        dlg.open()

    # ── Spustit ─────────────────────────────────────────────────────────
    render()
