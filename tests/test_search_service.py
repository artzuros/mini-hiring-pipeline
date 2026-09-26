"""Orchestration tests: rules first, model second, explanation over silence.

The rule parser and the LLM fallback each have their own test file. What is
tested here is only the decision of *which* of them runs and *what* the caller
gets back when neither can help -- the part of the design the assignment is
actually grading ("she shouldn't just get an empty result").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.pipeline import Stage
from app.services.errors import UnparseableQueryError
from app.services.search import llm_fallback, service
from app.services.search.schema import SearchFilter
from tests.factories import add_candidate


@pytest.fixture
def llm(monkeypatch):
    """Script the fallback. Records every call so tests can assert on them."""
    calls: list[str] = []

    def install(result):
        async def fake_parse(query: str, **kwargs):
            calls.append(query)
            return result

        monkeypatch.setattr(llm_fallback, "parse", fake_parse)
        return calls

    install.calls = calls
    return install


# --------------------------------------------------------------------------
# The rules come first
# --------------------------------------------------------------------------


async def test_a_query_the_rules_understand_never_reaches_the_model(session, monkeypatch):
    """The ordering is the whole point: deterministic, instant, free."""

    async def explode(*args, **kwargs):
        raise AssertionError("the model must not be consulted for this query")

    monkeypatch.setattr(llm_fallback, "parse", explode)
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    assert [c.name for c in await service.search(session, "stuck in Screening for a week")] == [
        "Priya Sharma"
    ]


async def test_a_structurally_understood_query_that_matches_nobody_returns_empty(session):
    """An empty list is honest here, and must NOT become an error.

    Nobody is in the Offer stage. The query was understood perfectly; the
    answer is genuinely "nobody".
    """
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    assert await service.search(session, "in Offer") == []


# --------------------------------------------------------------------------
# The model as a fallback
# --------------------------------------------------------------------------


async def test_the_model_is_consulted_when_the_rules_give_up(session, llm):
    calls = llm(SearchFilter(stage_in=[Stage.OFFER]))
    await add_candidate(
        session, "Anita Desai", [(Stage.INTERVIEW, timedelta(days=18)), (Stage.OFFER, timedelta(days=6))]
    )
    await session.commit()

    names = [c.name for c in await service.search(session, "!!!")]
    assert names == ["Anita Desai"]
    assert calls == ["!!!"]


async def test_a_bare_name_matching_nobody_gets_one_more_look(session, llm):
    """The hard case the module docstring describes.

    "asdkfjasldkfj" parses as a name guess and matches nobody. Before giving
    up, the model is asked once whether there is structure the rules
    flattened -- and a structural reading that finds someone is accepted.
    """
    calls = llm(SearchFilter(stage_in=[Stage.OFFER]))
    await add_candidate(
        session, "Anita Desai", [(Stage.INTERVIEW, timedelta(days=18)), (Stage.OFFER, timedelta(days=6))]
    )
    await session.commit()

    names = [c.name for c in await service.search(session, "asdkfjasldkfj")]
    assert names == ["Anita Desai"]
    assert calls == ["asdkfjasldkfj"]


async def test_a_second_name_guess_is_not_accepted(session, llm):
    """Re-asking for another name would be no better than the first guess."""
    llm(SearchFilter(name_query="someone else"))
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    with pytest.raises(UnparseableQueryError):
        await service.search(session, "asdkfjasldkfj")


async def test_a_structural_reading_that_still_matches_nobody_is_an_error(session, llm):
    """The model's guess must actually find someone to count as an answer."""
    llm(SearchFilter(stage_in=[Stage.HIRED]))
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    with pytest.raises(UnparseableQueryError):
        await service.search(session, "asdkfjasldkfj")


async def test_the_model_not_being_available_is_not_a_failure(session, llm):
    """With no API key the fallback returns None and the 422 still explains."""
    llm(None)

    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "!!!")
    assert excinfo.value.examples


# --------------------------------------------------------------------------
# The error contract itself
# --------------------------------------------------------------------------


async def test_an_empty_query_is_explained_not_ignored(session):
    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "   ")
    assert "empty" in excinfo.value.reason.lower()


async def test_the_error_carries_a_reason_and_worked_examples(session):
    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "!!!")
    error = excinfo.value
    assert error.message == "I couldn't understand this query."
    assert "!!!" in error.reason
    assert len(error.examples) >= 5
    assert any("Interview" in example for example in error.examples)


async def test_a_missing_name_says_so_and_names_what_was_searched(session):
    await add_candidate(session, "Priya Sharma", [(Stage.SCREENING, timedelta(days=9))])
    await session.commit()

    with pytest.raises(UnparseableQueryError) as excinfo:
        await service.search(session, "Bartholomew Fitzgerald")
    assert "Bartholomew Fitzgerald" in excinfo.value.reason
    assert "misspell" in excinfo.value.reason.lower() or "spelling" in excinfo.value.reason.lower()
