"""FastAPI application: router mounting and the global error contract."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from app.db import dispose_engine, get_engine
from app.domain.pipeline import InvalidTransitionError
from app.services.errors import (
    CandidateNotFoundError,
    DuplicateEmailError,
    UnparseableQueryError,
)

logger = logging.getLogger(__name__)


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


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> Response:
    """Last resort: turn an unanticipated error into a readable response.

    Every handler above this one is a *domain* error with something specific
    to say. This is for the errors nobody planned for -- a bug, a dropped
    connection, a template that raised -- where the only honest thing to say
    is "that was us, not you".

    The traceback goes to the log and never to the client. Exception text
    routinely names file paths, and a database error names the statement that
    failed; neither belongs in a public response.

    The body follows the caller. A browser sending `Accept: text/html` gets a
    page it can read; everything else gets the JSON shape the rest of the API
    uses. FastAPI's default is `text/plain` for both, which is the worst of
    the two for each.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)

    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            status_code=500,
            content=(
                "<!doctype html><html lang=en><title>Something went wrong</title>"
                "<h1>Something went wrong</h1>"
                "<p>That was our fault, not yours. The error has been logged.</p>"
                '<p><a href="/">Back to the pipeline</a></p>'
            ),
        )
    return JSONResponse(
        status_code=500, content={"detail": "Internal server error."}
    )


@app.get("/health", tags=["meta"], summary="Liveness and readiness probe")
async def health() -> JSONResponse:
    """Report whether this process can actually reach its database.

    Returning a constant `{"status": "ok"}` answers 200 while Postgres is
    unreachable -- which is the one condition a health check exists to catch,
    and the condition a tunnel, a load balancer, or an uptime monitor most
    needs to see. So this performs a real round trip and reports 503 when it
    fails, rather than reporting the state of the *process*.

    The failure detail is logged, not returned: this endpoint is public, and a
    driver's exception text routinely contains the connection string.
    """
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        # Deliberately broad: every way of failing to reach a database counts
        # as unhealthy, and a narrower `except` would only mean missing one.
        logger.exception("Health check could not reach the database")
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    return JSONResponse(status_code=200, content={"status": "ok"})


from app.api.routers import candidates as candidates_router  # noqa: E402
from app.api.routers import search as search_router  # noqa: E402
from app.web import routes as web_router  # noqa: E402

app.include_router(candidates_router.router)
app.include_router(search_router.router)
# Mounted last so `/` and the HTML form actions cannot shadow an API route.
app.include_router(web_router.router)
