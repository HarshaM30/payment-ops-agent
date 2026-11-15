from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payops.db.models import Customer, Invoice, Payment, PaymentHistory, PaymentPlan
from payops.db.models.enums import InvoiceStatus, PaymentPlanStatus, ScenarioType
from scripts.seed import MIN_PER_SCENARIO, seed

TODAY = date(2026, 11, 15)


def _snapshot(session: Session) -> list[tuple]:
    rows = session.execute(
        select(
            Invoice.id,
            Invoice.customer_id,
            Invoice.amount,
            Invoice.amount_paid,
            Invoice.status,
            Invoice.due_date,
        ).order_by(Invoice.invoice_number)
    ).all()
    customers = session.execute(
        select(Customer.id, Customer.name, Customer.email, Customer.scenario_type).order_by(
            Customer.email
        )
    ).all()
    return [*customers, *rows]


def test_every_scenario_type_present(db_session: Session) -> None:
    seed(db_session, seed=42, num_customers=60, today=TODAY)
    counts = dict(
        db_session.execute(
            select(Customer.scenario_type, func.count()).group_by(Customer.scenario_type)
        ).all()
    )
    for scenario in ScenarioType:
        if scenario is not ScenarioType.CURRENT:
            assert counts.get(scenario, 0) >= MIN_PER_SCENARIO, scenario
    assert counts[ScenarioType.CURRENT] > 0


def test_seed_is_deterministic_and_idempotent(db_session: Session) -> None:
    seed(db_session, seed=42, num_customers=60, today=TODAY)
    first = _snapshot(db_session)
    seed(db_session, seed=42, num_customers=60, today=TODAY)
    second = _snapshot(db_session)
    assert first == second
    assert db_session.scalar(select(func.count()).select_from(Customer)) == 60


def test_different_seed_gives_different_data(db_session: Session) -> None:
    seed(db_session, seed=42, num_customers=35, today=TODAY)
    first = _snapshot(db_session)
    seed(db_session, seed=7, num_customers=35, today=TODAY)
    assert _snapshot(db_session) != first


def test_scenarios_have_expected_shape(db_session: Session) -> None:
    seed(db_session, seed=42, num_customers=60, today=TODAY)

    def invoices_for(scenario: ScenarioType) -> list[Invoice]:
        return list(
            db_session.scalars(
                select(Invoice)
                .join(Customer)
                .where(Customer.scenario_type == scenario, Invoice.status != InvoiceStatus.PAID)
            )
        )

    assert all(
        i.status is InvoiceStatus.DISPUTED for i in invoices_for(ScenarioType.DISPUTED_INVOICE)
    )
    assert all(i.amount > Decimal("10000") for i in invoices_for(ScenarioType.LARGE_BALANCE))
    assert all(
        Decimal("0") < i.amount_paid < i.amount for i in invoices_for(ScenarioType.PARTIAL_PAYMENTS)
    )
    assert all(i.due_date > TODAY for i in invoices_for(ScenarioType.CURRENT))
    plans = db_session.scalar(
        select(func.count())
        .select_from(PaymentPlan)
        .where(PaymentPlan.status == PaymentPlanStatus.ACTIVE)
    )
    assert plans >= MIN_PER_SCENARIO
    # Every payment has a matching history event; paid invoices are fully paid.
    assert db_session.scalar(select(func.count()).select_from(Payment)) <= db_session.scalar(
        select(func.count()).select_from(PaymentHistory)
    )
    for inv in db_session.scalars(select(Invoice).where(Invoice.status == InvoiceStatus.PAID)):
        assert inv.amount_paid == inv.amount
