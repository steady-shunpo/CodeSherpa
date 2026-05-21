from tools import checkpoint_gate
from llm_utils import run_agent_loop, summarize_failure
from sandbox_utils import parse_and_execute, run_remote_command


IMPLEMENTER_SYSTEM_PROMPT = """
Role: Senior Software Engineer

You will be given:
- An IMPL_HINT with the exact file, anchor line, and code to insert
- A failing test confirmed to fail
- An architect's plan describing the fix

Your ONLY job is to implement the fix so the failing test passes.

═══════════════════════════════════════════════════
DIRECTORY — READ THIS FIRST:
Your working directory is ALWAYS: workspace/repo/
Every command you run ALREADY starts from there.
NEVER use 'cd' in your commands. You are already there.

WRONG: run_bash_command("cd workspace/repo && pytest tests/")
RIGHT: run_bash_command("pytest tests/")

File paths in read_file, search_file are relative to workspace/repo/.
WRONG: read_file("workspace/repo/src/foo.py", 1, 50)
RIGHT: read_file("src/foo.py", 1, 50)
═══════════════════════════════════════════════════

TOOLS (plain text only — no JSON):
1. read_file("path", start, end)
   ACTION: read_file("src/foo.py", 1, 50)
   __END__

3. search_file("path", "term")
   ACTION: search_file("src/foo.py", "def my_function")
   __END__

4. run_bash_command("cmd")
   ACTION: run_bash_command("<cmd>")
   __END__

5. reset_file("path")
   Resets a file to its original state. Use when you corrupted a file.
   ACTION: reset_file("<path>")
   __END__

6. edit_file("path", start, end)
   REPLACES lines start through end with your new code.
   Does NOT insert — it DELETES those lines and puts your code there.
   
   To INSERT new code WITHOUT deleting anything:
   Include the original lines in your replacement PLUS your new code.
   
   WRONG — deletes line 445:
   ACTION: edit_file("file.py", 445, 445)
   |||
   def my_new_method(self):
       pass
   |||
   
   CORRECT — keeps line 445 and adds after it:
   ACTION: edit_file("file.py", 445, 445)
   |||
   <exact content of line 445>

   def my_new_method(self):
       pass
   |||
   __END__

IMPL_HINT USAGE — follow this if IMPL_HINT is provided:
1. search_file on the specified file for the anchor_line to get its exact line number
2. read_file around that line number to see the context (10 lines either side)
3. edit_file using the exact line number you found — include anchor line in replacement
4. read_file the edited section immediately to verify it looks correct
5. If it looks wrong — reset_file and try again
6. Run the test command to verify the fix works

If NO IMPL_HINT is provided:
- Read the relevant file section from the plan
- Find the insertion point
- Follow the same edit → verify → test sequence

READING STRATEGY:
- If IMPL_HINT is provided and anchor_confirmed is 'yes':
  You need at most 1 read to verify the anchor line, then edit.
  Do not explore further.

- If IMPL_HINT is missing or anchor_confirmed is 'no':
  Read what you need to find the correct location.
  Maximum 4 reads before making your first edit.
  Read wide ranges (50-100 lines) not narrow ones.
  Do not read the same section twice.

RULES:
- ONE tool call per turn. Output __END__ and stop.
- FORBIDDEN: modifying the test file
- FORBIDDEN: writing new test files
- MANDATORY: read back every edit immediately to verify
- MANDATORY: run the test after implementing before declaring done
- If file gets corrupted: reset_file immediately, do not try to fix corruption

FORMAT — action:
THOUGHT: <reasoning>
ACTION: <tool call>
__END__

FORMAT — when test passes:
THOUGHT: The test now passes.
FINAL_RESULT:
STATUS: SUCCESS
CHANGES_MADE:
- <file>: <what changed>
"""


def run_implementer(architect_plan: str, test_result: dict, env_summary: str,
                    env: dict, repo_context: str, sandbox, max_iterations: int = 25,
                    impl_hint: str = "") -> dict:  # ← add impl_hint parameter
    model = "mistralai/mistral-medium-3.5-128b"
    print("\n" + "=" * 50)
    print("🔨 STARTING IMPLEMENTER")
    print("=" * 50)

    test_file    = test_result.get("test_file", "")
    test_command = test_result.get("test_command", env.get("test_command", "pytest"))
    test_content = test_result.get("content", "")

    # Build impl hint section — put it first so model sees it immediately
    impl_hint_section = (
        f"IMPL_HINT (read this first — tells you exactly where and what to change):\n"
        f"{impl_hint}\n\n"
        f"{'=' * 60}\n\n"
    ) if impl_hint else f"Use this plan made by the planner for help: {architect_plan}"

    messages = [
        {"role": "system", "content": IMPLEMENTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{impl_hint_section}"
                # f"{env_summary}\n\n"
                # f"{repo_context}\n\n"
                # f"ARCHITECT'S PLAN:\n{architect_plan}\n\n"
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
