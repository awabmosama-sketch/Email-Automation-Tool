"""Symmetric encryption for stored SMTP passwords.

Credentials live in the campaign store. They are encrypted with Fernet, using
a key derived from ``SESSION_SECRET``. Without the optional ``cryptography``
package the value is still obfuscated (base64) and the API never returns it,
but operators should install ``cryptography`` for real encryption.
"""

import base64
import hashlib
import logging

from . import config

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"
_PLAIN_PREFIX = "plain:v1:"


def _key() -> bytes:
    secret = (config.SESSION_SECRET or config.APP_PASSWORD).encode("utf-8")
    digest = hashlib.sha256(b"email-automation:smtp-credentials:" + secret).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    try:
        from cryptography.fernet import Fernet

        token = Fernet(_key()).encrypt(value.encode("utf-8")).decode("ascii")
        return _PREFIX + token
    except ImportError:
        logger.warning(
            "cryptography is not installed; SMTP passwords are stored obfuscated, not encrypted."
        )
        return _PLAIN_PREFIX + base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    value = value or ""
    if value.startswith(_PREFIX):
        from cryptography.fernet import Fernet, InvalidToken

        try:
            return Fernet(_key()).decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as error:
            raise ValueError(
                "The stored SMTP password could not be decrypted. "
                "Re-enter it on the sender account."
            ) from error
    if value.startswith(_PLAIN_PREFIX):
        return base64.urlsafe_b64decode(value[len(_PLAIN_PREFIX):].encode("ascii")).decode("utf-8")
    # Legacy/plain value from a hand-edited store.
    return value


def is_encrypted(value: str) -> bool:
    return (value or "").startswith((_PREFIX, _PLAIN_PREFIX))
