from llm_utils import call_llm, MODEL
from sandbox_utils import parse_and_execute
from llm_utils import run_agent_loop, build_tool_result_message
import json
import re
import asyncio
import httpx
import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
from db.models import RunStatus, Run
from streaming import publish_chat_token

from db.models import Message, Run, Checkpoint
from db.database import AsyncSessionLocal  # wherever AsyncSessionLocal lives

logger = logging.getLogger(__name__)

@asynccontextmanager
async def get_async_session():
    async with AsyncSessionLocal() as session:
        yield session


# ═══════════════════════════════════════════════════════════
#  INTERVENTION SYSTEM PROMPTS (SPLIT BY MODE)
# ═══════════════════════════════════════════════════════════

QA_SYSTEM_PROMPT = """\
You are a helpful assistant for an autonomous GitHub coding pipeline.

### Behavior:
- Answer user questions directly, accurately, and conversationally in plain prose.
- Use the provided pipeline doc for context when answering questions about the task, stage, hints, code, or pipeline state.
- Do NOT emit <patch>, <instruction>, or <escalate> tags for general Q&A.
- You may use read_file / search_file if a question genuinely requires inspecting a file in the workspace.
- Clearly distinguish what you know from the doc vs. what requires inspecting files.

## Available tools:
- read_file("path/to/file", start_line, end_line)
- search_file("path/to/file", "search term")
"""

SUPERVISOR_SYSTEM_PROMPT = """\
You are an autonomous supervisor diagnosing a failure in a GitHub coding pipeline.

### Behavior Rules:
- ALWAYS investigate with tools before drawing conclusions. Do not guess or speculate.
- If the failure references a file, a test, or a module: inspect it immediately with read_file or search_file.
- Emit only THOUGHT/ACTION turns until root cause is confirmed. No plain prose before that.

### Turn Format (during investigation):
THOUGHT: <what you know so far and what you need to check>
ACTION: read_file("path/to/file", start_line, end_line)
__END__

### Recovery Actions (Once root cause is confirmed, emit exactly ONE of the following):

1. INSTRUCTION (When the stage agent should retry with targeted guidance):
<instruction stage="planner|hint_writer|test_writer|implementer">
[DIAGNOSIS]: <what went wrong in previous attempt>
[CORRECTION]: <specific function/import/logic to use>
[MANDATORY ACTION]: <concrete instruction for what to write or do>
</instruction>

2. PATCH (When a static prompt or hint doc field needs direct updating):

When emitting `<patch field="test_hint">`:
- test_style: <pytest | unittest>
- test_file_location: <path/to/test_file.py>
- existing_test_example: <path/to/existing_test.py lines X-Y>
- existing_test_class: <TestClassName or none>
- relevant_imports: |
    <verbatim imports needed for test>
- models_available: <models needed or none required - reason>
- test_setup: <setup steps or none>
- trigger: <exact method/call that reproduces the bug>
- verify_with: <expected assertion / behavior post-fix>
- example_test: |
    <reproducer test code>

When emitting `<patch field="impl_hint">`:
- file: <target/source/file.py>
- location: <class/function name>
- anchor_line: <exact line of code right before change>
- anchor_confirmed: <yes | no>
- exact_code: |
    <exact fix code>
- verify_command: <command to test the fix>

When emitting `<patch field="architect_plan">`:
FILE: <exact file path>
LOCATION: <function/method name>
LINES: <line range>
ANCHOR: <exact line of code immediately before change>
CHANGE:
<before code>
---
<after code>

3. ESCALATE (When the problem requires human decisions or cannot be resolved autonomously):
<escalate>
<explanation of blocker and specific question for human user>
</escalate>

### Available tools:
- read_file("path/to/file", start_line, end_line)
- search_file("path/to/file", "search term")
"""


PIPELINE_STAGES = ["planner", "hint_writer", "test_writer", "implementer", "verifier"]


def get_next_stage(failed_stage: str) -> str | None:
    try:
        idx = PIPELINE_STAGES.index(failed_stage)
        return PIPELINE_STAGES[idx + 1] if idx + 1 < len(PIPELINE_STAGES) else None
    except ValueError:
        return None


