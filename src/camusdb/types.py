"""CamusDB value types and the PEP 249 type objects and constructors."""

from __future__ import annotations

import datetime as _dt
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

    Most values need no wrapper: the driver takes the type from the Python type (see the README).
    Use ``Typed`` for a type that Python cannot express, or to make an empty array typed::

        cur.execute("INSERT INTO m (v) VALUES (@v)", {"v": Typed(0.5, ColumnType.FLOAT32)})
        cur.execute("INSERT INTO m (tags) VALUES (@tags)",
                    {"tags": Typed([], ColumnType.ARRAY, element_type=ColumnType.INT64)})
    """

    value: Any
    type: ColumnType
    element_type: ColumnType | None = None


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


STRING = _DBAPIType({ColumnType.STRING, ColumnType.ID})
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
