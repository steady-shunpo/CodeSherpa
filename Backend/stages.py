import time
from sandbox_utils import probe_environment, build_repo_context
from agents.architect    import run_planner, run_hint_writer
from agents.test_writer  import run_test_writer
from agents.implementer  import run_implementer
from agents.verifier     import run_verifier
from agents.intervention import intervention_session
from llm_utils import extract_test_hint
from discussion import run_discussion_loop
from tools import get_issue, format_issue_for_pipeline, simple_clone
from repograph.construct_graph import build_and_save_repograph
from failure_doc import (
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
            doc            = doc,
            stage          = "planner",
            failure_reason = result.get("reason", "max_iterations"),
            messages       = result.get("messages", []),
            model          = "mistralai/mistral-medium-3.5-128b",
        ), Nonedoc["architect_plan"] = result.get("content", "")
        return finalize_failure_doc(
        

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
    # env_summary, env = probe_environment(sandbox)
    env_summary = ""
    env =  {}
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