import uuid
from datetime import datetime


class BytesEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='replace')
        if isinstance(o, (uuid.UUID, datetime)):
            return str(o)
        try:
            return super().default(o)
        except TypeError:
            return str(o)


def build_intervention_system_prompt(doc: dict, stage: str, failure_active: bool = False) -> str:
    next_stage = get_next_stage(stage)
    base_prompt = SUPERVISOR_SYSTEM_PROMPT if failure_active else QA_SYSTEM_PROMPT
    return (
        base_prompt
        + f"\n\n## Pipeline context:"
        + f"\n- current_stage: {stage}"
        + f"\n- failure_active: {str(failure_active).lower()}"
        + (f"\n- suggested_resume: {next_stage or '(last stage)'}" if failure_active else "")
        + f"\n\n## Current pipeline doc:\n{json.dumps(doc, indent=2, cls=BytesEncoder)}"
    )

# ═══════════════════════════════════════════════════════════
#  APPLY PATCHES
# ═══════════════════════════════════════════════════════════

def apply_pending_patches(doc: dict, history: list) -> dict:
    """
    Scans all assistant messages in history for:
    1. <patch field="...">...</patch> -> applies to doc[field]
    2. <instruction stage="...">...</instruction> or <instruction>...</instruction> -> applies to doc["feedback"]
    """
    patched = doc.copy()
    patch_pattern = re.compile(r'<patch field="(\w+)">(.*?)</patch>', re.DOTALL)
    instruction_pattern = re.compile(r'<instruction(?:\s+stage="([^"]+)")?>([\s\S]*?)</instruction>', re.DOTALL)

    for msg in history:
        if msg["role"] != "assistant":
            continue
        for match in patch_pattern.finditer(msg["content"]):
            field, value = match.group(1), match.group(2).strip()
            patched[field] = value
            print(f"  ✓ Patched field: {field} ({len(value)} chars)")
            logger.info(f"  ✓ Patched field: {field} ({len(value)} chars)")

        for match in instruction_pattern.finditer(msg["content"]):
            stage, value = match.group(1), match.group(2).strip()
            patched["feedback"] = value
            print(f"  ✓ Instruction captured for stage '{stage or 'current'}': ({len(value)} chars)")
            logger.info(f"  ✓ Instruction captured for stage '{stage or 'current'}': ({len(value)} chars)")

    return patched


def has_pending_patches(history: list, last_n: int = None) -> bool:
    """
    Scans assistant messages in history for <patch field="..."> or <instruction>.
    Returns True if any patch or instruction is found.

    If last_n is provided, only checks the most recent N messages.
    """
    patch_pattern = re.compile(r'<patch field="(\w+)">(.*?)</patch>', re.DOTALL)
    instruction_pattern = re.compile(r'<instruction(?:\s+stage="[^"]+")?>([\s\S]*?)</instruction>', re.DOTALL)

    messages = history[-last_n:] if last_n else history

    for msg in messages:
        if msg["role"] != "assistant":
            continue

        if patch_pattern.search(msg["content"]) or instruction_pattern.search(msg["content"]):
            print("  ✓ Patch/Instruction detected in messages")
            logger.info("  ✓ Patch/Instruction detected in messages")
            return True

    print("  ✗ No patches or instructions found")
    return False


