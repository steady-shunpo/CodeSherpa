from repograph.ast_search import SymbolSearchIndex


def test_find_symbol_exact_match(indexed_repo):
    """Test exact symbol lookup by name."""
    repograph_id, db_session, _ = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    result = index.find_symbol("clean_string")

    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["name"] == "clean_string"
    assert match["kind"] == "function"
    assert "utils.py" in match["file"]
    assert "Strip whitespace and lowercase." in match["docstring"]
    assert result["did_you_mean"] == []


def test_find_symbol_did_you_mean(indexed_repo):
    """Test fuzzy fallback suggestions when an exact match is not found."""
    repograph_id, db_session, _ = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    # Typo in 'clean_string'
    result = index.find_symbol("clean_strng")

    assert len(result["matches"]) == 0
    assert "clean_string" in result["did_you_mean"]


def test_search_by_text(indexed_repo):
    """Test searching by concept/phrase across symbol names and docstrings."""
    repograph_id, db_session, _ = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    # Search for concept in docstring: 'HTTP headers'
    results = index.search_by_text("HTTP headers")

    assert len(results) > 0
    assert any(r["name"] == "parse_header" for r in results)


def test_list_symbols_in_file(indexed_repo):
    """Test listing all symbols in a specific file ordered by line number."""
    repograph_id, db_session, _ = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    symbols = index.list_symbols_in_file("utils.py")

    assert len(symbols) == 2
    names = [s["name"] for s in symbols]
    assert names == ["parse_header", "clean_string"]
    assert symbols[0]["start_line"] < symbols[1]["start_line"]


def test_get_callers_and_callees(indexed_repo):
    """Test querying call graph edges (who calls what, and what calls what)."""
    repograph_id, db_session, _ = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    # Who calls clean_string?
    callers = index.get_callers("clean_string")
    caller_names = {c["caller_name"] for c in callers}
    assert "parse_header" in caller_names
    assert "authenticate_user" in caller_names

    # What does parse_header call?
    callees = index.get_callees("parse_header")
    callee_names = {c["callee_name"] for c in callees}
    assert "clean_string" in callee_names


def test_resolve_stack_trace(indexed_repo):
    """Test mapping traceback file and line references to enclosing symbol definitions."""
    repograph_id, db_session, _ = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    traceback_sample = """Traceback (most recent call last):
  File "c:/fake/path/utils.py", line 5, in some_caller
    sanitized = clean_string(raw_header)
ValueError: invalid input
"""

    resolved = index.resolve_stack_trace(traceback_sample)
    assert len(resolved) == 1
    hit = resolved[0]
    assert hit["line"] == 5
    assert hit["enclosing_symbol"] is not None
    assert hit["enclosing_symbol"]["name"] == "parse_header"


def test_get_source(indexed_repo):
    """Test reading a targeted slice of lines from a source file."""
    repograph_id, db_session, repo_path = indexed_repo
    index = SymbolSearchIndex(db_session, repograph_id)

    # Read the first 4 lines of utils.py
    code = index.get_source(repo_path, "utils.py", 1, 4)
    assert '"""General utility functions."""' in code
    assert "def parse_header" in code
