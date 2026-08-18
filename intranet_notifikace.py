import intranet_data

_DB_INIT = False

# ── Vizuální mapování typů ────────────────────────────────────────────────────
IKONY  = {'success': '✅', 'error': '❌', 'warning': '⚠️', 'info': 'ℹ️'}
BARVY  = {'success': '#16a34a', 'error': '#dc2626', 'warning': '#ea580c', 'info': '#3b82f6'}

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
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifikace (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                user_id     INT NOT NULL,
                text        VARCHAR(500) NOT NULL,
                typ         VARCHAR(20) DEFAULT 'info',
                precteno    BOOLEAN DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_precteno (user_id, precteno),
                INDEX idx_user_created  (user_id, created_at)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB
        """)
        conn.commit()
        _DB_INIT = True
    except Exception as e:
        print(f'[Notifikace] DB init: {e}')
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ==========================================
# VEŘEJNÉ FUNKCE
# ==========================================
def pridej(user_id, text, typ='info'):
    """Přidá novou notifikaci pro uživatele."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO notifikace (user_id, text, typ) VALUES (%s, %s, %s)",
            (int(user_id), str(text)[:500], typ)
        )
        conn.commit()
    except Exception as e:
        print(f'[Notifikace] pridej: {e}')
    finally:
        if cur: cur.close()
        if conn: conn.close()


def ziskej(user_id, limit=30):
    """Vrátí seznam notifikací pro uživatele (nejnovější první)."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, user_id, text, typ, precteno, created_at "
            "FROM notifikace WHERE user_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (int(user_id), int(limit))
        )
        return cur.fetchall()
    except Exception as e:
        print(f'[Notifikace] ziskej: {e}')
        return []
    finally:
        if cur: cur.close()
        if conn: conn.close()


def pocet_neprectenych(user_id):
    """Vrátí počet nepřečtených notifikací."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return 0
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM notifikace WHERE user_id = %s AND precteno = 0",
            (int(user_id),)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f'[Notifikace] pocet_neprectenych: {e}')
        return 0
    finally:
        if cur: cur.close()
        if conn: conn.close()


def oznac_precteno(notif_id, user_id):
    """Označí jednu notifikaci jako přečtenou."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE notifikace SET precteno = 1 WHERE id = %s AND user_id = %s",
            (int(notif_id), int(user_id))
        )
        conn.commit()
    except Exception as e:
        print(f'[Notifikace] oznac_precteno: {e}')
    finally:
        if cur: cur.close()
        if conn: conn.close()


def oznac_vse_precteno(user_id):
    """Označí všechny notifikace uživatele jako přečtené."""
    _init_db()
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE notifikace SET precteno = 1 WHERE user_id = %s AND precteno = 0",
            (int(user_id),)
        )
        conn.commit()
    except Exception as e:
        print(f'[Notifikace] oznac_vse_precteno: {e}')
    finally:
        if cur: cur.close()
        if conn: conn.close()
