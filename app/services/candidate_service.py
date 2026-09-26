"""Orchestration: domain rules + repository calls, wrapped in transactions.

The one invariant this module exists to protect:

    a candidate's `current_stage` always equals the `to_stage` of that
    candidate's most recent `stage_transitions` row.

Both halves of that invariant are written inside a single database
transaction, and the transaction is committed here -- not in the route handler
and not in the repository. A crash between the two writes rolls both back,
so the invariant cannot be observed broken. Splitting them across two commits
would make it corruptible by any untimely failure, and the corruption would be
invisible until someone read the candidate's history.

A note on time: the stage-change timestamp comes from Postgres `now()`, not
from Python. In Postgres, `now()` is the *transaction* timestamp -- it returns
the same value for every call inside one transaction. That means the audit
row's `transitioned_at` and the candidate's `current_stage_since` are
guaranteed byte-identical without any extra coordination, and the whole system
has exactly one clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.pipeline import Stage, next_stage, validate_creation, validate_reject
from app.models.candidate import Candidate
from app.models.stage_transition import StageTransition
from app.repositories import candidate_repo as repo
from app.schemas.candidate import CandidateCreate
from app.services.errors import CandidateNotFoundError, DuplicateEmailError


async def create_candidate(
    session: AsyncSession, payload: CandidateCreate
) -> Candidate:
    """Create a candidate into `applied` and open their audit trail.

    The audit trail starts here, at creation -- not at the candidate's first
    *move*. A candidate who has never moved still has a history, and that
    history is one row long.
    """
    validate_creation(Stage.APPLIED)

    if await repo.get_by_email(session, payload.email) is not None:
        raise DuplicateEmailError(payload.email)

    # Newest state first: the candidate row is created at the default
    # `applied` stage, then the creation row is appended. Both are flushed
    # inside one uncommitted transaction.
    candidate = await repo.add_candidate(
        session,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        resume_url=payload.resume_url,
    )
    await repo.add_transition(
        session,
        candidate_id=candidate.id,
        from_stage=None,  # the creation row has no predecessor
        to_stage=Stage.APPLIED,
        note=None,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        # Two concurrent creates with the same email can both pass the check
        # above and race to the unique index. The database is the real
        # arbiter; this turns its rejection into the same 409 the check
        # would have produced.
        await session.rollback()
        raise DuplicateEmailError(payload.email) from exc

    await session.refresh(candidate)
    return candidate


async def get_candidate(session: AsyncSession, candidate_id: uuid.UUID) -> Candidate:
    candidate = await repo.get_by_id(session, candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(candidate_id)
    return candidate


async def get_history(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[StageTransition]:
    # Confirms the candidate exists, so an unknown id is a 404 rather than an
    # empty list that looks like "this candidate has no history".
    await get_candidate(session, candidate_id)
    return await repo.list_history(session, candidate_id)


async def _apply_transition(
    session: AsyncSession,
    candidate: Candidate,
    *,
    to_stage: Stage,
    note: str | None,
) -> Candidate:
    """Write a stage change and its audit row as one atomic unit."""
    from_stage = candidate.current_stage

    await repo.set_current_stage(
        session, candidate, stage=to_stage, since=func.now()
    )
    await repo.add_transition(
        session,
        candidate_id=candidate.id,
        from_stage=from_stage,
        to_stage=to_stage,
        note=note,
    )

    await session.commit()
    await session.refresh(candidate)
    return candidate


async def advance_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, note: str | None = None
) -> Candidate:
    """Move a candidate forward exactly one stage.

    Raises `InvalidTransitionError` -- which the API renders as a 422 with an
    explanatory message -- if the candidate is already in a terminal stage.
    """
    candidate = await get_candidate(session, candidate_id)
    target = next_stage(candidate.current_stage)  # raises if terminal
    return await _apply_transition(session, candidate, to_stage=target, note=note)


async def reject_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, note: str | None = None
) -> Candidate:
    """Reject a candidate from whatever non-terminal stage they are in."""
    candidate = await get_candidate(session, candidate_id)
    validate_reject(candidate.current_stage)  # raises if terminal
    return await _apply_transition(
        session, candidate, to_stage=Stage.REJECTED, note=note
    )


async def list_candidates(session: AsyncSession) -> list[Candidate]:
    return await repo.list_all(session)


async def group_by_stage(session: AsyncSession) -> dict[str, list[Candidate]]:
    """Every stage as a key, including stages with nobody in them.

    The empty lists are the point: the caller renders a stable board without
    having to distinguish "no candidates in Offer" from "no such stage".
    """
    candidates = await repo.list_ordered_by_stage_entry(session)
    grouped: dict[str, list[Candidate]] = {stage.value: [] for stage in Stage}
    for candidate in candidates:
        grouped[candidate.current_stage.value].append(candidate)
    return grouped


def time_in_current_stage_seconds(
    candidate: Candidate, *, now: datetime | None = None
) -> int:
    """How long the candidate has been in their current stage.

    Derived on every read from `current_stage_since`. This is never a stored
    column -- a stored duration would be wrong the moment it was written and
    would silently disagree with the audit trail.
    """
    reference = now or datetime.now(UTC)
    delta = reference - candidate.current_stage_since
    return max(0, int(delta.total_seconds()))
