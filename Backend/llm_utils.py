import re
import json
import time
import hashlib
import httpx
import logging
from config import client, MODEL, SUPERVISOR_SYSTEM_PROMPT, STUCK_LOOP_INJECTION
from sandbox_utils import TOOL_PATTERNS
from streaming import publish_token
import asyncio
import threading
from ratelimit import limits, sleep_and_retry

logger = logging.getLogger(__name__)

# ── Hashing ───────────────────────────────────────────────────────────────────

def get_reply_hash(text: str) -> str:
    return hashlib.md5(text.strip().encode()).hexdigest()


import re

# ── Tool call completion detector ─────────────────────────────────────────────

class ToolCallDetector:
    """
    Scans the accumulated stripped reply for a COMPLETE tool call.
    'Complete' means the regex fully matches — for multiline tools like
    edit_file/write_file, this only fires once the closing ``` or ||| lands.
    Returns (tool_name, match_end_index) or (None, -1).
    """

    # Order matters: greedy multiline patterns first
    _ORDERED = [
        ("edit",      re.compile(r'(?:ACTION:\s*)?edit_file\(\s*"[^"]+"\s*,\s*-?\d+\s*,\s*-?\d+\s*\)[^\n]*\n?(?:\|\|\||```[^\n]*)\r?\n?.*?\r?\n?(?:\|\|\||```)', re.DOTALL)),
        ("write",     re.compile(r'(?:ACTION:\s*)?write_file\(\s*"[^"]+"\s*\)[^\n]*\n?(?:\|\|\||```[^\n]*)\r?\n?.*?\r?\n?(?:\|\|\||```)', re.DOTALL)),
        # ("read_bulk", re.compile(r'(?:ACTION:\s*)?read_files_bulk\(\s*\[.*?\]\s*\)', re.DOTALL)),
        ("read",      re.compile(r'(?:ACTION:\s*)?read_file\(\s*"[^"]+"\s*,\s*-?\d+\s*,\s*-?\d+\s*\)')),
        ("bash",      re.compile(r'(?:ACTION:\s*)?run_bash_command\(\s*"(?:[^"\\]|\\.)*"\s*\)')),
        ("search_repo",  re.compile(r'(?:ACTION:\s*)?search_repo\(\s*"([^"]+)"\s*\)')),
        ("list_symbols", re.compile(r'(?:ACTION:\s*)?list_symbols\(\s*"([^"]+)"\s*\)')),
        ("search",    re.compile(r'(?:ACTION:\s*)?search_file\(\s*"[^"]+"\s*,\s*"[^"]+"\s*\)')),
        ("reset",     re.compile(r'(?:ACTION:\s*)?reset_file\(\s*"[^"]+"\s*\)')),
        ("run_test",  re.compile(r'(?:ACTION:\s*)?run_python_test\(\s*"[^"]+"\s*\)')),
    ]

    def scan(self, stripped: str):
        """
        Returns (tool_name, end_index) if a complete tool call is found,
        else (None, -1).
        end_index is the position in `stripped` right after the match ends.
        """
        for name, pattern in self._ORDERED:
            m = pattern.search(stripped)
            if m:
                return name, m.end()
        return None, -1


_tool_detector = ToolCallDetector()


TIMEOUT = 60

