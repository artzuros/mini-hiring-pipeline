"""Create the candidate and audit-trail tables, plus the immutability guard.

Two things here are load-bearing and worth reading before changing anything:

1. `idx_candidates_name_trgm` is a GIN trigram index. Without it, the fuzzy
   name match in the search executor degrades to a sequential scan with a
   similarity() call per row -- correct, but slow.

2. `stage_transitions_no_update` / `_no_delete` make the audit trail
   append-only *in the database*. The application never issues an UPDATE or
   DELETE against this table, but "we promise not to" is not an enforcement
   mechanism -- a future bug, a migration, or somebody at a psql prompt would
   all get through. The trigger means they cannot.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE stage AS ENUM (
            'applied', 'screening', 'interview', 'offer', 'hired', 'rejected'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE candidates (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                 TEXT NOT NULL,
            email                TEXT NOT NULL UNIQUE,
            phone                TEXT,
            resume_url           TEXT,
            current_stage        stage NOT NULL DEFAULT 'applied',
            current_stage_since  TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Trigram index: powers the fuzzy `name_query` match.
    op.execute(
        "CREATE INDEX idx_candidates_name_trgm ON candidates USING GIN (name gin_trgm_ops)"
    )
    # Supports the grouping view and every `stage_in` / `stage_not_in` filter.
    op.execute("CREATE INDEX idx_candidates_current_stage ON candidates (current_stage)")

    op.execute(
        """
        CREATE TABLE stage_transitions (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id     UUID NOT NULL REFERENCES candidates(id),
            from_stage       stage,          -- NULL only for the creation row
            to_stage         stage NOT NULL,
            note             TEXT,
            transitioned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # The history endpoint and the `moved_to` / `reached` EXISTS subqueries
    # both scan this in (candidate_id, transitioned_at) order.
    op.execute(
        """
        CREATE INDEX idx_transitions_candidate
            ON stage_transitions (candidate_id, transitioned_at)
        """
    )
    # The search executor filters on to_stage alone for
    # "who moved to Interview" / "who reached Offer".
    op.execute(
        "CREATE INDEX idx_transitions_to_stage ON stage_transitions (to_stage, transitioned_at)"
    )

    # --- Audit immutability, enforced by the database --------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_transition_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'stage_transitions rows are immutable (audit trail); % is not permitted',
                TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER stage_transitions_no_update
            BEFORE UPDATE ON stage_transitions
            FOR EACH ROW EXECUTE FUNCTION forbid_transition_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER stage_transitions_no_delete
            BEFORE DELETE ON stage_transitions
            FOR EACH ROW EXECUTE FUNCTION forbid_transition_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS stage_transitions_no_delete ON stage_transitions")
    op.execute("DROP TRIGGER IF EXISTS stage_transitions_no_update ON stage_transitions")
    op.execute("DROP FUNCTION IF EXISTS forbid_transition_mutation()")
    op.execute("DROP TABLE IF EXISTS stage_transitions")
    op.execute("DROP TABLE IF EXISTS candidates")
    op.execute("DROP TYPE IF EXISTS stage")
