"""The REST protocol, without I/O: request bodies, response checks and result decoding.

The sync and the async transports send what this module builds and give back to it what they
receive, so both speak exactly the same protocol.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import codes
from ._codec import Column, WireValue, decode_row, read_schema
from .errors import Error, InterfaceError, ProgrammingError, error_for_code
from .types import ColumnType, IsolationLevel, Locking

NDJSON = "application/x-ndjson"

# Routes.
START_TRANSACTION = "/start-transaction"
COMMIT_TRANSACTION = "/commit-transaction"
ROLLBACK_TRANSACTION = "/rollback-transaction"
EXECUTE_QUERY = "/execute-sql-query"
EXECUTE_QUERY_STREAM = "/execute-sql-query-stream"
EXECUTE_NON_QUERY = "/execute-sql-non-query"
EXECUTE_DDL = "/execute-sql-ddl"
INSERT = "/insert"
PING = "/ping"
LOGIN = "/login"
LOGOUT = "/logout"
CREATE_DATABASE = "/create-db"
DROP_DATABASE = "/drop-db"


@dataclass(frozen=True)
class TransactionHandle:
    """A transaction the server began. It stays on the endpoint that began it."""

    pt: int
    counter: int
    endpoint: str

    @property
    def id(self) -> str:
        return f"{self.pt}:{self.counter}"


@dataclass(frozen=True)
class TransactionOptions:
    isolation_level: IsolationLevel | None = None
    read_only: bool | None = None
    locking: Locking | None = None

    def wire(self) -> dict[str, str]:
        fields: dict[str, str] = {}
        if self.isolation_level is not None:
            fields["isolationLevel"] = self.isolation_level.value
        if self.read_only is not None:
            fields["transactionMode"] = "ReadOnly" if self.read_only else "ReadWrite"
        if self.locking is not None:
            fields["locking"] = self.locking.value
        return fields


@dataclass(frozen=True)
class Branch:
    """One row of :meth:`Connection.show_branches` or :meth:`Connection.show_ancestors`."""

    database: str | None
    id: str | None
    depth: int
    parent: str | None
    fork_timestamp: str | None


@dataclass
class Result:
    """A decoded statement result. ``schema`` is None when the statement returns no rows."""

    schema: list[Column] | None
    rows: list[tuple[Any, ...]]
    rowcount: int


# --- Request bodies -------------------------------------------------------------------------------


def start_transaction_body(database: str, options: TransactionOptions) -> dict[str, Any]:
    return {"databaseName": database, **options.wire()}


def finalize_body(database: str, txn: TransactionHandle) -> dict[str, Any]:
    return {"databaseName": database, "txnIdPT": txn.pt, "txnIdCounter": txn.counter}


def sql_body(
    database: str,
    sql: str,
    parameters: Mapping[str, WireValue] | None,
    txn: TransactionHandle | None,
    autocommit: TransactionOptions | None,
) -> dict[str, Any]:
    """The body of the query, non-query and DDL routes.

    In a transaction the body names it. Outside one, ``autocommit`` sets the options of the short
    transaction the server begins for the statement.
    """
    body: dict[str, Any] = {"databaseName": database, "sql": sql}
    if parameters:
        body["parameters"] = parameters
    if txn is not None:
        body["txnIdPT"] = txn.pt
        body["txnIdCounter"] = txn.counter
    elif autocommit is not None:
        body.update(autocommit.wire())
    return body


def insert_body(
    database: str, table: str, values: Mapping[str, WireValue], txn: TransactionHandle | None
) -> dict[str, Any]:
    body: dict[str, Any] = {"databaseName": database, "tableName": table, "values": values}
    if txn is not None:
        body["txnIdPT"] = txn.pt
        body["txnIdCounter"] = txn.counter
    return body


# --- Responses ------------------------------------------------------------------------------------


def parse_response(
    status_code: int, content: bytes, fallback: str = "request failed"
) -> dict[str, Any]:
    """Decode a JSON response, and raise the server's error if the response is a failure."""
    try:
        body = json.loads(content) if content else None
    except ValueError:
        body = None

    if isinstance(body, dict):
        if status_code >= 400 or body.get("status") == "failed":
            raise error_for_code(body.get("code"), sanitize(body.get("message")) or fallback)
        return body

    if status_code >= 400:
        text = sanitize(content.decode("utf-8", "replace")) or fallback
        raise error_for_code(codes.UNKNOWN, f"HTTP {status_code}: {text}")
    raise InterfaceError(f"{fallback}: the server sent no JSON object")


def expect_ok(body: Mapping[str, Any], fallback: str) -> None:
    if body.get("status") != "ok":
        raise error_for_code(body.get("code"), sanitize(body.get("message")) or fallback)


def parse_start_transaction(body: Mapping[str, Any], endpoint: str) -> TransactionHandle:
    expect_ok(body, "start transaction failed")
    return TransactionHandle(int(body["txnIdPT"]), int(body.get("txnIdCounter") or 0), endpoint)


def parse_query(body: Mapping[str, Any]) -> Result:
    schema = read_schema(body.get("columns"))
    rows_raw = body.get("rows")
    rows = (
        [decode_row(schema, row) for row in rows_raw]
        if isinstance(rows_raw, list) and schema
        else []
    )
    return Result(schema, rows, len(rows))


