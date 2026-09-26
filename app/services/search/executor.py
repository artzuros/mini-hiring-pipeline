"""Turns a `SearchFilter` into one parameterized SQL query, plus ranking.

Every criterion in the filter becomes an additional ANDed condition, so a
query that sets three fields searches for candidates satisfying all three.
Stages inside `stage_in` are ORed with each other -- "screening interview
offer" means any of the three.

Everything is built through SQLAlchemy Core expressions. No SQL is assembled
by string concatenation, so the `name_query` a recruiter types is always a
bound parameter and never reaches the database as code.

A note on indexing: the name filter is a computed score, so Postgres cannot
use the GIN trigram index on `candidates.name` to satisfy it and scans
instead. That is a deliberate trade at this scale -- one job, one recruiter,
hundreds of candidates -- and it is what makes the word-by-word comparison in
`_name_score` possible. If the table ever grew, the fix is a `%` prefilter
(which the existing index *can* serve) in front of the same score.
"""

from __future__ import annotations

import re

from sqlalchemy import Select, and_, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.candidate_note import CandidateNote
from app.models.stage_transition import StageTransition
from app.services.search.schema import SearchFilter

#: Minimum name score (0..1) for a candidate to count as a name match.
#:
#: Chosen from measurements, not taste -- see `_name_score` for the method.
#: On a matrix of realistic name queries the weakest match that *should*
#: succeed ("sharam" -> "Sharma") scores 0.400, and the strongest that
#: should not ("rao" -> "Rahul Mehta") scores 0.250. 0.35 sits between
#: them with margin on both sides.
NAME_MATCH_THRESHOLD = 0.35

#: What separates one word from the next in a note. Deliberately *not* a plain
#: space: notes are prose, so `string_to_array(text, ' ')` leaves the
#: punctuation attached -- "this is a goat." splits to [..., 'goat.'], and
#: searching for "goat" would then miss it. Splitting on every non-alphanumeric
#: character handles that, and collapsing runs of them guarantees no empty
#: tokens beyond the leading/trailing case.
NOTE_TOKEN_PATTERN = r"[^a-z0-9]+"


def _best_token_similarity(token: str):
    """Best trigram similarity between `token` and any word in the name.

    Rendered as a correlated scalar subquery so the whole comparison happens
    inside Postgres rather than by pulling every name into Python.
    """
    name_tokens = func.unnest(
        func.string_to_array(func.lower(Candidate.name), literal(" "))
    ).column_valued("name_token")
    return select(
        func.coalesce(func.max(func.similarity(name_tokens, token)), 0.0)
    ).scalar_subquery()


def _name_score(query: str):
    """How well a candidate's name matches `query`, as a SQL expression.

    Names are compared *word against word*, not as whole strings, and the
    per-query-word best scores are averaged.

    Comparing whole strings was the first attempt and it does not work. The
    score for a short query against a long name is dominated by trigrams the
    two share by coincidence, and the results are actively wrong:

        word_similarity('sharam', 'Vikram Singh')  ->  0.43   (not a match!)
        word_similarity('sharam', 'Priya Sharma')  ->  0.57   (the match)
        word_similarity('shrma',  'Fatima Sheikh') ->  0.50   (not a match!)
        word_similarity('shrma',  'Priya Sharma')  ->  0.44   (the match)

    "shrma" scoring a non-candidate *above* the actual Priya Sharma cannot be
    fixed by moving a threshold; the ordering itself is wrong. Splitting both
    sides into words and comparing like with like removes the coincidence:
    "shrma" is compared to "priya" and to "sharma", and similarity('shrma',
    'sheikh') is simply low. Every false positive above disappears.

    Averaging (rather than taking the best word) is what makes "best matches
    first" true for multi-word queries: searching "Priya Sharma" when the
    pipeline holds both a Priya Sharma and a Priya Nair ranks the genuine
    full-name match at 1.0 and the partial one at ~0.5, instead of tying
    them both at 1.0 on the strength of the shared first name.
    """
    tokens = [token for token in query.lower().split() if token]
    if not tokens:
        return literal(0.0)

    total = _best_token_similarity(tokens[0])
    for token in tokens[1:]:
        total = total + _best_token_similarity(token)
    return total / len(tokens)


