import uuid
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from payops import clock
from payops.db.models import Customer, Invoice, PaymentHistory, PaymentPlan
from payops.db.models.enums import InvoiceStatus, PaymentEventType, PaymentPlanStatus
from payops.errors import NotFoundError
from payops.services.audit import audited

MAX_EVENTS = 20


class GetPaymentHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    months: int = Field(default=12, ge=1, le=36, description="Look-back window (30-day months)")


class PaymentPlanOut(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID | None
    total_amount: Decimal
    installment_amount: Decimal
    installment_count: int
    installments_paid: int
    start_date: date


class HistoryEventOut(BaseModel):
    event_type: PaymentEventType
    occurred_at: datetime
    invoice_id: uuid.UUID | None
    amount: Decimal | None
    details: dict


class HistorySummary(BaseModel):
    invoices_paid: int
    on_time_rate: float | None = Field(description="Share of paid invoices settled by due date")
    avg_days_late: float | None = Field(description="Mean days late across paid invoices")
    partial_payment_count: int = Field(description="Payments smaller than the invoice amount")
    total_paid: Decimal


class PaymentHistoryOut(HistorySummary):
    customer_id: uuid.UUID
    months: int
    active_plan: PaymentPlanOut | None
    recent_events: list[HistoryEventOut]


def summarize(invoices: Iterable[Invoice], since: datetime) -> HistorySummary:
    """Pure computation over invoices with their payments loaded.

    Only payments made on/after ``since`` count. An invoice counts as paid in the window
    when it is fully paid and its final payment falls in the window; days late compares
    the final payment date with the due date.
    """
    paid_count = 0
    days_late: list[int] = []
    partial = 0
    total_paid = Decimal("0.00")
    for inv in invoices:
        in_window = [p for p in inv.payments if p.paid_at >= since]
        total_paid += sum((p.amount for p in in_window), Decimal("0.00"))
        partial += sum(1 for p in in_window if p.amount < inv.amount)
        if inv.status is InvoiceStatus.PAID and inv.payments:
            last = max(p.paid_at for p in inv.payments)
            if last >= since:
                paid_count += 1
                days_late.append(max(0, (last.date() - inv.due_date).days))
    return HistorySummary(
        invoices_paid=paid_count,
        on_time_rate=round(sum(d == 0 for d in days_late) / paid_count, 3) if paid_count else None,
        avg_days_late=round(sum(days_late) / paid_count, 1) if paid_count else None,
        partial_payment_count=partial,
        total_paid=total_paid,
    )


@audited(name="get_payment_history")
def get_payment_history(session: Session, inp: GetPaymentHistoryInput) -> PaymentHistoryOut:
    """Summarize how a customer has paid: on-time rate, average days late, partial
    payments, any active payment plan, and recent events (promises, disputes, plans)."""
    if session.get(Customer, inp.customer_id) is None:
        raise NotFoundError(f"Customer {inp.customer_id} not found")
    since = clock.now() - timedelta(days=30 * inp.months)

    invoices = session.scalars(
        select(Invoice)
        .where(Invoice.customer_id == inp.customer_id)
        .options(selectinload(Invoice.payments))
    ).all()
    summary = summarize(invoices, since)

    plan = session.scalars(
        select(PaymentPlan)
        .where(
            PaymentPlan.customer_id == inp.customer_id,
            PaymentPlan.status == PaymentPlanStatus.ACTIVE,
        )
        .order_by(PaymentPlan.start_date.desc())
        .limit(1)
    ).first()
    events = session.scalars(
        select(PaymentHistory)
        .where(PaymentHistory.customer_id == inp.customer_id, PaymentHistory.occurred_at >= since)
        .order_by(PaymentHistory.occurred_at.desc(), PaymentHistory.id)
        .limit(MAX_EVENTS)
    ).all()

    return PaymentHistoryOut(
        customer_id=inp.customer_id,
        months=inp.months,
        **summary.model_dump(),
        active_plan=PaymentPlanOut.model_validate(plan, from_attributes=True) if plan else None,
        recent_events=[HistoryEventOut.model_validate(e, from_attributes=True) for e in events],
    )
