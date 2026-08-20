"""FastAPI application factory.

Serves the REST API under ``/api`` and the server-rendered dashboard at ``/``,
and owns the scheduler lifecycle. One process, one port, one deploy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, ensure_dirs, get_settings
from app.logging_setup import configure_logging, get_logger
from app.web.routes import accounts, api, pages, quick_actions

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

    app.state.signing_key = _load_signing_key()

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


def _load_signing_key() -> bytes:
    """The cookie-signing key, read once at startup.

    Persisted in the database so it survives the restarts a sleeping free-tier
    host performs constantly. If the database cannot be read yet -- a fresh
    install mid-migration, or a test harness -- fall back to a process-local
    key: sessions then last only as long as the process, which is correct
    behaviour for a database that cannot yet vouch for anyone.
    """
    import secrets

    from app.db import session_scope
    from app.services.auth import get_signing_key

    try:
        with session_scope() as session:
            return get_signing_key(session)
    except Exception:
        log.warning("auth.signing_key_ephemeral")
        return secrets.token_urlsafe(48).encode("utf-8")


#: Reachable without a session: the platform health check runs unauthenticated,
#: and the sign-in pages obviously cannot require being signed in.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/health", "/login", "/signup", "/logout", "/forgot"}
)
#: ``/a/`` carries its own signed, single-purpose authority -- see
#: app/services/action_tokens.py. It is deliberately usable from a phone that
#: has never signed in, which is the entire point of one-click triage.
#: ``/reset/`` carries a single-use token tied to the current password hash --
#: see app/services/password_reset.py. Someone who has forgotten their password
#: obviously cannot sign in to use it.
_PUBLIC_PREFIXES: tuple[str, ...] = ("/static/", "/a/", "/reset/")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _install_session_auth(app: FastAPI) -> None:
    """Resolve the signed session cookie into ``request.state.user``.

    The dashboard holds a personal profile, resumes and an application tracker,
    so nothing but the paths above is readable without an account. Browsers get
    a redirect to the sign-in page; ``/api`` callers get a 401, because
    redirecting an XHR to an HTML login form only produces a confusing error.
    """

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        request.state.user = None

        if not _is_public(request.url.path):
            from app.services import auth

            # The cookie is signed, so who it names can be trusted without
            # asking the database -- which keeps every page load free of an
            # extra round trip to a remote Postgres.
            claims = auth.read_claims(
                request.cookies.get(auth.SESSION_COOKIE), request.app.state.signing_key
            )
            user_id = claims.get("uid") if claims else None
            if user_id is not None:
                request.state.user = claims

            if user_id is None:
                if request.url.path.startswith("/api"):
                    return JSONResponse({"detail": "authentication required"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            request.state.user_id = user_id

        return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Internship Search Agent",
        description="Search broadly. Deduplicate aggressively. Rank intelligently. Notify selectively.",
        version="0.1.0",
        lifespan=lifespan,
    )

    _install_session_auth(app)

    static_dir = PROJECT_ROOT / "app" / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(api.router, prefix="/api")
    app.include_router(accounts.router)
    app.include_router(quick_actions.router)
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