@sleep_and_retry
@limits(35, TIMEOUT)
def call_llm(messages: list, model: str, temperature: float = 0.2,
             timeout: int = 60, retries: int = 3):
    for attempt in range(retries):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stop=["__END__"],
                temperature=temperature,
                timeout=timeout,
                stream=True,
                max_tokens=2048,
            )

            full_reply           = ""
            chunk_count          = 0
            repeat_counter       = 0
            think_repeat_counter = 0
            REPEAT_WINDOW        = 200
            REPEAT_THRESHOLD     = 3
            THINK_WINDOW         = 150
            MAX_CHARS            = 10000
            stripped             = ""
            tool_call_found      = False   # ← new flag

            print()

            for chunk in stream:
                chunk_count += 1
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta is None:
                    continue

                full_reply += delta

                inside_think = (
                    '<think>' in full_reply
                    and '</think>' not in full_reply.split('<think>')[-1]
                ) if '<think>' in full_reply else False

                if not inside_think:
                    print(delta, end="", flush=True)
                    yield delta

                # ── Hard length cap ───────────────────────────────────
                if len(full_reply) > MAX_CHARS:
                    if not inside_think:
                        print(f"\n⚠️ Response exceeded {MAX_CHARS} chars. Force stopping.")
                        try: stream.close()
                        except Exception: pass
                        break
                    elif len(full_reply) > MAX_CHARS * 3:
                        print(f"\n⚠️ Think block too long. Force stopping.")
                        try: stream.close()
                        except Exception: pass
                        full_reply = full_reply[:full_reply.rfind('<think>')]
                        break

                # ── Think block repetition ────────────────────────────
                if '<think>' in full_reply and '</think>' not in full_reply.split('<think>')[-1]:
                    think_content = full_reply.split('<think>')[-1]
                    if len(think_content) > THINK_WINDOW * 2:
                        curr = think_content[-THINK_WINDOW:]
                        prev = think_content[-(THINK_WINDOW * 2):-THINK_WINDOW]
                        if curr == prev:
                            think_repeat_counter += 1
                            if think_repeat_counter >= 2:
                                print(f"\n⚠️ Loop inside think block. Killing stream.")
                                try: stream.close()
                                except Exception: pass
                                full_reply = full_reply[:full_reply.rfind('<think>')]
                                break
                        else:
                            think_repeat_counter = 0

                # ── Recompute stripped selectively ────────────────────
                if any(t in delta for t in ['<', '>', 'ACTION', 'THOUGHT', 'FINAL', '__END__',
                                             'read_', 'write_', 'edit_', 'bash', 'search_',
                                             'reset_', 'run_', '```', '|||']) \
                        or chunk_count % 30 == 0:
                    stripped = strip_thinking(full_reply)
                    if '<think>' in stripped:
                        stripped = stripped[:stripped.rfind('<think>')]

                if not stripped:
                    continue

                # ── Hard stop on __END__ ──────────────────────────────
                if "__END__" in stripped:
                    full_reply = stripped.split("__END__")[0]
                    try: stream.close()
                    except Exception: pass
                    break

                # ── Early stop: complete tool call detected ───────────
                tool_name, end_idx = _tool_detector.scan(stripped)
                if tool_name and not tool_call_found:
                    tool_call_found = True
                    print(f"\n✂️ Complete {tool_name} call detected. Cutting stream.")
                    try: stream.close()
                    except Exception: pass
                    # Truncate: keep only up to the end of the tool call
                    full_reply = stripped[:end_idx]
                    break

                # ── Second ACTION safeguard (belt-and-suspenders) ─────
                action_count = stripped.count("ACTION:")
                if action_count > 1:
                    print(f"\n⚠️ Second ACTION detected. Killing stream.")
                    try: stream.close()
                    except Exception: pass
                    first = stripped.index("ACTION:")
                    full_reply = stripped[:stripped.index("ACTION:", first + 1)]
                    break

                # ── THOUGHT/FINAL after ACTION ────────────────────────
                if "ACTION:" in stripped:
                    after_action = stripped[stripped.index("ACTION:"):]
                    for marker in ["\nTHOUGHT:", "\nFINAL"]:
                        if marker in after_action:
                            print(f"\n⚠️ Model continued after ACTION. Killing stream.")
                            try: stream.close()
                            except Exception: pass
                            after_action = after_action[:after_action.index(marker)]
                            full_reply = stripped[:stripped.index("ACTION:")] + after_action
                            break

                # ── Output repetition ─────────────────────────────────
                if chunk_count % 50 == 0 and len(stripped) > REPEAT_WINDOW * 2:
                    curr = stripped[-REPEAT_WINDOW:]
                    prev = stripped[-(REPEAT_WINDOW * 2):-REPEAT_WINDOW]
                    curr_words = set(curr.split())
                    prev_words = set(prev.split())
                    if curr_words:
                        overlap = len(curr_words & prev_words) / len(curr_words)
                        if overlap > 0.85:
                            repeat_counter += 1
                            if repeat_counter >= REPEAT_THRESHOLD:
                                print(f"\n⚠️ Output repetition ({overlap:.0%} overlap). Killing stream.")
                                try: stream.close()
                                except Exception: pass
                                full_reply = stripped[:-REPEAT_WINDOW]
                                break
                        else:
                            repeat_counter = 0

            print(chunk_count)
            print()

            full_reply = strip_thinking(full_reply)

            if not full_reply.strip():
                print(f"⚠️ Empty reply after stripping think blocks ({chunk_count} chunks). Retrying...")
                time.sleep(5 * (attempt + 1))
                continue

            return

        except httpx.ReadTimeout:
            print(f"\n⏱️ Timeout (attempt {attempt+1}/{retries}). Retrying...")
            time.sleep(5 * (attempt + 1))
        except httpx.ConnectError:
            print(f"\n🔌 Connection error (attempt {attempt+1}/{retries}). Retrying...")
            time.sleep(10)
        except Exception as e:
            print(f"\n❌ Unexpected error (attempt {attempt+1}/{retries}): {e}")
            time.sleep(5)

    print("🛑 All retries exhausted.")
    return ""



def strip_thinking(text: str) -> str:
    """
    Removes <think>...</think> blocks that reasoning models emit.
    Also handles partial/unclosed think tags.
    """
    import re
    # Remove complete think blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Remove opening tag and everything after it if block never closed
    text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL)
    # Remove stray closing tags
    text = text.replace('</think>', '')
    return text.strip()




# ── JSON format normalization ─────────────────────────────────────────────────

def extract_action_string(agent_reply: str) -> str:
    """
    If the model responded in JSON, extract the action field
    and reformat as plain text so existing parsers still work.
    """
    if "ACTION:" in agent_reply:
        return agent_reply

    try:
        text = agent_reply.strip()
        json_start = text.find('{')
        json_end = text.rfind('}')
        if json_start == -1 or json_end == -1:
            return agent_reply
        data = json.loads(text[json_start:json_end + 1])
        action_str = data.get("action", "")
        if action_str:
            return f"THOUGHT: (recovered from JSON)\nACTION: {action_str}"
    except (json.JSONDecodeError, KeyError):
        pass

    return agent_reply


