"""Executes a queued run: status transitions, the graph, and clean failure handling."""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payops import clock
from payops.agents.facts import candidate_invoice_ids
from payops.agents.graph import build_graph, recursion_limit
from payops.agents.state import NODES_WITH_PROMPTS, AgentContext, load_prompt
from payops.config import Settings, get_settings
from payops.db.models import Action, AgentRun, Approval, AuditLog
from payops.db.models.enums import ApprovalStatus, RunStatus
from payops.db.session import get_sessionmaker
from payops.llm.client import LLMClient, build_client
from payops.services import audit

logger = logging.getLogger("payops.runner")

MAX_ERROR_LENGTH = 2000


def pending_approvals(session: Session, run_id: uuid.UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Approval)
            .join(Action, Approval.action_id == Action.id)
            .where(Action.run_id == run_id, Approval.status == ApprovalStatus.PENDING)
        )
        or 0
    )


def execute_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> AgentRun:
    settings = settings or get_settings()
    # Row lock: two workers can't both pick up the same queued run.
    run = session.get(AgentRun, run_id, with_for_update=True)
    if run is None:
        raise ValueError(f"Run {run_id} not found")
    if run.status is not RunStatus.QUEUED:
        session.rollback()
        return run  # already picked up: executing twice must not double-send

    try:
        llm = llm or build_client(settings)
        for node in NODES_WITH_PROMPTS:
            load_prompt(node, settings.prompt_version)
    except Exception as exc:
        return _fail(session, run_id, exc)

    run.status = RunStatus.RUNNING
    run.started_at = clock.now()
    run.llm_provider = llm.provider
    run.llm_model = llm.model
    run.prompt_version = settings.prompt_version
    audit.record(
        session,
        event_type="run_started",
        name="runner",
        run_id=run_id,
        actor="agent",
        outputs={
            "llm_provider": llm.provider,
            "llm_model": llm.model,
            "prompt_version": settings.prompt_version,
        },
    )
    session.commit()

    ctx = AgentContext(
        session=session,
        llm=llm,
        settings=settings,
        run_id=run_id,
        prompt_version=settings.prompt_version,
    )
    try:
        limit = recursion_limit(len(candidate_invoice_ids(session, run)))
        state = build_graph(ctx).invoke({"run_id": run_id}, {"recursion_limit": limit})
    except Exception as exc:
        logger.exception("run failed", extra={"run_id": str(run_id)})
        return _fail(session, run_id, exc)

    run = session.get(AgentRun, run_id)
    assert run is not None  # noqa: S101
    waiting = pending_approvals(session, run_id)
    run.status = RunStatus.AWAITING_APPROVAL if waiting else RunStatus.COMPLETED
    run.finished_at = clock.now()
    outcomes = state.get("outcomes", [])
    audit.record(
        session,
        event_type="run_finished",
        name="runner",
        run_id=run_id,
        actor="agent",
        outputs={
            "status": run.status.value,
            "invoices": len(outcomes),
            "pending_approvals": waiting,
            "outcomes": outcomes,
        },
    )
    session.commit()
    return run


_AUDIT_COLUMNS = (
    "run_id",
    "event_type",
    "name",
    "actor",
    "inputs",
    "outputs",
    "error",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


def _fail(session: Session, run_id: uuid.UUID, exc: BaseException) -> AgentRun:
    # Roll back partial work for the current invoice, but keep its audit trail: the
    # trace should show the steps (e.g. the failed LLM call) that led to the failure.
    def run_audit() -> list[AuditLog]:
        return list(
            session.scalars(select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.id))
        )

    try:
        before = [{c: getattr(r, c) for c in _AUDIT_COLUMNS} | {"id": r.id} for r in run_audit()]
    except Exception:  # the transaction itself may be broken (e.g. DB went away)
        before = []
    session.rollback()
    kept = {r.id for r in run_audit()}
    for row in before:
        if row.pop("id") not in kept:
            session.add(AuditLog(**row))
    run = session.get(AgentRun, run_id)
    assert run is not None  # noqa: S101
    message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
    run.status = RunStatus.FAILED
    run.error = message
    run.finished_at = clock.now()
    audit.record(
        session,
        event_type="run_failed",
        name="runner",
        run_id=run_id,
        actor="agent",
        error=message,
    )
    session.commit()
    return run


def run_in_background(run_id: uuid.UUID) -> None:
    """Entry point for FastAPI BackgroundTasks: its own session, never raises."""
    try:
        with get_sessionmaker()() as session:
            execute_run(session, run_id)
    except Exception:
        logger.exception("background run crashed", extra={"run_id": str(run_id)})
