"""Seed the database with deterministic synthetic customers, invoices, and payments.

Usage:  uv run python -m scripts.seed

- Deterministic: the same SEED_RANDOM_SEED and reference date always produce the same rows,
  including primary keys.
- Idempotent: every table is truncated first, so running it twice gives the same data.
- Each customer is tagged with a ``scenario_type`` that the eval dataset reuses.

All data is fake. Emails use example.com/.org/.net.
"""

import random
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from faker import Faker
from sqlalchemy import text
from sqlalchemy.orm import Session

from payops.db.base import Base
from payops.db.models import Customer, Invoice, Payment, PaymentHistory, PaymentPlan
from payops.db.models.enums import (
    InvoiceStatus,
    PaymentEventType,
    PaymentPlanStatus,
    ScenarioType,
)

MIN_PER_SCENARIO = 5
SCENARIO_TYPES = [s for s in ScenarioType if s is not ScenarioType.CURRENT]
CENTS = Decimal("0.01")

# (min, max) days late for historical, already-paid invoices.
HISTORY_LATENESS = {
    ScenarioType.CURRENT: (-5, 0),
    ScenarioType.RELIABLE_SLIGHTLY_LATE: (-2, 5),
    ScenarioType.CHRONIC_LATE: (15, 60),
    ScenarioType.DISPUTED_INVOICE: (-3, 3),
    ScenarioType.LARGE_BALANCE: (-2, 10),
    ScenarioType.PARTIAL_PAYMENTS: (5, 20),
    ScenarioType.RECENT_PAYMENT_PLAN: (10, 40),
}


@dataclass
class SeedSummary:
    customers: int = 0
    invoices: int = 0
    payments: int = 0
    history_events: int = 0
    payment_plans: int = 0
    by_scenario: Counter[str] = field(default_factory=Counter)


