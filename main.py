import time
from Backend.sandbox_utils import probe_environment, build_repo_context
from agents.architect    import run_planner, run_hint_writer
from agents.test_writer  import run_test_writer
from agents.implementer  import run_implementer
from agents.verifier     import run_verifier
from agents.intervention import intervention_session
from llm_utils import extract_test_hint
from discussion import run_discussion_loop
from tools import get_issue, format_issue_for_pipeline, simple_clone
from Backend.repograph.construct_graph import build_and_save_graph
from Backend.failure_doc import (
    create_failure_doc, finalize_failure_doc,
    finalize_success_doc, get_latest_doc
)


# Your existing sandbox setup function
from tools import setup_developer_environment

MAX_PIPELINE_RETRIES = 2  # how many times to retry the full impl+verify loop








# ═══════════════════════════════════════════════════════════
#  STAGE FUNCTIONS
# ═══════════════════════════════════════════════════════════

def stage_planner(doc: dict, **_) -> tuple[dict, dict | None]:
    doc["stage"] = "planner"
    result = run_planner(doc["user_issue"])

    doc["architect_plan"] = result.get("content", "")

    if result["status"] != "success":
        print(f"❌ Planner failed: {result['content']}")
        doc["architect_plan"] = result.get("content", "")
        return finalize_failure_doc(
            doc            = doc,
            stage          = "planner",
            failure_reason = result.get("reason", "max_iterations"),
            messages       = result.get("messages", []),
            model          = "mistralai/mistral-medium-3.5-128b",
        ), None

    doc["architect_plan"] = result["content"]
    print(f"\n📋 Planner done ({len(result['content'])} chars)")
    return doc, {"architect_plan": result["content"]}


def stage_hint_writer(doc: dict, architect_plan: str, **_) -> tuple[dict, dict | None]:
    doc["stage"] = "hint_writer"
    result = run_hint_writer(architect_plan)

    test_hint, impl_hint = "", ""
    doc['test_hint'] = result.get("content", "")
    doc['impl_hint'] = result.get("content", "")

    if result["status"] != "success":
        print(f"❌ Hint writer failed: {result['content']}")
        doc["test_hint"] = result.get("content", "")
        doc["impl_hint"] = result.get("content", "")
        return finalize_failure_doc(
            doc            = doc,
            stage          = "hint_writer",
            failure_reason = result.get("reason", "max_iterations"),
            messages       = result.get("messages", []),
            model          = "mistralai/mistral-medium-3.5-128b",
        ), None

    content = result["content"]
    if "TEST_HINT:" in content:
        test_hint = content[content.index("TEST_HINT:"):]
        if "IMPL_HINT:" in test_hint:
            impl_hint = test_hint[test_hint.index("IMPL_HINT:"):]
            test_hint = test_hint[:test_hint.index("IMPL_HINT:")].strip()

    doc["test_hint"] = test_hint
    doc["impl_hint"] = impl_hint
    print(f"📋 Hints ready — TEST_HINT: {len(test_hint)} chars, IMPL_HINT: {len(impl_hint)} chars")
    return doc, {"test_hint": test_hint, "impl_hint": impl_hint}


def stage_setup(doc: dict, architect_plan: str | None = None, **_) -> tuple[dict, dict | None]:
    """Sandbox + env probe + repo context. Not a user-visible stage, never fails the pipeline."""
    print("\n🖥️  Spinning up sandbox...")
    time.sleep(60)
    sandbox, pythonpath, pytestflags = setup_developer_environment(doc["repo_url"])

    print("\n🔍 Probing environment...")
    env_summary, env = probe_environment(sandbox)
    env["pythonpath"]   = pythonpath
    env["pytestflags"]  = pytestflags
    doc["env_summary"]  = env_summary
    print(env_summary)

    print("\n📁 Building repo context...")
    repo_context = build_repo_context(sandbox, architect_plan)

    return doc, {"sandbox": sandbox, "env": env, "env_summary": env_summary, "repo_context": repo_context}


