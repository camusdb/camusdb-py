"""The asyncio HTTP transport. It mirrors :mod:`camusdb._transport`."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .. import _protocol as proto
from .. import codes
from .._auth import AuthState, Token, busy_login_retry_delay
from .._config import Config
from .._endpoints import EndpointPool
from .._transport import LineSplitter, client_options, translate_transport_error
from ..errors import Error


class AsyncTransport:
    def __init__(self, config: Config) -> None:
        self.pool = EndpointPool.for_endpoints(config.endpoints)
        self.auth = AuthState(config)
        self._client = httpx.AsyncClient(**client_options(config))
        # One login at a time for this connection. Other connections with the same credentials share
        # the token through the process-wide store.
        self._login_lock = asyncio.Lock()
        # The background renewals in flight. The loop keeps only a weak reference to a task.
        self._renewals: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        renewals = list(self._renewals)
        for task in renewals:
            task.cancel()
        if renewals:
            await asyncio.gather(*renewals, return_exceptions=True)
        await self._client.aclose()

    def next_endpoint(self) -> str:
        return self.pool.next()

    async def post(
        self,
        path: str,
        body: Any,
        *,
        endpoint: str | None = None,
        fallback: str = "request failed",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "POST", path, body, endpoint=endpoint, fallback=fallback, timeout=timeout
        )

    async def get(
        self,
        path: str,
        *,
        endpoint: str | None = None,
        fallback: str = "request failed",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "GET", path, None, endpoint=endpoint, fallback=fallback, timeout=timeout
        )

    async def _call(
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
            token = await self._token(target)
            response = await self._send(method, target, path, body, token, timeout)
            try:
                return proto.parse_response(response.status_code, response.content, fallback)
            except Error as exc:
                if attempt == 0 and self._renewable(exc, token):
                    self.auth.discard(token)
                    continue
                raise
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def stream_lines(
        self, path: str, body: Any, *, endpoint: str
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        for attempt in (0, 1):
            token = await self._token(endpoint)
            headers = {"Accept": proto.NDJSON, **_bearer(token)}
            try:
                async with self._client.stream(
                    "POST", endpoint + path, json=body, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        content = await response.aread()
                        try:
                            proto.parse_response(
                                response.status_code, content, "streaming query failed"
                            )
                        except Error as exc:
                            if attempt == 0 and self._renewable(exc, token):
                                self.auth.discard(token)
                                continue
                            raise
                    yield _lines(response.aiter_bytes())
                    return
            except httpx.HTTPError as exc:
                raise translate_transport_error(self.pool, endpoint, exc) from exc

    async def _send(
        self,
        method: str,
        endpoint: str,
        path: str,
        body: Any,
        token: str | None,
        timeout: float | None,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                endpoint + path,
                json=body,
                headers=_bearer(token),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            raise translate_transport_error(self.pool, endpoint, exc) from exc

    def _renewable(self, exc: Error, token: str | None) -> bool:
        return (
            exc.code == codes.AUTHENTICATION_FAILED
            and token is not None
            and self.auth.can_login
            and self.auth.access_token is None
        )

    async def _token(self, endpoint: str) -> str | None:
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
        async with self._login_lock:
            cached = auth.cached()
            if cached is not None and cached.usable:
                return cached.value
            return await self.login(auth.user or "", auth.password, endpoint=endpoint)

    def _start_renewal(self, stale: Token) -> None:
        """Start one login in a background task to replace ``stale``, which is still sent."""
        key = self.auth.begin_renewal()
        if key is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._renew(key, stale))
        except BaseException:
            self.auth.end_renewal(key, failed=True)
            raise
        self._renewals.add(task)
        task.add_done_callback(self._renewals.discard)

    async def _renew(self, key: tuple[object, ...], stale: Token) -> None:
        failed = False
        try:
            auth = self.auth
            async with self._login_lock:
                current = auth.cached()
                if current is None or current.value != stale.value or not auth.can_login:
                    return
                await self.login(auth.user or "", auth.password)
        except Exception:
            failed = True
        finally:
            self.auth.end_renewal(key, failed)

    async def login(self, user: str, password: str, *, endpoint: str | None = None) -> str:
        target = endpoint or self.pool.next()
        attempt = 0
        while True:
            issued = time.monotonic()
            response = await self._send(
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
                await asyncio.sleep(delay)
        value, expires_in = proto.parse_login(body)
        self.auth.store(value, expires_in, issued)
        return value

    async def logout(self) -> None:
        token = self.auth.current_token
        if token is None:
            return
        target = self.pool.next()
        try:
            response = await self._send("POST", target, proto.LOGOUT, None, token, None)
            proto.parse_response(response.status_code, response.content, "logout failed")
        finally:
            self.auth.discard(token)


def _bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    splitter = LineSplitter()
    async for chunk in chunks:
        for line in splitter.feed(chunk):
            yield line
    for line in splitter.finish():
        yield line
