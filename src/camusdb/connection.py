"""The sync PEP 249 connection and cursor."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from types import TracebackType
from typing import Any, TypeVar

from . import _protocol as proto
from . import codes
from ._base import (
    MAX_FINALIZE_ATTEMPTS,
    ConnectionState,
    CursorState,
    Plan,
    StreamPlan,
    finalize_delay,
    retry_delay,
)
from ._codec import encode_row
from ._config import Config, build_config
from ._protocol import Branch
from ._transport import Transport
from .errors import DatabaseError, Error, InterfaceError, InternalError, ProgrammingError
from .retry import is_retryable
from .rows import RowFactory
from .types import IsolationLevel, Locking

__all__ = ["Connection", "Cursor", "connect", "run_transaction"]

T = TypeVar("T")


def connect(
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
    row_factory: RowFactory | None = None,
) -> Connection:
    """Open a connection to CamusDB.

    ``dsn`` is an optional CamusDB connection string, such as
    ``"Endpoint=http://localhost:5095;Database=test"``. A keyword argument wins over the same key in
    the string. ``endpoint`` can name several nodes, as a list or comma-separated, and the driver
    sends statements to them in rotation.

    No network request is made here: the first statement makes the first request.
    """
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
        },
    )
    return Connection(config, row_factory=row_factory)


class Connection:
    """A connection to one CamusDB deployment and database.

    By default the connection follows PEP 249: the first statement begins a transaction, and
    :meth:`commit` or :meth:`rollback` ends it. Set ``autocommit = True`` to run each statement in its
    own short transaction, and use :meth:`transaction` for an explicit block.

    Use the connection as a context manager to commit on success, roll back on an error, and close::

        with camusdb.connect("Endpoint=http://localhost:5095;Database=test") as conn:
            conn.execute("INSERT INTO robots (id, name) VALUES (GEN_ID(), @name)", {"name": "T-800"})
    """

    def __init__(self, config: Config, *, row_factory: RowFactory | None = None) -> None:
        self._state = ConnectionState(config, row_factory)
        self._transport = Transport(config)

    # --- PEP 249 ----------------------------------------------------------------------------------

    def cursor(self, *, row_factory: RowFactory | None = None) -> Cursor:
        self._state.check_open()
        return Cursor(self, row_factory=row_factory or self._state.row_factory)

    def commit(self) -> None:
        """Commit the current transaction. With no transaction, do nothing."""
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
            self._finalize(proto.COMMIT_TRANSACTION, txn)
        finally:
            state.end_transaction()

    def rollback(self) -> None:
        """Roll back the current transaction. With no transaction, do nothing."""
        state = self._state
        state.check_open()
        txn = state.txn
        if txn is None:
            return
        failed = state.txn_failed
        try:
            self._finalize(proto.ROLLBACK_TRANSACTION, txn)
        except Error:
            # The server discarded a failed transaction already; there is nothing left to undo.
            if not failed:
                raise
        finally:
            state.end_transaction()

    def close(self) -> None:
        """Roll back an open transaction and release the HTTP connections. A second call does nothing."""
        state = self._state
        if state.closed:
            return
        try:
            if state.txn is not None and not state.txn_failed:
                self._finalize(proto.ROLLBACK_TRANSACTION, state.txn)
        except Error:
            pass
        finally:
            state.end_transaction()
            state.closed = True
            self._transport.close()

    def __enter__(self) -> Connection:
        self._state.check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if not self._state.closed:
                if exc_type is None:
                    self.commit()
                else:
                    with suppress(Error):
                        self.rollback()
        finally:
            self.close()

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
        """The isolation level of new transactions. None means the server default (serializable)."""
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
        """The id of the current transaction, as ``"pt:counter"``, or None."""
        return self._state.txn.id if self._state.txn is not None else None

    @property
    def in_transaction(self) -> bool:
        return self._state.txn is not None

    def __repr__(self) -> str:
        status = "closed" if self.closed else "open"
        return f"<camusdb.Connection {status} database={self.database!r} endpoints={list(self.endpoints)!r}>"

    # --- Shortcuts --------------------------------------------------------------------------------

    def execute(self, operation: str, parameters: Mapping[str, Any] | None = None) -> Cursor:
        """Create a cursor, run ``operation`` on it and return it."""
        return self.cursor().execute(operation, parameters)

    @contextmanager
    def transaction(
        self,
        *,
        isolation_level: IsolationLevel | str | None = None,
        read_only: bool | None = None,
        locking: Locking | str | None = None,
    ) -> Iterator[Connection]:
        """Run a block in one transaction: commit when it ends, roll back when it raises.

        The transaction begins at once. An argument left as None takes the connection's setting.
        Transactions do not nest: with a transaction already open, this raises ProgrammingError.
        """
        state = self._state
        state.check_open()
        if state.txn is not None:
            raise ProgrammingError(
                "a transaction is already active; commit or roll back first", codes.INVALID_INPUT
            )
        self._begin(state.options(isolation_level, read_only, locking))
        try:
            yield self
        except BaseException:
            with suppress(Error):
                self.rollback()
            raise
        else:
            self.commit()

    # --- Authentication ---------------------------------------------------------------------------

    def login(self, user: str, password: str) -> str:
        """Log in as ``user`` and return the bearer token. Later statements send that token.

        Use it when the password is not in the connection settings, for example when it comes from
        a secret manager. The driver renews the token with these credentials when it expires.
        """
        if not user or password is None:
            raise ProgrammingError("a user and a password are required", codes.INVALID_INPUT)
        self._state.check_open()
        self._transport.auth.set_credentials(user, password)
        return self._transport.login(user, password)

    def logout(self) -> None:
        """Revoke the current token on the server. A later statement logs in again."""
        self._state.check_open()
        self._transport.logout()

    @property
    def access_token(self) -> str | None:
        """The bearer token that statements send now, or None."""
        return self._transport.auth.current_token

    # --- Administration ---------------------------------------------------------------------------

    def ping(self) -> bool:
        """Return True when the next endpoint in rotation answers."""
        self._state.check_open()
        body = self._transport.get(proto.PING, fallback="ping failed")
        return body.get("status") == "ok"

    def create_database(self, name: str | None = None, *, if_not_exists: bool = False) -> None:
        """Create a database: ``name``, or the connection's database."""
        self._state.check_open()
        name = name or self.database
        body = {"databaseName": name, "ifNotExists": if_not_exists}
        self._with_create_retry(
            lambda: proto.expect_ok(
                self._transport.post(
                    proto.CREATE_DATABASE, body, fallback="create database failed"
                ),
                "create database failed",
            ),
            if_not_exists,
        )

    def drop_database(self, name: str | None = None) -> None:
        """Drop a database: ``name``, or the connection's database."""
        self._state.check_open()
        body = self._transport.post(
            proto.DROP_DATABASE,
            {"databaseName": name or self.database},
            fallback="drop database failed",
        )
        proto.expect_ok(body, "drop database failed")

    def create_branch(
        self, name: str, source: str | None = None, *, if_not_exists: bool = False
    ) -> None:
        """Create the copy-on-write branch ``name`` of ``source`` (the connection's database by default)."""
        sql = proto.create_branch_sql(name, source or self.database, if_not_exists)
        self._with_create_retry(lambda: self._admin(proto.EXECUTE_NON_QUERY, sql), False)

    def show_branches(self, name: str | None = None) -> list[Branch]:
        """Every branch that descends from a database, at any depth."""
        body = self._admin(proto.EXECUTE_QUERY, proto.show_branches_sql(name or self.database))
        return proto.branches_from(proto.parse_query(body))

    def show_ancestors(self, name: str | None = None) -> list[Branch]:
        """The chain of databases that a branch descends from, nearest parent first."""
        body = self._admin(proto.EXECUTE_QUERY, proto.show_ancestors_sql(name or self.database))
        return proto.branches_from(proto.parse_query(body))

    def _admin(self, route: str, sql: str) -> dict[str, Any]:
        # A server-level statement: it runs outside the connection's transaction.
        self._state.check_open()
        return self._transport.post(route, proto.sql_body(self.database, sql, None, None, None))

    def _with_create_retry(self, create: Callable[[], object], if_not_exists: bool) -> None:
        # Concurrent creates can collide while the server allocates the database id; that conflict
        # is retryable. A create with IF NOT EXISTS that loses a registration race is a success.
        for attempt in range(1, 6):
            try:
                create()
                return
            except DatabaseError as exc:
                if if_not_exists and exc.code == codes.DATABASE_ALREADY_EXISTS:
                    return
                if attempt == 5 or not is_retryable(exc):
                    raise
                time.sleep(min(0.05 * (1 << (attempt - 1)), 0.8))

    # --- Internals for the cursor -----------------------------------------------------------------

    def _begin(self, options: proto.TransactionOptions) -> proto.TransactionHandle:
        state = self._state
        endpoint = self._transport.next_endpoint()
        body = self._transport.post(
            proto.START_TRANSACTION,
            proto.start_transaction_body(state.database, options),
            endpoint=endpoint,
            fallback="start transaction failed",
        )
        state.txn = proto.parse_start_transaction(body, endpoint)
        return state.txn

    def _transaction_for(self, plan: Plan) -> proto.TransactionHandle | None:
        state = self._state
        state.check_usable()
        if plan.begins_transaction and state.needs_begin():
            self._begin(state.options())
        plan.check_transaction(state.txn)
        return state.txn

    def _finalize(self, path: str, txn: proto.TransactionHandle) -> None:
        body = proto.finalize_body(self._state.database, txn)
        fallback = "commit failed" if path == proto.COMMIT_TRANSACTION else "rollback failed"
        for attempt in range(MAX_FINALIZE_ATTEMPTS + 1):
            try:
                proto.expect_ok(
                    self._transport.post(path, body, endpoint=txn.endpoint, fallback=fallback),
                    fallback,
                )
                return
            except DatabaseError as exc:
                if (
                    exc.code != codes.TRANSACTION_FINALIZE_UNRESOLVED
                    or attempt == MAX_FINALIZE_ATTEMPTS
                ):
                    raise
                time.sleep(finalize_delay(attempt))

    def _run(self, plan: Plan) -> proto.Result:
        txn = self._transaction_for(plan)
        body = plan.body(self._state, txn)
        try:
            response = self._transport.post(
                plan.route, body, endpoint=txn.endpoint if txn else None
            )
            return plan.result(response)
        except Error:
            self._state.statement_failed()
            raise


