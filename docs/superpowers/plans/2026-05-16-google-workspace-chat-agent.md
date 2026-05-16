# Google Workspace Chat Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Local single-user chat app that drives a Claude-powered agent capable of editing Google Drive files, Google Sheets, and Apps Script projects (via clasp), and ingesting local Excel reports — with an allow-list-based approval policy for dangerous operations.

**Architecture:** A FastAPI server runs on `localhost:8765` serving a single-page chat UI. Each user message starts an agent loop using the raw `anthropic` Python SDK (Messages API with tools). Tools are in-process Python functions that wrap `google-api-python-client` (Drive + Sheets), `openpyxl` (Excel), and `clasp` (Apps Script). A pre-tool-use policy gate compares the tool call against an allow-list (`.data/allowlist.json`); on miss, the agent pauses and emits an approval request to the UI via SSE, the user clicks Approve/Deny, the loop resumes.

**Tech Stack:** Python 3.11+ · `uv` (package + venv) · `anthropic` (raw Messages API) · `google-api-python-client` + `google-auth-oauthlib` · `openpyxl` · `fastapi` + `uvicorn[standard]` · `pytest` · `@google/clasp` (Node CLI, called as subprocess)

**Scope decisions (locked, do not relitigate):**
- Single-user, local-only, Windows host (`D:\Google work\`)
- No multi-user, no auth on the FastAPI server (bound to localhost)
- One global in-memory conversation session per server run (history lost on restart — fine for v1)
- Apps Script: edit + run via `clasp` (not Apps Script API directly)
- Approvals: **allow-list-based** — actions matching the list run silently; others prompt
- OAuth scopes: `drive` + `spreadsheets` only (clasp handles its own auth)
- Model: `claude-sonnet-4-6` default, allow override per request

---

## File Structure

```
D:\Google work\
├── .gitignore                                # excludes secrets, .data, .venv, __pycache__
├── README.md                                 # setup + run
├── pyproject.toml                            # uv project + deps
├── client_secret_*.apps.googleusercontent.com.json   # existing, gitignored
├── .data/                                    # gitignored
│   ├── token.json                            # OAuth refresh token (auto-created)
│   ├── allowlist.json                        # approved paths/IDs (user-editable)
│   └── scripts/                              # clasp project clones
├── src/
│   ├── __init__.py
│   ├── config.py                             # paths and constants
│   ├── auth.py                               # OAuth flow + creds caching
│   ├── policy.py                             # allow-list match logic
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py                       # tool function registry + schemas
│   │   ├── drive.py                          # Google Drive ops
│   │   ├── sheets.py                         # Google Sheets ops
│   │   ├── apps_script.py                    # clasp subprocess wrapper
│   │   ├── excel.py                          # local .xlsx parsing
│   │   └── local_fs.py                       # local file ops (read/write within allowed roots)
│   ├── agent.py                              # Anthropic Messages tool loop
│   └── app.py                                # FastAPI + SSE + approval endpoints
├── static/
│   └── index.html                            # chat UI (single file, vanilla JS)
└── tests/
    ├── __init__.py
    ├── conftest.py                           # shared fixtures
    ├── fixtures/
    │   └── sample.xlsx                       # tiny test workbook
    ├── test_policy.py
    ├── test_excel.py
    ├── test_auth.py
    ├── test_drive_adapter.py                 # adapter calls correct API methods (mocked service)
    ├── test_sheets_adapter.py
    ├── test_agent_loop.py                    # mocked Anthropic client
    └── test_app.py                           # FastAPI TestClient
```

**Testing philosophy:** TDD strictly for pure logic (`policy`, `excel`, `agent.py` loop, FastAPI routes). For Google API wrappers (`drive`, `sheets`, `apps_script`) we test the **adapter layer** — that our code constructs the correct API calls — using a `unittest.mock.MagicMock` of the Google service object. We **do not mock the entire Google API**. Real-API verification happens as **manual smoke tests** documented in the README. This is honest TDD: tests guard the code we own, not the SDK.

---

## Task 0: Project Bootstrap

**Files:**
- Create: `D:\Google work\.gitignore`
- Create: `D:\Google work\pyproject.toml`
- Create: `D:\Google work\src\__init__.py` (empty)
- Create: `D:\Google work\src\config.py`
- Create: `D:\Google work\src\tools\__init__.py` (empty)
- Create: `D:\Google work\tests\__init__.py` (empty)
- Create: `D:\Google work\tests\conftest.py` (empty placeholder)

- [ ] **Step 1: Verify uv is installed**

Run (PowerShell, working dir `D:\Google work`):
```powershell
uv --version
```
Expected: prints a version like `uv 0.5.x`. If not installed: `winget install --id=astral-sh.uv -e` then re-open shell.

- [ ] **Step 2: Initialize git and create `.gitignore`**

Run:
```powershell
git init
```

Create `D:\Google work\.gitignore`:
```gitignore
# Python
.venv/
__pycache__/
*.pyc
.pytest_cache/

# Secrets — never commit
client_secret_*.json
.data/

# Editor
.vscode/
.idea/
```

- [ ] **Step 3: Create `pyproject.toml`**

Create `D:\Google work\pyproject.toml`:
```toml
[project]
name = "google-work-agent"
version = "0.1.0"
description = "Local chat agent that drives Google Drive / Sheets / Apps Script via Claude"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40.0",
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "google-api-python-client>=2.150.0",
    "google-auth-oauthlib>=1.2.0",
    "google-auth-httplib2>=0.2.0",
    "openpyxl>=3.1.5",
    "pydantic>=2.9.0",
    "python-multipart>=0.0.20",
]

[dependency-groups]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.27.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
pythonpath = ["."]
```

- [ ] **Step 4: Install deps and verify**

Run:
```powershell
uv sync
uv run python -c "import anthropic, fastapi, googleapiclient, openpyxl; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 5: Create package skeleton**

Create empty files (use the Write tool with empty content or one-liner `# package` comment):
- `src/__init__.py` → `# package`
- `src/tools/__init__.py` → `# package`
- `tests/__init__.py` → empty
- `tests/conftest.py` → `# shared fixtures`

Create `D:\Google work\src\config.py`:
```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / ".data"
SCRIPTS_DIR = DATA_DIR / "scripts"
TOKEN_PATH = DATA_DIR / "token.json"
ALLOWLIST_PATH = DATA_DIR / "allowlist.json"

# OAuth client secret file — match by glob, the engineer has only one
CLIENT_SECRET_PATH = next(PROJECT_ROOT.glob("client_secret_*.apps.googleusercontent.com.json"))

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

DEFAULT_MODEL = "claude-sonnet-4-6"

DATA_DIR.mkdir(exist_ok=True)
SCRIPTS_DIR.mkdir(exist_ok=True)
```

- [ ] **Step 6: Commit**

```powershell
git add .gitignore pyproject.toml uv.lock src tests
git commit -m "feat: bootstrap python project structure"
```

---

## Task 1: OAuth Credentials Module

**Files:**
- Create: `src/auth.py`
- Create: `tests/test_auth.py`

The module exposes `get_credentials()` which returns a refreshed `google.oauth2.credentials.Credentials` object, running the browser OAuth flow on first call.

- [ ] **Step 1: Write the failing test**

Create `D:\Google work\tests\test_auth.py`:
```python
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
```

- [ ] **Step 2: Run the tests to confirm they fail**

```powershell
uv run pytest tests/test_auth.py -v
```
Expected: 3 errors/failures — `src.auth` doesn't exist yet.

- [ ] **Step 3: Implement `src/auth.py`**

