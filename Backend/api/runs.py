"""
app/api/runs.py

Phase 1 REST endpoints:

  POST   /runs                    — create run, kick off orchestrator
  GET    /runs/{id}               — status, current stage, retry count
  GET    /runs/{id}/doc           — full assembled doc from checkpoints
  POST   /runs/{id}/resume        — resume after intervention, optional rewind
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse 
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from failure_doc import finalize_failure_doc
from db.database import get_db
from db.models import STAGES, Checkpoint, Run, RunStatus, StageEnum, MessageResponse, SendMessageRequest, Message, MessagesListResponse
from orchestrator import delete_checkpoints_after, orchestrate, signal_resume, _get_ctx_for_run, signal_cancel, signal_failure_resolved, signal_failure
from agents.intervention import handle_user_message, apply_patches_and_write_checkpoint
from streaming import get_or_create_queue, STREAM_DONE, drop_queue, get_or_create_chat_queue


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runs", tags=["runs"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CreateRunRequest(BaseModel):
    issue_url: str


class CreateRunResponse(BaseModel):
    run_id: str
    status: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    current_stage: str | None
    stage_index: int
    retry_count: int
    turns_used: int


class ResumeRequest(BaseModel):
    from_stage: str | None = None
    context_summary: str | None = None
    extra_turns: int | None = None

class ContinueRequest(BaseModel):
    extra_turns: int = Field(default=10, ge=1, le=100)
    feedback: str | None = Field(default=None, max_length=4000)

class RunSummaryResponse(BaseModel):
    id: UUID
    issue_url: str
    status: RunStatus
    current_stage: str | None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# POST /runs — create and launch
# ---------------------------------------------------------------------------

@router.post("", response_model=RunSummaryResponse, status_code=201)
async def create_run(
    body: CreateRunRequest,
    session: AsyncSession = Depends(get_db),
):
    """
    Create a Run row and immediately launch the orchestrator as a background task.
    Returns run_id — poll GET /runs/{id} to track progress.
    """
    # Derive repo_url from issue_url for storage
    # Full parsing happens inside get_or_build_repograph — we just store the issue_url
    run = Run(
        issue_url=body.issue_url,
        repo_url="",          # filled by orchestrator after get_issue() resolves it
        status=RunStatus.INGESTING,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    # Launch pipeline as a background task — returns immediately
    asyncio.create_task(orchestrate(run.id, body.issue_url))
    logger.info(f"Run {run.id} created and launched")

    return RunSummaryResponse(
        id=run.id,
        issue_url=run.issue_url,
        status=run.status,
        current_stage=run.current_stage,
        created_at=run.created_at,
    )


# ---------------------------------------------------------------------------
# GET /runs/{id} — status poll
# ---------------------------------------------------------------------------

@router.get("/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_db),
):
    """
    Returns current status, active stage, and retry count.
    Poll this to know when to show the intervention UI (AWAITING_INTERVENTION)
    or when the run is done (SUCCEEDED / FAILED / CANCELLED).
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    return RunStatusResponse(
        run_id=str(run.id),
        status=run.status,
        current_stage=run.current_stage,
        stage_index=run.stage_index,
        retry_count=run.retry_count,
        turns_used=run.turns_used,
    )



