from tools import search_repo_advanced, read_local_file, checkpoint_gate
from llm_utils import run_agent_loop, call_llm, run_agent_loop_arch, summarize_failure
from sandbox_utils import parse_and_execute
from agents.hint_supervisor import run_hint_supervisor
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
- Never hallucinate observations. Wait for the real result. __END__ turn immediately after using a tool.
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
    "search":        re.compile(r'(?:ACTION:\s*)?search_repo\(\s*"([^"]+)"\s*\)'),
    "read":          re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "search_file":   re.compile(r'(?:ACTION:\s*)?search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "read_no_lines": re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*\)\s*(?:$|\n|__END__)'),
    "line_count":    re.compile(r'(?:ACTION:\s*)?line_count\(\s*"([^"]+)"\s*\)'),
}
SAFE_COMMANDS = ("grep", "find", "cat", "head", "tail", "wc", "ls", "sed")

def _arch_parse_and_execute(agent_reply: str, _sandbox) -> tuple[str, str]:
    """Architect uses local tools only — no sandbox, no shell writes."""
    agent_reply = re.sub(r'```\w*\n(.*?)```', r'\1', agent_reply, flags=re.DOTALL)
    agent_reply = agent_reply.strip()

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

    if m := ARCH_TOOL_PATTERNS["line_count"].search(agent_reply):
        fp = m.group(1)
        print(f"📏 line_count: {fp}")
        try:
            filepath = os.path.join("testRepos", fp)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                count = sum(1 for _ in f)
            return "line_count", (
                f"{fp} has {count} lines.\n"
                f"To read the whole file: read_file(\"{fp}\", 1, {count})\n"
                f"To read first 50 lines: read_file(\"{fp}\", 1, 50)"
            )
        except FileNotFoundError:
            return "line_count", f"ERROR: File not found: {fp}"
        except Exception as e:
            return "line_count", f"ERROR: {e}"

    if "run_bash_command" in agent_reply:
        return "none", (
            "ERROR: run_bash_command is not available to the Architect.\n"
            "Use search_file(\"path\", \"term\") to search within a specific file.\n"
            "Use search_repo(\"term\") to find where a function or class is defined.\n"
            "Use read_file(\"path\", start, end) to read a file at known line numbers."
        )

    if m := ARCH_TOOL_PATTERNS["read_no_lines"].search(agent_reply):
        fp = m.group(1)
        return "read_file", (
            f"ERROR: read_file requires line numbers.\n"
            f"You called: read_file(\"{fp}\")\n"
            f"Correct usage:\n"
            f"  read_file(\"{fp}\", 1, 50)     — read first 50 lines\n"
            f"  read_file(\"{fp}\", 1, -1)     — read entire file\n"
            f"Tip: use line_count(\"{fp}\") first to see how many lines the file has."
        )

    return "none", ""


def run_planner(user_issue: str, max_iterations: int = 25) -> dict:
    # model = "nvidia/nemotron-3-super-120b-a12b"
    model = "mistralai/mistral-medium-3.5-128b"
    # model  = 'mistralai/devstral-2-123b-instruct-2512'
    # model  = 'deepseek-ai/deepseek-v4-pro'
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
        done_token        = ("FINAL_PLAN" or "FINAL PLAN" or "final plan" or "Final Plan" or "Final plan" or "Final_plan"),
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


