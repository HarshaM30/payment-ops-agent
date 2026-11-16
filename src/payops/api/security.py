"""API key authentication, plus the signed session and CSRF tokens of the HTML pages.

Every JSON router except `/health` depends on `require_api_key`. The key is compared in
constant time and is never logged or written to the audit log.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from itsdangerous import BadSignature, URLSafeTimedSerializer

from payops.config import get_settings

API_KEY_HEADER = "X-API-Key"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Must match the server's API_KEY environment variable.",
)


def require_api_key(provided: str | None = Security(api_key_header)) -> None:
    if not check_api_key(provided or ""):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )


# ---------------------------------------------------------------------------- UI session
#
# The approvals page logs in with the API key once. The server then sets a signed,
# HttpOnly, SameSite=Strict cookie holding the approver's name and a random CSRF token.
# The key itself is never stored in the cookie: only a keyed fingerprint of it, so that
# rotating API_KEY logs every browser out. Forms echo the CSRF token in a hidden field,
# and the server compares it (in constant time) with the token in the signed cookie.

SESSION_COOKIE = "payops_session"
LOGIN_CSRF_COOKIE = "payops_login_csrf"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
LOGIN_CSRF_MAX_AGE_SECONDS = 30 * 60
_SESSION_SALT = "payops.ui.session"
_LOGIN_SALT = "payops.ui.login-csrf"


@dataclass(frozen=True)
class UISession:
    approver: str
    csrf_token: str


def _serializer(salt: str) -> URLSafeTimedSerializer:
    secret = get_settings().session_secret
    if secret is None:  # only possible with APP_ENV=test and no SESSION_SECRET
        raise RuntimeError("SESSION_SECRET is not configured")
    return URLSafeTimedSerializer(secret.get_secret_value(), salt=salt)


def check_api_key(provided: str) -> bool:
    """Constant-time comparison against API_KEY (shared by the header and the login form)."""
    expected = get_settings().api_key
    return (
        bool(provided)
        and expected is not None
        and secrets.compare_digest(
            provided.encode("utf-8"), expected.get_secret_value().encode("utf-8")
        )
    )


def _key_fingerprint() -> str:
    """A keyed hash of API_KEY: changes when the key rotates, reveals nothing about it."""
    settings = get_settings()
    key = settings.api_key.get_secret_value() if settings.api_key else ""
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    return hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()[:32]


def new_session_cookie(approver: str) -> str:
    return _serializer(_SESSION_SALT).dumps(
        {"approver": approver, "csrf": secrets.token_urlsafe(32), "kf": _key_fingerprint()}
    )


def read_session_cookie(value: str | None) -> UISession | None:
    """The session in a cookie, or None if missing, tampered with, expired, or stale."""
    if not value:
        return None
    try:
        data = _serializer(_SESSION_SALT).loads(value, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:  # includes SignatureExpired
        return None
    if not isinstance(data, dict):
        return None
    approver, csrf, kf = data.get("approver"), data.get("csrf"), data.get("kf")
    if not (isinstance(approver, str) and isinstance(csrf, str) and isinstance(kf, str)):
        return None
    if not secrets.compare_digest(kf, _key_fingerprint()):
        return None
    return UISession(approver=approver, csrf_token=csrf)


def new_login_csrf() -> tuple[str, str]:
    """(token for the form, signed value for the pre-login cookie)."""
    token = secrets.token_urlsafe(32)
    return token, _serializer(_LOGIN_SALT).dumps(token)


def check_login_csrf(cookie_value: str | None, form_token: str) -> bool:
    if not cookie_value or not form_token:
        return False
    try:
        token = _serializer(_LOGIN_SALT).loads(cookie_value, max_age=LOGIN_CSRF_MAX_AGE_SECONDS)
    except BadSignature:
        return False
    return isinstance(token, str) and secrets.compare_digest(token, form_token)


def tokens_match(expected: str, provided: str) -> bool:
    return bool(provided) and secrets.compare_digest(
        expected.encode("utf-8"), provided.encode("utf-8")
    )
