"""Unit tests for src/tools/_phase14_config.py."""
import json
import os
from pathlib import Path

import pytest

from src.tools import _phase14_config as cfg


@pytest.fixture
def _isolate_config(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a tmp file + clear env var so tests don't leak."""
    fake = tmp_path / "phase14_config.json"
    monkeypatch.setattr(cfg, "CONFIG_PATH", fake)
    monkeypatch.delenv(cfg.ENV_VAR, raising=False)
    yield fake


def test_env_var_takes_precedence(_isolate_config, monkeypatch):
    _isolate_config.write_text(json.dumps({"aggregator_script_id": "from-file"}))
    monkeypatch.setenv(cfg.ENV_VAR, "from-env")
    assert cfg.get_aggregator_script_id() == "from-env"


def test_file_used_when_env_unset(_isolate_config):
    _isolate_config.write_text(json.dumps({"aggregator_script_id": "from-file"}))
    assert cfg.get_aggregator_script_id() == "from-file"


def test_missing_config_raises_actionable_error(_isolate_config):
    with pytest.raises(cfg.Phase14ConfigError, match="PHASE_14_SETUP.md"):
        cfg.get_aggregator_script_id()


def test_malformed_json_raises_specific_error(_isolate_config):
    _isolate_config.write_text("not json {")
    with pytest.raises(cfg.Phase14ConfigError, match="malformed"):
        cfg.get_aggregator_script_id()


def test_empty_script_id_falls_through_to_error(_isolate_config):
    _isolate_config.write_text(json.dumps({"aggregator_script_id": "  "}))
    with pytest.raises(cfg.Phase14ConfigError):
        cfg.get_aggregator_script_id()


def test_write_config_creates_file(_isolate_config):
    path = cfg.write_config("ABC123")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["aggregator_script_id"] == "ABC123"
