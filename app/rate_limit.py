import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict


class SlidingWindowRateLimiter:
    """Small in-memory per-client rate limiter (fixed window bucket keyed
    by a caller-supplied id, e.g. API key or IP). Good enough for a single
    process; swap for Redis-backed limiting if you scale to multiple
    instances."""

    def __init__(self, max_requests: int, window_seconds: float):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, client_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[client_id]
            while hits and now - hits[0] > self._window_seconds:
                hits.popleft()
            if len(hits) >= self._max_requests:
                return False
            hits.append(now)
            return True
