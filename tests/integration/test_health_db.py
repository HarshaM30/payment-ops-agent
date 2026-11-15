from fastapi.testclient import TestClient


def test_health_with_real_db(client: TestClient) -> None:
    resp = client.get("/health", headers={"X-API-Key": ""})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["db"] == "ok"
