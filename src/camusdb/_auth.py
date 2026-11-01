"""Bearer tokens: one login for each set of credentials, shared by the whole process.

The server verifies a password slowly on purpose (PBKDF2) and limits logins to 20 for each account
each minute. Thus the token is cached for each (deployment, user, password), and every connection
with the same credentials uses it.

Renewal: at 80% of its lifetime the token is still sent, and one login in the background replaces
it, so a statement does not wait for a login that it does not need yet. A statement waits for a
login only when there is no token, or when the token reached 95% of its lifetime before a renewal
succeeded. A token that reaches 95% is never sent, because the server can expire it while the
request travels. After a background renewal fails, the next one starts 5 seconds later at the
earliest.

Busy server: the server refuses a login with CADB0518 when all its key-derivation slots are in use.
The driver sends that login again, up to three times, after a short random wait.
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass

from . import codes
from ._config import Config
from .errors import Error

RENEW_AT = 0.80
EXPIRE_AT = 0.95

# The minimum time between a failed background renewal and the next one, in seconds. The token is
# still valid, so the wait costs nothing. Without the wait, each statement after a failed renewal
# starts a new login, against a server that is down or that limits the logins of this account.
RENEWAL_RETRY_DELAY = 5.0

# The waits, in seconds, before each new attempt of a login that the server refused as busy
# (CADB0518). A refused login runs no statement, so a new attempt is always safe. The list is short,
# because the same code also reports the login rate limit of the account, and a few seconds do not
# clear that limit. Each wait is random, between 0.5 and 1.5 times its value, so many clients that the
# server refused together do not come back together.
BUSY_LOGIN_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.75, 1.5)


def busy_login_retry_delay(exc: Error, attempt: int) -> float | None:
    """The wait before the next attempt of a refused login, or None when no attempt is left."""
    delays = BUSY_LOGIN_RETRY_DELAYS
    if exc.code != codes.TOO_MANY_AUTH_ATTEMPTS or attempt >= len(delays):
        return None
    delay = delays[attempt]
    return delay * (0.5 + random.random()) if delay > 0 else 0.0


@dataclass(frozen=True)
class Token:
    value: str
    # time.monotonic() before the login request. The server starts the lifetime when it issues the
    # token, so the count starts early, and the time of the login counts against the token.
    issued: float
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
        # The keys that have a background renewal in flight: at most one for each key.
        self._renewing: set[tuple[object, ...]] = set()
        # For each key, the time.monotonic() before which no background renewal starts.
        self._renewal_retry_after: dict[tuple[object, ...], float] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._renewing.clear()
            self._renewal_retry_after.clear()

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

    def begin_renewal(self, key: tuple[object, ...]) -> bool:
        """Claim the background renewal of ``key``. False when one is in flight, or must wait."""
        with self._lock:
            if key in self._renewing:
                return False
            if time.monotonic() < self._renewal_retry_after.get(key, 0.0):
                return False
            self._renewing.add(key)
            return True

    def end_renewal(self, key: tuple[object, ...], failed: bool) -> None:
        with self._lock:
            self._renewing.discard(key)
            if failed:
                self._renewal_retry_after[key] = time.monotonic() + RENEWAL_RETRY_DELAY
            else:
                self._renewal_retry_after.pop(key, None)


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

    def store(self, value: str, expires_in: float | None, issued: float | None = None) -> Token:
        """Cache a token. ``issued`` is the time.monotonic() before the login request."""
        lifetime = expires_in if expires_in and expires_in > 0 else self.default_lifetime
        token = Token(value, time.monotonic() if issued is None else issued, lifetime)
        TOKENS.put(self._key, token)
        return token

    def discard(self, value: str | None = None) -> None:
        TOKENS.discard(self._key, value)

    def login_lock(self) -> threading.Lock:
        return TOKENS.login_lock(self._key)

    def begin_renewal(self) -> tuple[object, ...] | None:
        """Claim the background renewal. The key to give to :meth:`end_renewal`, or None."""
        key = self._key
        return key if TOKENS.begin_renewal(key) else None

    @staticmethod
    def end_renewal(key: tuple[object, ...], failed: bool) -> None:
        TOKENS.end_renewal(key, failed)

    @property
    def current_token(self) -> str | None:
        if self.access_token is not None:
            return self.access_token
        token = self.cached()
        return token.value if token is not None and token.usable else None
