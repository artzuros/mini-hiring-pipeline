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

from sqlalchemy import Select, and_, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
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
