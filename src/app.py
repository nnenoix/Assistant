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
STATIC_DIR.mkdir(exist_ok=True)
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
