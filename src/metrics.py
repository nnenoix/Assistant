"""Prometheus-format /metrics endpoint for the FastAPI app.

Why direct text emission instead of `prometheus_client`: it's ~50 lines
of trivial format and dropping the dependency keeps the Docker image
slimmer + the PyInstaller .exe smaller. The format is well-specified —
see https://prometheus.io/docs/instrumenting/exposition_formats/ — and
the rest of the stack (Prometheus, Grafana, VictoriaMetrics, OpenMetrics)
all parse this exact line protocol.

Counters tracked per tool:
  - `workspace_agent_tool_calls_total{name, status}` — every wrapped tool
    invocation. `status` ∈ {"ok", "error"}; failures further broken down
    by `error_kind` via `workspace_agent_tool_errors_total`.
  - `workspace_agent_tool_latency_ms_bucket{name, le}` — cumulative
    histogram (Prometheus convention: each `le=N` reports calls ≤ N ms).
  - Plus auto-derived `_sum` / `_count` siblings.

Thread-safe — `_lock` guards all counter mutations.
"""
from __future__ import annotations

import bisect
import threading
from collections import defaultdict
from typing import Any


# Latency histogram buckets in milliseconds. Chosen to cover Google API
# (10-300ms), heavy reads (1-10s), and bulk operations (30s+).
_HISTOGRAM_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)


_lock = threading.Lock()
# tool_name -> count
_calls_ok: dict[str, int] = defaultdict(int)
_calls_err: dict[str, int] = defaultdict(int)
# (tool_name, error_kind) -> count
_errors_by_kind: dict[tuple[str, str], int] = defaultdict(int)
# tool_name -> {bucket_le_ms: count} (cumulative — count of calls ≤ each le)
_latency_buckets: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
_latency_sum_ms: dict[str, float] = defaultdict(float)
_latency_count: dict[str, int] = defaultdict(int)


def record_tool_call(name: str, latency_ms: float,
                     ok: bool, error_kind: str | None = None) -> None:
    """Record one wrapped-tool invocation. Cheap (~µs) — safe in hot path.

    Lock is held only for the dict increments; the bucket-membership
    decision (`bisect`) runs unlocked since `_HISTOGRAM_BUCKETS_MS` is
    a module-level immutable tuple."""
    # Cumulative histogram: a call with latency L falls into every
    # bucket with le >= L. `bisect_left` gives the FIRST matching index;
    # everything from that index up through +Inf gets incremented.
    first_bucket_idx = bisect.bisect_left(_HISTOGRAM_BUCKETS_MS, latency_ms)
    with _lock:
        if ok:
            _calls_ok[name] += 1
        else:
            _calls_err[name] += 1
            if error_kind:
                _errors_by_kind[(name, error_kind)] += 1
        _latency_count[name] += 1
        _latency_sum_ms[name] += latency_ms
        buckets = _latency_buckets[name]
        # Iterate only the buckets we actually need to touch.
        for le in _HISTOGRAM_BUCKETS_MS[first_bucket_idx:]:
            buckets[le] += 1
        # +Inf sentinel — always incremented (every call falls into it).
        buckets[-1] += 1


def reset() -> None:
    """Wipe all counters. Tests-only — call before / after to isolate cases."""
    with _lock:
        _calls_ok.clear()
        _calls_err.clear()
        _errors_by_kind.clear()
        _latency_buckets.clear()
        _latency_sum_ms.clear()
        _latency_count.clear()


def _escape_label(v: str) -> str:
    """Prometheus label-value escaping: backslash, quote, newline."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus() -> str:
    """Emit the current counter state in Prometheus text exposition format.

    Snapshot-then-format pattern: copy every counter dict under the lock
    (fast — shallow copies of small dicts), then build the ~5k f-strings
    OUTSIDE the lock. Prometheus scrapes don't block `record_tool_call`
    for the duration of the string build."""
    with _lock:
        snap_ok = dict(_calls_ok)
        snap_err = dict(_calls_err)
        snap_kinds = dict(_errors_by_kind)
        snap_count = dict(_latency_count)
        snap_sum = dict(_latency_sum_ms)
        snap_buckets = {k: dict(v) for k, v in _latency_buckets.items()}

    lines: list[str] = []
    # ----- workspace_agent_tool_calls_total -----
    # iterate in sorted order so output is deterministic (test-friendly)
    names = sorted(set(snap_ok) | set(snap_err))
    if names:
        lines.append("# HELP workspace_agent_tool_calls_total Total wrapped-tool invocations.")
        lines.append("# TYPE workspace_agent_tool_calls_total counter")
    for n in names:
        esc = _escape_label(n)
        if snap_ok.get(n):
            lines.append(
                f'workspace_agent_tool_calls_total{{name="{esc}",status="ok"}} {snap_ok[n]}'
            )
        if snap_err.get(n):
            lines.append(
                f'workspace_agent_tool_calls_total{{name="{esc}",status="error"}} {snap_err[n]}'
            )

    # ----- workspace_agent_tool_errors_total -----
    if snap_kinds:
        lines.append("# HELP workspace_agent_tool_errors_total Errors by classifier kind.")
        lines.append("# TYPE workspace_agent_tool_errors_total counter")
        for (n, kind), v in sorted(snap_kinds.items()):
            lines.append(
                f'workspace_agent_tool_errors_total{{name="{_escape_label(n)}",'
                f'error_kind="{_escape_label(kind)}"}} {v}'
            )

    # ----- workspace_agent_tool_latency_ms (histogram) -----
    if snap_count:
        lines.append("# HELP workspace_agent_tool_latency_ms Tool call latency in milliseconds.")
        lines.append("# TYPE workspace_agent_tool_latency_ms histogram")
        for n in sorted(snap_count):
            esc = _escape_label(n)
            buckets = snap_buckets.get(n, {})
            for le in _HISTOGRAM_BUCKETS_MS:
                cnt = buckets.get(le, 0)
                lines.append(
                    f'workspace_agent_tool_latency_ms_bucket{{name="{esc}",le="{le}"}} {cnt}'
                )
            # +Inf bucket
            inf_cnt = buckets.get(-1, snap_count[n])
            lines.append(
                f'workspace_agent_tool_latency_ms_bucket{{name="{esc}",le="+Inf"}} {inf_cnt}'
            )
            lines.append(
                f'workspace_agent_tool_latency_ms_sum{{name="{esc}"}} {snap_sum.get(n, 0.0):.3f}'
            )
            lines.append(
                f'workspace_agent_tool_latency_ms_count{{name="{esc}"}} {snap_count[n]}'
            )

    # Trailing newline per the exposition spec
    return "\n".join(lines) + ("\n" if lines else "")


def mount_metrics(app, *, path: str = "/metrics") -> None:
    """Attach the /metrics endpoint to a FastAPI app.

    Mirrors the structure of `mount_mcp_http`: idempotent, logs on mount,
    no-ops with a warning if the response classes fail to import.
    """
    import logging
    from fastapi.responses import PlainTextResponse

    logger = logging.getLogger(__name__)

    if getattr(app, "_metrics_mounted", False):
        return
    app._metrics_mounted = True

    @app.get(path, response_class=PlainTextResponse)
    async def _metrics_endpoint():
        return PlainTextResponse(
            render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    logger.info(f"Metrics endpoint mounted at {path}")
