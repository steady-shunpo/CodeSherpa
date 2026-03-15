import re
import os
from google import genai
from google.genai import types
from tools import setup_developer_environment, run_remote_command, read_remote_file, edit_remote_file, write_remote_file
from dotenv import load_dotenv
import time
load_dotenv()
# Assuming you have setup_developer_environment and your E2B tools (read_remote_file, etc.) defined above
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_DEV"))

DEVELOPER_SYSTEM_PROMPT="""Role: Senior AI Software Engineer

Objective: You receive a Technical Design Document (Plan) from the Architect. Your job is to implement this plan precisely in the provided secure sandbox, verify it with tests, and ensure syntax remains perfectly intact.

Available Tools:
1. read_file("filepath", start_line, end_line): Reads lines from a file in the sandbox. (System automatically adds surrounding context padding).
2. edit_file("filepath", start_line, end_line): Replaces the specified lines with new code. 
3. write_file("filepath"): Creates a new file (used primarily for creating your testing/reproduction scripts).
4. run_bash_command("command"): Runs a terminal command (e.g., pytest, python script.py) inside the sandbox.DO NOT use triple pipes for this action. Keep it on one line.

CRITICAL SAFETY RULES:
* The "Test-First" Mandate: Before you modify any source code, you MUST use `write_file` to create a reproduction script, and `run_bash_command` to run it and prove the bug exists. Only use `edit_file` AFTER you have observed the failure.
* The "Read First" Mandate: You MUST NOT use `edit_file` without first using `read_file` to see the exact current state of the code.
* Bracket Safety: When replacing code, ensure you replace the entire logical block to avoid dangling parentheses or broken indentation.

Format for Writing/Editing Code:
To use the code tools, you must format your action EXACTLY like this:

ACTION: edit_file("filepath", start_line, end_line)
|||
your_new_code_here
|||

OR

ACTION: write_file("filepath")
|||
your_complete_file_content_here
|||

Strict Protocol:
You must strictly follow this exact format for every turn:
THOUGHT: Explain what part of the Architect's plan you are executing.
ACTION: Your tool call.
(Wait for OBSERVATION from the system)

When the code is successfully edited and all tests pass, output:
FINAL_RESULT:
STATUS: SUCCESS
CHANGES_MADE: [Briefly list the files and logic you changed]
"""


def run_developer_agent(architect_plan, repo_url, max_iterations=10):
    print("\n" + "="*50)
    print("🛠️  STARTING DEVELOPER AGENT")
    print("="*50)

    # 1. Spin up the E2B Sandbox and clone the repo
    sandbox = setup_developer_environment(repo_url)

    # 2. Prepare the System Prompt with the Architect's Plan
    # We tweak the edit instruction slightly for reliable regex parsing
    developer_instructions = f"""
                    {DEVELOPER_SYSTEM_PROMPT}

                    Here is the ARCHITECT'S PLAN you must execute:
                    {architect_plan}
                    """
    
    contents = [
    types.Content(role="user", parts=[types.Part.from_text(text=developer_instructions)]),
    types.Content(role="model", parts=[types.Part.from_text(text="Understood. I will follow the plan and rules strictly. Waiting for the signal to begin.")]),
    types.Content(role="user", parts=[types.Part.from_text(text="Begin execution. Start by writing or running a failing test, or reading the required files.")])
    ]

    for i in range(max_iterations):
        print(f"\n--- 🤖 Developer Iteration {i+1} ---")
        
        # 1. Call Gemini
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
        ) 
        agent_reply = response.text
        print(agent_reply)
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=agent_reply)]))

        # 2. Check for Final Result
        if "FINAL_RESULT:" in agent_reply:
            print("\n✅ Developer has completed the task!")
            sandbox.kill() # Always clean up your cloud resources
            return agent_reply

        # 3. Parse Tool Usage
        observation = ""
        
        # Parse Read
        # 3. Parse Tool Usage (Updated Regex for Triple Pipes)
        read_match = re.search(r'ACTION:\s*read_file\("([^"]+)",\s*(\d+),\s*(\d+)\)', agent_reply)
        bash_match = re.search(r'ACTION:\s*run_bash_command\("([^"]+)"\)', agent_reply)
        edit_match = re.search(r'ACTION:\s*edit_file\("([^"]+)",\s*(\d+),\s*(\d+)\)\s*\|\|\|\n(.*?)\|\|\|', agent_reply, re.DOTALL)
        write_match = re.search(r'ACTION:\s*write_file\("([^"]+)"\)\s*\|\|\|\n(.*?)\|\|\|', agent_reply, re.DOTALL)

        observation = ""

        # 4. Execute Tools (Perfectly linked if/elif/else chain)
        if read_match:
            filepath = read_match.group(1)
            start, end = int(read_match.group(2)), int(read_match.group(3))
            print(f"🔧 Tool: Reading {filepath} ({start}-{end})")
            observation = read_remote_file(sandbox, filepath, start, end)
            
        elif bash_match:
            command = bash_match.group(1)
            print(f"🔧 Tool: Running bash '{command}'")
            full_command = f"cd /workspace/repo && {command}"
            observation = run_remote_command(sandbox, full_command)
            
        elif edit_match:
            filepath = edit_match.group(1)
            start, end = int(edit_match.group(2)), int(edit_match.group(3))
            new_code = edit_match.group(4)
            print(f"🔧 Tool: Editing {filepath} ({start}-{end})")
            observation = edit_remote_file(sandbox, filepath, start, end, new_code)
            
        elif write_match:
            filepath = write_match.group(1)
            new_code = write_match.group(2)
            print(f"🔧 Tool: Writing new file to {filepath}")
            observation = write_remote_file(sandbox, filepath, new_code)
            
        else:
            print("⚠️ No valid ACTION found. Forcing retry.")
            observation = "ERROR: No valid ACTION format detected. Please review the protocol and try again."

        # 5. Time Awareness (Calculated perfectly at the end of the loop)
        turns_left = max_iterations - (i + 1)
        
        if turns_left > 1:
            time_warning = f"\n\n[SYSTEM NOTE: You have {turns_left} iterations remaining before timeout.]"
        elif turns_left == 1:
            time_warning = f"\n\n[CRITICAL SYSTEM NOTE: THIS IS YOUR FINAL TURN. You MUST output a FINAL_RESULT now. Do not use any more tools.]"
        else:
            time_warning = ""

        # 6. Feed Observation back
        print(f"\n[Observation Snippet]: {observation[:150]}...")
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=f"OBSERVATION:\n{observation}{time_warning}")]))
        time.sleep(57)
    print("\n🛑 Developer Agent timed out.")
    sandbox.kill()
    return "TIMEOUT"



