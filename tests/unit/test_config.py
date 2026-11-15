import pytest
from pydantic import ValidationError

from payops.config import Settings

GOOD_KEY = "k" * 40
GOOD_SECRET = "s" * 40


def make(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "development",
        "database_url": "postgresql+psycopg://u:p@localhost/db",
        "api_key": GOOD_KEY,
        "session_secret": GOOD_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_valid_settings_load() -> None:
    s = make()
    assert s.api_key is not None
    assert s.api_key.get_secret_value() == GOOD_KEY


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_key": None}, "API_KEY is required"),
        ({"session_secret": None}, "SESSION_SECRET is required"),
        ({"api_key": "short"}, "at least 32"),
        ({"api_key": "replace-with-a-long-random-string-xxxxxxxx"}, "placeholder"),
        ({"session_secret": GOOD_KEY}, "must be different"),
        ({"app_env": "production", "api_key": ""}, "API_KEY is required"),
    ],
)
def test_invalid_secrets_fail_fast(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object], message: str
) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ValidationError, match=message):
        make(**overrides)


def test_test_env_allows_missing_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    s = make(app_env="test", api_key=None, session_secret=None)
    assert s.api_key is None


def test_invalid_values_rejected() -> None:
    with pytest.raises(ValidationError):
        make(api_port=70000)
    with pytest.raises(ValidationError):
        make(llm_provider="openai")


def test_secret_not_in_repr() -> None:
    assert GOOD_KEY not in repr(make())
