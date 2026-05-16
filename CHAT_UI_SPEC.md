# Chat UI Spec — Google Workspace Agent

> Single deliverable for Claude Design. Read top-to-bottom, then produce one file: `static/index.html`. Backend is already finished — do **not** change it. Match the API contract below exactly.

---

## 1. What this product is

A local single-user chat that drives a Claude agent to work on Google Drive, Google Sheets, Google Apps Script projects, and local Excel files. The agent has 32 tools registered under an in-process MCP server. The user types a request in plain language; the agent calls tools to read/write files, and asks for explicit approval before any action not pre-authorized in the allow-list.

The whole app runs locally on the user's Windows machine. FastAPI server on `http://127.0.0.1:8765`. No authentication on the server itself. No deployment, no multi-tenancy.

The user is one person (occasionally two), Russian-speaking, working with several Google accounts simultaneously (their own + partners' accounts they've been granted access to).

## 2. What you need to build

**One file: `static/index.html`.** FastAPI's `GET /` already returns `FileResponse(static/index.html)`. Drop your file there and it works.

Use whatever stack you want as long as the result is a **single self-contained HTML file** (with inline CSS and JS, or with assets loaded from CDN). No build step. The user wants to be able to open and read the file. Vanilla JS or React-from-CDN are both fine; pick what produces the cleanest result for this scope.

There is an existing admin page at `static/accounts.html` for managing Google account logins (described in §6). You may either keep it as a separate page (and link to it from the chat header) **or** integrate the same functionality into the chat. Your call.

## 3. Backend you talk to

All endpoints live on the same origin (`http://127.0.0.1:8765`).

### 3.1 Chat endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/chat` | `{"message": str}` | `{"run_id": str}` |
| GET | `/stream/{run_id}` | — | SSE stream (see §4) |
| POST | `/approve/{request_id}` | `{"approved": bool}` | `{"ok": true}` |

**Flow:**
1. User types something, hits Enter.
2. UI: `POST /chat` with `{message: "..."}`. Receive `{run_id: "uuid"}`.
3. UI opens an SSE connection: `new EventSource('/stream/' + run_id)`.
4. UI receives events one at a time (see §4 for full schema). Render them as they arrive.
5. If an `approval_required` event arrives, show the approval modal. When the user clicks Approve or Deny, send `POST /approve/{request_id} {approved: bool}`. The SSE stream continues automatically once the backend gets your answer.
6. Stream eventually emits `{type: "done"}` — close the EventSource. Input is re-enabled. User can send the next message.

A new turn = a new `run_id`. The backend keeps conversation history server-side across turns within a single server run.

### 3.2 Accounts endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/accounts` | — | `{"accounts": ["main", "elena", ...]}` |
| POST | `/api/accounts` | `{"alias": "elena"}` | `{"ok": bool, "account": str, "hint": str?, "error": str?}` |
| DELETE | `/api/accounts/{alias}` | — | `{"removed": bool, "account": str}` |
| GET | `/accounts` | — | Static HTML (existing admin page) |

Adding an account (`POST /api/accounts`) opens a browser tab on the user's machine for Google OAuth and **blocks** until the user completes (or aborts) the flow — usually 15-60 seconds. The request resolves after the OAuth dance ends. On failure (common when the email isn't in Google Cloud Console's Test users list), the response carries `ok: false` and a `hint` field with a direct URL the user can open to fix it.

## 4. SSE event schema

Each line over the SSE stream is `data: <json>\n\n`. Each `<json>` is one of the following events. **Render every type.** Order is deterministic within a turn; do not buffer or reorder.

### 4.1 `text` — assistant prose
```json
{"type": "text", "text": "I'll list 3 of your most recent Drive files."}
```
Append to the current assistant message bubble. Claude often emits multiple `text` blocks per turn (one before each tool call and one summary at the end). Each `text` event is a complete block, not a partial token — but a single turn typically has 2-5 `text` events interleaved with tool activity.

