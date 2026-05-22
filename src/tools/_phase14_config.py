"""Phase 14 config resolution.

`sheets_cross_aggregate` needs the script ID of the persistently-deployed
ChatAgentAggregator project. We resolve in this order:

  1. Env var PHASE14_AGGREGATOR_SCRIPT_ID
  2. .data/phase14_config.json: {"aggregator_script_id": "..."}
  3. raise ConfigError with a clear setup hint pointing to docs/PHASE_14_SETUP.md
"""
import json
import os
from pathlib import Path

from src.config import DATA_DIR


class Phase14ConfigError(RuntimeError):
    """Raised when Phase 14 setup hasn't been completed."""
    pass


CONFIG_PATH = DATA_DIR / "phase14_config.json"
ENV_VAR = "PHASE14_AGGREGATOR_SCRIPT_ID"


def get_aggregator_script_id() -> str:
    """Return the persistent aggregator Apps Script ID.

    Raises Phase14ConfigError with setup hint if not configured.
    """
    # 1. Env var takes precedence
    sid = os.environ.get(ENV_VAR, "").strip()
    if sid:
        return sid

    # 2. Fall back to file
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise Phase14ConfigError(
                f"{CONFIG_PATH} is malformed: {e}. Expected "
                f'{{"aggregator_script_id": "..."}}.'
            )
        sid = (data.get("aggregator_script_id") or "").strip()
        if sid:
            return sid

    # 3. Nothing — give actionable error
    raise Phase14ConfigError(
        "Phase 14 aggregator script_id not configured. "
        "Either set env PHASE14_AGGREGATOR_SCRIPT_ID, or create "
        f"{CONFIG_PATH} with {{\"aggregator_script_id\": \"<SCRIPT_ID>\"}}. "
        "See docs/PHASE_14_SETUP.md for the one-time deploy ceremony."
    )


def write_config(script_id: str) -> Path:
    """Write the script_id to .data/phase14_config.json. Returns path."""
    CONFIG_PATH.write_text(
        json.dumps({"aggregator_script_id": script_id}, indent=2),
        encoding="utf-8",
    )
    return CONFIG_PATH
