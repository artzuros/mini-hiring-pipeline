"""Declarative base and the shared Postgres `stage` enum column type."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import DeclarativeBase

from app.domain.pipeline import Stage


class Base(DeclarativeBase):
    pass


# `create_type=False` because the enum is owned by migration 0002, not by the
# ORM. Without it, SQLAlchemy would try to CREATE TYPE on first use and fail
# against an existing database.
#
# `values_callable` is not optional here. By default SQLAlchemy stores an
# enum's *member name* -- `APPLIED` -- while the Postgres type created by the
# migration contains the lowercase *values*. Omitting it produces
# `invalid input value for enum stage: "APPLIED"` at runtime.
stage_enum = ENUM(
    Stage,
    name="stage",
    create_type=False,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)
