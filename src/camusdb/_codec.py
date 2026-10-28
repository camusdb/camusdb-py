"""Conversion between Python values and the CamusDB JSON wire format.

A parameter travels as a ``ColumnValue`` object: ``{"type": <ColumnType>, ...}`` with the one field
that holds the value for that type. A result arrives as a ``columns`` schema plus positional ``rows``,
and each cell is decoded against the declared type of its column.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import math
import struct
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import codes
from .errors import DataError, InterfaceError, ProgrammingError
from .types import ColumnType, ObjectId, Typed

WireValue = dict[str, Any]

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_UINT64_MASK = 2**64 - 1

# .NET DateTime ticks: 100-nanosecond intervals since 0001-01-01T00:00:00Z.
_TICKS_EPOCH = dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc)
_TICKS_PER_SECOND = 10_000_000

# An element type that the decoder can apply to the elements of an array column.
_ELEMENT_TYPES = frozenset(ColumnType) - {ColumnType.NULL, ColumnType.ARRAY}


# --- Encoding -------------------------------------------------------------------------------------


def encode_parameters(parameters: Mapping[str, Any] | None) -> dict[str, WireValue] | None:
    """Encode bound parameters, keyed by placeholder (``@name``), for a SQL request.

    A key can be written with or without the ``@``.
    """
    if parameters is None:
        return None
    if not isinstance(parameters, Mapping):
        raise ProgrammingError(
            "parameters must be a mapping of placeholder names to values, such as "
            '{"year": 1984} for "... WHERE year = @year"',
            codes.INVALID_INPUT,
        )
    encoded: dict[str, WireValue] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not key.lstrip("@"):
            raise ProgrammingError(f"invalid parameter name {key!r}", codes.INVALID_INPUT)
        name = key if key.startswith("@") else "@" + key
        if name in encoded:
            raise ProgrammingError(
                f"parameter {name!r} is bound more than once", codes.INVALID_INPUT
            )
        encoded[name] = encode_value(name, value)
    return encoded or None


def encode_row(values: Mapping[str, Any]) -> dict[str, WireValue]:
    """Encode a row for the typed insert route, keyed by column name."""
    return {column: encode_value(column, value) for column, value in values.items()}


def encode_value(name: str, value: Any) -> WireValue:
    """Encode one value, with the type taken from ``Typed`` or from the Python type."""
    if isinstance(value, Typed):
        return _encode_as(name, value.value, value.type, value.element_type)
    if value is None:
        return {"type": ColumnType.NULL}
    return _encode_as(name, value, infer_type(name, value), None)


def infer_type(name: str, value: Any) -> ColumnType:
    """The CamusDB type of a Python value that carries no explicit type."""
    # bool before int: a bool is an int in Python.
    if isinstance(value, bool):
        return ColumnType.BOOL
    if isinstance(value, int):
        return ColumnType.INT64
    if isinstance(value, float):
        return ColumnType.FLOAT64
    if isinstance(value, decimal.Decimal):
        return ColumnType.NUMERIC
    if isinstance(value, str):
        return ColumnType.STRING
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ColumnType.BYTES
    # datetime before date: a datetime is a date in Python.
    if isinstance(value, dt.datetime):
        return ColumnType.DATETIME
    if isinstance(value, dt.date):
        return ColumnType.DATE
    if isinstance(value, uuid.UUID):
        return ColumnType.UUID
    if isinstance(value, ObjectId):
        return ColumnType.ID
    if isinstance(value, (list, tuple)):
        return ColumnType.ARRAY
    raise ProgrammingError(
        f"cannot infer the CamusDB type of parameter {name!r} from {type(value).__name__}; "
        "wrap the value in camusdb.Typed(value, ColumnType.X)",
        codes.INVALID_INPUT,
    )


def _encode_as(
    name: str, value: Any, column_type: ColumnType, element_type: ColumnType | None
) -> WireValue:
    if value is None or column_type == ColumnType.NULL:
        return {"type": ColumnType.NULL}

    try:
        match column_type:
            case ColumnType.ID:
                return {"type": ColumnType.ID, "strValue": str(ObjectId(value))}
            case ColumnType.STRING:
                return {
                    "type": ColumnType.STRING,
                    "strValue": value if isinstance(value, str) else str(value),
                }
            case ColumnType.INT64:
                return {"type": ColumnType.INT64, "longValue": _to_int64(name, value)}
            case ColumnType.FLOAT64 | ColumnType.FLOAT32:
                return {"type": column_type, "floatValue": _to_finite_float(name, value)}
            case ColumnType.NUMERIC:
                return {"type": ColumnType.NUMERIC, "strValue": _to_numeric_text(name, value)}
            case ColumnType.BOOL:
                if not isinstance(value, bool):
                    raise TypeError("expected a bool")
                return {"type": ColumnType.BOOL, "boolValue": value}
            case ColumnType.BYTES:
                return {
                    "type": ColumnType.BYTES,
                    "bytesValue": base64.b64encode(bytes(value)).decode("ascii"),
                }
            case ColumnType.DATE:
                return {"type": ColumnType.DATE, "longValue": date_to_ticks(_to_date(value))}
            case ColumnType.DATETIME:
                return {
                    "type": ColumnType.DATETIME,
                    "longValue": datetime_to_ticks(_to_datetime(value)),
                }
            case ColumnType.UUID:
                return {
                    "type": ColumnType.UUID,
                    "strValue": str(value if isinstance(value, uuid.UUID) else uuid.UUID(value)),
                }
            case ColumnType.ARRAY:
                return _encode_array(name, value, element_type)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataError(
            f"cannot bind parameter {name!r} as {column_type.name}: {exc}", codes.INVALID_INPUT
        ) from None
    raise InterfaceError(f"unsupported column type {column_type!r}")


def _encode_array(name: str, value: Any, element_type: ColumnType | None) -> WireValue:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Iterable):
        raise TypeError("an ARRAY value must be a list or a tuple")
    items = list(value)

    if element_type is None:
        first = next((item for item in items if item is not None), None)
        if first is not None:
            element_type = infer_type(name, first)
            # The server has no ARRAY(NUMERIC): a Decimal element travels as a FLOAT64.
            if element_type == ColumnType.NUMERIC:
                element_type = ColumnType.FLOAT64
        elif items:
            raise ProgrammingError(
                f"cannot infer the element type of array parameter {name!r}: every element is None; "
                "use camusdb.Typed(value, ColumnType.ARRAY, element_type=ColumnType.X)",
                codes.INVALID_INPUT,
            )
    if element_type == ColumnType.ARRAY:
        raise TypeError("an array of arrays is not supported")

    elements = [
        {"type": ColumnType.NULL} if item is None else _encode_as(name, item, element_type, None)
        for item in items
        if element_type is not None
    ]
    return {
        "type": ColumnType.ARRAY,
        "arrayElementType": element_type if element_type is not None else ColumnType.NULL,
        "arrayValues": elements,
    }


def _to_int64(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, decimal.Decimal, float, str)):
        raise TypeError(f"expected an int, got {type(value).__name__}")
    number = int(value)
    if number != value and not isinstance(value, str):
        raise ValueError(f"{value!r} is not a whole number")
    if not _INT64_MIN <= number <= _INT64_MAX:
        raise OverflowError(f"{number} is outside the INT64 range")
    return number


def _to_finite_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("expected a float, got bool")
    number = float(value)
    if not math.isfinite(number):
        # JSON has no NaN or infinity.
        raise ValueError(f"{number} is not a finite number")
    return number


def _to_numeric_text(name: str, value: Any) -> str:
    if isinstance(value, bool):
        raise TypeError("expected a Decimal, got bool")
    if isinstance(value, str):
        # A string passes as it is, so a value that no Python type holds still binds exactly.
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} is not a finite number")
        value = decimal.Decimal(repr(value))
    number = decimal.Decimal(value)
    if not number.is_finite():
        raise ValueError(f"{number} is not a finite number")
    return format(number, "f")


def _to_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return _as_utc(value).date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value)
    raise TypeError(f"expected a date, got {type(value).__name__}")


def _to_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        return _as_utc(dt.datetime.fromisoformat(value))
    raise TypeError(f"expected a datetime, got {type(value).__name__}")


def _as_utc(value: dt.datetime) -> dt.datetime:
    # A naive datetime is taken as UTC, which is how the server stores every DATETIME.
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def datetime_to_ticks(value: dt.datetime) -> int:
    delta = _as_utc(value) - _TICKS_EPOCH
    return (delta.days * 86_400 + delta.seconds) * _TICKS_PER_SECOND + delta.microseconds * 10


def date_to_ticks(value: dt.date) -> int:
    return (value.toordinal() - 1) * 86_400 * _TICKS_PER_SECOND


def ticks_to_datetime(ticks: int) -> dt.datetime:
    # Python keeps microseconds; the last digit of the 100 ns ticks is dropped.
    return _TICKS_EPOCH + dt.timedelta(microseconds=ticks // 10)


def ticks_to_date(ticks: int) -> dt.date:
    return dt.date.fromordinal(ticks // (86_400 * _TICKS_PER_SECOND) + 1)


# --- Decoding -------------------------------------------------------------------------------------


class Column:
    """One column of a result schema."""

    __slots__ = ("element_type", "name", "type")

    def __init__(self, name: str, type_: ColumnType, element_type: ColumnType | None) -> None:
        self.name = name
        self.type = type_
        self.element_type = element_type


def read_schema(columns: Any) -> list[Column]:
    """Read the ``columns`` array of a response. An entry the client cannot read is typed NULL."""
    schema: list[Column] = []
    if not isinstance(columns, list):
        return schema
    for column in columns:
        if not isinstance(column, Mapping):
            continue
        schema.append(
            Column(
                str(column.get("name") or ""),
                _column_type(column.get("type")),
                _element_type(column.get("elementType")),
            )
        )
    return schema


def _column_type(raw: Any) -> ColumnType:
    try:
        return ColumnType(raw)
    except ValueError:
        # A type newer than this client: its cells are decoded from their JSON tokens.
        return ColumnType.NULL


def _element_type(raw: Any) -> ColumnType | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        try:
            element = ColumnType(raw)
        except ValueError:
            return None
        return element if element in _ELEMENT_TYPES else None
    return None


def decode_row(schema: Sequence[Column], row: Any) -> tuple[Any, ...]:
    """Decode one positional row. A position the row omits is ``None``."""
    if not isinstance(row, list):
        raise InterfaceError("malformed result row: expected a JSON array")
    values = [
        decode_cell(row[i], column.type, column.element_type) if i < len(row) else None
        for i, column in enumerate(schema)
    ]
    return tuple(values)


def decode_cell(cell: Any, declared: ColumnType, element_type: ColumnType | None = None) -> Any:
    """Decode one cell against the declared type of its column."""
    if cell is None:
        return None
    match declared:
        case ColumnType.ID:
            return str(cell)
        case ColumnType.BYTES:
            return base64.b64decode(cell)
        case ColumnType.DATE:
            return ticks_to_date(int(cell))
        case ColumnType.DATETIME:
            return ticks_to_datetime(int(cell))
        case ColumnType.UUID:
            return _decode_uuid(cell)
        case ColumnType.NUMERIC:
            return _decode_numeric(cell)
        case ColumnType.ARRAY:
            return _decode_array(cell, element_type)
        case _:
            # An expression column can carry a type that is too broad, so a scalar is read from its
            # JSON token. A JOIN can report a UUID column as STRING but still send [high, low].
            if isinstance(cell, list):
                return _decode_uuid(cell)
            return _decode_by_token(cell, declared)


def _decode_by_token(cell: Any, declared: ColumnType) -> Any:
    if isinstance(cell, (bool, str)):
        return cell
    if isinstance(cell, (int, float)):
        if declared == ColumnType.FLOAT64:
            return float(cell)
        if declared == ColumnType.FLOAT32:
            # The server writes the shortest text of the float32. Rounding to float32 gives the stored
            # value, which the gRPC protocol gives too.
            return struct.unpack("f", struct.pack("f", float(cell)))[0]
        return cell
    # An object or a nested array: nothing a scalar column can hold.
    return None


def _decode_numeric(cell: Any) -> decimal.Decimal | Any:
    if isinstance(cell, str):
        return decimal.Decimal(cell)
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, int):
        return decimal.Decimal(cell)
    if isinstance(cell, float):
        return decimal.Decimal(repr(cell))
    return None


def _decode_uuid(cell: Any) -> uuid.UUID | None:
    # A UUID travels as [high, low]: two signed, big-endian 64-bit halves.
    if isinstance(cell, list):
        if len(cell) != 2:
            return None
        high, low = (int(half) & _UINT64_MASK for half in cell)
        return uuid.UUID(int=(high << 64) | low)
    if isinstance(cell, str):
        return uuid.UUID(cell)
    return None


def _decode_array(cell: Any, element_type: ColumnType | None) -> list[Any] | None:
    if not isinstance(cell, list):
        return None
    if element_type is not None:
        return [decode_cell(item, element_type) for item in cell]
    # A server older than the elementType field: the elements are typed from their JSON tokens.
    return [_decode_by_token(item, ColumnType.NULL) for item in cell]
