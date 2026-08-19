# Single-process image: the FastAPI dashboard and the APScheduler digests run
# in one container, matching how the app runs locally.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini docker-entrypoint.sh ./

# Editable install, so templates, static files and migrations resolve to the
# copied source tree rather than a wheel that omits non-Python data.
RUN pip install --upgrade pip && pip install -e ".[postgres]" && chmod +x docker-entrypoint.sh

# Mount point for the persistent volume: SQLite, uploaded resumes, HTTP cache.
RUN mkdir -p /app/data

ENV PYTHONPATH=/app \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    LOG_FORMAT=json
EXPOSE 8080

ENTRYPOINT ["./docker-entrypoint.sh"]
