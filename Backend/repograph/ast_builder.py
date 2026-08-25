"""
ast_index_builder.py

Walks a repo, parses each source file with tree-sitter, and writes a
symbol-level index + call graph via the project's Session, matching
the existing SQLAlchemy pattern used elsewhere (e.g. the Run model).

Usage (inside a script/worker that already has a session):

    def index_repo(session: Session, repo_path: str, repograph_id: uuid.UUID):
        builder = ASTIndexBuilder(repo_path)
        await builder.build(session, repograph_id)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

# pyrefly: ignore [missing-import]
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session
from tree_sitter_languages import get_parser

from db.models import Call, Symbol

# ---------------------------------------------------------------------------
# Per-language config: which node types count as a "definition", how to
# pull the name/signature out of them, and which node type is a call site.
# Add a new language by adding an entry here -- no other code changes needed
# for the common case.
# ---------------------------------------------------------------------------

LANGUAGE_CONFIG = {
    "python": {
        "extensions": {".py"},
        "def_nodes": {
            "function_definition": "function",
            "class_definition": "class",
        },
        "call_node": "call",
        "call_function_field": "function",
    },
    "javascript": {
        "extensions": {".js", ".jsx", ".mjs"},
        "def_nodes": {
            "function_declaration": "function",
            "method_definition": "method",
            "class_declaration": "class",
        },
        "call_node": "call_expression",
        "call_function_field": "function",
    },
    "typescript": {
        "extensions": {".ts", ".tsx"},
        "def_nodes": {
            "function_declaration": "function",
            "method_definition": "method",
            "class_declaration": "class",
        },
        "call_node": "call_expression",
        "call_function_field": "function",
    },
}

EXT_TO_LANGUAGE = {
    ext: lang for lang, cfg in LANGUAGE_CONFIG.items() for ext in cfg["extensions"]
}

# Directories we never want to walk into.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", "site-packages",
}


@dataclass
class SymbolRecord:
    name: str
    kind: str          # "function" | "class" | "method"
    file: str            # path relative to repo root
    start_line: int
    end_line: int
    signature: str
    docstring: str


@dataclass
class CallRecord:
    caller_name: str    # name of the enclosing symbol (may be "" at module level)
    callee_name: str
    file: str
    line: int


class ASTIndexBuilder:
    """
    Parses a repository with tree-sitter and writes a symbol index +
    call graph via a Session, scoped to one repograph_id.

    This class only ever inserts into symbols/calls -- it assumes the
    repographs row already exists (created wherever repo_url/commit_sha
    are first recorded) and never touches that table itself.
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self._parsers: dict[str, "tree_sitter.Parser"] = {}

    # -- public API ---------------------------------------------------

    def build(self, session: Session, repograph_id: uuid.UUID) -> None:
        # Clear any prior index for this repograph_id first, so re-running
        # build() (e.g. after a repo update) doesn't leave stale/duplicate
        # rows behind.
        session.execute(delete(Call).where(Call.repograph_id == repograph_id))
        session.execute(delete(Symbol).where(Symbol.repograph_id == repograph_id))

        all_symbol_rows: list[dict] = []
        all_call_rows: list[dict] = []

        for file_path in self._iter_source_files():
            language = EXT_TO_LANGUAGE[file_path.suffix]
            try:
                source = file_path.read_bytes()
            except OSError:
                continue

            tree = self._get_parser(language).parse(source)
            rel_path = str(file_path.relative_to(self.repo_path))

            symbols, calls = self._extract_from_tree(
                tree.root_node, source, language, rel_path
            )
            all_symbol_rows.extend(
                {
                    "repograph_id": repograph_id,
                    "name": s.name,
                    "kind": s.kind,
                    "file": s.file,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "signature": s.signature,
                    "docstring": s.docstring,
                }
                for s in symbols
            )
            all_call_rows.extend(
                {
                    "repograph_id": repograph_id,
                    "caller_name": c.caller_name,
                    "callee_name": c.callee_name,
                    "file": c.file,
                    "line": c.line,
                }
                for c in calls
            )

        # Bulk insert in one shot per table -- much faster than row-by-row
        # for a whole-repo parse, and keeps this to two round trips.
        if all_symbol_rows:
            session.execute(insert(Symbol), all_symbol_rows)
        if all_call_rows:
            session.execute(insert(Call), all_call_rows)

        session.commit()

    # -- file walking ---------------------------------------------------

    def _iter_source_files(self):
        for path in self.repo_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in EXT_TO_LANGUAGE:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path

    def _get_parser(self, language: str):
        if language not in self._parsers:
            self._parsers[language] = get_parser(language)
        return self._parsers[language]

    # -- extraction -------------------------------------------------------

    def _extract_from_tree(self, root_node, source: bytes, language: str, rel_path: str):
        cfg = LANGUAGE_CONFIG[language]
        symbols: list[SymbolRecord] = []
        calls: list[CallRecord] = []

        def text(node) -> str:
            return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

        def find_name(def_node) -> str | None:
            name_node = def_node.child_by_field_name("name")
            return text(name_node) if name_node else None

        def find_docstring(def_node) -> str:
            # Python convention: first statement in the body is a bare string.
            body = def_node.child_by_field_name("body")
            if body is None or body.child_count == 0:
                return ""
            first = body.children[0]
            if first.type == "expression_statement" and first.child_count:
                inner = first.children[0]
                if inner.type == "string":
                    raw = text(inner).strip("\"'")
                    return raw.strip().split("\n")[0][:200]  # first line, capped
            return ""

        def find_signature(def_node, kind: str) -> str:
            name = find_name(def_node) or ""
            params_node = def_node.child_by_field_name("parameters")
            params = text(params_node) if params_node else "()"
            prefix = "class " if kind == "class" else "def "
            return f"{prefix}{name}{params}"

        def collect_calls(node, caller_name: str):
            """Recursively find call sites inside a node's subtree."""
            if node.type == cfg["call_node"]:
                fn_node = node.child_by_field_name(cfg["call_function_field"])
                if fn_node is not None:
                    # For `obj.method()` grab the last segment (`method`).
                    callee = text(fn_node).split(".")[-1]
                    calls.append(CallRecord(
                        caller_name=caller_name,
                        callee_name=callee,
                        file=rel_path,
                        line=node.start_point[0] + 1,
                    ))
            for child in node.children:
                collect_calls(child, caller_name)

        def walk(node, enclosing_caller: str = ""):
            if node.type in cfg["def_nodes"]:
                kind = cfg["def_nodes"][node.type]
                name = find_name(node)
                if name:
                    symbols.append(SymbolRecord(
                        name=name,
                        kind=kind,
                        file=rel_path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        signature=find_signature(node, kind),
                        docstring=find_docstring(node),
                    ))
                    collect_calls(node, caller_name=name)
                    # Still recurse for nested defs (methods inside classes,
                    # closures), but don't re-collect calls twice.
                    for child in node.children:
                        walk(child, enclosing_caller=name)
                    return
            for child in node.children:
                walk(child, enclosing_caller=enclosing_caller)

        walk(root_node)
        return symbols, calls