from tools import checkpoint_gate
from llm_utils import run_agent_loop, summarize_failure
from sandbox_utils import parse_and_execute, run_remote_command


IMPLEMENTER_SYSTEM_PROMPT = """
Role: Senior Software Engineer

You will be given:
- A failing test (already written and confirmed failing)
- An architect's plan describing exactly what to fix

Your ONLY job is to implement the fix so the failing test passes.

DIRECTORY — READ THIS FIRST:
Your working directory is ALWAYS: workspace/repo/
Every command you run ALREADY starts from there.
NEVER use 'cd' in your commands. You are already there.

WRONG: run_bash_command("cd workspace/repo && pytest tests/")
RIGHT: run_bash_command("pytest tests/")

WRONG: run_bash_command("cd workspace/repo && cat conftest.py")
RIGHT: run_bash_command("cat conftest.py")
File paths in read_file, search_file are also
relative to workspace/repo/. Never include workspace/repo/ in paths.

WRONG: read_file("workspace/repo/tests/test_foo.py", 1, 50)
RIGHT: read_file("tests/test_foo.py", 1, 50)


TOOLS (plain text only — no JSON):
1. read_file("path", start, end)        — Read source files.
2. read_files_bulk(["path1", "path2"])  — Read multiple files in one turn.
|||
<new code>
|||
3. run_bash_command("cmd")              — Run shell commands.
4. search_file("path", "term")          — Search for a term in a file.
5. reset_file("path")                   - Resets given file to its original state    
5. edit_file("path", start, end)        — Edit lines in a file.
edit_file REPLACES the specified lines with your new code.
It does NOT insert — it DELETES lines start through end and puts your code there.

To INSERT a new method after line 445 WITHOUT deleting anything:
- Your replacement must include the original line 445 PLUS your new code

WRONG — this deletes line 445:
ACTION: edit_file("file.py", 445, 445)
|||
def my_new_method(self):
    pass
|||

CORRECT — this keeps line 445 and adds after it:
ACTION: edit_file("file.py", 445, 445)
|||
<original content of line 445>

def my_new_method(self):
    pass
|||

MANDATORY: After every edit_file call you MUST immediately read back 
the edited section to verify the file looks correct before continuing.
If it looks wrong, use git checkout to reset and try again

RULES:
- ONE tool call per turn. Output __END__ and stop.
- You are FORBIDDEN from modifying the test file.
- You are FORBIDDEN from writing new test files.
- Follow the architect's plan exactly. Do not improvise.
- You have the codebase snapshot — use line numbers from it directly.
  Do NOT re-read files you already have line numbers for.
- After implementing, run the failing test to verify it passes.
- Only declare FINAL_RESULT after the test passes.

FORMAT — Taking an action:
THOUGHT: <reasoning>
ACTION: <tool call>
__END__

FORMAT — When fix is verified:
THOUGHT: The test now passes. Fix is complete.
FINAL_RESULT:
STATUS: SUCCESS
CHANGES_MADE:
- <file>: <what changed>
"""


def run_implementer(architect_plan: str, test_result: dict, env_summary: str,
                    env: dict, repo_context: str, sandbox, max_iterations: int = 25) -> dict:
    model = "deepseek-ai/deepseek-v3.1"
    print("\n" + "=" * 50)
    print("🔨 STARTING IMPLEMENTER")
    print("=" * 50)

    test_file    = test_result.get("test_file", "")
    test_command = test_result.get("test_command", env.get("test_command", "pytest"))
    test_content = test_result.get("content", "")

    messages = [
        {"role": "system", "content": IMPLEMENTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{env_summary}\n\n"
                f"{repo_context}\n\n"
                f"ARCHITECT'S PLAN:\n{architect_plan}\n\n"
                f"FAILING TEST (do NOT modify this file):\n"
                f"File: {test_file}\n"
                f"Run with: {test_command}\n\n"
                f"Test writer's output:\n{test_content}\n\n"
                f"Your job: implement the fix described in the plan.\n"
                f"After implementing, run: {test_command}\n"
                f"Only declare FINAL_RESULT when the test passes."
            )
        }
    ]

    def on_done(raw_reply: str, msgs: list):
        print("\n✅ Implementer reports fix complete.")
        git_diff = run_remote_command(sandbox, "cd workspace/repo && git diff")
        if not git_diff.strip():
            git_diff = "(No git diff detected — files may not have been saved.)"

        decision = checkpoint_gate("Implementer", git_diff)

        if decision["status"] == "PROCEED":
            return git_diff

        elif decision["status"] == "RETRY":
            msgs.append({
                "role": "user",
                "content": (
                    f"Your fix was rejected.\nFeedback: {decision['feedback']}\n\n"
                    f"Review the failing test and fix accordingly.\n"
                    f"Run {test_command} to verify before declaring done."
                )
            })
            return None

        elif decision["status"] == "TAKEOVER":
            # explanation = summarize_failure(messages, model, "implementer", True)
            return f"TAKEOVER"

        return git_diff

    result = run_agent_loop(
        messages          = messages,
        model=model,
        parse_and_execute = parse_and_execute,
        sandbox           = sandbox,
        max_iters         = max_iterations,
        done_token        = "FINAL_RESULT:",
        agent_name        = "🔨 Implementer",
        on_done           = on_done,
        env               = env,
    )

    if "TAKEOVER" in result:
        return {
            "status":   "failed",
            "content":  "TAKEOVER",
            "reason":   "bad implementation",      # ← add this
            "messages": messages,              # ← add this
        }
    if "TIMEOUT" in result:
        return {
            "status":   "failed",
            "content":  "TIMEOUT",
            "reason":   "max_iterations",      # ← add this
            "messages": messages,              # ← add this
        }
    return {"status": "success", "content": result, "git_diff": result}
