# Google Workspace Chat Agent

Local single-user chat that drives Claude to manage Google Drive, Sheets, Apps Script, and local Excel files. Built with [`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python) — uses your `claude` CLI authentication (Pro/Max subscription), **no Anthropic API key required**.

## Prerequisites
- Windows + PowerShell
- Python 3.11+ (verified with 3.11.0)
- [uv](https://github.com/astral-sh/uv) — `winget install --id=astral-sh.uv -e`
- Node.js (for `clasp` and the `claude` CLI): https://nodejs.org/
- **Claude Code CLI** logged in: `npm install -g @anthropic-ai/claude-code` then `claude login` (Pro/Max subscription)
- **clasp**: `npm install -g @google/clasp` then `clasp login` (separate Google OAuth, only needed for Apps Script)

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
Open http://127.0.0.1:8765 in your browser — the `/` route will 404 until a UI is added (see below).

The API works without a UI: POST `/chat`, then GET `/stream/{run_id}` for SSE, POST `/approve/{request_id}` for approvals. See `src/app.py` for the contract.

## Building the UI
The chat UI (`static/index.html`) is intentionally not included. Build it separately — e.g., via [Claude Design](https://claude.com/) — to interact with the backend.

UI requirements:
- Input box → POST `/chat {message: str}` → receive `{run_id}`
- Open `EventSource('/stream/{run_id}')` for SSE events
- Render event types: `text`, `tool_call`, `tool_result`, `tool_error`, `tool_denied`, `approval_required`, `fatal_error`, `done`
- On `approval_required`: show modal with `name`, `input`, `policy_op` → user clicks Approve/Deny → POST `/approve/{request_id} {approved: bool}`

## Allow-list
Edit `.data/allowlist.json` to grant standing permission for specific folders/spreadsheets/paths. Anything not listed will emit an `approval_required` event per-action.

Schema:
- `drive.read|create|update|delete`: `"*"` (all), or list of folder/file IDs
- `sheets.read|write`: `"*"` or list of spreadsheet IDs
- `local.read|write`: list of absolute path roots (path-traversal-safe via `Path.resolve()` prefix check)
- `apps_script.edit|run`: list of script IDs

Default scaffold grants `read` everywhere and read-only on `D:/Google work`. All writes require approval.

## Tests
```powershell
uv run pytest -v
```
43 tests across 7 files: auth (3), policy (9), excel (5), drive_adapter (8), sheets_adapter (7), agent_loop (6: policy/approval glue, prefix-stripping, no SDK end-to-end), app (3).

## Project layout
```
src/
  config.py          path constants, OAuth scopes, default model
  auth.py            google OAuth credentials
  policy.py          allow-list policy gate
  agent.py           ClaudeSDKClient wrapper with policy/approval bridge
  app.py             FastAPI server + SSE
  tools/
    drive.py         google drive adapter
    sheets.py        google sheets adapter
    apps_script.py   clasp subprocess wrapper (path-traversal-guarded)
    excel.py         openpyxl xlsx parser
    local_fs.py      local file ops
    registry.py      28 tools wrapped as SDK @tool, exposed via in-process MCP server
.data/               gitignored — token.json, allowlist.json, scripts/
tests/               pytest suite
```

## Known wrinkles
- **First run requires `claude login`** — uses your Pro/Max subscription via the `claude` CLI. The agent never reads `ANTHROPIC_API_KEY`.
- **Apps Script `run`** requires the target script to be deployed as **API executable** (Deploy → New deployment → API executable) and clasp must be logged in. If you only need to edit code, `clone` + `push` work without deployment.
- **Drive `delete` is permanent** (skips trash). The default allow-list requires approval for it.
- **Token expiry (Google OAuth)**: if `token.json` was created with fewer scopes than now configured, delete it and re-run the first-run command.
- **Conversation context** is managed by the SDK session inside `AgentSession`; restart wipes it. The SDK supports `session_id`/`resume` for cross-process continuity — not wired up here.
- **Tool surface**: Claude only sees our 28 tools (whitelisted via `allowed_tools`). Built-in `claude` CLI tools (Bash, Read, Write, etc.) are blocked — every tool call routes through `can_use_tool` → our Policy gate.
