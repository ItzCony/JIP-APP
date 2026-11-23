import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from email.header import Header
import imaplib
import mimetypes
import os
import time
import traceback

import intranet_data
import intranet_logger

def sys_log(text):
    """Pomocná funkce pro výpis do konzole (bez zápisu do Audit Logu)."""
    print(text)

def _text_na_html(text: str) -> str:
    """Převede plain-text e-mail na jednoduché HTML — URL → <a href>, \\n → <br>."""
    import html as _html
    escaped = _html.escape(text)
    # URL → klikatelný odkaz
    escaped = re.sub(
        r'(https?://[^\s<>"]+)',
        r'<a href="\1" style="color:#1d6fb8;">\1</a>',
        escaped,
    )
    # Řádkové zalomení
    body = escaped.replace('\n', '<br>\n')
    return (
        '<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;'
        'font-size:14px;color:#222;line-height:1.6;max-width:620px;margin:0 auto;padding:24px">'
        f'{body}'
        '</body></html>'
    )

def _smtp_duvod(smtp) -> str:
    """Proč se neodesílá — aby se ticho v logu dalo odlišit od chybějící konfigurace."""
    return "JIPKA_SMTP_ENABLED=0" if smtp["vypnuto_operatorem"] else "chybí JIPKA_SMTP_HOST/USER/PASS"


