import json
from unittest.mock import patch, MagicMock

import pytest

from src import auth


@pytest.fixture
def fresh_tokens_dir(tmp_path, monkeypatch):
    """Redirect auth's tokens dir + legacy token path to a clean tmp dir for each test."""
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    monkeypatch.setattr(auth, "_LEGACY_TOKEN", tmp_path / "token.json")
    return tokens_dir


def _write_token(path, scopes=None):
    payload = {
        "token": "fake_access",
        "refresh_token": "fake_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake_client",
        "client_secret": "fake_secret",
        "scopes": scopes or ["https://www.googleapis.com/auth/drive"],
    }
    path.write_text(json.dumps(payload))


def test_list_accounts_empty(fresh_tokens_dir):
    assert auth.list_accounts() == []


def test_list_accounts_returns_aliases(fresh_tokens_dir):
    _write_token(fresh_tokens_dir / "main.json")
    _write_token(fresh_tokens_dir / "work.json")
    assert auth.list_accounts() == ["main", "work"]


def test_legacy_token_migrates_to_main(tmp_path, monkeypatch):
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    legacy = tmp_path / "token.json"
    _write_token(legacy)
    monkeypatch.setattr(auth, "TOKENS_DIR", tokens_dir)
    monkeypatch.setattr(auth, "_LEGACY_TOKEN", legacy)

    accs = auth.list_accounts()
    assert accs == ["main"]
    assert not legacy.exists()
    assert (tokens_dir / "main.json").exists()


def test_load_existing_valid_credentials(fresh_tokens_dir):
    _write_token(fresh_tokens_dir / "main.json")
    fake_creds = MagicMock(valid=True, expired=False)
    with patch.object(auth.Credentials, "from_authorized_user_file", return_value=fake_creds) as m:
        result = auth.get_credentials("main")
    m.assert_called_once()
    assert result is fake_creds


def test_get_credentials_for_specific_alias(fresh_tokens_dir):
    _write_token(fresh_tokens_dir / "work.json")
    fake_creds = MagicMock(valid=True, expired=False)
    with patch.object(auth.Credentials, "from_authorized_user_file", return_value=fake_creds) as m:
        auth.get_credentials("work")
    called_path = m.call_args.args[0]
    assert called_path.endswith("work.json")


def test_refresh_expired_credentials(fresh_tokens_dir):
    path = fresh_tokens_dir / "main.json"
    _write_token(path)
    fake_creds = MagicMock(valid=False, expired=True, refresh_token="rt")
    fake_creds.to_json.return_value = '{"token": "refreshed"}'
    with patch.object(auth.Credentials, "from_authorized_user_file", return_value=fake_creds), \
         patch.object(auth, "Request") as mock_request:
        auth.get_credentials("main")
    fake_creds.refresh.assert_called_once_with(mock_request.return_value)
    assert path.read_text() == '{"token": "refreshed"}'


def test_runs_oauth_flow_when_no_token(fresh_tokens_dir, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", fresh_tokens_dir.parent / "cs.json")
    fake_creds = MagicMock()
    fake_creds.to_json.return_value = '{"token": "fresh"}'
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds
    with patch.object(auth.InstalledAppFlow, "from_client_secrets_file", return_value=fake_flow):
        auth.get_credentials("new")
    fake_flow.run_local_server.assert_called_once_with(port=0)
    assert (fresh_tokens_dir / "new.json").read_text() == '{"token": "fresh"}'


def test_add_account_runs_flow_and_saves(fresh_tokens_dir, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", fresh_tokens_dir.parent / "cs.json")
    fake_creds = MagicMock()
    fake_creds.to_json.return_value = '{"token": "added"}'
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds
    with patch.object(auth.InstalledAppFlow, "from_client_secrets_file", return_value=fake_flow):
        result = auth.add_account("partner")
    assert (fresh_tokens_dir / "partner.json").exists()
    assert result["account"] == "partner"


def test_remove_account_existing(fresh_tokens_dir):
    _write_token(fresh_tokens_dir / "work.json")
    result = auth.remove_account("work")
    assert result["removed"] is True
    assert not (fresh_tokens_dir / "work.json").exists()


def test_remove_account_missing(fresh_tokens_dir):
    result = auth.remove_account("nope")
    assert result["removed"] is False


# ---------- bug_012 lockdown: rename_account handles case-only on Win/macOS ----------

def test_rename_account_basic(fresh_tokens_dir):
    _write_token(fresh_tokens_dir / "old.json")
    result = auth.rename_account("old", "new")
    assert result["ok"] is True
    assert (fresh_tokens_dir / "new.json").exists()
    assert not (fresh_tokens_dir / "old.json").exists()


def test_rename_account_case_only(fresh_tokens_dir):
    """REGRESSION: ``main`` → ``Main`` on case-insensitive FS (Windows,
    default macOS) previously failed with 'имя уже занято' because
    ``dst.exists()`` saw the same file. Must succeed via two-step rename."""
    src = fresh_tokens_dir / "main.json"
    _write_token(src)
    result = auth.rename_account("main", "Main")
    assert result["ok"] is True, result
    # On case-insensitive FS both names resolve to the same inode; the test
    # passes on Linux too because rename to a different string is supported.
    assert (fresh_tokens_dir / "Main.json").exists()


def test_rename_account_clobber_blocked(fresh_tokens_dir):
    _write_token(fresh_tokens_dir / "a.json")
    _write_token(fresh_tokens_dir / "b.json")
    result = auth.rename_account("a", "b")
    assert result["ok"] is False
    assert "занято" in result["error"]


def test_rename_account_missing_source(fresh_tokens_dir):
    result = auth.rename_account("nope", "new")
    assert result["ok"] is False
    assert "нет" in result["error"]
