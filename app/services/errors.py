"""Service-layer exceptions.

These are deliberately HTTP-free. The API layer maps them to status codes in
`app/main.py`, which keeps the services callable from a script, a test, or a
future worker without dragging a web framework along.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for errors the API layer knows how to render."""


class CandidateNotFoundError(ServiceError):
    def __init__(self, candidate_id: object) -> None:
        self.candidate_id = candidate_id
        self.message = f"No candidate with id '{candidate_id}'."
        super().__init__(self.message)


class DuplicateEmailError(ServiceError):
    def __init__(self, email: str) -> None:
        self.email = email
        self.message = f"A candidate with email '{email}' already exists."
        super().__init__(self.message)


#: Worked examples returned alongside an unparseable-query error, so the
#: recruiter has something to copy rather than a dead end.
SEARCH_EXAMPLES = [
    "in Interview",
    "stuck in Screening for more than a week",
    "moved to Interview since Monday",
    "reached Offer but not hired",
    "except rejected",
    "sharam",
]


class UnparseableQueryError(ServiceError):
    """The search box was given something that could not be interpreted.

    Carries a `reason` explaining what defeated the parser and a list of
    `examples` that do work. This exists so a query the parser cannot read
    produces an explanation rather than an empty list, which would be
    indistinguishable from a query that was understood and matched nobody.
    """

    def __init__(self, reason: str, examples: list[str] | None = None) -> None:
        self.reason = reason
        self.examples = examples if examples is not None else list(SEARCH_EXAMPLES)
        self.message = "I couldn't understand this query."
        super().__init__(self.message)
