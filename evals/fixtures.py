"""Load one scenario's fixture into the (dedicated) eval database."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from evals.dataset import Scenario, invoice_number
from payops.db.base import Base
from payops.db.models import Customer, Invoice, Payment, PaymentHistory, PaymentPlan

CENTS = Decimal("0.01")


def reset(session: Session) -> None:
    """Empty every application table. Only ever called on the eval database."""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session.commit()


def _at(today: date, days_ago: int) -> datetime:
    return datetime.combine(today - timedelta(days=days_ago), time(12), tzinfo=UTC)


def load(session: Session, scenario: Scenario, today: date) -> list[uuid.UUID]:
    """Insert the fixture and return the ids of the invoices the run should handle."""
    fx = scenario.fixture
    customer = Customer(
        name=fx.customer.name,
        company=fx.customer.company,
        email=f"{scenario.id}@example.com",
        scenario_type=scenario.scenario_type,
    )
    session.add(customer)
    session.flush()

    ids: dict[str, uuid.UUID] = {}
    for inv in fx.invoices:
        due = inv.due_date(today)
        row = Invoice(
            customer_id=customer.id,
            invoice_number=invoice_number(scenario.id, inv.ref),
            amount=inv.amount,
            amount_paid=inv.amount_paid,
            currency="USD",
            status=inv.status,
            issue_date=due - timedelta(days=inv.terms_days),
            due_date=due,
            description=inv.description,
        )
        session.add(row)
        session.flush()
        ids[inv.ref] = row.id
        for i, p in enumerate(inv.payments):
            session.add(
                Payment(
                    invoice_id=row.id,
                    amount=p.amount,
                    paid_at=_at(today, p.days_ago),
                    method=p.method,
                    reference=f"{row.invoice_number}-P{i + 1}",
                )
            )

    for ev in fx.history:
        session.add(
            PaymentHistory(
                customer_id=customer.id,
                invoice_id=ids[ev.invoice_ref] if ev.invoice_ref else None,
                event_type=ev.event_type,
                amount=ev.amount,
                occurred_at=_at(today, ev.days_ago),
                details=ev.details,
            )
        )

    for plan in fx.plans:
        inv = fx.invoice(plan.invoice_ref)
        session.add(
            PaymentPlan(
                customer_id=customer.id,
                invoice_id=ids[plan.invoice_ref],
                total_amount=inv.amount,
                installment_amount=(inv.amount / plan.installment_count).quantize(CENTS),
                installment_count=plan.installment_count,
                installments_paid=plan.installments_paid,
                start_date=today - timedelta(days=plan.start_days_ago),
                status=plan.status,
            )
        )
    session.commit()
    return [ids[ref] for ref in fx.targets]
