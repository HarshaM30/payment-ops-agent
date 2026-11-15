"""Agent run records: create (queued) and read (status + trace).

Execution lives in ``payops.agents.runner``.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from payops.db.models import AgentRun, AuditLog, Invoice
from payops.db.models.enums import RunStatus
from payops.errors import InvalidInputError, NotFoundError
from payops.schemas.api import RunCreate, RunTotals
from payops.services import audit

LEAF_EVENT_TYPES = frozenset({"llm_call", "tool_call"})


def create_run(session: Session, req: RunCreate, *, actor: str = "api") -> AgentRun:
    requested: list[str] | None = None
    if req.invoice_ids is not None:
        found = set(session.scalars(select(Invoice.id).where(Invoice.id.in_(req.invoice_ids))))
        missing = [str(i) for i in req.invoice_ids if i not in found]
        if missing:
            raise InvalidInputError(
                "Unknown invoice ids",
                [
                    {
                        "loc": ["body", "invoice_ids"],
                        "msg": f"Invoice {i} not found",
                        "type": "not_found",
                    }
                    for i in missing
                ],
            )
        requested = [str(i) for i in req.invoice_ids]

    run = AgentRun(
        status=RunStatus.QUEUED,
        scope=req.scope,
        requested_invoice_ids=requested,
        max_invoices=req.max_invoices,
    )
    session.add(run)
    session.flush()
    audit.record(
        session,
        event_type="run_created",
        name="POST /runs",
        run_id=run.id,
        actor=actor,
        inputs=req,
        outputs={"status": run.status.value},
    )
    session.commit()
    return run


def get_run(session: Session, run_id: uuid.UUID) -> tuple[AgentRun, list[AuditLog], RunTotals]:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise NotFoundError(f"Run {run_id} not found")
    trace = list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.run_id == run_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )
    totals = RunTotals(
        steps=len(trace),
        prompt_tokens=sum(t.prompt_tokens or 0 for t in trace),
        completion_tokens=sum(t.completion_tokens or 0 for t in trace),
        total_tokens=sum(t.total_tokens or 0 for t in trace),
        # Node and policy steps wrap the LLM/tool calls they make, so only leaf steps
        # are summed; otherwise time would be counted twice.
        latency_ms=sum(t.latency_ms or 0 for t in trace if t.event_type in LEAF_EVENT_TYPES),
    )
    return run, trace, totals