Create `D:\Google work\src\auth.py`:
```python
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from src.config import CLIENT_SECRET_PATH, SCOPES, TOKEN_PATH


def get_credentials() -> Credentials:
    """Return valid Google OAuth credentials, refreshing or running browser flow as needed."""
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    return creds
```

- [ ] **Step 4: Run tests to verify they pass**

```powershell
uv run pytest tests/test_auth.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Manual smoke test — real OAuth flow**

Run a one-liner that triggers the real flow:
```powershell
uv run python -c "from src.auth import get_credentials; c = get_credentials(); print('scopes:', c.scopes); print('saved:', __import__('src.config', fromlist=['TOKEN_PATH']).TOKEN_PATH.exists())"
```
Expected:
- Browser opens to Google consent screen
- After grant, prints `scopes: ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']`
- Prints `saved: True`
- File `.data/token.json` exists

**Pre-condition the engineer must verify before running:** the Google Cloud project (`claude-mcp-496508`) has the OAuth consent screen configured with these scopes, and the user's email is added as a **test user** (otherwise `access_denied`). Drive API and Sheets API must be enabled.

- [ ] **Step 6: Commit**

```powershell
git add src/auth.py tests/test_auth.py
git commit -m "feat: oauth credentials with token caching"
```

---

## Task 2: Allow-List Policy

**Files:**
- Create: `src/policy.py`
- Create: `tests/test_policy.py`

Policy decides: given a tool call, does it match the allow-list (run silently) or require user approval. Format below.

**Allow-list shape (`.data/allowlist.json`):**
```json
{
  "drive": {
    "read": "*",
    "create": ["FOLDER_ID_A"],
    "update": ["FILE_ID_X"],
    "delete": []
  },
  "sheets": {
    "read": "*",
    "write": ["SPREADSHEET_ID_1"]
  },
  "local": {
    "read": ["D:/Google work/inputs"],
    "write": ["D:/Google work/outputs"]
  },
  "apps_script": {
    "edit": [],
    "run": []
  }
}
```

A value of `"*"` means "all" (no approval needed). A list of IDs/paths is an explicit allow-list. Missing keys default to `[]` (always require approval).

- [ ] **Step 1: Write the failing tests**

Create `D:\Google work\tests\test_policy.py`:
```python
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
```

- [ ] **Step 2: Run tests, confirm failure**

```powershell
uv run pytest tests/test_policy.py -v
```
Expected: ImportError — `src.policy` doesn't exist.

- [ ] **Step 3: Implement `src/policy.py`**

Create `D:\Google work\src\policy.py`:
```python
import json
from pathlib import Path
from typing import Any


class Policy:
    def __init__(self, rules: dict[str, dict[str, list[str] | str]]):
        self._rules = rules

    @classmethod
    def load(cls, path: Path) -> "Policy":
        if not path.exists():
            return cls({})
        return cls(json.loads(path.read_text()))

    def is_allowed(self, operation: str, args: dict[str, Any]) -> bool:
        """operation format: 'category.action' e.g. 'drive.create'."""
        if "." not in operation:
            return False
        category, action = operation.split(".", 1)
        category_rules = self._rules.get(category, {})
        allow = category_rules.get(action, [])

        if allow == "*":
            return True
        if not isinstance(allow, list) or not allow:
            return False

        return self._matches(category, action, args, allow)

    @staticmethod
    def _matches(category: str, action: str, args: dict, allow: list[str]) -> bool:
        if category == "drive":
            key = {"create": "parent_id", "update": "file_id", "delete": "file_id", "read": "file_id"}.get(action)
            return args.get(key) in allow if key else False
        if category == "sheets":
            return args.get("spreadsheet_id") in allow
        if category == "local":
            target = args.get("path", "")
            target_norm = Path(target).resolve().as_posix().lower() if target else ""
            for root in allow:
                root_norm = Path(root).resolve().as_posix().lower()
                if target_norm == root_norm or target_norm.startswith(root_norm + "/"):
                    return True
            return False
        if category == "apps_script":
            return args.get("script_id") in allow
        return False
```

- [ ] **Step 4: Run tests, confirm pass**

```powershell
uv run pytest tests/test_policy.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Create initial allowlist scaffold**

Create `D:\Google work\.data\allowlist.json`:
```json
{
  "drive": {
    "read": "*",
    "create": [],
    "update": [],
    "delete": []
  },
  "sheets": {
    "read": "*",
    "write": []
  },
  "local": {
    "read": ["D:/Google work"],
    "write": []
  },
  "apps_script": {
    "edit": [],
    "run": []
  }
}
```

(The user edits this file manually to grant standing permissions to specific folders/spreadsheets.)

- [ ] **Step 6: Commit**

```powershell
git add src/policy.py tests/test_policy.py
git commit -m "feat: allow-list policy with category-aware matching"
```

---

## Task 3: Excel Reading Tool

**Files:**
- Create: `src/tools/excel.py`
- Create: `tests/test_excel.py`
- Create: `tests/fixtures/sample.xlsx` (programmatically — see step 1)

- [ ] **Step 1: Generate the test fixture**

Create `D:\Google work\tests\fixtures\__init__.py` (empty).

Run once to generate `sample.xlsx`:
```powershell
uv run python -c "
from openpyxl import Workbook
from pathlib import Path
wb = Workbook()
ws1 = wb.active
ws1.title = 'Sales'
ws1.append(['date', 'product', 'amount'])
ws1.append(['2026-01-01', 'A', 100])
ws1.append(['2026-01-02', 'B', 250])
ws2 = wb.create_sheet('Costs')
ws2.append(['category', 'value'])
ws2.append(['rent', 1000])
Path('tests/fixtures').mkdir(parents=True, exist_ok=True)
wb.save('tests/fixtures/sample.xlsx')
print('written')
"
```

- [ ] **Step 2: Write the failing test**

Create `D:\Google work\tests\test_excel.py`:
```python
from pathlib import Path

from src.tools.excel import parse_xlsx


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xlsx"


def test_parses_all_sheets():
    result = parse_xlsx(str(FIXTURE))
    assert set(result.keys()) == {"Sales", "Costs"}


def test_sales_sheet_rows():
    result = parse_xlsx(str(FIXTURE))
    assert result["Sales"] == [
        {"date": "2026-01-01", "product": "A", "amount": 100},
        {"date": "2026-01-02", "product": "B", "amount": 250},
    ]


def test_costs_sheet_rows():
    result = parse_xlsx(str(FIXTURE))
    assert result["Costs"] == [{"category": "rent", "value": 1000}]


def test_parse_single_sheet():
    result = parse_xlsx(str(FIXTURE), sheet="Sales")
    assert isinstance(result, list)
    assert len(result) == 2
```

- [ ] **Step 3: Run test, confirm failure**

```powershell
uv run pytest tests/test_excel.py -v
```
Expected: ImportError.

- [ ] **Step 4: Implement `src/tools/excel.py`**

Create `D:\Google work\src\tools\excel.py`:
```python
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


def _cell(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10] if isinstance(value, date) and not isinstance(value, datetime) else value.isoformat()
    return value


def _sheet_to_rows(ws) -> list[dict]:
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows_iter))
    except StopIteration:
        return []
    return [
        {h: _cell(v) for h, v in zip(header, row) if h is not None}
        for row in rows_iter
        if any(c is not None for c in row)
    ]


def parse_xlsx(path: str, sheet: str | None = None) -> dict[str, list[dict]] | list[dict]:
    """Parse an .xlsx workbook. Returns {sheet_name: [row_dicts]} or [row_dicts] if `sheet` given."""
    if not Path(path).exists():
        raise FileNotFoundError(path)
    wb = load_workbook(path, data_only=True, read_only=True)
    if sheet is not None:
        if sheet not in wb.sheetnames:
            raise ValueError(f"sheet {sheet!r} not in {wb.sheetnames}")
        return _sheet_to_rows(wb[sheet])
    return {name: _sheet_to_rows(wb[name]) for name in wb.sheetnames}