def stage_test_writer(
    doc: dict,
    architect_plan: str,
    env_summary: str,
    env: dict,
    repo_context,
    sandbox,
    test_hint: str,
    **_,
) -> tuple[dict, dict | None]:
    doc["stage"] = "test_writer"
    result = run_test_writer(
        architect_plan = architect_plan,
        user_issue     = doc["user_issue"],
        env_summary    = env_summary,
        env            = env,
        repo_context   = repo_context,
        sandbox        = sandbox,
        max_iterations = 20,
        test_hint      = test_hint,
    )

    doc["test_result"] = {
        "test_file":    result.get("test_file", ""),
        "test_command": result.get("test_command", ""),
        "content":      result.get("content", ""),
    }

    if result["status"] != "success":
        doc["test_result"] = {
        "test_file":    result.get("test_file", ""),
        "test_command": result.get("test_command", ""),
        "content":      result.get("content", ""),
    }
        print(f"❌ Test Writer failed: {result['content']}")
        return finalize_failure_doc(
            doc            = doc,
            stage          = "test_writer",
            failure_reason = result.get("reason", "max_iterations"),
            messages       = result.get("messages", []),
            model          = "mistralai/mistral-medium-3.5-128b",
            sandbox        = sandbox,
        ), None

    print(f"\n🧪 Failing test ready: {result.get('test_file', 'unknown')}")
    doc["test_result"] = {
        "test_file":    result.get("test_file", ""),
        "test_command": result.get("test_command", ""),
        "content":      result.get("content", ""),
    }
    return doc, {"test_result": result}


def stage_implementer(
    doc: dict,
    architect_plan: str,
    test_result: dict,
    env_summary: str,
    env: dict,
    repo_context,
    sandbox,
    impl_hint: str,
    attempt: int,
    **_,
) -> tuple[dict, dict | None]:
    doc["stage"] = "implementer"
    result = run_implementer(
        architect_plan = architect_plan,
        test_result    = test_result,
        env_summary    = env_summary,
        env            = env,
        repo_context   = repo_context,
        sandbox        = sandbox,
        max_iterations = 25,
        impl_hint      = impl_hint,
    )
    doc["partial_diff"] = result.get("git_diff", "")

    if result["status"] != "success":
        doc["partial_diff"] = result.get("git_diff", "")
        print(f"❌ Implementer failed: {result['content']}")
        if attempt == MAX_PIPELINE_RETRIES or "TAKEOVER" in result["content"]:
            return finalize_failure_doc(
                doc            = doc,
                stage          = "implementer",
                failure_reason = result.get("reason", "max_iterations"),
                messages       = result.get("messages", []),
                model          = "mistralai/devstral-2-123b-instruct-2512",
                sandbox        = sandbox,
            ), None
        print("🔄 Resetting git state for retry...")
        sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
        return doc, {"retry": True}   # signals: retry the impl+verify loop

    doc["partial_diff"] = result.get("git_diff", "")
    return doc, {"impl_result": result}


def stage_verifier(
    doc: dict,
    impl_result: dict,
    test_result: dict,
    architect_plan: str,
    env: dict,
    sandbox,
    attempt: int,
    **_,
) -> tuple[dict, dict | None]:
    doc["stage"] = "verifier"
    verdict = run_verifier(
        git_diff       = impl_result["git_diff"],
        test_result    = test_result,
        architect_plan = architect_plan,
        env            = env,
        sandbox        = sandbox,
    )
    doc["verifier_verdict"] = verdict

    if verdict["verdict"] == "PASS":
        return doc, {"verdict": verdict, "passed": True}

    print(f"\n⚠️ Verifier FAIL: {verdict['summary']}")
    print(f"Issues: {verdict.get('issues', [])}")

    if attempt < MAX_PIPELINE_RETRIES:
        print("🔄 Resetting and retrying implementation...")
        sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
        return doc, {"retry": True}

    print("🛑 Max retries reached.")
    return finalize_failure_doc(
        doc            = doc,
        stage          = "verifier",
        failure_reason = "verifier_failed",
        messages       = [],
        model          = "mistralai/devstral-2-123b-instruct-2512",
        sandbox        = sandbox,
    ), None


