from tools import checkpoint_gate
from llm_utils import run_agent_loop, get_agent_explanation
from sandbox_utils import parse_and_execute, detect_custom_test_infrastructure

TEST_WRITER_SYSTEM_PROMPT = """
Role: Software Test Engineer

Your ONLY job is to write a single failing test that proves the bug exists.

═══════════════════════════════════════════════════
DIRECTORY — READ THIS FIRST:
Your working directory is ALWAYS: workspace/repo/
Every command you run ALREADY starts from there.
NEVER use 'cd' in your commands. You are already there.

WRONG: run_bash_command("cd workspace/repo && pytest tests/")
RIGHT: run_bash_command("pytest tests/")

WRONG: run_bash_command("cd workspace/repo && cat conftest.py")
RIGHT: run_bash_command("cat conftest.py")

File paths in read_file, write_file, search_file are also
relative to workspace/repo/. Never include workspace/repo/ in paths.

WRONG: read_file("workspace/repo/tests/test_foo.py", 1, 50)
RIGHT: read_file("tests/test_foo.py", 1, 50)
═══════════════════════════════════════════════════

AVAILABLE TOOLS (plain text only — no JSON, no markdown):

1. read_file("path", start, end)
   THOUGHT: I need to see the test file structure.
   ACTION: read_file("tests/test_foo.py", 1, 50)
   __END__

2. read_files_bulk(["path1", "path2"])
   THOUGHT: I need to see two files at once.
   ACTION: read_files_bulk(["tests/test_foo.py", "src/foo.py"])
   __END__

3. search_file("path", "term")
   THOUGHT: I need to find where bracket_split is tested.
   ACTION: search_file("tests/test_black.py", "bracket_split")
   __END__

4. run_bash_command("cmd")
   You are already in workspace/repo/. Never cd anywhere.
   THOUGHT: I need to run the test and confirm it fails.
   ACTION: run_bash_command("python3 -m pytest tests/test_foo.py::test_my_case -x 2>&1 | tail -20")
   __END__

5. write_file("path")
   THOUGHT: I will write the failing test now.
   ACTION: write_file("tests/test_my_bug.py")
   |||
   import pytest
   from mymodule import my_function

   def test_my_bug():
       result = my_function(input)
       assert result == expected_value
   |||
   __END__

RULES:
- ONE tool call per turn. Output __END__ and stop immediately after.
- NEVER use 'cd' in any command. You are already in workspace/repo/.
- NEVER use full paths like workspace/repo/... in file operations.
- You are FORBIDDEN from editing any source files. Tests only.
- You are FORBIDDEN from implementing any fix.
- Your test MUST fail before you declare FINAL_RESULT.
- You MUST confirm the test fails with run_bash_command before declaring done.
- No JSON. Plain text only.

IMPORT ERROR RECOVERY:
If you get ImportError or ModuleNotFoundError, follow IN ORDER:
1. ACTION: run_bash_command("python3 -c 'import sys; print(sys.path)'")
2. ACTION: run_bash_command("cat conftest.py 2>/dev/null | head -30")
3. ACTION: run_bash_command("pip3 install -e . --quiet 2>&1 | tail -5")
4. Retry: run_bash_command("python3 -m pytest tests/test_x.py::Class::method -x")
DO NOT retry the same failing command more than once.

FORMAT when test is confirmed failing:
THOUGHT: The test fails as expected. Failure output: <paste key line>
FINAL_RESULT:
STATUS: SUCCESS
TEST_FILE: <relative path, e.g. tests/test_my_bug.py>
TEST_COMMAND: <exact command, e.g. python3 -m pytest tests/test_my_bug.py -x>
FAILURE_OUTPUT:
<actual failure output>
"""

IMPORT_ERROR_STRATEGY = """
IMPORT ERROR RECOVERY — you are already in workspace/repo/:
1. run_bash_command("python3 -c 'import sys; print(sys.path)'")
2. run_bash_command("cat conftest.py 2>/dev/null | head -30")
3. run_bash_command("pip3 install -e . --quiet 2>&1 | tail -5")
4. run_bash_command("python3 -m pytest tests/test_x.py::Class::method -x")
DO NOT use cd. DO NOT retry the same command twice.
"""


