"""The single search box, as its own top-level route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import serializers
from app.db import get_session
from app.schemas.candidate import CandidateSummary
from app.services.search import service as search_service

router = APIRouter(tags=["search"])


async def run_search(session: AsyncSession, q: str) -> list[CandidateSummary]:
    """Shared implementation, reused by `GET /candidates?q=`."""
    candidates = await search_service.search(session, q)
    return [serializers.to_summary(c) for c in candidates]


@router.get(
    "/search",
    response_model=list[CandidateSummary],
    summary="Search candidates in plain language",
    description=(
        "One box, natural language. Combine criteria freely; results are "
        "ranked with the best match first.\n\n"
        "**What it understands**\n\n"
        "| Query | Meaning |\n"
        "|---|---|\n"
        "| `Priya Sharma` | fuzzy name match, tolerant of typos (`sharam` finds Sharma) |\n"
        "| `in Interview` | currently in that stage |\n"
        "| `screening interview offer` | currently in any of those stages |\n"
        "| `stuck in Screening for more than a week` | in that stage at least that long |\n"
        "| `moved to Interview since Monday` | entered that stage on or after that date |\n"
        "| `reached Offer but didn't get hired` | reached the stage, currently elsewhere |\n"
        "| `except rejected` | exclude a stage from the results |\n\n"
        "These combine: `Priya in Offer for 3 days except rejected` applies "
        "all four criteria.\n\n"
        "**When it cannot understand you**, it returns **422** with a `reason` "
        "naming what it failed to read and an `examples` list of queries that "
        "work. It never returns an empty list to mean 'I gave up' -- an empty "
        "list always means the query was understood and genuinely matched "
        "nobody."
    ),
    responses={
        200: {"description": "Ranked matches, best first. May legitimately be empty."},
        422: {
            "description": "The query could not be interpreted.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "I couldn't understand this query.",
                        "reason": (
                            "No recognizable stage name, time expression, or "
                            "candidate name found in 'asdkfjasldkfj'."
                        ),
                        "examples": [
                            "in Interview",
                            "stuck in Screening for more than a week",
                            "moved to Interview since Monday",
                            "reached Offer but not hired",
                            "except rejected",
                            "sharam",
                        ],
                    }
                }
            },
        },
    },
)
async def search(
    q: str = Query(
        description=(
            "The search query. At most "
            f"{search_service.MAX_QUERY_LENGTH} characters; anything longer "
            "is rejected with the same 422 as an unreadable query."
        ),
        examples=["stuck in Screening for more than a week"],
    ),
    session: AsyncSession = Depends(get_session),
) -> list[CandidateSummary]:
    return await run_search(session, q)
