"""The endpoint pool: round-robin selection, with a pause for an endpoint that is unreachable."""

from __future__ import annotations

import itertools
import threading
import time
from typing import ClassVar

QUARANTINE_SECONDS = 30.0


class EndpointPool:
    """Gives endpoints in rotation, and skips an endpoint for 30 s after it fails to answer.

    One pool exists for each set of endpoints in the process, so the rotation and the health are
    shared by every connection to the same deployment.
    """

    _registry: ClassVar[dict[tuple[str, ...], EndpointPool]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.endpoints = endpoints
        self._rotation = itertools.count()
        self._quarantined: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def for_endpoints(cls, endpoints: tuple[str, ...]) -> EndpointPool:
        with cls._registry_lock:
            pool = cls._registry.get(endpoints)
            if pool is None:
                pool = cls._registry[endpoints] = cls(endpoints)
            return pool

    def next(self) -> str:
        """The next endpoint in rotation that is not paused, or the next one if every one is paused."""
        count = len(self.endpoints)
        now = time.monotonic()
        with self._lock:
            start = next(self._rotation)
            for offset in range(count):
                endpoint = self.endpoints[(start + offset) % count]
                until = self._quarantined.get(endpoint)
                if until is None or until <= now:
                    self._quarantined.pop(endpoint, None)
                    return endpoint
        # Every endpoint is paused. A request to one of them is the only way to learn it is back.
        return self.endpoints[start % count]

    def mark_unreachable(self, endpoint: str) -> None:
        with self._lock:
            self._quarantined[endpoint] = time.monotonic() + QUARANTINE_SECONDS

    def is_quarantined(self, endpoint: str) -> bool:
        with self._lock:
            until = self._quarantined.get(endpoint)
            return until is not None and until > time.monotonic()
