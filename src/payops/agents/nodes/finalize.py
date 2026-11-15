"""Finalize: write the action. Auto-approved actions execute now; others wait for a human."""

import time

from payops.agents.facts import load_facts
from payops.agents.nodes.policy_node import policy_input
from payops.agents.state import AgentContext, AgentState, current_invoice
from payops.db.models import Action, Approval
from payops.db.models.enums import ActionStatus, ActionType
from payops.policy.engine import evaluate
from payops.policy.rules import Outcome
from payops.services.outbox import execute_action
from payops.tools.actions import draft_message


class PolicyBypassError(RuntimeError):
    """Defense in depth: finalize was asked to execute something the policy blocks."""


def finalize(state: AgentState, ctx: AgentContext) -> AgentState:
    started = time.perf_counter()
    invoice_id = current_invoice(state)
    final = state["final"]
    assert final is not None  # noqa: S101 - routed here only with a final action

    out = draft_message(
        ctx.session,
        {
            "run_id": ctx.run_id,
            "invoice_id": invoice_id,
            "action_type": final["action_type"],
            "tone": final["tone"],
            "subject": final["subject"],
            "body": final["body"],
            "context": (final.get("context") or "")[:2000] or None,
        },
        run_id=ctx.run_id,
        actor="agent",
    )
    action = ctx.session.get(Action, out.action_id)
    assert action is not None  # noqa: S101
    action.payload = {
        **action.payload,
        "priority": state.get("scores", {}).get(str(invoice_id)),
        "policy": final["policy"],
        "blocked_rules": final["blocked_rules"],
        "blocked_draft": final["blocked_draft"],
        "revisions": state.get("revisions", 0),
    }

    reasons = list(final["approval_reasons"])
    approval_id = None
    outbox_id = None
    if reasons:
        approval = Approval(action_id=action.id, reasons=reasons)
        ctx.session.add(approval)
        ctx.session.flush()
        approval_id = approval.id
        result = "awaiting_approval"
    else:
        # Re-check the exact text against current invoice state right before sending.
        facts = load_facts(ctx.session, [invoice_id])[invoice_id]
        check = evaluate(
            policy_input(
                facts,
                threshold=ctx.settings.approval_threshold_amount,
                action_type=ActionType(action.action_type),
                subject=action.subject,
                body=action.body,
                schema_valid=True,
            )
        )
        if check.outcome is not Outcome.ALLOW:
            raise PolicyBypassError(
                f"Refusing to auto-execute action {action.id}: policy says {check.outcome}"
            )
        action.status = ActionStatus.APPROVED
        row = execute_action(ctx.session, action)
        outbox_id = row.id if row else None
        result = "executed"

    outcome = {
        "invoice_id": str(invoice_id),
        "action_id": str(action.id),
        "action_type": action.action_type.value,
        "result": result,
        "approval_id": str(approval_id) if approval_id else None,
        "outbox_id": str(outbox_id) if outbox_id else None,
        "approval_reasons": reasons,
        "blocked_rules": final["blocked_rules"],
    }
    ctx.record_decision(
        "finalize", inputs={"invoice_id": invoice_id}, outputs=outcome, started=started
    )
    ctx.session.commit()  # each invoice's result survives a later failure in the run
    return {"outcomes": [*state.get("outcomes", []), outcome], "index": state["index"] + 1}


def route_next_invoice(state: AgentState) -> str:
    return "research" if state["index"] < len(state["queue"]) else "end"
