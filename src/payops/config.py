"""Application settings, loaded from environment variables (and `.env` if present).

Settings are validated on load so a misconfigured process fails at startup with a
clear message instead of failing later on the first request.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32
_PLACEHOLDER_PREFIXES = ("replace-with", "change-me", "your-")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside a container / dev machine
    api_port: int = Field(default=8000, ge=1, le=65535)

    # API security
    api_key: SecretStr | None = None
    session_secret: SecretStr | None = None

    # Database
    database_url: str = Field(min_length=1)
    test_database_url: str | None = None
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)

    # LLM (used from Milestone 3)
    llm_provider: Literal["groq", "gemini", "ollama", "fake"] = "fake"
    llm_model: str = "placeholder"

    # Policy
    approval_threshold_amount: Decimal = Field(default=Decimal("10000"), gt=0)
    default_currency: str = Field(default="USD", min_length=3, max_length=3)

    # Seed
    seed_random_seed: int = 42
    seed_num_customers: int = Field(default=60, ge=30, le=10_000)

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        if self.app_env == "test":
            return self
        for name in ("api_key", "session_secret"):
            value: SecretStr | None = getattr(self, name)
            raw = value.get_secret_value() if value else ""
            env_name = name.upper()
            if not raw:
                raise ValueError(f"{env_name} is required (APP_ENV={self.app_env})")
            if len(raw) < MIN_SECRET_LENGTH:
                raise ValueError(f"{env_name} must be at least {MIN_SECRET_LENGTH} characters")
            if raw.lower().startswith(_PLACEHOLDER_PREFIXES):
                raise ValueError(f"{env_name} still has the placeholder value from .env.example")
        if (
            self.api_key
            and self.session_secret
            and self.api_key.get_secret_value() == self.session_secret.get_secret_value()
        ):
            raise ValueError("API_KEY and SESSION_SECRET must be different values")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
