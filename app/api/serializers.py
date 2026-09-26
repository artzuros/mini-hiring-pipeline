"""Conversion from ORM objects to response schemas.

Kept out of both `schemas/` (which must stay free of logic) and
`services/` (which must stay free of HTTP concerns). This module is the one
place that knows how a database row becomes a JSON body.
"""

from __future__ import annotations

from app.models.candidate import Candidate
from app.schemas.candidate import (
    CandidateRead,
    CandidateSummary,
    StageTransitionRead,
)
from app.services.candidate_service import time_in_current_stage_seconds


def to_summary(candidate: Candidate) -> CandidateSummary:
    return CandidateSummary(
        id=candidate.id,
        name=candidate.name,
        email=candidate.email,
        phone=candidate.phone,
        resume_url=candidate.resume_url,
        current_stage=candidate.current_stage,
        current_stage_since=candidate.current_stage_since,
        time_in_current_stage_seconds=time_in_current_stage_seconds(candidate),
        created_at=candidate.created_at,
    )


def to_read(candidate: Candidate) -> CandidateRead:
    """Full representation, including the complete audit trail."""
    history = [
        StageTransitionRead(
            from_stage=t.from_stage,
            to_stage=t.to_stage,
            transitioned_at=t.transitioned_at,
            note=t.note,
        )
        # The relationship is ordered by `transitioned_at`, but `selectin`
        # loading does not guarantee it survives whatever the session cache
        # did. Sorting here makes chronological order a property of the
        # response rather than of the load path.
        for t in sorted(candidate.transitions, key=lambda t: (t.transitioned_at, str(t.id)))
    ]

    return CandidateRead(
        **to_summary(candidate).model_dump(),
        history=history,
    )


def transition_to_read(transition) -> StageTransitionRead:
    return StageTransitionRead.model_validate(transition)
