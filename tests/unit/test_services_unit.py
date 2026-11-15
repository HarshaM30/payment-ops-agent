import pytest

from payops.errors import InvalidInputError
from payops.services.audit import REDACTED, TokenUsage, redact, to_json
from payops.services.pagination import decode_cursor, encode_cursor


def test_cursor_roundtrip() -> None:
    cur = encode_cursor({"id": 42})
    assert decode_cursor(cur, {"id"}) == {"id": 42}


@pytest.mark.parametrize("bad", ["!!!", "bm90LWpzb24", "WzFd", encode_cursor({"other": 1})])
def test_invalid_cursor(bad: str) -> None:
    with pytest.raises(InvalidInputError):
        decode_cursor(bad, {"id"})


def test_redact_nested() -> None:
    data = {"a": 1, "API_KEY": "s3cret", "nested": [{"Authorization": "Bearer x", "ok": True}]}
    assert redact(data) == {
        "a": 1,
        "API_KEY": REDACTED,
        "nested": [{"Authorization": REDACTED, "ok": True}],
    }


def test_to_json_handles_models_and_decimals() -> None:
    from decimal import Decimal

    from pydantic import BaseModel

    class M(BaseModel):
        amount: Decimal
        token: str

    assert to_json(M(amount=Decimal("1.50"), token="t")) == {"amount": "1.50", "token": REDACTED}


def test_token_usage_total() -> None:
    assert TokenUsage(prompt_tokens=10, completion_tokens=5).total_tokens == 15
