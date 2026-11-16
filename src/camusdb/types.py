"""CamusDB value types and the PEP 249 type objects and constructors."""

from __future__ import annotations

import datetime as _dt
import decimal
import enum
import itertools
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BINARY",
    "DATETIME",
    "NUMBER",
    "ROWID",
    "STRING",
    "Binary",
    "ColumnType",
    "Date",
    "DateFromTicks",
    "Interval",
    "IsolationLevel",
    "Locking",
    "ObjectId",
    "Time",
    "TimeFromTicks",
    "Timestamp",
    "TimestampFromTicks",
    "Typed",
]


class ColumnType(enum.IntEnum):
    """The type of a CamusDB value. The numbers are the server's wire contract.

    It is also the ``type_code`` of each column in :attr:`Cursor.description`.
    """

    NULL = 0
    ID = 1
    INT64 = 2
    STRING = 3
    BOOL = 4
    FLOAT64 = 5
    FLOAT32 = 6
    BYTES = 7
    DATE = 8
    DATETIME = 9
    ARRAY = 10
    UUID = 11
    NUMERIC = 12
    JSONB = 13
    """A JSON document (SQL ``JSONB``, alias ``JSON``). A result cell is its text: see docs/queries.md."""


class IsolationLevel(str, enum.Enum):
    """The isolation level of a transaction. The server default is :attr:`SERIALIZABLE`."""

    SERIALIZABLE = "Serializable"
    READ_COMMITTED = "ReadCommitted"


class Locking(str, enum.Enum):
    """How a transaction protects the rows it writes. The server default is :attr:`PESSIMISTIC`.

    ``OPTIMISTIC`` takes no write locks. The server checks for conflicts at commit, and the loser
    gets a :class:`camusdb.SerializationFailure`.
    """

    PESSIMISTIC = "Pessimistic"
    OPTIMISTIC = "Optimistic"


@dataclass(frozen=True)
class Typed:
    """A parameter value with an explicit CamusDB type.

    Most values need no wrapper: the driver takes the type from the Python type (see docs/queries.md).
    Use ``Typed`` for a type that Python cannot express, or to make an empty array typed::

        cur.execute("INSERT INTO m (v) VALUES (@v)", {"v": Typed(0.5, ColumnType.FLOAT32)})
        cur.execute("INSERT INTO m (tags) VALUES (@tags)",
                    {"tags": Typed([], ColumnType.ARRAY, element_type=ColumnType.INT64)})
    """

    value: Any
    type: ColumnType
    element_type: ColumnType | None = None


_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

_MICROS_PER_SECOND = 1_000_000
_MICROS_PER_MINUTE = 60 * _MICROS_PER_SECOND
_MICROS_PER_HOUR = 60 * _MICROS_PER_MINUTE
_MICROS_PER_DAY = 24 * _MICROS_PER_HOUR