# ── Supervisor ────────────────────────────────────────────────────────────────

def run_supervisor(recent_actions: list[str]) -> dict:
    """
    Calls a supervisor LLM to check if the agent is stuck.
    Returns {"stuck": bool, "reason": str, "intervention": str}
    """
    if len(recent_actions) < 3:
        return {"stuck": False, "reason": "", "intervention": ""}

    actions_text = "\n".join(
        f"Turn {i+1}: {action}"
        for i, action in enumerate(recent_actions)
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SUPERVISOR_SYSTEM_PROMPT},
                {"role": "user",   "content": f"Agent's last {len(recent_actions)} actions:\n\n{actions_text}"}
            ],
            temperature=0.0,
            timeout=30,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"⚠️ Supervisor call failed: {e}")
        return {"stuck": False, "reason": "", "intervention": ""}


# ── Context management ────────────────────────────────────────────────────────

def should_compress(messages: list, threshold_chars: int = 40000) -> bool:
    return sum(len(m["content"]) for m in messages) > threshold_chars


def compress_old_messages(messages: list, keep_recent: int = 6) -> list:
    """
    Keeps system prompt, plan message, and last `keep_recent` messages in full.
    Everything in between is compressed to one-line summaries.
    """
    if len(messages) <= keep_recent + 2:
        return messages

    system_msg = messages[0]
    plan_msg   = messages[1]
    middle     = messages[2:-keep_recent]
    recent     = messages[-keep_recent:]

    compressed_lines = []
    for msg in middle:
        if msg["role"] == "assistant":
            thought = next((l for l in msg["content"].splitlines() if l.startswith("THOUGHT:")), "")
            action  = next((l for l in msg["content"].splitlines() if l.startswith("ACTION:")),  "")
            compressed_lines.append(f"{thought} | {action}")
        elif msg["role"] == "user" and msg["content"].startswith("TOOL:"):
            compressed_lines.append(f"  → {msg['content'].splitlines()[0]}")

    summary_block = {
        "role": "user",
        "content": (
            "[COMPRESSED HISTORY]\n"
            + "\n".join(compressed_lines)
            + "\n[END COMPRESSED HISTORY]"
        )
    }

    return [system_msg, plan_msg, summary_block] + list(recent)


def build_tool_result_message(tool_name: str, observation: str, turns_left: int) -> dict:
    warning = ""
    if turns_left == 1:
        warning = "\n\n[CRITICAL: Last turn. Output FINAL_RESULT now.]"
    elif turns_left <= 3:
        warning = f"\n\n[WARNING: {turns_left} turns remaining.]"

    return {
        "role": "user",
        "content": (
            f"[TOOL RESULT — {tool_name}]\n"
            f"The tool ran and returned this output. "
            # f"Do NOT summarize or describe it. "
            # f"Use it to decide your next ACTION.\n\n"
            f"{observation}"
            f"{warning}"
        )
    }


# ── Failure tracking ──────────────────────────────────────────────────────────

def build_failure_warning(messages: list) -> str:
    """
    Scans history for repeated failures and returns a warning string
    listing commands that have failed 2+ times.
    """
    failures = {}
    for msg in messages:
        if msg["role"] != "user":
            continue
        content = msg["content"]
        if "ERROR" not in content and "error" not in content.lower():
            continue
        for line in content.splitlines():
            if "bash:" in line.lower() or "run_bash_command" in line.lower():
                key = line.strip()[:80]
                failures[key] = failures.get(key, 0) + 1

    repeated = {k: v for k, v in failures.items() if v >= 2}
    if not repeated:
        return ""

    lines = ["[FAILURE TRACKER: These have already failed — DO NOT retry them:]"]
    for cmd, count in repeated.items():
        lines.append(f"  - Failed {count}x: {cmd}")
    lines.append("Try a completely different approach.")
    return "\n".join(lines)


# ── Loop runner (shared by all agents) ───────────────────────────────────────

    # def get_agent_explanation(messages: list, model: str, agent_name: str, failure_reason: str) -> str:
    #     """
    #     Asks the agent one final question before handoff.
    #     Only called when failure_reason suggests the agent has coherent context.
    #     """
    #     # If agent was looping or confused, its explanation won't be useful
    #     if failure_reason == "stuck_loop":
    #         return "(Agent was stuck in a loop — explanation skipped, would not be reliable)"

    #     print(f"\n🗣️ Asking {agent_name} to explain failure...")

    #     explanation_messages = messages.copy()
    #     explanation_messages.append({
    #         "role": "user",
    #         "content": (
    #             "Before we hand off to the user, explain the following clearly and concisely:\n\n"
    #             "1. What was your goal\n"
    #             "2. What approaches you tried (one line each)\n"
    #             "3. The exact last error or blocker you hit\n"
    #             "4. What you think the root cause is\n"
    #             "5. What you would try next if you had more iterations\n\n"
    #             "Be specific and technical. No fluff. Do not use any tools. Just explain."
    #         )
    #     })

    #     explanation = call_llm(
    #         messages   = explanation_messages,
    #         model      = model,
    #         temperature= 0.3,
    #         timeout    = 60,
    #         retries    = 2,
    #     )

    #     return explanation or "(Agent produced no explanation)"


