"""Deterministic facts about invoices, loaded straight from the database.

Triage features and every policy check use these facts, never anything the LLM
claims, so a model that misreads (or invents) an invoice status cannot get past the
guardrails.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from payops import clock
from payops.db.models import AgentRun, Invoice, PaymentHistory, PaymentPlan
from payops.db.models.enums import InvoiceStatus, PaymentEventType, PaymentPlanStatus
from payops.tools.history import summarize
from payops.tools.invoices import days_overdue

HISTORY_MONTHS = 12
MAX_CANDIDATES = 500


@dataclass(frozen=True)
class InvoiceFacts:
    invoice_id: uuid.UUID
    invoice_number: str
    customer_id: uuid.UUID
    customer_name: str
    company: str
    status: InvoiceStatus
    amount: Decimal
    balance: Decimal
    currency: str
    due_date: date
    days_overdue: int
    invoices_paid: int
    on_time_rate: float | None
    avg_days_late: float | None
    partial_payment_count: int
    broken_promises: int
    active_plan: bool

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, uuid.UUID | Decimal | date):
                data[key] = str(value)
            elif isinstance(value, InvoiceStatus):
                data[key] = value.value
        return data


def load_facts(session: Session, invoice_ids: list[uuid.UUID]) -> dict[uuid.UUID, InvoiceFacts]:
    if not invoice_ids:
        return {}
    invoices = session.scalars(
        select(Invoice)
        .where(Invoice.id.in_(invoice_ids))
        .options(joinedload(Invoice.customer))
        .execution_options(populate_existing=True)
    ).all()
    customer_ids = {i.customer_id for i in invoices}
    now = clock.now()
    since = now - timedelta(days=30 * HISTORY_MONTHS)

    history_invoices: dict[uuid.UUID, list[Invoice]] = {c: [] for c in customer_ids}
    for inv in session.scalars(
        select(Invoice)
        .where(Invoice.customer_id.in_(customer_ids))
        .options(selectinload(Invoice.payments))
    ):
        history_invoices[inv.customer_id].append(inv)

    broken: dict[uuid.UUID, int] = dict.fromkeys(customer_ids, 0)
    for ev in session.scalars(
        select(PaymentHistory).where(
            PaymentHistory.customer_id.in_(customer_ids),
            PaymentHistory.event_type == PaymentEventType.PROMISE_TO_PAY,
            PaymentHistory.occurred_at >= since,
        )
    ):
        if (ev.details or {}).get("kept") is False:
            broken[ev.customer_id] += 1

    with_plan = set(
        session.scalars(
            select(PaymentPlan.customer_id).where(
                PaymentPlan.customer_id.in_(customer_ids),
                PaymentPlan.status == PaymentPlanStatus.ACTIVE,
            )
        )
    )

    today = now.date()
    facts: dict[uuid.UUID, InvoiceFacts] = {}
    for inv in invoices:
        summary = summarize(history_invoices[inv.customer_id], since)
        facts[inv.id] = InvoiceFacts(
            invoice_id=inv.id,
            invoice_number=inv.invoice_number,
            customer_id=inv.customer_id,
            customer_name=inv.customer.name,
            company=inv.customer.company,
            status=inv.status,
            amount=inv.amount,
            balance=inv.balance,
            currency=inv.currency,
            due_date=inv.due_date,
            days_overdue=days_overdue(inv, today),
            invoices_paid=summary.invoices_paid,
            on_time_rate=summary.on_time_rate,
            avg_days_late=summary.avg_days_late,
            partial_payment_count=summary.partial_payment_count,
            broken_promises=broken[inv.customer_id],
            active_plan=inv.customer_id in with_plan,
        )
    return facts


def candidate_invoice_ids(session: Session, run: AgentRun) -> list[uuid.UUID]:
    """Invoices in scope for a run: the requested ones, or every past-due unpaid invoice."""
    if run.requested_invoice_ids is not None:
        return [uuid.UUID(i) for i in run.requested_invoice_ids]
    today = clock.today()
    stmt = (
        select(Invoice.id)
        .where(
            Invoice.amount_paid < Invoice.amount,
            or_(
                Invoice.status.in_((InvoiceStatus.OVERDUE, InvoiceStatus.DISPUTED)),
                (Invoice.status == InvoiceStatus.OPEN) & (Invoice.due_date < today),
            ),
        )
        .order_by(Invoice.due_date, Invoice.invoice_number)
        .limit(MAX_CANDIDATES)
    )
    return list(session.scalars(stmt))
