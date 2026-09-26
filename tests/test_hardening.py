"""The changes made because this app is reachable by anyone with the link.

None of these test the domain. They test the four places where being public
changes what the code has to do: a health check that must actually check, an
input with a bound on it, an error handler for the errors nobody predicted,
and a fixture loader that must not fire by accident.

Each one is here rather than in an existing file because they share a reason
for existing, and that reason is worth being able to read in one place.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import main
from app.db import get_session
from app.main import app
from app.services.errors import UnparseableQueryError
from app.services.search import service
from tests.factories import add_candidate


@pytest.fixture
async def client(engine):
    """A client that returns 500s instead of re-raising them.

    `raise_app_exceptions=False` is the whole point: the stock fixture lets an
    unhandled exception propagate into the test, which is what you want almost
    everywhere and exactly what makes the unexpected-error handler untestable.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# /health
# --------------------------------------------------------------------------


async def test_health_is_ok_when_the_database_answers(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_503_when_the_database_does_not_answer(client, monkeypatch):
    """The point of the change: a health check must be able to fail.

    Before this, `/health` returned a constant, so it reported 200 for a
    process that could not serve a single request. A monitor watching it
    would learn nothing.
    """

    class _Unreachable:
        def connect(self):
            raise OSError("connection refused")

    monkeypatch.setattr(main, "get_engine", lambda: _Unreachable())

    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_health_does_not_leak_the_connection_string(client, monkeypatch):
    """A driver's exception text routinely contains the URL and its password.

    This endpoint is public, so the failure is logged and not returned.
    """

    class _Unreachable:
        def connect(self):
            raise OSError("could not connect to postgresql://app:hunter2@db:5432/x")

    monkeypatch.setattr(main, "get_engine", lambda: _Unreachable())

    response = await client.get("/health")
    assert "hunter2" not in response.text
    assert "postgresql" not in response.text


# --------------------------------------------------------------------------
# The query-length bound
# --------------------------------------------------------------------------


async def test_a_query_over_the_limit_is_rejected(session):
    """Enforced in the service, so every entry point inherits it."""
    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "a" * (service.MAX_QUERY_LENGTH + 1))
    assert str(service.MAX_QUERY_LENGTH) in excinfo.value.reason


async def test_a_query_exactly_at_the_limit_is_not_rejected_for_length(session):
    """The boundary itself. Off-by-one here would reject a legitimate query.

    A query of exactly the limit still fails -- nothing matches 200 a's -- but
    it must fail as an *unreadable name*, not as an over-long query. Asserting
    on the reason rather than the exception type is what distinguishes the two.
    """
    await add_candidate(session, "Priya Sharma")
    await session.commit()

    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "a" * service.MAX_QUERY_LENGTH)
    assert "characters long" not in excinfo.value.reason


async def test_the_json_api_gives_an_overlong_query_the_usual_422(client, session):
    """Same three-field contract as any other unreadable query.

    `Query(max_length=...)` would have been one line, and would have returned
    FastAPI's validation body instead -- a different shape from every other
    422 this API produces.
    """
    response = await client.get(
        "/search", params={"q": "a" * (service.MAX_QUERY_LENGTH + 1)}
    )
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail", "reason", "examples"}


async def test_the_html_route_gives_an_overlong_query_a_readable_page(
    client, session
):
    """The browser gets HTML, not a JSON validation error.

    This is the case `Query(max_length=...)` would have actively broken: a
    recruiter pasting something long would have been shown raw JSON.
    """
    response = await client.get(
        "/", params={"q": "a" * (service.MAX_QUERY_LENGTH + 1)}
    )
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]


# --------------------------------------------------------------------------
# The unexpected-error handler
# --------------------------------------------------------------------------


@pytest.fixture
def explode(monkeypatch):
    """Make the search service raise something nobody planned for."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(service, "search", _boom)


async def test_an_unexpected_error_returns_json_to_a_script(client, explode):
    response = await client.get("/search", params={"q": "anything"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}


async def test_an_unexpected_error_returns_html_to_a_browser(client, explode):
    response = await client.get(
        "/", params={"q": "anything"}, headers={"accept": "text/html"}
    )
    assert response.status_code == 500
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text


async def test_an_unexpected_error_does_not_leak_its_traceback(client, explode):
    """The message is ours; the traceback goes to the log.

    Exception text names file paths and, for a database error, the statement
    that failed. Neither belongs in a response on a public endpoint.
    """
    for path, params in (("/search", {"q": "x"}), ("/", {"q": "x"})):
        response = await client.get(path, params=params)
        assert response.status_code == 500
        assert "kaboom" not in response.text
        assert "Traceback" not in response.text
