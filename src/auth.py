"""Multi-account Google OAuth.

Each Google account gets its own token under `.data/tokens/<alias>.json`.
Aliases are arbitrary strings — use anything memorable (e.g. "main", "work",
"partner", or the email itself).
"""
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from src.config import CLIENT_SECRET_PATH, DATA_DIR, SCOPES


TOKENS_DIR = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(exist_ok=True)

# Legacy single-account token location (auto-migrated to tokens/main.json on first call)
_LEGACY_TOKEN = DATA_DIR / "token.json"


def _token_path(account: str) -> Path:
    return TOKENS_DIR / f"{account}.json"


def _migrate_legacy() -> None:
    main_path = _token_path("main")
    if _LEGACY_TOKEN.exists() and not main_path.exists():
        main_path.write_bytes(_LEGACY_TOKEN.read_bytes())
        _LEGACY_TOKEN.unlink()


def list_accounts() -> list[str]:
    _migrate_legacy()
    return sorted(p.stem for p in TOKENS_DIR.glob("*.json"))


def remove_account(account: str) -> dict:
    path = _token_path(account)
    if not path.exists():
        return {"removed": False, "account": account, "reason": "not found"}
    path.unlink()
    return {"removed": True, "account": account}


def get_credentials(account: str = "main") -> Credentials:
    """Return valid credentials for `account`, refreshing or running browser flow as needed."""
    _migrate_legacy()
    path = _token_path(account)
    creds: Credentials | None = None

    if path.exists():
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        creds = flow.run_local_server(port=0)

    path.write_text(creds.to_json())
    return creds


def add_account(account: str) -> dict:
    """Run the OAuth flow and save the token under `account`. Opens a browser on this machine."""
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    _token_path(account).write_text(creds.to_json())
    return {"account": account, "saved_to": str(_token_path(account))}
