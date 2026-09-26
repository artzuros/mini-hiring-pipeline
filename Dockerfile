FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The project is installed as a package, so `app` has to be present before
# pip runs. Copying it first means a change to a migration or a template
# rebuilds only the later layers, not the dependency layer above.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations

# Named files, not `COPY scripts ./scripts`. The scripts directory also holds
# `db.sh`, which manages a *local* macOS Postgres cluster over trust auth --
# meaningless inside this image, and not a thing that should sit one `exec`
# away in a container. `seed.py` is kept because the README documents it as
# the way to load sample data; it carries its own confirmation guard.
COPY scripts/entrypoint.sh scripts/seed.py ./scripts/

RUN chmod +x scripts/entrypoint.sh

# Runs as an unprivileged user. Nothing here needs root once pip is done.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Migrates, then serves. See scripts/entrypoint.sh for why those are ordered
# and retried the way they are.
ENTRYPOINT ["scripts/entrypoint.sh"]
