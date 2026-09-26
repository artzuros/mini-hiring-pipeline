"""The `candidate_notes` table -- append-only recruiter notes.

Free-text observations that are not stage changes: "called her references",
"asked for a portfolio". Rows are append-only, enforced by the triggers
created in migration 0003, exactly as `stage_transitions` is.

Why a second table rather than another `stage_transitions` row with a null
`to_stage`: a transition describes a *move*, and a note is not one. Sharing
the table would force `to_stage` to become nullable, and the search
executor's "reached stage X" clause is an EXISTS over `stage_transitions` --
it would start matching rows that moved nobody anywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CandidateNote(Base):
    __tablename__ = "candidate_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False
    )

    # The column is `text` -- that is what the API field and the migration
    # call it. The Python attribute is `body` so that this module can import
    # SQLAlchemy's `text()` under its own name, the way every other model
    # does, instead of shadowing it.
    body: Mapped[str] = mapped_column("text", Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candidate: Mapped["Candidate"] = relationship(back_populates="notes")  # noqa: F821
