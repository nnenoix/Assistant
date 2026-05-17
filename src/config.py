from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / ".data"
SCRIPTS_DIR = DATA_DIR / "scripts"
ALLOWLIST_PATH = DATA_DIR / "allowlist.json"

# OAuth client secret file — match by glob, the engineer has only one
CLIENT_SECRET_PATH = next(
    PROJECT_ROOT.glob("client_secret_*.apps.googleusercontent.com.json"),
    None,
)
if CLIENT_SECRET_PATH is None:
    raise FileNotFoundError(
        "OAuth client secret file not found in project root. "
        "Download it from Google Cloud Console and place it as "
        "client_secret_*.apps.googleusercontent.com.json"
    )

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/script.projects",      # read/write Apps Script source + versions
    "https://www.googleapis.com/auth/script.deployments",   # create/manage deployments
    "https://www.googleapis.com/auth/script.scriptapp",     # scripts.run via Apps Script API
    "https://www.googleapis.com/auth/drive.activity.readonly",  # discover bound-script IDs via activity log
    "https://www.googleapis.com/auth/calendar",             # events, free-busy, calendars list
    "https://www.googleapis.com/auth/tasks",                # Google Tasks (todo lists, reminders)
]

DEFAULT_MODEL = "claude-sonnet-4-6"

DATA_DIR.mkdir(exist_ok=True)
SCRIPTS_DIR.mkdir(exist_ok=True)
