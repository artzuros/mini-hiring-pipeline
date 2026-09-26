"""The `stage_transitions` table -- the audit trail.

Rows here are append-only. That is enforced by database triggers created in
migration 0002, not by convention: an UPDATE or DELETE against this table
raises `restrict_violation` regardless of who issued it.

Consequently there is deliberately no update or delete method anywhere in the
repository layer for this model. There is nothing to write.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.pipeline import Stage
from app.models.base import Base, stage_enum


class StageTransition(Base):
    __tablename__ = "stage_transitions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("candidates.id"), nullable=False
    )

    # NULL only on the creation row, where the candidate had no prior stage.
    from_stage: Mapped[Stage | None] = mapped_column(stage_enum, nullable=True)
    to_stage: Mapped[Stage] = mapped_column(stage_enum, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candidate: Mapped["Candidate"] = relationship(back_populates="transitions")  # noqa: F821
