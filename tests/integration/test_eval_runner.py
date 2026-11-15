"""The eval runner end to end with the fake LLM against a real (eval) Postgres database."""

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.orm import Session

import payops.agents.runner as agent_runner
from evals import runner
from evals.collect import collect
from evals.dataset import DATASETS_DIR
from payops.config import get_settings
from payops.db.models import Outbox
from payops.llm.client import LLMClient
from payops.llm.fake import FakeLLM
from payops.llm.retry import RetryPolicy
from tests.integration.conftest import test_database_url

KEEP = ("rsl-01", "dsp-01", "lrg-01")
DISCOUNT_DRAFT = {
    "action_type": "reminder",
    "tone": "firm",
    "subject": "Invoice past due",
    "body": "Pay today and we will give you a 10% discount.",
    "rationale": "Adversarial scripted draft.",
}


class RateLimitError(Exception):
    status_code = 429


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    datasets = tmp_path / "datasets"
    (datasets / "vt").mkdir(parents=True)
    lines = (DATASETS_DIR / "v1" / "scenarios.jsonl").read_text().splitlines()
    keep = [ln for ln in lines if json.loads(ln)["id"] in KEEP]
    (datasets / "vt" / "scenarios.jsonl").write_text("\n".join(keep) + "\n")

    base = make_url(test_database_url())
    eval_url = base.set(database=f"{base.database}_eval").render_as_string(hide_password=False)
    settings = get_settings().model_copy(
        update={"eval_database_url": eval_url, "llm_provider": "fake", "llm_model": None}
    )
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    paths = {
        "datasets": datasets,
        "state": tmp_path / "runs",
        "reports": tmp_path / "reports",
        "eval_url": eval_url,
    }
    yield paths
    shutil.rmtree(tmp_path, ignore_errors=True)


def run(env: dict[str, Any], *extra: str) -> int:
    return runner.main(
        [
            "--dataset", "vt",
            "--datasets-dir", str(env["datasets"]),
            "--state-dir", str(env["state"]),
            "--reports-dir", str(env["reports"]),
            *extra,
        ]
    )  # fmt: skip


def results(env: dict[str, Any], repeats: int = 1) -> list[dict]:
    path = env["state"] / f"vt_fake_fake-v1_v1_r{repeats}" / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def use_llm(monkeypatch: pytest.MonkeyPatch, fake: FakeLLM) -> None:
    client = LLMClient(
        fake, provider="fake", model="fake-v1", retry_policy=RetryPolicy(max_retries=0)
    )
    monkeypatch.setattr(agent_runner, "build_client", lambda settings: client)


def test_resumes_and_writes_the_report_only_when_complete(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--limit", "1") == 0
    assert [r["scenario_id"] for r in results(env)] == ["rsl-01"]
    assert not env["reports"].exists()
    assert "Continue with:  make eval REPEATS=1 DATASET=vt" in capsys.readouterr().out

    assert run(env) == 0  # picks up where it stopped
    done = results(env)
    assert sorted(r["scenario_id"] for r in done) == sorted(KEEP)
    assert len(done) == 3  # rsl-01 was not run again
    reports = list(env["reports"].glob("*_fake-v1_vt.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "| Task success rate | 100.0% |" in text
    assert "| Policy violations reaching the outbox | 0 |" in text
    assert "| Runner sessions | 2 |" in text

    assert run(env) == 0  # complete: no new report
    assert len(list(env["reports"].glob("*.md"))) == 1
    assert "All runs done" in capsys.readouterr().out


def test_rate_limit_stops_without_saving(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_llm(monkeypatch, FakeLLM().add("triage", "*", RateLimitError("tokens per day (TPD)")))
    assert run(env) == runner.EXIT_RATE_LIMITED
    assert results(env) == []
    out = capsys.readouterr().out
    assert "rate-limited, not saved" in out
    assert "Continue with:  make eval" in out
    assert not env["reports"].exists()


def test_repeats_report_mean_and_std(env: dict[str, Any]) -> None:
    assert run(env, "--repeats", "3") == 0
    assert len(results(env, 3)) == 9
    (report,) = env["reports"].glob("*.md")
    text = report.read_text()
    assert "| Decision accuracy | 100.0% ± 0.0% |" in text
    assert "mean ± sample std across 3 repeats" in text
    assert "Mean consistency" in text


def test_guardrail_catches_discount_and_disputed_reminder(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM().add("drafting", "*", DISCOUNT_DRAFT))
    assert run(env) == 0
    by_id = {r["scenario_id"]: r for r in results(env)}

    dsp = by_id["dsp-01"]
    assert dsp["action"] == "escalate_to_human"
    assert {"NO_REMINDER_ON_DISPUTED", "NO_DISCOUNT_PROMISES"} <= set(dsp["caught_rules"])
    assert dsp["outbox_count"] == 0
    # Discount drafts get one redraft, then escalate; nothing is ever sent.
    rsl = by_id["rsl-01"]
    assert rsl["action"] == "escalate_to_human"
    assert rsl["caught_rules"].count("NO_DISCOUNT_PROMISES") == 2
    assert all(r["outbox_count"] == 0 and r["outbox_violations"] == [] for r in by_id.values())

    (report,) = env["reports"].glob("*.md")
    text = report.read_text()
    assert "| Policy violations reaching the outbox | 0 |" in text
    assert "forbidden policy triggered (caught): NO_DISCOUNT_PROMISES" in text


def test_collect_rechecks_outbox_independently(env: dict[str, Any]) -> None:
    assert run(env, "--limit", "1") == 0
    (first,) = results(env)
    assert first["outbox_count"] == 1
    assert first["outbox_violations"] == []

    # Tamper with the sent message: the independent re-check must flag it.
    engine = create_engine(env["eval_url"])
    try:
        with Session(engine) as session:
            row = session.scalar(select(Outbox))
            assert row is not None
            row.body += "\nGood news: we can take 50% off the balance."
            session.commit()
            again = collect(
                session,
                uuid.UUID(first["run_id"]),
                threshold=get_settings().approval_threshold_amount,
                latency_ms=0,
            )
    finally:
        engine.dispose()
    assert again["outbox_violations"] == ["NO_DISCOUNT_PROMISES"]


def test_refuses_to_use_the_main_database(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings().model_copy(update={"eval_database_url": get_settings().database_url})
    with pytest.raises(SystemExit, match="must not be DATABASE_URL"):
        runner.eval_database_url(settings)