def sync_ctx_from_doc(doc: dict, ctx: dict) -> None:
    """Keep ctx in sync with whatever is already saved in doc."""
    for key in ["architect_plan", "test_hint", "impl_hint", "test_result", "partial_diff", "env_summary"]:
        if doc.get(key):
            ctx[key] = doc[key]


# ═══════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════

def run_pipeline(user_issue: str, repo_url: str) -> dict:
    print("\n" + "=" * 60)
    print("🚀 PIPELINE START")
    print("=" * 60)

    doc = create_failure_doc(repo_url, user_issue)
    ctx: dict = {}

    LINEAR_STAGES = [
        ("setup",       stage_setup),
        ("planner",     stage_planner),
        ("hint_writer", stage_hint_writer),
        ("test_writer", stage_test_writer),
    ]
    LINEAR_NAMES = [name for name, _ in LINEAR_STAGES]

    # ── Linear stages ─────────────────────────────────────────────────
    idx = 0
    while idx < len(LINEAR_STAGES):
        name, stage_fn = LINEAR_STAGES[idx]
        doc, output = stage_fn(doc, **ctx)

        if output is None:
            doc, resume_stage = intervention_session(doc, ctx)
            if resume_stage is None:
                return doc  # aborted
            
            sync_ctx_from_doc(doc, ctx)
            if resume_stage in LINEAR_NAMES:
                idx = LINEAR_NAMES.index(resume_stage)
                continue
            else:
                break  # jump into impl+verify

        
        ctx.update(output)
        idx += 1

    # ── Implement + Verify retry loop ─────────────────────────────────
    attempt = 0
    while attempt < MAX_PIPELINE_RETRIES:
        attempt += 1
        print(f"\n🔁 Implement+Verify attempt {attempt}/{MAX_PIPELINE_RETRIES}")

        doc, output = stage_implementer(doc, attempt=attempt, **ctx)

        if output is None:
            doc, resume_stage = intervention_session(doc, ctx)
            if resume_stage is None:
                return doc
            sync_ctx_from_doc(doc, ctx)
            if resume_stage in LINEAR_NAMES:
                idx = LINEAR_NAMES.index(resume_stage)
                while idx < len(LINEAR_STAGES):
                    name, stage_fn = LINEAR_STAGES[idx]
                    doc, output = stage_fn(doc, **ctx)
                    if output is None:
                        return doc  # failed again after intervention — give up
                    ctx.update(output)
                    idx += 1
                attempt = 0
            sandbox = ctx.get("sandbox")
            if sandbox:
                sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
            continue

        if output.get("retry"):
            continue

        ctx.update(output)

        doc, output = stage_verifier(doc, attempt=attempt, **ctx)

        if output is None:
            doc, resume_stage = intervention_session(doc, ctx)
            if resume_stage is None:
                return doc
            sync_ctx_from_doc(doc, ctx)
            if resume_stage in LINEAR_NAMES:
                idx = LINEAR_NAMES.index(resume_stage)
                while idx < len(LINEAR_STAGES):
                    name, stage_fn = LINEAR_STAGES[idx]
                    doc, output = stage_fn(doc, **ctx)
                    if output is None:
                        return doc
                    ctx.update(output)
                    idx += 1
                attempt = 0
            sandbox = ctx.get("sandbox")
            if sandbox:
                sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
            continue

        if output.get("passed"):
            print("\n🎉 PIPELINE COMPLETE — All checks passed.")
            ctx["sandbox"].kill()
            return finalize_success_doc(doc, ctx["impl_result"]["git_diff"], output["verdict"])

        if output.get("retry"):
            sandbox = ctx.get("sandbox")
            if sandbox:
                sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
            continue

    ctx.get("sandbox") and ctx["sandbox"].kill()
    return {"status": "failed", "doc": doc}


















# def run_pipeline(user_issue: str, repo_url: str) -> dict:
#     print("\n" + "=" * 60)
#     print("🚀 PIPELINE START")
#     print("=" * 60)

