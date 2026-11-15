"""Request IDs, request timeouts, DB-unavailable mapping, startup recovery, fake latency."""

import asyncio
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

import payops.agents.runner as agent_runner
import payops.main as main
from payops.config import get_settings
from payops.llm import fake as fake_module
from payops.llm.client import LLMRequest
from payops.llm.fake import FakeLLM
from payops.logging import JsonFormatter, request_id_var


def make_app(monkeypatch: pytest.MonkeyPatch, **settings: object) -> FastAPI:
    patched = get_settings().model_copy(update=settings)
    monkeypatch.setattr(main, "get_settings", lambda: patched)
    app = main.create_app()

    @app.get("/_test/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(2)
        return {"ok": "late"}

    @app.get("/_test/db-down")
    def db_down() -> None:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))

    @app.get("/_test/pool-exhausted")
    def pool_exhausted() -> None:
        raise PoolTimeoutError("QueuePool limit reached")

    @app.get("/_test/whoami")
    def whoami() -> dict[str, str | None]:
        return {"request_id": request_id_var.get()}

    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with TestClient(make_app(monkeypatch, request_timeout_seconds=0.2)) as c:
        yield c


# ---------------------------------------------------------------- request id


def test_request_id_is_generated_and_returned(client: TestClient) -> None:
    resp = client.get("/_test/whoami")
    rid = resp.headers["x-request-id"]
    assert len(rid) == 32
    assert resp.json() == {"request_id": rid}  # visible to handlers in the threadpool


def test_valid_incoming_request_id_is_reused(client: TestClient) -> None:
    resp = client.get("/_test/whoami", headers={"X-Request-ID": "proxy-abc.123"})
    assert resp.headers["x-request-id"] == "proxy-abc.123"


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "semi;colon", "<script>"])
def test_malformed_request_id_is_replaced(client: TestClient, bad: str) -> None:
    resp = client.get("/_test/whoami", headers={"X-Request-ID": bad})
    assert resp.headers["x-request-id"] != bad
    assert len(resp.headers["x-request-id"]) == 32


def test_request_id_is_in_the_request_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="payops.request"):
        client.get("/health", headers={"X-Request-ID": "trace-me-1"})
    (record,) = [r for r in caplog.records if r.name == "payops.request"]
    assert record.request_id == "trace-me-1"
    assert json.loads(JsonFormatter().format(record))["request_id"] == "trace-me-1"


def test_formatter_adds_request_id_from_context() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    token = request_id_var.set("rid-42")
    try:
        assert json.loads(JsonFormatter().format(record))["request_id"] == "rid-42"
    finally:
        request_id_var.reset(token)
    assert "request_id" not in json.loads(JsonFormatter().format(record))


# ---------------------------------------------------------------- timeouts and DB errors


def test_slow_request_gets_504_with_uniform_error(client: TestClient) -> None:
    resp = client.get("/_test/slow")
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "timeout"
    assert "x-request-id" in resp.headers


@pytest.mark.parametrize("path", ["/_test/db-down", "/_test/pool-exhausted"])
def test_database_errors_are_503_not_500(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    body = resp.json()["error"]
    assert body["code"] == "service_unavailable"
    assert "refused" not in body["message"]  # no driver internals leak to clients


# ---------------------------------------------------------------- startup recovery


def test_startup_runs_recovery_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(agent_runner, "recover_interrupted_runs", lambda: calls.append(1) or 0)
    with TestClient(make_app(monkeypatch, run_recovery_on_startup=True)):
        pass
    assert calls == [1]


def test_startup_survives_recovery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise OperationalError("SELECT", {}, ConnectionRefusedError())

    monkeypatch.setattr(agent_runner, "recover_interrupted_runs", boom)
    with TestClient(make_app(monkeypatch, run_recovery_on_startup=True)) as c:
        assert c.get("/_test/whoami").status_code == 200


def test_startup_recovery_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_runner, "recover_interrupted_runs", lambda: pytest.fail("must not run")
    )
    with TestClient(make_app(monkeypatch, run_recovery_on_startup=False)):
        pass


# ---------------------------------------------------------------- fake latency


def test_fake_llm_simulated_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(fake_module.time, "sleep", slept.append)
    request = LLMRequest(
        "research", [{"role": "user", "content": "x"}], context={"customer_id": "c"}
    )
    FakeLLM(latency_seconds=0.25).complete(request, timeout=5)
    FakeLLM().complete(request, timeout=5)
    assert slept == [0.25]