```

- [ ] **Step 5: Run tests, confirm pass**

```powershell
uv run pytest tests/test_excel.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```powershell
git add src/tools/excel.py tests/test_excel.py tests/fixtures/sample.xlsx tests/fixtures/__init__.py
git commit -m "feat: openpyxl-based xlsx parser returning row dicts"
```

---

## Task 4: Google Drive Tool

**Files:**
- Create: `src/tools/drive.py`
- Create: `tests/test_drive_adapter.py`

Operations exposed to the agent: `list`, `get_metadata`, `create_folder`, `upload`, `download`, `update_content`, `rename`, `move`, `delete`, `copy`, `search`.

- [ ] **Step 1: Write the adapter tests**

Create `D:\Google work\tests\test_drive_adapter.py`:
```python
from unittest.mock import MagicMock, patch

import pytest

from src.tools import drive


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(drive, "_service", return_value=svc):
        yield svc


def test_list_files_passes_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "1", "name": "f"}]}
    result = drive.list_files(folder_id="ROOT", query=None)
    fake_service.files().list.assert_called_with(
        q="'ROOT' in parents and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,size,parents)",
        pageSize=200,
    )
    assert result == [{"id": "1", "name": "f"}]


def test_list_files_with_extra_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.list_files(folder_id="ROOT", query="name contains 'report'")
    fake_service.files().list.assert_called_with(
        q="'ROOT' in parents and trashed = false and (name contains 'report')",
        fields="files(id,name,mimeType,modifiedTime,size,parents)",
        pageSize=200,
    )


def test_create_folder(fake_service):
    fake_service.files().create().execute.return_value = {"id": "NEW", "name": "X"}
    result = drive.create_folder(parent_id="P", name="X")
    fake_service.files().create.assert_called_with(
        body={"name": "X", "mimeType": "application/vnd.google-apps.folder", "parents": ["P"]},
        fields="id,name,mimeType,parents",
    )
    assert result == {"id": "NEW", "name": "X"}


def test_delete(fake_service):
    fake_service.files().delete().execute.return_value = None
    drive.delete(file_id="ABC")
    fake_service.files().delete.assert_called_with(fileId="ABC")


def test_rename(fake_service):
    fake_service.files().update().execute.return_value = {"id": "ABC", "name": "newname"}
    drive.rename(file_id="ABC", new_name="newname")
    fake_service.files().update.assert_called_with(
        fileId="ABC", body={"name": "newname"}, fields="id,name"
    )


def test_move(fake_service):
    fake_service.files().get().execute.return_value = {"parents": ["OLD"]}
    fake_service.files().update().execute.return_value = {"id": "ABC", "parents": ["NEW"]}
    drive.move(file_id="ABC", new_parent_id="NEW")
    fake_service.files().update.assert_called_with(
        fileId="ABC",
        addParents="NEW",
        removeParents="OLD",
        fields="id,parents",
    )


def test_search(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "1"}]}
    drive.search("foo bar")
    fake_service.files().list.assert_called_with(
        q="name contains 'foo bar' and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=50,
    )
```

- [ ] **Step 2: Run, confirm failure**

```powershell
uv run pytest tests/test_drive_adapter.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `src/tools/drive.py`**

Create `D:\Google work\src\tools\drive.py`:
```python
from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from src.auth import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"


@lru_cache(maxsize=1)
def _service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def list_files(folder_id: str = "root", query: str | None = None) -> list[dict]:
    q = f"'{folder_id}' in parents and trashed = false"
    if query:
        q += f" and ({query})"
    resp = _service().files().list(
        q=q,
        fields="files(id,name,mimeType,modifiedTime,size,parents)",
        pageSize=200,
    ).execute()
    return resp.get("files", [])


def get_metadata(file_id: str) -> dict:
    return _service().files().get(
        fileId=file_id,
        fields="id,name,mimeType,modifiedTime,size,parents,webViewLink",
    ).execute()


def create_folder(parent_id: str, name: str) -> dict:
    return _service().files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id,name,mimeType,parents",
    ).execute()


def upload(local_path: str, parent_id: str, name: str | None = None, mime_type: str | None = None) -> dict:
    p = Path(local_path)
    if not p.exists():
        raise FileNotFoundError(local_path)
    media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)
    body = {"name": name or p.name, "parents": [parent_id]}
    return _service().files().create(
        body=body, media_body=media, fields="id,name,mimeType,parents,webViewLink"
    ).execute()


def download(file_id: str, dest_path: str) -> str:
    import io
    request = _service().files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()
    return dest_path


def update_content(file_id: str, local_path: str, mime_type: str | None = None) -> dict:
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    return _service().files().update(
        fileId=file_id, media_body=media, fields="id,name,modifiedTime"
    ).execute()


def rename(file_id: str, new_name: str) -> dict:
    return _service().files().update(
        fileId=file_id, body={"name": new_name}, fields="id,name"
    ).execute()


def move(file_id: str, new_parent_id: str) -> dict:
    meta = _service().files().get(fileId=file_id, fields="parents").execute()
    old_parents = ",".join(meta.get("parents", []))
    return _service().files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parents,
        fields="id,parents",
    ).execute()


def delete(file_id: str) -> None:
    _service().files().delete(fileId=file_id).execute()


def copy(file_id: str, new_name: str | None = None, parent_id: str | None = None) -> dict:
    body = {}
    if new_name:
        body["name"] = new_name
    if parent_id:
        body["parents"] = [parent_id]
    return _service().files().copy(
        fileId=file_id, body=body, fields="id,name,parents"
    ).execute()


def search(name_contains: str) -> list[dict]:
    resp = _service().files().list(
        q=f"name contains '{name_contains}' and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=50,
    ).execute()
    return resp.get("files", [])
```

- [ ] **Step 4: Run tests, confirm pass**

```powershell
uv run pytest tests/test_drive_adapter.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Manual smoke test**

```powershell
uv run python -c "from src.tools.drive import list_files; import json; print(json.dumps(list_files()[:5], indent=2, default=str))"
```
Expected: prints up to 5 files from the user's My Drive root. If permission error → re-check OAuth scopes in `token.json`, delete it and re-run Task 1 step 5.

- [ ] **Step 6: Commit**

```powershell
git add src/tools/drive.py tests/test_drive_adapter.py
git commit -m "feat: google drive adapter (list/create/upload/download/rename/move/delete/copy/search)"
```

---

## Task 5: Google Sheets Tool

**Files:**
- Create: `src/tools/sheets.py`
- Create: `tests/test_sheets_adapter.py`

Operations: `read_range`, `write_range`, `append_rows`, `clear_range`, `create_spreadsheet`, `add_sheet`, `get_metadata`.

- [ ] **Step 1: Write the adapter tests**

