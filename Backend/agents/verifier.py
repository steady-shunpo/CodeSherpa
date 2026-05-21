from llm_utils import call_llm
from sandbox_utils import run_remote_command


VERIFIER_SYSTEM_PROMPT = """
Role: QA Engineer

You will be given:
- A git diff of the implemented fix
- A test file and test command
- The architect's original plan

Your job:
1. Run the failing test — confirm it now passes
2. Run the full test suite — confirm nothing is broken
3. Review the diff — confirm it matches the architect's plan

Output ONLY this JSON:
{
  "verdict": "PASS" or "FAIL",
  "target_test": "passed or failed",
  "full_suite": "passed, N failed, or error",
  "diff_matches_plan": true or false,
  "issues": ["list of problems if any"],
  "summary": "one sentence"
}
"""


def run_verifier(git_diff: str, test_result: dict, architect_plan: str,
                 env: dict, sandbox) -> dict:
    print("\n" + "=" * 50)
    print("✅ STARTING VERIFIER")
    print("=" * 50)

    test_command = test_result.get("test_command", env.get("test_command", "pytest"))
    test_file    = test_result.get("test_file", "")

    # ── Run target test ───────────────────────────────────────────────
    print(f"🧪 Running target test: {test_command}")
    if test_file:
        target_cmd = f"cd workspace/repo && {test_command} {test_file} -v 2>&1 | tail -30"
    else:
        target_cmd = f"cd workspace/repo && {test_command} -v 2>&1 | tail -30"
    target_output = run_remote_command(sandbox, target_cmd)
    print(f"Target test output:\n{target_output[:500]}")

    # ── Run full suite ────────────────────────────────────────────────
    print(f"🧪 Running full test suite: {test_command}")
    full_cmd    = f"cd workspace/repo && {test_command} 2>&1 | tail -30"
    full_output = run_remote_command(sandbox, full_cmd)
    print(f"Full suite output:\n{full_output[:500]}")

    # ── Ask LLM to interpret results ──────────────────────────────────
    import json, re
    messages = [
        {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"ARCHITECT'S PLAN:\n{architect_plan}\n\n"
                f"GIT DIFF:\n{git_diff}\n\n"
                f"TARGET TEST OUTPUT:\n{target_output}\n\n"
                f"FULL SUITE OUTPUT:\n{full_output}"
            )
        }
    ]

    raw = ""
    for chunk in call_llm(messages, model="mistralai/devstral-2-123b-instruct-2512", temperature=0.0, timeout=30):
        raw += chunk
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: parse manually from output
        passed = "passed" in target_output.lower() and "failed" not in target_output.lower()
        verdict = {
            "verdict":           "PASS" if passed else "FAIL",
            "target_test":       "passed" if passed else "failed",
            "full_suite":        full_output[-200:],
            "diff_matches_plan": True,
            "issues":            [],
            "summary":           "LLM verdict parsing failed — inferred from output."
        }

    print(f"\n🏁 Verifier verdict: {verdict.get('verdict')} — {verdict.get('summary')}")
    return verdict