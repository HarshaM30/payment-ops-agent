"""Drafting: pick an action and tone, and write the message (structured output)."""

import time

from payops.agents.state import AgentContext, AgentState, current_invoice, user_json
from payops.llm.client import LLMRequest, LLMSchemaError
from payops.schemas.llm import DraftOutput


def drafting(state: AgentState, ctx: AgentContext) -> AgentState:
    started = time.perf_counter()
    invoice_id = current_invoice(state)
    facts = ctx.facts[invoice_id].to_json()
    feedback = state.get("feedback")
    payload = {
        "invoice": {k: v for k, v in facts.items() if k != "customer_id"},
        "research_notes": state.get("research_notes"),
    }
    if feedback:
        payload["policy_feedback"] = (
            "Your previous draft was rejected by the policy check: "
            f"{feedback} Rewrite it so it complies."
        )
    request = LLMRequest(
        node="drafting",
        messages=[{"role": "system", "content": ctx.prompt("drafting")}, user_json(payload)],
        context={
            "invoice_number": facts["invoice_number"],
            "facts": facts,
            "feedback": feedback,
            "revision": state.get("revisions", 0),
        },
    )
    draft: DraftOutput | None = None
    errors = None
    try:
        draft = ctx.llm.structured(request, DraftOutput, recorder=ctx.record_llm_call).value
    except LLMSchemaError as exc:
        errors = exc.errors

    ctx.record_decision(
        "drafting",
        inputs={"invoice_id": invoice_id, "revision": state.get("revisions", 0)},
        outputs=draft.model_dump(mode="json") if draft else None,
        error=f"LLMSchemaError: {errors}" if errors is not None else None,
        started=started,
    )
    return {"draft": draft, "draft_errors": errors}
