"""Search orchestration: rules first, LLM only as a fallback, never a bare [].

The decision this module encodes is *when* an empty result is an acceptable
answer. An empty list is only honest when the query was genuinely understood
and genuinely matched nobody -- "everyone in Interview" when nobody is in
Interview. For a query the parser could not read, an empty list is a lie: it
looks identical to a successful search that found nothing, and the
assignment is explicit that the recruiter should be told why instead.

The awkward case is a bare name. "Find Priya Sharma" and "asdkfjasldkfj" are
indistinguishable to a parser -- both are just leftover text that could be a
surname. They are distinguishable by *outcome*: the first matches a
candidate, the second matches nobody at any fuzzy threshold. So a bare name
guess that matches nobody is treated as "not understood", the model is
consulted once in case it can see structure the rules missed, and if that
fails too the caller gets the explanatory 422.

The trade-off, stated plainly: searching for a real person who is genuinely
not in the pipeline also produces that 422 rather than an empty list. That is
an acceptable cost -- the error names the name that was searched for and
suggests it may be misspelled, which is more useful to the recruiter than a
blank result -- but it is a real behaviour worth knowing about.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.services.errors import SEARCH_EXAMPLES, UnparseableQueryError
from app.services.search import executor, llm_fallback, rules


def _nothing_recognised(query: str) -> UnparseableQueryError:
    return UnparseableQueryError(
        reason=(
            "No recognizable stage name, time expression, or candidate name "
            f"found in '{query}'."
        ),
        examples=SEARCH_EXAMPLES,
    )


def _no_such_name(name: str, query: str) -> UnparseableQueryError:
    return UnparseableQueryError(
        reason=(
            f"No candidate matches the name '{name}'. Either nobody by that "
            f"name is in the pipeline, or the spelling is too far from any "
            f"stored name for fuzzy matching to bridge. Searched for: '{query}'."
        ),
        examples=SEARCH_EXAMPLES,
    )


async def search(session: AsyncSession, query: str) -> list[Candidate]:
    """Run a natural-language search. Raises UnparseableQueryError if stuck."""
    query = (query or "").strip()
    if not query:
        raise UnparseableQueryError(
            reason="The search box was empty. Type a name, a stage, or a question.",
            examples=SEARCH_EXAMPLES,
        )

    # --- Deterministic first -------------------------------------------
    parsed = rules.parse(query)
    llm_consulted = False

    if parsed is None:
        # The rules found nothing structural and not even a name guess.
        parsed = await llm_fallback.parse(query)
        llm_consulted = True

    if parsed is None:
        raise _nothing_recognised(query)

    results = await executor.run(session, parsed)

    if results or not parsed.is_name_only:
        # Either we found something, or the query was understood
        # structurally and genuinely matched nobody. Both are honest.
        return results

    # --- A bare name guess that matched nobody --------------------------
    # Before giving up, give the model one chance to see structure in the
    # query that the rules flattened into a name. Only a *structural*
    # reading is accepted; another name guess would be no better than the
    # one that just failed.
    if not llm_consulted:
        alternative = await llm_fallback.parse(query)
        if alternative is not None and not alternative.is_name_only:
            alternative_results = await executor.run(session, alternative)
            if alternative_results:
                return alternative_results

    raise _no_such_name(parsed.name_query or query, query)
