"""HTML routes. A thin skin over the same services the JSON API uses.

Two conventions are worth stating because they are deliberate:

* Every mutation is a POST followed by a 303 redirect (POST-redirect-GET), so
  reloading the resulting page cannot re-submit the form. The cost is that
  outcome messages have to survive a redirect, which is what the `flash` and
  `kind` query parameters are for.

* Errors are rendered as a page, not raised. The JSON API's exception
  handlers in `app/main.py` return JSON bodies, which are useless to a
  browser, so these routes catch the same exceptions and turn them into
  human-readable messages on the same page. The rules that produce the error
  are identical -- only the rendering differs.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.api import serializers
from app.domain.pipeline import (
    TERMINAL_STAGES,
    InvalidTransitionError,
    Stage,
    next_stage,
)
from app.schemas.candidate import CandidateCreate, NoteCreate
from app.services import candidate_service
from app.services.errors import SEARCH_EXAMPLES, ServiceError, UnparseableQueryError
from app.services.search import service as search_service
from app.web import templates

#: `include_in_schema=False` keeps the HTML routes out of the OpenAPI document.
#: `/docs` should describe the JSON API, not the pages, or the spec becomes
#: half a spec with HTML responses in it.
router = APIRouter(include_in_schema=False)

_TERMINAL_VALUES = {stage.value for stage in TERMINAL_STAGES}

#: Everything that should reach a browser as an explained message rather than
#: a stack trace. `InvalidTransitionError` is a *domain* error, deliberately
#: not a `ServiceError` -- the domain layer knows nothing about the service
#: layer -- so it has to be named here explicitly. Without it, clicking
#: "Reject" twice on the board renders raw JSON in the browser.
_EXPECTED_ERRORS = (ServiceError, InvalidTransitionError)


def _next_stage_map() -> dict[str, str | None]:
    """`{stage: what comes next}`, with None for the terminal stages."""
    return {
        stage.value: (None if stage in TERMINAL_STAGES else next_stage(stage).value)
        for stage in Stage
    }


def _safe_redirect_target(candidate: str | None, fallback: str) -> str:
    """Only ever redirect to a path on this site.

    A `next` field is attacker-controllable -- it arrives in a form body -- so
    an unchecked value would turn these routes into an open redirect, letting
    someone build a link that looks like this app and lands on theirs. Only
    single-slash-prefixed local paths are accepted; `//evil.test` and
    `https://evil.test` are both rejected.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def _redirect(to: str, *, flash: str | None = None, kind: str = "ok") -> RedirectResponse:
    url = to
    if flash:
        url = f"{to}?{urlencode({'flash': flash, 'kind': kind})}"
    # 303, not 302: it tells the browser to follow up with a GET, which is
    # what makes reload safe after a POST.
    return RedirectResponse(url, status_code=303)


async def _base_context(session: AsyncSession, request: Request, **extra) -> dict:
    """Context every page needs. `total_candidates` is in the page header."""
    grouped = await candidate_service.group_by_stage(session)
    return {
        "request": request,
        "grouped": grouped,
        "stages": list(Stage),
        "terminal": _TERMINAL_VALUES,
        "next_of": _next_stage_map(),
        "total_candidates": sum(len(rows) for rows in grouped.values()),
        "examples": SEARCH_EXAMPLES,
        "query": None,
        "results": None,
        "error": None,
        "flash": None,
        "flash_kind": "ok",
        **extra,
    }


