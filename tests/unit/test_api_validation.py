"""Invalid input returns 422 with the uniform error shape, before touching the DB."""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_API_KEY

U = str(uuid.uuid4())

CASES = [
    # POST /runs
    ("POST", "/runs", {}),
    ("POST", "/runs", {"invoice_ids": []}),
    ("POST", "/runs", {"invoice_ids": ["not-a-uuid"]}),
    ("POST", "/runs", {"invoice_ids": [U, U]}),
    ("POST", "/runs", {"scope": "everything"}),
    ("POST", "/runs", {"scope": "all_overdue", "invoice_ids": [U]}),
    ("POST", "/runs", {"scope": "all_overdue", "max_invoices": 0}),
    ("POST", "/runs", {"scope": "all_overdue", "max_invoices": 501}),
    ("POST", "/runs", {"scope": "all_overdue", "unexpected": 1}),
    ("POST", "/runs", None),
    # GET /runs/{id}
    ("GET", "/runs/not-a-uuid", None),
    # GET /approvals
    ("GET", "/approvals?status=bogus", None),
    ("GET", "/approvals?limit=0", None),
    ("GET", "/approvals?limit=201", None),
    # approve / reject
    ("POST", "/approvals/not-a-uuid/approve", {"approver": "dana"}),
    ("POST", f"/approvals/{U}/approve", {}),
    ("POST", f"/approvals/{U}/approve", {"approver": ""}),
    ("POST", f"/approvals/{U}/approve", {"approver": "   "}),
    ("POST", f"/approvals/{U}/approve", {"approver": "x" * 101}),
    ("POST", f"/approvals/{U}/approve", {"approver": "dana", "extra": True}),
    ("POST", f"/approvals/{U}/reject", {"approver": "dana"}),
    ("POST", f"/approvals/{U}/reject", {"approver": "dana", "reason": ""}),
    ("POST", "/approvals/not-a-uuid/reject", {"approver": "dana", "reason": "dup"}),
    # GET /audit
    ("GET", "/audit?limit=500", None),
    ("GET", "/audit?run_id=nope", None),
    ("GET", "/audit?event_type=", None),
    ("GET", "/audit?cursor=" + "x" * 501, None),
]


@pytest.mark.parametrize(("method", "path", "body"), CASES)
def test_invalid_input_returns_422(
    raw_client: TestClient, method: str, path: str, body: object
) -> None:
    resp = raw_client.request(method, path, json=body, headers={"X-API-Key": TEST_API_KEY})
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert err["message"]
    assert err["details"], "details should say which field failed"
    assert all({"loc", "msg", "type"} <= d.keys() for d in err["details"])


def test_validation_errors_do_not_echo_input(raw_client: TestClient) -> None:
    resp = raw_client.post(
        "/runs",
        json={"scope": "all_overdue", "secret_field": "sensitive-value"},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 422
    assert "sensitive-value" not in resp.text
