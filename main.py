import time
from sandbox_utils import probe_environment, build_repo_context
from agents.architect    import run_architect
from agents.test_writer  import run_test_writer
from agents.implementer  import run_implementer
from agents.verifier     import run_verifier
from llm_utils import extract_test_hint
from tools import get_issue
from failure_doc import (
    create_failure_doc, finalize_failure_doc,
    finalize_success_doc, get_latest_doc
)
from discussion import run_discussion_loop

# Your existing sandbox setup function
from tools import setup_developer_environment


MAX_PIPELINE_RETRIES = 2  # how many times to retry the full impl+verify loop


def run_pipeline(user_issue: str, repo_url: str) -> dict:
    print("\n" + "=" * 60)
    print("🚀 PIPELINE START")
    print("=" * 60)

    doc = create_failure_doc(repo_url, user_issue)

    # ── Stage 1: Architect ────────────────────────────────────────────
    arch_result = run_architect(user_issue)
#     arch_result = {"status": "success", "content": """
# FINAL_PLAN:
# 1. Modify the regex pattern in SQLCompiler.__init__ to include the re.DOTALL flag.
#    File: django/db/models/sql/compiler.py
#    Line: 35
#    Change: self.ordering_parts = re.compile(r'(.*)\s(ASC|DESC)(.*)')
#    To:     self.ordering_parts = re.compile(r'(.*)\s(ASC|DESC)(.*)', re.DOTALL)
# 2. No other changes are needed because the same regex is used in get_order_by (line 356) and get_extra_select (line 369).
# TEST_HINT:
# - test_style: pytest (since the project uses unittest but the test file uses TestCase, we'll match the existing style)
# - test_file_location: tests/expressions/tests.py
# - existing_test_example: tests/expressions/tests.py (specifically the test_order_by_exists method)
# - relevant_imports:
#     from django.db.models.expressions import RawSQL
#     from django.db import models
# - test_setup: Use the existing Company and Employee models from tests/expressions/tests.py (no additional setup needed)
# - trigger: Create a queryset that uses multiple RawSQL expressions in order_by where the RawSQL strings have identical endings (e.g., both end with 'else null end') but differ earlier, causing the bug to remove subsequent duplicates.
# - verify_with: Assert that the queryset returns all expected rows (i.e., the order_by clause includes all RawSQL expressions, not just the first one).
# Example test case (for reference, not to be written by us):
#     def test_raw_sql_multiline_order_by(self):
#         # Create test data if needed
#         qs = MyModel.objects.all().order_by(
#             RawSQL('''
#                     case when status in ('accepted', 'verification')
#                              then 2 else 1 end''', []).desc(),
#             RawSQL('''
#                     case when status in ('accepted', 'verification')
#                              then (accepted_datetime, preferred_datetime)
#                              else null end''', []).asc(),
#             RawSQL('''
#                     case when status not in ('accepted', 'verification')
#                              then (accepted_datetime, preferred_datetime, created_at)
#                              else null end''', []).desc()
#         )
#         # Evaluate the queryset and check that all three orderings are applied
#         # (e.g., by checking the SQL generated or the results)
#         # The exact assertion depends on the model and data, but the key is that
#         # the second RawSQL (with 'else null end') should not be dropped.
# """}
    if arch_result["status"] != "success":
        print(f"❌ Architect failed: {arch_result['content']}")
        if "TAKEOVER::" in arch_result['content']:
            # print(arch_result['content'])
            architect_plan = arch_result["content"]
            if "TEST_HINT:" not in arch_result['content']:
                architect_plan = arch_result['content']
                test_hint = ""
            else:
                architect_plan =  arch_result['content'].split("TEST_HINT:")[0].strip()
                test_hint =  arch_result['content'].split("TEST_HINT:")[-1].strip()
            doc['architect_plan'] = architect_plan
            doc['test_hint'] = test_hint


        return finalize_failure_doc(
            doc            = doc,
            stage          = "architect",
            failure_reason = arch_result.get("reason", "max_iterations"),
            messages       = arch_result.get("messages", []),
            model          = "deepseek-ai/deepseek-v3.1",
        )
        # return {"status": "failed", "stage": "architect", "content": arch_result["content"]}
    
    architect_plan = arch_result["content"]
    if "TEST_HINT:" not in arch_result['content']:
        architect_plan = arch_result['content']
        test_hint = ""
    else:
        architect_plan =  arch_result['content'].split("TEST_HINT:")[0].strip()
        test_hint =  arch_result['content'].split("TEST_HINT:")[-1].strip()
    doc['architect_plan'] = architect_plan
    doc['test_hint'] = test_hint

    # test_hint = extract_test_hint(architect_plan)
    print(f"\n📋 Architect plan received ({len(architect_plan)} chars)")
    print("Test hint: ", '\n', test_hint)

    # ── Stage 2: Spin up sandbox (shared by all remaining agents) ─────
    print("\n🖥️  Spinning up sandbox...")
    time.sleep(60)
    sandbox = setup_developer_environment(repo_url)

    # ── Stage 3: Environment probe (runs once, shared by all agents) ──
    print("\n🔍 Probing environment...")
    env_summary, env = probe_environment(sandbox)
    doc["env_summary"] = env_summary
    print(env_summary)

    # ── Stage 4: Repo context (runs once, shared by all agents) ───────
    print("\n📁 Building repo context...")
    repo_context = build_repo_context(sandbox, architect_plan)

    # ── Stage 5: Test Writer ──────────────────────────────────────────
    doc["stage"]  = "test_writer"
    test_result = run_test_writer(
        architect_plan = architect_plan,
        user_issue= user_issue,
        env_summary    = env_summary,
        env            = env,
        repo_context   = repo_context,
        sandbox        = sandbox,
        max_iterations = 20,
        test_hint = test_hint,
    )

    if test_result["status"] != "success":
        print(f"❌ Test Writer failed: {test_result['content']}")
        # sandbox.kill()
        return finalize_failure_doc(
            doc            = doc,
            stage          = "test_writer",
            failure_reason = test_result.get("reason", "max_iterations"),
            messages       = test_result.get("messages", []),
            model          = "deepseek-ai/deepseek-v3.1",
            sandbox        = sandbox,
        )
    print(f"\n🧪 Failing test ready: {test_result.get('test_file', 'unknown')}")
    doc["test_result"] = {
        "test_file":    test_result.get("test_file", ""),
        "test_command": test_result.get("test_command", ""),
        "content":      test_result.get("content", ""),
    }

    # ── Stage 6: Implementer + Verifier loop ─────────────────────────
    # Retry the implement+verify cycle up to MAX_PIPELINE_RETRIES times
    # before giving up. Each retry starts the implementer fresh but
    # keeps the same sandbox, env, and confirmed failing test.
    for attempt in range(1, MAX_PIPELINE_RETRIES + 1):
        print(f"\n🔁 Implement+Verify attempt {attempt}/{MAX_PIPELINE_RETRIES}")
        doc["stage"]  = "implementer"

        impl_result = run_implementer(
            architect_plan = architect_plan,
            test_result    = test_result,
            env_summary    = env_summary,
            env            = env,
            repo_context   = repo_context,
            sandbox        = sandbox,
            max_iterations = 25,
        )
        print(env)

        if impl_result["status"] != "success":
            print(f"❌ Implementer failed: {impl_result['content']}")
            if attempt == MAX_PIPELINE_RETRIES or "TAKEOVER" in impl_result['content']:
                # sandbox.kill()
                return finalize_failure_doc(
                    doc            = doc,
                    stage          = "implementer",
                    failure_reason = impl_result.get("reason", "max_iterations"),
                    messages       = impl_result.get("messages", []),
                    model          = "deepseek-ai/deepseek-v3.1",
                    sandbox        = sandbox,
                )
            # Reset git state before retry
            print("🔄 Resetting git state for retry...")
            sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
            continue

        doc["partial_diff"] = impl_result.get("git_diff", "")

        # ── Stage 7: Verifier ─────────────────────────────────────────
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
            print("\n🎉 PIPELINE COMPLETE — All checks passed.")
            sandbox.kill()
            result = finalize_success_doc(doc, impl_result["git_diff"], verdict)
            return {"status": "success", "git_diff": impl_result["git_diff"], "verdict": verdict, "doc": result}
            # return {
            #     "status":   "success",
            #     "git_diff": impl_result["git_diff"],
            #     "verdict":  verdict,
            # }

        else:
            print(f"\n⚠️ Verifier FAIL: {verdict['summary']}")
            print(f"Issues: {verdict.get('issues', [])}")

            if attempt < MAX_PIPELINE_RETRIES:
                print("🔄 Resetting and retrying implementation...")
                sandbox.commands.run("cd workspace/repo && git checkout .")
            else:
                print("🛑 Max retries reached.")
                return finalize_failure_doc(
                doc            = doc,
                stage          = "verifier",
                failure_reason = "verifier_failed",
                messages       = [],
                model          = "deepseek-ai/deepseek-v3.1",
                sandbox        = sandbox,
            )

    sandbox.kill()
    return {"status": "failed", "doc": doc}


