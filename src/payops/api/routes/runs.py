import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Response

from payops.api.deps import SessionDep, responses
from payops.schemas.api import RunCreate, RunCreated, RunDetail, TraceEntry
from payops.services import runs as run_service

router = APIRouter(prefix="/runs", tags=["runs"])

RunExecutor = Callable[[uuid.UUID], None]


def get_run_executor() -> RunExecutor:
    """What executes a run after the response is sent. Tests override this."""
    from payops.agents.runner import run_in_background

    return run_in_background


@router.post("", status_code=202, response_model=RunCreated, summary="Start an agent run")
def create_run(
    body: RunCreate,
    session: SessionDep,
    response: Response,
    background: BackgroundTasks,
    executor: Annotated[RunExecutor, Depends(get_run_executor)],
) -> RunCreated:
    """Queue a run over the given invoices, or over all past-due invoices.

    Returns **202** right away and executes the agent in the background; poll
    `GET /runs/{id}` for status and the trace. A `scope` run without `max_invoices`
    handles at most the 20 highest-priority invoices.
    """
    run = run_service.create_run(session, body)
    background.add_task(executor, run.id)
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
