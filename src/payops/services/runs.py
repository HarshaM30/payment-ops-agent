"""Agent run records: create (queued) and read (status + trace).

Execution lives in ``payops.agents.runner``.
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from payops.db.models import AgentRun, AuditLog, Invoice
from payops.db.models.enums import RunStatus
from payops.errors import ConflictError, InvalidInputError, NotFoundError
from payops.schemas.api import RunCreate, RunTotals
from payops.services import audit

LEAF_EVENT_TYPES = frozenset({"llm_call", "tool_call"})


def request_hash(req: RunCreate) -> str:
    return hashlib.sha256(req.model_dump_json().encode()).hexdigest()


def _replay(session: Session, key: str, fingerprint: str) -> AgentRun | None:
    """The run already created with this idempotency key, if any."""
    run = session.scalar(select(AgentRun).where(AgentRun.idempotency_key == key))
    if run is not None and run.request_hash != fingerprint:
        raise ConflictError("Idempotency-Key was already used with a different request body")
    return run


def create_run(
    session: Session, req: RunCreate, *, actor: str = "api", idempotency_key: str | None = None
) -> tuple[AgentRun, bool]:
    """Create a queued run. Returns ``(run, replayed)``.

    With an idempotency key, a retry of the same request returns the run created the
    first time (``replayed=True``) instead of starting a second one; the same key with a
    different body is a 409.
    """
    fingerprint = request_hash(req) if idempotency_key else None
    if idempotency_key and fingerprint:
        existing = _replay(session, idempotency_key, fingerprint)
        if existing is not None:
            return existing, True

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
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent request with the same key won the race: return its run.
        session.rollback()
        if idempotency_key and fingerprint:
            existing = _replay(session, idempotency_key, fingerprint)
            if existing is not None:
                return existing, True
        raise
    audit.record(
        session,
        event_type="run_created",
        name="POST /runs",
        run_id=run.id,
        actor=actor,
        inputs=req,
        outputs={"status": run.status.value, "idempotency_key": idempotency_key},
    )
    session.commit()
    return run, False


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


def list_recent(session: Session, *, limit: int = 20) -> list[AgentRun]:
    """Newest runs first, with their actions (for the HTML runs page)."""
    return list(
        session.scalars(
            select(AgentRun)
            .options(selectinload(AgentRun.actions))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(limit)
        )
    )
