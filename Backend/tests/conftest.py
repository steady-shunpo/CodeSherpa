import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from db.database import Base
from db.models import Repograph, Symbol, Call
from repograph.ast_builder import ASTIndexBuilder


@pytest.fixture(scope="session")
def db_engine():
    """Create an in-memory SQLite engine for the test session."""
    engine = create_engine("sqlite:///:memory:")
    # Create the AST/Repograph tables
    Base.metadata.create_all(engine, tables=[
        Repograph.__table__,
        Symbol.__table__,
        Call.__table__,
    ])
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Provide a transactional DB session for each test that rolls back on exit."""
    connection = db_engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, class_=Session, expire_on_commit=False)
    session = SessionLocal()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def sample_repo_dir():
    """
    Creates a temporary directory populated with sample Python, JS, and TS files
    along with ignored directories (.git, __pycache__, node_modules) to test the AST builder.
    """
    temp_dir = tempfile.mkdtemp(prefix="test_repo_")
    repo_path = Path(temp_dir)

    # 1. Python source file: utils.py
    (repo_path / "utils.py").write_text(
        '''"""General utility functions."""

def parse_header(raw_header: str) -> dict:
    """Parse HTTP headers into a dictionary."""
    sanitized = clean_string(raw_header)
    return {"raw": sanitized}

def clean_string(val: str) -> str:
    """Strip whitespace and lowercase."""
    return val.strip().lower()
''',
        encoding="utf-8",
    )

    # 2. Python source file with classes and methods: services/auth.py
    services_dir = repo_path / "services"
    services_dir.mkdir(parents=True, exist_ok=True)
    (services_dir / "auth.py").write_text(
        '''from utils import clean_string

class AuthService:
    """Authentication and session management service."""

    def authenticate_user(self, username: str, token: str) -> bool:
        """Validate user credentials against token."""
        cleaned_user = clean_string(username)
        return self.verify_token(token)

    def verify_token(self, token: str) -> bool:
        """Verify token integrity."""
        return len(token) > 10
''',
        encoding="utf-8",
    )

    # 3. TypeScript source file: src/client.ts
    src_dir = repo_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "client.ts").write_text(
        '''export class APIClient {
    fetchData(endpoint: string) {
        return this.sendRequest(endpoint);
    }

    sendRequest(url: string) {
        return "data from " + url;
    }
}

export function initializeClient() {
    const client = new APIClient();
    return client.fetchData("/api/v1");
}
''',
        encoding="utf-8",
    )

    # 4. Ignored directories and files that should not be indexed
    git_dir = repo_path / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "ignored.py").write_text("def git_internal(): pass\n", encoding="utf-8")

    pycache_dir = repo_path / "__pycache__"
    pycache_dir.mkdir(parents=True, exist_ok=True)
    (pycache_dir / "cache.py").write_text("def cached_func(): pass\n", encoding="utf-8")

    node_modules_dir = repo_path / "node_modules"
    node_modules_dir.mkdir(parents=True, exist_ok=True)
    (node_modules_dir / "dep.js").write_text("function externalDep() {}\n", encoding="utf-8")

    yield str(repo_path)

    # Cleanup temporary directory
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def indexed_repo(db_session, sample_repo_dir):
    """
    Builds an AST index on sample_repo_dir and returns:
    (repograph_id, db_session, sample_repo_dir)
    """
    repograph_id = uuid.uuid4()
    repograph = Repograph(
        id=repograph_id,
        repo_url="https://github.com/example/test-repo",
        commit_sha="a1b2c3d4e5f67890123456789012345678901234",
    )
    db_session.add(repograph)
    db_session.commit()

    builder = ASTIndexBuilder(sample_repo_dir)
    builder.build(db_session, repograph_id)

    return repograph_id, db_session, sample_repo_dir
