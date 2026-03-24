import time
from sandbox_utils import probe_environment, build_repo_context
from agents.architect    import run_architect
from agents.test_writer  import run_test_writer
from agents.implementer  import run_implementer
from agents.verifier     import run_verifier
from llm_utils import extract_test_hint

# Your existing sandbox setup function
from tools import setup_developer_environment


MAX_PIPELINE_RETRIES = 2  # how many times to retry the full impl+verify loop


def run_pipeline(user_issue: str, repo_url: str) -> dict:
    print("\n" + "=" * 60)
    print("🚀 PIPELINE START")
    print("=" * 60)

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
        if "TAKEOVER" in arch_result['content']:
            print(arch_result['content'])
        return {"status": "failed", "stage": "architect", "content": arch_result["content"]}
    
    architect_plan = arch_result["content"]
    if "TEST_HINT:" not in arch_result['content']:
        architect_plan = arch_result['content']
        test_hint = ""
    else:
        architect_plan =  arch_result['content'].split("TEST_HINT:")[0].strip()
        test_hint =  arch_result['content'].split("TEST_HINT:")[-1].strip()

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
    print(env_summary)

    # ── Stage 4: Repo context (runs once, shared by all agents) ───────
    print("\n📁 Building repo context...")
    repo_context = build_repo_context(sandbox, architect_plan)

    # ── Stage 5: Test Writer ──────────────────────────────────────────
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
        sandbox.kill()
        return {"status": "failed", "stage": "test_writer", "content": test_result["content"]}

    print(f"\n🧪 Failing test ready: {test_result.get('test_file', 'unknown')}")

    # ── Stage 6: Implementer + Verifier loop ─────────────────────────
    # Retry the implement+verify cycle up to MAX_PIPELINE_RETRIES times
    # before giving up. Each retry starts the implementer fresh but
    # keeps the same sandbox, env, and confirmed failing test.
    for attempt in range(1, MAX_PIPELINE_RETRIES + 1):
        print(f"\n🔁 Implement+Verify attempt {attempt}/{MAX_PIPELINE_RETRIES}")

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
                sandbox.kill()
                return {"status": "failed", "stage": "implementer", "content": impl_result["content"]}
            # Reset git state before retry
            print("🔄 Resetting git state for retry...")
            sandbox.commands.run("cd workspace/repo && git checkout . && git clean -fd")
            continue

        # ── Stage 7: Verifier ─────────────────────────────────────────
        verdict = run_verifier(
            git_diff       = impl_result["git_diff"],
            test_result    = test_result,
            architect_plan = architect_plan,
            env            = env,
            sandbox        = sandbox,
        )

        if verdict["verdict"] == "PASS":
            print("\n🎉 PIPELINE COMPLETE — All checks passed.")
            sandbox.kill()
            return {
                "status":   "success",
                "git_diff": impl_result["git_diff"],
                "verdict":  verdict,
            }

        else:
            print(f"\n⚠️ Verifier FAIL: {verdict['summary']}")
            print(f"Issues: {verdict.get('issues', [])}")

            if attempt < MAX_PIPELINE_RETRIES:
                print("🔄 Resetting and retrying implementation...")
                sandbox.commands.run("cd workspace/repo && git checkout .")
            else:
                print("🛑 Max retries reached.")
                sandbox.kill()
                return {
                    "status":   "failed",
                    "stage":    "verifier",
                    "verdict":  verdict,
                    "git_diff": impl_result["git_diff"],
                }

    sandbox.kill()
    return {"status": "failed", "stage": "pipeline", "content": "Max retries exhausted."}


if __name__ == "__main__":
    issue = """Issue: Indexed matrix-expression LaTeX printer is not compilable
```python
i, j, k = symbols("i j k")
M = MatrixSymbol("M", k, k)
N = MatrixSymbol("N", k, k)
latex((M*N)[i, j])
```

The LaTeX string produced by the last command is:
```
\sum_{i_{1}=0}^{k - 1} M_{i, _i_1} N_{_i_1, j}
```
LaTeX complains about a double subscript `_`. This expression won't render in MathJax either.

"""
    repo  = "https://github.com/sympy/sympy"
    result = run_pipeline(issue, repo)
    print("\nFINAL RESULT:", result["status"])
    if result.get("git_diff"):
        print("\nDIFF:\n", result["git_diff"])