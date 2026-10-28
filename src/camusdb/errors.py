"""The PEP 249 exception hierarchy, carrying the server's ``CADBxxxx`` codes.

Every exception the driver raises is an :class:`Error`. A server failure keeps the server's code in
:attr:`Error.code`, so a caller can match on the code as well as on the class::

    try:
        cur.execute("INSERT INTO robots (id, name) VALUES (@id, @name)", row)
    except camusdb.IntegrityError as exc:
        if exc.code == codes.DUPLICATE_PRIMARY_KEY:
            ...
"""

from __future__ import annotations

from . import codes

__all__ = [
    "DataError",
    "DatabaseError",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "SerializationFailure",
    "Warning",
    "error_for_code",
]


class Warning(Exception):
    """An important warning, such as data truncation on insert."""


class Error(Exception):
    """The base class of every error the driver raises.

    ``code`` is the server's ``CADBxxxx`` code, or a client code (``CADB0000`` for a failure that
    has no code of its own). ``message`` is the text without the code.
    """

    def __init__(self, message: str = "", code: str = codes.UNKNOWN) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class InterfaceError(Error):
    """An error in the driver itself or in how it is used, not in the database."""


class DatabaseError(Error):
    """An error the database reported."""


class DataError(DatabaseError):
    """A value is out of range, too long, or cannot be converted."""


class OperationalError(DatabaseError):
    """A failure of the database's operation: an unreachable node, a lost transaction, a refused login."""


class SerializationFailure(OperationalError):
    """A transaction conflict. Replay the whole transaction from its start to recover.

    Raised for ``CADB0502`` (lock conflict), ``CADB0504`` (retry budget exhausted, nothing written)
    and ``CADB0505`` (transaction lifetime exceeded). See :func:`camusdb.is_retryable`.
    """


class IntegrityError(DatabaseError):
    """A constraint failed: a duplicate key, a NOT NULL, a CHECK or a foreign key."""


class InternalError(DatabaseError):
    """The database is in a state it should not be in, such as corrupt storage."""


class ProgrammingError(DatabaseError):
    """The statement is wrong: bad syntax, an unknown table or column, a missing privilege."""


class NotSupportedError(DatabaseError):
    """The database or the driver does not support the requested feature."""


_BY_CODE: dict[str, type[Error]] = {
    # Connectivity and authentication.
    codes.ENDPOINT_UNREACHABLE: OperationalError,
    codes.AUTHENTICATION_FAILED: OperationalError,
    codes.TOO_MANY_AUTH_ATTEMPTS: OperationalError,
    codes.INSECURE_TRANSPORT: InterfaceError,
    codes.INSUFFICIENT_PRIVILEGE: ProgrammingError,
    # Transactions.
    codes.TRANSACTION_CONFLICT: SerializationFailure,
    codes.TRANSACTION_MUST_RETRY: SerializationFailure,
    codes.TRANSACTION_LIFETIME_EXCEEDED: SerializationFailure,
    codes.TRANSACTION_FINALIZE_UNRESOLVED: OperationalError,
    codes.TRANSACTION_ALREADY_COMPLETED: InternalError,
    codes.STATEMENT_NOT_ALLOWED_IN_TRANSACTION: ProgrammingError,
    # Values.
    codes.VALUE_TOO_LONG: DataError,
    codes.NUMERIC_VALUE_OUT_OF_RANGE: DataError,
    codes.MALFORMED_VECTOR: DataError,
    codes.VECTOR_DIMENSION_MISMATCH: DataError,
    codes.INVALID_VECTOR_VALUE: DataError,
    codes.DUPLICATE_PRIMARY_KEY: IntegrityError,
    # Storage the server cannot trust, and failures the server did not expect.
    codes.SERVER_INTERNAL_ERROR: InternalError,
    codes.SYSTEM_SPACE_CORRUPT: InternalError,
    codes.TABLE_CORRUPT: InternalError,
    "CADB0096": InternalError,
    "CADB0097": InternalError,
    "CADB0098": InternalError,
    "CADB0099": InternalError,
    codes.LARGE_VALUE_CORRUPT: InternalError,
    codes.LARGE_VALUE_NOT_RESOLVED: InternalError,
    codes.FEATURE_NOT_SUPPORTED: NotSupportedError,
}

# The class for a code that the table above does not name, chosen by the code's family.
_BY_FAMILY: dict[str, type[Error]] = {
    "CADB00": ProgrammingError,  # catalog: unknown database, table or index
    "CADB03": IntegrityError,  # constraints
    "CADB04": ProgrammingError,  # input, syntax and schema definition
    "CADB05": OperationalError,  # transactions, sessions and server resources
    "CADB06": OperationalError,  # configuration
    "CADB07": OperationalError,  # backups
}


def error_for_code(code: str | None, message: str = "") -> Error:
    """Return the exception for a server ``code`` and ``message``.

    The class follows PEP 249: a constraint failure is an :class:`IntegrityError`, a statement error is
    a :class:`ProgrammingError`, and so on. A code that is not known falls back to its family, then to
    :class:`DatabaseError`.
    """
    code = code or codes.UNKNOWN
    cls = _BY_CODE.get(code) or _BY_FAMILY.get(code[:6]) or DatabaseError
    if code == codes.UNKNOWN:
        cls = DatabaseError
    return cls(message, code)
