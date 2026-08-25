import os
import pickle
import json
import networkx as nx
import os
import re
import requests
from e2b_code_interpreter import Sandbox
from dotenv import load_dotenv
import time
import subprocess 
import shutil
import yaml
import toml

load_dotenv("../.env")
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

# e.g., in repograph/ast_search.py or tools.py
def format_symbol_result(term: str, res: dict) -> str:
    matches = res.get("matches", [])
    did_you_mean = res.get("did_you_mean", [])

    if matches:
        out = [f"Found {len(matches)} match(es) for '{term}':"]
        for m in matches:
            out.append(f"- {m['kind']} `{m['name']}` in `{m['file']}` (lines {m['start_line']}-{m['end_line']})")
            if m.get("signature"):
                out.append(f"  Signature: {m['signature']}")
            if m.get("docstring"):
                out.append(f"  Docstring: {m['docstring']}")
        return "\n".join(out)

    if did_you_mean:
        return f"No exact match for '{term}'. Did you mean: {', '.join(did_you_mean)}?"

    return f"No matches found for '{term}'."

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
        clean_path = re.sub(r'^(?:workspace/repo/|\./workspace/repo/)', '', file_path.lstrip("./\\"))

        content = sandbox.files.read(f"workspace/repo/{clean_path}")
        lines = content.splitlines(keepends=True)
        
        # Apply the Buffer Safety Mechanism (Look before you leap)
        BUFFER = 7
        start_idx = max(0, start_line - 1 - BUFFER)
        end_idx = len(lines) if end_line is None else min(len(lines), end_line + BUFFER)
        
        if start_idx >= len(lines):
            return f"ERROR: start_line {start_line} is beyond file length ({len(lines)} lines)."

        snippet = lines[start_idx:end_idx]
        
        formatted_output = [f"--- E2B FILE: {clean_path} (Lines {start_idx+1} to {end_idx}) ---"]
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
        clean_path = re.sub(r'^(?:workspace/repo/|\./workspace/repo/)', '', file_path.lstrip("./\\"))
        # 1. Read current content
        content = sandbox.files.read(f"workspace/repo/{clean_path}")
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
        sandbox.files.write(f"workspace/repo/{clean_path}", new_file_content)
            
        return f"SUCCESS: Replaced lines {start_line} to {end_line} in {clean_path} on E2B."
        
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
        output = [f"ERROR: Execution failed. {str(e)}"]
        if hasattr(e, "stdout") and e.stdout:
            output.append(f"STDOUT:\n{e.stdout}")
        if hasattr(e, "stderr") and e.stderr:
            output.append(f"STDERR:\n{e.stderr}")
        if hasattr(e, "result") and e.result:
            if getattr(e.result, "stdout", None):
                output.append(f"STDOUT:\n{e.result.stdout}")
            if getattr(e.result, "stderr", None):
                output.append(f"STDERR:\n{e.result.stderr}")
        return "\n".join(output)


def write_remote_file(sandbox: Sandbox, file_path: str, content: str):
    """Creates a new file or overwrites an existing one in the E2B Sandbox."""
    try:
        clean_path = re.sub(r'^(?:workspace/repo/|\./workspace/repo/)', '', file_path.lstrip("./\\"))
        dir_name = os.path.dirname(clean_path)
        if dir_name:
            # We use run_remote_command to safely create the directory
            sandbox.commands.run(f"cd workspace/repo && mkdir -p {dir_name}")
            
        # 2. Write the file directly using the E2B SDK
        sandbox.files.write(f"workspace/repo/{clean_path}", content)
        
        return f"SUCCESS: Wrote complete file to {clean_path} in the E2B sandbox."
        
    except Exception as e:
        return f"ERROR: Could not write file. {str(e)}"


UNSUPPORTED_REPOS = ["scikit-learn", "matplotlib", "astropy"]


def read_file(sandbox: Sandbox, path: str) -> str | None:
    """Return file contents as string, or None if file doesn't exist."""
    try:
        return sandbox.files.read(path)
    except Exception:
        return None
 
 
def file_exists(sandbox: Sandbox, path: str) -> bool:
    return read_file(sandbox, path) is not None


# ── 1. Python Version Detection & Environment Setup ───────────────────────────

DEFAULT_PYTHON_VERSION = "3.11"
CANDIDATE_PYTHON_VERSIONS = ["3.7", "3.8", "3.9", "3.10", "3.11", "3.12"]


def _version_key(v: str) -> tuple[int, ...]:
    """Converts '3.10.4' or '3.9' to tuple of ints (3, 10, 4) for numeric sorting."""
    digits = [int(p) for p in re.findall(r"\d+", str(v))]
    return tuple(digits) if digits else (0,)


def _resolve_best_version(versions: list[str]) -> str:
    """Given a list of version strings, returns the highest supported version."""
    if not versions:
        return DEFAULT_PYTHON_VERSION
    cleaned = []
    for v in versions:
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", str(v))
        if m:
            cleaned.append(m.group(1))
    if not cleaned:
        return DEFAULT_PYTHON_VERSION
    cleaned.sort(key=_version_key)
    return cleaned[-1]


