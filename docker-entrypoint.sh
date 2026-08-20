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

# Most PaaS hosts (Render, Railway, Heroku) assign the port at runtime via
# $PORT and route to nothing else, so it has to win over the baked-in default.
PORT_TO_BIND="${PORT:-${APP_PORT:-8080}}"

echo "==> starting dashboard on ${APP_HOST:-0.0.0.0}:${PORT_TO_BIND}"
exec python -m uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${PORT_TO_BIND}"
