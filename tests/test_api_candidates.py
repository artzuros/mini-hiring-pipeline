"""API-level tests, following the assignment's own checklist.

Each test name states the requirement it pins down.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_session
from app.main import app


@pytest.fixture
async def client(engine):
    """An HTTP client talking to the real app, backed by the test database."""
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
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _payload(name: str = "Priya Sharma", email: str | None = None) -> dict:
    return {
        "name": name,
        "email": email or f"{uuid.uuid4()}@example.com",
        "phone": "+91 98765 43210",
        "resume_url": None,
    }


async def _create(client: AsyncClient, **kwargs) -> dict:
    resp = await client.post("/candidates", json=_payload(**kwargs))
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


async def test_create_returns_201_and_lands_in_applied(client):
    body = await _create(client)
    assert body["current_stage"] == "applied"
    assert body["name"] == "Priya Sharma"
    assert body["time_in_current_stage_seconds"] < 60


async def test_creation_writes_the_first_audit_row(client):
    """The trail starts at creation, not at the first move."""
    body = await _create(client)
    history = body["history"]
    assert len(history) == 1
    assert history[0]["from_stage"] is None
    assert history[0]["to_stage"] == "applied"


async def test_duplicate_email_is_rejected_with_409(client):
    email = "dupe@example.com"
    await _create(client, email=email)
    resp = await client.post("/candidates", json=_payload(email=email))
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


async def test_duplicate_email_is_case_insensitive(client):
    """Otherwise 'Priya@x.com' and 'priya@x.com' become two candidates."""
    await _create(client, email="Case@Example.com")
    resp = await client.post("/candidates", json=_payload(email="case@example.com"))
    assert resp.status_code == 409


async def test_missing_name_is_rejected(client):
    resp = await client.post(
        "/candidates", json={"name": "", "email": "x@example.com"}
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


async def test_get_unknown_candidate_is_404(client):
    resp = await client.get(f"/candidates/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert "No candidate with id" in resp.json()["detail"]


async def test_malformed_uuid_is_422_not_500(client):
    resp = await client.get("/candidates/not-a-uuid")
    assert resp.status_code == 422


async def test_history_endpoint_returns_the_same_rows_as_the_embedded_array(client):
    body = await _create(client)
    resp = await client.get(f"/candidates/{body['id']}/history")
    assert resp.status_code == 200
    assert resp.json() == body["history"]


async def test_history_for_unknown_candidate_is_404_not_empty_list(client):
    """An empty list would read as 'this candidate has no history'."""
    resp = await client.get(f"/candidates/{uuid.uuid4()}/history")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# The forward path
# --------------------------------------------------------------------------


async def test_walking_the_full_pipeline_reaches_hired(client):
    body = await _create(client)
    cid = body["id"]

    for expected in ["screening", "interview", "offer", "hired"]:
        resp = await client.post(f"/candidates/{cid}/advance")
        assert resp.status_code == 200, resp.text
        assert resp.json()["current_stage"] == expected

    final = await client.get(f"/candidates/{cid}")
    assert [h["to_stage"] for h in final.json()["history"]] == [
        "applied",
        "screening",
        "interview",
        "offer",
        "hired",
    ]


async def test_advance_records_a_note_on_the_audit_row(client):
    body = await _create(client)
    resp = await client.post(
        f"/candidates/{body['id']}/advance", json={"note": "Strong portfolio"}
    )
    history = resp.json()["history"]
    assert history[-1]["note"] == "Strong portfolio"
    assert history[0]["note"] is None


async def test_advance_without_a_body_is_allowed(client):
    body = await _create(client)
    resp = await client.post(f"/candidates/{body['id']}/advance")
    assert resp.status_code == 200
    assert resp.json()["history"][-1]["note"] is None


async def test_cannot_advance_past_hired(client):
    body = await _create(client)
    cid = body["id"]
    for _ in range(4):
        await client.post(f"/candidates/{cid}/advance")

    resp = await client.post(f"/candidates/{cid}/advance")
    assert resp.status_code == 422
    assert "terminal" in resp.json()["detail"]


async def test_cannot_advance_after_rejection(client):
    body = await _create(client)
    cid = body["id"]
    await client.post(f"/candidates/{cid}/reject")

    resp = await client.post(f"/candidates/{cid}/advance")
    assert resp.status_code == 422
    assert "terminal" in resp.json()["detail"]


async def test_a_target_stage_in_the_body_cannot_be_used_to_skip(client):
    """There is no parameter that names a destination stage.

    Extra keys are ignored, so the request still moves exactly one stage --
    skipping is not expressible through this API, rather than being validated
    against.
    """
    body = await _create(client)
    resp = await client.post(
        f"/candidates/{body['id']}/advance", json={"to_stage": "hired"}
    )
    assert resp.status_code == 200
    assert resp.json()["current_stage"] == "screening"


# --------------------------------------------------------------------------
# Rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("steps", [0, 1, 2, 3])
async def test_can_reject_from_every_non_terminal_stage(client, steps):
    body = await _create(client)
    cid = body["id"]
    for _ in range(steps):
        await client.post(f"/candidates/{cid}/advance")

    resp = await client.post(f"/candidates/{cid}/reject", json={"note": "Not a fit"})
    assert resp.status_code == 200
    assert resp.json()["current_stage"] == "rejected"
    assert resp.json()["history"][-1]["note"] == "Not a fit"


async def test_cannot_reject_after_hired(client):
    body = await _create(client)
    cid = body["id"]
    for _ in range(4):
        await client.post(f"/candidates/{cid}/advance")

    resp = await client.post(f"/candidates/{cid}/reject")
    assert resp.status_code == 422
    assert "terminal" in resp.json()["detail"]


async def test_cannot_reject_twice(client):
    """A final outcome cannot be reversed or restated."""
    body = await _create(client)
    cid = body["id"]
    await client.post(f"/candidates/{cid}/reject")

    resp = await client.post(f"/candidates/{cid}/reject")
    assert resp.status_code == 422

    # And the audit trail still shows exactly one rejection.
    final = await client.get(f"/candidates/{cid}")
    history = final.json()["history"]
    assert [h["to_stage"] for h in history] == ["applied", "rejected"]


async def test_advance_and_reject_on_unknown_candidate_are_404(client):
    cid = uuid.uuid4()
    assert (await client.post(f"/candidates/{cid}/advance")).status_code == 404
    assert (await client.post(f"/candidates/{cid}/reject")).status_code == 404


# --------------------------------------------------------------------------
# Listing and grouping
# --------------------------------------------------------------------------


async def test_flat_list_is_newest_first(client):
    names = ["First", "Second", "Third"]
    for name in names:
        await _create(client, name=name)

    resp = await client.get("/candidates")
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()] == list(reversed(names))


async def test_group_by_stage_always_returns_every_stage_key(client):
    await _create(client)

    resp = await client.get("/candidates", params={"group_by": "stage"})
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) == {
        "applied",
        "screening",
        "interview",
        "offer",
        "hired",
        "rejected",
    }
    assert len(body["applied"]) == 1
    # Empty stages are present as empty lists, not omitted.
    assert body["hired"] == []


async def test_group_by_stage_places_candidates_in_the_right_bucket(client):
    a = await _create(client, name="A")
    b = await _create(client, name="B")
    await client.post(f"/candidates/{b['id']}/advance")
    await client.post(f"/candidates/{a['id']}/reject")

    body = (await client.get("/candidates", params={"group_by": "stage"})).json()
    assert [c["name"] for c in body["applied"]] == []
    assert [c["name"] for c in body["screening"]] == ["B"]
    assert [c["name"] for c in body["rejected"]] == ["A"]


async def test_unknown_group_by_value_explains_itself(client):
    resp = await client.get("/candidates", params={"group_by": "stages"})
    assert resp.status_code == 422
    assert "stages" in resp.json()["detail"]
    assert "only supported value is 'stage'" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Derived values
# --------------------------------------------------------------------------


async def test_time_in_current_stage_resets_when_the_stage_changes(client):
    """The value is derived from `current_stage_since`, and moves with it."""
    body = await _create(client)
    assert body["time_in_current_stage_seconds"] < 60
    original_since = body["current_stage_since"]

    moved = await client.post(f"/candidates/{body['id']}/advance")
    after = moved.json()
    assert after["current_stage_since"] != original_since
    assert after["time_in_current_stage_seconds"] < 60


async def test_stage_change_timestamp_matches_the_audit_row(client):
    """One clock: the cached stage timestamp and the audit row agree exactly.

    Both are written from Postgres `now()`, which is the transaction
    timestamp, so they are identical rather than merely close.
    """
    body = await _create(client)
    moved = (await client.post(f"/candidates/{body['id']}/advance")).json()
    assert moved["current_stage_since"] == moved["history"][-1]["transitioned_at"]
