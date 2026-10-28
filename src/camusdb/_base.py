"""State and rules that the sync and the async connection and cursor share."""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping
from typing import Any

from . import _protocol as proto
from . import _sql, codes
from ._codec import encode_parameters
from ._config import Config, coerce_isolation, coerce_locking
from .errors import InterfaceError, InternalError, ProgrammingError
from .rows import RowFactory, RowMaker, tuple_row
from .types import IsolationLevel, Locking

# A COMMIT or ROLLBACK whose outcome is not resolved yet (CADB0509) is sent again, the same one on the
# same transaction, never replayed from the start: the commit may already have applied.
MAX_FINALIZE_ATTEMPTS = 10


def finalize_delay(attempt: int) -> float:
    """50 ms x 2^attempt, at most 3.2 s: the server's contract for an unresolved finalize."""
    return 0.05 * (1 << min(attempt, 6))


def retry_delay(attempt: int) -> float:
    """min(20 ms x 2^attempt, 400 ms), with 25% jitter: the back-off between transaction replays."""
    base = min(0.02 * (1 << attempt), 0.4)
    return max(0.001, base + base * 0.25 * (2 * random.random() - 1))


def aborted_error() -> InternalError:
    return InternalError(
        "the current transaction failed and the server discarded it; call rollback() before you run "
        "another statement",
        codes.UNKNOWN,
    )


class ConnectionState:
    """Transaction state and settings of one connection."""

    def __init__(self, config: Config, row_factory: RowFactory | None) -> None:
        self.config = config
        self.database = config.database
        self.row_factory: RowFactory = row_factory or tuple_row
        self.closed = False
        self._autocommit = config.autocommit
        self._isolation_level = config.isolation_level
        self._read_only = config.read_only
        self._locking = config.locking
        self.txn: proto.TransactionHandle | None = None
        # True after a statement of the transaction failed. The server discarded the transaction.
        self.txn_failed = False

    # --- Settings ---------------------------------------------------------------------------------

    def _check_settable(self, name: str) -> None:
        if self.txn is not None:
            raise ProgrammingError(
                f"cannot change {name} while a transaction is active", codes.INVALID_INPUT
            )

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._check_settable("autocommit")
        self._autocommit = bool(value)

    @property
    def isolation_level(self) -> IsolationLevel | None:
        return self._isolation_level

    @isolation_level.setter
    def isolation_level(self, value: IsolationLevel | str | None) -> None:
        self._check_settable("isolation_level")
        self._isolation_level = coerce_isolation(value)

    @property
    def read_only(self) -> bool | None:
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool | None) -> None:
        self._check_settable("read_only")
        self._read_only = None if value is None else bool(value)

    @property
    def locking(self) -> Locking | None:
        return self._locking

    @locking.setter
    def locking(self, value: Locking | str | None) -> None:
        self._check_settable("locking")
        self._locking = coerce_locking(value)

    def set_database(self, name: str) -> None:
        self._check_settable("the database")
        if not name:
            raise ProgrammingError("a database name is required", codes.INVALID_INPUT)
        self.database = name

    def options(
        self,
        isolation_level: IsolationLevel | str | None = None,
        read_only: bool | None = None,
        locking: Locking | str | None = None,
    ) -> proto.TransactionOptions:
        """The options of a transaction: each one given here, else the connection's setting."""
        return proto.TransactionOptions(
            isolation_level=coerce_isolation(isolation_level) or self._isolation_level,
            read_only=self._read_only if read_only is None else read_only,
            locking=coerce_locking(locking) or self._locking,
        )

    # --- Rules ------------------------------------------------------------------------------------

    def check_open(self) -> None:
        if self.closed:
            raise InterfaceError("the connection is closed")

    def check_usable(self) -> None:
        self.check_open()
        if self.txn_failed:
            raise aborted_error()

    def needs_begin(self) -> bool:
        """Whether a statement must begin the implicit transaction first (PEP 249 mode)."""
        return self.txn is None and not self._autocommit

    def statement_failed(self) -> None:
        """Record that a statement sent in the transaction failed.

        The server discards a transaction when one of its statements fails. Thus a later statement
        must not run as if the transaction were still there. A failure before the request was sent,
        such as a value that cannot be bound, does not come here.
        """
        if self.txn is not None:
            self.txn_failed = True

    def end_transaction(self) -> None:
        self.txn = None
        self.txn_failed = False


