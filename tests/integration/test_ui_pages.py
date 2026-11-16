"""The approvals page against real Postgres: same service code and results as the JSON API."""

import html
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payops.agents.runner import execute_run
from payops.db.models import AgentRun, AuditLog, Outbox
from payops.db.models.enums import (
    ActionStatus,
    ActionType,
    ApprovalStatus,
    InvoiceStatus,
    RunStatus,
)
from payops.llm.client import LLMClient
from payops.llm.fake import FakeLLM
from payops.llm.retry import RetryPolicy
from tests.conftest import TEST_API_KEY
from tests.integration import factories as f
from tests.ui_helpers import login

XSS = '<script>alert("pwned")</script><img src=x onerror=alert(1)>'


@pytest.fixture
def ui(client: TestClient) -> TestClient:
    client.headers.pop("X-API-Key", None)  # the page must work on the session alone
    return client


@pytest.fixture
def csrf(ui: TestClient) -> str:
    return login(ui, "Dana Approver")


def test_approvals_page_shows_the_pending_queue(
    ui: TestClient, csrf: str, db_session: Session
) -> None:
    ap = f.pending_approval(db_session, subject="Invoice overdue", body="Hi Pat,\nPlease pay.")
    inv = ap.action.invoice
    resp = ui.get("/ui/approvals")
    assert resp.status_code == 200
    page = resp.text
    for expected in (
        inv.customer.name,
        inv.customer.company,
        inv.invoice_number,
        "25,000.00 USD",
        "reminder",
        "APPROVAL_OVER_THRESHOLD",
        "Invoice overdue",
        "Hi Pat,\nPlease pay.",
        f"/ui/approvals/{ap.id}/approve",
        f"/ui/approvals/{ap.id}/reject",
    ):
        assert expected in page, expected
    assert f'name="csrf_token" value="{csrf}"' in page


