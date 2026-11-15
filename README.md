# Payment Ops Agent

An agent that triages overdue invoices, researches each customer's payment history,
drafts follow-ups, and routes risky actions to a human for approval. Every action passes
deterministic policy checks and is recorded in an audit log.

All data is synthetic. Nothing is ever really paid or emailed: "sent" messages are rows
in the `outbox` table.

> Work in progress. See [PLAN.md](PLAN.md) for milestones and design decisions.

## Quickstart

Requirements: [uv](https://docs.astral.sh/uv/), Docker, and Python 3.12 (uv installs it if needed).

**With Docker only** (the API runs in a container and applies migrations on start):

```bash
cp .env.example .env      # then set API_KEY and SESSION_SECRET (see comments in the file)
make up                   # builds the image, starts Postgres 16 + the API on :8000
docker compose exec api python -m scripts.seed
open http://localhost:8000/docs
```

**For development** (the API runs on your machine with reload):

```bash
cp .env.example .env      # as above
make install              # uv sync
make db                   # only Postgres 16 in Docker
make migrate              # alembic upgrade head
make seed                 # deterministic synthetic customers, invoices, payments
make run                  # http://localhost:8000/docs
```

`GET /health` is open; every other endpoint needs an `X-API-Key` header matching `API_KEY`.

To use a real model, set `LLM_PROVIDER` and the matching key in `.env`. Leave `LLM_MODEL`
empty to use the default:

| Provider | Default model | Notes |
|---|---|---|
| `groq` | `openai/gpt-oss-120b` | Free plan (about 30 requests/min, 8K tokens/min). Llama models need an Enterprise plan |
| `gemini` | `gemini-3.5-flash-lite` | Free tier |
| `ollama` | `llama3.2` | Local; run `ollama pull llama3.2` first |
| `fake` | `fake-v1` | Deterministic, no key; used by tests and CI |

The free-tier limits above were checked on 2026-11-15 and can change. The client throttles
itself (`LLM_REQUESTS_PER_MINUTE`, `LLM_TOKENS_PER_MINUTE`) to stay under them.

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Open. `200` with DB status, `503` if the DB is unreachable |
| `POST` | `/runs` | `{"invoice_ids": [...]}` or `{"scope": "all_overdue"}`, optional `max_invoices`. Returns `202` + run id. Optional `Idempotency-Key` header: a retry returns the same run; the same key with another body is `409` |
| `GET` | `/runs/{id}` | Status, token/latency totals, and the ordered trace from `audit_log` |
| `GET` | `/approvals?status=pending` | Paginated (`limit` ≤ 200, `cursor`) |
| `POST` | `/approvals/{id}/approve` | `{"approver": "..."}`. Sends the drafted message as written (an outbox row); `409` if already decided or now blocked by policy |
| `POST` | `/approvals/{id}/reject` | `{"approver": "...", "reason": "..."}` |
| `GET` | `/audit?run_id=&event_type=&limit=&cursor=` | Paginated audit log, newest first |

Errors always look like `{"error": {"code": "...", "message": "...", "details": [...]}}`.
Every response carries an `X-Request-ID` (yours if you send a valid one), which also appears
in every log line for that request. A database outage returns `503` with `Retry-After`, and
a request slower than `REQUEST_TIMEOUT_SECONDS` returns `504`.
Interactive docs with examples are at `/docs` (use **Authorize** to set the key).

## How a run works

`POST /runs` returns `202` right away and runs a LangGraph workflow in the background:

```
triage -> research -> drafting -> policy -> finalize -> (next invoice) research ...
                         ^           |
                         +-- revise -+   (once, after a discount promise)
```

- **Triage** scores each invoice 0–100 from deterministic features (days overdue, balance,
  payment history). The LLM adds a rationale and can move a score by at most 20 points.
- **Research** is an LLM tool-calling loop over the read-only tools, capped at
  `RESEARCH_MAX_STEPS` rounds.
- **Drafting** picks `reminder`, `payment_plan_offer`, `escalate_to_human`, or `no_action`,
  picks a tone, and writes the message as JSON validated by a Pydantic schema (one
  corrective re-prompt on invalid output).
- **Policy** is plain code, no LLM (`src/payops/policy/`):

  | Rule | Effect |
  |---|---|
  | `SCHEMA_VALID` | Invalid LLM output is blocked and escalated |
  | `NO_REMINDER_ON_DISPUTED` | No reminder or plan offer on a disputed invoice; escalated instead |
  | `NO_DISCOUNT_PROMISES` | Discount, waiver, "% off", "forgive" (regex/keyword list) is blocked; one redraft with feedback, then escalated |
  | `APPROVAL_OVER_THRESHOLD` | Balance over `APPROVAL_THRESHOLD_AMOUNT` needs human approval |
  | `APPROVAL_FOR_PAYMENT_PLAN` | Every payment-plan offer needs human approval |

- **Finalize** stores the action. Allowed actions run immediately (messages become outbox
  rows). Anything needing approval creates an approval and the run ends as
  `awaiting_approval`. Approving sends the stored text exactly as written, with no further
  LLM call, after re-checking the policy against the invoice's current state.

Every LLM call, tool call, node decision, and policy result is written to `audit_log` with
tokens and latency, and shows up in order in `GET /runs/{id}`. If the provider keeps failing
after retries (backoff with jitter on 429/5xx/timeouts, honoring `Retry-After`), the run
ends as `failed` with the error.

## Agent tools

Plain typed functions in `src/payops/tools/`, each exported with a JSON schema for the LLM
and wrapped by `@audited`, so every call writes exactly one `audit_log` row:
`get_customer`, `get_invoices`, `get_payment_history`, `draft_message`, `schedule_followup`,
`flag_for_review`. The write tools only create *proposed* actions; nothing is sent from a tool.

## Development

```bash
make lint        # ruff check + format check
make test-unit   # no database, no API keys
make test-int    # needs `make db` (or `make up`)
make cov         # all tests + coverage gate on tools/, services/, api/
```

## Evaluation

`make eval` runs the 40 scenarios in `evals/datasets/v1/` through the real agent graph,
each in a fresh, dedicated eval database (`payops_eval`), and writes a markdown report to
`evals/reports/`. It scores decision accuracy, task success, tool-call precision/recall,
policy violations caught vs. reaching the outbox, p95 latency, and tokens.

```bash
make eval LLM_PROVIDER=fake         # smoke run, no API key (what CI runs)
make eval                           # with the provider in .env, e.g. Groq
make eval REPEATS=3                 # metrics as mean ± std, plus per-scenario consistency
make eval LIMIT=10                  # stop after 10 more scenarios
make eval-compare A=evals/reports/<a>.md B=evals/reports/<b>.md
```

A full Groq run needs more tokens than the free tier's daily quota, so the runner is
resumable: each finished scenario is saved to `evals/runs/<config>/results.jsonl`, a
rerun skips them, a rate limit stops the run cleanly (exit code 75), and the report is
written once all scenarios are done. Just run the same `make eval` again the next day.

## Load testing

```bash
LLM_PROVIDER=fake docker compose up -d --wait --build
make bench                          # Locust: 50 users for 5 minutes (USERS=, SPAWN=, TIME=)
```

Results and machine specs are in [benchmarks/results.md](benchmarks/results.md).

## Seed scenarios

The seed tags every customer with a `scenario_type`, which the evaluation dataset reuses:

| Scenario | What it looks like |
|---|---|
| `current` | Open invoice not yet due, pays on time |
| `reliable_slightly_late` | One invoice 3–10 days overdue, good history |
| `chronic_late` | 2–3 invoices 30–90 days overdue, broken promise to pay |
| `disputed_invoice` | Invoice in `disputed` status with a dispute event |
| `large_balance` | Overdue balance above the approval threshold |
| `partial_payments` | Overdue invoice partly paid in installments |
| `recent_payment_plan` | Active payment plan started in the last few weeks |

The seed is deterministic for a given `SEED_RANDOM_SEED` and day, and truncates all tables
before inserting, so running it twice gives the same data.
