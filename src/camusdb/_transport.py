"""The sync HTTP transport."""

from __future__ import annotations

import importlib.util
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from . import _protocol as proto
from . import codes
from ._auth import AuthState, Token, busy_login_retry_delay
from ._config import Config
from ._endpoints import EndpointPool
from .errors import Error, InterfaceError, OperationalError

USER_AGENT = "camusdb-python"


def client_options(config: Config) -> dict[str, Any]:
    if config.http2 and importlib.util.find_spec("h2") is None:
        raise InterfaceError("HTTP/2 needs the h2 package: pip install 'camusdb-py[http2]'")
    return {
        "http2": config.http2,
        "timeout": config.timeout,
        "headers": {"User-Agent": USER_AGENT, "Accept": "application/json"},
        # Credentials must not follow a redirect to another host.
        "follow_redirects": False,
    }


def translate_transport_error(pool: EndpointPool, endpoint: str, exc: httpx.HTTPError) -> Error:
    """Map an httpx failure to a driver error, and pause an endpoint that did not answer."""
    if isinstance(exc, httpx.TimeoutException) and not isinstance(exc, httpx.ConnectTimeout):
        # The server may still be working on the request, so the endpoint is not marked.
        return OperationalError(f"the request to {endpoint} timed out", codes.UNKNOWN)
    if isinstance(exc, httpx.TransportError):
        pool.mark_unreachable(endpoint)
        return proto.unreachable(endpoint, exc)
    return OperationalError(proto.sanitize(exc), codes.UNKNOWN)


class Transport:
    """Sends REST requests for one connection.

    Each method is one attempt, except for one replay after the server rejects a token that the
    driver can renew. A rejected request never reached execution, so the replay is safe.
    """

    def __init__(self, config: Config) -> None:
        self.pool = EndpointPool.for_endpoints(config.endpoints)
        self.auth = AuthState(config)
        self._client = httpx.Client(**client_options(config))

    def close(self) -> None:
        self._client.close()

    def next_endpoint(self) -> str:
        return self.pool.next()

    # --- Requests ---------------------------------------------------------------------------------

    def post(
        self,
        path: str,
        body: Any,
        *,
        endpoint: str | None = None,
        fallback: str = "request failed",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call("POST", path, body, endpoint=endpoint, fallback=fallback, timeout=timeout)

    def get(
        self,
        path: str,
        *,
        endpoint: str | None = None,
        fallback: str = "request failed",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call("GET", path, None, endpoint=endpoint, fallback=fallback, timeout=timeout)

    def _call(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        endpoint: str | None,
        fallback: str,
        timeout: float | None,
    ) -> dict[str, Any]:
        target = endpoint or self.pool.next()
        for attempt in (0, 1):
            token = self._token(target)
            response = self._send(method, target, path, body, token, timeout)
            try:
                return proto.parse_response(response.status_code, response.content, fallback)
            except Error as exc:
                if attempt == 0 and self._renewable(exc, token):
                    self.auth.discard(token)
                    continue
                raise
        raise AssertionError("unreachable")

    @contextmanager
    def stream_lines(self, path: str, body: Any, *, endpoint: str) -> Iterator[Iterator[bytes]]:
        """Post ``body`` and give the non-empty lines of the response as they arrive."""
        for attempt in (0, 1):
            token = self._token(endpoint)
            headers = {"Accept": proto.NDJSON, **_bearer(token)}
            try:
                with self._client.stream(
                    "POST", endpoint + path, json=body, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        content = response.read()
                        try:
                            proto.parse_response(
                                response.status_code, content, "streaming query failed"
                            )
                        except Error as exc:
                            if attempt == 0 and self._renewable(exc, token):
                                self.auth.discard(token)
                                continue
                            raise
                    yield (line for line in _lines(response.iter_bytes()) if line)
                    return
            except httpx.HTTPError as exc:
                raise translate_transport_error(self.pool, endpoint, exc) from exc

    def _send(
        self,
        method: str,
        endpoint: str,
        path: str,
        body: Any,
        token: str | None,
        timeout: float | None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                endpoint + path,
                json=body,
                headers=_bearer(token),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            raise translate_transport_error(self.pool, endpoint, exc) from exc

    # --- Authentication ---------------------------------------------------------------------------

    def _renewable(self, exc: Error, token: str | None) -> bool:
        return (
            exc.code == codes.AUTHENTICATION_FAILED
            and token is not None
            and self.auth.can_login
            and self.auth.access_token is None
        )

    def _token(self, endpoint: str) -> str | None:
        auth = self.auth
        if auth.access_token is not None:
            return auth.access_token
        if not auth.can_login:
            return None
        cached = auth.cached()
        if cached is not None and cached.usable:
            if not cached.fresh:
                self._start_renewal(cached)
            return cached.value
        with auth.login_lock():
            # Another thread may have logged in while this one waited.
            cached = auth.cached()
            if cached is not None and cached.usable:
                return cached.value
            return self.login(auth.user or "", auth.password, endpoint=endpoint)

    def _start_renewal(self, stale: Token) -> None:
        """Start one login in a background thread to replace ``stale``, which is still sent."""
        key = self.auth.begin_renewal()
        if key is None:
            return
        thread = threading.Thread(
            target=self._renew, args=(key, stale), name="camusdb-token-renewal", daemon=True
        )
        try:
            thread.start()
        except BaseException:
            self.auth.end_renewal(key, failed=True)
            raise

    def _renew(self, key: tuple[object, ...], stale: Token) -> None:
        failed = False
        try:
            auth = self.auth
            with auth.login_lock():
                # A login, a logout or a rejected token while this thread waited already decided
                # what the next token is.
                current = auth.cached()
                if current is None or current.value != stale.value or not auth.can_login:
                    return
                self.login(auth.user or "", auth.password)
        except Exception:
            # The old token is still valid, so nothing failed yet: statements continue to send it.
            # If it expires first, the next statement logs in itself and gets the error.
            failed = True
        finally:
            self.auth.end_renewal(key, failed)

    def login(self, user: str, password: str, *, endpoint: str | None = None) -> str:
        target = endpoint or self.pool.next()
        attempt = 0
        while True:
            issued = time.monotonic()
            response = self._send(
                "POST", target, proto.LOGIN, {"user": user, "password": password}, None, None
            )
            try:
                body = proto.parse_response(
                    response.status_code, response.content, "authentication failed"
                )
                break
            except Error as exc:
                delay = busy_login_retry_delay(exc, attempt)
                if delay is None:
                    raise
                attempt += 1
                time.sleep(delay)
        value, expires_in = proto.parse_login(body)
        self.auth.store(value, expires_in, issued)
        return value

    def logout(self) -> None:
        token = self.auth.current_token
        if token is None:
            return
        target = self.pool.next()
        try:
            response = self._send("POST", target, proto.LOGOUT, None, token, None)
            proto.parse_response(response.status_code, response.content, "logout failed")
        finally:
            self.auth.discard(token)


def _bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _lines(chunks: Iterator[bytes]) -> Iterator[bytes]:
    # Lines are split on b"\n" only. A JSON text has no raw newline, so this is exact, and it is
    # faster than a text decode of each chunk.
    pending = b""
    for chunk in chunks:
        pending += chunk
        *complete, pending = pending.split(b"\n")
        yield from (line.strip() for line in complete)
    if pending.strip():
        yield pending.strip()
