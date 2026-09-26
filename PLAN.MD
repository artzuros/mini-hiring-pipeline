# Mini Hiring Pipeline — Implementation Plan

**Stack:** Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2.0 (async), Alembic, Pydantic v2, pytest.
**Interface:** API-only. No frontend. Reviewer uses Swagger UI (`/docs`) and/or Postman.
**Deployment:** You will deploy on AWS + Cloudflare yourself — this plan gives Claude Code a deployable Docker setup and leaves infra scripting minimal (see Section 10).

This document is written to be handed directly to Claude Code as a build spec. It fixes every decision so there's nothing to re-litigate mid-build. Follow it in order — each phase should be a working, testable checkpoint before moving to the next.

---

## 0. Non-negotiable business rules (encode these first, in the domain layer, before writing any endpoint)

1. Stage sequence: `applied → screening → interview → offer → hired`. Strictly linear.
2. A candidate can only move **forward exactly one stage at a time**. No skipping (`applied → interview` is illegal). No reversing, ever (`interview → screening` is illegal), even as an admin override — there is no override.
3. A candidate can be **rejected** from any stage *except* `hired`. Rejection is not part of the forward sequence — it's a separate terminal branch.
4. `hired` and `rejected` are **terminal states**. No transition is legal out of either.
5. Every transition (including the very first one, "created into `applied`") is written as an **immutable audit row**. Audit rows are never updated or deleted — enforce this at the database level, not just in application code (see Section 3.3).
6. "Time in current stage" is always a *derived* value (`now() - current_stage_since`), never stored redundantly in a way that could drift from the audit trail.

Put this state machine in one place — `app/domain/pipeline.py` — as a pure function with no DB/HTTP dependency, so it's trivially unit-testable:

```python
# app/domain/pipeline.py
from enum import Enum

class Stage(str, Enum):
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    HIRED = "hired"
    REJECTED = "rejected"

FORWARD_SEQUENCE = [Stage.APPLIED, Stage.SCREENING, Stage.INTERVIEW, Stage.OFFER, Stage.HIRED]
TERMINAL_STAGES = {Stage.HIRED, Stage.REJECTED}

class InvalidTransitionError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

def next_stage(current: Stage) -> Stage:
    if current in TERMINAL_STAGES:
        raise InvalidTransitionError(f"Candidate is in terminal stage '{current.value}'; cannot advance.")
    idx = FORWARD_SEQUENCE.index(current)
    if idx == len(FORWARD_SEQUENCE) - 1:
        raise InvalidTransitionError("Candidate is already Hired.")
    return FORWARD_SEQUENCE[idx + 1]

def validate_reject(current: Stage) -> None:
    if current in TERMINAL_STAGES:
        raise InvalidTransitionError(f"Candidate is in terminal stage '{current.value}'; cannot reject.")
```

Write `tests/test_domain_pipeline.py` against this module alone, no DB, before touching FastAPI. This is the highest-value test file in the whole project — the grader will poke at exactly these edges (skip stages, reverse stages, reject-after-hired, advance-after-rejected).

---

## 1. Database schema

### 1.1 Extensions (migration 0001)

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

`pg_trgm` powers the typo-tolerant name search ("sharam" → "Sharma") via trigram similarity. Don't reach for a third-party fuzzy-search library — Postgres already does this natively and it's queryable in the same SQL as everything else.

### 1.2 Tables (migration 0002)

```sql
CREATE TYPE stage AS ENUM ('applied','screening','interview','offer','hired','rejected');

CREATE TABLE candidates (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 TEXT NOT NULL,
    email                TEXT NOT NULL UNIQUE,
    phone                TEXT,
    resume_url           TEXT,
    current_stage        stage NOT NULL DEFAULT 'applied',
    current_stage_since  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_candidates_name_trgm ON candidates USING GIN (name gin_trgm_ops);
CREATE INDEX idx_candidates_current_stage ON candidates (current_stage);

CREATE TABLE stage_transitions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id     UUID NOT NULL REFERENCES candidates(id),
    from_stage       stage,              -- NULL for the initial "created" row
    to_stage         stage NOT NULL,
    note             TEXT,
    transitioned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_transitions_candidate ON stage_transitions (candidate_id, transitioned_at);
```

