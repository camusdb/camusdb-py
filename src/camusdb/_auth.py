"""Bearer tokens: one login for each set of credentials, shared by the whole process.

The server verifies a password slowly on purpose (PBKDF2) and limits logins to 20 for each account
each minute. Thus the token is cached for each (deployment, user, password), and every connection
with the same credentials uses it. A token is renewed when it reaches 80% of its lifetime. A token
that reaches 95% is never sent, because the server can expire it while the request travels.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from ._config import Config

RENEW_AT = 0.80
EXPIRE_AT = 0.95


@dataclass(frozen=True)
class Token:
    value: str
    issued: float  # time.monotonic() when the login reply arrived
    lifetime: float  # seconds

    def age_fraction(self) -> float:
        return (time.monotonic() - self.issued) / self.lifetime

    @property
    def fresh(self) -> bool:
        """The token is young enough that it needs no renewal."""
        return self.age_fraction() < RENEW_AT

    @property
    def usable(self) -> bool:
        """The token can still be sent, while a renewal is not available."""
        return self.age_fraction() < EXPIRE_AT


class _TokenStore:
    def __init__(self) -> None:
        self._tokens: dict[tuple[object, ...], Token] = {}
        self._login_locks: dict[tuple[object, ...], threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[object, ...]) -> Token | None:
        with self._lock:
            return self._tokens.get(key)

    def put(self, key: tuple[object, ...], token: Token) -> None:
        with self._lock:
            self._tokens[key] = token

    def discard(self, key: tuple[object, ...], value: str | None = None) -> None:
        """Forget the token, but only if it is still ``value``: a newer token stays."""
        with self._lock:
            current = self._tokens.get(key)
            if current is not None and (value is None or current.value == value):
                del self._tokens[key]

    def login_lock(self, key: tuple[object, ...]) -> threading.Lock:
        with self._lock:
            return self._login_locks.setdefault(key, threading.Lock())


TOKENS = _TokenStore()


class AuthState:
    """The credentials of one connection, and access to the shared token for them."""

    def __init__(self, config: Config) -> None:
        self._endpoints = config.endpoints
        self.default_lifetime = config.token_lifetime
        self.access_token = config.access_token
        self.user: str | None = None
        self._password: str | None = None
        self._key: tuple[object, ...] = ()
        self.set_credentials(config.user, config.password)

    def set_credentials(self, user: str | None, password: str | None) -> None:
        self.user = user
        self._password = password
        digest = hashlib.sha256(password.encode()).hexdigest() if password is not None else None
        self._key = (self._endpoints, user, digest)

    @property
    def can_login(self) -> bool:
        return self.user is not None and self._password is not None

    @property
    def password(self) -> str:
        assert self._password is not None
        return self._password

    @property
    def enabled(self) -> bool:
        return self.access_token is not None or self.can_login

    def cached(self) -> Token | None:
        return TOKENS.get(self._key) if self.can_login else None

    def store(self, value: str, expires_in: float | None) -> Token:
        lifetime = expires_in if expires_in and expires_in > 0 else self.default_lifetime
        token = Token(value, time.monotonic(), lifetime)
        TOKENS.put(self._key, token)
        return token

    def discard(self, value: str | None = None) -> None:
        TOKENS.discard(self._key, value)

    def login_lock(self) -> threading.Lock:
        return TOKENS.login_lock(self._key)

    @property
    def current_token(self) -> str | None:
        if self.access_token is not None:
            return self.access_token
        token = self.cached()
        return token.value if token is not None and token.usable else None
