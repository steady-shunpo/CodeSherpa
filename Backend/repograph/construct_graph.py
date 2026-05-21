# This file is adapted from the following sources:
# RepoMap: https://github.com/paul-gauthier/aider/blob/main/aider/repomap.py
# Agentless: https://github.com/OpenAutoCoder/Agentless/blob/main/get_repo_structure/get_repo_structure.py
# grep-ast: https://github.com/paul-gauthier/grep-ast

import colorsys
import os
import random
import sys
import re
import warnings
from collections import defaultdict, namedtuple
from pathlib import Path
import builtins
import inspect
import networkx as nx
from grep_ast import TreeContext, filename_to_lang
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from tqdm import tqdm
import ast
import pickle
import json
from copy import deepcopy

# Optional: only used for Python structure parsing
try:
    from utils import create_structure
    HAS_CREATE_STRUCTURE = True
except ImportError:
    HAS_CREATE_STRUCTURE = False

warnings.simplefilter("ignore", category=FutureWarning)
from tree_sitter_languages import get_language, get_parser

Tag = namedtuple("Tag", "rel_fname fname line name kind category info".split())


def _validate_queries(queries: dict) -> dict:
    """
    Compile every SCM query against its language at import time.
    Invalid queries are dropped with a warning so the program never
    crashes mid-run with a NameError from tree-sitter.
    """
    valid = {}
    for lang, scm in queries.items():
        try:
            language = get_language(lang)
            language.query(scm)
            valid[lang] = scm
        except Exception as exc:
            print(
                f"[construct_graph] WARNING: query for '{lang}' failed validation "
                f"and will be skipped.\n  Reason: {exc}",
                file=sys.stderr,
            )
    return valid


# ---------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------

# Languages where we can rely on tree-sitter alone (no Python ast needed)
SUPPORTED_LANGS = {
    "python", "javascript", "typescript", "tsx", "jsx",
    "java", "c", "cpp", "c_sharp", "go", "rust", "ruby",
    "php", "kotlin", "swift", "scala", "bash", "lua",
}

# Map from tree-sitter lang name → file extensions (informational)
LANG_EXTENSIONS = {
    "python":     [".py"],
    "javascript": [".js", ".mjs", ".cjs"],
    "typescript": [".ts"],
    "tsx":        [".tsx"],
    "java":       [".java"],
    "c":          [".c", ".h"],
    "cpp":        [".cpp", ".cc", ".cxx", ".hpp", ".hh"],
    "c_sharp":    [".cs"],
    "go":         [".go"],
    "rust":       [".rs"],
    "ruby":       [".rb"],
    "php":        [".php"],
    "kotlin":     [".kt"],
    "swift":      [".swift"],
    "scala":      [".scala"],
    "bash":       [".sh", ".bash"],
    "lua":        [".lua"],
}

# Per-language tree-sitter tag queries.
# Each query captures:
#   @name.definition.class    – class / struct / interface / trait / enum definitions
#   @name.definition.function – function / method definitions
#   @name.reference.call      – call-sites (references)
LANG_QUERIES: dict[str, str] = {}

# ── Python ──────────────────────────────────────────────────────────────────
LANG_QUERIES["python"] = """
(class_definition
  name: (identifier) @name.definition.class) @definition.class

(function_definition
  name: (identifier) @name.definition.function) @definition.function

(call
  function: [
    (identifier) @name.reference.call
    (attribute attribute: (identifier) @name.reference.call)
  ]) @reference.call
"""

# ── JavaScript ───────────────────────────────────────────────────────────────
# Node types verified against tree-sitter-javascript grammar:
#   class_declaration, function_declaration, method_definition,
#   call_expression, member_expression, property_identifier
# NOTE: anonymous arrow_function / function nodes have no name field —
#       we capture them via their variable_declarator parent instead.
# "function" (not "function_expression") is the correct node type for
#   `const f = function() {}` in tree-sitter-javascript.
LANG_QUERIES["javascript"] = """
(class_declaration
  name: (identifier) @name.definition.class) @definition.class

(function_declaration
  name: (identifier) @name.definition.function) @definition.function

(method_definition
  name: (property_identifier) @name.definition.function) @definition.function

(variable_declarator
  name: (identifier) @name.definition.function
  value: (arrow_function)) @definition.function

(variable_declarator
  name: (identifier) @name.definition.function
  value: (function)) @definition.function

(call_expression
  function: [
    (identifier) @name.reference.call
    (member_expression
      property: (property_identifier) @name.reference.call)
  ]) @reference.call
"""

