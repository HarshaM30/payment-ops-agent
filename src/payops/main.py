"""FastAPI application factory.

Run with:  uvicorn --factory payops.main:create_app
"""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request, Response

from payops import __version__
from payops.api.deps import PROTECTED_RESPONSES
from payops.api.errors import error_response, install_error_handlers
from payops.api.routes import approvals, audit, health, runs, ui
from payops.api.security import require_api_key
from payops.config import get_settings
from payops.logging import configure_logging, request_id_var

logger = logging.getLogger("payops.request")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def request_id_from(request: Request) -> str:
    """Reuse a well-formed incoming request id (e.g. from a proxy), else make one."""
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    return incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.run_recovery_on_startup:
        from payops.agents.runner import recover_interrupted_runs

        try:
            await asyncio.to_thread(recover_interrupted_runs)
        except Exception:  # the DB may be down at startup; /health will say so
            logger.exception("run recovery at startup failed")
    yield
    # Graceful shutdown: uvicorn has stopped accepting requests and waited (up to
    # --timeout-graceful-shutdown) for in-flight ones and their background runs.
    from payops.db.session import get_engine

    if get_engine.cache_info().currsize:
        get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()  # fail fast on bad configuration
    configure_logging(settings.log_level)
    timeout = settings.request_timeout_seconds

    app = FastAPI(
        title="Payment Ops Agent",
        version=__version__,
        description=(
            "Triage overdue invoices, draft follow-ups, and route risky actions to a human. "
            "All data is synthetic; 'sent' messages are rows in the outbox table."
        ),
        lifespan=lifespan,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Request id, request timeout, and one structured log line per request."""
        rid = request_id_from(request)
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            try:
                response = await asyncio.wait_for(call_next(request), timeout=timeout)
            except TimeoutError:
                # The client gets a 504; a sync handler already running in the threadpool
                # finishes on its own (Python threads can't be cancelled), and the DB
                # statement timeout bounds how long it can hold a connection.
                response = error_response(504, "timeout", f"Request took longer than {timeout:g}s")
            response.headers[REQUEST_ID_HEADER] = rid
            # Headers and query strings are deliberately not logged.
            logger.info(
                "request",
                extra={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    app.include_router(health.router)  # open: used by Docker / load-balancer health checks

    protected = APIRouter(dependencies=[Depends(require_api_key)], responses=PROTECTED_RESPONSES)
    protected.include_router(runs.router)
    protected.include_router(approvals.router)
    protected.include_router(audit.router)
    app.include_router(protected)

    # Server-rendered approvals page: session cookie + CSRF instead of the API key header.
    ui.install_ui(app)
    app.include_router(ui.router)
    return app
