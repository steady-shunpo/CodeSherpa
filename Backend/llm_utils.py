import re
import json
import time
import hashlib
import httpx
from config import client, MODEL, SUPERVISOR_SYSTEM_PROMPT, STUCK_LOOP_INJECTION
from sandbox_utils import TOOL_PATTERNS


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
        ("edit",      re.compile(r'(?:ACTION:\s*)?edit_file\(\s*"[^"]+"\s*,\s*-?\d+\s*,\s*-?\d+\s*\)\s*(?:\|\|\||```(?:\w+)?)\n.*?(?:\|\|\||```)', re.DOTALL)),
        ("write",     re.compile(r'(?:ACTION:\s*)?write_file\(\s*"[^"]+"\s*\)\s*(?:\|\|\||```(?:\w+)?)\n.*?(?:\|\|\||```)', re.DOTALL)),
        # ("read_bulk", re.compile(r'(?:ACTION:\s*)?read_files_bulk\(\s*\[.*?\]\s*\)', re.DOTALL)),
        ("read",      re.compile(r'(?:ACTION:\s*)?read_file\(\s*"[^"]+"\s*,\s*-?\d+\s*,\s*-?\d+\s*\)')),
        ("bash",      re.compile(r'(?:ACTION:\s*)?run_bash_command\(\s*"(?:[^"\\]|\\.)*"\s*\)')),
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
            )

            full_reply           = ""
            chunk_count          = 0
            repeat_counter       = 0
            think_repeat_counter = 0
            REPEAT_WINDOW        = 200
            REPEAT_THRESHOLD     = 3
            THINK_WINDOW         = 150
            MAX_CHARS            = 6000
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

# def _was_stuck(reply_history: list) -> bool:
#     """
#     Returns True if the last few replies were identical,
#     indicating the agent was looping rather than making progress.
#     """
#     if len(reply_history) < 3:
#         return False
#     return len(set(reply_history[-3:])) == 1


