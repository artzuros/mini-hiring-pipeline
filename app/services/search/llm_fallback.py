"""LLM fallback parser. Only ever called when the rules cannot cope.

This module is deliberately the *second* choice, not the first. Every query
the rules can read is read by the rules -- deterministically, instantly, and
for free. The model is consulted only for phrasing the rules genuinely
cannot reduce, which for the assignment's example queries is never.

The reason that ordering matters, rather than being a micro-optimisation:

* Determinism. The same query parses identically on every call, so a bug
  reproduces and a fix can be verified.
* Testability. The rule path is covered by unit tests that need no network.
  This module is exercised with a monkeypatched client, so CI never depends
  on an API key or an internet connection.
* Cost and latency. A search box invites typing; a per-keystroke API call
  would be slow and metered.

The module is also failure-tolerant by construction. Any problem -- no API
key, network down, malformed JSON, timeout -- returns ``None``, which the
caller treats exactly like "the parser gave up" and renders as the documented
422. The search endpoint never 500s because an optional dependency is
unhappy.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import anthropic

from app.config import get_settings
from app.domain.pipeline import Stage
from app.services.search.schema import SearchFilter

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None

#: Cap on the model's output. Generous relative to the small JSON object
#: asked for, because on current models thinking tokens count against this
#: budget and a truncated response would parse as nothing.
_MAX_TOKENS = 4096

_SYSTEM_PROMPT = """You translate a recruiter's natural-language search into \
a JSON filter over a hiring pipeline database.

Today's date is {today} ({weekday}). Resolve all relative dates against it.

The `stage` enum has exactly these values:
  applied, screening, interview, offer, hired, rejected

The pipeline is linear: applied -> screening -> interview -> offer -> hired.
A candidate may instead be `rejected` from any stage before `hired`.
`hired` and `rejected` are terminal.

Reply with ONLY a JSON object with these keys, omitting any key you cannot
fill. No prose, no markdown fences.

{{
  "name_query": string,               // a person's name, possibly misspelled
  "stage_in": [stage],                // currently in any of these stages
  "stage_not_in": [stage],            // currently in none of these stages
  "moved_to_stage": stage,            // has a transition INTO this stage
  "moved_since": string,              // ISO 8601 date, pairs with moved_to_stage
  "stuck_in_stage": stage,            // sitting in this stage right now
  "stuck_for_min_seconds": integer,   // pairs with stuck_in_stage
  "reached_stage": stage,             // history contains a transition into this
  "reached_but_not_current": stage    // ...but is not currently in this stage
}}

Return the bare JSON value null if the query contains no interpretable
candidate search at all. Do not guess."""


def _get_client() -> anthropic.AsyncAnthropic:
    """Lazily construct the async client.

    Constructed on first use rather than at import so that importing this
    module never requires credentials -- which matters because the rules
    handle the overwhelming majority of queries and the API key is optional.
    """
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests to inject a fake."""
    global _client
    _client = None


def build_system_prompt(now: datetime | None = None) -> str:
    reference = now or datetime.now(UTC)
    return _SYSTEM_PROMPT.format(
        today=reference.strftime("%Y-%m-%d"),
        weekday=reference.strftime("%A"),
    )


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers the model may add despite instructions."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return fenced.group(1).strip() if fenced else text.strip()


def _coerce_stage(value: object) -> Stage | None:
    if not isinstance(value, str):
        return None
    try:
        return Stage(value.strip().lower())
    except ValueError:
        # A stage name outside the enum is a hallucination, not a crash.
        logger.debug("LLM fallback returned unknown stage %r", value)
        return None


def _coerce_stages(value: object) -> list[Stage]:
    if not isinstance(value, list):
        return []
    stages: list[Stage] = []
    for item in value:
        stage = _coerce_stage(item)
        if stage is not None and stage not in stages:
            stages.append(stage)
    return stages


def _coerce_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _coerce_seconds(value: object) -> int | None:
    # bool is an int subclass; `true` must not become 1 second.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def parse_response_text(text: str) -> SearchFilter | None:
    """Turn the model's raw reply into a SearchFilter. Never raises."""
    try:
        payload = json.loads(_strip_code_fences(text))
    except (json.JSONDecodeError, TypeError):
        logger.debug("LLM fallback returned unparseable JSON: %.200s", text)
        return None

    if not isinstance(payload, dict):
        # Includes the documented `null` reply meaning "nothing here".
        return None

    name = payload.get("name_query")
    name_query = name.strip() if isinstance(name, str) and name.strip() else None

    result = SearchFilter(
        name_query=name_query,
        stage_in=_coerce_stages(payload.get("stage_in")),
        stage_not_in=_coerce_stages(payload.get("stage_not_in")),
        moved_to_stage=_coerce_stage(payload.get("moved_to_stage")),
        moved_since=_coerce_datetime(payload.get("moved_since")),
        stuck_in_stage=_coerce_stage(payload.get("stuck_in_stage")),
        stuck_for_min_seconds=_coerce_seconds(payload.get("stuck_for_min_seconds")),
        reached_stage=_coerce_stage(payload.get("reached_stage")),
        reached_but_not_current=_coerce_stage(
            payload.get("reached_but_not_current")
        ),
    )

    return None if result.is_empty else result


async def parse(query: str, *, now: datetime | None = None) -> SearchFilter | None:
    """Ask the model to interpret `query`.

    Returns ``None`` -- never raises -- if the fallback is disabled, the call
    fails, or the reply carries nothing usable.
    """
    settings = get_settings()
    if not settings.llm_fallback_available:
        return None

    try:
        client = _get_client()
        response = await client.with_options(
            timeout=settings.search_llm_timeout_seconds
        ).messages.create(
            model=settings.anthropic_model,
            max_tokens=_MAX_TOKENS,
            # This is a short, well-specified extraction, not a reasoning
            # problem, so the cheapest effort setting is the right one --
            # it cuts latency on a path the recruiter is waiting on.
            output_config={"effort": "low"},
            system=build_system_prompt(now),
            messages=[{"role": "user", "content": query}],
        )
    except anthropic.APIStatusError as exc:
        # Covers rate limits, bad requests, auth failures, and 5xx alike:
        # in every case the search still works, just without the fallback.
        logger.warning("LLM fallback API error (%s): %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        logger.warning("LLM fallback connection error: %s", exc)
        return None
    except Exception:
        # An optional dependency must not be able to break search. Anything
        # unexpected here degrades to "rules only" rather than a 500.
        logger.exception("LLM fallback failed unexpectedly")
        return None

    text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    return parse_response_text(text)
