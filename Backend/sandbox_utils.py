import re
from tools import read_remote_file, run_remote_command, format_symbol_result, edit_remote_file, write_remote_file, search_repo_advanced
from tools import read_local_file
import os
from config import AST_INDEX_DB_PATH

from repograph.ast_search import SymbolSearchIndex
from db.database import SessionLocal

TOOL_PATTERNS = {
    "read":      re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "bash":      re.compile(r'(?:ACTION:\s*)?run_bash_command\(\s*"((?:[^"\\]|\\.)*)"\s*\)'),
    "searchf":    re.compile(r'(?:ACTION:\s*)?search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "edit": re.compile(r'(?:ACTION:\s*)?edit_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)[^\n]*\n?'
        r'(?:\|\|\||```[^\n]*)\r?\n?(.*?)\r?\n?(?:\|\|\||```)', re.DOTALL),
    "write": re.compile(r'(?:ACTION:\s*)?write_file\(\s*"([^"]+)"\s*\)[^\n]*\n?'
        r'(?:\|\|\||```[^\n]*)\r?\n?(.*?)\r?\n?(?:\|\|\||```)', re.DOTALL),
    "read_bulk": re.compile(r'(?:ACTION:\s*)?read_files_bulk\(\s*\[(.*?)\]\s*\)', re.DOTALL),
    "reset_file":re.compile(r'(?:ACTION:\s*)?reset_file\(\s*"([^"]+)"\s*\)'),
    "search":        re.compile(r'(?:ACTION:\s*)?search_repo\(\s*"([^"]+)"\s*\)'),
    # "read_no_lines": re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*\)\s*(?:$|\n|__END__)'),
    "line_count":    re.compile(r'(?:ACTION:\s*)?line_count\(\s*"([^"]+)"\s*\)'),
    "list_symbols":  re.compile(r'(?:ACTION:\s*)?list_symbols\(\s*"([^"]+)"\s*\)'),
}

def _normalize_xml_tool_call(reply: str) -> str:
    """
    Normalizes XML-style tool calls (e.g. <tool_call><tool_name>read_file</tool_name>...)
    into standard ACTION: name(...) plain text format.
    """
    m_bash = re.search(r'<tool_name>run_bash_command</tool_name>\s*<(?:tool|args|cmd)>(.*?)</(?:tool|args|cmd)>', reply, re.DOTALL)
    if m_bash:
        cmd = m_bash.group(1).strip()
        return f'ACTION: run_bash_command("{cmd}")\n__END__'

    m_read = re.search(r'<tool_name>read_file</tool_name>\s*<(?:tool|path)>(.*?)</(?:tool|path)>\s*<(?:tool_call|lines|args)>(-?\d+)\s*,\s*(-?\d+)</(?:tool_call|lines|args)>', reply, re.DOTALL)
    if m_read:
        fp, start, end = m_read.group(1).strip(), m_read.group(2).strip(), m_read.group(3).strip()
        return f'ACTION: read_file("{fp}", {start}, {end})\n__END__'

    m_line_count = re.search(r'<tool_name>line_count</tool_name>\s*<(?:tool|path|tool_call)>(.*?)</(?:tool|path|tool_call)>', reply, re.DOTALL)
    if m_line_count:
        fp = m_line_count.group(1).strip()
        return f'ACTION: line_count("{fp}")\n__END__'

    m_search = re.search(r'<tool_name>search_file</tool_name>\s*<(?:tool|path)>(.*?)</(?:tool|path)>\s*<(?:term|args)>(.*?)</(?:term|args)>', reply, re.DOTALL)
    if m_search:
        fp, term = m_search.group(1).strip(), m_search.group(2).strip()
        return f'ACTION: search_file("{fp}", "{term}")\n__END__'

    m_edit = re.search(r'<tool_name>edit_file</tool_name>\s*<(?:tool|path)>(.*?)</(?:tool|path)>\s*<(?:lines|range|tool_call)>(-?\d+)\s*,\s*(-?\d+)</(?:lines|range|tool_call)>\s*<(?:code|content)>(.*?)</(?:code|content)>', reply, re.DOTALL)
    if m_edit:
        fp, start, end, code = m_edit.group(1).strip(), m_edit.group(2).strip(), m_edit.group(3).strip(), m_edit.group(4).strip()
        return f'ACTION: edit_file("{fp}", {start}, {end})\n|||\n{code}\n|||\n__END__'

    m_write = re.search(r'<tool_name>write_file</tool_name>\s*<(?:tool|path)>(.*?)</(?:tool|path)>\s*<(?:code|content)>(.*?)</(?:code|content)>', reply, re.DOTALL)
    if m_write:
        fp, code = m_write.group(1).strip(), m_write.group(2).strip()
        return f'ACTION: write_file("{fp}")\n|||\n{code}\n|||\n__END__'

    return reply