def summarize_failure(messages: list, model: str, agent_name: str, include_observations: bool = False) -> str:
    history_text = ""
    print("MESSAGES: ")
    print(messages[2:8])
    for msg in messages[2:]:
        role = msg["role"]
        content = msg["content"]

        # -----------------------
        # ASSISTANT → TOOL CALL
        # -----------------------
        if role == "assistant":
            for tool_name, pattern in TOOL_PATTERNS.items():
                m = pattern.search(content)
                if m:
                    history_text += f"AGENT: {tool_name} -> {m.groups()}\n"
                    break
            else:
                # fallback if no pattern matched
                history_text += f"AGENT: {content.strip()}\n"

        # -----------------------
        # USER → TOOL RESULT
        # -----------------------
        elif role == "user":
            tool_match = re.match(r'\[TOOL RESULT — (.*?)\]', content)

            if tool_match:
                tool_name = tool_match.group(1)

                if include_observations:
                    history_text += f"RESULT ({tool_name}): {content[:300]}\n"
                else:
                    history_text += f"RESULT ({tool_name})\n"

    summary_messages = [
        {
            "role": "system",
            "content": (
                "You are summarizing what happened during a failed automated coding agent run. "
                "You will receive a log of what the agent tried. "
                "Produce a clear, technical summary covering: "
                "1) what the agent was trying to do, "
                "2) what approaches it took, "
                "3) where it got stuck or failed, "
                "4) what the likely root cause is. "
                "Be concise and specific. No fluff."
            )
        },
        {
            "role": "user",
            "content": f"Agent: {agent_name}\n\nAction log:\n{history_text}"
        }
    ]
    print("HISTORY: " )
    print(history_text[:100])

    full = ""
    for chunk in call_llm(summary_messages, model=model, temperature=0.1, timeout=60, retries=2):
        full+=chunk
    return full



def extract_action_line(reply: str) -> str:
    for line in reply.splitlines():
        if line.strip().startswith("ACTION:"):
            return line.strip()
    return reply.strip()[:120]


def prune_old_tool_observations(messages: list, keep_recent: int = 4) -> list:
    """
    Prunes bulky older tool observations to prevent quadratic token growth.
    Keeps:
      - System message (index 0)
      - Initial user task message (index 1)
      - The last `keep_recent` messages with full content
    For older tool result user messages between index 2 and len(messages) - keep_recent:
      - Collapses bulky code readings or multi-line observations into a concise summary.
    """
    if len(messages) <= keep_recent + 2:
        return messages

    pruned = [messages[0], messages[1]]
    middle = messages[2:-keep_recent]
    recent = messages[-keep_recent:]

    for msg in middle:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "user" and isinstance(content, str) and "[TOOL RESULT" in content:
            match = re.match(r"^\[TOOL RESULT — ([^\]]+)\]", content)
            if match:
                tool_name = match.group(1)
                lines = content.splitlines()
                first_content = ""
                for l in lines[1:]:
                    if l.strip() and not l.startswith("The tool ran and returned"):
                        first_content = l.strip()[:100]
                        break
                summary_text = f"[TOOL RESULT — {tool_name}]\n{first_content}... [Observation truncated for context efficiency]"
                pruned.append({"role": "user", "content": summary_text})
            else:
                pruned.append({"role": "user", "content": content[:200] + "... [Observation truncated]"})
        else:
            pruned.append(msg)

    pruned.extend(recent)
    return pruned


AGENT_SUPERVISOR_GOALS = {
    "Planner": "Agent Goal: Produce FINAL_PLAN describing root cause, bug explanation, and code changes needed.",
    "HintWriter": "Agent Goal: Produce TEST_HINT and IMPL_HINT for downstream agents.",
    "TestWriter": "Agent Goal: Write a complete standalone reproducer test file with write_file('tests/test_<name>_reproducer.py') and declare FINAL_RESULT (with STATUS: SUCCESS, TEST_FILE, TEST_COMMAND, FAILURE_OUTPUT). Do NOT edit existing test files or output bare code.",
    "Implementer": "Agent Goal: Edit the source code using edit_file() and run the test command to verify it passes, then declare FINAL_RESULT (STATUS: SUCCESS, CHANGES_MADE).",
}

