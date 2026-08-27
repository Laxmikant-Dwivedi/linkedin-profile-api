from cachetools import TTLCache
from threading import Lock
from typing import Any, Optional


class ProfileCache:
    """Simple process-local TTL cache keyed by public identifier.

    Avoids re-hitting LinkedIn (and burning rate-limit budget) for repeat
    lookups of the same profile within the TTL window. Not shared across
    multiple instances of the service — fine for a single-dyno deployment,
    a real deployment with multiple workers would want Redis instead.
    """

    def __init__(self, max_size: int, ttl_seconds: int):
        self._cache: TTLCache = TTLCache(maxsize=max_size, ttl=ttl_seconds)
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            return self._cache.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = value
