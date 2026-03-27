# failure_doc.py

import json
import os
from datetime import datetime


def create_failure_doc(repo_url: str, user_issue: str) -> dict:
    """
    Creates an empty failure doc at the start of every pipeline run.
    Fields get populated as the pipeline progresses.
    """
    return {
        # ── Always present ────────────────────────────────────────
        "status":           "running",
        "stage":            "starting",
        "failure_reason":   "none",
        "timestamp":        datetime.now().isoformat(),
        "repo_url":         repo_url,
        "user_issue":       user_issue,
        "env_summary":      "",

        # ── Available if architect ran ────────────────────────────
        "architect_plan":   "",
        "test_hint":        "",

        # ── Available if test writer ran ──────────────────────────
        "test_result":      {},

        # ── Available if implementer ran ──────────────────────────
        "partial_diff":     "",

        # ── Available if verifier ran ─────────────────────────────
        "verifier_verdict": {},

        # ── Failure specific ──────────────────────────────────────
        "failure_summary":  "",
        "actions_taken":    [],
        "last_observation": "",
    }


def extract_actions_taken(messages: list) -> list:
    """
    Pulls just the ACTION lines from message history.
    Used to show the chatbot what the agent actually tried.
    """
    actions = []
    for msg in messages:
        if msg["role"] != "assistant":
            continue
        for line in msg["content"].splitlines():
            if line.strip().startswith("ACTION:"):
                actions.append(line.strip())
    return actions


def extract_last_observation(messages: list) -> str:
    """
    Returns the last tool result the agent saw before failing.
    Most useful debugging context for the chatbot.
    """
    for msg in reversed(messages):
        if msg["role"] == "user" and msg["content"].startswith("TOOL:"):
            return msg["content"][:500]
    return ""


def finalize_failure_doc(
    doc:            dict,
    stage:          str,
    failure_reason: str,
    messages:       list,
    model:          str,
    sandbox=None,
) -> dict:
    """
    Called at any failure point. Populates failure-specific fields
    and generates the summary. Writes to disk.
    """
    from llm_utils import summarize_failure

    doc["status"]         = "failed"
    doc["stage"]          = stage
    doc["failure_reason"] = failure_reason
    doc["actions_taken"]  = extract_actions_taken(messages)
    doc["last_observation"] = extract_last_observation(messages)

    # Get partial diff if sandbox is still alive
    if sandbox:
        try:
            from sandbox_utils import run_remote_command
            diff = run_remote_command(sandbox, "cd workspace/repo && git diff")
            if diff.strip():
                doc["partial_diff"] = diff
        except Exception:
            pass

    # Generate summary
    include_obs = stage in ("test_writer", "implementer", "verifier")
    doc["failure_summary"] = summarize_failure(
        messages             = messages,
        model                = model,
        agent_name           = stage,
        include_observations = include_obs,
    )

    save_failure_doc(doc)
    return doc


def finalize_success_doc(doc: dict, git_diff: str, verifier_verdict: dict) -> dict:
    """
    Called when pipeline completes successfully.
    """
    doc["status"]           = "success"
    doc["stage"]            = "complete"
    doc["failure_reason"]   = "none"
    doc["partial_diff"]     = git_diff
    doc["verifier_verdict"] = verifier_verdict
    doc["failure_summary"]  = ""

    save_failure_doc(doc)
    return doc


def save_failure_doc(doc: dict) -> str:
    """
    Writes the doc to disk as JSON.
    Filename includes timestamp and stage so runs don't overwrite each other.
    """
    os.makedirs("runs", exist_ok=True)
    timestamp = doc["timestamp"].replace(":", "-").replace(".", "-")
    filename  = f"runs/{timestamp}_{doc['stage']}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)

    print(f"📄 Run doc saved: {filename}")
    return filename


def load_failure_doc(filepath: str) -> dict:
    """
    Loads a previously saved doc for post-run chatbot access.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def get_latest_doc() -> dict | None:
    """
    Loads the most recent run doc from the runs/ directory.
    Used when user triggers chatbot after a completed run.
    """
    if not os.path.exists("runs"):
        return None

    files = [
        os.path.join("runs", f)
        for f in os.listdir("runs")
        if f.endswith(".json")
    ]

    if not files:
        return None

    latest = max(files, key=os.path.getmtime)
    print(f"📄 Loading latest run doc: {latest}")
    return load_failure_doc(latest)