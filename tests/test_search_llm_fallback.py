"""Tests for the LLM fallback parser.

No network call is made anywhere in this file. The client is replaced with a
stand-in that returns whatever text a test hands it, which is the only way to
test the interesting behaviour -- malformed replies, unknown enum values,
timeouts, 500s -- without either mocking at the wrong level or depending on a
live API key in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime

import anthropic
import httpx
import pytest

from app.domain.pipeline import Stage
from app.services.search import llm_fallback
from app.services.search.llm_fallback import parse_response_text

# --------------------------------------------------------------------------
# A stand-in for anthropic.AsyncAnthropic
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str, type_: str = "text") -> None:
        self.type = type_
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _Response(self._outcome)


class FakeClient:
    """Mimics the `with_options(...).messages.create(...)` shape."""

    def __init__(self, outcome) -> None:
        self._messages = _Messages(outcome)
        self.timeout: float | None = None
        self.calls = 0

    def with_options(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        return self

    @property
    def messages(self) -> _Messages:
        return self._messages

    @property
    def last_kwargs(self) -> dict | None:
        return self._messages.kwargs


class _StubSettings:
    llm_fallback_available = True
    anthropic_model = "claude-opus-5"
    search_llm_timeout_seconds = 8.0


@pytest.fixture
def fake_llm(monkeypatch):
    """Enable the fallback and install a scriptable fake client."""

    def install(outcome):
        client = FakeClient(outcome)
        monkeypatch.setattr(llm_fallback, "get_settings", lambda: _StubSettings())
        monkeypatch.setattr(llm_fallback, "_get_client", lambda: client)
        return client

    return install


# --------------------------------------------------------------------------
# Response coercion -- never raises, whatever the model says
# --------------------------------------------------------------------------


def test_parses_a_well_formed_response():
    result = parse_response_text(
        '{"name_query": "Priya", "stage_in": ["screening"], '
        '"stuck_for_min_seconds": 604800}'
    )
    assert result is not None
    assert result.name_query == "Priya"
    assert result.stage_in == [Stage.SCREENING]
    assert result.stuck_for_min_seconds == 604800


def test_strips_markdown_code_fences():
    """Models add them despite instructions; a fence must not fail the parse."""
    result = parse_response_text('```json\n{"stage_in": ["offer"]}\n```')
    assert result is not None
    assert result.stage_in == [Stage.OFFER]


def test_a_null_reply_means_nothing_understood():
    assert parse_response_text("null") is None


def test_an_empty_object_is_treated_as_nothing_understood():
    """A filter with no criteria would match the whole table, so it is
    rejected here rather than silently returning everyone."""
    assert parse_response_text("{}") is None


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not json at all", "{unclosed", '["stage_in"]', "42"],
)
def test_malformed_replies_return_none_rather_than_raising(text):
    assert parse_response_text(text) is None


def test_an_unknown_stage_is_dropped_not_fatal():
    """A hallucinated enum value must not take the whole parse down."""
    result = parse_response_text('{"stage_in": ["screening", "ghosted"]}')
    assert result is not None
    assert result.stage_in == [Stage.SCREENING]


def test_a_stage_of_only_unknown_values_yields_nothing():
    assert parse_response_text('{"stage_in": ["ghosted"]}') is None


def test_stage_casing_is_normalised():
    result = parse_response_text('{"stuck_in_stage": "SCREENING"}')
    assert result is not None
    assert result.stuck_in_stage is Stage.SCREENING


def test_a_boolean_is_not_accepted_as_a_duration():
    """`True` is an int in Python; 1 second of 'stuck' would be nonsense."""
    result = parse_response_text('{"stuck_in_stage": "screening", "stuck_for_min_seconds": true}')
    assert result is not None
    assert result.stuck_for_min_seconds is None


def test_a_negative_duration_is_clamped():
    result = parse_response_text(
        '{"stuck_in_stage": "screening", "stuck_for_min_seconds": -99}'
    )
    assert result is not None
    assert result.stuck_for_min_seconds == 0


def test_a_bad_date_is_dropped_but_the_rest_survives():
    result = parse_response_text(
        '{"moved_to_stage": "interview", "moved_since": "last Tuesday"}'
    )
    assert result is not None
    assert result.moved_to_stage is Stage.INTERVIEW
    assert result.moved_since is None


def test_a_naive_date_is_assumed_utc():
    result = parse_response_text(
        '{"moved_to_stage": "interview", "moved_since": "2026-09-21T09:00:00"}'
    )
    assert result is not None
    assert result.moved_since == datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def test_blank_name_is_treated_as_absent():
    assert parse_response_text('{"name_query": "   "}') is None


# --------------------------------------------------------------------------
# The call itself
# --------------------------------------------------------------------------


async def test_parses_a_live_shaped_reply(fake_llm):
    fake_llm('{"stage_in": ["interview"], "reached_stage": "offer"}')
    result = await llm_fallback.parse("who is around")
    assert result is not None
    assert result.stage_in == [Stage.INTERVIEW]
    assert result.reached_stage is Stage.OFFER


async def test_the_request_uses_the_configured_model_and_timeout(fake_llm):
    client = fake_llm('{"stage_in": ["offer"]}')
    await llm_fallback.parse("who is in offer")
    assert client.timeout == 8.0
    assert client.last_kwargs["model"] == "claude-opus-5"
    assert client.last_kwargs["messages"] == [{"role": "user", "content": "who is in offer"}]


async def test_the_prompt_carries_todays_date(fake_llm):
    """"since Monday" is unanswerable without a reference date."""
    client = fake_llm("null")
    await llm_fallback.parse("moved since monday")
    system = client.last_kwargs["system"]
    assert datetime.now(UTC).strftime("%Y-%m-%d") in system
    assert datetime.now(UTC).strftime("%A") in system


async def test_the_prompt_lists_every_stage(fake_llm):
    """The model cannot map to an enum it has not been shown."""
    client = fake_llm("null")
    await llm_fallback.parse("anything")
    system = client.last_kwargs["system"]
    for stage in Stage:
        assert stage.value in system


async def test_an_api_error_degrades_to_none(fake_llm):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIStatusError(
        "overloaded", response=httpx.Response(529, request=request), body=None
    )
    fake_llm(error)
    assert await llm_fallback.parse("who is in offer") is None


async def test_a_connection_error_degrades_to_none(fake_llm):
    fake_llm(anthropic.APIConnectionError(request=httpx.Request("POST", "https://x.test")))
    assert await llm_fallback.parse("who is in offer") is None


async def test_an_unexpected_error_degrades_to_none(fake_llm):
    """An optional dependency must never be able to 500 the search box."""
    fake_llm(RuntimeError("something nobody predicted"))
    assert await llm_fallback.parse("who is in offer") is None


async def test_the_fallback_is_skipped_when_disabled(monkeypatch):
    """With no API key the module does not even build a client."""
    calls: list[int] = []

    def explode():
        calls.append(1)
        raise AssertionError("client should not be constructed")

    monkeypatch.setattr(
        llm_fallback, "get_settings",
        lambda: type("S", (), {"llm_fallback_available": False})(),
    )
    monkeypatch.setattr(llm_fallback, "_get_client", explode)

    assert await llm_fallback.parse("who is in offer") is None
    assert calls == []


async def test_reset_client_clears_the_cache(monkeypatch):
    monkeypatch.setattr(llm_fallback, "_client", object())
    llm_fallback.reset_client()
    assert llm_fallback._client is None
