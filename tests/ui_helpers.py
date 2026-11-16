"""Log in through the HTML form, the way a browser does."""

import re

from fastapi.testclient import TestClient

from tests.conftest import TEST_API_KEY

_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    match = _CSRF.search(html)
    assert match, "no csrf_token field on the page"
    return match.group(1)


def login(client: TestClient, approver: str = "Dana Approver") -> str:
    """Sign in and return the session's CSRF token (as printed on the approvals page)."""
    page = client.get("/ui/login")
    assert page.status_code == 200
    resp = client.post(
        "/ui/login",
        data={"api_key": TEST_API_KEY, "approver": approver, "csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/ui/approvals"
    return session_csrf(client)


def session_csrf(client: TestClient) -> str:
    """The CSRF token of the current session, read from the signed cookie."""
    from payops.api.security import read_session_cookie

    session = read_session_cookie(client.cookies.get("payops_session"))
    assert session is not None
    return session.csrf_token
