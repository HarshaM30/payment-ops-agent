"""Deterministic triage features and how the LLM's score is folded in.

The base score (0-100) comes only from facts: days overdue (up to 50 points), balance
(up to 30, log scale), and payment history (up to 20). The LLM may move an invoice by
at most ``MAX_LLM_ADJUSTMENT`` points; the final score is clamped to 0-100. So the
model can reorder close calls, but it cannot bury a 90-day, 50k invoice.
"""

import math
from decimal import Decimal

from payops.agents.facts import InvoiceFacts

MAX_LLM_ADJUSTMENT = 20
_BALANCE_CAP = 50_000


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return int(max(low, min(high, round(value))))


def base_score(f: InvoiceFacts) -> int:
    overdue = min(max(f.days_overdue, 0), 90) / 90 * 50
    balance = max(f.balance, Decimal(0))
    amount = math.log10(1 + min(float(balance), _BALANCE_CAP)) / math.log10(1 + _BALANCE_CAP) * 30
    late_share = 1 - f.on_time_rate if f.on_time_rate is not None else 0.5
    history = min(20.0, late_share * 10 + f.broken_promises * 5 + min(f.partial_payment_count, 2))
    if f.active_plan:
        history = max(0.0, history - 10)
    return clamp(overdue + amount + history)


def final_score(base: int, llm_score: int | None) -> int:
    if llm_score is None:
        return base
    adjusted = clamp(llm_score, base - MAX_LLM_ADJUSTMENT, base + MAX_LLM_ADJUSTMENT)
    return clamp(adjusted)
