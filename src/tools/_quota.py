"""Per-bucket sliding-window quota budgeter (Phase 14F foundations).

Tracks recent API calls across separate buckets (sheets-direct, drive,
gmail) and proactively paces the NEXT call when the window approaches
the user-quota limit. Complements RetryingHttpRequest (`src/auth.py`)
which is reactive (retries 429s AFTER they fire); the budgeter avoids
hitting them in the first place.

Apps Script is intentionally NOT paced: one `apps_script.run_function`
call consumes one user-quota token regardless of how many spreadsheets
the script opens internally. That's the entire point of cross_aggregate.

This module ships in 14A-prep but is not yet wired into `_wrap_for_sdk`
— that integration is task #8 (14F).
"""
from __future__ import annotations

import collections
import threading
import time

# (calls_per_window, window_seconds). None disables pacing.
#   sheets-direct: 60/min/user is the Sheets API limit. 50/55s gives a 10%
#     buffer below it AND survives clock drift between client and Google.
#   apps-script: no pacing (1 token regardless of internal openById count)
#   drive: empirical 429 spike threshold from stress (HANDOFF stage_6
#     showed 63s spike at ~20 file-mutations in a tight burst)
#   gmail: same as sheets-direct (Gmail user quota is similar order)
BUCKETS: dict[str, tuple[int | None, float | None]] = {
    "sheets-direct": (50, 55.0),
    "apps-script":   (None, None),
    "drive":         (20, 60.0),
    "gmail":         (50, 55.0),
}


class QuotaBudgeter:
    """Sliding-window per-bucket pacer. Thread-safe."""

    def __init__(self) -> None:
        self._logs: dict[str, collections.deque[float]] = {
            b: collections.deque() for b in BUCKETS
        }
        self._lock = threading.Lock()

    def acquire(self, bucket: str) -> float:
        """Reserve a quota slot in `bucket`. Sleeps if the window is full.

        Returns paced_ms — the number of milliseconds we waited (0 if no
        pacing was needed, or if the bucket is unconfigured/exempt).
        Logs the call as part of acquire — caller doesn't need a separate
        log step.
        """
        limit, window_s = BUCKETS.get(bucket, (None, None))
        if limit is None or window_s is None:
            return 0.0

        with self._lock:
            now = time.time()
            q = self._logs[bucket]
            while q and q[0] < now - window_s:
                q.popleft()

            if len(q) < limit:
                q.append(now)
                return 0.0

            wait_until = q[0] + window_s
            sleep_s = max(0.0, wait_until - now)

        # Release lock during sleep so other threads can read remaining_pct
        time.sleep(sleep_s)

        with self._lock:
            self._logs[bucket].append(time.time())

        return round(sleep_s * 1000, 1)

    def remaining_pct(self, bucket: str) -> float | None:
        """0.0 (window full) → 1.0 (empty). None for unconfigured/exempt buckets."""
        limit, window_s = BUCKETS.get(bucket, (None, None))
        if limit is None or window_s is None:
            return None
        with self._lock:
            now = time.time()
            q = self._logs[bucket]
            while q and q[0] < now - window_s:
                q.popleft()
            return max(0.0, 1.0 - len(q) / limit)

    def reset(self, bucket: str | None = None) -> None:
        """Clear logs (for tests). If bucket is None — clear all."""
        with self._lock:
            if bucket is None:
                for q in self._logs.values():
                    q.clear()
            elif bucket in self._logs:
                self._logs[bucket].clear()


# Global singleton — tool code calls module-level `acquire` / `remaining_pct`
BUDGETER = QuotaBudgeter()


def acquire(bucket: str) -> float:
    return BUDGETER.acquire(bucket)


def remaining_pct(bucket: str) -> float | None:
    return BUDGETER.remaining_pct(bucket)


def reset(bucket: str | None = None) -> None:
    BUDGETER.reset(bucket)
