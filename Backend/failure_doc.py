import os
from llm_utils import call_llm, MODEL
import os
import json
from datetime import datetime
import re



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



TOOL_NAMES = {
    "edit_file", "write_file", "read_files_bulk", "read_file",
    "run_bash_command", "search_file", "reset_file", "line_count",
    "search_repo", "none",
}


import uuid
from datetime import datetime


class BytesEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='replace') # Gracefully handles non-utf8 bytes too
        if isinstance(o, (uuid.UUID, datetime)):
            return str(o)
        try:
            return super().default(o)
        except TypeError:
            return str(o)
    

def _trim_messages_for_summary(messages: list) -> list:
    trimmed = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant":
            trimmed.append({"role": "assistant", "content": content})

        elif role == "user" and isinstance(content, str):
            # Format is:
            #   [TOOL RESULT — tool_name]\n
            #   The tool ran and returned this output. <observation>
            #   <optional turns warning>
            match = re.match(
                r"^\[TOOL RESULT — [^\]]+\]\n"       # strip header line
                r"The tool ran and returned this output\. ?"  # strip boilerplate
                r"(.*?)"                              # capture observation
                r"(?:\n\n\[(?:WARNING|CRITICAL)[^\]]*\])?$",  # strip trailing warning
                content,
                flags=re.DOTALL,
            )
            if match:
                observation = match.group(1).strip()
                if observation:
                    trimmed.append({"role": "user", "content": observation})
            # If it doesn't match the tool result format, it's the initial
            # user prompt — skip it to avoid including the full issue/repo context.

    return trimmed

def finalize_failure_doc(
    doc: dict,
    stage: str,
    failure_reason: str,
    messages: list,
    model: str = MODEL,
) -> dict:
    """
    Finalize a failure document, generate a failure summary via LLM,
    and save it to the runs/ folder with a timestamped filename.

    Returns the finalized doc dict.
    """
    # Update doc fields
    doc["stage"] = stage
    doc["status"] = "failed"
    doc["failure_reason"] = failure_reason
    doc["timestamp"] = datetime.utcnow().isoformat()

    trimmed = _trim_messages_for_summary(messages)

    # Generate failure summary from messages
    if messages:
        summary_prompt = [
            {
                "role": "user",
                "content": (
                    "You are analyzing a failed AI agent run. "
                    "Below is the message history from the run. "
                    "Write a concise failure summary covering:\n"
                    "1. What the agent was trying to do\n"
                    "2. What steps it took\n"
                    "3. Where it got stuck or went wrong\n"
                    "4. The likely root cause\n\n"
                    f"Messages:\n{json.dumps(trimmed, indent=2, cls=BytesEncoder)}"
                ),
            }
        ]
        raw_reply = ""
        for chunk in call_llm(summary_prompt, model=model, temperature=0.3):
            raw_reply += chunk
        doc["failure_summary"] = raw_reply.strip()
    else:
        doc["failure_summary"] = doc.get("failure_summary", "")

    # Build filename: timestamp with colons/dots replaced, plus stage
    ts = doc["timestamp"]  # e.g. "2026-05-07T09:50:44.202755"
    # Replace : with - and . with - to get a safe filename
    ts_safe = ts.replace(":", "-").replace(".", "-")
    filename = f"{ts_safe}_{stage}.json"

    # Save to runs/
    os.makedirs("runs", exist_ok=True)
    filepath = os.path.join("runs", filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False, cls=BytesEncoder)

    return doc