def scenario_counts(num_customers: int) -> dict[ScenarioType, int]:
    """At least MIN_PER_SCENARIO of each scenario type; the remainder are `current` customers."""
    per_type = max(MIN_PER_SCENARIO, num_customers // 7)
    needed = per_type * len(SCENARIO_TYPES)
    if num_customers < needed:
        raise ValueError(
            f"num_customers={num_customers} is too small; need at least "
            f"{MIN_PER_SCENARIO * len(SCENARIO_TYPES)}"
        )
    counts = dict.fromkeys(SCENARIO_TYPES, per_type)
    counts[ScenarioType.CURRENT] = num_customers - needed
    return counts


class _Seeder:
    def __init__(self, session: Session, seed: int, today: date) -> None:
        self.session = session
        self.rng = random.Random(seed)
        self.fake = Faker("en_US")
        self.fake.seed_instance(seed)
        self.today = today
        self.invoice_counter = 0
        self.summary = SeedSummary()

    # -- helpers ---------------------------------------------------------------------------

    def uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def money(self, low: int, high: int) -> Decimal:
        return (Decimal(self.rng.randint(low * 100, high * 100)) / 100).quantize(CENTS)

    def at(self, day: date) -> datetime:
        minutes = self.rng.randint(9 * 60, 17 * 60)
        return datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=UTC)

    def add_invoice(
        self,
        customer: Customer,
        *,
        amount: Decimal,
        due: date,
        status: InvoiceStatus,
        amount_paid: Decimal = Decimal("0"),
    ) -> Invoice:
        self.invoice_counter += 1
        inv = Invoice(
            id=self.uuid(),
            customer_id=customer.id,
            invoice_number=f"INV-{self.invoice_counter:05d}",
            amount=amount,
            amount_paid=amount_paid,
            currency="USD",
            status=status,
            issue_date=due - timedelta(days=30),
            due_date=due,
            description=f"{self.fake.bs().capitalize()} services",
        )
        self.session.add(inv)
        self.summary.invoices += 1
        return inv

    def add_payment(self, customer: Customer, invoice: Invoice, amount: Decimal, day: date) -> None:
        paid_at = self.at(day)
        self.session.add(
            Payment(
                id=self.uuid(),
                invoice_id=invoice.id,
                amount=amount,
                paid_at=paid_at,
                method=self.rng.choice(["ach", "wire", "card", "check"]),
                reference=f"PMT-{self.rng.randint(100000, 999999)}",
            )
        )
        self.add_event(
            customer, PaymentEventType.PAYMENT_RECEIVED, paid_at, invoice=invoice, amount=amount
        )
        self.summary.payments += 1

    def add_event(
        self,
        customer: Customer,
        event_type: PaymentEventType,
        occurred_at: datetime,
        *,
        invoice: Invoice | None = None,
        amount: Decimal | None = None,
        details: dict | None = None,
    ) -> None:
        self.session.add(
            PaymentHistory(
                id=self.uuid(),
                customer_id=customer.id,
                invoice_id=invoice.id if invoice else None,
                event_type=event_type,
                amount=amount,
                occurred_at=occurred_at,
                details=details or {},
            )
        )
        self.summary.history_events += 1

    # -- generation ------------------------------------------------------------------------

    def customer(self, scenario: ScenarioType, index: int) -> Customer:
        name = self.fake.name()
        slug = re.sub(r"[^a-z]+", ".", name.lower()).strip(".")
        tld = self.rng.choice(["com", "org", "net"])
        cust = Customer(
            id=self.uuid(),
            name=name,
            company=self.fake.company(),
            email=f"{slug}.{index}@example.{tld}",
            phone=self.fake.numerify("555-###-####"),
            scenario_type=scenario,
        )
        self.session.add(cust)
        self.session.flush()
        self.summary.customers += 1
        self.summary.by_scenario[scenario.value] += 1
        return cust

    def paid_history(self, cust: Customer, scenario: ScenarioType) -> None:
        """3-6 fully paid invoices spread over the last 12 months."""
        low, high = HISTORY_LATENESS[scenario]
        for _ in range(self.rng.randint(3, 6)):
            due = self.today - timedelta(days=self.rng.randint(45, 360))
            amount = self.money(300, 6000)
            inv = self.add_invoice(
                cust, amount=amount, due=due, status=InvoiceStatus.PAID, amount_paid=amount
            )
            final_day = due + timedelta(days=self.rng.randint(low, high))
            if scenario is ScenarioType.PARTIAL_PAYMENTS:
                first = (amount * Decimal(self.rng.uniform(0.3, 0.6))).quantize(CENTS)
                self.add_payment(cust, inv, first, final_day - timedelta(days=15))
                self.add_payment(cust, inv, amount - first, final_day)
            else:
                self.add_payment(cust, inv, amount, final_day)

    def open_items(self, cust: Customer, scenario: ScenarioType) -> None:
        today, rng = self.today, self.rng
        match scenario:
            case ScenarioType.CURRENT:
                self.add_invoice(
                    cust,
                    amount=self.money(300, 5000),
                    due=today + timedelta(days=rng.randint(5, 25)),
                    status=InvoiceStatus.OPEN,
                )
            case ScenarioType.RELIABLE_SLIGHTLY_LATE:
                self.add_invoice(
                    cust,
                    amount=self.money(500, 5000),
                    due=today - timedelta(days=rng.randint(3, 10)),
                    status=InvoiceStatus.OVERDUE,
                )
            case ScenarioType.CHRONIC_LATE:
                for _ in range(rng.randint(2, 3)):
                    inv = self.add_invoice(
                        cust,
                        amount=self.money(1000, 8000),
                        due=today - timedelta(days=rng.randint(30, 90)),
                        status=InvoiceStatus.OVERDUE,
                    )
                promised = today - timedelta(days=rng.randint(5, 20))
                self.add_event(
                    cust,
                    PaymentEventType.PROMISE_TO_PAY,
                    self.at(promised),
                    invoice=inv,
                    details={"promised_date": str(promised + timedelta(days=7)), "kept": False},
                )
            case ScenarioType.DISPUTED_INVOICE:
                inv = self.add_invoice(
                    cust,
                    amount=self.money(1000, 9000),
                    due=today - timedelta(days=rng.randint(10, 45)),
                    status=InvoiceStatus.DISPUTED,
                )
                self.add_event(
                    cust,
                    PaymentEventType.DISPUTE_OPENED,
                    self.at(inv.due_date + timedelta(days=rng.randint(1, 5))),
                    invoice=inv,
                    details={
                        "reason": rng.choice(
                            [
                                "Services not delivered as agreed",
                                "Billed amount does not match the contract",
                                "Duplicate invoice",
                            ]
                        )
                    },
                )
            case ScenarioType.LARGE_BALANCE:
                self.add_invoice(
                    cust,
                    amount=self.money(12_000, 75_000),
                    due=today - timedelta(days=rng.randint(10, 40)),
                    status=InvoiceStatus.OVERDUE,
                )
            case ScenarioType.PARTIAL_PAYMENTS:
                amount = self.money(2000, 9000)
                paid_parts = [
                    (amount * Decimal(rng.uniform(0.1, 0.3))).quantize(CENTS)
                    for _ in range(rng.randint(1, 2))
                ]
                due = today - timedelta(days=rng.randint(15, 45))
                inv = self.add_invoice(
                    cust,
                    amount=amount,
                    due=due,
                    status=InvoiceStatus.OVERDUE,
                    amount_paid=sum(paid_parts, Decimal("0")),
                )
                for i, part in enumerate(paid_parts):
                    self.add_payment(cust, inv, part, due + timedelta(days=3 + 5 * i))
            case ScenarioType.RECENT_PAYMENT_PLAN:
                amount = self.money(3000, 9500)
                count = rng.randint(3, 6)
                installment = (amount / count).quantize(CENTS)
                start = today - timedelta(days=rng.randint(5, 25))
                paid_installments = 1 if (today - start).days >= 14 else 0
                paid = installment if paid_installments else Decimal("0")
                inv = self.add_invoice(
                    cust,
                    amount=amount,
                    due=today - timedelta(days=rng.randint(30, 60)),
                    status=InvoiceStatus.OVERDUE,
                    amount_paid=paid,
                )
                self.session.add(
                    PaymentPlan(
                        id=self.uuid(),
                        customer_id=cust.id,
                        invoice_id=inv.id,
                        total_amount=amount,
                        installment_amount=installment,
                        installment_count=count,
                        installments_paid=paid_installments,
                        start_date=start,
                        status=PaymentPlanStatus.ACTIVE,
                    )
                )
                self.summary.payment_plans += 1
                self.add_event(
                    cust,
                    PaymentEventType.PLAN_CREATED,
                    self.at(start),
                    invoice=inv,
                    amount=amount,
                    details={"installments": count, "installment_amount": str(installment)},
                )
                if paid_installments:
                    self.add_payment(cust, inv, installment, start + timedelta(days=14))