@router.get("/", response_class=HTMLResponse, summary="The pipeline board and search box")
async def board(
    request: Request,
    q: str | None = None,
    flash: str | None = None,
    kind: str = "ok",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if q is not None and q.strip():
        try:
            results = await search_service.search(session, q)
        except UnparseableQueryError as exc:
            # 422, matching the JSON API exactly -- the same query gets the
            # same status whether it arrives from a browser or a script.
            context = await _base_context(
                session, request, query=q, error=exc, flash=flash, flash_kind=kind
            )
            return templates.TemplateResponse(
                request, "board.html", context, status_code=422
            )
        context = await _base_context(
            session, request, query=q, results=results, flash=flash, flash_kind=kind
        )
        return templates.TemplateResponse(request, "board.html", context)

    context = await _base_context(session, request, flash=flash, flash_kind=kind)
    return templates.TemplateResponse(request, "board.html", context)


@router.get(
    "/ui/candidates/{candidate_id}",
    response_class=HTMLResponse,
    summary="One candidate, with their complete history",
)
async def candidate_detail(
    request: Request,
    candidate_id: uuid.UUID,
    flash: str | None = None,
    kind: str = "ok",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        candidate = await candidate_service.get_candidate(session, candidate_id)
    except _EXPECTED_ERRORS as exc:
        return _redirect("/", flash=exc.message, kind="bad")

    context = await _base_context(
        session,
        request,
        candidate=candidate,
        history=serializers.to_history(candidate),
        time_in_stage=candidate_service.time_in_current_stage_seconds(candidate),
        next_stage=_next_stage_map()[candidate.current_stage.value],
        flash=flash,
        flash_kind=kind,
    )
    return templates.TemplateResponse(request, "candidate.html", context)


@router.post("/ui/candidates", summary="Add a candidate from the board")
async def create_candidate_form(
    # Declared with empty-string defaults rather than `Form(...)`: FastAPI
    # treats an empty required field as *missing* and answers with a raw 422
    # JSON body before this function runs. A recruiter who tabs past the name
    # box deserves a sentence, not a JSON array.
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not name.strip():
        return _redirect("/", flash="A name is required.", kind="bad")
    if not email.strip():
        return _redirect("/", flash="An email is required.", kind="bad")

    try:
        payload = CandidateCreate(name=name.strip(), email=email.strip(), phone=phone.strip() or None)
    except ValidationError as exc:
        # Pydantic's own message, so a too-short name or a malformed email
        # explains itself rather than silently doing nothing.
        first = exc.errors()[0]
        field = first["loc"][-1] if first.get("loc") else "input"
        return _redirect("/", flash=f"{field}: {first['msg']}", kind="bad")

    try:
        candidate = await candidate_service.create_candidate(session, payload)
    except _EXPECTED_ERRORS as exc:
        return _redirect("/", flash=exc.message, kind="bad")

    return _redirect("/", flash=f"{candidate.name} added to the pipeline.")


@router.post("/ui/candidates/{candidate_id}/advance", summary="Move a candidate forward one stage")
async def advance_form(
    candidate_id: uuid.UUID,
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    target = _safe_redirect_target(next, "/")
    try:
        candidate = await candidate_service.advance_candidate(session, candidate_id)
    except _EXPECTED_ERRORS as exc:
        return _redirect(target, flash=exc.message, kind="bad")

    return _redirect(
        target,
        flash=f"{candidate.name} moved to {candidate.current_stage.value}.",
    )


@router.post("/ui/candidates/{candidate_id}/reject", summary="Reject a candidate")
async def reject_form(
    candidate_id: uuid.UUID,
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    target = _safe_redirect_target(next, "/")
    try:
        candidate = await candidate_service.reject_candidate(session, candidate_id)
    except _EXPECTED_ERRORS as exc:
        return _redirect(target, flash=exc.message, kind="bad")

    return _redirect(target, flash=f"{candidate.name} was rejected.")


@router.post("/ui/candidates/{candidate_id}/notes", summary="Add a note to a candidate")
async def add_note_form(
    candidate_id: uuid.UUID,
    text: str = Form(""),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    target = _safe_redirect_target(next, "/")

    # Checked here rather than left to `NoteCreate`: a browser needs a
    # sentence, and the schema's own message ("String should have at least 1
    # character") describes the field rather than the problem.
    if not text.strip():
        return _redirect(target, flash="A note needs some text.", kind="bad")

    try:
        payload = NoteCreate(text=text)
    except ValidationError as exc:
        first = exc.errors()[0]
        if first["type"] == "string_too_long":
            return _redirect(
                target,
                flash=f"A note is limited to 2000 characters; that one was {len(text.strip())}.",
                kind="bad",
            )
        return _redirect(target, flash=f"note: {first['msg']}", kind="bad")

    try:
        await candidate_service.add_note(session, candidate_id, payload.text)
    except _EXPECTED_ERRORS as exc:
        return _redirect(target, flash=exc.message, kind="bad")

    return _redirect(target, flash="Note added.")
