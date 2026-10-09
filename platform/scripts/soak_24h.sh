#!/usr/bin/env bash
# 24-hour PAPER soak test against the running compose stack. Writes reports/soak-<time>.md.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T api python -m app.cli soak --hours "${HOURS:-24}" --every "${EVERY:-60}" --out /app/reports
