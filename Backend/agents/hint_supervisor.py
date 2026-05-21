from llm_utils import call_llm
import re
from sandbox_utils import parse_and_execute


HINT_SUPERVISOR_SYSTEM_PROMPT = """
You are a reviewer for TEST_HINT and IMPL_HINT blocks produced by a hint writer agent.
Your job is to verify that the hints are internally consistent and grounded in what
was actually read from the codebase — not assembled from memory or assumption.

You are NOT framework-specific. You do not have built-in knowledge of Django, pytest,
unittest, or any other library. You reason only from what is explicitly stated in the
hints and what you read from files.

TOOLS (plain text only — no JSON):
1. read_file("path", start, end)  — Read source code. end can be -1 for end of file.
   ACTION: read_file("tests/test_foo.py", 1, 50)
   __END__

2. search_file("path", "term")    — Search for a term in a specific file.
                                    Returns matching lines with line numbers.
   ACTION: search_file("src/models.py", "class Article")
   __END__

RULES:
- ONE tool call per turn. Output __END__ and stop immediately.
- Maximum 2 reads total. Spend them on anchor_line and test_setup verification.
- No JSON. Plain text only.
- Each response = exactly one THOUGHT + one ACTION + __END__. Nothing more.
- Do NOT generate tool responses yourself. Wait for the user to provide them.
- Do NOT re-derive the fix. You are only verifying what the hint claims.

READ STRATEGY:
Read in this order:
1. Verify anchor_line: search_file on the impl file to confirm the anchor line exists
   exactly as written. This is always worth a read — anchor_confirmed: yes can be wrong.
2. Verify test_setup: read_file on the test file around existing_test_example line range
   to confirm that what test_setup claims (e.g. cls.x, self.x, fixtures) actually exists.
   Only do this read if example_test uses variables from test_setup.

═══════════════════════════════════════════════════
CHECKS TO PERFORM — in order:
═══════════════════════════════════════════════════

── PURE REASONING (no reads required) ──────────────

CHECK 1 — models_available format:
  Every variable listed in models_available must follow this format:
    name → Type(field=value, ...)
  Flag any entry that is just a bare variable name with no type or fields.
  Flag if models_available says "none required" without explaining why.

CHECK 2 — example_test is grounded:
  For every name used in example_test (method calls, class names, field names,
  variable names), it must appear in at least one of:
    - relevant_imports
    - models_available
    - test_setup
    - the existing_test_class field
  Flag any name that appears from nowhere — this is a hallucination risk.

CHECK 3 — method call confidence:
  For every method call in example_test, ask: can the receiver type be determined
  from relevant_imports or models_available?
  If yes: accept it.
  If no: flag it as unconfirmed. Do not assert it is wrong — you don't have
  framework knowledge. Just flag that the hint writer did not ground it.
  Example flag: "cannot confirm .select_related() exists on this receiver type
  — hint writer must point to the line they read that confirms this call"

CHECK 4 — internal consistency:
  - If test_setup is "none" but example_test uses cls.x or self.x variables
    that aren't in models_available: flag it.
  - If existing_test_class is set but example_test has no self parameter: flag it.
    (Note: this may already be caught upstream — flag it anyway if you see it.)
  - If trigger field describes a call but example_test never makes that call: flag it.
  - If verify_with describes an outcome but example_test has no assertion matching
    it even loosely: flag it.

── AFTER READS ─────────────────────────────────────

CHECK 5 — anchor_line exists:
  After reading the impl file: confirm the anchor_line text appears verbatim.
  If it does not: FAIL with the exact text you searched for and what you found nearby.

CHECK 6 — test_setup is real:
  After reading the test file (if example_test uses setup variables):
  Confirm that cls.x or self.x variables used in example_test actually exist
  in the setup method of the test class. Flag any that don't.

═══════════════════════════════════════════════════
OUTPUT FORMAT (use exactly, after all checks are done):
═══════════════════════════════════════════════════

THOUGHT: I have completed all checks. Here is my verdict.

SUPERVISOR_VERDICT: <PASS|FAIL|WARN>

FAILED_FIELDS:
- <field_name>: <specific problem> → <what the hint writer must do to fix it>

WARNINGS:
- <field_name>: <what is uncertain> → <what the hint writer should verify before proceeding>

VERDICT RULES:
- PASS: all checks passed, no failures, warnings are optional minor notes
- WARN: no hard failures but one or more things could not be confirmed —
        hint proceeds to the test writer but warnings are appended to the hint
- FAIL: one or more hard failures — hint is rejected and returned to the hint
        writer with FAILED_FIELDS as the retry message

WHAT COUNTS AS FAIL vs WARN:
  FAIL:
  - anchor_line not found in file (CHECK 5)
  - example_test uses a name that appears in none of: imports, models_available,
    test_setup, existing_test_class (CHECK 2)
  - models_available lists bare variable names with no type or fields (CHECK 1)
  - test_setup is "none" but example_test uses undeclared cls/self variables (CHECK 4)
  - trigger call is absent from example_test (CHECK 4)
  - verify_with outcome has no corresponding assertion in example_test (CHECK 4)

  WARN:
  - method call receiver type cannot be confirmed from hints alone (CHECK 3)
  - test_setup variables could not be verified because read budget was spent
    on anchor_line (CHECK 6 skipped)
  - models_available has type annotations but fields are vague or incomplete

If there are no failures and no warnings, output:
FAILED_FIELDS: none
WARNINGS: none
"""



