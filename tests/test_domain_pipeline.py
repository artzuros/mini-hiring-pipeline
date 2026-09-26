"""Exhaustive tests for the pure state machine.

No database, no HTTP, no fixtures. This file is the specification: if these
pass, the pipeline rules are correct regardless of what any layer above does.
"""

import itertools

import pytest

from app.domain.pipeline import (
    ALL_STAGES,
    FORWARD_SEQUENCE,
    TERMINAL_STAGES,
    InvalidTransitionError,
    Stage,
    is_terminal,
    next_stage,
    validate_creation,
    validate_reject,
    validate_transition,
)

NON_TERMINAL = [s for s in FORWARD_SEQUENCE if s not in TERMINAL_STAGES]


# --------------------------------------------------------------------------
# Structure of the sequence itself
# --------------------------------------------------------------------------


def test_forward_sequence_is_the_documented_order():
    assert FORWARD_SEQUENCE == [
        Stage.APPLIED,
        Stage.SCREENING,
        Stage.INTERVIEW,
        Stage.OFFER,
        Stage.HIRED,
    ]


def test_terminal_stages_are_hired_and_rejected():
    assert TERMINAL_STAGES == {Stage.HIRED, Stage.REJECTED}


def test_all_stages_covers_forward_plus_rejected():
    assert ALL_STAGES == set(FORWARD_SEQUENCE) | {Stage.REJECTED}
    assert len(ALL_STAGES) == 6


# --------------------------------------------------------------------------
# next_stage: the happy path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (Stage.APPLIED, Stage.SCREENING),
        (Stage.SCREENING, Stage.INTERVIEW),
        (Stage.INTERVIEW, Stage.OFFER),
        (Stage.OFFER, Stage.HIRED),
    ],
)
def test_next_stage_walks_the_forward_sequence(current, expected):
    assert next_stage(current) is expected


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STAGES))
def test_cannot_advance_out_of_a_terminal_stage(terminal):
    """Covers 'cannot advance after hired' and 'cannot advance after rejected'."""
    with pytest.raises(InvalidTransitionError, match="terminal stage"):
        next_stage(terminal)


# --------------------------------------------------------------------------
# validate_transition: the full rule matrix
# --------------------------------------------------------------------------


def test_creation_into_applied_is_legal():
    validate_creation(Stage.APPLIED)
    validate_transition(None, Stage.APPLIED)


@pytest.mark.parametrize("stage", [Stage.SCREENING, Stage.INTERVIEW, Stage.OFFER, Stage.HIRED, Stage.REJECTED])
def test_creation_into_any_other_stage_is_illegal(stage):
    """You cannot create a candidate straight into Interview."""
    with pytest.raises(InvalidTransitionError, match="always created into 'applied'"):
        validate_transition(None, stage)


@pytest.mark.parametrize("stage", NON_TERMINAL)
def test_reject_is_legal_from_every_non_terminal_stage(stage):
    """Rejection is available from applied, screening, interview, and offer."""
    validate_reject(stage)
    validate_transition(stage, Stage.REJECTED)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STAGES))
def test_reject_after_a_terminal_stage_is_illegal(terminal):
    """Covers 'cannot reject after hired' and the double-reject case."""
    with pytest.raises(InvalidTransitionError, match="terminal stage"):
        validate_reject(terminal)
    with pytest.raises(InvalidTransitionError, match="terminal stage"):
        validate_transition(terminal, Stage.REJECTED)


def test_cannot_skip_a_stage():
    """applied -> interview is the canonical illegal skip."""
    with pytest.raises(InvalidTransitionError, match="Cannot skip"):
        validate_transition(Stage.APPLIED, Stage.INTERVIEW)


@pytest.mark.parametrize(
    ("src", "dst"),
    [(Stage.APPLIED, Stage.OFFER), (Stage.APPLIED, Stage.HIRED), (Stage.SCREENING, Stage.HIRED)],
)
def test_all_forward_skips_are_illegal(src, dst):
    with pytest.raises(InvalidTransitionError):
        validate_transition(src, dst)


def test_cannot_reverse_a_stage():
    """interview -> screening is the canonical illegal reversal."""
    with pytest.raises(InvalidTransitionError, match="Cannot move backwards"):
        validate_transition(Stage.INTERVIEW, Stage.SCREENING)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (Stage.SCREENING, Stage.APPLIED),
        (Stage.OFFER, Stage.INTERVIEW),
        (Stage.HIRED, Stage.OFFER),
        (Stage.HIRED, Stage.SCREENING),
    ],
)
def test_every_backward_move_is_illegal(src, dst):
    """Note hired->offer is caught by the terminal rule, not the backward rule.

    Both are refusals, so the test asserts on the refusal rather than on
    which specific message won.
    """
    with pytest.raises(InvalidTransitionError):
        validate_transition(src, dst)


def test_rejecting_an_already_rejected_candidate_is_illegal():
    with pytest.raises(InvalidTransitionError, match="terminal stage"):
        validate_transition(Stage.REJECTED, Stage.REJECTED)


def test_staying_in_the_same_stage_is_illegal():
    with pytest.raises(InvalidTransitionError, match="already in stage"):
        validate_transition(Stage.SCREENING, Stage.SCREENING)


def test_exhaustive_matrix_permits_exactly_the_legal_moves():
    """Sweep all 7 x 6 (from, to) pairs and pin down the legal set.

    This is the test that would catch a future edit quietly widening the state
    machine. 7 sources because `None` (creation) is a source too.
    """
    sources = [None, *ALL_STAGES]
    legal = set()

    for src, dst in itertools.product(sources, ALL_STAGES):
        try:
            validate_transition(src, dst)
        except InvalidTransitionError:
            continue
        legal.add((src, dst))

    assert legal == {
        (None, Stage.APPLIED),
        (Stage.APPLIED, Stage.SCREENING),
        (Stage.APPLIED, Stage.REJECTED),
        (Stage.SCREENING, Stage.INTERVIEW),
        (Stage.SCREENING, Stage.REJECTED),
        (Stage.INTERVIEW, Stage.OFFER),
        (Stage.INTERVIEW, Stage.REJECTED),
        (Stage.OFFER, Stage.HIRED),
        (Stage.OFFER, Stage.REJECTED),
    }


def test_there_is_no_path_from_rejected_to_hired():
    """A rejected candidate can never be hired, by any number of steps."""
    with pytest.raises(InvalidTransitionError):
        next_stage(Stage.REJECTED)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(TERMINAL_STAGES))
def test_is_terminal_true_for_terminal_stages(stage):
    assert is_terminal(stage) is True


@pytest.mark.parametrize("stage", NON_TERMINAL)
def test_is_terminal_false_for_forward_stages(stage):
    assert is_terminal(stage) is False


def test_stage_enum_values_match_the_database_enum():
    """The Postgres `stage` type is created from these exact strings."""
    assert [s.value for s in Stage] == [
        "applied",
        "screening",
        "interview",
        "offer",
        "hired",
        "rejected",
    ]


def test_error_carries_a_readable_message():
    err = InvalidTransitionError("something specific")
    assert err.message == "something specific"
    assert str(err) == "something specific"
