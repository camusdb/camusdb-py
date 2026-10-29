"""CamusDB driver for Python: PEP 249 (DB-API 2.0), with an asyncio interface in :mod:`camusdb.aio`.

::

    import camusdb

    with camusdb.connect("Endpoint=http://localhost:5095;Database=test") as conn:
        cur = conn.execute("SELECT name, year FROM robots WHERE year > @year", {"year": 1980})
        for name, year in cur:
            print(name, year)
"""

from . import codes, rows
from ._admin import (
    BackupGcDeletion,
    BackupGcOrphan,
    BackupGcResult,
    BackupInfo,
    CpuStats,
    DiskVolumeStats,
    MemoryStats,
    NodeIdentity,
    NodeStats,
    NodeStatsError,
)
from ._config import redact as redact_connection_string
from ._protocol import Branch
from .connection import Backups, Connection, Cursor, connect, run_transaction
from .errors import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    SerializationFailure,
    Warning,
)
from .retry import is_retryable
from .types import (
    BINARY,
    DATETIME,
    NUMBER,
    ROWID,
    STRING,
    Binary,
    ColumnType,
    Date,
    DateFromTicks,
    IsolationLevel,
    Locking,
    ObjectId,
    Time,
    TimeFromTicks,
    Timestamp,
    TimestampFromTicks,
    Typed,
)

__version__ = "0.1.0"

# PEP 249 module globals.
apilevel = "2.0"
# Threads may share the module, but not connections.
threadsafety = 1
# Placeholders are named, and they are written with "@", as the server reads them: WHERE id = @id.
paramstyle = "named"

__all__ = [
    "BINARY",
    "DATETIME",
    "NUMBER",
    "ROWID",
    "STRING",
    "BackupGcDeletion",
    "BackupGcOrphan",
    "BackupGcResult",
    "BackupInfo",
    "Backups",
    "Binary",
    "Branch",
    "ColumnType",
    "Connection",
    "CpuStats",
    "Cursor",
    "DataError",
    "DatabaseError",
    "Date",
    "DateFromTicks",
    "DiskVolumeStats",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "IsolationLevel",
    "Locking",
    "MemoryStats",
    "NodeIdentity",
    "NodeStats",
    "NodeStatsError",
    "NotSupportedError",
    "ObjectId",
    "OperationalError",
    "ProgrammingError",
    "SerializationFailure",
    "Time",
    "TimeFromTicks",
    "Timestamp",
    "TimestampFromTicks",
    "Typed",
    "Warning",
    "apilevel",
    "codes",
    "connect",
    "is_retryable",
    "paramstyle",
    "redact_connection_string",
    "rows",
    "run_transaction",
    "threadsafety",
]
