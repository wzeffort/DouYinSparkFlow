from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic

from spark_console.models import utc_now


class BoundedRequestLimiter:
    """Atomic admission before expensive work; full key space fails closed."""

    def __init__(self, limit=30, window_seconds=600, max_keys=2048, now=monotonic):
        self.limit, self.window, self.max_keys, self.now = limit, window_seconds, max_keys, now
        self._entries = {}
        self._lock = Lock()

    def allow(self, key):
        with self._lock:
            now = self.now()
            expired = [k for k, (_, until) in self._entries.items() if until <= now]
            for k in expired:
                del self._entries[k]
            if key not in self._entries:
                if len(self._entries) >= self.max_keys:
                    return False
                self._entries[key] = (0, now + self.window)
            count, until = self._entries[key]
            if count >= self.limit:
                return False
            self._entries[key] = (count + 1, until)
            return True


class FailedAttemptLimiter:
    def __init__(
        self,
        limit: int = 10,
        window: timedelta = timedelta(minutes=10),
        now=utc_now,
    ):
        self.limit = limit
        self.window = window
        self.now = now
        self.attempts: dict[str, deque[datetime]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[datetime]:
        values = self.attempts[key]
        cutoff = self.now() - self.window
        while values and values[0] <= cutoff:
            values.popleft()
        return values

    def allow(self, key: str) -> bool:
        return len(self._prune(key)) < self.limit

    def record_failure(self, key: str) -> None:
        self._prune(key).append(self.now())

    def clear(self, key: str) -> None:
        self.attempts.pop(key, None)
