"""Small builders for integration tests. Each flushes so ids are usable immediately."""

import itertools
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from payops.db.models import (
    Action,
    AgentRun,
    Approval,
    Customer,
    Invoice,
    Payment,
    PaymentHistory,
    PaymentPlan,
)
from payops.db.models.enums import (
    ActionStatus,
    ActionType,
    InvoiceStatus,
    PaymentEventType,
    PaymentPlanStatus,
    RunStatus,
    ScenarioType,
    Tone,
)

_seq = itertools.count(1)


def customer(s: Session, **kw: object) -> Customer:
    n = next(_seq)
    c = Customer(
        name=kw.pop("name", f"Test Customer {n}"),
        company=kw.pop("company", f"Test Co {n}"),
        email=kw.pop("email", f"test{n}-{uuid.uuid4().hex[:6]}@example.com"),
        scenario_type=kw.pop("scenario_type", ScenarioType.CURRENT),
        **kw,
    )
    s.add(c)
    s.flush()
    return c


def invoice(s: Session, cust: Customer, **kw: object) -> Invoice:
    due = kw.pop("due_date", date.today() - timedelta(days=10))
    inv = Invoice(
        customer_id=cust.id,
        invoice_number=kw.pop("invoice_number", f"T-{uuid.uuid4().hex[:10]}"),
        amount=Decimal(str(kw.pop("amount", "1000.00"))),
        amount_paid=Decimal(str(kw.pop("amount_paid", "0"))),
        currency="USD",
        status=kw.pop("status", InvoiceStatus.OVERDUE),
        issue_date=due - timedelta(days=30),
        due_date=due,
        **kw,
    )
    s.add(inv)
    s.flush()
    return inv


def payment(s: Session, inv: Invoice, amount: str, paid_on: date) -> Payment:
    p = Payment(
        invoice_id=inv.id,
        amount=Decimal(amount),
        paid_at=datetime(paid_on.year, paid_on.month, paid_on.day, 12, tzinfo=UTC),
        method="ach",
    )
    s.add(p)
    s.flush()
    return p


def event(
    s: Session, cust: Customer, event_type: PaymentEventType, when: datetime
) -> PaymentHistory:
    e = PaymentHistory(customer_id=cust.id, event_type=event_type, occurred_at=when, details={})
    s.add(e)
    s.flush()
    return e


def plan(s: Session, cust: Customer, inv: Invoice) -> PaymentPlan:
    p = PaymentPlan(
        customer_id=cust.id,
        invoice_id=inv.id,
        total_amount=inv.amount,
        installment_amount=inv.amount / 4,
        installment_count=4,
        installments_paid=0,
        start_date=date.today() - timedelta(days=3),
        status=PaymentPlanStatus.ACTIVE,
    )
    s.add(p)
    s.flush()
    return p


def run(s: Session) -> AgentRun:
    r = AgentRun(status=RunStatus.QUEUED, scope="all_overdue")
    s.add(r)
    s.flush()
    return r


def action(
    s: Session,
    r: AgentRun,
    inv: Invoice,
    action_type: ActionType = ActionType.REMINDER,
    **kw: object,
) -> Action:
    a = Action(
        run_id=r.id,
        invoice_id=inv.id,
        action_type=action_type,
        status=ActionStatus.PROPOSED,
        tone=kw.pop("tone", Tone.FRIENDLY),
        subject=kw.pop("subject", "Friendly reminder"),
        body=kw.pop("body", "Hi, invoice is overdue. Please pay."),
        payload={},
        **kw,
    )
    s.add(a)
    s.flush()
    return a


def approval(s: Session, a: Action, reasons: list[str] | None = None) -> Approval:
    ap = Approval(action_id=a.id, reasons=reasons or ["APPROVAL_OVER_THRESHOLD"])
    s.add(ap)
    s.flush()
    return ap


def pending_approval(s: Session, **action_kw: object) -> Approval:
    c = customer(s)
    inv = invoice(s, c, amount="25000.00")
    return approval(s, action(s, run(s), inv, **action_kw))