res = run_developer_agent(r"""The bug is caused by `HTMLTagRemover.handle_data` stripping all whitespace from text nodes, leading to words merging incorrectly around inline HTML tags. The fix involves preventing this immediate stripping and instead normalizing all whitespace *after* the HTML has been parsed and the content collected.

**File:** `openlibrary\plugins\upstream\utils.py`

**Change 1: Add `import re`**

*   **Location:** At the top of the file, after existing imports if any, or as the first import.
*   **Original Code (Conceptual):**
    ```python
    # ... existing imports ...
    ```
*   **New Code:**
    ```python
    # ... existing imports ...
    import re
    ```

**Change 2: Modify `HTMLTagRemover.handle_data`**

*   **Location:** Line 1572
*   **Original Code:**
    ```python
    1571 |     def handle_data(self, data):
    1572 |         self.data.append(data.strip())
    ```
*   **New Code:**
    ```python
    1571 |     def handle_data(self, data):
    1572 |         self.data.append(data)
    ```

**Change 3: Modify `reformat_html` to normalize whitespace**

*   **Location:** Lines 1599-1601
*   **Original Code:**
    ```python
    1593 |     parser = HTMLTagRemover()
    1594 |     # Must have a root node, otherwise the parser will fail
    1595 |     parser.feed(f'<div>{html_str}</div>')
    1596 |     content = [web.websafe(s) for s in parser.data if s]
    1597 |
    1598 |     if max_length:
    1599 |         return truncate(''.join(content), max_length).strip().replace('\n', '<br>')
    1600 |     else:
    1601 |         return ''.join(content).strip().replace('\n', '<br>')
    ```
*   **New Code:**
    ```python
    1593 |     parser = HTMLTagRemover()
    1594 |     # Must have a root node, otherwise the parser will fail
    1595 |     parser.feed(f'<div>{html_str}</div>')
    1596 |     content = [web.websafe(s) for s in parser.data if s]
    1597 |
    1598 |     # Join content and normalize all whitespace (including newlines from handle_endtag)
    1599 |     joined_and_normalized_content = re.sub(r'\s+', ' ', ''.join(content)).strip()
    1600 |
    1601 |     if max_length:
    1602 |         # Apply truncate to the already normalized content
    1603 |         return truncate(joined_and_normalized_content, max_length).replace('\n', '<br>')
    1604 |     else:
    1605 |         # Return the normalized content
    1606 |         return joined_and_normalized_content.replace('\n', '<br>')
    ```""", "https://github.com/internetarchive/openlibrary.git")