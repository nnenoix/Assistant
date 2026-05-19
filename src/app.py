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
_chat_log: chats.ChatLog | None = None  # rolls over on server restart


def _ensure_chat_log() -> chats.ChatLog:
    global _chat_log
    if _chat_log is None:
        _chat_log = chats.ChatLog.start_new()
    return _chat_log


class ChatRequest(BaseModel):
    message: str
    attachments: list[dict] | None = None  # [{path, name, kind}, ...]


class ApproveRequest(BaseModel):
    approved: bool


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/chat")
async def chat(req: ChatRequest):
    log = _ensure_chat_log()
    log.append_user(req.message)

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

    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[run_id] = queue

    async def emit(event: dict):
        await queue.put(event)
        try:
            log.append_event(event)
        except Exception:
            pass

    async def runner():
        try:
            await _session.run_turn(message, emit)
        except Exception as e:
            await emit({"type": "fatal_error", "error": _scrub_secrets(f"{type(e).__name__}: {e}")})
            await emit({"type": "done"})

    asyncio.create_task(runner())
    return {"run_id": run_id}


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
        "is_folder": folder_name is not None,
        "folder_name": folder_name,
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
