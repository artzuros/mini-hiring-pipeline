# Mini Hiring Pipeline

A recruiter manages candidates for one job through a linear pipeline:

```
applied → screening → interview → offer → hired
```

Candidates move **one stage at a time**. Skipping stages and moving backwards
are impossible. A candidate can be rejected from any stage before `hired`;
`hired` and `rejected` are both terminal. Every move is appended to a history
that cannot be edited or deleted — enforced by database triggers, not by
convention.

On top of that: **one search box that takes plain English**, is tolerant of
typos, and explains itself when it does not understand you rather than
returning an empty list.

---

## Running it

Two ways. Both give you the same app on <http://localhost:8000>.

### Option A — Docker (one command)

```bash
docker compose up --build
docker compose exec app python scripts/seed.py   # optional sample data
```

Open <http://localhost:8000> for the board, or <http://localhost:8000/docs>
for the API.

> **Honesty note.** Docker is not installed on the machine this was built on,
> so this path is written but **not executed**. The `Dockerfile`,
> `docker-compose.yml` and `scripts/entrypoint.sh` are complete and the
> entrypoint's shell syntax is checked, but treat them as unverified. Option B
> is the path that has actually been run, repeatedly, with the whole test
> suite against it.

### Option B — local Postgres (the path that has been tested)

Requires Python 3.12 and a Postgres 16 `initdb`/`pg_ctl` on your `PATH`
(`brew install postgresql@16`).

```bash
# 1. Isolated database cluster, project-local, on port 5433.
./scripts/db.sh start          # creates .pgdata, starts it, creates hiring + hiring_test

# 2. Python environment.
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 3. Schema.
cp .env.example .env
.venv/bin/alembic upgrade head

# 4. Sample data (15 candidates with backdated history — see below).
.venv/bin/python scripts/seed.py

# 5. Serve.
.venv/bin/uvicorn app.main:app --reload
```

<http://localhost:8000> is the board. <http://localhost:8000/docs> is the API.

