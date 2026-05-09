"""Symmetric encryption for secrets at rest (certificates, API tokens, credentials).

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.
Key is read from HUB_SECRETS_KEY env var (32 url-safe base64 bytes).

In development, if HUB_SECRETS_KEY is unset, a deterministic key is derived
from HUB_SECRET_KEY to keep dev experience smooth. In production the absence
of HUB_SECRETS_KEY raises an error at startup (enforced by settings validator).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class SecretsKeyMissingError(RuntimeError):
    """Raised in production when HUB_SECRETS_KEY is not configured."""


class SecretDecryptionError(RuntimeError):
    """Raised when a ciphertext cannot be decrypted (wrong key or tampered)."""


def _derive_dev_key(seed: str) -> bytes:
    """Derive a stable 32-byte Fernet key from a seed string for dev only."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Build the Fernet instance from env. Cached for process lifetime."""
    key = os.getenv("HUB_SECRETS_KEY")
    if key:
        try:
            return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except ValueError as exc:
            raise SecretsKeyMissingError(
                "HUB_SECRETS_KEY is set but is not a valid 32-byte url-safe base64 key"
            ) from exc

    deployment_mode = os.getenv("HUB_DEPLOYMENT_MODE", "local").lower()
    if deployment_mode != "local":
        raise SecretsKeyMissingError(
            "HUB_SECRETS_KEY is required in non-local deployments. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )

    seed = os.getenv("HUB_SECRET_KEY", "dev-fallback-insecure")
    logger.warning(
        "HUB_SECRETS_KEY not set — deriving dev key from HUB_SECRET_KEY. "
        "Do NOT use this configuration in production."
    )
    return Fernet(_derive_dev_key(seed))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a string secret, returning ciphertext as url-safe base64 string.

    Empty strings are returned unchanged so that optional fields remain empty
    rather than storing an encrypted empty value (which would otherwise bloat
    the database and leak schema size).
    """
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a ciphertext produced by ``encrypt_secret``. Empty → empty."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Failed to decrypt secret — wrong HUB_SECRETS_KEY or tampered ciphertext"
        ) from exc


def generate_key() -> str:
    """Generate a fresh Fernet key. Used by setup/bootstrap tooling."""
    return Fernet.generate_key().decode("utf-8")


def reset_cache() -> None:
    """Reset the cached Fernet instance. Used by tests when env changes mid-run."""
    _get_fernet.cache_clear()
