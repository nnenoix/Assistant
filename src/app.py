import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import auth, chats
from src.agent import AgentSession, KNOWN_MODELS
from src.config import ALLOWLIST_PATH, DATA_DIR, PROJECT_ROOT, STATIC_DIR as _STATIC_DIR
from src.policy import Policy


UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# Reject uploads larger than this (per batch). A misbehaving client (or
# browser tab) streaming infinity bytes to /api/upload would otherwise fill
# the disk.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

# Localhost CSRF defense: state-changing requests must originate from us
# (pywebview, or a local browser tab opened to this server). Curl / agent
# subprocess on the user's own machine sends no Origin → allow.
_ALLOWED_ORIGINS = {f"http://127.0.0.1:8765", f"http://localhost:8765"}

# Match JWTs (the WB token format is the most common one we'd leak — three
# base64url chunks separated by dots). Used to scrub credentials out of
# fatal_error messages before they hit the chat log on disk.
import re as _re
_JWT_RE = _re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _scrub_secrets(text: str) -> str:
    return _JWT_RE.sub("<jwt-redacted>", text)

# File-extension → semantic kind. Used by /api/upload to tag uploads so the
# UI can pick an icon and the agent can pick a tool (bank_parse_statement
# for pdf, excel_parse for excel, etc.).
_KIND_BY_SUFFIX = {
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".bmp": "image",
    ".pdf": "pdf",
    ".xlsx": "excel", ".xls": "excel", ".xlsm": "excel",
    ".txt": "text", ".md": "text", ".csv": "text",
    ".json": "text", ".yaml": "text", ".yml": "text", ".log": "text",
}

logger = logging.getLogger("workspace_agent")
WATCHER_INTERVAL_SEC = 300  # 5 min between Cloud Logging polls
WATCHER_LOOKBACK_MIN = 30   # how far back each poll looks


GCP_TEST_USERS_URL = "https://console.cloud.google.com/auth/audience?project=claude-mcp-496508"


async def _watcher_loop():
    """Background poller. Every WATCHER_INTERVAL_SEC, scans known scripts'
    Cloud Logging for failures and appends new ones to .data/alerts.json.
    Failures during polling are logged but don't kill the loop.
    """
    from src.tools import watcher
    while True:
        try:
            await asyncio.sleep(WATCHER_INTERVAL_SEC)
            r = await asyncio.to_thread(watcher.poll_known_scripts, WATCHER_LOOKBACK_MIN)
            if r["new_alerts"] > 0:
                logger.info(f"[watcher] {r['new_alerts']} new alerts from {r['checked_scripts']} scripts")
                # Push to any open SSE streams so user sees immediately
                for alert in r["alerts_added"]:
                    payload = {"type": "alert", "alert": alert}
                    for q in list(_run_queues.values()):
                        try:
                            q.put_nowait(payload)
                        except Exception:
                            pass
            if r.get("errors"):
                logger.warning(f"[watcher] {len(r['errors'])} scripts errored on polling")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[watcher] loop iteration failed: {type(e).__name__}: {e}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_watcher_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Google Workspace Chat Agent", lifespan=_lifespan)


@app.get("/health")
async def _health():
    """Liveness probe — used by Docker HEALTHCHECK + Kubernetes readinessProbe."""
    return {"status": "ok"}


# Phase 0: tenant-id propagation middleware. Binds `request.state.tenant_id`
# and ContextVar `src.tenancy.current_tenant_id()` for downstream tools.
try:
    from src.tenancy import add_tenant_middleware
    add_tenant_middleware(app)
except Exception as _e:
    import logging
    logging.getLogger(__name__).warning(f"tenant middleware install failed: {_e}")


# Phase 0: optionally expose MCP Streamable HTTP transport for external
# clients (LibreChat, Open WebUI, etc.). Gated by ENABLE_MCP_HTTP=1 env;
# no-op when off so the desktop app's local-only behavior is unchanged.
try:
    from src.mcp_http import mount_mcp_http
    mount_mcp_http(app)
except Exception as _e:
    import logging
    logging.getLogger(__name__).warning(f"MCP HTTP mount failed: {_e}")


