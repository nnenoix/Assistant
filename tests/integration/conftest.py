"""Integration test fixtures — hit live Google APIs against egor.titt@gmail.com.

All integration tests are gated by `LIVE_GOOGLE_TESTS=1` env var. Without it
they auto-skip, so unit-test CI is unaffected. With it, each test creates
artifacts under `CLAUDE-TEST/phase-{N}/{test_name}-{utc_timestamp}/` and
**leaves them in place** — history is kept for manual inspection.

Layout of CLAUDE-TEST in Drive:

    CLAUDE-TEST/
      seed/                       # populated once by scripts/seed_claude_test.py
      phase-0/                    # foundation tests
      phase-1/                    # sheets-provenance tests
        test_named_ranges_round_trip-2026-05-20T14-30-12/
          spreadsheet artifacts...
      phase-2/
      ...

The `CLAUDE-TEST` folder ID is persisted in `.data/integration_test_config.json`
after Phase 0 setup. Tests fail fast with a clear message if it's missing.
"""
import datetime as _dt
import json
import os
from pathlib import Path

import pytest


CONFIG_PATH = Path(__file__).resolve().parents[2] / ".data" / "integration_test_config.json"


def _live_enabled() -> bool:
    return os.environ.get("LIVE_GOOGLE_TESTS") == "1"


def pytest_collection_modifyitems(config, items):
    """Auto-skip every @pytest.mark.integration test unless LIVE_GOOGLE_TESTS=1.

    Implemented as a collection hook (not an autouse fixture) so the skip
    fires BEFORE session-scoped fixtures resolve — otherwise loading
    `.data/integration_test_config.json` would error on a fresh checkout
    where the config doesn't exist yet.
    """
    if _live_enabled():
        return
    skip_marker = pytest.mark.skip(
        reason="set LIVE_GOOGLE_TESTS=1 to run integration tests against egor.titt@gmail.com"
    )
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def claude_test_config() -> dict:
    """Load .data/integration_test_config.json (written by Phase 0 setup).

    Required fields:
      account              : Google account alias (typically 'main')
      claude_test_folder_id: Drive file_id of the CLAUDE-TEST root folder

    Fails fast with a clear remediation hint if missing.
    """
    if not CONFIG_PATH.exists():
        pytest.fail(
            f"Integration tests need {CONFIG_PATH} — run Phase 0 setup:\n"
            "  python scripts/seed_claude_test.py --bootstrap-only\n"
            "to create the CLAUDE-TEST folder and persist its ID."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def claude_test_root_id(claude_test_config) -> str:
    fid = claude_test_config.get("claude_test_folder_id")
    if not fid:
        pytest.fail(
            f"{CONFIG_PATH} is missing 'claude_test_folder_id'. Re-run Phase 0 setup."
        )
    return fid


@pytest.fixture(scope="session")
def claude_test_account(claude_test_config) -> str:
    return claude_test_config.get("account", "main")


def _utc_timestamp() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")


def _phase_from_path(node_path: str) -> str:
    """Derive 'phase-N' from a test file path like 'tests/integration/test_sheets_phase1_live.py'."""
    name = Path(node_path).name
    # try matching '_phase<N>_' anywhere in filename
    for token in name.split("_"):
        if token.startswith("phase") and token[5:].isdigit():
            return f"phase-{token[5:]}"
    return "phase-misc"


@pytest.fixture
def claude_test_subfolder(request, claude_test_root_id, claude_test_account):
    """Create a fresh `CLAUDE-TEST/phase-{N}/{test_name}-{utc}/` subfolder for
    one test. Returns its Drive file_id. NEVER cleans up — intentional, so
    history survives.
    """
    from src.tools import drive

    phase = _phase_from_path(str(request.node.path))
    test_name = request.node.name
    leaf = f"{test_name}-{_utc_timestamp()}"

    # Walk-or-create the phase folder once, then create the leaf.
    phase_folder_id = _ensure_subfolder(drive, claude_test_root_id, phase, claude_test_account)
    leaf_folder = drive.create_folder(phase_folder_id, leaf, account=claude_test_account)
    return leaf_folder["id"]


def _ensure_subfolder(drive_module, parent_id: str, name: str, account: str) -> str:
    """Find a subfolder by name under parent_id; create if missing. Returns its id."""
    listing = drive_module.list_files(folder_id=parent_id, account=account, page_size=200)
    for f in listing.get("files", []):
        if f.get("name") == name and f.get("mimeType") == "application/vnd.google-apps.folder":
            return f["id"]
    created = drive_module.create_folder(parent_id, name, account=account)
    return created["id"]
