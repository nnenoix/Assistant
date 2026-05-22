"""Unit tests for `apps_script_api.run_ad_hoc` per-step error surfacing.

The previous shape let raw HttpError propagate through `_wrap_for_sdk`, so
the agent saw `"HttpError: (404) ..."` truncated to 600 chars with no
indication of WHICH step failed (create_project? update_content? run?).
These tests lock down the new structured failures.
"""
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

# Pre-load registry BEFORE any test patches apps_script_api functions. The
# error classifier inside run_ad_hoc does a runtime `from src.tools.registry
# import _classify_exception`; if registry hasn't loaded yet, that import
# walks the tool list at module-load time and trips over patched MagicMocks
# (registry inspects each tool fn's `__code__` for an `account` param).
from src.tools import registry  # noqa: F401
from src.tools import apps_script_api


def _httperror(status: int, body: bytes = b'{"error": "x"}') -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Test"
    return HttpError(resp, body, uri="https://x/")


def test_create_project_404_returns_step_not_found():
    """First step fails → no script_id, no cleanup; error_kind classified."""
    with patch.object(apps_script_api, "create_project", side_effect=_httperror(404)):
        result = apps_script_api.run_ad_hoc(code="function main(){}")
    assert result["ok"] is False
    assert result["step"] == "create_project"
    assert result["_meta"]["error_kind"] == "not_found"
    assert result["_meta"]["http_status"] == 404
    assert "HttpError 404" in result["error"]
    # Nothing was created — script_id must not be in payload
    assert "script_id" not in result
    assert "cleanup_attempted" not in result


def test_update_content_failure_returns_script_id_and_cleans_up():
    """create_project succeeded but update_content failed → return script_id
    so the agent/user can retry-or-inspect, and best-effort cleanup runs."""
    with patch.object(apps_script_api, "create_project", return_value={"scriptId": "S1"}), \
         patch.object(apps_script_api, "update_content", side_effect=_httperror(403)), \
         patch("src.tools.drive.delete") as mock_delete:
        result = apps_script_api.run_ad_hoc(code="function main(){}")
    assert result["ok"] is False
    assert result["step"] == "update_content"
    assert result["script_id"] == "S1"
    assert result["script_url"].endswith("S1/edit")
    assert result["cleanup_attempted"] is True
    assert result["cleanup_failed"] is False
    mock_delete.assert_called_once_with("S1", account="main")


def test_update_content_failure_cleanup_also_fails():
    """If cleanup itself raises, the failure dict still returns with
    cleanup_failed=True — agent gets a hint to retry deletion."""
    with patch.object(apps_script_api, "create_project", return_value={"scriptId": "S1"}), \
         patch.object(apps_script_api, "update_content", side_effect=_httperror(403)), \
         patch("src.tools.drive.delete", side_effect=RuntimeError("flake")):
        result = apps_script_api.run_ad_hoc(code="function main(){}")
    assert result["ok"] is False
    assert result["step"] == "update_content"
    assert result["script_id"] == "S1"
    assert result["cleanup_attempted"] is True
    assert result["cleanup_failed"] is True


def test_run_function_httperror_returns_step_with_script_id():
    """run_function raising HttpError (most likely GCP-project mismatch) →
    step=run_function, script_id present, cleanup attempted."""
    with patch.object(apps_script_api, "create_project", return_value={"scriptId": "S2"}), \
         patch.object(apps_script_api, "update_content", return_value={}), \
         patch.object(apps_script_api, "run_function", side_effect=_httperror(403)), \
         patch("src.tools.drive.delete") as mock_delete:
        result = apps_script_api.run_ad_hoc(code="function main(){}")
    assert result["ok"] is False
    assert result["step"] == "run_function"
    assert result["script_id"] == "S2"
    assert result["cleanup_attempted"] is True
    mock_delete.assert_called_once_with("S2", account="main")


def test_apps_script_runtime_error_pass_through_unchanged():
    """If run_function RUNS but the user's code threw a TypeError, the
    existing structured shape is preserved — no `step` key, error_type/
    error_message intact. Regression lockdown."""
    runtime_payload = {
        "ok": False,
        "error_type": "TypeError",
        "error_message": "Cannot read property 'x' of undefined",
        "stack": [{"function": "main", "line": 3}],
        "raw": {},
    }
    with patch.object(apps_script_api, "create_project", return_value={"scriptId": "S3"}), \
         patch.object(apps_script_api, "update_content", return_value={}), \
         patch.object(apps_script_api, "run_function", return_value=runtime_payload), \
         patch("src.tools.drive.delete"):
        result = apps_script_api.run_ad_hoc(code="function main(){}")
    assert "step" not in result, "runtime errors must not be confused with API failures"
    assert result["ok"] is False
    assert result["error_type"] == "TypeError"
    assert result["error_message"].startswith("Cannot read")
    assert result["script_id"] == "S3"
    assert result["script_url"].endswith("S3/edit")


