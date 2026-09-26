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
consulted once in case it can see structure the rules missed, the notes are
searched once in case the recruiter was remembering something written down
rather than a name, and only if all three fail does the caller get the
explanatory 422.

That ordering is not arbitrary. Every step is appended to the *one* path that
was already about to fail, so each can only turn a 422 into a 200 -- no query
that succeeded before this chain existed can be answered differently by it.

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

#: Longest query accepted, in characters.
#:
#: A bound exists because a query is not free: it is lowercased, split, and
#: run through a dozen regular expressions in `rules.parse`, and any leftover
#: name-shaped text may be sent to `dateparser`. None of that is expensive
#: for a real query -- the longest of the assignment's examples is 55
#: characters -- but all of it is linear in the input, on a public endpoint.
#:
#: 200 leaves generous room for a pasted paragraph while keeping the work
#: bounded. It is enforced here rather than with `Query(max_length=...)` at
#: each entry point so that all three share one limit *and one error shape*:
#: FastAPI's own validation failure returns a different body from the
#: three-field contract the rest of the API uses, and on the HTML route it
#: returns JSON to a browser. Both are worse than the 422 below.
MAX_QUERY_LENGTH = 200


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
            f"No candidate matches the name '{name}', and no candidate's "
            f"notes mention it either. Either nobody by that name is in the "
            f"pipeline, or the spelling is too far from any stored name for "
            f"fuzzy matching to bridge. Searched for: '{query}'."
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

    if len(query) > MAX_QUERY_LENGTH:
        raise UnparseableQueryError(
            reason=(
                f"That query is {len(query):,} characters long, and the most "
                f"this search box accepts is {MAX_QUERY_LENGTH}. It reads a "
                f"name, a stage, or a short question -- not a document."
            ),
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

    # --- Then the notes -------------------------------------------------
    # Last resort, and deliberately *last*: a note search runs only here,
    # where the alternative is the 422 below. That placement is what makes
    # the whole feature provably additive -- every query that returned
    # results before still returns exactly the same results, because this
    # line is unreachable for any of them. It can convert a 422 into a 200
    # and it cannot do anything else.
    #
    # The cost of that ordering is a real one and worth naming: results
    # change kind depending on unrelated data. If a candidate were literally
    # named "Goat", the name filter would match them and the note that says
    # "goat" would never be reached. The query means "names, unless no name
    # matches, in which case notes".
    note_results = await executor.find_by_note_text(
        session, parsed.name_query or query
    )
    if note_results:
        return note_results

    raise _no_such_name(parsed.name_query or query, query)