def parse_and_execute(agent_reply: str, sandbox, repograph_id) -> tuple[str, str]:
    """
    Tries all tool patterns and executes the first match.
    Returns (tool_name, observation).
    """
    agent_reply = _normalize_xml_tool_call(agent_reply)
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
        cmd = re.sub(r'^\s*cd\s+(?:workspace/repo|\./workspace/repo|repo)\s*(?:&&|;)\s*', '', cmd)
        print(f"🔧 bash: {cmd}")
        return "run_bash_command", run_remote_command(sandbox, f"cd workspace/repo && {cmd}")

    if m := TOOL_PATTERNS["searchf"].search(agent_reply):
        fp, term = m.group(1), m.group(2)
        fp = re.sub(r'^(?:workspace/repo/|\./workspace/repo/)', '', fp.strip())
        print(f"🔧 search_file: {fp} for '{term}'")
        result = run_remote_command(sandbox, f"cd workspace/repo && grep -n '{term}' {fp}")
        return "search_file", result

    if m := TOOL_PATTERNS["reset_file"].search(agent_reply):
        fp = m.group(1)
        fp = re.sub(r'^(?:workspace/repo/|\./workspace/repo/)', '', fp.strip())
        result = run_remote_command(sandbox, f"cd workspace/repo && git checkout {fp}")
        return "reset_file", f"Reset {fp} to original state. Start fresh."

    # if m := TOOL_PATTERNS["read_no_lines"].search(agent_reply):
    #     fp = m.group(1)
    #     return "read_file", (
    #         f"ERROR: read_file requires line numbers.\n"
    #         f"You called: read_file(\"{fp}\")\n"
    #         f"Correct usage:\n"
    #         f"  read_file(\"{fp}\", 1, 50)     — read first 50 lines\n"
    #         f"  read_file(\"{fp}\", 1, -1)     — read entire file\n"
    #         f"Tip: use line_count(\"{fp}\") first to see how many lines the file has."
    #     )

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
    
    # if m := TOOL_PATTERNS["search"].search(agent_reply): 
    #     term = m.group(1) 
    #     print(f"🔍 search_repo: {term}") 
    #     return "search_repo", search_repo_advanced(term)

    if m := TOOL_PATTERNS["search"].search(agent_reply):
        term = m.group(1)
        print(f"🔍 search_repo: {term}")
        with SessionLocal() as session:
            index = SymbolSearchIndex(session, repograph_id)
            res = index.find_symbol(term)
            return "search_repo", format_symbol_result(term, res)

    if m := TOOL_PATTERNS["list_symbols"].search(agent_reply):
        fp = m.group(1).strip()
        print(f"📋 list_symbols: {fp}")
        with SessionLocal() as session:
            index = SymbolSearchIndex(session, repograph_id)
            symbols = index.list_symbols_in_file(fp)
            if not symbols:
                return "list_symbols", f"No symbols found in '{fp}'."
            lines = [f"Symbols in `{fp}`:"]
            for s in symbols:
                sig = f" — `{s['signature']}`" if s.get('signature') else ""
                lines.append(f"- Lines {s['start_line']}-{s['end_line']}: {s['kind']} `{s['name']}`{sig}")
            return "list_symbols", "\n".join(lines)
        
    return "none", ""



# Architect uses local files, not a sandbox
ARCH_TOOL_PATTERNS = {
    "search":        re.compile(r'(?:ACTION:\s*)?search_repo\(\s*"([^"]+)"\s*\)'),
    "read":          re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "search_file":   re.compile(r'(?:ACTION:\s*)?search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "read_no_lines": re.compile(r'(?:ACTION:\s*)?read_file\(\s*"([^"]+)"\s*\)\s*(?:$|\n|__END__)'),
    "line_count":    re.compile(r'(?:ACTION:\s*)?line_count\(\s*"([^"]+)"\s*\)'),
    "list_symbols":  re.compile(r'(?:ACTION:\s*)?list_symbols\(\s*"([^"]+)"\s*\)'),
    "list_dir":      re.compile(r'(?:ACTION:\s*)?list_dir\(\s*"([^"]+)"\s*\)'),
    "find_files":    re.compile(r'(?:ACTION:\s*)?find_files\(\s*"([^"]+)"\s*\)'),
}
SAFE_COMMANDS = ("grep", "find", "cat", "head", "tail", "wc", "ls", "sed")

