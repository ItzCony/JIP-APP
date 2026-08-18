"""
Generický engine pro hlasovací moduly Značky (JIP, Provoz, …).

Použití:
    engine = ZnackyEngine(
        prefix='znacky',                    # předpona DB tabulek → znacky_pripad, znacky_varianta …
        pravo_uzivatel='znacky_uzivatel',
        pravo_spravce='znacky_spravce',
        nazev='Privátní značky JIP',
        popis='Hlasování o privátních výrobcích',
        log_kategorie='Znacky JIP',
        barva='purple',                     # Tailwind barva pro akcent (purple / blue / green / …)
    )
    # V intranet.py:
    engine.vykresli(user_id, user_name, vsechna_prava)
    # Ve web_main.py:
    app.on_startup(lambda: asyncio.create_task(engine.bg_uzavreni_pripadu()))
    os.makedirs(engine.foto_dir, exist_ok=True)
    app.add_static_files(engine.foto_route, engine.foto_dir)
"""

from nicegui import ui
import intranet_data
import intranet_logger
import asyncio
import datetime
import os
import base64


class ZnackyEngine:
    def __init__(
        self,
        prefix: str,
        pravo_uzivatel: str,
        pravo_spravce: str,
        nazev: str,
        popis: str,
        log_kategorie: str,
        barva: str = 'purple',
        page_path: str = '',
        pozvani_rezim: bool = False,
    ):
        self.prefix        = prefix
        self.pravo_uzivatel = pravo_uzivatel
        self.pravo_spravce  = pravo_spravce
        self.nazev          = nazev
        self.popis          = popis
        self.log_kategorie  = log_kategorie
        self.barva          = barva
        self.page_path      = page_path.lstrip('/')
        # True → správce při zadání vybírá konkrétní hlasující; ti vidí jen své
        # případy a jen jim chodí e-mail. False → původní režim (všichni uživatelé modulu).
        self.pozvani_rezim  = pozvani_rezim

        # DB tabulky
        self.t_pripad   = f'{prefix}_pripad'
        self.t_varianta = f'{prefix}_varianta'
        self.t_foto     = f'{prefix}_foto'
        self.t_hlas     = f'{prefix}_hlas'
        self.t_komentar = f'{prefix}_komentar'
        self.t_pozvani  = f'{prefix}_pozvani'

        # Soubory
        self.foto_dir   = f'{prefix}_foto'
        self.foto_route = f'/{prefix}_foto'

        # Klíče nastavení
        self.set_emaily = f'{prefix}_emaily_zapnuty'

        # Pevná adresa portálu
        self._portal_base = 'http://analytikasys.jip-napoje.cz'

        # Runtime
        self._db_init  = False
        self._ui_state = {}   # {user_id: {'aktivni_pripad': id | None}}

        # Vytvoříme refreshable render funkci pro tuto instanci
        _engine = self

        @ui.refreshable
        def _vykresli(user_id, user_name, vsechna_prava):
            _engine._inicializace_db()
            je_spravce      = _engine.pravo_spravce  in vsechna_prava or 'vse' in vsechna_prava
            je_uzivatel     = je_spravce or _engine.pravo_uzivatel in vsechna_prava
            je_admin_mazani = 'vse' in vsechna_prava

            aktivni_id = _engine._ui_state.get(user_id, {}).get('aktivni_pripad')
            if aktivni_id:
                _engine._render_detail(int(aktivni_id), user_id, user_name,
                                       je_spravce, je_uzivatel, je_admin_mazani)
            else:
                _engine._render_seznam(user_id, user_name, je_spravce, je_uzivatel, je_admin_mazani)

        self.vykresli = _vykresli

    # ==========================================
    # DB INICIALIZACE
    # ==========================================
    def _inicializace_db(self):
        if self._db_init:
            return
        conn = intranet_data.get_db_connection()
        if not conn:
            return
        cursor = conn.cursor()
        tp = self.t_pripad
        tv = self.t_varianta
        tf = self.t_foto
        th = self.t_hlas
        tk = self.t_komentar
        try:
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{tp}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                nazev VARCHAR(255) NOT NULL,
                zadavatel_id INT NOT NULL,
                datum_od DATE NOT NULL,
                datum_do DATE NOT NULL,
                stav VARCHAR(20) DEFAULT 'aktivni',
                storno_duvod TEXT NULL,
                created_at DATETIME DEFAULT NOW(),
                FOREIGN KEY (zadavatel_id) REFERENCES user(iduser) ON DELETE RESTRICT
            ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{tv}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                pripad_id INT NOT NULL,
                pismeno CHAR(1) NOT NULL,
                INDEX idx_p (pripad_id),
                FOREIGN KEY (pripad_id) REFERENCES `{tp}`(id) ON DELETE CASCADE
            ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{tf}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                varianta_id INT NOT NULL,
                filename VARCHAR(500) NOT NULL,
                original_name VARCHAR(500),
                INDEX idx_v (varianta_id),
                FOREIGN KEY (varianta_id) REFERENCES `{tv}`(id) ON DELETE CASCADE
            ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{th}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                varianta_id INT NOT NULL,
                user_id INT NOT NULL,
                body INT NOT NULL,
                created_at DATETIME DEFAULT NOW(),
                UNIQUE KEY uq_vote (varianta_id, user_id),
                FOREIGN KEY (varianta_id) REFERENCES `{tv}`(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE
            ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{tk}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                pripad_id INT NOT NULL,
                user_id INT NOT NULL,
                text TEXT NOT NULL,
                created_at DATETIME DEFAULT NOW(),
                INDEX idx_pc (pripad_id),
                FOREIGN KEY (pripad_id) REFERENCES `{tp}`(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE
            ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
            # Migrace: odstranění redundantního sloupce user_name
            try:
                cursor.execute(f"ALTER TABLE `{tk}` DROP COLUMN user_name")
                conn.commit()
            except Exception:
                pass
            # Migrace: přidání sloupce zprava
            try:
                cursor.execute(f"ALTER TABLE `{tp}` ADD COLUMN zprava TEXT NULL AFTER datum_do")
                conn.commit()
            except Exception:
                pass
            # Tabulka přizvaných hlasujících k případu (správce vybírá při zadání)
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{self.t_pozvani}` (
                pripad_id INT NOT NULL,
                user_id INT NOT NULL,
                PRIMARY KEY (pripad_id, user_id),
                INDEX idx_user (user_id),
                FOREIGN KEY (pripad_id) REFERENCES `{tp}`(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES user(iduser) ON DELETE CASCADE
            ) ENGINE=InnoDB CHARACTER SET utf8mb4""")
            conn.commit()
            self._db_init = True
        except Exception as e:
            print(f"[{self.prefix}] DB init chyba: {e}")
        finally:
            cursor.close()
            conn.close()
        os.makedirs(self.foto_dir, exist_ok=True)

    # ==========================================
    # DB HELPERS
    # ==========================================
    @staticmethod
    def _stav_vypocitany(pripad):
        if pripad['stav'] == 'storno':
            return 'storno'
        if pripad['stav'] == 'uzavreno':
            return 'po'
        dnes = datetime.date.today()
        od = pripad['datum_od'] if isinstance(pripad['datum_od'], datetime.date) else datetime.date.fromisoformat(str(pripad['datum_od']))
        do = pripad['datum_do'] if isinstance(pripad['datum_do'], datetime.date) else datetime.date.fromisoformat(str(pripad['datum_do']))
        if dnes < od:
            return 'pred'
        if dnes > do:
            return 'po'
        return 'otevreno'

    def _ziskej_vsechny_pripady(self, jen_pro_uzivatele=None):
        # jen_pro_uzivatele=None → všechny případy (správce/superadmin).
        # jen_pro_uzivatele=user_id → jen případy, ke kterým je uživatel přizván.
        conn = intranet_data.get_db_connection()
        if not conn:
            return []
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True, buffered=True)
            if jen_pro_uzivatele is not None:
                cursor.execute(f"""
                    SELECT p.*, CONCAT(u.name, ' ', u.surname) AS zadavatel_jmeno
                    FROM `{self.t_pripad}` p
                    JOIN user u ON p.zadavatel_id = u.iduser
                    JOIN `{self.t_pozvani}` pz ON pz.pripad_id = p.id AND pz.user_id = %s
                    ORDER BY p.created_at DESC
                """, (int(jen_pro_uzivatele),))
            else:
                cursor.execute(f"""
                    SELECT p.*, CONCAT(u.name, ' ', u.surname) AS zadavatel_jmeno
                    FROM `{self.t_pripad}` p
                    JOIN user u ON p.zadavatel_id = u.iduser
                    ORDER BY p.created_at DESC
                """)
            return cursor.fetchall()
        except Exception as e:
            print(f"[{self.prefix}] ziskej_vsechny_pripady: {e}"); return []
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _je_pozvan(self, pripad_id, user_id):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor(buffered=True)
            cursor.execute(
                f"SELECT 1 FROM `{self.t_pozvani}` WHERE pripad_id=%s AND user_id=%s LIMIT 1",
                (int(pripad_id), int(user_id)))
            return cursor.fetchone() is not None
        except Exception as e:
            print(f"[{self.prefix}] je_pozvan: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _ziskej_hlasujici_uzivatele(self):
        # Uživatelé, které lze přizvat k hlasování = aktivní držitelé práva
        # znacky_uzivatel / znacky_spravce (přímo nebo přes oddělení).
        uzivatele = intranet_data.ziskej_vsechny_uzivatele()
        oddeleni  = intranet_data.ziskej_vsechna_oddeleni()
        result = []
        for u in uzivatele.values():
            if not u.get('aktivni') or not u.get('id'):
                continue
            prava = set(p.strip().lower() for p in (u.get('prava') or '').split(',') if p.strip())
            for odd in str(u.get('oddeleni') or '').split(','):
                odd = odd.strip()
                if odd in oddeleni:
                    prava.update(p.strip().lower() for p in (oddeleni[odd].get('prava') or '').split(',') if p.strip())
            if prava & {self.pravo_uzivatel, self.pravo_spravce, 'vse'}:
                result.append({'id': u['id'], 'jmeno': u['jmeno_cele']})
        result.sort(key=lambda x: x['jmeno'].lower())
        return result

    def _ziskej_emaily_pozvanych(self, pripad_id):
        conn = intranet_data.get_db_connection()
        if not conn:
            return []
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute(f"""
                SELECT u.email, CONCAT(u.name,' ',u.surname) AS jmeno
                FROM `{self.t_pozvani}` pz JOIN user u ON pz.user_id = u.iduser
                WHERE pz.pripad_id = %s AND u.is_active = 1
            """, (int(pripad_id),))
            return [{'email': r['email'], 'jmeno': r['jmeno']} for r in cursor.fetchall() if r['email']]
        except Exception as e:
            print(f"[{self.prefix}] ziskej_emaily_pozvanych: {e}"); return []
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _prijemci_emailu(self, pripad_id):
        # Režim pozvání: jen přizvaní. Jinak: přizvaní, pokud byli vybráni, jinak všichni s právem.
        pozvani = self._ziskej_emaily_pozvanych(pripad_id) if pripad_id else []
        if self.pozvani_rezim:
            return pozvani
        return pozvani or self._ziskej_emaily_s_pravem()

    def _ziskej_pripad_detail(self, pripad_id):
        conn = intranet_data.get_db_connection()
        if not conn:
            return None
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute(f"""SELECT p.*, CONCAT(u.name, ' ', u.surname) AS zadavatel_jmeno
                FROM `{self.t_pripad}` p JOIN user u ON p.zadavatel_id = u.iduser WHERE p.id=%s""",
                (pripad_id,))
            pripad = cursor.fetchone()
            if not pripad:
                return None
            cursor.execute(f"SELECT * FROM `{self.t_varianta}` WHERE pripad_id=%s ORDER BY pismeno", (pripad_id,))
            varianty = cursor.fetchall()
            for v in varianty:
                cursor.execute(f"SELECT * FROM `{self.t_foto}` WHERE varianta_id=%s", (v['id'],))
                v['fotografie'] = cursor.fetchall()
                cursor.execute(f"""SELECT h.*, CONCAT(u.name,' ',u.surname) AS uzivatel_jmeno
                    FROM `{self.t_hlas}` h JOIN user u ON h.user_id=u.iduser
                    WHERE h.varianta_id=%s ORDER BY h.created_at""", (v['id'],))
                v['hlasy'] = cursor.fetchall()
                v['body_celkem'] = sum(h['body'] for h in v['hlasy'])
            pripad['varianty'] = varianty
            cursor.execute(f"""SELECT k.*, CONCAT(u.name,' ',u.surname) AS uzivatel_jmeno
                FROM `{self.t_komentar}` k JOIN user u ON k.user_id=u.iduser
                WHERE k.pripad_id=%s ORDER BY k.created_at""", (pripad_id,))
            pripad['komentare'] = cursor.fetchall()
            return pripad
        except Exception as e:
            print(f"[{self.prefix}] ziskej_pripad_detail: {e}"); return None
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _uloz_pripad(self, nazev, zadavatel_id, datum_od, datum_do, varianty_data, zprava=None, pozvani_ids=None):
        conn = intranet_data.get_db_connection()
        if not conn:
            return None
        cursor = None
        try:
            cursor = conn.cursor(buffered=True)
            cursor.execute(
                f"INSERT INTO `{self.t_pripad}` (nazev,zadavatel_id,datum_od,datum_do,zprava) VALUES(%s,%s,%s,%s,%s)",
                (nazev, zadavatel_id, datum_od, datum_do, zprava or None))
            pripad_id = cursor.lastrowid
            for uid in (pozvani_ids or []):
                cursor.execute(
                    f"INSERT IGNORE INTO `{self.t_pozvani}` (pripad_id,user_id) VALUES(%s,%s)",
                    (pripad_id, int(uid)))
            for v in varianty_data:
                cursor.execute(f"INSERT INTO `{self.t_varianta}` (pripad_id,pismeno) VALUES(%s,%s)",
                               (pripad_id, v['pismeno']))
                vid = cursor.lastrowid
                for f in v['fotografie']:
                    cursor.execute(
                        f"INSERT INTO `{self.t_foto}` (varianta_id,filename,original_name) VALUES(%s,%s,%s)",
                        (vid, f['filename'], f['original_name']))
            conn.commit()
            return pripad_id
        except Exception as e:
            print(f"[{self.prefix}] uloz_pripad: {e}"); conn.rollback(); return None
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _uprav_pripad(self, pripad_id, nazev, datum_od, datum_do, zprava=None):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE `{self.t_pripad}` SET nazev=%s,datum_od=%s,datum_do=%s,zprava=%s"
                f" WHERE id=%s AND stav='aktivni'",
                (nazev, datum_od, datum_do, zprava or None, pripad_id))
            conn.commit(); return True
        except Exception as e:
            print(f"[{self.prefix}] uprav_pripad: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _stornuj_pripad(self, pripad_id, duvod):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE `{self.t_pripad}` SET stav='storno',storno_duvod=%s WHERE id=%s",
                           (duvod, pripad_id))
            conn.commit(); return True
        except Exception as e:
            print(f"[{self.prefix}] stornuj_pripad: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _smaz_pripad(self, pripad_id):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM `{self.t_pripad}` WHERE id=%s", (pripad_id,))
            conn.commit()
            import shutil
            foto_dir = os.path.join(self.foto_dir, str(pripad_id))
            if os.path.isdir(foto_dir):
                shutil.rmtree(foto_dir)
            return True
        except Exception as e:
            print(f"[{self.prefix}] smaz_pripad: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _uzavri_pripad_db(self, pripad_id):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE `{self.t_pripad}` SET stav='uzavreno' WHERE id=%s AND stav='aktivni'",
                (pripad_id,))
            conn.commit(); return cursor.rowcount > 0
        except Exception as e:
            print(f"[{self.prefix}] uzavri_pripad_db: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _uloz_hlas(self, varianta_id, user_id, pripad_id):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor(buffered=True)
            # Odmítni hlas pokud případ není aktivní
            cursor.execute(
                f"SELECT stav FROM `{self.t_pripad}` WHERE id=%s LIMIT 1",
                (pripad_id,))
            row = cursor.fetchone()
            if not row or row[0] != 'aktivni':
                return False
            cursor.execute(f"""
                SELECT h.id FROM `{self.t_hlas}` h
                JOIN `{self.t_varianta}` v ON h.varianta_id = v.id
                WHERE v.pripad_id = %s AND h.user_id = %s
                LIMIT 1
            """, (pripad_id, user_id))
            if cursor.fetchone():
                return False
            cursor.execute(
                f"INSERT IGNORE INTO `{self.t_hlas}` (varianta_id,user_id,body) VALUES(%s,%s,1)",
                (varianta_id, user_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            print(f"[{self.prefix}] uloz_hlas: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _pridat_komentar(self, pripad_id, user_id, text):
        conn = intranet_data.get_db_connection()
        if not conn:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO `{self.t_komentar}` (pripad_id,user_id,text) VALUES(%s,%s,%s)",
                (pripad_id, user_id, text))
            conn.commit(); return True
        except Exception as e:
            print(f"[{self.prefix}] pridat_komentar: {e}"); return False
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def _ziskej_emaily_s_pravem(self):
        uzivatele = intranet_data.ziskej_vsechny_uzivatele()
        oddeleni  = intranet_data.ziskej_vsechna_oddeleni()
        result = []
        for email, u in uzivatele.items():
            if not u['aktivni']:
                continue
            prava = set(p.strip().lower() for p in (u.get('prava') or '').split(',') if p.strip())
            for odd in str(u.get('oddeleni') or '').split(','):
                odd = odd.strip()
                if odd in oddeleni:
                    prava.update(p.strip().lower() for p in (oddeleni[odd].get('prava') or '').split(',') if p.strip())
            if prava & {self.pravo_uzivatel, self.pravo_spravce, 'vse'}:
                result.append({'email': email, 'jmeno': u['jmeno_cele']})
        return result

    # ==========================================
    # EMAILY
    # ==========================================
    def _odesli_email_novy_pripad(self, nazev, datum_od=None, pripad_id=None):
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
        if not nastaveni.get(self.set_emaily, True):
            return
        try:
            import intranet_emaily
            prijemci = self._prijemci_emailu(pripad_id)
            if not prijemci:
                return
            predmet    = f'Nový hlasovací případ: {nazev}'
            termin_radek = f'Termín zahájení hlasování: {self._format_date(datum_od)}\n\n' if datum_od else ''
            server_url   = f'{self._portal_base}/{self.page_path}' if self.page_path else self._portal_base
            odkaz_radek  = f'Přehled hlasování: {server_url}\n\n'
            text = (f'Dobrý den,\n\nbyli jste přizváni k hlasování o novém případu v aplikaci Moje JIPka — {self.nazev}.\n\n'
                    f'Název případu: {nazev}\n\n'
                    f'{termin_radek}'
                    f'{odkaz_radek}'
                    f'Prosím přihlaste se a hlasujte.\n\nPortál Moje JIPka')
            for p in prijemci:
                intranet_emaily.odesli_upozorneni_email(p['email'], predmet, text)
        except Exception as e:
            print(f"[{self.prefix}] email nový případ: {e}")

    def _odesli_email_uzavreni(self, pripad):
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
        if not nastaveni.get(self.set_emaily, True):
            return
        try:
            import intranet_emaily
            detail   = self._ziskej_pripad_detail(pripad['id'])
            if not detail:
                return
            varianty = sorted(detail.get('varianty', []), key=lambda v: v['body_celkem'], reverse=True)
            radky    = '\n'.join(f'  Varianta {v["pismeno"]}: {v["body_celkem"]} bodů' for v in varianty)
            vitez    = varianty[0] if varianty else None
            server_url  = f'{self._portal_base}/{self.page_path}' if self.page_path else self._portal_base
            odkaz_radek = f'Přehled hlasování: {server_url}\n\n'
            predmet  = f'Výsledek hlasování: {pripad["nazev"]}'
            text = (f'Dobrý den,\n\nhlasování pro případ „{pripad["nazev"]}" bylo ukončeno.\n\n'
                    f'Výsledky:\n{radky}\n\n'
                    + (f'Vítěz: Varianta {vitez["pismeno"]} s {vitez["body_celkem"]} body.\n\n' if vitez else '')
                    + odkaz_radek
                    + 'Tým Moje JIPka')
            prijemci = self._prijemci_emailu(pripad['id'])
            for p in prijemci:
                intranet_emaily.odesli_upozorneni_email(p['email'], predmet, text)
            intranet_logger.log_activity("Systém", self.log_kategorie,
                                         f"Email uzavření odeslán: {pripad['nazev']}")
        except Exception as e:
            print(f"[{self.prefix}] email uzavření: {e}")

    # ==========================================
    # BACKGROUND TASK — automatické uzavírání
    # ==========================================
    async def bg_uzavreni_pripadu(self):
        await asyncio.sleep(120)
        self._inicializace_db()
        while True:
            try:
                conn = intranet_data.get_db_connection()
                if conn:
                    cursor = conn.cursor(dictionary=True, buffered=True)
                    cursor.execute(f"""SELECT p.*, CONCAT(u.name,' ',u.surname) AS zadavatel_jmeno
                        FROM `{self.t_pripad}` p JOIN user u ON p.zadavatel_id=u.iduser
                        WHERE p.stav='aktivni' AND p.datum_do < CURDATE()""")
                    expired = cursor.fetchall()
                    cursor.close(); conn.close()
                    for pripad in expired:
                        if self._uzavri_pripad_db(pripad['id']):
                            await asyncio.to_thread(self._odesli_email_uzavreni, pripad)
            except Exception as e:
                print(f"[{self.prefix}] bg_uzavreni: {e}")
            await asyncio.sleep(3600)

    # ==========================================
    # UI HELPERS
    # ==========================================
    @staticmethod
    def _stav_badge(stav_vypoc):
        if stav_vypoc == 'storno':
            return ('Stornováno', 'bg-red-100 text-red-700 border border-red-300')
        if stav_vypoc == 'po':
            return ('Hlasování ukončeno', 'bg-gray-100 text-gray-500 border border-gray-300')
        if stav_vypoc == 'pred':
            return ('Hlasování nezahájeno', 'bg-orange-100 text-orange-700 border border-orange-300')
        return ('Hlasování probíhá', 'bg-green-100 text-green-700 border border-green-300')

    @staticmethod
    def _format_date(d):
        if not d:
            return ''
        try:
            if not isinstance(d, datetime.date):
                d = datetime.date.fromisoformat(str(d)[:10])
            return f'{d.day}. {d.month}. {d.year}'
        except Exception:
            return str(d)

    @staticmethod
    def _parse_datum(s):
        if not s:
            raise ValueError('Prázdné datum')
        return datetime.date.fromisoformat(str(s).replace('/', '-'))

    # ==========================================
    # SEZNAM PŘÍPADŮ
    # ==========================================
    def _render_seznam(self, user_id, user_name, je_spravce, je_uzivatel, je_admin_mazani=False):
        with ui.row().classes('w-full justify-between items-center mb-6'):
            with ui.column().classes('gap-1'):
                ui.label(self.nazev).classes('text-4xl font-extrabold text-gray-800')
                ui.label(self.popis).classes('text-lg text-gray-500 font-bold')
            if je_spravce:
                ui.button('Přidat hlasovací případ', icon='add_circle',
                          on_click=self._otevri_pridani(user_id, user_name)).classes(
                    f'bg-{self.barva}-600 hover:bg-{self.barva}-700 text-white font-bold px-6 h-12 rounded-xl shadow-md')

        # V režimu pozvání: správce vidí vše, běžný uživatel jen přizvané případy.
        omezit = self.pozvani_rezim and not je_spravce
        pripady = self._ziskej_vsechny_pripady(user_id if omezit else None)

        with ui.card().classes('w-full p-4 bg-gray-50 rounded-xl border border-gray-200 mb-6'):
            with ui.row().classes('gap-4 flex-wrap items-end'):
                filtr_nazev = ui.input('Hledat dle názvu').classes('w-48').props('outlined clearable dense')
                filtr_zadav = ui.input('Zadavatel').classes('w-40').props('outlined clearable dense')
                filtr_stav  = ui.select(
                    {'vse': 'Vše', 'pred': 'Nezahájeno', 'otevreno': 'Otevřeno', 'po': 'Uzavřeno', 'storno': 'Storno'},
                    value='vse', label='Stav'
                ).classes('w-44').props('outlined dense')

        @ui.refreshable
        def vykresli_radky():
            fn = filtr_nazev.value.lower().strip() if filtr_nazev.value else ''
            fz = filtr_zadav.value.lower().strip() if filtr_zadav.value else ''
            fs = filtr_stav.value

            zobrazene = []
            for p in pripady:
                sv = self._stav_vypocitany(p)
                if fn and fn not in p['nazev'].lower(): continue
                if fz and fz not in p['zadavatel_jmeno'].lower(): continue
                if fs != 'vse' and sv != fs: continue
                zobrazene.append((p, sv))

            if not zobrazene:
                ui.label('Žádné případy neodpovídají filtru.').classes('italic text-gray-400 py-8 text-center w-full')
                return

            for pripad, sv in zobrazene:
                text_badge, cls_badge = self._stav_badge(sv)
                je_storno = sv == 'storno'

                card_cls = 'w-full px-5 py-4 bg-white border border-gray-200 rounded-2xl shadow-sm hover:shadow-md transition-shadow'
                if je_storno:
                    card_cls = 'w-full px-5 py-4 bg-red-50 border border-red-200 rounded-2xl shadow-sm'

                with ui.card().classes(card_cls):
                    with ui.row().classes('w-full items-center justify-between gap-4 flex-wrap'):
                        with ui.column().classes('gap-0.5 flex-1 min-w-0'):
                            ui.label(pripad['nazev']).classes(
                                'font-bold text-lg text-gray-800 leading-tight' +
                                (' line-through text-gray-400' if je_storno else ''))
                            with ui.row().classes('gap-4 text-sm text-gray-500 mt-1 flex-wrap items-center'):
                                ui.label(f'Zadavatel: {pripad["zadavatel_jmeno"]}')
                                ui.label('·').classes('text-gray-300')
                                ui.label(f'Hlasování: {self._format_date(pripad["datum_od"])} – {self._format_date(pripad["datum_do"])}')
                            if je_storno and pripad.get('storno_duvod'):
                                ui.label(f'Storno: {pripad["storno_duvod"]}').classes('text-xs text-red-500 font-semibold mt-1')

                        with ui.row().classes('gap-2 items-center flex-shrink-0'):
                            ui.label(text_badge).classes(f'text-xs font-black px-3 py-1.5 rounded-full {cls_badge}')

                            if je_spravce and sv == 'pred':
                                ui.button(icon='edit', on_click=self._otevri_editaci(pripad, user_name)).props('flat round size=sm').classes('text-blue-500').tooltip('Upravit')
                            if je_spravce and sv != 'storno':
                                ui.button(icon='cancel', on_click=self._otevri_storno(pripad['id'], pripad['nazev'], user_name)).props('flat round size=sm').classes('text-red-400').tooltip('Stornovat')
                            if je_admin_mazani:
                                ui.button(icon='delete_forever', on_click=self._otevri_smazani(pripad['id'], pripad['nazev'], user_name)).props('flat round size=sm').classes('text-red-700').tooltip('Trvale smazat z databáze')

                            def prejit(pid=pripad['id']):
                                self._ui_state.setdefault(user_id, {})['aktivni_pripad'] = pid
                                ui.timer(0, self.vykresli.refresh, once=True)

                            btn_cls = f'bg-{self.barva}-600 hover:bg-{self.barva}-700 text-white' if not je_storno else 'bg-gray-300 text-gray-500 cursor-not-allowed'
                            ui.button('Přejít do hlasování', icon='how_to_vote',
                                      on_click=(prejit if not je_storno else lambda: None)).classes(
                                f'{btn_cls} font-bold h-9 px-4 rounded-xl shadow-sm text-sm')

        filtr_nazev.on_value_change(lambda _: vykresli_radky.refresh())
        filtr_zadav.on_value_change(lambda _: vykresli_radky.refresh())
        filtr_stav.on_value_change(lambda _: vykresli_radky.refresh())
        vykresli_radky()

    # ==========================================
    # PŘIDAT / EDITOVAT — factory handlery
    # ==========================================
    def _otevri_pridani(self, user_id, user_name):
        def handler():
            import string as _string
            fotos    = {'A': [], 'B': []}
            previews = {}
            karty    = {}

            with ui.dialog().props('maximized') as dlg, \
                 ui.card().classes('p-6 w-full max-w-4xl max-h-[95vh] overflow-y-auto rounded-2xl mx-auto mt-4'):
                bv = self.barva
                with ui.row().classes(f'w-full items-center gap-3 mb-6 px-5 py-4 rounded-2xl '
                                      f'bg-gradient-to-r from-{bv}-600 to-{bv}-500 shadow-md'):
                    ui.icon('how_to_vote').classes('text-white text-3xl')
                    ui.label('Nový hlasovací případ').classes('text-2xl font-bold text-white')

                inp_nazev = ui.input('Název případu').classes('w-full mb-4').props('outlined')
                with ui.row().classes('gap-4 mb-4 w-full'):
                    with ui.input('Hlasování od', placeholder='RRRR-MM-DD').classes('flex-1').props('outlined readonly') as inp_od:
                        with inp_od.add_slot('append'):
                            ui.icon('event').classes('cursor-pointer').on('click', lambda: menu_od.open())
                        with ui.menu() as menu_od:
                            ui.date(mask='YYYY-MM-DD').bind_value(inp_od).props('today-btn')
                    with ui.input('Hlasování do', placeholder='RRRR-MM-DD').classes('flex-1').props('outlined readonly') as inp_do:
                        with inp_do.add_slot('append'):
                            ui.icon('event').classes('cursor-pointer').on('click', lambda: menu_do.open())
                        with ui.menu() as menu_do:
                            ui.date(mask='YYYY-MM-DD').bind_value(inp_do).props('today-btn')

                inp_zprava = ui.textarea('Informace o případu (nepovinné)').classes('w-full mb-4').props('outlined rows=3')

                hlasujici_opts = {u['id']: u['jmeno'] for u in self._ziskej_hlasujici_uzivatele()}
                sel_hlasujici = ui.select(
                    options=hlasujici_opts, multiple=True, with_input=True,
                    label=('Přizvat k hlasování (komu přijde e-mail)' if self.pozvani_rezim
                           else 'Přizvat k hlasování (nepovinné — prázdné = e-mail všem oprávněným)')) \
                    .props('outlined use-chips').classes('w-full mb-6')

                def _vytvor_kartu(pismeno):
                    smazatelna = pismeno not in ('A', 'B')
                    with varianty_box:
                        karta = ui.card().classes('w-full p-5 mb-4 bg-white border border-gray-200 rounded-2xl shadow-sm hover:shadow-md transition-shadow')
                        karty[pismeno] = karta
                        with karta:
                            with ui.row().classes('w-full items-center justify-between mb-3'):
                                ui.label(f'Varianta {pismeno}').classes('font-bold text-lg text-gray-700')
                                if smazatelna:
                                    ui.button(icon='delete_outline',
                                              on_click=lambda _=None, p=pismeno: _smaz_variantu(p)) \
                                        .props('flat round dense color=red').tooltip('Odebrat variantu')
                            prev_row = ui.row().classes('gap-2 flex-wrap mb-2 min-h-[2rem]')
                            previews[pismeno] = prev_row

                            def make_upload(p=pismeno):
                                async def on_up(e, _p=p):
                                    if len(fotos[_p]) >= 5:
                                        ui.notify(f'Max. 5 fotografií pro variantu {_p}!', type='warning'); return
                                    content = await e.file.read()
                                    name = e.file.name
                                    fotos[_p].append({'content': content, 'name': name})
                                    b64  = base64.b64encode(content).decode()
                                    ext  = os.path.splitext(name)[1].lower()
                                    mime = 'image/png' if ext == '.png' else 'image/gif' if ext == '.gif' else 'image/jpeg'
                                    with previews[_p]:
                                        with ui.card().classes('w-24 h-24 overflow-hidden p-0 cursor-pointer relative'):
                                            ui.image(f'data:{mime};base64,{b64}').classes('w-full h-full object-cover')
                                            ui.label(name[:12]).classes('text-xs text-center text-gray-500 absolute bottom-0 left-0 right-0 bg-white/80 px-1')
                                return on_up
                            ui.upload(on_upload=make_upload(pismeno), multiple=True, max_files=5,
                                      max_file_size=10_000_000,
                                      label=f'Fotografie (max. 5, PNG/JPG, max. 10 MB)') \
                                .props(f'accept=".png,.jpg,.jpeg" flat bordered color={bv}').classes('w-full rounded-lg')

                def _smaz_variantu(pismeno):
                    if pismeno in karty:
                        karty.pop(pismeno).delete()
                    fotos.pop(pismeno, None)
                    previews.pop(pismeno, None)

                def _pridej_variantu():
                    dalsi = next((c for c in _string.ascii_uppercase if c not in fotos), None)
                    if dalsi is None:
                        ui.notify('Dosažen maximální počet variant (A–Z)!', type='warning'); return
                    fotos[dalsi] = []
                    _vytvor_kartu(dalsi)

                with ui.row().classes('w-full items-center justify-between mb-3'):
                    ui.label('Varianty').classes('text-lg font-bold text-gray-700')
                    ui.button(icon='add', on_click=_pridej_variantu) \
                        .props('round unelevated color=green size=sm').classes('shadow-md') \
                        .tooltip('Přidat další variantu')

                varianty_box = ui.column().classes('w-full gap-0')
                for _p in ('A', 'B'):
                    _vytvor_kartu(_p)

                async def odeslat():
                    nazev = inp_nazev.value.strip()
                    if not nazev:
                        return ui.notify('Zadejte název případu!', type='warning')
                    varianty_s_fotos = [p for p in sorted(fotos) if fotos[p]]
                    if len(varianty_s_fotos) < 2:
                        return ui.notify('Musíte nahrát fotografie alespoň pro 2 varianty!', type='warning')
                    pozvani_ids = list(sel_hlasujici.value or []) if sel_hlasujici else []
                    if self.pozvani_rezim and not pozvani_ids:
                        return ui.notify('Vyberte alespoň jednoho hlasujícího!', type='warning')
                    try:
                        datum_od = self._parse_datum(inp_od.value)
                        datum_do = self._parse_datum(inp_do.value)
                        if datum_od > datum_do:
                            return ui.notify('Datum Od musí být dříve než Do!', type='negative')
                    except ValueError:
                        return ui.notify('Vyberte datum od a do!', type='negative')

                    import time as _t
                    varianty_db      = []
                    varianty_soubory = []
                    for p in sorted(fotos):
                        if not fotos[p]: continue
                        flist_db, flist_disk = [], []
                        for i, f in enumerate(fotos[p]):
                            ext   = os.path.splitext(f['name'])[1].lower() or '.jpg'
                            fname = f"{int(_t.time() * 1000)}_{p}_{i}{ext}"
                            flist_db.append({'filename': fname, 'original_name': f['name']})
                            flist_disk.append({'filename': fname, 'content': f['content']})
                        varianty_db.append({'pismeno': p, 'fotografie': flist_db})
                        varianty_soubory.append({'pismeno': p, 'soubory': flist_disk})

                    zprava_val = inp_zprava.value.strip() or None
                    dlg.close()

                    pripad_id = await asyncio.to_thread(
                        self._uloz_pripad, nazev, user_id, datum_od, datum_do, varianty_db, zprava_val, pozvani_ids)
                    if not pripad_id:
                        ui.notify('Chyba při ukládání do databáze!', type='negative'); return

                    def _uloz_soubory():
                        for vs in varianty_soubory:
                            d = os.path.join(self.foto_dir, str(pripad_id), vs['pismeno'])
                            os.makedirs(d, exist_ok=True)
                            for s in vs['soubory']:
                                with open(os.path.join(d, s['filename']), 'wb') as fh:
                                    fh.write(s['content'])
                    await asyncio.to_thread(_uloz_soubory)

                    await asyncio.to_thread(self._odesli_email_novy_pripad, nazev, datum_od, pripad_id)
                    intranet_logger.log_activity(user_name, self.log_kategorie,
                                                 f"Vytvořen hlasovací případ: {nazev}")
                    ui.notify('Případ byl vytvořen a e-mail odeslán!', type='positive', position='top')
                    self.vykresli.refresh()

                with ui.row().classes('w-full justify-between items-center mt-8 pt-4 border-t border-gray-100'):
                    ui.button('Zrušit', on_click=dlg.close) \
                        .props('flat').classes('text-gray-500 font-bold h-12 px-6 rounded-xl')
                    ui.button('Odeslat hlasovací případ', icon='send', on_click=odeslat).classes(
                        f'bg-gradient-to-r from-{self.barva}-600 to-{self.barva}-500 hover:from-{self.barva}-700 '
                        f'hover:to-{self.barva}-600 text-white font-bold h-12 px-8 shadow-md rounded-xl')
            dlg.open()
        return handler

    def _otevri_editaci(self, pripad, user_name):
        def handler():
            with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl w-full max-w-lg'):
                ui.label('Upravit případ').classes('text-xl font-bold text-blue-700 mb-4')
                inp_n = ui.input('Název', value=pripad['nazev']).classes('w-full mb-3').props('outlined')
                with ui.row().classes('gap-4 mb-4'):
                    with ui.input('Od', value=str(pripad['datum_od'])[:10]).classes('flex-1').props('outlined readonly') as inp_od:
                        with inp_od.add_slot('append'):
                            ui.icon('event').classes('cursor-pointer').on('click', lambda: menu_od_e.open())
                        with ui.menu() as menu_od_e:
                            ui.date(mask='YYYY-MM-DD').bind_value(inp_od).props('today-btn')
                    with ui.input('Do', value=str(pripad['datum_do'])[:10]).classes('flex-1').props('outlined readonly') as inp_do:
                        with inp_do.add_slot('append'):
                            ui.icon('event').classes('cursor-pointer').on('click', lambda: menu_do_e.open())
                        with ui.menu() as menu_do_e:
                            ui.date(mask='YYYY-MM-DD').bind_value(inp_do).props('today-btn')

                inp_zprava_e = ui.textarea('Informace o případu (nepovinné)',
                                           value=pripad.get('zprava') or '').classes('w-full mb-4').props('outlined rows=3')

                async def ulozit():
                    try:
                        od = self._parse_datum(inp_od.value)
                        do = self._parse_datum(inp_do.value)
                        if od > do: return ui.notify('Datum Od musí být dříve!', type='negative')
                    except ValueError:
                        return ui.notify('Vyberte datum od a do!', type='negative')
                    ok = await asyncio.to_thread(
                        self._uprav_pripad, pripad['id'], inp_n.value.strip(), od, do,
                        inp_zprava_e.value.strip() or None)
                    if ok:
                        intranet_logger.log_activity(user_name, self.log_kategorie,
                                                     f"Upraven případ ID {pripad['id']}: {inp_n.value.strip()}")
                        ui.notify('Případ upraven.', type='positive')
                        dlg.close()
                        ui.timer(0, self.vykresli.refresh, once=True)
                    else:
                        ui.notify('Chyba při ukládání!', type='negative')

                with ui.row().classes('w-full justify-between mt-4'):
                    ui.button('Zrušit', on_click=dlg.close).classes('bg-gray-400 text-white font-bold')
                    ui.button('Uložit', icon='save', on_click=ulozit).classes('bg-blue-600 text-white font-bold px-6')
            dlg.open()
        return handler

    def _otevri_storno(self, pripad_id, nazev, user_name):
        def handler():
            with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl w-full max-w-md'):
                ui.label('Stornovat případ').classes('text-xl font-bold text-red-600 mb-2')
                ui.label(nazev).classes('font-bold text-gray-700 mb-4')
                inp_d = ui.textarea('Důvod storna (povinné)').classes('w-full mb-4').props('outlined rows=3')

                async def proved():
                    if not inp_d.value.strip():
                        return ui.notify('Zadejte důvod storna!', type='warning')
                    ok = await asyncio.to_thread(self._stornuj_pripad, pripad_id, inp_d.value.strip())
                    if ok:
                        intranet_logger.log_activity(user_name, self.log_kategorie,
                                                     f"Stornován případ ID {pripad_id}: {inp_d.value.strip()}")
                        ui.notify('Případ byl stornován.', type='info')
                        dlg.close()
                        ui.timer(0, self.vykresli.refresh, once=True)
                    else:
                        ui.notify('Chyba!', type='negative')

                with ui.row().classes('w-full justify-between'):
                    ui.button('Zrušit', on_click=dlg.close).classes('bg-gray-400 text-white font-bold')
                    ui.button('Stornovat', icon='cancel', on_click=proved).classes('bg-red-600 text-white font-bold px-6')
            dlg.open()
        return handler

    def _otevri_smazani(self, pripad_id, nazev, user_name, po_smazani=None):
        def handler():
            with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl w-full max-w-md'):
                ui.label('Trvale smazat případ').classes('text-xl font-bold text-red-700 mb-2')
                ui.label(nazev).classes('font-bold text-gray-700 mb-3')
                ui.label('Tato akce je nevratná. Případ, všechny varianty, fotografie, hlasy i komentáře budou trvale odstraněny z databáze i disku.').classes(
                    'text-sm text-red-600 bg-red-50 border border-red-200 p-3 rounded-xl mb-5')

                async def proved():
                    ok = await asyncio.to_thread(self._smaz_pripad, pripad_id)
                    if ok:
                        intranet_logger.log_activity(user_name, self.log_kategorie,
                                                     f"Trvale smazán případ ID {pripad_id}: {nazev}")
                        ui.notify('Případ byl trvale smazán.', type='positive')
                        dlg.close()
                        if po_smazani:
                            po_smazani()
                        else:
                            ui.timer(0, self.vykresli.refresh, once=True)
                    else:
                        ui.notify('Chyba při mazání!', type='negative')

                with ui.row().classes('w-full justify-between'):
                    ui.button('Zrušit', on_click=dlg.close).classes('bg-gray-400 text-white font-bold')
                    ui.button('Trvale smazat', icon='delete_forever', on_click=proved).classes('bg-red-700 text-white font-bold px-6')
            dlg.open()
        return handler

    # ==========================================
    # DETAIL / HLASOVÁNÍ
    # ==========================================
    def _render_detail(self, pripad_id, user_id, user_name, je_spravce, je_uzivatel, je_admin_mazani=False):
        pripad = self._ziskej_pripad_detail(pripad_id)
        if not pripad:
            ui.label('Případ nenalezen.').classes('text-red-500')
            return

        # V režimu pozvání smí běžný uživatel otevřít jen případ, ke kterému je přizván.
        if self.pozvani_rezim and not je_spravce and not self._je_pozvan(pripad_id, user_id):
            ui.label('K tomuto případu nemáte přístup.').classes('text-red-500 font-bold')
            ui.button('← Zpět na seznam', icon='arrow_back',
                      on_click=lambda: (self._ui_state.setdefault(user_id, {}).__setitem__('aktivni_pripad', None),
                                        ui.timer(0, self.vykresli.refresh, once=True))).props('flat').classes('text-gray-600 font-bold mt-2')
            return

        sv = self._stav_vypocitany(pripad)
        text_badge, cls_badge = self._stav_badge(sv)
        je_otevreno = sv == 'otevreno'

        def zpet():
            self._ui_state.setdefault(user_id, {})['aktivni_pripad'] = None
            ui.timer(0, self.vykresli.refresh, once=True)

        with ui.row().classes('w-full items-center gap-4 mb-4'):
            ui.button('← Zpět na seznam', icon='arrow_back', on_click=zpet).props('flat').classes('text-gray-600 font-bold')
            ui.label(pripad['nazev']).classes('text-3xl font-extrabold text-gray-800 flex-1')
            ui.label(text_badge).classes(f'font-black text-sm px-4 py-2 rounded-full {cls_badge}')
            if je_admin_mazani:
                def _zpet_po_smazani():
                    self._ui_state.setdefault(user_id, {})['aktivni_pripad'] = None
                    ui.timer(0, self.vykresli.refresh, once=True)
                ui.button('Smazat případ', icon='delete_forever',
                          on_click=self._otevri_smazani(pripad_id, pripad['nazev'], user_name, _zpet_po_smazani)).classes(
                    'bg-red-700 hover:bg-red-800 text-white font-bold px-4 h-10 rounded-xl shadow-sm text-sm')

        if sv == 'storno':
            ui.label(f'Případ byl stornován — {pripad.get("storno_duvod", "")}').classes(
                'w-full bg-red-50 border border-red-200 text-red-700 font-bold p-4 rounded-xl mb-4')

        with ui.row().classes('text-sm text-gray-500 font-bold gap-6 mb-6'):
            ui.label(f'Zadavatel: {pripad["zadavatel_jmeno"]}')
            ui.label(f'Hlasování od: {self._format_date(pripad["datum_od"])}')
            ui.label(f'Hlasování do: {self._format_date(pripad["datum_do"])}')

        zprava = pripad.get('zprava')
        if zprava:
            with ui.card().classes(f'w-full p-5 mb-6 bg-{self.barva}-50 border border-{self.barva}-200 rounded-2xl'):
                with ui.row().classes('items-center gap-3 mb-2'):
                    ui.icon('campaign', size='sm')
                    ui.label('Informace o případu').classes(f'font-bold text-{self.barva}-700 text-sm uppercase tracking-wide')
                ui.label(zprava).classes('text-gray-800 whitespace-pre-wrap')

        varianty = pripad.get('varianty', [])
        if varianty and (je_spravce or sv != 'otevreno'):
            max_body = max((v['body_celkem'] for v in varianty), default=0)
            with ui.card().classes('w-full p-5 mb-6 bg-indigo-50 border border-indigo-200 rounded-2xl'):
                ui.label('Aktuální výsledky hlasování').classes('font-bold text-indigo-700 mb-4 text-lg')
                with ui.row().classes('gap-8 flex-wrap'):
                    for v in varianty:
                        je_vitez = v['body_celkem'] == max_body and max_body > 0
                        bck = 'bg-indigo-600 text-white shadow-lg' if je_vitez else 'bg-white text-indigo-700 border border-indigo-200'
                        with ui.card().classes(f'px-8 py-4 rounded-2xl items-center {bck}'):
                            ui.label(f'Varianta {v["pismeno"]}').classes('font-bold text-sm')
                            ui.label(str(v['body_celkem'])).classes('text-4xl font-black')
                            ui.label('bodů').classes('text-xs')
                            if je_vitez:
                                ui.label('🏆 Vedoucí').classes('text-xs font-bold mt-1')

        # Zjisti hlas uživatele
        uzivatel_hlas_varianta_id = None
        for _v in varianty:
            if any(h['user_id'] == user_id for h in _v['hlasy']):
                uzivatel_hlas_varianta_id = _v['id']
                break

        for v in varianty:
            with ui.card().classes('w-full p-6 mb-5 shadow-sm rounded-2xl border border-gray-200'):
                with ui.row().classes('w-full justify-between items-center mb-4'):
                    ui.label(f'Varianta {v["pismeno"]}').classes('text-xl font-extrabold text-gray-800')
                    if je_spravce or sv != 'otevreno':
                        bp = v['body_celkem']
                        body_cls = 'bg-green-100 text-green-800' if bp >= 10 else 'bg-yellow-100 text-yellow-800' if bp >= 5 else 'bg-gray-100 text-gray-700'
                        ui.label(f'{bp} bodů celkem').classes(f'font-black px-4 py-1 rounded-full text-sm {body_cls}')

                fotos = v.get('fotografie', [])
                if fotos:
                    with ui.row().classes('gap-3 flex-wrap mb-5'):
                        for foto in fotos:
                            img_url = f'{self.foto_route}/{pripad_id}/{v["pismeno"]}/{foto["filename"]}'

                            def zoom(url=img_url, fname=foto.get('original_name', '')):
                                with ui.dialog().props('maximized') as zdlg:
                                    with ui.element('div').style(
                                        'position:relative;width:100vw;height:100vh;'
                                        'background:#000;overflow:hidden;'
                                    ):
                                        with ui.element('div').style(
                                            'position:absolute;top:0;left:0;right:0;height:52px;'
                                            'background:#111827;z-index:10;'
                                            'display:flex;align-items:center;gap:4px;padding:0 12px;'
                                        ):
                                            ui.icon('photo').style('color:#9ca3af;font-size:20px;flex-shrink:0;margin-right:4px')
                                            ui.label(fname or '').style(
                                                'font-size:13px;color:#d1d5db;flex:1;'
                                                'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;')
                                            ui.button(icon='remove',     on_click=lambda: ui.run_javascript('window._znZoom(-0.35)')).props('flat round size=sm color=grey-4').tooltip('Zmenšit')
                                            ui.button(icon='add',        on_click=lambda: ui.run_javascript('window._znZoom(+0.35)')).props('flat round size=sm color=grey-4').tooltip('Zvětšit')
                                            ui.button(icon='fit_screen', on_click=lambda: ui.run_javascript('window._znZoom(0)')).props('flat round size=sm color=grey-4').tooltip('Původní velikost')
                                            ui.button(icon='close',      on_click=zdlg.close).props('flat round size=sm color=white').style('margin-left:4px')
                                        with ui.element('div').style(
                                            'position:absolute;top:52px;left:0;right:0;bottom:0;'
                                            'overflow:hidden;cursor:grab;'
                                            'display:flex;align-items:center;justify-content:center;'
                                        ).props('id=zn_viewer'):
                                            ui.element('img').props(f'src="{url}" id=zn_img draggable=false').style(
                                                'max-width:100%;max-height:100%;display:block;'
                                                'transform-origin:center center;user-select:none;'
                                            )
                                zdlg.open()
                                ui.run_javascript('''
                                    setTimeout(function () {
                                        var el  = document.getElementById('zn_viewer');
                                        var img = document.getElementById('zn_img');
                                        if (!el || !img) return;
                                        var scale = 1, tx = 0, ty = 0;
                                        var drag = false, sx = 0, sy = 0, otx = 0, oty = 0;
                                        function applyT() {
                                            img.style.transform =
                                                'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
                                        }
                                        el.addEventListener('wheel', function (e) {
                                            e.preventDefault();
                                            var factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
                                            scale = Math.max(0.1, Math.min(12, scale * factor));
                                            applyT();
                                        }, {passive: false});
                                        el.addEventListener('mousedown', function (e) {
                                            drag = true; sx = e.clientX; sy = e.clientY;
                                            otx = tx; oty = ty;
                                            el.style.cursor = 'grabbing';
                                            e.preventDefault();
                                        });
                                        document.addEventListener('mousemove', function (e) {
                                            if (!drag) return;
                                            tx = otx + (e.clientX - sx);
                                            ty = oty + (e.clientY - sy);
                                            applyT();
                                        });
                                        document.addEventListener('mouseup', function () {
                                            drag = false;
                                            if (el) el.style.cursor = 'grab';
                                        });
                                        var lastDist = 0;
                                        el.addEventListener('touchstart', function (e) {
                                            if (e.touches.length === 2) {
                                                lastDist = Math.hypot(
                                                    e.touches[0].clientX - e.touches[1].clientX,
                                                    e.touches[0].clientY - e.touches[1].clientY);
                                            } else if (e.touches.length === 1) {
                                                drag = true;
                                                sx = e.touches[0].clientX; sy = e.touches[0].clientY;
                                                otx = tx; oty = ty;
                                            }
                                        }, {passive: true});
                                        el.addEventListener('touchmove', function (e) {
                                            if (e.touches.length === 2) {
                                                var d = Math.hypot(
                                                    e.touches[0].clientX - e.touches[1].clientX,
                                                    e.touches[0].clientY - e.touches[1].clientY);
                                                scale = Math.max(0.1, Math.min(12, scale * (d / lastDist)));
                                                lastDist = d; applyT(); e.preventDefault();
                                            } else if (drag && e.touches.length === 1) {
                                                tx = otx + (e.touches[0].clientX - sx);
                                                ty = oty + (e.touches[0].clientY - sy);
                                                applyT();
                                            }
                                        }, {passive: false});
                                        el.addEventListener('touchend', function () { drag = false; });
                                        window._znZoom = function (delta) {
                                            if (delta === 0) { scale = 1; tx = 0; ty = 0; }
                                            else { scale = Math.max(0.1, Math.min(12, scale + delta)); }
                                            applyT();
                                        };
                                    }, 150);
                                ''')

                            with ui.card().classes(
                                f'w-36 h-36 overflow-hidden p-0 cursor-pointer rounded-xl border-2 '
                                f'border-transparent hover:border-{self.barva}-400 transition-all relative group'
                            ).on('click', zoom):
                                ui.image(img_url).classes('w-full h-full object-cover')
                                ui.icon('zoom_in', size='sm').classes(
                                    'absolute bottom-1 right-1 text-white opacity-0 group-hover:opacity-100 transition-opacity drop-shadow')
                else:
                    ui.label('Žádné fotografie.').classes('italic text-gray-400 mb-4')

                with ui.row().classes('w-full items-center justify-between flex-wrap gap-3'):
                    if je_otevreno and sv != 'storno' and je_uzivatel:
                        if uzivatel_hlas_varianta_id is not None:
                            if uzivatel_hlas_varianta_id == v['id']:
                                with ui.row().classes('gap-2 items-center'):
                                    ui.icon('check_circle', color='green').classes('text-xl')
                                    ui.label('Váš hlas (1 bod)').classes('font-bold text-green-700 text-sm')
                                    ui.label('— hlasování uzamčeno').classes('text-xs text-gray-400 italic')
                            else:
                                ui.label('Hlasoval/a jste pro jinou variantu.').classes('text-xs text-gray-400 italic')
                        else:
                            def _make_hlas(vid=v['id'], pp=pripad_id, pm=v['pismeno']):
                                async def hlasovat():
                                    ok = await asyncio.to_thread(self._uloz_hlas, vid, user_id, pp)
                                    if ok:
                                        intranet_logger.log_activity(user_name, self.log_kategorie,
                                            f"Hlasování — případ {pp} varianta {pm} = 1 bod")
                                    else:
                                        ui.notify('Váš hlas již byl zaznamenán a nelze jej změnit.', type='warning')
                                    ui.timer(0, self.vykresli.refresh, once=True)
                                return hlasovat
                            ui.button('Hlasovat (1 bod)', icon='how_to_vote', on_click=_make_hlas()).classes(
                                f'bg-{self.barva}-600 hover:bg-{self.barva}-700 text-white font-bold h-9 px-4 rounded-xl shadow-sm')
                    elif not je_uzivatel:
                        ui.label('Nemáte oprávnění hlasovat.').classes('text-xs text-gray-400 italic')
                    elif sv == 'pred':
                        ui.label('Hlasování zatím nezahájeno.').classes('text-xs text-orange-500 italic')
                    elif sv in ('po', 'uzavreno'):
                        ui.label('Hlasování bylo ukončeno.').classes('text-xs text-gray-500 italic')

                    if je_spravce or sv != 'otevreno':
                        hlasy = v['hlasy']
                        def ukaz_hlasy(hl=hlasy, pm=v['pismeno']):
                            with ui.dialog() as edlg, ui.card().classes('p-6 rounded-2xl w-full max-w-sm'):
                                ui.label(f'Hlasování — varianta {pm}').classes('font-bold text-lg mb-4')
                                if not hl:
                                    ui.label('Zatím nikdo nehlasoval.').classes('italic text-gray-400')
                                else:
                                    for h in hl:
                                        with ui.row().classes('w-full justify-between py-2 border-b border-gray-100'):
                                            ui.label(h['uzivatel_jmeno']).classes('font-bold text-gray-700')
                                            ui.label(f'{h["body"]} bod(y)').classes(f'text-{self.barva}-600 font-black')
                                ui.button('Zavřít', on_click=edlg.close).classes('mt-4 bg-gray-400 text-white font-bold w-full')
                            edlg.open()
                        ui.button(icon='visibility', on_click=ukaz_hlasy).props('flat round').classes(
                            f'text-gray-400 hover:text-{self.barva}-500').tooltip('Zobrazit hlasování')

        # --- KOMENTÁŘE ---
        ui.separator().classes('my-6')
        ui.label('Konverzace / Připomínky').classes('text-xl font-bold text-gray-700 mb-4')

        komentare = pripad.get('komentare', [])
        with ui.column().classes('w-full gap-3 mb-6'):
            if not komentare:
                ui.label('Zatím žádné komentáře.').classes('italic text-gray-400')
            for k in komentare:
                _ca = k['created_at']
                cas = (f'{_ca.day}. {_ca.month}. {_ca.year} {_ca.strftime("%H:%M")}'
                       if isinstance(_ca, datetime.datetime) else str(_ca)[:16])
                with ui.card().classes('w-full p-4 bg-gray-50 rounded-xl border border-gray-200'):
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label(k['uzivatel_jmeno']).classes('font-bold text-gray-800 text-sm')
                        ui.label(cas).classes('text-xs text-gray-400')
                    ui.label(k['text']).classes('text-gray-700')

        if je_uzivatel:
            txt_input = ui.textarea('Napsat komentář...').classes('w-full').props('outlined rows=2')

            async def odeslat_komentar():
                txt = txt_input.value.strip()
                if not txt: return
                ok = await asyncio.to_thread(self._pridat_komentar, pripad_id, user_id, txt)
                if ok:
                    txt_input.value = ''
                    ui.timer(0, self.vykresli.refresh, once=True)

            ui.button('Odeslat komentář', icon='send', on_click=odeslat_komentar).classes(
                f'bg-{self.barva}-600 hover:bg-{self.barva}-700 text-white font-bold h-10 px-6 mt-2 rounded-xl shadow-sm')
