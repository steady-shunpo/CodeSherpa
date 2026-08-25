import pytest
from agents.intervention import parse_intervention_decision


def test_parse_instruction_decision():
    reply = """
    THOUGHT: I see the issue with the imports in tests.
    <instruction stage="test_writer">
    [DIAGNOSIS]: Missing import for fixture 'mock_client'.
    [CORRECTION]: Import from 'my_app.testing.fixtures'.
    [MANDATORY ACTION]: Rewrite the test with proper fixtures.
    </instruction>
    """
    decision = parse_intervention_decision(reply)
    assert decision["decision"] == "INSTRUCTION"
    assert decision["stage"] == "test_writer"
    assert "[DIAGNOSIS]: Missing import for fixture 'mock_client'." in decision["instruction"]


def test_parse_patch_decision():
    reply = """
    THOUGHT: The test hint had the wrong trigger method.
    <patch field="test_hint">
    - test_style: pytest
    - trigger: cookies.extract_cookies()
    </patch>
    """
    decision = parse_intervention_decision(reply)
    assert decision["decision"] == "PATCH"
    assert "test_hint" in decision["patches"]
    assert "cookies.extract_cookies()" in decision["patches"]["test_hint"]


def test_parse_escalate_decision():
    reply = """
    THOUGHT: The problem requires a design choice from the maintainer.
    <escalate>
    The issue mentions deprecating v1 endpoints, but does not specify if backward compatibility is required. Please advise.
    </escalate>
    """
    decision = parse_intervention_decision(reply)
    assert decision["decision"] == "ESCALATE"
    assert "backward compatibility" in decision["escalate_reason"]


def test_parse_plain_qa_decision():
    reply = """
    In this repository, the main entry point is `app/main.py`. The models are defined under `db/models.py`.
    Let me know if you would like me to inspect any specific files!
    """
    decision = parse_intervention_decision(reply)
    assert decision["decision"] == "NONE"
    assert decision["instruction"] is None
    assert decision["patches"] == {}
    assert decision["escalate_reason"] is None


def test_prune_old_tool_observations():
    from llm_utils import prune_old_tool_observations

    messages = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "INITIAL ISSUE PROMPT"},
        {"role": "assistant", "content": "ACTION: read_file(\"old_file.py\", 1, 100)"},
        {"role": "user", "content": "[TOOL RESULT — read_file]\nThe tool ran and returned this output. 100 lines of code... line 1\nline 2\nline 3"},
        {"role": "assistant", "content": "ACTION: search_file(\"old_file.py\", \"foo\")"},
        {"role": "user", "content": "[TOOL RESULT — search_file]\nThe tool ran and returned this output. match 1\nmatch 2"},
        {"role": "assistant", "content": "ACTION: read_file(\"recent_file.py\", 1, 50)"},
        {"role": "user", "content": "[TOOL RESULT — read_file]\nThe tool ran and returned this output. RECENT CODE CONTENT"},
    ]

    pruned = prune_old_tool_observations(messages, keep_recent=2)
    assert len(pruned) == len(messages)
    # Check system and user prompt are preserved
    assert pruned[0]["content"] == "SYSTEM PROMPT"
    assert pruned[1]["content"] == "INITIAL ISSUE PROMPT"

    # Check older tool observation at index 3 was truncated
    assert "truncated" in pruned[3]["content"]
    assert "100 lines of code" not in pruned[3]["content"]

    # Check recent tool observation at index 7 is preserved with full content
    assert "RECENT CODE CONTENT" in pruned[7]["content"]


def test_bytes_encoder_with_uuid():
    import uuid
    import json
    from datetime import datetime
    from agents.intervention import BytesEncoder

    doc = {
        "repograph_id": uuid.uuid4(),
        "timestamp": datetime.now(),
        "raw_bytes": b"hello world",
    }

    serialized = json.dumps(doc, cls=BytesEncoder)
    assert str(doc["repograph_id"]) in serialized
    assert "hello world" in serialized


def test_generate_supervisor_turn_nudge_parsing(monkeypatch):
    from llm_utils import generate_supervisor_turn_nudge

    # Test READY parsing
    monkeypatch.setattr("llm_utils.call_llm", lambda *args, **kwargs: ["STATUS: READY\nDIRECTIVE: You already found the fix. Output FINAL_PLAN now."])
    res_ready = generate_supervisor_turn_nudge([{"role": "user", "content": "task"}], "Planner")
    assert res_ready["status"] == "READY"
    assert res_ready["granted_turns"] == 1
    assert "Output FINAL_PLAN now" in res_ready["directive"]

    # Test EXPLORING parsing
    monkeypatch.setattr("llm_utils.call_llm", lambda *args, **kwargs: ["STATUS: EXPLORING\nDIRECTIVE: Look at src/black/linegen.py."])
    res_exploring = generate_supervisor_turn_nudge([{"role": "user", "content": "task"}], "Planner")
    assert res_exploring["status"] == "EXPLORING"
    assert res_exploring["granted_turns"] == 4
    assert "src/black/linegen.py" in res_exploring["directive"]
