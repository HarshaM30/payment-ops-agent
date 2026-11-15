import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from payops.db.models import AgentRun
from payops.db.models.enums import RunStatus
from tests.integration import factories as f


def test_create_run_with_invoice_ids(client: TestClient, db_session: Session) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    resp = client.post("/runs", json={"invoice_ids": [str(inv.id)]})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert resp.headers["location"] == f"/runs/{body['run_id']}"
    run = db_session.get(AgentRun, uuid.UUID(body["run_id"]))
    assert run.status is RunStatus.QUEUED
    assert run.requested_invoice_ids == [str(inv.id)]


def test_create_run_all_overdue(client: TestClient, db_session: Session) -> None:
    resp = client.post("/runs", json={"scope": "all_overdue", "max_invoices": 5})
    assert resp.status_code == 202
    run = db_session.get(AgentRun, uuid.UUID(resp.json()["run_id"]))
    assert run.scope == "all_overdue"
    assert run.max_invoices == 5


def test_create_run_unknown_invoice_is_422(client: TestClient, db_session: Session) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    missing = str(uuid.uuid4())
    resp = client.post("/runs", json={"invoice_ids": [str(inv.id), missing]})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert missing in err["details"][0]["msg"]


def test_get_run_with_trace(client: TestClient) -> None:
    run_id = client.post("/runs", json={"scope": "all_overdue"}).json()["run_id"]
    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == run_id
    assert body["status"] == "queued"
    assert [t["event_type"] for t in body["trace"]] == ["run_created"]
    assert body["trace"][0]["inputs"]["scope"] == "all_overdue"
    assert body["totals"]["steps"] == 1


def test_trace_is_ordered_and_totals_add_up(client: TestClient, db_session: Session) -> None:
    from payops.services.audit import TokenUsage, record

    run_id = uuid.UUID(client.post("/runs", json={"scope": "all_overdue"}).json()["run_id"])
    record(
        db_session,
        event_type="llm_call",
        name="triage",
        run_id=run_id,
        latency_ms=100,
        usage=TokenUsage(prompt_tokens=50, completion_tokens=20),
    )
    record(
        db_session,
        event_type="llm_call",
        name="drafting",
        run_id=run_id,
        latency_ms=200,
        usage=TokenUsage(prompt_tokens=30, completion_tokens=10),
    )
    body = client.get(f"/runs/{run_id}").json()
    assert [t["name"] for t in body["trace"]] == ["POST /runs", "triage", "drafting"]
    assert body["trace"][1]["total_tokens"] == 70
    assert body["totals"] == {
        "steps": 3,
        "prompt_tokens": 80,
        "completion_tokens": 30,
        "total_tokens": 110,
        "latency_ms": 300,
    }


def test_get_run_404(client: TestClient) -> None:
    resp = client.get(f"/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