class Cursor:
    """A PEP 249 cursor. Results are buffered: every row is in memory when ``execute`` returns.

    Use :meth:`stream` for a large result, whose rows arrive while you read them.
    """

    def __init__(self, connection: Connection, *, row_factory: RowFactory) -> None:
        self._connection = connection
        self._state = CursorState(row_factory)

    @property
    def connection(self) -> Connection:
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
        """Always None. Use ``INSERT ... RETURNING id`` to read a generated key."""
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

    def close(self) -> None:
        self._state.closed = True
        self._state.reset()

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _check(self) -> None:
        self._state.check_open()
        self._connection._state.check_open()

    def execute(self, operation: str, parameters: Mapping[str, Any] | None = None) -> Cursor:
        """Run one statement. Placeholders are ``@name``; ``parameters`` maps each name to its value.

        A key in ``parameters`` can be written with or without the ``@``. Returns the cursor.
        """
        self._check()
        self._state.reset()
        result = self._connection._run(Plan(operation, parameters))
        self._state.set_result(result)
        return self

    def executemany(self, operation: str, seq_of_parameters: Iterable[Mapping[str, Any]]) -> Cursor:
        """Run one statement for each set of parameters. ``rowcount`` is the total."""
        self._check()
        total = 0
        counted = False
        for parameters in seq_of_parameters:
            self.execute(operation, parameters)
            if self._state.rowcount >= 0:
                total += self._state.rowcount
                counted = True
        self._state.reset()
        self._state.rowcount = total if counted else -1
        return self

    def stream(self, operation: str, parameters: Mapping[str, Any] | None = None) -> Iterator[Any]:
        """Run a query and give its rows as they arrive from the network.

        The rows never all sit in memory at once. The schema is in ``description`` when the first row
        arrives. Close the iterator, or read it to its end, to release the HTTP connection.

        The server cannot retry a serializable conflict that surfaces after rows began to arrive;
        it is raised from the iteration instead.
        """
        self._check()
        plan = StreamPlan(operation, parameters)
        if not plan.streams:
            self.execute(operation, parameters)
            return iter(())
        return self._stream(plan)

    def _stream(self, plan: StreamPlan) -> Iterator[Any]:
        conn = self._connection
        self._state.reset()
        txn = conn._transaction_for(plan)
        endpoint = txn.endpoint if txn else conn._transport.next_endpoint()
        decoder = proto.StreamDecoder()
        try:
            with conn._transport.stream_lines(
                proto.EXECUTE_QUERY_STREAM, plan.body(conn._state, txn), endpoint=endpoint
            ) as lines:
                header = next(lines, None)
                if header is None:
                    raise InterfaceError("the streaming response ended before the schema header")
                make = self._state.set_streaming(proto.description(decoder.header(header)))
                count = 0
                for line in lines:
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

    def insert(self, table: str, row: Mapping[str, Any]) -> int:
        """Insert one row through the typed insert route, with no SQL text. Returns the row count."""
        self._check()
        conn = self._connection
        state = conn._state
        state.check_usable()
        if not table:
            raise ProgrammingError("a table name is required", codes.INVALID_INPUT)
        values = encode_row(row)
        if state.needs_begin():
            conn._begin(state.options())
        txn = state.txn
        self._state.reset()
        try:
            body = conn._transport.post(
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

    def fetchone(self) -> Any:
        return self._state.fetchone()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        return self._state.fetchmany(size)

    def fetchall(self) -> list[Any]:
        return self._state.fetchall()

    def scroll(self, value: int, mode: str = "relative") -> None:
        self._state.scroll(value, mode)

    def __iter__(self) -> Iterator[Any]:
        return self._state.iter_buffered()

    def setinputsizes(self, sizes: Any) -> None:
        """Does nothing: values carry their own types."""

    def setoutputsize(self, size: Any, column: int | None = None) -> None:
        """Does nothing: results are not truncated."""


def run_transaction(
    connection: Connection,
    func: Callable[[Connection], T],
    *,
    max_attempts: int = 5,
    isolation_level: IsolationLevel | str | None = None,
    read_only: bool | None = None,
    locking: Locking | str | None = None,
) -> T:
    """Run ``func(connection)`` in a transaction, and replay it after a serialization conflict.

    A conflict aborts the whole transaction, so ``func`` must do all of its reads and writes again on
    each attempt; it must not keep state from an earlier attempt. Any other error is raised at once.
    The back-off between attempts is min(20 ms x 2^attempt, 400 ms), with 25% jitter.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(1, max_attempts + 1):
        try:
            with connection.transaction(
                isolation_level=isolation_level, read_only=read_only, locking=locking
            ):
                return func(connection)
        except Error as exc:
            if attempt == max_attempts or not is_retryable(exc):
                raise
            time.sleep(retry_delay(attempt))
    raise AssertionError("unreachable")
