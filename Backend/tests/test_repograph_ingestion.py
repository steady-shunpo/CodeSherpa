import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from db.models import Repograph, Symbol
from repograph.repograph import build_repograph, get_or_build_repograph


def test_build_repograph_direct(db_session, sample_repo_dir):
    """Test directly invoking build_repograph to populate symbols for a repograph_id."""
    repograph_id = uuid.uuid4()
    repograph = Repograph(
        id=repograph_id,
        repo_url="https://github.com/example/direct-test",
        commit_sha="abcdef1234567890abcdef1234567890abcdef12",
    )
    db_session.add(repograph)
    db_session.commit()

    # Patch SessionLocal inside repograph.py to return our test db_session
    with patch("repograph.repograph.SessionLocal", return_value=db_session):
        build_repograph(sample_repo_dir, repograph_id)

    symbols = db_session.execute(
        select(Symbol).where(Symbol.repograph_id == repograph_id)
    ).scalars().all()

    assert len(symbols) > 0
    names = {s.name for s in symbols}
    assert "parse_header" in names


@pytest.mark.asyncio
async def test_get_or_build_repograph_cache_hit_and_miss(sample_repo_dir):
    """
    Test the full ingestion lifecycle:
    1. First call (cache miss) -> clones, creates Repograph row in DB, builds AST, returns from_cache=False.
    2. Second call (cache hit) -> finds existing row in DB, skips build, returns from_cache=True.
    """
    mock_session = AsyncMock()

    # Case 1: Cache Miss
    mock_result_miss = MagicMock()
    mock_result_miss.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result_miss

    generated_id = uuid.uuid4()

    async def fake_refresh(row):
        row.id = generated_id

    mock_session.refresh.side_effect = fake_refresh

    with patch("repograph.repograph.get_issue", return_value=("Sample issue", "example_owner", "sample_repo")), \
         patch("repograph.repograph.simple_clone", return_value=sample_repo_dir), \
         patch("repograph.repograph._get_commit_sha", return_value="1234567890abcdef1234567890abcdef12345678"), \
         patch("repograph.repograph.build_repograph") as mock_build:

        res_miss = await get_or_build_repograph("https://github.com/example_owner/sample_repo/issues/1", mock_session)

        assert res_miss["from_cache"] is False
        assert res_miss["repograph_id"] == generated_id
        assert res_miss["owner"] == "example_owner"
        assert res_miss["repo_name"] == "sample_repo"
        mock_build.assert_called_once()

    # Case 2: Cache Hit
    existing_repograph = Repograph(
        id=uuid.uuid4(),
        repo_url="https://github.com/example_owner/sample_repo",
        commit_sha="1234567890abcdef1234567890abcdef12345678",
    )
    mock_result_hit = MagicMock()
    mock_result_hit.scalar_one_or_none.return_value = existing_repograph
    mock_session.execute.return_value = mock_result_hit

    with patch("repograph.repograph.get_issue", return_value=("Sample issue", "example_owner", "sample_repo")), \
         patch("repograph.repograph.simple_clone", return_value=sample_repo_dir), \
         patch("repograph.repograph._get_commit_sha", return_value="1234567890abcdef1234567890abcdef12345678"), \
         patch("repograph.repograph.build_repograph") as mock_build_hit:

        res_hit = await get_or_build_repograph("https://github.com/example_owner/sample_repo/issues/1", mock_session)

        assert res_hit["from_cache"] is True
        assert res_hit["repograph_id"] == existing_repograph.id
        mock_build_hit.assert_not_called()