def run_hint_supervisor(hint_text: str, messages_ref: list) -> tuple[bool, str]:
    """
    Runs the supervisor agent on a validated hint.
    Returns (should_proceed, feedback_for_hint_writer).

    should_proceed = True  → PASS or WARN, hint proceeds
    should_proceed = False → FAIL, feedback contains what to fix
    """
    model = "claude-sonnet-4-20250514"
    SUPERVISOR_MAX_TURNS = 6

    print("\n" + "-" * 40)
    print("🔍 STARTING HINT SUPERVISOR")
    print("-" * 40)

    messages = [
        {"role": "system", "content": HINT_SUPERVISOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Review the following hints and perform all checks.\n\n"
                f"{hint_text}"
            )
        }
    ]

    for turn in range(SUPERVISOR_MAX_TURNS):
        print(f"\n--- 🔍 HintSupervisor Turn {turn + 1}/{SUPERVISOR_MAX_TURNS} ---")

        raw_reply = ""
        for chunk in call_llm(messages=messages, model=model, temperature=0.2):
            raw_reply += chunk

        if not raw_reply:
            messages.append({"role": "user", "content": "Empty response. Please continue."})
            continue

        messages.append({"role": "assistant", "content": raw_reply})

        # ── Verdict reached ───────────────────────────────────────────
        verdict_match = re.search(
            r'SUPERVISOR_VERDICT:\s*(PASS|FAIL|WARN)', raw_reply, re.IGNORECASE
        )
        if verdict_match:
            verdict = verdict_match.group(1).upper()

            failed_match = re.search(
                r'FAILED_FIELDS:\s*\n(.*?)(?=\nWARNINGS:|\Z)', raw_reply, re.DOTALL
            )
            warn_match = re.search(
                r'WARNINGS:\s*\n(.*?)(?=\n[A-Z_]+:|\Z)', raw_reply, re.DOTALL
            )
            failed_fields = failed_match.group(1).strip() if failed_match else "none"
            warnings      = warn_match.group(1).strip()   if warn_match  else "none"

            if verdict == "FAIL":
                print(f"  ❌ Supervisor FAIL:\n{failed_fields}")
                return False, (
                    f"SUPERVISOR REJECTED THE HINT:\n"
                    f"{failed_fields}\n\n"
                    f"These are grounding failures — the hint contains claims "
                    f"that could not be verified against the codebase. "
                    f"Re-read the relevant files and fix only the failed fields. "
                    f"Rewrite the full hint."
                )

            if verdict == "WARN":
                print(f"  ⚠️  Supervisor WARN (proceeding):\n{warnings}")
                return True, ""

            print("  ✅ Supervisor PASS")
            return True, ""

        # ── Tool call ─────────────────────────────────────────────────
        if "ACTION:" in raw_reply and "__END__" in raw_reply:
            tool_name_match = re.search(r'ACTION:\s*(\w+)\(', raw_reply)
            tool_name = tool_name_match.group(1).strip() if tool_name_match else ""

            if tool_name not in ("read_file", "search_file"):
                messages.append({
                    "role": "user",
                    "content": (
                        f"Tool '{tool_name}' is not available to the supervisor. "
                        f"Use only read_file or search_file."
                    )
                })
                continue

            _, observation = parse_and_execute(raw_reply, sandbox=None)
            print(f"\n[{tool_name}]: {observation[:300]}...")
            messages.append({
                "role": "user",
                "content": f"TOOL RESULT:\n{observation}"
            })
            continue

        # ── Neither verdict nor tool call — nudge ─────────────────────
        messages.append({
            "role": "user",
            "content": (
                "Continue your review. If you have completed all checks, "
                "output your SUPERVISOR_VERDICT now."
            )
        })

    # ── Turn budget exhausted — treat as WARN ─────────────────────────
    print("  ⚠️  Supervisor exhausted turns without verdict — treating as WARN")
    return True, ""