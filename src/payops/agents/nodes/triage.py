"""Triage: deterministic features + LLM rationale -> priority score and ordering."""

import time
import uuid
from typing import Any

from payops.agents.facts import candidate_invoice_ids, load_facts
from payops.agents.scoring import base_score, final_score
from payops.agents.state import AgentContext, AgentState, user_json
from payops.db.models import AgentRun
from payops.llm.client import LLMRequest, LLMSchemaError
from payops.schemas.llm import TriageOutput

# Without max_invoices, a scope run handles at most this many (keeps free-tier usage sane).
DEFAULT_SCOPE_LIMIT = 20


def triage(state: AgentState, ctx: AgentContext) -> AgentState:
    started = time.perf_counter()
    run = ctx.session.get(AgentRun, ctx.run_id)
    assert run is not None  # noqa: S101 - the runner created it
    ids = candidate_invoice_ids(ctx.session, run)
    ctx.facts = load_facts(ctx.session, ids)
    ids = [i for i in ids if i in ctx.facts]
    bases = {i: base_score(ctx.facts[i]) for i in ids}

    limit = run.max_invoices or (DEFAULT_SCOPE_LIMIT if run.scope else len(ids))
    if len(ids) > limit:
        ids = sorted(ids, key=lambda i: (-bases[i], ctx.facts[i].invoice_number))[:limit]

    llm_scores: dict[uuid.UUID, tuple[int, str]] = {}
    error: str | None = None
    if ids:
        items = [
            {
                "invoice_id": str(i),
                "base_score": bases[i],
                **{
                    k: v
                    for k, v in ctx.facts[i].to_json().items()
                    if k not in {"customer_id", "customer_name", "company"}
                },
            }
            for i in ids
        ]
        request = LLMRequest(
            node="triage",
            messages=[{"role": "system", "content": ctx.prompt("triage")}, user_json(items)],
            context={"invoices": items},
        )
        try:
            result = ctx.llm.structured(request, TriageOutput, recorder=ctx.record_llm_call)
            wanted = set(ids)
            llm_scores = {
                p.invoice_id: (p.priority_score, p.rationale)
                for p in result.value.priorities
                if p.invoice_id in wanted
            }
        except LLMSchemaError as exc:
            # Ordering falls back to the deterministic score; nothing unsafe can happen.
            error = f"LLMSchemaError: {exc}"

    scores: dict[str, dict[str, Any]] = {}
    for i in ids:
        llm_score, rationale = llm_scores.get(i, (None, None))
        scores[str(i)] = {
            "invoice_number": ctx.facts[i].invoice_number,
            "base_score": bases[i],
            "llm_score": llm_score,
            "priority_score": final_score(bases[i], llm_score),
            "rationale": rationale,
        }
    queue = sorted(
        ids,
        key=lambda i: (
            -scores[str(i)]["priority_score"],
            -ctx.facts[i].days_overdue,
            ctx.facts[i].invoice_number,
        ),
    )
    ctx.record_decision(
        "triage",
        inputs={"candidates": len(bases), "limit": limit},
        outputs={"order": [str(i) for i in queue], "scores": scores},
        error=error,
        started=started,
    )
    return {"queue": queue, "index": 0, "scores": scores, "outcomes": []}
