from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from payops import __version__
from payops.db.session import check_db

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "error"]
    version: str


def get_db_check() -> Callable[[], None]:
    return check_db


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and database connectivity",
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
)
def health(db_check: Callable[[], None] = Depends(get_db_check)) -> JSONResponse:
    """Open endpoint (no API key) for Docker and load-balancer health checks."""
    try:
        db_check()
    except Exception:
        body = HealthResponse(status="degraded", db="error", version=__version__)
        return JSONResponse(status_code=503, content=body.model_dump())
    return JSONResponse(
        content=HealthResponse(status="ok", db="ok", version=__version__).model_dump()
    )