# ── TypeScript ────────────────────────────────────────────────────────────────
# tree-sitter-typescript adds: interface_declaration, type_alias_declaration,
# enum_declaration, abstract_class_declaration. Method/function nodes are same
# as JS. "function" is still the right type for function expressions.
LANG_QUERIES["typescript"] = """
(class_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(abstract_class_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(interface_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(enum_declaration
  name: (identifier) @name.definition.class) @definition.class

(function_declaration
  name: (identifier) @name.definition.function) @definition.function

(method_definition
  name: (property_identifier) @name.definition.function) @definition.function

(method_signature
  name: (property_identifier) @name.definition.function) @definition.function

(variable_declarator
  name: (identifier) @name.definition.function
  value: (arrow_function)) @definition.function

(variable_declarator
  name: (identifier) @name.definition.function
  value: (function)) @definition.function

(call_expression
  function: [
    (identifier) @name.reference.call
    (member_expression
      property: (property_identifier) @name.reference.call)
  ]) @reference.call
"""

# TSX is identical to TypeScript (same grammar, JSX extension enabled)
LANG_QUERIES["tsx"] = LANG_QUERIES["typescript"]
# grep-ast maps .jsx → javascript
LANG_QUERIES["jsx"] = LANG_QUERIES["javascript"]

# ── Java ─────────────────────────────────────────────────────────────────────
LANG_QUERIES["java"] = """
(class_declaration
  name: (identifier) @name.definition.class) @definition.class

(interface_declaration
  name: (identifier) @name.definition.class) @definition.class

(enum_declaration
  name: (identifier) @name.definition.class) @definition.class

(method_declaration
  name: (identifier) @name.definition.function) @definition.function

(constructor_declaration
  name: (identifier) @name.definition.function) @definition.function

(method_invocation
  name: (identifier) @name.reference.call) @reference.call
"""

# ── C ────────────────────────────────────────────────────────────────────────
LANG_QUERIES["c"] = """
(function_definition
  declarator: (function_declarator
    declarator: (identifier) @name.definition.function)) @definition.function

(call_expression
  function: (identifier) @name.reference.call) @reference.call
"""

# ── C++ ──────────────────────────────────────────────────────────────────────
LANG_QUERIES["cpp"] = """
(class_specifier
  name: (type_identifier) @name.definition.class) @definition.class

(struct_specifier
  name: (type_identifier) @name.definition.class) @definition.class

(function_definition
  declarator: [
    (function_declarator
      declarator: [(identifier)(field_identifier)] @name.definition.function)
    (pointer_declarator
      declarator: (function_declarator
        declarator: (identifier) @name.definition.function))
  ]) @definition.function

(call_expression
  function: [
    (identifier) @name.reference.call
    (field_expression field: (field_identifier) @name.reference.call)
  ]) @reference.call
"""

# ── C# ───────────────────────────────────────────────────────────────────────
LANG_QUERIES["c_sharp"] = """
(class_declaration
  name: (identifier) @name.definition.class) @definition.class

(interface_declaration
  name: (identifier) @name.definition.class) @definition.class

(method_declaration
  name: (identifier) @name.definition.function) @definition.function

(constructor_declaration
  name: (identifier) @name.definition.function) @definition.function

(invocation_expression
  expression: [
    (identifier) @name.reference.call
    (member_access_expression name: (identifier) @name.reference.call)
  ]) @reference.call
"""

# ── Go ───────────────────────────────────────────────────────────────────────
LANG_QUERIES["go"] = """
(type_declaration
  (type_spec
    name: (type_identifier) @name.definition.class)) @definition.class

(function_declaration
  name: (identifier) @name.definition.function) @definition.function

(method_declaration
  name: (field_identifier) @name.definition.function) @definition.function

(call_expression
  function: [
    (identifier) @name.reference.call
    (selector_expression field: (field_identifier) @name.reference.call)
  ]) @reference.call
"""