def _spoj_smtp(smtp):
    """Otevře a přihlásí SMTP spojení. Volající ho zavírá."""
    if smtp["smtp_port"] == 465:
        server = smtplib.SMTP_SSL(smtp["smtp_server"], smtp["smtp_port"], timeout=15)
    else:
        server = smtplib.SMTP(smtp["smtp_server"], smtp["smtp_port"], timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
    server.login(smtp["smtp_user"], smtp["smtp_password"])
    return server


def otestuj_spojeni():
    """Odešle kontrolní e-mail sám sobě (na smtp_user). Vrací (ok, hláška).

    V UI na to není tlačítko — kontrola SMTP se spouští ručně na serveru:
        sudo -u www-data python3 -c "import intranet_emaily as e; print(e.otestuj_spojeni())"
    (ve WorkingDirectory aplikace, aby se načetl stejný env z EnvironmentFile).

    Blokující (síť) — z UI by se muselo volat přes asyncio.to_thread.
    Jede úplně stejnou cestou jako ostrý provoz včetně IMAP kopie do Odeslané."""
    smtp = intranet_data.nacti_smtp()
    if not smtp["enabled"]:
        return False, f"Odesílání e-mailů je vypnuto ({_smtp_duvod(smtp)})."
    prijemce = smtp["smtp_user"]
    ok = odesli_upozorneni_email(
        prijemce,
        "Test spojení z intranetu",
        "Kontrolní e-mail z Nastavení portálu. Odesílání e-mailů funguje.",
    )
    if not ok:
        return False, "Odeslání selhalo — důvod je v logu služby (journalctl -u intranet)."
    return True, f"Kontrolní e-mail odeslán na {prijemce} ({smtp['smtp_server']}:{smtp['smtp_port']})."


def odesli_upozorneni_email(prijemce, predmet, text, prilohy=None):
    """`prilohy` = [(název souboru, bajty)] — připojí se jako soubory ke stažení."""
    sys_log(f"=== START ODESÍLÁNÍ E-MAILU PRO: {prijemce} ===")
    try:
        sys_log("[KROK 1] Čtení SMTP konfigurace z prostředí...")
        smtp = intranet_data.nacti_smtp()

        if not smtp["enabled"]:
            sys_log(f" -> INFO: Odesílání e-mailů je vypnuto ({_smtp_duvod(smtp)}). Přerušuji.")
            return False

        smtp_server = smtp["smtp_server"]
        smtp_port = smtp["smtp_port"]
        smtp_user = smtp["smtp_user"]
        smtp_password = smtp["smtp_password"]

        sys_log("[KROK 3] Sestavování e-mailové zprávy...")
        msg = MIMEMultipart('mixed' if prilohy else 'related')
        msg['From'] = smtp_user
        msg['To'] = prijemce
        msg['Subject'] = Header(predmet, 'utf-8')
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(text, 'plain', 'utf-8'))
        alt.attach(MIMEText(_text_na_html(text), 'html', 'utf-8'))
        for nazev, data in prilohy or ():
            soubor = MIMEApplication(data)
            soubor.add_header('Content-Disposition', 'attachment', filename=nazev)
            msg.attach(soubor)

        sys_log(f"[KROK 4] Připojování a přihlašování k SMTP {smtp_server}:{smtp_port} (Timeout 15s)...")
        server = _spoj_smtp(smtp)

        sys_log("[KROK 6] Odesílání zprávy přes SMTP...")
        server.sendmail(smtp_user, prijemce, msg.as_string())

        sys_log("[KROK 7] Odpojování od SMTP...")
        server.quit()
        sys_log(f" -> ÚSPĚCH: E-mail byl úspěšně odeslán na {prijemce}.")

    except smtplib.SMTPAuthenticationError:
        sys_log(" -> KRITICKÁ CHYBA SMTP: Server odmítl přihlášení (Zkontrolujte jméno, heslo, nebo oprávnění pro aplikace třetích stran).")
        return False
    except Exception as e:
        sys_log(f" -> KRITICKÁ CHYBA SMTP při odesílání: {e}")
        traceback.print_exc()
        return False

    # ------------------ IMAP ČÁST (Uložení do Odeslané pošty) ------------------
    sys_log("[KROK 8] Připojování k IMAP pro uložení do Odeslané pošty...")
    try:
        imap_server = smtp.get("imap_server")
        if not imap_server:
            imap_server = smtp_server.replace('smtp', 'imap')
            sys_log(f" -> IMAP server nebyl zadán, zkouším automaticky odvodit: {imap_server}")

        mail = imaplib.IMAP4_SSL(imap_server, 993)

        sys_log("[KROK 9] Přihlašování k IMAP...")
        mail.login(smtp_user, smtp_password)

        sys_log("[KROK 10] Hledání složky Odeslané...")
        slozky = ['"Sent Items"', 'Sent', 'Odeslané', 'Odeslana_posta', 'INBOX.Sent']
        ulozeno = False

        for slozka in slozky:
            status, _ = mail.select(slozka)
            if status == 'OK':
                sys_log(f" -> Nalezena složka: {slozka}, ukládám zprávu...")
                mail.append(slozka, '\\Seen', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                ulozeno = True
                break

        sys_log("[KROK 11] Odpojování od IMAP...")
        mail.logout()

        if not ulozeno:
            sys_log(" -> IMAP VAROVÁNÍ: E-mail odeslán, ale nenašla se složka pro jeho uložení (Odeslané).")
        else:
            sys_log(" -> IMAP ÚSPĚCH: Zpráva uložena do odeslané pošty.")

    except Exception as e_imap:
        sys_log(f" -> IMAP CHYBA: E-mail byl sice odeslán, ale selhalo uložení do složky Odeslané: {e_imap}")
        traceback.print_exc()

    sys_log(f"=== KONEC ODESÍLÁNÍ PRO: {prijemce} ===")
    intranet_logger.log_activity("Systém E-mail", "Odeslání e-mailu", f"Příjemce: {prijemce} | Předmět: {predmet}")
    return True


def odesli_html_email(prijemce, predmet, html_obsah, inline_soubory=None):
    """
    Odešle HTML e-mail s volitelnými inline obrázky.
    inline_soubory: seznam (cid, cesta) pro vložení přes <img src="cid:...">
    """
    sys_log(f"=== START ODESÍLÁNÍ HTML E-MAILU PRO: {prijemce} ===")
    try:
        smtp = intranet_data.nacti_smtp()
        if not smtp["enabled"]:
            sys_log(f" -> INFO: Odesílání e-mailů je vypnuto ({_smtp_duvod(smtp)}). Přerušuji.")
            return False

        smtp_server   = smtp["smtp_server"]
        smtp_port     = smtp["smtp_port"]
        smtp_user     = smtp["smtp_user"]
        smtp_password = smtp["smtp_password"]

        msg = MIMEMultipart('related')
        msg['From']    = smtp_user
        msg['To']      = prijemce
        msg['Subject'] = Header(predmet, 'utf-8')

        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(html_obsah, 'html', 'utf-8'))

        if inline_soubory:
            for cid, cesta in inline_soubory:
                try:
                    with open(cesta, 'rb') as f:
                        img_data = f.read()
                    subtype = (mimetypes.guess_type(cesta)[0] or 'image/png').split('/')[-1]
                    img = MIMEImage(img_data, _subtype=subtype)
                    img.add_header('Content-ID', f'<{cid}>')
                    img.add_header('Content-Disposition', 'inline', filename=os.path.basename(cesta))
                    msg.attach(img)
                except Exception as e:
                    sys_log(f" -> VAROVÁNÍ: Nelze načíst obrázek {cesta}: {e}")

        sys_log(f"[KROK 4] Připojování k SMTP {smtp_server}:{smtp_port}...")
        server = _spoj_smtp(smtp)
        server.sendmail(smtp_user, prijemce, msg.as_string())
        server.quit()
        sys_log(f" -> ÚSPĚCH: HTML e-mail odeslán na {prijemce}.")

    except smtplib.SMTPAuthenticationError:
        sys_log(" -> KRITICKÁ CHYBA SMTP: Selhalo přihlášení.")
        return False
    except Exception as e:
        sys_log(f" -> KRITICKÁ CHYBA SMTP: {e}")
        traceback.print_exc()
        return False

    try:
        imap_server = smtp.get("imap_server") or smtp_server.replace('smtp', 'imap')
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(smtp_user, smtp_password)
        for slozka in ['"Sent Items"', 'Sent', 'Odeslané', 'Odeslana_posta', 'INBOX.Sent']:
            status, _ = mail.select(slozka)
            if status == 'OK':
                mail.append(slozka, '\\Seen', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                break
        mail.logout()
    except Exception as e_imap:
        sys_log(f" -> IMAP CHYBA: {e_imap}")

    sys_log(f"=== KONEC HTML ODESÍLÁNÍ PRO: {prijemce} ===")
    intranet_logger.log_activity("Systém E-mail", "Odeslání HTML e-mailu", f"Příjemce: {prijemce} | Předmět: {predmet}")
    return True


# =====================================================================
# DALŠÍ ÚČTY (např. sankce.dodavatel@…) — vlastní přihlášení z env
# =====================================================================
_RE_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}")


