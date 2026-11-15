"""Policy: deterministic guardrails, no LLM. Decides allow / needs approval / block.

A blocked draft is never sent. A discount promise gets one chance to be redrafted with
feedback; every other block (or a second discount promise) becomes an escalation to a
human, with the blocked draft kept in the action payload for the reviewer.
"""

import time
from typing import Any

from payops.agents.facts import InvoiceFacts, load_facts
from payops.agents.state import AgentContext, AgentState, current_invoice
from payops.db.models.enums import ActionType, Tone
from payops.policy.engine import PolicyDecision, evaluate
from payops.policy.rules import Outcome, PolicyInput
from payops.schemas.llm import DraftOutput

MAX_REVISIONS = 1
REVISABLE_RULES = frozenset({"NO_DISCOUNT_PROMISES"})


def policy_input(
    facts: InvoiceFacts,
    *,
    threshold: Any,
    action_type: ActionType | None,
    subject: str | None,
    body: str | None,
    schema_valid: bool,
) -> PolicyInput:
    return PolicyInput(
        action_type=action_type,
        invoice_status=facts.status,
        balance=facts.balance,
        approval_threshold=threshold,
        subject=subject,
        body=body,
        schema_valid=schema_valid,
    )


def _escalation(
    facts: InvoiceFacts, draft: DraftOutput | None, decision: PolicyDecision
) -> dict[str, Any]:
    rules = decision.violated_rules
    lines = [f"- {r.rule}: {r.message}" for r in decision.violations]
    return {
        "action_type": ActionType.ESCALATE_TO_HUMAN.value,
        "tone": Tone.NEUTRAL.value,
        "subject": f"Escalation: invoice {facts.invoice_number} blocked by policy",
        "body": "The drafted action was blocked by the policy check and was not sent:\n"
        + "\n".join(lines)
        + "\nA person should decide how to follow up.",
        "context": f"Converted to escalate_to_human by policy ({', '.join(rules)})",
        "blocked_rules": rules,
        "blocked_draft": draft.model_dump(mode="json") if draft else None,
    }


def policy(state: AgentState, ctx: AgentContext) -> AgentState:
    started = time.perf_counter()
    invoice_id = current_invoice(state)
    # Re-read the invoice: the guardrails use the current database state, not the
    # facts from earlier in the run, and never anything the model said.
    facts = load_facts(ctx.session, [invoice_id])[invoice_id]
    ctx.facts[invoice_id] = facts
    draft = state.get("draft")
    threshold = ctx.settings.approval_threshold_amount
    decision = evaluate(
        policy_input(
            facts,
            threshold=threshold,
            action_type=ActionType(draft.action_type) if draft else None,
            subject=draft.subject if draft else None,
            body=draft.body if draft else None,
            schema_valid=draft is not None and state.get("research_ok", True),
        )
    )
    revisions = state.get("revisions", 0)
    ctx.record_decision(
        "policy",
        event_type="policy_result",
        inputs={
            "invoice_id": invoice_id,
            "action_type": draft.action_type if draft else None,
            "invoice_status": facts.status.value,
            "balance": facts.balance,
            "revision": revisions,
        },
        outputs=decision.to_json(),
        started=started,
    )

    if decision.outcome is Outcome.BLOCK:
        if set(decision.violated_rules) <= REVISABLE_RULES and revisions < MAX_REVISIONS:
            feedback = " ".join(v.message for v in decision.violations)
            return {"revisions": revisions + 1, "feedback": feedback, "final": None}
        final = _escalation(facts, draft, decision)
        converted = evaluate(
            policy_input(
                facts,
                threshold=threshold,
                action_type=ActionType.ESCALATE_TO_HUMAN,
                subject=final["subject"],
                body=final["body"],
                schema_valid=True,
            )
        )
        ctx.record_decision(
            "policy",
            event_type="policy_result",
            inputs={"invoice_id": invoice_id, "action_type": final["action_type"]},
            outputs={**converted.to_json(), "converted_from": decision.violated_rules},
            started=started,
        )
        final["policy"] = converted.to_json()
        final["approval_reasons"] = converted.approval_reasons
        return {"final": final}

    assert draft is not None  # noqa: S101 - SCHEMA_VALID blocks a missing draft
    return {
        "final": {
            "action_type": draft.action_type,
            "tone": draft.tone.value,
            "subject": draft.subject,
            "body": draft.body,
            "context": draft.rationale,
            "blocked_rules": [],
            "blocked_draft": None,
            "policy": decision.to_json(),
            "approval_reasons": decision.approval_reasons,
        }
    }


def route_after_policy(state: AgentState) -> str:
    return "finalize" if state.get("final") else "drafting"