# Phase 0: /metrics for Prometheus scrape. Zero deps — text exposition is
# emitted by hand. config/prometheus.yml already targets this path. The
# counters are populated by `src.tools.registry._wrap_for_sdk` whenever
# the agent invokes a tool.
try:
    from src.metrics import mount_metrics
    mount_metrics(app)
except Exception as _e:
    import logging
    logging.getLogger(__name__).warning(f"metrics mount failed: {_e}")


@app.middleware("http")
async def _origin_gate(request, call_next):
    """Reject state-changing requests with a cross-origin `Origin` header.
    Localhost-only server, but a malicious page on evil.com could otherwise
    POST to our endpoints from the user's browser. No-Origin requests
    (curl, native pywebview shell) pass through."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and origin not in _ALLOWED_ORIGINS:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
    return await call_next(request)

STATIC_DIR = _STATIC_DIR
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_session = AgentSession(policy=Policy.load(ALLOWLIST_PATH))

_run_queues: dict[str, asyncio.Queue] = {}
_chat_log: chats.ChatLog | None = None  # currently active chat


async def _switch_to_chat(chat_id: str | None) -> chats.ChatLog:
    """Resolve which chat the next message belongs to.

    - `chat_id=None` → start a fresh chat. If a different chat was active
      before, close the Claude SDK client so the new conversation starts
      with a clean context.
    - `chat_id="<existing>"` → load that chat. If it's the SAME one
      currently active, no-op. If DIFFERENT, close the session so the
      new client gets a recap injection on its first turn (see _maybe_
      inject_recap below).
    """
    global _chat_log
    if chat_id is None:
        # Always start fresh — close current session if any
        if _chat_log is not None:
            await _session.close()
        _chat_log = chats.ChatLog.start_new()
        return _chat_log
    # Continuing an existing chat
    if _chat_log is not None and _chat_log.data.get("id") == chat_id:
        # Same chat — keep current SDK session
        return _chat_log
    # Switching to a different existing chat — reset session
    if _chat_log is not None:
        await _session.close()
    _chat_log = chats.load_chat_log(chat_id)
    return _chat_log


def _maybe_inject_recap(log: chats.ChatLog) -> str:
    """If this chat already has prior messages AND the SDK session is
    fresh (we just switched chats), return a recap to prepend to the
    user's message. Returns "" when no injection is needed."""
    # Recap is needed when there are messages already saved AND the
    # SDK client doesn't yet exist for this session (i.e. fresh after
    # _session.close()). The next call to run_turn() will create the
    # client, and the user_message includes the recap as context.
    if _session._client is not None:
        return ""  # client kept across this turn — no recap needed
    messages = log.data.get("messages", [])
    if not messages:
        return ""  # brand-new chat
    recap = chats.render_history_for_resume(log.data["id"])
    if not recap:
        return ""
    return recap + "\n\n## New user message:\n"


class ChatRequest(BaseModel):
    message: str
    chat_id: str | None = None  # continue this chat; None = start new
    attachments: list[dict] | None = None  # [{path, name, kind}, ...]


class ApproveRequest(BaseModel):
    approved: bool


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/chat")
async def chat(req: ChatRequest):
    # Resolve which chat we're talking into. May close the previous
    # Claude SDK session if the user switched chats.
    try:
        log = await _switch_to_chat(req.chat_id)
    except FileNotFoundError:
        raise HTTPException(404, f"chat {req.chat_id!r} not found")
    log.append_user(req.message)

    # If we just resumed an existing chat (session is fresh), prepend
    # the previous history as context so the new SDK client doesn't
    # start blind.
    recap = _maybe_inject_recap(log)

    # Bake attachment paths into the message so the agent sees them.
    message = req.message
    if req.attachments:
        atts_lines = []
        for a in req.attachments:
            name = a.get("name") or "(unnamed)"
            path = a.get("path")
            kind = a.get("kind", "file")
            if path:
                atts_lines.append(f"  - {kind}: {name} → {path}")
        if atts_lines:
            message = (
                req.message
                + "\n\n[Attachments — local paths the user just shared:]\n"
                + "\n".join(atts_lines)
            )
    if recap:
        message = recap + message

    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[run_id] = queue

    async def emit(event: dict):
        await queue.put(event)
        try:
            log.append_event(event)
        except Exception:
            pass

    # Tell the UI which chat this turn belongs to — frontend uses it to
    # update its sidebar (highlight + bump-to-top) and to seed the
    # currentChatId for follow-up messages.
    await emit({
        "type": "chat_meta",
        "chat_id": log.data["id"],
        "title": log.data.get("title"),
        "resumed": bool(recap),  # tells UI we replayed history
    })

    async def runner():
        try:
            await _session.run_turn(message, emit)
        except Exception as e:
            await emit({"type": "fatal_error", "error": _scrub_secrets(f"{type(e).__name__}: {e}")})
            await emit({"type": "done"})
        finally:
            # Close the SDK client after every turn so the next /chat
            # always gets a fresh session + replays the history recap.
            # Without this, SDK might keep the connection alive but
            # silently drop prior-turn context, leaving the agent
            # "stateless" against its own history.
            try:
                await _session.close()
            except Exception:
                pass

    asyncio.create_task(runner())
    return {"run_id": run_id, "chat_id": log.data["id"]}


