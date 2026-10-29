"""The asyncio connection and cursor. They mirror :mod:`camusdb.connection`."""

from __future__ import annotations

import asyncio
import builtins
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from types import TracebackType
from typing import Any, TypeVar

from .. import _admin, codes
from .. import _protocol as proto
from .._admin import BackupGcResult, BackupInfo, NodeStats
from .._base import (
    MAX_FINALIZE_ATTEMPTS,
    ConnectionState,
    CursorState,
    Plan,
    StreamPlan,
    finalize_delay,
    retry_delay,
)
from .._codec import encode_row
from .._config import Config, build_config
from .._protocol import Branch
from ..errors import DatabaseError, Error, InterfaceError, InternalError, ProgrammingError
from ..retry import is_retryable
from ..rows import RowFactory
from ..types import IsolationLevel, Locking
from ._transport import AsyncTransport

__all__ = ["AsyncBackups", "AsyncConnection", "AsyncCursor", "connect", "run_transaction"]

T = TypeVar("T")


async def connect(
    dsn: str | None = None,
    *,
    endpoint: str | Sequence[str] | None = None,
    database: str | None = None,
    user: str | None = None,
    password: str | None = None,
    access_token: str | None = None,
    timeout: float | None = None,
    autocommit: bool | None = None,
    isolation_level: IsolationLevel | str | None = None,
    read_only: bool | None = None,
    locking: Locking | str | None = None,
    http2: bool | None = None,
    token_lifetime: float | None = None,
    allow_insecure_credentials: bool | None = None,
    backup_endpoint: str | None = None,
    backup_timeout: float | None = None,
    row_factory: RowFactory | None = None,
) -> AsyncConnection:
    """Open an asyncio connection to CamusDB. The arguments are those of :func:`camusdb.connect`."""
    config = build_config(
        dsn,
        {
            "endpoint": endpoint,
            "database": database,
            "user": user,
            "password": password,
            "access_token": access_token,
            "timeout": timeout,
            "autocommit": autocommit,
            "isolation_level": isolation_level,
            "read_only": read_only,
            "locking": locking,
            "http2": http2,
            "token_lifetime": token_lifetime,
            "allow_insecure_credentials": allow_insecure_credentials,
            "backup_endpoint": backup_endpoint,
            "backup_timeout": backup_timeout,
        },
    )
    return AsyncConnection(config, row_factory=row_factory)


