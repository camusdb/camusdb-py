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
from typing import NamedTuple


class StatementKind(enum.Enum):
    QUERY = "query"
    NON_QUERY = "non-query"
    DDL = "ddl"


_QUERY_VERBS = frozenset(
    {"SELECT", "WITH", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "ANALYZE", "VALUES"}
)

# UPSERT INTO is INSERT ... ON CONFLICT (primary key) DO UPDATE, so it is a write like INSERT. Sent as a
# query, it would run without the autocommit options of the connection.
_DML_VERBS = frozenset({"INSERT", "UPSERT", "UPDATE", "DELETE", "CALL"})

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
    return [word.text for word in _words(sql, limit)]


class _Word(NamedTuple):
    text: str
    # The number of parentheses open around the word.
    depth: int
    # True when the word is the first one after an opening parenthesis.
    opens: bool


def _words(sql: str, limit: int | None = None) -> list[_Word]:
    words: list[_Word] = []
    depth = 0
    opens = False
    i, n = 0, len(sql)
    while i < n and (limit is None or len(words) < limit):
        c = sql[i]
        if c == "(":
            depth += 1
            opens = True
            i += 1
        elif c == ")":
            depth = max(depth - 1, 0)
            opens = False
            i += 1
        elif c.isspace():
            i += 1
        elif c in ",;":
            opens = False
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c in "'\"`":
            i = _skip_quoted(sql, i, c)
            opens = False
        elif c.isalpha() or c == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            words.append(_Word(sql[start:i].upper(), depth, opens))
            opens = False
        else:
            opens = False
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
    if head[0] == "WITH" and _with_writes(_words(sql)):
        return StatementKind.NON_QUERY
    if head[0] in _QUERY_VERBS or head[:2] == ["SET", "TRANSACTION"]:
        return StatementKind.QUERY
    return StatementKind.NON_QUERY


def is_dml(sql: str) -> bool:
    words = _words(sql)
    if not words:
        return False
    if words[0].text == "WITH":
        return _with_writes(words)
    return words[0].text in _DML_VERBS


# The words that can begin the main statement after the WITH clause.
_MAIN_VERBS = _DML_VERBS | {"SELECT", "VALUES", "TABLE"}


def _with_writes(words: list[_Word]) -> bool:
    """Whether a statement that begins with ``WITH`` writes.

    It writes when its main statement is a write (``WITH x AS (...) DELETE ...``), or when the body of
    one of its common table expressions is a write (``WITH d AS (DELETE ... RETURNING *) SELECT ...``).
    Such a statement must go to the non-query endpoint and carry the autocommit options, as an
    ``INSERT`` does.
    """
    for k in range(1, len(words)):
        word = words[k]
        if word.depth == 0 and word.text in _MAIN_VERBS:
            # The main statement. Every CTE body is before it.
            return word.text in _DML_VERBS
        previous = words[k - 1]
        if (
            word.depth == 1
            and word.opens
            and previous.depth == 0
            and previous.text in {"AS", "MATERIALIZED"}
            and word.text in _DML_VERBS
        ):
            return True
    return False


def has_returning(sql: str) -> bool:
    """Whether a write statement has a ``RETURNING`` list."""
    return "RETURNING" in keywords(sql)


def runs_in_own_transaction(sql: str) -> bool:
    """``TRUNCATE`` commits on its own, so the server refuses it inside a transaction."""
    return keywords(sql, limit=1) == ["TRUNCATE"]
