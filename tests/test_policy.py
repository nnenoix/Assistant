import json

import pytest

from src.policy import Policy


@pytest.fixture
def policy_file(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({
        "drive": {"read": "*", "create": ["FOLDER_A"], "update": [], "delete": []},
        "sheets": {"read": "*", "write": ["SHEET_1"]},
        "local": {"read": ["D:/work/in"], "write": ["D:/work/out"]},
    }))
    return path


def test_wildcard_read_allowed(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("drive.read", {"file_id": "anything"}) is True


def test_create_in_listed_folder_allowed(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("drive.create", {"parent_id": "FOLDER_A", "name": "x"}) is True


def test_create_in_unlisted_folder_denied(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("drive.create", {"parent_id": "FOLDER_B", "name": "x"}) is False


def test_update_always_denied_when_empty_list(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("drive.update", {"file_id": "X"}) is False


def test_sheets_write_listed(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("sheets.write", {"spreadsheet_id": "SHEET_1", "range": "A1"}) is True
    assert p.is_allowed("sheets.write", {"spreadsheet_id": "SHEET_2", "range": "A1"}) is False


def test_local_write_within_allowed_root(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("local.write", {"path": "D:/work/out/report.csv"}) is True
    assert p.is_allowed("local.write", {"path": "D:/work/in/report.csv"}) is False
    assert p.is_allowed("local.write", {"path": "C:/Windows/something"}) is False


def test_missing_operation_defaults_deny(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("apps_script.run", {}) is False


def test_missing_file_creates_empty_policy(tmp_path):
    p = Policy.load(tmp_path / "nope.json")
    assert p.is_allowed("drive.read", {}) is False


def test_local_with_empty_or_missing_path_denied(policy_file):
    p = Policy.load(policy_file)
    assert p.is_allowed("local.write", {}) is False
    assert p.is_allowed("local.write", {"path": ""}) is False


def test_drive_read_matches_folder_id_when_non_wildcard(tmp_path):
    path = tmp_path / "al.json"
    path.write_text(json.dumps({"drive": {"read": ["FOLDER_X", "FILE_Y"]}}))
    p = Policy.load(path)
    # list_files passes folder_id
    assert p.is_allowed("drive.read", {"folder_id": "FOLDER_X"}) is True
    # get_metadata / download pass file_id
    assert p.is_allowed("drive.read", {"file_id": "FILE_Y"}) is True
    # something not in the list
    assert p.is_allowed("drive.read", {"folder_id": "OTHER"}) is False
    # search has no id at all → always denied under non-wildcard
    assert p.is_allowed("drive.read", {"name_contains": "report"}) is False
