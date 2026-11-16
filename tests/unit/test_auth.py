"""Every route except /health must require the API key; every HTML page a session.

The route list comes from the app itself, so a newly added route is covered
automatically and fails here if it forgets the auth dependency.
"""

import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from payops.api.routes.health import get_db_check
from payops.main import create_app
from tests.conftest import TEST_API_KEY

OPEN_PATHS = {"/health"}
UI_PREFIX = "/ui"
# The login page is the only HTML page reachable without a session.
UI_OPEN = {("GET", "/ui/login"), ("POST", "/ui/login")}


def _walk(routes: list, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Yield (full_path, route) for every APIRoute, including nested routers.

    FastAPI >= 0.16x keeps included routers as lazy wrappers holding `original_router`;
    older versions flatten them into `app.routes`. Handle both.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            ctx_prefix = getattr(getattr(route, "include_context", None), "prefix", "")
            yield from _walk(route.original_router.routes, prefix + ctx_prefix)


def _routes() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(API routes needing the key, HTML routes needing a session)."""
    app = create_app()
    walked = list(_walk(app.routes))
    # Cross-check against the public OpenAPI paths so a silent walk failure can't pass.
    assert {p for p, r in walked if r.include_in_schema} == set(app.openapi()["paths"])
    api, ui = [], []
    for full_path, route in walked:
        if full_path in OPEN_PATHS:
            continue
        is_ui = full_path == UI_PREFIX or full_path.startswith(UI_PREFIX + "/")
        # HTML pages are hidden from the OpenAPI docs; every API route is in them.
        assert route.include_in_schema is not is_ui, full_path
        path = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), full_path)
        for method in sorted(route.methods):
            if is_ui:
                if (method, full_path) not in UI_OPEN:
                    ui.append((method, path))
            else:
                api.append((method, path))
    return api, ui


PROTECTED, UI_PROTECTED = _routes()


def test_route_discovery_is_not_vacuous() -> None:
    paths = {p.split("/")[1] for _, p in PROTECTED}
    assert {"runs", "approvals", "audit"} <= paths
    assert len(PROTECTED) >= 6
    ui_paths = {p for _, p in UI_PROTECTED}
    assert {"/ui/approvals", "/ui/outbox", "/ui/runs"} <= ui_paths
    assert any(p.endswith("/approve") for p in ui_paths)


@pytest.mark.parametrize(("method", "path"), UI_PROTECTED)
@pytest.mark.parametrize(
    "cookie",
    [None, "garbage", "eyJhcHByb3ZlciI6ImV2ZSJ9.forged.signature"],
    ids=["none", "garbage", "forged"],
)
def test_ui_route_requires_session(
    raw_client: TestClient, method: str, path: str, cookie: str | None
) -> None:
    if cookie is not None:
        raw_client.cookies.set("payops_session", cookie)
    resp = raw_client.request(method, path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "wrong-key"}, {"X-API-Key": ""}, {"X-API-Key": TEST_API_KEY + "x"}],
    ids=["missing", "wrong", "empty", "suffix"],
)
def test_protected_route_rejects_bad_key(
    raw_client: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    resp = raw_client.request(method, path, headers=headers, json={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    assert set(body["error"]) == {"code", "message", "details"}


def test_health_is_open(app) -> None:
    app.dependency_overrides[get_db_check] = lambda: lambda: None
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


def test_valid_key_passes_auth(raw_client: TestClient) -> None:
    # A bad path param with a valid key gets past auth and fails validation instead.
    resp = raw_client.get("/runs/not-a-uuid", headers={"X-API-Key": TEST_API_KEY})
    assert resp.status_code == 422
