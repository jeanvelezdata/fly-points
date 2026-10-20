"""Thread-safe limiter: minimum spacing between starts plus a concurrency cap."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, min_interval_s: float, max_concurrent: int):
        self.min_interval_s = min_interval_s
        self._slots = threading.Semaphore(max_concurrent)
        self._lock = threading.Lock()
        self._next_start = 0.0

    def __enter__(self) -> "RateLimiter":
        self._slots.acquire()
        with self._lock:
            start = max(time.monotonic(), self._next_start)
            self._next_start = start + self.min_interval_s
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return self

    def __exit__(self, *exc) -> None:
        self._slots.release()
