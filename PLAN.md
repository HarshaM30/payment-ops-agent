# PLAN: AI-Powered Payment Operations Agent

Working plan for building the project. All data is synthetic; nothing is ever
really paid or emailed. "Sent" messages are rows in the `outbox` table.

---

## 1. Folder structure

```
payment-ops-agent/
├── src/payops/
│   ├── main.py                 # FastAPI app factory, router wiring, exception handlers
│   ├── config.py               # pydantic-settings Settings (reads env vars)
│   ├── logging.py              # structured JSON logging setup
│   ├── db/
│   │   ├── base.py             # DeclarativeBase, naming conventions
│   │   ├── session.py          # engine, SessionLocal, get_session dependency
│   │   └── models/             # one module per table group
│   │       ├── customers.py    # Customer
│   │       ├── billing.py      # Invoice, Payment, PaymentHistory, PaymentPlan
│   │       ├── agent.py        # AgentRun, Action, Approval
│   │       └── audit.py        # Outbox, AuditLog
│   ├── schemas/
│   │   ├── api.py              # request/response models for the HTTP API
│   │   └── llm.py              # strict Pydantic schemas for every LLM output
│   ├── api/
│   │   ├── deps.py
│   │   ├── security.py         # X-API-Key check (constant-time), UI session + CSRF
│   │   ├── errors.py           # uniform error response shape
│   │   └── routes/             # health.py, runs.py, approvals.py, audit.py, ui.py
│   ├── web/                    # server-rendered approvals page (Milestone 6)
│   │   ├── templates/          # Jinja2: base.html, login.html, approvals.html
│   │   └── static/             # one small CSS file, no JS framework
│   ├── services/               # business logic, called by API and agents
│   │   ├── runs.py
│   │   ├── approvals.py        # approve/reject -> execute action -> outbox
│   │   ├── outbox.py
│   │   └── audit.py            # audit_log writer + @audited decorator
│   ├── tools/                  # agent tools (plain functions + JSON schemas)
│   │   ├── registry.py
│   │   ├── customers.py        # get_customer
│   │   ├── invoices.py         # get_invoices
│   │   ├── history.py          # get_payment_history
│   │   └── actions.py          # draft_message, schedule_followup, flag_for_review
│   ├── llm/
│   │   ├── client.py           # LiteLLM wrapper: structured output, usage capture
│   │   ├── models.py           # provider -> default model names (picked in M3)
│   │   ├── throttle.py         # token-bucket limiter (requests/min, tokens/min)
│   │   ├── retry.py            # exponential backoff + jitter, 429/5xx/timeout handling
│   │   └── fake.py             # deterministic fake LLM for tests, CI, load tests
│   ├── policy/
│   │   ├── rules.py            # each guardrail is a pure function -> PolicyResult
│   │   └── engine.py           # runs all rules, returns allow/block/needs_approval
│   └── agents/
│       ├── state.py            # LangGraph state (TypedDict / Pydantic)
│       ├── graph.py            # builds and compiles the graph
│       ├── nodes/              # triage.py, research.py, drafting.py, policy_node.py, finalize.py
│       └── prompts/            # versioned prompt templates (triage_v1.md, ...)
├── migrations/                 # Alembic env + versions/
├── scripts/
│   └── seed.py                 # Faker seed with named scenario types
├── evals/
│   ├── datasets/v1/scenarios.jsonl   # 40 scenarios + expected outcomes
│   ├── datasets/v1/README.md         # schema and changelog for the dataset
│   ├── runner.py               # `make eval`
│   ├── metrics.py              # accuracy, success rate, tool-call accuracy, p95, tokens, mean ± std
│   ├── report.py               # markdown report writer
│   └── reports/                # YYYY-MM-DDTHH-MM-SS_<model>_<dataset>.md
├── benchmarks/
│   ├── locustfile.py
│   └── results.md              # Locust + eval numbers with machine specs
├── tests/
│   ├── conftest.py
│   ├── unit/                   # tools, policy rules, LLM layer, API (fake LLM)
│   └── integration/            # real Postgres in Docker, full graph with fake LLM
├── docs/
│   └── demo.gif
├── .github/workflows/ci.yml
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── pyproject.toml              # deps, ruff, pytest config (managed with uv)
├── alembic.ini
├── .env.example
├── README.md
└── PLAN.md
```

