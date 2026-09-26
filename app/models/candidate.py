"""The `candidates` table.

`current_stage` and `current_stage_since` are a denormalised cache of where
the candidate is right now, maintained in the same transaction as the audit
row that caused the move. The audit trail in `stage_transitions` remains the
source of truth; these two columns exist so the grouping view and the
"stuck in X" search do not have to replay every candidate's history.

The invariant that keeps them honest is: for any candidate, `current_stage`
equals the `to_stage` of that candidate's most recent transition. Both writes
happen inside one transaction in `CandidateService`, so it cannot be observed
violated.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.pipeline import Stage
from app.models.base import Base, stage_enum


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    current_stage: Mapped[Stage] = mapped_column(
        stage_enum, nullable=False, server_default=text("'applied'")
    )
    current_stage_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    transitions: Mapped[list["StageTransition"]] = relationship(  # noqa: F821
        back_populates="candidate",
        order_by="StageTransition.transitioned_at",
        lazy="selectin",
    )
    notes: Mapped[list["CandidateNote"]] = relationship(  # noqa: F821
        back_populates="candidate",
        order_by="CandidateNote.created_at",
        lazy="selectin",
    )