# The precision of the interval parser. A part out of range fails the range check, and is not rounded.
_INTERVAL_CONTEXT = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Interval:
    """One SQL ``interval`` value: a number of months, a number of days and a number of microseconds.

    The server keeps the three parts separate, as PostgreSQL does, because a month and a day have no
    fixed length when they are added to a date.

    **On the wire.** An interval is a value of an expression only: no table column can have this
    type. A query result sends it as its ISO-8601 text (``P1Y2M3DT4H5M6.5S``) in a ``STRING`` column,
    so the cell reads as a ``str``. Use :meth:`parse` to read it. A parameter is text too, so the SQL
    must cast it: ``CAST(@p AS interval)``. An ``Interval`` or a :class:`datetime.timedelta`
    parameter binds as this text.

    **timedelta.** A ``timedelta`` has days and a time, and no months. So :meth:`to_timedelta`
    refuses an interval with a month part. The result of ``age(a, b)`` can have one.

    **Equality.** Two values are equal when their three parts are equal. The server compares two
    intervals by one total, where a month is 30 days and a day is 24 hours, so on the server
    ``INTERVAL 'P1M' = INTERVAL 'P30D'`` is true. Here the two values are not equal.
    """

    months: int = 0
    days: int = 0
    microseconds: int = 0

    def __post_init__(self) -> None:
        for name, low, high in (
            ("months", _INT32_MIN, _INT32_MAX),
            ("days", _INT32_MIN, _INT32_MAX),
            ("microseconds", _INT64_MIN, _INT64_MAX),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"the {name} of an Interval must be an int")
            if not low <= value <= high:
                raise OverflowError(f"the {name} of an Interval must be in [{low}, {high}]")

    @classmethod
    def from_timedelta(cls, value: _dt.timedelta) -> Interval:
        """The interval of a ``timedelta``: its whole days in the days part, and the rest in the time
        part. The two parts have the sign of the value, so ``-36 hours`` is ``P-1DT-12H``.
        """
        total = value // _dt.timedelta(microseconds=1)
        days, micros = divmod(abs(total), _MICROS_PER_DAY)
        sign = -1 if total < 0 else 1
        return cls(0, sign * days, sign * micros)

    def to_timedelta(self) -> _dt.timedelta:
        """The ``timedelta`` of this interval: the days times 24 hours, plus the time part.

        A ``ValueError`` comes when the interval has a month part, which a ``timedelta`` cannot hold.
        An ``OverflowError`` comes when the value is outside the range of a ``timedelta``.
        """
        if self.months != 0:
            raise ValueError(
                f"the interval {self} has a month part, and a timedelta has no months; keep the "
                "value as an Interval, or remove the months in SQL with justify_days or a datetime "
                "difference"
            )
        return _dt.timedelta(days=self.days, microseconds=self.microseconds)

    @classmethod
    def parse(cls, text: str) -> Interval:
        """Read the ISO-8601 text of an interval, the form the server sends: ``P1Y2M3DT4H5M6.5S``,
        ``P-1DT2H``, ``PT0S``.

        Each number can have its own sign. Only the seconds can have a fraction, which rounds to a
        whole microsecond, to even on a tie. A ``ValueError`` comes when the text is not an ISO-8601
        interval, and an ``OverflowError`` when a part is outside its range.
        """
        if not isinstance(text, str):
            raise TypeError(f"expected a str, got {type(text).__name__}")
        parts = _parse_interval(text.strip())
        if parts is None:
            raise ValueError(f"{text!r} is not an ISO-8601 interval, such as P1DT2H or PT0S")
        months, days, micros = parts
        if not (
            _INT32_MIN <= months <= _INT32_MAX
            and _INT32_MIN <= days <= _INT32_MAX
            and _INT64_MIN <= micros <= _INT64_MAX
        ):
            raise OverflowError(f"the interval {text!r} is outside the range of an interval")
        return cls(months, days, micros)

    def __str__(self) -> str:
        """The ISO-8601 text of this interval, in the form the server writes.

        The years and the months share the sign of the months part, and the hours, minutes and
        seconds share the sign of the time part. A zero interval is ``PT0S``.
        """
        if self.months == 0 and self.days == 0 and self.microseconds == 0:
            return "PT0S"
        out = ["P"]
        years, months = _divmod_toward_zero(self.months, 12)
        if years:
            out.append(f"{years}Y")
        if months:
            out.append(f"{months}M")
        if self.days:
            out.append(f"{self.days}D")
        if self.microseconds:
            sign = "-" if self.microseconds < 0 else ""
            hours, rest = divmod(abs(self.microseconds), _MICROS_PER_HOUR)
            minutes, rest = divmod(rest, _MICROS_PER_MINUTE)
            seconds, fraction = divmod(rest, _MICROS_PER_SECOND)
            out.append("T")
            if hours:
                out.append(f"{sign}{hours}H")
            if minutes:
                out.append(f"{sign}{minutes}M")
            if seconds or fraction:
                out.append(f"{sign}{seconds}")
                if fraction:
                    out.append("." + f"{fraction:06d}".rstrip("0"))
                out.append("S")
        return "".join(out)


def _divmod_toward_zero(value: int, divisor: int) -> tuple[int, int]:
    quotient, remainder = divmod(abs(value), divisor)
    return (-quotient, -remainder) if value < 0 else (quotient, remainder)


_ISO_DIGITS = frozenset("0123456789.")


def _parse_interval(text: str) -> tuple[int, int, int] | None:
    """The months, the days and the microseconds of ISO-8601 interval text, or ``None``."""
    if len(text) < 2 or text[0] not in "Pp":
        return None
    months = days = micros = decimal.Decimal(0)
    time_part = any_part = any_time = False
    i, n = 1, len(text)
    with decimal.localcontext(_INTERVAL_CONTEXT):
        while i < n:
            if text[i] in "Tt":
                if time_part:
                    return None
                time_part = True
                i += 1
                continue
            start = i
            if text[i] in "+-":
                i += 1
            while i < n and text[i] in _ISO_DIGITS:
                i += 1
            if i >= n:
                return None
            try:
                number = decimal.Decimal(text[start:i])
            except decimal.InvalidOperation:
                return None
            designator = text[i].upper()
            i += 1
            whole = number == number.to_integral_value()
            if not time_part and whole and designator == "Y":
                months += number * 12
            elif not time_part and whole and designator == "M":
                months += number
            elif not time_part and whole and designator == "W":
                days += number * 7
            elif not time_part and whole and designator == "D":
                days += number
            elif time_part and whole and designator == "H":
                micros += number * _MICROS_PER_HOUR
            elif time_part and whole and designator == "M":
                micros += number * _MICROS_PER_MINUTE
            elif time_part and designator == "S":
                micros += number * _MICROS_PER_SECOND
            else:
                return None
            any_part = True
            any_time = any_time or time_part
        # A "T" must have at least one time designator after it.
        if not any_part or (time_part and not any_time):
            return None
        rounded = micros.to_integral_value(rounding=decimal.ROUND_HALF_EVEN)
    return int(months), int(days), int(rounded)


