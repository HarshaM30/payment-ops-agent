# Payment Ops Agent

An agent that triages overdue invoices, researches each customer's payment history,
drafts follow-ups, and routes risky actions to a human for approval. Every action passes
deterministic policy checks and is recorded in an audit log.

All data is synthetic. Nothing is ever really paid or emailed: "sent" messages are rows
in the `outbox` table.

> Work in progress. See [PLAN.md](PLAN.md) for milestones and design decisions.

## Quickstart

Requirements: [uv](https://docs.astral.sh/uv/), Docker, and Python 3.12 (uv installs it if needed).

```bash
cp .env.example .env      # then set API_KEY and SESSION_SECRET (see comments in the file)
make install              # uv sync
make up                   # Postgres 16 in Docker
make migrate              # alembic upgrade head
make seed                 # deterministic synthetic customers, invoices, payments
make run                  # http://localhost:8000/docs
```

`GET /health` is open; every other endpoint needs an `X-API-Key` header matching `API_KEY`.

## Development

```bash
make lint        # ruff check + format check
make test-unit   # no database, no API keys
make test-int    # needs `make up`
make cov         # all tests + coverage gate on tools/, services/, api/
```

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