def _arch_parse_and_execute(agent_reply: str, _sandbox=None, repograph_id=None) -> tuple[str, str]:
    """Architect uses local tools only — no sandbox, no shell writes."""
    agent_reply = _normalize_xml_tool_call(agent_reply)
    agent_reply = re.sub(r'```\w*\n(.*?)```', r'\1', agent_reply, flags=re.DOTALL)
    agent_reply = agent_reply.strip()

    if m := ARCH_TOOL_PATTERNS["search"].search(agent_reply):
        term = m.group(1)
        print(f"🔍 search_repo: {term}")
        with SessionLocal() as session:
            index = SymbolSearchIndex(session, repograph_id)
            res = index.find_symbol(term)
            output = format_symbol_result(term, res)
            if "No exact match for" in output:
                # Also check matching file paths in testRepos
                matching_files = []
                repo_root = "testRepos"
                if os.path.exists(repo_root):
                    for root, _, files in os.walk(repo_root):
                        for f in files:
                            if term.lower() in f.lower():
                                rel_path = os.path.relpath(os.path.join(root, f), repo_root).replace("\\", "/")
                                matching_files.append(rel_path)
                                if len(matching_files) >= 5:
                                    break
                        if len(matching_files) >= 5:
                            break
                if matching_files:
                    output += f"\n\nMatching file paths in repo:\n" + "\n".join(f"- {mf}" for mf in matching_files)
            return "search_repo", output

    if m := ARCH_TOOL_PATTERNS["list_symbols"].search(agent_reply):
        fp = m.group(1).strip()
        print(f"📋 list_symbols: {fp}")
        with SessionLocal() as session:
            index = SymbolSearchIndex(session, repograph_id)
            symbols = index.list_symbols_in_file(fp)
            if not symbols:
                return "list_symbols", f"No symbols found in '{fp}'."
            lines = [f"Symbols in `{fp}`:"]
            for s in symbols:
                sig = f" — `{s['signature']}`" if s.get('signature') else ""
                lines.append(f"- Lines {s['start_line']}-{s['end_line']}: {s['kind']} `{s['name']}`{sig}")
            return "list_symbols", "\n".join(lines)

    if m := ARCH_TOOL_PATTERNS["list_dir"].search(agent_reply) or (m := ARCH_TOOL_PATTERNS["find_files"].search(agent_reply)):
        dir_or_pattern = m.group(1).strip()
        print(f"📂 list_dir/find_files: {dir_or_pattern}")
        target_path = os.path.join("testRepos", dir_or_pattern)
        if os.path.isdir(target_path):
            entries = os.listdir(target_path)[:30]
            return "list_dir", f"Contents of `{dir_or_pattern}`:\n" + "\n".join(f"- {e}" for e in entries)
        else:
            # Pattern search
            import fnmatch
            matches = []
            repo_root = "testRepos"
            if os.path.exists(repo_root):
                for root, _, files in os.walk(repo_root):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), repo_root).replace("\\", "/")
                        if fnmatch.fnmatch(f, dir_or_pattern) or fnmatch.fnmatch(rel, dir_or_pattern) or dir_or_pattern.lower() in f.lower():
                            matches.append(rel)
                            if len(matches) >= 20:
                                break
                    if len(matches) >= 20:
                        break
            return "find_files", f"Matching files for `{dir_or_pattern}`:\n" + "\n".join(f"- {m}" for m in matches) if matches else f"No files matching `{dir_or_pattern}` found."

    if m := ARCH_TOOL_PATTERNS["read"].search(agent_reply):
        fp, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if end == -1:
            end = 99999
        print(f"📖 read_file: {fp} lines {start}-{end}")
        return "read_file", read_local_file(fp, start, end)

    if m := ARCH_TOOL_PATTERNS["search_file"].search(agent_reply):
        fp, term = m.group(1), m.group(2)
        print(f"🔎 search_file: {fp} for '{term}'")
        try:
            filepath = os.path.join("testRepos", fp)
            results = []
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, start=1):
                    if term in line:
                        results.append(f"{i}:{line.rstrip()}")
            return "search_file", "\n".join(results) if results else "(no matches found)"
        except FileNotFoundError:
            return "search_file", f"ERROR: File not found: {fp}"
        except Exception as e:
            return "search_file", f"ERROR: {e}"

    if m := ARCH_TOOL_PATTERNS["line_count"].search(agent_reply):
        fp = m.group(1)
        print(f"📏 line_count: {fp}")
        try:
            filepath = os.path.join("testRepos", fp)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                count = sum(1 for _ in f)
            return "line_count", (
                f"{fp} has {count} lines.\n"
                f"To read the whole file: read_file(\"{fp}\", 1, {count})\n"
                f"To read first 50 lines: read_file(\"{fp}\", 1, 50)"
            )
        except FileNotFoundError:
            return "line_count", f"ERROR: File not found: {fp}"
        except Exception as e:
            return "line_count", f"ERROR: {e}"

    if "run_bash_command" in agent_reply:
        return "none", (
            "ERROR: run_bash_command is not available to the Architect.\n"
            "Use search_file(\"path\", \"term\") to search within a specific file.\n"
            "Use search_repo(\"symbol_name\") to find where a function or class is defined.\n"
            "Use list_symbols(\"path\") to list definitions inside a file.\n"
            "Use read_file(\"path\", start, end) to read a file at known line numbers."
        )

    if m := ARCH_TOOL_PATTERNS["read_no_lines"].search(agent_reply):
        fp = m.group(1)
        return "read_file", (
            f"ERROR: read_file requires line numbers.\n"
            f"You called: read_file(\"{fp}\")\n"
            f"Correct usage:\n"
            f"  read_file(\"{fp}\", 1, 50)     — read first 50 lines\n"
            f"  read_file(\"{fp}\", 1, -1)     — read entire file\n"
            f"Tip: use line_count(\"{fp}\") first to see how many lines the file has."
        )

    return "none", ""