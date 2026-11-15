"""Executes approved actions. "Sending" a message means inserting an outbox row."""

from sqlalchemy.orm import Session

from payops import clock
from payops.db.models import Action, Outbox
from payops.db.models.enums import MESSAGE_ACTION_TYPES, ActionStatus
from payops.errors import ConflictError

EMAIL_CHANNEL = "email"


def ensure_sendable(action: Action) -> None:
    """Raise before any state change if a message action has nothing to send."""
    if action.action_type in MESSAGE_ACTION_TYPES and not (action.subject and action.body):
        raise ConflictError(f"Action {action.id} has no drafted message to send")


def execute_action(session: Session, action: Action) -> Outbox | None:
    """Run an approved action deterministically (no LLM involved).

    Message actions (reminder, payment-plan offer) write exactly one outbox row with the
    already-drafted subject and body. Other action types are marked executed with no
    outbox row. The unique constraint on ``outbox.action_id`` prevents double sends.
    """
    if action.status is not ActionStatus.APPROVED:
        raise ConflictError(f"Action {action.id} is {action.status.value}, not approved")

    ensure_sendable(action)

    row: Outbox | None = None
    if action.action_type in MESSAGE_ACTION_TYPES:
        invoice = action.invoice
        row = Outbox(
            action_id=action.id,
            customer_id=invoice.customer_id,
            invoice_id=invoice.id,
            channel=EMAIL_CHANNEL,
            recipient=invoice.customer.email,
            subject=action.subject or "",
            body=action.body or "",
        )
        session.add(row)

    action.status = ActionStatus.EXECUTED
    action.executed_at = clock.now()
    session.flush()
    return row
