"""Phase 14E — TTL+LRU cache for sheet read results.

OPT-IN via env SHEETS_READ_CACHE=1. When disabled (default), `get()` returns
None and `set()` is a no-op — zero overhead in the normal hot path.

Why opt-in: agent workflows mix reads with writes. After a `write_range` /
`write_and_verify`, a cached read could return stale data and the agent
wouldn't notice. The user must explicitly opt in when they know the workload
is read-heavy (e.g. monthly P&L reconciliation across 50 books).

Invalidation:
  - TTL: 300s default
  - LRU: bounded at 500 entries; oldest evicted
  - Explicit: `cache_invalidate(spreadsheet_id)` clears all entries for a book
    (called automatically by `sheets.write_and_verify` when cache is enabled)
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any

DEFAULT_TTL_S = 300.0
DEFAULT_MAX_ENTRIES = 500
ENV_VAR = "SHEETS_READ_CACHE"


class SheetReadCache:
    """OrderedDict-backed LRU + per-entry TTL. Thread-safe."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._store: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._enabled = os.environ.get(ENV_VAR, "") == "1"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        """Force-enable (used by tests; production opts in via env)."""
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def get(self, key: tuple) -> Any | None:
        """Return cached value or None on miss / expired / disabled."""
        if not self._enabled:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                # Expired — drop and miss
                self._store.pop(key, None)
                return None
            # LRU: move to most-recent end
            self._store.move_to_end(key)
            return value

    def set(self, key: tuple, value: Any) -> None:
        """Cache `value` under `key`. No-op when disabled."""
        if not self._enabled:
            return
        with self._lock:
            self._store[key] = (time.time() + self._ttl_s, value)
            self._store.move_to_end(key)
            # Evict oldest if over capacity
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def invalidate_spreadsheet(self, spreadsheet_id: str) -> int:
        """Drop ALL entries for a given spreadsheet. Returns count dropped.

        Keys are tuples of (account, spreadsheet_id, range, formatted) —
        spreadsheet_id is at index 1.
        """
        with self._lock:
            to_drop = [k for k in self._store if len(k) >= 2 and k[1] == spreadsheet_id]
            for k in to_drop:
                self._store.pop(k, None)
            return len(to_drop)

    def clear(self) -> int:
        """Drop everything. Returns count dropped."""
        with self._lock:
            n = len(self._store)
            self._store.clear()
            return n

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton — tools import and use CACHE directly
CACHE = SheetReadCache()


def make_key(
    account: str,
    spreadsheet_id: str,
    range_: str,
    formatted: bool,
) -> tuple:
    """Compose the cache key from a read_range call's identifying args."""
    return (account, spreadsheet_id, range_, bool(formatted))


def invalidate(spreadsheet_id: str) -> int:
    """Public invalidation hook — called from write_and_verify, etc."""
    return CACHE.invalidate_spreadsheet(spreadsheet_id)
