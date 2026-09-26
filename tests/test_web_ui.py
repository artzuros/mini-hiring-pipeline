"""Tests for the server-rendered UI.

The UI is a second skin over the same services as the JSON API, so these
tests deliberately do not re-test the pipeline rules. What they check is the
things that can only go wrong in the HTML layer: routes shadowing the API,
illegal moves rendering as raw JSON, and form input failing without an
explanation.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_session
from app.domain.pipeline import Stage
from app.main import app
from tests.factories import add_candidate


@pytest.fixture
async def client(engine):
    """An HTML client. Redirects are NOT followed, so the POST-redirect-GET
    contract can be asserted directly rather than inferred from the page."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _override_get_session():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c
    app.dependency_overrides.clear()


def _form(name="Test Person", email=None, phone=""):
    return {"name": name, "email": email or f"{uuid.uuid4()}@example.com", "phone": phone}


# --------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------


async def test_the_board_lists_every_stage_even_when_empty(client):
    """The shape of the pipeline is the point; an absent column would hide it."""
    response = await client.get("/")
    assert response.status_code == 200
    for stage in Stage:
        assert f"<span>{stage.value}</span>" in response.text


async def test_the_board_groups_candidates_under_their_stage(client, session):
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await add_candidate(session, "Vikram Singh", [
        (Stage.INTERVIEW, timedelta(days=30)), (Stage.HIRED, timedelta(days=5))
    ])
    await session.commit()

    body = (await client.get("/")).text
    assert "Priya Sharma" in body
    assert "Vikram Singh" in body


async def test_the_board_has_a_search_box_and_an_add_form(client):
    body = (await client.get("/")).text
    assert 'name="q"' in body
    assert 'action="/ui/candidates"' in body


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


async def test_search_results_are_rendered_best_first(client, session):
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=30))],
                        email="a@example.com")
    await add_candidate(session, "Priya Nair", [(Stage.SCREENING, timedelta(days=1))],
                        email="b@example.com")
    await session.commit()

    body = (await client.get("/", params={"q": "Priya Sharma"})).text
    assert body.index("Priya Sharma") < body.index("Priya Nair")


async def test_an_unparseable_query_explains_itself(client, session):
    """The assignment's headline requirement, rendered for a person."""
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    response = await client.get("/", params={"q": "asdkfjasldkfj"})
    # Same status the JSON API would return for the same query.
    assert response.status_code == 422
    assert "understand this query" in response.text
    assert "No candidate matches the name" in response.text
    # ...and it offers a way forward rather than a dead end.
    assert "stuck in Screening for more than a week" in response.text


async def test_a_typod_name_finds_the_candidate(client, session):
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    body = (await client.get("/", params={"q": "sharam"})).text
    assert "Priya Sharma" in body


async def test_an_understood_query_matching_nobody_says_so_plainly(client, session):
    """Distinct from the error case: understood, and the answer is 'nobody'."""
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    response = await client.get("/", params={"q": "in Offer"})
    assert response.status_code == 200
    assert "was understood" in response.text
    assert "understand this query" not in response.text


# --------------------------------------------------------------------------
# Mutations: POST-redirect-GET
# --------------------------------------------------------------------------


async def test_adding_a_candidate_redirects_and_flashes(client):
    response = await client.post("/ui/candidates", data=_form("New Person"))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?flash=")
    assert (await client.get("/")).status_code == 200


async def test_a_duplicate_email_is_explained_not_crashed(client):
    form = _form("Dupe Person", email="dupe@example.com")
    await client.post("/ui/candidates", data=form)

    response = await client.post("/ui/candidates", data=form)
    assert response.status_code == 303
    assert "kind=bad" in response.headers["location"]
    assert "already exists" in response.headers["location"].replace("+", " ")


@pytest.mark.parametrize(
    "form,expected",
    [
        ({"name": "", "email": "a@b.co", "phone": ""}, "name is required"),
        ({"name": "   ", "email": "a@b.co", "phone": ""}, "name is required"),
        ({"name": "Ok Name", "email": "", "phone": ""}, "email is required"),
    ],
)
async def test_missing_form_fields_are_explained_not_422_json(client, form, expected):
    """FastAPI would answer a bare 422 for these. A browser needs a sentence."""
    response = await client.post("/ui/candidates", data=form)
    assert response.status_code == 303
    assert expected in response.headers["location"].replace("+", " ")


