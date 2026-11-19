import base64
import logging
import os
import smtplib
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from . import config

logger = logging.getLogger(__name__)


def wrap_in_html(body, sender_name, unsubscribe_url: str | None = None):
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
    if unsubscribe_url:
        html += "Don't want these emails? "
        html += "<a href='" + escape(unsubscribe_url, quote=True) + "' style='color:#999;'>Unsubscribe</a>."
    else:
        html += "If you don't want these emails, reply with unsubscribe."
    html += "</p></body></html>"
    return html


_SMTP_PROVIDER_DEFAULTS = {
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    "zoho": ("smtp.zoho.com", 465, "ssl"),
    "outlook": ("smtp.office365.com", 587, "starttls"),
    "custom": ("", 587, "starttls"),
}


def _get_smtp_settings(provider: str | None = None):
    provider = (provider or config.SMTP_PROVIDER or "zoho").strip().lower()
    if provider not in _SMTP_PROVIDER_DEFAULTS:
        provider = "custom"

    defaults = _SMTP_PROVIDER_DEFAULTS[provider]
    matches_env = provider == config.SMTP_PROVIDER

    if provider == "custom":
        host = config.SMTP_HOST
        port = config.SMTP_PORT
        security = config.SMTP_SECURITY
        user = config.SMTP_USER
        password = config.SMTP_PASSWORD
    elif provider == "gmail":
        # .env overrides only apply to the provider they were configured for;
        # switching providers at runtime must use that provider's own host.
        host = (config.SMTP_HOST or defaults[0]) if matches_env else defaults[0]
        port = (config.SMTP_PORT or defaults[1]) if matches_env else defaults[1]
        security = (config.SMTP_SECURITY or defaults[2]) if matches_env else defaults[2]
        user = config.GMAIL_USER or (config.SMTP_USER if matches_env else "")
        password = config.GMAIL_APP_PASSWORD or (config.SMTP_PASSWORD if matches_env else "")
    elif provider == "outlook":
        host = (config.SMTP_HOST or defaults[0]) if matches_env else defaults[0]
        port = (config.SMTP_PORT or defaults[1]) if matches_env else defaults[1]
        security = (config.SMTP_SECURITY or defaults[2]) if matches_env else defaults[2]
        user = config.SMTP_USER if matches_env else ""
        password = config.SMTP_PASSWORD if matches_env else ""
    else:
        host = (config.SMTP_HOST or defaults[0]) if matches_env else defaults[0]
        port = (config.SMTP_PORT or defaults[1]) if matches_env else defaults[1]
        security = (config.SMTP_SECURITY or defaults[2]) if matches_env else defaults[2]
        # SMTP_USER/SMTP_PASSWORD belong to Zoho regardless of which provider
        # .env declares, so a runtime switch to Zoho still finds credentials.
        user = config.SMTP_USER_ENV
        password = config.SMTP_PASSWORD_ENV

    if security not in {"ssl", "starttls"}:
        security = defaults[2]
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = defaults[1]

    return {
        "provider": provider,
        "host": host,
        "port": port,
        "security": security,
        "user": user or "",
        "password": password or "",
        "oauth_token": "",
    }


def _smtp_connection(settings: dict):
    """Opens an SMTP connection using the configured security mode."""
    if settings["security"] == "starttls":
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
        return server
    return smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30)


def _authenticate(server, settings: dict) -> None:
    oauth_token = (settings.get("oauth_token") or "").strip()
    if oauth_token:
        auth_string = (
            f"user={settings['user']}\x01auth=Bearer {oauth_token}\x01\x01"
        )
        encoded = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")
        code, response = server.docmd("AUTH", "XOAUTH2 " + encoded)
        if code not in (235, 503):
            raise smtplib.SMTPAuthenticationError(code, response)
        return
    server.login(settings["user"], settings["password"])


def sending_domain(provider: str | None = None) -> str:
    user = _get_smtp_settings(provider)["user"]
    return user.rsplit("@", 1)[-1].strip().lower() if "@" in user else ""


