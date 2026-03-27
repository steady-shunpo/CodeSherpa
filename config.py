import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── API Client ────────────────────────────────────────────────────────────────
client = OpenAI(
    api_key=os.environ.get("NVIDIA_API_KEY"),
    base_url="https://integrate.api.nvidia.com/v1",
)

MODEL = "deepseek-ai/deepseek-v3.1"

# ── Shared prompt snippets ────────────────────────────────────────────────────

TOOL_FORMAT_REMINDER = """
REQUIRED FORMAT (plain text only — no JSON, no markdown):
THOUGHT: your reasoning
ACTION: tool_name("arg")
__END__
"""

# config.py
DEFAULT_RUNTIME_BINS = {
    "python":     "python3",
    "javascript": "node",
    "go":         "go",
    "java":       "java",
    "rust":       "cargo",
    "ruby":       "ruby",
    "cpp":        "g++",
    "unknown":    "",
}

STUCK_LOOP_INJECTION = """
STOP. Your last {n} responses were identical and produced no progress.

You MUST switch to plain text format immediately.
THOUGHT: ...
ACTION: <one tool call>
__END__

No JSON. No explanations. Just act.
"""

SUPERVISOR_SYSTEM_PROMPT = """
Allow further read/search ONLY if the repository infra is stated to be complex. DO NOT mark stuck as true in this case. 
You are a supervisor monitoring an AI coding agent.
You will receive the agent's last N action lines.
Detect if the agent is stuck in a loop.

STUCK patterns:
- Reading or searching the same file/term more than once
- Repeatedly stating the same conclusion without acting
- More than 3 consecutive read/search actions with no write/edit/test action
- Trying the same failing command more than once

Output ONLY this JSON object, nothing else:
{
  "stuck": true or false,
  "reason": "one sentence or empty string",
  "intervention": "specific instruction for the agent or empty string"
}
"""