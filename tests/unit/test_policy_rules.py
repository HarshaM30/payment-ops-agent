"""Table-driven tests: every guardrail has allow and block cases."""

from decimal import Decimal

import pytest

from payops.db.models.enums import ActionType, InvoiceStatus
from payops.policy.engine import evaluate
from payops.policy.rules import (
    Outcome,
    PolicyInput,
    approval_for_payment_plan,
    approval_over_threshold,
    find_discount_promises,
    no_discount_promises,
    no_reminder_on_disputed,
    schema_valid,
)

THRESHOLD = Decimal("10000")
R, P, E, N = (
    ActionType.REMINDER,
    ActionType.PAYMENT_PLAN_OFFER,
    ActionType.ESCALATE_TO_HUMAN,
    ActionType.NO_ACTION,
)
OVERDUE, DISPUTED = InvoiceStatus.OVERDUE, InvoiceStatus.DISPUTED


def inp(
    action: ActionType | None = R,
    status: InvoiceStatus = OVERDUE,
    balance: str = "500.00",
    body: str = "Please pay invoice INV-1.",
    subject: str = "Reminder",
    valid: bool = True,
) -> PolicyInput:
    return PolicyInput(
        action_type=action,
        invoice_status=status,
        balance=Decimal(balance),
        approval_threshold=THRESHOLD,
        subject=subject,
        body=body,
        schema_valid=valid,
    )


CASES = [
    # (id, rule, input, expected outcome)
    ("schema ok", schema_valid, inp(), Outcome.ALLOW),
    ("schema invalid", schema_valid, inp(valid=False), Outcome.BLOCK),
    ("schema missing draft", schema_valid, inp(action=None), Outcome.BLOCK),
    ("disputed reminder", no_reminder_on_disputed, inp(R, DISPUTED), Outcome.BLOCK),
    ("disputed plan offer", no_reminder_on_disputed, inp(P, DISPUTED), Outcome.BLOCK),
    ("disputed escalate", no_reminder_on_disputed, inp(E, DISPUTED), Outcome.ALLOW),
    ("disputed no action", no_reminder_on_disputed, inp(N, DISPUTED), Outcome.ALLOW),
    ("overdue reminder", no_reminder_on_disputed, inp(R, OVERDUE), Outcome.ALLOW),
    ("over threshold", approval_over_threshold, inp(balance="10000.01"), Outcome.NEEDS_APPROVAL),
    ("at threshold", approval_over_threshold, inp(balance="10000.00"), Outcome.ALLOW),
    ("under threshold", approval_over_threshold, inp(balance="9999.99"), Outcome.ALLOW),
    ("big escalate", approval_over_threshold, inp(E, balance="50000"), Outcome.ALLOW),
    ("plan offer", approval_for_payment_plan, inp(P), Outcome.NEEDS_APPROVAL),
    ("reminder no plan", approval_for_payment_plan, inp(R), Outcome.ALLOW),
    ("clean text", no_discount_promises, inp(), Outcome.ALLOW),
    ("10% discount", no_discount_promises, inp(body="We offer a 10% discount."), Outcome.BLOCK),
    ("discount in subject", no_discount_promises, inp(subject="Discount inside"), Outcome.BLOCK),
    ("discount in plan", no_discount_promises, inp(P, body="a discount if paid"), Outcome.BLOCK),
    ("note may mention", no_discount_promises, inp(E, body="asked for discount"), Outcome.ALLOW),
]


@pytest.mark.parametrize(
    ("rule", "policy_input", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_rule(rule, policy_input: PolicyInput, expected: Outcome) -> None:
    result = rule(policy_input)
    assert result.outcome is expected
    assert result.message
    assert result.passed is (expected is Outcome.ALLOW)


DISCOUNT_TEXTS = [
    ("We can give you a 10% discount", True),
    ("take 15 percent off if you pay today", True),
    ("5 pct off for early payment", True),
    ("we will waive the late fee", True),
    ("a fee waiver is available", True),
    ("we are happy to forgive the remaining amount", True),
    ("we'll knock $200 off the balance", True),
    ("we can write off the rest", True),
    ("we could reduce the balance for you", True),
    ("we will credit your account", True),
    ("a rebate applies", True),
    ("we can cancel the debt", True),
    ("DISCOUNTED pricing", True),
    ("Please pay the full balance of USD 1,200.00 by Friday.", False),
    ("Invoice INV-00042 is 10 days overdue.", False),
    ("We can split the balance into 3 monthly installments.", False),
    ("Thank you for your business. Accounts Receivable", False),
]


@pytest.mark.parametrize(("text", "flagged"), DISCOUNT_TEXTS)
def test_discount_detection(text: str, flagged: bool) -> None:
    assert bool(find_discount_promises(text)) is flagged


ENGINE_CASES = [
    ("small reminder", inp(), Outcome.ALLOW, []),
    ("large reminder", inp(balance="20000"), Outcome.NEEDS_APPROVAL, ["APPROVAL_OVER_THRESHOLD"]),
    (
        "large plan",
        inp(P, balance="20000"),
        Outcome.NEEDS_APPROVAL,
        ["APPROVAL_OVER_THRESHOLD", "APPROVAL_FOR_PAYMENT_PLAN"],
    ),
    ("block beats approval", inp(R, DISPUTED, balance="20000"), Outcome.BLOCK, []),
    ("escalation", inp(E, DISPUTED, balance="20000"), Outcome.ALLOW, []),
]


@pytest.mark.parametrize(
    ("policy_input", "expected", "reasons"),
    [c[1:] for c in ENGINE_CASES],
    ids=[c[0] for c in ENGINE_CASES],
)
def test_engine_combines_results(
    policy_input: PolicyInput, expected: Outcome, reasons: list[str]
) -> None:
    decision = evaluate(policy_input)
    assert decision.outcome is expected
    assert len(decision.results) == 5  # every rule always runs and is reported
    if expected is not Outcome.BLOCK:
        assert decision.approval_reasons == reasons


def test_engine_reports_violations() -> None:
    decision = evaluate(inp(R, DISPUTED, body="10% discount"))
    assert decision.violated_rules == ["NO_REMINDER_ON_DISPUTED", "NO_DISCOUNT_PROMISES"]
    data = decision.to_json()
    assert data["outcome"] == "block"
    assert {r["rule"] for r in data["results"]} >= {"SCHEMA_VALID", "NO_DISCOUNT_PROMISES"}


def test_rules_are_deterministic() -> None:
    policy_input = inp(P, balance="12000", body="pay 5% less")
    assert evaluate(policy_input) == evaluate(policy_input)
