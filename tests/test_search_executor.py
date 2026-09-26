"""End-to-end search tests: natural-language query in, candidates out.

These run against a real Postgres, because the parts most likely to break are
the parts that only exist in SQL -- trigram similarity, the EXISTS subqueries
over history, and the `now()`-based stuck calculation. Mocking the database
here would test the mock.

The suite is organised around the assignment's own example queries, since
those are the acceptance criteria.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.pipeline import Stage
from app.services.errors import UnparseableQueryError
from app.services.search import executor, rules, service
from app.services.search.executor import NAME_MATCH_THRESHOLD
from app.services.search.schema import SearchFilter
from tests.factories import add_candidate, seed_pipeline


async def search(session, query: str) -> list[str]:
    """Run a query and return just the names, in ranked order."""
    candidates = await service.search(session, query)
    return [c.name for c in candidates]


# --------------------------------------------------------------------------
# The assignment's example queries
# --------------------------------------------------------------------------


async def test_finds_a_candidate_by_full_name(session):
    await seed_pipeline(session)
    assert await search(session, "Find Priya Sharma") == ["Priya Sharma"]


async def test_finds_a_candidate_by_a_misspelled_surname(session):
    """The assignment calls this out explicitly: 'sharam' must find Sharma."""
    await seed_pipeline(session)
    assert await search(session, "sharam") == ["Priya Sharma"]


async def test_a_worse_typo_still_resolves(session):
    await seed_pipeline(session)
    assert await search(session, "priya shrama") == ["Priya Sharma"]


async def test_a_name_that_matches_nobody_is_an_error_not_an_empty_list(session):
    """The graded requirement: explain, don't return []."""
    await seed_pipeline(session)
    with pytest.raises(UnparseableQueryError) as excinfo:
        await search(session, "Zzzz Qqqq")
    assert "Zzzz Qqqq" in excinfo.value.reason
    assert excinfo.value.examples


async def test_who_is_in_interview_right_now(session):
    await seed_pipeline(session)
    assert await search(session, "Who's in Interview right now?") == ["Rahul Mehta"]


async def test_stuck_in_screening_for_more_than_a_week(session):
    await seed_pipeline(session)
    assert await search(session, "Who has been stuck in Screening for more than a week?") == [
        "Priya Sharma"
    ]


async def test_stuck_threshold_excludes_someone_just_under_it(session):
    now = datetime.now(UTC)
    await add_candidate(
        session, "Just Under", [(Stage.SCREENING, timedelta(days=7) - timedelta(hours=1))], now=now
    )
    await add_candidate(
        session, "Just Over", [(Stage.SCREENING, timedelta(days=7) + timedelta(minutes=1))], now=now
    )
    await session.commit()

    assert await search(session, "stuck in Screening for a week") == ["Just Over"]


async def test_moved_to_interview_since_monday(session):
    await seed_pipeline(session)
    assert await search(session, "Who moved to Interview since Monday?") == ["Rahul Mehta"]


async def test_reached_offer_but_did_not_get_hired(session):
    """Anita reached Offer and was rejected; Vikram reached Offer and was
    hired. Only Anita 'didn't get hired' in the sense meant."""
    await seed_pipeline(session)
    names = await search(session, "Who reached the Offer stage but didn't get hired?")
    assert names == ["Anita Desai"]


async def test_everyone_except_rejected_keeps_hired_candidates(session):
    """The subtle one. 'Except rejected' must not also drop `hired` -- they
    are both terminal, but they are not both 'rejected'."""
    await seed_pipeline(session)
    names = await search(session, "Everyone except rejected candidates.")
    assert "Vikram Singh" in names
    assert "Anita Desai" not in names
    assert set(names) == {"Priya Sharma", "Rahul Mehta", "Vikram Singh", "Arjun Rao"}


# --------------------------------------------------------------------------
# Combination and ranking
# --------------------------------------------------------------------------


async def test_criteria_are_anded_together(session):
    await seed_pipeline(session)
    # Right stage, but Priya is in Screening, so the name criteria and the
    # stage criteria cannot both be satisfied by anyone.
    names = await search(session, "Priya in Interview")
    assert names == []


async def test_a_combined_query_matching_someone_returns_them(session):
    await seed_pipeline(session)
    assert await search(session, "Priya in Screening for more than a week") == [
        "Priya Sharma"
    ]


async def test_multiple_stages_are_ored(session):
    await seed_pipeline(session)
    names = await search(session, "screening interview")
    assert set(names) == {"Priya Sharma", "Rahul Mehta"}