No `ANTHROPIC_API_KEY` is required. Search works fully without one — see
[Search](#search-one-box-plain-english).

### Tests

```bash
./scripts/db.sh start
.venv/bin/python -m pytest
```

201 tests. **None of them touch the network** — the LLM fallback is exercised
through a scripted fake client, so the suite runs offline and without a key.

---

## The API

| Method | Path | What it does |
|---|---|---|
| `POST` | `/candidates` | Add a candidate (starts in `applied`) |
| `GET` | `/candidates` | List, optionally `?group_by=stage` |
| `GET` | `/candidates/{id}` | One candidate, with full history |
| `GET` | `/candidates/{id}/history` | Just the history |
| `POST` | `/candidates/{id}/advance` | Move forward exactly one stage |
| `POST` | `/candidates/{id}/reject` | Reject |
| `GET` | `/search?q=…` | The search box, as an endpoint |
| `GET` | `/health` | Liveness |

Errors are never bare. An illegal move returns **422** with a message saying
*which rule* stopped it (`"Cannot skip from 'applied' to 'interview'"`); a
duplicate email returns **409**; an unparseable search returns **422** with a
`reason` and a list of `examples` that work.

---

## Search: one box, plain English

`GET /search?q=…` or the box on the board page. Every example from the brief,
against the seed data:

| Query | Result |
|---|---|
| `Find Priya Sharma` | Priya Sharma |
| `sharam` *(typo)* | Priya Sharma |
| `Who's in Interview right now?` | Rahul Mehta, Fatima Sheikh |
| `Who has been stuck in Screening for more than a week?` | Priya Sharma (9d), Karan Malhotra (11d) |
| `Who moved to Interview since Monday?` | Rahul Mehta, Fatima Sheikh |
| `Who reached the Offer stage but didn't get hired?` | Anita Desai, Meera Iyer, Joseph Fernandes |
| `Everyone except rejected candidates.` | 11 candidates — **including the 2 who were hired** |
| `asdkfjasldkfj` | **422**, with a reason and worked examples |

They combine: `Priya in Screening for more than a week except rejected` applies
all four criteria at once. Results are ranked best-match-first.

### How it works

Rules first, model second.

1. **`rules.py`** — seven ordered regex passes turn the text into a
   `SearchFilter`: exclusions, negated outcomes, `reached`, `stuck`,
   durations, `moved`/`since`, bare stage names, then whatever is left over as
   a name. Deterministic, instant, free, and unit-testable with no network.
   The ordering is load-bearing and there are tests pinning it.
2. **`executor.py`** — one parameterised SQL query. No string concatenation;
   the typed name is always a bound parameter.
3. **`llm_fallback.py`** — consulted **only** when the rules cannot parse the
   query at all. Returns `None` on any problem (no key, timeout, 5xx,
   malformed JSON), which degrades to "rules only" rather than an error. An
   optional dependency must never be able to break search.

### Why the answer is sometimes a 422, deliberately

The brief says: *"When she types something that doesn't make sense, she should
be told why. She shouldn't just get an empty result."*

An empty list is only honest when the query was **understood** and genuinely
matched nobody. `in Offer` when nobody is in Offer → `[]`, correctly. But
`asdkfjasldkfj` → `[]` would be a lie: it looks exactly like a successful
search that found nothing.

The awkward case is a bare name, because `Priya Sharma` and `asdkfjasldkfj`
are the same shape to a parser — both are leftover text that could be a
surname. They are distinguishable by *outcome*: one matches a candidate, the
other does not. So a bare-name guess that matches nobody is treated as "not
understood": the model gets one chance to see structure the rules missed, and
if that fails too you get the explanatory 422.

**The trade-off, stated plainly:** searching for a real person who is genuinely
not in the pipeline *also* produces that 422. The error names the name you
searched for and says it may be misspelled, which I think is more useful than a
blank page — the honest answer to "why is there nothing here?" is almost always
"wrong spelling" — but it is a genuine behaviour, not a free win.

---

## Architecture

```
app/
  domain/pipeline.py        The state machine. Pure. No DB, no HTTP, no I/O.
  models/                   SQLAlchemy ORM: candidates, stage_transitions.
  repositories/             Queries only. No business rules.
  services/                 Transaction boundaries and orchestration.
    candidate_service.py      create / advance / reject / group_by_stage
    search/                   rules → executor → llm_fallback, via `service.py`
  api/                      JSON: routers + serializers. Thin.
  web/                      HTML: one board page, one candidate page.
  main.py                   App assembly and the global error contract.
migrations/                 Alembic. 0002 creates the tables and the triggers.
scripts/db.sh               Project-local Postgres cluster.
scripts/seed.py             Backdated sample data.
```

Layers point inwards. `domain/` imports nothing from the app. `services/` own
transactions; routers own HTTP and nothing else. Both the JSON API and the HTML
UI call the *same* service functions, so a move made in the browser and one
made through the API cannot disagree about what is legal.

**Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (async) + asyncpg, Alembic,
Postgres 16, Pydantic v2, Jinja2, pytest. Search uses `pg_trgm`; the optional
fallback uses the Anthropic SDK (`claude-sonnet-5` by default).

---

## Decisions, and why

### 1. Immutability is enforced by the database, not by the application

`stage_transitions` carries `BEFORE UPDATE` and `BEFORE DELETE` row triggers
that raise `restrict_violation`. The app never updates or deletes a transition
— but "the app never does" is a convention, and conventions erode. An audit
trail that holds only as long as nobody opens `psql` is not an audit trail.

The cost is that test cleanup had to switch from `DELETE` to `TRUNCATE`
(which is statement-level and does not fire row triggers). That is a good
trade: it made the immutability real, and the test suite caught the one place
that had been relying on `DELETE` working.

### 2. One clock: Postgres `now()`, not Python

`now()` in Postgres is the **transaction** timestamp, so every statement in one
transaction sees the identical instant. Writing `current_stage_since` and the
audit row's `transitioned_at` in the same transaction means they are equal by
construction, not by luck. There is a test asserting exact equality between
`current_stage_since` and the last history row.

It also means "stuck for more than a week" is measured against the same clock
that wrote the timestamp, instead of comparing a database value to Python's
idea of now.

### 3. Time in stage is derived, never stored

A stored `days_in_stage` column would be wrong the moment it was written and
would silently drift from the history. It is computed on every read from
`current_stage_since`.

### 4. Illegal states are unrepresentable, not validated against

`validate_transition(from_stage, to_stage)` is the single source of truth, and
`next_stage` / `validate_reject` are thin wrappers over it — so the rules
cannot drift between call sites. The domain module is pure: no session, no
request, no clock. All 42 domain tests run in milliseconds with no database.

The test that earns its keep sweeps all 7×6 `(from, to)` pairs and pins exactly
which 9 are legal, so a change to the sequence cannot quietly open a hole.

### 5. Search: rules before the model

Answered above. The reason is not cost, it is **determinism**: a query that
parses the same way every time is a query whose bugs reproduce and whose fixes
can be verified. A model in the hot path of a search box makes "it returned
something odd yesterday" untraceable.

### 6. `pg_trgm` over a third-party fuzzy-matching library

Postgres ships trigram matching. Adding a Python fuzzy library would mean
loading every candidate name into memory to score it, plus another dependency,
to reproduce something the database already does with an index.

---

## Where I disagreed with the AI

### The name matcher was wrong, and the measurements said so

The brief asks for a search that finds **"Priya Sharma" even when typed
"sharam"**. The obvious implementation — and the one I wrote first — scores the
query against the stored name with Postgres trigram similarity:

```python
func.greatest(
    func.similarity(Candidate.name, query),
    func.word_similarity(query, Candidate.name),
)
```

It looked reasonable and it passed the one example in the brief. Then I
measured it against a matrix of realistic name queries instead of trusting it,
and it was not merely imprecise — it was **ordered wrongly**:

```
word_similarity('sharam', 'Priya Sharma')   ->  0.57   ← the candidate
word_similarity('sharam', 'Vikram Singh')   ->  0.43   ← not the candidate
word_similarity('shrma',  'Priya Sharma')   ->  0.44   ← the candidate
word_similarity('shrma',  'Fatima Sheikh')  ->  0.50   ← not the candidate
```

`shrma` scores a **non-candidate above the actual match**. No threshold fixes
that; the ordering itself is broken. The cause is that a short query against a
long name is dominated by trigrams the two share by coincidence — "sharam" and
"Singh" both contain `s` and `h`, and `word_similarity` divides by the *query's*
trigram count, so short queries are scored generously.

I replaced it with word-against-word comparison: split both sides on
whitespace, take the best `similarity()` between any query word and any name
word, and average across query words. `similarity('shrma', 'sheikh')` is simply
low, so the coincidence disappears. Measured on the same matrix:

| | old scheme (best) | new scheme |
|---|---|---|
| `sharam` → Priya Sharma *(should match)* | 0.57 | **0.40** |
| `shrma` → Priya Sharma *(should match)* | 0.44 | **0.44** |
| `rao` → Arjun Rao *(should match)* | 1.00 | **1.00** |
| `sharam` → Vikram Singh *(must not)* | 0.43 | **0.17** |
| `shrma` → Fatima Sheikh *(must not)* | 0.50 | **0.18** |
| `rao` → Rahul Mehta *(must not)* | 0.50 | **0.25** |

Every false positive is gone, and the weakest true positive (0.40) now sits
well clear of the strongest false one (0.25). The threshold of 0.35 is set
from that gap, not from taste. Both cases are pinned by tests that name the
bug they exist to prevent.

Averaging rather than taking the best word is a second deliberate choice: it
is what makes "best matches first" true for a full name. Searching
`Priya Sharma` when the pipeline holds both a *Priya Sharma* and a *Priya
Nair* ranks the real match at 1.0 and the partial one at ~0.5, instead of
tying both at 1.0 on the strength of the shared first name.

**What I gave up:** the new score is computed per row, so Postgres cannot use
the GIN trigram index on `candidates.name` and scans instead. At this scale —
one job, one recruiter, hundreds of candidates — that is unmeasurable, and it
buys a matcher that is actually correct. If the table ever grew, the fix is a
`%` prefilter (which the index *can* serve) in front of the same score. The
index is left in place for exactly that.

### Two smaller ones

**The plan said "API only, no frontend."** The brief says *"build a small web
app"* and *"the recruiter has a single search box"*. I built the API first and
completely, then added one server-rendered page on top — no JavaScript, no
build step, sharing the same service functions. The API stays canonical;
`/docs` is still a real, complete OpenAPI document, and the HTML routes live
under `/ui` and are excluded from the schema.

**The plan's `next_stage()` had an unreachable branch** — it tested for
"already Hired" *after* computing the next stage from a sequence where Hired
has no successor. More importantly, the module as planned could not *express*
skipping or reversing as a function call, which made its own highest-value
tests impossible to write at the domain layer. I restructured around
`validate_transition(from, to)`, which can express any attempted move — legal
or not — and made the other two functions thin wrappers over it.

---

## What I'd do with more time

**Correctness and robustness**

- **Pagination.** `GET /candidates` returns everything. Fine for one job, wrong
  in general. The ordering already has `id` as a final tiebreaker so keyset
  pagination would be stable.
- **Optimistic concurrency on stage moves.** Two recruiters advancing the same
  candidate at once both read `applied`, both write `screening`, and the audit
  trail gets two rows for one real move. A `version` column on `candidates`
  with a conditional `UPDATE` would turn that into a detectable conflict.
  Today the app assumes a single recruiter, which is what the brief describes —
  but it is an assumption, not an invariant.
- **The name-filter index problem above**, if candidate volume ever justified it.
- **Rate limiting / cost control on the LLM fallback.** It is off the hot path
  and only reachable when the rules fail, but nothing stops a script from
  hammering it.

**Search quality**

- The rule parser covers the brief's grammar. I would add a **golden-file
  suite** of several hundred real recruiter phrasings, capturing the parse for
  each, so grammar changes show up as diffs rather than surprises.
- **Transposition typos** (`shrama` for `sharma`) currently fall below
  threshold and become a 422 that suggests checking the spelling. Trigram
  similarity is weak on transpositions by construction; a Damerau-Levenshtein
  pass on tokens would catch them. I left it out because the gap between the
  weakest true positive and the strongest false one is only 0.15 wide, and
  widening recall without a better scorer would trade false negatives for
  false positives — a worse bug for a recruiter.

**Product**

- Notes on transitions are supported by the API (`note` is on every audit row)
  but the UI does not collect them. That is the cheapest real improvement here.
- The board polls nothing; it is render-on-request by design.

---

## AI chat logs

The full session transcript, including the wrong turns, is in
[`ai-logs/`](ai-logs/). The name-matcher failure above is in there as it
happened — proposed, measured, contradicted, replaced.