async def test_advancing_moves_exactly_one_stage(client, session):
    candidate = await add_candidate(session, "Mover", [])
    await session.commit()

    response = await client.post(f"/ui/candidates/{candidate.id}/advance", data={"next": "/"})
    assert response.status_code == 303
    assert "moved to screening" in response.headers["location"].replace("+", " ")


async def test_an_illegal_move_explains_itself_instead_of_returning_json(client, session):
    """Regression pin.

    `InvalidTransitionError` is a domain error, not a `ServiceError`. Before
    the web routes caught it, clicking Reject twice rendered a raw JSON error
    body in the browser.
    """
    candidate = await add_candidate(session, "Terminal", [(Stage.REJECTED, timedelta(days=1))])
    await session.commit()

    response = await client.post(f"/ui/candidates/{candidate.id}/advance", data={"next": "/"})
    assert response.status_code == 303
    location = response.headers["location"].replace("+", " ")
    assert "kind=bad" in location
    assert "terminal stage" in location


async def test_a_redirect_never_leaves_the_site(client, session):
    """The `next` field is attacker-controllable; an open redirect would let
    someone craft a link that looks like this app and lands elsewhere."""
    candidate = await add_candidate(session, "Guarded", [])
    await session.commit()

    for hostile in ["https://evil.test", "//evil.test", "javascript:alert(1)"]:
        response = await client.post(
            f"/ui/candidates/{candidate.id}/advance", data={"next": hostile}
        )
        location = response.headers["location"]
        assert location.startswith("/"), location
        assert "evil.test" not in location


# --------------------------------------------------------------------------
# The candidate page
# --------------------------------------------------------------------------


async def test_the_detail_page_shows_the_complete_history(client, session):
    candidate = await add_candidate(
        session,
        "Anita Desai",
        [(Stage.INTERVIEW, timedelta(days=18)), (Stage.OFFER, timedelta(days=6)),
         (Stage.REJECTED, timedelta(days=3))],
    )
    await session.commit()

    body = (await client.get(f"/ui/candidates/{candidate.id}")).text
    assert "Anita Desai" in body
    # Every stage she passed through appears, not just her current one.
    for stage in ["applied", "interview", "offer", "rejected"]:
        assert f'<span class="badge {stage}">{stage}</span>' in body
    assert "terminal" in body


async def test_a_terminal_candidate_offers_no_move_buttons(client, session):
    candidate = await add_candidate(session, "Done", [(Stage.HIRED, timedelta(days=2))])
    await session.commit()

    body = (await client.get(f"/ui/candidates/{candidate.id}")).text
    assert "/advance" not in body
    assert "/reject" not in body


async def test_the_detail_page_shows_time_in_current_stage(client, session):
    candidate = await add_candidate(session, "Waiting", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    body = (await client.get(f"/ui/candidates/{candidate.id}")).text
    assert "9 days" in body


async def test_an_unknown_candidate_redirects_home_with_an_explanation(client):
    response = await client.get(f"/ui/candidates/{uuid.uuid4()}")
    assert response.status_code == 303
    assert "kind=bad" in response.headers["location"]


# --------------------------------------------------------------------------
# The UI must not shadow the API
# --------------------------------------------------------------------------


async def test_the_api_still_owns_its_own_paths(client, session):
    """`/candidates/{id}` is the JSON API's. The HTML page lives under /ui
    precisely so that mounting the UI cannot change the API's contract."""
    candidate = await add_candidate(session, "Api Person", [])
    await session.commit()

    response = await client.get(f"/candidates/{candidate.id}")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["name"] == "Api Person"

    page = await client.get(f"/ui/candidates/{candidate.id}")
    assert page.headers["content-type"].startswith("text/html")


async def test_the_board_does_not_leak_into_the_openapi_schema(client):
    schema = (await client.get("/openapi.json")).json()
    assert "/" not in schema["paths"]
    assert "/search" in schema["paths"]