**Render `text.text` as Markdown.** Claude uses markdown heavily — bold, lists, headings, code blocks, tables. Use a small Markdown library (e.g. `marked`) loaded from CDN, or roll a tiny renderer. Code blocks must have a monospace font.

### 4.2 `tool_call` — agent invoked a tool
```json
{
  "type": "tool_call",
  "tool_use_id": "toolu_abc123",
  "name": "drive_list_files",
  "input": {"folder_id": "root", "page_size": 3}
}
```
Show this inline in the conversation as a small, distinct block — not as a chat bubble. Style suggestion: monospace font, dimmer color, prefix with `→` or an icon. The user wants to see WHAT the agent did, not just the final answer.

The full list of possible `name` values is in §7. The `input` object varies per tool.

### 4.3 `tool_result` — tool returned successfully
```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_abc123",
  "result_preview": "[{\"id\": \"15A_QouAbOKj6...\", \"name\": \"WB_Lib_v2\", \"mimeType\": \"application/vnd.google-apps.script\", ...}, ...]"
}
```
Pair with the prior `tool_call` (match by `tool_use_id`). `result_preview` is a string, max 500 chars, already truncated server-side. Render as a small collapsed block under the tool call; let the user click to expand if it's long. The full result was sent to the model, not to the UI — this is just a peek so the human can see what the agent saw.

### 4.4 `tool_error` — tool raised
```json
{
  "type": "tool_error",
  "tool_use_id": "toolu_abc123",
  "result_preview": "FileNotFoundError: /path/that/does/not/exist"
}
```
Same shape as `tool_result`, but render in red/warning style. The agent receives this too and usually adapts (retries with different inputs).

### 4.5 `approval_required` — user must approve
```json
{
  "type": "approval_required",
  "request_id": "req_a1b2c3",
  "name": "drive_delete",
  "input": {"file_id": "1AbC...xYz"},
  "policy_op": "drive.delete"
}
```
Open the approval modal (§5.3). The stream pauses on the backend until the user resolves it via `POST /approve/{request_id}`. While the modal is open, **disable the chat input** so the user can't start a new turn mid-approval.

### 4.6 `tool_denied` — user denied
```json
{"type": "tool_denied", "name": "drive_delete"}
```
Fires after the user clicks Deny in the approval modal. Render as a strikethrough or red badge inline ("denied: drive_delete"). The agent then receives a tool-result error and typically responds with text explaining what it would have done.

### 4.7 `fatal_error` — agent crashed
```json
{"type": "fatal_error", "error": "ConnectionError: ..."}
```
Show as a banner at the top of the conversation or as a red bubble. Then expect a `done` event right after.

### 4.8 `done` — turn finished
```json
{"type": "done"}
```
Close the EventSource. Re-enable the input field. Clear any "thinking" indicator. The next user message starts a fresh turn.

## 5. UI requirements

### 5.1 Layout

Single-column chat, full viewport height. Three areas vertically:

```
┌──────────────────────────────────────────┐
│  Header  (project name, link to /accounts) │
├──────────────────────────────────────────┤
│                                          │
│  Message log (scrollable, auto-bottom)   │
│  - user messages                         │
│  - assistant text blocks (Markdown)      │
│  - tool calls + results inline           │
│  - errors                                │
│                                          │
├──────────────────────────────────────────┤
│  Input box (textarea + Send button)      │
└──────────────────────────────────────────┘
```

Width: cap chat content at ~720px wide centered, full-bleed background. Tool blocks indent slightly less than text bubbles so the user can scan them.

### 5.2 Message types

| Kind | Visual |
|---|---|
| User message | Right-aligned bubble, accent color (e.g. blue) |
| Assistant text | Left-aligned bubble, neutral background, **Markdown rendered** |
| Tool call | Inline, monospace, prefix `→`, smaller font, muted color |
| Tool result | Inline, monospace, indented under the call, collapsible if `>2` lines |
| Tool error | Same as result but red |
| Denied | Strikethrough badge "denied: <tool_name>" |
| Fatal error | Banner at top OR red bubble |

### 5.3 Approval modal

Triggered by `approval_required`. Centered overlay, dimmed background, click-outside does NOT close it (force a decision).

