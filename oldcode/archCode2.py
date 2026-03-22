import re
from tools import search_repo_advanced, read_local_file, checkpoint_gate
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

ARCH_SYSTEM_PROMPT = """
Role: Senior AI Software Architect

Objective: Analyze a GitHub issue and produce a surgical implementation plan.

AVAILABLE TOOLS (plain text only — no JSON):
1. search_repo("term")      — Returns the ego-graph of a function, class, or variable.
                              Use ONLY the name as the term. No keywords before it.
2. read_file("path", start, end)  — Read source code. end can be -1 for end of file.

RULES:
- ONE tool call per turn. Stop after __END__.
- Do NOT write test cases. Another agent handles that.
- Do NOT write commit messages.
- Do NOT hallucinate observations. Wait for the real one.
- Never output JSON tool calls.

FORMAT 1 — Gathering context:
THOUGHT: <your reasoning>
ACTION: search_repo("SomeName") or read_file("path/to/file.py", 10, 50)
__END__

FORMAT 2 — Final plan (only when fully confident):
THOUGHT: <how you found the bug and why this fix works>
FINAL_PLAN:
<step-by-step fix with file paths and exact code changes>
"""

ARCH_TOOL_PATTERNS = {
    "search": re.compile(r'ACTION:\s*search_repo\(\s*"([^"]+)"\s*\)'),
    "read":   re.compile(r'ACTION:\s*read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
}

client2 = OpenAI(
    api_key=os.environ.get("NVIDIA_API_KEY"),
    base_url="https://integrate.api.nvidia.com/v1",
)


def parse_and_execute_arch(agent_reply: str) -> tuple[str, str]:
    if m := ARCH_TOOL_PATTERNS["search"].search(agent_reply):
        term = m.group(1)
        print(f"🔍 search_repo: {term}")
        return "search_repo", search_repo_advanced(term)

    if m := ARCH_TOOL_PATTERNS["read"].search(agent_reply):
        filepath, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        print(f"📖 read_file: {filepath} lines {start}-{end}")
        # Handle -1 as "read to end" — read_local_file may already support this,
        # but pass a large number as fallback if not
        if end == -1:
            end = 99999
        return "read_file", read_local_file(filepath, start, end)

    return "none", (
        "ERROR: No valid ACTION detected.\n"
        f"Your response started with: {agent_reply[:120]!r}\n\n"
        "Required format:\n"
        "THOUGHT: your reasoning\n"
        "ACTION: search_repo(\"TermName\")\n"
        "__END__\n\n"
        "No JSON. Plain text only."
    )


def get_reply_hash(text: str) -> str:
    import hashlib
    return hashlib.md5(text.strip().encode()).hexdigest()


def run_architect_loop(user_issue: str, max_iterations: int = 15) -> dict:

    # ✅ Correct roles: system prompt as "system", not crammed into "user"
    messages = [
        {"role": "system", "content": ARCH_SYSTEM_PROMPT},
        {"role": "user",   "content": f"Here is the issue to analyze:\n\n{user_issue}"},
    ]

    reply_history = []
    i = 0

    while i < max_iterations:
        print(f"\n--- 🧠 Architect Thinking (Iteration {i+1}/{max_iterations}) ---")

        response = client2.chat.completions.create(
            model="nvidia/nemotron-3-super-120b-a12b",
            messages=messages,
            stop=["__END__"],
            temperature=0.2,  # ✅ Low temp for consistent formatting
        )

        raw_reply = response.choices[0].message.content
        if not raw_reply:
            print("⚠️ Empty response, retrying...")
            messages.append({"role": "user", "content": "Your response was empty. Please continue."})
            i += 1
            continue

        print(raw_reply)

        # ── Stuck loop detection ──────────────────────────────────────────
        reply_hash = get_reply_hash(raw_reply)
        reply_history.append(reply_hash)

        consecutive_identical = 0
        for h in reversed(reply_history):
            if h == reply_hash:
                consecutive_identical += 1
            else:
                break

        if consecutive_identical >= 2:
            print(f"🔁 Stuck loop detected ({consecutive_identical}x). Injecting recovery.")
            messages.append({
                "role": "user",
                "content": (
                    f"STOP. Your last {consecutive_identical} responses were identical.\n"
                    "You must output a different ACTION or output FINAL_PLAN if you have enough context.\n"
                    "Plain text only. No JSON.\n"
                    "THOUGHT: ...\nACTION: ...\n__END__"
                )
            })
            response = client2.chat.completions.create(
                model="nvidia/nemotron-3-super-120b-a12b",
                messages=messages,
                stop=["__END__"],
                temperature=0.7,
            )
            raw_reply = response.choices[0].message.content or raw_reply
            reply_history.clear()

        # ✅ Model reply goes back as "assistant"
        messages.append({"role": "assistant", "content": raw_reply})

        # ── Completion check ──────────────────────────────────────────────
        # Use startswith-style check to avoid matching "FINAL_PLAN" inside a THOUGHT
        if "FINAL_PLAN:" in raw_reply:
            print("\n✅ Architect has a plan!")
            decision = checkpoint_gate("Architect", raw_reply)

            if decision["status"] == "PROCEED":
                return {"status": "success", "content": raw_reply}

            elif decision["status"] == "RETRY":
                # ✅ Don't append again — it's already in history
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your plan was rejected. Fix it based on this feedback:\n{decision['feedback']}\n\n"
                        "Search or read more files if needed, then output a revised FINAL_PLAN."
                    )
                })
                reply_history.clear()
                max_iterations += 7
                print("🔄 Retrying with checkpoint feedback...")
                i += 1
                continue

            elif decision["status"] == "TAKEOVER":
                return {"status": "failed", "content": f"Takeover triggered. Last plan:\n{raw_reply}"}

            return {"status": "success", "content": raw_reply}

        # ── Tool execution ────────────────────────────────────────────────
        turns_left = max_iterations - (i + 1)
        tool_name, observation = parse_and_execute_arch(raw_reply)
        print(f"Observation ({tool_name}): {observation[:300]}...")

        time_warning = ""
        if turns_left == 1:
            time_warning = "\n\n[CRITICAL: This is your last turn. You MUST output FINAL_PLAN now.]"
        elif turns_left <= 3:
            time_warning = f"\n\n[WARNING: Only {turns_left} turns remaining. Wrap up and output FINAL_PLAN soon.]"

        messages.append({
            "role": "user",
            "content": f"TOOL: {tool_name}\nOBSERVATION:\n{observation}{time_warning}"
        })

        i += 1
        

    return {
        "status": "failed",
        "content": f"Architect hit iteration limit without a plan. Last message:\n{messages[-1]['content']}"
    }