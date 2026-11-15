import uuid

from fastapi import APIRouter, Response

from payops.api.deps import SessionDep, responses
from payops.schemas.api import RunCreate, RunCreated, RunDetail, TraceEntry
from payops.services import runs as run_service

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", status_code=202, response_model=RunCreated, summary="Start an agent run")
def create_run(body: RunCreate, session: SessionDep, response: Response) -> RunCreated:
    """Queue a run over the given invoices, or over all overdue invoices.

    Returns **202** right away; poll `GET /runs/{id}` for status and the trace.
    """
    run = run_service.create_run(session, body)
    response.headers["Location"] = f"/runs/{run.id}"
    return RunCreated(run_id=run.id, status=run.status)


@router.get(
    "/{run_id}", response_model=RunDetail, summary="Run status and trace", responses=responses(404)
)
def get_run(run_id: uuid.UUID, session: SessionDep) -> RunDetail:
    """Status plus the ordered trace from `audit_log` (tokens and latency per step)."""
    run, trace, totals = run_service.get_run(session, run_id)
    return RunDetail(
        id=run.id,
        status=run.status,
        scope=run.scope,
        requested_invoice_ids=run.requested_invoice_ids,
        max_invoices=run.max_invoices,
        llm_provider=run.llm_provider,
        llm_model=run.llm_model,
        prompt_version=run.prompt_version,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        totals=totals,
        trace=[TraceEntry.model_validate(t) for t in trace],
    )
