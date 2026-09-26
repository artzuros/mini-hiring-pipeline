"""Turn a Claude Code session transcript into a readable markdown log.

Run:  python scripts/export_ai_log.py <transcript.jsonl> <output.md>
            [--since ISO] [--until ISO] [--title T] [--note "paragraph"]

      python scripts/export_ai_log.py --scrub <existing.md>

Why this exists
---------------
The brief asks for the AI chat logs to be in the repository. The raw
transcript is a multi-megabyte JSONL file with one JSON object per event --
accurate, but not something a reviewer is going to read. This renders the
same events as markdown: what was asked, what was said, which tools ran.

Tool *results* are omitted entirely. Their full contents are the files
themselves, which are already in the repository; including them would
multiply the log's size to say nothing new.

One transcript file accumulates every session in a project, so `--since` and
`--until` select one window out of it. Every log is passed through
`REDACTIONS` on the way out, and `--scrub` applies that same table to a log
exported earlier -- so a file published months ago and a file exported today
cannot disagree about what is removed.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

#: Characters of a tool's input to keep.
INPUT_LIMIT = 600

DEFAULT_TITLE = "AI chat log — Mini Hiring Pipeline"

#: Applied in order to the fully rendered log -- header and body together, so
#: the two cannot drift apart -- and reused verbatim by `--scrub`.
#:
#: These are patterns rather than literal strings because the published copy
#: has already been hand-edited once (commit 2a7926f), and that edit left
#: behind fragments -- `...@gmail.com`, `n@gmail.com`, `gun**********@gmail.com`
#: -- that a literal replacement would not catch. Patterns absorb their own
#: history, which is the whole reason this table exists.
REDACTIONS: list[tuple[str, str]] = [
    # A personal Gmail address, in whatever form it appears. The `*` in the
    # class is there for the partially-masked form a previous hand-edit left
    # behind. Its cost: an address wrapped in markdown bold loses the `**`
    # that marked it, because the local-part match is greedy. That is a
    # cosmetic loss on a line whose entire content is being removed anyway.
    (r"[A-Za-z0-9._%+*-]+@gmail\.com", "[redacted email]"),
    # The same address's local part standing on its own -- in a shell command,
    # or in a table of counts -- where no domain is attached to give it away.
    # Runs *after* the rule above, so the domain form is matched whole rather
    # than left with a dangling `@gmail.com`.
    (r"\w*gunubansal\w*", "[redacted email]"),
    # The Claude Code attribution trailer, deliberately excluded from this
    # repository. Kept out by rule rather than by remembering. Three spellings
    # are covered, each for a reason seen in the transcript:
    #   - `&lt;…&gt;` -- a tool result that HTML-escaped its own output;
    #   - `\.`       -- the same string read back out of a Python regex literal
    #                   in a script whose job was to *detect* the trailer.
    # That last one is not attribution, and redacting it costs a slightly odd
    # line in a code listing. The alternative is an invariant with an exception
    # in it ("no trailer, except inside a detector"), which is harder to state
    # honestly and impossible to verify with a single grep.
    (
        r"Co-Authored-By: Claude Code (?:&lt;|<)noreply@anthropic\\?\.com(?:&gt;|>)",
        "[redacted from this published copy]",
    ),
    # The macOS account name, everywhere it appears. One rule because it is
    # one string doing four jobs: the path component in `/Users/pranavbansal`,
    # the hyphenated form in the transcript's project directory name, the
    # owner column in `ls -l` output, and -- for one bad stretch of this
    # project -- a fabricated git author address,
    # `pranavbansal@users.noreply.github.com`. That last one is additionally a
    # real GitHub handle belonging to someone else, which is the second reason
    # to remove it.
    #
    # Replaced with `[user]` and not `<user>` because this is markdown:
    # an angle-bracketed token is an HTML tag to every renderer, and GitHub
    # strips unknown tags, so `<user>` would silently vanish from the page.
    (r"\bpranavbansal\b", "[user]"),
]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [{len(text) - limit:,} more characters]"


def _render_tool_use(block: dict) -> str:
    name = block.get("name", "tool")
    tool_input = block.get("input", {}) or {}
    # The interesting field differs per tool; pick the one that says *what*
    # was acted on rather than dumping the whole input.
    target = (
        tool_input.get("file_path")
        or tool_input.get("command")
        or tool_input.get("description")
        or tool_input.get("pattern")
        or tool_input.get("prompt")
        or ""
    )
    lines = [f"**→ `{name}`**"]
    if target:
        lines.append(f"\n```\n{_truncate(str(target), INPUT_LIMIT)}\n```")
    return "\n".join(lines)


def redact(text: str) -> str:
    """Apply every rule in `REDACTIONS`, in order, to `text`."""
    for pattern, replacement in REDACTIONS:
        text = re.sub(pattern, replacement, text)
    return text


def _parse_stamp(value: str) -> datetime:
    """Parse a transcript or CLI timestamp into an aware UTC datetime.

    Transcript stamps end in `Z`, but a `--since` typed by hand usually does
    not, and comparing the two raises rather than guessing. Rather than
    require the caller to remember the suffix, a naive stamp is read as UTC --
    which is what the transcript's own stamps are, so the two are comparable
    and a bare `--since 2026-09-26T07:00:00` means what it looks like.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fmt(stamp: str | None) -> str:
    if not stamp:
        return "unknown"
    return _parse_stamp(stamp).strftime("%Y-%m-%d %H:%M UTC")


