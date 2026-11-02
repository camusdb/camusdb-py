"""JSONB documents: the text that binds a parameter, and the text form of a result cell.

A ``JSONB`` cell (SQL alias ``JSON``) holds one JSON document. It travels as JSON text. The driver
gives each cell in the PostgreSQL ``jsonb_out`` text form: one space after each ``:`` and each ``,``,
keys in the stored order, and no other white space, for example ``{"a": [1, 2.50], "b": null}``.

A cell is a ``str`` and not a parsed ``dict``, because JSON ``null`` and SQL ``NULL`` are different
values. A document that is JSON ``null`` reads as the text ``"null"``, and SQL ``NULL`` reads as
``None``. Parse the text with :func:`json.loads`, and give it ``parse_float=decimal.Decimal`` when
exact numbers are necessary.
"""

from __future__ import annotations

import decimal
import json
import math
from collections.abc import Mapping
from typing import Any

# The deepest document the server stores (JsonNestingTooDeep, CADB0419).
MAX_NESTING_DEPTH = 512


class _Number(str):
    """The text of a JSON number token, kept as it was written, so every digit and the scale stay."""


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def format_jsonb(text: str | bytes) -> str:
    """Return the PostgreSQL text form of one JSON document.

    Keys keep their order, and numbers keep their text. A ``ValueError`` comes when ``text`` is not
    exactly one JSON value.
    """
    value = json.loads(
        text, parse_float=_Number, parse_int=_Number, parse_constant=_refuse_constant
    )
    return to_text(value)


def to_text(value: Any) -> str:
    """The PostgreSQL text form of a Python value that holds a JSON document.

    The value can be a ``dict`` with ``str`` keys, a ``list``, a ``tuple``, a ``str``, an ``int``, a
    finite ``float``, a ``Decimal``, a ``bool`` or ``None``. A ``TypeError`` or a ``ValueError`` comes
    for a value that JSON cannot hold, and a ``ValueError`` for a document nested more than
    ``MAX_NESTING_DEPTH`` levels deep.
    """
    out: list[str] = []
    _append(value, out, 0)
    return "".join(out)


def _append(value: Any, out: list[str], depth: int) -> None:
    # bool before int: a bool is an int in Python. _Number before str: a _Number is a str.
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, _Number):
        out.append(str.__str__(value))
    elif isinstance(value, str):
        # The PostgreSQL escape_json rules: a quote, a backslash and each control character are
        # escaped, and every other character stays as it is. json.dumps follows the same rules.
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        out.append(int.__repr__(value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} is not a finite number, which JSON cannot hold")
        out.append(float.__repr__(value))
    elif isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError(f"{value} is not a finite number, which JSON cannot hold")
        out.append(str(value))
    elif isinstance(value, (Mapping, list, tuple)) and depth >= MAX_NESTING_DEPTH:
        # The server stores no deeper document. The bound also keeps the recursion of this function
        # far from the interpreter's limit, for a value from an application or from a response.
        raise ValueError(f"the document is nested more than {MAX_NESTING_DEPTH} levels deep")
    elif isinstance(value, Mapping):
        out.append("{")
        for i, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise TypeError(f"a JSON object key must be a str, not {type(key).__name__}")
            if i:
                out.append(", ")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(": ")
            _append(item, out, depth + 1)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(", ")
            _append(item, out, depth + 1)
        out.append("]")
    else:
        raise TypeError(f"{type(value).__name__} cannot be held in a JSON document")


def from_parameter(value: Any) -> str:
    """The JSON text that binds a parameter as JSONB.

    A ``str`` is sent as it is, and the server parses it: text that is not JSON fails with
    ``CADB0418``. ``bytes`` are UTF-8 JSON text. Any other value is written as a document.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8")
    return to_text(value)