def _evaluate_specifier_against_versions(spec_str: str) -> str | None:
    """
    Given a version specifier (e.g. '>=3.8, <3.11', '^3.9', '~=3.8.0', '==3.10.*'),
    finds the highest compatible version from CANDIDATE_PYTHON_VERSIONS.
    """
    if not spec_str or not isinstance(spec_str, str):
        return None
    spec_str = spec_str.strip()
    if not spec_str:
        return None

    # Handle poetry caret ^3.9 -> >=3.9, <4.0
    if spec_str.startswith("^"):
        base = spec_str[1:].strip()
        spec_str = f">={base}, <4.0"

    # Handle tilde ~=3.8.0 or ~=3.8
    if spec_str.startswith("~="):
        parts = spec_str[2:].strip().split(".")
        if len(parts) >= 3:
            spec_str = f">={parts[0]}.{parts[1]}.{parts[2]}, <{parts[0]}.{int(parts[1])+1}.0"
        elif len(parts) == 2:
            spec_str = f">={parts[0]}.{parts[1]}, <{int(parts[0])+1}.0"
    elif spec_str.startswith("~"):
        parts = spec_str[1:].strip().split(".")
        if len(parts) >= 2:
            spec_str = f">={parts[0]}.{parts[1]}, <{parts[0]}.{int(parts[1])+1}"

    clauses = [c.strip() for c in re.split(r"[,;]", spec_str) if c.strip()]
    if not clauses:
        return None

    valid = []
    for ver in CANDIDATE_PYTHON_VERSIONS:
        ver_key = _version_key(ver)
        satisfies_all = True
        for clause in clauses:
            m = re.match(r"(===|==|!=|<=|>=|<|>|=)?\s*([0-9]+(?:\.[0-9]+)*(?:\.\*)?)", clause)
            if not m:
                continue
            op = m.group(1) or "=="
            target_str = m.group(2)

            if target_str.endswith(".*"):
                prefix = target_str[:-2]
                if op in ("==", "=", "==="):
                    if not ver.startswith(prefix):
                        satisfies_all = False
                        break
                    continue
                target_str = prefix

            target_key = _version_key(target_str)
            max_len = max(len(ver_key), len(target_key))
            v_pad = ver_key + (0,) * (max_len - len(ver_key))
            t_pad = target_key + (0,) * (max_len - len(target_key))

            if op in ("==", "=", "==="):
                if ver_key[:len(target_key)] != target_key and v_pad != t_pad:
                    satisfies_all = False
                    break
            elif op == "!=" and v_pad == t_pad:
                satisfies_all = False
                break
            elif op == "<=" and v_pad > t_pad:
                satisfies_all = False
                break
            elif op == ">=" and v_pad < t_pad:
                satisfies_all = False
                break
            elif op == "<" and v_pad >= t_pad:
                satisfies_all = False
                break
            elif op == ">" and v_pad <= t_pad:
                satisfies_all = False
                break

        if satisfies_all:
            valid.append(ver)

    if valid:
        return valid[-1]

    m = re.search(r"(\d+\.\d+)", spec_str)
    if m:
        return m.group(1)
    return None


def _parse_python_version_file(sandbox: Sandbox, repo_root: str) -> str | None:
    """1. Explicit pin files: .python-version, .tool-versions, runtime.txt"""
    # .python-version
    raw = read_file(sandbox, f"{repo_root}/.python-version")
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.search(r"(\d+\.\d+(?:\.\d+)?)", line)
                if m:
                    return m.group(1)

    # .tool-versions (asdf / mise / rtx)
    raw = read_file(sandbox, f"{repo_root}/.tool-versions")
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("python "):
                m = re.search(r"python\s+(\d+\.\d+(?:\.\d+)?)", line)
                if m:
                    return m.group(1)

    # runtime.txt (Heroku / cloud)
    raw = read_file(sandbox, f"{repo_root}/runtime.txt")
    if raw:
        m = re.search(r"(?:python-)?(\d+\.\d+(?:\.\d+)?)", raw.strip(), re.IGNORECASE)
        if m:
            return m.group(1)

    return None


def _parse_lockfiles(sandbox: Sandbox, repo_root: str) -> str | None:
    """2. Lockfiles: poetry.lock, Pipfile.lock, pdm.lock, uv.lock"""
    # poetry.lock
    raw = read_file(sandbox, f"{repo_root}/poetry.lock")
    if raw:
        m = re.search(r'python-versions\s*=\s*["\']([^"\']+)["\']', raw)
        if m:
            ver = _evaluate_specifier_against_versions(m.group(1))
            if ver:
                return ver

    # Pipfile.lock
    raw = read_file(sandbox, f"{repo_root}/Pipfile.lock")
    if raw:
        try:
            data = json.loads(raw)
            meta = data.get("_meta", {}).get("requires", {})
            ver_str = meta.get("python_full_version") or meta.get("python_version")
            if ver_str:
                return str(ver_str)
        except Exception:
            pass

    # pdm.lock
    raw = read_file(sandbox, f"{repo_root}/pdm.lock")
    if raw:
        m = re.search(r'(?:python_version|requires-python)\s*=\s*["\']([^"\']+)["\']', raw)
        if m:
            ver = _evaluate_specifier_against_versions(m.group(1))
            if ver:
                return ver

    # uv.lock
    raw = read_file(sandbox, f"{repo_root}/uv.lock")
    if raw:
        m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', raw)
        if m:
            ver = _evaluate_specifier_against_versions(m.group(1))
            if ver:
                return ver

    return None