def test_happy_path_includes_script_url():
    """All three steps succeed → ok=True, script_url present, result echoed."""
    success_payload = {"ok": True, "result": [1, 2, 3], "raw": {}}
    with patch.object(apps_script_api, "create_project", return_value={"scriptId": "S4"}), \
         patch.object(apps_script_api, "update_content", return_value={}), \
         patch.object(apps_script_api, "run_function", return_value=success_payload), \
         patch("src.tools.drive.delete"):
        result = apps_script_api.run_ad_hoc(code="function main(){}")
    assert result["ok"] is True
    assert result["script_id"] == "S4"
    assert result["script_url"] == "https://script.google.com/d/S4/edit"
    assert result["result"] == [1, 2, 3]
    assert "step" not in result


# ---------- apps_script_api.status — pre-call health check ----------

import json as _json


def _write_token(path, scopes):
    path.write_text(_json.dumps({
        "token": "x", "refresh_token": "x",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "c", "client_secret": "s",
        "scopes": list(scopes),
    }), encoding="utf-8")


def test_status_missing_scopes_returns_not_ok(tmp_path, monkeypatch):
    """Token granted only a subset of required scopes → ok=False, missing list filled,
    API call is skipped (no point pinging when scope is doomed)."""
    from src import auth
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    _write_token(tokens_dir / "main.json", [
        "https://www.googleapis.com/auth/script.projects",
        # deployments + scriptapp missing
    ])

    # Service must NOT be called when scopes are short.
    with patch.object(apps_script_api, "_service") as mock_svc:
        result = apps_script_api.status()
    mock_svc.assert_not_called()

    assert result["ok"] is False
    assert result["scopes"]["granted"] == ["https://www.googleapis.com/auth/script.projects"]
    assert "https://www.googleapis.com/auth/script.deployments" in result["scopes"]["missing"]
    assert result["api_reachable"] is None  # was never tested


def test_status_full_scopes_no_script_returns_unknown_reachability(tmp_path, monkeypatch):
    """All scopes granted, but no script_id and no Phase 14 aggregator →
    api_reachable=None with an actionable hint."""
    from src import auth
    from src.tools import _phase14_config
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    _write_token(tokens_dir / "main.json", list(apps_script_api.REQUIRED_SCOPES))
    # Force "no aggregator configured"
    monkeypatch.setattr(_phase14_config, "CONFIG_PATH", tmp_path / "no_such_config.json")
    monkeypatch.delenv("PHASE14_AGGREGATOR_SCRIPT_ID", raising=False)

    result = apps_script_api.status()
    assert result["ok"] is True
    assert result["scopes"]["missing"] == []
    assert result["api_reachable"] is None
    assert "pass script_id" in result["api_error"]


def test_status_explicit_script_id_succeeds(tmp_path, monkeypatch):
    """Full scopes + explicit script_id + projects.get returns metadata → ok."""
    from src import auth
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    _write_token(tokens_dir / "main.json", list(apps_script_api.REQUIRED_SCOPES))

    fake_svc = MagicMock()
    fake_svc.projects().get().execute.return_value = {"scriptId": "SX", "title": "My Project"}
    with patch.object(apps_script_api, "_service", return_value=fake_svc):
        result = apps_script_api.status(script_id="SX")

    assert result["ok"] is True
    assert result["api_reachable"] is True
    assert result["aggregator"] is None  # explicit script_id was used, not aggregator


def test_status_api_returns_403_marks_unreachable(tmp_path, monkeypatch):
    """Full scopes BUT projects.get fails (e.g. GCP project mismatch) →
    ok=False, api_reachable=False, api_meta classifies the error."""
    from src import auth
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    _write_token(tokens_dir / "main.json", list(apps_script_api.REQUIRED_SCOPES))

    fake_svc = MagicMock()
    fake_svc.projects().get().execute.side_effect = _httperror(403)
    with patch.object(apps_script_api, "_service", return_value=fake_svc):
        result = apps_script_api.status(script_id="SX")

    assert result["ok"] is False
    assert result["api_reachable"] is False
    assert "HttpError 403" in result["api_error"]
    assert result["api_meta"]["http_status"] == 403


# ---------- triggers_install_one_shot / list / remove — per-step failures ----------

