import re
from config import DEFAULT_RUNTIME_BINS

# ── Remote command helpers ────────────────────────────────────────────────────
# These wrap your existing sandbox API calls.
# Replace the bodies if your sandbox SDK has a different interface.

def run_remote_command(sandbox, command: str) -> str:
    try:
        result = sandbox.commands.run(command)
        NO_RESULTS_CMDS = ("grep", "find", "diff", "git diff")
        is_no_result = any(
            command.lstrip("cd workspace/repo && ").startswith(c)
            for c in NO_RESULTS_CMDS
        )
        if result.exit_code == 0:
            return result.stdout or ""
        elif result.exit_code == 1 and is_no_result:
            return "(no matches found)"
        else:
            return (
                f"ERROR: exit code {result.exit_code}.\n"
                f"stdout: {result.stdout or '(empty)'}\n"
                f"stderr: {result.stderr or '(empty)'}"
            )
    except Exception as e:
        return f"ERROR: {e}"
    
def read_remote_file(sandbox, filepath: str, start: int, end: int) -> str:
    command = f"cd workspace/repo && sed -n '{start},{end}p' {filepath}"
    return run_remote_command(sandbox, command)


def write_remote_file(sandbox, filepath: str, content: str) -> str:
    escaped = content.replace("'", "'\\''")
    command = f"cat > workspace/repo/{filepath} << 'HEREDOC'\n{content}\nHEREDOC"
    return run_remote_command(sandbox, command)


def edit_remote_file(sandbox, filepath: str, start: int, end: int, new_code: str) -> str:
    # Write new_code to a temp file then splice it in
    write_remote_file(sandbox, "__tmp_edit__.py", new_code)
    command = (
        f"cd workspace/repo && "
        f"head -n {start-1} {filepath} > __tmp_full__.py && "
        f"cat __tmp_edit__.py >> __tmp_full__.py && "
        f"tail -n +{end+1} {filepath} >> __tmp_full__.py && "
        f"mv __tmp_full__.py {filepath} && "
        f"rm -f __tmp_edit__.py"
    )
    return run_remote_command(sandbox, command)


# ── Tool pattern definitions ──────────────────────────────────────────────────

