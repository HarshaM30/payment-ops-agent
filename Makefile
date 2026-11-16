.DEFAULT_GOAL := help
.PHONY: help install demo demo-gif up db down migrate seed run lint fmt test test-unit test-int cov eval eval-compare bench

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-13s %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies with uv
	uv sync

DEMO_LATENCY_MS ?= 150
demo: ## One command: .env secrets, full stack with the fake LLM, seed data, approvals page
	python3 scripts/demo_env.py
	LLM_PROVIDER=fake FAKE_LLM_LATENCY_MS=$(DEMO_LATENCY_MS) docker compose up -d --wait --build
	docker compose exec -T api python -m scripts.seed
	@echo ""
	@echo "Ready: open http://localhost:8000/ui/login and sign in with the API_KEY from .env"
	@echo "(LLM_PROVIDER=fake: no API key needed; stop with 'make down')"

demo-gif: ## Re-record docs/demo.gif from the running demo (needs Google Chrome)
	uv run --with playwright --with pillow python -m scripts.record_demo

up: ## Start the full stack, Postgres + API on :8000 (builds the image; waits until healthy)
	docker compose up -d --wait --build

db: ## Start only Postgres (for `make run` on the Mac and the integration tests)
	docker compose up -d --wait db

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

test: ## Run all tests (integration tests need `make db` or `make up`)
	uv run pytest

test-unit: ## Unit tests only: no database, no API keys
	uv run pytest tests/unit

test-int: ## Integration tests against Postgres (needs `make db` or `make up`)
	uv run pytest tests/integration

cov: ## All tests with coverage for tools/, services/, api/ (fails under 85%)
	uv run pytest --cov=payops --cov-report=term-missing
	uv run coverage report --include='src/payops/tools/*,src/payops/services/*,src/payops/api/*' --fail-under=85

eval: ## Run the eval (resumable). Options: REPEATS=N DATASET=v1 LIMIT=N FRESH=1
	uv run python -m evals.runner $(if $(REPEATS),--repeats $(REPEATS)) $(if $(DATASET),--dataset $(DATASET)) $(if $(LIMIT),--limit $(LIMIT)) $(if $(FRESH),--fresh)

eval-compare: ## Diff two eval reports: make eval-compare A=evals/reports/x.md B=evals/reports/y.md
	uv run python -m evals.report $(A) $(B)

USERS ?= 50
SPAWN ?= 5
TIME ?= 5m
bench: ## Locust load test against the running stack (start it with LLM_PROVIDER=fake)
	uv run --env-file .env locust -f benchmarks/locustfile.py --host http://localhost:8000 \
		--headless -u $(USERS) -r $(SPAWN) -t $(TIME) --csv benchmarks/locust --only-summary