# In agents/architect.py — add below run_planner
HINT_WRITER_SYSTEM_PROMPT = """
You will receive a FINAL_PLAN from a planner agent that describes a bug fix.
Your ONLY job is to produce TEST_HINT and IMPL_HINT for downstream agents.

You do NOT need to understand the bug deeply.
You do NOT need to verify the fix.
You only need to READ specific files to extract exact information.

TOOLS (plain text only — no JSON):
0. line_count("path")
   ACTION: line_count("exampleFile.py")
   __END__

1. read_file("path", start, end) — Read source code. end can be -1 for end of file.
   ACTION: read_file("exampleTestFile.py", 1, 50)
   __END__

2. search_file("path", "term")   — Search for a term in a specific file.
                                   Returns matching lines with line numbers.
   ACTION: search_file("tests/test_foo.py", "def test_")
   __END__

3. search_repo("term")           — Ego-graph of a function, class, or variable.
                                   Use ONLY the name. No keywords before it.
   ACTION: search_repo("ExampleFunction")
   __END__

RULES:
- ONE tool call per turn. Output __END__ and stop immediately.
- Maximum 4 reads total. Use the budget wisely.
- No JSON. Plain text only.
- Each response = exactly one THOUGHT + one ACTION + __END__. Nothing more.
- Do NOT generate tool responses yourself. Wait for the user to provide them.
- Do NOT explore beyond what you need for the hints.
- Do NOT re-derive the fix. Trust the planner's plan.

READING STRATEGY:
Read in this exact order — do not deviate:
1. search_file on the test file for an existing similar test class/method
2. read_file on the test file at the line range from step 1
3. read_file on the source file at the ANCHOR line from the plan to confirm it exists
4. read_file on models file ONLY if the test needs real model names

═══════════════════════════════════════════════════
FILLING IN THE HINT FIELDS — rules per field:
═══════════════════════════════════════════════════

relevant_imports:
  Copy the FULL import block from the top of the existing test file.
  Then append any additional imports the new test will need that are NOT
  already in that block (e.g. the specific module being tested, specific models).
  The test writer will use this verbatim — it must be complete and correct.
  Never guess. Only include lines you have read directly from a file.

models_available:
  This field tells the test writer what objects to use in test setup.
  - If the test needs database models: list exact model names, their fields,
    and how they are instantiated (e.g. "Article(author=..., headline=...)")
  - If the test needs no database models (e.g. pure function call, no ORM):
    write exactly: "none required — test needs no database objects"
  - Never write just "none required" without the explanation. The test writer
    must know WHY so it does not go searching for models that don't exist.

example_test:
  Write a complete, runnable test method.
  - Use real class names, real field names, real import paths.
  - Never use placeholders like MyModel, SomeObject, expected_value.
  - The test must call the trigger and assert correct behavior (not assertRaises).
  - This should be close enough that the test writer can use it with minimal changes.

═══════════════════════════════════════════════════
OUTPUT FORMAT (use exactly):
═══════════════════════════════════════════════════
THOUGHT: I have everything I need to write the hints.
TEST_HINT:
- test_style: <unittest|pytest — confirmed from reading the test file>
- test_file_location: <exact path to test file>
- existing_test_example: <exact path AND line range e.g. tests/foo.py lines 45-70>
- existing_test_class: <exact class name confirmed from reading>
- relevant_imports: <complete import block — see rules above>
- models_available: <see rules above — never just "none required">
- test_setup: <exact setup e.g. "use cls.example_inc from setUpTestData" or "none">
- trigger: <exact call that triggers the bug, copied from the plan>
- verify_with: <what should be true after the fix — drives the assertion>
- example_test: <complete runnable test method — see rules above>

IMPL_HINT:
- file: <exact file path from the plan>
- location: <function/method name>
- anchor_line: <exact line of code immediately before insertion point>
- anchor_confirmed: <yes/no — did you read the file and confirm this line exists>
- exact_code: <complete code to insert, copied exactly from the plan>
- verify_command: <exact test command to run after implementing>

IMPORTANT: Only one IMPL_HINT block per response. If the fix requires changes in
multiple locations, describe both in a single IMPL_HINT block with comments:
  # --- change 1: imports section ---
  # --- change 2: logic change ---

MANDATORY CHECKS BEFORE OUTPUTTING:
1. existing_test_example MUST have line numbers — 'tests/foo.py' alone is rejected
2. example_test MUST use real names — never MyModel, SomeModel, or placeholders
3. relevant_imports MUST include everything the new test file needs, not just
   what the existing test file imports
4. anchor_confirmed MUST be 'yes' — you must have read the source file to verify
5. models_available MUST include the explanation, not just "none required"
"""


HINT_PLACEHOLDER_MODELS = ["MyModel", "SomeModel", "YourModel", "ExampleModel"]
 
NONE_VALUES = {"none", "n/a", "na", ""}
 
