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
    """Record one wrapped-tool invocation. Cheap (~µs) — safe in hot path."""
    with _lock:
        if ok:
            _calls_ok[name] += 1
        else:
            _calls_err[name] += 1
            if error_kind:
                _errors_by_kind[(name, error_kind)] += 1
        _latency_count[name] += 1
        _latency_sum_ms[name] += latency_ms
        for le in _HISTOGRAM_BUCKETS_MS:
            if latency_ms <= le:
                _latency_buckets[name][le] += 1
        # +Inf bucket is implicit — we always increment it via count, but
        # Prometheus convention also wants an explicit `le="+Inf"` line.
        _latency_buckets[name][-1] += 1  # sentinel for +Inf


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
    """Emit the current counter state in Prometheus text exposition format."""
    lines: list[str] = []
    with _lock:
        # ----- workspace_agent_tool_calls_total -----
        # iterate in sorted order so output is deterministic (test-friendly)
        names = sorted(set(_calls_ok) | set(_calls_err))
        if names:
            lines.append("# HELP workspace_agent_tool_calls_total Total wrapped-tool invocations.")
            lines.append("# TYPE workspace_agent_tool_calls_total counter")
        for n in names:
            esc = _escape_label(n)
            if _calls_ok[n]:
                lines.append(
                    f'workspace_agent_tool_calls_total{{name="{esc}",status="ok"}} {_calls_ok[n]}'
                )
            if _calls_err[n]:
                lines.append(
                    f'workspace_agent_tool_calls_total{{name="{esc}",status="error"}} {_calls_err[n]}'
                )

        # ----- workspace_agent_tool_errors_total -----
        if _errors_by_kind:
            lines.append("# HELP workspace_agent_tool_errors_total Errors by classifier kind.")
            lines.append("# TYPE workspace_agent_tool_errors_total counter")
            for (n, kind), v in sorted(_errors_by_kind.items()):
                lines.append(
                    f'workspace_agent_tool_errors_total{{name="{_escape_label(n)}",'
                    f'error_kind="{_escape_label(kind)}"}} {v}'
                )

        # ----- workspace_agent_tool_latency_ms (histogram) -----
        if _latency_count:
            lines.append("# HELP workspace_agent_tool_latency_ms Tool call latency in milliseconds.")
            lines.append("# TYPE workspace_agent_tool_latency_ms histogram")
            for n in sorted(_latency_count):
                esc = _escape_label(n)
                buckets = _latency_buckets[n]
                for le in _HISTOGRAM_BUCKETS_MS:
                    cnt = buckets.get(le, 0)
                    lines.append(
                        f'workspace_agent_tool_latency_ms_bucket{{name="{esc}",le="{le}"}} {cnt}'
                    )
                # +Inf bucket
                inf_cnt = buckets.get(-1, _latency_count[n])
                lines.append(
                    f'workspace_agent_tool_latency_ms_bucket{{name="{esc}",le="+Inf"}} {inf_cnt}'
                )
                lines.append(
                    f'workspace_agent_tool_latency_ms_sum{{name="{esc}"}} {_latency_sum_ms[n]:.3f}'
                )
                lines.append(
                    f'workspace_agent_tool_latency_ms_count{{name="{esc}"}} {_latency_count[n]}'
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