def _parse_pyproject_version(sandbox: Sandbox, repo_root: str) -> str | None:
    """3. pyproject.toml PEP 621, Poetry, PDM, Rye, Hatch, Classifiers"""
    raw = read_file(sandbox, f"{repo_root}/pyproject.toml")
    if not raw:
        return None

    try:
        data = toml.loads(raw)
        # [project] requires-python
        req_python = data.get("project", {}).get("requires-python")
        if req_python:
            ver = _evaluate_specifier_against_versions(req_python)
            if ver:
                return ver

        # [tool.poetry.dependencies] python
        poetry_py = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
        if isinstance(poetry_py, str):
            ver = _evaluate_specifier_against_versions(poetry_py)
            if ver:
                return ver
        elif isinstance(poetry_py, dict) and "version" in poetry_py:
            ver = _evaluate_specifier_against_versions(str(poetry_py["version"]))
            if ver:
                return ver

        # [tool.pdm] requires-python
        pdm_py = data.get("tool", {}).get("pdm", {}).get("requires-python")
        if pdm_py:
            ver = _evaluate_specifier_against_versions(pdm_py)
            if ver:
                return ver

        # [tool.rye]
        rye_py = (
            data.get("tool", {}).get("rye", {}).get("python-version")
            or data.get("tool", {}).get("rye", {}).get("requires-python")
        )
        if rye_py:
            ver = _evaluate_specifier_against_versions(str(rye_py))
            if ver:
                return ver

        # [tool.hatch.envs]
        hatch_envs = data.get("tool", {}).get("hatch", {}).get("envs", {})
        if isinstance(hatch_envs, dict):
            for env in hatch_envs.values():
                if isinstance(env, dict) and "python" in env:
                    ver = _evaluate_specifier_against_versions(str(env["python"]))
                    if ver:
                        return ver

        # Classifiers
        classifiers = data.get("project", {}).get("classifiers", [])
        clf_versions = []
        for c in classifiers:
            m = re.search(r"Programming Language :: Python :: (\d+\.\d+)", str(c))
            if m:
                clf_versions.append(m.group(1))
        if clf_versions:
            return _resolve_best_version(clf_versions)
    except Exception:
        pass

    return None


def _parse_setup_cfg_version(sandbox: Sandbox, repo_root: str) -> str | None:
    """4. setup.cfg [options] python_requires and classifiers"""
    raw = read_file(sandbox, f"{repo_root}/setup.cfg")
    if not raw:
        return None

    m = re.search(r"python_requires\s*=\s*([^\n]+)", raw)
    if m:
        ver = _evaluate_specifier_against_versions(m.group(1))
        if ver:
            return ver

    clf_versions = re.findall(r"Programming Language :: Python :: (\d+\.\d+)", raw)
    if clf_versions:
        return _resolve_best_version(clf_versions)

    return None


def _parse_setup_py_version(sandbox: Sandbox, repo_root: str) -> str | None:
    """5. setup.py python_requires and classifiers"""
    raw = read_file(sandbox, f"{repo_root}/setup.py")
    if not raw:
        return None

    m = re.search(r"python_requires\s*=\s*['\"]([^'\"]+)['\"]", raw)
    if m:
        ver = _evaluate_specifier_against_versions(m.group(1))
        if ver:
            return ver

    clf_versions = re.findall(r"Programming Language :: Python :: (\d+\.\d+)", raw)
    if clf_versions:
        return _resolve_best_version(clf_versions)

    return None