# ============================================================
# Chat history endpoints — sidebar in the UI consumes these
# ============================================================

@app.get("/api/chats")
async def list_chats_api(limit: int = 50):
    """List recent chats for the sidebar. Newest first."""
    return {"chats": chats.list_chats(limit=limit)}


@app.get("/api/chats/{chat_id}")
async def read_chat_api(chat_id: str):
    """Full message list for a chat — UI calls this when user clicks
    a sidebar item to populate the chat view."""
    try:
        return chats.read_chat(chat_id)
    except FileNotFoundError:
        raise HTTPException(404, f"chat {chat_id!r} not found")


class RenameChatRequest(BaseModel):
    title: str


@app.post("/api/chats/{chat_id}/rename")
async def rename_chat_api(chat_id: str, req: RenameChatRequest):
    out = chats.rename_chat(chat_id, req.title)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error", "rename failed"))
    return out


@app.delete("/api/chats/{chat_id}")
async def delete_chat_api(chat_id: str):
    """Delete a chat. If it was the active one, also clears the
    server-side current-chat pointer so the next /chat call starts fresh."""
    global _chat_log
    out = chats.delete_chat(chat_id)
    if _chat_log is not None and _chat_log.data.get("id") == chat_id:
        await _session.close()
        _chat_log = None
    return out


@app.get("/api/setup/status")
async def setup_status_api(probe_auth: bool = True):
    """One-stop check for the welcome wizard. Returns whether Claude CLI is
    on PATH AND authenticated, OAuth client bundled, and main token granted.
    `complete=True` only when ALL four are good (auth costs ~few tokens —
    pass probe_auth=False for cheap polling that only checks installation)."""
    from src import setup
    return await asyncio.to_thread(setup.check_setup_status, probe_auth)


@app.post("/api/setup/install_claude")
async def setup_install_claude_api():
    """Install Claude Code natively via Anthropic's PowerShell bootstrap.
    Blocks until done (no console window). Returns the same shape as the
    setup.install_claude_cli function.
    """
    from src import setup
    return await asyncio.to_thread(setup.install_claude_cli)


@app.post("/api/setup/login_claude")
async def setup_login_claude_api():
    """Spawn a visible terminal with `claude setup-token`. Returns
    immediately; client polls /api/setup/check_claude_auth to know when
    the user has finished.
    """
    from src import setup
    return await asyncio.to_thread(setup.login_claude)


@app.post("/api/setup/check_claude_auth")
async def setup_check_claude_auth_api():
    """Probe whether `claude` is authenticated by making a tiny test
    request to the model. Returns ok=True iff Claude responds."""
    from src import setup
    return await asyncio.to_thread(setup.check_claude_auth)


@app.post("/api/setup/start_oauth")
async def setup_start_oauth_api():
    """Trigger Google OAuth for alias='main' from the wizard. Blocks
    until the user finishes the browser consent (or it times out).
    Returns {ok, bound_email?, error?}.
    """
    try:
        result = await asyncio.to_thread(auth.add_account, "main")
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


# ============================================================
# Onboarding state — single endpoint the UI polls to drive the wizard
# ============================================================

