import re
import os
from google import genai
from google.genai import types
from tools import setup_developer_environment, run_remote_command, read_remote_file, edit_remote_file, write_remote_file, checkpoint_gate
from dotenv import load_dotenv
import time
from openai import OpenAI
load_dotenv()
# Assuming you have setup_developer_environment and your E2B tools (read_remote_file, etc.) defined above
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_DEV"))
client2 = OpenAI(
    api_key=os.environ.get("OPENROUTER_ARCH_KEY"),
    base_url="https://openrouter.ai/api/v1",
)

DEV_SYSTEM_PROMPT="""
Role: Senior AI Software Engineer

Objective: Implement a Technical Design Document perfectly in a secure sandbox, verify it with tests, and ensure syntax remains intact.

AVAILABLE COMMANDS (USE AS PLAIN TEXT ONLY):
1. read_file("filepath", start_line, end_line): Reads lines from a file.
2. edit_file("filepath", start_line, end_line): Replaces the specified lines with new code. 
3. write_file("filepath"): Creates a new file (primarily for testing scripts).
4. run_bash_command("command"): Runs a terminal command. Keep on one line. Do not use the ||| delimiter for this.

CRITICAL CONSTRAINTS (OBEY STRICTLY):
1. NO NATIVE FUNCTION CALLING: Do not output JSON tool calls. Use the exact plain text formats below.
2. NO MARKDOWN CODE BLOCKS: Never use ```python or ```bash. When writing or editing code, strictly use the ||| delimiters.
3. TEST-FIRST MANDATE: You MUST write_file a reproduction script and run_bash_command to prove the bug exists BEFORE editing source code.
4. READ-FIRST MANDATE: You MUST read_file to see the exact current state before you edit_file.
5. BRACKET SAFETY: When replacing code, replace the entire logical block (e.g., the whole function) to avoid indentation or bracket errors.
6. PAUSE AFTER ACTION: You must output "PAUSE" and stop generating immediately after your command or code block. Do not hallucinate the OBSERVATION.

THE TDD LOOP (STRICT SEQUENCE):
   - Step 1 (Prove): Use write_file to create a test script. Use run_bash_command to watch it FAIL.
   - Step 2 (Fix): Use read_file and edit_file to modify the source code.
   - Step 3 (Verify): You MUST run_bash_command on the exact same test script again. You cannot finish until it PASSES (exit code 0).

THE "NO GUESSING" MANDATE:
   You are strictly forbidden from outputting FINAL_RESULT: STATUS: SUCCESS unless your very last OBSERVATION was a successful test run proving the bug is fixed.

REQUIRED OUTPUT FORMATS:
You must strictly choose ONE of the three formats below per turn. Do not add conversational text.

Format 1: Standard Command (Reading files or running bash)
THOUGHT: [Explain what part of the plan you are executing]
ACTION: [e.g., read_file("main.py", 1, 50) OR run_bash_command("pytest test_bug.py")]
PAUSE

Format 2: Code Modification (Writing or editing files)
THOUGHT: [Explain exactly what logic you are changing and why]
ACTION: [e.g., edit_file("main.py", 10, 25) OR write_file("test_bug.py")]
|||
[Insert exactly the raw code here. Do not wrap in markdown]
|||
PAUSE

Format 3: Task Complete (Use ONLY when the fix is verified)
THOUGHT: [Confirm the reproduction script now passes and the plan is fully executed]
FINAL_RESULT:
STATUS: SUCCESS
CHANGES_MADE: [Briefly list the files and logic you changed]
"""