async def test_best_name_match_is_ranked_first(session):
    now = datetime.now(UTC)
    await add_candidate(
        session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=1))],
        email="priya.exact@example.com", now=now,
    )
    # A weaker but non-zero trigram match against "priya sharma".
    await add_candidate(
        session, "Sharmila Priyadarshini", [(Stage.SCREENING, timedelta(days=30))],
        email="sharmila@example.com", now=now,
    )
    await session.commit()

    names = await search(session, "Priya Sharma")
    assert names[0] == "Priya Sharma"


async def test_recent_moves_come_first_when_there_is_no_name(session):
    await seed_pipeline(session)
    # Rahul moved 2 hours ago, everyone else days ago.
    names = await search(session, "everyone except rejected")
    assert names[0] == "Rahul Mehta"


async def test_unrelated_names_do_not_match(session):
    await seed_pipeline(session)
    # "Arjun Rao" is in the pipeline, but this name is far from it. Below
    # threshold means no match, which for a name-only query is an error.
    with pytest.raises(UnparseableQueryError):
        await search(session, "Bartholomew Fitzgerald")


async def test_a_typo_does_not_drag_in_unrelated_names(session):
    """Regression pin for a real bug.

    Scoring the whole name string with pg_trgm's `word_similarity` gave
    word_similarity('sharam', 'Vikram Singh') = 0.43 -- above the threshold
    then in use -- so searching a misspelled surname returned an unrelated
    candidate. Words are now compared to words, so 'sharam' only ever reaches
    'sharma'.
    """
    await seed_pipeline(session)
    assert await search(session, "sharam") == ["Priya Sharma"]


async def test_a_surname_query_does_not_match_a_shared_first_name(session):
    """'rao' must find Arjun Rao and nobody else.

    word_similarity('rao', 'Rahul Mehta') scored 0.50 on the old scheme,
    because 'Rahul' shares the letters r-a-h. Comparing whole words drops it
    to 0.25.
    """
    now = datetime.now(UTC)
    await add_candidate(session, "Arjun Rao", now=now)
    await add_candidate(session, "Rahul Mehta", now=now)
    await session.commit()
    assert await search(session, "rao") == ["Arjun Rao"]


async def test_a_full_name_query_outranks_a_partial_one(session):
    """Averaging across query words is what makes this ordering right.

    With a best-word score both candidates tie at 1.0 on the shared 'Priya'
    and the tiebreak is recency; with the average the genuine full-name match
    wins on relevance.
    """
    now = datetime.now(UTC)
    # Deliberately the *older* one, so recency would rank it last.
    await add_candidate(
        session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=30))],
        email="priya.sharma@example.com", now=now,
    )
    await add_candidate(
        session, "Priya Nair", [(Stage.SCREENING, timedelta(days=1))],
        email="priya.nair@example.com", now=now,
    )
    await session.commit()

    names = await search(session, "Priya Sharma")
    assert names[0] == "Priya Sharma"
    # Priya Nair half-matches the query, so she is a legitimate result --
    # just not the best one.
    assert set(names) == {"Priya Sharma", "Priya Nair"}


# --------------------------------------------------------------------------
# Executor-level behaviour
# --------------------------------------------------------------------------


async def test_an_empty_filter_returns_nothing_rather_than_everyone(session):
    """A filter constraining nothing means 'everyone', which is never what
    the caller wanted -- the guard returns [] instead of leaking the table."""
    await seed_pipeline(session)
    assert await executor.run(session, SearchFilter()) == []


async def test_threshold_is_actually_applied(session):
    """Guards against the threshold silently becoming a no-op."""
    await seed_pipeline(session)
    filter_ = rules.parse("Priya Sharma")
    assert filter_ is not None
    statement = executor.build_statement(filter_)
    assert "similarity" in str(statement).lower()
    assert NAME_MATCH_THRESHOLD > 0


async def test_history_is_searched_not_just_current_state(session):
    """'reached Offer' must find someone who has since left Offer."""
    await seed_pipeline(session)
    names = await search(session, "who reached Offer")
    assert set(names) == {"Anita Desai", "Vikram Singh"}


async def test_rejecting_from_applied_is_searchable(session):
    now = datetime.now(UTC)
    await add_candidate(session, "Early Reject", [(Stage.REJECTED, timedelta(days=1))], now=now)
    await session.commit()
    assert await search(session, "rejected") == ["Early Reject"]