Create `D:\Google work\tests\test_sheets_adapter.py`:
```python
from unittest.mock import MagicMock, patch

import pytest

from src.tools import sheets


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(sheets, "_service", return_value=svc):
        yield svc


def test_read_range(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {"values": [["a", "b"], ["c", "d"]]}
    result = sheets.read_range(spreadsheet_id="SID", range="Sheet1!A1:B2")
    fake_service.spreadsheets().values().get.assert_called_with(
        spreadsheetId="SID", range="Sheet1!A1:B2"
    )
    assert result == [["a", "b"], ["c", "d"]]


def test_read_range_empty_returns_empty_list(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {}
    assert sheets.read_range("SID", "Sheet1!A1") == []


def test_write_range(fake_service):
    fake_service.spreadsheets().values().update().execute.return_value = {"updatedCells": 4}
    result = sheets.write_range("SID", "Sheet1!A1:B2", [[1, 2], [3, 4]])
    fake_service.spreadsheets().values().update.assert_called_with(
        spreadsheetId="SID",
        range="Sheet1!A1:B2",
        valueInputOption="USER_ENTERED",
        body={"values": [[1, 2], [3, 4]]},
    )
    assert result == {"updatedCells": 4}


def test_append_rows(fake_service):
    fake_service.spreadsheets().values().append().execute.return_value = {"updates": {"updatedRows": 2}}
    sheets.append_rows("SID", "Sheet1!A1", [["x"], ["y"]])
    fake_service.spreadsheets().values().append.assert_called_with(
        spreadsheetId="SID",
        range="Sheet1!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [["x"], ["y"]]},
    )


def test_clear_range(fake_service):
    fake_service.spreadsheets().values().clear().execute.return_value = {}
    sheets.clear_range("SID", "Sheet1!A:Z")
    fake_service.spreadsheets().values().clear.assert_called_with(
        spreadsheetId="SID", range="Sheet1!A:Z", body={}
    )


def test_create_spreadsheet(fake_service):
    fake_service.spreadsheets().create().execute.return_value = {"spreadsheetId": "NEW", "spreadsheetUrl": "..."}
    result = sheets.create_spreadsheet(title="My Report")
    fake_service.spreadsheets().create.assert_called_with(
        body={"properties": {"title": "My Report"}},
        fields="spreadsheetId,spreadsheetUrl,properties.title",
    )
    assert result == {"spreadsheetId": "NEW", "spreadsheetUrl": "..."}


def test_add_sheet(fake_service):
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 99, "title": "T"}}}]
    }
    result = sheets.add_sheet("SID", "T")
    fake_service.spreadsheets().batchUpdate.assert_called_with(
        spreadsheetId="SID",
        body={"requests": [{"addSheet": {"properties": {"title": "T"}}}]},
    )
    assert result == {"sheetId": 99, "title": "T"}
```

- [ ] **Step 2: Run, confirm failure**

```powershell
uv run pytest tests/test_sheets_adapter.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `src/tools/sheets.py`**

Create `D:\Google work\src\tools\sheets.py`:
```python
from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import get_credentials


@lru_cache(maxsize=1)
def _service():
    return build("sheets", "v4", credentials=get_credentials(), cache_discovery=False)


def read_range(spreadsheet_id: str, range: str) -> list[list]:
    resp = _service().spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range
    ).execute()
    return resp.get("values", [])


def write_range(spreadsheet_id: str, range: str, values: list[list]) -> dict:
    return _service().spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def append_rows(spreadsheet_id: str, range: str, values: list[list]) -> dict:
    return _service().spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def clear_range(spreadsheet_id: str, range: str) -> dict:
    return _service().spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range, body={}
    ).execute()


def create_spreadsheet(title: str) -> dict:
    return _service().spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId,spreadsheetUrl,properties.title",
    ).execute()


def add_sheet(spreadsheet_id: str, title: str) -> dict:
    resp = _service().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]


def get_metadata(spreadsheet_id: str) -> dict:
    return _service().spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="spreadsheetId,properties.title,sheets.properties",
    ).execute()
```

- [ ] **Step 4: Run tests, confirm pass**

```powershell
uv run pytest tests/test_sheets_adapter.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```powershell
git add src/tools/sheets.py tests/test_sheets_adapter.py
git commit -m "feat: google sheets adapter (read/write/append/clear/create/add_sheet)"
```

---

## Task 6: Apps Script via clasp

**Files:**
- Create: `src/tools/apps_script.py`
- Verify: clasp installed and `clasp login` completed

The clasp wrapper shells out to `clasp` binary. Project clones live under `.data/scripts/<script_id>/`.

- [ ] **Step 1: Verify clasp is installed and logged in**