Content:
- Heading: `Approval required: <name>`
- `policy_op` shown as a small badge (e.g. `drive.delete`)
- `input` shown as pretty-printed JSON in a code block
- Buttons: **Approve** (primary), **Deny** (danger)

When clicked:
- POST to `/approve/{request_id}` with `{approved: true}` or `{approved: false}`
- Close the modal
- The SSE stream resumes — you'll either see `tool_call` + `tool_result` (approved) or `tool_denied` + agent response (denied)

While the modal is open: input is disabled.

If multiple `approval_required` events arrive in sequence (rare but possible), queue them — show one modal at a time, in arrival order.

### 5.4 Input box

- Textarea, multi-line, auto-grow up to ~6 rows
- Placeholder: "Что нужно сделать? Например: «покажи мои последние 5 файлов»"
- Enter = send; Shift+Enter = newline
- Disabled while: a turn is in progress (between POST `/chat` and `done` event), OR an approval modal is open
- Show a subtle spinner or `…` indicator under the last user message while waiting for assistant text

### 5.5 Accounts UI

Two acceptable approaches — pick one:

**(A) Keep accounts on a separate page** at `/accounts` (existing). Add a header link "Аккаунты" in the chat that opens `/accounts` in a new tab.

**(B) Integrate accounts into the chat header.** A dropdown showing configured aliases, plus a small "+ Добавить" button that opens a slide-in panel or modal with the same form fields as `static/accounts.html`. Hits the same `/api/accounts` endpoints.

Option A is simpler; option B is slicker. Either is fine. **Do not duplicate the admin functionality in two places.**

### 5.6 Empty state

When there are no messages yet (fresh session), show a centered hint above the input:

```
Я могу:
  • показывать файлы в Drive (свои и расшаренные)
  • читать и редактировать Google Sheets
  • парсить Excel и заливать данные в Sheets
  • управлять Apps Script проектами
  • работать с несколькими Google-аккаунтами одновременно

Просто опишите что нужно сделать.
```

(Russian by default; consider a language toggle if you want — not required.)

## 6. Existing pages and assets

- `static/accounts.html` — admin page for OAuth account management. Dark theme, vanilla JS. Functional. You may keep, replace, or supersede it.
- No CSS framework included. No bundler. The backend simply serves whatever is under `static/`.
- FastAPI also mounts `/static/*` for any extra assets you create (images, additional pages, etc.).

## 7. Tool name reference

For tooltips, icons, or grouping in the UI, here's the full set of tool names the user may see in `tool_call.name`:

**Drive (12 tools):** `drive_list_files`, `drive_get_metadata`, `drive_list_shared`, `drive_create_folder`, `drive_upload`, `drive_download`, `drive_update_content`, `drive_rename`, `drive_move`, `drive_delete`, `drive_copy`, `drive_search`

**Sheets (7 tools):** `sheets_read_range`, `sheets_write_range`, `sheets_append_rows`, `sheets_clear_range`, `sheets_create_spreadsheet`, `sheets_add_sheet`, `sheets_get_metadata`

**Apps Script (6 tools):** `apps_script_clone`, `apps_script_list_files`, `apps_script_read_file`, `apps_script_write_file`, `apps_script_push`, `apps_script_run`

**Excel (1 tool):** `excel_parse`

**Local FS (3 tools):** `local_read_file`, `local_write_file`, `local_list_dir`

**Auth (3 tools):** `auth_list_accounts`, `auth_add_account`, `auth_remove_account`

You can group these visually if helpful (e.g. show a small Drive/Sheets/Script/Local icon next to each `tool_call`), but it's not required.

## 8. Style direction

- **Dark theme by default.** Background `#0e0f12`, surfaces `#111827`, borders `#1f2937`, primary `#2563eb`, danger `#b91c1c`. System fonts. Match the existing `accounts.html` aesthetic.
- **Calm, dense, professional.** This is a tool, not a toy. Avoid heavy animations, gradients, decorative illustrations.
- **Russian copy by default** — placeholders, buttons, modal text. The user's working language is Russian.
- **Responsive** — works at 1280px+, degrades gracefully on smaller, doesn't need a mobile mode (it's a desktop local tool).
- **Markdown matters.** Code blocks should have a real monospace font and a subtle background tint. Tables should render. Links should be clickable.