def parse_intervention_decision(reply: str) -> dict:
    """
    Parses an assistant intervention reply for <instruction>, <patch>, or <escalate>.
    Returns a structured dictionary indicating the decision type and content.
    """
    instruction_pattern = re.compile(r'<instruction(?:\s+stage="([^"]+)")?>([\s\S]*?)</instruction>', re.DOTALL)
    patch_pattern = re.compile(r'<patch field="(\w+)">(.*?)</patch>', re.DOTALL)
    escalate_pattern = re.compile(r'<escalate>([\s\S]*?)</escalate>', re.DOTALL)

    instructions = []
    for match in instruction_pattern.finditer(reply):
        stage, content = match.group(1), match.group(2).strip()
        instructions.append({"stage": stage, "content": content})

    patches = {}
    for match in patch_pattern.finditer(reply):
        field, value = match.group(1), match.group(2).strip()
        patches[field] = value

    escalate_match = escalate_pattern.search(reply)
    escalate_reason = escalate_match.group(1).strip() if escalate_match else None

    if instructions:
        return {
            "decision": "INSTRUCTION",
            "stage": instructions[-1]["stage"],
            "instruction": instructions[-1]["content"],
            "instructions": instructions,
            "patches": patches,
            "escalate_reason": escalate_reason,
        }
    elif patches:
        return {
            "decision": "PATCH",
            "patches": patches,
            "stage": None,
            "instruction": None,
            "instructions": [],
            "escalate_reason": escalate_reason,
        }
    elif escalate_reason:
        return {
            "decision": "ESCALATE",
            "escalate_reason": escalate_reason,
            "stage": None,
            "instruction": None,
            "instructions": [],
            "patches": {},
        }

    return {
        "decision": "NONE",
        "stage": None,
        "instruction": None,
        "instructions": [],
        "patches": {},
        "escalate_reason": None,
    }


# ═══════════════════════════════════════════════════════════
#  INTERVENTION SESSION
# ═══════════════════════════════════════════════════════════

def run_intervention_loop(run_id, messages: list, sandbox, loop: asyncio.AbstractEventLoop, ctx: dict) -> None:
    # env = ctx.get("env", {"pythonpath": "", "pytestflags": ""})
    logger.info(f"[{run_id}] [Intervention Loop] Started (context: {len(messages)} messages)")

    while True:
        raw_reply = ""
        for chunk in call_llm(messages, model=MODEL, temperature=0.3):
            raw_reply += chunk

            publish_chat_token(run_id, chunk, loop)
            
        publish_chat_token(run_id, '__NEWLINE__', loop)
        if not raw_reply:
            print("⚠️ Empty response.")
            logger.warning(f"[{run_id}] [Intervention Loop] Empty response received")
            continue

        messages.append({"role": "assistant", "content": raw_reply})
        print(f"\n🤖 Assistant: {raw_reply}\n")

        tool_name, observation = parse_and_execute(
            raw_reply, sandbox, ctx.get("repograph_id")
        )

        if tool_name != "none":
            # Tool was called — feed observation back and loop for next LLM turn
            print(f"\n[{tool_name}]: {observation}")
            logger.info(f"[{run_id}] [Intervention Loop] Tool executed: {tool_name} | Obs length: {len(observation)} chars | Snippet: {observation[:100].strip() if observation else 'None'}")
            messages.append(build_tool_result_message(tool_name, observation, turns_left=1))
            continue  # ← let the agent respond to the observation naturally

        # No tool called — agent gave a plain reply, hand back to user
        logger.info(f"[{run_id}] [Intervention Loop] Finished turn ({len(raw_reply)} chars)")
        return

# ── New: DB-aware versions of the session boundaries ─────────────────────────

async def _load_doc_from_checkpoints(session: AsyncSession, run_id) -> dict:
    result = await session.execute(
        select(Checkpoint)
        .where(Checkpoint.run_id == run_id)
        .order_by(Checkpoint.stage_index)
    )
    checkpoints = result.scalars().all()
    # print("CP OUTPUT JSON AT LOAD: ", checkpoints[0].output_json)
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