if __name__ == "__main__":
    # issue_url = "https://github.com/fastapi/fastapi/issues/13056"
    # res = get_issue(issue_url)
    # issue_title = res['title']
    # issue_body = res['body']
    # rep = res['repo']
    # owner = res['owner']
    # repo = f"https://api.github.com/repos/{owner}/{rep}"
    # result = run_pipeline(f'{issue_title} \n {issue_body}', repo)



    repo_url = "https://github.com/sympy/sympy"
    user_issue = r'''

Issue: collect_factor_and_dimension does not detect equivalent dimensions in addition
Code to reproduce:
```python
from sympy.physics import units
from sympy.physics.units.systems.si import SI

v1 = units.Quantity('v1')
SI.set_quantity_dimension(v1, units.velocity)
SI.set_quantity_scale_factor(v1, 2 * units.meter / units.second)

a1 = units.Quantity('a1')
SI.set_quantity_dimension(a1, units.acceleration)
SI.set_quantity_scale_factor(a1, -9.8 * units.meter / units.second**2)

t1 = units.Quantity('t1')
SI.set_quantity_dimension(t1, units.time)
SI.set_quantity_scale_factor(t1, 5 * units.second)

expr1 = a1*t1 + v1
SI._collect_factor_and_dimension(expr1)
```
Results in:
```
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
  File "C:\Python\Python310\lib\site-packages\sympy\physics\units\unitsystem.py", line 179, in _collect_factor_and_dimension
    raise ValueError(
ValueError: Dimension of "v1" is Dimension(velocity), but it should be Dimension(acceleration*time)
```


'''

    discussion_result = run_discussion_loop(user_issue)

    if not discussion_result["proceed"]:
        print("\n👋 Pipeline not started. Exiting.")
        exit(0)


    full_issue = user_issue
    if discussion_result["extra_context"]:
        full_issue += f"\n\nADDITIONAL CONTEXT FROM USER:\n{discussion_result['extra_context']}"

    result = run_pipeline(full_issue, repo_url)



    print(result)
    print("\nFINAL RESULT:", result["status"])
    if result.get("git_diff"):
        print("\nDIFF:\n", result["git_diff"])