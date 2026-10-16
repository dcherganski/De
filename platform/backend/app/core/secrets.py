"""Exchange credentials at rest and secret redaction in logs.

Credentials are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using SECRETS_MASTER_KEY, which
lives only in the server environment. Nothing decrypted is ever returned by the API.
"""

from __future__ import annotations

import json
import logging
import re

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings


class SecretsUnavailable(RuntimeError):
    pass


def _fernet(settings: Settings) -> Fernet:
    if settings.secrets_master_key is None or not settings.secrets_master_key.get_secret_value():
        raise SecretsUnavailable("SECRETS_MASTER_KEY is not set; credentials cannot be stored")
    return Fernet(settings.secrets_master_key.get_secret_value().encode())


def encrypt_credentials(settings: Settings, payload: dict[str, str]) -> bytes:
    return _fernet(settings).encrypt(json.dumps(payload).encode())


def decrypt_credentials(settings: Settings, blob: bytes) -> dict[str, str]:
    try:
        return json.loads(_fernet(settings).decrypt(blob))
    except InvalidToken as exc:  # wrong key or tampered row
        raise SecretsUnavailable("credentials cannot be decrypted with the configured key") from exc


def mask(value: str, keep: int = 4) -> str:
    return "…" + value[-keep:] if len(value) > keep else "…"


_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(\"?(?:api[_-]?key|secret|private[_-]?key|token|password|x-api-key|x-signature)\"?\s*[:=]\s*\"?)[^\s\"',}]+"),
]


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Attach to every handler so a stray key in an exception message never reaches the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.getMessage()))
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
