"""Password hashing (argon2id), session tokens and a small login throttle.

Session tokens are random 256-bit values. Only their SHA-256 is stored, so a database leak does not
yield usable sessions. The cookie is HttpOnly and SameSite=Strict (Secure behind HTTPS), and every
state-changing request must also carry the `X-Requested-With: platform` header (CSRF defence).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_ph = PasswordHasher()
COOKIE = "tp_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "platform"
# Checked when the username does not exist, so a wrong name and a wrong password take the same time.
DUMMY_HASH = _ph.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _ph.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _ph.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginThrottle:
    """At most `limit` failed attempts per key within `window_s`."""

    def __init__(self, limit: int = 5, window_s: int = 300):
        self.limit = limit
        self.window_s = window_s
        self._fails: dict[str, deque] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        q = self._fails[key]
        now = time.monotonic()
        while q and now - q[0] > self.window_s:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, key: str) -> None:
        self._fails[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