async def run_autonomous_intervention(
    session: AsyncSession,
    run_id: str,
    stage: str,
    doc: dict,
    ctx: dict,
) -> dict:
    """
    Autonomous diagnostic turn executed by the orchestrator when a stage fails.
    Investigates with tools and emits either an <instruction>, <patch>, or <escalate>.
    """
    logger.info(f"[{run_id}] [Autonomous Intervention] Triggered for failed stage '{stage}'. Failure reason: '{doc.get('failure_reason')}'")
    from orchestrator import signal_failure
    signal_failure(str(run_id))

    sandbox = ctx.get("sandbox")
    messages = [
        {"role": "system", "content": build_intervention_system_prompt(doc, stage, failure_active=True)},
        {
            "role": "user",
            "content": (
                f"The pipeline stage '{stage}' just failed. "
                "Read the failure doc carefully and investigate the codebase using read_file / search_file to diagnose the root cause.\n"
                "Strictly use a maximum of 2 read/search tool calls.\n"
                "Once confirmed, choose ONE autonomous recovery action:\n"
                "1. Emit an <instruction stage='...'> block to guide the agent on retry.\n"
                "2. Emit a <patch field='...'> block if a doc/hint field needs direct updating.\n"
                "3. Emit an <escalate> block if human guidance is strictly needed."
            ),
        },
    ]

    loop = asyncio.get_event_loop()
    await asyncio.to_thread(run_intervention_loop, run_id, messages, sandbox, loop, ctx)

    reply = messages[-1]["content"]
    await _persist_message(session, run_id, stage, "assistant", reply)

    decision = parse_intervention_decision(reply)
    print(f"🤖 Autonomous Intervention Decision: {decision['decision']}")
    logger.info(f"[{run_id}] [Autonomous Intervention] Decision reached: {decision['decision']} | Stage: {decision.get('stage')} | Patches: {list(decision.get('patches', {}).keys())} | Has instruction: {bool(decision.get('instruction'))}")
    return decision


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
    logger.info(f"[{run_id}] [Intervention] Generating opening failure diagnosis for stage '{stage}'")
    run = await session.get(Run, run_id)

    from orchestrator import signal_failure
    signal_failure(str(run_id))


    print("RUN STATUS 1: ", run.status)
    sandbox = ctx.get("sandbox")

    messages = [
        {"role": "system", "content": build_intervention_system_prompt(doc, stage)},
        {
            "role": "user",
            "content": (
                "The pipeline just failed. Please explain what went wrong. STRICTLY ONLY USE A MAXIMUM OF 2 READS."
                "based on the failure doc, and ask me what I'd like to do."
            ),
        },
    ]

    # Uses your existing tool-calling loop unchanged
    loop = asyncio.get_event_loop()
    result = await asyncio.to_thread(run_intervention_loop, run_id, messages, sandbox, loop, ctx)
    print("RUN STATUS 2: ", run.status)

    # The last assistant message is the opening diagnosis
    reply = messages[-1]["content"]

    # Persist only the assistant reply — seed user turn is synthetic
    await _persist_message(session, run_id, stage, "assistant", reply)
    print("RUN STATUS 3: ", run.status)
    logger.info(f"[{run_id}] [Intervention] Opening diagnosis persisted ({len(reply)} chars)")

    return reply


async def handle_user_message(
    run_id,
    stage: str,
    user_content: str,
    ctx: dict,
) -> str:
    logger.info(f"[{run_id}] [Intervention] User message received at stage '{stage}': {user_content[:100]}...")
    async with get_async_session() as session:  # fresh session, self-contained
        sandbox = ctx.get("sandbox")
        doc = await _load_doc_from_checkpoints(session, run_id)
        history = await _load_message_history(session, run_id, stage)
        print("DOC AT CHAT: ", doc)
        messages = [
            {"role": "system", "content": build_intervention_system_prompt(doc, stage, ctx['failure_active'].is_set())},
            *history,
            {"role": "user", "content": user_content},
        ]

        await _persist_message(session, run_id, stage, "user", user_content)

        loop = asyncio.get_event_loop()
        result = await asyncio.to_thread(run_intervention_loop, run_id, messages, sandbox, loop, ctx)

        reply = messages[-1]["content"]
        await _persist_message(session, run_id, stage, "assistant", reply)

        patched = has_pending_patches(messages)
        if patched:
            logger.info(f"[{run_id}] [Intervention] Pending patches detected in message history")
            run = await session.get(Run, run_id)
            run.status = RunStatus.AWAITING_INTERVENTION
            await session.commit()

        logger.info(f"[{run_id}] [Intervention] User reply generated ({len(reply)} chars)")
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


async def output_final_message(doc: dict, run_id, diff, loop: asyncio.AbstractEventLoop, verdict):
    chunk = f"""FINAL DOC: 
    IMPLEMENTATION PLAN: {doc.get('architect_plan')}

    *****************************************************************
    
    GIT DIFF: {diff}
    
    *****************************************************************
    
    FINAL VERDICT: {verdict}
    """

    publish_chat_token(run_id, chunk, loop)
