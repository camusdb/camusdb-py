"""The asyncio HTTP transport. It mirrors :mod:`camusdb._transport`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .. import _protocol as proto
from .. import codes
from .._auth import AuthState
from .._config import Config
from .._endpoints import EndpointPool
from .._transport import client_options, translate_transport_error
from ..errors import Error


class AsyncTransport:
    def __init__(self, config: Config) -> None:
        self.pool = EndpointPool.for_endpoints(config.endpoints)
        self.auth = AuthState(config)
        self._client = httpx.AsyncClient(**client_options(config))
        # One login at a time for this connection. Other connections with the same credentials share
        # the token through the process-wide store.
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
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
        self, path: str, *, endpoint: str | None = None, fallback: str = "request failed"
    ) -> dict[str, Any]:
        return await self._call(
            "GET", path, None, endpoint=endpoint, fallback=fallback, timeout=None
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
        if cached is not None and cached.fresh:
            return cached.value
        async with self._login_lock:
            cached = auth.cached()
            if cached is not None and cached.fresh:
                return cached.value
            try:
                return await self.login(auth.user or "", auth.password, endpoint=endpoint)
            except Error:
                if cached is not None and cached.usable:
                    return cached.value
                raise

    async def login(self, user: str, password: str, *, endpoint: str | None = None) -> str:
        target = endpoint or self.pool.next()
        response = await self._send(
            "POST", target, proto.LOGIN, {"user": user, "password": password}, None, None
        )
        body = proto.parse_response(response.status_code, response.content, "authentication failed")
        value, expires_in = proto.parse_login(body)
        self.auth.store(value, expires_in)
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
    pending = b""
    async for chunk in chunks:
        pending += chunk
        *complete, pending = pending.split(b"\n")
        for line in complete:
            if line := line.strip():
                yield line
    if pending := pending.strip():
        yield pending