PLACEHOLDER_COMMENTS = [
    "# Add appropriate",
    "# TODO",
    "# assert based on",
    "# Add assertion",
    "# appropriate assertion",
    "# based on expected",
]
 
TEST_HINT_REQUIRED = [
    "test_style:", "test_file_location:", "existing_test_example:",
    "existing_test_class:", "relevant_imports:", "models_available:",
    "test_setup:", "trigger:", "verify_with:", "example_test:",
]
 
IMPL_HINT_REQUIRED = [
    "file:", "location:", "anchor_line:", "anchor_confirmed:",
    "exact_code:", "verify_command:",
]
 
 
def _split_blocks(raw_reply: str) -> tuple[str, str]:
    """
    Split raw_reply into (test_hint_block, impl_hint_block).
    Each block runs from its header to the next header or end of string.
    """
    test_start = raw_reply.index("TEST_HINT:")
    impl_start = raw_reply.index("IMPL_HINT:")
 
    if test_start < impl_start:
        test_block = raw_reply[test_start:impl_start]
        impl_block = raw_reply[impl_start:]
    else:
        impl_block = raw_reply[impl_start:test_start]
        test_block = raw_reply[test_start:]
 
    return test_block, impl_block
 
 
def _mask_code_blocks(text: str) -> str:
    """
    Replace triple-backtick fence contents with placeholder lines.
    Used only for required-field presence checks so code inside
    fences cannot fake a field match.
    """
    return re.sub(
        r'```[a-z]*\n.*?```',
        lambda m: '\n'.join(['# <masked>'] * m.group().count('\n')),
        text,
        flags=re.DOTALL,
    )
 
 
def _get_field_value(block: str, field: str) -> str:
    """
    Extract the value of '- field: value' from a single hint block.
    Skips content inside triple-backtick code fences entirely.
    Returns the full value (possibly multiline) as a string.
    """
    lines = block.splitlines()
    value_lines: list[str] = []
    capturing = False
    in_fence = False
 
    for line in lines:
        stripped = line.strip()
 
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                if capturing:
                    break  # fence closed while capturing — stop here
            else:
                in_fence = True
                if capturing:
                    break  # entering fence while capturing — stop
            continue
 
        if in_fence:
            continue
 
        if re.match(rf'^\s*-?\s*{re.escape(field)}\s*', line):
            capturing = True
            after_colon = re.split(rf'{re.escape(field)}\s*:\s*', line, maxsplit=1)[-1].strip()
            if after_colon:
                value_lines.append(after_colon)
            continue
 
        if capturing:
            if re.match(r'^\s*-\s+\w[\w_]*:', line):
                break
            value_lines.append(line)
 
    return "\n".join(value_lines).strip()
 
 
def _extract_code_block(block: str, field: str) -> str:
    """
    Extract the code content of a field whose value is a triple-backtick
    fence or plain indented text. Strips fence markers.
    Normalises escaped newlines.
    """
    lines = block.splitlines()
    section_lines: list[str] = []
    capturing = False
    in_fence = False
    found_field = False
 
    for line in lines:
        stripped = line.strip()
 
        if not found_field:
            if re.match(rf'^\s*-?\s*{re.escape(field)}\s*:', line):
                found_field = True
                capturing = True
                inline = re.split(rf'{re.escape(field)}\s*:\s*', line, maxsplit=1)[-1].strip()
                if inline and not inline.startswith("```"):
                    section_lines.append(inline)
            continue
 
        if capturing:
            if stripped.startswith("```") and not in_fence:
                in_fence = True
                continue
 
            if stripped.startswith("```") and in_fence:
                in_fence = False
                break  # fence closed — done
 
            if not in_fence:
                if re.match(r'^\s*-\s+\w[\w_]*:', line):
                    break
                if re.match(r'^(TEST_HINT|IMPL_HINT):', line):
                    break
 
            section_lines.append(line)
 
    raw = "\n".join(section_lines).replace("\\n", "\n")
    return raw.strip()
 
 
