# Benchmarks

Two kinds of numbers: API load tests (Locust, fake LLM, so they measure the API and
Postgres rather than a provider's rate limits) and agent quality (the eval, see
[the eval section](#agent-quality-eval)).

## Machine

| | |
| --- | --- |
| Machine | MacBook Air (Mac17,3), Apple M5, 10 cores (4 performance + 6 efficiency) |
| Memory | 16 GB |
| OS | macOS 26.6.2 |
| Docker | Docker Desktop 29.9.3, VM with 10 CPUs and 7.7 GiB memory |
| Stack | `docker compose up` (API image from this repo + `postgres:16`, PostgreSQL 16.16), Python 3.12.16 |
| API process | one uvicorn worker, `DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=10`, `LLM_PROVIDER=fake`, `FAKE_LLM_LATENCY_MS=0` |
| Data | `make seed` (60 customers) before each test |
| Load generator | Locust 2.47.0 on the same Mac (`make bench`) |

## Traffic mix

`benchmarks/locustfile.py`. Each simulated user waits 0.5 to 2 s between requests.

| Weight | Request | Notes |
| ---: | --- | --- |
| 20 | `GET /runs/{id}` | Status + full trace of a recent run (≈50 KB) |
| 15 | `GET /approvals?status=pending&limit=50` | Approvals queue (≈90 KB per page) |
| 15 | `GET /audit?limit=50` | Audit log page |
| 10 | `GET /health` | Includes a DB round trip |
| 2 | `POST /runs` `{"scope": "all_overdue", "max_invoices": 2}` | With an `Idempotency-Key`; runs the full agent graph in the background |

## Results (2026-11-15)

### Main run: 50 users, 5 minutes

`make bench` (50 users, spawn rate 5/s, 5 min).

| Endpoint | Requests | Failures | p50 | p95 | p99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `GET /runs/{id}` | 3,802 | 0 | 7 ms | 15 ms | 27 ms | 140 ms |
| `GET /approvals?status=pending` | 2,736 | 0 | 11 ms | 21 ms | 46 ms | 150 ms |
| `GET /audit` | 2,884 | 0 | 7 ms | 15 ms | 30 ms | 140 ms |
| `GET /health` | 1,918 | 0 | 4 ms | 9 ms | 18 ms | 94 ms |
| `POST /runs` | 357 | 0 | 8 ms | 19 ms | 59 ms | 77 ms |
| **All** | **11,697** | **0 (0.00%)** | **7 ms** | **17 ms** | **33 ms** | **150 ms** |

Throughput: **39 requests/s**. At 50 users this is set by Locust's wait time
(50 users / ~1.25 s average wait), not by the API. All 357 runs started during the test
finished in the background (all `awaiting_approval`: the highest-priority overdue
invoices each run picks include at least one that needs a human). Background run time: 56 ms average, 196 ms max.

### Headroom check: 200 users, 2 minutes

`make bench USERS=200 SPAWN=20 TIME=2m`

| | Value |
| --- | --- |
| Requests | 18,429, 0 failures |
| Throughput | 154 requests/s |
| Latency p50 / p95 / p99 / max | 6 / 28 / 63 / 790 ms |
| API container CPU (sampled mid-test) | ~48% of one core |
| Postgres container CPU | ~14% of one core |
| Background runs | 617 started, all finished; 102 ms average, 841 ms max |

Four times the load gave four times the throughput, with p95 still under 30 ms, so the
API is far from saturated here. The ceilings to expect next are the single uvicorn
worker (one Python process) and the threadpool shared by sync endpoints and background
runs; a real LLM adds seconds per run, which is why runs execute in the background.

### Failure behaviour (manual, 2026-11-15)

Killing Postgres (`docker compose kill db`) in the middle of a run, with
`FAKE_LLM_LATENCY_MS=1500` so the run lasts long enough to be interrupted:

| Check | Result |
| --- | --- |
| `GET /health` while the DB is down | `503 {"status": "degraded", "db": "error"}` |
| Other endpoints while the DB is down | `503 service_unavailable` with `Retry-After: 5` (not 500) |
| API process | kept running, 0 container restarts |
| The interrupted run, after `docker compose start db` | `failed`, with the database error recorded |

## Agent quality (eval)

Full report:
[`evals/reports/2026-11-16T08-10-00_openai-gpt-oss-120b_v1.md`](../evals/reports/2026-11-16T08-10-00_openai-gpt-oss-120b_v1.md),
raw results in
[`evals/runs/v1_groq_openai-gpt-oss-120b_v1_r1/results.jsonl`](../evals/runs/v1_groq_openai-gpt-oss-120b_v1_r1/results.jsonl).
Groq `openai/gpt-oss-120b`, prompt v1, dataset v1 (40 scenarios), 1 repeat. The run took
four runner sessions over two days because a full run needs more than the free tier's
200K tokens/day; the runner resumed where it stopped each time.

| Metric | Groq, 40/40 | Fake LLM, 40/40 |
| --- | ---: | ---: |
| Decision accuracy | 87.5% | 100% |
| Task success rate | 87.5% | 100% |
| Acceptable-action rate | 97.5% | 100% |
| Approval-flag accuracy | 92.5% | 100% |
| Tool-call precision / recall | 100% / 100% | 100% / 100% |
| Schema-valid rate | 100% | 100% |
| Policy violations reaching the outbox | **0** | 0 |
| Policy violations caught by the guardrail | 0 | 0 |
| Latency p50 / p95 per run | 48.5 s / 58.6 s | – / 36 ms |
| Average tokens per run | 5,984 | 3,592 |

The fake LLM's 100% is a check of the fixtures, not of the agent: its decision rule is
the one the dataset encodes.

**Groq latency includes free-tier rate-limit throttling.** It is mostly the client-side
throttle (`LLM_TOKENS_PER_MINUTE=7000`, under Groq's 8K TPM) waiting before each call,
plus any backoff after a 429, not model time.

Failures (5 of 40): three times the model sent a reminder where a payment-plan offer was
expected (chl-04, prt-02, prt-05, all customers with partial payments or a long history
of lateness), once it sent a reminder for an invoice that is not yet due (cur-04), and
once it escalated a customer on an active payment plan whose history mentions a waiver
request (rpp-04) instead of taking no action.
