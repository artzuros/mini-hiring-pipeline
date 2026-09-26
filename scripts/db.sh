#!/usr/bin/env bash
# Manage the project-local Postgres 16 cluster used for development.
#
# This exists because Docker is optional here. On a machine with Docker, use
# `docker compose up` instead and ignore this script entirely -- both paths
# reach an identical database schema, because both run the same Alembic
# migrations.
#
#   ./scripts/db.sh init     # create the cluster (once)
#   ./scripts/db.sh start    # start it on 127.0.0.1:5433
#   ./scripts/db.sh stop
#   ./scripts/db.sh status
#   ./scripts/db.sh psql     # open a shell on the dev database
#
# The data directory lives in ./.pgdata and is gitignored. Creating it touches
# nothing on your system outside this repo -- no shared service, no password.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="$REPO_ROOT/.pgdata"
PGLOG="$REPO_ROOT/.pglog"
PORT="${PGPORT:-5433}"
DB_NAME="${PGDATABASE:-hiring}"
TEST_DB_NAME="${TEST_PGDATABASE:-hiring_test}"

# Prefer postgresql@16 (what the plan pins); fall back to whatever is on PATH.
if [[ -d /opt/homebrew/opt/postgresql@16/bin ]]; then
  export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
elif [[ -d /usr/local/opt/postgresql@16/bin ]]; then
  export PATH="/usr/local/opt/postgresql@16/bin:$PATH"
fi

require_pg() {
  command -v initdb >/dev/null 2>&1 || {
    echo "Postgres 16 not found. Install it with:  brew install postgresql@16" >&2
    exit 1
  }
}

cmd_init() {
  require_pg
  if [[ -d "$PGDATA" ]]; then
    echo "Cluster already exists at $PGDATA -- nothing to do."
    return
  fi
  initdb -D "$PGDATA" -U postgres --auth=trust --encoding=UTF8 --locale=C
  echo
  echo "Cluster created. Run: ./scripts/db.sh start"
}

cmd_start() {
  require_pg
  if [[ ! -d "$PGDATA" ]]; then
    echo "No cluster at $PGDATA -- run: ./scripts/db.sh init" >&2
    exit 1
  fi
  if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    echo "Already running."
    return
  fi
  pg_ctl -D "$PGDATA" -l "$PGLOG" \
    -o "-p $PORT -c listen_addresses=127.0.0.1" start

  # Create the databases if this is a fresh cluster.
  for db in "$DB_NAME" "$TEST_DB_NAME"; do
    if ! psql -h 127.0.0.1 -p "$PORT" -U postgres -lqt \
        | cut -d'|' -f1 | grep -qw "$db"; then
      createdb -h 127.0.0.1 -p "$PORT" -U postgres "$db"
      echo "Created database: $db"
    fi
  done
  echo "Postgres 16 listening on 127.0.0.1:$PORT"
}

cmd_stop() {
  require_pg
  pg_ctl -D "$PGDATA" stop -m fast
}

cmd_status() {
  require_pg
  pg_ctl -D "$PGDATA" status || true
}

cmd_psql() {
  require_pg
  psql -h 127.0.0.1 -p "$PORT" -U postgres -d "$DB_NAME"
}

case "${1:-}" in
  init)   cmd_init ;;
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  psql)   cmd_psql ;;
  *)
    echo "usage: $0 {init|start|stop|status|psql}" >&2
    exit 2
    ;;
esac