@app.get("/api/onboarding/state")
async def onboarding_state_api():
    """One-stop status for the welcome wizard the .exe shows on first
    run. Drives a 3-step UI:

      step 1: install_claude  — Claude CLI on PATH + authenticated
      step 2: google_signin   — at least one Google account registered
      step 3: ready           — green-light, hand off to the chat UI

    Returns:
        {
            steps: [
                {id: "install_claude", title: "...", done: bool, action: "..."},
                {id: "google_signin",  title: "...", done: bool, action: "..."},
            ],
            complete: bool,        # all steps done
            current_step: str,     # id of the first not-done step
            accounts: [{alias, email}],   # already-connected accounts
        }

    The frontend just renders steps in order, lights up the "done"
    ones, and calls the corresponding action endpoint for the
    current_step (`/api/setup/install_claude`, `/api/setup/start_oauth`,
    etc.).
    """
    from src import setup

    setup_status = await asyncio.to_thread(setup.check_setup_status, True)
    try:
        accounts = await asyncio.to_thread(auth.list_accounts_with_identity)
        accounts_list = accounts.get("accounts", []) if isinstance(accounts, dict) else []
    except Exception:
        accounts_list = []

    claude_done = bool(setup_status.get("claude_installed") and
                       setup_status.get("claude_authenticated"))
    google_done = len(accounts_list) > 0

    steps = [
        {
            "id": "install_claude",
            "title": "Установить Claude Code",
            "description": "Локальный AI-движок, который запускает агента.",
            "done": claude_done,
            "action_endpoint": "/api/setup/install_claude",
        },
        {
            "id": "google_signin",
            "title": "Войти в Google",
            "description": "Один клик — авторизуем доступ к Drive / Sheets / Gmail / Calendar.",
            "done": google_done,
            "action_endpoint": "/api/setup/start_oauth",
        },
    ]
    complete = all(s["done"] for s in steps)
    current_step = next((s["id"] for s in steps if not s["done"]), "ready")
    return {
        "steps": steps,
        "complete": complete,
        "current_step": current_step,
        "accounts": accounts_list,
    }


# ============================================================
# Auto-update — UI polls /check, asks to apply via /apply
# ============================================================

@app.get("/api/updates/check")
async def updates_check_api():
    """Check whether a newer build is available.

    Manifest URL comes from env `UPDATE_MANIFEST_URL` — set this when
    you publish a GitHub Release with a manifest.json next to the .exe.
    When unset, returns `{ok: True, update_available: False, reason:
    "manifest URL not configured"}` so the UI can hide the update
    banner instead of crashing.
    """
    import os
    from src import updater
    manifest_url = os.environ.get("UPDATE_MANIFEST_URL", "").strip()
    if not manifest_url:
        return {
            "ok": True,
            "update_available": False,
            "current_version": updater.get_current_version(),
            "_meta": {"reason": "manifest URL not configured (UPDATE_MANIFEST_URL env)"},
        }
    current = updater.get_current_version()
    return await asyncio.to_thread(updater.check_for_updates, current, manifest_url)


class ApplyUpdateRequest(BaseModel):
    download_url: str
    expected_sha256: str | None = None


@app.post("/api/updates/apply")
async def updates_apply_api(req: ApplyUpdateRequest):
    """Download the new binary + swap it into place + ready to relaunch.

    Returns:
        {ok, applied?, backup_path?, current_path?, error?, error_kind?}

    The UI should:
      1. Show a "downloading..." indicator while this runs.
      2. On `ok: True`, prompt the user "Restart to finish update?"
      3. On user confirmation, call `/api/updates/relaunch` (separate
         endpoint, not auto — so a half-applied update doesn't yank
         the rug out from under an active conversation).
    """
    import tempfile
    from pathlib import Path
    from src import updater

    tmp_dir = Path(tempfile.gettempdir()) / "workspace-agent-update"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    new_path = tmp_dir / "agent-new.exe"

    # Download with SHA-256 verify
    dl = await asyncio.to_thread(
        updater.download_update,
        req.download_url, str(new_path),
        64 * 1024, 600,  # 10-min cap for big binaries
        req.expected_sha256,
    )
    if not dl.get("ok"):
        return dl

    # Swap into place (DON'T relaunch — UI does that after user confirms)
    apply = await asyncio.to_thread(
        updater.apply_update, str(new_path), None, False
    )
    return apply


