.DEFAULT_GOAL := help
.PHONY: help install up down migrate seed run lint fmt test test-unit test-int cov eval eval-compare

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-13s %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies with uv
	uv sync

up: ## Start Postgres (waits until healthy)
	docker compose up -d --wait

down: ## Stop containers (data volume is kept)
	docker compose down

migrate: ## Apply database migrations
	uv run alembic upgrade head

seed: ## Reset and seed the database with deterministic synthetic data
	uv run python -m scripts.seed

run: ## Start the API dev server on http://localhost:8000
	uv run uvicorn payops.main:create_app --factory --reload --host 127.0.0.1 --port 8000

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint issues and format
	uv run ruff check --fix .
	uv run ruff format .

test: ## Run all tests (integration tests need `make up`)
	uv run pytest

test-unit: ## Unit tests only: no database, no API keys
	uv run pytest tests/unit

test-int: ## Integration tests against Postgres (needs `make up`)
	uv run pytest tests/integration

cov: ## All tests with coverage for tools/, services/, api/ (fails under 85%)
	uv run pytest --cov=payops --cov-report=term-missing
	uv run coverage report --include='src/payops/tools/*,src/payops/services/*,src/payops/api/*' --fail-under=85

eval: ## Run the eval (resumable). Options: REPEATS=N DATASET=v1 LIMIT=N FRESH=1
	uv run python -m evals.runner $(if $(REPEATS),--repeats $(REPEATS)) $(if $(DATASET),--dataset $(DATASET)) $(if $(LIMIT),--limit $(LIMIT)) $(if $(FRESH),--fresh)

eval-compare: ## Diff two eval reports: make eval-compare A=evals/reports/x.md B=evals/reports/y.md
	uv run python -m evals.report $(A) $(B)
