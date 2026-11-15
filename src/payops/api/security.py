"""API key authentication.

Every router except `/health` depends on `require_api_key`. The key is compared in
constant time and is never logged or written to the audit log.
"""

import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from payops.config import get_settings

API_KEY_HEADER = "X-API-Key"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Must match the server's API_KEY environment variable.",
)


def require_api_key(provided: str | None = Security(api_key_header)) -> None:
    expected = get_settings().api_key
    if (
        not provided
        or expected is None
        or not secrets.compare_digest(
            provided.encode("utf-8"), expected.get_secret_value().encode("utf-8")
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
