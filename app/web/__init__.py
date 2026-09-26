"""The server-rendered UI: one board page and one candidate page.

This exists because the assignment asks for a small *web app* with a search
box, and an API alone does not satisfy that however good the API is. It is
deliberately minimal and deliberately separate from `app/api/`:

* No JavaScript build step, no bundler, no client-side framework. Every
  interaction is a form POST followed by a redirect, so the whole thing works
  with JavaScript switched off and can be read top to bottom by a reviewer.
* Both layers call the *same* service functions. The UI is not a second
  implementation of the rules; it is a second skin over them. A stage move
  made through the browser and one made through the API are the same code
  path, so they cannot disagree about what is legal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def humanize_duration(seconds: int) -> str:
    """Render a duration the way a person would say it.

    Coarse on purpose. A recruiter asking "how long has she been in
    Screening?" wants "9 days", not "9 days, 4 hours, 12 minutes".
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"

    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''}"

    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''}"

    years = days // 365
    return f"{years} year{'s' if years != 1 else ''}"


def humanize_date(value: datetime) -> str:
    """A short absolute timestamp, for the audit trail."""
    local = value.astimezone(UTC)
    return local.strftime("%d %b %Y, %H:%M")


def humanize_ago(value: datetime, *, now: datetime | None = None) -> str:
    reference = now or datetime.now(UTC)
    return humanize_duration(int((reference - value).total_seconds()))


templates.env.filters["duration"] = humanize_duration
templates.env.filters["timestamp"] = humanize_date
templates.env.filters["ago"] = humanize_ago
