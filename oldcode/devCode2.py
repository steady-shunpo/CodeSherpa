import re
import os
from google import genai
from google.genai import types
from tools import setup_developer_environment, run_remote_command, read_remote_file, edit_remote_file, write_remote_file, checkpoint_gate
from dotenv import load_dotenv
import time
from openai import OpenAI
import json
import hashlib
import httpx
load_dotenv()
# Assuming you have setup_developer_environment and your E2B tools (read_remote_file, etc.) defined above
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_DEV"))
client2 = OpenAI(
    api_key=os.environ.get("NVIDIA_API_KEY"),
    base_url="https://integrate.api.nvidia.com/v1",
)


DEV_SYSTEM_PROMPT = """
You are a Senior AI Software Engineer executing a Technical Design Document in a secure sandbox.

TOOLS (use EXACT plain text format — no JSON, no markdown):

read_file("filepath", start_line, end_line)
edit_file("filepath", start_line, end_line)
|||
<raw code here>
|||
write_file("filepath")
|||
<raw code here>
|||
run_bash_command("command")
search_file("filepath", "search_term")

RULES:
- ONE tool call per turn. Stop after __END__.
- Never guess. Always read_file before edit_file.
- Never declare SUCCESS unless your last OBSERVATION showed a passing test.
- Write a failing test FIRST, then fix, then re-run to confirm it passes.

OUTPUT FORMAT:
THOUGHT: <your reasoning>
ACTION: <exactly one tool call as shown above>
__END__

OR when done:
THOUGHT: <confirm the test passed>
FINAL_RESULT:
STATUS: SUCCESS
CHANGES_MADE: <list of files changed>
"""



SUPERVISOR_SYSTEM_PROMPT = """
You are a supervisor monitoring an AI coding agent.
You will be given the last N actions the agent took.
Your job is to detect if the agent is stuck and output a single JSON object.

STUCK patterns to look for:
- Reading the same file more than once
- Searching for the same term more than once  
- Repeatedly stating the same conclusion without acting on it
- More than 3 consecutive read/search actions with no write/edit/test action

Output ONLY this JSON, nothing else:
{
  "stuck": true/false,
  "reason": "one sentence explanation or empty string",
  "intervention": "specific instruction to give the agent, or empty string"
}
"""

def run_supervisor(recent_actions: list[str]) -> dict:
    """
    Passes the last N agent actions to a supervisor LLM.
    Returns {"stuck": bool, "reason": str, "intervention": str}
    """
    if len(recent_actions) < 3:
        return {"stuck": False, "reason": "", "intervention": ""}

    actions_text = "\n".join(
        f"Turn {i+1}: {action}" 
        for i, action in enumerate(recent_actions)
    )

    try:
        response = client2.chat.completions.create(
            model="deepseek-ai/deepseek-v3.1",
            messages=[
                {"role": "system", "content": SUPERVISOR_SYSTEM_PROMPT},
                {"role": "user",   "content": f"Here are the agent's last {len(recent_actions)} actions:\n\n{actions_text}"}
            ],
            temperature=0.0,  # fully deterministic for a binary decision
            timeout=30,       # supervisor should be fast
        )
        raw = response.choices[0].message.content.strip()

        # Strip markdown fences if model wraps in ```json
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)

    except Exception as e:
        print(f"⚠️ Supervisor call failed: {e}")
        return {"stuck": False, "reason": "", "intervention": ""}


def extract_action_line(reply: str) -> str:
    """Pull just the ACTION line from a reply for the supervisor's action log."""
    for line in reply.splitlines():
        if line.strip().startswith("ACTION:"):
            return line.strip()
    # If no ACTION line, return first 120 chars as summary
    return reply.strip()[:120]