Run:
```powershell
clasp --version
clasp login --status
```
If not installed: `npm install -g @google/clasp` (requires Node.js).
If not logged in: `clasp login` — opens browser for separate OAuth (this is clasp's own consent flow, independent of our `token.json`).

Note: For `clasp run` to work, the target script must be deployed as an **API executable** in its Apps Script editor (`Deploy → New deployment → API executable`). The engineer should do this once per script project they want to run via clasp.

- [ ] **Step 2: Implement `src/tools/apps_script.py`**

Create `D:\Google work\src\tools\apps_script.py`:
```python
import json
import subprocess
from pathlib import Path

from src.config import SCRIPTS_DIR


class ClaspError(RuntimeError):
    pass


def _run_clasp(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["clasp", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=True,
    )
    if proc.returncode != 0:
        raise ClaspError(f"clasp {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _project_dir(script_id: str) -> Path:
    return SCRIPTS_DIR / script_id


def clone(script_id: str) -> dict:
    """Clone a script project into .data/scripts/<id>/. Idempotent — pulls if dir exists."""
    target = _project_dir(script_id)
    if target.exists() and any(target.iterdir()):
        return pull(script_id)
    target.mkdir(parents=True, exist_ok=True)
    output = _run_clasp(["clone", script_id, "--rootDir", "."], cwd=target)
    return {"script_id": script_id, "local_dir": str(target), "stdout": output.strip()}


def pull(script_id: str) -> dict:
    output = _run_clasp(["pull"], cwd=_project_dir(script_id))
    return {"script_id": script_id, "local_dir": str(_project_dir(script_id)), "stdout": output.strip()}


def push(script_id: str) -> dict:
    output = _run_clasp(["push", "--force"], cwd=_project_dir(script_id))
    return {"script_id": script_id, "stdout": output.strip()}


def list_files(script_id: str) -> list[str]:
    p = _project_dir(script_id)
    if not p.exists():
        raise ClaspError(f"project not cloned: {script_id}")
    return sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))


def read_file(script_id: str, relpath: str) -> str:
    return (_project_dir(script_id) / relpath).read_text(encoding="utf-8")


def write_file(script_id: str, relpath: str, content: str) -> dict:
    target = _project_dir(script_id) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}


def run_function(script_id: str, function_name: str, params: list | None = None) -> dict:
    """Run a function in the deployed API executable. Requires deployment first."""
    args = ["run", function_name]
    if params:
        args += ["--params", json.dumps(params)]
    output = _run_clasp(args, cwd=_project_dir(script_id))
    return {"script_id": script_id, "function": function_name, "output": output.strip()}
```

- [ ] **Step 3: Manual smoke test**

Pick an existing Apps Script (or create a tiny one in script.google.com) and copy its script ID from the URL. Replace `YOUR_SCRIPT_ID` below:
```powershell
uv run python -c "from src.tools.apps_script import clone, list_files; r = clone('YOUR_SCRIPT_ID'); print(r); print(list_files('YOUR_SCRIPT_ID'))"
```
Expected: clones into `.data/scripts/YOUR_SCRIPT_ID/`, prints file list (typically `appsscript.json`, `Code.js`).

Tests for this module are skipped — the wrapper is too tightly coupled to the `clasp` binary's stdout to test meaningfully with mocks. Trust the smoke test.

- [ ] **Step 4: Commit**

```powershell
git add src/tools/apps_script.py
git commit -m "feat: clasp subprocess wrapper for apps script projects"
```

---

## Task 7: Tool Registry + Local FS

**Files:**
- Create: `src/tools/local_fs.py`
- Create: `src/tools/registry.py`

Registry exposes each tool function with: name, Python callable, JSON Schema for Anthropic Messages API, and the policy operation key.

- [ ] **Step 1: Implement local_fs.py**

Create `D:\Google work\src\tools\local_fs.py`:
```python
from pathlib import Path


def read_file(path: str) -> str:
    """Read a local text file (UTF-8). For binary, use drive.upload pattern instead."""
    return Path(path).read_text(encoding="utf-8")


def write_file(path: str, content: str) -> dict:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p.resolve()), "bytes_written": len(content.encode("utf-8"))}


def list_dir(path: str) -> list[dict]:
    p = Path(path)
    return [
        {"name": e.name, "is_dir": e.is_dir(), "size": (e.stat().st_size if e.is_file() else None)}
        for e in sorted(p.iterdir())
    ]
```

- [ ] **Step 2: Build the registry**

Create `D:\Google work\src\tools\registry.py`:
```python
"""Single source of truth for tool name → callable, schema, policy op."""
from src.tools import apps_script, drive, excel, local_fs, sheets


def _tool(name, fn, policy_op, description, input_schema):
    return {
        "name": name,
        "fn": fn,
        "policy_op": policy_op,
        "schema": {"name": name, "description": description, "input_schema": input_schema},
    }


TOOLS = [
    # --- Drive ---
    _tool(
        "drive_list_files",
        drive.list_files,
        "drive.read",
        "List files in a Google Drive folder. folder_id='root' for My Drive root.",
        {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "default": "root"},
                "query": {"type": "string", "description": "Optional Drive query, e.g. \"name contains 'report'\""},
            },
        },
    ),
    _tool(
        "drive_get_metadata",
        drive.get_metadata,
        "drive.read",
        "Get metadata for a Drive file by id.",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_create_folder",
        drive.create_folder,
        "drive.create",
        "Create a new folder inside parent_id.",
        {"type": "object", "properties": {"parent_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["parent_id", "name"]},
    ),
    _tool(
        "drive_upload",
        drive.upload,
        "drive.create",
        "Upload a local file to Drive folder parent_id.",
        {
            "type": "object",
            "properties": {
                "local_path": {"type": "string"},
                "parent_id": {"type": "string"},
                "name": {"type": "string"},
                "mime_type": {"type": "string"},
            },
            "required": ["local_path", "parent_id"],
        },
    ),
    _tool(
        "drive_download",
        drive.download,
        "drive.read",
        "Download a Drive file to a local path.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "dest_path": {"type": "string"}}, "required": ["file_id", "dest_path"]},
    ),
    _tool(
        "drive_update_content",
        drive.update_content,
        "drive.update",
        "Replace the content of an existing Drive file from a local file.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "local_path": {"type": "string"}, "mime_type": {"type": "string"}}, "required": ["file_id", "local_path"]},
    ),
    _tool(
        "drive_rename",
        drive.rename,
        "drive.update",
        "Rename a Drive file/folder.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "new_name": {"type": "string"}}, "required": ["file_id", "new_name"]},
    ),
    _tool(
        "drive_move",
        drive.move,
        "drive.update",
        "Move a Drive file to a new parent folder.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "new_parent_id": {"type": "string"}}, "required": ["file_id", "new_parent_id"]},
    ),
    _tool(
        "drive_delete",
        drive.delete,
        "drive.delete",
        "Permanently delete a Drive file (no trash).",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_copy",
        drive.copy,
        "drive.create",
        "Copy a Drive file.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "new_name": {"type": "string"}, "parent_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_search",
        drive.search,
        "drive.read",
        "Search files by name substring across all of My Drive.",
        {"type": "object", "properties": {"name_contains": {"type": "string"}}, "required": ["name_contains"]},
    ),
    # --- Sheets ---
    _tool(
        "sheets_read_range",
        sheets.read_range,
        "sheets.read",
        "Read a range from a Google Sheet. range example: 'Sheet1!A1:C100'.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}}, "required": ["spreadsheet_id", "range"]},
    ),
    _tool(
        "sheets_write_range",
        sheets.write_range,
        "sheets.write",
        "Overwrite a range with values (list of rows). Formulas allowed.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["spreadsheet_id", "range", "values"],
        },
    ),
    _tool(
        "sheets_append_rows",
        sheets.append_rows,
        "sheets.write",
        "Append rows below existing data in the given range.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["spreadsheet_id", "range", "values"],
        },
    ),
    _tool(
        "sheets_clear_range",
        sheets.clear_range,
        "sheets.write",
        "Clear all values in a range.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}}, "required": ["spreadsheet_id", "range"]},
    ),
    _tool(
        "sheets_create_spreadsheet",
        sheets.create_spreadsheet,
        "sheets.write",
        "Create a brand-new spreadsheet.",
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    ),
    _tool(
        "sheets_add_sheet",
        sheets.add_sheet,
        "sheets.write",
        "Add a new tab/sheet to an existing spreadsheet.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "title": {"type": "string"}}, "required": ["spreadsheet_id", "title"]},
    ),
    _tool(
        "sheets_get_metadata",
        sheets.get_metadata,
        "sheets.read",
        "Get spreadsheet metadata: title and list of sheets/tabs.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}}, "required": ["spreadsheet_id"]},
    ),
    # --- Apps Script ---
    _tool(
        "apps_script_clone",
        apps_script.clone,
        "apps_script.edit",
        "Clone (or pull) an Apps Script project to local .data/scripts/.",
        {"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]},
    ),
    _tool(
        "apps_script_list_files",
        apps_script.list_files,
        "apps_script.edit",
        "List files in a cloned Apps Script project.",
        {"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]},
    ),
    _tool(
        "apps_script_read_file",
        apps_script.read_file,
        "apps_script.edit",
        "Read a file from a cloned Apps Script project.",
        {"type": "object", "properties": {"script_id": {"type": "string"}, "relpath": {"type": "string"}}, "required": ["script_id", "relpath"]},
    ),
    _tool(
        "apps_script_write_file",
        apps_script.write_file,
        "apps_script.edit",
        "Write a file in a cloned Apps Script project (local only, call apps_script_push to upload).",
        {"type": "object", "properties": {"script_id": {"type": "string"}, "relpath": {"type": "string"}, "content": {"type": "string"}}, "required": ["script_id", "relpath", "content"]},
    ),
    _tool(
        "apps_script_push",
        apps_script.push,
        "apps_script.edit",
        "Push local Apps Script project changes to Google.",
        {"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]},
    ),
    _tool(
        "apps_script_run",
        apps_script.run_function,
        "apps_script.run",
        "Run a function in an Apps Script that has been deployed as API executable.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "function_name": {"type": "string"},
                "params": {"type": "array"},
            },
            "required": ["script_id", "function_name"],
        },
    ),
    # --- Excel ---
    _tool(
        "excel_parse",
        excel.parse_xlsx,
        "local.read",
        "Parse a local .xlsx file into row dicts. If `sheet` given, returns rows for that sheet only.",
        {"type": "object", "properties": {"path": {"type": "string"}, "sheet": {"type": "string"}}, "required": ["path"]},
    ),
    # --- Local FS ---
    _tool(
        "local_read_file",
        local_fs.read_file,
        "local.read",
        "Read a local text file.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    _tool(
        "local_write_file",
        local_fs.write_file,
        "local.write",
        "Write a local text file (creates parent dirs).",
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    ),
    _tool(
        "local_list_dir",
        local_fs.list_dir,
        "local.read",
        "List entries in a local directory.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
]


BY_NAME = {t["name"]: t for t in TOOLS}
ANTHROPIC_SCHEMAS = [t["schema"] for t in TOOLS]
```

- [ ] **Step 2: Quick sanity check**

```powershell
uv run python -c "from src.tools.registry import TOOLS, BY_NAME; print(len(TOOLS), 'tools'); print(sorted(BY_NAME.keys()))"
```
Expected: prints `28 tools` and a sorted list of tool names.

- [ ] **Step 3: Commit**

```powershell
git add src/tools/local_fs.py src/tools/registry.py
git commit -m "feat: tool registry mapping name to fn/schema/policy op"
```

---

## Task 8: Agent Loop

**Files:**
- Create: `src/agent.py`
- Create: `tests/test_agent_loop.py`

The loop:
1. Sends user message + history + tool schemas to Anthropic
2. For each `tool_use` block in the response: check policy → if allowed, run immediately; if not, emit `approval_required` event and `await` an approval future
3. Append assistant message and `tool_result`s, loop until `stop_reason != "tool_use"`
4. Emit text deltas (we'll do non-streaming v1, document the streaming upgrade path)

- [ ] **Step 1: Write the agent loop test**

Create `D:\Google work\tests\test_agent_loop.py`:
```python
import asyncio
from unittest.mock import MagicMock

import pytest

from src.agent import AgentSession


def make_anthropic_response(content_blocks, stop_reason="end_turn"):
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


def text_block(text):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def tool_block(id, name, input):
    b = MagicMock()
    b.type = "tool_use"
    b.id = id
    b.name = name
    b.input = input
    return b


def _tool_entry(name, fn, policy_op):
    """Helper: build a tool dict matching what registry.BY_NAME produces."""
    return {
        "fn": fn,
        "policy_op": policy_op,
        "schema": {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}},
    }


@pytest.mark.asyncio
async def test_simple_text_response_no_tools():
    fake_client = MagicMock()
    fake_client.messages.create.return_value = make_anthropic_response([text_block("Hello!")])
    session = AgentSession(client=fake_client, policy=MagicMock(), tools={})

    events = []
    async def emit(e): events.append(e)

    await session.run_turn("hi", emit)

    text_events = [e for e in events if e["type"] == "text"]
    assert any("Hello!" in e["text"] for e in text_events)
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_allowed_tool_runs_silently():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        make_anthropic_response([tool_block("tu_1", "drive_list_files", {"folder_id": "root"})], stop_reason="tool_use"),
        make_anthropic_response([text_block("Done.")]),
    ]
    fake_policy = MagicMock()
    fake_policy.is_allowed.return_value = True
    tools = {"drive_list_files": _tool_entry("drive_list_files", lambda **kw: [{"id": "1"}], "drive.read")}

    session = AgentSession(client=fake_client, policy=fake_policy, tools=tools)

    events = []
    async def emit(e): events.append(e)

    await session.run_turn("list", emit)

    fake_policy.is_allowed.assert_called_with("drive.read", {"folder_id": "root"})
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "drive_list_files"
    assert not any(e["type"] == "approval_required" for e in events)


@pytest.mark.asyncio
async def test_denied_tool_waits_for_approval_and_runs_on_approve():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        make_anthropic_response([tool_block("tu_2", "drive_delete", {"file_id": "X"})], stop_reason="tool_use"),
        make_anthropic_response([text_block("Deleted.")]),
    ]
    fake_policy = MagicMock(); fake_policy.is_allowed.return_value = False
    called = []
    tools = {"drive_delete": _tool_entry("drive_delete", lambda **kw: called.append(kw) or None, "drive.delete")}

    session = AgentSession(client=fake_client, policy=fake_policy, tools=tools)

    events = []
    async def emit(e): events.append(e)

    task = asyncio.create_task(session.run_turn("delete X", emit))
    await asyncio.sleep(0.05)

    pending = [e for e in events if e["type"] == "approval_required"]
    assert len(pending) == 1
    request_id = pending[0]["request_id"]

    session.resolve_approval(request_id, approved=True)
    await task

    assert called == [{"file_id": "X"}]


@pytest.mark.asyncio
async def test_denied_tool_returns_error_on_deny():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        make_anthropic_response([tool_block("tu_3", "drive_delete", {"file_id": "X"})], stop_reason="tool_use"),
        make_anthropic_response([text_block("Skipped.")]),
    ]
    fake_policy = MagicMock(); fake_policy.is_allowed.return_value = False
    deleted = []
    tools = {"drive_delete": _tool_entry("drive_delete", lambda **kw: deleted.append(kw), "drive.delete")}

    session = AgentSession(client=fake_client, policy=fake_policy, tools=tools)
    events = []
    async def emit(e): events.append(e)

    task = asyncio.create_task(session.run_turn("delete X", emit))
    await asyncio.sleep(0.05)

    request_id = next(e["request_id"] for e in events if e["type"] == "approval_required")
    session.resolve_approval(request_id, approved=False)
    await task

    assert deleted == []  # function not called
    # Second call should have received a tool_result with is_error
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    last_msg = second_call_kwargs["messages"][-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"][0]["is_error"] is True
```

- [ ] **Step 2: Run, confirm failure**

```powershell
uv run pytest tests/test_agent_loop.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `src/agent.py`**

Create `D:\Google work\src\agent.py`:
```python
import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

from anthropic import Anthropic

from src.config import DEFAULT_MODEL


SYSTEM_PROMPT = """You are a personal assistant operating on the user's Google Workspace and local machine.

You have tools for:
- Google Drive: list/search/create/upload/download/rename/move/delete/copy files
- Google Sheets: read/write/append ranges, create spreadsheets, add tabs
- Apps Script: clone/pull/push/run script projects via clasp
- Local filesystem: read/write files, list directories
- Excel (.xlsx): parse local workbooks into row dicts

Rules:
1. Always confirm with the user before destructive actions (delete, overwrite) unless they explicitly said "yes, do it" in this turn.
2. When the user references a file/folder by name, search first to find the id, then ask which one if ambiguous.
3. Prefer `sheets_append_rows` over `sheets_write_range` when adding data.
4. For Excel-to-Sheets pipelines: parse with `excel_parse`, then write via `sheets_write_range` or `sheets_append_rows`.
5. Report what you did with file IDs and links so the user can verify.
6. If a tool returns an error, read the error message and adapt — do not silently ignore.
"""


Emit = Callable[[dict], Awaitable[None]]


class AgentSession:
    def __init__(self, client: Anthropic, policy, tools: dict[str, dict], model: str = DEFAULT_MODEL):
        self.client = client
        self.policy = policy
        self.tools = tools
        self.model = model
        self.history: list[dict] = []
        self._pending_approvals: dict[str, asyncio.Future] = {}

    def resolve_approval(self, request_id: str, approved: bool) -> None:
        fut = self._pending_approvals.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(approved)

    async def run_turn(self, user_message: str, emit: Emit) -> None:
        self.history.append({"role": "user", "content": user_message})

        while True:
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=self.model,
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                tools=[t["schema"] for t in self.tools.values()] if self.tools else [],
                messages=self.history,
            )

            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    await emit({"type": "text", "text": block.text})
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use", "id": block.id, "name": block.name, "input": block.input,
                    })

            self.history.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool = self.tools.get(block.name)
                if tool is None:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Unknown tool: {block.name}",
                        "is_error": True,
                    })
                    continue

                allowed = self.policy.is_allowed(tool["policy_op"], block.input)
                if not allowed:
                    request_id = str(uuid.uuid4())
                    fut: asyncio.Future = asyncio.get_event_loop().create_future()
                    self._pending_approvals[request_id] = fut
                    await emit({
                        "type": "approval_required",
                        "request_id": request_id,
                        "tool_use_id": block.id,
                        "name": block.name,
                        "input": block.input,
                        "policy_op": tool["policy_op"],
                    })
                    approved = await fut
                    if not approved:
                        await emit({"type": "tool_denied", "tool_use_id": block.id, "name": block.name})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "User denied this action.",
                            "is_error": True,
                        })
                        continue

                await emit({"type": "tool_call", "tool_use_id": block.id, "name": block.name, "input": block.input})
                try:
                    result = await asyncio.to_thread(tool["fn"], **block.input)
                    content_str = json.dumps(result, default=str) if result is not None else "(no output)"
                    await emit({"type": "tool_result", "tool_use_id": block.id, "result_preview": content_str[:500]})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content_str})
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    await emit({"type": "tool_error", "tool_use_id": block.id, "error": err})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": err, "is_error": True})

            self.history.append({"role": "user", "content": tool_results})

        await emit({"type": "done"})
