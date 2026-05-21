import re
from tools import read_remote_file, run_remote_command, edit_remote_file, write_remote_file, search_repo_advanced

TOOL_PATTERNS = {
    "read":      re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "bash":      re.compile(r'(?:ACTION:\s*)?run_bash_command\(\s*"((?:[^"\\]|\\.)*)"\s*\)'),
    "searchf":    re.compile(r'(?:ACTION:\s*)?search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "edit": re.compile(r'(?:ACTION:\s*)?edit_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*'
        r'(?:\|\|\||```(?:\w+)?)\s*(.*?)(?:\|\|\||```)',re.DOTALL),
    "write": re.compile(r'(?:ACTION:\s*)?write_file\(\s*"([^"]+)"\s*\)\s*'
        r'(?:\|\|\||```(?:\w+)?)\s*(.*?)(?:\|\|\||```)',re.DOTALL),
    "read_bulk": re.compile(r'(?:ACTION:\s*)?read_files_bulk\(\s*\[(.*?)\]\s*\)', re.DOTALL),
    "reset_file":re.compile(r'(?:ACTION:\s*)?reset_file\(\s*"([^"]+)"\s*\)'),
    "search": re.compile(r'(?:ACTION:\s*)?search_repo\(\s*["\']([^"\']+)["\']\s*\)',),
}

def parse_and_execute(agent_reply: str, sandbox) -> tuple[str, str]:
    """
    Tries all tool patterns and executes the first match.
    Returns (tool_name, observation).
    """
    if m := TOOL_PATTERNS["edit"].search(agent_reply):
        fp, start, end, code = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        print(f"🔧 edit_file: {fp} lines {start}-{end}")
        return "edit_file", edit_remote_file(sandbox, fp, start, end, code)

    if m := TOOL_PATTERNS["write"].search(agent_reply):
        fp, code = m.group(1), m.group(2)

        check = run_remote_command(sandbox, f"test -f workspace/repo/{fp} && echo EXISTS || echo NEW")
        if "EXISTS" in check:
            return "write_file", (
                f"ERROR: '{fp}' already exists. write_file would delete all existing content.\n"
                f"To create a new file: choose a unique name like 'tests/test_identity_bug.py'"
            )

        print(f"🔧 write_file: {fp}")
        return "write_file", write_remote_file(sandbox, fp, code)

    if m := TOOL_PATTERNS["read_bulk"].search(agent_reply):
        filepaths = re.findall(r'"([^"]+)"', m.group(1))
        results = []
        for fp in filepaths:
            content = read_remote_file(sandbox, fp, 1, 99999)
            results.append(f"=== {fp} ===\n{content}")
        return "read_files_bulk", "\n\n".join(results)

    if m := TOOL_PATTERNS["read"].search(agent_reply):
        fp, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if end == -1:
            wc = run_remote_command(sandbox, f"wc -l workspace/repo/{fp}")
            try:
                end = int(wc.strip().split()[0])
            except (ValueError, IndexError):
                end = 99999
        print(f"🔧 read_file: {fp} lines {start}-{end}")
        return "read_file", read_remote_file(sandbox, fp, start, end)

    if m := TOOL_PATTERNS["bash"].search(agent_reply):
        cmd = m.group(1).replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
        print(f"🔧 bash: {cmd}")
        return "run_bash_command", run_remote_command(sandbox, f"cd workspace/repo && {cmd}")

    if m := TOOL_PATTERNS["searchf"].search(agent_reply):
        fp, term = m.group(1), m.group(2)
        print(f"🔧 search_file: {fp} for '{term}'")
        result = run_remote_command(sandbox, f"cd workspace/repo && grep -n '{term}' {fp}")
        return "search_file", result
    if m := TOOL_PATTERNS["reset_file"].search(agent_reply):
        fp = m.group(1)
        result = run_remote_command(sandbox, f"cd workspace/repo && git checkout {fp}")
        return "reset_file", f"Reset {fp} to original state. Start fresh."

    if m := TOOL_PATTERNS["read_no_lines"].search(agent_reply):
        fp = m.group(1)
        return "read_file", (
            f"ERROR: read_file requires line numbers.\n"
            f"You called: read_file(\"{fp}\")\n"
            f"Correct usage:\n"
            f"  read_file(\"{fp}\", 1, 50)     — read first 50 lines\n"
            f"  read_file(\"{fp}\", 1, -1)     — read entire file\n"
            f"Tip: use line_count(\"{fp}\") first to see how many lines the file has."
        )

    if m := TOOL_PATTERNS["line_count"].search(agent_reply):
        fp = m.group(1)
        print(f"📏 line_count: {fp}")
        try:
            # Run wc -l inside sandbox
            result = run_remote_command(sandbox, f"wc -l workspace/repo/{fp}")
            
            try:
                count = int(result.strip().split()[0])
            except (ValueError, IndexError):
                return "line_count", f"ERROR: Could not parse line count for {fp}"

            return "line_count", (
                f"{fp} has {count} lines.\n"
                f"To read the whole file: read_file(\"{fp}\", 1, {count})\n"
                f"To read first 50 lines: read_file(\"{fp}\", 1, 50)"
            )

        except Exception as e:
            return "line_count", f"ERROR: {e}"  
    
    if m := TOOL_PATTERNS["search"].search(agent_reply): 
        term = m.group(1) 
        print(f"🔍 search_repo: {term}") 
        return "search_repo", search_repo_advanced(term)
    return "none", ""