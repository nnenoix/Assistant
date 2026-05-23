import sys
from pathlib import Path


def _app_root() -> Path:
    """Where state and config live.

    - From source: parent of src/ (the project root).
    - Frozen (PyInstaller .exe): the directory containing the .exe itself.
      User drops client_secret_*.json next to the exe; .data/ is created
      alongside on first run.
    """
    if getattr(sys, "frozen", False):  # PyInstaller sets this
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_root() -> Path:
    """Where READ-ONLY resources shipped with the build live.

    - From source: same as _app_root() (static/, bank_parsers/, etc. sit
      next to src/).
    - Frozen: PyInstaller puts the `datas=...` entries from the .spec
      under `sys._MEIPASS` (a temp dir for --onefile, or the `_internal/`
      sibling folder for --onedir). Resources are read-only; the user-
      writable state lives in _app_root().
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent / "_internal"))
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _app_root()
BUNDLE_ROOT = _bundle_root()
DATA_DIR = PROJECT_ROOT / ".data"
SCRIPTS_DIR = DATA_DIR / "scripts"
ALLOWLIST_PATH = DATA_DIR / "allowlist.json"
STATIC_DIR = BUNDLE_ROOT / "static"

# OAuth client secret. Search order:
#  1. Next to the exe (user dropped a custom client there)
#  2. Bundled with the build (PyInstaller datas — Desktop-app client)
#  3. Project root in source mode
# Missing client is no longer fatal — the wizard will surface the issue
# in the UI instead of crashing at import time.
def _find_client_secret() -> Path | None:
    for root in (PROJECT_ROOT, BUNDLE_ROOT):
        try:
            return next(root.glob("client_secret_*.apps.googleusercontent.com.json"))
        except StopIteration:
            continue
    return None


CLIENT_SECRET_PATH = _find_client_secret()


# Auto-update channel. UI's UpdateBanner polls /api/updates/check, which
# reads this env var. Bundling a default here means a user-installed .exe
# checks the right place WITHOUT setup. Override at runtime via env var
# or .env file next to the .exe.
#
# To enable auto-update for your distribution: replace `<OWNER>/<REPO>` with
# your GitHub repo. The release.yml workflow publishes manifest.json to
# `releases/latest/download/` on every `v*` tag — that's the URL pattern below.
import os as _os
_os.environ.setdefault(
    "UPDATE_MANIFEST_URL",
    # Points at the manifest.json produced by .github/workflows/release.yml
    # on every `v*` tag push. Installed .exe instances poll this URL via
    # /api/updates/check and surface a banner when latest_version differs
    # from the bundled version.
    "https://github.com/nnenoix/Assistant/releases/latest/download/manifest.json",
)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/script.projects",      # read/write Apps Script source + versions
    "https://www.googleapis.com/auth/script.deployments",   # create/manage deployments
    "https://www.googleapis.com/auth/script.scriptapp",     # scripts.run via Apps Script API
    "https://www.googleapis.com/auth/drive.activity.readonly",  # discover bound-script IDs via activity log
    "https://www.googleapis.com/auth/cloud-platform",       # GCP API enable, Cloud Logging, project list
    "https://www.googleapis.com/auth/calendar",             # events, free-busy, calendars list
    "https://www.googleapis.com/auth/tasks",                # Google Tasks (todo lists, reminders)
    "https://www.googleapis.com/auth/documents",            # Google Docs — read/write
    "https://www.googleapis.com/auth/presentations",        # Google Slides — read/write
    "https://www.googleapis.com/auth/forms.body",           # Google Forms — create/edit form structure
    "https://www.googleapis.com/auth/forms.responses.readonly",  # Forms — read submissions
    "https://www.googleapis.com/auth/contacts.readonly",    # Google Contacts (People API) — read
    "https://www.googleapis.com/auth/contacts",             # Google Contacts — write
    "https://www.googleapis.com/auth/gmail.settings.basic", # Gmail filters (CRUD) — gmail.modify alone isn't enough
]

DEFAULT_MODEL = "claude-sonnet-4-6"

DATA_DIR.mkdir(exist_ok=True)
SCRIPTS_DIR.mkdir(exist_ok=True)
