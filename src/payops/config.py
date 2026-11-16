"""Application settings, loaded from environment variables (and `.env` if present).

Settings are validated on load so a misconfigured process fails at startup with a
clear message instead of failing later on the first request.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Requests taking longer get a 504 (the work itself is not cancelled; see main.py).
    request_timeout_seconds: float = Field(default=30, gt=0, le=600)
    # At startup, mark runs left queued/running by a previous process as failed. Runs
    # execute in-process, so they cannot survive a restart (one API process per DB).
    run_recovery_on_startup: bool = True
    # If a run crashes and even recording the failure fails (e.g. the database is down),
    # keep retrying to mark it failed for this long.
    run_fail_retry_seconds: float = Field(default=120, ge=0, le=3600)

    # API security
    api_key: SecretStr | None = None
    session_secret: SecretStr | None = None
    # Mark the approvals-page session cookie Secure (HTTPS only). Leave off for http://localhost.
    session_cookie_secure: bool = False

    # Database
    database_url: str = Field(min_length=1)
    test_database_url: str | None = None
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10, gt=0, le=120)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=15_000, ge=0, le=600_000)  # 0 disables

    # LLM
    llm_provider: Literal["groq", "gemini", "ollama", "fake"] = "fake"
    # Empty means "use the provider default" from payops.llm.models.
    llm_model: str | None = None
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_timeout_seconds: float = Field(default=30, gt=0, le=600)
    llm_max_retries: int = Field(default=5, ge=0, le=20)
    llm_backoff_base_seconds: float = Field(default=1.0, ge=0, le=60)
    llm_backoff_max_seconds: float = Field(default=60, ge=0, le=600)
    llm_requests_per_minute: int = Field(default=25, ge=0, le=10_000)  # 0 disables
    llm_tokens_per_minute: int = Field(default=5000, ge=0, le=10_000_000)  # 0 disables
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    ollama_api_base: str = "http://localhost:11434"
    # Simulated per-call latency for the fake provider (load tests, demos). 0 = instant.
    fake_llm_latency_ms: int = Field(default=0, ge=0, le=60_000)

    # Agent
    prompt_version: str = Field(default="v1", pattern=r"^v\d+$")
    research_max_steps: int = Field(default=5, ge=1, le=20)

    # Policy
    approval_threshold_amount: Decimal = Field(default=Decimal("10000"), gt=0)
    default_currency: str = Field(default="USD", min_length=3, max_length=3)

    # Seed
    seed_random_seed: int = 42
    seed_num_customers: int = Field(default=60, ge=30, le=10_000)

    # Evaluation. Empty EVAL_DATABASE_URL means DATABASE_URL with an "_eval" suffix on
    # the database name: the runner wipes it before every scenario.
    eval_database_url: str | None = None
    eval_dataset_version: str = Field(default="v1", pattern=r"^v\d+$")
    eval_repeats: int = Field(default=1, ge=1, le=20)
    eval_reports_dir: str = "evals/reports"

    @field_validator("llm_model", "eval_database_url", mode="before")
    @classmethod
    def _blank_model_is_default(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

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
