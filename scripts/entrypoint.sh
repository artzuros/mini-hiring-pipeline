#!/usr/bin/env sh
# Container entrypoint: bring the schema up to date, then serve.
#
# Migrations run here rather than at build time because the database does not
# exist when the image is built -- the schema belongs to the deployment, not
# to the artefact. Running them on every start is safe: Alembic is a no-op
# once the database is already at head.
set -e

MAX_ATTEMPTS="${MIGRATION_MAX_ATTEMPTS:-30}"
attempt=1

while true; do
    if alembic upgrade head; then
        break
    fi

    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
        echo "Migrations failed after ${attempt} attempts. Giving up." >&2
        exit 1
    fi

    # The usual reason for a failure here is that Postgres is still starting.
    # The retry is bounded so that a genuine migration error surfaces as a
    # crash loop rather than an indefinite hang.
    echo "Database not ready (attempt ${attempt}/${MAX_ATTEMPTS}); retrying in 2s..." >&2
    attempt=$((attempt + 1))
    sleep 2
done

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
