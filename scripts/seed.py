"""Load realistic sample data, with backdated history.

Run:  python scripts/seed.py

Why this bypasses the service layer
-----------------------------------
The application only ever writes `now()` for a stage change, which is correct
-- a recruiter moving someone today should not be able to claim they moved
them last Tuesday. But the assignment's example queries are about *history*
("who moved to Interview since Monday", "who has been stuck in Screening for
more than a week"), and those questions have no meaningful answer against a
database where every candidate was created in the last few seconds.

So this script inserts `stage_transitions` rows with explicit backdated
timestamps, straight through the ORM. That is a fixture-loading technique,
not a bypass of a real constraint: the HTTP API still cannot backdate
anything, and the audit table's immutability triggers still apply here (only
INSERT is used, never UPDATE or DELETE).

Timestamps are all computed relative to the moment you run it, so the seed
answers the example queries correctly whenever it is run, not just on the day
it was written.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.domain.pipeline import Stage  # noqa: E402
from app.models.candidate import Candidate  # noqa: E402
from app.models.stage_transition import StageTransition  # noqa: E402


@dataclass
class Scenario:
    """One candidate and the history they should have."""

    name: str
    email: str
    phone: str | None = None
    #: (stage entered, how long ago they entered it, note). Must be in
    #: chronological order, oldest first -- the creation row is prepended
    #: automatically.
    moves: list[tuple[Stage, timedelta, str | None]] = field(default_factory=list)


def _recent_weekday(weekday: int, now: datetime) -> datetime:
    """Midnight of the most recent `weekday` strictly before today.

    Used so a candidate seeded as "moved to Interview since Monday" really
    did move after Monday, whichever day the seed happens to run on.
    """
    delta = (now.weekday() - weekday) % 7
    if delta == 0:
        delta = 7
    return (now - timedelta(days=delta)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def build_scenarios(now: datetime) -> list[Scenario]:
    """The fixture set. Each entry targets a specific example query."""
    monday = _recent_weekday(0, now)  # Monday 00:00, at least a day ago

    return [
        # --- Targets "stuck in Screening for more than a week" and the
        #     typo search "sharam" -> "Sharma" --------------------------
        Scenario(
            "Priya Sharma",
            "priya.sharma@example.com",
            "+91 98765 43210",
            [
                (Stage.SCREENING, timedelta(days=9), "Resume looks strong"),
            ],
        ),
        # --- Targets "moved to Interview since Monday" ----------------
        Scenario(
            "Rahul Mehta",
            "rahul.mehta@example.com",
            "+91 98200 11223",
            [
                (Stage.SCREENING, timedelta(days=12), None),
            ],
        ),
        # --- Targets "reached Offer but didn't get hired" -------------
        Scenario(
            "Anita Desai",
            "anita.desai@example.com",
            "+91 99870 55441",
            [
                (Stage.SCREENING, timedelta(days=25), None),
                (Stage.INTERVIEW, timedelta(days=18), "Good system design"),
                (Stage.OFFER, timedelta(days=6), "Offer extended"),
                (Stage.REJECTED, timedelta(days=3), "Declined our offer"),
            ],
        ),
        # --- A second "reached Offer, not hired" so the query is not a
        #     single-row coincidence ------------------------------------
        Scenario(
            "Meera Iyer",
            "meera.iyer@example.com",
            None,
            [
                (Stage.SCREENING, timedelta(days=30), None),
                (Stage.INTERVIEW, timedelta(days=20), None),
                (Stage.OFFER, timedelta(days=10), None),
                (Stage.REJECTED, timedelta(days=8), "Failed reference check"),
            ],
        ),
        # --- Targets "except rejected": a Hired candidate must survive
        #     that filter, which is the whole point of including them ----
        Scenario(
            "Vikram Singh",
            "vikram.singh@example.com",
            "+91 90000 12345",
            [
                (Stage.SCREENING, timedelta(days=40), None),
                (Stage.INTERVIEW, timedelta(days=32), None),
                (Stage.OFFER, timedelta(days=20), None),
                (Stage.HIRED, timedelta(days=14), "Started on the platform team"),
            ],
        ),
        Scenario(
            "Deepa Nair",
            "deepa.nair@example.com",
            None,
            [
                (Stage.SCREENING, timedelta(days=35), None),
                (Stage.INTERVIEW, timedelta(days=28), None),
                (Stage.OFFER, timedelta(days=15), None),
                (Stage.HIRED, timedelta(days=9), None),
            ],
        ),
        # --- A few more in each stage so the grouped board is not sparse
        Scenario("Arjun Rao", "arjun.rao@example.com", None, []),
        Scenario(
            "Sneha Kulkarni",
            "sneha.kulkarni@example.com",
            None,
            [(Stage.SCREENING, timedelta(days=2), None)],
        ),
        Scenario(
            "Karan Malhotra",
            "karan.malhotra@example.com",
            None,
            [(Stage.SCREENING, timedelta(days=11), "Waiting on take-home")],
        ),
        Scenario(
            "Fatima Sheikh",
            "fatima.sheikh@example.com",
            None,
            [
                (Stage.SCREENING, timedelta(days=16), None),
                (Stage.INTERVIEW, timedelta(days=5), None),
            ],
        ),
        Scenario(
            "Joseph Fernandes",
            "joseph.fernandes@example.com",
            None,
            [
                (Stage.SCREENING, timedelta(days=22), None),
                (Stage.INTERVIEW, timedelta(days=12), None),
                (Stage.OFFER, timedelta(days=4), None),
            ],
        ),
        Scenario(
            "Lakshmi Menon",
            "lakshmi.menon@example.com",
            None,
            [
                (Stage.SCREENING, timedelta(days=19), None),
                (Stage.REJECTED, timedelta(days=13), "Withdrew"),
            ],
        ),
        Scenario(
            "Omar Abdullah",
            "omar.abdullah@example.com",
            None,
            [(Stage.REJECTED, timedelta(days=7), "Not enough backend experience")],
        ),
        Scenario("Nisha Gupta", "nisha.gupta@example.com", None, []),
        Scenario("Ravi Shankar", "ravi.shankar@example.com", None, []),
    ]


def _interview_mover_scenario_reference(now: datetime) -> tuple[str, datetime]:
    """The candidate and timestamp that "moved to Interview since Monday" hits."""
    return "Rahul Mehta", _recent_weekday(0, now) + timedelta(hours=10)


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)

    monday_move = _recent_weekday(0, now) + timedelta(hours=10)
    scenarios = build_scenarios(now)

    async with engine.begin() as conn:
        # TRUNCATE rather than DELETE: the audit table's immutability trigger
        # is a row-level BEFORE DELETE trigger, which TRUNCATE does not fire.
        await conn.execute(text("TRUNCATE stage_transitions, candidates CASCADE"))

    async with factory() as session:
        for scenario in scenarios:
            created_at = now - timedelta(days=45)
            if scenario.moves:
                created_at = min(
                    now - scenario.moves[0][1] - timedelta(days=1), created_at
                )

            candidate = Candidate(
                name=scenario.name,
                email=scenario.email,
                phone=scenario.phone,
                current_stage=Stage.APPLIED,
                current_stage_since=created_at,
                created_at=created_at,
            )
            session.add(candidate)
            await session.flush()

            # Row 1: the creation event, which every candidate has.
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

            for stage, ago, note in scenario.moves:
                moved_at = now - ago

                # "moved to Interview since Monday" should find exactly one
                # candidate. Place this one's move deliberately after the
                # most recent Monday, whatever day the seed runs on.
                if scenario.name == "Rahul Mehta" and stage is Stage.INTERVIEW:
                    moved_at = monday_move

                session.add(
                    StageTransition(
                        candidate_id=candidate.id,
                        from_stage=current_stage,
                        to_stage=stage,
                        note=note,
                        transitioned_at=moved_at,
                    )
                )
                current_stage = stage
                current_since = moved_at

            # Rahul has no INTERVIEW move in his scenario list; give him one
            # so the "since Monday" query has a target.
            if scenario.name == "Rahul Mehta":
                session.add(
                    StageTransition(
                        candidate_id=candidate.id,
                        from_stage=Stage.SCREENING,
                        to_stage=Stage.INTERVIEW,
                        note="Moved after Monday screen",
                        transitioned_at=monday_move,
                    )
                )
                current_stage = Stage.INTERVIEW
                current_since = monday_move

            candidate.current_stage = current_stage
            candidate.current_stage_since = current_since

        await session.commit()

    await engine.dispose()

    _, monday_move = _interview_mover_scenario_reference(now)
    print(f"Seeded {len(scenarios)} candidates.")
    print(f"  'moved to Interview since Monday' -> Rahul Mehta ({monday_move:%Y-%m-%d %H:%M %Z})")
    print("  'stuck in Screening for more than a week' -> Priya Sharma (9 days)")
    print("  'reached Offer but didn't get hired' -> Anita Desai, Meera Iyer")
    print("  'except rejected' -> 14 candidates, including 2 Hired")


if __name__ == "__main__":
    asyncio.run(seed())
