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
│   │       ├── billing.py      # Invoice, Payment, PaymentHistory
│   │       ├── agent.py        # AgentRun, Action, Approval
│   │       └── audit.py        # Outbox, AuditLog
│   ├── schemas/
│   │   ├── api.py              # request/response models for the HTTP API
│   │   └── llm.py              # strict Pydantic schemas for every LLM output
│   ├── api/
│   │   ├── deps.py
│   │   ├── errors.py           # uniform error response shape
│   │   └── routes/             # health.py, runs.py, approvals.py, audit.py
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
│   ├── metrics.py              # accuracy, success rate, tool-call accuracy, p95, tokens
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

---

### Milestone 1: Scaffold, database, seed data, /health, CI

**Tasks**
1. `pyproject.toml` with Python 3.12, uv lockfile, runtime and dev dependency groups; ruff config.
2. `config.py` using pydantic-settings; fail fast on missing or invalid values.
3. `docker-compose.yml` with a `db` service (`postgres:16`, healthcheck, named volume). The API service is added in M5.
4. SQLAlchemy 2 models (typed `Mapped[...]`) for all 9 tables, with:
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
5. Unit tests for every tool and endpoint.

**Done when**
- [ ] Each tool has tests for the happy path, not-found, and invalid input
- [ ] Every tool call produces exactly one `audit_log` row (tested)
- [ ] Invalid input on every endpoint returns 422 with the uniform error shape (tested)
- [ ] Approving an already-decided approval returns 409
- [ ] OpenAPI docs at `/docs` show request/response examples
- [ ] Coverage of at least 85% on `tools/`, `services/`, `api/`

---

### Milestone 3: LangGraph workflow, policy node, approvals, audit

**Tasks**
1. `llm/` layer:
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
3. Metrics:
   - **Decision accuracy**: chosen action == expected_action
   - **Task success rate**: correct action AND correct approval flag AND zero policy violations AND valid schema
   - **Tool-call accuracy**: precision and recall of tools called vs `expected_tools`
   - **Policy violation count**: violations that reached the outbox (should be 0), reported separately from violations the guardrail caught
   - **p95 latency** per run, **average tokens** per run
4. Markdown report: config (provider, model, prompt versions, dataset version, git SHA), metrics table, per-scenario-type breakdown, list of failures with reasons.
5. `make eval` and `make eval-compare A=... B=...` (diff two reports).

**Done when**
- [ ] The dataset has 40 scenarios that pass schema validation, and the dataset README documents fields
- [ ] `make eval LLM_PROVIDER=fake` runs in CI-like conditions with no API key
- [ ] The first real eval report on Groq is committed to `evals/reports/`
- [ ] Metric functions have unit tests with hand-computed expected values
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

### Milestone 6: Documentation

**Tasks**
1. README: pitch, Mermaid architecture diagram + graph diagram, one-command quickstart, env var table, results summary (linking reports), demo GIF, "Deploying to the cloud" **guide** (Cloud Run or ECS Fargate + Cloud SQL/RDS, secrets in Secret Manager, migrations as a job), clearly marked as not deployed.
2. Record the demo GIF (start run → trace → approval → outbox).
3. Interview notes (kept local, not committed): architecture walkthrough, design decisions and tradeoffs, and 15 interview Q&As (e.g. why deterministic guardrails, how retries work, how evals are scored, what fails, what would change at scale).

**Done when**
- [ ] A fresh clone + `cp .env.example .env` + `make demo` (or `docker compose up`) works by following only the README
- [ ] Mermaid diagrams render on GitHub
- [ ] The README states plainly that the cloud section is a guide and the project is not deployed
- [ ] The interview notes have 15 Q&As, each tied to real code in the repo

---

## 3. Questions and suggested changes

**Suggested changes (I'll assume these unless you say otherwise)**

1. **Fake LLM provider.** Add `LLM_PROVIDER=fake` as a fourth option. It lets CI, integration tests, and Locust run with no key and no rate limits. Otherwise the load test measures Groq's free tier, not your API.
2. **`POST /runs` is asynchronous (202 Accepted).** A run makes several LLM calls and can take 10–60 s on a free tier. Clients poll `GET /runs/{id}`.
3. **Approvals don't resume the LLM graph.** The run ends at `awaiting_approval`; approving runs the already-drafted action deterministically. This is simpler than LangGraph interrupts + a Postgres checkpointer, and it means a human approves exactly the text that gets sent. (We could switch to LangGraph `interrupt()` if you want to show that feature.)
4. **Add a `payment_plans` table** (or a plan record in `payment_history`). The "recent payment plan" scenario needs somewhere to live. I'd also define `payment_history` as a per-customer event log (payment received, promise to pay, plan created, dispute opened) so it is distinct from `payments`.
5. **Integration tests run in CI** too, using a GitHub Actions Postgres service container. It's free and makes the CI badge more meaningful.
6. **uv** for dependency management (fast, lockfile, good CI caching).
7. **Sync SQLAlchemy + psycopg 3.** Simpler to test and reason about. The slow part is the LLM, which already runs in the background. Async can be noted in the interview notes as a tradeoff.
8. **Discount detection is rule-based** (regex + keyword list), consistent with "the policy node is deterministic". Its limits (paraphrasing) will be documented, and eval scenarios will try to get around it.
9. **Prompt versioning.** Prompts live in files and their version is stored on each run and in each eval report, so before/after comparisons are traceable.

**Questions for you**

1. **MLflow?** You asked for `mlruns` in `.gitignore`, but the spec doesn't mention MLflow. Should eval runs also be logged to a local MLflow tracker, or is the markdown report enough? (I'd lean toward markdown only, plus optional MLflow later.)
2. **Auth.** The spec has no auth. Should I add a simple static API key header (`X-API-Key`) so approvals aren't open, or keep it auth-free and say so in the README?
3. **Demo UI.** The demo GIF can use Swagger UI + terminal. Do you want a minimal approvals page (one HTML file served by FastAPI) to make the GIF more visual? It's about half a day of work.
4. **Eval determinism.** LLM outputs vary between runs. Should the eval runner support `--repeats N` and report mean ± std? This costs N× more free-tier quota.
5. **Model names.** Groq and Gemini retire models often. I'll check the current free-tier model names when M3 starts, rather than locking them in now. Is that OK?