def parse_non_query(body: Mapping[str, Any]) -> Result:
    """A write's row count, and the rows of its RETURNING list when it has one."""
    rowcount = int(body.get("rows") or 0)
    if isinstance(body.get("columns"), list):
        schema = read_schema(body["columns"])
        raw = body.get("returningRows")
        rows = [decode_row(schema, row) for row in raw] if isinstance(raw, list) else []
        return Result(schema, rows, rowcount)
    return Result(None, [], rowcount)


def parse_login(body: Mapping[str, Any]) -> tuple[str, float | None]:
    """The token and its remaining lifetime in seconds, if the server reported one."""
    token = body.get("token")
    if body.get("status") != "ok" or not token:
        raise error_for_code(
            body.get("code") or codes.AUTHENTICATION_FAILED,
            sanitize(body.get("message")) or "authentication failed",
        )
    expires_in = body.get("expiresInSeconds")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return str(token), float(expires_in)
    expires_at = body.get("expiresAtUnixMs")
    if isinstance(expires_at, (int, float)):
        remaining = expires_at / 1000.0 - time.time()
        if remaining > 0:
            return str(token), remaining
    return str(token), None


def quote_identifier(name: str) -> str:
    """Delimit a database name for SQL text.

    The server trims the backticks and does not decode a doubled one, so a name that holds a
    backtick cannot be written safely, and it is refused.
    """
    if not name or "`" in name:
        raise ProgrammingError(f"{name!r} cannot be used as a database name", codes.INVALID_INPUT)
    return f"`{name}`"


def create_branch_sql(name: str, source: str, if_not_exists: bool) -> str:
    condition = "IF NOT EXISTS " if if_not_exists else ""
    return f"CREATE DATABASE {condition}{quote_identifier(name)} BRANCH FROM {quote_identifier(source)}"


def show_branches_sql(name: str) -> str:
    return f"SHOW BRANCHES FROM {quote_identifier(name)}"


def show_ancestors_sql(name: str) -> str:
    return f"SHOW ANCESTORS FROM {quote_identifier(name)}"


def branches_from(result: Result) -> list[Branch]:
    """The rows of SHOW BRANCHES or SHOW ANCESTORS. An ancestor row has no parent column."""
    names = [column.name for column in result.schema or []]
    branches = []
    for values in result.rows:
        row = dict(zip(names, values, strict=False))
        branches.append(
            Branch(
                database=row.get("database"),
                id=row.get("id"),
                depth=int(row.get("depth") or 0),
                parent=row.get("parent"),
                fork_timestamp=row.get("fork_timestamp"),
            )
        )
    return branches


class StreamDecoder:
    """Decodes the NDJSON stream of the streaming query route, one line at a time.

    The first line is a header object with the schema. Each later line is a positional row array,
    and the last line is a trailer object. A trailer with ``"status": "failed"`` is an error that
    surfaced after rows began to arrive.
    """

    def __init__(self) -> None:
        self.schema: list[Column] | None = None
        self.finished = False

    def header(self, line: bytes) -> list[Column]:
        meta = json.loads(line)
        if not isinstance(meta, dict):
            raise InterfaceError("the streaming response did not start with a schema header")
        self._raise_if_failed(meta)
        self.schema = read_schema(meta.get("columns"))
        return self.schema

    def row(self, line: bytes) -> tuple[Any, ...] | None:
        """The decoded row, or None for the trailer."""
        value = json.loads(line)
        if isinstance(value, list):
            assert self.schema is not None
            return decode_row(self.schema, value)
        self.finished = True
        if isinstance(value, dict):
            self._raise_if_failed(value)
        return None

    @staticmethod
    def _raise_if_failed(meta: Mapping[str, Any]) -> None:
        if meta.get("status") == "failed":
            raise error_for_code(meta.get("code"), sanitize(meta.get("message")))


def description(schema: list[Column] | None) -> tuple[tuple[Any, ...], ...] | None:
    """The PEP 249 ``description`` of a result: name, type_code and five unknown fields."""
    if schema is None:
        return None
    return tuple(
        (column.name, ColumnType(column.type), None, None, None, None, None) for column in schema
    )


# --- Error text -----------------------------------------------------------------------------------

_MAX_ERROR_LENGTH = 2048
_SECRET_PREFIXES = ("bearer ", "password=", 'password":', "pwd=", "accesstoken=", 'token":')
_VALUE_ENDS = frozenset(" \"',;})\n\r")


def sanitize(text: Any) -> str:
    """Make server text safe to log: mask secrets, drop control characters, limit the length."""
    if not text:
        return ""
    text = str(text)
    out: list[str] = []
    length = 0
    i, n = 0, len(text)
    lower = text.lower()
    while i < n and length < _MAX_ERROR_LENGTH:
        prefix = next((p for p in _SECRET_PREFIXES if lower.startswith(p, i)), None)
        if prefix is not None:
            out.append(text[i : i + len(prefix)] + "***")
            length += len(prefix) + 3
            i += len(prefix)
            while i < n and text[i] in " \"'":
                i += 1
            while i < n and text[i] not in _VALUE_ENDS:
                i += 1
            continue
        c = text[i]
        # A newline lets the sender forge a second log entry; other control characters can rewrite
        # a terminal. Both become a space.
        out.append(" " if (c < " " or c == "\x7f") else c)
        length += 1
        i += 1
    if i < n:
        out.append("…")
    return "".join(out)


def unreachable(endpoint: str, cause: BaseException) -> Error:
    return error_for_code(
        codes.ENDPOINT_UNREACHABLE, f"{endpoint} is unreachable: {sanitize(cause)}"
    )