def _moved_to_condition(stage, since):
    """EXISTS a transition into `stage`, optionally at or after `since`."""
    conditions = [
        StageTransition.candidate_id == Candidate.id,
        StageTransition.to_stage == stage,
    ]
    if since is not None:
        conditions.append(StageTransition.transitioned_at >= since)
    return exists().where(and_(*conditions))


def _reached_condition(stage):
    """EXISTS a transition into `stage`, at any point in history."""
    return exists().where(
        and_(
            StageTransition.candidate_id == Candidate.id,
            StageTransition.to_stage == stage,
        )
    )


def note_query_tokens(text: str) -> list[str]:
    """Split a note (or a query) into comparable words.

    Kept immediately above `_note_contains`, and in the same module, because
    the two must agree: the SQL side splits with Postgres' `regexp_split_to_array`
    and this splits with Python's `re`, and if the two ever disagreed the
    fallback would silently miss notes that plainly contain the word. There is
    a test that runs both over the same inputs and asserts they match.

    Both sides lowercase first. Neither stems, so "references" will not find
    "reference" -- see the docstring on `find_by_note_text` for why that is the
    deliberate choice rather than an oversight.
    """
    return [token for token in re.split(NOTE_TOKEN_PATTERN, text.lower()) if token]


def _note_contains(token: str):
    """EXISTS a note on this candidate whose text contains `token` as a word.

    Correlated to `Candidate.id` from the enclosing statement, and to
    `CandidateNote.candidate_id` so each candidate is only checked against
    their *own* notes.

    Matching is by exact word, not by trigram similarity. That is the whole
    point of splitting into words: `similarity('goat', 'goal')` is 0.429, so
    a fuzzy pass at the 0.35 name threshold would return every note mentioning
    a "goal" for a search for "goat". Names are short and typo-prone, so fuzzy
    matching earns its keep there; notes are prose, where a near-miss is far
    more likely to be a different word than a misspelling.
    """
    note_tokens = func.unnest(
        func.regexp_split_to_array(
            func.lower(CandidateNote.body), literal(NOTE_TOKEN_PATTERN)
        )
    ).column_valued("note_token")
    # `exists(select(1))` rather than a bare `exists()`, and the correlate is
    # pinned rather than inferred. Left to infer, SQLAlchemy pulls `candidates`
    # into this subquery and correlates the wrong side, emitting
    #     EXISTS (SELECT * FROM candidates, unnest(...) WHERE
    #             candidate_notes.candidate_id = candidates.id AND ...)
    # where that inner `candidates.id` is a fresh, unconstrained scan -- so the
    # EXISTS turns true for every candidate as soon as any note anywhere
    # contains the word. Giving the EXISTS no FROM of its own leaves
    # `candidate_notes` correlated to the enclosing statement, which is where
    # it belongs; `correlate(Candidate)` forbids the other direction.
    return exists(
        select(literal(1)).where(note_tokens == token)
    ).correlate(Candidate)


def build_note_statement(tokens: list[str]) -> Select:
    """Candidates whose notes contain *every* token, newest matching note first.

    Tokens are ANDed rather than ORed, which is what `SearchFilter` means by a
    multi-word query everywhere else: "called references" asks for the notes
    containing both words, not for every note mentioning either.

    Ordering is by the timestamp of the newest note that matched, so a
    candidate noted about this today outranks one noted about it last month.
    Because that expression is a correlated subquery it cannot be reused as a
    plain column, so it appears twice -- once in the WHERE to filter, once in
    the ORDER BY to rank. Expressed once as a scalar subquery and referenced
    twice, rather than written out twice.
    """
    # Both the correlation to the candidate and the presence of
    # `candidate_notes` in this subquery's FROM come from this first clause.
    # `_note_contains` below cannot supply either: its `candidate_notes`
    # reference is scoped to its own inner EXISTS.
    conditions = [CandidateNote.candidate_id == Candidate.id]
    conditions += [_note_contains(token) for token in tokens]

    newest_match = (
        select(func.max(CandidateNote.created_at))
        .where(and_(*conditions))
        .scalar_subquery()
    )
    return (
        select(Candidate)
        .where(newest_match.is_not(None))
        .order_by(newest_match.desc(), Candidate.id)
    )


