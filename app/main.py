"""FastAPI application factory.

Serves the REST API under ``/api`` and the server-rendered dashboard at ``/``,
and owns the scheduler lifecycle. One process, one port, one deploy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Internship Search Agent",
        description="Search broadly. Deduplicate aggressively. Rank intelligently. Notify selectively.",
        version="0.1.0",
        lifespan=lifespan,
    )

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
