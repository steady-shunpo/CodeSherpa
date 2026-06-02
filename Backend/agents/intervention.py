from llm_utils import call_llm
from sandbox_utils import parse_and_execute
from llm_utils import run_agent_loop, build_tool_result_message
import json
import re
import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Message, Run, Checkpoint


# ═══════════════════════════════════════════════════════════
#  INTERVENTION SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════

INTERVENTION_SYSTEM_PROMPT = """\
You are a pipeline repair assistant. The user's autonomous GitHub coding pipeline has failed.

## Your behavior rules (follow strictly):
- ALWAYS investigate before explaining. Do not describe what you *would* read — read it.
- If the failure references a file, a test, or a module: read it immediately in your first ACTION.
- Never say "I'll look at X" — just look at X.
- Only emit a THOUGHT/ACTION turn. Never respond in plain prose until you have enough context.

## Workflow:
1. Read the failure doc carefully.
2. Identify the most likely root cause location (file, test, config).
3. Use read_file / search_file to confirm — do this in your FIRST turn, unprompted.
4. Once you have evidence, explain the root cause to the user with specific line references.
5. Propose a fix. If the user agrees, emit the appropriate patch block.

## Turn format — use this for every turn until root cause is confirmed:
THOUGHT: <what you know so far and what you need to check>
ACTION: read_file("path/to/file", start_line, end_line)
__END__

## Available tools (read-only):
- read_file("path/to/file", start_line, end_line)
- search_file("path/to/file", "search term")

## Patchable doc fields — emit when the user confirms what to fix:
<patch field="test_hint">corrected test hint here</patch>
<patch field="impl_hint">corrected implementation hint here</patch>
<patch field="architect_plan">revised plan here</patch>
<patch field="user_issue">clarified issue here</patch>

## Resume commands:
  /resume <stage>   — continue from a specific stage
  /resume           — continue from the next stage automatically
  /abort            — stop the pipeline

## Pipeline stage order:
planner → hint_writer → test_writer → implementer → verifier

Reason only from evidence you have read. Never speculate from the failure doc alone.
"""

PIPELINE_STAGES = ["planner", "hint_writer", "test_writer", "implementer", "verifier"]


def get_next_stage(failed_stage: str) -> str | None:
    try:
        idx = PIPELINE_STAGES.index(failed_stage)
        return PIPELINE_STAGES[idx + 1] if idx + 1 < len(PIPELINE_STAGES) else None
    except ValueError:
        return None


def build_intervention_system_prompt(doc: dict, stage: str) -> str:

    next_stage = get_next_stage(stage)
    return (
        INTERVENTION_SYSTEM_PROMPT
        + f"\n\n## Current failure doc:\n{json.dumps(doc, indent=2, cls=BytesEncoder)}"
        + f"\n\n## Failed at stage: {stage}"
        + f"\n## Suggested resume point: {next_stage or '(last stage — cannot resume further)'}"
    )


class BytesEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='replace') # Gracefully handles non-utf8 bytes too
        return super().default(o)

# ═══════════════════════════════════════════════════════════
#  APPLY PATCHES
# ═══════════════════════════════════════════════════════════

def apply_pending_patches(doc: dict, history: list) -> dict:
    """
    Scans all assistant messages in history for <patch field="...">...</patch>.
    Applies the LAST patch seen per field so the user can iterate and correct.
    """
    patched = doc.copy()
    pattern = re.compile(r'<patch field="(\w+)">(.*?)</patch>', re.DOTALL)

    for msg in history:
        if msg["role"] != "assistant":
            continue
        for match in pattern.finditer(msg["content"]):
            field, value = match.group(1), match.group(2).strip()
            if field in patched:
                patched[field] = value
                print(f"  ✓ Patched: {field} ({len(value)} chars)")
            else:
                print(f"  ⚠️  Unknown field '{field}' — skipped")

    return patched


# ═══════════════════════════════════════════════════════════
#  INTERVENTION SESSION
# ═══════════════════════════════════════════════════════════

def run_intervention_loop(messages: list, sandbox, ctx: dict) -> None:
    env = ctx.get("env", {"pythonpath": "", "pytestflags": ""})

    while True:
        raw_reply = ""
        for chunk in call_llm(messages, model="mistralai/mistral-medium-3.5-128b", temperature=0.3):
            raw_reply += chunk

        if not raw_reply:
            print("⚠️ Empty response.")
            continue

        messages.append({"role": "assistant", "content": raw_reply})
        print(f"\n🤖 Assistant: {raw_reply}\n")

        tool_name, observation = parse_and_execute(
            raw_reply, sandbox
        )

        if tool_name != "none":
            # Tool was called — feed observation back and loop for next LLM turn
            print(f"\n[{tool_name}]: {observation}")
            messages.append(build_tool_result_message(tool_name, observation, turns_left=1))
            continue  # ← let the agent respond to the observation naturally

        # No tool called — agent gave a plain reply, hand back to user
        return