def _parse_ci_workflows(sandbox: Sandbox, repo_root: str) -> str | None:
    """6. CI configs (.github/workflows/*.yml, .travis.yml, .circleci, azure-pipelines, gitlab, bitbucket)"""
    found_versions = []

    # GitHub Workflows
    workflows_dir = f"{repo_root}/.github/workflows"
    try:
        entries = sandbox.files.list(workflows_dir)
    except Exception:
        entries = []

    for entry in entries:
        entry_name = getattr(entry, "name", str(entry))
        if not entry_name.endswith((".yml", ".yaml")):
            continue
        raw = read_file(sandbox, f"{workflows_dir}/{entry_name}")
        if not raw:
            continue

        matches = re.findall(r'python-version:\s*\[?(.*?)\]?(?:\n|$)', raw)
        for match in matches:
            sub_versions = re.findall(r'[\'"]?(\d+\.\d+(?:\.\d+)?)[\'"]?', match)
            found_versions.extend(sub_versions)

        matches_matrix = re.findall(r'python:\s*\[(.*?)\]', raw)
        for match in matches_matrix:
            sub_versions = re.findall(r'[\'"]?(\d+\.\d+(?:\.\d+)?)[\'"]?', match)
            found_versions.extend(sub_versions)

    # .travis.yml
    raw_travis = read_file(sandbox, f"{repo_root}/.travis.yml")
    if raw_travis:
        m_py = re.search(r'python:\s*\n((?:\s*-\s*[\'"]?\d+\.\d+[\'"]?\n?)+)', raw_travis)
        if m_py:
            found_versions.extend(re.findall(r'(\d+\.\d+)', m_py.group(1)))

    # .circleci/config.yml
    raw_circle = read_file(sandbox, f"{repo_root}/.circleci/config.yml")
    if raw_circle:
        matches = re.findall(r'(?:cimg|circleci)/python:(\d+\.\d+(?:\.\d+)?)', raw_circle)
        found_versions.extend(matches)

    # azure-pipelines.yml
    raw_azure = read_file(sandbox, f"{repo_root}/azure-pipelines.yml")
    if raw_azure:
        matches = re.findall(r'python\.version:\s*[\'"]?(\d+\.\d+(?:\.\d+)?)[\'"]?', raw_azure)
        found_versions.extend(matches)

    # .gitlab-ci.yml
    raw_gitlab = read_file(sandbox, f"{repo_root}/.gitlab-ci.yml")
    if raw_gitlab:
        matches = re.findall(r'image:\s*python:(\d+\.\d+(?:\.\d+)?)', raw_gitlab)
        found_versions.extend(matches)

    # bitbucket-pipelines.yml
    raw_bitbucket = read_file(sandbox, f"{repo_root}/bitbucket-pipelines.yml")
    if raw_bitbucket:
        matches = re.findall(r'image:\s*python:(\d+\.\d+(?:\.\d+)?)', raw_bitbucket)
        found_versions.extend(matches)

    if found_versions:
        return _resolve_best_version(found_versions)

    return None


def _parse_tox_ini(sandbox: Sandbox, repo_root: str) -> str | None:
    """7. tox.ini / noxfile.py envlist"""
    for fname in ["tox.ini", "noxfile.py"]:
        raw = read_file(sandbox, f"{repo_root}/{fname}")
        if not raw:
            continue
        matches = re.findall(r'py(?:thon)?(3)(\d+)', raw)
        versions = [f"3.{minor}" for major, minor in matches]
        if versions:
            return _resolve_best_version(versions)

        m = re.search(r'envlist\s*=\s*([^\n]+)', raw)
        if m:
            env_matches = re.findall(r'py3(\d+)', m.group(1))
            if env_matches:
                return _resolve_best_version([f"3.{min_ver}" for min_ver in env_matches])

    return None


def _parse_pipfile(sandbox: Sandbox, repo_root: str) -> str | None:
    """8. Pipfile [requires] section"""
    raw = read_file(sandbox, f"{repo_root}/Pipfile")
    if not raw:
        return None

    m = re.search(r'python_(?:full_)?version\s*=\s*["\']([^"\']+)["\']', raw)
    if m:
        return m.group(1)

    return None


def _parse_conda_env(sandbox: Sandbox, repo_root: str) -> str | None:
    """9. Conda environment files (environment.yml, environment.yaml, conda.yaml, meta.yaml)"""
    for fname in ["environment.yml", "environment.yaml", "conda.yaml", "meta.yaml"]:
        raw = read_file(sandbox, f"{repo_root}/{fname}")
        if not raw:
            continue
        m = re.search(r'-\s*python\s*([=><~]+)?\s*(\d+\.\d+(?:\.\d+)?)', raw)
        if m:
            op = m.group(1) or "=="
            ver_spec = f"{op}{m.group(2)}"
            ver = _evaluate_specifier_against_versions(ver_spec)
            if ver:
                return ver
            return m.group(2)

    return None


def _parse_dockerfile(sandbox: Sandbox, repo_root: str) -> str | None:
    """10. Dockerfile / docker-compose.yml"""
    for fname in ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"]:
        raw = read_file(sandbox, f"{repo_root}/{fname}")
        if not raw:
            continue
        m = re.search(r'FROM\s+(?:[\w\.\-]+/)?python:(\d+\.\d+(?:\.\d+)?)', raw, re.IGNORECASE)
        if m:
            return m.group(1)

    return None


def _parse_pre_commit(sandbox: Sandbox, repo_root: str) -> str | None:
    """11. .pre-commit-config.yaml"""
    raw = read_file(sandbox, f"{repo_root}/.pre-commit-config.yaml")
    if not raw:
        return None

    m = re.search(r'default_language_version:\s*\n\s*python:\s*python(\d+\.\d+)', raw)
    if m:
        return m.group(1)

    return None