### 1.3 Enforce immutability at the DB level

The requirement says the audit trail "can never be altered" — don't just rely on never writing an UPDATE/DELETE in application code (a bug or a future dev will eventually do it). Add a trigger:

```sql
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stage_transitions rows are immutable (audit trail)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stage_transitions_no_update
    BEFORE UPDATE ON stage_transitions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER stage_transitions_no_delete
    BEFORE DELETE ON stage_transitions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

Write a test (`tests/test_audit_immutability.py`) that opens a raw connection and attempts `UPDATE stage_transitions SET note = 'x' WHERE ...` and asserts it raises. This is a good thing to point at in the README as a deliberate design decision — "audit immutability enforced at the DB layer, not just the app layer, so it holds even if someone connects with psql directly."

---

## 2. Project layout

```
mini-hiring-pipeline/
├── app/
│   ├── main.py                    # FastAPI app, router mounting, exception handlers
│   ├── config.py                  # Settings (pydantic-settings), reads .env
│   ├── db.py                      # async engine/session factory
│   ├── domain/
│   │   └── pipeline.py            # state machine (Section 0) — no DB/HTTP imports
│   ├── models/
│   │   ├── candidate.py           # SQLAlchemy model
│   │   └── stage_transition.py
│   ├── schemas/
│   │   ├── candidate.py           # Pydantic request/response models
│   │   └── search.py
│   ├── repositories/
│   │   └── candidate_repo.py      # all raw SQL / ORM queries live here
│   ├── services/
│   │   ├── candidate_service.py   # orchestrates repo + domain state machine
│   │   └── search/
│   │       ├── rules.py           # rule-based query parser
│   │       ├── llm_fallback.py    # LLM parser, same output schema as rules.py
│   │       ├── executor.py        # turns a parsed SearchFilter into SQL + ranks results
│   │       └── schema.py          # SearchFilter dataclass — the contract between rules/llm/executor
│   └── api/
│       └── routers/
│           ├── candidates.py      # POST/GET candidates, advance, reject, history
│           └── search.py          # GET /search
├── migrations/                    # Alembic
├── tests/
│   ├── test_domain_pipeline.py
│   ├── test_audit_immutability.py
│   ├── test_api_candidates.py
│   ├── test_search_rules.py
│   ├── test_search_llm_fallback.py   # mocks the LLM call
│   └── conftest.py                   # spins up test DB, seeds fixtures
├── scripts/
│   └── seed.py                    # loads realistic sample data with backdated timestamps
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── .env.example
├── README.md
└── ai-logs/
    └── (exported chat transcripts go here — see Section 11)