# ── New: DB-aware versions of the session boundaries ─────────────────────────

async def _load_doc_from_checkpoints(session: AsyncSession, run_id) -> dict:
    result = await session.execute(
        select(Checkpoint)
        .where(Checkpoint.run_id == run_id)
        .order_by(Checkpoint.stage_index)
    )
    checkpoints = result.scalars().all()
    doc = {}
    for cp in checkpoints:
        doc.update(cp.output_json)
    return doc


async def _load_message_history(session: AsyncSession, run_id, stage: str) -> list[dict]:
    result = await session.execute(
        select(Message)
        .where(Message.run_id == run_id, Message.stage == stage)
        .order_by(Message.created_at)
    )
    return [{"role": m.role, "content": m.content} for m in result.scalars().all()]


async def _persist_message(
    session: AsyncSession,
    run_id,
    stage: str,
    role: str,
    content: str,
) -> Message:
    msg = Message(run_id=run_id, stage=stage, role=role, content=content)
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


async def create_opening_diagnosis(
    session: AsyncSession,
    run_id,
    stage: str,
    doc: dict,
    ctx: dict,
) -> str:
    """
    Called once by the orchestrator right before pausing.
    Mirrors the CLI's opening turn — seeds the conversation,
    runs the intervention loop (including any tool calls),
    persists only the final assistant reply.
    """
    sandbox = ctx.get("sandbox")
    messages = [
        {"role": "system", "content": build_intervention_system_prompt(doc, stage)},
        {
            "role": "user",
            "content": (
                "The pipeline just failed. Please explain what went wrong "
                "based on the failure doc, and ask me what I'd like to do."
            ),
        },
    ]

    # Uses your existing tool-calling loop unchanged
    run_intervention_loop(messages, sandbox, ctx)

    # The last assistant message is the opening diagnosis
    reply = messages[-1]["content"]

    # Persist only the assistant reply — seed user turn is synthetic
    await _persist_message(session, run_id, stage, "assistant", reply)

    return reply


async def handle_user_message(
    session: AsyncSession,
    run_id,
    stage: str,
    user_content: str,
    ctx: dict,
) -> str:
    """
    Called by POST /runs/{id}/messages for each real user turn.
    Loads history from DB, runs intervention loop, persists both turns.
    """
    sandbox = ctx.get("sandbox")
    doc = await _load_doc_from_checkpoints(session, run_id)
    history = await _load_message_history(session, run_id, stage)

    # Rebuild messages list exactly as the CLI did
    messages = [
        {"role": "system", "content": build_intervention_system_prompt(doc, stage)},
        *history,
        {"role": "user", "content": user_content},
    ]

    # Persist user turn first
    await _persist_message(session, run_id, stage, "user", user_content)

    # Run the intervention loop — tool calls happen here if the LLM emits them
    run_intervention_loop(messages, sandbox, ctx)

    # Last message is the final assistant reply after any tool turns
    reply = messages[-1]["content"]
    await _persist_message(session, run_id, stage, "assistant", reply)

    return reply


async def apply_patches_and_write_checkpoint(
    session: AsyncSession,
    run_id,
    stage: str,
    stage_index: int,
) -> dict:
    """
    Called by the resume endpoint before signalling the orchestrator.
    Scans history for patches, applies them, writes a special checkpoint
    so the orchestrator picks them up when it rebuilds doc.
    """
    doc = await _load_doc_from_checkpoints(session, run_id)
    history = await _load_message_history(session, run_id, stage)

    patched_doc = apply_pending_patches(doc, history)

    # Find which fields actually changed
    patches = {k: v for k, v in patched_doc.items() if v != doc.get(k)}

    if patches:
        # Write as a checkpoint so _rebuild_doc_from_checkpoints picks it up
        cp = Checkpoint(
            run_id=run_id,
            stage=f"intervention_{stage}",   # distinct name, won't collide with stage names
            stage_index=stage_index,         # same index as the paused stage — replays on top
            output_json=patches,
        )
        session.add(cp)
        await session.commit()
        print(f"  ✓ Intervention checkpoint written: {list(patches.keys())}")

    return patched_doc