def test_triggers_install_one_shot_edit_file_failure():
    """First step (edit_file) fails → step='edit_file' + triggered_function preserved."""
    with patch.object(apps_script_api, "edit_file", side_effect=_httperror(403)):
        result = apps_script_api.triggers_install_one_shot(
            script_id="S1", function_name="myFn", delay_minutes=5,
        )
    assert result["ok"] is False
    assert result["step"] == "edit_file"
    assert result["triggered_function"] == "myFn"
    assert result["_meta"]["error_kind"] == "permission"
    assert result["_meta"]["http_status"] == 403
    assert "HttpError 403" in result["error"]


def test_triggers_install_one_shot_run_function_failure():
    """edit_file succeeds, run_function raises → step='run_function'."""
    with patch.object(apps_script_api, "edit_file", return_value={}), \
         patch.object(apps_script_api, "run_function", side_effect=_httperror(403)):
        result = apps_script_api.triggers_install_one_shot(
            script_id="S1", function_name="myFn",
        )
    assert result["ok"] is False
    assert result["step"] == "run_function"
    assert result["triggered_function"] == "myFn"
    assert result["_meta"]["error_kind"] == "permission"


def test_triggers_list_run_function_failure_preserves_script_id():
    with patch.object(apps_script_api, "edit_file", return_value={}), \
         patch.object(apps_script_api, "run_function", side_effect=_httperror(404)):
        result = apps_script_api.triggers_list(script_id="S2")
    assert result["ok"] is False
    assert result["step"] == "run_function"
    assert result["script_id"] == "S2"
    assert result["_meta"]["error_kind"] == "not_found"


def test_triggers_remove_run_function_failure_preserves_identifiers():
    """trigger_id / function_name preserved so the agent can retry."""
    with patch.object(apps_script_api, "edit_file", return_value={}), \
         patch.object(apps_script_api, "run_function", side_effect=_httperror(401)):
        result = apps_script_api.triggers_remove(
            script_id="S3", function_name="cleanup", trigger_id=None,
        )
    assert result["ok"] is False
    assert result["step"] == "run_function"
    assert result["function_name"] == "cleanup"
    assert result["script_id"] == "S3"
    assert result["_meta"]["error_kind"] == "auth_scope"


# ---------- run_smart — structured per-attempt classification ----------

def test_run_smart_records_error_kind_per_attempt():
    """Each attempt failure carries error_kind + http_status (not just str(e)[:200])."""
    # First run_function: HttpError(404). Second (after create_version + create_deployment): HttpError(403).
    rf_calls = [_httperror(404, b'{"error":"not found"}'), _httperror(403, b'{"error":"permission"}')]

    def fake_run_function(*args, **kwargs):
        e = rf_calls.pop(0)
        raise e

    with patch.object(apps_script_api, "run_function", side_effect=fake_run_function), \
         patch.object(apps_script_api, "create_version", return_value={"versionNumber": 1}), \
         patch.object(apps_script_api, "create_deployment", return_value={}):
        result = apps_script_api.run_smart(script_id="S1", function_name="main")

    assert result["ok"] is False
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["step"] == "scripts.run dev"
    assert result["attempts"][0]["error_kind"] == "not_found"
    assert result["attempts"][1]["step"] == "scripts.run pinned"
    assert result["attempts"][1]["error_kind"] == "permission"
    # Top-level _meta reflects the LAST attempt (most-specific clue)
    assert result["_meta"]["error_kind"] == "permission"
    # No 200-char truncation on the err field
    for a in result["attempts"]:
        assert isinstance(a["err"], str) and a["err"].startswith("HttpError")


def test_status_uses_aggregator_when_no_script_id(tmp_path, monkeypatch):
    """No script_id given, but Phase 14 aggregator is configured → use it for
    the ping and report under `aggregator`."""
    from src import auth
    from src.tools import _phase14_config
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    _write_token(tokens_dir / "main.json", list(apps_script_api.REQUIRED_SCOPES))
    # Configure aggregator
    cfg_path = tmp_path / "phase14_config.json"
    cfg_path.write_text(_json.dumps({"aggregator_script_id": "AGG1"}), encoding="utf-8")
    monkeypatch.setattr(_phase14_config, "CONFIG_PATH", cfg_path)
    monkeypatch.delenv("PHASE14_AGGREGATOR_SCRIPT_ID", raising=False)

    fake_svc = MagicMock()
    fake_svc.projects().get().execute.return_value = {"scriptId": "AGG1", "title": "ChatAgentAggregator"}
    with patch.object(apps_script_api, "_service", return_value=fake_svc):
        result = apps_script_api.status()

    assert result["ok"] is True
    assert result["api_reachable"] is True
    assert result["aggregator"]["script_id"] == "AGG1"
    assert result["aggregator"]["accessible"] is True
    assert result["aggregator"]["title"] == "ChatAgentAggregator"
