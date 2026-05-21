"""
ORM models — one class per DB table.

Table overview:
  Run         — one row per issue resolution attempt
  Checkpoint  — one row per completed pipeline stage (append-only)
  Message     — intervention chat history (append-only)
  Repograph   — cached repo graphs, keyed by (repo_url, commit_sha)
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from db.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    INGESTING              = "ingesting"             # cloning repo, building repograph
    DISCUSSING             = "discussing"            # pre-run chat (Phase 2)
    STAGE_RUNNING          = "stage_running"         # an agent is actively running
    AWAITING_INTERVENTION  = "awaiting_intervention" # paused between stages
    BLOCKED_ON_HUMAN       = "blocked_on_human"      # retries exhausted, needs human
    SUCCEEDED              = "succeeded"             # terminal ✓
    FAILED                 = "failed"                # terminal ✗
    CANCELLED              = "cancelled"             # terminal —


class StageEnum(str, Enum):
    PLANNER      = "planner"
    HINT_WRITER  = "hint_writer"   # hint_supervisor runs internally here, not a separate stage
    TEST_WRITER  = "test_writer"
    IMPLEMENTER  = "implementer"
    VERIFIER     = "verifier"


# Ordered list used by the orchestrator to know what runs next.
# Index into this list == stage_index on Run and Checkpoint rows.
STAGES = [
    StageEnum.PLANNER,
    StageEnum.HINT_WRITER,
    StageEnum.TEST_WRITER,
    StageEnum.IMPLEMENTER,
    StageEnum.VERIFIER,
]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    issue_url:   Mapped[str]  = mapped_column(Text, nullable=False)
    repo_url:    Mapped[str]  = mapped_column(Text, nullable=False)
    commit_sha:  Mapped[str | None] = mapped_column(String(40), nullable=True)  # filled after clone

    status:        Mapped[str] = mapped_column(String(40), nullable=False, default=RunStatus.INGESTING)
    current_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)  # which stage is active/next
    stage_index:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # index into STAGES
    retry_count:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships — convenient for loading related rows
    checkpoints: Mapped[list["Checkpoint"]] = relationship(
        "Checkpoint", back_populates="run", order_by="Checkpoint.created_at"
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="run", order_by="Message.created_at"
    )

    def __repr__(self) -> str:
        return f"<Run id={self.id} status={self.status} stage={self.current_stage}>"


# ---------------------------------------------------------------------------
# Checkpoint  (append-only — never update, only insert or delete)
# ---------------------------------------------------------------------------

class Checkpoint(Base):
    __tablename__ = "checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    stage:       Mapped[str]  = mapped_column(String(40), nullable=False)  # StageEnum value
    stage_index: Mapped[int]  = mapped_column(Integer, nullable=False)     # position in STAGES
    output_json: Mapped[dict] = mapped_column(JSONB, nullable=False)       # agent's output dict

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped["Run"] = relationship("Run", back_populates="checkpoints")

    def __repr__(self) -> str:
        return f"<Checkpoint run={self.run_id} stage={self.stage}>"


# ---------------------------------------------------------------------------
# Message  (append-only intervention chat)
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    USER      = "user"
    ASSISTANT = "assistant"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    stage:   Mapped[str] = mapped_column(String(40), nullable=False)  # stage this message belongs to
    role:    Mapped[str] = mapped_column(String(20), nullable=False)   # MessageRole value
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped["Run"] = relationship("Run", back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message run={self.run_id} stage={self.stage} role={self.role}>"


# ---------------------------------------------------------------------------
# Repograph  (cached by repo+commit — shared across runs)
#
# graph.pkl  → stored as raw bytes (LargeBinary / bytea in Postgres)
#              load back with: pickle.loads(row.graph_pkl)
#
# tags.jsonl → each line is a JSON object; stored as a JSON array in JSONB
#              build it with: [json.loads(line) for line in tags_jsonl.splitlines()]
# ---------------------------------------------------------------------------

class Repograph(Base):
    __tablename__ = "repographs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_url:   Mapped[str]   = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str]   = mapped_column(String(40), nullable=False)

    # graph.pkl — the raw pickle bytes your repograph function produces
    graph_pkl:  Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # tags.jsonl — stored as a JSON array (one element per original line)
    tags_json:  Mapped[list]  = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Unique constraint — one graph per (repo, commit) pair
    __table_args__ = (
        UniqueConstraint("repo_url", "commit_sha", name="uq_repograph_repo_commit"),
    )

    def __repr__(self) -> str:
        return f"<Repograph repo={self.repo_url} commit={self.commit_sha[:7]}>"