"""Generate the one-page-per-section summary PDF.

Run:  python scripts/make_pdf.py [output.pdf]

Kept as a script rather than a checked-in binary so the document can be
regenerated after any change to the code it describes. It deliberately
duplicates numbers from the README instead of parsing it -- a hand-written
summary that has to be re-read is better than a generated one nobody reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

REPO_URL = "https://github.com/artzuros/mini-hiring-pipeline"

#: fpdf2 reserves the style letters B and I, so the monospace face is
#: registered as a separate *family* rather than a style of `body`.
SANS = {
    "": "/Library/Fonts/Arial Unicode.ttf",
    "B": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "I": "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
}
MONO = "/System/Library/Fonts/SFNSMono.ttf"

INK = (26, 31, 36)
MUTED = (108, 117, 125)
ACCENT = (47, 111, 235)
RULE = (222, 226, 230)
PANEL = (246, 247, 249)


class Doc(FPDF):
    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        for style, path in SANS.items():
            self.add_font("body", style, path)
        self.add_font("mono", "", MONO)
        self.set_margins(16, 15, 16)
        self.set_auto_page_break(auto=True, margin=15)
        self.set_text_color(*INK)

    # -- building blocks ---------------------------------------------------

    def h1(self, text: str) -> None:
        self.set_font("body", "B", 21)
        self.multi_cell(0, 9, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def lede(self, text: str) -> None:
        self.set_font("body", "", 10.5)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*INK)
        self.ln(2)

    def h2(self, text: str) -> None:
        self.ln(2)
        self.set_font("body", "B", 12.5)
        self.set_text_color(*ACCENT)
        self.multi_cell(0, 6, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*INK)
        self.ln(1)

    def para(self, text: str, size: float = 9.8) -> None:
        self.set_font("body", "", size)
        self.multi_cell(0, 4.6, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def bullet(self, text: str, size: float = 9.8) -> None:
        self.set_font("body", "", size)
        x = self.get_x()
        self.set_x(x + 2)
        self.multi_cell(
            0, 4.6, "•  " + text,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
        self.set_x(x)

    def kv(self, label: str, value: str) -> None:
        self.set_font("body", "B", 9.8)
        self.cell(34, 4.6, label, new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("body", "", 9.8)
        self.multi_cell(0, 4.6, value, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def pre(self, text: str) -> None:
        self.set_font("mono", "", 8.4)
        self.set_fill_color(*PANEL)
        self.multi_cell(
            0, 4.2, text, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

    def hairline(self) -> None:
        self.ln(1.5)
        self.set_draw_color(*RULE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2.5)


def pipeline_diagram(pdf: Doc) -> None:
    """The forward sequence, with the reject branch drawn beneath it."""
    stages = ["applied", "screening", "interview", "offer", "hired"]
    box_w, gap, box_h = 30.0, 7.0, 8.5
    total = len(stages) * box_w + (len(stages) - 1) * gap
    x0 = pdf.l_margin + ((pdf.w - pdf.l_margin - pdf.r_margin) - total) / 2
    y = pdf.get_y()

    pdf.set_font("body", "", 8.6)
    for i, stage in enumerate(stages):
        x = x0 + i * (box_w + gap)
        pdf.set_fill_color(*PANEL)
        pdf.set_draw_color(*RULE)
        pdf.rect(x, y, box_w, box_h, style="DF", round_corners=True, corner_radius=1.6)
        pdf.set_xy(x, y + 2.1)
        pdf.cell(box_w, 4, stage, align="C")
        if i < len(stages) - 1:
            pdf.set_xy(x + box_w, y + 1.6)
            pdf.set_text_color(*MUTED)
            pdf.cell(gap, 5, ">", align="C")
            pdf.set_text_color(*INK)

    # The reject branch: from any non-terminal stage, down, then right.
    mid_y = y + box_h
    drop = 6.0
    pdf.set_draw_color(*MUTED)
    pdf.set_dash_pattern(dash=1.0, gap=0.8)
    for i in range(4):
        x = x0 + i * (box_w + gap) + box_w / 2
        pdf.line(x, mid_y, x, mid_y + drop)
    rx = x0 + 3.5 * (box_w + gap) + box_w / 2
    pdf.line(x0 + box_w / 2, mid_y + drop, rx, mid_y + drop)
    pdf.set_dash_pattern()

    pdf.set_font("body", "", 8.2)
    pdf.set_xy(x0, mid_y + drop + 0.6)
    pdf.set_text_color(*MUTED)
    pdf.cell(60, 4, "rejected  (terminal, from any stage above)", align="L")
    pdf.set_text_color(*INK)
    pdf.set_xy(pdf.l_margin, mid_y + drop + 7.5)


def architecture_diagram(pdf: Doc) -> None:
    """Four layers, with the arrow that matters: everything points inward."""
    layers = [
        ("HTTP", "api/  (JSON)          web/  (HTML board + search box)"),
        ("Services", "candidate_service  |  search: rules -> executor -> llm_fallback"),
        ("Repositories / Models", "candidate_repo  |  SQLAlchemy ORM"),
        ("Domain", "domain/pipeline.py - pure state machine, no DB, no HTTP, no I/O"),
    ]
    width = pdf.w - pdf.l_margin - pdf.r_margin
    y = pdf.get_y()
    for i, (name, detail) in enumerate(layers):
        h = 9.0
        # The domain layer is tinted: it is the one layer with no dependencies.
        pdf.set_fill_color(*(232, 240, 254) if i == 3 else PANEL)
        pdf.set_draw_color(*RULE)
        pdf.rect(pdf.l_margin, y, width, h, style="DF", round_corners=True, corner_radius=1.6)
        pdf.set_xy(pdf.l_margin + 3, y + 1.4)
        pdf.set_font("body", "B", 8.8)
        pdf.cell(42, 4, name)
        pdf.set_font("body", "", 8.2)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 4, detail)
        pdf.set_text_color(*INK)
        y += h + 1.8
    pdf.set_xy(pdf.l_margin, y + 0.6)


def build(output: Path) -> None:
    pdf = Doc()

    # ================= PAGE 1 =================
    pdf.add_page()
    pdf.h1("Mini Hiring Pipeline")
    pdf.lede(
        "A single-recruiter hiring pipeline: a linear, unbreakable stage machine, "
        "an audit trail the database itself refuses to alter, and one search box "
        "that takes plain English."
    )
    pdf.set_font("body", "", 9.5)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 5, REPO_URL, link=REPO_URL, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*INK)
    pdf.hairline()

    pdf.h2("The pipeline")
    pdf.para(
        "Candidates move forward exactly one stage at a time. Skipping and reversing are "
        "impossible - not validated against, but unrepresentable: one pure function decides "
        "every legal move, and the rest of the system asks it."
    )
    pdf.ln(2)
    pipeline_diagram(pdf)
    pdf.ln(2)
    pdf.para(
        "Every move, including the initial creation, appends a row to an immutable history. "
        "Immutability is enforced by BEFORE UPDATE / BEFORE DELETE triggers in Postgres, so it "
        "holds even against a direct psql session - not merely because the application "
        "promises not to."
    )

    pdf.h2("The search box")
    pdf.para(
        "Deterministic rules parse the query first; a language model is consulted only for "
        "phrasing the rules genuinely cannot read. Every example from the brief is handled "
        "by the rules alone, with no API key and no network call:"
    )
    pdf.ln(1.5)
    pdf.pre(
        '"Find Priya Sharma"  /  "sharam"        -> Priya Sharma      (typo tolerant)\n'
        '"Who\'s in Interview right now?"        -> currently in that stage\n'
        '"stuck in Screening for more than a week" -> in stage at least that long\n'
        '"Who moved to Interview since Monday?"  -> entered that stage on/after a date\n'
        '"reached the Offer stage, not hired"    -> reached it, currently elsewhere\n'
        '"Everyone except rejected candidates."  -> excludes rejected, KEEPS hired'
    )
    pdf.ln(1)
    pdf.para(
        "Criteria combine and results rank best-match-first. When a query cannot be "
        "understood the caller gets a 422 naming what defeated the parser and a list of "
        "queries that work - never an empty list, which would be indistinguishable from a "
        "successful search that found nobody."
    )
    pdf.ln(1)
    pdf.para(
        "Note text is searched too, but only after names and after the model: a note reading "
        "\"this is a goat\" is found by searching goat. The report that prompted this blamed the "
        "name threshold; the real cause was that note text was never searched. Sitting last, "
        "the fallback can only turn a 422 into a 200 - no answer that already worked can change."
    )

    pdf.h2("Architecture")
    pdf.ln(1)
    architecture_diagram(pdf)
    pdf.ln(1)
    pdf.kv("Stack", "Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, Postgres 16 (pg_trgm), Pydantic v2, Jinja2, pytest.")
    pdf.kv("Tests", "271 passing. None touch the network; the model fallback runs against a scripted fake.")
    pdf.kv("Run it", "docker compose up --build, or ./scripts/db.sh start + alembic upgrade head + uvicorn.")

    # ================= PAGE 2 =================
    pdf.add_page()
    pdf.h2("Five decisions, and why")
    pdf.bullet(
        "Immutability is a database trigger, not a convention. An audit trail that holds only "
        "as long as nobody opens psql is not an audit trail. The cost was switching test "
        "cleanup from DELETE to TRUNCATE, which does not fire row triggers."
    )
    pdf.bullet(
        "Recruiter notes are a second append-only table with their own trigger function, not "
        "extra rows in the audit trail. A note is not a move, and a shared table would have "
        "forced to_stage to become nullable -- at which point \"who reached Offer\" starts "
        "matching rows that moved nobody anywhere."
    )
    pdf.bullet(
        "One clock: Postgres now(), never Python. now() is the transaction timestamp, so "
        "writing the stage change and its audit row in one transaction makes them equal by "
        "construction. A test asserts exact equality."
    )
    pdf.bullet(
        "Time in stage is derived on every read, never stored. A stored duration is wrong the "
        "moment it is written and silently drifts from the history."
    )
    pdf.bullet(
        "Rules before the model, for determinism rather than cost. A query that parses the "
        "same way every time is one whose bugs reproduce and whose fixes can be verified."
    )
    pdf.bullet(
        "pg_trgm rather than a fuzzy-matching library: the database already does it, with an "
        "index, instead of loading every name into memory to compare in Python."
    )
    pdf.bullet(
        "Note search matches whole words, not trigrams. similarity('goat', 'goal') is 0.429 - "
        "above the name threshold - so a fuzzy note search would answer \"goat\" with everyone "
        "who wrote down a goal."
    )

    pdf.h2("Where I disagreed with the AI")
    pdf.para(
        "The name matcher I wrote first - scoring the whole name with Postgres trigram "
        "similarity - passed the brief's single example, \"sharam\" -> Sharma. I then measured "
        "it against a wider matrix instead of trusting it, and it was not merely imprecise; it "
        "was ordered wrongly:"
    )
    pdf.ln(1.5)
    pdf.pre(
        "similarity('sharam', 'Priya Sharma')  0.57   <- the candidate\n"
        "similarity('sharam', 'Vikram Singh')  0.43   <- not the candidate\n"
        "similarity('shrma',  'Priya Sharma')  0.44   <- the candidate\n"
        "similarity('shrma',  'Fatima Sheikh') 0.50   <- not the candidate"
    )
    pdf.ln(1)
    pdf.para(
        "\"shrma\" scored a non-candidate above the real match. No threshold fixes that - the "
        "ordering itself was broken, because a short query against a long name is dominated by "
        "trigrams the two share by coincidence (\'sharam\' and \'Singh\' both contain s and h)."
    )
    pdf.ln(1)
    pdf.para(
        "I replaced it with word-against-word comparison: split both sides, take the best "
        "similarity between any query word and any name word, average across query words. "
        "Every false positive disappears and the weakest true match (0.40) sits clear of the "
        "strongest false one (0.25). The 0.35 threshold is set from that gap, not from taste."
    )
    pdf.ln(1)
    pdf.para(
        "I gave up something real for it: the score is computed per row, so the GIN trigram "
        "index can no longer serve the filter. At this scale that is unmeasurable, and it buys "
        "a matcher that is actually correct. Both cases are pinned by tests naming the bug "
        "they exist to prevent."
    )
    pdf.ln(1)
    pdf.para(
        "Two smaller disagreements: the plan said \"API only, no frontend\" where the brief asks "
        "for a web app with a search box, so the API was built first and completely and one "
        "server-rendered page was added on top; and the plan's next_stage() had an unreachable "
        "branch and could not express a skip or a reversal as a call, making its own "
        "highest-value tests unwritable at the domain layer."
    )

    pdf.h2("With more time")
    pdf.bullet(
        "Optimistic concurrency on stage moves. Two recruiters advancing one candidate at once "
        "produce two audit rows for one real move. A version column with a conditional UPDATE "
        "would turn that into a detectable conflict."
    )
    pdf.bullet(
        "A golden-file suite of several hundred real recruiter phrasings, so grammar changes "
        "show up as diffs rather than surprises."
    )
    pdf.bullet(
        "Transposition typos (\"shrama\" for \"sharma\") currently fall below threshold and "
        "return an explanatory 422 suggesting a spelling check. Trigram similarity is weak on "
        "transpositions by construction; a Damerau-Levenshtein pass on tokens would catch them."
    )
    pdf.bullet(
        "Pagination on GET /candidates. The board already eager-loads each candidate's "
        "transitions and notes, which is one extra query per load at this scale and would "
        "not be at a few thousand."
    )

    pdf.h2("Deployment")
    pdf.para(
        "Prepared, not launched: the build machine has no AWS credentials, and "
        "deploy/README.md records which parts are verified. One small EC2 instance runs the "
        "same compose file as local, Postgres included, behind a Cloudflare Tunnel - binding "
        "loopback, so the security group opens only SSH, and carrying no API key, so search "
        "degrades to the rules that answer the examples above. No authentication either, "
        "deliberately: anyone with the link can write."
    )

    pdf.output(str(output))


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "Mini-Hiring-Pipeline.pdf")
    build(target)
    print(f"Wrote {target} ({target.stat().st_size:,} bytes)")