def _message_from_settings(
    settings: dict,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    sender_name: str | None,
    unsubscribe_url: str | None,
):
    from_name = sender_name or config.SENDER_NAME
    from_email = settings.get("from_email") or settings["user"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_email
    msg["Reply-To"] = formataddr((from_name, settings.get("reply_to") or from_email))
    msg["Date"] = formatdate(localtime=False)

    sender_domain = from_email.rsplit("@", 1)[-1].strip().lower() if "@" in from_email else ""
    if sender_domain and all(ch.isalnum() or ch in ".-" for ch in sender_domain):
        msg["Message-ID"] = make_msgid(domain=sender_domain)
    else:
        msg["Message-ID"] = make_msgid()

    unsubscribe_targets = []
    if unsubscribe_url:
        unsubscribe_targets.append(f"<{unsubscribe_url}>")
    unsubscribe_targets.append(f"<mailto:{from_email}?subject=unsubscribe>")
    msg["List-Unsubscribe"] = ", ".join(unsubscribe_targets)
    if unsubscribe_url:
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    plain_body = text_body or "Please view this message in an HTML-capable email client."
    if unsubscribe_url:
        plain_body = plain_body.rstrip() + f"\n\nUnsubscribe: {unsubscribe_url}"
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def deliver_message(
    settings: dict,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str = "",
    sender_name: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[bool, str]:
    """Low-level delivery used by both .env senders and saved sender accounts."""
    if not settings.get("host") or not settings.get("user"):
        return False, "The sending account is missing an SMTP host or username."
    if not settings.get("oauth_token") and not settings.get("password"):
        return False, "The sending account is missing an SMTP password or OAuth token."

    try:
        msg = _message_from_settings(
            settings, to_email, subject, html_body, text_body, sender_name, unsubscribe_url
        )
        with _smtp_connection(settings) as server:
            _authenticate(server, settings)
            server.sendmail(settings["user"], to_email, msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError as error:
        logger.error("SMTP authentication failed: %s", error)
        return False, (
            "SMTP authentication failed. Check the username and app password, "
            "or reconnect OAuth if the token was revoked."
        )
    except smtplib.SMTPRecipientsRefused as error:
        return False, f"The recipient was refused by the mail server: {error}"
    except smtplib.SMTPException as error:
        logger.exception("Email send failed")
        return False, f"SMTP error: {error}" if str(error) else "Unknown SMTP error."
    except OSError as error:
        logger.exception("SMTP connection failed")
        return False, (
            f"Could not connect to {settings.get('host')}:{settings.get('port')} "
            f"({settings.get('security')}). {error}"
        )
    except Exception as error:
        logger.exception("Email send failed")
        return False, str(error) or "Unknown SMTP error."


def send_email(
    to_email,
    subject,
    html_body,
    text_body="",
    smtp_provider: str | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    sender_name: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[bool, str]:
    """Sends the message over the configured SMTP provider. Returns (sent, error_message)."""
    settings = _get_smtp_settings(smtp_provider)

    if smtp_user:
        settings["user"] = smtp_user.strip()
    if smtp_password:
        settings["password"] = smtp_password.strip()

    if not settings["user"] or not settings["password"] or not settings["host"]:
        credentials = (
            "GMAIL_USER and GMAIL_APP_PASSWORD"
            if settings["provider"] == "gmail"
            else "SMTP_USER and SMTP_PASSWORD"
        )
        location = (
            "the Vercel environment variables (then redeploy)"
            if os.getenv("VERCEL")
            else ".env"
        )
        return False, (
            f"Email sending is not configured for {settings['provider']}. "
            f"Set {credentials} in {location}."
        )

    return deliver_message(
        settings,
        to_email,
        subject,
        html_body,
        text_body,
        sender_name=sender_name,
        unsubscribe_url=unsubscribe_url,
    )


def send_with_settings(
    settings: dict,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str = "",
    sender_name: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[bool, str]:
    """Public wrapper for saved sender accounts (including OAuth tokens)."""
    return deliver_message(
        settings,
        to_email,
        subject,
        html_body,
        text_body,
        sender_name=sender_name,
        unsubscribe_url=unsubscribe_url,
    )


def _settings_from_account(account: dict) -> dict:
    """Maps a stored sender document onto the low-level SMTP settings shape."""
    settings = {
        "provider": account.get("provider") or "smtp",
        "host": (account.get("host") or "").strip(),
        "port": account.get("port") or 465,
        "security": (account.get("security") or "ssl").strip().lower(),
        "user": (account.get("username") or account.get("email") or "").strip(),
        "password": account.get("password") or "",
        "oauth_token": account.get("oauth_token") or "",
        "reply_to": (account.get("reply_to") or "").strip(),
        "from_email": (account.get("email") or "").strip(),
    }
    if settings["security"] not in {"ssl", "starttls"}:
        settings["security"] = "ssl"
    try:
        settings["port"] = int(settings["port"])
    except (TypeError, ValueError):
        settings["port"] = 465
    if settings["from_email"]:
        settings["user"] = settings["user"] or settings["from_email"]
    return settings


def send_account_email(
    account: dict,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str = "",
    unsubscribe_url: str | None = None,
    sender_name: str | None = None,
) -> tuple[bool, str, str]:
    """Sends through a saved sender account.

    Returns ``(sent, error_message, kind)`` where ``kind`` is ``"sent"``,
    ``"bounced"`` (the recipient address was rejected), or ``"failed"``.
    """
    settings = _settings_from_account(account)
    if not settings["host"]:
        return False, "The sending account is missing an SMTP host.", "failed"
    if not settings["user"]:
        return False, "The sending account is missing a username.", "failed"
    if not settings["oauth_token"] and not settings["password"]:
        return False, "The sending account is missing an SMTP password.", "failed"

    try:
        msg = _message_from_settings(
            settings,
            to_email,
            subject,
            html_body,
            text_body,
            sender_name or account.get("name") or config.SENDER_NAME,
            unsubscribe_url,
        )
        with _smtp_connection(settings) as server:
            _authenticate(server, settings)
            server.sendmail(settings["user"], to_email, msg.as_string())
        return True, "", "sent"
    except smtplib.SMTPRecipientsRefused as error:
        return False, f"The recipient was refused by the mail server: {error}", "bounced"
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed for %s", settings["user"])
        return (
            False,
            "SMTP authentication failed. Check the username and app password, "
            "or reconnect OAuth if the token was revoked.",
            "failed",
        )
    except smtplib.SMTPSenderRefused:
        return False, "The mail server refused the sender address.", "failed"
    except smtplib.SMTPException as error:
        logger.exception("Email send failed")
        return False, f"SMTP error: {error}" if str(error) else "Unknown SMTP error.", "failed"
    except OSError as error:
        logger.exception("SMTP connection failed")
        return (
            False,
            f"Could not connect to {settings['host']}:{settings['port']} "
            f"({settings['security']}). {error}",
            "failed",
        )
    except Exception as error:  # noqa: BLE001 - surface a readable failure to the UI
        logger.exception("Email send failed")
        return False, str(error) or "Unknown SMTP error.", "failed"


def test_account(account: dict) -> tuple[bool, str]:
    """Connects and authenticates without sending. Returns ``(ok, message)``."""
    settings = _settings_from_account(account)
    if not settings["host"] or not settings["user"]:
        return False, "This sender is missing its SMTP host or username."
    if not settings["oauth_token"] and not settings["password"]:
        return False, "This sender is missing its SMTP password or OAuth token."

    server = None
    try:
        server = _smtp_connection(settings)
        _authenticate(server, settings)
        server.noop()
        return True, "SMTP login succeeded."
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed. Check the username and app password."
    except smtplib.SMTPException as error:
        return False, f"SMTP error: {error}" if str(error) else "Unknown SMTP error."
    except OSError as error:
        return (
            False,
            f"Could not connect to {settings['host']}:{settings['port']} "
            f"({settings['security']}). {error}",
        )
    except Exception as error:  # noqa: BLE001 - report any adapter failure
        return False, str(error) or "Unknown SMTP error."
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
