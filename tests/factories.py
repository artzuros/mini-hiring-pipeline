"""Helpers for building candidates with backdated history in tests.

The service layer always writes `now()`, which is right but useless for
testing questions about history ("who moved to Interview since Monday"). These
helpers insert `stage_transitions` rows with explicit timestamps so a test can
state a candidate's past directly.

They deliberately do NOT go through `candidate_service`: that would test the
service against itself, and these are search tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.pipeline import Stage
from app.models.candidate import Candidate
from app.models.candidate_note import CandidateNote
from app.models.stage_transition import StageTransition

#: (stage entered, how long ago). Oldest first.
Move = tuple[Stage, timedelta]


async def add_candidate(
    session: AsyncSession,
    name: str,
    moves: list[Move] | tuple[Move, ...] = (),
    *,
    email: str | None = None,
    phone: str | None = None,
    now: datetime | None = None,
) -> Candidate:
    """Insert one candidate plus their full audit history.

    `moves` lists the stages after `applied`, oldest first. The creation row
    into `applied` is always written, one day before the first move.
    """
    now = now or datetime.now(UTC)
    moves = list(moves)

    created_at = now - timedelta(days=45)
    if moves:
        created_at = min(created_at, now - moves[0][1] - timedelta(days=1))

    candidate = Candidate(
        name=name,
        email=email or f"{name.lower().replace(' ', '.')}@example.com",
        phone=phone,
        current_stage=Stage.APPLIED,
        current_stage_since=created_at,
        created_at=created_at,
    )
    session.add(candidate)
    await session.flush()

    session.add(
        StageTransition(
            candidate_id=candidate.id,
            from_stage=None,
            to_stage=Stage.APPLIED,
            note=None,
            transitioned_at=created_at,
        )
    )

    current_stage = Stage.APPLIED
    current_since = created_at
    for stage, ago in moves:
        moved_at = now - ago
        session.add(
            StageTransition(
                candidate_id=candidate.id,
                from_stage=current_stage,
                to_stage=stage,
                note=None,
                transitioned_at=moved_at,
            )
        )
        current_stage = stage
        current_since = moved_at

    candidate.current_stage = current_stage
    candidate.current_stage_since = current_since
    await session.flush()
    return candidate


async def add_note(
    session: AsyncSession,
    candidate: Candidate,
    text: str,
    *,
    at: datetime | None = None,
) -> CandidateNote:
    """Insert one note on `candidate`, optionally backdated.

    Goes through the model rather than `candidate_service.add_note` for the
    same reason `add_candidate` skips the service: a search test that wrote
    its fixtures through the code under test could not tell a search bug from
    a write bug. The direct route also lets a test place a note in the past,
    which `now()` never will.
    """
    note = CandidateNote(
        candidate_id=candidate.id,
        body=text,
        created_at=at or datetime.now(UTC),
    )
    session.add(note)
    await session.flush()
    return note


async def seed_pipeline(session: AsyncSession, now: datetime | None = None) -> dict:
    """A small pipeline covering every example query in the assignment.

    Deliberately mirrors `scripts/seed.py` in spirit but stays minimal: each
    candidate exists to make exactly one assertion possible.
    """
    now = now or datetime.now(UTC)

    priya = await add_candidate(
        session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))], now=now
    )
    # Moved into interview two hours ago -- always "since Monday" on a
    # weekday, and its recency beats everything else in the result order.
    rahul = await add_candidate(
        session, "Rahul Mehta", [(Stage.INTERVIEW, timedelta(hours=2))], now=now
    )
    anita = await add_candidate(
        session,
        "Anita Desai",
        [
            (Stage.INTERVIEW, timedelta(days=18)),
            (Stage.OFFER, timedelta(days=6)),
            (Stage.REJECTED, timedelta(days=3)),
        ],
        now=now,
    )
    vikram = await add_candidate(
        session,
        "Vikram Singh",
        [
            (Stage.INTERVIEW, timedelta(days=32)),
            (Stage.OFFER, timedelta(days=20)),
            (Stage.HIRED, timedelta(days=14)),
        ],
        now=now,
    )
    arjun = await add_candidate(session, "Arjun Rao", now=now)

    await session.commit()
    return {
        "priya": priya,
        "rahul": rahul,
        "anita": anita,
        "vikram": vikram,
        "arjun": arjun,
    }
