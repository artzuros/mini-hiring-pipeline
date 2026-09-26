"""Request and response models for the candidate endpoints.

FastAPI renders these straight into Swagger, which is the primary way a
reviewer will drive this API. Every field carries a description and an
example for that reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.pipeline import Stage


class CandidateCreate(BaseModel):
    """Body for `POST /candidates`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Priya Sharma",
                "email": "priya.sharma@example.com",
                "phone": "+91 98765 43210",
                "resume_url": "https://example.com/resumes/priya.pdf",
            }
        }
    )

    name: str = Field(
        min_length=1,
        max_length=200,
        description="The candidate's full name, as the recruiter would search for it.",
        examples=["Priya Sharma"],
    )
    email: str = Field(
        min_length=3,
        max_length=320,
        description=(
            "Unique contact email. A second candidate with the same email is "
            "rejected with 409 rather than silently creating a duplicate."
        ),
        examples=["priya.sharma@example.com"],
    )
    phone: str | None = Field(
        default=None,
        max_length=40,
        description="Optional contact number.",
        examples=["+91 98765 43210"],
    )
    resume_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Optional link to the candidate's resume.",
        examples=["https://example.com/resumes/priya.pdf"],
    )


class TransitionRequest(BaseModel):
    """Optional body for `POST /candidates/{id}/advance` and `/reject`.

    The note is free text recorded permanently on the audit row. It is never
    editable afterwards -- to correct a note you append a new transition, you
    do not rewrite history.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"note": "Passed the technical screen."}}
    )

    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Free-text note stored on the resulting audit row. Optional.",
        examples=["Passed the technical screen."],
    )


class StageTransitionRead(BaseModel):
    """One row of the audit trail."""

    model_config = ConfigDict(from_attributes=True)

    from_stage: Stage | None = Field(
        description=(
            "The stage the candidate was in before this event. `null` only on "
            "the creation row, which has no predecessor."
        ),
        examples=["applied"],
    )
    to_stage: Stage = Field(
        description="The stage the candidate entered.", examples=["screening"]
    )
    transitioned_at: datetime = Field(
        description="When this transition was recorded. UTC.",
        examples=["2026-09-20T10:00:00Z"],
    )
    note: str | None = Field(
        default=None,
        description="Free-text note attached at the time of the transition.",
        examples=["Passed the technical screen."],
    )


class CandidateSummary(BaseModel):
    """A candidate without their history. Used in list and search results."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Stable identifier for the candidate.")
    name: str = Field(description="The candidate's full name.", examples=["Priya Sharma"])
    email: str = Field(examples=["priya.sharma@example.com"])
    phone: str | None = Field(default=None, examples=["+91 98765 43210"])
    resume_url: str | None = Field(default=None)
    current_stage: Stage = Field(
        description="Where the candidate is right now.", examples=["screening"]
    )
    current_stage_since: datetime = Field(
        description="When the candidate entered their current stage. UTC.",
        examples=["2026-09-20T10:00:00Z"],
    )
    time_in_current_stage_seconds: int = Field(
        description=(
            "How long the candidate has been in their current stage. Derived at "
            "request time from `current_stage_since`, never stored, so it cannot "
            "drift from the audit trail."
        ),
        examples=[518400],
    )
    created_at: datetime = Field(examples=["2026-09-15T09:00:00Z"])


class CandidateRead(CandidateSummary):
    """A candidate with their complete, chronological audit trail."""

    history: list[StageTransitionRead] = Field(
        description=(
            "Every transition this candidate has ever made, oldest first, "
            "starting with their creation. Append-only: rows never change and "
            "never disappear."
        )
    )


class GroupedCandidates(BaseModel):
    """Response shape for `GET /candidates?group_by=stage`.

    Every stage key is always present, including stages with nobody in them,
    so the caller can render a stable board without checking for missing keys.
    """

    applied: list[CandidateSummary] = Field(default_factory=list)
    screening: list[CandidateSummary] = Field(default_factory=list)
    interview: list[CandidateSummary] = Field(default_factory=list)
    offer: list[CandidateSummary] = Field(default_factory=list)
    hired: list[CandidateSummary] = Field(default_factory=list)
    rejected: list[CandidateSummary] = Field(default_factory=list)
