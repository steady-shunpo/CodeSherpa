import os
import pickle
import json
import networkx as nx
import os
from e2b_code_interpreter import Sandbox

#CHANGE read_local_file. FILE PATH IS HARDCODED RN

def search_repo_advanced(search_term, graph_path='graph.pkl', tags_path='tags.jsonl'):
    # Load the graph
    with open(graph_path, 'rb') as f:
        G = pickle.load(f)
    
    # Load tags into a dictionary for quick lookup
    # We'll key them by 'name' to find the definition/refs
    tag_data = []
    with open(tags_path, 'r') as f:
        for line in f:
            tag_data.append(json.loads(line))

    # 1. Identify the Target Node in the Graph
    # We look for nodes that contain our search term (e.g., "RequestsCookieJar")
    matching_nodes = [n for n in G.nodes if search_term in str(n)]
    if not matching_nodes:
        return f"No matches found in graph for '{search_term}'."

    target = matching_nodes[0] # Take the most likely definition node
    
    # 2. Get the "Ego-Graph" (Immediate neighbors)
    # This captures: Who calls this? What does this call?
    neighbors = list(G.neighbors(target))
    if G.is_directed():
        neighbors += list(G.predecessors(target))
    
    context_nodes = list(set([target] + neighbors))

    # 3. Build the Architect's Report
    report = [
        f"## Repository Context: {search_term}",
        "The following entities are related to your query. Use this to understand dependencies.",
        "| Entity Name | Relationship | File Path | Line | Category |",
        "| :--- | :--- | :--- | :--- | :--- |"
    ]

    for node in context_nodes:
        # Extract the base name from the node string (e.g., 'cookies.RequestsCookieJar' -> 'RequestsCookieJar')
        node_name = str(node).split('.')[-1]
        
        # Find matching entries in tags.json
        matches = [t for t in tag_data if t['name'] == node_name]
        
        for m in matches:
            rel = "**DEFINITION**" if node == target and m['kind'] == 'def' else "Related Reference"
            line = m['line'] if m['line'] != -1 else "Multiple/Global"
            report.append(f"| {m['name']} | {rel} | {m['rel_fname']} | {line} | {m['category']} |")

    return "\n".join(report)

# print(search_repo_advanced("parse_header_parameters"))


def edit_local_file(file_path, start_line, end_line, new_content):
    """
    Replaces a specific range of lines in a file with new content.
    - start_line: The first line to remove (1-indexed)
    - end_line: The last line to remove (1-indexed)
    - new_content: The string to insert in place of the removed lines.
    """
    if not os.path.exists(file_path):
        return f"ERROR: File not found at {file_path}"
    
    try:
        # 1. Read the existing file
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        # Convert to 0-based indexing for Python lists
        start_idx = max(0, start_line - 1)
        # If end_line is identical to start_line, we replace just that one line
        end_idx = min(len(lines), end_line)
        
        if start_idx >= len(lines):
            return f"ERROR: start_line {start_line} is beyond file length ({len(lines)} lines)."

        # 2. Prepare the new content
        # Ensure new_content ends with a newline to maintain proper formatting
        if not new_content.endswith('\n'):
            new_content += '\n'
            
        # 3. Splice the array (Keep everything before, insert new, keep everything after)
        before_lines = lines[:start_idx]
        after_lines = lines[end_idx:]
        
        # 4. Write it back to the file
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(before_lines)
            f.write(new_content)
            f.writelines(after_lines)
            
        # 5. Return a success observation to the AI
        return f"SUCCESS: Replaced lines {start_line} to {end_line} in {file_path}."
        
    except Exception as e:
        return f"ERROR: Could not edit file. {str(e)}"


import os

def read_local_file(file_path, start_line=1, end_line=None):
    """Reads a local file and returns the content with line numbers."""
    file_path = 'testRepos/' +file_path
    if not os.path.exists(file_path):
        return f"ERROR: File not found at {file_path}"
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        # Convert 1-based line numbers to 0-based index
        start_idx = max(0, start_line - 1 - 5)
        end_idx = len(lines) if end_line is None else min(len(lines), end_line + 6)
        
        if start_idx >= len(lines):
            return f"ERROR: start_line {start_line} is beyond file length ({len(lines)} lines)."

        snippet = lines[start_idx:end_idx]
        
        # Format output with line numbers so the Architect knows where it is
        formatted_output = [f"--- FILE: {file_path} (Lines {start_idx+1} to {end_idx}) ---"]
        for i, line in enumerate(snippet):
            formatted_output.append(f"{start_idx + i + 1} | {line.rstrip()}")
            
        return "\n".join(formatted_output)
    except Exception as e:
        return f"ERROR: Could not read file. {str(e)}"




# -----------------------------------------
# Tool 1: Read File (E2B Version)
# -----------------------------------------
def read_remote_file(sandbox: Sandbox, file_path: str, start_line: int = 1, end_line: int = None):
    """Reads a file directly from the E2B Sandbox."""
    try:
        # Read the entire file into memory from the sandbox
        clean_path = file_path.lstrip("./\\")

        content = sandbox.files.read(f"workspace/repo/{clean_path}")
        lines = content.splitlines(keepends=True)
        
        # Apply the Buffer Safety Mechanism (Look before you leap)
        BUFFER = 7
        start_idx = max(0, start_line - 1 - BUFFER)
        end_idx = len(lines) if end_line is None else min(len(lines), end_line + BUFFER)
        
        if start_idx >= len(lines):
            return f"ERROR: start_line {start_line} is beyond file length ({len(lines)} lines)."

        snippet = lines[start_idx:end_idx]
        
        formatted_output = [f"--- E2B FILE: {file_path} (Lines {start_idx+1} to {end_idx}) ---"]
        for i, line in enumerate(snippet):
            formatted_output.append(f"{start_idx + i + 1} | {line.rstrip()}")
            
        return "\n".join(formatted_output)
    except Exception as e:
        return f"ERROR: Could not read file from Sandbox. {str(e)}"

