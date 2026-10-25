import base64
import hashlib
import hmac
from urllib.parse import quote

from . import config


def _sign(payload: str) -> str:
    return hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def create_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(email.strip().lower().encode("utf-8")).decode("ascii").rstrip("=")
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> str | None:
    payload, separator, signature = (token or "").partition(".")
    if not separator or not payload or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        email = base64.urlsafe_b64decode(padded).decode("utf-8").strip().lower()
    except (ValueError, UnicodeDecodeError):
        return None
    if "@" not in email or len(email) > 320:
        return None
    return email


def build_unsubscribe_url(email: str, base_url: str | None = None) -> str:
    base = (base_url or config.APP_BASE_URL or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/unsubscribe?token={quote(create_token(email), safe='')}"