def test_approve_on_the_page_writes_the_outbox_like_the_api(
    ui: TestClient, csrf: str, db_session: Session
) -> None:
    via_page = f.pending_approval(db_session)
    via_api = f.pending_approval(db_session)

    resp = ui.post(
        f"/ui/approvals/{via_page.id}/approve", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/approvals?notice=approved"
    # The JSON API still needs its key; send it explicitly for the comparison.
    api = ui.post(
        f"/approvals/{via_api.id}/approve",
        json={"approver": "Dana Approver"},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert api.status_code == 200

    for ap in (via_page, via_api):
        db_session.refresh(ap)
        db_session.refresh(ap.action)
        assert ap.status is ApprovalStatus.APPROVED
        assert ap.decided_by == "Dana Approver"
        assert ap.action.status is ActionStatus.EXECUTED
        [sent] = db_session.scalars(select(Outbox).where(Outbox.action_id == ap.action_id)).all()
        assert sent.subject == ap.action.subject
        assert sent.body == ap.action.body
        assert sent.recipient == ap.action.invoice.customer.email
        audit = db_session.scalars(
            select(AuditLog).where(
                AuditLog.event_type == "approval_decision", AuditLog.run_id == ap.action.run_id
            )
        ).one()
        assert audit.actor == "Dana Approver"
        assert audit.name == "approve"

    page = ui.get("/ui/approvals?notice=approved").text
    assert "Approved. The message was added to the outbox" in page
    assert str(via_page.id) not in page


def test_drafted_script_renders_as_text(ui: TestClient, csrf: str, db_session: Session) -> None:
    ap = f.pending_approval(db_session, subject=XSS[:150], body=f"Hello {XSS} bye")
    page = ui.get("/ui/approvals").text
    assert "<script>" not in page
    assert "<img src=x" not in page
    assert html.escape(XSS, quote=True) in page or "&lt;script&gt;" in page

    ui.post(f"/ui/approvals/{ap.id}/approve", data={"csrf_token": csrf})
    outbox = ui.get("/ui/outbox").text
    assert "<script>" not in outbox
    assert "&lt;script&gt;" in outbox


def test_reject_on_the_page(ui: TestClient, csrf: str, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    resp = ui.post(
        f"/ui/approvals/{ap.id}/reject",
        data={"csrf_token": csrf, "reason": "  Customer called, paying Friday  "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(ap)
    assert ap.status is ApprovalStatus.REJECTED
    assert ap.decided_by == "Dana Approver"
    assert ap.decision_reason == "Customer called, paying Friday"
    assert db_session.scalar(select(Outbox).where(Outbox.action_id == ap.action_id)) is None


@pytest.mark.parametrize("reason", ["", "  ", "no", "x" * 1001])
def test_reject_needs_a_reason(ui: TestClient, csrf: str, db_session: Session, reason) -> None:
    ap = f.pending_approval(db_session)
    resp = ui.post(f"/ui/approvals/{ap.id}/reject", data={"csrf_token": csrf, "reason": reason})
    assert resp.status_code == 422
    assert "needs a reason" in resp.text
    db_session.refresh(ap)
    assert ap.status is ApprovalStatus.PENDING


def test_deciding_twice_shows_a_conflict(ui: TestClient, csrf: str, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    url = f"/ui/approvals/{ap.id}/approve"
    assert ui.post(url, data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    again = ui.post(url, data={"csrf_token": csrf})
    assert again.status_code == 409
    assert "already approved" in again.text
    assert (
        len(db_session.scalars(select(Outbox).where(Outbox.action_id == ap.action_id)).all()) == 1
    )


def test_unknown_approval_is_404(ui: TestClient, csrf: str) -> None:
    resp = ui.post(f"/ui/approvals/{uuid.uuid4()}/approve", data={"csrf_token": csrf})
    assert resp.status_code == 404


def test_policy_still_blocks_on_the_page(ui: TestClient, csrf: str, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    ap.action.invoice.status = InvoiceStatus.DISPUTED  # disputed after the draft was written
    db_session.flush()
    resp = ui.post(f"/ui/approvals/{ap.id}/approve", data={"csrf_token": csrf})
    assert resp.status_code == 409
    assert "NO_REMINDER_ON_DISPUTED" in resp.text
    db_session.refresh(ap)
    assert ap.status is ApprovalStatus.PENDING
    assert db_session.scalar(select(Outbox).where(Outbox.action_id == ap.action_id)) is None


def test_non_message_action_has_no_message_box(
    ui: TestClient, csrf: str, db_session: Session
) -> None:
    f.pending_approval(
        db_session, action_type=ActionType.ESCALATE_TO_HUMAN, subject=None, body=None
    )
    assert "approving only records the action" in ui.get("/ui/approvals").text


def test_bad_cursor_shows_an_error(ui: TestClient, csrf: str) -> None:
    resp = ui.get("/ui/approvals", params={"cursor": "garbage!!"})
    assert resp.status_code == 422
    assert "no longer valid" in resp.text


def test_outbox_page_lists_sent_messages(ui: TestClient, csrf: str, db_session: Session) -> None:
    assert "Nothing sent yet" in ui.get("/ui/outbox").text
    ap = f.pending_approval(db_session, subject="Your invoice", body="Please pay soon.")
    ui.post(f"/ui/approvals/{ap.id}/approve", data={"csrf_token": csrf})
    page = ui.get("/ui/outbox").text
    assert "Your invoice" in page
    assert "Please pay soon." in page
    assert ap.action.invoice.customer.email in page


def test_start_run_from_the_page(ui: TestClient, csrf: str, db_session: Session) -> None:
    resp = ui.post(
        "/ui/runs", data={"csrf_token": csrf, "max_invoices": "3"}, follow_redirects=False
    )
    assert resp.status_code == 303
    run_id = uuid.UUID(resp.headers["location"].rsplit("/", 1)[1])
    run = db_session.get(AgentRun, run_id)
    assert run is not None
    assert run.scope == "all_overdue"
    assert run.max_invoices == 3
    created = db_session.scalars(select(AuditLog).where(AuditLog.run_id == run_id)).one()
    assert created.actor == "ui:Dana Approver"

    page = ui.get(f"/ui/runs/{run_id}")
    assert page.status_code == 200
    assert "run created" in page.text
    assert 'http-equiv="refresh"' in page.text  # queued: the page refreshes itself
    assert str(run_id)[:8] in ui.get("/ui/runs").text


@pytest.mark.parametrize("value", ["0", "51", "abc", "2.5"])
def test_start_run_validates_max_invoices(ui: TestClient, csrf: str, value: str) -> None:
    resp = ui.post("/ui/runs", data={"csrf_token": csrf, "max_invoices": value})
    assert resp.status_code == 422
    assert "Max invoices" in resp.text


def test_run_page_shows_the_trace_of_a_finished_run(
    ui: TestClient, csrf: str, db_session: Session
) -> None:
    c = f.customer(db_session)
    inv = f.invoice(db_session, c, amount="500.00")
    run = AgentRun(status=RunStatus.QUEUED, requested_invoice_ids=[str(inv.id)])
    db_session.add(run)
    db_session.commit()
    llm = LLMClient(
        FakeLLM(),
        provider="fake",
        model="fake-v1",
        retry_policy=RetryPolicy(max_retries=0, base_seconds=0, max_seconds=0),
        sleep=lambda _: None,
    )
    execute_run(db_session, run.id, llm=llm)

    page = ui.get(f"/ui/runs/{run.id}").text
    assert 'http-equiv="refresh"' not in page
    for expected in ("llm call", "tool call", "policy", inv.invoice_number, "fake-v1"):
        assert expected in page, expected


def test_unknown_run_is_404(ui: TestClient, csrf: str) -> None:
    resp = ui.get(f"/ui/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert "not found" in resp.text