# ── Rust ─────────────────────────────────────────────────────────────────────
LANG_QUERIES["rust"] = """
(struct_item
  name: (type_identifier) @name.definition.class) @definition.class

(enum_item
  name: (type_identifier) @name.definition.class) @definition.class

(trait_item
  name: (type_identifier) @name.definition.class) @definition.class

(function_item
  name: (identifier) @name.definition.function) @definition.function

(call_expression
  function: [
    (identifier) @name.reference.call
    (field_expression field: (field_identifier) @name.reference.call)
    (scoped_identifier name: (identifier) @name.reference.call)
  ]) @reference.call
"""

# ── Ruby ─────────────────────────────────────────────────────────────────────
LANG_QUERIES["ruby"] = """
(class
  name: (constant) @name.definition.class) @definition.class

(module
  name: (constant) @name.definition.class) @definition.class

(method
  name: (identifier) @name.definition.function) @definition.function

(singleton_method
  name: (identifier) @name.definition.function) @definition.function

(call
  method: (identifier) @name.reference.call) @reference.call
"""

# ── PHP ──────────────────────────────────────────────────────────────────────
LANG_QUERIES["php"] = """
(class_declaration
  name: (name) @name.definition.class) @definition.class

(interface_declaration
  name: (name) @name.definition.class) @definition.class

(function_definition
  name: (name) @name.definition.function) @definition.function

(method_declaration
  name: (name) @name.definition.function) @definition.function

(function_call_expression
  function: (name) @name.reference.call) @reference.call

(member_call_expression
  name: (name) @name.reference.call) @reference.call
"""

# ── Kotlin ───────────────────────────────────────────────────────────────────
LANG_QUERIES["kotlin"] = """
(class_declaration
  (type_identifier) @name.definition.class) @definition.class

(object_declaration
  (type_identifier) @name.definition.class) @definition.class

(function_declaration
  (simple_identifier) @name.definition.function) @definition.function

(call_expression
  (simple_identifier) @name.reference.call) @reference.call
"""

# ── Swift ─────────────────────────────────────────────────────────────────────
LANG_QUERIES["swift"] = """
(class_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(struct_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(protocol_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(enum_declaration
  name: (type_identifier) @name.definition.class) @definition.class

(function_declaration
  name: (simple_identifier) @name.definition.function) @definition.function

(init_declaration) @definition.function

(call_expression
  function: (simple_identifier) @name.reference.call) @reference.call

(call_expression
  function: (explicit_member_expression
    (simple_identifier) @name.reference.call)) @reference.call
"""

# ── Scala ─────────────────────────────────────────────────────────────────────
LANG_QUERIES["scala"] = """
(class_definition
  name: (identifier) @name.definition.class) @definition.class

(object_definition
  name: (identifier) @name.definition.class) @definition.class

(trait_definition
  name: (identifier) @name.definition.class) @definition.class

(function_definition
  name: (identifier) @name.definition.function) @definition.function

(call_expression
  function: (identifier) @name.reference.call) @reference.call

(call_expression
  function: (field_expression
    (identifier) @name.reference.call)) @reference.call
"""

# ── Bash ──────────────────────────────────────────────────────────────────────
LANG_QUERIES["bash"] = """
(function_definition
  name: (word) @name.definition.function) @definition.function

(command
  name: (command_name (word) @name.reference.call)) @reference.call
"""

# ── Lua ───────────────────────────────────────────────────────────────────────
LANG_QUERIES["lua"] = """
(function_declaration
  name: (identifier) @name.definition.function) @definition.function

(function_declaration
  name: (dot_index_expression) @name.definition.function) @definition.function

(function_declaration
  name: (method_index_expression) @name.definition.function) @definition.function

(local_function
  name: (identifier) @name.definition.function) @definition.function

(function_call
  name: (identifier) @name.reference.call) @reference.call

(function_call
  name: (dot_index_expression) @name.reference.call) @reference.call

(function_call
  name: (method_index_expression) @name.reference.call) @reference.call
"""


# ---------------------------------------------------------------------------
# Validate all queries at import time — drops any with invalid node types
# ---------------------------------------------------------------------------
LANG_QUERIES = _validate_queries(LANG_QUERIES)


# ---------------------------------------------------------------------------
# Structure extraction: language-agnostic fallback via tree-sitter
# ---------------------------------------------------------------------------

