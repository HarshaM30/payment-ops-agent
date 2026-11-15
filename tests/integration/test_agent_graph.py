"""The full LangGraph workflow with FakeLLM against real Postgres, end to end."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payops.agents.runner import execute_run
from payops.api.routes.runs import get_run_executor
from payops.config import get_settings
from payops.db.models import Action, AgentRun, AuditLog, Outbox
from payops.db.models.enums import (
    ActionStatus,
    ActionType,
    ApprovalStatus,
    InvoiceStatus,
    PaymentEventType,
    RunStatus,
)
from payops.llm.client import LLMClient
from payops.llm.fake import FakeLLM, FakeToolCalls
from payops.llm.retry import RetryPolicy
from payops.services import approvals
from tests.integration import factories as f

GOOD = {
    "action_type": "reminder",
    "tone": "friendly",
    "subject": "Reminder: invoice past due",
    "body": "Hi, invoice is past due. Please arrange payment. Accounts Receivable",
    "rationale": "test",
}


def client_for(fake: FakeLLM, retries: int = 1) -> LLMClient:
    return LLMClient(
        fake,
        provider="fake",
        model="fake-v1",
        retry_policy=RetryPolicy(max_retries=retries, base_seconds=0, max_seconds=0),
        sleep=lambda _: None,
    )


def new_run(s: Session, invoice_ids: list[uuid.UUID]) -> AgentRun:
    run = AgentRun(status=RunStatus.QUEUED, requested_invoice_ids=[str(i) for i in invoice_ids])
    s.add(run)
    s.commit()  # like POST /runs: the queued run is committed before it executes
    return run


def run_graph(s: Session, invoice_ids: list[uuid.UUID], fake: FakeLLM | None = None) -> AgentRun:
    run = new_run(s, invoice_ids)
    return execute_run(s, run.id, llm=client_for(fake or FakeLLM()))


def actions_for(s: Session, run_id: uuid.UUID) -> dict[uuid.UUID, Action]:
    return {a.invoice_id: a for a in s.scalars(select(Action).where(Action.run_id == run_id))}


def outbox_for(s: Session, invoice_id: uuid.UUID) -> list[Outbox]:
    return list(s.scalars(select(Outbox).where(Outbox.invoice_id == invoice_id)))


def trace(s: Session, run_id: uuid.UUID) -> list[AuditLog]:
    return list(
        s.scalars(
            select(AuditLog)
            .where(AuditLog.run_id == run_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def test_full_graph_end_to_end(db_session: Session) -> None:
    s = db_session
    small = f.invoice(s, f.customer(s), amount="800.00", due_date=date.today() - timedelta(days=5))
    large = f.invoice(s, f.customer(s), amount="25000.00")
    disputed = f.invoice(s, f.customer(s), status=InvoiceStatus.DISPUTED)
    partial_cust = f.customer(s)
    partial = f.invoice(s, partial_cust, amount="4000.00", amount_paid="500.00")
    f.payment(s, partial, "500.00", date.today() - timedelta(days=3))
    planned_cust = f.customer(s)
    planned = f.invoice(s, planned_cust, amount="3000.00")
    f.plan(s, planned_cust, planned)
    ids = [small.id, large.id, disputed.id, partial.id, planned.id]

    run = run_graph(s, ids)

    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.error is None
    assert (run.llm_provider, run.llm_model, run.prompt_version) == ("fake", "fake-v1", "v1")
    assert run.started_at
    assert run.finished_at
    acts = actions_for(s, run.id)
    assert {i: a.action_type for i, a in acts.items()} == {
        small.id: ActionType.REMINDER,
        large.id: ActionType.REMINDER,
        disputed.id: ActionType.ESCALATE_TO_HUMAN,
        partial.id: ActionType.PAYMENT_PLAN_OFFER,
        planned.id: ActionType.NO_ACTION,
    }
    # Small reminder: auto-approved and "sent".
    assert acts[small.id].status is ActionStatus.EXECUTED
    assert len(outbox_for(s, small.id)) == 1
    # Over threshold and plan offer: waiting for a human, nothing sent.
    assert acts[large.id].approval.reasons == ["APPROVAL_OVER_THRESHOLD"]
    assert acts[partial.id].approval.reasons == ["APPROVAL_FOR_PAYMENT_PLAN"]
    for inv in (large, partial, disputed, planned):
        assert outbox_for(s, inv.id) == []
    assert acts[disputed.id].status is ActionStatus.EXECUTED  # escalation recorded, not sent
    # Ordering and priority stored on the action.
    assert acts[large.id].payload["priority"]["priority_score"] >= 0
    assert acts[small.id].payload["policy"]["outcome"] == "allow"

    events = trace(s, run.id)
    kinds = [e.event_type for e in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert kinds.count("policy_result") == 5
    assert {e.name for e in events if e.event_type == "tool_call"} >= {
        "get_customer",
        "get_invoices",
        "get_payment_history",
        "draft_message",
    }
    llm_calls = [e for e in events if e.event_type == "llm_call"]
    assert {e.name for e in llm_calls} == {"triage", "research", "drafting"}
    assert all(e.total_tokens and e.latency_ms is not None for e in llm_calls)


def test_approving_executes_stored_draft_and_completes_run(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="25000.00")
    run = run_graph(s, [inv.id])
    assert run.status is RunStatus.AWAITING_APPROVAL
    action = actions_for(s, run.id)[inv.id]
    fake_calls_before = s.scalar(select(func.count()).select_from(AuditLog))

    approvals.approve(s, action.approval.id, approver="dana")

    sent = outbox_for(s, inv.id)
    assert len(sent) == 1
    assert (sent[0].subject, sent[0].body) == (action.subject, action.body)
    s.refresh(run)
    assert run.status is RunStatus.COMPLETED
    new_events = s.scalars(select(AuditLog).order_by(AuditLog.id).offset(fake_calls_before)).all()
    assert "llm_call" not in {e.event_type for e in new_events}  # no LLM after a human decision


def test_disputed_never_gets_reminder_even_if_llm_insists(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), status=InvoiceStatus.DISPUTED, amount="500.00")
    fake = FakeLLM()
    fake.add("drafting", inv.invoice_number, GOOD)  # always a reminder, every attempt

    run = run_graph(s, [inv.id], fake)

    action = actions_for(s, run.id)[inv.id]
    assert action.action_type is ActionType.ESCALATE_TO_HUMAN
    assert action.payload["blocked_rules"] == ["NO_REMINDER_ON_DISPUTED"]
    assert action.payload["blocked_draft"]["action_type"] == "reminder"
    assert outbox_for(s, inv.id) == []
    assert run.status is RunStatus.COMPLETED
    blocked = [
        e
        for e in trace(s, run.id)
        if e.event_type == "policy_result" and e.outputs["outcome"] == "block"
    ]
    assert len(blocked) == 1


def test_discount_is_redrafted_once_with_feedback(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="500.00")
    fake = FakeLLM()
    discount = {**GOOD, "body": "Pay this week and get a 10% discount!"}
    fake.add("drafting", inv.invoice_number, discount, GOOD)

    run = run_graph(s, [inv.id], fake)

    drafting_calls = fake.calls_for("drafting")
    assert len(drafting_calls) == 2
    assert "discount" in drafting_calls[1].messages[-1]["content"].lower()  # feedback sent
    action = actions_for(s, run.id)[inv.id]
    assert action.action_type is ActionType.REMINDER
    assert action.payload["revisions"] == 1
    [sent] = outbox_for(s, inv.id)
    assert "discount" not in sent.body.lower()


def test_ten_percent_discount_is_never_executed(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="500.00")
    fake = FakeLLM()
    fake.add("drafting", inv.invoice_number, {**GOOD, "body": "We offer a 10% discount."})

    run = run_graph(s, [inv.id], fake)

    assert len(fake.calls_for("drafting")) == 2  # original + one redraft, then escalate
    action = actions_for(s, run.id)[inv.id]
    assert action.action_type is ActionType.ESCALATE_TO_HUMAN
    assert "10% discount" not in (action.body or "")
    assert action.payload["blocked_draft"]["body"] == "We offer a 10% discount."
    assert outbox_for(s, inv.id) == []
    assert (
        s.scalar(select(func.count()).select_from(Outbox).where(Outbox.body.ilike("%discount%")))
        == 0
    )


def test_invalid_schema_twice_escalates(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="500.00")
    fake = FakeLLM()
    fake.add("drafting", inv.invoice_number, "I think you should send a reminder.")

    run = run_graph(s, [inv.id], fake)

    action = actions_for(s, run.id)[inv.id]
    assert action.action_type is ActionType.ESCALATE_TO_HUMAN
    assert action.payload["blocked_rules"] == ["SCHEMA_VALID"]
    assert outbox_for(s, inv.id) == []
    purposes = [
        e.inputs["purpose"]
        for e in trace(s, run.id)
        if e.event_type == "llm_call" and e.name == "drafting"
    ]
    assert purposes == ["call", "corrective_reprompt"]


def test_llm_unavailable_fails_run_cleanly(db_session: Session) -> None:
    class RateLimitedError(Exception):
        status_code = 429

    s = db_session
    inv = f.invoice(s, f.customer(s))
    fake = FakeLLM()
    fake.add("triage", "*", RateLimitedError("429 Too Many Requests"))

    run = run_graph(s, [inv.id], fake)

    assert run.status is RunStatus.FAILED
    assert "LLMUnavailableError" in (run.error or "")
    assert run.finished_at is not None
    assert actions_for(s, run.id) == {}
    kinds = [e.event_type for e in trace(s, run.id)]
    assert kinds[-1] == "run_failed"
    assert "llm_call" in kinds  # the failed attempt is audited too


def test_research_loop_is_capped(db_session: Session) -> None:
    s = db_session
    cust = f.customer(s)
    inv = f.invoice(s, cust, amount="500.00")
    fake = FakeLLM()
    fake.add("research", "*", FakeToolCalls([("get_customer", {"customer_id": str(cust.id)})]))

    run = run_graph(s, [inv.id], fake)

    steps = get_settings().research_max_steps
    assert len(fake.calls_for("research")) == steps
    research = [
        e for e in trace(s, run.id) if e.name == "research" and e.event_type == "node_decision"
    ]
    assert research[0].outputs["steps"] == steps
    assert run.status is RunStatus.COMPLETED


def test_research_cannot_call_write_tools_or_bad_args(db_session: Session) -> None:
    s = db_session
    cust = f.customer(s)
    inv = f.invoice(s, cust, amount="500.00")
    fake = FakeLLM()
    fake.add(
        "research",
        "*",
        FakeToolCalls([("draft_message", {"invoice_id": str(inv.id)})]),
        FakeToolCalls([("get_customer", {"customer_id": "not-a-uuid"})]),
        FakeToolCalls([("get_invoices", {"customer_id": str(uuid.uuid4())})]),
        "Done.",
    )

    run = run_graph(s, [inv.id], fake)

    tool_rows = [e for e in trace(s, run.id) if e.event_type == "tool_call"]
    assert tool_rows[0].name == "draft_message"
    assert "read-only" in tool_rows[0].error
    assert tool_rows[1].name == "get_customer"
    assert "InvalidInputError" in tool_rows[1].error
    assert tool_rows[2].name == "get_invoices"
    assert "NotFoundError" in tool_rows[2].error
    # Errors went back to the model as tool messages, and the run carried on.
    last_research = fake.calls_for("research")[-1].messages
    assert sum(1 for m in last_research if m["role"] == "tool") == 3
    assert run.status is RunStatus.COMPLETED
    assert actions_for(s, run.id)[inv.id].action_type is ActionType.REMINDER


def test_triage_orders_and_clamps_llm_scores(db_session: Session) -> None:
    s = db_session
    old = f.invoice(s, f.customer(s), amount="9000.00", due_date=date.today() - timedelta(days=80))
    new = f.invoice(s, f.customer(s), amount="200.00", due_date=date.today() - timedelta(days=2))

    def flip(request: Any) -> dict[str, Any]:
        # The model tries to bury the old, large invoice and promote the tiny new one.
        return {
            "priorities": [
                {
                    "invoice_id": i["invoice_id"],
                    "priority_score": 0 if i["invoice_number"] == old.invoice_number else 100,
                    "rationale": "x",
                }
                for i in request.context["invoices"]
            ]
        }

    fake = FakeLLM()
    fake.add("triage", "*", flip)
    run = run_graph(s, [new.id, old.id], fake)

    [decision] = [
        e for e in trace(s, run.id) if e.name == "triage" and e.event_type == "node_decision"
    ]
    scores = decision.outputs["scores"]
    for score in scores.values():
        assert abs(score["priority_score"] - score["base_score"]) <= 20
    assert decision.outputs["order"][0] == str(old.id)


def test_triage_schema_failure_falls_back_to_base_scores(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="500.00")
    fake = FakeLLM()
    fake.add("triage", "*", {"priorities": [{"invoice_id": "nope", "priority_score": 500}]})
    run = run_graph(s, [inv.id], fake)
    [decision] = [
        e for e in trace(s, run.id) if e.name == "triage" and e.event_type == "node_decision"
    ]
    assert "LLMSchemaError" in decision.error
    score = decision.outputs["scores"][str(inv.id)]
    assert score["llm_score"] is None
    assert score["priority_score"] == score["base_score"]
    assert run.status is RunStatus.COMPLETED


def test_broken_promise_customer_gets_plan_offer(db_session: Session) -> None:
    s = db_session
    cust = f.customer(s)
    inv = f.invoice(s, cust, amount="3000.00", due_date=date.today() - timedelta(days=60))
    ev = f.event(s, cust, PaymentEventType.PROMISE_TO_PAY, datetime.now(UTC) - timedelta(days=5))
    ev.details = {"kept": False}
    s.flush()
    run = run_graph(s, [inv.id])
    action = actions_for(s, run.id)[inv.id]
    assert action.action_type is ActionType.PAYMENT_PLAN_OFFER
    assert action.approval.status is ApprovalStatus.PENDING


def test_execute_run_twice_does_not_double_send(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="500.00")
    run = run_graph(s, [inv.id])
    again = execute_run(s, run.id, llm=client_for(FakeLLM()))
    assert again.status is RunStatus.COMPLETED
    assert len(outbox_for(s, inv.id)) == 1


def test_approval_refused_if_invoice_became_disputed(db_session: Session) -> None:
    from payops.errors import ConflictError

    s = db_session
    inv = f.invoice(s, f.customer(s), amount="25000.00")
    run = run_graph(s, [inv.id])
    approval = actions_for(s, run.id)[inv.id].approval
    inv.status = InvoiceStatus.DISPUTED
    s.flush()
    with pytest.raises(ConflictError, match="NO_REMINDER_ON_DISPUTED"):
        approvals.approve(s, approval.id, approver="dana")
    assert outbox_for(s, inv.id) == []
    s.refresh(approval)
    assert approval.status is ApprovalStatus.PENDING


def test_reject_completes_run(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s), amount="25000.00")
    run = run_graph(s, [inv.id])
    approval = actions_for(s, run.id)[inv.id].approval
    approvals.reject(s, approval.id, approver="dana", reason="Customer called")
    s.refresh(run)
    assert run.status is RunStatus.COMPLETED
    assert outbox_for(s, inv.id) == []


def test_scope_all_overdue_respects_max_invoices(db_session: Session) -> None:
    s = db_session
    for _ in range(3):
        f.invoice(s, f.customer(s), amount="500.00")
    run = AgentRun(status=RunStatus.QUEUED, scope="all_overdue", max_invoices=2)
    s.add(run)
    s.flush()
    execute_run(s, run.id, llm=client_for(FakeLLM()))
    assert len(actions_for(s, run.id)) == 2


def test_api_post_run_executes_in_background_and_trace_has_tokens(
    client: TestClient, db_session: Session
) -> None:
    inv = f.invoice(db_session, f.customer(db_session), amount="500.00")
    fake = FakeLLM()
    client.app.dependency_overrides[get_run_executor] = lambda: (
        lambda run_id: execute_run(db_session, run_id, llm=client_for(fake))
    )

    resp = client.post("/runs", json={"invoice_ids": [str(inv.id)]})
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"  # response is sent before the run executes

    body = client.get(f"/runs/{resp.json()['run_id']}").json()
    assert body["status"] == "completed"
    assert body["llm_model"] == "fake-v1"
    assert body["prompt_version"] == "v1"
    entries = body["trace"]
    ids = [e["id"] for e in entries]
    times = [e["created_at"] for e in entries]
    assert times == sorted(times)
    assert len(ids) == len(set(ids))
    assert entries[0]["event_type"] == "run_created"
    llm_steps = [e for e in entries if e["event_type"] == "llm_call"]
    assert llm_steps
    assert all(e["total_tokens"] > 0 for e in llm_steps)
    timed = {"llm_call", "tool_call", "node_decision", "policy_result"}
    assert all(e["latency_ms"] is not None for e in entries if e["event_type"] in timed)
    assert body["totals"]["total_tokens"] == sum(e["total_tokens"] or 0 for e in entries)
    leaf = [e for e in entries if e["event_type"] in {"llm_call", "tool_call"}]
    assert body["totals"]["latency_ms"] == sum(e["latency_ms"] for e in leaf)


def test_unknown_run_raises(db_session: Session) -> None:
    with pytest.raises(ValueError, match="not found"):
        execute_run(db_session, uuid.uuid4(), llm=client_for(FakeLLM()))


def test_missing_prompt_version_fails_run(db_session: Session) -> None:
    s = db_session
    inv = f.invoice(s, f.customer(s))
    run = new_run(s, [inv.id])
    settings = get_settings().model_copy(update={"prompt_version": "v999"})
    result = execute_run(s, run.id, llm=client_for(FakeLLM()), settings=settings)
    assert result.status is RunStatus.FAILED
    assert "v999" in (result.error or "")
