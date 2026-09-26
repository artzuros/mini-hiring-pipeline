"""Deterministic, rule-based query parser.

This is the first and normally only parser a query meets. It is a sequence of
regex passes over the lowercased query. Each pass *consumes* the text it
matches -- replacing it with a space -- so that whatever survives to the end
is presumed to be a candidate name.

Two properties this buys us, which an LLM-only parser would not have:

* It is deterministic. The same query parses the same way on every call, so
  a bug is reproducible and a fix is verifiable.
* It is free and instant. No network round trip, no per-keystroke cost, and
  it works with no API key configured.

The parser returns ``None`` when it extracted nothing at all, which is the
signal to try the LLM fallback. Note that a *bare name guess* is not "nothing
extracted" -- see `app/services/search/service.py` for how a name that
matches nobody is handled differently from a query that was never understood.

Pass order matters and is deliberate; each pass is documented with why it must
precede or follow its neighbours.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import dateparser

from app.domain.pipeline import Stage
from app.services.search.schema import SearchFilter

STAGE_WORD = r"(?:applied|screening|interview|offer|hired|rejected)"
#: A stage mention, tolerating "the Offer stage" and "offers".
STAGE_PHRASE = rf"(?:the\s+)?{STAGE_WORD}(?:\s+stage)?s?"

# --------------------------------------------------------------------------
# Pass patterns
# --------------------------------------------------------------------------

# Pass 1. Runs first so that "except rejected" is read as an exclusion rather
# than as a plain mention of the rejected stage further down.
_EXCLUDE = re.compile(
    rf"\b(?:except|excluding|exclude|but\s+not|other\s+than|apart\s+from"
    rf"|not\s+in|isn'?t\s+in|aren'?t\s+in|no\s+longer\s+in)\s+{STAGE_PHRASE}\b"
)

# Pass 2. Reads the negated outcome in "reached Offer but didn't get hired".
# Runs before the plain "reached X" pass so the "hired" in "get hired" is not
# mistaken for a mention of the hired stage.
_NOT_HIRED = re.compile(
    r"\b(?:(?:did\s*n'?o?t|did\s+not|wasn'?t|was\s+not|weren'?t|were\s+not|never)"
    r"\s+(?:get|got|been)\s+hired"
    r"|not\s+hired"
    r"|never\s+hired)\b"
)

# Pass 3. "reached Offer" -- an event somewhere in the candidate's history.
_REACHED = re.compile(
    rf"\b(?:reached|got\s+to|made\s+it\s+to|progressed\s+to)\s+{STAGE_PHRASE}\b"
)

# Pass 4. "stuck in Screening" -- where the candidate is sitting right now.
# Must run before the bare-stage pass, or "screening" would be consumed as a
# plain `stage_in` mention and the "stuck" qualifier lost.
_STUCK = re.compile(
    rf"\b(?:stuck|sitting|waiting|lingering|parked|idle)\s+(?:in|at)\s+{STAGE_PHRASE}\b"
)

# "in Screening for 9 days" -- a duration with no "stuck" keyword. Also
# matches "... stuck in screening for more than a week" once pass 4 has
# consumed the leading phrase.
_DURATION = re.compile(
    r"\bfor\s+(?:more\s+than\s+|over\s+|at\s+least\s+|longer\s+than\s+)?"
    r"(?P<num>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*(?P<unit>day|week|month|year)s?\b"
)

# Pass 5. "moved to Interview" -- a transition event. Runs after _REACHED,
# which claims the "advanced to"/"progressed to" phrasings.
_MOVED = re.compile(
    rf"\b(?:moved|moves|move|went|promoted|advanced|progressed)\s+(?:to|into)\s+{STAGE_PHRASE}\b"
)

# "in Interview since Monday" -- a stage plus an explicit start time. Treated
# as a transition event rather than a current-stage filter, because "since"
# describes when they arrived.
_IN_STAGE = re.compile(rf"\b(?:in|at)\s+{STAGE_PHRASE}\b")

_SINCE = re.compile(r"\bsince\s+(?P<when>.+?)\s*$")

# Pass 6. Anything left that names a stage is a plain "who's in X" filter.
_BARE_STAGE = re.compile(rf"\b{STAGE_PHRASE}\b")

# Words that carry no name information once the structural passes have run.
_STOPWORDS = {
    "a", "all", "an", "and", "any", "are", "as", "at", "be", "been", "but",
    "by", "candidate", "candidates", "currently", "did", "do", "does",
    "everyone", "everybody", "find", "for", "from", "get", "give", "has",
    "have", "her", "him", "his", "in", "is", "it", "list", "me", "more",
    "my", "now", "of", "on", "or", "our", "out", "please", "right", "show",
    "still", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "those", "to", "us", "was", "we", "were", "who",
    "whose", "with", "you", "your",
}

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}

_UNIT_SECONDS = {
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,  # 30 days, deliberately the round figure a recruiter means
    "year": 31_536_000,
}


def _consume(text: str, pattern: re.Pattern[str]) -> tuple[str, list[re.Match[str]]]:
    """Remove every match of `pattern` from `text`, returning both.

    Replacing with a space rather than an empty string keeps surrounding
    words from being glued together, which would corrupt the name query.
    """
    matches = list(pattern.finditer(text))
    if not matches:
        return text, []
    return pattern.sub(" ", text), matches


def _stage_from(phrase: str) -> Stage | None:
    """Pull the stage out of a matched phrase like 'the offer stage'."""
    match = re.search(STAGE_WORD, phrase)
    return Stage(match.group(0)) if match else None


def parse_relative_date(
    expression: str, *, now: datetime | None = None
) -> datetime | None:
    """Resolve a human date expression against `now`, in UTC.

    Delegates to `dateparser` rather than hand-rolling: "Monday", "last
    week", "3 days ago", and "2026-09-01" all work, and getting weekday
    resolution right by hand is a classic source of off-by-one bugs.

    The result is truncated to the start of the day. "Moved to Interview
    since Monday" means any time on Monday, not Monday at the current
    clock time.
    """
    reference = now or datetime.now(UTC)
    cleaned = expression.strip().strip("?.!,;:")

    # Trim trailing clause fragments word by word ("monday in interview")
    # until dateparser recognises what is left. Bounded so a pathological
    # input cannot spin here.
    candidates = [cleaned]
    words = cleaned.split()
    for cut in range(1, min(4, len(words))):
        candidates.append(" ".join(words[:-cut]))

    for candidate in candidates:
        if not candidate:
            continue
        parsed = dateparser.parse(
            candidate,
            settings={
                "RELATIVE_BASE": reference,
                "PREFER_DATES_FROM": "past",
                "TIMEZONE": "UTC",
                "TO_TIMEZONE": "UTC",
            },
        )
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
    return None


def _parse_duration(text: str) -> tuple[str, int | None]:
    """Extract a "for <n> <unit>" duration, returning seconds."""
    match = _DURATION.search(text)
    if not match:
        return text, None

    raw = match.group("num").lower()
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 0)
    seconds = count * _UNIT_SECONDS[match.group("unit").lower()]

    return text[: match.start()] + " " + text[match.end() :], seconds


def _clean_name(text: str) -> str:
    """Strip stopwords and punctuation from the leftover text."""
    tokens = re.findall(r"[a-z][a-z'\-]*", text.lower())
    kept: list[str] = []
    for token in tokens:
        # Fold possessives and contractions onto their stem before the
        # stopword check, so "who's" is recognised as the stopword "who"
        # rather than surviving as a one-word name query.
        stem = token[:-2] if token.endswith("'s") else token
        stem = stem.strip("'-")
        if len(stem) > 1 and stem not in _STOPWORDS:
            kept.append(stem)
    return " ".join(kept).strip()


def parse(query: str, *, now: datetime | None = None) -> SearchFilter | None:
    """Interpret `query`. Returns None if nothing at all was understood."""
    if not query or not query.strip():
        return None

    text = query.lower()
    result = SearchFilter()

    # --- Pass 1: exclusions, before any inclusion can claim the stage ----
    text, matches = _consume(text, _EXCLUDE)
    for match in matches:
        stage = _stage_from(match.group(0))
        if stage and stage not in result.stage_not_in:
            result.stage_not_in.append(stage)

    # --- Pass 2: "didn't get hired" --------------------------------------
    text, matches = _consume(text, _NOT_HIRED)
    if matches:
        # "didn't get hired" is a constraint on the candidate's *current*
        # stage, whether or not a "reached X" clause accompanies it.
        result.reached_but_not_current = Stage.HIRED

    # --- Pass 3: "reached Offer" -----------------------------------------
    text, matches = _consume(text, _REACHED)
    for match in matches:
        stage = _stage_from(match.group(0))
        if stage:
            result.reached_stage = stage

    # --- Pass 4: "stuck in Screening ... for a week" ---------------------
    text, matches = _consume(text, _STUCK)
    for match in matches:
        stage = _stage_from(match.group(0))
        if stage:
            result.stuck_in_stage = stage

    text, duration = _parse_duration(text)
    if duration is not None:
        result.stuck_for_min_seconds = duration
        # A duration with no stage is still meaningful, but it only makes
        # sense against a stage; without one it is a bare "stuck for a week"
        # and we leave stuck_in_stage unset for the executor to ignore.

    # --- Pass 5: "moved to Interview since Monday" -----------------------
    text, matches = _consume(text, _MOVED)
    for match in matches:
        stage = _stage_from(match.group(0))
        if stage:
            result.moved_to_stage = stage

    # "in Interview since Monday" is a transition event too -- but only when
    # a "since" clause is actually present. Otherwise it is a plain
    # current-stage filter and belongs to pass 6.
    since_match = _SINCE.search(text)
    if since_match:
        parsed_when = parse_relative_date(since_match.group("when"), now=now)
        if parsed_when is not None:
            result.moved_since = parsed_when
            text = text[: since_match.start()] + " " + text[since_match.end() :]
            if result.moved_to_stage is None:
                stage_match = _IN_STAGE.search(text)
                if stage_match:
                    stage = _stage_from(stage_match.group(0))
                    if stage:
                        result.moved_to_stage = stage
                        text = (
                            text[: stage_match.start()]
                            + " "
                            + text[stage_match.end() :]
                        )
        else:
            # "since" with an unreadable date. Leave the text in place; it
            # becomes part of `unparsed_remainder` so the caller can explain
            # what defeated the parser rather than silently ignoring it.
            result.unparsed_remainder = since_match.group("when").strip()

    # --- Pass 6: any remaining bare stage mention ------------------------
    text, matches = _consume(text, _BARE_STAGE)
    for match in matches:
        stage = _stage_from(match.group(0))
        if stage and stage not in result.stage_in:
            result.stage_in.append(stage)

    # --- Reconcile a duration with the stage it belongs to ---------------
    # "in Offer for 3 days" reaches here as a plain `stage_in=[offer]` plus a
    # duration, because the duration pass runs before the bare-stage pass.
    # A duration only means anything against a stage, so if exactly one stage
    # was named, the query was really "stuck in that stage".
    if (
        result.stuck_for_min_seconds is not None
        and result.stuck_in_stage is None
        and len(result.stage_in) == 1
    ):
        result.stuck_in_stage = result.stage_in.pop()

    # --- Pass 7: whatever is left is presumed to be a name ---------------
    name = _clean_name(text)
    if name:
        result.name_query = name

    if result.is_empty:
        return None
    return result