def extract_structure_from_tree(tree, code: str, lang: str) -> dict:
    """
    Build a structure dict  {classes: [...], functions: [...]}
    purely from tree-sitter, for any language.
    Used as a fallback when create_structure() (Python-only) is unavailable
    or the file is not Python.
    """
    codelines = code.splitlines()

    query_scm = LANG_QUERIES.get(lang)
    if not query_scm:
        return {"classes": [], "functions": []}

    language = get_language(lang)
    query = language.query(query_scm)
    captures = list(query.captures(tree.root_node))

    classes: dict[str, dict] = {}
    functions: list[dict] = []

    # First pass — collect definitions
    for node, tag in captures:
        if tag == "name.definition.class":
            name = node.text.decode("utf-8")
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            # Walk up to the actual class body node for accurate line range
            parent = node.parent
            if parent:
                start = parent.start_point[0] + 1
                end = parent.end_point[0] + 1
            classes[name] = {
                "name": name,
                "start_line": start,
                "end_line": end,
                "methods": [],
                "text": codelines[start - 1: end],
            }

        elif tag == "name.definition.function":
            name = node.text.decode("utf-8")
            parent = node.parent
            start = parent.start_point[0] + 1 if parent else node.start_point[0] + 1
            end = parent.end_point[0] + 1 if parent else node.end_point[0] + 1
            functions.append({
                "name": name,
                "start_line": start,
                "end_line": end,
                "text": codelines[start - 1: end],
            })

    # Second pass — assign methods to classes by line containment
    standalone_functions = []
    for func in functions:
        assigned = False
        for cls in classes.values():
            if cls["start_line"] <= func["start_line"] <= cls["end_line"]:
                cls["methods"].append(func)
                assigned = True
                break
        if not assigned:
            standalone_functions.append(func)

    return {
        "classes": list(classes.values()),
        "functions": standalone_functions,
    }


def normalize_structure(s: dict) -> dict:
    classes = list(s.get("classes", []))
    functions = list(s.get("functions", []))

    # Go / Rust / C++ sometimes use "structs" key
    for struct in s.get("structs", []):
        classes.append({
            "name": struct.get("name"),
            "methods": struct.get("methods", []),
            "start_line": struct.get("start_line"),
            "end_line": struct.get("end_line"),
            "text": struct.get("text", []),
        })

    for cls in classes:
        cls.setdefault("methods", [])
        cls.setdefault("text", [])

    for func in functions:
        func.setdefault("text", [])

    return {"classes": classes, "functions": functions}


# ---------------------------------------------------------------------------
# Python-specific stdlib import analysis (unchanged, Python only)
# ---------------------------------------------------------------------------

