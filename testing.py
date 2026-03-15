import re
from tools import search_repo_advanced, read_local_file
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox



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
# print(os.environ.get("GEMINI_API_KEY_ARCH"))
# client = genai.Client(api_key=str(os.environ.get("GEMINI_API_KEY_ARCH")))

# response = client.models.generate_content(
#             model='gemini-3-flash-preview', # Or whichever model version you are using
#             contents="reply with 'pong' if you can hear me",
#         ) 

# print(response)



sandbox = Sandbox.create()

# result = sandbox.commands.run("cd ..")

# result = sandbox.commands.run("git clone ") 
# sandbox.commands.run("mkdir workspace")
# sandbox.commands.run("cd workspace")
# sandbox.commands.run("mkdir hello")
result = sandbox.commands.run("git clone https://github.com/psf/requests.git repo/test") 
result=sandbox.commands.run("cd repo/test && pip install -e .[tests]")
# result=sandbox.commands.run("pwd")

# result = sandbox.commands.run("cd requests && pip install -e .[tests]")

print(result)