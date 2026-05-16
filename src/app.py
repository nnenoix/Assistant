import asyncio
import json
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import auth, chats
from src.agent import AgentSession, KNOWN_MODELS
from src.config import ALLOWLIST_PATH, PROJECT_ROOT
from src.policy import Policy


GCP_TEST_USERS_URL = "https://console.cloud.google.com/auth/audience?project=claude-mcp-496508"


app = FastAPI(title="Google Workspace Chat Agent")

STATIC_DIR = PROJECT_ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)
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


class ApproveRequest(BaseModel):
    approved: bool


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/chat")
async def chat(req: ChatRequest):
    log = _ensure_chat_log()
    log.append_user(req.message)

    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[run_id] = queue

    async def emit(event: dict):
        await queue.put(event)
        try:
            log.append_event(event)
        except Exception:
            pass  # persistence failures must not break the stream

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