def std_proj_funcs_python(code: str, fname: str):
    """Return (std_funcs, std_libs) for Python files by executing imports."""
    std_libs, std_funcs = [], []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return std_funcs, std_libs

    codelines = code.split("\n")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_statement = codelines[node.lineno - 1].strip()
            for alias in node.names:
                import_name = alias.name.split(".")[0]
                if import_name in fname:
                    continue
                try:
                    exec(import_statement)
                except Exception:
                    continue
                std_libs.append(alias.name)
                eval_name = alias.name if alias.asname is None else alias.asname
                std_funcs.extend(
                    [n for n, m in inspect.getmembers(eval(eval_name)) if callable(m)]
                )

        elif isinstance(node, ast.ImportFrom):
            import_statement = codelines[node.lineno - 1]
            if node.module is None:
                continue
            module_name = node.module.split(".")[0]
            if module_name in fname:
                continue
            if "(" in import_statement:
                for ln in range(node.lineno - 1, len(codelines)):
                    if ")" in codelines[ln]:
                        import_statement = "\n".join(codelines[node.lineno - 1: ln + 1])
                        break
            import_statement = import_statement.strip()
            try:
                exec(import_statement)
            except Exception:
                continue
            for alias in node.names:
                std_libs.append(alias.name)
                eval_name = alias.name if alias.asname is None else alias.asname
                if eval_name == "*":
                    continue
                std_funcs.extend(
                    [n for n, m in inspect.getmembers(eval(eval_name)) if callable(m)]
                )

    return std_funcs, std_libs


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class CodeGraph:
    warned_files: set = set()

    def __init__(
        self,
        map_tokens=1024,
        root=None,
        main_model=None,
        io=None,
        repo_content_prefix=None,
        verbose=False,
        max_context_window=None,
    ):
        self.io = io
        self.verbose = verbose

        if not root:
            root = os.getcwd()
        self.root = root

        self.max_map_tokens = map_tokens
        self.max_context_window = max_context_window
        self.repo_content_prefix = repo_content_prefix

        # Python-specific repo structure (best-effort)
        if HAS_CREATE_STRUCTURE:
            try:
                self.py_structure = create_structure(self.root)
            except Exception:
                self.py_structure = {}
        else:
            self.py_structure = {}

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_code_graph(self, other_files, mentioned_fnames=None, on_progress=None):
        if self.max_map_tokens <= 0:
            return [], nx.Graph()
        if not other_files:
            return [], nx.Graph()
        if not mentioned_fnames:
            mentioned_fnames = set()

        tags = self.get_tag_files(other_files, mentioned_fnames, on_progress=on_progress)
        code_graph = self.tag_to_graph(tags)
        return tags, code_graph

    def get_tag_files(self, other_files, mentioned_fnames=None, on_progress=None):
        try:
            return self.get_ranked_tags(other_files, mentioned_fnames or set(), on_progress=on_progress)
        except RecursionError:
            if self.io:
                self.io.tool_error("Disabling code graph, git repo too large?")
            self.max_map_tokens = 0
            return []

    def tag_to_graph(self, tags):
        G = nx.MultiDiGraph()
        for tag in tags:
            G.add_node(
                tag.name,
                category=tag.category,
                info=tag.info,
                fname=tag.fname,
                line=tag.line,
                kind=tag.kind,
            )

        for tag in tags:
            if tag.category == "class":
                for method_name in tag.info.split("\t"):
                    method_name = method_name.strip()
                    if method_name:
                        G.add_edge(tag.name, method_name, relation="has_method")

        tags_ref = [t for t in tags if t.kind == "ref"]
        tags_def = {t.name: t for t in tags if t.kind == "def"}
        for tag in tags_ref:
            if tag.name in tags_def:
                G.add_edge(tag.name, tags_def[tag.name].name, relation="calls")

        return G

    # ------------------------------------------------------------------ #
    # File helpers                                                         #
    # ------------------------------------------------------------------ #

    def get_rel_fname(self, fname):
        return os.path.relpath(fname, self.root)

    def get_mtime(self, fname):
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            if self.io:
                self.io.tool_error(f"File not found error: {fname}")
            return None

    def find_src_files(self, directory):
        if not os.path.isdir(directory):
            return [directory]
        src_files = []
        for root, dirs, files in os.walk(directory):
            # Skip hidden dirs and common noise directories
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in {"node_modules", "__pycache__", ".git", "venv", ".venv",
                              "dist", "build", "target", ".idea", ".vscode"}
            ]
            for file in files:
                src_files.append(os.path.join(root, file))
        return src_files

    def find_files(self, dirs):
        chat_fnames = []
        for fname in dirs:
            if Path(fname).is_dir():
                chat_fnames += self.find_src_files(fname)
            else:
                chat_fnames.append(fname)

        return [f for f in chat_fnames if filename_to_lang(f) is not None]

    # ------------------------------------------------------------------ #
    # Tag extraction                                                       #
    # ------------------------------------------------------------------ #

    def get_ranked_tags(self, other_fnames, mentioned_fnames, on_progress=None):
        tags_of_files = []
        personalization = {}
        fnames = sorted(set(other_fnames))
        personalize = 10 / max(len(fnames), 1)
        total_files = len(fnames)

        for index, fname in enumerate(tqdm(fnames)):
        
            if on_progress and total_files > 0:
                # Calculate percentage (0 to 100)
                percent_complete = int(((index + 1) / total_files) * 100)
                on_progress(percent_complete)
            if not Path(fname).is_file():
                if fname not in self.warned_files:
                    if self.io:
                        self.io.tool_error(f"Code graph can't include {fname}")
                    self.warned_files.add(fname)
                continue

            rel_fname = self.get_rel_fname(fname)
            if fname in mentioned_fnames:
                personalization[rel_fname] = personalize

            tags = list(self.get_tags(fname, rel_fname))
            tags_of_files.extend(tags)

        return tags_of_files

    def get_tags(self, fname, rel_fname):
        if self.get_mtime(fname) is None:
            return []
        return list(self.get_tags_raw(fname, rel_fname))

    def get_tags_raw(self, fname, rel_fname):  # noqa: C901
        lang = filename_to_lang(fname)
        if not lang:
            return

        # ── select query ────────────────────────────────────────────────
        query_scm = LANG_QUERIES.get(lang)
        if not query_scm:
            # Unsupported language — emit nothing but don't crash
            return

        # ── read file ───────────────────────────────────────────────────
        try:
            with open(str(fname), "r", encoding="utf-8", errors="replace") as f:
                code = f.read()
            with open(str(fname), "r", encoding="utf-8", errors="replace") as f:
                codelines = f.readlines()
        except OSError:
            return

        if not code.strip():
            return

        # ── Python-specific sanitisations ────────────────────────────────
        if lang == "python":
            code = code.replace("\ufeff", "")
            code = code.replace("constants.False", "_False")
            code = code.replace("constants.True", "_True")
            code = code.replace("False", "_False")
            code = code.replace("True", "_True")
            code = code.replace("DOMAIN\\username", "DOMAIN\\\\username")
            code = code.replace("Error, ", "Error as ")
            code = code.replace("Exception, ", "Exception as ")
            code = code.replace("print ", "yield ")
            pattern = r"except\s+\(([^,]+)\s+as\s+([^)]+)\):"
            code = re.sub(pattern, r"except (\1, \2):", code)
            code = code.replace("raise AttributeError as aname", "raise AttributeError")

        # ── parse with tree-sitter ───────────────────────────────────────
        try:
            language = get_language(lang)
            parser = get_parser(lang)
        except Exception:
            return

        tree = parser.parse(bytes(code, "utf-8"))

        # ── build structure (class/function index) ───────────────────────
        if lang == "python" and HAS_CREATE_STRUCTURE and self.py_structure:
            # Use the richer Python-specific structure when available
            ref_fname_lst = os.path.normpath(rel_fname).split(os.sep)
            s = deepcopy(self.py_structure)
            try:
                s = normalize_structure(s)
                for part in ref_fname_lst:
                    if part not in s:
                        s = None
                        break
                    s = s[part]
                    if s is None:
                        break
                if s:
                    s = normalize_structure(s)
                else:
                    s = extract_structure_from_tree(tree, code, lang)
                    s = normalize_structure(s)
            except Exception:
                s = extract_structure_from_tree(tree, code, lang)
                s = normalize_structure(s)
        else:
            # All non-Python languages use tree-sitter extraction
            s = extract_structure_from_tree(tree, code, lang)
            s = normalize_structure(s)

        structure_classes = {item["name"]: item for item in s["classes"]}
        structure_functions = {item["name"]: item for item in s["functions"]}
        structure_class_methods: dict[str, dict] = {}
        for cls in s["classes"]:
            for method in cls["methods"]:
                structure_class_methods[method["name"]] = method
        structure_all_funcs = {**structure_functions, **structure_class_methods}

        # ── stdlib / builtin filter (Python only) ───────────────────────
        if lang == "python":
            try:
                std_funcs, std_libs = std_proj_funcs_python(code, fname)
            except Exception:
                std_funcs, std_libs = [], []
            builtins_funs = set(dir(builtins) + dir(list) + dir(dict) + dir(set) + dir(str) + dir(tuple))
        else:
            std_funcs, std_libs = [], []
            builtins_funs = set()

        # ── run tree-sitter query ────────────────────────────────────────
        query = language.query(query_scm)
        captures = list(query.captures(tree.root_node))

        saw = set()
        for node, tag in captures:
            if tag.startswith("name.definition."):
                kind = "def"
            elif tag.startswith("name.reference."):
                kind = "ref"
            else:
                continue

            saw.add(kind)

            tag_name = node.text.decode("utf-8")

            # Filter stdlib / builtins for Python
            if tag_name in std_funcs or tag_name in std_libs or tag_name in builtins_funs:
                continue

            # Determine category
            cur_cdl = codelines[node.start_point[0]] if node.start_point[0] < len(codelines) else ""

            # Heuristic: if the tag maps to a known class, treat as class
            if tag_name in structure_classes:
                category = "class"
            elif "class " in cur_cdl or "struct " in cur_cdl or "interface " in cur_cdl or "trait " in cur_cdl:
                category = "class"
            else:
                category = "function"

            # ── build Tag ────────────────────────────────────────────────
            if category == "class":
                if tag_name not in structure_classes:
                    if kind == "ref":
                        yield Tag(
                            rel_fname=rel_fname, fname=fname,
                            name=tag_name, kind=kind,
                            category=category, info="",
                            line=[node.start_point[0], node.end_point[0]],
                        )
                    continue

                cls_info = structure_classes[tag_name]
                method_names = [m["name"] for m in cls_info.get("methods", [])]
                line_nums = (
                    [cls_info["start_line"], cls_info["end_line"]]
                    if kind == "def"
                    else [node.start_point[0], node.end_point[0]]
                )
                yield Tag(
                    rel_fname=rel_fname, fname=fname,
                    name=tag_name, kind=kind,
                    category=category,
                    info="\t".join(method_names),
                    line=line_nums,
                )

            else:  # function
                if kind == "def":
                    if tag_name not in structure_all_funcs:
                        # Fallback: emit tag with node-level info
                        parent = node.parent
                        start = parent.start_point[0] + 1 if parent else node.start_point[0] + 1
                        end = parent.end_point[0] + 1 if parent else node.end_point[0] + 1
                        yield Tag(
                            rel_fname=rel_fname, fname=fname,
                            name=tag_name, kind=kind,
                            category=category,
                            info="\n".join(codelines[start - 1: end]),
                            line=[start, end],
                        )
                        continue

                    func_info = structure_all_funcs[tag_name]
                    yield Tag(
                        rel_fname=rel_fname, fname=fname,
                        name=tag_name, kind=kind,
                        category=category,
                        info="\n".join(func_info.get("text", [])),
                        line=[func_info["start_line"], func_info["end_line"]],
                    )
                else:
                    yield Tag(
                        rel_fname=rel_fname, fname=fname,
                        name=tag_name, kind=kind,
                        category=category,
                        info="",
                        line=[node.start_point[0], node.end_point[0]],
                    )

        # ── pygments fallback for def-only languages (e.g. C headers) ───
        if "ref" not in saw and "def" in saw:
            try:
                lexer = guess_lexer_for_filename(fname, code)
                tokens = [tok[1] for tok in lexer.get_tokens(code) if tok[0] in Token.Name]
                for token in tokens:
                    yield Tag(
                        rel_fname=rel_fname, fname=fname,
                        name=token, kind="ref",
                        line=-1, category="function", info="",
                    )
            except ClassNotFound:
                pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def get_random_color():
    hue = random.random()
    r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1, 0.75)]
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


    

