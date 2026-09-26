"""Candidate endpoints: create, read, advance, reject, history, group."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import serializers
from app.db import get_session
from app.schemas.candidate import (
    CandidateCreate,
    CandidateRead,
    CandidateSummary,
    GroupedCandidates,
    HistoryEntry,
    NoteCreate,
    NoteOut,
    TransitionRequest,
)
from app.services import candidate_service

router = APIRouter(prefix="/candidates", tags=["candidates"])


@router.post(
    "",
    response_model=CandidateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a candidate",
    description=(
        "Creates a candidate directly into the `applied` stage and writes the "
        "first row of their audit trail. The creation event is part of the "
        "history: a candidate who has never moved still has a one-row "
        "history, not an empty one.\n\n"
        "Returns **409** if the email is already in use."
    ),
    responses={409: {"description": "A candidate with this email already exists."}},
)
async def create_candidate(
    payload: CandidateCreate,
    session: AsyncSession = Depends(get_session),
) -> CandidateRead:
    candidate = await candidate_service.create_candidate(session, payload)
    return serializers.to_read(candidate)


@router.get(
    "",
    response_model=None,
    summary="List candidates, optionally grouped by stage",
    description=(
        "Without parameters, returns a flat list of every candidate, newest "
        "first.\n\n"
        "With `group_by=stage`, returns an object keyed by stage name. Every "
        "stage key is always present, including stages with nobody in them, "
        "so a board can be rendered without checking for missing keys.\n\n"
        "`group_by` and `q` are mutually exclusive. If both are supplied, "
        "**`q` wins** and the grouping is ignored."
    ),
    responses={
        200: {
            "description": "Either a flat list, a stage-keyed object, or ranked search results.",
        }
    },
)
async def list_candidates(
    group_by: str | None = Query(
        default=None,
        description="Set to `stage` to receive candidates grouped by their current stage.",
        examples=["stage"],
    ),
    q: str | None = Query(
        default=None,
        description=(
            "Natural-language search query. See `GET /search` for the full "
            "grammar and the error contract."
        ),
        examples=["stuck in Screening for more than a week"],
    ),
    session: AsyncSession = Depends(get_session),
):
    if q:
        # Imported here rather than at module scope: the search service is
        # wired in a later phase, and this keeps the import graph honest
        # about what depends on what.
        from app.api.routers.search import run_search

        return await run_search(session, q)

    if group_by is not None:
        if group_by != "stage":
            # A typo like `group_by=stages` should say so rather than
            # silently returning the ungrouped list.
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown group_by value '{group_by}'. "
                    "The only supported value is 'stage'."
                ),
            )
        grouped = await candidate_service.group_by_stage(session)
        return GroupedCandidates(
            **{
                stage: [serializers.to_summary(c) for c in candidates]
                for stage, candidates in grouped.items()
            }
        )

    candidates = await candidate_service.list_candidates(session)
    return [serializers.to_summary(c) for c in candidates]


@router.get(
    "/{candidate_id}",
    response_model=CandidateRead,
    summary="Open a candidate",
    description=(
        "Returns the full candidate record including `time_in_current_stage_seconds` "
        "(derived at request time, never stored) and the complete chronological "
        "audit trail."
    ),
    responses={404: {"description": "No candidate with that id."}},
)
async def get_candidate(
    candidate_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CandidateRead:
    candidate = await candidate_service.get_candidate(session, candidate_id)
    return serializers.to_read(candidate)


@router.get(
    "/{candidate_id}/history",
    response_model=list[HistoryEntry],
    summary="A candidate's complete timeline",
    description=(
        "The same array embedded in `GET /candidates/{id}`, as its own "
        "endpoint. Oldest first, always complete.\n\n"
        "Two shapes share the stream, discriminated by `type`: `transition` "
        "for a stage move, `note` for a freestanding recruiter note.\n\n"
        "This is an audit trail: entries are never updated and never deleted. "
        "That is enforced by database triggers, so it holds even against a "
        "direct connection to Postgres."
    ),
    responses={404: {"description": "No candidate with that id."}},
)
async def get_history(
    candidate_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[HistoryEntry]:
    # Deliberately the candidate, not a separate history query: both this
    # endpoint and `GET /candidates/{id}` build their array through
    # `serializers.to_history`, so the two cannot drift apart.
    candidate = await candidate_service.get_candidate(session, candidate_id)
    return serializers.to_history(candidate)


@router.post(
    "/{candidate_id}/notes",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a note to a candidate",
    description=(
        "Appends a freestanding note to the candidate's timeline.\n\n"
        "A note is **not** a stage change: it does not move the candidate and "
        "does not affect `time_in_current_stage_seconds`. It is for things "
        "that happen between moves -- \"called her references\", \"asked for a "
        "portfolio\".\n\n"
        "Notes are append-only, enforced by database triggers. There is no "
        "endpoint to edit or delete one; to correct a note, add another."
    ),
    responses={404: {"description": "No candidate with that id."}},
)
async def add_note(
    candidate_id: uuid.UUID,
    payload: NoteCreate,
    session: AsyncSession = Depends(get_session),
) -> NoteOut:
    note = await candidate_service.add_note(session, candidate_id, payload.text)
    return serializers.note_to_read(note)


@router.post(
    "/{candidate_id}/advance",
    response_model=CandidateRead,
    summary="Move a candidate forward one stage",
    description=(
        "Advances `applied -> screening -> interview -> offer -> hired`. "
        "Exactly one stage at a time.\n\n"
        "Returns **422** with an explanatory message if the move is illegal: "
        "the candidate is already `hired` or `rejected`, or the pipeline is "
        "complete. Skipping and reversing are not expressible through this "
        "endpoint -- they are not rejected by a validation check, they simply "
        "have no representation."
    ),
    responses={
        404: {"description": "No candidate with that id."},
        422: {"description": "The candidate cannot advance from their current stage."},
    },
)
async def advance_candidate(
    candidate_id: uuid.UUID,
    payload: TransitionRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> CandidateRead:
    note = payload.note if payload else None
    candidate = await candidate_service.advance_candidate(session, candidate_id, note)
    return serializers.to_read(candidate)


@router.post(
    "/{candidate_id}/reject",
    response_model=CandidateRead,
    summary="Reject a candidate",
    description=(
        "Rejects a candidate from any non-terminal stage. Rejection is a "
        "separate terminal branch, not a step in the forward sequence.\n\n"
        "Returns **422** if the candidate is already `hired` or `rejected` -- "
        "a final outcome cannot be reversed."
    ),
    responses={
        404: {"description": "No candidate with that id."},
        422: {"description": "The candidate is already in a terminal stage."},
    },
)
async def reject_candidate(
    candidate_id: uuid.UUID,
    payload: TransitionRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> CandidateRead:
    note = payload.note if payload else None
    candidate = await candidate_service.reject_candidate(session, candidate_id, note)
    return serializers.to_read(candidate)