class AsyncConnection:
    """An asyncio connection. See :class:`camusdb.Connection` for the transaction model::

    async with await camusdb.aio.connect("Endpoint=http://localhost:5095;Database=test") as conn:
        cur = await conn.execute("SELECT name FROM robots")
        print(await cur.fetchall())
    """

    def __init__(self, config: Config, *, row_factory: RowFactory | None = None) -> None:
        self._state = ConnectionState(config, row_factory)
        self._transport = AsyncTransport(config)
        self._backups: AsyncBackups | None = None

    def cursor(self, *, row_factory: RowFactory | None = None) -> AsyncCursor:
        self._state.check_open()
        return AsyncCursor(self, row_factory=row_factory or self._state.row_factory)

    async def commit(self) -> None:
        state = self._state
        state.check_open()
        txn = state.txn
        if txn is None:
            return
        if state.txn_failed:
            state.end_transaction()
            raise InternalError(
                "the transaction failed and the server rolled it back: nothing was committed",
                codes.UNKNOWN,
            )
        try:
            await self._finalize(proto.COMMIT_TRANSACTION, txn)
        finally:
            state.end_transaction()

    async def rollback(self) -> None:
        state = self._state
        state.check_open()
        txn = state.txn
        if txn is None:
            return
        failed = state.txn_failed
        try:
            await self._finalize(proto.ROLLBACK_TRANSACTION, txn)
        except Error:
            if not failed:
                raise
        finally:
            state.end_transaction()

    async def close(self) -> None:
        state = self._state
        if state.closed:
            return
        try:
            if state.txn is not None and not state.txn_failed:
                await self._finalize(proto.ROLLBACK_TRANSACTION, state.txn)
        except Error:
            pass
        finally:
            state.end_transaction()
            state.closed = True
            await self._transport.aclose()

    async def __aenter__(self) -> AsyncConnection:
        self._state.check_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if not self._state.closed:
                if exc_type is None:
                    await self.commit()
                else:
                    with suppress(Error):
                        await self.rollback()
        finally:
            await self.close()

    # --- Settings ---------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._state.closed

    @property
    def autocommit(self) -> bool:
        return self._state.autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._state.autocommit = value

    @property
    def isolation_level(self) -> IsolationLevel | None:
        return self._state.isolation_level

    @isolation_level.setter
    def isolation_level(self, value: IsolationLevel | str | None) -> None:
        self._state.isolation_level = value

    @property
    def read_only(self) -> bool | None:
        return self._state.read_only

    @read_only.setter
    def read_only(self, value: bool | None) -> None:
        self._state.read_only = value

    @property
    def locking(self) -> Locking | None:
        return self._state.locking

    @locking.setter
    def locking(self, value: Locking | str | None) -> None:
        self._state.locking = value

    @property
    def database(self) -> str:
        return self._state.database

    @database.setter
    def database(self, name: str) -> None:
        self._state.set_database(name)

    @property
    def endpoints(self) -> tuple[str, ...]:
        return self._state.config.endpoints

    @property
    def row_factory(self) -> RowFactory:
        return self._state.row_factory

    @row_factory.setter
    def row_factory(self, value: RowFactory) -> None:
        self._state.row_factory = value

    @property
    def transaction_id(self) -> str | None:
        return self._state.txn.id if self._state.txn is not None else None

    @property
    def in_transaction(self) -> bool:
        return self._state.txn is not None

    def __repr__(self) -> str:
        status = "closed" if self.closed else "open"
        return f"<camusdb.aio.AsyncConnection {status} database={self.database!r} endpoints={list(self.endpoints)!r}>"

    # --- Shortcuts --------------------------------------------------------------------------------

    async def execute(
        self, operation: str, parameters: Mapping[str, Any] | None = None
    ) -> AsyncCursor:
        return await self.cursor().execute(operation, parameters)

    @asynccontextmanager
    async def transaction(
        self,
        *,
        isolation_level: IsolationLevel | str | None = None,
        read_only: bool | None = None,
        locking: Locking | str | None = None,
    ) -> AsyncIterator[AsyncConnection]:
        """Run a block in one transaction. See :meth:`camusdb.Connection.transaction`."""
        state = self._state
        state.check_open()
        if state.txn is not None:
            raise ProgrammingError(
                "a transaction is already active; commit or roll back first", codes.INVALID_INPUT
            )
        await self._begin(state.options(isolation_level, read_only, locking))
        try:
            yield self
        except BaseException:
            with suppress(Error):
                await self.rollback()
            raise
        else:
            await self.commit()

    # --- Authentication ---------------------------------------------------------------------------

    async def login(self, user: str, password: str) -> str:
        if not user or password is None:
            raise ProgrammingError("a user and a password are required", codes.INVALID_INPUT)
        self._state.check_open()
        self._transport.auth.set_credentials(user, password)
        return await self._transport.login(user, password)

    async def logout(self) -> None:
        self._state.check_open()
        await self._transport.logout()

    @property
    def access_token(self) -> str | None:
        return self._transport.auth.current_token

    # --- Administration ---------------------------------------------------------------------------

    async def ping(self) -> bool:
        self._state.check_open()
        body = await self._transport.get(proto.PING, fallback="ping failed")
        return body.get("status") == "ok"

    async def create_database(
        self, name: str | None = None, *, if_not_exists: bool = False
    ) -> None:
        self._state.check_open()
        body = {"databaseName": name or self.database, "ifNotExists": if_not_exists}

        async def create() -> None:
            proto.expect_ok(
                await self._transport.post(
                    proto.CREATE_DATABASE, body, fallback="create database failed"
                ),
                "create database failed",
            )

        await self._with_create_retry(create, if_not_exists)

    async def drop_database(self, name: str | None = None) -> None:
        self._state.check_open()
        body = await self._transport.post(
            proto.DROP_DATABASE,
            {"databaseName": name or self.database},
            fallback="drop database failed",
        )
        proto.expect_ok(body, "drop database failed")

    async def create_branch(
        self, name: str, source: str | None = None, *, if_not_exists: bool = False
    ) -> None:
        sql = proto.create_branch_sql(name, source or self.database, if_not_exists)

        async def create() -> None:
            await self._admin(proto.EXECUTE_NON_QUERY, sql)

        await self._with_create_retry(create, False)

    async def show_branches(self, name: str | None = None) -> list[Branch]:
        body = await self._admin(
            proto.EXECUTE_QUERY, proto.show_branches_sql(name or self.database)
        )
        return proto.branches_from(proto.parse_query(body))

    async def show_ancestors(self, name: str | None = None) -> list[Branch]:
        body = await self._admin(
            proto.EXECUTE_QUERY, proto.show_ancestors_sql(name or self.database)
        )
        return proto.branches_from(proto.parse_query(body))

    async def node_stats(self, endpoint: str | None = None) -> NodeStats:
        self._state.check_open()
        target = _admin.node_endpoint(self.endpoints, endpoint) or self._transport.next_endpoint()
        body = await self._transport.get(
            _admin.NODE_STATS, endpoint=target, fallback="node stats failed"
        )
        return _admin.parse_node_stats(body, target)

    @property
    def backups(self) -> AsyncBackups:
        if self._backups is None:
            self._backups = AsyncBackups(self)
        return self._backups

    async def _admin(self, route: str, sql: str) -> dict[str, Any]:
        self._state.check_open()
        return await self._transport.post(
            route, proto.sql_body(self.database, sql, None, None, None)
        )

    async def _with_create_retry(
        self, create: Callable[[], Awaitable[None]], if_not_exists: bool
    ) -> None:
        for attempt in range(1, 6):
            try:
                await create()
                return
            except DatabaseError as exc:
                if if_not_exists and exc.code == codes.DATABASE_ALREADY_EXISTS:
                    return
                if attempt == 5 or not is_retryable(exc):
                    raise
                await asyncio.sleep(min(0.05 * (1 << (attempt - 1)), 0.8))

    # --- Internals for the cursor -----------------------------------------------------------------

    async def _begin(self, options: proto.TransactionOptions) -> proto.TransactionHandle:
        state = self._state
        endpoint = self._transport.next_endpoint()
        body = await self._transport.post(
            proto.START_TRANSACTION,
            proto.start_transaction_body(state.database, options),
            endpoint=endpoint,
            fallback="start transaction failed",
        )
        state.txn = proto.parse_start_transaction(body, endpoint)
        return state.txn

    async def _transaction_for(self, plan: Plan) -> proto.TransactionHandle | None:
        state = self._state
        state.check_usable()
        if plan.begins_transaction and state.needs_begin():
            await self._begin(state.options())
        plan.check_transaction(state.txn)
        return state.txn

    async def _finalize(self, path: str, txn: proto.TransactionHandle) -> None:
        body = proto.finalize_body(self._state.database, txn)
        fallback = "commit failed" if path == proto.COMMIT_TRANSACTION else "rollback failed"
        for attempt in range(MAX_FINALIZE_ATTEMPTS + 1):
            try:
                proto.expect_ok(
                    await self._transport.post(
                        path, body, endpoint=txn.endpoint, fallback=fallback
                    ),
                    fallback,
                )
                return
            except DatabaseError as exc:
                if (
                    exc.code != codes.TRANSACTION_FINALIZE_UNRESOLVED
                    or attempt == MAX_FINALIZE_ATTEMPTS
                ):
                    raise
                await asyncio.sleep(finalize_delay(attempt))

    async def _run(self, plan: Plan) -> proto.Result:
        txn = await self._transaction_for(plan)
        body = plan.body(self._state, txn)
        try:
            response = await self._transport.post(
                plan.route, body, endpoint=txn.endpoint if txn else None
            )
            return plan.result(response)
        except Error:
            self._state.statement_failed()
            raise


