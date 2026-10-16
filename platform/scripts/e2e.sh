#!/usr/bin/env bash
# Browser smoke test of the dashboard against LABELLED synthetic demo data.
# Needs an EMPTY, separate PostgreSQL database (default: trading_e2e) and a built frontend.
#   E2E_DATABASE_URL=postgresql+psycopg://trader:...@127.0.0.1:5432/trading_e2e ./scripts/e2e.sh
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
PY="${E2E_PYTHON:-python}"
export DATABASE_URL="${E2E_DATABASE_URL:-postgresql+psycopg://trader:trader_local_only@127.0.0.1:5432/trading_e2e}"
export E2E_USER="${E2E_USER:-e2e}" E2E_PASSWORD="${E2E_PASSWORD:-e2e-password-not-secret}"
export NEWS_PROVIDER=none TYPESAFE_API_KEY= SECRETS_MASTER_KEY=

cd "$here/backend"
"$PY" - <<'PY'
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
name = make_url(os.environ["DATABASE_URL"]).database or ""
if "e2e" not in name and "test" not in name:
    sys.exit(f"refusing to wipe database {name!r}: its name must contain 'e2e' or 'test'")
engine = create_engine(os.environ["DATABASE_URL"])
with engine.begin() as conn:
    conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))  # e2e database only
PY
"$PY" -m alembic upgrade head
"$PY" -m app.cli seed-demo
PLATFORM_PASSWORD="$E2E_PASSWORD" "$PY" -m app.cli create-user "$E2E_USER"

cd "$here/frontend"
E2E_PYTHON="$PY" npx playwright test "$@"