#     doc = create_failure_doc(repo_url, user_issue)

#     # ── Stage 1: Architect ────────────────────────────────────────────
#     # arch_result = run_architect(user_issue)
# #     arch_result = {"status": "success", "content": """
# # FINAL_PLAN:
# # 1. Modify the regex pattern in SQLCompiler.__init__ to include the re.DOTALL flag.
# #    File: django/db/models/sql/compiler.py
# #    Line: 35
# #    Change: self.ordering_parts = re.compile(r'(.*)\s(ASC|DESC)(.*)')
# #    To:     self.ordering_parts = re.compile(r'(.*)\s(ASC|DESC)(.*)', re.DOTALL)
# # 2. No other changes are needed because the same regex is used in get_order_by (line 356) and get_extra_select (line 369).
# # TEST_HINT:
# # - test_style: pytest (since the project uses unittest but the test file uses TestCase, we'll match the existing style)
# # - test_file_location: tests/expressions/tests.py
# # - existing_test_example: tests/expressions/tests.py (specifically the test_order_by_exists method)
# # - relevant_imports:
# #     from django.db.models.expressions import RawSQL
# #     from django.db import models
# # - test_setup: Use the existing Company and Employee models from tests/expressions/tests.py (no additional setup needed)
# # - trigger: Create a queryset that uses multiple RawSQL expressions in order_by where the RawSQL strings have identical endings (e.g., both end with 'else null end') but differ earlier, causing the bug to remove subsequent duplicates.
# # - verify_with: Assert that the queryset returns all expected rows (i.e., the order_by clause includes all RawSQL expressions, not just the first one).
# # Example test case (for reference, not to be written by us):
# #     def test_raw_sql_multiline_order_by(self):
# #         # Create test data if needed
# #         qs = MyModel.objects.all().order_by(
# #             RawSQL('''
# #                     case when status in ('accepted', 'verification')
# #                              then 2 else 1 end''', []).desc(),
# #             RawSQL('''
# #                     case when status in ('accepted', 'verification')
# #                              then (accepted_datetime, preferred_datetime)
# #                              else null end''', []).asc(),
# #             RawSQL('''
# #                     case when status not in ('accepted', 'verification')
# #                              then (accepted_datetime, preferred_datetime, created_at)
# #                              else null end''', []).desc()
# #         )
# #         # Evaluate the queryset and check that all three orderings are applied
# #         # (e.g., by checking the SQL generated or the results)
# #         # The exact assertion depends on the model and data, but the key is that
# #         # the second RawSQL (with 'else null end') should not be dropped.
# # """}
#     # ── Stage 1: Planner ──────────────────────────────────────────────
#     doc["stage"] = "planner"
#     planner_result = run_planner(user_issue)

#     if planner_result["status"] != "success":
#         print(f"❌ Planner failed: {planner_result['content']}")
#         doc["architect_plan"] = planner_result.get("content", "")
#         return finalize_failure_doc(
#             doc            = doc,
#             stage          = "planner",
#             failure_reason = planner_result.get("reason", "max_iterations"),
#             messages       = planner_result.get("messages", []),
#             model          = "mistralai/devstral-2-123b-instruct-2512",
#         )

#     planner_plan = planner_result["content"]
#     doc["architect_plan"] = planner_plan
#     print(f"\n📋 Planner done ({len(planner_plan)} chars)")

#     # ── Stage 1b: Hint Writer ─────────────────────────────────────────
#     doc["stage"] = "hint_writer"
#     hint_result = run_hint_writer(planner_plan)

#     if hint_result["status"] != "success":
#         print(f"❌ Hint writer failed: {hint_result['content']}")
#         # Pipeline can still continue with empty hints —
#         # test writer and implementer will have less info but can still try
#         print("⚠️ Continuing without hints...")
#         test_hint = ""
#         impl_hint = ""
#     else:
#         hint_content = hint_result["content"]
#         test_hint = ""
#         impl_hint = ""

