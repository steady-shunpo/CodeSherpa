"""
symbol_search_index.py

The query side. This is what actually gets exposed to the coding agent
as tools. Every method returns a small, structured result -- never the
raw index -- so the agent's context window only ever holds what it
asked for.

Construct one instance per tool-call/turn, wrapping a plain SQLAlchemy
`Session` (e.g. one built the same way your bash/search_file dispatch
already opens whatever DB handle it uses). This class holds no
connection of its own -- creating a new instance is cheap. The
expensive resource (the connection pool) lives on the engine you build
the session from.

Lookup strategy, cheapest first:
    1. find_symbol(name)      -- exact match, falls back to did_you_mean
    2. search_by_text(query)  -- fuzzy match over names + docstrings
    3. semantic_search(query) -- embedding search (optional, pluggable)

Plus two utilities that don't require knowing a name at all:
    resolve_stack_trace(trace)   -- turn a traceback into symbol hits
    list_symbols_in_file(fname)  -- browse a file once you've narrowed to it

And the call graph:
    get_callers(name) / get_callees(name)
"""

from __future__ import annotations

import re
import uuid
from typing import Callable, Optional

from rapidfuzz import fuzz, process
from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from db.models import Call, Symbol


class SymbolSearchIndex:
    """
    Usage:

        session = SessionLocal()  # or however your project builds a sync Session
        index = SymbolSearchIndex(session, repograph_id)
        result = index.find_symbol("parse_config")
    """

    def __init__(
        self,
        session: Session,
        repograph_id: uuid.UUID,
        embed_fn: Optional[Callable[[str], list[float]]] = None,
    ):
        self.session = session
        self.repograph_id = repograph_id
        # Optional: pass in an embedding function (e.g. wrapping the
        # Anthropic/OpenAI embeddings API) to enable semantic_search.
        # Without it, semantic_search degrades gracefully to search_by_text.
        self._embed_fn = embed_fn
        self._embedding_cache: dict[int, list[float]] | None = None

    # -- tier 1: exact + did-you-mean ------------------------------------

    def find_symbol(self, name: str, limit: int = 5) -> dict:
        """
        Exact lookup by symbol name. If nothing matches, returns close
        spelling suggestions instead of an empty result, so a wrong
        guess is a hint rather than a dead end.
        """
        result = self.session.execute(
            select(Symbol)
            .where(Symbol.repograph_id == self.repograph_id, Symbol.name == name)
            .limit(limit)
        )
        rows = result.scalars().all()

        if rows:
            return {"matches": [self._symbol_to_dict(r) for r in rows], "did_you_mean": []}

        name_result = self.session.execute(
            select(distinct(Symbol.name)).where(Symbol.repograph_id == self.repograph_id)
        )
        all_names = [n for (n,) in name_result.all()]

        candidates = process.extract(name, all_names, scorer=fuzz.WRatio, limit=limit, score_cutoff=60)
        close = [n for n, _score, _ in candidates]
        return {"matches": [], "did_you_mean": close}

    # -- tier 2: fuzzy text search over names + docstrings ----------------

    def search_by_text(self, query: str, limit: int = 8, min_score: float = 55.0) -> list[dict]:
        """
        Fuzzy match over symbol names AND docstrings/signatures, for
        when the agent has a concept ("retry logic") rather than an
        exact name. Results below min_score are dropped rather than
        padding the result with unrelated symbols -- an empty list is
        more honest than four irrelevant guesses.
        """
        result = self.session.execute(
            select(Symbol).where(Symbol.repograph_id == self.repograph_id)
        )
        rows = result.scalars().all()

        scored = []
        for r in rows:
            haystack = f"{r.name} {r.signature or ''} {r.docstring or ''}"
            score = fuzz.WRatio(query, haystack)
            if score >= min_score:
                scored.append((score, r))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [self._symbol_to_dict(r) for _score, r in scored[:limit]]

    # -- tier 3: semantic search (pluggable) -------------------------------

    def semantic_search(self, query: str, limit: int = 8) -> list[dict]:
        """
        Embedding-based search over `name + signature + docstring`.
        Falls back to search_by_text if no embed_fn was configured --
        this method is meant to be a drop-in upgrade, not a hard
        dependency.
        """
        if self._embed_fn is None:
            return self.search_by_text(query, limit=limit)

        if self._embedding_cache is None:
            self._embedding_cache = self._build_embedding_cache()

        query_vec = self._embed_fn(query)
        scored = [
            (self._cosine_sim(query_vec, vec), symbol_id)
            for symbol_id, vec in self._embedding_cache.items()
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top_ids = [sid for _score, sid in scored[:limit]]

        if not top_ids:
            return []
        result = self.session.execute(
            select(Symbol).where(Symbol.repograph_id == self.repograph_id, Symbol.id.in_(top_ids))
        )
        rows_by_id = {r.id: self._symbol_to_dict(r) for r in result.scalars().all()}
        return [rows_by_id[i] for i in top_ids if i in rows_by_id]

    def _build_embedding_cache(self) -> dict[int, list[float]]:
        result = self.session.execute(
            select(Symbol).where(Symbol.repograph_id == self.repograph_id)
        )
        cache = {}
        for r in result.scalars().all():
            text = f"{r.name} {r.signature or ''} {r.docstring or ''}"
            cache[r.id] = self._embed_fn(text)
        return cache

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        return dot / (norm_a * norm_b + 1e-9)

    # -- stack trace resolution --------------------------------------------

    _TRACE_LINE_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')

    def resolve_stack_trace(self, trace_text: str) -> list[dict]:
        """
        Pull file:line references out of a Python-style traceback and
        map each one to its enclosing function/class definition, so
        the agent gets "here's the function that failed" instead of
        a bare line number.
        """
        results = []
        for match in self._TRACE_LINE_RE.finditer(trace_text):
            file_hint = match.group("file")
            line = int(match.group("line"))
            symbol = self._find_enclosing_symbol(file_hint, line)
            results.append({
                "file": file_hint,
                "line": line,
                "enclosing_symbol": symbol,
            })
        return results

    def _find_enclosing_symbol(self, file_hint: str, line: int) -> Optional[dict]:
        # file_hint from a traceback is often an absolute path; match on suffix.
        result = self.session.execute(
            select(Symbol)
            .where(
                Symbol.repograph_id == self.repograph_id,
                Symbol.start_line <= line,
                Symbol.end_line >= line,
            )
        )
        candidates = [r for r in result.scalars().all() if file_hint.endswith(r.file)]
        if not candidates:
            return None
        # smallest enclosing range wins (innermost function over outer class)
        best = min(candidates, key=lambda r: r.end_line - r.start_line)
        return self._symbol_to_dict(best)

    # -- browsing ------------------------------------------------------------

    def list_symbols_in_file(self, fname: str) -> list[dict]:
        """Once narrowed to a file, list what's defined in it."""
        result = self.session.execute(
            select(Symbol)
            .where(Symbol.repograph_id == self.repograph_id, Symbol.file == fname)
            .order_by(Symbol.start_line)
        )
        return [self._symbol_to_dict(r) for r in result.scalars().all()]

    # -- call graph ------------------------------------------------------------

    def get_callers(self, name: str) -> list[dict]:
        """Who calls this symbol -- use before changing a function's contract."""
        result = self.session.execute(
            select(Call.caller_name, Call.file, Call.line)
            .distinct()
            .where(Call.repograph_id == self.repograph_id, Call.callee_name == name)
        )
        return [
            {"caller_name": caller_name, "file": file, "line": line}
            for caller_name, file, line in result.all()
        ]

    def get_callees(self, name: str) -> list[dict]:
        """What this symbol calls -- use to trace a bug upstream."""
        result = self.session.execute(
            select(Call.callee_name)
            .distinct()
            .where(Call.repograph_id == self.repograph_id, Call.caller_name == name)
        )
        return [{"callee_name": callee_name} for (callee_name,) in result.all()]

    # -- retrieving actual source once a symbol is located ----------------------

    def get_source(self, repo_root: str, file: str, start_line: int, end_line: int) -> str:
        """Read only the needed byte range -- never the whole file. Not a DB op."""
        from pathlib import Path
        path = Path(repo_root) / file
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[start_line - 1:end_line])

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _symbol_to_dict(row: Symbol) -> dict:
        return {
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "file": row.file,
            "start_line": row.start_line,
            "end_line": row.end_line,
            "signature": row.signature,
            "docstring": row.docstring,
        }