def generate_supervisor_turn_nudge(messages: list, agent_name: str, model: str = MODEL) -> dict:
    """
    Analyzes the agent's turn trajectory when it hits max iterations.
    Determines if the agent is READY (already found context / answer -> grant 1 turn with tool ban)
    or EXPLORING (still searching -> grant 4 turns with specific file pointers).
    """
    initial_task = ""
    if len(messages) > 1:
        initial_task = str(messages[1].get("content", ""))[:500]

    steps = []
    for msg in messages[2:]:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "assistant":
            steps.append(f"AGENT: {content[:250]}")
        elif role == "user" and isinstance(content, str) and "[TOOL RESULT" in content:
            steps.append(f"RESULT: {content[:180]}")

    recent_trajectory = "\n".join(steps[-8:])

    clean_name = re.sub(r'[^a-zA-Z]', '', agent_name)
    agent_goal = AGENT_SUPERVISOR_GOALS.get(clean_name, f"Agent Goal: Produce the required final output for {agent_name}.")

    prompt = [
        {
            "role": "system",
            "content": (
                f"You are an expert supervisor for '{agent_name}'. The agent reached its iteration limit.\n"
                f"{agent_goal}\n\n"
                "CLASSIFICATION RULES:\n"
                "1. Output 'STATUS: READY' if the agent has already found/read the relevant code, identified the bug/root cause, or has enough context to produce the final output. Command it to stop searching and produce its required final output immediately.\n"
                "2. Output 'STATUS: EXPLORING' if the agent was searching in the wrong place, hasn't found the target file/function yet, or was stuck. State the exact real file/function to inspect.\n\n"
                "OUTPUT FORMAT (strict):\n"
                "STATUS: <READY|EXPLORING>\n"
                "DIRECTIVE: <1-2 concise, actionable sentences>"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original Task / Plan Context:\n{initial_task}\n\n"
                f"Recent agent activity:\n{recent_trajectory}\n\n"
                "Classification & Directive:"
            )
        }
    ]

    try:
        reply = ""
        for chunk in call_llm(prompt, model=model, temperature=0.1, timeout=30):
            reply += chunk
        reply = reply.strip()

        status_match = re.search(r'STATUS:\s*(READY|EXPLORING)', reply, re.IGNORECASE)
        directive_match = re.search(r'DIRECTIVE:\s*(.*)', reply, re.DOTALL | re.IGNORECASE)

        status = status_match.group(1).upper() if status_match else ("READY" if "READY" in reply else "EXPLORING")
        directive = directive_match.group(1).strip() if directive_match else reply

        granted_turns = 1 if status == "READY" else 4
        return {
            "status": status,
            "directive": directive,
            "granted_turns": granted_turns,
        }
    except Exception as e:
        return {
            "status": "READY",
            "directive": "Review the code you have already inspected and immediately produce the final required output.",
            "granted_turns": 1,
        }


from db.models import RunStatus
from db.db_utils import _set_status_sync

