
import re
from llm_utils import call_llm
from config import MODEL
from tools import read_local_file, search_repo_advanced
import subprocess
import os

DISCUSSION_SYSTEM_PROMPT = """
You are a technical assistant helping a developer understand a GitHub issue before deciding whether to run an automated fix pipeline.

You have access to the repository and can read files or search for relevant code. Use these tools when the user asks about specific code, or when it would help explain the issue more concretely.

TOOLS (plain text only):
1. read_file("path", start, end)     — Read lines from a file in the repo
2. search_file("path", "term")       — Search for a term in a specific file  
3. search_repo("term")               — Find where a function or class is defined

FORMAT when using a tool:
THOUGHT: <why you need this>
ACTION: read_file("path", 1, 50)
__END__

FORMAT when responding to the user (no tool needed):
Just respond in plain conversational text. No THOUGHT/ACTION needed.

RULES:
- ONLY use tools when the user asks about specific code or it genuinely helps
- Do not explore the codebase unprompted
- Do not suggest fixes — that is the pipeline's job
- Be concise and technical
- When you think the user has enough context, suggest they start the pipeline
"""

PIPELINE_TRIGGERS = [
    "run pipeline", "start pipeline", "run it", "start it"
]

DISCUSSION_TOOL_PATTERNS = {
    "read":        __import__('re').compile(r'ACTION:\s*read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "search_file": __import__('re').compile(r'ACTION:\s*search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "search_repo": __import__('re').compile(r'ACTION:\s*search_repo\(\s*"([^"]+)"\s*\)'),
}

class DiscussionSession:
    """
    Holds all state for one discussion session.
    Works for both terminal and FastAPI — 
    terminal creates one and loops,
    FastAPI creates one per session and stores it.
    """
    def __init__(self, issue_text: str):
        self.issue_text = issue_text
        self.messages   = [
            {"role": "system", "content": DISCUSSION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Here is the issue I want to fix:\n\n{issue_text}\n\n"
                )
            }
        ]
        self.pipeline_triggered = False
        self.extra_context      = []

    def _collect(self, gen) -> str:
        """Collects a call_llm generator into a full string."""
        full = ""
        for chunk in gen:
            full += chunk
        return full.strip()

    def process_message(self, user_input: str | None = None) -> dict:
        """
        Processes one message and returns the response.
        If user_input is None, returns the initial explanation.
        
        Returns:
        {
            "response":          str,
            "used_tool":         bool,
            "pipeline_triggered": bool,
            "extra_context":     str,
        }
        """
        # ── Initial explanation (no user input yet) ───────────────
        if user_input is None:
            response = self._collect(call_llm(self.messages, model=MODEL, temperature=0.3))
            self.messages.append({"role": "assistant", "content": response})
            return {
                "response":           response,
                "used_tool":          False,
                "pipeline_triggered": False,
                "extra_context":      "",
            }

        # ── Pipeline trigger detection ────────────────────────────
        if detect_pipeline_trigger(user_input):
            self.pipeline_triggered = True
            return {
                "response":           "Starting pipeline...",
                "used_tool":          False,
                "pipeline_triggered": True,
                "extra_context":      self._collect_extra_context(),
            }

        # ── Normal message ────────────────────────────────────────
        # Track extra context
        if len(user_input) > 30 and "?" not in user_input:
            self.extra_context.append(user_input)

        self.messages.append({"role": "user", "content": user_input})
        raw_reply = self._collect(call_llm(self.messages, model=MODEL, temperature=0.3))

        # ── Tool use ──────────────────────────────────────────────
        tool_result = _discussion_parse_and_execute(raw_reply)
        used_tool   = False

        if tool_result:
            tool_name, observation = tool_result
            used_tool = True
            self.messages.append({"role": "assistant", "content": raw_reply})
            self.messages.append({
                "role":    "user",
                "content": f"TOOL: {tool_name}\nOBSERVATION:\n{observation}"
            })
            raw_reply = self._collect(call_llm(self.messages, model=MODEL, temperature=0.3))

        self.messages.append({"role": "assistant", "content": raw_reply})

        return {
            "response":           raw_reply,
            "used_tool":          used_tool,
            "pipeline_triggered": False,
            "extra_context":      "",
        }

    def _collect_extra_context(self) -> str:
        return "\n".join(self.extra_context)


