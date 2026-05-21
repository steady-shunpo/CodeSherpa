from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import json
import asyncio
import threading
from repograph.construct_graph import build_and_save_repograph
import time
from utils.tools import get_issue, simple_clone
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import uuid
from discussion import DiscussionSession, detect_pipeline_trigger
from utils.llmutils import call_llm
from pydantic import BaseModel
from contextlib import asynccontextmanager
from db.database import Base, engine
from api.runs import router as runs_router
from db.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once on startup, then yields, then runs teardown on shutdown.
    create_all is safe to call repeatedly — it skips tables that already exist.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Database tables ready")
    yield
    # Teardown (Phase 3 will close Redis connections here too)
    await engine.dispose()
    print("✓ DB engine disposed")



class StartDiscussionRequest(BaseModel):
    issue_text: str
class MessageRequest(BaseModel):
    session_id: str
    user_input: str


app = FastAPI(
    title="Autonomous GitHub Issue Resolver",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(runs_router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins (perfect for local dev)
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allows all headers
    expose_headers=["X-Session-ID"]
)









@app.get("/health")
async def health():
    """Smoke-test endpoint — confirms the server is up."""
    return {"status": "ok"}






@app.get('/')
def test():
    print("test works")

@app.get('/build-workspace')
async def prepare_workspace(issue_url: str):
    try:
        res = get_issue(issue_url)
        issue_title = res['title']
        issue_body = res['body']
        rep = res['repo']
        owner = res['owner']
        repo = f"https://api.github.com/repos/{owner}/{rep}"
 
        simple_clone(repo)
        build_and_save_repograph('testRepos')
 
        return {
            "status": "complete",
            "title": issue_title,
            "body": issue_body,
            "repo": rep,
            "owner": owner,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

sessions: dict[str, DiscussionSession] = {}

@app.post("/discussion/start")
async def start_discussion(req: StartDiscussionRequest):
    session_id = str(uuid.uuid4())
    session = DiscussionSession(req.issue_text)
    sessions[session_id] = session

    def stream():
        full = ""
        for chunk in call_llm(session.messages, model="deepseek-ai/deepseek-v3.1", temperature=0.3):
            full += chunk
            yield chunk
        session.messages.append({"role": "assistant", "content": full})

    return StreamingResponse(
        stream(),
        media_type="text/plain",
        headers={"X-Session-ID": session_id}
    )


@app.post("/discussion/message")
async def send_message(req: MessageRequest):
    print(req.user_input)
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if detect_pipeline_trigger(req.user_input):
        session.pipeline_triggered = True
        return {
            "pipeline_triggered": True,
            "extra_context": session._collect_extra_context(),
        }

    if len(req.user_input) > 30 and "?" not in req.user_input:
        session.extra_context.append(req.user_input)

    session.messages.append({"role": "user", "content": req.user_input})

    def stream():
        full = ""
        for chunk in call_llm(session.messages, model="deepseek-ai/deepseek-v3.1", temperature=0.3):
            full += chunk
            yield chunk
        session.messages.append({"role": "assistant", "content": full})

    return StreamingResponse(stream(), media_type="text/plain")