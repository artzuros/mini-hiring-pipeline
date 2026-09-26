"""All database access for candidates and their audit trail.

Everything that knows about SQL lives here (or in the search executor, which
is a query builder in its own right). Services above this layer deal in
objects and domain rules, never in queries.

Note what is *absent*: there is no method to update or delete a
`StageTransition`. The table is append-only and enforced as such by database
triggers, so no such method could work even if it existed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.pipeline import Stage
from app.models.candidate import Candidate
from app.models.stage_transition import StageTransition


async def add_candidate(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    phone: str | None,
    resume_url: str | None,
) -> Candidate:
    """Insert a candidate row. Does not commit -- the caller owns the transaction."""
    candidate = Candidate(
        name=name,
        email=email,
        phone=phone,
        resume_url=resume_url,
        current_stage=Stage.APPLIED,
    )
    session.add(candidate)
    # Flush (not commit) so the server defaults for id/created_at are populated
    # and the candidate is visible to subsequent statements in this same
    # transaction -- which the audit insert below depends on.
    await session.flush()
    return candidate


async def add_transition(
    session: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    from_stage: Stage | None,
    to_stage: Stage,
    note: str | None = None,
    transitioned_at: datetime | None = None,
) -> StageTransition:
    """Append a row to the audit trail.

    `transitioned_at` is only supplied by the seed script, which backdates
    history to create realistic fixtures. The application always lets the
    database default (`now()`) stand.
    """
    transition = StageTransition(
        candidate_id=candidate_id,
        from_stage=from_stage,
        to_stage=to_stage,
        note=note,
    )
    if transitioned_at is not None:
        transition.transitioned_at = transitioned_at
    session.add(transition)
    await session.flush()
    return transition


async def get_by_id(session: AsyncSession, candidate_id: uuid.UUID) -> Candidate | None:
    """Fetch one candidate, with `transitions` eagerly loaded."""
    result = await session.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    return result.scalar_one_or_none()


async def get_by_email(session: AsyncSession, email: str) -> Candidate | None:
    result = await session.execute(
        select(Candidate).where(func.lower(Candidate.email) == email.lower())
    )
    return result.scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[Candidate]:
    """All candidates, newest first."""
    result = await session.execute(
        select(Candidate).order_by(Candidate.created_at.desc(), Candidate.id)
    )
    return list(result.scalars().all())


async def list_ordered_by_stage_entry(session: AsyncSession) -> list[Candidate]:
    """All candidates, most recently moved first.

    Used by the grouping view so that within each stage the most recently
    active candidate is at the top -- the recruiter's natural reading order.
    """
    result = await session.execute(
        select(Candidate).order_by(Candidate.current_stage_since.desc(), Candidate.id)
    )
    return list(result.scalars().all())


async def list_history(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[StageTransition]:
    """The full audit trail for one candidate, oldest first."""
    result = await session.execute(
        select(StageTransition)
        .where(StageTransition.candidate_id == candidate_id)
        .order_by(StageTransition.transitioned_at.asc(), StageTransition.id.asc())
    )
    return list(result.scalars().all())


async def set_current_stage(
    session: AsyncSession,
    candidate: Candidate,
    *,
    stage: Stage,
    since: datetime,
) -> None:
    """Move a candidate's cached current stage. Does not commit."""
    candidate.current_stage = stage
    candidate.current_stage_since = since
    await session.flush()
