"""
app/db/repograph.py

Integration layer between your existing repograph functions and the DB cache.

Flow:
  1. Check DB for existing repograph keyed by (repo_url, commit_sha)
  2. Cache hit  → return the stored graph_pkl bytes and tags_json list
  3. Cache miss → clone repo, get commit SHA, build repograph,
                  read files into memory, store in DB, clean up disk

Your functions are called here but never modified. If you change a function
signature, update only the "YOUR FUNCTIONS" section below.
"""

import asyncio
import json
import logging
import os
import pickle
import shutil
import subprocess
from pathlib import Path
import requests
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from repograph.ast_builder import ASTIndexBuilder
from db.database import SessionLocal


from db.models import Repograph
# from repograph.construct_graph import build_and_save_repograph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YOUR FUNCTIONS — import and call your existing code here
# ---------------------------------------------------------------------------
# Replace these imports with your actual module paths, e.g.:
#   from app.agents.planner import get_issue, clone_repo, build_repograph
#
# Expected signatures (adapt the calls below if yours differ):
#   get_issue(issue_url: str) -> (issue_text, comments, owner, repo_name)
#   clone_repo(repo_url: str) -> repo_path (str)          # clones into testRepos/
#   build_repograph(repo_path: str) -> (graph_path, tags_path)  # writes to root dir
#
# TODO: replace with your real imports
def get_issue(url):
    # 1. Regex Match
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    match = re.search(pattern, url)
    
    if not match:
        raise ValueError("Invalid GitHub issue URL")

    owner, repo, number = match.groups()
    headers = {"Authorization": f"token {os.environ.get('GITHUB_TOKEN', '')}"}
    print(owner, repo, number)
    # 2. API Request
    res = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
        # headers=headers  # ✅ add this
    )
    print("GITHUB REQUEST", res)
    # Check for 404s or connection errors
    if res.status_code != 200:
        raise Exception(f"Issue not found: {res.status_code}")

    issue = res.json()
    try:
        comments_resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
            # headers=headers
        )
        if not comments_resp.ok:
            print(f"Comments fetch failed: {comments_resp.status_code} - {comments_resp.json()}")
            comments = []
        else:
            comments = comments_resp.json()
            if not isinstance(comments, list):
                print(f"Unexpected comments response: {comments}")
                comments = []
    except Exception as e:
        logger.warning(f"Failed to fetch comments: {e}")
        comments = []
    
    # Format comments — include author and body, skip bot comments
    formatted_comments = []
    for c in comments:
        author = c["user"]["login"]
        # Skip common bots
        if any(bot in author.lower() for bot in ["bot", "stale", "codecov"]):
            continue
        body = c["body"].strip()
        if len(body) < 20:  # skip trivial comments like "same here"
            continue
        formatted_comments.append(f"@{author}:\n{body}")

    
    parts = [
        f"ISSUE TITLE: {issue.get('title')}",
        f"\nISSUE DESCRIPTION:\n{issue.get('body')}",
    ]
    
    if formatted_comments and False:
        parts.append("\nDISCUSSION COMMENTS:")
        total_comment_chars = 0

        for comment in formatted_comments:
            if total_comment_chars > 3000:
                parts.append("... (remaining comments truncated)")
                break

            parts.append(comment)
            total_comment_chars += len(comment)
    
    issue_text = "\n\n".join(parts)

    # 3. Return Dictionary (Mapping the JS object structure)
    return issue_text, owner, repo


def simple_clone(git_url: str, target_dir: str = "testRepos"):
    # 1. Nuke the folder if it exists (ignore_errors bypasses the Windows read-only lock)
    if os.path.exists(target_dir):
        subprocess.run(["rmdir", "/s", "/q", target_dir], shell=True)
    
    # 3. Clone it
    subprocess.run(["git", "clone", git_url, target_dir], check=True)

    return 'testRepos'


