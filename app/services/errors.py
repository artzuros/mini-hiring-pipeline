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
