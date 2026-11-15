"""FastAPI application factory.

Run with:  uvicorn --factory payops.main:create_app
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Request, Response

from payops import __version__
from payops.api.deps import PROTECTED_RESPONSES
from payops.api.errors import install_error_handlers
from payops.api.routes import approvals, audit, health, runs
from payops.api.security import require_api_key
from payops.config import get_settings
from payops.logging import configure_logging

logger = logging.getLogger("payops.request")


def create_app() -> FastAPI:
    settings = get_settings()  # fail fast on bad configuration
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Payment Ops Agent",
        version=__version__,
        description=(
            "Triage overdue invoices, draft follow-ups, and route risky actions to a human. "
            "All data is synthetic; 'sent' messages are rows in the outbox table."
        ),
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        # Headers and query strings are deliberately not logged.
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response

    app.include_router(health.router)  # open: used by Docker / load-balancer health checks

    protected = APIRouter(dependencies=[Depends(require_api_key)], responses=PROTECTED_RESPONSES)
    protected.include_router(runs.router)
    protected.include_router(approvals.router)
    protected.include_router(audit.router)
    app.include_router(protected)
    return app