#         if "TEST_HINT:" in hint_content:
#             test_hint = hint_content[hint_content.index("TEST_HINT:"):]
#             if "IMPL_HINT:" in test_hint:
#                 impl_hint = test_hint[test_hint.index("IMPL_HINT:"):]
#                 test_hint = test_hint[:test_hint.index("IMPL_HINT:")].strip()

#     doc["test_hint"] = test_hint
#     doc["impl_hint"] = impl_hint
#     print(f"📋 Hints ready — TEST_HINT: {len(test_hint)} chars, IMPL_HINT: {len(impl_hint)} chars")


#     # ── Stage 2: Spin up sandbox (shared by all remaining agents) ─────
#     print("\n🖥️  Spinning up sandbox...")
#     time.sleep(60)
#     sandbox, pythonpath, pytestflags = setup_developer_environment(repo_url)

#     # ── Stage 3: Environment probe (runs once, shared by all agents) ──
#     print("\n🔍 Probing environment...")
#     env_summary, env = probe_environment(sandbox)
#     env['pythonpath'] = pythonpath
#     env["pytestflags"] = pytestflags
#     doc["env_summary"] = env_summary
#     print(env_summary)

#     # ── Stage 4: Repo context (runs once, shared by all agents) ───────
#     print("\n📁 Building repo context...")
#     repo_context = build_repo_context(sandbox, planner_plan)

#     # ── Stage 5: Test Writer ──────────────────────────────────────────
#     doc["stage"]  = "test_writer"
#     test_result = run_test_writer(
#         architect_plan = planner_plan,
#         user_issue= user_issue,
#         env_summary    = env_summary,
#         env            = env,
#         repo_context   = repo_context,
#         sandbox        = sandbox,
#         max_iterations = 20,
#         test_hint = test_hint,
#     )

#     if test_result["status"] != "success":
#         print(f"❌ Test Writer failed: {test_result['content']}")
#         # sandbox.kill()
#         return finalize_failure_doc(
#             doc            = doc,
#             stage          = "test_writer",
#             failure_reason = test_result.get("reason", "max_iterations"),
#             messages       = test_result.get("messages", []),
#             model          = "deepseek-ai/deepseek-v3.1",
#             sandbox        = sandbox,
#         )
#     print(f"\n🧪 Failing test ready: {test_result.get('test_file', 'unknown')}")
#     doc["test_result"] = {
#         "test_file":    test_result.get("test_file", ""),
#         "test_command": test_result.get("test_command", ""),
#         "content":      test_result.get("content", ""),
#     }

#     # ── Stage 6: Implementer + Verifier loop ─────────────────────────
#     # Retry the implement+verify cycle up to MAX_PIPELINE_RETRIES times
#     # before giving up. Each retry starts the implementer fresh but
#     # keeps the same sandbox, env, and confirmed failing test.
#     for attempt in range(1, MAX_PIPELINE_RETRIES + 1):
#         print(f"\n🔁 Implement+Verify attempt {attempt}/{MAX_PIPELINE_RETRIES}")
#         doc["stage"] = "implementer"

#         impl_result = run_implementer(
#             architect_plan = planner_plan,
#             test_result    = test_result,
#             env_summary    = env_summary,
#             env            = env,
#             repo_context   = repo_context,
#             sandbox        = sandbox,
#             max_iterations = 25,
#             impl_hint      = impl_hint,   # ← add this
#         )

#         if impl_result["status"] != "success":
#             print(f"❌ Implementer failed: {impl_result['content']}")
#             if attempt == MAX_PIPELINE_RETRIES or "TAKEOVER" in impl_result['content']:
#                 return finalize_failure_doc(
#                     doc            = doc,
#                     stage          = "implementer",
#                     failure_reason = impl_result.get("reason", "max_iterations"),
#                     messages       = impl_result.get("messages", []),
#                     model          = "deepseek-ai/deepseek-v3.1",
#                     sandbox        = sandbox,
#                 )
#             print("🔄 Resetting git state for retry...")
#             sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
#             continue

#         doc["partial_diff"] = impl_result.get("git_diff", "")
#                 # ── Stage 7: Verifier ─────────────────────────────────────────
#         doc["stage"] = "verifier"