def run_developer_agent(architect_plan, repo_url, max_iterations=30):
    print("\n" + "="*50)
    print("🛠️  STARTING DEVELOPER AGENT")
    print("="*50)

    # 1. Spin up the E2B Sandbox and clone the repo
    sandbox = setup_developer_environment(repo_url)

    # 2. Prepare the System Prompt with the Architect's Plan
    # We tweak the edit instruction slightly for reliable regex parsing
    developer_instructions = f"""
                    {DEV_SYSTEM_PROMPT}

                    Here is the ARCHITECT'S PLAN you must execute:
                    {architect_plan}
                    """
    
    contents = [
    types.Content(role="user", parts=[types.Part.from_text(text=developer_instructions)]),
    types.Content(role="model", parts=[types.Part.from_text(text="Understood. I will follow the plan and rules strictly. Waiting for the signal to begin.")]),
    types.Content(role="user", parts=[types.Part.from_text(text="Begin execution. Start by writing or running a failing test, or reading the required files.")])
    ]
    messages = [
        {"role": "user","content":developer_instructions},
        {"role": "system","content": "Understood. I will follow the rules strictly. Waiting for the signal to begin."},
        {"role": "user","content": f"Begin execution. Start by writing or running a failing test, or reading the required files."},

    ]

    i = 0
    while i < max_iterations:
        print(f"\n--- 🤖 Developer Iteration {i+1} ---")
        
        # 1. Call Gemini
        response = client2.chat.completions.create(
            model="arcee-ai/trinity-large-preview:free", # Or whichever model version you are using
            messages=messages
        ) 
        agent_reply =response.choices[0].message.content
        print("Agent Reply: ",agent_reply)
        messages.append({"role":"system", "content":agent_reply})

        # 2. Check for Final Result
        if "FINAL_RESULT:" in agent_reply:
            print("\n✅ Developer has completed the task!")
            diff_command = "cd workspace/repo && git diff"
            git_diff_output = run_remote_command(sandbox, diff_command)
            
            if not git_diff_output.strip():
                git_diff_output = "(No changes detected in git diff. The developer may not have saved files or ran the wrong commands.)"
                
            decision = checkpoint_gate("Developer", git_diff_output)
            if decision["status"] == "PROCEED":
            # You approved it! Return the diff so it can go to the Reviewer 
            # or directly to the patch extraction function.
                sandbox.kill()
                return git_diff_output
                
            elif decision["status"] == "RETRY":
                # Append the "success" message so the model's history is complete
                messages.append({"role": "assistant", "content": "FINAL_RESULT:\nSTATUS: SUCCESS"})
                
                # Feed the human rejection back into the loop
                retry_prompt = f"Your changes were rejected at the checkpoint. Fix the code based on this feedback:\n{decision['feedback']}\n\nRemember to use read_file to check the current state before editing."
                messages.append({"role": "user", "content": retry_prompt})
                
                print("🔄 Restarting Developer with new feedback...")
                max_iterations+=7
                continue # This kicks it back to the top of the while True loop
                
            elif decision["status"] == "TAKEOVER":
                sandbox.kill()
                return "TAKEOVER_TRIGGERED"
        # ---------------------------------------------------------
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
            full_command = f"cd workspace/repo && {command}"
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
        messages.append({"role":"user", "content": f"OBSERVATION:\n{observation}{time_warning}"})
        i += 1
        time.sleep(57)
    print("\n🛑 Developer Agent timed out.")
    sandbox.kill()
    return "TIMEOUT"



res = run_developer_agent(r"""Here's the step-by-step implementation plan:

1. Create ReleaseModelAssembler class:
   - Path: rest/resource-server/src/main/java/org/eclipse/sw360/rest/resourceserver/release/ReleaseModelAssembler.java
   - Extract createHalReleaseResource and createHalReleaseResourceWithAllDetails methods
   - Move all HAL resource creation logic from ReleaseController
   - Implement RepresentationModelProcessor<EntityModel<Release>> interface
   - Add self links and component links

2. Create ReleaseRequestMapper class:
   - Path: rest/resource-server/src/main/java/org/eclipse/sw360/rest/resourceserver/release/ReleaseRequestMapper.java
   - Extract setBackwardCompatibleFieldsInRelease method
   - Move request body mapping and backward compatibility logic
   - Handle attachment processing
   - Convert request maps to Release objects

3. Create ReleaseLinkProcessor class:
   - Path: rest/resource-server/src/main/java/org/eclipse/sw360/rest/resourceserver/release/ReleaseLinkProcessor.java
   - Extract getLinkedReleases method logic
   - Move repository link processing
   - Handle transitive link resolution
   - Return CollectionModel<HalResource<ReleaseLink>>

4. Update ReleaseController:
   - Remove extracted methods and logic
   - Inject new processor classes
   - Update method implementations to use the new classes
   - Maintain existing controller layer responsibilities

5. Update imports and dependencies:
   - Add new class imports to ReleaseController
   - Remove unused imports
   - Update any references to moved methods""", "https://github.com/eclipse-sw360/sw360.git")
