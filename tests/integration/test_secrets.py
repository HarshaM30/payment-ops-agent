"""The API key must never appear in logs, the audit log, or responses."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payops.db.models import AuditLog
from tests.conftest import TEST_API_KEY
from tests.integration import factories as f


def test_api_key_never_logged_or_audited(
    client: TestClient,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level("DEBUG")
    wrong_key = "attacker-guess-0123456789-abcdefghij"
    ap = f.pending_approval(db_session)
    inv = f.invoice(db_session, f.customer(db_session))

    responses = [
        client.get("/health"),
        client.post("/runs", json={"invoice_ids": [str(inv.id)]}),
        client.get("/approvals"),
        client.post(f"/approvals/{ap.id}/approve", json={"approver": "dana"}),
        client.get("/audit"),
        client.get("/audit", headers={"X-API-Key": wrong_key}),
        client.post("/runs", json={"bad": True}),
    ]
    assert [r.status_code for r in responses] == [200, 202, 200, 200, 200, 401, 422]

    out = capsys.readouterr()
    logs = caplog.text + out.out + out.err
    assert "request" in logs, "request logging should be active during this test"
    audit_rows = db_session.scalars(select(AuditLog)).all()
    assert audit_rows
    audit_dump = json.dumps(
        [[r.inputs, r.outputs, r.error, r.actor, r.name] for r in audit_rows], default=str
    )
    for secret in (TEST_API_KEY, wrong_key):
        assert secret not in logs
        assert secret not in audit_dump
        for r in responses:
            assert secret not in r.text
