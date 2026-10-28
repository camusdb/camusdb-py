"""Row factories: how a cursor builds each row it returns.

A row factory takes the cursor's ``description`` and returns a function that turns the values of one
row into the row object. The default is :func:`tuple_row`, as PEP 249 describes::

    cur = conn.cursor(row_factory=camusdb.rows.dict_row)
    cur.execute("SELECT id, name FROM robots")
    cur.fetchone()  # {'id': '6ae2...', 'name': 'T-800'}
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["RowFactory", "RowMaker", "dict_row", "namedtuple_row", "tuple_row"]

RowMaker = Callable[[tuple[Any, ...]], Any]
RowFactory = Callable[[Sequence[tuple[Any, ...]]], RowMaker]


def tuple_row(description: Sequence[tuple[Any, ...]]) -> RowMaker:
    """Rows are tuples."""
    return _identity


def dict_row(description: Sequence[tuple[Any, ...]]) -> RowMaker:
    """Rows are dicts keyed by column name. A later column wins over an earlier one with its name."""
    names = [column[0] for column in description]
    return lambda values: dict(zip(names, values, strict=False))


def namedtuple_row(description: Sequence[tuple[Any, ...]]) -> RowMaker:
    """Rows are named tuples. A column name that is not an identifier gets a positional name."""
    cls = namedtuple("Row", [column[0] for column in description], rename=True)  # type: ignore[misc]
    return lambda values: cls._make(values)


def _identity(values: tuple[Any, ...]) -> tuple[Any, ...]:
    return values
