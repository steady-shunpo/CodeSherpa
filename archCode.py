import re
from tools import search_repo_advanced, read_local_file
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
import time
from openai import OpenAI

load_dotenv()

ARCH_SYSTEM_PROMPT="""
Role: Senior AI Software Architect

Objective: Analyze a GitHub issue and provide a surgical implementation plan. You have access to a repository graph that shows how files are connected.

Your Available Tools:
1. search_repo(term): Use this to see the "Ego-Graph" (Definitions and References) of a specific function, class, or variable.
2. read_file(path, start, end): Use this to see the actual source code once you've identified a relevant file.

Strict Protocol:
THOUGHT: Explain your reasoning. What are you looking for? Why is this file relevant?
ACTION: Call a tool (e.g., ACTION: search_repo("RequestsCookieJar") OR ACTION: read_file("src/main.py", 10, 20)).
PAUSE: Stop and wait for the user to provide the OBSERVATION.

FINAL_PLAN: Once you have found the bug, provide a step-by-step fix, including file paths and exact code changes.
"""
print(os.environ.get("GEMINI_API_KEY_ARCH"))
client = genai.Client(api_key=str(os.environ.get("GEMINI_API_KEY_ARCH")))
client2 = OpenAI(api_key=os.environ.get("DEEPSEEK_ARCH_KEY"))

def run_architect_loop(user_issue, max_iterations=7):
    # This stores the whole 'brain' of the conversation
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=ARCH_SYSTEM_PROMPT)]),
        types.Content(role="model", parts=[types.Part.from_text(text="Understood. I will follow the rules strictly. Waiting for the signal to begin.")]),
        types.Content(role="user", parts=[types.Part.from_text(text=f"Here is the user issue: {user_issue}")])
    ]

    for i in range(max_iterations):
        print(f"\n--- 🧠 Architect Thinking (Iteration {i+1}) ---")
        
        # 1. Call the LLM (e.g., Gemini API)
        response = client.models.generate_content(
            model='gemini-2.5-flash', # Or whichever model version you are using
            contents=contents,
        ) 
        agent_reply = response.text
        print("Agent Reply: ",agent_reply)
        
        # Add the agent's reply to the conversation history
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=agent_reply)]))
        
        # 2. Check if the Architect is finished
        if "FINAL_PLAN" in agent_reply:
            print("\n✅ Architect has a plan!")
            return agent_reply

        # 3. Check for Tool Use (Regex)
        # Using \s* makes the regex forgiving if the AI adds extra spaces
        search_match = re.search(r'ACTION:\s*search_repo\("([^"]+)"\)', agent_reply)
        read_match = re.search(r'ACTION:\s*read_file\("([^"]+)",\s*(\d+),\s*(\d+)\)', agent_reply)
        
        observation = ""

        # 4. Execute the matched tool
        if search_match:
            search_term = search_match.group(1)
            print(f"🔍 Running local search for: {search_term}")
            observation = search_repo_advanced(search_term)

        elif read_match:
            filepath = read_match.group(1)
            start = int(read_match.group(2))
            end = int(read_match.group(3))
            print(f"📖 Reading file: {filepath} (Lines {start}-{end})")
            observation = read_local_file(filepath, start, end)

        else:
            print("⚠️ No valid action found. Forcing retry.")
            observation = "ERROR: No valid ACTION format detected. Please review the protocol and try again."

        # 5. Time Awareness (Iteration Budgeting)
        turns_left = max_iterations - (i + 1)
        time_warning = ""
        if turns_left == 1:
            time_warning = "\n\n[CRITICAL SYSTEM NOTE: THIS IS YOUR FINAL TURN. You MUST output a FINAL_PLAN now.]"
        elif turns_left > 1:
            time_warning = f"\n\n[SYSTEM NOTE: You have {turns_left} iterations remaining.]"
        print("Observation: ", observation)
        # 6. Feed the observation + time warning back to the agent
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=f"OBSERVATION:\n{observation}{time_warning}")]))

        time.sleep(57)

    return "Architect failed to find a solution within the iteration limit."


res = run_architect_loop("""
                         Bug description

reformat_html in utils.py incorrectly strips word-boundary spaces around inline HTML tags like <strong>, <em>, <b>, <i>.

Steps to reproduce

Pass the following to reformat_html:

<p>Hello <strong>world</strong> foo</p>
Expected: Hello world foo

Actual: Helloworld foo

Root cause

HTMLTagRemover.handle_data calls .strip() on every text node, destroying the spaces that separate inline-tagged words from surrounding text.

Impact

Any book description or field on Open Library that uses inline formatting will have words incorrectly merged when displayed as plain text.
                         """)


print(res)