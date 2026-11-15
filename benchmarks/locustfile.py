"""Load test for the API. Run it against LLM_PROVIDER=fake so it measures the API and the
database, not an LLM provider's rate limits.

    LLM_PROVIDER=fake docker compose up -d --wait --build
    make bench                     # 5 min, 50 users (override with USERS=, SPAWN=, TIME=)

The traffic mix is read-heavy, like a dashboard polling run status and an approvals
queue, plus a steady trickle of new runs (each one executes the full agent graph with
the fake LLM in the API's background worker).
"""

import os
import random
import uuid

from locust import HttpUser, between, events, task

API_KEY = os.environ.get("API_KEY", "")
RUN_BODY = {"scope": "all_overdue", "max_invoices": 2}
_run_ids: list[str] = []  # shared across users in this worker process


@events.test_start.add_listener
def _check_key(environment, **_kwargs) -> None:
    if not API_KEY:
        environment.runner.quit()
        raise SystemExit("API_KEY is not set (run via `make bench`, which loads .env)")


class ApiUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.client.headers.update({"X-API-Key": API_KEY})
        if not _run_ids:
            self.create_run()

    @task(10)
    def health(self) -> None:
        self.client.get("/health")

    @task(20)
    def get_run(self) -> None:
        if _run_ids:
            self.client.get(f"/runs/{random.choice(_run_ids)}", name="/runs/{id}")

    @task(15)
    def pending_approvals(self) -> None:
        self.client.get("/approvals?status=pending&limit=50", name="/approvals?status=pending")

    @task(15)
    def audit_page(self) -> None:
        self.client.get("/audit?limit=50", name="/audit")

    @task(2)
    def create_run(self) -> None:
        resp = self.client.post(
            "/runs", json=RUN_BODY, headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        if resp.status_code == 202:
            _run_ids.append(resp.json()["run_id"])
            del _run_ids[:-500]  # keep the pool bounded
