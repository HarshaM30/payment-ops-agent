"""The eval dataset file is valid and covers what the plan asks for."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.dataset import BLOCKING_RULES, Scenario, load_dataset
from evals.runner import estimate
from payops.db.models.enums import ActionType, InvoiceStatus, ScenarioType
from payops.policy.rules import RULES, Outcome, PolicyInput

DATASET = load_dataset("v1")


def test_v1_has_40_valid_scenarios_covering_every_type() -> None:
    assert len(DATASET.scenarios) == 40
    counts = Counter(s.scenario_type for s in DATASET.scenarios)
    assert set(counts) == set(ScenarioType)
    named = [t for t in ScenarioType if t is not ScenarioType.CURRENT]
    assert all(counts[t] >= 5 for t in named)
    assert sum("discount_bait" in s.tags for s in DATASET.scenarios) >= 4


def test_every_expected_action_appears() -> None:
    actions = {s.expected_action for s in DATASET.scenarios}
    assert actions == {"reminder", "payment_plan_offer", "escalate_to_human", "no_action"}


def test_disputed_scenarios_forbid_reminders() -> None:
    for s in DATASET.scenarios:
        if s.fixture.invoice(s.fixture.targets[0]).status is InvoiceStatus.DISPUTED:
            assert "NO_REMINDER_ON_DISPUTED" in s.forbidden_policies
            assert s.expected_action == "escalate_to_human"


def test_blocking_rules_match_the_policy_engine() -> None:
    probe = PolicyInput(
        action_type=ActionType.REMINDER,
        invoice_status=InvoiceStatus.DISPUTED,
        balance=Decimal("1"),
        approval_threshold=Decimal("10"),
        subject="10% discount",
        body="we waive it",
        schema_valid=False,
    )
    blocking = {r.rule for r in (rule(probe) for rule in RULES) if r.outcome is Outcome.BLOCK}
    assert blocking == BLOCKING_RULES


def _scenario(**overrides: object) -> dict:
    raw = json.loads(DATASET.path.read_text().splitlines()[0])
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_action": "no_action"}, "expected_action must be one of"),
        ({"expected_tools": ["draft_message"]}, "read-only tools"),
        ({"forbidden_policies": ["APPROVAL_OVER_THRESHOLD"]}, "blocking rules"),
        ({"id": "Bad Id"}, "String should match pattern"),
        ({"surprise": 1}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_scenarios_are_rejected(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(_scenario(**overrides))


def test_invalid_fixture_is_rejected() -> None:
    raw = _scenario()
    raw["fixture"]["targets"] = ["ZZ"]
    with pytest.raises(ValidationError, match="unknown invoice refs"):
        Scenario.model_validate(raw)
    raw = _scenario()
    raw["fixture"]["invoices"][-1]["status"] = "paid"  # but not fully paid
    with pytest.raises(ValidationError, match="status 'paid' iff"):
        Scenario.model_validate(raw)


def test_load_dataset_reports_line_and_duplicates(tmp_path: Path) -> None:
    line = DATASET.path.read_text().splitlines()[0]
    (tmp_path / "vx").mkdir()
    (tmp_path / "vx" / "scenarios.jsonl").write_text(line + "\n" + line + "\n")
    with pytest.raises(ValueError, match="duplicate scenario ids"):
        load_dataset("vx", tmp_path)
    (tmp_path / "vx" / "scenarios.jsonl").write_text(line + "\n{}\n")
    with pytest.raises(ValueError, match=r"scenarios.jsonl:2"):
        load_dataset("vx", tmp_path)


def test_estimate_uses_measured_tokens_and_daily_quota() -> None:
    lines, per_run = estimate(30, [], "groq", "openai/gpt-oss-120b")
    assert per_run == 7000
    assert "~210,000 tokens" in lines[0]
    assert "needs ~2 days of quota" in lines[1]
    done = [{"total_tokens": 4000, "llm_calls": 5}, {"total_tokens": 6000, "llm_calls": 7}]
    lines, per_run = estimate(10, done, "groq", "openai/gpt-oss-120b")
    assert per_run == 5000
    assert "~60 LLM calls, ~50,000 tokens" in lines[0]
    assert "fits in one day" in lines[1]
    lines, _ = estimate(10, [], "fake", "fake-v1")
    assert "no quota used" in lines[1]