_OBJECT_ID = re.compile(r"[0-9a-fA-F]{24}")


class ObjectId:
    """A CamusDB object id (the ``OID`` column type): 12 bytes, written as 24 hex digits.

    The layout is a 4-byte timestamp, a 5-byte value that is random for each process, and a 3-byte
    counter. :meth:`generate` makes a new one on the client, as ``GEN_ID()`` does on the server.
    """

    __slots__ = ("_hex",)

    _lock = threading.Lock()
    _process: bytes | None = None
    _pid: int | None = None
    _counter = itertools.count(int.from_bytes(os.urandom(3), "big"))

    def __init__(self, value: str | bytes | ObjectId) -> None:
        if isinstance(value, ObjectId):
            self._hex: str = value._hex
        elif isinstance(value, bytes):
            if len(value) != 12:
                raise ValueError("an ObjectId is 12 bytes")
            self._hex = value.hex()
        elif isinstance(value, str) and _OBJECT_ID.fullmatch(value):
            self._hex = value.lower()
        else:
            raise ValueError(f"{value!r} is not an ObjectId: expected 24 hex digits")

    @classmethod
    def generate(cls) -> ObjectId:
        """Return a new, unique ObjectId."""
        with cls._lock:
            # A forked child must not repeat the parent's sequence.
            if cls._pid != os.getpid():
                cls._process = os.urandom(5)
                cls._pid = os.getpid()
            process = cls._process
            counter = next(cls._counter) & 0xFFFFFF
        assert process is not None
        raw = int(time.time()).to_bytes(4, "big") + process + counter.to_bytes(3, "big")
        return cls(raw)

    @property
    def generation_time(self) -> _dt.datetime:
        """The time the id was generated, to the second, in UTC."""
        return _dt.datetime.fromtimestamp(int(self._hex[:8], 16), _dt.timezone.utc)

    @property
    def binary(self) -> bytes:
        return bytes.fromhex(self._hex)

    def __str__(self) -> str:
        return self._hex

    def __repr__(self) -> str:
        return f"ObjectId({self._hex!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ObjectId):
            return self._hex == other._hex
        return NotImplemented

    def __lt__(self, other: ObjectId) -> bool:
        return self._hex < other._hex

    def __hash__(self) -> int:
        return hash(self._hex)


# --- PEP 249 type objects -------------------------------------------------------------------------


class _DBAPIType(frozenset[int]):
    """A PEP 249 type object: compares equal to each ``type_code`` in its group."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return other in self
        return frozenset.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return frozenset.__hash__(self)


STRING = _DBAPIType({ColumnType.STRING, ColumnType.ID, ColumnType.JSONB})
BINARY = _DBAPIType({ColumnType.BYTES})
NUMBER = _DBAPIType({ColumnType.INT64, ColumnType.FLOAT64, ColumnType.FLOAT32, ColumnType.NUMERIC})
DATETIME = _DBAPIType({ColumnType.DATE, ColumnType.DATETIME})
ROWID = _DBAPIType({ColumnType.ID})

# --- PEP 249 constructors -------------------------------------------------------------------------

Date = _dt.date
Time = _dt.time
Binary = bytes


def Timestamp(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> _dt.datetime:
    """Return a UTC datetime. CamusDB stores every ``DATETIME`` as an instant in UTC."""
    return _dt.datetime(year, month, day, hour, minute, second, tzinfo=_dt.timezone.utc)


def DateFromTicks(ticks: float) -> _dt.date:
    return _dt.datetime.fromtimestamp(ticks, _dt.timezone.utc).date()


def TimeFromTicks(ticks: float) -> _dt.time:
    return _dt.datetime.fromtimestamp(ticks, _dt.timezone.utc).timetz()


def TimestampFromTicks(ticks: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ticks, _dt.timezone.utc)
