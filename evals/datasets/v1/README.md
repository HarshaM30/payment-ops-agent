# Eval dataset v1

`scenarios.jsonl` has 40 scenarios, one JSON object per line. Each scenario is one
customer with one target invoice. The runner loads it into an empty eval database, runs
the full agent graph on the target invoice, and scores what the system actually did.

| Scenario type | Count | Expected action |
| --- | ---: | --- |
| `reliable_slightly_late` | 6 | `reminder` |
| `chronic_late` | 6 | `payment_plan_offer` (long overdue / broken promises), else `reminder` |
| `disputed_invoice` | 7 | `escalate_to_human` |
| `large_balance` | 6 | `reminder` (needs approval); one `payment_plan_offer` |
| `partial_payments` | 6 | `payment_plan_offer` (needs approval) |
| `recent_payment_plan` | 5 | `no_action` |
| `current` (edge cases) | 4 | `no_action` (not yet due, due today, already paid), one `reminder` exactly at the threshold |

Five scenarios are tagged `discount_bait`. Their history contains a customer note asking
for a discount, a waiver, or a write-off, to see whether the drafted message gives in.
The discount guardrail is rule-based (regex and keyword list). A paraphrase it does not
recognize would get past it, so these scenarios check it against realistic text.

## Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string, `abc-NN` | Unique id. The prefix is the scenario type. |
| `scenario_type` | enum | Same values as `customers.scenario_type` in the seed. |
| `description` | string | What the scenario tests, in one sentence. |
| `tags` | list of strings | Free-form labels, e.g. `discount_bait`. |
| `fixture` | object | Data loaded before the run (see below). |
| `expected_action` | `reminder` \| `payment_plan_offer` \| `escalate_to_human` \| `no_action` | The best action. Decision accuracy compares against this. |
| `acceptable_actions` | list | Actions a reasonable reviewer would also accept. Must include `expected_action`. Reported as a secondary metric. |
| `requires_approval` | bool | Whether `expected_action` should create an approval (over the 10,000 threshold, or any payment-plan offer). |
| `expected_tools` | list | Read-only tools research should call (`get_customer`, `get_invoices`, `get_payment_history`). Used for tool-call precision/recall. |
| `forbidden_policies` | list | Blocking rules (`SCHEMA_VALID`, `NO_DISCOUNT_PROMISES`, `NO_REMINDER_ON_DISPUTED`) that must not trigger at all in this scenario. If the guardrail has to catch one, the task is not a success, even though nothing bad was sent. |

### `fixture`

All dates are offsets from the day the eval runs, so a scenario means the same thing
whenever it runs.

| Field | Meaning |
| --- | --- |
| `customer` | `{name, company}`. Email is generated from the scenario id. |
| `invoices[]` | `{ref, amount, status, due_days_ago, terms_days?, description?, payments[]}`. `due_days_ago` is negative for a future due date. `amount_paid` is the sum of `payments`. A `paid` invoice must be fully paid. |
| `invoices[].payments[]` | `{amount, days_ago, method?}` |
| `history[]` | Rows for `payment_history`: `{event_type, days_ago, invoice_ref?, amount?, details}`. A `promise_to_pay` with `details.kept = false` counts as a broken promise. |
| `plans[]` | `{invoice_ref, installment_count, installments_paid, start_days_ago, status?}` |
| `targets` | The invoice ref the run handles (exactly one). Invoice numbers are `EV-<ID>-<REF>`. |

`evals/dataset.py` validates every line with Pydantic (strict: unknown fields are
rejected), and `tests/unit/test_eval_dataset.py` runs that validation in CI.

## Changelog

- **v1** (2026-11-15): first version, 40 scenarios.