def je_platny_email(adresa) -> bool:
    """Syntaktická kontrola jedné adresy (bez mezer, s doménou a TLD ≥ 2 znaky)."""
    a = str(adresa or '').strip()
    return bool(a) and '..' not in a and bool(_RE_EMAIL.fullmatch(a))


def nacti_smtp_uctu(prefix: str) -> dict:
    """SMTP konfigurace dalšího účtu z env, stejný tvar jako intranet_data.nacti_smtp().

    Povinné:  {prefix}_USER, {prefix}_PASS
    Volitelné: {prefix}_HOST, {prefix}_PORT, {prefix}_IMAP_HOST — když chybí,
               převezmou se z hlavní konfigurace JIPKA_SMTP_* (stejný poštovní server).
    Vypínač JIPKA_SMTP_ENABLED=0 (odstávka) vypíná i tento účet.
    Necachuje se — čte jen env, je to levné."""
    zakl = intranet_data.nacti_smtp()
    try:
        port = int(float(os.environ.get(f'{prefix}_PORT', '') or zakl["smtp_port"] or 465))
    except (ValueError, TypeError):
        port = zakl["smtp_port"] or 465
    cfg = {
        "smtp_server":   os.environ.get(f'{prefix}_HOST', '').strip() or zakl["smtp_server"],
        "smtp_port":     port,
        "smtp_user":     os.environ.get(f'{prefix}_USER', '').strip(),
        "smtp_password": os.environ.get(f'{prefix}_PASS', ''),
        "imap_server":   os.environ.get(f'{prefix}_IMAP_HOST', '').strip() or zakl["imap_server"],
    }
    cfg["udaje_kompletni"] = bool(cfg["smtp_server"] and cfg["smtp_user"] and cfg["smtp_password"])
    cfg["vypnuto_operatorem"] = zakl["vypnuto_operatorem"]
    cfg["enabled"] = cfg["udaje_kompletni"] and not cfg["vypnuto_operatorem"]
    cfg["duvod"] = ('' if cfg["enabled"] else
                    'vypnuto přes JIPKA_SMTP_ENABLED' if cfg["vypnuto_operatorem"] else
                    f'chybí {prefix}_USER / {prefix}_PASS'
                    + ('' if cfg["smtp_server"] else ' (a JIPKA_SMTP_HOST)'))
    return cfg