---

## 2. Milestones

### Milestone 0: Repo setup (done)

- [x] `git init`, Python `.gitignore`
- [x] Private GitHub repo `payment-ops-agent` set as `origin`
- [x] `.env.example` with placeholders for every env var
- [x] `PLAN.md`
- [x] Open questions answered and recorded in section 3

---

### Milestone 1: Scaffold, database, seed data, /health, CI

**Tasks**
1. `pyproject.toml` with Python 3.12, uv lockfile, runtime and dev dependency groups; ruff config.
2. `config.py` using pydantic-settings; fail fast on missing or invalid values (including a missing or too-short `API_KEY` / `SESSION_SECRET` outside `APP_ENV=test`).
3. `docker-compose.yml` with a `db` service (`postgres:16`, healthcheck, named volume). The API service is added in M5.
4. SQLAlchemy 2 models (typed `Mapped[...]`) for all 10 tables (the 9 in the spec plus `payment_plans`), with:
   - Enums: invoice status `open/paid/overdue/disputed`; action status `proposed/approved/rejected/executed`; run status `queued/running/awaiting_approval/completed/failed`.
   - `Numeric(12,2)` for money (never float), `timestamptz` everywhere.
   - FKs, indexes on `invoices(status, due_date)`, `audit_log(run_id, created_at)`.
   - `CHECK` constraints (amount > 0, paid amount <= invoice amount).
5. Alembic setup and initial migration (autogenerate, then reviewed by hand).
6. `scripts/seed.py`: deterministic (`SEED_RANDOM_SEED`), idempotent (truncates first), and tags each customer with a `scenario_type`:
   `reliable_slightly_late`, `chronic_late`, `disputed_invoice`, `large_balance`, `partial_payments`, `recent_payment_plan`, plus some filler `current` customers.
7. FastAPI app with `GET /health` that checks DB connectivity and returns version + DB status.
8. Makefile: `install`, `up`, `down`, `migrate`, `seed`, `run`, `lint`, `fmt`, `test`, `test-unit`, `test-int`.
9. GitHub Actions `ci.yml`: install with uv, `ruff check`, `ruff format --check`, unit tests; integration tests using a `postgres:16` service container.

**Done when**
- [ ] `make up && make migrate && make seed` works on a clean clone
- [ ] `alembic downgrade base && alembic upgrade head` works with no errors
- [ ] Seed creates at least 5 customers of every scenario type; running it twice gives the same data
- [ ] `GET /health` returns `200 {"status":"ok","db":"ok"}`, and `503` when the DB is down
- [ ] A test asserts every scenario type exists after seeding
- [ ] CI is green on GitHub with no secrets configured

---

### Milestone 2: Tools and core API

**Tasks**
1. Tools in `tools/`, each a typed function `(session, input_model) -> output_model` with a JSON schema exported for the LLM:
   - `get_customer(customer_id)`
   - `get_invoices(customer_id, status?)`
   - `get_payment_history(customer_id, months=12)`: on-time rate, avg days late, partial payment count, active plan
   - `draft_message(action_type, tone, context)`: stores the draft on the `actions` row; never sends anything
   - `schedule_followup(invoice_id, date)`: date must be in the future and within 60 days
   - `flag_for_review(invoice_id, reason)`
