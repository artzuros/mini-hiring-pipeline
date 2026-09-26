"""The contract between the query parsers and the query executor.

Both the rule-based parser and the LLM fallback produce this one dataclass.
Nothing downstream knows or cares which of them produced it, which is what
makes the fallback a genuinely swappable component rather than a second code
path through the executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.pipeline import Stage


@dataclass
class SearchFilter:
    """A structured interpretation of a natural-language query.

    Every field is optional. Fields that are set are ANDed together by the
    executor; the stages inside `stage_in` are ORed with each other.
    """

    #: Free-text name, matched fuzzily (trigram similarity) rather than exactly.
    name_query: str | None = None

    #: Candidate's *current* stage is one of these.
    stage_in: list[Stage] = field(default_factory=list)

    #: Candidate's current stage is none of these.
    stage_not_in: list[Stage] = field(default_factory=list)

    #: Candidate has a transition *into* this stage (they may have moved on).
    moved_to_stage: Stage | None = None

    #: ...and that transition happened at or after this moment.
    moved_since: datetime | None = None

    #: Candidate is sitting in this stage right now.
    stuck_in_stage: Stage | None = None

    #: ...and has been for at least this many seconds.
    stuck_for_min_seconds: int | None = None

    #: Candidate's history contains a transition into this stage.
    reached_stage: Stage | None = None

    #: ...but their current stage is not this one. Pairs with `reached_stage`
    #: to express "reached Offer but didn't get hired".
    reached_but_not_current: Stage | None = None

    #: Text the parser could not interpret. Empty when the whole query was
    #: understood.
    unparsed_remainder: str = ""

    @property
    def has_structural_criteria(self) -> bool:
        """True if anything other than a fuzzy name guess was extracted.

        The distinction matters for error handling: a query understood
        structurally can legitimately return zero results, but a bare name
        guess that matched nobody is indistinguishable from nonsense and
        deserves a different response.
        """
        return bool(
            self.stage_in
            or self.stage_not_in
            or self.moved_to_stage
            or self.stuck_in_stage
            or self.reached_stage
            or self.reached_but_not_current
        )

    @property
    def is_empty(self) -> bool:
        return not self.name_query and not self.has_structural_criteria

    @property
    def is_name_only(self) -> bool:
        """A bare name guess with nothing structural behind it."""
        return bool(self.name_query) and not self.has_structural_criteria

    def describe(self) -> str:
        """A short human-readable summary of what was understood.

        Used to explain a zero-result search: telling the recruiter what the
        parser *did* understand is far more useful than an empty list.
        """
        parts: list[str] = []
        if self.name_query:
            parts.append(f"name similar to '{self.name_query}'")
        if self.stage_in:
            parts.append("currently in " + ", ".join(s.value for s in self.stage_in))
        if self.stage_not_in:
            parts.append(
                "not currently in " + ", ".join(s.value for s in self.stage_not_in)
            )
        if self.moved_to_stage:
            since = (
                f" since {self.moved_since.isoformat()}" if self.moved_since else ""
            )
            parts.append(f"moved to {self.moved_to_stage.value}{since}")
        if self.stuck_in_stage:
            if self.stuck_for_min_seconds:
                days = self.stuck_for_min_seconds / 86400
                parts.append(
                    f"in {self.stuck_in_stage.value} for at least {days:g} days"
                )
            else:
                parts.append(f"currently in {self.stuck_in_stage.value}")
        if self.reached_stage:
            parts.append(f"reached {self.reached_stage.value}")
        if self.reached_but_not_current:
            parts.append(f"but not currently {self.reached_but_not_current.value}")
        return "; ".join(parts) if parts else "nothing"
