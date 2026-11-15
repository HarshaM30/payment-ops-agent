from fastapi.testclient import TestClient

from payops import __version__
from payops.api.routes.health import get_db_check
from payops.main import create_app


def _client(check) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db_check] = lambda: check
    return TestClient(app)


def test_health_ok_when_db_reachable() -> None:
    resp = _client(lambda: None).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok", "version": __version__}


def test_health_503_when_db_down() -> None:
    def down() -> None:
        raise ConnectionError("db down")

    resp = _client(down).get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"
    assert resp.json()["status"] == "degraded"


def test_unknown_route_uses_uniform_error_shape() -> None:
    resp = _client(lambda: None).get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