```

- [ ] **Step 4: Run tests, confirm pass**

```powershell
uv run pytest tests/test_agent_loop.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```powershell
git add src/agent.py tests/test_agent_loop.py
git commit -m "feat: agent loop with policy gate and async approval flow"
```

---

## Task 9: FastAPI App + SSE + Approval Endpoints

**Files:**
- Create: `src/app.py`
- Create: `tests/test_app.py`

Endpoints:
- `GET /` → serves `static/index.html`
- `POST /chat` body `{message: str}` → starts agent turn, returns `{run_id}` (or session_id if first call)
- `GET /stream/{run_id}` → SSE of agent events
- `POST /approve/{request_id}` body `{approved: bool}` → resolves pending approval

- [ ] **Step 1: Write test for the app**

Create `D:\Google work\tests\test_app.py`:
```python
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

import src.app as app_module


def test_chat_returns_run_id():
    fake_session = MagicMock()
    async def fake_run_turn(msg, emit):
        await emit({"type": "text", "text": "ok"})
        await emit({"type": "done"})
    fake_session.run_turn = fake_run_turn

    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert "run_id" in resp.json()


def test_stream_emits_events_in_sse_format():
    fake_session = MagicMock()
    async def fake_run_turn(msg, emit):
        await emit({"type": "text", "text": "ok"})
        await emit({"type": "done"})
    fake_session.run_turn = fake_run_turn

    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        run_id = client.post("/chat", json={"message": "hi"}).json()["run_id"]
        with client.stream("GET", f"/stream/{run_id}") as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode()
    assert 'data: {"type": "text"' in body
    assert 'data: {"type": "done"}' in body


def test_approve_resolves_pending():
    fake_session = MagicMock()
    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        resp = client.post("/approve/abc-123", json={"approved": True})
    assert resp.status_code == 200
    fake_session.resolve_approval.assert_called_with("abc-123", True)
```