@app.get("/api/alerts")
async def list_alerts_api(unread_only: bool = False, limit: int = 50):
    """Return queued failure alerts. UI polls this every 30s to show the
    notification badge / banner."""
    from src.tools import watcher
    return watcher.list_alerts(unread_only=unread_only, limit=limit)


class MarkAlertsRequest(BaseModel):
    alert_ids: list[str] | None = None


@app.post("/api/alerts/mark_read")
async def mark_alerts_read_api(req: MarkAlertsRequest):
    from src.tools import watcher
    return watcher.mark_alerts_read(req.alert_ids)


@app.post("/api/alerts/clear")
async def clear_alerts_api(read_only: bool = True):
    from src.tools import watcher
    return watcher.clear_alerts(read_only=read_only)


@app.post("/api/watcher/poll_now")
async def poll_now_api(since_minutes: int = 30):
    """Force an immediate watcher poll (for manual checks / dev)."""
    from src.tools import watcher
    return await asyncio.to_thread(watcher.poll_known_scripts, since_minutes)


def _safe_relpath(rel: str) -> Path:
    """Normalize a multipart `filename` so it never escapes the batch dir.
    Rejects absolute paths, `..` segments, drive letters, and other Windows-
    specific shenanigans. Returns a relative Path with `/` separators.
    """
    rel = (rel or "unnamed").replace("\\", "/").strip("/")
    p = Path(rel)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise HTTPException(400, f"unsafe upload filename: {rel!r}")
    # Reject Windows drive letters like 'C:'
    if any(":" in part for part in p.parts):
        raise HTTPException(400, f"unsafe upload filename: {rel!r}")
    return p


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), folder_name: str | None = Form(None)):
    """Receive file uploads from the chat UI. Saves them under
    `.data/uploads/<batch_id>/[<folder_name>/]<filename>` so the agent has
    a stable local path to reference. Returns list of {name, path, size, kind}.

    For folder uploads, the browser sends multiple files with relative paths
    in the `name` field (webkitdirectory); we preserve that structure.
    """
    batch_id = uuid.uuid4().hex[:8]
    batch_dir = UPLOADS_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_root = batch_dir.resolve()

    saved: list[dict] = []
    total_bytes = 0
    for f in files:
        rel_path = _safe_relpath(f.filename)
        target = (batch_dir / rel_path).resolve()
        # Belt-and-suspenders: even after _safe_relpath, ensure the resolved
        # path lives under batch_root (catches symlink shenanigans).
        if not str(target).startswith(str(batch_root)):
            raise HTTPException(400, f"path escape attempt: {f.filename!r}")
        target.parent.mkdir(parents=True, exist_ok=True)

        bytes_written = 0
        with target.open("wb") as out:
            while True:
                chunk = await f.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                bytes_written += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    # Clean up partial write and refuse
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, f"upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB cap")
                out.write(chunk)

        kind = _KIND_BY_SUFFIX.get(target.suffix.lower(), "file")
        saved.append({
            "name": str(rel_path),
            "path": str(target),
            "size": bytes_written,
            "kind": kind,
        })

    return {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir.resolve()),
        "count": len(saved),
        "files": saved,
        # Filled below; keep this comment so the indent diff stays small.
        "is_folder": folder_name is not None,
        "folder_name": folder_name,
    }


