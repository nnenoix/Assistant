"""Verify Phase 14C Apps Script deploy.

Reads script_id from env or .data/phase14_config.json, then calls the
deployed `ping()` function via clasp run. Prints ✓ on success, actionable
error otherwise.

Usage:
    $env:LIVE_GOOGLE_TESTS = "1"
    uv run python scripts/verify_phase14_setup.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools import _phase14_config
from src.tools import apps_script


def main() -> int:
    # 1. Resolve script_id
    try:
        script_id = _phase14_config.get_aggregator_script_id()
    except _phase14_config.Phase14ConfigError as e:
        print(f"✗ {e}")
        return 1
    print(f"✓ config found: aggregator_script_id={script_id[:8]}...")

    # 2. Clone the project locally (needed by clasp run)
    try:
        apps_script.clone(script_id)
    except apps_script.ClaspError as e:
        # If already cloned, clone is idempotent; this means a real error.
        print(f"✗ clasp clone failed: {e}")
        print("  Hint: is `clasp` installed and logged in? `clasp login`")
        return 1
    print(f"✓ cloned locally to .data/scripts/{script_id[:8]}.../")

    # 3. Call ping() — proves the API executable deploy is live
    try:
        result = apps_script.run_function(script_id, "ping")
    except apps_script.ClaspError as e:
        msg = str(e)
        print(f"✗ clasp run ping failed: {msg}")
        if "function not found" in msg.lower():
            print("  Hint: did you `clasp push --force` after creating the project?")
            print("  Run: cd apps_script_src/aggregator && clasp push --force")
        elif "api executable" in msg.lower() or "not deployed" in msg.lower():
            print("  Hint: the project isn't deployed as API executable yet.")
            print("  See docs/PHASE_14_SETUP.md step 3.")
        return 1

    # 4. Parse and validate
    try:
        # clasp run output is a JSON-like dict — the actual return value
        # is inside .output as a string. Try to parse it.
        raw = result.get("output", "").strip()
        # clasp may pretty-print the JS object; tolerate either.
        if raw.startswith("{") and "version" in raw:
            print(f"✓ clasp run ping → {raw[:200]}")
        else:
            print(f"⚠ unexpected output shape: {raw[:200]}")
            return 1
    except Exception as e:
        print(f"⚠ couldn't parse output: {e}")
        return 1

    print()
    print("✓ ready: sheets_cross_aggregate will work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