## 9. Behavioral requirements (must-haves)

1. **Streaming feels live.** Text appears as `text` events arrive; tool blocks appear interleaved between text.
2. **Auto-scroll to bottom** on new content, BUT pause auto-scroll if the user manually scrolls up. Re-enable when they scroll back to bottom.
3. **No silent failures.** Network errors, SSE drops, 404 on stream — all visible to the user. Reload button on error.
4. **Approval modal is unmissable.** Block input. Center it. Don't allow dismissal except via the two buttons.
5. **History persists within a session.** A page refresh clears history (backend keeps it but UI loses connection — acceptable for v1). Don't add persistence.
6. **One turn at a time.** Don't let the user send a new message while a previous turn is in progress.
7. **Tool call rendering reveals what was done.** The user should be able to skim the history and understand what the agent actually did, not just the final summary.

## 10. Out of scope (do not do)

- Do **not** add login / auth on the FastAPI server. Single user, localhost.
- Do **not** modify backend files (`src/*.py`). If you think the API should be different, write a comment in your HTML noting it — don't change the server.
- Do **not** add analytics, telemetry, or external resources beyond a CDN for Markdown.
- Do **not** implement a typing indicator that fakes streaming — only show indicators when actually waiting.
- Do **not** introduce a build step (no Vite, no Webpack, no TypeScript compilation).
- Do **not** persist messages to localStorage or IndexedDB. Server holds session; UI is stateless.

## 11. Deliverable checklist

- [ ] Single file at `static/index.html`
- [ ] Works when FastAPI is running at `http://127.0.0.1:8765`
- [ ] All 8 SSE event types render correctly
- [ ] Approval modal blocks input
- [ ] Markdown rendering for assistant text
- [ ] Accounts management accessible (either via link to `/accounts` or integrated)
- [ ] Empty state with hints
- [ ] Auto-scroll with manual-scroll override
- [ ] Russian copy
- [ ] Dark theme
- [ ] No build step required

## 12. Quick start for testing your UI

```powershell
cd "D:\Google work"
uv run uvicorn src.app:app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. First message to try:

> покажи 3 моих последних файла в Drive

Expected sequence on the stream:
1. `tool_call` for `drive_list_files`
2. `tool_result` with the file list
3. `text` with a formatted summary
4. `done`

Then try something that requires approval:

> создай папку test-agent в моём Drive

Expected sequence:
1. `tool_call` for `drive_search` or similar
2. `tool_result`
3. `approval_required` for `drive_create_folder` (because `drive.create` is empty in the allow-list) → your modal opens
4. User clicks Approve → `POST /approve/{id} {approved: true}`
5. `tool_call` for `drive_create_folder`
6. `tool_result` with the new folder ID
7. `text` confirming
8. `done`

If these two flows work, the UI is done.

## 13. Reference: backend structure (for context only)

You don't need to touch the backend, but here's the shape so the contract makes sense:

```
src/
  agent.py            ClaudeSDKClient wrapper, emits the SSE events listed in §4
  app.py              FastAPI routes (the API surface above)
  policy.py           Allow-list policy — determines which tool calls auto-run vs trigger approval_required
  auth.py             Multi-account OAuth manager
  tools/
    drive.py          12 Drive operations
    sheets.py         7 Sheets operations
    apps_script.py    6 clasp wrappers
    excel.py          xlsx parser
    local_fs.py       local file ops
    registry.py       wires the 32 tools as a single in-process MCP server
static/
  accounts.html       existing admin page
  index.html          ← you create this
```

Authentication: the agent uses the `claude` CLI's existing OAuth login (no Anthropic API key). For Google Drive/Sheets, each Google account has its own OAuth token under `.data/tokens/<alias>.json`.

That's it. Build the file, drop it at `static/index.html`, refresh the browser.