def truncate_all(session: Session) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def seed(
    session: Session, *, seed: int, num_customers: int, today: date | None = None
) -> SeedSummary:
    """Truncate all tables and insert a fresh deterministic dataset. Caller commits."""
    counts = scenario_counts(num_customers)
    truncate_all(session)
    seeder = _Seeder(session, seed, today or datetime.now(UTC).date())
    index = 0
    for scenario in [ScenarioType.CURRENT, *SCENARIO_TYPES]:
        for _ in range(counts[scenario]):
            index += 1
            cust = seeder.customer(scenario, index)
            seeder.paid_history(cust, scenario)
            seeder.open_items(cust, scenario)
    session.flush()
    return seeder.summary


def main() -> None:
    from payops.config import get_settings
    from payops.db.session import get_sessionmaker

    settings = get_settings()
    with get_sessionmaker()() as session:
        summary = seed(
            session, seed=settings.seed_random_seed, num_customers=settings.seed_num_customers
        )
        session.commit()
    print(
        f"Seeded {summary.customers} customers, {summary.invoices} invoices, "
        f"{summary.payments} payments, {summary.history_events} history events, "
        f"{summary.payment_plans} payment plans"
    )
    for scenario, n in sorted(summary.by_scenario.items()):
        print(f"  {scenario:<24} {n}")


if __name__ == "__main__":
    main()
