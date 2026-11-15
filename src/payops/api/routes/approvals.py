import uuid

from fastapi import APIRouter, Query

from payops.api.deps import DEFAULT_LIMIT, CursorQuery, LimitQuery, SessionDep, responses
from payops.db.models import Approval
from payops.db.models.enums import ApprovalStatus
from payops.schemas.api import (
    ActionSummary,
    ApprovalOut,
    ApprovalPage,
    ApproveRequest,
    CustomerSummary,
    InvoiceSummary,
    RejectRequest,
)
from payops.services import approvals as approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


def to_out(approval: Approval) -> ApprovalOut:
    action = approval.action
    return ApprovalOut(
        id=approval.id,
        status=approval.status,
        reasons=approval.reasons,
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
        decision_reason=approval.decision_reason,
        created_at=approval.created_at,
        action=ActionSummary.model_validate(action),
        invoice=InvoiceSummary.model_validate(action.invoice),
        customer=CustomerSummary.model_validate(action.invoice.customer),
    )


@router.get("", response_model=ApprovalPage, summary="List approvals")
def list_approvals(
    session: SessionDep,
    status: ApprovalStatus = Query(default=ApprovalStatus.PENDING),
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> ApprovalPage:
    """Newest first. Defaults to the pending queue."""
    rows, next_cursor = approval_service.list_approvals(
        session, status=status, limit=limit, cursor=cursor
    )
    return ApprovalPage(items=[to_out(r) for r in rows], next_cursor=next_cursor)


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalOut,
    summary="Approve and execute",
    responses=responses(404, 409),
)
def approve(approval_id: uuid.UUID, body: ApproveRequest, session: SessionDep) -> ApprovalOut:
    """Executes the already-drafted action exactly as written (message actions write one
    outbox row). No LLM call happens after the decision. **409** if already decided."""
    return to_out(approval_service.approve(session, approval_id, approver=body.approver))


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalOut,
    summary="Reject",
    responses=responses(404, 409),
)
def reject(approval_id: uuid.UUID, body: RejectRequest, session: SessionDep) -> ApprovalOut:
    """Marks the action rejected; nothing is sent. A reason is required."""
    return to_out(
        approval_service.reject(session, approval_id, approver=body.approver, reason=body.reason)
    )