```

---

## 3. API surface

All request/response bodies are JSON. All endpoints are versionless (`/candidates`, not `/v1/candidates`) — fine for a take-home.

### 3.1 `POST /candidates` — create candidate

Request:

```json
{ "name": "Priya Sharma", "email": "priya@example.com", "phone": "9876543210", "resume_url": null }
```

Behavior: insert candidate row with `current_stage='applied'`, `current_stage_since=now()`, **and** insert a `stage_transitions` row with `from_stage=NULL, to_stage='applied'` in the same DB transaction. This is important: the audit trail starts at creation, not at the first *move*. `email` unique — duplicate returns `409`.

Response `201`: full candidate object (see 3.3 shape).

### 3.2 `GET /candidates?group_by=stage&q=...`

- No params: flat list of all candidates, newest first.
- `group_by=stage`: response shape becomes `{ "applied": [...], "screening": [...], ... }` — one array per stage, including empty arrays for stages with no one in them.
- `q=<query>`: delegates to the search pipeline (Section 5) and returns ranked results instead of the default listing. `group_by` and `q` are mutually exclusive; if both given, `q` wins and grouping is ignored (document this in the docstring, FastAPI will surface it in `/docs`).

### 3.3 `GET /candidates/{id}`

Response:

```json
{
  "id": "...",
  "name": "Priya Sharma",
  "email": "priya@example.com",
  "phone": "9876543210",
  "resume_url": null,
  "current_stage": "screening",
  "current_stage_since": "2026-09-20T10:00:00Z",
  "time_in_current_stage_seconds": 518400,
  "created_at": "2026-09-15T09:00:00Z",
  "history": [
    { "from_stage": null, "to_stage": "applied", "transitioned_at": "2026-09-15T09:00:00Z", "note": null },
    { "from_stage": "applied", "to_stage": "screening", "transitioned_at": "2026-09-20T10:00:00Z", "note": null }
  ]
}
```

`time_in_current_stage_seconds` is computed at request time (`now() - current_stage_since`), never stored. `history` is always sorted chronologically ascending and returned in full — no pagination needed at this scale.

404 if candidate not found.

### 3.4 `POST /candidates/{id}/advance`

Optional body: `{ "note": "Passed technical screen" }`.

- Looks up candidate, calls `domain.pipeline.next_stage(current)`.
- On `InvalidTransitionError`: return `422` with `{ "detail": "<message from the exception>" }` — e.g. "Candidate is in terminal stage 'hired'; cannot advance." Never a bare validation error; the message must say *why*.
- On success: update `candidates.current_stage` and `current_stage_since=now()`, insert transition row, all in one DB transaction. Return the updated candidate (3.3 shape).

### 3.5 `POST /candidates/{id}/reject`

Optional body: `{ "note": "Not a fit for the role" }`.

- Calls `domain.pipeline.validate_reject(current)`.
- Same transaction pattern as advance, `to_stage='rejected'`.
- `422` with clear message if already terminal.

### 3.6 `GET /candidates/{id}/history`

Same `history` array as embedded in 3.3, as its own endpoint for convenience — some reviewers will hit this directly per the assignment wording ("Open a candidate and see their complete history").

### 3.7 `GET /search?q=...`

Same as `GET /candidates?q=...` — expose both because the assignment explicitly frames search as "a single search box," so give it its own top-level route, but reuse the exact same service function so there's only one implementation.

**Error contract for unparseable queries — this is graded, get it right:**

```json
{
  "detail": "I couldn't understand this query.",
  "reason": "No recognizable stage name, time expression, or candidate name found in 'asdkfjasldkfj'.",
  "examples": [
    "in Interview",
    "stuck in Screening for more than a week",
    "moved to Interview since Monday",
    "reached Offer but not hired",
    "except rejected",
    "sharam"
  ]
}
```

Status `422`. Never return an empty `[]` for a query the parser gave up on — that's indistinguishable from "understood you, zero matches," which the spec explicitly says to avoid.

---

## 4. Why a real `repositories/` + `services/` split (for the README's "decisions" section)

- `domain/pipeline.py` has zero I/O — it's the thing you point at in an interview to show the state machine is correct independent of the database.
- `repositories/` isolates SQL so the search executor (Section 5) can build dynamic `WHERE` clauses without leaking raw SQL string-building into the route handlers.
- `services/` is where a transition's two writes (candidate row + audit row) are wrapped in one DB transaction — this must never be split across two separate commits, or a crash between them corrupts the invariant that current_stage always matches the latest transition row.

---

## 5. Search: the interesting part

### 5.1 Contract between the parser and the executor

Define one dataclass that both the rule-based parser and the LLM fallback must produce. Nothing downstream ever knows which one produced it.

```python
# app/services/search/schema.py
from dataclasses import dataclass, field
from datetime import datetime
from app.domain.pipeline import Stage

@dataclass
class SearchFilter:
    name_query: str | None = None                 # free-text name, fuzzy-matched
    stage_in: list[Stage] = field(default_factory=list)     # currently in these stages
    stage_not_in: list[Stage] = field(default_factory=list) # exclude these stages
    moved_to_stage: Stage | None = None            # "moved to Interview" — has a transition into this stage
    moved_since: datetime | None = None            # ...since this timestamp
    stuck_in_stage: Stage | None = None            # currently sitting in this stage
    stuck_for_min_seconds: int | None = None       # ...for at least this long
    reached_stage: Stage | None = None             # "reached Offer" — history contains a transition into this stage
    reached_but_not_current: Stage | None = None   # ...but current_stage != this (e.g. != 'hired')
    unparsed_remainder: str = ""                   # anything left over after rule extraction; if non-empty
                                                    # AND nothing else got parsed, this triggers 422/LLM fallback
