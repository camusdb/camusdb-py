"""Statement classification: which REST endpoint runs a SQL statement.

The server has three SQL endpoints, and a statement sent to the wrong one fails with ``CADB0407``:

* the query endpoint runs reads (``SELECT``, ``SHOW``, ``EXPLAIN`` ...) and returns rows,
* the DDL endpoint runs schema changes (``CREATE TABLE``, ``CREATE INDEX`` ...),
* the non-query endpoint runs everything else (``INSERT``, ``UPDATE``, ``CREATE USER``, ``GRANT`` ...).

The scanner reads keywords only. It skips white space, comments, opening parentheses, string
literals and quoted identifiers, so a keyword inside one of these does not count.
"""

from __future__ import annotations

import enum


class StatementKind(enum.Enum):
    QUERY = "query"
    NON_QUERY = "non-query"
    DDL = "ddl"


_QUERY_VERBS = frozenset(
    {"SELECT", "WITH", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "ANALYZE", "VALUES"}
)

_DML_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "CALL"})

# The statements the DDL endpoint runs, as keyword sequences. Kept in step with the .NET driver.
_DDL_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("CREATE", "TABLE"),
    ("DROP", "TABLE"),
    ("ALTER", "TABLE"),
    ("TRUNCATE",),
    ("CREATE", "UNIQUE", "INDEX"),
    ("CREATE", "INDEX"),
    ("DROP", "INDEX"),
    ("CREATE", "VIEW"),
    ("CREATE", "OR", "REPLACE", "VIEW"),
    ("DROP", "VIEW"),
    ("ALTER", "VIEW"),
    ("CREATE", "MATERIALIZED", "VIEW"),
    ("DROP", "MATERIALIZED", "VIEW"),
    ("ALTER", "MATERIALIZED", "VIEW"),
    ("CREATE", "SEQUENCE"),
    ("DROP", "SEQUENCE"),
    ("ALTER", "SEQUENCE"),
    ("CREATE", "TRIGGER"),
    ("DROP", "TRIGGER"),
)


def keywords(sql: str, limit: int | None = None) -> list[str]:
    """Return the bare words of ``sql``, upper-cased, in order, at most ``limit`` of them."""
    words: list[str] = []
    i, n = 0, len(sql)
    while i < n and (limit is None or len(words) < limit):
        c = sql[i]
        if c.isspace() or c in "(),;":
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c in "'\"`":
            i = _skip_quoted(sql, i, c)
        elif c.isalpha() or c == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            words.append(sql[start:i].upper())
        else:
            i += 1
    return words


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    # The server's lexer reads a backslash and the next character as one unit, and a doubled quote
    # as a quote character.
    i, n = start + 1, len(sql)
    while i < n:
        c = sql[i]
        if c == "\\" and quote != "`":
            i += 2
        elif c == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
            else:
                return i + 1
        else:
            i += 1
    return n


def classify(sql: str) -> StatementKind:
    """Return the endpoint kind that runs ``sql``."""
    head = keywords(sql, limit=4)
    if not head:
        return StatementKind.NON_QUERY
    if any(tuple(head[: len(prefix)]) == prefix for prefix in _DDL_PREFIXES):
        return StatementKind.DDL
    if head[0] in _QUERY_VERBS or head[:2] == ["SET", "TRANSACTION"]:
        return StatementKind.QUERY
    return StatementKind.NON_QUERY


def is_dml(sql: str) -> bool:
    head = keywords(sql, limit=1)
    return bool(head) and head[0] in _DML_VERBS


def has_returning(sql: str) -> bool:
    """Whether a write statement has a ``RETURNING`` list."""
    return "RETURNING" in keywords(sql)


def runs_in_own_transaction(sql: str) -> bool:
    """``TRUNCATE`` commits on its own, so the server refuses it inside a transaction."""
    return keywords(sql, limit=1) == ["TRUNCATE"]
