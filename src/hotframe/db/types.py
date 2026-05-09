"""Custom SQLAlchemy TypeDecorators for the hub.

Currently provides:
- EncryptedString: transparently encrypts/decrypts string values using
  app.core.security.crypto (Fernet). Store VARCHAR ciphertext in DB,
  read plaintext in Python.
- EncryptedText: same as EncryptedString but backed by TEXT for large payloads
  (PEM certificates, .pfx base64, etc.).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, Text, TypeDecorator

from hotframe.auth.crypto import decrypt_secret, encrypt_secret


class EncryptedString(TypeDecorator):
    """VARCHAR column that stores Fernet-encrypted strings.

    The Python value is plaintext; the DB stores ciphertext. Length refers to
    the ciphertext capacity, which is always larger than plaintext (base64
    overhead). As a rule of thumb, pass length >= 4 * plaintext_max_length.
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int = 512, **kwargs: Any) -> None:
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_secret(value)

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt_secret(value)


class EncryptedText(TypeDecorator):
    """TEXT column that stores Fernet-encrypted strings (for large payloads)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_secret(value)

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt_secret(value)