- [ ] **Step 2: Run, confirm failure**

```powershell
uv run pytest tests/test_app.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `src/app.py`**

Create `D:\Google work\src\app.py`:
```python
import asyncio
import json
import uuid
from pathlib import Path

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.agent import AgentSession
from src.config import ALLOWLIST_PATH, PROJECT_ROOT
from src.policy import Policy
from src.tools.registry import BY_NAME


app = FastAPI(title="Google Workspace Chat Agent")

STATIC_DIR = PROJECT_ROOT / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_session = AgentSession(
    client=Anthropic(),
    policy=Policy.load(ALLOWLIST_PATH),
    tools=BY_NAME,
)

_run_queues: dict[str, asyncio.Queue] = {}


class ChatRequest(BaseModel):
    message: str


class ApproveRequest(BaseModel):
    approved: bool


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/chat")
async def chat(req: ChatRequest):
    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[run_id] = queue

    async def emit(event: dict):
        await queue.put(event)

    async def runner():
        try:
            await _session.run_turn(req.message, emit)
        except Exception as e:
            await emit({"type": "fatal_error", "error": f"{type(e).__name__}: {e}"})
            await emit({"type": "done"})

    asyncio.create_task(runner())
    return {"run_id": run_id}


@app.get("/stream/{run_id}")
async def stream(run_id: str):
    queue = _run_queues.get(run_id)
    if queue is None:
        raise HTTPException(404, "unknown run_id")

    async def gen():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            _run_queues.pop(run_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/approve/{request_id}")
async def approve(request_id: str, body: ApproveRequest):
    _session.resolve_approval(request_id, body.approved)
    return {"ok": True}
```

- [ ] **Step 4: Run tests, confirm pass**

```powershell
uv run pytest tests/test_app.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```powershell
git add src/app.py tests/test_app.py
git commit -m "feat: fastapi app with sse stream and approval endpoint"
```

---

## Task 10: Chat UI

**Files:**
- Create: `static/index.html`

Single page: a chat log, an input box, an approval modal. Vanilla JS, no framework.

- [ ] **Step 1: Implement the UI**

Create `D:\Google work\static\index.html`:
```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Google Work Agent</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background: #0e0f12; color: #e5e7eb; display: grid; grid-template-rows: 1fr auto; height: 100vh; }
  #log { overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  .msg { padding: 10px 14px; border-radius: 10px; max-width: 760px; white-space: pre-wrap; }
  .msg.user { background: #1f2937; align-self: flex-end; }
  .msg.assistant { background: #111827; align-self: flex-start; }
  .msg.tool { background: #0b3b2e; align-self: flex-start; font-family: ui-monospace, monospace; font-size: 12px; }
  .msg.error { background: #5a1e1e; align-self: flex-start; font-family: ui-monospace, monospace; }
  form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #1f2937; background: #0e0f12; }
  textarea { flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #1f2937; background: #111827; color: #e5e7eb; resize: none; min-height: 44px; max-height: 200px; }
  button { padding: 10px 14px; border: 0; border-radius: 8px; background: #2563eb; color: #fff; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: wait; }
  #modal { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; }
  #modal.show { display: flex; }
  .card { background: #1f2937; padding: 18px; border-radius: 12px; max-width: 560px; }
  .card pre { background: #0b1220; padding: 10px; border-radius: 8px; overflow-x: auto; font-size: 12px; }
  .card .actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
  .deny { background: #b91c1c; }
</style>
</head>
<body>
<div id="log"></div>
<form id="form">
  <textarea id="input" placeholder="Type your request… (Enter to send, Shift+Enter for newline)"></textarea>
  <button id="send">Send</button>
</form>

<div id="modal">
  <div class="card">
    <h3 id="m-title">Approval required</h3>
    <p id="m-desc"></p>
    <pre id="m-args"></pre>
    <div class="actions">
      <button class="deny" id="m-deny">Deny</button>
      <button id="m-approve">Approve</button>
    </div>
  </div>
</div>

<script>
const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
const modal = document.getElementById('modal');
const mTitle = document.getElementById('m-title');
const mDesc = document.getElementById('m-desc');
const mArgs = document.getElementById('m-args');
const mApprove = document.getElementById('m-approve');
const mDeny = document.getElementById('m-deny');

function add(cls, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}

let currentAssistant = null;
let pendingRequestId = null;

function showApproval(ev) {
  pendingRequestId = ev.request_id;
  mTitle.textContent = `Approval: ${ev.name}`;
  mDesc.textContent = `Policy: ${ev.policy_op}`;
  mArgs.textContent = JSON.stringify(ev.input, null, 2);
  modal.classList.add('show');
}

async function decide(approved) {
  modal.classList.remove('show');
  await fetch(`/approve/${pendingRequestId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({approved}),
  });
  pendingRequestId = null;
}
mApprove.onclick = () => decide(true);
mDeny.onclick = () => decide(false);

