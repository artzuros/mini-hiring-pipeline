"""Conversion from ORM objects to response schemas.

Kept out of both `schemas/` (which must stay free of logic) and
`services/` (which must stay free of HTTP concerns). This module is the one
place that knows how a database row becomes a JSON body.
"""

from __future__ import annotations

from datetime import datetime

from app.models.candidate import Candidate
from app.models.candidate_note import CandidateNote
from app.schemas.candidate import (
    CandidateRead,
    CandidateSummary,
    HistoryEntry,
    NoteOut,
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


def to_history(candidate: Candidate) -> list[HistoryEntry]:
    """The candidate's timeline: transitions and notes as one chronological stream.

    This is the *only* place the timeline is assembled. Both
    `GET /candidates/{id}` and `GET /candidates/{id}/history` call it, which
    is what makes "the history endpoint returns the same array that is
    embedded in the candidate" a structural property rather than two
    independently-written orderings that happen to agree.

    Transitions and notes are merged in Python rather than with a SQL UNION.
    The two tables have different columns, so a union would have to invent
    nulls for whichever side each row came from, and the result would be
    narrower than either input. Sorting here also makes chronological order a
    property of the response rather than of the load path: the relationships
    carry an `order_by`, but `selectin` loading does not guarantee it survives
    whatever the session cache did.
    """
    # `(sort key, entry)` pairs, because the two entry shapes share `at` but
    # are built from differently-named ORM columns -- and because the UUID
    # tiebreaker comes from the row, not from the schema (a transition entry
    # has no `id` field of its own).
    events: list[tuple[tuple[datetime, str], HistoryEntry]] = [
        (
            (t.transitioned_at, str(t.id)),
            StageTransitionRead(
                at=t.transitioned_at,
                from_stage=t.from_stage,
                to_stage=t.to_stage,
                transition_note=t.note,
            ),
        )
        for t in candidate.transitions
    ]
    events += [
        (
            (n.created_at, str(n.id)),
            NoteOut(at=n.created_at, id=n.id, text=n.body),
        )
        for n in candidate.notes
    ]

    # Two events written in the same transaction share a timestamp, because
    # `now()` is the transaction clock. The UUID makes the order total, so a
    # caller never sees the stream reshuffle between requests.
    events.sort(key=lambda pair: pair[0])
    return [entry for _, entry in events]


def to_read(candidate: Candidate) -> CandidateRead:
    """Full representation, including the complete timeline."""
    return CandidateRead(
        **to_summary(candidate).model_dump(),
        history=to_history(candidate),
    )


def note_to_read(note: CandidateNote) -> NoteOut:
    return NoteOut(at=note.created_at, id=note.id, text=note.body)