def validate_hints(raw_reply: str) -> tuple[bool, str]:
    """
    Validates that TEST_HINT and IMPL_HINT are complete and specific.
    Returns (is_valid, rejection_reason).
    """
 
    # ── Block presence ────────────────────────────────────────────
    if "TEST_HINT:" not in raw_reply:
        return False, "Missing TEST_HINT block."
    if "IMPL_HINT:" not in raw_reply:
        return False, "Missing IMPL_HINT block."
 
    # ── Only one IMPL_HINT block allowed ──────────────────────────
    impl_hint_count = raw_reply.count("IMPL_HINT:")
    if impl_hint_count > 1:
        return False, (
            f"Found {impl_hint_count} IMPL_HINT blocks. Only one is allowed. "
            "If the fix touches multiple locations, use a single IMPL_HINT block: "
            "set anchor_line to the first change location and describe all changes "
            "in exact_code with inline comments indicating where each part goes."
        )
 
    test_block, impl_block = _split_blocks(raw_reply)
 
    # ── Required fields per block (code fences masked) ───────────
    masked_test = _mask_code_blocks(test_block)
    masked_impl = _mask_code_blocks(impl_block)
 
    missing_test = [f for f in TEST_HINT_REQUIRED if f not in masked_test]
    missing_impl = [f for f in IMPL_HINT_REQUIRED if f not in masked_impl]
    missing = missing_test + missing_impl
    if missing:
        return False, f"Missing fields: {', '.join(missing)}"
 
    # ── existing_test_example must have line numbers ──────────────
    example_ref = _get_field_value(test_block, "existing_test_example")
    if not re.search(r'\d+', example_ref):
        return False, (
            "existing_test_example must include line numbers. "
            "e.g. 'tests/foo.py lines 45-70'"
        )
 
    # ── anchor_confirmed must be exactly 'yes' ────────────────────
    anchor_confirmed = _get_field_value(impl_block, "anchor_confirmed").lower().strip()
    anchor_confirmed_word = anchor_confirmed.split()[0] if anchor_confirmed else ""
    if anchor_confirmed_word != "yes":
        return False, (
            "anchor_confirmed is not 'yes'. "
            "Read the source file to verify the anchor line exists exactly as written."
        )
 
    # ── No placeholder model names ────────────────────────────────
    for placeholder in HINT_PLACEHOLDER_MODELS:
        if placeholder in test_block or placeholder in impl_block:
            return False, (
                f"Found placeholder '{placeholder}' in hints. "
                "Use real model names from the codebase."
            )
 
    # ── relevant_imports must contain actual import statements ─────
    imports_value = _extract_code_block(test_block, "relevant_imports")
    if not re.search(r'\bimport\b', imports_value):
        imports_value = _get_field_value(test_block, "relevant_imports")
    if not re.search(r'\bimport\b', imports_value):
        return False, (
        "relevant_imports must contain actual import statements "
        "(e.g. 'from scrapy.commands import ScrapyCommand'). "
        "Do not leave this field empty or set to 'none'."
    )
 
    # ── import syntax check ───────────────────────────────────────
    if re.search(r'\bfrom\b[^=\n]+=\s*\w', imports_value):
        return False, (
            "relevant_imports contains invalid import syntax. "
            "Use 'from module import name', not 'from module = name'."
        )
 
    # ── example_test checks ───────────────────────────────────────
    example_test_section = _extract_code_block(test_block, "example_test")
 
    if not re.search(r'def test_\w+', example_test_section):
        return False, (
            "example_test must contain a complete test method starting with 'def test_'."
        )
 
    for placeholder in PLACEHOLDER_COMMENTS:
        if placeholder in example_test_section:
            return False, (
                f"example_test contains placeholder comment '{placeholder}'. "
                "Write a complete test with real assertions. "
                "Read the source file to understand what the correct assertion should be."
            )
 
    has_assert = "assert " in example_test_section
    has_raises = "pytest.raises" in example_test_section
    has_unittest_assert = bool(re.search(r'\bself\.assert\w+\s*\(', example_test_section))
    has_fail = bool(re.search(r'\bself\.fail\s*\(', example_test_section))
    if not has_assert and not has_raises and not has_unittest_assert and not has_fail:
        return False, (
            "example_test has no assertions. "
            "It must contain at least one of: assert statement, "
            "pytest.raises block, or unittest self.assert*/self.fail call."
        )
 
    # ── Class/function consistency ────────────────────────────────
    existing_test_class = _get_field_value(test_block, "existing_test_class").strip()
    # Strip parenthetical notes e.g. "none (tests are standalone functions)"
    existing_test_class_clean = re.split(r'[\(\s]', existing_test_class)[0].strip()
    class_is_set = existing_test_class_clean.lower() not in NONE_VALUES
 
    example_uses_self = bool(
        re.search(r'def test_\w+\s*\(\s*self\b', example_test_section)
    )
 
    if class_is_set and not example_uses_self:
        return False, (
            f"existing_test_class is set to '{existing_test_class}' "
            f"but example_test is a standalone function (no 'self' parameter). "
            f"These contradict each other. Either:\n"
            f"  - Set existing_test_class to 'none' if writing a standalone function\n"
            f"  - Add 'self' to example_test if adding to an existing class\n"
            f"Read the test file to confirm whether it uses classes or standalone functions."
        )
 
    if not class_is_set and example_uses_self:
        return False, (
            "example_test uses 'self' but existing_test_class is not set. "
            "Set existing_test_class to the name of the class this test belongs to."
        )
 
    # ── verify_command must not be empty ──────────────────────────
    verify_command = _get_field_value(impl_block, "verify_command").strip()
    if not verify_command or verify_command.lower() in NONE_VALUES:
        return False, (
            "verify_command must contain the command to run the target test. "
            "e.g. 'pytest tests/test_commands.py::TestClass::test_method' "
            "or 'python runtests.py scrapy.tests.test_commands'"
        )
 
    return True, ""


