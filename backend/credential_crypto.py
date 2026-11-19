"""Encryption helpers for sender credentials stored by the campaign console.

SMTP passwords and OAuth refresh tokens must never be stored in plain text.
Values are encrypted with Fernet (AES-128-CBC + HMAC) using a key derived from
``SENDER_CREDENTIAL_KEY`` (or ``SESSION_SECRET`` when that is unset).

Key rotation: a deployment that used to rely on the built-in fallback secret (or
on an older ``SESSION_SECRET``) lists the old material in
``SENDER_CREDENTIAL_KEY_LEGACY``. Reads then decrypt with a legacy key and hand
back a freshly encrypted value so the caller can persist it, after which the
legacy key is no longer needed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterable

from . import config

logger = logging.getLogger(__name__)

_PREFIX = "enc:"
_LEGACY_PREFIX = "b64:"

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]
    logger.warning("cryptography is not installed; sender credentials use weak encoding.")


class CredentialDecryptError(RuntimeError):
    """A stored credential cannot be decrypted with any configured key."""

    def __init__(self, sender_id: str) -> None:
        self.sender_id = sender_id or "unknown"
        super().__init__(
            f"Stored credential for sender '{self.sender_id}' cannot be decrypted with the "
            "current key or any key in SENDER_CREDENTIAL_KEY_LEGACY. Re-enter that sender's "
            "password, or restore the previous SESSION_SECRET / set the old value in "
            "SENDER_CREDENTIAL_KEY_LEGACY, then retry."
        )


def current_material() -> str:
    """The secret the current key is derived from (never log this)."""
    return (
        config.SENDER_CREDENTIAL_KEY
        or config.SESSION_SECRET
        or config.APP_PASSWORD
        or "email-automation-credentials"
    )


def legacy_materials() -> list[str]:
    """Secrets from SENDER_CREDENTIAL_KEY_LEGACY, deduped and excluding the current one."""
    raw = getattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", "") or ""
    seen = {current_material()}
    materials: list[str] = []
    for item in raw.split(","):
        material = item.strip()
        if material and material not in seen:
            seen.add(material)
            materials.append(material)
    return materials


# sha256 of secrets that are public and must never be used to decrypt stored
# credentials. The values themselves stay out of the tree: the pre-hardening
# built-in fallback is public in git history, so a deployment that points
# SENDER_CREDENTIAL_KEY_LEGACY at it is using a known key, not recovering one.
_PUBLIC_LEGACY_FINGERPRINTS = {
    "29029fdab939e36fdd52559ef34ba22e7e355f617c6ba86b768c0cd851759f66": "built-in fallback",
}


def legacy_key_is_public() -> bool:
    """True when SENDER_CREDENTIAL_KEY_LEGACY includes a publicly known secret."""
    digests = {
        hashlib.sha256(material.encode("utf-8")).hexdigest()
        for material in legacy_materials()
    }
    return bool(digests & set(_PUBLIC_LEGACY_FINGERPRINTS))


def _fernet_for(material: str) -> "Fernet | None":
    if Fernet is None:
        return None
    digest = hashlib.sha256(("sender-credentials:" + material).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _decrypt_with(value: str, material: str) -> str:
    cipher = _fernet_for(material)
    if cipher is None:
        raise InvalidToken("cryptography is unavailable")
    return cipher.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")


def _try_materials(
    value: str, materials: Iterable[str]
) -> tuple[str | None, str | None]:
    """Returns (plaintext, material_used), or (None, None) when nothing works."""
    if Fernet is None:
        return None, None
    for material in materials:
        try:
            return _decrypt_with(value, material), material
        except (InvalidToken, ValueError, TypeError):
            continue
    return None, None


def _decrypt_encrypted(value: str, sender_id: str) -> tuple[str, str]:
    plaintext, material = _try_materials(
        value, [current_material(), *legacy_materials()]
    )
    if plaintext is None or material is None:
        raise CredentialDecryptError(sender_id)
    return plaintext, material


def encrypt(value: str | None) -> str:
    """Encrypts a secret. Empty values stay empty so "unchanged" is detectable."""
    value = (value or "").strip()
    if not value:
        return ""
    cipher = _fernet_for(current_material())
    if cipher is None:
        return _LEGACY_PREFIX + base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return _PREFIX + cipher.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None, sender_id: str = "unknown") -> str:
    """Decrypts a stored secret; raises CredentialDecryptError when no key works."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith(_PREFIX):
        return _decrypt_encrypted(value, sender_id)[0]
    if value.startswith(_LEGACY_PREFIX):
        try:
            return base64.urlsafe_b64decode(value[len(_LEGACY_PREFIX):]).decode("utf-8")
        except (ValueError, TypeError) as error:
            raise CredentialDecryptError(sender_id) from error
    # Rows written before encryption existed; nothing to migrate.
    return value


def decrypt_and_migrate(
    value: str | None, sender_id: str = "unknown"
) -> tuple[str, str | None]:
    """Decrypts, and re-encrypts under the current key if a legacy key was used.

    Returns ``(plaintext, new_ciphertext_or_None)``. The caller persists the new
    ciphertext so subsequent requests need only the current key.
    """
    value = (value or "").strip()
    if not value.startswith(_PREFIX):
        return decrypt(value, sender_id), None
    plaintext, material = _decrypt_encrypted(value, sender_id)
    if material == current_material():
        return plaintext, None
    logger.info("Re-encrypting stored credential for sender %s under the current key.", sender_id)
    return plaintext, encrypt(plaintext)


def legacy_encrypted_ids(items: Iterable[tuple[str, str | None]]) -> list[str]:
    """Sender ids whose credential only decrypts with a legacy key."""
    if not legacy_materials():
        return []
    affected: list[str] = []
    for sender_id, value in items:
        value = (value or "").strip()
        if not value.startswith(_PREFIX):
            continue
        if _try_materials(value, [current_material()])[0] is not None:
            continue
        if _try_materials(value, legacy_materials())[0] is not None:
            affected.append(sender_id or "unknown")
    return affected


def undecryptable_ids(items: Iterable[tuple[str, str | None]]) -> list[str]:
    """Sender ids whose credential decrypts with no configured key at all."""
    affected: list[str] = []
    for sender_id, value in items:
        value = (value or "").strip()
        if not value.startswith(_PREFIX):
            continue
        if _try_materials(value, [current_material(), *legacy_materials()])[0] is None:
            affected.append(sender_id or "unknown")
    return affected


def is_encrypted(value: str | None) -> bool:
    return bool(value) and (value or "").startswith((_PREFIX, _LEGACY_PREFIX))
