import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from src import auth


def test_load_existing_valid_credentials(tmp_path, monkeypatch):
    """If token.json exists and is valid, return creds without browser flow."""
    token_path = tmp_path / "token.json"
    fake_token = {
        "token": "fake_access",
        "refresh_token": "fake_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake_client",
        "client_secret": "fake_secret",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }
    token_path.write_text(json.dumps(fake_token))

    monkeypatch.setattr(auth, "TOKEN_PATH", token_path)
    monkeypatch.setattr(auth, "SCOPES", ["https://www.googleapis.com/auth/drive"])

    fake_creds = MagicMock(valid=True, expired=False)
    with patch.object(auth.Credentials, "from_authorized_user_file", return_value=fake_creds) as m:
        result = auth.get_credentials()

    m.assert_called_once_with(str(token_path), ["https://www.googleapis.com/auth/drive"])
    assert result is fake_creds


def test_refresh_expired_credentials(tmp_path, monkeypatch):
    """If creds expired but have refresh_token, refresh and save."""
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    monkeypatch.setattr(auth, "TOKEN_PATH", token_path)

    fake_creds = MagicMock(valid=False, expired=True, refresh_token="rt")
    fake_creds.to_json.return_value = '{"token": "new"}'

    with patch.object(auth.Credentials, "from_authorized_user_file", return_value=fake_creds), \
         patch.object(auth, "Request") as mock_request:
        result = auth.get_credentials()

    fake_creds.refresh.assert_called_once_with(mock_request.return_value)
    assert token_path.read_text() == '{"token": "new"}'
    assert result is fake_creds


def test_runs_oauth_flow_when_no_token(tmp_path, monkeypatch):
    """If no token file, run InstalledAppFlow and save the result."""
    token_path = tmp_path / "token.json"
    monkeypatch.setattr(auth, "TOKEN_PATH", token_path)
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", tmp_path / "cs.json")

    fake_creds = MagicMock()
    fake_creds.to_json.return_value = '{"token": "fresh"}'
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds

    with patch.object(auth.InstalledAppFlow, "from_client_secrets_file", return_value=fake_flow) as m:
        result = auth.get_credentials()

    m.assert_called_once()
    fake_flow.run_local_server.assert_called_once_with(port=0)
    assert token_path.read_text() == '{"token": "fresh"}'
    assert result is fake_creds
