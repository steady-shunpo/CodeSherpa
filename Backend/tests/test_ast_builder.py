import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select
from db.models import Call, Repograph, Symbol
from repograph.ast_builder import ASTIndexBuilder


def test_extract_python_symbols(db_session, sample_repo_dir):
    """Test extracting functions, classes, methods, docstrings, and signatures from Python files."""
    repograph_id = uuid.uuid4()
    db_session.add(Repograph(id=repograph_id, repo_url="https://github.com/test/repo", commit_sha="1111111111111111111111111111111111111111"))
    db_session.commit()

    builder = ASTIndexBuilder(sample_repo_dir)
    builder.build(db_session, repograph_id)

    symbols = db_session.execute(
        select(Symbol).where(Symbol.repograph_id == repograph_id)
    ).scalars().all()

    symbol_map = {s.name: s for s in symbols}

    # Verify function: parse_header
    assert "parse_header" in symbol_map
    s_parse = symbol_map["parse_header"]
    assert s_parse.kind == "function"
    assert "def parse_header" in s_parse.signature
    assert "Parse HTTP headers into a dictionary." in s_parse.docstring
    assert s_parse.start_line < s_parse.end_line

    # Verify function: clean_string
    assert "clean_string" in symbol_map
    s_clean = symbol_map["clean_string"]
    assert s_clean.kind == "function"
    assert "Strip whitespace and lowercase." in s_clean.docstring

    # Verify class: AuthService
    assert "AuthService" in symbol_map
    s_class = symbol_map["AuthService"]
    assert s_class.kind == "class"
    assert "Authentication and session management service." in s_class.docstring

    # Verify methods
    assert "authenticate_user" in symbol_map
    assert "verify_token" in symbol_map
    assert symbol_map["authenticate_user"].kind == "function" or symbol_map["authenticate_user"].kind == "method"


def test_extract_typescript_symbols(db_session, sample_repo_dir):
    """Test extracting TypeScript classes, methods, and functions."""
    repograph_id = uuid.uuid4()
    db_session.add(Repograph(id=repograph_id, repo_url="https://github.com/test/repo", commit_sha="2222222222222222222222222222222222222222"))
    db_session.commit()

    builder = ASTIndexBuilder(sample_repo_dir)
    builder.build(db_session, repograph_id)

    symbols = db_session.execute(
        select(Symbol).where(Symbol.repograph_id == repograph_id)
    ).scalars().all()

    symbol_names = {s.name for s in symbols}
    assert "APIClient" in symbol_names
    assert "fetchData" in symbol_names
    assert "sendRequest" in symbol_names
    assert "initializeClient" in symbol_names


def test_skip_ignored_directories(db_session, sample_repo_dir):
    """Ensure files in .git, node_modules, and __pycache__ are strictly ignored."""
    repograph_id = uuid.uuid4()
    db_session.add(Repograph(id=repograph_id, repo_url="https://github.com/test/repo", commit_sha="3333333333333333333333333333333333333333"))
    db_session.commit()

    builder = ASTIndexBuilder(sample_repo_dir)
    builder.build(db_session, repograph_id)

    symbols = db_session.execute(
        select(Symbol).where(Symbol.repograph_id == repograph_id)
    ).scalars().all()

    symbol_names = {s.name for s in symbols}
    assert "git_internal" not in symbol_names
    assert "cached_func" not in symbol_names
    assert "externalDep" not in symbol_names


def test_call_graph_extraction(db_session, sample_repo_dir):
    """Test that function and method calls are captured accurately in the calls table."""
    repograph_id = uuid.uuid4()
    db_session.add(Repograph(id=repograph_id, repo_url="https://github.com/test/repo", commit_sha="4444444444444444444444444444444444444444"))
    db_session.commit()

    builder = ASTIndexBuilder(sample_repo_dir)
    builder.build(db_session, repograph_id)

    calls = db_session.execute(
        select(Call).where(Call.repograph_id == repograph_id)
    ).scalars().all()

    call_pairs = {(c.caller_name, c.callee_name) for c in calls}

    # parse_header calls clean_string
    assert ("parse_header", "clean_string") in call_pairs

    # authenticate_user calls clean_string and verify_token
    assert ("authenticate_user", "clean_string") in call_pairs
    assert ("authenticate_user", "verify_token") in call_pairs


def test_rebuild_clears_old_records(db_session, sample_repo_dir):
    """Test that re-running build for the same repograph_id clears old records and avoids duplication."""
    repograph_id = uuid.uuid4()
    db_session.add(Repograph(id=repograph_id, repo_url="https://github.com/test/repo", commit_sha="5555555555555555555555555555555555555555"))
    db_session.commit()

    builder = ASTIndexBuilder(sample_repo_dir)
    builder.build(db_session, repograph_id)

    count_first = len(db_session.execute(select(Symbol).where(Symbol.repograph_id == repograph_id)).scalars().all())

    # Run build again
    builder.build(db_session, repograph_id)
    count_second = len(db_session.execute(select(Symbol).where(Symbol.repograph_id == repograph_id)).scalars().all())

    assert count_first == count_second
    assert count_first > 0


def test_empty_repository(db_session):
    """Test that building an index on an empty repository completes cleanly with 0 records."""
    repograph_id = uuid.uuid4()
    db_session.add(Repograph(id=repograph_id, repo_url="https://github.com/test/empty", commit_sha="6666666666666666666666666666666666666666"))
    db_session.commit()

    with tempfile.TemporaryDirectory() as empty_dir:
        builder = ASTIndexBuilder(empty_dir)
        builder.build(db_session, repograph_id)

        symbols = db_session.execute(select(Symbol).where(Symbol.repograph_id == repograph_id)).scalars().all()
        calls = db_session.execute(select(Call).where(Call.repograph_id == repograph_id)).scalars().all()

        assert len(symbols) == 0
        assert len(calls) == 0
