"""Write tools. They only create *proposed* `actions` rows; nothing is ever sent from here.

Sending happens later, and only for actions that passed the policy engine (Milestone 3)
and, where required, human approval (`services/approvals.py`).
"""

import uuid
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from payops import clock
from payops.db.models import Action, AgentRun, Invoice
from payops.db.models.enums import ActionStatus, ActionType, InvoiceStatus, Tone
from payops.errors import InvalidInputError, NotFoundError
from payops.services.audit import audited

MAX_FOLLOWUP_DAYS = 60

DraftActionType = Literal["reminder", "payment_plan_offer", "escalate_to_human", "no_action"]
DRAFT_ACTION_TYPES = frozenset(ActionType(t) for t in DraftActionType.__args__)  # type: ignore[attr-defined]

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(description="Injected by the agent runtime, not chosen by the LLM")
    invoice_id: uuid.UUID


class DraftMessageInput(_ActionInput):
    action_type: DraftActionType
    tone: Tone
    subject: Annotated[Text, StringConstraints(max_length=200)]
    body: Annotated[Text, StringConstraints(max_length=5000)]
    context: Annotated[str, StringConstraints(max_length=2000)] | None = Field(
        default=None, description="Why this action and tone were chosen"
    )


class ScheduleFollowupInput(_ActionInput):
    followup_date: date = Field(description=f"In the future, at most {MAX_FOLLOWUP_DAYS} days out")

    @field_validator("followup_date")
    @classmethod
    def _within_window(cls, value: date) -> date:
        today = clock.today()
        if value <= today:
            raise ValueError("followup_date must be in the future")
        if value > today + timedelta(days=MAX_FOLLOWUP_DAYS):
            raise ValueError(f"followup_date must be within {MAX_FOLLOWUP_DAYS} days")
        return value


class FlagForReviewInput(_ActionInput):
    reason: Annotated[Text, StringConstraints(min_length=3, max_length=1000)]


class ActionOut(BaseModel):
    action_id: uuid.UUID
    run_id: uuid.UUID
    invoice_id: uuid.UUID
    action_type: ActionType
    status: ActionStatus
    tone: Tone | None = None
    subject: str | None = None
    body: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _out(action: Action) -> ActionOut:
    return ActionOut(
        action_id=action.id,
        run_id=action.run_id,
        invoice_id=action.invoice_id,
        action_type=action.action_type,
        status=action.status,
        tone=action.tone,
        subject=action.subject,
        body=action.body,
        payload=action.payload,
    )


def _load_targets(session: Session, inp: _ActionInput) -> Invoice:
    if session.get(AgentRun, inp.run_id) is None:
        raise NotFoundError(f"Run {inp.run_id} not found")
    invoice = session.get(Invoice, inp.invoice_id)
    if invoice is None:
        raise NotFoundError(f"Invoice {inp.invoice_id} not found")
    return invoice


@audited(name="draft_message")
def draft_message(session: Session, inp: DraftMessageInput) -> ActionOut:
    """Store a drafted action and message for an invoice as a *proposed* action.
    Never sends anything. Redrafting replaces the previous proposed draft for the same
    run and invoice."""
    _load_targets(session, inp)
    action = session.scalars(
        select(Action).where(
            Action.run_id == inp.run_id,
            Action.invoice_id == inp.invoice_id,
            Action.status == ActionStatus.PROPOSED,
            Action.action_type.in_(DRAFT_ACTION_TYPES),
        )
    ).first()
    if action is None:
        action = Action(run_id=inp.run_id, invoice_id=inp.invoice_id)
        session.add(action)
    action.action_type = ActionType(inp.action_type)
    action.status = ActionStatus.PROPOSED
    action.tone = inp.tone
    action.subject = inp.subject
    action.body = inp.body
    action.payload = {"context": inp.context} if inp.context else {}
    session.flush()
    return _out(action)


@audited(name="schedule_followup")
def schedule_followup(session: Session, inp: ScheduleFollowupInput) -> ActionOut:
    """Propose a follow-up date for an unpaid invoice (future, within 60 days)."""
    invoice = _load_targets(session, inp)
    if invoice.status is InvoiceStatus.PAID:
        raise InvalidInputError(f"Invoice {invoice.id} is already paid")
    action = Action(
        run_id=inp.run_id,
        invoice_id=inp.invoice_id,
        action_type=ActionType.SCHEDULE_FOLLOWUP,
        status=ActionStatus.PROPOSED,
        payload={"followup_date": inp.followup_date.isoformat()},
    )
    session.add(action)
    session.flush()
    return _out(action)


@audited(name="flag_for_review")
def flag_for_review(session: Session, inp: FlagForReviewInput) -> ActionOut:
    """Flag an invoice for a human to look at, with a reason."""
    _load_targets(session, inp)
    action = Action(
        run_id=inp.run_id,
        invoice_id=inp.invoice_id,
        action_type=ActionType.FLAG_FOR_REVIEW,
        status=ActionStatus.PROPOSED,
        payload={"reason": inp.reason},
    )
    session.add(action)
    session.flush()
    return _out(action)
