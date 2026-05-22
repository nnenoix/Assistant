"""Multi-account Google OAuth.

Each Google account gets its own token under `.data/tokens/<alias>.json`.
Aliases are arbitrary strings — use anything memorable (e.g. "main", "work",
"partner", or the email itself).
"""
import os
from pathlib import Path

# Google's OAuth server sometimes returns scopes in a different order than
# we requested, or adds/removes adjacent scopes (especially with
# prompt=consent). oauthlib's strict comparison then raises "Scope has
# changed from ..." and refuses the token. Relaxing this is standard
# practice for Google OAuth — must be set BEFORE importing the flow.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.http import HttpRequest

from src.config import CLIENT_SECRET_PATH, DATA_DIR, SCOPES


class RetryingHttpRequest(HttpRequest):
    """googleapiclient HttpRequest with num_retries defaulted to 5.

    googleapiclient already implements exponential backoff for transient
    failures (429 rate-limit, 5xx, network errors) — see
    `googleapiclient.http._retry_request` / `_should_retry_response` — but
    only when `num_retries > 0` is passed to `.execute()`. We bake in a
    sane default so every call site benefits without sprinkling
    `num_retries=` everywhere.

    Sheets per-user-per-minute quota refills inside the retry window
    (~62s worst-case across 5 retries), so transient 429s recover
    transparently instead of bubbling up as tool errors.
    """

    def execute(self, http=None, num_retries=0):
        return super().execute(http=http, num_retries=max(num_retries, 5))


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


def rename_account(old_alias: str, new_alias: str) -> dict:
    """Rename a token alias. Moves .data/tokens/<old>.json to <new>.json.
    Returns {ok, old, new, error?}. Refuses if the destination already
    exists (would clobber another account's token).

    Case-only renames (``main`` → ``Main``) on case-insensitive filesystems
    (Windows/macOS-default) need a two-step rename via a temp path —
    otherwise the FS treats source and dest as the same file and the rename
    silently keeps the old casing."""
    new_alias = new_alias.strip()
    if not new_alias:
        return {"ok": False, "error": "новое имя пустое"}
    if any(c in new_alias for c in "/\\:*?\"<>|"):
        return {"ok": False, "error": "имя содержит запрещённые символы /\\:*?\"<>|"}
    src = _token_path(old_alias)
    if not src.exists():
        return {"ok": False, "error": f"аккаунта {old_alias!r} нет"}
    dst = _token_path(new_alias)
    case_only = old_alias.lower() == new_alias.lower() and old_alias != new_alias
    if dst.exists() and dst != src and not case_only:
        return {"ok": False, "error": f"имя {new_alias!r} уже занято"}
    if old_alias == new_alias:
        return {"ok": True, "old": old_alias, "new": new_alias, "noop": True}
    if case_only:
        tmp = src.with_name(f".__rename__{new_alias}.tmp")
        src.rename(tmp)
        tmp.rename(dst)
    else:
        src.rename(dst)
    return {"ok": True, "old": old_alias, "new": new_alias}


def get_credentials(account: str = "main") -> Credentials:
    """Return valid credentials for `account`, refreshing or running browser flow as needed.

    On refresh, we deliberately don't pass `scopes` (neither config.SCOPES
    nor the stored ones) so google-auth doesn't send a `scope` param to
    Google's token endpoint. That avoids `invalid_scope` errors when
    config.SCOPES grows beyond what the existing token was granted —
    Google simply returns an access token for whatever scopes the refresh
    token already covers. The actual scope coverage is re-read from the
    refresh response and persisted back to the file.
    """
    _migrate_legacy()
    path = _token_path(account)
    creds: Credentials | None = None

    if path.exists():
        # Load with scopes=None: from_authorized_user_info will fall back to
        # whatever 'scopes' key is in the JSON. Passing SCOPES would override
        # that ONLY if the JSON lacked 'scopes' (rare on our tokens).
        # Critically, we then DROP creds.scopes to None before refresh so the
        # request body has no `scope` param — see docstring.
        creds = Credentials.from_authorized_user_file(str(path))

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        # Refresh without sending scope param to avoid invalid_scope on
        # tokens that pre-date config.SCOPES expansions.
        original_scopes = creds.scopes
        try:
            creds._scopes = None  # google-auth uses this attr to build the request body
        except Exception:
            pass
        creds.refresh(Request())
        # Restore granted scopes (refresh response carries them) so callers
        # that introspect creds.scopes see the real grant, not None.
        if not creds.scopes and original_scopes:
            try:
                creds._scopes = original_scopes
            except Exception:
                pass
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
        # prompt='consent' forces Google to show the FULL scope list every
        # time, even ones already granted. Without it Google hides
        # already-granted scopes (the "У приложения уже есть некоторые
        # права" banner) which looks like the app is asking for less than
        # it needs. NB: do NOT combine with include_granted_scopes='true' —
        # the two together can make Google return scopes in a different
        # order than requested, triggering oauthlib's "Scope has changed"
        # warning. We accept the trade-off: each consent re-grants all 10.
        prompt="consent",
        access_type="offline",
    )
    _sys.stdout.flush()

    # Find the bound Google identity
    from googleapiclient.discovery import build as _build
    svc = _build("drive", "v3", credentials=creds, cache_discovery=False, requestBuilder=RetryingHttpRequest)
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
        "scopes_count": len(creds.scopes or []),
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
        svc = _build("drive", "v3", credentials=creds, cache_discovery=False, requestBuilder=RetryingHttpRequest)
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
