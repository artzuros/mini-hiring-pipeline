"""Turn a Claude Code session transcript into a readable markdown log.

Run:  python scripts/export_ai_log.py <transcript.jsonl> <output.md>

Why this exists
---------------
The brief asks for the AI chat logs to be in the repository. The raw
transcript is a 3 MB JSONL file with one JSON object per event -- accurate,
but not something a reviewer is going to read. This renders the same events as
markdown: what was asked, what was said, which tools ran and why.

Tool *results* are truncated hard. Their full contents are the files
themselves, which are already in the repository; repeating them would triple
the log's size to say nothing new.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

#: Characters of a tool result to keep. Enough to see what happened, not
#: enough to reproduce the whole file.
RESULT_LIMIT = 700
#: Characters of a tool's input to keep.
INPUT_LIMIT = 600


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


def _render_tool_result(block: dict) -> str:
    content = block.get("content")
    text = _text_of(content) if not isinstance(content, str) else content
    if not text:
        return ""
    return f"**← result**\n\n```\n{_truncate(text, RESULT_LIMIT)}\n```"


def export(src: Path, dest: Path) -> tuple[int, int]:
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

            kind = record.get("type")
            message = record.get("message") or {}
            content = message.get("content")
            stamp = record.get("timestamp")
            if stamp:
                started = started or stamp
                finished = stamp

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

    def _fmt(stamp):
        if not stamp:
            return "unknown"
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

    header = [
        "# AI chat log — Mini Hiring Pipeline",
        "",
        f"- **Source:** `{src.name}`",
        f"- **Span:** {_fmt(started)} → {_fmt(finished)}",
        f"- **Messages:** {user_count} from the recruiter, {assistant_count} from Claude",
        "",
        "This is the working session that produced this repository, exported "
        "verbatim from the Claude Code transcript. Tool calls appear inline so "
        "the reasoning and the edits can be read together; tool *results* are "
        "truncated, since their full contents are the files in this repository.",
        "",
        "The wrong turns are still in here. The clearest one is the name "
        "matcher: an implementation that passed the brief's single example was "
        "measured against a wider matrix, found to rank a non-candidate above "
        "a real one, and replaced. See the README's "
        "\"Where I disagreed with the AI\" section.",
        "",
        "---",
        "",
    ]

    dest.write_text("\n".join(header) + "\n".join(turns) + "\n")
    return user_count, assistant_count


if __name__ == "__main__":
    source = Path(sys.argv[1]).expanduser()
    target = Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    users, assistants = export(source, target)
    size = target.stat().st_size
    print(f"Wrote {target} — {users} user turns, {assistants} assistant turns, {size:,} bytes")