async def find_by_note_text(
    session: AsyncSession, text: str
) -> list[Candidate]:
    """Search note bodies. Only ever called as a last resort -- see `service.py`.

    Exact words, ANDed, ordered by the newest matching note. This is a
    *fallback*, not a second search mode: it runs only when a bare-name query
    has already matched no candidate by name and the model fallback found no
    structure either. That placement is what makes the change provably
    additive -- it sits on the one path that was about to raise
    `_no_such_name`, so it can turn a 422 into a 200 and cannot alter a
    response that was already going to succeed.

    Returns `[]` for a query with no usable tokens, which the caller treats
    the same as any other miss.
    """
    tokens = note_query_tokens(text)
    if not tokens:
        return []

    result = await session.execute(build_note_statement(tokens))
    return list(result.scalars().unique().all())


def build_statement(filter_: SearchFilter) -> Select:
    """Build the SELECT for a filter. Exposed separately so tests can inspect it."""
    conditions = []

    if filter_.name_query:
        conditions.append(_name_score(filter_.name_query) > NAME_MATCH_THRESHOLD)

    if filter_.stage_in:
        conditions.append(Candidate.current_stage.in_(filter_.stage_in))

    if filter_.stage_not_in:
        # `not_in` on a non-nullable column: a candidate always has a current
        # stage, so there is no NULL to make this three-valued.
        conditions.append(Candidate.current_stage.not_in(filter_.stage_not_in))

    if filter_.moved_to_stage:
        conditions.append(
            _moved_to_condition(filter_.moved_to_stage, filter_.moved_since)
        )

    if filter_.stuck_in_stage:
        current = Candidate.current_stage == filter_.stuck_in_stage
        if filter_.stuck_for_min_seconds is not None:
            # Computed against Postgres `now()` rather than a Python
            # timestamp, so "stuck for a week" is measured on the same clock
            # that wrote `current_stage_since`.
            #
            # make_interval's positional arguments are
            # (years, months, weeks, days, hours, mins, secs).
            cutoff = func.now() - func.make_interval(
                0, 0, 0, 0, 0, 0, filter_.stuck_for_min_seconds
            )
            conditions.append(and_(current, Candidate.current_stage_since <= cutoff))
        else:
            # "stuck in Screening" with no duration named is simply a
            # current-stage filter.
            conditions.append(current)

    if filter_.reached_stage:
        conditions.append(_reached_condition(filter_.reached_stage))

    if filter_.reached_but_not_current:
        # "reached Offer but didn't get hired."
        conditions.append(Candidate.current_stage != filter_.reached_but_not_current)

    statement = select(Candidate)
    if conditions:
        statement = statement.where(and_(*conditions))

    # Ranking. A name query orders by how well the name matched, best first.
    # Without a name there is no notion of relevance, so most recently moved
    # comes first -- the most useful default for a recruiter scanning a
    # pipeline. `id` is the final tiebreaker so ordering is total and
    # pagination would be stable.
    if filter_.name_query:
        statement = statement.order_by(
            _name_score(filter_.name_query).desc().nullslast(),
            Candidate.current_stage_since.desc(),
            Candidate.id,
        )
    else:
        statement = statement.order_by(
            Candidate.current_stage_since.desc(), Candidate.id
        )

    return statement


async def run(session: AsyncSession, filter_: SearchFilter) -> list[Candidate]:
    """Execute a filter and return matching candidates, best match first."""
    if filter_.is_empty:
        # A filter that constrains nothing would silently return everyone.
        # Callers check for this before executing; returning nothing is the
        # safe behaviour if one ever forgets.
        return []

    result = await session.execute(build_statement(filter_))
    return list(result.scalars().unique().all())