def run_agent_loop(
    messages: list,
    parse_and_execute,       # fn(reply, sandbox) -> (tool_name, observation)
    sandbox,
    max_iters: int,
    done_token: str,         # e.g. "FINAL_RESULT:" or "FINAL_PLAN:"
    agent_name: str,
    on_done,                 # fn(raw_reply) -> str | None  (None = keep looping)
    model: str,       # optional env dict for supervisor reminders
    env: dict = None, 
    is_complex: bool = None
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

        if should_compress(messages):
            before = len(messages)
            messages[:] = compress_old_messages(messages, keep_recent=6)
            print(f"🗜️ Compressed: {before} → {len(messages)} messages")

        # Inject failure warning without saving to history
        failure_warning = build_failure_warning(messages)
        messages_to_send = messages.copy()
        if failure_warning:
            messages_to_send.append({"role": "user", "content": failure_warning})

        raw_reply = ""
        for chunk in call_llm(messages_to_send, model=model, temperature=0.2):
            raw_reply += chunk
        if not raw_reply:
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
                # env_reminder = ""
                # if env:
                #     env_reminder = (
                #         f"\nEnvironment reminder: "
                #         f"use '{env.get('python_bin', 'python3')}', "
                #         f"run tests with '{env.get('test_command', 'pytest')}'"
                #     )
                messages.append({
                    "role": "user",
                    "content": (
                        # f"Repository infra is complex: {is_complex}"
                        f"[SUPERVISOR]: Stuck — {verdict['reason']}\n"
                        f"{verdict.get('intervention', '')}\n"
                        f"FORBIDDEN this turn: read_file, search_file\n"
                        f"REQUIRED this turn: write_file, edit_file, or run_bash_command"
                        # f"{env_reminder}"
                    )
                })
                raw_reply = ""
                for chunk in call_llm(messages, model, temperature=0.6):
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
            result = on_done(raw_reply, messages)
            if result is not None:
                return result
            # on_done returned None → keep looping (e.g. retry after rejection)
            i += 1
            continue

        # ── Tool execution ────────────────────────────────────────────
        turns_left = max_iters - (i + 1)
        tool_name, observation = parse_and_execute(agent_reply, sandbox, env.get("pythonpath"), env.get("pytestflags"))
        print(f"\n[{tool_name}]: {observation}")

        if tool_name == "none":
            # observation = (
            #     "ERROR: No valid ACTION detected.\n"
            #     # f"Response started with: {raw_reply[:120]!r}\n\n"
            #     # "THOUGHT: ...\nACTION: run_bash_command(\"cmd\")\n__END__\n"
            #     "No JSON. Plain text only."
            # )
            observation=""

        messages.append(build_tool_result_message(tool_name, observation, turns_left))
        i += 1

    # ── Timeout ───────────────────────────────────────────────────────
    print(f"\n🛑 {agent_name} reached max iterations.")
    print("What would you like to do?")
    print("  [n] Provide feedback and continue(+10 turns)")
    print("  [t] Takeover (Halt automation / Launch Co-Pilot later)")
    ans = input().strip().lower()

    if ans == "n":
        # messages.append({
        #     "role": "user",
        #     "content": "[SYSTEM: 10 more turns granted. Resume exactly where you left off.]"
        # })
        # print(messages[-5:])
        feedback = input("Feedback: ")
        messages.append({
                "role": "user",
                "content": (
                    f"User has provided feedback.\nFeedback: {feedback}\n\n"
                    "[SYSTEM: 10 more turns granted. Continue according to the feedback.]"
                ) if feedback else(
                    "[SYSTEM: 10 more turns granted. Resume exactly where you left off.]"
                )
            })
        return run_agent_loop(
            messages, parse_and_execute, sandbox,
            10, done_token, agent_name, on_done, model, env
        )
    elif ans == "t":
        # failure_reason = "stuck_loop" if _was_stuck(reply_history) else "max_iterations"
        # explanation = summarize_failure(messages, model, agent_name, True)
        # print(f"\n📋 Agent explanation:\n{explanation}")
        # Return both the trigger signal and the explanation
        return f"TAKEOVER"
    
    return "TIMEOUT"

def extract_test_hint(architect_plan: str) -> str:
    if "TEST_HINT:" not in architect_plan:
        return ""
    return architect_plan.split("TEST_HINT:")[-1].strip()


def run_agent_loop_arch(
    messages: list,
    parse_and_execute,       # fn(reply, sandbox) -> (tool_name, observation)
    sandbox,
    max_iters: int,
    done_token: str,         # e.g. "FINAL_RESULT:" or "FINAL_PLAN:"
    agent_name: str,
    on_done,                 # fn(raw_reply) -> str | None  (None = keep looping)
    model: str,       # optional env dict for supervisor reminders
    env: dict = None, 
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

        # if should_compress(messages):
        #     before = len(messages)
        #     messages[:] = compress_old_messages(messages, keep_recent=6)
        #     print(f"🗜️ Compressed: {before} → {len(messages)} messages")

        # Inject failure warning without saving to history
        # failure_warning = build_failure_warning(messages)
        # messages_to_send = messages.copy()
        # if failure_warning:
        #     messages_to_send.append({"role": "user", "content": failure_warning})
        raw_reply = ""
        for chunk in call_llm(messages=messages, model=model, temperature=0.2):
            raw_reply += chunk
        if not raw_reply:
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
            messages.append({
                "role": "user",
                "content": STUCK_LOOP_INJECTION.format(n=consecutive_identical)
            })
            full = ""
            for chunk in call_llm(messages, model=model, temperature=0.7):
                full += chunk
            raw_reply = full.strip()
            reply_history.clear()

        # ── Supervisor check every 3 turns ────────────────────────────
        # elif i > 0 and i % 3 == 0 and len(action_log) >= 3:
        #     print("👁️ Supervisor check...")
        #     verdict = run_supervisor(action_log[-4:])
        #     if verdict.get("stuck"):
        #         print(f"🚨 Supervisor: {verdict['reason']}")
        #         env_reminder = ""
        #         if env:
        #             env_reminder = (
        #                 f"\nEnvironment reminder: "
        #                 f"use '{env.get('python_bin', 'python3')}', "
        #                 f"run tests with '{env.get('test_command', 'pytest')}'"
        #             )
        #         messages.append({
        #             "role": "user",
        #             "content": (
        #                 f"[SUPERVISOR]: Stuck — {verdict['reason']}\n"
        #                 f"{verdict.get('intervention', '')}\n"
        #                 f"FORBIDDEN this turn: read_file, search_file\n"
        #                 f"REQUIRED this turn: write_file, edit_file, or run_bash_command"
        #                 f"{env_reminder}"
        #             )
        #         })
        #         raw_reply = call_llm(messages, model, temperature=0.6)
        #         reply_history.clear()
        #         action_log.clear()

        agent_reply = extract_action_string(raw_reply)
        if agent_reply != raw_reply:
            print("⚠️ JSON normalized.")

        messages.append({"role": "assistant", "content": raw_reply})
        action_log.append(extract_action_line(raw_reply))

        # ── Done check ────────────────────────────────────────────────
        if done_token in agent_reply:
            result = on_done(raw_reply, messages)
            if result is not None:
                return result
            # on_done returned None → keep looping (e.g. retry after rejection)
            i += 1
            continue

        # ── Tool execution ────────────────────────────────────────────
        turns_left = max_iters - (i + 1)
        tool_name, observation = parse_and_execute(agent_reply, sandbox)
        print(f"\n[{tool_name}]: {observation[:300]}...")

        if tool_name == "none":
            observation = (
                "ERROR: No valid ACTION detected.\n"
                f"Response started with: {raw_reply[:120]!r}\n\n"
                "THOUGHT: ...\nACTION: run_bash_command(\"cmd\")\n__END__\n"
                "No JSON. Plain text only."
            )

        messages.append(build_tool_result_message(tool_name, observation, turns_left))
        i += 1

    # ── Timeout ───────────────────────────────────────────────────────
    print(f"\n🛑 {agent_name} reached max iterations.")
    print("What would you like to do?")
    print("  [n] Provide feedback and continue(+10 turns)")
    print("  [t] Takeover (Halt automation / Launch Co-Pilot later)")
    ans = input().strip().lower()

    if ans == "n":
        feedback = input("Feedback: ")
        messages.append({
                "role": "user",
                "content": (
                    f"User has provided feedback.\nFeedback: {feedback}\n\n"
                    "[SYSTEM: 10 more turns granted. Continue according to the feedback.]"
                ) if feedback else(
                    "[SYSTEM: 10 more turns granted. Resume exactly where you left off.]"
                )
            })
        return run_agent_loop_arch(
            messages, parse_and_execute, sandbox, 
            10, done_token, agent_name, on_done, model, env
        )
    
    elif ans == "t":
        # failure_reason = "stuck_loop" if _was_stuck(reply_history) else "max_iterations"
        # explanation = summarize_failure(messages, model, 'architect', False)
        # print(f"\n📋 Agent explanation:\n{explanation}")
        # Return both the trigger signal and the explanation
        return f"TAKEOVER"
    
    return "TIMEOUT"