"""Searching note bodies -- the last resort, and the properties that constrain it.

The feature exists because of a real report: a note reading "this is a goat"
could not be found by searching `goat`. It could not be found because note
text was never searched at all, and no amount of loosening the *name*
threshold would have helped -- `similarity('goat', 'person')` is 0.000, as is
`similarity('goat', 'note')`. There was no threshold to lower.

So names are still searched exactly as before and note text is searched only
when that finds nobody. The tests below spend most of their length on that
ordering, because the ordering is the feature: it is what makes the change
provably unable to alter a response that already worked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sql_text

from app.domain.pipeline import Stage
from app.services.errors import UnparseableQueryError
from app.services.search import executor, service
from tests.factories import add_candidate, add_note, seed_pipeline


async def _names(session, query: str) -> list[str]:
    return [c.name for c in await service.search(session, query)]


# --------------------------------------------------------------------------
# The reported bug
# --------------------------------------------------------------------------


async def test_a_word_from_a_note_finds_its_candidate(session):
    """The exact case that was reported: a note saying "goat", searched by "goat"."""
    person = await add_candidate(session, "UI Note Person")
    await add_note(session, person, "this is a goat")
    await session.commit()

    assert await _names(session, "goat") == ["UI Note Person"]


async def test_note_search_ignores_case(session):
    person = await add_candidate(session, "UI Note Person")
    await add_note(session, person, "This Is A Goat")
    await session.commit()

    for query in ("goat", "GOAT", "Goat", "gOaT"):
        assert await _names(session, query) == ["UI Note Person"], query


@pytest.mark.parametrize(
    "note",
    [
        "this is a goat",       # bare
        "this is a goat.",      # sentence-final period
        "this is a goat!",      # exclamation
        "goat, allegedly",      # comma
        "call the goat (later)",# parentheses
        "goat-related",         # hyphenated
        "a goat's owner",       # apostrophe
        "goat\nsecond line",    # newline
    ],
)
async def test_surrounding_punctuation_does_not_hide_the_word(session, note):
    """A word is a run between non-alphanumerics, not a space-delimited chunk.

    Splitting on `' '` would leave "goat." and "goat," as distinct tokens that
    match nothing, which is the subtlest way this could have been wrong.
    """
    person = await add_candidate(session, "Punctuation Person")
    await add_note(session, person, note)
    await session.commit()

    assert await _names(session, "goat") == ["Punctuation Person"]


# --------------------------------------------------------------------------
# The ordering, which is the whole safety argument
# --------------------------------------------------------------------------


async def test_a_query_that_already_matched_a_name_is_untouched(session):
    """Notes are never consulted when the name search found someone.

    This is the property that makes the feature additive: every query that
    returned results before returns byte-identical results after.
    """
    named = await add_candidate(session, "Goat Person")
    noted = await add_candidate(session, "Someone Else")
    await add_note(session, noted, "this is a goat")
    await session.commit()

    # "goat" matches the *name* "Goat Person", so the note on "Someone Else"
    # must not appear -- even though it is a far more literal reading.
    assert await _names(session, "goat") == ["Goat Person"]
    assert await _names(session, "goat person") == ["Goat Person"]


async def test_a_structural_query_matching_nobody_stays_empty(session):
    """A understood query that genuinely matched nobody is still an empty list.

    Note search must not turn "nobody is in Offer" into a list of people who
    happen to have the word "offer" written on them -- that would be the
    fallback overruling a correct answer.
    """
    person = await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await add_note(session, person, "we made her an offer in a previous life")
    await session.commit()

    assert await service.search(session, "in Offer") == []


async def test_a_word_in_no_note_still_gets_the_explanatory_422(session):
    """The bug fix must not cost the assignment's error contract."""
    person = await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await add_note(session, person, "this is a goat")
    await session.commit()

    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "asdkfjasldkfj")
    assert "asdkfjasldkfj" in excinfo.value.reason
    assert excinfo.value.examples


async def test_the_422_says_notes_were_searched_too(session):
    """Having searched notes and found nothing, the error should say so.

    Otherwise a recruiter who knows the word is written down somewhere is told
    only that no *name* matched, and goes looking in the wrong place.
    """
    await add_candidate(session, "Priya Sharma")
    await session.commit()

    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "Bartholomew Fitzgerald")
    assert "notes" in excinfo.value.reason


# --------------------------------------------------------------------------
# Matching semantics
# --------------------------------------------------------------------------


async def test_every_word_of_the_query_must_appear(session):
    """Tokens are ANDed, matching how a multi-word query means everywhere else."""
    both = await add_candidate(session, "Both Words")
    await add_note(session, both, "called the references today")
    only_one = await add_candidate(session, "One Word")
    await add_note(session, only_one, "called about the salary")
    await session.commit()

    assert await _names(session, "called references") == ["Both Words"]


async def test_a_note_search_that_matches_nobody_is_a_422_not_an_empty_list(session):
    person = await add_candidate(session, "Priya Sharma")
    await add_note(session, person, "this is a goat")
    await session.commit()

    with pytest.raises(UnparseableQueryError):
        await service.search(session, "zebra")


