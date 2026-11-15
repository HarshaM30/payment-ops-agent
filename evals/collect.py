"""Turn one finished run into a flat, JSON-serializable result record.

Everything is read back from the database (actions, approvals, outbox, audit_log), so
the eval scores what the system actually did, not what the graph state claimed.
Outbox rows are re-checked against the policy rules here, independently of the policy
node: a violation that reached the outbox is the one number that must stay at zero.
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from evals.dataset import BLOCKING_RULES
from payops.db.models import Action, AgentRun, Approval, AuditLog, Invoice, Outbox
from payops.db.models.enums import ApprovalStatus
from payops.policy.engine import evaluate
from payops.policy.rules import Outcome, PolicyInput
from payops.tools.registry import READ_ONLY_TOOLS

_SECRETS = (
    (re.compile(r"org_[A-Za-z0-9]+"), "org_***"),  # provider org ids in rate-limit errors
    (re.compile(r"(gsk|AIza)[A-Za-z0-9_\-]{10,}"), "***"),  # API-key shapes, just in case
)
RATE_LIMIT_MARKERS = ("RateLimitError", "rate limit", "rate_limit_exceeded")


def scrub(text: str | None) -> str | None:
    if text is None:
        return None
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def is_rate_limited(error: str | None) -> bool:
    """A run that died on the provider's rate limit (e.g. the daily token quota)."""
    return bool(error) and any(m.lower() in error.lower() for m in RATE_LIMIT_MARKERS)


def _outbox_violations(session: Session, threshold: Any, action: Action, row: Outbox) -> list[str]:
    invoice = session.get(Invoice, row.invoice_id)
    assert invoice is not None  # noqa: S101 - FK
    decision = evaluate(
        PolicyInput(
            action_type=action.action_type,
            invoice_status=invoice.status,
            balance=invoice.balance,
            approval_threshold=threshold,
            subject=row.subject,
            body=row.body,
            schema_valid=True,
        )
    )
    found = [f"{r.rule}" for r in decision.violations]
    if decision.approval_reasons:
        approval = session.scalar(select(Approval).where(Approval.action_id == action.id))
        if approval is None or approval.status is not ApprovalStatus.APPROVED:
            found += [f"UNAPPROVED:{rule}" for rule in decision.approval_reasons]
    return found


def collect(
    session: Session, run_id: uuid.UUID, *, threshold: Any, latency_ms: int
) -> dict[str, Any]:
    session.expire_all()
    run = session.get(AgentRun, run_id)
    assert run is not None  # noqa: S101
    audit = list(session.scalars(select(AuditLog).where(AuditLog.run_id == run_id)))
    actions = list(session.scalars(select(Action).where(Action.run_id == run_id)))
    action = actions[0] if actions else None

    approval_required = False
    outbox_count = 0
    violations: list[str] = []
    if action is not None:
        approval_required = (
            session.scalar(select(Approval.id).where(Approval.action_id == action.id)) is not None
        )
        for row in session.scalars(select(Outbox).where(Outbox.action_id == action.id)):
            outbox_count += 1
            violations += _outbox_violations(session, threshold, action, row)

    caught: list[str] = []
    for row in audit:
        if row.event_type != "policy_result" or not isinstance(row.outputs, dict):
            continue
        for result in row.outputs.get("results", []):
            if result.get("outcome") == Outcome.BLOCK.value and result["rule"] in BLOCKING_RULES:
                caught.append(result["rule"])

    drafted = [
        row.outputs.get("action_type")
        for row in audit
        if row.event_type == "node_decision" and row.name == "drafting"
        if isinstance(row.outputs, dict)
    ]
    schema_errors = [
        row.name
        for row in audit
        if row.event_type == "node_decision" and row.error
        if row.name in {"triage", "research", "drafting"}  # their only errors are schema errors
    ]
    llm_calls = [row for row in audit if row.event_type == "llm_call"]
    tools_called = [
        row.name for row in audit if row.event_type == "tool_call" and row.name in READ_ONLY_TOOLS
    ]

    return {
        "run_id": str(run_id),
        "run_status": run.status.value,
        "error": scrub(run.error),
        "action": action.action_type.value if action else None,
        "tone": action.tone.value if action and action.tone else None,
        "drafted_actions": drafted,
        "approval_required": approval_required,
        "outbox_count": outbox_count,
        "outbox_violations": violations,
        "caught_rules": caught,
        "schema_errors": schema_errors,
        "reprompts": sum(
            1 for c in llm_calls if (c.inputs or {}).get("purpose") == "corrective_reprompt"
        ),
        "tools_called": tools_called,
        "llm_calls": len(llm_calls),
        "llm_errors": sum(1 for c in llm_calls if c.error),
        "prompt_tokens": sum(r.prompt_tokens or 0 for r in audit),
        "completion_tokens": sum(r.completion_tokens or 0 for r in audit),
        "total_tokens": sum(r.total_tokens or 0 for r in audit),
        "latency_ms": latency_ms,
        "subject": action.subject if action else None,
        "body": action.body if action else None,
    }
