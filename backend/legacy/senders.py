"""User-managed sending accounts (SMTP/IMAP + OAuth) and their DNS settings."""

from __future__ import annotations

import base64
import imaplib
import logging
import smtplib
from typing import Any

import httpx

from . import automation_store as store
from .. import credential_crypto
from .. import deliverability
from ..sender import send_with_settings
from ..verifier import is_valid_format

logger = logging.getLogger(__name__)


class SenderError(Exception):
    """Raised when a sender account cannot be created, resolved, or tested."""


PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "zoho": {
        "label": "Zoho Mail",
        "smtp_host": "smtp.zoho.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "imap_host": "imap.zoho.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "spf_include": "include:zoho.com",
        "dkim_selector": "zoho",
        "oauth_token_uri": "https://accounts.zoho.com/oauth/v2/token",
        "oauth_scope": "ZohoMail.messages.ALL,ZohoMail.accounts.READ",
    },
    "gmail": {
        "label": "Google Workspace / Gmail",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "spf_include": "include:_spf.google.com",
        "dkim_selector": "google",
        "oauth_token_uri": "https://oauth2.googleapis.com/token",
        "oauth_scope": "https://mail.google.com/",
    },
    "outlook": {
        "label": "Microsoft 365 / Outlook",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "spf_include": "include:spf.protection.outlook.com",
        "dkim_selector": "selector1",
        "oauth_token_uri": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "oauth_scope": "offline_access https://outlook.office.com/SMTP.Send",
    },
    "custom": {
        "label": "Custom SMTP / IMAP",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "imap_host": "",
        "imap_port": 993,
        "imap_security": "ssl",
        "spf_include": "",
        "dkim_selector": "default",
        "oauth_token_uri": "",
        "oauth_scope": "",
    },
}

ALLOWED_PROVIDERS = tuple(PROVIDER_CATALOG)
ALLOWED_AUTH_TYPES = ("password", "oauth2")
ALLOWED_SECURITY = ("ssl", "starttls")
ALLOWED_DMARC = ("none", "quarantine", "reject")