class AsyncCursor:
    """An asyncio cursor. Results are buffered; use :meth:`stream` for a large result."""

    def __init__(self, connection: AsyncConnection, *, row_factory: RowFactory) -> None:
        self._connection = connection
        self._state = CursorState(row_factory)

    @property
    def connection(self) -> AsyncConnection:
        return self._connection

    @property
    def description(self) -> tuple[tuple[Any, ...], ...] | None:
        return self._state.description

    @property
    def rowcount(self) -> int:
        return self._state.rowcount

    @property
    def rownumber(self) -> int | None:
        return self._state.rownumber

    @property
    def lastrowid(self) -> None:
        return None

    @property
    def arraysize(self) -> int:
        return self._state.arraysize

    @arraysize.setter
    def arraysize(self, value: int) -> None:
        self._state.arraysize = value

    @property
    def closed(self) -> bool:
        return self._state.closed

    async def close(self) -> None:
        self._state.closed = True
        self._state.reset()

    async def __aenter__(self) -> AsyncCursor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _check(self) -> None:
        self._state.check_open()
        self._connection._state.check_open()

    async def execute(
        self, operation: str, parameters: Mapping[str, Any] | None = None
    ) -> AsyncCursor:
        self._check()
        self._state.reset()
        result = await self._connection._run(Plan(operation, parameters))
        self._state.set_result(result)
        return self

    async def executemany(
        self, operation: str, seq_of_parameters: Iterable[Mapping[str, Any]]
    ) -> AsyncCursor:
        self._check()
        total = 0
        counted = False
        for parameters in seq_of_parameters:
            await self.execute(operation, parameters)
            if self._state.rowcount >= 0:
                total += self._state.rowcount
                counted = True
        self._state.reset()
        self._state.rowcount = total if counted else -1
        return self

    async def stream(
        self, operation: str, parameters: Mapping[str, Any] | None = None
    ) -> AsyncIterator[Any]:
        """Run a query and give its rows as they arrive. See :meth:`camusdb.Cursor.stream`."""
        self._check()
        plan = StreamPlan(operation, parameters)
        if not plan.streams:
            await self.execute(operation, parameters)
            return
        conn = self._connection
        self._state.reset()
        txn = await conn._transaction_for(plan)
        endpoint = txn.endpoint if txn else conn._transport.next_endpoint()
        decoder = proto.StreamDecoder()
        try:
            async with conn._transport.stream_lines(
                proto.EXECUTE_QUERY_STREAM, plan.body(conn._state, txn), endpoint=endpoint
            ) as lines:
                header = await anext(lines, None)
                if header is None:
                    raise InterfaceError("the streaming response ended before the schema header")
                make = self._state.set_streaming(proto.description(decoder.header(header)))
                count = 0
                async for line in lines:
                    row = decoder.row(line)
                    if row is None:
                        break
                    count += 1
                    yield make(row)
                self._state.rowcount = count
        except Error:
            conn._state.statement_failed()
            raise
        except ValueError as exc:
            conn._state.statement_failed()
            raise InterfaceError(f"malformed streaming response: {exc}") from exc

    async def insert(self, table: str, row: Mapping[str, Any]) -> int:
        self._check()
        conn = self._connection
        state = conn._state
        state.check_usable()
        if not table:
            raise ProgrammingError("a table name is required", codes.INVALID_INPUT)
        values = encode_row(row)
        if state.needs_begin():
            await conn._begin(state.options())
        txn = state.txn
        self._state.reset()
        try:
            body = await conn._transport.post(
                proto.INSERT,
                proto.insert_body(state.database, table, values, txn),
                endpoint=txn.endpoint if txn else None,
                fallback="insert failed",
            )
        except Error:
            state.statement_failed()
            raise
        self._state.rowcount = int(body.get("rows") or 0)
        return self._state.rowcount

    async def fetchone(self) -> Any:
        return self._state.fetchone()

    async def fetchmany(self, size: int | None = None) -> list[Any]:
        return self._state.fetchmany(size)

    async def fetchall(self) -> list[Any]:
        return self._state.fetchall()

    async def scroll(self, value: int, mode: str = "relative") -> None:
        self._state.scroll(value, mode)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        for row in self._state.iter_buffered():
            yield row

    async def setinputsizes(self, sizes: Any) -> None:
        """Does nothing: values carry their own types."""

    async def setoutputsize(self, size: Any, column: int | None = None) -> None:
        """Does nothing: results are not truncated."""