def _parse_readme(sandbox: Sandbox, repo_root: str) -> str | None:
    """12. README badges and text mentions"""
    for fname in ["README.md", "README.rst", "readme.md", "readme.rst"]:
        raw = read_file(sandbox, f"{repo_root}/{fname}")
        if not raw:
            continue
        badge_matches = re.findall(r'python[_-]?(\d+\.\d+)', raw, re.IGNORECASE)
        if badge_matches:
            return _resolve_best_version(badge_matches)
        text_match = re.search(r'Python\s+(?:>=|version\s+)?(\d+\.\d+)', raw, re.IGNORECASE)
        if text_match:
            return text_match.group(1)

    return None


def detect_python_version(sandbox: Sandbox, repo_root: str) -> str:
    """
    Detects the required Python version for a repository by inspecting
    configuration files, packaging metadata, CI files, and environment definitions.
    """
    parsers = [
        ("explicit pin files", _parse_python_version_file),
        ("lockfiles", _parse_lockfiles),
        ("pyproject.toml", _parse_pyproject_version),
        ("setup.cfg", _parse_setup_cfg_version),
        ("setup.py", _parse_setup_py_version),
        ("CI workflows", _parse_ci_workflows),
        ("tox.ini", _parse_tox_ini),
        ("Pipfile", _parse_pipfile),
        ("conda env", _parse_conda_env),
        ("Dockerfile", _parse_dockerfile),
        ("pre-commit config", _parse_pre_commit),
        ("README", _parse_readme),
    ]

    for name, parser in parsers:
        try:
            ver = parser(sandbox, repo_root)
            if ver:
                print(f"📌 Detected Python {ver} from {name}")
                return ver
        except Exception:
            continue

    print(f"📌 Defaulting Python version to {DEFAULT_PYTHON_VERSION}")
    return DEFAULT_PYTHON_VERSION


def create_virtual_environment(sandbox: Sandbox, python_version: str) -> bool:
    """
    Installs uv in the sandbox (if needed), provisions the requested Python version,
    creates $HOME/.venv, and links binaries so all commands use this environment.
    """
    print(f"🐍 Provisioning Python {python_version} environment with uv...")

    # 1. Install uv if not available
    cmd_install_uv = (
        "command -v uv >/dev/null 2>&1 || "
        "(curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null || pip install uv)"
    )
    run_remote_command(sandbox, cmd_install_uv, timeout=60)

    # 2. Create virtual environment with requested python version in $HOME/.venv
    cmd_create_venv = (
        f"export PATH=\"$HOME/.local/bin:$HOME/.cargo/bin:$PATH\" && "
        f"mkdir -p \"$HOME/.local/bin\" && "
        f"uv venv \"$HOME/.venv\" --python {python_version} || "
        f"python3 -m venv \"$HOME/.venv\""
    )
    res = run_remote_command(sandbox, cmd_create_venv, timeout=120)
    print(f"📦 venv creation: {res}")

    # 3. Pre-install base test and build tools (pytest, wheel, setuptools) into the venv
    cmd_install_base = (
        f"export PATH=\"$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.venv/bin:$PATH\" && "
        f"uv pip install --python \"$HOME/.venv\" pytest pytest-cov wheel setuptools 2>/dev/null || "
        f"\"$HOME/.venv/bin/pip\" install pytest pytest-cov wheel setuptools"
    )
    run_remote_command(sandbox, cmd_install_base, timeout=120)

    # 4. Link binaries and configure PATH & PYTHONPATH
    cmd_link = (
        "mkdir -p \"$HOME/.local/bin\" && "
        "ln -sf \"$HOME/.venv/bin/python\" \"$HOME/.local/bin/python\" && "
        "ln -sf \"$HOME/.venv/bin/python3\" \"$HOME/.local/bin/python3\" && "
        "ln -sf \"$HOME/.venv/bin/pip\" \"$HOME/.local/bin/pip\" && "
        "ln -sf \"$HOME/.venv/bin/pip3\" \"$HOME/.local/bin/pip3\" && "
        "ln -sf \"$HOME/.venv/bin/pytest\" \"$HOME/.local/bin/pytest\" 2>/dev/null || true; "
        "(sudo ln -sf \"$HOME/.venv/bin/python\" /usr/local/bin/python 2>/dev/null || true); "
        "(sudo ln -sf \"$HOME/.venv/bin/python3\" /usr/local/bin/python3 2>/dev/null || true); "
        "(sudo ln -sf \"$HOME/.venv/bin/pip\" /usr/local/bin/pip 2>/dev/null || true); "
        "(sudo ln -sf \"$HOME/.venv/bin/pip3\" /usr/local/bin/pip3 2>/dev/null || true); "
        "(sudo ln -sf \"$HOME/.venv/bin/pytest\" /usr/local/bin/pytest 2>/dev/null || true); "
        "echo 'export PATH=\"$HOME/.venv/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH\"' >> \"$HOME/.bashrc\" && "
        "echo 'export PATH=\"$HOME/.venv/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH\"' >> \"$HOME/.profile\" && "
        "echo 'export PYTHONPATH=\"$HOME/workspace/repo:$HOME/workspace/repo/src:$PYTHONPATH\"' >> \"$HOME/.bashrc\" && "
        "echo 'export PYTHONPATH=\"$HOME/workspace/repo:$HOME/workspace/repo/src:$PYTHONPATH\"' >> \"$HOME/.profile\""
    )
    run_remote_command(sandbox, cmd_link, timeout=30)

    # 5. Verify the Python & Pytest version
    check_py = run_remote_command(sandbox, "export PATH=\"$HOME/.venv/bin:$HOME/.local/bin:$PATH\" && python --version && pytest --version")
    print(f"✅ Active Sandbox Python & Pytest:\n{check_py}")
    return True
 