def run_agent_loop(
    run_id: str,
    messages: list,
    parse_and_execute,       # fn(reply, sandbox) -> (tool_name, observation)
    sandbox,
    max_iters: int,
    done_token: str,         # e.g. "FINAL_RESULT:" or "FINAL_PLAN:"
    agent_name: str,
    on_done,                 # fn(raw_reply) -> str | None  (None = keep looping)
    repograph_id,   
    model: str,       # optional env dict for supervisor reminders
    loop: asyncio.AbstractEventLoop,
    cancel_flag: threading.Event,
    env: dict = None, 
    is_complex: bool = None,
    auto_grant_budget: int = 2,
    auto_extra_turns: int = 5,
) -> str:
    """
    Generic agent loop shared by all agents.
    Handles: streaming, stuck detection, supervisor, compression,
             failure tracking, turn warnings, and continuation.
    """
    reply_history = []
    action_log    = []
    i = 0

    while i < max_iters:
        print(f"\n--- {agent_name} Iteration {i+1}/{max_iters} ---")
        logger.info(f"[{run_id}] [{agent_name}] --- Turn {i+1}/{max_iters} --- (context: {len(messages)} messages)")

        messages_to_send = prune_old_tool_observations(messages, keep_recent=4)
        if len(messages_to_send) != len(messages):
            logger.debug(f"[{run_id}] [{agent_name}] Pruned messages for context window ({len(messages_to_send)} sent)")

        # Inject failure warning without saving to history
        failure_warning = build_failure_warning(messages)
        if failure_warning:
            messages_to_send = messages_to_send.copy()
            messages_to_send.append({"role": "user", "content": failure_warning})

        raw_reply = ""
        for chunk in call_llm(messages_to_send, model=model, temperature=0.2):
            if cancel_flag.is_set():
                logger.info(f"[{run_id}] [{agent_name}] Cancel flag set — aborting loop")
                return ""
            raw_reply += chunk
            publish_token(run_id, chunk, loop)
        if not raw_reply:
            logger.warning(f"[{run_id}] [{agent_name}] [Turn {i+1}] Empty LLM response received")
            messages.append({"role": "user", "content": "Empty response. Please continue."})
            i += 1
            continue

        # ── Identical reply loop detection ────────────────────────────
        reply_hash = get_reply_hash(raw_reply)
        reply_history.append(reply_hash)

        consecutive_identical = 0
        for h in reversed(reply_history):
            if h == reply_hash:
                consecutive_identical += 1
            else:
                break

        if consecutive_identical >= 2:
            print(f"🔁 Identical loop ({consecutive_identical}x). Recovering.")
            logger.warning(f"[{run_id}] [{agent_name}] [Turn {i+1}] Identical loop detected ({consecutive_identical}x)")
            messages.append({
                "role": "user",
                "content": STUCK_LOOP_INJECTION.format(n=consecutive_identical)
            })
            raw_reply = ""

            for chunk in call_llm(messages, model=model, temperature=0.7):
                raw_reply += chunk
            reply_history.clear()

        # ── Supervisor check every 3 turns ────────────────────────────
        elif (is_complex == False or is_complex == None) and  i > 0 and i % 3 == 0 and len(action_log) >= 3:
            print("👁️ Supervisor check...")
            verdict = run_supervisor(action_log[-4:])
            if verdict.get("stuck"):
                print(f"🚨 Supervisor: {verdict['reason']}")
                logger.warning(f"[{run_id}] [{agent_name}] [Turn {i+1}] Periodic supervisor flagged stuck: {verdict['reason']}")
                messages.append({
                    "role": "user",
                    "content": (
                        f"[SUPERVISOR]: Stuck — {verdict['reason']}\n"
                        f"{verdict.get('intervention', '')}\n"
                        f"FORBIDDEN this turn: read_file, search_file\n"
                        f"REQUIRED this turn: write_file, edit_file, or run_bash_command"
                    )
                })
                raw_reply = ""
                for chunk in call_llm(messages, model=model, temperature=0.6):
                    raw_reply+=chunk
                reply_history.clear()
                action_log.clear()

        agent_reply = extract_action_string(raw_reply)
        if agent_reply != raw_reply:
            print("⚠️ JSON normalized.")

        messages.append({"role": "assistant", "content": raw_reply})
        action_log.append(extract_action_line(raw_reply))

        # ── Done check ────────────────────────────────────────────────
        if done_token in agent_reply:
            logger.info(f"[{run_id}] [{agent_name}] [Turn {i+1}] Done token '{done_token}' detected")
            result = on_done(raw_reply, messages)
            if result is not None:
                logger.info(f"[{run_id}] [{agent_name}] Done check accepted result at turn {i+1}")
                return result
            # on_done returned None → keep looping (e.g. retry after rejection)
            logger.info(f"[{run_id}] [{agent_name}] on_done rejected result — continuing loop")
            i += 1
            continue

        # ── Tool execution ────────────────────────────────────────────
        turns_left = max_iters - (i + 1)
        tool_name, observation = parse_and_execute(agent_reply, sandbox, repograph_id)
        print(f"\n[{tool_name}]: {observation}")
        logger.info(f"[{run_id}] [{agent_name}] [Turn {i+1}] Tool: {tool_name} | Obs length: {len(observation)} chars | Snippet: {observation[:500].strip() if observation else 'None'}")

        if auto_grant_budget == 0 and turns_left == 0 and tool_name != "none":
            logger.warning(f"[{run_id}] [{agent_name}] Tool call '{tool_name}' blocked on final turn to force conclusion")
            observation = (
                f"CRITICAL: Maximum tool calls reached for this stage. Tool execution '{tool_name}' is FORBIDDEN.\n"
                f"You already have all necessary context in your previous steps. You MUST output your {done_token} now."
            )
        elif tool_name == "none":
            observation = (
                "ERROR: No valid ACTION detected.\n"
                f"Response started with: {raw_reply[:120]!r}\n\n"
                "You must format your action in plain text:\n"
                "THOUGHT: ...\nACTION: run_bash_command(\"cmd\")\n__END__\n"
                "No XML tags (<tool_call>), no JSON."
            )

        messages.append(build_tool_result_message(tool_name, observation, turns_left))
        i += 1

    # ── Autonomous Turn Extension ─────────────────────────────────────
    if auto_grant_budget > 0:
        print(f"\n🤖 [SUPERVISOR]: Auto-diagnosing turn limit for {agent_name}...")
        decision = generate_supervisor_turn_nudge(messages, agent_name, model=model)
        status = decision["status"]
        nudge = decision["directive"]
        turns_to_grant = decision["granted_turns"]
        print(f"🤖 [SUPERVISOR {status}]: (Granting {turns_to_grant} turns) {nudge}")
        logger.info(f"[{run_id}] [{agent_name}] Turn limit reached ({max_iters}). Supervisor [{status}] granting {turns_to_grant} turns (budget left: {auto_grant_budget-1}). Directive: {nudge}")

        if status == "READY":
            injected = (
                f"⚠️ [SUPERVISOR DIRECTIVE — FINAL TURN]:\n"
                f"{nudge}\n\n"
                f"[SYSTEM: You already have all necessary context. 1 turn granted. "
                f"Tool calls are strictly FORBIDDEN. Output your {done_token} immediately.]"
            )
        else:
            injected = (
                f"⚠️ [SUPERVISOR DIRECTIVE — ITERATION EXTENSION]:\n"
                f"{nudge}\n\n"
                f"[SYSTEM: {turns_to_grant} more turns granted. Follow this directive and conclude immediately.]"
            )
        messages.append({"role": "user", "content": injected})

        return run_agent_loop(
            run_id=run_id,
            messages=messages,
            parse_and_execute=parse_and_execute,
            sandbox=sandbox,
            max_iters=turns_to_grant,
            done_token=done_token,
            agent_name=agent_name,
            on_done=on_done,
            repograph_id=repograph_id,
            model=model,
            loop=loop,
            cancel_flag=cancel_flag,
            env=env,
            is_complex=is_complex,
            auto_grant_budget=auto_grant_budget - 1,
            auto_extra_turns=auto_extra_turns,
        )

    # ── Human Escalation Timeout ──────────────────────────────────────
    print(f"\n🛑 {agent_name} reached max iterations ({max_iters}) and autonomous budget exhausted.")
    logger.warning(f"[{run_id}] [{agent_name}] Max iterations ({max_iters}) and autonomous budget exhausted. Pausing for human turn grant.")

    # Signal the orchestrator/frontend that we're waiting for a turn grant
    _set_status_sync(run_id, RunStatus.AWAITING_MORE_TURNS, loop)

    from turn_events import wait_for_grant
    grant = wait_for_grant(run_id, timeout=3600.0)

    if grant is None:
        # Timed out waiting — treat as takeover
        return "TIMEOUT"

    # Build the message to inject based on what the user sent
    if grant.feedback:
        injected = (
            f"User has provided feedback.\nFeedback: {grant.feedback}\n\n"
            f"[SYSTEM: {grant.extra_turns} more turns granted. "
            f"Continue according to the feedback.]"
        )
    else:
        injected = (
            f"[SYSTEM: {grant.extra_turns} more turns granted. "
            f"Resume exactly where you left off.]"
        )

    messages.append({"role": "user", "content": injected})

    # Recurse with the granted budget
    return run_agent_loop(
        run_id=run_id,
        messages=messages,
        parse_and_execute=parse_and_execute,
        sandbox=sandbox,
        max_iters=grant.extra_turns,
        done_token=done_token,
        agent_name=agent_name,
        on_done=on_done,
        repograph_id=repograph_id,
        model=model,
        loop=loop,
        cancel_flag=cancel_flag,
        env=env,
        is_complex=is_complex,
        auto_grant_budget=0,
        auto_extra_turns=auto_extra_turns,
    )

