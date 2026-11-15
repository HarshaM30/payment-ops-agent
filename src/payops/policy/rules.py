"""Guardrails. Each rule is a pure function ``PolicyInput -> PolicyResult``: no I/O,
no LLM, no clock. The same input always gives the same result.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from payops.db.models.enums import MESSAGE_ACTION_TYPES, ActionType, InvoiceStatus


class Outcome(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class PolicyInput:
    action_type: ActionType | None  # None when the draft never passed schema validation
    invoice_status: InvoiceStatus
    balance: Decimal
    approval_threshold: Decimal
    subject: str | None = None
    body: str | None = None
    schema_valid: bool = True


@dataclass(frozen=True)
class PolicyResult:
    rule: str
    outcome: Outcome
    message: str

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.ALLOW


Rule = Callable[[PolicyInput], PolicyResult]


def _is_message(inp: PolicyInput) -> bool:
    return inp.action_type in MESSAGE_ACTION_TYPES


def schema_valid(inp: PolicyInput) -> PolicyResult:
    rule = "SCHEMA_VALID"
    if not inp.schema_valid or inp.action_type is None:
        return PolicyResult(rule, Outcome.BLOCK, "LLM output failed schema validation")
    return PolicyResult(rule, Outcome.ALLOW, "All LLM outputs passed schema validation")


def no_reminder_on_disputed(inp: PolicyInput) -> PolicyResult:
    """No collection message (reminder or plan offer) on a disputed invoice."""
    rule = "NO_REMINDER_ON_DISPUTED"
    if inp.invoice_status is InvoiceStatus.DISPUTED and _is_message(inp):
        return PolicyResult(
            rule,
            Outcome.BLOCK,
            f"Invoice is disputed; a {inp.action_type} must not be sent. Escalate to a human.",
        )
    return PolicyResult(rule, Outcome.ALLOW, "No collection message on a disputed invoice")


def approval_over_threshold(inp: PolicyInput) -> PolicyResult:
    rule = "APPROVAL_OVER_THRESHOLD"
    if _is_message(inp) and inp.balance > inp.approval_threshold:
        return PolicyResult(
            rule,
            Outcome.NEEDS_APPROVAL,
            f"Balance {inp.balance} is over the approval threshold {inp.approval_threshold}",
        )
    return PolicyResult(rule, Outcome.ALLOW, "Balance within the auto-approval threshold")


def approval_for_payment_plan(inp: PolicyInput) -> PolicyResult:
    rule = "APPROVAL_FOR_PAYMENT_PLAN"
    if inp.action_type is ActionType.PAYMENT_PLAN_OFFER:
        return PolicyResult(rule, Outcome.NEEDS_APPROVAL, "Payment-plan offers need approval")
    return PolicyResult(rule, Outcome.ALLOW, "Not a payment-plan offer")


# Discount detection is deliberately simple and explainable: a keyword/regex list over
# normalized text. Known limits (paraphrase, other languages, numbers spelled out) are
# documented, and some eval scenarios probe them.
_DISCOUNT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("discount", r"\bdiscount(s|ed|ing)?\b"),
        ("waive", r"\bwaiv(e|ed|er|ers|ing)\b"),
        ("forgive", r"\bforgiv(e|en|eness|ing)\b"),
        ("percent off", r"\d+(\.\d+)?\s*(%|percent|per cent|pct)\s*(off|reduction|less)\b"),
        ("off the balance", r"\boff\s+(the|your)\s+(total|balance|invoice|amount|bill)\b"),
        ("write off", r"\bwrit(e|ing|ten)[\s-]?off\b"),
        ("reduce the amount", r"\b(reduc|lower|cut)\w*\s+(the|your)\s+(balance|amount|total)\b"),
        ("rebate", r"\brebate\b"),
        ("credit", r"\bcredit\s+(you|your\s+account)\b"),
        ("cancel the debt", r"\b(cancel|drop|erase)\w*\s+(the|your)\s+(debt|balance|invoice)\b"),
    )
)


def find_discount_promises(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text)
    return [label for label, pattern in _DISCOUNT_PATTERNS if pattern.search(normalized)]


def no_discount_promises(inp: PolicyInput) -> PolicyResult:
    rule = "NO_DISCOUNT_PROMISES"
    hits = find_discount_promises(f"{inp.subject or ''}\n{inp.body or ''}")
    if hits and _is_message(inp):
        return PolicyResult(
            rule,
            Outcome.BLOCK,
            "Message promises a discount or waiver (matched: "
            + ", ".join(hits)
            + "). Remove any discount, waiver, or reduction of the amount owed.",
        )
    return PolicyResult(rule, Outcome.ALLOW, "No discount or waiver language")


RULES: tuple[Rule, ...] = (
    schema_valid,
    no_reminder_on_disputed,
    no_discount_promises,
    approval_over_threshold,
    approval_for_payment_plan,
)
