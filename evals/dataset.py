"""Eval dataset schema and loader.

Each line of ``evals/datasets/<version>/scenarios.jsonl`` is one ``Scenario``. Fixture
dates are offsets from "today" (``due_days_ago`` etc.), so a scenario means the same
thing whenever it runs. See ``evals/datasets/v1/README.md`` for the field reference.
"""

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from payops.db.models.enums import (
    InvoiceStatus,
    PaymentEventType,
    PaymentPlanStatus,
    ScenarioType,
)
from payops.schemas.llm import DraftAction
from payops.tools.registry import READ_ONLY_TOOLS

DATASETS_DIR = Path(__file__).parent / "datasets"
SCENARIOS_FILE = "scenarios.jsonl"

Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Ref = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,4}$")]


# Rules whose outcome is BLOCK (the other rules only ask for approval). A unit test
# checks these names against payops.policy.rules.RULES.
BLOCKING_RULES = frozenset({"SCHEMA_VALID", "NO_REMINDER_ON_DISPUTED", "NO_DISCOUNT_PROMISES"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PaymentFixture(_Strict):
    amount: Money
    days_ago: int = Field(ge=0, le=720)
    method: Literal["ach", "card", "wire", "check"] = "ach"


class InvoiceFixture(_Strict):
    ref: Ref
    amount: Money
    status: InvoiceStatus
    due_days_ago: int = Field(ge=-90, le=720, description="Negative: due in the future")
    terms_days: int = Field(default=30, ge=0, le=120)
    description: str | None = Field(default=None, max_length=500)
    payments: list[PaymentFixture] = Field(default_factory=list)

    @property
    def amount_paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal("0.00"))

    def due_date(self, today: date) -> date:
        return today - timedelta(days=self.due_days_ago)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.amount_paid > self.amount:
            raise ValueError(f"invoice {self.ref}: payments exceed the amount")
        fully_paid = self.amount_paid == self.amount
        if (self.status is InvoiceStatus.PAID) != fully_paid:
            raise ValueError(f"invoice {self.ref}: status 'paid' iff payments cover the amount")
        if self.status is InvoiceStatus.OVERDUE and self.due_days_ago <= 0:
            raise ValueError(f"invoice {self.ref}: 'overdue' needs a past due date")
        return self


class EventFixture(_Strict):
    event_type: PaymentEventType
    days_ago: int = Field(ge=0, le=720)
    invoice_ref: Ref | None = None
    amount: Money | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PlanFixture(_Strict):
    invoice_ref: Ref
    installment_count: int = Field(ge=1, le=36)
    installments_paid: int = Field(ge=0)
    start_days_ago: int = Field(ge=0, le=720)
    status: PaymentPlanStatus = PaymentPlanStatus.ACTIVE

    @model_validator(mode="after")
    def _paid_range(self) -> Self:
        if self.installments_paid > self.installment_count:
            raise ValueError("installments_paid exceeds installment_count")
        return self


class CustomerFixture(_Strict):
    name: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)


class Fixture(_Strict):
    customer: CustomerFixture
    invoices: list[InvoiceFixture] = Field(min_length=1, max_length=50)
    history: list[EventFixture] = Field(default_factory=list)
    plans: list[PlanFixture] = Field(default_factory=list)
    targets: list[Ref] = Field(min_length=1, max_length=1, description="Invoice refs to run")

    @model_validator(mode="after")
    def _refs(self) -> Self:
        refs = [i.ref for i in self.invoices]
        if len(refs) != len(set(refs)):
            raise ValueError("invoice refs must be unique")
        used = [*self.targets, *(e.invoice_ref for e in self.history if e.invoice_ref)]
        used += [p.invoice_ref for p in self.plans]
        unknown = sorted(set(used) - set(refs))
        if unknown:
            raise ValueError(f"unknown invoice refs: {unknown}")
        return self

    def invoice(self, ref: str) -> InvoiceFixture:
        return next(i for i in self.invoices if i.ref == ref)


class Scenario(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z]{2,4}-\d{2}$")]
    scenario_type: ScenarioType
    description: str = Field(min_length=10, max_length=500)
    tags: list[str] = Field(default_factory=list)
    fixture: Fixture
    expected_action: DraftAction
    acceptable_actions: list[DraftAction] = Field(min_length=1)
    requires_approval: bool
    expected_tools: list[str]
    forbidden_policies: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expected_action not in self.acceptable_actions:
            raise ValueError("expected_action must be one of acceptable_actions")
        bad_tools = sorted(set(self.expected_tools) - READ_ONLY_TOOLS)
        if bad_tools:
            raise ValueError(f"expected_tools must be read-only tools, got {bad_tools}")
        bad_rules = sorted(set(self.forbidden_policies) - BLOCKING_RULES)
        if bad_rules:
            raise ValueError(f"forbidden_policies must be blocking rules, got {bad_rules}")
        return self

    @property
    def invoice_numbers(self) -> list[str]:
        return [invoice_number(self.id, ref) for ref in self.fixture.targets]


def invoice_number(scenario_id: str, ref: str) -> str:
    return f"EV-{scenario_id.upper()}-{ref}"


class Dataset(BaseModel):
    version: str
    path: Path
    sha256: str
    scenarios: list[Scenario]

    def by_id(self) -> dict[str, Scenario]:
        return {s.id: s for s in self.scenarios}


def load_dataset(version: str, base: Path = DATASETS_DIR) -> Dataset:
    path = base / version / SCENARIOS_FILE
    raw = path.read_bytes()
    scenarios: list[Scenario] = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scenarios.append(Scenario.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
    ids = [s.id for s in scenarios]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{path}: duplicate scenario ids {dupes}")
    return Dataset(
        version=version, path=path, sha256=hashlib.sha256(raw).hexdigest(), scenarios=scenarios
    )