@app.post("/api/pick_folder")
async def pick_folder_api():
    """Open the native OS folder picker (pywebview) and return the chosen
    absolute path. UI calls this when the user clicks the folder-attach
    icon — works MUCH better than `<input webkitdirectory>` which in
    pywebview/WebView2 sometimes falls back to a file picker.

    Returns:
        {ok: True, path: "<absolute path>", name: "<basename>"}
        {ok: False, error: str}  — when user cancelled or pywebview unavailable

    Browser-only fallback: if pywebview isn't running (pure HTTP access
    via a remote browser), returns {ok: False, error: "native dialog
    unavailable"} so the frontend can fall back to the file-input flow.
    """
    try:
        import webview
        windows = webview.windows
        if not windows:
            return {"ok": False, "error": "no active pywebview window — "
                    "this endpoint only works when running inside the desktop wrapper"}
        result = await asyncio.to_thread(
            windows[0].create_file_dialog,
            webview.FOLDER_DIALOG,
        )
    except ImportError:
        return {"ok": False, "error": "pywebview not installed (running in plain HTTP mode)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    if not result:
        # User cancelled
        return {"ok": False, "error": "cancelled"}
    # pywebview returns either a tuple of paths or a single string depending
    # on the platform — normalize.
    path = result[0] if isinstance(result, (list, tuple)) else result
    from pathlib import Path
    p = Path(path)
    return {"ok": True, "path": str(p.resolve()), "name": p.name}


@app.post("/api/register_folder")
async def register_folder_api(req: dict):
    """Register an already-existing folder path as an "attachment" without
    copying it into .data/uploads. Used after /api/pick_folder gave us a
    real absolute path the user picked from disk.

    Body: {"path": "C:/Users/.../some/folder"}
    Returns the same shape as /api/upload so the UI can treat folder
    attachments uniformly.
    """
    from pathlib import Path
    raw = (req or {}).get("path", "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    p = Path(raw).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"folder not found: {raw}")
    # Quick stat — count files + total size — so the UI can show meaningful info
    total_size = 0
    file_count = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total_size += f.stat().st_size
                file_count += 1
            except OSError:
                pass
            # Stop counting past a sane cap so very large folders don't hang
            if file_count > 10000:
                break
    return {
        "ok": True,
        "path": str(p),
        "name": p.name,
        "size": total_size,
        "kind": "folder",
        "file_count": file_count,
    }


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


# -------- Account management --------

class AddAccountRequest(BaseModel):
    alias: str


@app.get("/accounts")
async def accounts_page():
    return FileResponse(str(STATIC_DIR / "accounts.html"))


@app.get("/api/accounts")
async def list_accounts_api():
    return {"accounts": auth.list_accounts()}


@app.get("/api/accounts/detailed")
async def list_accounts_detailed_api():
    """Each alias enriched with the bound Google identity (email + display
    name) via Drive about().get. Slow when there are many aliases — used
    by the in-app Accounts modal."""
    return await asyncio.to_thread(auth.list_accounts_with_identity)


@app.post("/api/accounts/add_auto")
async def add_account_auto_api():
    """Open Google OAuth, save the token under the email-derived alias.
    No user typing. Returns {ok, alias, email, name?, error?}."""
    try:
        result = await asyncio.to_thread(auth.add_account_auto)
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


@app.post("/api/accounts")
async def add_account_api(req: AddAccountRequest):
    alias = req.alias.strip()
    if not alias:
        raise HTTPException(400, "alias is required")
    if any(c in alias for c in "/\\:*?\"<>|"):
        raise HTTPException(400, "alias must not contain path-unsafe characters")
    try:
        result = await asyncio.to_thread(auth.add_account, alias)
        return {"ok": True, **result}
    except Exception as e:
        msg = str(e)
        hint = None
        if "access_denied" in msg or "verification" in msg.lower():
            hint = (
                "Google blocked the login because this email is not in the project's Test users list. "
                f"Add it here: {GCP_TEST_USERS_URL}"
            )
        return {"ok": False, "error": f"{type(e).__name__}: {msg}", "hint": hint}


@app.delete("/api/accounts/{alias}")
async def remove_account_api(alias: str):
    return auth.remove_account(alias)


class RenameAccountRequest(BaseModel):
    new_alias: str


@app.post("/api/accounts/{alias}/rename")
async def rename_account_api(alias: str, req: RenameAccountRequest):
    return auth.rename_account(alias, req.new_alias)


# -------- Model selection --------

class SetModelRequest(BaseModel):
    alias: str


@app.get("/api/model")
async def get_model_api():
    return {
        "current": _session.model_alias,
        "models": [
            {"alias": a, **info}
            for a, info in KNOWN_MODELS.items()
        ],
    }


@app.post("/api/model")
async def set_model_api(req: SetModelRequest):
    try:
        await _session.set_model(req.alias)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"current": _session.model_alias}


# -------- Chat history --------

@app.get("/api/chats")
async def list_chats_api(q: str | None = None, limit: int = 30):
    if q:
        return {"chats": chats.search_chats(q, limit=limit)}
    return {"chats": chats.list_chats(limit=limit)}


@app.get("/api/chats/{chat_id}")
async def read_chat_api(chat_id: str):
    try:
        return chats.read_chat(chat_id)
    except FileNotFoundError:
        raise HTTPException(404, f"chat {chat_id} not found")
