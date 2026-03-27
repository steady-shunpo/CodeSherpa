from tools import search_repo_advanced, read_local_file, checkpoint_gate
from llm_utils import run_agent_loop, call_llm, run_agent_loop_arch, summarize_failure
from sandbox_utils import parse_and_execute
import re
import subprocess
import os

PLANNER_SYSTEM_PROMPT = """
Role: Senior AI Software Engineer

Objective: Analyze a GitHub issue, find the root cause in the codebase,
and produce a precise implementation plan.

TOOLS (plain text only — no JSON):
1. search_repo("term")           — Ego-graph of a function, class, or variable.
                                   Use ONLY the name. No keywords before it.
2. read_file("path", start, end) — Read source code. end can be -1 for end of file.
3. search_file("path", "term")   — Search for a term in a specific file.
                                   Returns matching lines with line numbers.

RULES:
- ONE tool call per turn. Output __END__ and stop immediately.
- Do NOT write test cases. A separate agent handles that.
- Do NOT write commit messages.
- Do NOT write TEST_HINT. A separate agent handles that.
- Never hallucinate observations. Wait for the real result.
- No JSON tool calls ever.
- Each response = exactly one THOUGHT + one ACTION + __END__. Nothing more.

READING STRATEGY:
- Read wide ranges (50-100 lines) rather than multiple narrow reads
- Do not read the same section twice
- search_repo first to find the file, then read_file to see the code
- Once you have enough context, output FINAL_PLAN immediately

FORMAT 1 — Gathering context:
THOUGHT: <reasoning>
ACTION: search_repo("Name") or read_file("path/file.py", 10, 50) or search_file("path", "term")
__END__

FORMAT 2 — Final output (when you are confident about the fix):
THOUGHT: <how you found the bug and exactly why this fix works>
FINAL_PLAN:

<plain english explanation of the bug and why the fix works>

FILE: <exact file path>
LOCATION: <function/method name where change goes>
LINES: <approximate line numbers>
ANCHOR: <exact line of code immediately before the insertion/change point>
CHANGE:
<before code>
---
<after code>

Repeat FILE/LOCATION/LINES/ANCHOR/CHANGE block for each change needed.
"""
# Architect uses local files, not a sandbox
ARCH_TOOL_PATTERNS = {
    "search":      re.compile(r'ACTION:\s*search_repo\(\s*"([^"]+)"\s*\)'),
    "read":        re.compile(r'ACTION:\s*read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "search_file": re.compile(r'ACTION:\s*search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
}

SAFE_COMMANDS = ("grep", "find", "cat", "head", "tail", "wc", "ls", "sed")

def _arch_parse_and_execute(agent_reply: str, _sandbox) -> tuple[str, str]:
    """Architect uses local tools only — no sandbox, no shell writes."""

    if m := ARCH_TOOL_PATTERNS["search"].search(agent_reply):
        term = m.group(1)
        print(f"🔍 search_repo: {term}")
        return "search_repo", search_repo_advanced(term)

    if m := ARCH_TOOL_PATTERNS["read"].search(agent_reply):
        fp, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if end == -1:
            end = 99999
        print(f"📖 read_file: {fp} lines {start}-{end}")
        return "read_file", read_local_file(fp, start, end)

    if m := ARCH_TOOL_PATTERNS["search_file"].search(agent_reply):
        fp, term = m.group(1), m.group(2)
        print(f"🔎 search_file: {fp} for '{term}'")
        
        # Pure Python grep — works on Windows and Unix
        try:
            filepath = os.path.join("testRepos", fp)
            results = []
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, start=1):
                    if term in line:
                        results.append(f"{i}:{line.rstrip()}")
            return "search_file", "\n".join(results) if results else "(no matches found)"
        except FileNotFoundError:
            return "search_file", f"ERROR: File not found: {fp}"
        except Exception as e:
            return "search_file", f"ERROR: {e}"

    # Detect if model tried to use a removed tool and give helpful error
    if "run_bash_command" in agent_reply:
        return "none", (
            "ERROR: run_bash_command is not available to the Architect.\n"
            "Use search_file(\"path\", \"term\") to search within a specific file.\n"
            "Use search_repo(\"term\") to find where a function or class is defined.\n"
            "Use read_file(\"path\", start, end) to read a file at known line numbers."
        )

    return "none", ""


def run_planner(user_issue: str, max_iterations: int = 25) -> dict:
    model = "nvidia/nemotron-3-super-120b-a12b"
    print("\n" + "=" * 50)
    print("🧠 STARTING PLANNER")
    print("=" * 50)

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user",   "content": f"Here is the issue to analyze:\n\n{user_issue}"},
    ]

    def on_done(raw_reply: str, msgs: list):
        print("\n✅ Planner has a plan!")
        decision = checkpoint_gate("Planner", raw_reply)

        if decision["status"] == "PROCEED":
            return raw_reply

        elif decision["status"] == "RETRY":
            msgs.append({
                "role": "user",
                "content": (
                    f"Your plan was rejected.\nFeedback: {decision['feedback']}\n\n"
                    "Read more files if needed, then output a revised FINAL_PLAN."
                )
            })
            return None

        elif decision["status"] == "TAKEOVER":
            return f"TAKEOVER::{raw_reply}"

        return raw_reply

    result = run_agent_loop_arch(
        messages          = messages,
        model             = model,
        parse_and_execute = _arch_parse_and_execute,
        sandbox           = None,
        max_iters         = max_iterations,
        done_token        = "FINAL_PLAN:",
        agent_name        = "🧠 Planner",
        on_done           = on_done,
    )

    if "TAKEOVER" in result:
        return {
            "status":   "failed",
            "content":  result,
            "reason":   "takeover",
            "messages": messages,
        }
    if "TIMEOUT" in result:
        return {
            "status":   "failed",
            "content":  "TIMEOUT",
            "reason":   "max_iterations",
            "messages": messages,
        }

    return {
        "status":   "success",
        "content":  result,
        "reason":   "",
        "messages": messages,
    }