def build_repograph(repo_path: str, repograph_id):
    with SessionLocal() as session:
        ASTIndexBuilder(repo_path).build(session, repograph_id)
    


    # graph_path = ""
    # tags_path = ""
    # return graph_path, tags_path

# ---------------------------------------------------------------------------


def _get_commit_sha(repo_path: str) -> str:
    """
    Runs `git rev-parse HEAD` inside the cloned repo.
    Returns the full 40-char SHA.
    Raises RuntimeError if git fails.
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed in {repo_path}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _read_graph_pkl(graph_path: str) -> bytes:
    """Read graph.pkl from disk into raw bytes for DB storage."""
    with open(graph_path, "rb") as f:
        return f.read()


def _read_tags_jsonl(tags_path: str) -> list:
    """
    Parse tags.jsonl into a list of dicts for JSONB storage.
    Each line in the file becomes one element in the list.
    """
    tags = []
    with open(tags_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                tags.append(json.loads(line))
    return tags


def _cleanup(repo_path: str, graph_path: str, tags_path: str) -> None:
    """
    Remove all temporary files from disk after storing in DB.
    Logs warnings on failure but never raises — cleanup is best-effort.
    """
    for path, label in [(graph_path, "graph.pkl"), (tags_path, "tags.jsonl")]:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                logger.debug(f"Deleted {label} at {path}")
        except Exception as e:
            logger.warning(f"Could not delete {label} at {path}: {e}")

    try:
        # testRepos/ is the parent of repo_path (e.g. testRepos/owner_repo/)
        # We delete the whole testRepos/ dir to avoid stale clones accumulating
        repo_parent = str(Path(repo_path).parent)
        if os.path.exists(repo_parent):
            shutil.rmtree(repo_parent)
            logger.debug(f"Deleted cloned repo dir: {repo_parent}")
    except Exception as e:
        logger.warning(f"Could not delete repo dir {repo_path}: {e}")


# ---------------------------------------------------------------------------
# Main entry point — called by the orchestrator during INGESTING
# ---------------------------------------------------------------------------

async def get_or_build_repograph(
    issue_url: str,
    session: AsyncSession,
) -> dict:
    """
    Full ingestion flow. Returns a dict with everything downstream agents need:

      {
        "issue_text":  str,
        "comments":    ...,        # whatever get_issue returns
        "owner":       str,
        "repo_name":   str,
        "repo_url":    str,
        "commit_sha":  str,
        "graph_pkl":   bytes,      # pickle.loads(graph_pkl) to use the graph
        "tags":        list[dict], # parsed tags.jsonl entries
        "from_cache":  bool,       # True if we reused an existing repograph
      }

    Raises on any failure — the orchestrator will catch and mark the run FAILED.
    """

    # ------------------------------------------------------------------
    # Step 1 — extract issue data
    # ------------------------------------------------------------------
    logger.info(f"Fetching issue: {issue_url}")

    # Run your sync function in a thread so it doesn't block the event loop
    issue_text, owner, repo_name = await asyncio.to_thread(
        get_issue, issue_url
    )

    repo_url = f"https://github.com/{owner}/{repo_name}"
    logger.info(f"Resolved repo: {repo_url}")

    # ------------------------------------------------------------------
    # Step 2 — clone to get the commit SHA, then check cache
    # ------------------------------------------------------------------
    # We need the SHA before we can check the cache, so we always clone first.
    # If it's a cache hit we immediately delete the clone — cheap wasted clone
    # is better than storing the graph twice or skipping the SHA check.

    logger.info("Cloning repo to get commit SHA...")
    repo_path = await asyncio.to_thread(simple_clone, repo_url)
    commit_sha = _get_commit_sha(repo_path)
    logger.info(f"Commit SHA: {commit_sha}")

    # ------------------------------------------------------------------
    # Step 3 — DB cache check
    # ------------------------------------------------------------------
    stmt = select(Repograph).where(
        Repograph.repo_url == repo_url,
        Repograph.commit_sha == commit_sha,
    )
    result = await session.execute(stmt)
    cached = result.scalar_one_or_none()

    if cached is not None:
        logger.info("Repograph cache hit — skipping build, deleting clone")
        
        # base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # graph_path = os.path.join(base_dir, "graph.pkl")
        # tags_path  = os.path.join(base_dir, "tags.jsonl")
                
        # with open(graph_path, "wb") as f:
        #     f.write(cached.graph_pkl)
        
        # with open(tags_path, "w", encoding="utf-8") as f:
        #     for tag in cached.tags_json:
        #         f.write(json.dumps(tag) + "\n")
        
        return {
            "repograph_id": cached.id,
            "issue_text": issue_text,
            "owner":      owner,
            "repo_name":  repo_name,
            "repo_url":   repo_url,
            "commit_sha": commit_sha,
            # "graph_pkl":  cached.graph_pkl,
            # "tags":       cached.tags_json,
            # "graph_path": graph_path,
            # "tags_path":  tags_path,
            "from_cache": True,
        }

    # ------------------------------------------------------------------
    # Step 4 — cache miss: build repograph
    # ------------------------------------------------------------------
    logger.info(f"Repograph cache miss — building AST index...")
    repograph_row = Repograph(repo_url=repo_url, commit_sha=commit_sha)
    session.add(repograph_row)
    await session.commit()
    await session.refresh(repograph_row)
    repograph_id = repograph_row.id
    await asyncio.to_thread(build_repograph, repo_path, repograph_id)

    # ------------------------------------------------------------------
    # Step 5 — read files into memory
    # ------------------------------------------------------------------
    # graph_pkl = _read_graph_pkl(graph_path)
    # tags      = _read_tags_jsonl(tags_path)
    # logger.info(f"Repograph built: {len(graph_pkl)} bytes, {len(tags)} tags")
    # graph_pkl = ""
    # tags = ""

    # ------------------------------------------------------------------
    # Step 6 — store in DB
    # ------------------------------------------------------------------
    # repograph_row = Repograph(

    #     repo_url=repo_url,
    #     commit_sha=commit_sha,
    #     # graph_pkl=graph_pkl,
    #     # tags_json=tags,
    # )
    # session.add(repograph_row)
    # await session.commit()
    logger.info("Repograph stored in DB")

    # ------------------------------------------------------------------
    # Step 7 — clean up disk (best-effort)
    # ------------------------------------------------------------------

    # stmt = select(Repograph).where(
    #     Repograph.repo_url == repo_url,
    #     Repograph.commit_sha == commit_sha,
    # )
    # result = await session.execute(stmt)
    # cached = result.scalar_one_or_none()

    # if cached is not None:
    #     logger.info("Repograph cache hit — skipping build, deleting clone")
        
    #     base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    #     graph_path = os.path.join(base_dir, "graph.pkl")
    #     tags_path  = os.path.join(base_dir, "tags.jsonl")
                
    #     with open(graph_path, "wb") as f:
    #         f.write(cached.graph_pkl)
        
    #     with open(tags_path, "w", encoding="utf-8") as f:
    #         for tag in cached.tags_json:
    #             f.write(json.dumps(tag) + "\n")
        
    #     return {
    #         "issue_text": issue_text,
    #         "owner":      owner,
    #         "repo_name":  repo_name,
    #         "repo_url":   repo_url,
    #         "commit_sha": commit_sha,
    #         "graph_pkl":  cached.graph_pkl,
    #         "tags":       cached.tags_json,
    #         "graph_path": graph_path,
    #         "tags_path":  tags_path,
    #         "from_cache": True,
    #     }
    

    # _cleanup(repo_path, graph_path, tags_path)

    return {
            "repograph_id": repograph_id,
            "issue_text": issue_text,
            "owner":      owner,
            "repo_name":  repo_name,
            "repo_url":   repo_url,
            "commit_sha": commit_sha,
            "from_cache": False,
        }