TOOL_PATTERNS = {
    "read": re.compile(r'ACTION:\s*read_file\(\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(-?\d+)\s*\)'),
    "bash":   re.compile(r'ACTION:\s*run_bash_command\(\s*"((?:[^"\\]|\\.)*)"\s*\)'),  # ← handles \"
    "search": re.compile(r'ACTION:\s*search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "edit":   re.compile(r'ACTION:\s*edit_file\(\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*\|\|\|\n(.*?)\|\|\|', re.DOTALL),
    "write":  re.compile(r'ACTION:\s*write_file\(\s*"([^"]+)"\s*\)\s*\|\|\|\n(.*?)\|\|\|', re.DOTALL),
}

def build_tool_result_message(tool_name: str, observation: str, turns_left: int) -> dict:
    """
    Returns the observation as a proper user message with clear structure.
    This helps the model distinguish tool output from conversation.
    """
    warning = ""
    if turns_left == 1:
        warning = "\n\n[CRITICAL: This is your last turn. Output FINAL_RESULT now.]"
    elif turns_left <= 3:
        warning = f"\n\n[WARNING: {turns_left} turns remaining.]"

    return {
        "role": "user",
        "content": f"TOOL: {tool_name}\nOBSERVATION:\n{observation}{warning}",
        # Store full observation as metadata for compression later
        # This key is ignored by the API but available to compress_old_messages
        "_tool_name": tool_name,
        "_observation": observation,
    }


def parse_and_execute(agent_reply: str, sandbox) -> tuple[str, str]:
    """
    Returns (tool_name, observation). Tries all patterns.
    """
    if m := TOOL_PATTERNS["edit"].search(agent_reply):
        filepath, start, end, code = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        print(f"🔧 edit_file: {filepath} lines {start}-{end}")
        return "edit_file", edit_remote_file(sandbox, filepath, start, end, code)

    if m := TOOL_PATTERNS["write"].search(agent_reply):
        filepath, code = m.group(1), m.group(2)
        print(f"🔧 write_file: {filepath}")
        return "write_file", write_remote_file(sandbox, filepath, code)

    if m := TOOL_PATTERNS["read"].search(agent_reply):
        filepath, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        print(f"🔧 read_file: {filepath} lines {start}-{end}")
        if end == -1:
            # Get total line count first, then read to end
            wc = run_remote_command(sandbox, f"wc -l workspace/repo/{filepath}")
            try:
                end = int(wc.strip().split()[0])
            except (ValueError, IndexError):
                end = 99999  # fallback
        return "read_file", read_remote_file(sandbox, filepath, start, end)

    if m := TOOL_PATTERNS["bash"].search(agent_reply):
        cmd = m.group(1)
        cmd = cmd.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
        print(f"🔧 bash: {cmd}")
        return "run_bash_command", run_remote_command(sandbox, f"cd workspace/repo && {cmd}")

    if m := TOOL_PATTERNS["search"].search(agent_reply):
        filepath, term = m.group(1), m.group(2)
        print(f"🔧 search_file: {filepath} for '{term}'")
        result = run_remote_command(sandbox, f"cd workspace/repo && grep -n '{term}' {filepath}")
        return "search_file", result

    return "none", "ERROR: No valid ACTION detected. Re-read the tool format and try again."




def try_parse_json_action(agent_reply: str) -> tuple[str, dict] | None:
    """
    If the model responds in JSON format, extract the action and parse it
    into something we can execute. Returns (tool_name, params) or None.
    """
    try:
        # Handle both raw JSON and JSON wrapped in the response
        text = agent_reply.strip()
        # Sometimes it's embedded — find the first { ... }
        json_start = text.find('{')
        json_end = text.rfind('}')
        if json_start == -1 or json_end == -1:
            return None
        
        data = json.loads(text[json_start:json_end + 1])
        action_str = data.get("action", "")
        if not action_str:
            return None
        return action_str  # Return the raw action string for existing parsers to handle
    except (json.JSONDecodeError, KeyError):
        return None


def extract_action_string(agent_reply: str) -> str:
    """
    Normalizes the agent reply to always produce a plain-text ACTION line,
    regardless of whether the model responded in JSON or plain text format.
    """
    # Already in correct format
    if "ACTION:" in agent_reply:
        return agent_reply

    # Try JSON extraction
    json_action = try_parse_json_action(agent_reply)
    if json_action:
        # Reconstruct as plain text so existing TOOL_PATTERNS still match
        return f"THOUGHT: (recovered from JSON)\nACTION: {json_action}"

    return agent_reply  # Return as-is, will hit the "none" fallback


def get_reply_hash(text: str) -> str:
    return hashlib.md5(text.strip().encode()).hexdigest()


STUCK_LOOP_INJECTION = """
STOP. Your last {n} responses were IDENTICAL and produced no result.
You are stuck in a loop.

MANDATORY RECOVERY STEPS:
1. You MUST switch to plain text format immediately — NO JSON.
2. Your response must start with the literal word THOUGHT: on its own line.
3. Then ACTION: on its own line with exactly one tool call.
4. End with __END__ on its own line.

Example of the ONLY acceptable format:
THOUGHT: I will search for the test file.
ACTION: run_bash_command("grep -r 'bracket_split' tests/ --include='*.py' | head -5")
__END__

Do NOT output JSON. Do NOT output any other format. Begin now.
"""


def build_repo_context(sandbox, architect_plan: str) -> str:
    
    # File tree (lightweight, always useful)
    tree = run_remote_command(sandbox, 
        "cd workspace/repo && find . -type f -name '*.py' | grep -v __pycache__ | sort")
    
    # Extract keywords from the plan to filter symbols
    # Grab anything that looks like a function/class name (snake_case or CamelCase)
    keywords = re.findall(r'\b([a-z_][a-z0-9_]{2,}|[A-Z][a-zA-Z0-9]{2,})\b', architect_plan)
    keywords = list(set(keywords))[:20]  # cap at 20
    
    # Build a grep pattern from keywords
    if keywords:
        pattern = '\\|'.join(keywords)
        symbols = run_remote_command(sandbox,
            f"cd workspace/repo && grep -rn 'def \\|class ' --include='*.py' "
            f"| grep -v __pycache__ | grep -E '({'|'.join(keywords)})'")
    else:
        # Fallback: just get top-level defs, skip indented ones (methods)
        symbols = run_remote_command(sandbox,
            "cd workspace/repo && grep -rn '^def \\|^class ' --include='*.py' | grep -v __pycache__")
    
    if len(symbols) > 8000:
        symbols = symbols[:8000] + "\n... (truncated — use search_file for more)"

    # Test files
    test_files = run_remote_command(sandbox,
        "cd workspace/repo && find . -name 'test_*.py' -o -name '*_test.py' | sort")
    
    # README
    readme = run_remote_command(sandbox,
        "cd workspace/repo && cat README.md 2>/dev/null | head -30")

    return f"""
CODEBASE SNAPSHOT:

FILE TREE:
{tree}

RELEVANT SYMBOLS (filtered to architect plan keywords):
{symbols}

TEST FILES:
{test_files}

README:
{readme}
"""    


def summarize_observation(tool_name: str, observation: str, agent_thought: str) -> str:
    """
    Compresses a tool result into a one-line summary for history.
    The full observation is only needed in the turn it was received.
    """
    lines = observation.strip().splitlines()
    line_count = len(lines)

    if tool_name == "read_file":
        # Keep first 3 lines so model knows what it saw
        preview = "\n".join(lines[:3])
        return f"[read_file: {line_count} lines read. Preview: {preview}...]"

    elif tool_name == "run_bash_command":
        if "ERROR" in observation or "error" in observation.lower():
            return f"[bash: FAILED — {observation[:200]}]"
        elif "(no matches found)" in observation:
            return f"[bash: no matches found]"
        else:
            preview = "\n".join(lines[:5])
            return f"[bash: {line_count} lines output. Preview:\n{preview}...]"

    elif tool_name == "write_file":
        return f"[write_file: {observation.strip()}]"

    elif tool_name == "edit_file":
        return f"[edit_file: {observation.strip()}]"

    elif tool_name == "search_file":
        preview = "\n".join(lines[:5])
        return f"[search_file: {line_count} matches. Preview:\n{preview}...]"

    elif tool_name == "none":
        return f"[ERROR: no valid action detected]"

    return f"[{tool_name}: {observation[:150]}...]"


def compress_old_messages(messages: list, keep_recent: int = 6) -> list:
    """
    Keeps the system prompt, plan message, and last `keep_recent` messages
    in full. Everything in between gets its observation compressed to a
    one-liner summary.

    Structure after compression:
    [system] [plan] [SUMMARY BLOCK] [last N messages in full]
    """
    # Always preserve these
    system_msg = messages[0]
    plan_msg = messages[1]
    middle = messages[2:-keep_recent] if len(messages) > keep_recent + 2 else []
    recent = messages[max(2, len(messages) - keep_recent):]

    if not middle:
        return messages  # Nothing to compress yet

    # Compress middle messages — turn each user/assistant pair into a summary
    compressed_lines = []
    j = 0
    while j < len(middle):
        msg = middle[j]

        if msg["role"] == "assistant":
            # Extract just the THOUGHT and ACTION lines, drop the body
            thought = ""
            action = ""
            for line in msg["content"].splitlines():
                if line.startswith("THOUGHT:"):
                    thought = line
                elif line.startswith("ACTION:"):
                    action = line
            compressed_lines.append(f"{thought} | {action}")

        elif msg["role"] == "user" and msg["content"].startswith("TOOL:"):
            # Already structured — just grab the first line
            first_line = msg["content"].splitlines()[0]
            compressed_lines.append(f"  → {first_line}")

        j += 1

    summary_block = {
        "role": "user",
        "content": (
            "[COMPRESSED HISTORY — earlier turns summarized to save context]\n"
            + "\n".join(compressed_lines)
            + "\n[END COMPRESSED HISTORY — full detail resumes below]"
        )
    }

    return [system_msg, plan_msg, summary_block] + list(recent)


def should_compress(messages: list, threshold_chars: int = 40000) -> bool:
    total = sum(len(m["content"]) for m in messages)
    return total > threshold_chars


def call_llm_streaming(messages: list, temperature: float = 0.2, timeout: int = 60, retries: int = 3) -> str:
    """
    Streams the response token by token, stops early on __END__.
    Returns the full reply string.
    """
    for attempt in range(retries):
        try:
            stream = client2.chat.completions.create(
                model="deepseek-ai/deepseek-v3.1",
                messages=messages,
                stop=["__END__"],
                temperature=temperature,
                timeout=timeout,
                stream=True,  # ← only change to the API call
            )

            full_reply = ""
            print()  # newline before streaming output starts

            for chunk in stream:
                # print("DELTA", chunk)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta is None:
                    continue

                print(delta, end="", flush=True)  # print token as it arrives
                full_reply += delta

                # Early exit if __END__ slips through despite being a stop token
                if "__END__" in full_reply:
                    full_reply = full_reply.split("__END__")[0]
                    break

            print()  # newline after streaming finishes
            return full_reply.strip()

        except httpx.ReadTimeout:
            print(f"\n⏱️ Stream timeout (attempt {attempt+1}/{retries}). Retrying...")
            time.sleep(5 * (attempt + 1))

        except httpx.ConnectError:
            print(f"\n🔌 Connection error (attempt {attempt+1}/{retries}). Retrying...")
            time.sleep(10)

        except Exception as e:
            print(f"\n❌ Unexpected error (attempt {attempt+1}/{retries}): {e}")
            time.sleep(5)

    print("🛑 All retries exhausted.")
    return ""



def run_developer_agent(architect_plan: str, repo_url: str, max_iterations: int = 30) -> str:
    # time.sleep(60)
    print("\n" + "=" * 50)
    print("🛠️  STARTING DEVELOPER AGENT")
    print("=" * 50)

    sandbox = setup_developer_environment(repo_url)

    repo_context = build_repo_context(sandbox, architect_plan)

    messages = [
        {"role": "system", "content": DEV_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{repo_context}\n\n"
                f"ARCHITECT'S PLAN:\n{architect_plan}\n\n"
                "You already have the full file tree and all function locations above. "
                "Do NOT waste turns exploring the codebase — you can see everything already. "
                "Begin by writing a failing test."
            )
        }
    ]

    reply_history = []

    def _run_loop(max_iters: int) -> str:
        """Inner loop — shares messages and sandbox via closure."""
        nonlocal messages, reply_history
        i = 0
        action_log = []  # rolling list of what the agent actually did
        SUPERVISOR_WINDOW = 4   # how many recent actions supervisor sees
        SUPERVISOR_EVERY  = 3

        while i < max_iters:
            print(f"\n--- 🤖 Developer Iteration {i + 1}/{max_iters} ---")

            # ── Context management ────────────────────────────────────────
            total_chars = sum(len(m["content"]) for m in messages)
            estimated_tokens = total_chars // 4
            print(f"📊 Context: ~{estimated_tokens} tokens across {len(messages)} messages")
            if should_compress(messages):
                before = len(messages)
                messages = compress_old_messages(messages, keep_recent=6)
                print(f"🗜️ Compressed history: {before} → {len(messages)} messages")

            print("hi1")
            response = call_llm_streaming(messages)
            print("hi2")

            raw_reply = response
            if not raw_reply:
                print("⚠️ Empty response, retrying...")
                messages.append({"role": "user", "content": "Your last response was empty. Please continue."})
                i += 1
                continue

            print("hi3")
            # Stuck loop detection
            reply_hash = get_reply_hash(raw_reply)
            print("hi4")
            reply_history.append(reply_hash)
            print("hi5")

            # simpler: just count from the end
            consecutive_identical = 0
            for h in reversed(reply_history):
                if h == reply_hash:
                    consecutive_identical += 1
                else:
                    break
            print("hi6")

            if consecutive_identical >= 2:
                print(f"🔁 Stuck loop detected ({consecutive_identical}x). Injecting recovery.")
                messages.append({
                    "role": "user",
                    "content": STUCK_LOOP_INJECTION.format(n=consecutive_identical)
                })
                response = call_llm_streaming(messages, 0.7)
                raw_reply = response
                reply_history.clear()



            elif i > 0 and i % SUPERVISOR_EVERY == 0 and len(action_log) >= 3:
                print(f"👁️ Running supervisor check...")
                verdict = run_supervisor(action_log[-SUPERVISOR_WINDOW:])

                if verdict.get("stuck"):
                    print(f"🚨 Supervisor: STUCK — {verdict['reason']}")
                    intervention = verdict.get("intervention", "")

                    messages.append({
                        "role": "user",
                        "content": (
                            f"[SUPERVISOR INTERVENTION]\n"
                            f"You are stuck: {verdict['reason']}\n\n"
                            f"{intervention}\n\n"
                            f"You are FORBIDDEN from using read_file or search_file on your next turn. "
                            f"You MUST use write_file, edit_file, or run_bash_command."
                        )
                    })
                    # Re-call with higher temp to break the pattern
                    raw_reply = call_llm_streaming(messages, temperature=0.6)
                    reply_history.clear()
                    action_log.clear()  # reset log after intervention
                else:
                    print(f"✅ Supervisor: on track — {verdict.get('reason', 'no issues')}")


            agent_reply = extract_action_string(raw_reply)
            print("hi7")
            if agent_reply != raw_reply:
                print("⚠️  JSON format detected and normalized.")
            print(agent_reply)

            messages.append({"role": "assistant", "content": raw_reply})

            action_log.append(extract_action_line(raw_reply))

            # Completion check
            if "FINAL_RESULT:" in agent_reply:
                print("\n✅ Developer reports task complete.")
                git_diff_output = run_remote_command(sandbox, "cd workspace/repo && git diff")
                if not git_diff_output.strip():
                    git_diff_output = "(No git diff detected.)"

                decision = checkpoint_gate("Developer", git_diff_output)

                if decision["status"] == "PROCEED":
                    sandbox.kill()
                    return git_diff_output
                elif decision["status"] == "RETRY":
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your changes were rejected.\nFeedback: {decision['feedback']}\n\n"
                            "Use read_file to review current state, then fix."
                        )
                    })
                    reply_history.clear()
                    # Add more iterations and continue the SAME loop
                    return _run_loop(max_iters=7)
                elif decision["status"] == "TAKEOVER":
                    sandbox.kill()
                    return "TAKEOVER_TRIGGERED"

                sandbox.kill()
                return agent_reply

            # Tool execution
            turns_left = max_iters - (i + 1)
            tool_name, observation = parse_and_execute(agent_reply, sandbox)
            print(f"\n[Observation ({tool_name})]: {observation}...")

            if tool_name == "none":
                observation = (
                    "ERROR: No valid ACTION detected.\n"
                    f"Your response started with: {raw_reply[:120]!r}\n\n"
                    "Required format:\n"
                    "THOUGHT: your reasoning\n"
                    "ACTION: run_bash_command(\"your command\")\n"
                    "__END__\n\n"
                    "Do NOT use JSON. Plain text only."
                )

            messages.append(build_tool_result_message(tool_name, observation, turns_left))
            i += 1

        # ── Timeout — ask user without losing context ─────────────────────
        print("\n🛑 Reached max iterations.")
        print("Continue (+10 turns)? [t] / Kill [k] / Takeover [p]")
        ans = input().strip().lower()

        if ans == "t":
            print("🔄 Continuing with same context...")
            messages.append({
                "role": "user",
                "content": (
                    "[SYSTEM: You have been granted 10 additional turns. "
                    "Resume exactly where you left off. Do not restart or repeat completed steps.]"
                )
            })
            return _run_loop(max_iters=10)  # ← same closure, same messages

        elif ans == "p":
            sandbox.kill()
            return "TAKEOVER_TRIGGERED"

        sandbox.kill()
        return "TIMEOUT"

    return _run_loop(max_iterations)


# res = run_developer_agent(r"""

# Edit the file `astropy/modeling/separable.py` in the function `_cstack`.
# In the else branch for the right operand (when `right` is not a Model), replace the line:
#     cright[-right.shape[0]:, -right.shape[1]:] = 1
# with:
#     cright[-right.shape[0]:, -right.shape[1]:] = right

# This ensures that the coordinate matrix of the right operand is correctly placed in the result, rather than filling the block with ones.     

#                           """, "https://github.com/astropy/astropy")