@router.post("/{run_id}/continue", status_code=200)
async def continue_run(
    run_id: UUID,
    body: ContinueRequest = ContinueRequest(),
    session: AsyncSession = Depends(get_db),
):
    """
    Grant more turns to an agent that has reached its iteration limit.
    Only valid when run status is AWAITING_MORE_TURNS.

    Body fields:
      extra_turns  — how many more iterations to allow (default 10, max 100)
      feedback     — optional guidance injected into the agent's message history
    """
    result = await session.execute(select(Run).where(Run.id == run_id))
    run = result.scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != RunStatus.AWAITING_MORE_TURNS:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting more turns (current status: {run.status})",
        )

    # Update turn counters on the row
    run.turns_remaining = body.extra_turns
    run.turns_used      = run.turns_used + body.extra_turns   # cumulative
    run.status          = RunStatus.STAGE_RUNNING             # optimistic — agent will take over
    await session.commit()
    signal_failure_resolved(str(run_id))
    

    from turn_events import grant_turns
    # Wake the blocked thread
    granted = grant_turns(
        run_id=str(run_id),
        extra_turns=body.extra_turns,
        feedback=body.feedback,
    )

    if not granted:
        raise HTTPException(
            status_code=409,
            detail="No active agent waiting for turns — run may have already finished or crashed",
        )

    return {
        "run_id": str(run_id),
        "extra_turns_granted": body.extra_turns,
        "turns_used_total": run.turns_used,
        "feedback_injected": body.feedback is not None,
    }


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_db)):
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in (RunStatus.STAGE_RUNNING,):
        raise HTTPException(
            status_code=400,
            detail=f"Run is in '{run.status}' — only STAGE_RUNNING runs can be cancelled"
        )

    signal_cancel(str(run_id))
    signal_failure(str(run_id))

    last_cp = await session.execute(
        select(Checkpoint)
        .where(Checkpoint.run_id == run_id)
        .order_by(Checkpoint.created_at.desc())
        .limit(1)
    )

    last_checkpoint = last_cp.fetchone()

    # print("LAST CHECK DB: ", last_checkpoint)

    # run.status = RunStatus.PAUSED
    # run.current_stage = last_checkpoint.stage if last_checkpoint else 'planner'
    # print("LAST CHECKPOINT: ", last_checkpoint.stage if last_checkpoint else None)
    # run.stage_index = last_checkpoint.stage_index if last_checkpoint else 0
    # print("LAST STAGE INDEX: ", last_checkpoint.stage_index if last_checkpoint else 0)
    # await session.commit()
    print("RUN STATUS AT END OF CANCEL ENDPOINT: ", run.status)

    return {
        "run_id": run_id,
        "status": "paused",
        "reverted_to_stage": run.current_stage,
    }


@router.get("/", response_model=list[RunSummaryResponse])
async def list_runs(session: AsyncSession = Depends(get_db)):
    result = await session.execute(
        select(Run).order_by(Run.created_at.desc())
    )
    runs = result.scalars().all()
    return runs

# ---------------------------------------------------------------------------
# GET /runs/{id}/doc — full assembled doc
# ---------------------------------------------------------------------------

