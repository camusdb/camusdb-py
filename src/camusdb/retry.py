"""Which errors a replay of the whole transaction can resolve."""

from __future__ import annotations

from . import codes
from .errors import Error

__all__ = ["is_retryable"]

_RETRYABLE_CODES = frozenset(
    {
        codes.TRANSACTION_CONFLICT,  # lock conflict; aborted when the lock was requested
        codes.TRANSACTION_MUST_RETRY,  # retry budget exhausted; nothing was written
        codes.TRANSACTION_LIFETIME_EXCEEDED,  # held open too long; locks released
    }
)

# Contention that the server reports in the message text, not with its own code: for example many
# databases created at the same time collide on a shared sequence.
_RETRYABLE_MARKERS = ("MustRetry", "AlreadyLocked", "commit returned Aborted")


def is_retryable(exc: BaseException | None) -> bool:
    """Whether a replay of the whole transaction, from its start, can succeed after ``exc``.

    ``CADB0509`` (a commit whose outcome is not resolved) is never retryable this way: the commit can
    already be durable, so a replay could apply it twice. :meth:`Connection.commit` sends that same
    commit again on its own.

    The causes of ``exc`` (``__cause__`` and ``__context__``) are examined too.
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Error):
            if current.code == codes.TRANSACTION_FINALIZE_UNRESOLVED:
                return False
            if current.code in _RETRYABLE_CODES:
                return True
            if any(marker in current.message for marker in _RETRYABLE_MARKERS):
                return True
        current = current.__cause__ or current.__context__
    return False