TOOL_PATTERNS = {
    "read":      re.compile(r'ACTION:\s*read_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)'),
    "bash":      re.compile(r'ACTION:\s*run_bash_command\(\s*"((?:[^"\\]|\\.)*)"\s*\)'),
    "search":    re.compile(r'ACTION:\s*search_file\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'),
    "edit":      re.compile(r'ACTION:\s*edit_file\(\s*"([^"]+)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*(?:\|\|\||```(?:\w+)?)\n(.*?)(?:\|\|\||```)', re.DOTALL),
    "write":     re.compile(r'ACTION:\s*write_file\(\s*"([^"]+)"\s*\)\s*(?:\|\|\||```(?:\w+)?)\n(.*?)(?:\|\|\||```)', re.DOTALL),
    "read_bulk": re.compile(r'ACTION:\s*read_files_bulk\(\s*\[(.*?)\]\s*\)', re.DOTALL),
    "reset_file": re.compile(r'ACTION:\s*reset_file\(\s*"([^"]+)"\s*\)'),
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

    if m := TOOL_PATTERNS["search"].search(agent_reply):
        fp, term = m.group(1), m.group(2)
        print(f"🔧 search_file: {fp} for '{term}'")
        result = run_remote_command(sandbox, f"cd workspace/repo && grep -n '{term}' {fp}")
        return "search_file", result
    if m := TOOL_PATTERNS["reset_file"].search(agent_reply):
        fp = m.group(1)
        result = run_remote_command(sandbox, f"cd workspace/repo && git checkout {fp}")
        return "reset_file", f"Reset {fp} to original state. Start fresh."

    return "none", ""


# ── Environment probe ─────────────────────────────────────────────────────────
REPO_ROOT = "workspace/repo"

def rc(sandbox, cmd: str) -> str:
    """Shorthand — always runs from repo root."""
    return run_remote_command(sandbox, f"cd {REPO_ROOT} && {cmd}")


def detect_custom_test_infrastructure(sandbox, env: dict) -> dict:
    """
    Detects repos that have custom test runners that can't be
    bypassed with a simple pytest/unittest call.
    """
    signals = {
        "has_runtests":   rc(sandbox, "ls tests/runtests.py 2>/dev/null"),
        "has_conftest":   rc(sandbox, "ls conftest.py tests/conftest.py 2>/dev/null"),
        "has_tox":        rc(sandbox, "ls tox.ini 2>/dev/null"),
        "settings_needed": rc(sandbox, 
            "grep -r 'django.setup\\|settings.configure\\|DJANGO_SETTINGS_MODULE' "
            "tests/ --include='*.py' 2>/dev/null | head -3"
        ),
    }
    
    is_complex = bool(
        signals["has_runtests"].strip() or 
        signals["settings_needed"].strip()
    )
    
    custom_runner = ""
    if signals["has_runtests"].strip():
        custom_runner = f"{env.get('python_bin', 'python3')} tests/runtests.py"
    
    return {
        "is_complex":     is_complex,
        "custom_runner":  custom_runner,
        "signals":        signals,
        "warning": (
            "⚠️ This repo uses custom test infrastructure. "
            "Tests MUST be added to existing test files and run with: "
            f"{custom_runner or env.get('test_command', 'pytest')}"
        ) if is_complex else ""
    }



def probe_environment(sandbox) -> tuple[str, dict]:

    # ── All checks run from repo root ─────────────────────────────────
    checks = {
        "dir":        run_remote_command(sandbox, f"pwd && ls {REPO_ROOT}"),
        "ci_config":  rc(sandbox,
            "cat .github/workflows/*.yml 2>/dev/null | head -60 "
            "|| cat .travis.yml 2>/dev/null | head -30 "
            "|| cat tox.ini 2>/dev/null | head -30"
        ),
        "makefile":   rc(sandbox, "grep -i 'test\\|lint\\|build' Makefile 2>/dev/null | head -15"),
        "ext_counts": rc(sandbox,
            "find . -type f | grep -v __pycache__ | grep -v .git "
            "| sed 's/.*\\.//' | sort | uniq -c | sort -rn | head -15"
        ),
    }

    lang_checks = {
        # ── Python ────────────────────────────────────────────────────
        "py_bin":      run_remote_command(sandbox, "which python3 2>/dev/null || which python 2>/dev/null"),
        "py_version":  run_remote_command(sandbox, "python3 --version 2>/dev/null || python --version 2>/dev/null"),
        "py_runner":   rc(sandbox, "cat tox.ini pytest.ini setup.cfg pyproject.toml 2>/dev/null | grep -E 'pytest|unittest|nose|testpaths|commands' | head -10"),
        "py_install":  rc(sandbox, "ls setup.py setup.cfg pyproject.toml requirements*.txt 2>/dev/null"),
        "py_conftest": rc(sandbox, "find . -name 'conftest.py' | head -5"),
        # Exclude manage.py files inside test fixtures/sample projects
        "py_manage":   rc(sandbox,
            "find . -name 'manage.py' "
            "| grep -v '/tests/' "
            "| grep -v '/test/' "
            "| grep -v 'sample' "
            "| grep -v 'example' "
            "| head -1"
        ),
        # Django-specific: check for runtests.py
        "py_runtests": rc(sandbox, "ls tests/runtests.py 2>/dev/null"),
        "py_ci_ver":   rc(sandbox,
            "grep -r 'python-version\\|PYTHON_VERSION\\|python_version' "
            ".github/workflows/*.yml .travis.yml tox.ini setup.cfg 2>/dev/null "
            "| grep -oE '[0-9]+\\.[0-9]+' | head -1"
        ),

        # ── JavaScript ────────────────────────────────────────────────
        "js_version":  run_remote_command(sandbox, "node --version 2>/dev/null && npm --version 2>/dev/null"),
        "js_scripts":  rc(sandbox, "cat package.json 2>/dev/null | grep -A20 '\"scripts\"'"),

        # ── Go ────────────────────────────────────────────────────────
        "go_version":  run_remote_command(sandbox, "go version 2>/dev/null"),
        "go_mod":      rc(sandbox, "cat go.mod 2>/dev/null | head -5"),

        # ── Java ──────────────────────────────────────────────────────
        "java_build":  rc(sandbox, "ls pom.xml build.gradle 2>/dev/null"),

        # ── Rust ──────────────────────────────────────────────────────
        "rust_cargo":  rc(sandbox, "cat Cargo.toml 2>/dev/null | head -5"),

        # ── Ruby ──────────────────────────────────────────────────────
        "ruby_build":  rc(sandbox, "ls Gemfile Rakefile 2>/dev/null"),

        # ── C++ ───────────────────────────────────────────────────────
        "cmake":       rc(sandbox, "ls CMakeLists.txt 2>/dev/null"),
    }

    results = {}
    for key, result in {**checks, **lang_checks}.items():
        results[key] = "" if result.startswith("ERROR:") else result
        if result.startswith("ERROR:"):
            print(f"⚠️  Probe '{key}' failed (skipping): {result[:80]}")

    ext = results["ext_counts"].lower()

    # ── Detect language ───────────────────────────────────────────────
    language = "unknown"
    if results["py_bin"].strip() and ("py" in ext or results["py_manage"].strip() or results["py_runtests"].strip()):
        language = "python"
    elif results["js_version"].strip() and results["js_scripts"].strip():
        language = "javascript"
    elif results["go_version"].strip() and results["go_mod"].strip():
        language = "go"
    elif results["java_build"].strip() and "pom.xml" in results["java_build"]:
        language = "java"
    elif results["rust_cargo"].strip():
        language = "rust"
    elif results["ruby_build"].strip():
        language = "ruby"
    elif results["cmake"].strip():
        language = "cpp"

    env = {"language": language}

    # ── Python setup ──────────────────────────────────────────────────
    if language == "python":
        # Get correct python binary from CI config
        py_bin = results["py_bin"].strip().splitlines()[0] if results["py_bin"].strip() else "python3"

        ci_version = results["py_ci_ver"].strip()
        if ci_version:
            print(f"📋 CI requires Python {ci_version}")
            candidate = f"python{ci_version}"
            check = run_remote_command(sandbox, f"which {candidate} 2>/dev/null")
            if check.strip() and not check.startswith("ERROR:"):
                py_bin = candidate
                print(f"✅ Using {py_bin}")
            else:
                print(f"📦 Installing Python {ci_version}...")
                run_remote_command(sandbox,
                    f"apt-get install -y python{ci_version} python{ci_version}-dev 2>&1 | tail -3"
                )
                check = run_remote_command(sandbox, f"which {candidate} 2>/dev/null")
                if check.strip() and not check.startswith("ERROR:"):
                    py_bin = candidate
                    print(f"✅ Installed {py_bin}")
                else:
                    print(f"⚠️ Could not install Python {ci_version}, using {py_bin}")
        else:
            print(f"⚠️ No CI Python version found, using system default: {py_bin}")

        env["python_bin"]  = py_bin
        env["pkg_manager"] = "pip"
        # install_cmd MUST cd to repo root
        env["install_cmd"] = f"cd {REPO_ROOT} && {py_bin} -m pip install -e . --quiet 2>&1 | tail -5"

        # ── Determine test command in priority order ───────────────────
        if results["py_runtests"].strip():
            # Django-style: has its own runtests.py
            env["test_command"] = f"{py_bin} tests/runtests.py"
            env["test_note"]    = "Django-style runtests.py — append test labels e.g. 'queries' to run specific tests"
        elif results["py_manage"].strip():
            manage = results["py_manage"].strip().splitlines()[0].lstrip("./")
            env["test_command"] = f"{py_bin} {manage} test"
        elif "tox" in results["py_runner"]:
            env["test_command"] = "tox"
        elif "pytest" in results["py_runner"] or "pytest" in results["py_install"]:
            env["test_command"] = f"{py_bin} -m pytest"
        else:
            env["test_command"] = f"{py_bin} -m pytest"

    elif language == "javascript":
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "npm"
        env["install_cmd"]  = f"cd {REPO_ROOT} && npm install --quiet 2>&1 | tail -5"
        scripts = results["js_scripts"]
        env["test_command"] = (
            "npm test"      if '"test"'  in scripts else
            "npx jest"      if "jest"    in scripts else
            "npx mocha"     if "mocha"   in scripts else
            "npm test"
        )

    elif language == "go":
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "go modules"
        env["install_cmd"]  = f"cd {REPO_ROOT} && go mod download 2>&1 | tail -5"
        env["test_command"] = "go test ./..."

    elif language == "java":
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "maven"
        env["install_cmd"]  = f"cd {REPO_ROOT} && mvn install -DskipTests --quiet 2>&1 | tail -5"
        env["test_command"] = "mvn test"

    elif language == "rust":
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "cargo"
        env["install_cmd"]  = f"cd {REPO_ROOT} && cargo build --quiet 2>&1 | tail -5"
        env["test_command"] = "cargo test"

    elif language == "ruby":
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "bundler"
        env["install_cmd"]  = f"cd {REPO_ROOT} && bundle install --quiet 2>&1 | tail -5"
        env["test_command"] = "bundle exec rake test"

    elif language == "cpp":
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "cmake"
        env["install_cmd"]  = f"cd {REPO_ROOT} && cmake -B build . 2>&1 | tail -5"
        env["test_command"] = "cmake --build build --target test"

    else:
        env["python_bin"]   = "N/A"
        env["pkg_manager"]  = "unknown"
        env["install_cmd"]  = f"echo 'ERROR: unknown package manager'"
        env["test_command"] = (
            "make test" if results["makefile"].strip()
            else "echo 'ERROR: could not determine test command'"
        )

    # ── Install dependencies ──────────────────────────────────────────
    print(f"📦 Language: {language}. Installing dependencies...")
    install_output = run_remote_command(sandbox, env["install_cmd"])
    env["install_output"] = install_output

    # ── Build summary ─────────────────────────────────────────────────
    test_note = env.get("test_note", "")
    summary = f"""
ENVIRONMENT:
============
Language:      {language}
Python binary: {env.get('python_bin', 'N/A')}  ← USE THIS EXACT BINARY
Test command:  {env['test_command']}
{f"Test note:     {test_note}" if test_note else ""}
Package mgr:   {env['pkg_manager']}
Working dir:   {REPO_ROOT}  ← ALL COMMANDS RUN FROM HERE

CI config:
{results['ci_config'].strip() or '(none found)'}

Dependency install:
{install_output[:300]}

File breakdown:
{results['ext_counts'].strip()}

RULES:
- You are already in {REPO_ROOT}. NEVER use cd in your commands.
- Run tests with: {env['test_command']}
- Python binary: {env.get('python_bin', 'N/A')} — do not use 'python' or 'python3' directly
"""
    return summary, env


# ── Repo context ──────────────────────────────────────────────────────────────

def build_repo_context(sandbox, architect_plan: str) -> str:
    tree = run_remote_command(sandbox,
        "cd workspace/repo && find . -type f -name '*.py' -o -name '*.js' -o -name '*.go' "
        "-o -name '*.java' -o -name '*.rs' | grep -v __pycache__ | grep -v node_modules | sort"
    )

    keywords = re.findall(r'\b([a-z_][a-z0-9_]{2,}|[A-Z][a-zA-Z0-9]{2,})\b', architect_plan)
    keywords = list(set(keywords))[:20]

    if keywords:
        pattern = '|'.join(re.escape(k) for k in keywords)
        symbols = run_remote_command(sandbox,
            f"cd workspace/repo && grep -rn '^def \\|^class \\|^func \\|^pub fn ' "
            f"--include='*.py' --include='*.go' --include='*.rs' "
            f"| grep -E '({pattern})' | grep -v __pycache__"
        )
    else:
        symbols = run_remote_command(sandbox,
            "cd workspace/repo && grep -rn '^def \\|^class \\|^func \\|^pub fn ' "
            "--include='*.py' --include='*.go' --include='*.rs' | grep -v __pycache__"
        )

    if len(symbols) > 8000:
        symbols = symbols[:8000] + "\n... (truncated — use search_file for more)"

    test_files = run_remote_command(sandbox,
        "cd workspace/repo && find . -name 'test_*.py' -o -name '*_test.py' "
        "-o -name '*_test.go' -o -name '*.test.js' | sort"
    )

    return f"""
CODEBASE SNAPSHOT:
==================
FILE TREE:
{tree}

RELEVANT SYMBOLS (with line numbers):
{symbols}

TEST FILES:
{test_files}
"""