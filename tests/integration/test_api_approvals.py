import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payops.db.models import Action, AuditLog, Outbox
from payops.db.models.enums import ActionStatus, ActionType, ApprovalStatus
from payops.services import outbox as outbox_service
from tests.integration import factories as f


def test_list_pending_with_details(client: TestClient, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    resp = client.get("/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    [item] = body["items"]
    assert item["id"] == str(ap.id)
    assert item["status"] == "pending"
    assert item["reasons"] == ["APPROVAL_OVER_THRESHOLD"]
    assert item["action"]["action_type"] == "reminder"
    assert item["action"]["body"].startswith("Hi")
    assert item["invoice"]["amount"] == "25000.00"
    assert item["invoice"]["balance"] == "25000.00"
    assert item["customer"]["email"].endswith("@example.com")


def test_list_pagination_and_status_filter(client: TestClient, db_session: Session) -> None:
    created = [f.pending_approval(db_session) for _ in range(5)]
    decided = f.pending_approval(db_session)
    decided.status = ApprovalStatus.REJECTED
    db_session.flush()

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get("/approvals", params=params).json()
        assert len(body["items"]) <= 2
        seen.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(str(a.id) for a in created)
    assert len(seen) == len(set(seen))

    rejected = client.get("/approvals", params={"status": "rejected"}).json()["items"]
    assert [i["id"] for i in rejected] == [str(decided.id)]


def test_list_bad_cursor_is_422(client: TestClient) -> None:
    resp = client.get("/approvals", params={"cursor": "garbage!!"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_approve_executes_and_writes_outbox(client: TestClient, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    action = ap.action
    resp = client.post(f"/approvals/{ap.id}/approve", json={"approver": "  dana  "})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "dana"
    assert body["decided_at"] is not None
    assert body["action"]["status"] == "executed"

    [sent] = db_session.scalars(select(Outbox).where(Outbox.action_id == action.id)).all()
    assert sent.subject == action.subject
    assert sent.body == action.body  # exactly the approved text
    assert sent.recipient == action.invoice.customer.email
    assert sent.channel == "email"

    audit = db_session.scalars(
        select(AuditLog).where(AuditLog.event_type == "approval_decision")
    ).one()
    assert audit.actor == "dana"
    assert audit.run_id == action.run_id
    assert audit.outputs["outbox_id"] == str(sent.id)


def test_approve_twice_is_409_and_sends_once(client: TestClient, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    assert client.post(f"/approvals/{ap.id}/approve", json={"approver": "dana"}).status_code == 200
    resp = client.post(f"/approvals/{ap.id}/approve", json={"approver": "sam"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert db_session.scalar(select(func.count()).select_from(Outbox)) == 1


def test_reject_records_reason_and_sends_nothing(client: TestClient, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    resp = client.post(
        f"/approvals/{ap.id}/reject", json={"approver": "dana", "reason": "Customer called us"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["decision_reason"] == "Customer called us"
    assert body["action"]["status"] == "rejected"
    assert db_session.scalar(select(func.count()).select_from(Outbox)) == 0


def test_decide_after_reject_is_409(client: TestClient, db_session: Session) -> None:
    ap = f.pending_approval(db_session)
    client.post(f"/approvals/{ap.id}/reject", json={"approver": "dana", "reason": "No thanks"})
    assert client.post(f"/approvals/{ap.id}/approve", json={"approver": "dana"}).status_code == 409
    resp = client.post(f"/approvals/{ap.id}/reject", json={"approver": "dana", "reason": "Again"})
    assert resp.status_code == 409


def test_approve_404(client: TestClient) -> None:
    resp = client.post(f"/approvals/{uuid.uuid4()}/approve", json={"approver": "dana"})
    assert resp.status_code == 404
    resp = client.post(f"/approvals/{uuid.uuid4()}/reject", json={"approver": "d", "reason": "abc"})
    assert resp.status_code == 404


def test_approve_non_message_action_executes_without_outbox(
    client: TestClient, db_session: Session
) -> None:
    ap = f.pending_approval(
        db_session, action_type=ActionType.ESCALATE_TO_HUMAN, subject=None, body=None, tone=None
    )
    resp = client.post(f"/approvals/{ap.id}/approve", json={"approver": "dana"})
    assert resp.status_code == 200
    assert resp.json()["action"]["status"] == "executed"
    assert db_session.scalar(select(func.count()).select_from(Outbox)) == 0


def test_approve_when_action_no_longer_proposed_is_409(
    client: TestClient, db_session: Session
) -> None:
    ap = f.pending_approval(db_session)
    ap.action.status = ActionStatus.REJECTED
    db_session.flush()
    assert client.post(f"/approvals/{ap.id}/approve", json={"approver": "dana"}).status_code == 409


def test_message_action_without_draft_cannot_be_sent(
    client: TestClient, db_session: Session
) -> None:
    ap = f.pending_approval(db_session, body=None)
    resp = client.post(f"/approvals/{ap.id}/approve", json={"approver": "dana"})
    assert resp.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(Outbox)) == 0
    db_session.expire_all()
    assert db_session.get(Action, ap.action_id).status is ActionStatus.PROPOSED


def test_execute_requires_approved_status(db_session: Session) -> None:
    import pytest

    from payops.errors import ConflictError

    ap = f.pending_approval(db_session)
    with pytest.raises(ConflictError):
        outbox_service.execute_action(db_session, ap.action)