def run_hint_writer(planner_output: str, max_iterations: int = 10) -> dict:
    """
    Receives the planner's FINAL_PLAN and produces TEST_HINT + IMPL_HINT.
    Runs locally, no sandbox needed.
    """
    # model = "mistralai/devstral-2-123b-instruct-2512"  # smaller model — this is a lookup task
    model = "mistralai/mistral-medium-3.5-128b"  # smaller model — this is a lookup task
    print("\n" + "=" * 50)
    print("📝 STARTING HINT WRITER")
    print("=" * 50)

    # Extract just the FINAL_PLAN content to keep context small
    plan_content = planner_output
    if "FINAL_PLAN:" in planner_output:
        plan_content = planner_output[planner_output.index("FINAL_PLAN:"):]

    messages = [
        {"role": "system", "content": HINT_WRITER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Here is the planner's output:\n\n{plan_content}\n\n"
                f"Read the files referenced in the plan, then produce "
                f"TEST_HINT and IMPL_HINT following the format exactly."
            )
        }
    ]

    def on_done(raw_reply: str, msgs: list):
        print("\n✅ Hint writer produced hints.")

        is_valid, reason = validate_hints(raw_reply)
        if not is_valid:
            print(f"⚠️ Hints invalid: {reason}")
            msgs.append({
                "role": "user",
                "content": (
                    f"Your hints were rejected: {reason}\n\n"
                    f"Read the necessary files and fix the issue. "
                    f"Remember you have a maximum of 4 reads total."
                )
            })
            return None  # keep looping
        

        # ── NEW: supervisor check ─────────────────────────────────────────────
        should_proceed, supervisor_feedback = run_hint_supervisor(
            hint_text=raw_reply,
            messages_ref=msgs,
        )
        if not should_proceed:
            msgs.append({
                "role": "user",
                "content": (
                    f"{supervisor_feedback}\n\n"
                    f"Read the necessary files and fix the issue."
                )
            })
            return None  # keep looping
        # ── end supervisor check ──────────────────────────────────────────────


        decision = checkpoint_gate("HintWriter", raw_reply)

        if decision["status"] == "PROCEED":
            return raw_reply

        elif decision["status"] == "RETRY":
            msgs.append({
                "role": "user",
                "content": (
                    f"Hints rejected at checkpoint.\n"
                    f"Feedback: {decision['feedback']}\n\n"
                    f"Fix and resubmit."
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
        done_token        = "TEST_HINT",
        agent_name        = "📝 HintWriter",
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