def provider_catalog() -> list[dict[str, Any]]:
    return [{"id": key, **value} for key, value in PROVIDER_CATALOG.items()]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _domain_from_email(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""


def _provider(provider: str) -> str:
    provider = _clean(provider).lower()
    return provider if provider in PROVIDER_CATALOG else "custom"


def _defaults(provider: str) -> dict[str, Any]:
    return PROVIDER_CATALOG[_provider(provider)]


def normalize_sender(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merges a create/update payload into a full sender document."""
    existing = existing or {}
    payload = payload or {}

    def pick(key: str, default: Any = "") -> Any:
        if key in payload and payload[key] is not None:
            return payload[key]
        if key in existing:
            return existing[key]
        return default

    email = _clean(pick("email")).lower()
    if not is_valid_format(email):
        raise SenderError("Enter a valid sender email address.")

    provider = _provider(_clean(pick("provider", "custom")))
    old_provider = _provider(_clean(existing.get("provider", provider)))
    defaults = _defaults(provider)

    host_provided = "smtp_host" in payload and payload.get("smtp_host") is not None
    imap_provided = "imap_host" in payload and payload.get("imap_host") is not None

    smtp_host = _clean(pick("smtp_host")) or defaults["smtp_host"]
    if existing and provider != old_provider and not host_provided:
        smtp_host = defaults["smtp_host"]
    imap_host = _clean(pick("imap_host")) or defaults["imap_host"]
    if existing and provider != old_provider and not imap_provided:
        imap_host = defaults["imap_host"]

    auth_type = _clean(pick("auth_type", "password")).lower()
    if auth_type not in ALLOWED_AUTH_TYPES:
        auth_type = "password"

    smtp_security = _clean(pick("smtp_security", defaults["smtp_security"])).lower()
    if smtp_security not in ALLOWED_SECURITY:
        smtp_security = defaults["smtp_security"]
    imap_security = _clean(pick("imap_security", defaults["imap_security"])).lower()
    if imap_security not in ALLOWED_SECURITY:
        imap_security = defaults["imap_security"]

    dmarc_policy = _clean(pick("dmarc_policy", "none")).lower()
    if dmarc_policy not in ALLOWED_DMARC:
        dmarc_policy = "none"

    status = _clean(pick("status", "active")).lower()
    if status not in {"active", "paused"}:
        status = "active"

    doc = {
        "id": existing.get("id") or store.new_id("snd"),
        "label": _clean(pick("label")) or email,
        "email": email,
        "domain": _domain_from_email(email),
        "provider": provider,
        "auth_type": auth_type,
        "smtp_host": smtp_host,
        "smtp_port": _int(pick("smtp_port", defaults["smtp_port"]), defaults["smtp_port"], 1, 65535),
        "smtp_security": smtp_security,
        "smtp_username": _clean(pick("smtp_username")) or email,
        "smtp_password": existing.get("smtp_password", ""),
        "imap_host": imap_host,
        "imap_port": _int(pick("imap_port", defaults["imap_port"]), defaults["imap_port"], 1, 65535),
        "imap_security": imap_security,
        "imap_username": _clean(pick("imap_username")) or email,
        "oauth_client_id": _clean(pick("oauth_client_id", defaults.get("oauth_client_id", ""))),
        "oauth_client_secret": existing.get("oauth_client_secret", ""),
        "oauth_refresh_token": existing.get("oauth_refresh_token", ""),
        "oauth_token_uri": _clean(pick("oauth_token_uri", defaults.get("oauth_token_uri", ""))),
        "oauth_scope": _clean(pick("oauth_scope", defaults.get("oauth_scope", ""))),
        "from_name": _clean(pick("from_name")) or _clean(pick("label")) or email,
        "reply_to": _clean(pick("reply_to")),
        "daily_limit": _int(pick("daily_limit", 40), 40, 1, 500),
        "hourly_limit": _int(pick("hourly_limit", 8), 8, 1, 100),
        "dkim_selector": _clean(pick("dkim_selector", defaults["dkim_selector"])) or defaults["dkim_selector"],
        "spf_include": _clean(pick("spf_include", defaults["spf_include"])) or defaults["spf_include"],
        "dmarc_policy": dmarc_policy,
        "status": status,
        "last_test_at": existing.get("last_test_at", ""),
        "last_test_ok": existing.get("last_test_ok", None),
        "last_test_error": existing.get("last_test_error", ""),
    }

    # Secrets are encrypted only when a new value is supplied. Blank on update
    # means "keep the existing secret".
    for key in ("smtp_password", "oauth_client_secret", "oauth_refresh_token"):
        supplied = payload.get(key)
        if supplied is not None and _clean(supplied):
            doc[key] = credential_crypto.encrypt(_clean(supplied))
        elif not existing:
            doc[key] = ""

    if auth_type == "password" and not doc["smtp_password"]:
        raise SenderError("Enter the SMTP password or app password for this account.")
    if auth_type == "oauth2":
        if not (
            doc.get("oauth_client_id")
            and doc.get("oauth_client_secret")
            and doc.get("oauth_refresh_token")
        ):
            raise SenderError(
                "OAuth needs a client ID, client secret, and refresh token. "
                "Use an app password instead if you do not have an OAuth app."
            )

    if provider == "custom" and not doc["smtp_host"]:
        raise SenderError("A custom SMTP host is required.")

    return doc


def public_sender(sender: dict[str, Any]) -> dict[str, Any]:
    """Returns a sender without secrets, suitable for the frontend."""
    item = {key: value for key, value in sender.items() if key not in {
        "smtp_password",
        "oauth_client_secret",
        "oauth_refresh_token",
    }}
    item["has_password"] = bool(sender.get("smtp_password"))
    item["has_oauth"] = bool(
        sender.get("oauth_client_id")
        and sender.get("oauth_client_secret")
        and sender.get("oauth_refresh_token")
    )
    item["dns_required"] = True
    return item


def list_senders() -> list[dict[str, Any]]:
    return [public_sender(item) for item in store.list_docs("senders")]


def get_sender(sender_id: str) -> dict[str, Any] | None:
    return store.get_doc("senders", sender_id)


def create_sender(payload: dict[str, Any]) -> dict[str, Any]:
    sender = normalize_sender(payload)
    return store.upsert_doc("senders", sender)


def update_sender(sender_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_sender(sender_id)
    if not existing:
        raise SenderError("Sender account not found.")
    sender = normalize_sender(payload, existing=existing)
    sender["id"] = sender_id
    return store.upsert_doc("senders", sender)


def delete_sender(sender_id: str) -> bool:
    return store.delete_doc("senders", sender_id)


def _oauth_token_uri(sender: dict[str, Any]) -> str:
    return _clean(sender.get("oauth_token_uri")) or _defaults(sender.get("provider", "custom")).get(
        "oauth_token_uri", ""
    )


def _oauth_access_token(sender: dict[str, Any]) -> str:
    client_id = _clean(sender.get("oauth_client_id"))
    # Read-only on purpose (decrypt option b): this module is quarantined legacy
    # code and is not imported by backend/app.py or backend/outreach.py, so it
    # must never write a migrated ciphertext.
    client_secret = credential_crypto.decrypt(sender.get("oauth_client_secret"))
    refresh_token = credential_crypto.decrypt(sender.get("oauth_refresh_token"))
    token_uri = _oauth_token_uri(sender)
    if not (client_id and client_secret and refresh_token and token_uri):
        raise SenderError("OAuth credentials are incomplete for this sender.")
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    scope = _clean(sender.get("oauth_scope"))
    if scope:
        data["scope"] = scope
    try:
        response = httpx.post(token_uri, data=data, timeout=25)
    except httpx.HTTPError as error:
        raise SenderError(f"Could not refresh the OAuth token: {error}") from error
    if response.status_code >= 400:
        raise SenderError(
            "OAuth token refresh failed. Reconnect the account or check the client ID/secret."
        )
    try:
        token = response.json().get("access_token", "")
    except ValueError as error:
        raise SenderError("OAuth token endpoint returned an unreadable response.") from error
    if not token:
        raise SenderError("OAuth token endpoint did not return an access token.")
    return str(token)


def resolve_settings(sender: dict[str, Any]) -> dict[str, Any]:
    """Builds the settings dict expected by ``sender.send_with_settings``."""
    settings = {
        "provider": sender.get("provider", "custom"),
        "host": _clean(sender.get("smtp_host")),
        "port": _int(sender.get("smtp_port"), 587, 1, 65535),
        "security": _clean(sender.get("smtp_security")) or "starttls",
        "user": _clean(sender.get("smtp_username")) or _clean(sender.get("email")),
        # Read-only on purpose (decrypt option b): quarantined legacy path.
        "password": credential_crypto.decrypt(sender.get("smtp_password")),
        "oauth_token": "",
        "reply_to": _clean(sender.get("reply_to")),
    }
    if settings["security"] not in ALLOWED_SECURITY:
        settings["security"] = "starttls"
    if sender.get("auth_type") == "oauth2":
        settings["password"] = ""
        settings["oauth_token"] = _oauth_access_token(sender)
    if not settings["host"] or not settings["user"]:
        raise SenderError("This sender is missing its SMTP host or username.")
    if not settings["oauth_token"] and not settings["password"]:
        raise SenderError("This sender is missing its SMTP password. Edit the account and add it.")
    return settings


def _test_smtp(settings: dict[str, Any]) -> tuple[bool, str]:
    try:
        if settings["security"] == "starttls":
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=20)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=20)
        try:
            if settings.get("oauth_token"):
                auth_string = (
                    f"user={settings['user']}\x01auth=Bearer {settings['oauth_token']}\x01\x01"
                )
                encoded = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")
                code, response = server.docmd("AUTH", "XOAUTH2 " + encoded)
                if code not in (235, 503):
                    return False, f"SMTP OAuth login failed (code {code})."
            else:
                server.login(settings["user"], settings["password"])
            server.noop()
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed. Check the username and app password."
    except smtplib.SMTPException as error:
        return False, f"SMTP error: {error}"
    except OSError as error:
        return False, f"Could not connect to {settings['host']}:{settings['port']}: {error}"


def _test_imap(sender: dict[str, Any], settings: dict[str, Any]) -> tuple[bool, str]:
    host = _clean(sender.get("imap_host"))
    if not host:
        return True, ""
    port = _int(sender.get("imap_port"), 993, 1, 65535)
    security = _clean(sender.get("imap_security")) or "ssl"
    try:
        if security == "starttls":
            connection = imaplib.IMAP4(host, port, timeout=20)
            connection.starttls()
        else:
            connection = imaplib.IMAP4_SSL(host, port, timeout=20)
        try:
            if sender.get("auth_type") == "oauth2":
                token = settings.get("oauth_token") or ""

                def auth_bytes(_challenge: bytes) -> bytes:
                    return (
                        f"user={settings['user']}\x01auth=Bearer {token}\x01\x01"
                    ).encode("utf-8")

                connection.authenticate("XOAUTH2", auth_bytes)
            else:
                connection.login(settings["user"], settings["password"])
        finally:
            try:
                connection.logout()
            except Exception:
                pass
        return True, ""
    except imaplib.IMAP4.error as error:
        return False, f"IMAP login failed: {error}"
    except OSError as error:
        return False, f"Could not connect to IMAP host {host}:{port}: {error}"


def test_sender(
    sender_id: str,
    *,
    send_test_to: str = "",
    check_imap: bool = True,
) -> dict[str, Any]:
    sender = get_sender(sender_id)
    if not sender:
        raise SenderError("Sender account not found.")

    result: dict[str, Any] = {
        "smtp_ok": False,
        "imap_ok": False,
        "smtp_error": "",
        "imap_error": "",
        "test_email_sent": False,
        "message": "",
    }

    try:
        settings = resolve_settings(sender)
    except SenderError as error:
        result["smtp_error"] = str(error)
        result["message"] = str(error)
    else:
        smtp_ok, smtp_error = _test_smtp(settings)
        result["smtp_ok"] = smtp_ok
        result["smtp_error"] = smtp_error
        if check_imap:
            imap_ok, imap_error = _test_imap(sender, settings)
            result["imap_ok"] = imap_ok
            result["imap_error"] = imap_error

        if smtp_ok and send_test_to:
            if not is_valid_format(send_test_to):
                result["smtp_error"] = "The test recipient email address is invalid."
                result["smtp_ok"] = False
            else:
                ok, error = send_with_settings(
                    settings,
                    send_test_to.strip(),
                    "Email automation connection test",
                    "<p>Your sending account is connected successfully.</p>",
                    "Your sending account is connected successfully.",
                    sender_name=sender.get("from_name") or sender.get("label"),
                )
                result["test_email_sent"] = ok
                if not ok:
                    result["smtp_ok"] = False
                    result["smtp_error"] = error

    overall_ok = bool(result["smtp_ok"] and (result["imap_ok"] or not check_imap))
    sender["last_test_at"] = store.now_iso()
    sender["last_test_ok"] = overall_ok
    sender["last_test_error"] = (result["smtp_error"] or result["imap_error"] or "")[:500]
    store.upsert_doc("senders", sender)

    if overall_ok:
        result["message"] = (
            "Connection successful and test email sent."
            if result["test_email_sent"]
            else "Connection successful."
        )
    else:
        result["message"] = result["smtp_error"] or result["imap_error"] or "Connection failed."
    return result


def sender_dns(sender_id: str, *, refresh: bool = False) -> dict[str, Any]:
    sender = get_sender(sender_id)
    if not sender:
        raise SenderError("Sender account not found.")
    domain = sender.get("domain") or _domain_from_email(sender.get("email", ""))
    if not domain:
        raise SenderError("This sender has no domain to check.")
    result = deliverability.check_domain(
        domain,
        sender.get("provider"),
        refresh=refresh,
        dkim_selector=sender.get("dkim_selector"),
    )
    result["sender"] = public_sender(sender)
    result["recommended"] = deliverability.recommended_records(
        domain,
        sender.get("provider"),
        dkim_selector=sender.get("dkim_selector"),
        spf_include=sender.get("spf_include"),
        dmarc_policy=sender.get("dmarc_policy"),
    )
    return result


def sender_by_id_or_error(sender_id: str) -> dict[str, Any]:
    sender = get_sender(sender_id)
    if not sender:
        raise SenderError("Sender account not found.")
    return sender


__all__ = [
    "ALLOWED_AUTH_TYPES",
    "ALLOWED_PROVIDERS",
    "PROVIDER_CATALOG",
    "SenderError",
    "create_sender",
    "delete_sender",
    "get_sender",
    "list_senders",
    "normalize_sender",
    "provider_catalog",
    "public_sender",
    "resolve_settings",
    "sender_by_id_or_error",
    "sender_dns",
    "test_sender",
    "update_sender",
]
