"""
app/orchestrator.py

Async orchestrator — runs the full agent pipeline for a single Run.
Mirrors your existing run_pipeline() logic but adds:
  - DB checkpointing after each stage
  - Run status updates
  - asyncio.Event pause at each stage transition (intervention hook for Phase 2)
  - Crash recovery via checkpoint rebuild on startup
  - Sandbox cleanup on all exit paths

Call from outside:
  asyncio.create_task(orchestrate(run_id, issue_url))
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import STAGES, Checkpoint, Run, RunStatus, StageEnum
from repograph.repograph import get_or_build_repograph
from agents.intervention import create_opening_diagnosis
from stages import stage_setup, stage_planner, stage_hint_writer, stage_test_writer, stage_implementer, stage_verifier, sync_ctx_from_doc
from failure_doc import (
    create_failure_doc, finalize_failure_doc,
    # finalize_success_doc, get_latest_doc
)
from turn_events import register, deregister
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YOUR STAGE FUNCTIONS — replace with your real imports
# ---------------------------------------------------------------------------
# Expected signatures (all sync — wrapped in asyncio.to_thread below):
#
#   stage_setup(doc, **ctx)        -> (doc, output | None)
#   stage_planner(doc, **ctx)      -> (doc, output | None)
#   stage_hint_writer(doc, **ctx)  -> (doc, output | None)
#   stage_test_writer(doc, **ctx)  -> (doc, output | None)
#   stage_implementer(doc, attempt, **ctx) -> (doc, output | None)
#   stage_verifier(doc, attempt, **ctx)    -> (doc, output | None)
#
#   create_failure_doc(repo_url, issue_text) -> dict
#   sync_ctx_from_doc(doc, ctx)    -> None  (mutates ctx in place)
#   finalize_success_doc(doc, git_diff, verdict) -> dict
#
# TODO: replace with your real imports
# def stage_setup(doc, **ctx):        raise NotImplementedError
# def stage_planner(doc, **ctx):      raise NotImplementedError
# def stage_hint_writer(doc, **ctx):  raise NotImplementedError
# def stage_test_writer(doc, **ctx):  raise NotImplementedError
# def stage_implementer(doc, attempt, **ctx): raise NotImplementedError
# def stage_verifier(doc, attempt, **ctx):    raise NotImplementedError
# def create_failure_doc(repo_url, issue_text): raise NotImplementedError
# def sync_ctx_from_doc(doc, ctx):    raise NotImplementedError
# def finalize_success_doc(doc, git_diff, verdict): raise NotImplementedError
# ---------------------------------------------------------------------------

MAX_PIPELINE_RETRIES = 2  # or import from config

# One asyncio.Event per run — used to pause between stages for intervention
# Keyed by run_id (str). Populated on run creation, cleaned up on termination.
_resume_events: dict[str, asyncio.Event] = {}
_run_contexts: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _set_status(session: AsyncSession, run: Run, status: RunStatus, stage: str | None = None):
    """Update run status (and optionally current_stage) and flush to DB."""
    run.status = status
    if stage is not None:
        run.current_stage = stage
    await session.commit()


async def _write_checkpoint(session: AsyncSession, run: Run, stage: StageEnum, output: dict):
    """Append a checkpoint row for a completed stage."""
    stage_index = STAGES.index(stage)
    checkpoint = Checkpoint(
        run_id=run.id,
        stage=stage.value,
        stage_index=stage_index,
        output_json=output,
    )
    session.add(checkpoint)
    # Also update run's stage_index to reflect progress
    run.stage_index = stage_index
    await session.commit()
    logger.info(f"[{run.id}] Checkpoint saved: {stage.value}")


def _rebuild_doc_from_checkpoints(base_doc: dict, checkpoints: list[Checkpoint]) -> dict:
    """
    Replay checkpoint outputs onto base_doc in order.
    This is how we restore state after a crash or rewind.
    """
    doc = dict(base_doc)
    for cp in sorted(checkpoints, key=lambda c: c.stage_index):
        doc.update(cp.output_json)
    return doc


async def _pause_for_intervention(run_id: str, session: AsyncSession, run: Run, stage: str, doc: dict, ctx: dict):
    """
    Mark run as AWAITING_INTERVENTION and wait for resume signal.
    Phase 2 will send chat messages during this pause.
    Phase 1: just waits until POST /runs/{id}/resume is called.
    """

    print("DOC AT PAUSE FOR INTERVENTION: ", doc)
    event = _resume_events.get(run_id)
    if event is None:
        logger.warning(f"[{run_id}] No resume event found — skipping pause")
        return

        
    print("SETTING STATUS")
    await _set_status(session, run, RunStatus.AWAITING_INTERVENTION, stage)
    print("STATUS SET")

    await create_opening_diagnosis(session, run_id, stage, doc, ctx)

    logger.info(f"[{run_id}] Paused at {stage} — waiting for resume")
    event.clear()
    await event.wait()
    logger.info(f"[{run_id}] Resumed at {stage}")



async def _load_checkpoints(session: AsyncSession, run_id: UUID) -> list[Checkpoint]:
    result = await session.execute(
        select(Checkpoint)
        .where(Checkpoint.run_id == run_id)
        .order_by(Checkpoint.stage_index)
    )
    return result.scalars().all()


async def _notify_awaiting_more_turns(run_id: str, loop: asyncio.AbstractEventLoop):
    """
    Called via run_coroutine_threadsafe from the agent thread.
    Updates run status so the frontend knows to show the 'grant more turns' UI.
    """
    async with get_session() as session:            # use your session factory
        result = await session.execute(
            select(Run).where(Run.id == UUID(run_id))
        )
        run = result.scalar_one_or_none()
        if run:
            run.status = RunStatus.AWAITING_MORE_TURNS
            await session.commit()


# ---------------------------------------------------------------------------
# Delete checkpoints after a given stage_index (used by rewind)
# ---------------------------------------------------------------------------

async def delete_checkpoints_after(session: AsyncSession, run_id: UUID, stage_index: int):
    """
    Hard rewind: delete all checkpoints with stage_index > given index.
    Called by POST /runs/{id}/resume?from_stage=X.
    """
    await session.execute(
        delete(Checkpoint).where(
            Checkpoint.run_id == run_id,
            Checkpoint.stage_index > stage_index,
        )
    )
    await session.commit()
    logger.info(f"[{run_id}] Deleted checkpoints after stage_index {stage_index}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def orchestrate(run_id: UUID, issue_url: str):
    """
    Full pipeline for one run. Designed to be launched as a background task:
        asyncio.create_task(orchestrate(run_id, issue_url))

    Creates its own DB session — never shares a session across await boundaries.
    """
    run_id_str = str(run_id)
    _resume_events[run_id_str] = asyncio.Event()
    _resume_events[run_id_str].set()  # not paused initially

    register(run_id_str)



    async with AsyncSessionLocal() as session:
        # Load the Run row
        run = await session.get(Run, run_id)
        if run is None:
            logger.error(f"[{run_id}] Run not found — aborting")
            return

        ctx: dict = {}
        ctx["run_id"] = str(run_id)
        ctx["loop"] = asyncio.get_event_loop()

        try:
            # ------------------------------------------------------------------
            # INGESTING — repograph + setup
            # ------------------------------------------------------------------
            await _set_status(session, run, RunStatus.INGESTING)

            ingestion = await get_or_build_repograph(issue_url, session)

            # Build the base doc from ingestion data
            # removed graphpkl and tagsjson. put back if required, but remember to exclude at chat history places
            doc = create_failure_doc(ingestion["repo_url"], ingestion["issue_text"])
            doc.update({
                "issue_text": ingestion["issue_text"],
                "owner":      ingestion["owner"],
                "repo_name":  ingestion["repo_name"],
                "repo_url":   ingestion["repo_url"],
                "commit_sha": ingestion["commit_sha"],
                # "graph_pkl":  ingestion["graph_pkl"],
                # "tags":       ingestion["tags"],
            })

            # stage_setup spins up sandbox — not checkpointed
            logger.info(f"[{run_id}] Running stage_setup")
            
            doc, setup_output = await asyncio.to_thread(stage_setup, doc, **ctx)
            if setup_output is None:
                logger.error(f"[{run_id}] stage_setup failed — marking FAILED")
                await _set_status(session, run, RunStatus.FAILED)
                return
            ctx.update(setup_output)
            _run_contexts[run_id_str] = ctx
            print("CTX: ", ctx)


            # ------------------------------------------------------------------
            # Rebuild doc from any existing checkpoints (crash recovery / rewind)
            # ------------------------------------------------------------------
            # result = await session.execute(
            #     select(Checkpoint)
            #     .where(Checkpoint.run_id == run_id)
            #     .order_by(Checkpoint.stage_index)
            # )
            # existing_checkpoints = result.scalars().all()
            existing_checkpoints = await _load_checkpoints(session, run_id)
            if existing_checkpoints:
                logger.info(f"[{run_id}] Rebuilding doc from {len(existing_checkpoints)} checkpoints")
                doc = _rebuild_doc_from_checkpoints(doc, existing_checkpoints)
                # Find the stage to resume from
                last_stage_index = existing_checkpoints[-1].stage_index
                start_index = last_stage_index + 1
            else:
                start_index = 0

            # ------------------------------------------------------------------
            # LINEAR STAGES: planner → hint_writer → test_writer
            # ------------------------------------------------------------------
            await _set_status(session, run, RunStatus.STAGE_RUNNING)

            LINEAR_STAGES = [
                (StageEnum.PLANNER,     stage_planner),
                (StageEnum.HINT_WRITER, stage_hint_writer),
                (StageEnum.TEST_WRITER, stage_test_writer),
            ]
            LINEAR_NAMES = [s.value for s, _ in LINEAR_STAGES]

            idx = start_index  # may be > 0 if resuming after crash/rewind
            while idx < len(LINEAR_STAGES):
                stage_enum, stage_fn = LINEAR_STAGES[idx]

                # Skip stages already checkpointed (crash recovery)
                already_done = any(cp.stage == stage_enum.value for cp in existing_checkpoints)
                if already_done:
                    logger.info(f"[{run_id}] Skipping {stage_enum.value} — already checkpointed")
                    idx += 1
                    continue

                await _set_status(session, run, RunStatus.STAGE_RUNNING, stage_enum.value)
                logger.info(f"[{run_id}] Running {stage_enum.value}")

                doc, output = await asyncio.to_thread(stage_fn, doc, **ctx)
                print("OUTPUT: ", output)

                if output is None:
                    # Stage failed — pause for intervention
                    print("INTERVENTION PAUSE")
                    await _pause_for_intervention(run_id_str, session, run, stage_enum.value, doc, ctx)

                    # After resume: check if a rewind was requested
                    # (rewind deletes checkpoints and updates run.current_stage)
                    await session.refresh(run)
                    print("SYNCING CTX")
                    sync_ctx_from_doc(doc, ctx)
                    print("SYNC DONE")

                    if run.current_stage in LINEAR_NAMES and run.current_stage != stage_enum.value:
                        idx = LINEAR_NAMES.index(run.current_stage)
                    else:
                        idx += 1  # patches applied, move on
                    continue


                print("CTX UPDATE")
                ctx.update(output)
                print("UPDATE DONE")
                await _write_checkpoint(session, run, stage_enum, output)
                print("CHECKPOINT WRITTEN")

                # Pause between stages for intervention (Phase 2 hook)
                print("PAUSING FOR INTERVENTION AGAIN")
                await _pause_for_intervention(run_id_str, session, run, stage_enum.value, doc, ctx)
                print("DONE")
                await session.refresh(run)
                print("SESSION REF DONE")

                #Rebuild in case of rewind
                fresh_checkpoints = await _load_checkpoints(session, run_id)
                doc = _rebuild_doc_from_checkpoints(doc, fresh_checkpoints)
                sync_ctx_from_doc(doc, ctx)

                if run.current_stage in LINEAR_NAMES and run.current_stage != stage_enum.value:
                    idx = LINEAR_NAMES.index(run.current_stage)
                else:
                    idx += 1

            # ------------------------------------------------------------------
            # IMPLEMENT + VERIFY retry loop
            # ------------------------------------------------------------------
            attempt = 0
            while attempt < MAX_PIPELINE_RETRIES:
                attempt += 1
                logger.info(f"[{run_id}] Implement+Verify attempt {attempt}/{MAX_PIPELINE_RETRIES}")

                # ── Implementer ──
                await _set_status(session, run, RunStatus.STAGE_RUNNING, StageEnum.IMPLEMENTER.value)
                doc, output = await asyncio.to_thread(stage_implementer, doc, attempt=attempt, **ctx)

                if output is None:
                    await _set_status(session, run, RunStatus.BLOCKED_ON_HUMAN, StageEnum.IMPLEMENTER.value)
                    await _pause_for_intervention(run_id_str, session, run, StageEnum.IMPLEMENTER.value, doc, ctx)
                    await session.refresh(run)
                    sync_ctx_from_doc(doc, ctx)

                    if run.current_stage in LINEAR_NAMES:
                        # Rewound back to a linear stage — re-run linear stages from there
                        idx = LINEAR_NAMES.index(run.current_stage)
                        while idx < len(LINEAR_STAGES):
                            s_enum, s_fn = LINEAR_STAGES[idx]
                            doc, out = await asyncio.to_thread(s_fn, doc, **ctx)
                            if out is None:
                                await _set_status(session, run, RunStatus.FAILED)
                                return
                            ctx.update(out)
                            await _write_checkpoint(session, run, s_enum, out)
                            idx += 1
                        attempt = 0

                    _reset_sandbox(ctx)
                    continue

                if output.get("retry"):
                    _reset_sandbox(ctx)
                    continue

                ctx.update(output)
                await _write_checkpoint(session, run, StageEnum.IMPLEMENTER, output)

                # ── Verifier ──
                await _set_status(session, run, RunStatus.STAGE_RUNNING, StageEnum.VERIFIER.value)
                doc, output = await asyncio.to_thread(stage_verifier, doc, attempt=attempt, **ctx)

                if output is None:
                    await _set_status(session, run, RunStatus.BLOCKED_ON_HUMAN, StageEnum.VERIFIER.value)
                    await _pause_for_intervention(run_id_str, session, run, StageEnum.VERIFIER.value, doc, ctx)
                    await session.refresh(run)
                    sync_ctx_from_doc(doc, ctx)

                    if run.current_stage in LINEAR_NAMES:
                        idx = LINEAR_NAMES.index(run.current_stage)
                        while idx < len(LINEAR_STAGES):
                            s_enum, s_fn = LINEAR_STAGES[idx]
                            doc, out = await asyncio.to_thread(s_fn, doc, **ctx)
                            if out is None:
                                await _set_status(session, run, RunStatus.FAILED)
                                return
                            ctx.update(out)
                            await _write_checkpoint(session, run, s_enum, out)
                            idx += 1
                        attempt = 0

                    _reset_sandbox(ctx)
                    continue

                if output.get("passed"):
                    logger.info(f"[{run_id}] ✓ Pipeline complete")
                    final_doc = finalize_success_doc(
                        doc,
                        ctx["impl_result"]["git_diff"],
                        output["verdict"],
                    )
                    await _write_checkpoint(session, run, StageEnum.VERIFIER, output)
                    await _set_status(session, run, RunStatus.SUCCEEDED)
                    _kill_sandbox(ctx)
                    return

                if output.get("retry"):
                    _reset_sandbox(ctx)
                    continue

            # Retries exhausted
            logger.warning(f"[{run_id}] Max retries exhausted — BLOCKED_ON_HUMAN")
            await _set_status(session, run, RunStatus.BLOCKED_ON_HUMAN)
            _kill_sandbox(ctx)

        except Exception as e:
            logger.exception(f"[{run_id}] Unhandled exception in orchestrator: {e}")
            try:
                await _set_status(session, run, RunStatus.FAILED)
            except Exception:
                pass
            _kill_sandbox(ctx)

        finally:
            _resume_events.pop(run_id_str, None)
            _run_contexts.pop(run_id_str, None)
            deregister(run_id_str)


# ---------------------------------------------------------------------------
# Sandbox helpers — isolated so they never raise and kill the pipeline
# ---------------------------------------------------------------------------

def _reset_sandbox(ctx: dict):
    """Reset repo state between implementer attempts."""
    sandbox = ctx.get("sandbox")
    if sandbox:
        try:
            sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
        except Exception as e:
            logger.warning(f"Sandbox reset failed: {e}")


def _kill_sandbox(ctx: dict):
    """Kill sandbox on all exit paths."""
    sandbox = ctx.get("sandbox")
    if sandbox:
        try:
            sandbox.kill()
        except Exception as e:
            logger.warning(f"Sandbox kill failed: {e}")


# ---------------------------------------------------------------------------
# Resume — called by POST /runs/{id}/resume
# ---------------------------------------------------------------------------

def signal_resume(run_id: str):
    """
    Unblock the orchestrator's asyncio.Event pause.
    Called by the resume endpoint after optionally rewinding checkpoints.
    """
    event = _resume_events.get(run_id)
    if event:
        event.set()
        logger.info(f"[{run_id}] Resume signal sent")
    else:
        logger.warning(f"[{run_id}] signal_resume called but no event found")



# ---------------------------------------------------------------------------
# Helper for intervention ctx
# ---------------------------------------------------------------------------
def _get_ctx_for_run(run_id: str) -> dict:
    return _run_contexts.get(run_id, {})     