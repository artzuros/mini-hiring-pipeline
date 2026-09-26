"""The audit trail is append-only, and the *database* is what enforces it.

These tests deliberately do not go through the application at all. They open a
raw connection and issue UPDATE/DELETE directly, which is exactly what a bug,
a careless migration, or a person at a psql prompt would do. If the guarantee
only held in application code, these would pass while the real invariant was
broken.

Both append-only tables are covered: `stage_transitions` (migration 0002) and
`candidate_notes` (migration 0003). They have separate trigger functions, so a
mistake in one would not show up in the other's tests.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


async def _insert_candidate_and_transition(conn) -> uuid.UUID:
    candidate_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO candidates (id, name, email) "
            "VALUES (:id, 'Audit Test', :email)"
        ),
        {"id": candidate_id, "email": f"{candidate_id}@example.com"},
    )
    await conn.execute(
        text(
            "INSERT INTO stage_transitions (candidate_id, from_stage, to_stage, note) "
            "VALUES (:id, NULL, 'applied', 'created')"
        ),
        {"id": candidate_id},
    )
    await conn.commit()
    return candidate_id


async def _insert_candidate_and_note(conn) -> uuid.UUID:
    candidate_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO candidates (id, name, email) "
            "VALUES (:id, 'Note Test', :email)"
        ),
        {"id": candidate_id, "email": f"{candidate_id}@example.com"},
    )
    await conn.execute(
        text("INSERT INTO candidate_notes (candidate_id, text) VALUES (:id, 'original')"),
        {"id": candidate_id},
    )
    await conn.commit()
    return candidate_id


async def test_update_on_stage_transitions_is_rejected(raw_connection):
    conn = raw_connection
    await _insert_candidate_and_transition(conn)

    with pytest.raises(DBAPIError) as excinfo:
        await conn.execute(text("UPDATE stage_transitions SET note = 'rewritten'"))

    assert "immutable" in str(excinfo.value)
    await conn.rollback()


async def test_delete_on_stage_transitions_is_rejected(raw_connection):
    conn = raw_connection
    await _insert_candidate_and_transition(conn)

    with pytest.raises(DBAPIError) as excinfo:
        await conn.execute(text("DELETE FROM stage_transitions"))

    assert "immutable" in str(excinfo.value)
    await conn.rollback()


async def test_the_original_row_survives_a_rejected_update(raw_connection):
    """The guard must *refuse*, not silently no-op and not corrupt."""
    conn = raw_connection
    await _insert_candidate_and_transition(conn)

    with pytest.raises(DBAPIError):
        await conn.execute(
            text("UPDATE stage_transitions SET to_stage = 'hired'")
        )
    await conn.rollback()

    result = await conn.execute(text("SELECT to_stage, note FROM stage_transitions"))
    rows = result.all()
    assert len(rows) == 1
    assert rows[0].to_stage == "applied"
    assert rows[0].note == "created"


async def test_the_guard_reports_the_blocked_operation(raw_connection):
    """The error should say whether it was an UPDATE or a DELETE."""
    conn = raw_connection
    await _insert_candidate_and_transition(conn)

    with pytest.raises(DBAPIError) as excinfo:
        await conn.execute(text("DELETE FROM stage_transitions"))

    assert "DELETE" in str(excinfo.value)
    await conn.rollback()


async def test_insert_is_still_allowed(raw_connection):
    """Immutability must not accidentally make the trail un-writable."""
    conn = raw_connection
    candidate_id = await _insert_candidate_and_transition(conn)

    await conn.execute(
        text(
            "INSERT INTO stage_transitions (candidate_id, from_stage, to_stage) "
            "VALUES (:id, 'applied', 'screening')"
        ),
        {"id": candidate_id},
    )
    await conn.commit()

    result = await conn.execute(
        text("SELECT count(*) FROM stage_transitions WHERE candidate_id = :id"),
        {"id": candidate_id},
    )
    assert result.scalar_one() == 2


async def test_truncate_clears_the_table_despite_the_guard(raw_connection):
    """Documents why conftest uses TRUNCATE for cleanup.

    The guard is a row-level BEFORE DELETE trigger. TRUNCATE is statement
    level and does not fire it, so test cleanup works. If someone later
    changes the triggers to statement-level, cleanup breaks loudly here
    rather than mysteriously in an unrelated test.
    """
    conn = raw_connection
    await _insert_candidate_and_transition(conn)

    await conn.execute(
        text("TRUNCATE candidate_notes, stage_transitions, candidates CASCADE")
    )
    await conn.commit()

    result = await conn.execute(text("SELECT count(*) FROM stage_transitions"))
    assert result.scalar_one() == 0


# --------------------------------------------------------------------------
# candidate_notes: the same guarantee, a different table and trigger
# --------------------------------------------------------------------------


async def test_update_on_candidate_notes_is_rejected(raw_connection):
    conn = raw_connection
    await _insert_candidate_and_note(conn)

    with pytest.raises(DBAPIError) as excinfo:
        await conn.execute(text("UPDATE candidate_notes SET text = 'rewritten'"))

    assert "immutable" in str(excinfo.value)
    await conn.rollback()


async def test_delete_on_candidate_notes_is_rejected(raw_connection):
    conn = raw_connection
    await _insert_candidate_and_note(conn)

    with pytest.raises(DBAPIError) as excinfo:
        await conn.execute(text("DELETE FROM candidate_notes"))

    assert "immutable" in str(excinfo.value)
    await conn.rollback()


async def test_the_note_error_names_its_own_table(raw_connection):
    """Not a cosmetic detail.

    The message has to name `candidate_notes`, not `stage_transitions`. If the
    two tables shared one trigger function, an operator who tried to edit a
    note would be told the wrong table was immutable and would go looking in
    the wrong place.
    """
    conn = raw_connection
    await _insert_candidate_and_note(conn)

    with pytest.raises(DBAPIError) as excinfo:
        await conn.execute(text("UPDATE candidate_notes SET text = 'rewritten'"))

    message = str(excinfo.value)
    assert "candidate_notes" in message
    assert "stage_transitions" not in message
    await conn.rollback()


async def test_the_original_note_survives_a_rejected_update(raw_connection):
    conn = raw_connection
    await _insert_candidate_and_note(conn)

    with pytest.raises(DBAPIError):
        await conn.execute(text("UPDATE candidate_notes SET text = 'rewritten'"))
    await conn.rollback()

    result = await conn.execute(text("SELECT text FROM candidate_notes"))
    assert [row.text for row in result.all()] == ["original"]


async def test_a_candidate_with_notes_cannot_be_deleted(raw_connection):
    """The FK is restrictive, deliberately.

    `ON DELETE CASCADE` here would let notes vanish along with a candidate --
    the same silent loss of history the triggers exist to prevent, reached by
    a different route.
    """
    conn = raw_connection
    candidate_id = await _insert_candidate_and_note(conn)

    with pytest.raises(IntegrityError):
        await conn.execute(
            text("DELETE FROM candidates WHERE id = :id"), {"id": candidate_id}
        )
    await conn.rollback()


async def test_candidate_email_must_be_unique(raw_connection):
    conn = raw_connection
    await conn.execute(
        text("INSERT INTO candidates (name, email) VALUES ('A', 'dup@example.com')")
    )
    await conn.commit()

    with pytest.raises(IntegrityError):
        await conn.execute(
            text("INSERT INTO candidates (name, email) VALUES ('B', 'dup@example.com')")
        )
        await conn.commit()
    await conn.rollback()


async def test_stage_enum_rejects_an_unknown_value(raw_connection):
    """The `stage` type is a real Postgres enum, not a free-text column."""
    conn = raw_connection
    with pytest.raises(DBAPIError):
        await conn.execute(
            text("INSERT INTO candidates (name, email, current_stage) VALUES ('C', 'c@example.com', 'ghosted')")
        )
        await conn.commit()
    await conn.rollback()
