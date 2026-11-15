import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from payops.api.deps import DEFAULT_LIMIT, CursorQuery, LimitQuery, SessionDep
from payops.schemas.api import AuditEntry, AuditPage
from payops.services import audit as audit_service

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditPage, summary="Query the audit log")
def list_audit(
    session: SessionDep,
    run_id: uuid.UUID | None = None,
    event_type: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> AuditPage:
    """Newest first, filterable by run and event type (`tool_call`, `run_created`,
    `approval_decision`, ...)."""
    rows, next_cursor = audit_service.list_entries(
        session, run_id=run_id, event_type=event_type, limit=limit, cursor=cursor
    )
    return AuditPage(items=[AuditEntry.model_validate(r) for r in rows], next_cursor=next_cursor)
