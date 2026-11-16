"""Approvals page auth: login, signed session cookie, CSRF. No database needed."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from payops.api import security
from payops.config import get_settings
from tests.conftest import TEST_API_KEY
from tests.ui_helpers import csrf_from, login

APPROVE_URL = "/ui/approvals/3f2b8c1e-9a4d-4e7b-8f21-6c5d0a9e1b23/approve"


@pytest.fixture
def ui(raw_client: TestClient) -> TestClient:
    return raw_client


def _set_cookie_headers(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


def test_approvals_redirects_to_login_without_session(ui: TestClient) -> None:
    resp = ui.get("/ui/approvals", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


def test_login_page_renders_with_csrf_cookie_and_security_headers(ui: TestClient) -> None:
    resp = ui.get("/ui/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert csrf_from(resp.text)
    [cookie] = _set_cookie_headers(resp)
    assert cookie.startswith("payops_login_csrf=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"


def test_static_css_is_served(ui: TestClient) -> None:
    resp = ui.get("/ui/static/app.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


@pytest.mark.parametrize("token", ["", "wrong-token"], ids=["missing", "wrong"])
def test_login_without_valid_csrf_is_403(ui: TestClient, token: str) -> None:
    ui.get("/ui/login")  # sets the pre-login cookie
    resp = ui.post(
        "/ui/login",
        data={"api_key": TEST_API_KEY, "approver": "dana", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert "payops_session" not in ui.cookies


def test_login_csrf_token_needs_its_cookie(ui: TestClient) -> None:
    token = csrf_from(ui.get("/ui/login").text)
    ui.cookies.clear()
    resp = ui.post(
        "/ui/login",
        data={"api_key": TEST_API_KEY, "approver": "dana", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_login_with_wrong_key_is_401_and_sets_no_session(ui: TestClient) -> None:
    token = csrf_from(ui.get("/ui/login").text)
    resp = ui.post(
        "/ui/login",
        data={"api_key": TEST_API_KEY + "x", "approver": "dana", "csrf_token": token},
    )
    assert resp.status_code == 401
    assert "Invalid API key" in resp.text
    assert "payops_session" not in ui.cookies


def test_login_requires_a_name(ui: TestClient) -> None:
    token = csrf_from(ui.get("/ui/login").text)
    resp = ui.post(
        "/ui/login", data={"api_key": TEST_API_KEY, "approver": "   ", "csrf_token": token}
    )
    assert resp.status_code == 422
    assert "payops_session" not in ui.cookies


def test_login_sets_signed_httponly_strict_cookie_without_the_key(ui: TestClient) -> None:
    token = csrf_from(ui.get("/ui/login").text)
    resp = ui.post(
        "/ui/login",
        data={"api_key": TEST_API_KEY, "approver": "Dana", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    [session_cookie] = [c for c in _set_cookie_headers(resp) if c.startswith("payops_session=")]
    assert "HttpOnly" in session_cookie
    assert "samesite=strict" in session_cookie.lower()
    assert "Path=/ui" in session_cookie
    assert "Secure" not in session_cookie  # SESSION_COOKIE_SECURE defaults to false
    value = ui.cookies["payops_session"]
    assert TEST_API_KEY not in value
    # The payload is signed, not encrypted: check the decoded content too.
    session = security.read_session_cookie(value)
    assert session is not None
    assert session.approver == "Dana"
    assert TEST_API_KEY not in repr(session)
    # Logged in: the login page now forwards to the approvals page.
    again = ui.get("/ui/login", follow_redirects=False)
    assert again.status_code == 303


def test_tampered_session_cookie_is_rejected(ui: TestClient) -> None:
    login(ui)
    value = ui.cookies["payops_session"]
    ui.cookies.set("payops_session", value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
    resp = ui.get("/ui/approvals", follow_redirects=False)
    assert resp.status_code == 303


def test_expired_session_is_rejected(ui: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    login(ui)
    monkeypatch.setattr(security, "SESSION_MAX_AGE_SECONDS", -1)
    assert security.read_session_cookie(ui.cookies["payops_session"]) is None


@pytest.fixture
def restore_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("restore_settings")
def test_rotating_the_api_key_ends_sessions(
    ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(ui)
    value = ui.cookies["payops_session"]
    assert security.read_session_cookie(value) is not None
    monkeypatch.setenv("API_KEY", "rotated-key-0123456789-abcdefghijklmnopqr")
    get_settings.cache_clear()
    assert security.read_session_cookie(value) is None


@pytest.mark.parametrize("token", [None, "", "not-the-token"], ids=["absent", "empty", "wrong"])
def test_form_post_without_valid_csrf_is_403(ui: TestClient, token: str | None) -> None:
    login(ui)
    data = {} if token is None else {"csrf_token": token}
    # The unit-test app's DB session raises if touched: CSRF fails before any DB work.
    resp = ui.post(APPROVE_URL, data=data, follow_redirects=False)
    assert resp.status_code == 403
    assert "form token" in resp.text


@pytest.mark.parametrize(
    "path", [APPROVE_URL, APPROVE_URL.replace("approve", "reject"), "/ui/runs", "/ui/logout"]
)
def test_every_form_post_checks_csrf(ui: TestClient, path: str) -> None:
    login(ui)
    resp = ui.post(path, data={"csrf_token": "forged", "reason": "nope"}, follow_redirects=False)
    assert resp.status_code == 403


def test_csrf_token_from_another_session_is_rejected(ui: TestClient) -> None:
    other = login(ui, "Mallory")
    ui.cookies.clear()
    login(ui, "Dana")
    resp = ui.post(APPROVE_URL, data={"csrf_token": other}, follow_redirects=False)
    assert resp.status_code == 403


def test_logout_clears_the_session(ui: TestClient) -> None:
    csrf = login(ui)
    resp = ui.post("/ui/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login?notice=logged_out"
    assert "payops_session" not in ui.cookies
    assert ui.get("/ui/approvals", follow_redirects=False).status_code == 303
    assert "Signed out." in ui.get("/ui/login?notice=logged_out").text


def test_notice_codes_are_not_reflected(ui: TestClient) -> None:
    resp = ui.get("/ui/login", params={"notice": "<script>alert(1)</script>"})
    assert "alert(1)" not in resp.text