def detect_install_command(sandbox: Sandbox, repo_root: str) -> str:
    """
    Detects all required installation commands to run inside workspace/repo.
    Installs requirement files and installs the repo in editable mode.
    """
    cmds = []

    # 1. Requirements files
    for fname in ["requirements.txt", "requirements-dev.txt", "requirements-test.txt", "test-requirements.txt"]:
        if file_exists(sandbox, f"{repo_root}/{fname}"):
            cmds.append(f"pip install -r {fname}")

    # 2. pyproject.toml / setup.py / setup.cfg
    pyproject_raw = read_file(sandbox, f"{repo_root}/pyproject.toml")
    has_setup = file_exists(sandbox, f"{repo_root}/setup.py") or file_exists(sandbox, f"{repo_root}/setup.cfg")

    if pyproject_raw:
        try:
            pyproject = toml.loads(pyproject_raw)
            extras = (
                pyproject.get("project", {}).get("optional-dependencies", {})
                or pyproject.get("tool", {}).get("poetry", {}).get("extras", {})
            )
            installed_extra = False
            for extra in ["dev", "test", "testing", "d"]:
                if extra in extras:
                    cmds.append(f'pip install -e ".[{extra}]"')
                    installed_extra = True
                    break
            if not installed_extra:
                cmds.append('pip install -e "."')
        except Exception:
            cmds.append('pip install -e "."')
    elif has_setup:
        cmds.append('pip install -e "."')

    if not cmds:
        cmds.append('pip install -e "."')

    return " && ".join(cmds)


# Detect test command
def _extract_from_workflows(sandbox: Sandbox, repo_root: str) -> str | None:
    """
    Walk .github/workflows/*.yml and look for a step that runs pytest or unittest.
    Returns the raw command string if found.
    """
    workflows_dir = f"{repo_root}/.github/workflows"
    try:
        entries = sandbox.files.list(workflows_dir)
    except Exception:
        return None
 
    for entry in entries:
        if not entry.name.endswith((".yml", ".yaml")):
            continue
 
        raw = read_file(sandbox, f"{workflows_dir}/{entry.name}")
        if not raw:
            continue
 
        try:
            workflow = yaml.safe_load(raw)
        except Exception:
            continue
 
        # Walk every job -> steps -> run field
        jobs = workflow.get("jobs", {})
        for job in jobs.values():
            steps = job.get("steps", []) if isinstance(job, dict) else []
            for step in steps:
                run_cmd = step.get("run", "") if isinstance(step, dict) else ""
                if not run_cmd:
                    continue
                # Look for pytest or python -m pytest or unittest
                lines = run_cmd.strip().splitlines()
                for line in lines:
                    line = line.strip()
                    if "pytest" in line or "python -m pytest" in line or "python -m unittest" in line:
                        return line
 
    return None
 
 
def detect_test_command(sandbox: Sandbox, repo_root: str) -> str:
    """
    Priority order:
    1. .github/workflows — most reliable, it's what CI actually runs
    2. Makefile test target
    3. pyproject.toml [tool.pytest.ini_options]
    4. pytest.ini / tox.ini presence
    5. Bare pytest fallback
    """
    # 1. Workflows
    workflow_cmd = _extract_from_workflows(sandbox, repo_root)
    if workflow_cmd:
        return workflow_cmd
 
    # 2. Makefile
    makefile_raw = read_file(sandbox, f"{repo_root}/Makefile")
    if makefile_raw:
        in_test_target = False
        for line in makefile_raw.splitlines():
            if line.startswith("test:") or line.startswith("tests:"):
                in_test_target = True
                continue
            if in_test_target:
                if line.startswith("\t"):  # Makefile recipe line
                    cmd = line.strip()
                    if "pytest" in cmd or "unittest" in cmd:
                        return cmd
                else:
                    in_test_target = False  # left the target block
 
    # 3. pyproject.toml pytest options
    pyproject_raw = read_file(sandbox, f"{repo_root}/pyproject.toml")
    if pyproject_raw:
        try:
            pyproject = toml.loads(pyproject_raw)
            pytest_opts = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})
            if pytest_opts:
                # Build a pytest command from known options
                parts = ["pytest"]
                if "testpaths" in pytest_opts:
                    paths = pytest_opts["testpaths"]
                    if isinstance(paths, list):
                        parts.extend(paths)
                    else:
                        parts.append(paths)
                return " ".join(parts)
        except Exception:
            pass
 
    # 4. pytest.ini / tox.ini — just means pytest is configured, use bare pytest
    for fname in ["pytest.ini", "tox.ini", "setup.cfg"]:
        if file_exists(sandbox, f"{repo_root}/{fname}"):
            return "pytest"
 
    # 5. Fallback
    return "pytest"


