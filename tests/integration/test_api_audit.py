import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from payops.services.audit import record
from tests.integration import factories as f


def test_audit_filters_and_pagination(client: TestClient, db_session: Session) -> None:
    r1, r2 = f.run(db_session), f.run(db_session)
    for i in range(5):
        record(db_session, event_type="tool_call", name=f"t{i}", run_id=r1.id)
    record(db_session, event_type="llm_call", name="triage", run_id=r1.id)
    record(db_session, event_type="tool_call", name="other", run_id=r2.id)

    body = client.get("/audit", params={"run_id": str(r1.id)}).json()
    assert len(body["items"]) == 6
    assert body["items"][0]["name"] == "triage"  # newest first
    assert body["next_cursor"] is None

    body = client.get("/audit", params={"run_id": str(r1.id), "event_type": "tool_call"}).json()
    assert {i["name"] for i in body["items"]} == {f"t{i}" for i in range(5)}

    names: list[str] = []
    cursor = None
    while True:
        params = {"run_id": str(r1.id), "event_type": "tool_call", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/audit", params=params).json()
        names.extend(i["name"] for i in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert names == ["t4", "t3", "t2", "t1", "t0"]


def test_audit_limit_cap_and_bad_cursor(client: TestClient) -> None:
    assert client.get("/audit", params={"limit": 200}).status_code == 200
    assert client.get("/audit", params={"limit": 201}).status_code == 422
    resp = client.get("/audit", params={"cursor": "eyJpZCI6ICJ4In0"})  # {"id": "x"}
    assert resp.status_code == 422


def test_audit_unknown_run_is_empty(client: TestClient) -> None:
    body = client.get("/audit", params={"run_id": str(uuid.uuid4())}).json()
    assert body == {"items": [], "next_cursor": None}
