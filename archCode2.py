import re
from tools import search_repo_advanced, read_local_file, checkpoint_gate
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
import time
from openai import OpenAI


load_dotenv()

ARCH_SYSTEM_PROMPT="""
Role: Senior AI Software Architect

Objective: Analyze a GitHub issue and provide a surgical implementation plan. You have access to a repository graph showing how files are connected.

AVAILABLE COMMANDS (USE AS PLAIN TEXT ONLY):
1. search_repo(term): See the "Ego-Graph" (Definitions and References) of a specific function, class, or variable.strictly ONLY mention the function or class name as term. DO NOT PUT reserved keywords before the term.
2. read_file(path, start, end): See the actual source code once you've identified a relevant file.

CRITICAL CONSTRAINTS (OBEY STRICTLY):
1. NO NATIVE FUNCTION CALLING: Do not output JSON tool calls (e.g., {"name": "..."}). You must output plain text format exactly as shown below.
2. NO TEST CASES: Do not write, suggest, or plan test cases. Another agent handles testing.
3. NO COMMIT MESSAGES: Do not write commit messages.
4. ONE COMMAND AT A TIME: You must output "PAUSE" and stop generating immediately after an ACTION. Do not hallucinate the OBSERVATION.

REQUIRED OUTPUT FORMAT:
You must strictly follow one of the two formats below. 

Format 1: Gathering Context
THOUGHT: [Explain your exact reasoning]
ACTION: [e.g., search_repo("RequestsCookieJar")]
PAUSE

Format 2: Proposing the Fix
THOUGHT: [Explain how you found the bug and why this fix will work]
FINAL_PLAN: [Provide a step-by-step fix, including file paths and exact code changes]
"""


print(os.environ.get("GROQ_ARCH_KEY"))
# client = genai.Client(api_key=str(os.environ.get("GROQ_ARCH_KEY")))
client2 = OpenAI(
    api_key=os.environ.get("OPENROUTER_ARCH_KEY"),
    base_url="https://openrouter.ai/api/v1",
)
def run_architect_loop(user_issue, max_iterations=15):
    # This stores the whole 'brain' of the conversation
    # contents = [
    #     types.Content(role="user", parts=[types.Part.from_text(text=ARCH_SYSTEM_PROMPT)]),
    #     types.Content(role="model", parts=[types.Part.from_text(text="Understood. I will follow the rules strictly. Waiting for the signal to begin.")]),
    #     types.Content(role="user", parts=[types.Part.from_text(text=f"Here is the user issue: {user_issue}")])
    # ]

    messages = [
        {"role": "user","content": ARCH_SYSTEM_PROMPT},
        {"role": "system","content": "Understood. I will follow the rules strictly. Waiting for the signal to begin."},
        {"role": "user","content": f"Here is the user issue: {user_issue}"},

    ]

    i = 0
    while i < max_iterations:
        print(f"\n--- 🧠 Architect Thinking (Iteration {i+1}) ---")
        
        # 1. Call the LLM (e.g., Gemini API)
        response = client2.chat.completions.create(
            model="arcee-ai/trinity-large-preview:free", # Or whichever model version you are using
            messages=messages
        ) 
        agent_reply =response.choices[0].message.content
        print("Agent Reply: ",agent_reply)
        
        # Add the agent's reply to the conversation history
        messages.append({"role": "system","content": agent_reply})
        



        # 2. Check if the Architect is finished
        if "FINAL_PLAN" in agent_reply:
            print("\n✅ Architect has a plan!")
            decision = checkpoint_gate("Architect", agent_reply)

            if decision["status"] == "PROCEED":
                return {"status": "success", "content": agent_reply} # Success! Hand this off to the Developer
                
            elif decision["status"] == "RETRY":
                # Append the user's feedback and let the while loop run the Architect again
                messages.append({"role": "assistant", "content": agent_reply})
                messages.append({"role": "user", "content": f"Your plan was rejected. Fix it based on this feedback:\n{decision['feedback']}"})
                print("🔄 Restarting Architect with new feedback...")
                max_iterations += 7
                continue 
                
            elif decision["status"] == "TAKEOVER":
                # Kill the loop and return a flag so the main script knows to launch Co-Pilot
                return {"status": "failed", "content": f"Takeover. FINAL PLAN: {messages[-1]}"}

            return agent_reply




        # 3. Check for Tool Use (Regex)
        # Using \s* makes the regex forgiving if the AI adds extra spaces
        # The \** catches any number of stars before, inside, or after the colon
        search_match = re.search(r'\**ACTION\**:\**\s*search_repo\("([^"]+)"\)', agent_reply)

        read_match = re.search(r'\**ACTION\**:\**\s*read_file\("([^"]+)",\s*(\d+),\s*(\d+)\)', agent_reply)
        
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
        messages.append({"role": "user", "content": f"OBSERVATION:\n{observation}{time_warning}"})
        i+=1
        time.sleep(57)

    return {"status": "failed", "content": f"Architect failed to find a solution within the iteration limit. FINAL PLAN: {messages[-1]}"}


# res = run_architect_loop("""Several REST API handlers use slog.Warn() / slog.Info() directly instead of going through api.Logger. This inconsistency means log output may bypass structured logging configuration. All handlers should use api.Logger for consistency.

# Known instances
# internal/restapi/trip_details_handler.go (lines 164, 174) — uses slog.Warn() directly
# There may be additional instances in other handler files
# Acceptance criteria

# Audit all *_handler.go files in internal/restapi/ for direct slog usage

# Replace all direct slog.Warn(), slog.Info(), slog.Error() calls with the equivalent api.Logger.Warn(), api.Logger.Info(), api.Logger.Error() calls

# grep -r "slog\." internal/restapi/*handler*.go returns zero matches

# make test passes

# make lint passes
# Context
# Handlers in this project access shared dependencies (including the logger) through the api *RestAPI receiver, which embeds *app.Application. The pattern to follow is api.Logger.Warn("message", "key", value). See handlers like trips_for_route_handler.go for correct usage.""")


# print(res)