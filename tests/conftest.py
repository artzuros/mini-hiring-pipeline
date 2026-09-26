"""Shared test fixtures.

Design notes, because the ordering here is load-bearing:

* The test database URL is pushed into the environment *before* anything from
  `app` is imported. `app.config.get_settings` is `lru_cache`d and
  `app.db` builds its engine lazily but only once, so whichever URL is
  visible on first access wins for the whole session. Setting it here means
  the suite can never accidentally truncate the development database.

* Migrations run via a subprocess rather than `alembic.command.upgrade`.
  `migrations/env.py` calls `asyncio.run`, which cannot be nested inside the
  event loop pytest-asyncio is already running. A subprocess sidesteps that
  entirely and has the side benefit of exercising the exact command a
  developer would run by hand.

* Tables are cleaned with `TRUNCATE`, not `DELETE`. The audit table carries a
  `BEFORE DELETE ... FOR EACH ROW` trigger that raises; `TRUNCATE` is a
  statement-level operation and does not fire row triggers, so it clears the
  table without fighting the immutability guard.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Environment must be set before any `app` import ----------------------
from app.config import Settings  # noqa: E402

_settings = Settings()

os.environ["DATABASE_URL"] = _settings.test_database_url
# Never let a test reach the network, even if the developer has a real key in
# .env. The LLM fallback is exercised with a monkeypatched client instead.
os.environ["SEARCH_LLM_FALLBACK_ENABLED"] = "false"
os.environ["ANTHROPIC_API_KEY"] = ""

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

TEST_DATABASE_URL = _settings.test_database_url


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_database() -> Iterator[None]:
    """Bring the test database to `head` once per session."""
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed against the test database.\n"
            f"Is Postgres running?  ./scripts/db.sh start\n\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    yield


@pytest.fixture(scope="session")
async def engine(_migrate_test_database: None):
    """One async engine for the whole session, on the session's event loop."""
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean_database(engine) -> AsyncIterator[None]:
    """Empty both tables before every test that touches the database.

    Autouse and independent of which connection fixture a test happens to
    request -- an earlier version hung this off the `session` fixture, which
    meant raw-SQL tests (that never asked for a session) silently inherited
    rows from their predecessors.

    Truncating *before* rather than after means a test that crashes hard still
    leaves the next one a clean slate.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE candidate_notes, stage_transitions, candidates CASCADE")
        )
    yield


@pytest.fixture
async def session(engine, _clean_database: None) -> AsyncIterator[AsyncSession]:
    """A session bound to a clean database."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest.fixture
async def raw_connection(engine, _clean_database: None):
    """A connection for tests that need to bypass the ORM and issue raw SQL."""
    async with engine.connect() as conn:
        yield conn
