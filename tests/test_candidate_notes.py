"""Recruiter notes: the API endpoint, the timeline merge, and the UI form.

Notes are the second append-only table. The database-level immutability guard
is covered in `test_audit_immutability.py`, which issues raw SQL; these tests
go through the application.
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
    """One client for both skins. Redirects are not followed so the
    POST-redirect-GET contract can be asserted directly."""
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


async def _create(client: AsyncClient, name: str = "Priya Sharma") -> dict:
    resp = await client.post(
        "/candidates", json={"name": name, "email": f"{uuid.uuid4()}@example.com"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _note(client: AsyncClient, candidate_id: str, text: str):
    return await client.post(f"/candidates/{candidate_id}/notes", json={"text": text})


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


async def test_adding_a_note_returns_201_and_the_note(client):
    body = await _create(client)
    resp = await _note(client, body["id"], "Called both references -- positive.")

    assert resp.status_code == 201, resp.text
    note = resp.json()
    assert note["type"] == "note"
    assert note["text"] == "Called both references -- positive."
    assert note["id"]
    assert note["at"]


async def test_a_note_on_an_unknown_candidate_is_404(client):
    resp = await _note(client, str(uuid.uuid4()), "orphan")
    assert resp.status_code == 404
    assert "No candidate with id" in resp.json()["detail"]


async def test_a_note_is_not_a_stage_change(client):
    """It must not move the candidate, and must not touch the audit trail."""
    body = await _create(client)
    await _note(client, body["id"], "asked for a portfolio")

    after = (await client.get(f"/candidates/{body['id']}")).json()
    assert after["current_stage"] == "applied"
    assert after["current_stage_since"] == body["current_stage_since"]
    # Two entries now: the creation transition and the note. No new transition.
    assert [e["type"] for e in after["history"]] == ["transition", "note"]


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", "  \n  "])
async def test_a_blank_note_is_rejected(client, text):
    """Whitespace is stripped *before* the length check, so a note of only
    spaces is a 422 rather than a blank row in an append-only table -- which
    could never be cleaned up afterwards."""
    body = await _create(client)
    resp = await _note(client, body["id"], text)
    assert resp.status_code == 422


async def test_a_note_over_the_length_limit_is_rejected(client):
    body = await _create(client)
    resp = await _note(client, body["id"], "x" * 2001)
    assert resp.status_code == 422


async def test_a_note_at_the_length_limit_is_accepted(client):
    body = await _create(client)
    resp = await _note(client, body["id"], "x" * 2000)
    assert resp.status_code == 201


async def test_surrounding_whitespace_is_stripped(client):
    body = await _create(client)
    resp = await _note(client, body["id"], "  called references  ")
    assert resp.json()["text"] == "called references"


# --------------------------------------------------------------------------
# The merged timeline
# --------------------------------------------------------------------------


async def test_a_note_is_merged_into_the_timeline_in_time_order(client):
    """Transitions and notes share one stream, ordered by when they happened."""
    body = await _create(client)  # transition: applied
    await _note(client, body["id"], "first note")
    await client.post(f"/candidates/{body['id']}/advance")  # transition: screening
    await _note(client, body["id"], "second note")

    history = (await client.get(f"/candidates/{body['id']}")).json()["history"]
    assert [e["type"] for e in history] == [
        "transition",
        "note",
        "transition",
        "note",
    ]
    assert history[1]["text"] == "first note"
    assert history[3]["text"] == "second note"


async def test_the_timeline_is_ordered_by_timestamp(client):
    """The interleaving test above assumes the clock separated four separate
    requests. Assert that directly, so a resolution problem reports itself as
    a resolution problem rather than as a confusing order mismatch."""
    body = await _create(client)
    await _note(client, body["id"], "a")
    await client.post(f"/candidates/{body['id']}/advance")
    await _note(client, body["id"], "b")

    history = (await client.get(f"/candidates/{body['id']}")).json()["history"]
    ats = [e["at"] for e in history]
    assert ats == sorted(ats), f"timestamps not strictly ordered: {ats}"


async def test_the_history_endpoint_includes_notes_and_still_matches(client):
    """The two endpoints share one builder, so this equality is structural
    rather than two orderings that happen to agree."""
    body = await _create(client)
    await _note(client, body["id"], "a note")

    embedded = (await client.get(f"/candidates/{body['id']}")).json()["history"]
    standalone = (await client.get(f"/candidates/{body['id']}/history")).json()
    assert standalone == embedded
    assert [e["type"] for e in standalone] == ["transition", "note"]


async def test_a_note_keeps_the_one_clock_invariant(client):
    """`current_stage_since` still equals the last *transition*, not the last
    timeline entry -- a trailing note must not appear to move the candidate."""
    body = await _create(client)
    moved = (await client.post(f"/candidates/{body['id']}/advance")).json()
    await _note(client, body["id"], "noted after moving")

    after = (await client.get(f"/candidates/{body['id']}")).json()
    last_transition = [e for e in after["history"] if e["type"] == "transition"][-1]
    assert after["current_stage_since"] == last_transition["at"]
    # ...and the note really is the last entry, so the assertion has teeth.
    assert after["history"][-1]["type"] == "note"


# --------------------------------------------------------------------------
# The UI
# --------------------------------------------------------------------------


async def test_a_note_appears_on_the_candidate_page(client, session):
    candidate = await add_candidate(
        session, "Anita Desai", [(Stage.SCREENING, timedelta(days=9))]
    )
    await session.commit()
    await _note(client, str(candidate.id), "called her references")

    body = (await client.get(f"/ui/candidates/{candidate.id}")).text
    assert "called her references" in body
    assert '<span class="badge note">note</span>' in body


async def test_the_note_form_adds_a_note(client, session):
    candidate = await add_candidate(session, "Form Person", [])
    await session.commit()

    resp = await client.post(
        f"/ui/candidates/{candidate.id}/notes",
        data={"text": "spoke to her manager", "next": f"/ui/candidates/{candidate.id}"},
    )
    assert resp.status_code == 303
    assert "Note+added" in resp.headers["location"]

    body = (await client.get(f"/ui/candidates/{candidate.id}")).text
    assert "spoke to her manager" in body


async def test_the_note_form_explains_a_blank_note(client, session):
    candidate = await add_candidate(session, "Blank Person", [])
    await session.commit()

    resp = await client.post(
        f"/ui/candidates/{candidate.id}/notes", data={"text": "   ", "next": "/"}
    )
    assert resp.status_code == 303
    location = resp.headers["location"].replace("+", " ")
    assert "kind=bad" in location
    assert "needs some text" in location


async def test_the_note_form_explains_an_over_long_note(client, session):
    candidate = await add_candidate(session, "Wordy Person", [])
    await session.commit()

    resp = await client.post(
        f"/ui/candidates/{candidate.id}/notes", data={"text": "x" * 2001, "next": "/"}
    )
    location = resp.headers["location"].replace("+", " ")
    assert "kind=bad" in location
    assert "2000 characters" in location


async def test_a_terminal_candidate_can_still_be_noted(client, session):
    """The move buttons are gone once a candidate is hired or rejected, but
    there is still plenty worth writing down about them."""
    candidate = await add_candidate(
        session, "Hired Person", [(Stage.HIRED, timedelta(days=2))]
    )
    await session.commit()

    body = (await client.get(f"/ui/candidates/{candidate.id}")).text
    assert "/advance" not in body
    assert "/reject" not in body
    assert f"/ui/candidates/{candidate.id}/notes" in body