def run_test_writer(architect_plan: str, user_issue: str, env_summary: str, env: dict,
                    repo_context: str, sandbox, test_hint: str, max_iterations: int = 25) -> dict:
    model = "deepseek-ai/deepseek-v3.1"
    print("\n" + "=" * 50)
    print("🧪 STARTING TEST WRITER")
    print("=" * 50)

    result_holder = {}

    system_prompt = TEST_WRITER_SYSTEM_PROMPT + "\n\n" + IMPORT_ERROR_STRATEGY

    infra = detect_custom_test_infrastructure(sandbox, env)
    print(infra)
    test_writer_extra = ""
    if infra["is_complex"]:
        test_writer_extra = (
            f"⚠️ IMPORTANT: This repo uses custom test infrastructure.\n"
            f"You MUST add your test to an existing test file — do NOT create a new file.\n"
            f"Use edit_file to add your test method to the class shown in existing_test_example.\n"
            f"Run tests with: {infra['custom_runner']}\n"
            f"Do NOT use pytest directly.\n"
        )


    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                # Put TEST_HINT FIRST so it's the first thing the model sees
                f"TEST_HINT (everything you need is here — read this before anything else):\n" 
                f"{test_hint}\n\n" + test_writer_extra + '\n\n' +
                f"{'=' * 60}\n\n"
                f"ORIGINAL USER ISSUE:\n{user_issue}\n\n"
                f"ARCHITECT'S PLAN:\n{architect_plan}\n\n"
                f"{env_summary}\n\n"
                f"{repo_context}\n\n"
                f"Your job: write ONE failing test.\n"
                f"The TEST_HINT above gives you the exact file, imports, models, and trigger.\n"
                f"You are allowed ONE read: the existing_test_example line in TEST_HINT.\n"
                f"After that one read you MUST write the test. No more reads."
            )
        }
    ]

    def on_done(raw_reply: str, msgs: list):
        print("\n✅ Test Writer reports a failing test.")
        decision = checkpoint_gate("TestWriter", raw_reply)

        if decision["status"] == "PROCEED":
            result_holder["result"] = raw_reply
            return raw_reply

        elif decision["status"] == "RETRY":
            msgs.append({
                "role": "user",
                "content": (
                    f"Your test was rejected.\nFeedback: {decision['feedback']}\n\n"
                    "Fix the test and confirm it fails before trying again."
                )
            })
            return None

        elif decision["status"] == "TAKEOVER":
            explanation = get_agent_explanation(
            messages       = msgs,
            model          = model,  # needs to be in scope — pass via closure
            agent_name     = "🧠 Architect",
            failure_reason = "checkpoint_rejected"  # not a loop — human rejected it
        )
            return f"TAKEOVER_TRIGGERED::{explanation}"

        result_holder["result"] = raw_reply
        return raw_reply

    result = run_agent_loop(
        messages          = messages,
        model= model,
        parse_and_execute = parse_and_execute,
        sandbox           = sandbox,
        max_iters         = max_iterations,
        done_token        = "FINAL_RESULT:",
        agent_name        = "🧪 TestWriter",
        on_done           = on_done,
        env               = env,
        is_complex        = infra["is_complex"]
    )

    if result == "TAKEOVER_TRIGGERED":
        return {"status": "failed", "content": "Takeover triggered."}
    if result in ("TIMEOUT", ""):
        return {"status": "failed", "content": "Test writer timed out without a failing test."}

    # Extract test file and command from the FINAL_RESULT block
    test_file    = _extract_field(result, "TEST_FILE:")
    test_command = _extract_field(result, "TEST_COMMAND:")

    return {
        "status":       "success",
        "content":      result,
        "test_file":    test_file,
        "test_command": test_command or env.get("test_command", "pytest"),
    }


def _extract_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith(field):
            return line.split(field, 1)[-1].strip()
    return ""



# res = run_test_writer("""
# django/db/models/fields/__init__.py:
# - Add _get_path() method that returns self.path() if callable, else self.path
# - Modify deconstruct() to store callable paths directly in kwargs (so they serialize in migrations)
# - Modify formfield() to use _get_path() instead of self.path directly

# Here are the exact changes needed:

# 1. Add _get_path method after __init__ (around line 1669):
# ```python
# def _get_path(self):
#     """
#     Return the actual path string, resolving callables if needed.
#     """
#     if callable(self.path):
#         return self.path()
#     return self.path
# ```

# 2. Modify deconstruct() method (lines 1688-1702):
# ```python
# def deconstruct(self):
#     name, path, args, kwargs = super().deconstruct()
#     # If path is a callable, store it as-is so it gets serialized in migrations
#     if callable(self.path):
#         kwargs['path'] = self.path
#     else:
#         if self.path != '':
#             kwargs['path'] = self.path
#     if self.match is not None:
#         kwargs['match'] = self.match
#     if self.recursive is not False:
#         kwargs['recursive'] = self.recursive
#     if self.allow_files is not True:
#         kwargs['allow_files'] = self.allow_files
#     if self.allow_folders is not False:
#         kwargs['allow_folders'] = self.allow_folders
#     if kwargs.get("max_length") == 100:
#         del kwargs["max_length"]
#     return name, path, args, kwargs
# ```

# 3. Modify formfield() method (lines 1710-1719):
# ```python
# def formfield(self, **kwargs):
#     return super().formfield(**{
#         'path': self._get_path(),
#         'match': self.match,
#         'recursive': self.recursive,
#         'form_class': forms.FilePathField,
#         'allow_files': self.allow_files,
#         'allow_folders': self.allow_folders,
#         **kwargs,
#     })```

# These changes will:
# - Allow path to be a callable that returns the actual path string
# - Ensure callable paths are properly serialized in migrations (the callable itself is stored)
# - Resolve the path at runtime when needed (in formfield and elsewhere)
# - Maintain backward compatibility with string paths""", """Issue: Allow FilePathField path to accept a callable.
# Description

# I have a special case where I want to create a model containing the path to some local files on the server/dev machine. Seeing as the place where these files are stored is different on different machines I have the following:
# import os
# from django.conf import settings
# from django.db import models
# class LocalFiles(models.Model):
#         name = models.CharField(max_length=255)
#         file = models.FilePathField(path=os.path.join(settings.LOCAL_FILE_DIR, 'example_dir'))
# Now when running manage.py makemigrations it will resolve the path based on the machine it is being run on. Eg: /home/<username>/server_files/example_dir
# I had to manually change the migration to include the os.path.join() part to not break this when running the migration on production/other machine.
# """, )