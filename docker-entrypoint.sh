#!/bin/sh
# Migrate, then serve. Idempotent, so it is safe on every restart.
#
# Seeding the ATS registry is deliberately NOT done here: it takes minutes, and
# anything slow before the port binds fails the platform health check. The app
# seeds itself in the background on first boot instead -- see
# app/services/bootstrap.py.
set -e

echo "==> running migrations"
python -m alembic upgrade head

echo "==> starting dashboard + scheduler on ${APP_HOST:-0.0.0.0}:${APP_PORT:-8080}"
exec python -m uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8080}"
