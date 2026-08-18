import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
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

def odesli_upozorneni_email(prijemce, predmet, text):
    sys_log(f"=== START ODESÍLÁNÍ E-MAILU PRO: {prijemce} ===")
    try:
        sys_log("[KROK 1] Načítání nastavení intranetu...")
        nastaveni = intranet_data.nacti_nastaveni_intranetu()

        if not nastaveni.get("emaily_zapnuty", True):
            sys_log(" -> INFO: E-maily jsou globálně vypnuty v nastavení. Přerušuji.")
            return False

        sys_log("[KROK 2] Čtení SMTP/IMAP konfigurace...")
        smtp_server = nastaveni.get("smtp_server")
        smtp_port_raw = nastaveni.get("smtp_port")

        try:
            smtp_port = int(smtp_port_raw) if smtp_port_raw else 465
        except ValueError:
            smtp_port = 465

        smtp_user = nastaveni.get("smtp_user")
        smtp_password = nastaveni.get("smtp_password")

        if not smtp_server or not smtp_user or not smtp_password:
            msg = " -> CHYBA: Nejsou vyplněny přihlašovací údaje (Server, Uživatel nebo Heslo chybí)!"
            sys_log(msg)
            return False

        sys_log("[KROK 3] Sestavování e-mailové zprávy...")
        msg = MIMEMultipart('related')
        msg['From'] = smtp_user
        msg['To'] = prijemce
        msg['Subject'] = Header(predmet, 'utf-8')
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(text, 'plain', 'utf-8'))
        alt.attach(MIMEText(_text_na_html(text), 'html', 'utf-8'))

        sys_log(f"[KROK 4] Připojování k SMTP serveru {smtp_server}:{smtp_port} (Timeout 15s)...")
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()

        sys_log("[KROK 5] Přihlašování k SMTP (ověřování uživatele a hesla)...")
        server.login(smtp_user, smtp_password)

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
        imap_server = nastaveni.get("imap_server")
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


def odesli_email_s_prilohou(prijemce, predmet, text, cesta_souboru):
    """Odešle e-mail s textovým tělem a přiloženým souborem."""
    sys_log(f"=== IKOS EMAIL S PŘÍLOHOU PRO: {prijemce} ===")
    try:
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
        if not nastaveni.get("emaily_zapnuty", True):
            sys_log(" -> INFO: E-maily jsou globálně vypnuty.")
            return False

        smtp_server   = nastaveni.get("smtp_server")
        smtp_port_raw = nastaveni.get("smtp_port")
        smtp_port     = int(smtp_port_raw) if smtp_port_raw else 465
        smtp_user     = nastaveni.get("smtp_user")
        smtp_password = nastaveni.get("smtp_password")

        if not smtp_server or not smtp_user or not smtp_password:
            sys_log(" -> CHYBA: Chybí SMTP přihlašovací údaje.")
            return False

        msg = MIMEMultipart()
        msg['From']    = smtp_user
        msg['To']      = prijemce
        msg['Subject'] = Header(predmet, 'utf-8')
        msg.attach(MIMEText(text, 'plain', 'utf-8'))

        # Příloha
        nazev_souboru = os.path.basename(cesta_souboru)
        mime_type, _ = mimetypes.guess_type(cesta_souboru)
        if not mime_type:
            mime_type = 'application/octet-stream'
        main_type, sub_type = mime_type.split('/', 1)
        with open(cesta_souboru, 'rb') as f:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=nazev_souboru)
        msg.attach(part)

        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.ehlo(); server.starttls(); server.ehlo()

        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, prijemce, msg.as_string())
        server.quit()

        intranet_logger.log_activity("Systém E-mail", "Odeslání e-mailu", f"IKOS příloha | Příjemce: {prijemce} | Soubor: {nazev_souboru}")
        sys_log(f" -> ÚSPĚCH: IKOS e-mail odeslán na {prijemce}.")
        return True
    except Exception as e:
        sys_log(f" -> CHYBA: IKOS e-mail selhal ({prijemce}): {e}")
        traceback.print_exc()
        return False


def odesli_html_email(prijemce, predmet, html_obsah, inline_soubory=None):
    """
    Odešle HTML e-mail s volitelnými inline obrázky.
    inline_soubory: seznam (cid, cesta) pro vložení přes <img src="cid:...">
    """
    sys_log(f"=== START ODESÍLÁNÍ HTML E-MAILU PRO: {prijemce} ===")
    try:
        nastaveni = intranet_data.nacti_nastaveni_intranetu()
        if not nastaveni.get("emaily_zapnuty", True):
            sys_log(" -> INFO: E-maily jsou globálně vypnuty. Přerušuji.")
            return False

        smtp_server   = nastaveni.get("smtp_server")
        smtp_port_raw = nastaveni.get("smtp_port")
        try:
            smtp_port = int(smtp_port_raw) if smtp_port_raw else 465
        except ValueError:
            smtp_port = 465
        smtp_user     = nastaveni.get("smtp_user")
        smtp_password = nastaveni.get("smtp_password")

        if not smtp_server or not smtp_user or not smtp_password:
            sys_log(" -> CHYBA: Chybí SMTP přihlašovací údaje.")
            return False

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
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.ehlo(); server.starttls(); server.ehlo()

        server.login(smtp_user, smtp_password)
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
        imap_server = nastaveni.get("imap_server") or smtp_server.replace('smtp', 'imap')
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