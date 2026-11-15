"""Deterministic fake LLM for tests, CI, load tests, and eval smoke runs.

Responses are chosen in this order:

1. A scripted response for ``(node, key)``, where ``key`` is the invoice number in the
   request context (the "scenario"), then ``(node, "*")``. Each key holds a queue; the
   last item repeats once the queue is down to one.
2. A built-in heuristic per node that behaves like a sensible collections agent.

A scripted item may be: a ``str`` (raw content), a ``dict`` (sent as JSON content), a
``FakeToolCalls`` (tool calls), an ``Exception`` instance (raised, e.g. to simulate a
429), or a callable ``(LLMRequest) -> item``.
"""

import json
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from payops.llm.client import Completion, LLMRequest, ToolCall
from payops.llm.throttle import estimate_tokens
from payops.services.audit import TokenUsage

ScriptItem = Any


@dataclass(frozen=True)
class FakeToolCalls:
    calls: list[tuple[str, dict[str, Any]]]


@dataclass
class FakeLLM:
    script: dict[tuple[str, str], Iterable[ScriptItem]] = field(default_factory=dict)
    requests: list[LLMRequest] = field(default_factory=list)
    latency_seconds: float = 0.0  # simulated provider latency (FAKE_LLM_LATENCY_MS)

    def __post_init__(self) -> None:
        self._queues: dict[tuple[str, str], deque[ScriptItem]] = defaultdict(deque)
        for key, items in self.script.items():
            self._queues[key].extend(items)
        self._call_ids = 0

    def add(self, node: str, key: str, *items: ScriptItem) -> "FakeLLM":
        self._queues[(node, key)].extend(items)
        return self

    def calls_for(self, node: str) -> list[LLMRequest]:
        return [r for r in self.requests if r.node == node]

    # ------------------------------------------------------------------ Backend protocol

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion:
        self.requests.append(request)
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        item = self._scripted(request)
        if item is None:
            item = _HEURISTICS[request.node](request)
        while callable(item) and not isinstance(item, FakeToolCalls):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        return self._completion(request, item)

    def _scripted(self, request: LLMRequest) -> ScriptItem | None:
        key = str(request.context.get("invoice_number", ""))
        for k in ((request.node, key), (request.node, "*")):
            queue = self._queues.get(k)
            if queue:
                return queue.popleft() if len(queue) > 1 else queue[0]
        return None

    def _completion(self, request: LLMRequest, item: ScriptItem) -> Completion:
        prompt_tokens = estimate_tokens(json.dumps(request.messages, default=str))
        if isinstance(item, FakeToolCalls):
            calls = []
            for name, args in item.calls:
                self._call_ids += 1
                calls.append(ToolCall(f"call_{self._call_ids}", name, json.dumps(args)))
            usage = TokenUsage(prompt_tokens, 20 * len(calls))
            return Completion(content=None, tool_calls=calls, usage=usage)
        content = item if isinstance(item, str) else json.dumps(item, default=str)
        usage = TokenUsage(prompt_tokens, estimate_tokens(content))
        return Completion(content=content, usage=usage)


# -------------------------------------------------------------------------- heuristics


def _triage(request: LLMRequest) -> dict[str, Any]:
    return {
        "priorities": [
            {
                "invoice_id": inv["invoice_id"],
                "priority_score": inv["base_score"],
                "rationale": f"{inv['days_overdue']} days overdue, balance {inv['balance']}",
            }
            for inv in request.context.get("invoices", [])
        ]
    }


_RESEARCH_ORDER = ("get_customer", "get_invoices", "get_payment_history")


def _research(request: LLMRequest) -> ScriptItem:
    done = {
        call["function"]["name"]
        for m in request.messages
        if m.get("role") == "assistant"
        for call in m.get("tool_calls") or []
    }
    customer_id = request.context["customer_id"]
    for name in _RESEARCH_ORDER:
        if name not in done:
            return FakeToolCalls([(name, {"customer_id": customer_id})])
    return "Research complete: reviewed the customer profile, invoices, and payment history."


def choose_action(facts: dict[str, Any]) -> tuple[str, str]:
    """The fake agent's decision rule: (action_type, tone)."""
    days = int(facts.get("days_overdue") or 0)
    on_time = facts.get("on_time_rate")
    if facts.get("status") == "disputed":
        return "escalate_to_human", "neutral"
    if float(facts.get("balance") or 0) <= 0 or facts.get("status") == "paid":
        return "no_action", "neutral"
    if facts.get("active_plan"):
        return "no_action", "neutral"
    if int(facts.get("partial_payment_count") or 0) > 0:
        return "payment_plan_offer", "friendly"
    if int(facts.get("broken_promises") or 0) > 0 or (
        on_time is not None and on_time < 0.5 and days >= 30
    ):
        return "payment_plan_offer", "firm"
    if days <= 0:
        return "no_action", "neutral"
    return "reminder", "friendly" if days <= 14 else "neutral" if days <= 45 else "firm"


def _drafting(request: LLMRequest) -> dict[str, Any]:
    facts = request.context.get("facts", {})
    action, tone = choose_action(facts)
    number = facts.get("invoice_number", "your invoice")
    name = facts.get("customer_name", "there")
    balance = f"{facts.get('currency', 'USD')} {facts.get('balance', '')}"
    days = facts.get("days_overdue", 0)
    if action == "reminder":
        subject = f"Reminder: invoice {number} is {days} days past due"
        body = (
            f"Hi {name},\n\nThis is a reminder that invoice {number} has an outstanding "
            f"balance of {balance}, which was due {days} days ago. Please arrange payment "
            "at your earliest convenience, or reply if you have any questions.\n\nThank you."
        )
    elif action == "payment_plan_offer":
        subject = f"Options for paying invoice {number}"
        body = (
            f"Hi {name},\n\nInvoice {number} has an outstanding balance of {balance}. If "
            "paying it in full right now is difficult, we can set up a payment plan with "
            "monthly installments. Reply to this email and we will work out a schedule."
            "\n\nThank you."
        )
    elif action == "escalate_to_human":
        subject = f"Escalation: invoice {number} needs human review"
        body = "The invoice is disputed. A person should review the dispute before any contact."
    else:
        subject = f"No action for invoice {number}"
        body = "No outreach is needed right now."
    return {
        "action_type": action,
        "tone": tone,
        "subject": subject,
        "body": body,
        "rationale": f"Fake heuristic: status={facts.get('status')}, days_overdue={days}",
    }


_HEURISTICS: dict[str, Callable[[LLMRequest], ScriptItem]] = {
    "triage": _triage,
    "research": _research,
    "drafting": _drafting,
}