def export(
    src: Path,
    dest: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    title: str = DEFAULT_TITLE,
    notes: list[str] | None = None,
) -> tuple[int, int]:
    """Render the `[since, until)` window of `src` into `dest`.

    The window is half-open so that consecutive windows tile without
    overlapping: `--since A --until B` followed by `--since B` exports every
    record exactly once.
    """
    turns: list[str] = []
    user_count = assistant_count = 0
    started = finished = None

    with src.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            stamp = record.get("timestamp")
            if stamp:
                when = _parse_stamp(stamp)
                if since is not None and when < since:
                    continue
                if until is not None and when >= until:
                    continue
                started = started or stamp
                finished = stamp
            elif since is not None or until is not None:
                # A record with no timestamp cannot be placed inside a window,
                # so a windowed export leaves it out rather than guessing.
                continue

            kind = record.get("type")
            message = record.get("message") or {}
            content = message.get("content")

            if kind == "user":
                text = _text_of(content)
                # Tool results arrive as "user" records too. They are rendered
                # with their tool call, so skip them here rather than doubling
                # up the log with raw output.
                if not text.strip():
                    continue
                user_count += 1
                turns.append(f"### 🧑 Recruiter\n\n{text.strip()}\n")

            elif kind == "assistant":
                pieces: list[str] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            body = block.get("text", "").strip()
                            if body:
                                pieces.append(body)
                        elif block.get("type") == "tool_use":
                            pieces.append(_render_tool_use(block))
                if not pieces:
                    # Reasoning-only turns carry no visible content.
                    continue
                assistant_count += 1
                turns.append("### 🤖 Claude\n\n" + "\n\n".join(pieces) + "\n")

    header = [
        f"# {title}",
        "",
        f"- **Source:** `{src.name}`",
        f"- **Span:** {_fmt(started)} → {_fmt(finished)}",
        f"- **Messages:** {user_count} from the recruiter, "
        f"{assistant_count} from Claude",
        "",
        "Exported verbatim from the Claude Code transcript. Tool calls appear "
        "inline so the reasoning and the edits can be read together; tool "
        "*results* are omitted, since their full contents are the files "
        "themselves and are already in this repository.",
        "",
    ]
    for note in notes or []:
        header += [note, ""]
    header += ["---", ""]

    body = "\n".join(header) + "\n".join(turns)
    dest.write_text(redact(body) + "\n")
    return user_count, assistant_count


def scrub(path: Path) -> None:
    """Apply `REDACTIONS` to an already-exported log, in place.

    Exists so that a file published before a rule was added can be brought up
    to date by the *same* table that governs new exports. Editing it by hand
    instead would work exactly once, and the two would drift.
    """
    original = path.read_text()
    cleaned = redact(original)
    if cleaned == original:
        print(f"{path}: nothing to redact")
        return
    path.write_text(cleaned)

    before, after = original.splitlines(), cleaned.splitlines()
    # Every replacement is newline-free, so redaction can rewrite lines but
    # never add or remove them. If that ever stops being true, the diff this
    # prints would be misleading, so it is asserted rather than assumed.
    assert len(before) == len(after), "redaction changed the line count"
    changed = sum(1 for a, b in zip(before, after) if a != b)
    print(f"{path}: rewrote {changed} line(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_ai_log.py",
        description="Render a Claude Code transcript as a markdown log.",
    )
    parser.add_argument(
        "transcript", nargs="?", type=Path, help="the .jsonl transcript to read"
    )
    parser.add_argument(
        "output", nargs="?", type=Path, help="the .md file to write"
    )
    parser.add_argument(
        "--since", help="ISO timestamp; skip records before it (inclusive)"
    )
    parser.add_argument(
        "--until", help="ISO timestamp; skip records at or after it (exclusive)"
    )
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        metavar="PARAGRAPH",
        help="a paragraph to place in the header; repeatable",
    )
    parser.add_argument(
        "--scrub",
        type=Path,
        metavar="FILE",
        help="apply the redaction rules to an existing log, in place",
    )
    args = parser.parse_args(argv)

    if args.scrub:
        scrub(args.scrub.expanduser())
        return 0

    if not args.transcript or not args.output:
        parser.error("transcript and output are required unless --scrub is given")

    window: dict = {}
    if args.since:
        window["since"] = _parse_stamp(args.since)
    if args.until:
        window["until"] = _parse_stamp(args.until)

    target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    users, assistants = export(
        args.transcript.expanduser(),
        target,
        title=args.title,
        notes=args.note,
        **window,
    )
    print(
        f"Wrote {target} — {users} user turns, {assistants} assistant turns, "
        f"{target.stat().st_size:,} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
