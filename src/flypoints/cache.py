"""Small thread-safe SQLite JSON cache with per-entry TTL."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import paths


def make_key(*parts: Any) -> str:
    """Stable hash of JSON-serializable parts."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Cache:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            paths.ensure_state_dir()
            path = paths.STATE_DIR / "cache.sqlite"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                "expires_at REAL NOT NULL, PRIMARY KEY (namespace, key))"
            )

    def get_json(self, namespace: str, key: str) -> Any | None:
        """Cached value, or None if missing or expired."""
        with self._lock:
            row = self._db.execute(
                "SELECT value, expires_at FROM cache WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None or row[1] <= time.time():
            return None
        return json.loads(row[0])

    def put_json(self, namespace: str, key: str, value: Any, ttl_seconds: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (namespace, key, value, expires_at) VALUES (?, ?, ?, ?)",
                (namespace, key, json.dumps(value), time.time() + ttl_seconds),
            )

    def purge_expired(self) -> int:
        """Delete expired entries; returns how many were removed."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM cache WHERE expires_at <= ?", (time.time(),)).rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
