"""Unit tests for the /metrics endpoint and the in-process counter store.

The endpoint emits Prometheus text exposition format (v0.0.4). Parsers
worth being compatible with: Prometheus itself, VictoriaMetrics,
Grafana Agent, OpenMetrics, OTel collector's `prometheusreceiver`. All
of them tolerate the same line protocol — the assertions below check
its key invariants instead of byte-for-byte equality.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from src import metrics


@pytest.fixture(autouse=True)
def _isolate_counters():
    metrics.reset()
    yield
    metrics.reset()


# ============================================================
# record_tool_call + render_prometheus
# ============================================================

def test_empty_renders_blank():
    """No calls recorded → empty output (Prometheus accepts this)."""
    assert metrics.render_prometheus() == ""


def test_single_ok_call_renders_counter_line():
    metrics.record_tool_call("sheets_read_range", 42.0, ok=True)
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_calls_total{name="sheets_read_range",status="ok"} 1' in out
    # No error counter line when there were no errors
    assert "status=\"error\"" not in out


def test_error_call_records_kind():
    metrics.record_tool_call("gmail_send", 120.0, ok=False, error_kind="auth_scope")
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_calls_total{name="gmail_send",status="error"} 1' in out
    assert 'workspace_agent_tool_errors_total{name="gmail_send",error_kind="auth_scope"} 1' in out


def test_help_and_type_directives_present():
    metrics.record_tool_call("x", 10.0, ok=True)
    out = metrics.render_prometheus()
    assert "# HELP workspace_agent_tool_calls_total" in out
    assert "# TYPE workspace_agent_tool_calls_total counter" in out


def test_histogram_buckets_cumulative():
    """A 50ms call should fall into all buckets le ∈ {50, 100, 250, ...}
    but NOT le ∈ {5, 10, 25} — that's Prometheus's cumulative semantics."""
    metrics.record_tool_call("t", 50.0, ok=True)
    out = metrics.render_prometheus()
    # included
    for le in (50, 100, 250, 500, 1000):
        assert f'name="t",le="{le}"}} 1' in out, f"bucket le={le} should include 50ms call"
    # excluded
    for le in (5, 10, 25):
        assert f'name="t",le="{le}"}} 0' in out, f"bucket le={le} should exclude 50ms call"


def test_histogram_sum_and_count():
    metrics.record_tool_call("t", 100.0, ok=True)
    metrics.record_tool_call("t", 200.0, ok=True)
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_latency_ms_count{name="t"} 2' in out
    # Sum is 300.0; format uses %.3f so the formatted value is "300.000"
    assert 'workspace_agent_tool_latency_ms_sum{name="t"} 300.000' in out


def test_inf_bucket_always_emitted():
    metrics.record_tool_call("t", 999999.0, ok=True)  # huge — beyond all buckets
    out = metrics.render_prometheus()
    assert 'le="+Inf"' in out


def test_label_escaping():
    """Tool names containing characters that need escaping in label values."""
    metrics.record_tool_call('weird"name\\with\nnewline', 10.0, ok=True)
    out = metrics.render_prometheus()
    # Quote, backslash, newline all escaped
    assert 'weird\\"name\\\\with\\nnewline' in out


def test_deterministic_output_ordering():
    """Sorted output makes diffs / golden-tests stable."""
    metrics.record_tool_call("b", 10.0, ok=True)
    metrics.record_tool_call("a", 10.0, ok=True)
    out = metrics.render_prometheus()
    # 'a' counter line must come before 'b' counter line
    pos_a = out.index('workspace_agent_tool_calls_total{name="a"')
    pos_b = out.index('workspace_agent_tool_calls_total{name="b"')
    assert pos_a < pos_b


def test_thread_safety_under_concurrent_recording():
    """Lots of concurrent recordings shouldn't lose any updates."""
    import threading
    def hammer():
        for _ in range(1000):
            metrics.record_tool_call("hot", 1.0, ok=True)
    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_calls_total{name="hot",status="ok"} 4000' in out


# ============================================================
# HTTP mount
# ============================================================

def test_mount_metrics_serves_endpoint():
    """End-to-end: mount on a fresh FastAPI app, hit /metrics."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    metrics.mount_metrics(app)
    metrics.record_tool_call("test_tool", 30.0, ok=True)

    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "version=0.0.4" in resp.headers["content-type"]
    assert "test_tool" in resp.text


def test_mount_metrics_is_idempotent():
    """Calling mount twice doesn't register the route twice (would FastAPI-warn)."""
    from fastapi import FastAPI
    app = FastAPI()
    metrics.mount_metrics(app)
    metrics.mount_metrics(app)
    # One /metrics route on the app
    paths = [r.path for r in app.routes if getattr(r, "path", None) == "/metrics"]
    assert len(paths) == 1


# ============================================================
# Integration: _wrap_for_sdk records into the metrics store
# ============================================================

def test_wrap_for_sdk_increments_call_counter():
    """A successful tool call must show up as status=ok in the counter."""
    from src.tools import registry
    spec = registry._tool(
        "metrics_probe_ok", lambda x: {"x": x}, "sheets.read", "test",
        {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
    )
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1}))
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_calls_total{name="metrics_probe_ok",status="ok"} 1' in out


def test_wrap_for_sdk_records_error_with_kind():
    """A raising tool must show as status=error AND populate errors_total."""
    from src.tools import registry

    def boom(x):
        raise PermissionError("no scope")  # classifier maps PermissionError → "permission"

    spec = registry._tool(
        "metrics_probe_err", boom, "sheets.read", "test",
        {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
    )
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1}))
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_calls_total{name="metrics_probe_err",status="error"} 1' in out
    # error_kind populated (exact label depends on classifier — assert it exists)
    assert re.search(
        r'workspace_agent_tool_errors_total\{name="metrics_probe_err",error_kind="[^"]+"\} 1',
        out,
    )


def test_wrap_for_sdk_latency_recorded():
    """Latency_count + latency_sum must exist after a call."""
    from src.tools import registry
    spec = registry._tool(
        "metrics_latency_probe", lambda x: {"x": x}, "sheets.read", "test",
        {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
    )
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1}))
    out = metrics.render_prometheus()
    assert 'workspace_agent_tool_latency_ms_count{name="metrics_latency_probe"} 1' in out
    # _sum is a non-negative float
    m = re.search(
        r'workspace_agent_tool_latency_ms_sum\{name="metrics_latency_probe"\} ([\d.]+)',
        out,
    )
    assert m
    assert float(m.group(1)) >= 0.0