async def test_only_the_candidates_own_notes_are_searched(session):
    """A note on one candidate must not make a different candidate a match.

    This pins a bug that was live during development. Left to infer its own
    FROM, SQLAlchemy put `candidates` inside the EXISTS and correlated the
    wrong side, producing an unconstrained inner scan:

        EXISTS (SELECT * FROM candidates, unnest(...) AS note_token
                WHERE candidate_notes.candidate_id = candidates.id AND ...)

    With that, the subquery is true for *every* candidate as soon as any note
    anywhere contains the word, and this assertion returns both names.
    """
    noted = await add_candidate(session, "Has The Note")
    await add_note(session, noted, "this is a goat")
    bystander = await add_candidate(session, "Has Nothing")
    await session.commit()
    assert bystander.name == "Has Nothing"  # guards the fixture, not the code

    assert await _names(session, "goat") == ["Has The Note"]


async def test_the_newest_matching_note_ranks_first(session):
    """Ordering is by the note that matched, so recent news beats old news."""
    now = datetime.now(UTC)
    stale = await add_candidate(session, "Noted Long Ago")
    await add_note(session, stale, "goat", at=now - timedelta(days=60))

    fresh = await add_candidate(session, "Noted Recently")
    await add_note(session, fresh, "goat", at=now - timedelta(hours=1))

    # A candidate whose *other*, non-matching note is newer must not be
    # promoted on the strength of a note that did not match.
    other = await add_candidate(session, "Newest Note Is Unrelated")
    await add_note(session, other, "goat", at=now - timedelta(days=30))
    await add_note(session, other, "unrelated chit chat", at=now)
    await session.commit()

    assert await _names(session, "goat") == [
        "Noted Recently",
        "Newest Note Is Unrelated",
        "Noted Long Ago",
    ]


async def test_notes_do_not_disturb_the_briefs_example_searches(session):
    """The graded example queries, against the fixture pipeline, unchanged.

    Both notes below were written to collide with these queries on purpose:
    "this is a goat" and "references checked" are exactly the sort of text a
    loose fallback would leak into an answer. Every assertion names the
    candidates rather than counting them, so a fallback that quietly added one
    more person cannot pass by coincidence.
    """
    seeded = await seed_pipeline(session)
    await add_note(session, seeded["priya"], "this is a goat")
    await add_note(session, seeded["rahul"], "references checked, all good")
    await session.commit()

    assert await _names(session, "Find Priya Sharma") == ["Priya Sharma"]
    assert await _names(session, "sharam") == ["Priya Sharma"]
    # Only Rahul is *currently* in Interview; Anita and Vikram passed through
    # it on their way to rejected and hired.
    assert await _names(session, "Who's in Interview right now?") == ["Rahul Mehta"]
    assert await _names(
        session, "stuck in Screening for more than a week"
    ) == ["Priya Sharma"]
    assert await _names(session, "Who moved to Interview since Monday?") == [
        "Rahul Mehta"
    ]
    # "not hired" is read as reached-but-not-currently-hired, so Vikram -- who
    # also reached Offer -- is correctly excluded for having been hired.
    assert await _names(session, "reached the Offer stage, not hired") == [
        "Anita Desai"
    ]
    assert set(await _names(session, "Everyone except rejected candidates.")) == {
        "Rahul Mehta",
        "Priya Sharma",
        "Vikram Singh",
        "Arjun Rao",
    }
    with pytest.raises(UnparseableQueryError):
        await service.search(session, "asdkfjasldkfj")


async def test_a_query_of_only_punctuation_is_not_a_note_search(session):
    """A query with no alphanumeric characters yields no tokens, hence no matches.

    Guards the empty-token path: `` would otherwise build `unnest('{}')`, and
    an empty array compared against a token matches nothing -- but `[]` short
    circuits it before the database sees it at all.
    """
    person = await add_candidate(session, "Priya Sharma")
    await add_note(session, person, "anything at all")
    await session.commit()

    assert executor.note_query_tokens("--- ...") == []
    assert await executor.find_by_note_text(session, "--- ...") == []


# --------------------------------------------------------------------------
# The two tokenizers must agree
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "this is a goat",
        "This is a Goat.",
        "well-known: e-mail!",
        "  leading and trailing  ",
        "punctuation,,,runs!!!here",
        "goat's",
        "a1b2c3",
        "UPPER lower MiXeD",
        "tab\tseparated\twords",
        "newline\nseparated\nwords",
        "single",
        "!!!",
        "",
        "   ",
        "9",
        "3 goats and 2 sheep",
    ],
)
async def test_the_python_and_sql_tokenizers_agree(session, raw_connection, value):
    """`note_query_tokens` and Postgres must split a string identically.

    The fallback compares a token produced in Python against tokens produced
    by `regexp_split_to_array`. If the two ever diverged -- a different regex,
    a different locale, a different treatment of the empty string -- searches
    would silently miss notes that plainly contain the word, and no other test
    here would notice. So both are run over the same inputs and compared.

    Postgres keeps the empty strings from leading/trailing separators; the
    Python side drops them. That difference is normalised here rather than
    papered over, because it is the one place they legitimately differ.
    """
    sql_tokens = (
        await raw_connection.execute(
            sql_text(
                "SELECT regexp_split_to_array(lower(:v), :p) AS toks"
            ),
            {"v": value, "p": executor.NOTE_TOKEN_PATTERN},
        )
    ).scalar_one()

    assert executor.note_query_tokens(value) == [t for t in sql_tokens if t]