def verify_environment(sbx: Sandbox, repo_root: str) -> dict:
    """
    Run pytest --collect-only to verify env is correctly set up
    without actually executing tests.
    Returns dict with success bool, stdout, stderr.
    """
    collect_cmd = f"cd {repo_root} && pytest --collect-only -q"
    result = sbx.commands.run(collect_cmd, timeout=60)
 
    return {
        "success": result.exit_code == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


def setup_developer_environment(repo_url: str):
    # Check if repo is supported
    if any(repo in repo_url.lower() for repo in UNSUPPORTED_REPOS):
        raise ValueError(f"Repo {repo_url} requires system-level dependencies and is not supported.")

    print("☁️  Spinning up E2B Sandbox...")
    original_cwd = os.getcwd()
    import tempfile
    os.chdir(tempfile.gettempdir())  # move away from project root
    
    try:
        print(f"CWD before sandbox create: {os.getcwd()}")
        sandbox = Sandbox.create(timeout=3000)
    finally:
        os.chdir(original_cwd)
    git_hash = ""
    # ── 1. Clone & checkout ──────────────────────────────────────────────────
    print(f"📦 Cloning {repo_url}...")
    run_remote_command(sandbox, f"git clone --depth 1 {repo_url} workspace/repo", timeout=120)
    # run_remote_command(sandbox, f"cd workspace/repo && git fetch --depth 50 origin {git_hash}", timeout=60)
    # run_remote_command(sandbox, f"cd workspace/repo && git checkout {git_hash}", timeout=30)

    # Verify checkout landed
    verify = run_remote_command(sandbox, "cd workspace/repo && git rev-parse HEAD")
    print(f"✅ HEAD: {verify}")

    # ── 2. Detect Python Version & Provision Virtual Environment ──────────────
    print("🔍 Detecting Python version required by repository...")
    python_version = detect_python_version(sandbox, "workspace/repo")
    print(f"🎯 Target Python version: {python_version}")
    create_virtual_environment(sandbox, python_version)

    # ── 3. Resolve home dir & PYTHONPATH ─────────────────────────────────────
    home_result = run_remote_command(sandbox, "echo $HOME")
    home = "/root"  # fallback
    for line in home_result.splitlines():
        line = line.strip()
        if line.startswith("/"):
            home = line
            break

    pythonpath = f"{home}/workspace/repo:{home}/workspace/repo/src"

    # ── 4. Install dependencies ───────────────────────────────────────────────
    result = {
        "python_version": python_version,
        "install_command": None,
        "test_command": None,
        "smoke_test": {"success": False, "stdout": "", "stderr": ""},
        "ready": False,
    }

    print("📦 Installing dependencies inside workspace/repo...")
    result["install_command"] = detect_install_command(sandbox, "workspace/repo")
    
    install_cmd = (
        f"cd workspace/repo && "
        f"export PATH=\"{home}/.venv/bin:{home}/.local/bin:$PATH\" && "
        f"export PYTHONPATH=\"{pythonpath}:$PYTHONPATH\" && "
        f"{result['install_command']}"
    )
    install_result = run_remote_command(sandbox, install_cmd, timeout=300)
    print(f"📦 Install output:\n{install_result}")

    # Detect test command
    test_cmd = detect_test_command(sandbox, "workspace/repo")
    result["test_command"] = test_cmd

    return sandbox, result
    

    # ── 4. Resolve PYTHONPATH ─────────────────────────────────────────────────
    # src_check = run_remote_command(sandbox, f"[ -d {home}/workspace/repo/src ] && echo 'src' || echo 'root'")
    # pythonpath = f"{home}/workspace/repo/src" if "src" in src_check else f"{home}/workspace/repo"
    # print("PYTHONPATH", pythonpath)
    # # ── 5. Repo-specific overrides ────────────────────────────────────────────
    # pytest_flags = "--import-mode=importlib"  # default for all

    # if "django" in repo_url.lower():
    #     run_remote_command(sandbox, "pip install pytest-django", timeout=60)
    #     grep_result = run_remote_command(sandbox,
    #         "cd workspace/repo && grep -r 'DJANGO_SETTINGS_MODULE' --include='*.py' -l | head -1")
    #     settings_path = ""
    #     for line in grep_result.splitlines():
    #         line = line.strip()
    #         if line.endswith(".py"):
    #             settings_path = line
    #             break
    #     if settings_path:
    #         settings_module = settings_path.lstrip("./").replace("/", ".").replace(".py", "")
    #         pytest_flags = f"--import-mode=importlib --ds={settings_module}"
    #         print(f"🔧 Django settings: {settings_module}")
    #     else:
    #         print("⚠️  Could not find DJANGO_SETTINGS_MODULE — Django tests may fail")

    # elif "seaborn" in repo_url.lower():
    #     run_remote_command(sandbox, "pip install matplotlib scipy", timeout=60)

    # print(f"✅ Environment ready — PYTHONPATH={pythonpath}, pytest_flags={pytest_flags}")
    # return sandbox, pythonpath, pytest_flags


# Usage:
# my_sandbox = setup_developer_environment("https://github.com/psf/requests.git")
# Then pass `my_sandbox` into your Developer Agent's tool calls!


import asyncio
from turn_events import wait_for_grant
# from llm_utils import _set_status_sync   # wherever you put _set_status_sync
from db.models import RunStatus
from db.db_utils import _set_status_sync

def checkpoint_gate(agent_name: str, agent_output: str, run_id: str, loop: asyncio.AbstractEventLoop) -> dict:
    """
    Pauses the pipeline at a checkpoint and waits for the HTTP layer to respond.
    Writes AWAITING_MORE_TURNS status synchronously (safe from agent thread),
    then blocks on the turn_events registry until /continue is called.
    """
    print(f"\n{'='*50}")
    print(f"🛑 CHECKPOINT: {agent_name.upper()} TASK COMPLETE")
    print(f"{'='*50}")
    print("\n--- AGENT OUTPUT ---")
    print(agent_output.strip())
    print("--------------------\n")

    _set_status_sync(run_id, RunStatus.AWAITING_MORE_TURNS, loop)
    grant = wait_for_grant(run_id, timeout=3600.0)

    if grant is None:
        return {"status": "TAKEOVER", "feedback": None}

    if grant.extra_turns == 0 and grant.feedback is None:
        return {"status": "PROCEED", "feedback": None}
    elif grant.feedback:
        return {"status": "RETRY", "feedback": grant.feedback}
    else:
        return {"status": "PROCEED", "feedback": None}


def extract_final_plan(text):
    marker = "FINAL_PLAN:"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return None


# sandbox = setup_developer_environment('https://github.com/psf/black')
# res = sandbox.commands.run("cd workspace/repo && find . -type f -name '*.py' | grep -v __pycache__ | sort")
# print(res)
# readme = run_remote_command(sandbox, "cd workspace/repo && cat README.md 2>/dev/null | head -50")
# print(readme)




def get_issue(url):
    # 1. Regex Match
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    match = re.search(pattern, url)
    
    if not match:
        raise ValueError("Invalid GitHub issue URL")

    owner, repo, number = match.groups()
    headers = {"Authorization": f"token {os.environ.get('GITHUB_TOKEN', '')}"}

    # 2. API Request
    res = requests.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", )
    print("GITHUB REQUEST", res)
    # Check for 404s or connection errors
    if res.status_code != 200:
        raise Exception(f"Issue not found: {res.status_code}")

    issue = res.json()

    comments_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
        # headers=headers
    )
    if not comments_resp.ok:
        print(f"Comments fetch failed: {comments_resp.status_code} - {comments_resp.json()}")
        comments = []
    else:
        comments = comments_resp.json()
        if not isinstance(comments, list):
            print(f"Unexpected comments response: {comments}")
            comments = []
    
    # Format comments — include author and body, skip bot comments
    formatted_comments = []
    for c in comments:
        author = c["user"]["login"]
        # Skip common bots
        if any(bot in author.lower() for bot in ["bot", "stale", "codecov"]):
            continue
        body = c["body"].strip()
        if len(body) < 20:  # skip trivial comments like "same here"
            continue
        formatted_comments.append(f"@{author}:\n{body}")

    # 3. Return Dictionary (Mapping the JS object structure)
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "title": issue.get("title"),
        "body": issue.get("body"),
        "labels": [l["name"] for l in issue.get("labels", [])],
        "state": issue.get("state"),
        "comments": formatted_comments
    }