class Plan:
    """What one ``execute`` sends: the route, the body builder input and the target."""

    __slots__ = ("kind", "parameters", "sql")

    def __init__(self, sql: str, parameters: Mapping[str, Any] | None) -> None:
        if not isinstance(sql, str) or not sql.strip():
            raise ProgrammingError("the statement is empty", codes.INVALID_INPUT)
        self.sql = sql
        self.kind = _sql.classify(sql)
        if self.kind is _sql.StatementKind.DDL and parameters:
            raise ProgrammingError("a DDL statement takes no parameters", codes.INVALID_INPUT)
        self.parameters = encode_parameters(parameters)

    @property
    def route(self) -> str:
        if self.kind is _sql.StatementKind.QUERY:
            return proto.EXECUTE_QUERY
        if self.kind is _sql.StatementKind.DDL:
            return proto.EXECUTE_DDL
        return proto.EXECUTE_NON_QUERY

    @property
    def begins_transaction(self) -> bool:
        # A schema change commits on its own, so it does not open the implicit transaction.
        return self.kind is not _sql.StatementKind.DDL

    def check_transaction(self, txn: proto.TransactionHandle | None) -> None:
        if txn is not None and _sql.runs_in_own_transaction(self.sql):
            raise ProgrammingError(
                "TRUNCATE runs in its own transaction, which a ROLLBACK cannot undo, so it is refused "
                "inside a transaction. Commit or roll back first, or use autocommit",
                codes.STATEMENT_NOT_ALLOWED_IN_TRANSACTION,
            )

    def body(self, state: ConnectionState, txn: proto.TransactionHandle | None) -> dict[str, Any]:
        # Reads run in a snapshot with no locking mode, so only writes carry the autocommit options.
        autocommit = state.options() if self.kind is not _sql.StatementKind.QUERY else None
        return proto.sql_body(state.database, self.sql, self.parameters, txn, autocommit)

    def result(self, body: Mapping[str, Any]) -> proto.Result:
        if self.kind is _sql.StatementKind.QUERY:
            return proto.parse_query(body)
        if self.kind is _sql.StatementKind.DDL:
            proto.expect_ok(body, "DDL statement failed")
            return proto.Result(None, [], -1)
        return proto.parse_non_query(body)


class StreamPlan(Plan):
    """A statement for the streaming route: a read, or a write with a RETURNING list."""

    def __init__(self, sql: str, parameters: Mapping[str, Any] | None) -> None:
        super().__init__(sql, parameters)
        if self.kind is _sql.StatementKind.DDL:
            raise ProgrammingError(
                "stream() runs queries; use execute() for DDL", codes.INVALID_INPUT
            )
        self.write = _sql.is_dml(sql)
        # A write without RETURNING has no rows to stream; it runs on its own route.
        self.streams = not self.write or _sql.has_returning(sql)

    def body(self, state: ConnectionState, txn: proto.TransactionHandle | None) -> dict[str, Any]:
        autocommit = state.options() if self.write else None
        return proto.sql_body(state.database, self.sql, self.parameters, txn, autocommit)


class CursorState:
    """The result buffer and the PEP 249 attributes of a cursor."""

    def __init__(self, row_factory: RowFactory) -> None:
        self.row_factory = row_factory
        self.arraysize = 1
        self.description: tuple[tuple[Any, ...], ...] | None = None
        self.rowcount = -1
        self.closed = False
        self._rows: list[tuple[Any, ...]] = []
        self._pos = 0
        self._make: RowMaker = tuple_row(())

    @property
    def rownumber(self) -> int | None:
        return self._pos if self.description is not None else None

    def reset(self) -> None:
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._pos = 0

    def set_result(self, result: proto.Result) -> None:
        self.description = proto.description(result.schema)
        self._make = (
            self.row_factory(self.description) if self.description is not None else tuple_row(())
        )
        self._rows = result.rows
        self._pos = 0
        self.rowcount = result.rowcount

    def set_streaming(self, description: tuple[tuple[Any, ...], ...] | None) -> RowMaker:
        self.description = description
        self._rows = []
        self._pos = 0
        self.rowcount = -1
        self._make = self.row_factory(description) if description is not None else tuple_row(())
        return self._make

    def check_open(self) -> None:
        if self.closed:
            raise InterfaceError("the cursor is closed")

    def check_result(self) -> None:
        self.check_open()
        if self.description is None:
            raise ProgrammingError("the last statement returned no rows", codes.INVALID_INPUT)

    def fetchone(self) -> Any:
        self.check_result()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return self._make(row)

    def fetchmany(self, size: int | None = None) -> list[Any]:
        self.check_result()
        count = self.arraysize if size is None else size
        rows = self._rows[self._pos : self._pos + max(count, 0)]
        self._pos += len(rows)
        return [self._make(row) for row in rows]

    def fetchall(self) -> list[Any]:
        self.check_result()
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return [self._make(row) for row in rows]

    def scroll(self, value: int, mode: str = "relative") -> None:
        self.check_result()
        position = self._pos + value if mode == "relative" else value
        if mode not in ("relative", "absolute"):
            raise ProgrammingError(
                f"scroll mode {mode!r} is not 'relative' or 'absolute'", codes.INVALID_INPUT
            )
        if not 0 <= position <= len(self._rows):
            raise IndexError("scroll target is out of the result set")
        self._pos = position

    def iter_buffered(self) -> Iterator[Any]:
        while (row := self.fetchone()) is not None:
            yield row