def extract_test_hint(architect_plan: str) -> str:
    if "TEST_HINT:" not in architect_plan:
        return ""
    return architect_plan.split("TEST_HINT:")[-1].strip()


def run_agent_loop_arch(
    run_id  : str,
    messages: list,
    parse_and_execute,       # fn(reply, sandbox) -> (tool_name, observation)
    sandbox,
    max_iters: int,
    done_token: str,         # e.g. "FINAL_RESULT:" or "FINAL_PLAN:"
    agent_name: str,
    on_done,  
    repograph_id,               # fn(raw_reply) -> str | None  (None = keep looping)
    loop: asyncio.AbstractEventLoop, 
    model: str,       # optional env dict for supervisor reminders
    cancel_flag: threading.Event,
    env: dict = None, 
    auto_grant_budget: int = 2,
    auto_extra_turns: int = 5,
) -> str:
    """
    Generic agent loop shared by all agents.
    Handles: streaming, stuck detection, supervisor, compression,
             failure tracking, turn warnings, and continuation.
    """
    reply_history = []
    action_log    = []
    i = 0

    while i < max_iters:
        print(f"\n--- {agent_name} Iteration {i+1}/{max_iters} ---")
        logger.info(f"[{run_id}] [{agent_name}] --- Turn {i+1}/{max_iters} --- (context: {len(messages)} messages)")

        messages_to_send = prune_old_tool_observations(messages, keep_recent=4)
        if len(messages_to_send) != len(messages):
            logger.debug(f"[{run_id}] [{agent_name}] Pruned messages for context window ({len(messages_to_send)} sent)")

        raw_reply = ""
        for chunk in call_llm(messages=messages_to_send, model=model, temperature=0.2):
            if cancel_flag.is_set():
                logger.info(f"[{run_id}] [{agent_name}] Cancel flag set — aborting loop")
                break

            raw_reply += chunk
            
            publish_token(run_id, chunk, loop)
        publish_token(run_id, '__NEWLINE__', loop)
        if cancel_flag.is_set():
            break
        if not raw_reply:
            logger.warning(f"[{run_id}] [{agent_name}] [Turn {i+1}] Empty LLM response received")
            messages.append({"role": "user", "content": "Empty response. Please continue."})
            i += 1
            continue

        # ── Identical reply loop detection ────────────────────────────
        reply_hash = get_reply_hash(raw_reply)
        reply_history.append(reply_hash)

        consecutive_identical = 0
        for h in reversed(reply_history):
            if h == reply_hash:
                consecutive_identical += 1
            else:
                break

        if consecutive_identical >= 2:
            print(f"🔁 Identical loop ({consecutive_identical}x). Recovering.")
            logger.warning(f"[{run_id}] [{agent_name}] [Turn {i+1}] Identical loop detected ({consecutive_identical}x)")
            messages.append({
                "role": "user",
                "content": STUCK_LOOP_INJECTION.format(n=consecutive_identical)
            })
            full = ""
            for chunk in call_llm(messages, model=model, temperature=0.7):
                full += chunk
            raw_reply = full.strip()
            reply_history.clear()

        agent_reply = extract_action_string(raw_reply)
        if agent_reply != raw_reply:
            print("⚠️ JSON normalized.")

        messages.append({"role": "assistant", "content": raw_reply})
        action_log.append(extract_action_line(raw_reply))

        # ── Done check ────────────────────────────────────────────────
        if done_token in agent_reply:
            logger.info(f"[{run_id}] [{agent_name}] [Turn {i+1}] Done token '{done_token}' detected")
            result = on_done(raw_reply, messages)
            if result is not None:
                logger.info(f"[{run_id}] [{agent_name}] Done check accepted result at turn {i+1}")
                return result
            # on_done returned None → keep looping (e.g. retry after rejection)
            logger.info(f"[{run_id}] [{agent_name}] on_done rejected result — continuing loop")
            i += 1
            continue

        # ── Tool execution ────────────────────────────────────────────
        turns_left = max_iters - (i + 1)
        tool_name, observation = parse_and_execute(agent_reply, sandbox, repograph_id)
        print(f"\n[{tool_name}]: {observation[:300]}...")
        logger.info(f"[{run_id}] [{agent_name}] [Turn {i+1}] Tool: {tool_name} | Obs length: {len(observation)} chars | Snippet: {observation[:100].strip() if observation else 'None'}")

        if auto_grant_budget == 0 and turns_left == 0 and tool_name != "none":
            logger.warning(f"[{run_id}] [{agent_name}] Tool call '{tool_name}' blocked on final turn to force conclusion")
            observation = (
                f"CRITICAL: Maximum tool calls reached for this stage. Tool execution '{tool_name}' is FORBIDDEN.\n"
                f"You already have all necessary context in your previous steps. You MUST output your {done_token} now."
            )
        elif tool_name == "none":
            observation = (
                "ERROR: No valid ACTION detected.\n"
                f"Response started with: {raw_reply[:120]!r}\n\n"
                "THOUGHT: ...\nACTION: run_bash_command(\"cmd\")\n__END__\n"
                "No JSON. Plain text only."
            )

        messages.append(build_tool_result_message(tool_name, observation, turns_left))
        i += 1

    # ── Autonomous Turn Extension ─────────────────────────────────────
    if auto_grant_budget > 0:
        print(f"\n🤖 [SUPERVISOR]: Auto-diagnosing turn limit for {agent_name}...")
        decision = generate_supervisor_turn_nudge(messages, agent_name, model=model)
        status = decision["status"]
        nudge = decision["directive"]
        turns_to_grant = decision["granted_turns"]
        print(f"🤖 [SUPERVISOR {status}]: (Granting {turns_to_grant} turns) {nudge}")
        logger.info(f"[{run_id}] [{agent_name}] Turn limit reached ({max_iters}). Supervisor [{status}] granting {turns_to_grant} turns (budget left: {auto_grant_budget-1}). Directive: {nudge}")

        if status == "READY":
            injected = (
                f"⚠️ [SUPERVISOR DIRECTIVE — FINAL TURN]:\n"
                f"{nudge}\n\n"
                f"[SYSTEM: You already have all necessary context. 1 turn granted. "
                f"Tool calls are strictly FORBIDDEN. Output your {done_token} immediately.]"
            )
        else:
            injected = (
                f"⚠️ [SUPERVISOR DIRECTIVE — ITERATION EXTENSION]:\n"
                f"{nudge}\n\n"
                f"[SYSTEM: {turns_to_grant} more turns granted. Follow this directive and conclude immediately.]"
            )
        messages.append({"role": "user", "content": injected})

        return run_agent_loop_arch(
            run_id=run_id,
            messages=messages,
            parse_and_execute=parse_and_execute,
            sandbox=sandbox,
            max_iters=turns_to_grant,
            done_token=done_token,
            agent_name=agent_name,
            on_done=on_done,
            repograph_id=repograph_id,
            model=model,
            loop=loop,
            cancel_flag=cancel_flag,
            env=env,
            auto_grant_budget=auto_grant_budget - 1,
            auto_extra_turns=auto_extra_turns,
        )

    # ── Human Escalation Timeout ──────────────────────────────────────
    print(f"\n🛑 {agent_name} reached max iterations ({max_iters}) and autonomous budget exhausted.")
    logger.warning(f"[{run_id}] [{agent_name}] Max iterations ({max_iters}) and autonomous budget exhausted. Pausing for human turn grant.")

    # Signal the orchestrator/frontend that we're waiting for a turn grant
    _set_status_sync(run_id, RunStatus.AWAITING_MORE_TURNS, loop)

    from turn_events import wait_for_grant
    grant = wait_for_grant(run_id, timeout=3600.0)

    if grant is None:
        # Timed out waiting — treat as takeover
        return "TIMEOUT"

    # Build the message to inject based on what the user sent
    if grant.feedback:
        injected = (
            f"User has provided feedback.\nFeedback: {grant.feedback}\n\n"
            f"[SYSTEM: {grant.extra_turns} more turns granted. "
            f"Continue according to the feedback.]"
        )
    else:
        injected = (
            f"[SYSTEM: {grant.extra_turns} more turns granted. "
            f"Resume exactly where you left off.]"
        )

    messages.append({"role": "user", "content": injected})

    # Recurse with the granted budget
    return run_agent_loop_arch(
        run_id=run_id,
        messages=messages,
        parse_and_execute=parse_and_execute,
        sandbox=sandbox,
        max_iters=grant.extra_turns,
        done_token=done_token,
        agent_name=agent_name,
        on_done=on_done,
        repograph_id=repograph_id,
        model=model,
        loop=loop,
        cancel_flag=cancel_flag,
        env=env,
        auto_grant_budget=0,
        auto_extra_turns=auto_extra_turns,
    )