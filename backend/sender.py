import logging
import smtplib
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from . import config

logger = logging.getLogger(__name__)


def wrap_in_html(body, sender_name):
    paragraphs = body.split("\n\n")
    html_paragraphs = ""
    for p in paragraphs:
        if p.strip():
            cleaned = escape(p.strip())
            cleaned = cleaned.replace("\n", "<br>")
            html_paragraphs += "<p>" + cleaned + "</p>"

    html = "<!DOCTYPE html>"
    html += "<html><head><meta charset='UTF-8'></head>"
    html += "<body style='font-family: Arial, sans-serif; color:#333; line-height:1.6; max-width:600px;'>"
    html += html_paragraphs
    html += "<hr style='border:none; border-top:1px solid #eee; margin:24px 0;'>"
    html += "<p style='font-size:12px; color:#999;'>"
    html += "If you don't want these emails, reply with unsubscribe."
    html += "</p></body></html>"
    return html


def _smtp_connection():
    """Opens an SMTP connection using the configured security mode."""
    if config.SMTP_SECURITY == "starttls":
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
        return server
    return smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)


def send_email(to_email, subject, html_body, text_body="") -> tuple[bool, str]:
    """Sends the message over the configured SMTP provider. Returns (sent, error_message)."""
    if not config.smtp_configured():
        return False, (
            "Email sending is not configured. Set SMTP_USER and SMTP_PASSWORD in .env "
            f"for SMTP_PROVIDER={config.SMTP_PROVIDER}."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((config.SENDER_NAME, config.SMTP_USER))
    msg["To"] = to_email
    msg["Reply-To"] = config.SMTP_USER
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    msg["X-Mailer"] = "Email Automation"
    msg["List-Unsubscribe"] = "<mailto:" + config.SMTP_USER + "?subject=unsubscribe>"

    msg.attach(MIMEText(text_body or "Please view this message in an HTML-capable email client.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with _smtp_connection() as server:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_USER, to_email, msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError as error:
        logger.error("SMTP authentication failed: %s", error)
        return False, (
            f"SMTP authentication failed for {config.SMTP_PROVIDER}. "
            "Check SMTP_USER and SMTP_PASSWORD (use an app password if 2FA is enabled)."
        )
    except smtplib.SMTPException as error:
        logger.exception("Email send failed")
        return False, f"SMTP error: {error}" if str(error) else "Unknown SMTP error."
    except OSError as error:
        logger.exception("SMTP connection failed")
        return False, (
            f"Could not connect to {config.SMTP_HOST}:{config.SMTP_PORT} "
            f"({config.SMTP_SECURITY}). {error}"
        )
    except Exception as error:
        logger.exception("Email send failed")
        return False, str(error) or "Unknown SMTP error."