2. `services/audit.py`: an `@audited` wrapper that writes inputs, outputs, error, latency_ms, and token usage (if any) to `audit_log` for every tool call.
3. API routes with Pydantic validation and a uniform error shape:
   - `POST /runs` (body: `invoice_ids` list or `scope: "all_overdue"`, optional `max_invoices`) returns **202** + run id (M3 wires in the graph; M2 stores a `queued` run)
   - `GET /runs/{id}`: status + trace from `audit_log`
   - `GET /approvals?status=pending`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject` (reject requires a reason; approver name required on both)
   - `GET /audit?run_id=&event_type=&limit=&cursor=`: paginated
4. Pagination (limit capped at 200), UUID path validation, 404/409/422 handling.
5. API key auth in `api/security.py`:
   - A FastAPI dependency checks the `X-API-Key` header against `API_KEY` using `secrets.compare_digest`
   - Applied to every router except `GET /health` (which stays open for Docker/load-balancer health checks)
   - Missing or wrong key → `401` with the uniform error shape; the key is never logged or written to `audit_log`
   - Declared as an OpenAPI security scheme so the "Authorize" button works in `/docs`
6. Unit tests for every tool and endpoint.

**Done when**
- [ ] Each tool has tests for the happy path, not-found, and invalid input
- [ ] Every tool call produces exactly one `audit_log` row (tested)
- [ ] Invalid input on every endpoint returns 422 with the uniform error shape (tested)
- [ ] Approving an already-decided approval returns 409
- [ ] Every protected endpoint returns 401 with no key and with a wrong key; `/health` returns 200 with no key (tested with a parametrized test over all routes, so new routes can't skip auth)
- [ ] A test confirms the API key never appears in logs or `audit_log`
- [ ] OpenAPI docs at `/docs` show request/response examples
- [ ] Coverage of at least 85% on `tools/`, `services/`, `api/`

---

### Milestone 3: LangGraph workflow, policy node, approvals, audit

**Tasks**
1. `llm/` layer:
   - First, check the current free-tier model names and limits for Groq, Gemini, and Ollama, then record the defaults in `llm/models.py`, `.env.example`, and the README
   - LiteLLM wrapper picking the provider/model from `LLM_PROVIDER` + `LLM_MODEL`
   - Client-side throttle (requests/min and tokens/min token buckets)
   - Retry with exponential backoff + jitter on 429, 5xx, and timeouts; respect `Retry-After` when present; hard timeout per call
   - Structured output: request JSON, validate against a Pydantic schema, one corrective re-prompt on validation failure, then fail cleanly
   - Return token usage + latency for auditing
   - `FakeLLM` that returns scripted responses keyed by node + scenario
2. Graph: `triage -> (for each invoice) research -> drafting -> policy -> finalize`
   - **Triage**: deterministic features (days overdue, amount, history) + LLM rationale → priority score 0–100 and ordering. Score is clamped and validated.
   - **Research**: LLM tool-calling loop over the read-only tools, capped at N steps.
   - **Drafting**: picks `reminder | payment_plan_offer | escalate_to_human | no_action`, picks a tone, drafts the message.
   - **Policy (deterministic, no LLM)**: rules in `policy/rules.py`:
     - `NO_REMINDER_ON_DISPUTED`: block a reminder on a disputed invoice → convert to `escalate_to_human`
     - `APPROVAL_OVER_THRESHOLD`: balance > `APPROVAL_THRESHOLD_AMOUNT` → needs approval
     - `APPROVAL_FOR_PAYMENT_PLAN`: any payment-plan offer → needs approval
     - `NO_DISCOUNT_PROMISES`: regex/keyword detection of discounts, waivers, "% off", "forgive" → reject draft; return to drafting once with feedback, then escalate
     - `SCHEMA_VALID`: every LLM output passed Pydantic validation
   - **Finalize**: writes the `actions` row; auto-approved actions are executed (outbox row); others create an `approvals` row and the run ends as `awaiting_approval`.
3. Approval flow: `POST /approvals/{id}/approve` executes the action deterministically (writes to outbox, marks `executed`); reject marks `rejected`. No LLM call happens after a human decision.
4. Runs execute in a background worker (FastAPI `BackgroundTasks` or a simple in-process queue) so `POST /runs` returns right away.
5. Prompts stored as versioned files; prompt version and model recorded on `agent_runs`.
6. Every node decision, LLM call, and policy result is written to `audit_log`.

**Done when**
- [ ] Each policy rule has unit tests for both allow and block cases (table-driven)
- [ ] A disputed-invoice scenario can never produce an outbox reminder, even when the fake LLM tries to (tested)
- [ ] A draft containing "10% discount" is never executed (tested)
- [ ] An integration test runs the full graph with `FakeLLM` against real Postgres, end to end
- [ ] The retry layer is tested with a simulated 429 → backoff → success, and 429s exhausted → clean failure
- [ ] One real run against Groq succeeds by hand, and its trace is visible in `GET /runs/{id}`
- [ ] `GET /runs/{id}` shows an ordered trace with tokens and latency per step

---

### Milestone 4: Evaluation framework and first report

**Tasks**
1. Dataset `evals/datasets/v1/scenarios.jsonl`, 40 scenarios (about 6–7 per scenario type, plus edge cases). Each has:
   `id, description, fixture (customer + invoices + payments), expected_action, acceptable_actions, requires_approval, expected_tools, forbidden_policies`.
2. The runner loads each scenario into an isolated eval schema/database, runs the graph, and collects results. It is sequential by default to respect free-tier limits and can resume after interruption.
3. Repeats: `--repeats N` (or `EVAL_REPEATS`, default 1) runs each scenario N times. With N > 1, every metric is reported as mean ± std across repeats, and each scenario gets a consistency score (share of repeats that chose the same action). Before starting, the runner prints the estimated number of LLM calls so you can see the free-tier cost.
3. Metrics:
   - **Decision accuracy**: chosen action == expected_action
   - **Task success rate**: correct action AND correct approval flag AND zero policy violations AND valid schema
   - **Tool-call accuracy**: precision and recall of tools called vs `expected_tools`
   - **Policy violation count**: violations that reached the outbox (should be 0), reported separately from violations the guardrail caught
   - **p95 latency** per run, **average tokens** per run
5. Markdown report (the only output; no MLflow): config (provider, model, prompt versions, dataset version, repeats, git SHA), metrics table, per-scenario-type breakdown, list of failures with reasons.
6. `make eval` (accepts `REPEATS=N`) and `make eval-compare A=... B=...` (diff two reports).

**Done when**
- [ ] The dataset has 40 scenarios that pass schema validation, and the dataset README documents fields
- [ ] `make eval LLM_PROVIDER=fake` runs in CI-like conditions with no API key
- [ ] The first real eval report on Groq is committed to `evals/reports/`
- [ ] Metric functions have unit tests with hand-computed expected values, including mean ± std
- [ ] `make eval REPEATS=3 LLM_PROVIDER=fake` produces a report with mean ± std columns; `REPEATS=1` shows plain values
- [ ] Policy violations reaching the outbox = 0 in the committed report

---

### Milestone 5: Load testing, benchmarks, hardening

**Tasks**
1. `Dockerfile` (multi-stage, slim, non-root, works on arm64 and amd64) and add an `api` service to Compose that runs migrations on start.
2. `locustfile.py`: a realistic mix (`GET /health`, `GET /runs/{id}`, `GET /approvals`, `GET /audit`, `POST /runs`) run against `LLM_PROVIDER=fake`, so the benchmark measures the API and not Groq's rate limits.
3. Hardening: DB connection pool sizing, request timeouts, graceful shutdown, request ID middleware, run-level error capture (failed runs get `status=failed` with the error), idempotency key on `POST /runs`.
4. Record results in `benchmarks/results.md`: machine specs (chip, RAM, macOS, Docker resources), users/spawn rate/duration, RPS, p50/p95/p99, error rate, plus eval metrics from M4.

**Done when**
- [ ] `docker compose up` starts the full stack from a clean clone
- [ ] Locust runs for 5 min at the chosen concurrency with error rate < 1%
- [ ] `benchmarks/results.md` has a filled-in table and machine specs
- [ ] Killing Postgres mid-run produces a `failed` run and a 503 health check, not a crash

---

### Milestone 6: Approvals page and documentation

**Tasks**
1. Minimal approvals page, server-rendered with Jinja2 (no React, no JS framework):
   - `GET /ui/login`: a form where you enter the API key once. On success the server sets a signed, `HttpOnly`, `SameSite=Strict` session cookie (signed with `SESSION_SECRET`). The key itself is never stored in the cookie.
   - `GET /ui/approvals`: pending approvals table showing customer, invoice, amount, proposed action, drafted message, and the policy rules that triggered approval
   - Approve/reject buttons are plain HTML forms that POST to `/ui/approvals/{id}/approve|reject` with a CSRF token. They call the same `services/approvals.py` code as the JSON API, so the logic isn't duplicated.
   - `GET /ui/outbox`: read-only list of simulated sent messages, so the demo shows the end result
   - Message text is auto-escaped by Jinja2 (LLM output is never rendered as HTML)
2. README: pitch, Mermaid architecture diagram + graph diagram, one-command quickstart, env var table, results summary (linking reports), demo GIF, "Deploying to the cloud" **guide** (Cloud Run or ECS Fargate + Cloud SQL/RDS, secrets in Secret Manager, migrations as a job), clearly marked as not deployed.
3. Record the demo GIF using the approvals page (start run → trace → approve on the page → outbox page).
4. Interview notes (kept local, not committed): architecture walkthrough, design decisions and tradeoffs, and 15 interview Q&As (e.g. why deterministic guardrails, how retries work, how evals are scored, what fails, what would change at scale).

**Done when**
- [ ] `/ui/approvals` redirects to `/ui/login` with no session cookie (tested)
- [ ] A form POST without a valid CSRF token returns 403 (tested)
- [ ] Approving on the page creates an outbox row, the same as the JSON API (tested)
- [ ] A drafted message containing `<script>` renders as text, not HTML (tested)
- [ ] A fresh clone + `cp .env.example .env` + `make demo` (or `docker compose up`) works by following only the README
- [ ] Mermaid diagrams render on GitHub
- [ ] The README states plainly that the cloud section is a guide and the project is not deployed
- [ ] The interview notes have 15 Q&As, each tied to real code in the repo

---

## 3. Decisions

Agreed on 2026-11-15. These override anything in the original spec they conflict with.

**Architecture**

1. **Fake LLM provider.** `LLM_PROVIDER=fake` is a fourth option alongside groq, gemini, and ollama. CI, unit/integration tests, and Locust use it, so none of them need an API key or hit rate limits.
2. **`POST /runs` is asynchronous.** It returns `202 Accepted` with a run ID; clients poll `GET /runs/{id}`.
3. **Approvals don't restart the agents.** A run that needs approval ends as `awaiting_approval`. Approving sends the already-drafted message exactly as written, with no further LLM call.
4. **`payment_plans` table.** Added to the data model. `payment_history` is a per-customer event log (payment received, promise to pay, plan created, dispute opened), separate from `payments`.
5. **Sync SQLAlchemy + psycopg 3**, not async. The tradeoff is written up in the interview notes.
6. **Discount detection is rule-based** (regex + keyword list), keeping the policy node fully deterministic. Its limits are documented, and some eval scenarios try to get around it.
7. **Prompt versioning.** Prompts live in versioned files; the version is stored on each run and printed in each eval report.

**Tooling**

8. **uv** for dependency management.
9. **Integration tests run in CI** against a GitHub Actions Postgres service container.
10. **No MLflow.** Eval results are markdown reports only. (`mlruns/` stays in `.gitignore` as a harmless safeguard.)

**Answers to open questions**

11. **API key auth (Milestone 2).** Every endpoint except `GET /health` requires an `X-API-Key` header matching `API_KEY`. The key is compared in constant time and is never logged.
12. **Approvals page (Milestone 6).** Minimal server-rendered HTML with Jinja2; no React or JS framework. It logs in with the API key once and uses a signed session cookie (`SESSION_SECRET`) with CSRF tokens on form posts.
13. **Eval repeats (Milestone 4).** `--repeats N` / `EVAL_REPEATS`, default 1. With N > 1, metrics are reported as mean ± std, plus a per-scenario consistency score.
14. **Model names.** Current free-tier model names for Groq, Gemini, and Ollama are checked and picked at the start of Milestone 3. The model in `.env.example` is a placeholder until then.

**Environment variables added**

- `API_KEY`, `SESSION_SECRET`, `EVAL_REPEATS` (all in `.env.example` with placeholder values)