def format_issue_for_pipeline(issue_data: dict) -> str:
    """
    Combines issue body and comments into a single string for the planner.
    Keeps comments but caps length to avoid blowing up context.
    """
    parts = [
        f"ISSUE TITLE: {issue_data['title']}",
        f"\nISSUE DESCRIPTION:\n{issue_data['body']}",
    ]
    
    if issue_data.get("comments"):
        parts.append("\nDISCUSSION COMMENTS:")
        total_comment_chars = 0
        for comment in issue_data["comments"]:
            if total_comment_chars > 3000:
                parts.append("... (remaining comments truncated)")
                break
            parts.append(comment)
            total_comment_chars += len(comment)
    
    return "\n\n".join(parts)

def simple_clone(api_url: str, target_dir: str = "testRepos"):
    # 1. Nuke the folder if it exists (ignore_errors bypasses the Windows read-only lock)
    if os.path.exists(target_dir):
        subprocess.run(["rmdir", "/s", "/q", target_dir], shell=True)
    
    # 2. Fix the URL so Git can actually read it
    # Changes "https://api.github.com/repos/owner/repo" -> "https://github.com/owner/repo.git"
    git_url = api_url.replace("api.github.com/repos", "github.com") + ".git"
    
    # 3. Clone it
    subprocess.run(
    ["git", "clone", "--depth", "1", git_url, target_dir],
    check=True
)