"""Shared FastAPI dependencies and OpenAPI response declarations."""

from typing import Annotated, Any

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from payops.db.session import get_session
from payops.schemas.api import ErrorResponse
from payops.services.pagination import DEFAULT_LIMIT, MAX_LIMIT

SessionDep = Annotated[Session, Depends(get_session)]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_LIMIT, description=f"Page size (max {MAX_LIMIT})")]
CursorQuery = Annotated[
    str | None, Query(max_length=500, description="`next_cursor` from the previous page")
]

PROTECTED_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    422: {"model": ErrorResponse, "description": "Invalid request"},
}


def responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    descriptions = {404: "Not found", 409: "Conflict (already decided, or key reused)"}
    return {c: {"model": ErrorResponse, "description": descriptions[c]} for c in codes}


__all__ = ["DEFAULT_LIMIT", "CursorQuery", "LimitQuery", "SessionDep", "responses"]