def _uloz_do_odeslanych(smtp: dict, msg_bytes: bytes) -> bool:
    """Uloží kopii zprávy do složky Odeslané (IMAP). Selhání jen zaloguje."""
    try:
        imap_server = smtp.get("imap_server") or smtp["smtp_server"].replace('smtp', 'imap')
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(smtp["smtp_user"], smtp["smtp_password"])
        ulozeno = False
        for slozka in ['"Sent Items"', 'Sent', 'Odeslané', 'Odeslana_posta', 'INBOX.Sent']:
            status, _ = mail.select(slozka)
            if status == 'OK':
                mail.append(slozka, '\\Seen', imaplib.Time2Internaldate(time.time()), msg_bytes)
                ulozeno = True
                break
        mail.logout()
        if not ulozeno:
            sys_log(" -> IMAP VAROVÁNÍ: nenašla se složka Odeslané, kopie neuložena.")
        return ulozeno
    except Exception as e_imap:
        sys_log(f" -> IMAP CHYBA (kopie do Odeslané): {e_imap}")
        return False


def odesli_email_z_uctu(smtp: dict, prijemci: list, predmet: str, text: str,
                        prilohy=None, odesilatel_jmeno: str = '', reply_to: str = '') -> tuple:
    """Odešle JEDNU zprávu všem `prijemci` (To) z účtu `smtp` (viz nacti_smtp_uctu).

    `prilohy` = [(název_souboru, bajty)]. Na rozdíl od odesli_upozorneni_email
    vrací (ok, text): při ok=False důvod selhání, při ok=True případné varování
    (server odmítl část adres) nebo ''. Hromadná rozesílka ho zapisuje k dodavateli.
    Neuložení kopie do Odeslané odeslání nezneplatní.
    Blokující (síť) — z UI volat přes asyncio.to_thread."""
    import email.utils as _eu
    prijemci = [p.strip() for p in (prijemci or []) if p and p.strip()]
    if not prijemci:
        return False, 'Žádný příjemce.'
    if not smtp.get("enabled"):
        return False, f'Odesílání je vypnuto ({smtp.get("duvod") or "chybí konfigurace"}).'

    msg = MIMEMultipart('mixed')
    msg['From'] = _eu.formataddr((odesilatel_jmeno, smtp["smtp_user"]), charset='utf-8') \
        if odesilatel_jmeno else smtp["smtp_user"]
    msg['To'] = ', '.join(prijemci)
    if reply_to:
        msg['Reply-To'] = reply_to
    msg['Subject'] = Header(predmet, 'utf-8')
    msg['Date'] = _eu.formatdate(localtime=True)
    msg['Message-ID'] = _eu.make_msgid(domain=smtp["smtp_user"].rpartition('@')[2] or None)
    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(text, 'plain', 'utf-8'))
    alt.attach(MIMEText(_text_na_html(text), 'html', 'utf-8'))
    msg.attach(alt)
    for nazev, data in (prilohy or []):
        hlavni, _, podtyp = (mimetypes.guess_type(nazev)[0] or '').partition('/')
        cast = MIMEApplication(data, _subtype=podtyp if hlavni == 'application' else 'octet-stream')
        cast.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', nazev))
        msg.attach(cast)

    varovani = ''
    try:
        server = _spoj_smtp(smtp)
        try:
            # Část adres odmítnutých serverem nevyhazuje výjimku — vrátí se slovník.
            castecne = server.sendmail(smtp["smtp_user"], prijemci, msg.as_string()) or {}
            if castecne:
                varovani = 'Server odmítl část adres: ' + ', '.join(sorted(castecne))
                sys_log(f" -> SMTP {smtp['smtp_user']}: {varovani}")
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        sys_log(f" -> SMTP {smtp['smtp_user']}: selhalo přihlášení.")
        return False, f'Selhalo přihlášení k poště ({smtp["smtp_user"]}) — zkontrolujte heslo.'
    except smtplib.SMTPRecipientsRefused as e:
        odmitnuti = ', '.join(sorted(e.recipients or {}))
        sys_log(f" -> SMTP {smtp['smtp_user']}: server odmítl příjemce {odmitnuti}")
        return False, f'Server odmítl adresy: {odmitnuti}'
    except Exception as e:
        sys_log(f" -> SMTP {smtp['smtp_user']}: {e}")
        traceback.print_exc()
        return False, f'Chyba odesílání: {e}'

    _uloz_do_odeslanych(smtp, msg.as_bytes())
    intranet_logger.log_activity("Systém E-mail", "Odeslání e-mailu",
                                 f"Účet: {smtp['smtp_user']} | Příjemci: {', '.join(prijemci)} "
                                 f"| Předmět: {predmet} | Příloh: {len(prilohy or [])}")
    return True, varovani