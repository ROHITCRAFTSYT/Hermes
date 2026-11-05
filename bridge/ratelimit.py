"""Per-client sliding-window rate limit for the token-protected endpoints."""

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, per_minute: int, window_s: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = per_minute, window_s, clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next slot frees up."""
        if self.limit <= 0:
            return None
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(0.0, self.window - (now - q[0]))
            q.append(now)
            if len(self._hits) > 4096:   # forget idle clients so memory stays bounded
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                    del self._hits[k]
            return None