form.onsubmit = async (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  add('user', message);
  input.value = '';
  send.disabled = true;
  currentAssistant = null;

  const r = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message})});
  const {run_id} = await r.json();

  const es = new EventSource(`/stream/${run_id}`);
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'text') {
      if (!currentAssistant) currentAssistant = add('assistant', '');
      currentAssistant.textContent += ev.text;
    } else if (ev.type === 'tool_call') {
      add('tool', `→ ${ev.name}(${JSON.stringify(ev.input)})`);
      currentAssistant = null;
    } else if (ev.type === 'tool_result') {
      add('tool', `← ${ev.result_preview}`);
    } else if (ev.type === 'tool_error') {
      add('error', `tool error: ${ev.error}`);
    } else if (ev.type === 'tool_denied') {
      add('error', `denied: ${ev.name}`);
    } else if (ev.type === 'approval_required') {
      showApproval(ev);
    } else if (ev.type === 'fatal_error') {
      add('error', `fatal: ${ev.error}`);
    } else if (ev.type === 'done') {
      es.close();
      send.disabled = false;
      input.focus();
    }
  };
  es.onerror = () => { es.close(); send.disabled = false; };
};

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});
</script>
</body>
</html>
```

- [ ] **Step 2: Manual smoke — launch server**

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # set your key (or rely on existing env var)
uv run uvicorn src.app:app --host 127.0.0.1 --port 8765
```
Open `http://127.0.0.1:8765` in browser. Type: `List the 5 most recent files in my Drive`.
Expected: assistant calls `drive_list_files`, the call happens silently (drive.read = "*"), result appears, assistant summarizes.

Then type: `Create a folder called "agent-test" in my Drive root.`
Expected: approval modal appears (drive.create has empty allow-list). Click Approve. Folder is created. Verify in `drive.google.com`.

- [ ] **Step 3: Commit**

```powershell
git add static/index.html
git commit -m "feat: minimal chat ui with sse stream and approval modal"
```

---

## Task 11: End-to-End Smoke Test + README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Run an end-to-end pipeline scenario manually**

In the chat UI:
1. **Setup:** drop a real `.xlsx` file into `D:\Google work\inputs\sales.xlsx` (create the folder if needed; ensure it's in `allowlist.json` under `local.read`).
2. **Prompt:** `Parse D:\Google work\inputs\sales.xlsx and create a new Google Sheet called "Sales Import" with the data.`
3. **Expected:**
   - Agent calls `excel_parse` (silent, on allow-list)
   - Agent calls `sheets_create_spreadsheet` (approval modal — Approve)
   - Agent calls `sheets_write_range` (approval — Approve OR add the new spreadsheet ID to allowlist first)
   - Agent reports the spreadsheet URL
4. **Verify:** open the URL, confirm rows match `sales.xlsx`.

- [ ] **Step 2: Write the README**

Create `D:\Google work\README.md`:
```markdown
# Google Workspace Chat Agent

Local single-user chat that drives Claude to manage Google Drive, Sheets, Apps Script, and local Excel files.

## Prerequisites
- Windows + PowerShell
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) — `winget install --id=astral-sh.uv -e`
- Node.js (for clasp): https://nodejs.org/
- clasp: `npm install -g @google/clasp` then `clasp login`
- Anthropic API key in `$env:ANTHROPIC_API_KEY`

## One-time Google setup
The OAuth client is already configured (`client_secret_*.apps.googleusercontent.com.json`, project `claude-mcp-496508`).

In Google Cloud Console for that project:
1. **APIs & Services → Library** — enable: Google Drive API, Google Sheets API.
2. **OAuth consent screen** — add your email as a Test user. Confirm scopes include `.../auth/drive` and `.../auth/spreadsheets`.

## Install
```powershell
uv sync
```

## First run (triggers OAuth)
```powershell
uv run python -c "from src.auth import get_credentials; get_credentials()"
```
A browser will open. Grant access. `token.json` is saved under `.data/`.

## Start the chat server
```powershell
uv run uvicorn src.app:app --host 127.0.0.1 --port 8765
```
Open http://127.0.0.1:8765 in your browser.

## Allow-list
Edit `.data/allowlist.json` to grant standing permission for specific folders/spreadsheets/paths. Anything not listed will prompt for approval per-action.

Schema:
- `drive.read|create|update|delete`: `"*"` (all), or list of folder/file IDs
- `sheets.read|write`: `"*"` or list of spreadsheet IDs
- `local.read|write`: list of absolute path roots
- `apps_script.edit|run`: list of script IDs

## Tests
```powershell
uv run pytest -v
```

## Known wrinkles
- **Apps Script `run`** requires the target script to be deployed as **API executable** (Deploy → New deployment → API executable) and clasp must be logged in. If you only need to edit code, `clone` + `push` work without deployment.
- **Drive `delete` is permanent** (skips trash). Always approve manually.
- **Token expiry**: if `token.json` was created with fewer scopes than now configured, delete it and re-run the first-run command.
- **Conversation history** is in-memory per server run — restart wipes it. (Easy upgrade: persist `_session.history` to disk per turn.)
```

- [ ] **Step 3: Run the full test suite**

```powershell
uv run pytest -v
```
Expected: all tests pass — 36 tests across 7 files (auth=3, policy=8, excel=4, drive_adapter=7, sheets_adapter=7, agent_loop=4, app=3).

- [ ] **Step 4: Final commit**

```powershell
git add README.md
git commit -m "docs: readme with setup, run, and known wrinkles"
```

---

## Spec Coverage Self-Review

Mapping user requirements → tasks:

| Requirement | Covered by |
|---|---|
| Chat interface | Task 9 (FastAPI) + Task 10 (HTML) |
| "Console version of Claude Code" capability | Task 8 (raw `anthropic` SDK tool loop) — note: we use the SDK directly, not the `claude` CLI, because the CLI is process-per-turn and approval injection is harder. Same agent behavior, better integration. |
| Drive: create/edit/delete files | Task 4 + Task 7 registry (`drive_create_folder`, `drive_upload`, `drive_update_content`, `drive_delete`, `drive_rename`, `drive_move`, `drive_copy`) |
| Apps Script management | Task 6 (clasp wrapper) + Task 7 registry (`apps_script_*`) |
| Edit Google Sheets freely | Task 5 + Task 7 (`sheets_read_range`, `sheets_write_range`, `sheets_append_rows`, `sheets_clear_range`, `sheets_create_spreadsheet`, `sheets_add_sheet`) |
| Collect data from Excel reports on disk | Task 3 (`excel.parse_xlsx`) + Task 7 (`local_list_dir`, `local_read_file`) |
| Upload results to Drive | Task 4 (`drive.upload`, `drive.update_content`) |
| "With permission" / approvals | Task 2 (policy) + Task 8 (approval flow in loop) + Task 10 (modal in UI) |
| Allow-list for auto-approval | Task 2 + Task 8 (`is_allowed` checked before every tool call) |

All user requirements are covered.

## Execution Notes (do not skip)

- **No streaming inside one turn (v1):** the agent waits for the full Anthropic response before emitting events. This is intentional simplicity. To add token-by-token streaming later, swap `client.messages.create` for `client.messages.stream` and emit `text_delta` events from the stream iterator.
- **One global session:** v1 has a single `AgentSession` in `app.py`. If you want multi-user or multi-tab, key sessions by a cookie/header and store them in a dict.
- **Path quoting on Windows:** all shell commands quote `"D:\Google work"` because of the space. Inside Python the path is fine as a raw string.
- **`SCRIPTS_DIR.mkdir()` in config.py** runs on import — harmless side effect, makes `.data/scripts/` exist before clasp tries to use it.
