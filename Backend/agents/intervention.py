from llm_utils import call_llm
from sandbox_utils import parse_and_execute
from llm_utils import run_agent_loop, build_tool_result_message
import json
import re



# ═══════════════════════════════════════════════════════════
#  INTERVENTION SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════

INTERVENTION_SYSTEM_PROMPT = """\
You are a pipeline repair assistant. The user's autonomous GitHub coding pipeline has failed.

Your goals:
1. Help the user understand WHY the pipeline failed, using the failure doc below
2. Investigate the repo if needed using read_file, read_files_bulk, or search_file
3. Help the user decide what to fix, and emit patch blocks for any doc fields they want to change

## Available tools (read-only):
- read_file("path/to/file", start_line, end_line)
- search_file("path/to/file", "search term")

Use the same THOUGHT / ACTION / __END__ format as the other agents.

## Patchable doc fields — emit these when the user is ready to fix something:
<patch field="test_hint">corrected hint here</patch>
<patch field="impl_hint">corrected hint here</patch>
<patch field="architect_plan">revised plan here</patch>
<patch field="user_issue">clarified issue here</patch>

## Resume commands (remind the user when they're ready):
  /resume <stage>   — continue from a specific stage
  /resume           — continue from the next stage automatically
  /abort            — stop the pipeline

## Pipeline stage order:
planner → hint_writer → test_writer → implementer → verifier

Reason only from what's in the failure doc and what you read from the repo.
"""

PIPELINE_STAGES = ["planner", "hint_writer", "test_writer", "implementer", "verifier"]


def get_next_stage(failed_stage: str) -> str | None:
    try:
        idx = PIPELINE_STAGES.index(failed_stage)
        return PIPELINE_STAGES[idx + 1] if idx + 1 < len(PIPELINE_STAGES) else None
    except ValueError:
        return None


def build_intervention_system_prompt(doc: dict) -> str:
    next_stage = get_next_stage(doc["stage"])
    return (
        INTERVENTION_SYSTEM_PROMPT
        + f"\n\n## Current failure doc:\n{json.dumps(doc, indent=2)}"
        + f"\n\n## Failed at stage: {doc['stage']}"
        + f"\n## Suggested resume point: {next_stage or '(last stage — cannot resume further)'}"
    )


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
            raw_reply, sandbox, env.get("pythonpath"), env.get("pytestflags")
        )

        if tool_name != "none":
            # Tool was called — feed observation back and loop for next LLM turn
            print(f"\n[{tool_name}]: {observation}")
            messages.append(build_tool_result_message(tool_name, observation, turns_left=1))
            continue  # ← let the agent respond to the observation naturally

        # No tool called — agent gave a plain reply, hand back to user
        return


def intervention_session(doc: dict, ctx: dict) -> tuple[dict, str | None]:
    sandbox    = ctx.get("sandbox")
    next_stage = get_next_stage(doc["stage"])

    print("\n" + "=" * 60)
    print("🔴 PIPELINE FAILED — INTERVENTION MODE")
    print("=" * 60)
    print(f"  Stage     : {doc['stage']}")
    print(f"  Reason    : {doc['failure_reason']}")
    print(f"  Summary   : {doc.get('failure_summary', 'n/a')}")
    print(f"  Resume at : {next_stage or 'n/a'}")
    print()
    print("Chat with the assistant to diagnose and fix the issue.")
    print("Commands: /resume [stage]  |  /abort")
    print("=" * 60 + "\n")

    messages = [
        {"role": "system", "content": build_intervention_system_prompt(doc)},
        {
            "role": "user",
            "content": (
                "The pipeline just failed. Please explain what went wrong "
                "based on the failure doc, and ask me what I'd like to do."
            ),
        },
    ]

    # Opening diagnosis — agent may immediately read a file here too
    run_intervention_loop(messages, sandbox, ctx)

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue

        # ── /resume ───────────────────────────────────────────────
        if user_input.startswith("/resume"):
            parts = user_input.split()
            if len(parts) > 1:
                stage = parts[1]
                if stage not in PIPELINE_STAGES:
                    print(f"⚠️  Unknown stage. Choose from: {PIPELINE_STAGES}")
                    continue
            else:
                if next_stage is None:
                    print("⚠️  No next stage — use /abort.")
                    continue
                stage = next_stage

            patched_doc = apply_pending_patches(doc, messages)
            patched_doc["status"] = "running"
            patched_doc["stage"]  = stage
            print(f"\n▶️  Resuming from: {stage}")
            return patched_doc, stage

        # ── /abort ────────────────────────────────────────────────
        if user_input == "/abort":
            print("🛑 Pipeline aborted by user.")
            return doc, None

        # ── Normal turn ───────────────────────────────────────────
        messages.append({"role": "user", "content": user_input})
        run_intervention_loop(messages, sandbox, ctx)