def build_and_save_repograph(dir_name: str, on_progress=None):
    code_graph = CodeGraph(root=dir_name)
    chat_fnames_new = code_graph.find_files([dir_name])
    print(f"Files found: {len(chat_fnames_new)}")
    if chat_fnames_new:
        print("Sample:", chat_fnames_new[:10])

    tags, G = code_graph.get_code_graph(chat_fnames_new, on_progress=on_progress)

    print("-" * 60)
    print(f"✅  Code graph built for: {dir_name}")
    print(f"   Nodes : {len(G.nodes)}")
    print(f"   Edges : {len(G.edges)}")
    print("-" * 60)

    # Persist
    graph_path = os.path.join(os.getcwd(), "graph.pkl")
    tags_path = os.path.join(os.getcwd(), "tags.jsonl")

    with open(graph_path, "wb") as f:
        pickle.dump(G, f)

    with open(tags_path, "w", encoding="utf-8") as f:
        for tag in tags:
            line = json.dumps({
                "fname": tag.fname,
                "rel_fname": tag.rel_fname,
                "line": tag.line,
                "name": tag.name,
                "kind": tag.kind,
                "category": tag.category,
                "info": tag.info,
            })
            f.write(line + "\n")

    print(f"📦  Graph  → {graph_path}")
    print(f"📋  Tags   → {tags_path}")

    return graph_path, tags_path




if __name__ == "__main__":
    dir_name = sys.argv[1]
    build_and_save_graph(dir_name)





