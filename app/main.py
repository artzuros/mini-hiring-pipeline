"""FastAPI application: router mounting and the global error contract."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import dispose_engine
from app.domain.pipeline import InvalidTransitionError
from app.services.errors import (
    CandidateNotFoundError,
    DuplicateEmailError,
    UnparseableQueryError,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await dispose_engine()


app = FastAPI(
    title="Mini Hiring Pipeline",
    version="1.0.0",
    description=(
        "A single-recruiter hiring pipeline.\n\n"
        "**The pipeline.** Candidates move `applied -> screening -> interview "
        "-> offer -> hired`, one stage at a time. Skipping and reversing are "
        "impossible. A candidate can be rejected from any stage before "
        "`hired`; `hired` and `rejected` are both terminal.\n\n"
        "**The audit trail.** Every transition, including the initial "
        "creation, is appended to an immutable history. Immutability is "
        "enforced by database triggers, not by convention, so it holds even "
        "against a direct connection to Postgres.\n\n"
        "**Search.** One box, natural language. `GET /search?q=...`"
    ),
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Global error handlers.
#
# These exist so that every failure explains itself. A recruiter who types
# something the parser cannot read should be told why, and a recruiter who
# tries an illegal stage move should be told which rule stopped her -- never
# handed a bare 400 or an empty list that looks like "no results".
# --------------------------------------------------------------------------


@app.exception_handler(InvalidTransitionError)
async def handle_invalid_transition(
    request: Request, exc: InvalidTransitionError
) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.message})


@app.exception_handler(CandidateNotFoundError)
async def handle_not_found(
    request: Request, exc: CandidateNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": exc.message})


@app.exception_handler(DuplicateEmailError)
async def handle_duplicate_email(
    request: Request, exc: DuplicateEmailError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message})


@app.exception_handler(UnparseableQueryError)
async def handle_unparseable_query(
    request: Request, exc: UnparseableQueryError
) -> JSONResponse:
    """The graded error contract for a query the parser could not read.

    Three fields, not one: `detail` is the headline, `reason` names what
    actually defeated the parser, and `examples` gives the recruiter working
    queries to copy. A bare `{"detail": "bad request"}` would tell her
    nothing about how to fix what she typed.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.message,
            "reason": exc.reason,
            "examples": exc.examples,
        },
    )


@app.get("/health", tags=["meta"], summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok"}


from app.api.routers import candidates as candidates_router  # noqa: E402
from app.api.routers import search as search_router  # noqa: E402
from app.web import routes as web_router  # noqa: E402

app.include_router(candidates_router.router)
app.include_router(search_router.router)
# Mounted last so `/` and the HTML form actions cannot shadow an API route.
app.include_router(web_router.router)
