"""Tests for the rule-based query parser.

The six queries from the assignment appear here verbatim as literal test
cases, followed by adversarial inputs. Dates are resolved against a frozen
`now` so the relative-date assertions are stable regardless of when the suite
runs.

`NOW` is a Saturday, deliberately: "most recent Monday" is then five days
back, which is far enough from today that an off-by-one in weekday resolution
cannot hide.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.pipeline import Stage
from app.services.search.rules import parse

NOW = datetime(2026, 9, 26, 14, 30, tzinfo=UTC)  # Saturday
LAST_MONDAY = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


def p(query: str):
    return parse(query, now=NOW)


# --------------------------------------------------------------------------
# The six queries from the assignment, verbatim
# --------------------------------------------------------------------------


def test_find_priya_sharma():
    result = p("Find Priya Sharma")
    assert result.name_query == "priya sharma"
    assert not result.has_structural_criteria


def test_typo_sharam_is_treated_as_a_name():
    """The typo is not corrected here -- pg_trgm does that in the executor."""
    result = p("sharam")
    assert result.name_query == "sharam"


def test_who_is_in_interview_right_now():
    result = p("Who's in Interview right now?")
    assert result.stage_in == [Stage.INTERVIEW]
    assert result.name_query is None


def test_stuck_in_screening_for_more_than_a_week():
    result = p("Who has been stuck in Screening for more than a week?")
    assert result.stuck_in_stage is Stage.SCREENING
    assert result.stuck_for_min_seconds == 7 * 86_400


def test_moved_to_interview_since_monday():
    result = p("Who moved to Interview since Monday?")
    assert result.moved_to_stage is Stage.INTERVIEW
    assert result.moved_since == LAST_MONDAY


def test_reached_offer_but_did_not_get_hired():
    result = p("Who reached the Offer stage but didn't get hired?")
    assert result.reached_stage is Stage.OFFER
    assert result.reached_but_not_current is Stage.HIRED
    assert result.name_query is None


def test_everyone_except_rejected():
    result = p("Everyone except rejected candidates.")
    assert result.stage_not_in == [Stage.REJECTED]
    assert result.stage_in == []
    assert result.name_query is None


# --------------------------------------------------------------------------
# Combinations
# --------------------------------------------------------------------------


def test_in_interview_since_monday_is_a_transition_filter():
    """"in X since Y" describes when they arrived, not where they are."""
    result = p("in Interview since Monday")
    assert result.moved_to_stage is Stage.INTERVIEW
    assert result.moved_since == LAST_MONDAY


def test_name_combined_with_a_stage():
    result = p("Priya Sharma in Offer")
    assert result.name_query == "priya sharma"
    assert result.stage_in == [Stage.OFFER]


def test_name_combined_with_a_stuck_duration():
    result = p("Priya in Offer for 3 days")
    assert result.name_query == "priya"
    assert result.stuck_in_stage is Stage.OFFER
    assert result.stuck_for_min_seconds == 3 * 86_400


def test_name_combined_with_an_exclusion():
    result = p("Priya except rejected")
    assert result.name_query == "priya"
    assert result.stage_not_in == [Stage.REJECTED]


def test_an_excluded_stage_is_consumed_and_not_also_included():
    """Pass 1 removes the text it matches, so pass 6 cannot re-read it.

    Without that consumption "everyone in screening except rejected" would
    filter to rejected *and* exclude rejected -- an empty result for a query
    that plainly means "candidates in screening who are not rejected".
    """
    result = p("everyone in screening except rejected")
    assert result.stage_in == [Stage.SCREENING]
    assert result.stage_not_in == [Stage.REJECTED]


# --------------------------------------------------------------------------
# Multiple stages
# --------------------------------------------------------------------------


def test_several_bare_stages_are_ored_together():
    result = p("screening interview offer")
    assert result.stage_in == [Stage.SCREENING, Stage.INTERVIEW, Stage.OFFER]


def test_repeated_stage_is_not_duplicated():
    result = p("interview or interview")
    assert result.stage_in == [Stage.INTERVIEW]


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "seconds"),
    [
        ("stuck in Screening for a day", 86_400),
        ("stuck in Screening for 2 days", 172_800),
        ("stuck in Screening for 9 days", 777_600),
        ("stuck in Screening for a week", 604_800),
        ("stuck in Screening for over 2 weeks", 1_209_600),
        ("stuck in Screening for a month", 2_592_000),
        ("stuck in Offer for more than 3 weeks", 1_814_400),
    ],
)
def test_duration_phrases_convert_to_seconds(query, seconds):
    assert p(query).stuck_for_min_seconds == seconds


def test_stuck_without_a_duration_still_filters_the_stage():
    result = p("stuck in screening")
    assert result.stuck_in_stage is Stage.SCREENING
    assert result.stuck_for_min_seconds is None


def test_a_duration_attaches_to_the_single_named_stage():
    """"in Offer for 3 days" is a stuck filter, not a bare stage filter."""
    result = p("in Offer for 3 days")
    assert result.stuck_in_stage is Stage.OFFER
    assert result.stage_in == []


def test_a_duration_with_several_stages_is_not_attached_to_any_one():
    """Ambiguous: leave it as a stage filter rather than guess."""
    result = p("in interview or offer for 3 days")
    assert result.stuck_in_stage is None
    assert result.stage_in == [Stage.INTERVIEW, Stage.OFFER]


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("since Monday", LAST_MONDAY),
        ("since monday", LAST_MONDAY),
        ("since 2026-09-01", datetime(2026, 9, 1, tzinfo=UTC)),
        ("since yesterday", datetime(2026, 9, 25, tzinfo=UTC)),
        # dateparser reads "last week" as "seven days ago". Recorded here as
        # the documented behaviour rather than the Monday-of-last-week
        # reading one might assume -- the point of this test is to pin
        # whatever the library actually does, so a version bump that changes
        # it shows up as a failure instead of a silent shift in results.
        ("since last week", datetime(2026, 9, 19, tzinfo=UTC)),
    ],
)
def test_since_expressions_resolve_against_the_frozen_now(phrase, expected):
    result = p(f"moved to Interview {phrase}")
    assert result.moved_since == expected


def test_since_is_truncated_to_the_start_of_the_day():
    """"since Monday" includes everything that happened on Monday."""
    result = p("moved to Interview since Monday")
    assert result.moved_since.hour == 0
    assert result.moved_since.minute == 0


def test_an_unreadable_date_leaves_the_filter_incomplete():
    result = p("moved to Interview since flurble")
    assert result.moved_since is None
    assert result.moved_to_stage is Stage.INTERVIEW
    assert result.unparsed_remainder


# --------------------------------------------------------------------------
# Adversarial input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "?!", "the a of"])
def test_empty_and_stopword_only_queries_are_unparseable(query):
    """None is the signal to try the LLM fallback, then the 422 contract."""
    assert p(query) is None


def test_nonsense_becomes_a_bare_name_guess():
    """The parser cannot know 'asdkfjasldkfj' is not a surname.

    It extracts a name guess; the search service is what later decides that a
    name matching nobody is worth explaining rather than returning [].
    """
    result = p("asdkfjasldkfj")
    assert result.name_query == "asdkfjasldkfj"
    assert result.is_name_only


def test_who_is_in_screening_has_no_stray_name():
    """A leftover contraction must not survive as a name query."""
    result = p("who is in screening")
    assert result.stage_in == [Stage.SCREENING]
    assert result.name_query is None


def test_possessive_contraction_is_stripped():
    result = p("who's stuck in Interview")
    assert result.stuck_in_stage is Stage.INTERVIEW
    assert result.name_query is None


def test_stage_words_are_matched_on_word_boundaries():
    """'offered' and 'hired' inside other words must not match."""
    result = p("interviewer")
    assert result.stage_in == []
    assert result.name_query == "interviewer"


def test_hyphenated_names_survive_cleaning():
    result = p("Find Anne-Marie Dupont")
    assert result.name_query == "anne-marie dupont"


def test_query_is_case_insensitive():
    a = p("STUCK IN SCREENING FOR A WEEK")
    b = p("stuck in screening for a week")
    assert a == b


def test_describe_summarises_what_was_understood():
    result = p("Priya stuck in Offer for 3 days except rejected")
    description = result.describe()
    assert "priya" in description
    assert "offer" in description
    assert "rejected" in description
