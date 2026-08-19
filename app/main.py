"""FastAPI application factory.

Serves the REST API under ``/api`` and the server-rendered dashboard at ``/``,
and owns the scheduler lifecycle. One process, one port, one deploy.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, ensure_dirs, get_settings
from app.logging_setup import configure_logging, get_logger
from app.web.routes import api, pages

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_dirs()
    configure_logging(settings.log_level, settings.log_format)

    # First boot on a fresh volume: seed the registry in the background so the
    # port binds immediately and the platform health check passes.
    from app.services.bootstrap import schedule_seed

    if schedule_seed() is not None:
        log.info("bootstrap.seed_scheduled")

    scheduler = None
    if settings.scheduler_enabled:
        from app.scheduler import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        app.state.scheduler = scheduler
        log.info("scheduler.started", jobs=[j.id for j in scheduler.get_jobs()])
    else:
        app.state.scheduler = None
        log.info("scheduler.disabled")

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
        log.info("scheduler.stopped")


#: Paths that must stay reachable without credentials -- the platform health
#: check runs unauthenticated, and blocking it fails the deploy.
_PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})


def _install_basic_auth(app: FastAPI, username: str, password: str) -> None:
    """Gate the whole dashboard behind HTTP basic auth.

    The dashboard exposes a personal profile, resumes and an application
    tracker, so a public deployment must not be world-readable. Basic auth is
    enough for a single user and costs no session storage.
    """

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, secret = decoded.partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user, secret = "", ""
            # compare_digest on both fields, so neither is a timing oracle.
            if secrets.compare_digest(user, username) and secrets.compare_digest(secret, password):
                return await call_next(request)

        return Response(
            status_code=401,
            content="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="Internship Search"'},
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Internship Search Agent",
        description="Search broadly. Deduplicate aggressively. Rank intelligently. Notify selectively.",
        version="0.1.0",
        lifespan=lifespan,
    )

    settings = get_settings()
    if settings.dashboard_password:
        _install_basic_auth(app, settings.dashboard_user, settings.dashboard_password)
        log.info("auth.enabled", user=settings.dashboard_user)
    else:
        log.warning("auth.disabled")

    static_dir = PROJECT_ROOT / "app" / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(api.router, prefix="/api")
    app.include_router(pages.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover - safety net
        log.exception("request.failed", path=request.url.path)
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "internal error"}, status_code=500)
        return HTMLResponse(
            f"<h1>Something went wrong</h1><pre>{type(exc).__name__}</pre>", status_code=500
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