#         verdict = run_verifier(
#             git_diff       = impl_result["git_diff"],
#             test_result    = test_result,
#             architect_plan = planner_plan,
#             env            = env,
#             sandbox        = sandbox,
#         )

#         doc["verifier_verdict"] = verdict

#         if verdict["verdict"] == "PASS":
#             print("\n🎉 PIPELINE COMPLETE — All checks passed.")
#             sandbox.kill()
#             result = finalize_success_doc(doc, impl_result["git_diff"], verdict)
#             return {"status": "success", "git_diff": impl_result["git_diff"], "verdict": verdict, "doc": result}
#             # return {
#             #     "status":   "success",
#             #     "git_diff": impl_result["git_diff"],
#             #     "verdict":  verdict,
#             # }

#         else:
#             print(f"\n⚠️ Verifier FAIL: {verdict['summary']}")
#             print(f"Issues: {verdict.get('issues', [])}")

#             if attempt < MAX_PIPELINE_RETRIES:
#                 print("🔄 Resetting and retrying implementation...")
#                 sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
#             else:
#                 print("🛑 Max retries reached.")
#                 return finalize_failure_doc(
#                 doc            = doc,
#                 stage          = "verifier",
#                 failure_reason = "verifier_failed",
#                 messages       = [],
#                 model          = "deepseek-ai/deepseek-v3.1",
#                 sandbox        = sandbox,
#             )

#     sandbox.kill()
#     return {"status": "failed", "doc": doc}


if __name__ == "__main__":
    # issue_url = "https://github.com/scrapy/scrapy/issues/7260"
    # res = get_issue(issue_url)
    # issue_title = res['title']
    # issue_body = res['body']
    # rep = res['repo']
    # owner = res['owner']
    # repo_url = f"https://api.github.com/repos/{owner}/{rep}"
    # result = run_pipeline(f'{issue_title} \n {issue_body}', repo)



    repo_url = "https://github.com/sphinx-doc/sphinx"
    user_issue = r'''

Issue: `sphinx-quickstart` with existing conf.py doesn't exit easily
**Describe the bug**
I've attached a screenshot in the screenshots section which I think explains the bug better.

- I'm running `sphinx-quickstart` in a folder with a conf.py already existing. 
- It says *"Please enter a new root path name (or just Enter to exit)"*. 
- However, upon pressing 'Enter' it returns an error message *"Please enter a valid path name"*. 


**To Reproduce**
Steps to reproduce the behavior:
```
$ sphinx-quickstart
$ sphinx-quickstart
```

**Expected behavior**
After pressing Enter, sphinx-quickstart exits. 

**Your project**
n/a

**Screenshots**

![sphinx-enter-exit](https://user-images.githubusercontent.com/30437511/121676712-4bf54f00-caf8-11eb-992b-636e56999d54.png)
I press Enter for the first prompt.


**Environment info**
- OS: Ubuntu 20.04
- Python version: Python 3.8.5
- Sphinx version: sphinx-build 3.2.1 
- Sphinx extensions:  none
- Extra tools: none

**Additional context**
I had a quick search but couldn't find any similar existing issues. Sorry if this is a duplicate.

# '''

    user_issue = format_issue_for_pipeline(res)
    # print("FINAL ISSUE: ", user_issue)

    
    # simple_clone(repo_url)
    # build_and_save_graph('testRepos')

    discussion_result = run_discussion_loop(user_issue)

    # if not discussion_result["proceed"]:
    #     print("\n👋 Pipeline not started. Exiting.")
    #     exit(0)


    full_issue = user_issue
    # if discussion_result["extra_context"]:
    #     full_issue += f"\n\nADDITIONAL CONTEXT FROM USER:\n{discussion_result['extra_context']}"

    repo_url = repo_url.replace("api.github.com/repos", "github.com") + ".git"
    result = run_pipeline(full_issue, repo_url)



    print(result)
    print("\nFINAL RESULT:", result["status"])
    if result.get("git_diff"):
        print("\nDIFF:\n", result["git_diff"])