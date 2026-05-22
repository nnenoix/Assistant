"""Lockdown tests for the remaining 6 swallow-and-stringify sites: every
caught exception now carries `error_kind` (from `_errors._classify_exception`)
so the agent can branch on the failure type instead of regexing a string.
"""
from unittest.mock import MagicMock, patch


def test_calendar_overlay_accounts_surfaces_per_account_error_kind():
    """calendar.overlay_accounts: one account fails → out[acct]['error']
    is now a dict (not a string) with error_kind + http_status."""
    from src.tools import calendar

    def fake_freebusy(emails, time_min, time_max, account):
        if account == "bad":
            raise PermissionError("denied")
        return {"per_email": [], "_meta": {}}

    with patch.object(calendar, "freebusy", side_effect=fake_freebusy):
        result = calendar.overlay_accounts(
            {"main": ["a@x.com"], "bad": ["b@y.com"]},
            time_min="2026-05-20", time_max="2026-05-21",
        )

    err = result["per_account"]["bad"]["error"]
    assert isinstance(err, dict)
    assert err["error_kind"] == "permission"
    # Top-level _meta surfaces error_count + warning for the agent
    assert result["_meta"]["error_count"] == 1
    assert "warning" in result["_meta"]


def test_calendar_overlay_accounts_clean_run_has_no_errors_block():
    from src.tools import calendar

    with patch.object(calendar, "freebusy", return_value={"per_email": [], "_meta": {}}):
        result = calendar.overlay_accounts(
            {"main": ["a@x.com"]},
            time_min="2026-05-20", time_max="2026-05-21",
        )
    assert "errors" not in result["_meta"]
    assert "warning" not in result["_meta"]


def test_watcher_poll_known_scripts_classifies_recent_failures_error(tmp_path, monkeypatch):
    """When recent_failures raises, watcher.poll_known_scripts returns a
    classified errors[] entry with step + error_kind."""
    from src.tools import watcher
    monkeypatch.setattr(watcher, "ALERTS_SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(watcher, "ALERTS_PATH", tmp_path / "alerts.json")

    with patch.object(watcher, "recent_failures", side_effect=ConnectionError("dns failure")), \
         patch.object(watcher, "_known_scripts", return_value=["S1"]):
        out = watcher.poll_known_scripts(since_minutes=30)

    assert len(out["errors"]) == 1
    err = out["errors"][0]
    assert err["step"] == "recent_failures"
    assert err["error_kind"] == "network"
    assert out["_meta"]["error_kind"] == "network"


def test_wb_check_token_classifies_per_family_exception():
    """wb.check_token: a request failure must surface as
    {error, error_kind, http_status, exception_type} per family."""
    from src.tools import wb

    def fake_request(host, path, token, timeout):
        raise TimeoutError("read timeout")

    with patch.object(wb, "_request", side_effect=fake_request):
        out = wb.check_token("dummy.jwt.token")
    # Every family entry now carries classification
    sample = next(iter(out.values()))
    assert sample["error_kind"] == "network"
    assert sample["exception_type"] == "TimeoutError"
    assert "read timeout" in sample["error"]


def test_file_analyze_list_analyses_classifies_per_file_error(tmp_path, monkeypatch):
    """file_analyze.list_analyses: a corrupt .md file → entry carries
    error_kind + exception_type, not just str(e)[:120]."""
    from src.tools import file_analyze
    analyses_dir = tmp_path / "analyses"
    analyses_dir.mkdir()
    # One valid file
    (analyses_dir / "ok.md").write_text(
        "---\nsource: test\nfocus: x\ncreated_at: 2026-05-22T10:00:00Z\nchars_in: 100\nfile_kind: pdf\n---\nbody",
        encoding="utf-8",
    )
    # One corrupt — read_text won't raise; we'll patch front-matter parser
    (analyses_dir / "bad.md").write_text("not yaml at all", encoding="utf-8")
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", analyses_dir)

    # Force the front-matter parse to raise on the corrupt entry only
    original_parse = file_analyze._parse_front_matter
    def selective_parse(text):
        if text == "not yaml at all":
            raise ValueError("malformed front matter")
        return original_parse(text)
    monkeypatch.setattr(file_analyze, "_parse_front_matter", selective_parse)

    result = file_analyze.list_analyses()
    bad_entry = next(a for a in result["analyses"] if a["name"] == "bad")
    assert bad_entry["error_kind"] == "bad_input"
    assert bad_entry["exception_type"] == "ValueError"


def test_browser_set_script_gcp_project_classifies_exception():
    """browser.set_script_gcp_project: an in-flight Playwright exception
    (e.g. page.goto times out) ends in a structured return shape with
    _meta.error_kind. The _launch_persistent call itself is OUTSIDE the
    try block by design — it has no exception envelope to test here, so
    we inject failure after the launch."""
    from src.tools import browser

    fake_pw = MagicMock()
    fake_ctx = MagicMock()
    fake_page = MagicMock()
    fake_page.goto.side_effect = TimeoutError("nav timeout")
    fake_ctx.new_page.return_value = fake_page
    with patch.object(browser, "_launch_persistent", return_value=(fake_pw, fake_ctx, "msedge")):
        result = browser.set_script_gcp_project(
            script_id="S1", project_number="123456789",
        )
    assert result["ok"] is False
    assert result["_meta"]["error_kind"] == "network"
    assert result["exception_type"] == "TimeoutError"
