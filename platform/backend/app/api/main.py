"""FastAPI application: `uvicorn --factory app.api.main:create_app`.

The browser reaches it only through the Next.js same-origin proxy. There is no CORS, no public
OpenAPI page, and validation errors never echo submitted values (they may contain API keys).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..core.config import Settings, get_settings
from ..core.db import session_factory
from ..core.secrets import configure_logging
from ..core.timeutil import utcnow
from . import auth, control, read


def create_app(settings: Settings | None = None, sf=None, clock=utcnow, http_transport=None) -> FastAPI:
    if settings is None:  # production start; tests pass their own settings and keep pytest's logging
        configure_logging()
    s = settings or get_settings()
    app = FastAPI(title="Trading platform API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = s
    app.state.sf = sf or session_factory(s.database_url)
    app.state.clock = clock
    app.state.http_transport = http_transport  # tests inject a mock; production uses the network

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"detail": "Невалидни данни.", "errors": errors}, status_code=422)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True}

    app.include_router(auth.router)
    app.include_router(read.router)
    app.include_router(control.router)
    return app

