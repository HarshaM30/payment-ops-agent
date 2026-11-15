"""Shared test setup. Environment is pinned *before* any payops module reads settings."""

import os

TEST_API_KEY = "test-api-key-0123456789-abcdefghijklmnop"

os.environ["APP_ENV"] = "test"
os.environ["API_KEY"] = TEST_API_KEY
os.environ["SESSION_SECRET"] = "test-session-secret-0123456789-abcdefgh"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["LLM_MODEL"] = ""  # provider default (fake-v1), whatever .env says
os.environ["LOG_LEVEL"] = "INFO"
os.environ["RUN_RECOVERY_ON_STARTUP"] = "false"  # never touch the dev DB at app startup
os.environ["FAKE_LLM_LATENCY_MS"] = "0"
# Unit tests never connect; this only satisfies validation when no .env exists (CI).
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://payops:payops@localhost:5432/payops")
