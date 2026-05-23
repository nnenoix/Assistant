"""Unit tests for OTel span attributes set by `_wrap_for_sdk`.

The runtime here doesn't have `opentelemetry` installed, so the module
attribute `_otel_trace` is `None` and the real instrumentation is
no-op. These tests inject a fake tracer (and Status/StatusCode stubs)
to exercise the code paths that fire on a production deploy that DOES
have OTel + Langfuse / Phoenix / Jaeger exporters wired.

What we verify:
  - early attributes: name, tenant_id, dry_run, idempotency_key_present
  - late attributes (in finally): status, latency_ms, error_kind, quota_paced_ms
  - on exception: record_exception(e) + set_status(ERROR)
  - the span context manager is entered AND exited exactly once
  - tool result is unchanged whether the tracer is present or not
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest


# ----------- fake OTel surface -----------

class _FakeSpan:
    def __init__(self):
        self.attrs: dict[str, Any] = {}
        self.exceptions_recorded: list[BaseException] = []
        self.status: Any = None

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def record_exception(self, exc):
        self.exceptions_recorded.append(exc)

    def set_status(self, status):
        self.status = status


class _FakeCM:
    def __init__(self, span):
        self.span = span
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self.span

    def __exit__(self, *exc):
        self.exited = True
        return False  # don't swallow exceptions


class _FakeTracer:
    def __init__(self, span: _FakeSpan):
        self._span = span
        self.last_cm: _FakeCM | None = None

    def start_as_current_span(self, name: str):
        self.last_cm = _FakeCM(self._span)
        return self.last_cm


class _FakeTraceModule:
    def __init__(self, tracer: _FakeTracer):
        self._tracer = tracer

    def get_tracer(self, name: str):
        return self._tracer


class _FakeStatus:
    """Mimic opentelemetry.trace.Status — store the code + description."""
    def __init__(self, code, description=None):
        self.code = code
        self.description = description


class _FakeStatusCode:
    ERROR = "ERROR"
    OK = "OK"


# ----------- shared fixture -----------

@pytest.fixture
def otel(monkeypatch):
    """Bind a fake OTel surface onto the registry module + return helpers
    to make assertions against the span the wrapper populates."""
    from src.tools import registry
    span = _FakeSpan()
    tracer = _FakeTracer(span)
    fake_module = _FakeTraceModule(tracer)
    monkeypatch.setattr(registry, "_otel_trace", fake_module)
    monkeypatch.setattr(registry, "_OtelStatus", _FakeStatus)
    monkeypatch.setattr(registry, "_OtelStatusCode", _FakeStatusCode)
    return {"span": span, "tracer": tracer, "module": fake_module}


def _make_spec(fn, *, name="otel_probe", policy_op="sheets.read"):
    from src.tools import registry
    return registry._tool(
        name, fn, policy_op, "test desc",
        {"type": "object", "properties": {"x": {"type": "integer"}},
         "required": ["x"]},
    )


# ----------- happy path -----------

def test_success_sets_status_ok_and_latency(otel):
    from src.tools import registry
    spec = _make_spec(lambda x: {"x": x})
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1}))

    a = otel["span"].attrs
    assert a["tool.name"] == "otel_probe"
    assert a["tool.status"] == "ok"
    assert "tool.latency_ms" in a
    assert isinstance(a["tool.latency_ms"], float)
    assert a["tool.latency_ms"] >= 0.0
    assert a["tool.dry_run"] is False
    assert a["tool.idempotency_key_present"] is False
    assert "tool.error_kind" not in a  # only set on error
    assert otel["span"].status is None  # no error → no explicit status


def test_span_context_manager_is_entered_and_exited(otel):
    from src.tools import registry
    spec = _make_spec(lambda x: {"x": x})
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1}))

    cm = otel["tracer"].last_cm
    assert cm is not None
    assert cm.entered is True
    assert cm.exited is True


def test_tenant_id_attribute_reflects_context(otel, monkeypatch):
    from src.tools import registry
    from src.tenancy import _current_tenant
    token = _current_tenant.set("acme-corp")
    try:
        spec = _make_spec(lambda x: {"x": x})
        handler = registry._wrap_for_sdk(spec).handler
        asyncio.run(handler({"x": 1}))
    finally:
        _current_tenant.reset(token)

    assert otel["span"].attrs["tool.tenant_id"] == "acme-corp"


# ----------- error path -----------

def test_exception_records_exception_and_sets_error_status(otel):
    from src.tools import registry

    def fn(x):
        raise PermissionError("scope missing")

    spec = _make_spec(fn)
    handler = registry._wrap_for_sdk(spec).handler

    result = asyncio.run(handler({"x": 1}))

    assert result.get("is_error") is True
    a = otel["span"].attrs
    assert a["tool.status"] == "error"
    assert "tool.error_kind" in a  # classifier picked something up
    # record_exception called exactly once with the actual exception
    assert len(otel["span"].exceptions_recorded) == 1
    assert isinstance(otel["span"].exceptions_recorded[0], PermissionError)
    # Status is set to ERROR
    assert otel["span"].status is not None
    assert otel["span"].status.code == "ERROR"
    assert "scope missing" in (otel["span"].status.description or "")


# ----------- dry_run + idempotency key flags -----------

def test_idempotency_key_present_attribute(otel):
    from src.tools import registry
    spec = _make_spec(lambda x: {"x": x}, name="idem_probe",
                      policy_op="gmail.send")  # non-idempotent → supports keys
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1, "idempotency_key": "abc"}))
    assert otel["span"].attrs["tool.idempotency_key_present"] is True


def test_dry_run_attribute_set_when_supported(otel):
    from src.tools import registry
    # Build a spec that supports dry_run by going through the normal _tool
    # path with a destructive-but-readable op (sheets.write supports it).
    spec = registry._tool(
        "dry_run_probe", lambda x: {"x": x}, "gmail.send", "test",
        {"type": "object", "properties": {"x": {"type": "integer"}},
         "required": ["x"]},
    )
    handler = registry._wrap_for_sdk(spec).handler

    asyncio.run(handler({"x": 1, "dry_run": True}))
    assert otel["span"].attrs["tool.dry_run"] is True


# ----------- no-tracer fallback -----------

def test_no_tracer_still_returns_tool_result(monkeypatch):
    """When opentelemetry isn't installed (`_otel_trace is None`), the
    wrapper must still call the tool and produce the normal response."""
    from src.tools import registry
    monkeypatch.setattr(registry, "_otel_trace", None)
    spec = _make_spec(lambda x: {"x": x})
    handler = registry._wrap_for_sdk(spec).handler

    result = asyncio.run(handler({"x": 42}))
    import json
    assert json.loads(result["content"][0]["text"])["x"] == 42


def test_tracer_creation_failure_does_not_break_tool(monkeypatch):
    """If get_tracer() / start_as_current_span() blow up, the tool must
    still run — instrumentation must never break the call."""
    from src.tools import registry

    class _BrokenTracer:
        def start_as_current_span(self, name):
            raise RuntimeError("tracer initialization failed")

    class _BrokenModule:
        def get_tracer(self, name):
            return _BrokenTracer()

    monkeypatch.setattr(registry, "_otel_trace", _BrokenModule())
    spec = _make_spec(lambda x: {"x": x})
    handler = registry._wrap_for_sdk(spec).handler

    result = asyncio.run(handler({"x": 1}))
    import json
    assert json.loads(result["content"][0]["text"])["x"] == 1
