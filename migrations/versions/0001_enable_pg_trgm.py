"""Enable the pg_trgm extension.

Trigram similarity is what makes the typo-tolerant name search work
("sharam" -> "Sharma"). Postgres ships this; there is no need to pull in a
third-party fuzzy-matching library, and keeping it in the database means the
name match can be expressed in the same SQL as every other search filter.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # Deliberately not dropping the extension: other things in the database
    # (or a later migration) may depend on it, and dropping an extension is
    # not a safe thing for a schema downgrade to do implicitly.
    pass