@router.get("/{run_id}/doc")
async def get_run_doc(
    run_id: UUID,
    session: AsyncSession = Depends(get_db),
):
    """
    Returns the full doc assembled from all checkpoints in order.
    Each checkpoint's output_json is merged on top of the previous,
    exactly as the orchestrator does it in memory.

    Useful for debugging and for the intervention UI to show context.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    result = await session.execute(
        select(Checkpoint)
        .where(Checkpoint.run_id == run_id)
        .order_by(Checkpoint.stage_index)
    )
    checkpoints = result.scalars().all()

    # Rebuild doc the same way the orchestrator does
    doc: dict = {}
    for cp in checkpoints:
        doc.update(cp.output_json)
    print(doc)
    return {
        "run_id": str(run_id),
        "status": run.status,
        "current_stage": run.current_stage,
        "stages_completed": [cp.stage for cp in checkpoints],
        "doc": doc,
    }


@router.get("/{run_id}/stream")
async def stream_run(run_id: str):
    queue = get_or_create_queue(run_id)

    async def event_generator():
        try:
            while True:
                token = await asyncio.wait_for(queue.get(), timeout=600.0)
                if token == STREAM_DONE:
                    yield f"event: done\ndata: \n\n"
                    break
                yield f"event: token\ndata: {token}\n\n"
        except asyncio.TimeoutError:
            yield f"event: timeout\ndata: \n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # important for nginx
        }
    )


@router.get("/{run_id}/chat/stream")
async def stream_chat(run_id: str):
    queue = get_or_create_chat_queue(run_id)

    async def event_generator():
        try:
            while True:
                token = await asyncio.wait_for(queue.get(), timeout=600.0)
                if token == STREAM_DONE:
                    yield f"event: done\ndata: \n\n"
                    break
                yield f"event: token\ndata: {token}\n\n"
        except asyncio.TimeoutError:
            yield f"event: timeout\ndata: \n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

# ---------------------------------------------------------------------------
# POST /runs/{id}/resume — resume after intervention
# ---------------------------------------------------------------------------

@router.post("/{run_id}/resume", status_code=200)
async def resume_run(
    run_id: UUID,
    body: ResumeRequest = ResumeRequest(),
    session: AsyncSession = Depends(get_db),
):
    """
    Resume a paused run.

    Optional body fields:
      from_stage       — rewind to this stage before resuming (e.g. "planner")
                         deletes all checkpoints after that stage
      context_summary  — extra context string appended to doc (Phase 2)

    The orchestrator wakes up, refreshes the Run row from DB,
    and picks up from wherever current_stage points.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Only resumable if paused
    resumable_statuses = {RunStatus.AWAITING_INTERVENTION, RunStatus.BLOCKED_ON_HUMAN}
    if run.status not in resumable_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Run is not paused (status={run.status}). Only {[s.value for s in resumable_statuses]} can be resumed.",
        )

    # ── Optional rewind ──────────────────────────────────────────────
    if body.from_stage is not None:
        # Validate the stage name
        valid_stages = [s.value for s in StageEnum]
        if body.from_stage not in valid_stages:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid stage '{body.from_stage}'. Valid stages: {valid_stages}",
            )

        target_stage = StageEnum(body.from_stage)
        target_index = STAGES.index(target_stage)

        # Delete checkpoints after the target stage
        await delete_checkpoints_after(session, run_id, target_index)

        # Point the run at the rewind target
        run.current_stage = target_stage.value
        run.stage_index = target_index
        await session.commit()
        logger.info(f"[{run_id}] Rewound to {body.from_stage}")

    # ── Optional context injection (Phase 2) ─────────────────────────
    if body.context_summary is not None:
        # Stored as a special checkpoint so it survives crash recovery
        # The orchestrator will pick it up when rebuilding doc
        # TODO Phase 2: append to doc before next stage runs
        logger.info(f"[{run_id}] Context summary received (Phase 2 — not yet applied)")


    await apply_patches_and_write_checkpoint(
        session,
        run_id,
        run.current_stage,
        run.stage_index,
    )
    # ── Fire the resume signal ────────────────────────────────────────
    signal_resume(str(run_id))
    signal_failure_resolved(str(run_id))

    return {
        "run_id": str(run_id),
        "resumed": True,
        "from_stage": body.from_stage,
    }



# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

@router.post("/{run_id}/messages", status_code=202)
async def send_message(
    run_id: UUID,
    body: SendMessageRequest,
    session: AsyncSession = Depends(get_db),
):
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    ctx = _get_ctx_for_run(str(run_id))
    stage = run.current_stage

    asyncio.create_task(
        handle_user_message(str(run_id), stage, body.content, ctx)
    )

    return {"status": "streaming"}


# routes/runs.py — add

@router.get("/{run_id}/messages", response_model=MessagesListResponse)
async def get_messages(
    run_id: UUID,
    stage: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
):
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    query = select(Message).where(Message.run_id == run_id)

    if stage is not None:
        valid_stages = [s.value for s in StageEnum]
        if stage not in valid_stages:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid stage '{stage}'. Valid stages: {valid_stages}",
            )
        query = query.where(Message.stage == stage)

    query = query.order_by(Message.created_at)
    result = await session.execute(query)
    messages = result.scalars().all()

    return MessagesListResponse(
        messages=[
            MessageResponse(
                id=str(m.id),
                run_id=str(m.run_id),
                stage=m.stage,
                role=m.role,
                content=m.content,
                created_at=m.created_at,
            )
            for m in messages
        ],
        total=len(messages),
    )
