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


def add_account_auto() -> dict:
    """Run the OAuth flow without picking an alias in advance. After the
    user finishes consent, look up the bound Google identity and save the
    token under the local-part of the email (e.g. elenatitarenko247@gmail.com
    → elenatitarenko247.json). Returns {alias, email, name, saved_to}.

    If a token under that alias already exists, it's overwritten (re-OAuth
    of the same account). User never types a name.
    """
    import sys as _sys
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=True,
        authorization_prompt_message=(
            "\n>>> If a browser window did NOT open, copy this URL manually:\n{url}\n"
        ),
        success_message="Auth complete — you can close this tab.",
    )
    _sys.stdout.flush()

    # Find the bound Google identity
    from googleapiclient.discovery import build as _build
    svc = _build("drive", "v3", credentials=creds, cache_discovery=False)
    user = svc.about().get(fields="user(emailAddress,displayName)").execute()["user"]
    email = user["emailAddress"]
    name = user.get("displayName")

    # Sanitize email-local-part into a safe filename
    alias_raw = email.split("@", 1)[0]
    alias = "".join(c if c.isalnum() or c in "-_." else "_" for c in alias_raw).strip("._-") or "account"
    path = _token_path(alias)
    path.write_text(creds.to_json())
    return {
        "alias": alias,
        "email": email,
        "name": name,
        "saved_to": str(path),
    }


def add_account(account: str) -> dict:
    """Run the OAuth flow and save the token under `account`. Opens a browser on this machine.

    If the browser doesn't open automatically (e.g. default handler missing on
    Windows), the auth URL is printed to stdout so the user can paste it.

    After the token is saved, calls Drive about().get to identify which Google
    account was actually bound (we've been burned by accidentally picking the
    wrong account in the consent screen). The result includes `bound_email`.
    """
    import sys as _sys
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message=(
            "\n>>> If a browser window did NOT open, copy this URL manually:\n{url}\n"
        ),
        success_message="Auth complete — you can close this tab.",
    )
    _sys.stdout.flush()
    _token_path(account).write_text(creds.to_json())

    # Identify the bound Google account by hitting Drive about().get
    bound_info = describe_account(account)
    return {
        "account": account,
        "saved_to": str(_token_path(account)),
        "bound_email": bound_info.get("email"),
        "bound_name": bound_info.get("name"),
    }


def describe_account(account: str = "main") -> dict:
    """Identify the Google identity bound to `account`'s token. Returns
    {account, email, name, scopes, error?}. Use this right after add_account
    to verify which account the consent screen picked.
    """
    from googleapiclient.discovery import build as _build
    try:
        creds = get_credentials(account)
    except Exception as e:
        return {"account": account, "error": f"no token: {e}"}
    try:
        svc = _build("drive", "v3", credentials=creds, cache_discovery=False)
        u = svc.about().get(fields="user(emailAddress,displayName)").execute()["user"]
        return {
            "account": account,
            "email": u.get("emailAddress"),
            "name": u.get("displayName"),
            "scopes": sorted(creds.scopes or []),
        }
    except Exception as e:
        return {"account": account, "error": str(e)[:300]}


def add_account_incremental(account: str, new_scopes: list[str] | None = None) -> dict:
    """Re-authorize `account` adding `new_scopes` while preserving existing
    scopes. Uses Google's `include_granted_scopes=true` so the user doesn't
    have to re-grant everything. If `new_scopes` is None, just re-runs the
    standard flow with the current SCOPES list.

    If the existing token's grant doesn't cover the new scopes (different
    user, revoked grant), Google will show the full consent screen with
    everything pre-checked.
    """
    import sys as _sys
    # The current token (if any) tells us what scopes are already granted —
    # we tell Google to keep those AND request new ones.
    existing_scopes: list[str] = []
    path = _token_path(account)
    if path.exists():
        try:
            existing = Credentials.from_authorized_user_file(str(path), None)
            existing_scopes = list(existing.scopes or [])
        except Exception:
            pass

    scopes_to_request = sorted(set(existing_scopes) | set(new_scopes or []) | set(SCOPES))

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), scopes_to_request)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message=(
            "\n>>> If a browser window did NOT open, copy this URL manually:\n{url}\n"
        ),
        success_message="Auth complete — you can close this tab.",
        # The two flags Google uses for incremental authorization
        access_type="offline",
        include_granted_scopes="true",
    )
    _sys.stdout.flush()
    path.write_text(creds.to_json())

    bound_info = describe_account(account)
    return {
        "account": account,
        "saved_to": str(path),
        "bound_email": bound_info.get("email"),
        "scopes": sorted(creds.scopes or []),
        "added_scopes": sorted(set(new_scopes or []) - set(existing_scopes)),
    }


def list_accounts_with_identity() -> dict:
    """Like list_accounts but also returns {email, name} for each alias.
    Useful as a sanity check ('main is bound to which Google account?')."""
    out = {}
    for a in list_accounts():
        out[a] = describe_account(a)
    return out
