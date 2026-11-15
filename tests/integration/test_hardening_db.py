"""Idempotent POST /runs, and runs that crash or are interrupted end up ``failed``."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import payops.agents.runner as agent_runner
from payops.api.routes.runs import get_run_executor
from payops.db.models import AgentRun, AuditLog
from payops.db.models.enums import RunStatus
from tests.integration import factories as f


@pytest.fixture
def executed(client: TestClient) -> list[uuid.UUID]:
    calls: list[uuid.UUID] = []
    client.app.dependency_overrides[get_run_executor] = lambda: calls.append
    return calls


def test_same_key_same_body_returns_the_original_run(
    client: TestClient, db_session: Session, executed: list[uuid.UUID]
) -> None:
    body = {"scope": "all_overdue", "max_invoices": 3}
    headers = {"Idempotency-Key": "retry-7f3a"}
    first = client.post("/runs", json=body, headers=headers)
    second = client.post("/runs", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert executed == [uuid.UUID(first.json()["run_id"])]  # executed once, not twice
    run = db_session.get(AgentRun, uuid.UUID(first.json()["run_id"]))
    assert run.idempotency_key == "retry-7f3a"


def test_same_key_different_body_is_409(client: TestClient, executed: list[uuid.UUID]) -> None:
    headers = {"Idempotency-Key": "k-1"}
    assert client.post("/runs", json={"scope": "all_overdue"}, headers=headers).status_code == 202
    resp = client.post("/runs", json={"scope": "all_overdue", "max_invoices": 1}, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert len(executed) == 1


def test_without_key_every_post_is_a_new_run(client: TestClient, executed: list[uuid.UUID]) -> None:
    a = client.post("/runs", json={"scope": "all_overdue"}).json()["run_id"]
    b = client.post("/runs", json={"scope": "all_overdue"}).json()["run_id"]
    assert a != b
    assert len(executed) == 2


@pytest.mark.parametrize("key", ["", "x" * 201, "has space", "tab\tkey"])
def test_malformed_idempotency_key_is_422(client: TestClient, key: str) -> None:
    resp = client.post("/runs", json={"scope": "all_overdue"}, headers={"Idempotency-Key": key})
    assert resp.status_code == 422


# ---------------------------------------------------------------- failure capture


def _sessions(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )


def test_crashed_run_is_marked_failed_once_the_db_is_back(db_session: Session) -> None:
    run = f.run(db_session)
    run.status = RunStatus.RUNNING
    db_session.flush()
    attempts = {"n": 0}

    def flaky() -> sessionmaker[Session]:
        attempts["n"] += 1
        if attempts["n"] <= 2:  # the database is still down for two attempts
            raise OperationalError("connect", {}, ConnectionRefusedError())
        return _sessions(db_session)

    slept: list[float] = []
    ok = agent_runner.mark_failed_with_retry(
        run.id, "OperationalError: server closed the connection", sessions=flaky,
        deadline_seconds=60, sleep=slept.append,
    )  # fmt: skip
    assert ok
    assert slept == [1.0, 2.0]  # exponential backoff between attempts
    db_session.expire_all()
    run = db_session.get(AgentRun, run.id)
    assert run.status is RunStatus.FAILED
    assert "server closed the connection" in run.error
    assert run.finished_at is not None


def test_mark_failed_gives_up_after_the_deadline(db_session: Session) -> None:
    run = f.run(db_session)
    clock = {"t": 0.0}

    def down() -> sessionmaker[Session]:
        raise OperationalError("connect", {}, ConnectionRefusedError())

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    ok = agent_runner.mark_failed_with_retry(
        run.id, "x", sessions=down, deadline_seconds=10, sleep=sleep, monotonic=lambda: clock["t"]
    )
    assert not ok
    assert clock["t"] <= 10


def test_run_in_background_never_raises_and_records_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = f.run(db_session)
    run.status = RunStatus.RUNNING
    db_session.flush()

    def explode(session: Session, run_id: uuid.UUID) -> None:
        raise OperationalError("SELECT", {}, ConnectionResetError("terminating connection"))

    monkeypatch.setattr(agent_runner, "execute_run", explode)
    agent_runner.run_in_background(run.id, sessions=lambda: _sessions(db_session))
    db_session.expire_all()
    failed = db_session.get(AgentRun, run.id)
    assert failed.status is RunStatus.FAILED
    assert failed.error.startswith("OperationalError")
    events = db_session.query(AuditLog.event_type).filter(AuditLog.run_id == run.id).all()
    assert ("run_failed",) in events


def test_mark_failed_leaves_finished_runs_alone(db_session: Session) -> None:
    run = f.run(db_session)
    run.status = RunStatus.COMPLETED
    db_session.flush()
    assert not agent_runner.mark_failed(_sessions(db_session)(), run.id, "late failure")
    db_session.expire_all()
    assert db_session.get(AgentRun, run.id).status is RunStatus.COMPLETED


def test_startup_recovery_fails_orphaned_runs(db_session: Session) -> None:
    queued, running, done = f.run(db_session), f.run(db_session), f.run(db_session)
    running.status = RunStatus.RUNNING
    done.status = RunStatus.AWAITING_APPROVAL
    db_session.flush()

    recovered = agent_runner.recover_interrupted_runs(lambda: _sessions(db_session))
    assert recovered == 2
    db_session.expire_all()
    for run in (queued, running):
        r = db_session.get(AgentRun, run.id)
        assert r.status is RunStatus.FAILED
        assert r.error == agent_runner.INTERRUPTED_ERROR
    assert db_session.get(AgentRun, done.id).status is RunStatus.AWAITING_APPROVAL
