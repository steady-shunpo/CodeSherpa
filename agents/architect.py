from tools import search_repo_advanced, read_local_file, checkpoint_gate
from llm_utils import run_agent_loop, call_llm, run_agent_loop_arch, summarize_failure
from sandbox_utils import parse_and_execute
import re
import subprocess
import os

ARCHITECT_SYSTEM_PROMPT = """
Role: Senior AI Software Architect

Objective: Analyze a GitHub issue and produce a surgical implementation plan.
You have access to a repository graph showing how files are connected.

TOOLS (plain text only — no JSON):
1. search_repo("term")           — Ego-graph of a function, class, or variable.
                                   Use ONLY the name. No keywords before it.
2. read_file("path", start, end) — Read source code. end can be -1 for end of file.
3. search_file("path", "term")   — Search for a term in a specific file.
                                   Returns matching lines with line numbers.
                                   e.g. search_file("tests/expressions/tests.py", "order_by")

RULES:
- ONE tool call per turn. Output __END__ and stop immediately.
- Do NOT write test cases. A separate agent handles testing.
- Do NOT write commit messages.
- Never hallucinate observations. Wait for the real result.
- No JSON tool calls ever.
- Each response = exactly one THOUGHT + one ACTION + __END__. Nothing more.

FORMAT 1 — Gathering context:
THOUGHT: <reasoning>
ACTION: search_repo("Name") or read_file("path/file.py", 10, 50) or search_file("path", "term")
__END__

FORMAT 2 — Final output:
THOUGHT: <how you found the bug and why this fix works>
FINAL_PLAN:
<step-by-step fix with exact file paths, line numbers, and code changes>

TEST_HINT:
- test_style: <unittest|pytest — must be confirmed by reading an existing test file>
- test_file_location: <exact path where test should be added, e.g. tests/expressions/tests.py>
- existing_test_example: <exact path AND line range, e.g. tests/expressions/tests.py lines 379-400>
- existing_test_class: <exact class name to add the test to, e.g. BasicExpressionsTests>
- relevant_imports: <exact import lines the test will need, copied from the actual file>
- models_available: <exact model names and their fields, e.g. Employee(firstname, lastname, salary)>
- test_setup: <exact setup needed, e.g. 'use cls.example_inc from setUpTestData' or 'none'>
- trigger: <one sentence: exact call that triggers the bug>
- verify_with: <exact assertion — what is wrong that should be right>
- example_test: <a complete working test method using REAL model names, REAL fields, REAL imports>

MANDATORY BEFORE WRITING TEST_HINT:
1. You MUST read the relevant test file to find exact class name and line numbers.
2. You MUST read the models file to get exact model names and field names.
3. existing_test_example MUST include line numbers — 'tests/foo.py' alone is rejected.
4. example_test MUST use real model names — never use MyModel, SomeModel, or any placeholder.
5. relevant_imports MUST be copied from the actual file, not guessed.
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


def run_architect(user_issue: str, max_iterations: int = 20) -> dict:
    model = "nvidia/nemotron-3-super-120b-a12b"
    print("\n" + "=" * 50)
    print("🧠 STARTING ARCHITECT")
    print("=" * 50)

    final_plan_holder = {}

    messages = [
        {"role": "system", "content": ARCHITECT_SYSTEM_PROMPT},
        {"role": "user",   "content": f"Here is the issue to analyze:\n\n{user_issue}"},
    ]

    def on_done(raw_reply: str, msgs: list):
        print("\n✅ Architect has a plan!")
        decision = checkpoint_gate("Architect", raw_reply)

        if decision["status"] == "PROCEED":
            final_plan_holder["plan"] = raw_reply
            return raw_reply  # signals loop to stop

        elif decision["status"] == "RETRY":
            msgs.append({
                "role": "user",
                "content": (
                    f"Your plan was rejected.\nFeedback: {decision['feedback']}\n\n"
                    "Search or read more files if needed, then output a revised FINAL_PLAN."
                )
            })
            return None  # signals loop to keep going

        elif decision["status"] == "TAKEOVER":
            explanation = summarize_failure(messages, model, "architect", False)
            final_plan_holder["takeover"] = True
            return f"TAKEOVER_TRIGGERED::{explanation}"


        final_plan_holder["plan"] = raw_reply
        return raw_reply

    result = run_agent_loop_arch(
        messages        = messages,
        model = model,
        parse_and_execute = _arch_parse_and_execute,
        sandbox         = None,
        max_iters       = max_iterations,
        done_token      = "FINAL_PLAN:",
        agent_name      = "🧠 Architect",
        on_done         = on_done,
    )

    if "TAKEOVER_TRIGGERED" in result:
        return {"status": "failed", "content": "Takeover triggered.", "result": result}
    if result in ("TIMEOUT", ""):
        return {"status": "failed", "content": f"Architect timed out. Last message:\n{messages[-1]['content']}"}

    return {"status": "success", "content": result}