class AsyncBackups:
    """The backup administration API, with coroutines. See :class:`camusdb.connection.Backups`."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def take_full(self) -> BackupInfo:
        fallback = "take full backup failed"
        return _admin.parse_backup(await self._post(_admin.BACKUP_FULL, None, fallback), fallback)

    async def take_incremental(self, parent_backup_id: str | uuid.UUID) -> BackupInfo:
        fallback = "take incremental backup failed"
        body = {"parentBackupId": _admin.backup_id(parent_backup_id, "parent_backup_id")}
        return _admin.parse_backup(
            await self._post(_admin.BACKUP_INCREMENTAL, body, fallback), fallback
        )

    async def take_coordinated(self) -> BackupInfo:
        fallback = "take coordinated backup failed"
        return _admin.parse_backup(
            await self._post(_admin.BACKUP_COORDINATED, None, fallback), fallback
        )

    async def list(self) -> builtins.list[BackupInfo]:
        fallback = "list backups failed"
        return _admin.parse_backup_list(await self._get(_admin.BACKUPS, fallback), fallback)

    async def chain(self, leaf_backup_id: str | uuid.UUID) -> builtins.list[BackupInfo]:
        fallback = "get backup chain failed"
        leaf = _admin.backup_id(leaf_backup_id, "leaf_backup_id")
        return _admin.parse_backup_list(
            await self._get(_admin.backup_chain_path(leaf), fallback), fallback
        )

    async def preview_garbage_collection(self) -> BackupGcResult:
        path = _admin.backup_gc_path(dry_run=True)
        return _admin.parse_backup_gc(
            await self._post(path, None, "backup garbage collection failed")
        )

    async def collect_garbage(self) -> BackupGcResult:
        path = _admin.backup_gc_path(dry_run=False)
        return _admin.parse_backup_gc(
            await self._post(path, None, "backup garbage collection failed")
        )

    async def _post(self, path: str, body: Any, fallback: str) -> dict[str, Any]:
        conn = self._connection
        conn._state.check_open()
        return await conn._transport.post(
            path,
            body,
            endpoint=self._endpoint(),
            fallback=fallback,
            timeout=conn._state.config.backup_timeout,
        )

    async def _get(self, path: str, fallback: str) -> dict[str, Any]:
        conn = self._connection
        conn._state.check_open()
        return await conn._transport.get(
            path,
            endpoint=self._endpoint(),
            fallback=fallback,
            timeout=conn._state.config.backup_timeout,
        )

    def _endpoint(self) -> str:
        conn = self._connection
        return conn._state.config.backup_endpoint or conn._transport.next_endpoint()


async def run_transaction(
    connection: AsyncConnection,
    func: Callable[[AsyncConnection], Awaitable[T]],
    *,
    max_attempts: int = 5,
    isolation_level: IsolationLevel | str | None = None,
    read_only: bool | None = None,
    locking: Locking | str | None = None,
) -> T:
    """Run ``await func(connection)`` in a transaction, replayed after a conflict.

    See :func:`camusdb.run_transaction`.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(1, max_attempts + 1):
        try:
            async with connection.transaction(
                isolation_level=isolation_level, read_only=read_only, locking=locking
            ):
                return await func(connection)
        except Error as exc:
            if attempt == max_attempts or not is_retryable(exc):
                raise
            await asyncio.sleep(retry_delay(attempt))
    raise AssertionError("unreachable")