```

### 5.2 Rule-based parser (`rules.py`) — tries first, always

Process the lowercased query as a sequence of regex/keyword passes, each pass *consuming* the text it matches and removing it from the string, so what's left at the end is presumed to be a name query:

1. **Exclusion first** (so it doesn't get mistaken for an inclusion): `except|excluding|but not|other than\s+(rejected|hired|applied|screening|interview|offer)` → `stage_not_in`.
2. **"Didn't get hired" / negated outcome**: `didn't get hired|not hired|but (?:weren't|wasn't) hired` combined with a preceding `reached (offer|interview|...)` → sets `reached_stage` + `reached_but_not_current=Stage.HIRED`.
3. **"reached X" alone**: `reached (offer|interview|screening|applied)` → `reached_stage`.
4. **"stuck in X for Y"**: `stuck in (screening|interview|offer|applied)` → `stuck_in_stage`; separately parse the duration: `for (more than |over )?(a|an|\d+)\s*(day|week|month)s?` → convert to seconds (`a week` = 7 days, `2 weeks` = 14 days, etc.) → `stuck_for_min_seconds`. If a stage word is found but no duration phrase, treat as `stage_in=[that stage]` (i.e. plain "who's in Screening").
5. **"moved to X since Y"**: `moved to (screening|interview|offer|hired)` → `moved_to_stage`; then parse the date expression after `since` using the `dateparser` library (handles "Monday", "last week", "2026-09-01", "3 days ago" etc. — don't hand-roll a date parser). Resolve weekday names to the most recent past occurrence of that weekday.
6. **Plain stage mention** ("who's in Interview right now"): any bare stage keyword not already consumed by rules 1–5 → append to `stage_in`.
7. **Everything else** (whatever text remains after stripping matched keywords/phrases and common stopwords like "who's", "in", "right now", "is", "the") → `name_query`, trimmed.

If after all passes `name_query` is empty AND every other field is empty/None → the query is **unparsed**: return `None` from `rules.py`, which is the signal to try the LLM fallback (or, if the LLM fallback is also disabled/fails, return the 422 from Section 3.7).

Write `tests/test_search_rules.py` with the six example queries from the assignment as literal test cases, plus 2–3 adversarial ones ("sharam", "asdkfjasldkfj", "screening interview offer" (ambiguous multi-stage — decide: treat as `stage_in` with multiple values, i.e. OR across stages)).

### 5.3 LLM fallback (`llm_fallback.py`) — only called when `rules.py` returns `None`

- Call the Anthropic API (`claude-sonnet-4-6` or whatever the project's `.env` specifies) with a system prompt that:
  - States today's date explicitly (so relative dates resolve correctly).
  - Lists the exact enum values (`applied, screening, interview, offer, hired, rejected`).
  - Instructs it to output **only** JSON matching the `SearchFilter` shape (give it the field names above), or `null` if it truly can't extract anything.
  - No free text, no markdown fences — parse defensively anyway (strip ```json fences before `json.loads`).
- Wrap in try/except: network failure, malformed JSON, or timeout → treat as "still unparsed" and fall through to the 422 error contract, **do not crash the request**.
- This call is fully mockable in tests — `test_search_llm_fallback.py` should monkeypatch the API call and assert the executor correctly consumes whatever JSON shape comes back, without needing a real API key or network access in CI.

### 5.4 Executor (`executor.py`) — turns `SearchFilter` into SQL + ranking

Build one parameterized SQL query (via SQLAlchemy Core, not raw string concatenation) that ANDs together whichever fields of `SearchFilter` are non-empty:

- `name_query` → `similarity(name, :q) > 0.3 OR word_similarity(name, :q) > 0.3`, and include `similarity(name, :q)` in the `SELECT` as `name_score` for ranking.
- `stage_in` → `current_stage = ANY(:stages)`.
- `stage_not_in` → `current_stage != ALL(:stages)`.
- `moved_to_stage` + `moved_since` → `EXISTS (SELECT 1 FROM stage_transitions WHERE candidate_id = candidates.id AND to_stage = :stage AND transitioned_at >= :since)`.
- `stuck_in_stage` + `stuck_for_min_seconds` → `current_stage = :stage AND now() - current_stage_since >= (:secs || ' seconds')::interval`.
- `reached_stage` (+ optional `reached_but_not_current`) → `EXISTS (SELECT 1 FROM stage_transitions WHERE candidate_id = candidates.id AND to_stage = :reached)` AND, if set, `current_stage != :not_current`.

**Ranking**: `ORDER BY name_score DESC NULLS LAST, current_stage_since DESC`. When there's no `name_query`, `name_score` is null for everyone and results just sort by recency — fine, since without a name there's no natural relevance ordering, recency is the most useful default.

---

## 6. Seed data (`scripts/seed.py`)

The assignment's example queries only make sense against data with realistic *history*, not just current state — you need candidates who've been sitting in Screening for 8+ days, someone who moved to Interview specifically after "Monday" relative to seed time, someone who reached Offer and was then rejected, etc. Since the app itself only ever writes `now()` for transitions, the seed script must **insert directly via SQL with explicit backdated timestamps**, bypassing the service layer — that's expected and fine for a seed script (call this out explicitly in the README so it's clear this isn't a loophole in the real API, just a fixture-loading technique).

Include at least:

- "Priya Sharma" — currently in Screening, `current_stage_since` = 9 days ago (for the "stuck in Screening for more than a week" case, and misspell-search "sharam" test).
- Someone moved to Interview 2 days ago (for "moved to Interview since Monday" — pick a seed date where this lands correctly relative to "today").
- Someone who reached Offer then got Rejected (for "reached Offer but didn't get hired").
- Someone Hired (to prove "except rejected" doesn't exclude Hired).
- A few in each stage for the grouping view.

---

## 7. `main.py` wiring, error handling

- Global exception handler for `InvalidTransitionError` → `422` with `{"detail": exc.message}`.
- Global handler for a custom `UnparseableQueryError` (raised by the search service when both rules and LLM fail) → the `422` shape from Section 3.7.
- Enable `/docs` (Swagger) — this is the reviewer's entire UI, so make sure every schema has a `description` and every field has an `example` in the Pydantic models; FastAPI renders these directly into Swagger. Spend real effort here — it's doing the job a frontend would otherwise do.

---

## 8. Testing checklist (map 1:1 to the assignment's stated requirements)

- [ ] Cannot skip a stage (`applied → interview` rejected with clear message).
- [ ] Cannot reverse a stage (`interview → screening` rejected).
- [ ] Cannot advance or reject after `hired`.
- [ ] Cannot advance or reject after `rejected`.
- [ ] Can reject from any of `applied/screening/interview/offer`.
- [ ] Audit row inserted on creation, and on every advance/reject; history returned in chronological order.
- [ ] Attempting `UPDATE`/`DELETE` on `stage_transitions` raises at the DB level.
- [ ] `time_in_current_stage_seconds` is computed, not stored, and reflects `current_stage_since`.
- [ ] Search: all six example queries from the assignment return the expected seeded candidates.
- [ ] Search: typo "sharam" finds "Priya Sharma".
- [ ] Search: combined query (e.g. "in Interview since Monday") ANDs both conditions correctly.
- [ ] Search: nonsense query returns `422` with a `reason` and `examples`, never an empty list.
- [ ] Search: LLM fallback path is exercised with a mocked response (no live network call in the test suite).

---

## 9. Docker / local run

`docker-compose.yml` — two services, `db` (postgres:16, with `pg_trgm` available out of the box) and `api` (built from `Dockerfile`, runs `alembic upgrade head` then `uvicorn app.main:app --host 0.0.0.0` on container start via an entrypoint script). `.env.example` documents `DATABASE_URL`, `ANTHROPIC_API_KEY` (for the LLM fallback — optional; document that the app degrades gracefully to "rules-only + 422 on ambiguous queries" if it's unset).

README "how to run" section should be literally:

```
cp .env.example .env   # fill in ANTHROPIC_API_KEY if you want the LLM fallback
docker-compose up --build
python scripts/seed.py   # or a make target that runs it inside the api container
# open http://localhost:8000/docs
```

---

## 10. Deployment (AWS + Cloudflare) — you're doing this yourself, so this is just a shape, not a script

- Cheapest reasonable shape: one small EC2 instance running `docker-compose up -d` (same compose file as local, Postgres included on the box — fine at this scale, no need for RDS for a take-home). Open port 8000 only to Cloudflare's IP ranges if you want TLS termination via Cloudflare proxy (orange-cloud the DNS record), or put Caddy/Nginx in front for automatic TLS if you'd rather terminate TLS yourself.
- Simpler alternative if you don't want to manage a reverse proxy: Cloudflare Tunnel (`cloudflared`) from the EC2 box — no open inbound ports at all, and you get a `*.trycloudflare.com` or your own domain over HTTPS for free.
- Either way, put the live URL in both the README and the PDF.

---

## 11. Deliverables checklist

- [ ] **GitHub repo** — code as above, README with: how to run (Section 9), decisions-and-why (pull straight from the "why" callouts throughout this plan — DB-level immutability trigger, pg_trgm over a third-party fuzzy lib, hybrid rule+LLM search, transactional writes for candidate+audit), and a "with more time" section (see below for seed suggestions).
  - `ai-logs/` folder in the repo containing the exported Claude Code conversation(s) used to build this.
- [ ] **One documented AI disagreement**, in the README, explicit heading like "Where I disagreed with the AI." A genuine candidate from this build: *the AI's first instinct for search was often "just send every query to an LLM" — pure LLM-only parsing. Push back on that in the README if you actually do disagree with it: it's slower, costs money per search keystroke, is non-deterministic (same query could parse two different ways on two calls), and is untestable in CI without hitting a live API or extensively mocking. The hybrid design in Section 5 is the resolution — deterministic first, LLM only as a fallback for genuinely ambiguous phrasing, so 95% of searches never leave the process and are instant + free.* Only put this in if it reflects something you actually pushed back on — don't fabricate a disagreement.
- [ ] **PDF** — one to two pages: architecture summary (the diagram below), link to the GitHub repo, link to the live deployment, and 4–5 bullets of key decisions. Suggested architecture diagram content (render this as a simple boxes-and-arrows diagram in whatever tool you use for the PDF):
  ```
  Client (Swagger/Postman)
        │ HTTP/JSON
        ▼
  FastAPI app ── domain/pipeline.py (state machine, pure)
        │
        ├── services/candidate_service.py ── repositories/candidate_repo.py
        │                                            │
        └── services/search/  rules.py → executor.py │
                    │ (fallback)          │           ▼
                    └────► llm_fallback.py┘      Postgres
                                                  (candidates,
                                                   stage_transitions,
                                                   pg_trgm index,
                                                   immutability trigger)
  ```
- [ ] **"With more time" ideas** for the README (pick honestly, don't oversell): a real frontend; websocket/SSE push when a candidate moves stage; pagination on `GET /candidates` once volume grows; rate-limiting the LLM fallback path; a `/candidates/{id}/undo-last-note` style correction mechanism that *adds* a compensating audit row rather than mutating history (append-only correction, preserving immutability); structured logging + request tracing; auth (this build has none — single-recruiter tool, no login, worth flagging as a real gap for anything beyond a take-home).

---

## 12. Build order for Claude Code (do it in this sequence, commit at each checkpoint)

1. Repo scaffold + `pyproject.toml` + Docker Compose + empty FastAPI app that boots.
2. `domain/pipeline.py` + its unit tests. Get this 100% correct and tested before anything else touches it.
3. Alembic migrations (extensions, tables, trigger) + `test_audit_immutability.py`.
4. Models, schemas, repositories, `candidate_service.py`.
5. `POST /candidates`, `GET /candidates/{id}`, `GET /candidates/{id}/history` + tests.
6. `POST /candidates/{id}/advance`, `POST /candidates/{id}/reject` + tests (this is where the domain state machine gets wired to HTTP — reuse Section 8's checklist verbatim as test names).
7. `GET /candidates?group_by=stage` grouping.
8. `services/search/schema.py` + `rules.py` + its tests (all six example queries + adversarial cases) — get this fully working and tested with zero LLM involvement first.
9. `executor.py` wired to `rules.py` output, `GET /search` endpoint, `422` error contract for genuinely unparseable queries.
10. `llm_fallback.py` + its mocked tests, wired in as the fallback path only.
11. `scripts/seed.py` with backdated data covering every example query.
12. README + PDF content + `ai-logs/` export.
13. Deploy (Section 10), drop the live URL into README/PDF.
