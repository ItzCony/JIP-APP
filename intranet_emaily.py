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


def odesli_upozorneni_email(prijemce, predmet, text):
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
        msg = MIMEMultipart('related')
        msg['From'] = smtp_user
        msg['To'] = prijemce
        msg['Subject'] = Header(predmet, 'utf-8')
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(text, 'plain', 'utf-8'))
        alt.attach(MIMEText(_text_na_html(text), 'html', 'utf-8'))

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


def odesli_email_s_prilohou(prijemce, predmet, text, cesta_souboru):
    """Odešle e-mail s textovým tělem a přiloženým souborem."""
    sys_log(f"=== IKOS EMAIL S PŘÍLOHOU PRO: {prijemce} ===")
    try:
        smtp = intranet_data.nacti_smtp()
        if not smtp["enabled"]:
            sys_log(f" -> INFO: Odesílání e-mailů je vypnuto ({_smtp_duvod(smtp)}).")
            return False

        smtp_user = smtp["smtp_user"]

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

        server = _spoj_smtp(smtp)
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