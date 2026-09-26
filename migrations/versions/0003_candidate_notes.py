"""Append-only recruiter notes.

The second append-only table. The guard mirrors the one on `stage_transitions`
in 0002: row-level `BEFORE UPDATE` / `BEFORE DELETE` triggers that raise
`restrict_violation`, so a note cannot be rewritten even from a `psql`
session.

It is a *separate function* rather than a reuse of
`forbid_transition_mutation()`. That one's message names `stage_transitions`,
and a rejected note edit that blames the wrong table is worse than no message
at all -- it sends the reader looking in the wrong place.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE candidate_notes (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id  UUID NOT NULL REFERENCES candidates(id),
            text          TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # The candidate timeline reads notes in (candidate_id, created_at) order.
    # This is the notes half of that; the transitions half already exists as
    # idx_transitions_candidate.
    op.execute(
        """
        CREATE INDEX idx_notes_candidate
            ON candidate_notes (candidate_id, created_at)
        """
    )

    # --- Append-only, enforced by the database ---------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_note_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'candidate_notes rows are immutable (append-only); % is not permitted',
                TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER candidate_notes_no_update
            BEFORE UPDATE ON candidate_notes
            FOR EACH ROW EXECUTE FUNCTION forbid_note_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER candidate_notes_no_delete
            BEFORE DELETE ON candidate_notes
            FOR EACH ROW EXECUTE FUNCTION forbid_note_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS candidate_notes_no_delete ON candidate_notes")
    op.execute("DROP TRIGGER IF EXISTS candidate_notes_no_update ON candidate_notes")
    op.execute("DROP FUNCTION IF EXISTS forbid_note_mutation()")
    op.execute("DROP TABLE IF EXISTS candidate_notes")
