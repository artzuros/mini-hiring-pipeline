"""The hiring pipeline state machine.

This module is deliberately pure: it imports no database, no HTTP, and no
framework code. Every rule about which stage moves are legal lives here and
nowhere else, which means the rules can be exhaustively unit-tested without a
database, a server, or a network.

The rules, restated:

1. ``applied -> screening -> interview -> offer -> hired`` is the only forward
   path, and it is strictly linear.
2. A candidate moves forward exactly one stage at a time. Skipping is illegal
   (``applied -> interview``). So is reversing (``interview -> screening``).
   There is no admin override for either.
3. A candidate may be rejected from any non-terminal stage. Rejection is a
   separate terminal branch, not a step in the forward sequence.
4. ``hired`` and ``rejected`` are terminal. Nothing transitions out of them.
5. A candidate is created directly into ``applied``; that creation is itself
   the first entry in the audit trail.
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    HIRED = "hired"
    REJECTED = "rejected"


#: The linear forward path. Index order is the progression order.
FORWARD_SEQUENCE: list[Stage] = [
    Stage.APPLIED,
    Stage.SCREENING,
    Stage.INTERVIEW,
    Stage.OFFER,
    Stage.HIRED,
]

#: States with no legal outgoing transition.
TERMINAL_STAGES: frozenset[Stage] = frozenset({Stage.HIRED, Stage.REJECTED})

#: Every stage, forward plus the rejection branch. Useful for search filters
#: that need to enumerate stages (e.g. "except rejected").
ALL_STAGES: frozenset[Stage] = frozenset(FORWARD_SEQUENCE) | TERMINAL_STAGES


class InvalidTransitionError(Exception):
    """Raised when a requested stage move is not legal.

    Carries a human-readable ``message`` that is surfaced verbatim to the API
    caller. The message must always explain *why* the move was refused -- a
    bare "invalid transition" is useless to the recruiter reading it.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def is_terminal(stage: Stage) -> bool:
    """True if no transition may leave ``stage``."""
    return stage in TERMINAL_STAGES


def stage_index(stage: Stage) -> int:
    """Position of a forward stage in the linear sequence.

    Raises ``ValueError`` for ``rejected``, which is not on the forward path
    and therefore has no index. Callers that may hold a ``rejected`` stage
    must check :func:`is_terminal` first.
    """
    return FORWARD_SEQUENCE.index(stage)


def validate_transition(from_stage: Stage | None, to_stage: Stage) -> None:
    """Assert that moving ``from_stage`` to ``to_stage`` is legal.

    This is the single source of truth for the state machine's rules. Every
    other function in this module is a thin convenience wrapper over it, so
    the rules cannot drift apart between call sites.

    ``from_stage`` is ``None`` only for the initial creation event, which is
    the one transition with no predecessor.

    Raises :class:`InvalidTransitionError` with an explanatory message.
    """
    # Rule 5: creation. The only legal creation target is `applied`.
    if from_stage is None:
        if to_stage is not Stage.APPLIED:
            raise InvalidTransitionError(
                f"A candidate is always created into '{Stage.APPLIED.value}'; "
                f"cannot create directly into '{to_stage.value}'."
            )
        return

    # Rule 4: terminal states absorb. This one check covers both
    # "cannot advance after hired" and "cannot reject after rejected".
    if from_stage in TERMINAL_STAGES:
        raise InvalidTransitionError(
            f"Candidate is in terminal stage '{from_stage.value}'; "
            f"no further transitions are allowed."
        )

    # Rule 3: rejection is legal from any non-terminal stage.
    if to_stage is Stage.REJECTED:
        return

    # Rules 1 & 2: forward moves must be exactly one step, and never backward.
    # `from_stage` is guaranteed non-terminal here, so it has a forward index.
    expected = FORWARD_SEQUENCE[stage_index(from_stage) + 1]

    if to_stage is not expected:
        # `to_stage` may be `rejected`, which is handled above, so anything
        # reaching here is either a forward skip, a reversal, or the same
        # stage twice. Say which -- the caller is a human.
        if to_stage is from_stage:
            raise InvalidTransitionError(
                f"Candidate is already in stage '{from_stage.value}'."
            )
        if stage_index(to_stage) < stage_index(from_stage):
            raise InvalidTransitionError(
                f"Cannot move backwards from '{from_stage.value}' to "
                f"'{to_stage.value}'. Stages only move forward."
            )
        raise InvalidTransitionError(
            f"Cannot skip from '{from_stage.value}' to '{to_stage.value}'. "
            f"The next stage after '{from_stage.value}' is '{expected.value}'."
        )


def next_stage(current: Stage) -> Stage:
    """Return the single legal next stage after ``current``.

    Raises :class:`InvalidTransitionError` if ``current`` is terminal.
    """
    if current in TERMINAL_STAGES:
        raise InvalidTransitionError(
            f"Candidate is in terminal stage '{current.value}'; cannot advance."
        )

    nxt = FORWARD_SEQUENCE[stage_index(current) + 1]

    # Cheap invariant guard: if someone ever edits FORWARD_SEQUENCE or
    # TERMINAL_STAGES into an inconsistent state, fail loudly here rather
    # than silently writing an illegal row into the audit trail.
    validate_transition(current, nxt)

    return nxt


def validate_reject(current: Stage) -> None:
    """Assert that a candidate in ``current`` may be rejected."""
    validate_transition(current, Stage.REJECTED)


def validate_creation(to_stage: Stage = Stage.APPLIED) -> None:
    """Assert that creating a candidate into ``to_stage`` is legal."""
    validate_transition(None, to_stage)