# ── Terminal runner ───────────────────────────────────────────────
def run_discussion_loop(issue_text: str) -> dict:
    print("\n" + "=" * 60)
    print("💬 DISCUSSION PHASE")
    print("=" * 60)
    print("Discuss the issue. Type 'run pipeline' when ready.\n")

    session = DiscussionSession(issue_text)

    # Initial explanation
    print("🤖 Assistant: ", end="")
    result = session.process_message()
    print(result["response"])

    while True:
        print()
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Cancelled.")
            return {"proceed": False, "issue_text": issue_text, "extra_context": ""}

        if not user_input:
            continue

        if user_input.lower() == "skip":
            return {"proceed": False, "issue_text": issue_text, "extra_context": ""}

        result = session.process_message(user_input)
        
        if result["pipeline_triggered"]:
            return {
                "proceed":       True,
                "issue_text":    issue_text,
                "extra_context": result["extra_context"],
            }

        print("\n🤖 Assistant: ", end="")
        print(result["response"])






def _discussion_parse_and_execute(agent_reply: str) -> tuple[str, str] | None:
    """
    Tries to match a tool call in the reply.
    Returns (tool_name, observation) or None if no tool used.
    """
    if m := DISCUSSION_TOOL_PATTERNS["read"].search(agent_reply):
        fp, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if end == -1:
            end = 99999
        print(f"📖 read_file: {fp} lines {start}-{end}")
        return "read_file", read_local_file(fp, start, end)

    if m := DISCUSSION_TOOL_PATTERNS["search_file"].search(agent_reply):
        fp, term = m.group(1), m.group(2)
        print(f"🔎 search_file: {fp} for '{term}'")
        try:
            full_path = os.path.join("testRepos", fp)
            results = []
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, start=1):
                    if term in line:
                        results.append(f"{i}:{line.rstrip()}")
            return "search_file", "\n".join(results) if results else "(no matches found)"
        except FileNotFoundError:
            return "search_file", f"ERROR: File not found: {fp}"

    if m := DISCUSSION_TOOL_PATTERNS["search_repo"].search(agent_reply):
        term = m.group(1)
        print(f"🔍 search_repo: {term}")
        return "search_repo", search_repo_advanced(term)

    return None



def detect_pipeline_trigger(user_input: str) -> bool:
    normalized = user_input.strip().lower()
    return any(trigger in normalized for trigger in PIPELINE_TRIGGERS)


def build_discussion_messages(issue_text: str) -> list:
    """
    Builds the initial message list for a discussion session.
    Kept separate so FastAPI can initialize state without running the loop.
    """
    return [
        {"role": "system", "content": DISCUSSION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Here is the issue I want to fix:\n\n{issue_text}\n\n"
                f"Please explain what is broken and why."
            )
        }
    ]


def get_initial_explanation(messages: list) -> str:
    """
    Gets the chatbot's first response to the issue.
    Called once when the issue is first pasted.
    """
    full = ""
    for chunk in call_llm(messages, model=MODEL, temperature=0.3):
        full += chunk
    response = full.strip()
    messages.append({"role": "assistant", "content": response})
    return response


def collect_extra_context(messages: list) -> str:
    """
    Extracts any new information the user provided during discussion
    that isn't in the original issue. Passed to the pipeline.
    """
    extra = []
    # Skip system message and first user/assistant exchange
    for msg in messages[3:]:
        if msg["role"] == "user" and len(msg["content"]) > 30 and "?" not in msg["content"]:
            extra.append(msg["content"])
    return "\n".join(extra)




# ── FastAPI usage (future) ────────────────────────────────────────
# sessions: dict[str, DiscussionSession] = {}
#
# @app.post("/discussion/start")
# async def start_discussion(issue_text: str):
#     session = DiscussionSession(issue_text)
#     sessions[session_id] = session
#     result = session.process_message()  # initial explanation
#     return result
#
# @app.post("/discussion/message")
# async def send_message(session_id: str, user_input: str):
#     result = sessions[session_id].process_message(user_input)
#     if result["pipeline_triggered"]:
#         # kick off pipeline
#         pass
#     return result