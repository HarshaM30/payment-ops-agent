"""Human approval decisions. Shared by the JSON API and (Milestone 6) the HTML page.

Approving executes the already-drafted action deterministically: no LLM call happens
after a human decision, so the approver signs off on exactly the text that is sent.
"""

import uuid
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, joinedload

from payops import clock
from payops.config import get_settings
from payops.db.models import Action, AgentRun, Approval, Invoice
from payops.db.models.enums import ActionStatus, ApprovalStatus, RunStatus
from payops.errors import ConflictError, NotFoundError
from payops.policy.engine import evaluate
from payops.policy.rules import Outcome, PolicyInput
from payops.services import audit
from payops.services.outbox import ensure_sendable, execute_action
from payops.services.pagination import decode_cursor, encode_cursor, invalid_cursor

_LOAD_DETAILS = (
    joinedload(Approval.action).joinedload(Action.invoice).joinedload(Invoice.customer),
)


def list_approvals(
    session: Session, *, status: ApprovalStatus, limit: int, cursor: str | None
) -> tuple[list[Approval], str | None]:
    """Newest first, keyset-paginated on (created_at, id)."""
    stmt = select(Approval).options(*_LOAD_DETAILS).where(Approval.status == status)
    if cursor:
        data = decode_cursor(cursor, {"created_at", "id"})
        try:
            after = (datetime.fromisoformat(data["created_at"]), uuid.UUID(data["id"]))
        except (TypeError, ValueError) as exc:
            raise invalid_cursor() from exc
        stmt = stmt.where(tuple_(Approval.created_at, Approval.id) < after)
    rows = list(
        session.scalars(
            stmt.order_by(Approval.created_at.desc(), Approval.id.desc()).limit(limit + 1)
        )
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})
    return rows, next_cursor


def _lock_pending(session: Session, approval_id: uuid.UUID) -> Approval:
    approval = session.scalars(
        select(Approval)
        .where(Approval.id == approval_id)
        .with_for_update(of=Approval)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if approval is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    if approval.status is not ApprovalStatus.PENDING:
        raise ConflictError(f"Approval {approval_id} is already {approval.status.value}")
    if approval.action.status is not ActionStatus.PROPOSED:
        raise ConflictError(
            f"Action {approval.action_id} is already {approval.action.status.value}"
        )
    return approval


def _reload(session: Session, approval_id: uuid.UUID) -> Approval:
    return session.scalars(
        select(Approval)
        .options(*_LOAD_DETAILS)
        .where(Approval.id == approval_id)
        .execution_options(populate_existing=True)
    ).one()


def _check_policy(action: Action) -> None:
    """Re-run the guardrails on the exact stored text against the invoice as it is now.

    A human can approve what needs approval, but never what the policy blocks (e.g. the
    invoice was disputed after the draft was written).
    """
    invoice = action.invoice
    decision = evaluate(
        PolicyInput(
            action_type=action.action_type,
            invoice_status=invoice.status,
            balance=invoice.balance,
            approval_threshold=get_settings().approval_threshold_amount,
            subject=action.subject,
            body=action.body,
        )
    )
    if decision.outcome is Outcome.BLOCK:
        raise ConflictError(
            f"Action {action.id} is blocked by policy: {', '.join(decision.violated_rules)}",
            [{"rule": r.rule, "msg": r.message} for r in decision.violations],
        )


def _complete_run_if_done(session: Session, run_id: uuid.UUID) -> None:
    """A run awaiting approval is completed once none of its approvals are pending."""
    run = session.get(AgentRun, run_id)
    if run is None or run.status is not RunStatus.AWAITING_APPROVAL:
        return
    session.flush()
    pending = session.scalar(
        select(Approval.id)
        .join(Action, Approval.action_id == Action.id)
        .where(Action.run_id == run_id, Approval.status == ApprovalStatus.PENDING)
        .limit(1)
    )
    if pending is None:
        run.status = RunStatus.COMPLETED
        run.finished_at = clock.now()


def approve(session: Session, approval_id: uuid.UUID, *, approver: str) -> Approval:
    approval = _lock_pending(session, approval_id)
    action = approval.action
    ensure_sendable(action)  # validate before changing any state
    _check_policy(action)
    approval.status = ApprovalStatus.APPROVED
    approval.decided_by = approver
    approval.decided_at = clock.now()
    action.status = ActionStatus.APPROVED
    outbox_row = execute_action(session, action)
    audit.record(
        session,
        event_type="approval_decision",
        name="approve",
        run_id=action.run_id,
        actor=approver,
        inputs={"approval_id": approval_id, "approver": approver},
        outputs={
            "approval_status": approval.status.value,
            "action_id": action.id,
            "action_status": action.status.value,
            "outbox_id": outbox_row.id if outbox_row else None,
        },
    )
    _complete_run_if_done(session, action.run_id)
    session.commit()
    return _reload(session, approval_id)


def reject(session: Session, approval_id: uuid.UUID, *, approver: str, reason: str) -> Approval:
    approval = _lock_pending(session, approval_id)
    action = approval.action
    approval.status = ApprovalStatus.REJECTED
    approval.decided_by = approver
    approval.decided_at = clock.now()
    approval.decision_reason = reason
    action.status = ActionStatus.REJECTED
    audit.record(
        session,
        event_type="approval_decision",
        name="reject",
        run_id=action.run_id,
        actor=approver,
        inputs={"approval_id": approval_id, "approver": approver, "reason": reason},
        outputs={
            "approval_status": approval.status.value,
            "action_id": action.id,
            "action_status": action.status.value,
        },
    )
    _complete_run_if_done(session, action.run_id)
    session.commit()
    return _reload(session, approval_id)