# -----------------------------------------
# Tool 2: Edit File (E2B Version)
# -----------------------------------------
def edit_remote_file(sandbox: Sandbox, file_path: str, start_line: int, end_line: int, new_content: str):
    """Surgically edits a file inside the E2B Sandbox."""
    try:
        # 1. Read current content
        content = sandbox.files.read(f"workspace/repo/{file_path}")
        lines = content.splitlines(keepends=True)
        
        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), end_line)
        
        if not new_content.endswith('\n'):
            new_content += '\n'
            
        # 2. Splice lines
        before_lines = lines[:start_idx]
        after_lines = lines[end_idx:]
        
        new_file_content = "".join(before_lines) + new_content + "".join(after_lines)
        
        # 3. Write back to Sandbox
        sandbox.files.write(f"workspace/repo/{file_path}", new_file_content)
            
        return f"SUCCESS: Replaced lines {start_line} to {end_line} in {file_path} on E2B."
        
    except Exception as e:
        return f"ERROR: Could not edit file in Sandbox. {str(e)}"

# -----------------------------------------
# Tool 3: Run Bash Command (E2B Version)
# -----------------------------------------
def run_remote_command(sandbox: Sandbox, command: str, timeout: int = 60):
    """Executes a command inside the E2B Sandbox."""
    try:
        # E2B handles the subprocess logic securely in the cloud
        result = sandbox.commands.run(f"{command}", timeout=timeout)
        
        output = []
        if result.stdout:
            output.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")

        if result.error:
             output.append(f"EXECUTION ERROR:\n{result.error}")
             
        if not output:
            return f"SUCCESS: Command '{command}' executed silently."
            
        return "\n".join(output)

    except Exception as e:
         # Note: E2B handles timeouts inside the result object usually, 
         # but catching exceptions here is good practice.
        return f"ERROR: Execution failed. {str(e)}"


def write_remote_file(sandbox: Sandbox, file_path: str, content: str):
    """Creates a new file or overwrites an existing one in the E2B Sandbox."""
    try:
        # 1. E2B usually runs in /home/user or /workspace. 
        # Make sure we don't crash if the AI tries to put a test in a new folder.
        dir_name = os.path.dirname(file_path)
        if dir_name:
            # We use run_remote_command to safely create the directory
            sandbox.commands.run(f"cd workspace/repo && mkdir -p {dir_name}")
            
        # 2. Write the file directly using the E2B SDK
        sandbox.files.write(f"workspace/repo/{file_path}", content)
        
        return f"SUCCESS: Wrote complete file to {file_path} in the E2B sandbox."
        
    except Exception as e:
        return f"ERROR: Could not write file. {str(e)}"




def setup_developer_environment(repo_url: str):
    print("☁️  Spinning up E2B Sandbox...")
    sandbox = Sandbox.create(timeout=3000) # This creates the remote Linux environment
    
    print(f"📦 Cloning repository {repo_url} into Sandbox...")
    # Clone the repo. We use run_remote_command so we can see if it worked!
    clone_result = run_remote_command(sandbox, f"git clone {repo_url} workspace/repo")
    print(clone_result)
    
    # Optional: If you are testing the 'requests' repo, you might need to install it
    # print("⚙️  Installing dependencies...")
    # install_result = run_remote_command(sandbox, "cd workspace/repo && pip install -e .[tests]")
    # print(install_result)
    
    return sandbox

# Usage:
# my_sandbox = setup_developer_environment("https://github.com/psf/requests.git")
# Then pass `my_sandbox` into your Developer Agent's tool calls!


def checkpoint_gate(agent_name, agent_output):
    """
    Pauses the pipeline to request human approval before proceeding.
    """
    print(f"\n{'='*50}")
    print(f"🛑 CHECKPOINT: {agent_name.upper()} TASK COMPLETE")
    print(f"{'='*50}")
    
    # Placeholder: Currently prints raw output. Later, pass this through your Summarizer.
    print("\n--- AGENT OUTPUT ---")
    print(agent_output.strip())
    print("--------------------\n")
    
    while True:
        print("What would you like to do?")
        print("  [y] Approve and proceed to the next step")
        print("  [n] Reject and provide feedback for a retry")
        print("  [t] Takeover (Halt automation / Launch Co-Pilot later)")
        
        choice = input("Enter your choice (y/n/t): ").strip().lower()
        
        if choice == 'y':
            print(f"✅ Approved. Proceeding past {agent_name}...")
            return {"status": "PROCEED", "feedback": None}
            
        elif choice == 'n':
            print(f"❌ Rejected. Please provide feedback for the {agent_name} to fix:")
            feedback = input("Feedback: ").strip()
            return {"status": "RETRY", "feedback": feedback}
            
        elif choice == 't':
            print("⚠️ Taking over. Halting autonomous pipeline...")
            return {"status": "TAKEOVER", "feedback": None}
            
        else:
            print("Invalid choice. Please enter 'y', 'n', or 't'.")


def extract_final_plan(text):
    marker = "FINAL_PLAN:"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return None