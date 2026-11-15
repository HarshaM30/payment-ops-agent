"""Eval metrics, checked against hand-computed values."""

import math
from pathlib import Path

import pytest

from evals.dataset import load_dataset
from evals.metrics import (
    Stat,
    aggregate,
    compute,
    consistency,
    group_by_repeat,
    percentile,
    score,
    tool_scores,
)
from evals.report import compare, fmt, fmt_stat, load_summary, render

DATASET = load_dataset("v1")
SCENARIOS = DATASET.by_id()
ALL_TOOLS = ["get_customer", "get_invoices", "get_payment_history"]


def result(sid: str, repeat: int = 0, **kw: object) -> dict:
    s = SCENARIOS[sid]
    base = {
        "scenario_id": sid,
        "scenario_type": s.scenario_type.value,
        "repeat": repeat,
        "run_status": "completed",
        "error": None,
        "action": s.expected_action,
        "approval_required": s.requires_approval,
        "outbox_violations": [],
        "caught_rules": [],
        "schema_errors": [],
        "tools_called": list(ALL_TOOLS),
        "llm_calls": 6,
        "total_tokens": 1000,
        "latency_ms": 100,
        "started_at": "2026-11-15T00:00:00+00:00",
        "finished_at": "2026-11-15T00:00:01+00:00",
    }
    return base | kw


# ---------------------------------------------------------------- primitives


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([1, 2, 3, 4, 10], 50, 3.0),
        ([1, 2, 3, 4, 10], 95, 8.8),  # pos 3.8 -> 4 + 0.8 * (10 - 4)
        ([10, 20], 95, 19.5),
        ([5], 95, 5.0),
        ([3, 1, 2], 0, 1.0),
        ([3, 1, 2], 100, 3.0),
    ],
)
def test_percentile(values: list[float], q: float, expected: float) -> None:
    assert percentile(values, q) == pytest.approx(expected)


def test_percentile_edges() -> None:
    assert math.isnan(percentile([], 50))
    with pytest.raises(ValueError, match="q must be"):
        percentile([1], 101)


@pytest.mark.parametrize(
    ("called", "expected", "precision", "recall"),
    [
        (["a", "b", "a"], ["a", "c"], 0.5, 0.5),
        (["a", "b", "c"], ["a", "b", "c"], 1.0, 1.0),
        (["a"], ["a", "b", "c", "d"], 1.0, 0.25),
        ([], [], 1.0, 1.0),
        ([], ["a"], 0.0, 0.0),
        (["x"], [], 0.0, 1.0),
    ],
)
def test_tool_scores(called: list, expected: list, precision: float, recall: float) -> None:
    assert tool_scores(called, expected) == (pytest.approx(precision), pytest.approx(recall))


def test_stat_mean_and_sample_std() -> None:
    s = Stat.of([0.8, 0.9, 1.0])
    assert s.mean == pytest.approx(0.9)
    assert s.std == pytest.approx(0.1)  # sqrt((0.01 + 0 + 0.01) / (3 - 1))
    s = Stat.of([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert s.mean == pytest.approx(5.0)
    assert s.std == pytest.approx(math.sqrt(32 / 7))
    assert Stat.of([0.75]) == Stat(0.75, None)
    assert math.isnan(Stat.of([]).mean)


# ---------------------------------------------------------------- scoring


def test_score_perfect_run() -> None:
    sc = score(SCENARIOS["rsl-01"], result("rsl-01"))
    assert sc.task_success
    assert sc.decision_correct
    assert sc.schema_valid
    assert sc.reasons == ()


def test_score_wrong_action_and_approval() -> None:
    sc = score(
        SCENARIOS["rsl-01"],
        result("rsl-01", action="payment_plan_offer", approval_required=True),
    )
    assert not sc.decision_correct
    assert not sc.approval_correct
    assert not sc.acceptable
    assert sc.reasons == (
        "expected reminder, got payment_plan_offer",
        "approval expected=False, got=True",
    )


def test_caught_forbidden_rule_fails_task_but_decision_is_correct() -> None:
    # The model drafted a reminder on a disputed invoice; policy converted it to an
    # escalation. The final action is right, but the task does not count as a success.
    r = result("dsp-01", caught_rules=["NO_REMINDER_ON_DISPUTED"])
    sc = score(SCENARIOS["dsp-01"], r)
    assert sc.decision_correct
    assert sc.forbidden_hit == ("NO_REMINDER_ON_DISPUTED",)
    assert not sc.task_success


def test_caught_rule_not_forbidden_does_not_fail_task() -> None:
    # rsl-01 doesn't forbid NO_REMINDER_ON_DISPUTED (it can't trigger there anyway).
    sc = score(SCENARIOS["rsl-01"], result("rsl-01", caught_rules=["NO_REMINDER_ON_DISPUTED"]))
    assert sc.task_success


def test_outbox_violation_and_schema_errors_fail_task() -> None:
    sc = score(SCENARIOS["rsl-01"], result("rsl-01", outbox_violations=["NO_DISCOUNT_PROMISES"]))
    assert "VIOLATION REACHED OUTBOX: NO_DISCOUNT_PROMISES" in sc.reasons
    sc = score(SCENARIOS["rsl-01"], result("rsl-01", schema_errors=["drafting"]))
    assert not sc.schema_valid
    assert not sc.task_success


def test_failed_run() -> None:
    sc = score(
        SCENARIOS["rsl-01"], result("rsl-01", run_status="failed", action=None, error="boom")
    )
    assert sc.failed
    assert not sc.schema_valid
    assert sc.reasons[0] == "run failed: boom"


# ---------------------------------------------------------------- compute / aggregate


def test_compute_hand_computed() -> None:
    pairs = [
        (SCENARIOS["rsl-01"], result("rsl-01", latency_ms=100, total_tokens=1000)),
        # wrong action, still the right approval flag
        (
            SCENARIOS["rsl-02"],
            result("rsl-02", action="no_action", latency_ms=200, total_tokens=2000),
        ),
        # right action, one tool missing, guardrail caught a forbidden rule
        (
            SCENARIOS["dsp-01"],
            result(
                "dsp-01",
                tools_called=["get_customer", "get_invoices"],
                caught_rules=["NO_REMINDER_ON_DISPUTED"],
                latency_ms=300,
                total_tokens=3000,
                llm_calls=8,
            ),
        ),
        # needs approval but none was created
        (
            SCENARIOS["lrg-01"],
            result("lrg-01", approval_required=False, latency_ms=1000, total_tokens=6000),
        ),
    ]
    m = compute(pairs)
    assert m["n"] == 4
    assert m["decision_accuracy"] == pytest.approx(3 / 4)
    assert m["task_success_rate"] == pytest.approx(1 / 4)  # only rsl-01
    assert m["approval_accuracy"] == pytest.approx(3 / 4)
    assert m["acceptable_action_rate"] == pytest.approx(3 / 4)
    assert m["tool_precision"] == pytest.approx(1.0)
    assert m["tool_recall"] == pytest.approx((1 + 1 + 2 / 3 + 1) / 4)
    assert m["violations_caught"] == 1
    assert m["violations_reached_outbox"] == 0
    assert m["latency_p50_ms"] == pytest.approx(250)  # between 200 and 300
    assert m["latency_p95_ms"] == pytest.approx(895)  # 300 + 0.85 * 700
    assert m["avg_tokens"] == pytest.approx(3000)
    assert m["avg_llm_calls"] == pytest.approx(6.5)


def test_repeats_aggregate_and_consistency() -> None:
    results = [
        result("rsl-01", 0),
        result("rsl-02", 0),
        result("rsl-01", 1),
        result("rsl-02", 1, action="no_action"),
        result("rsl-01", 2),
        result("rsl-02", 2, action="no_action"),
    ]
    repeats = group_by_repeat(SCENARIOS, results)
    assert [len(r) for r in repeats] == [2, 2, 2]
    stats = aggregate([compute(r) for r in repeats])
    # Decision accuracy per repeat: 1.0, 0.5, 0.5 -> mean 2/3, std sqrt(1/12)
    assert stats["decision_accuracy"].mean == pytest.approx(2 / 3)
    assert stats["decision_accuracy"].std == pytest.approx(math.sqrt(1 / 12))
    assert consistency(results) == {"rsl-01": 1.0, "rsl-02": pytest.approx(2 / 3)}


# ---------------------------------------------------------------- report


def test_fmt() -> None:
    assert fmt(0.825, "rate") == "82.5%"
    assert fmt(1234.4, "ms") == "1,234 ms"
    assert fmt(3.0, "count") == "3"
    assert fmt(7012.0, "num") == "7,012"
    assert fmt(float("nan"), "rate") == "n/a"
    assert fmt_stat(Stat(0.9, 0.1), "rate") == "90.0% ± 10.0%"
    assert fmt_stat(Stat(0.9, None), "rate") == "90.0%"


CONFIG = {
    "llm_provider": "fake",
    "llm_model": "fake-v1",
    "prompt_version": "v1",
    "dataset_version": "v1",
    "repeats": 1,
}


def test_render_single_repeat_shows_plain_values() -> None:
    text = render(config=CONFIG, scenarios=SCENARIOS, results=[result("rsl-01")])
    assert "| Decision accuracy | 100.0% |" in text
    assert "±" not in text
    assert "## Failures (0 of 1 scenario runs)" in text


def test_render_repeats_shows_mean_std_and_failures(tmp_path: Path) -> None:
    results = [result("rsl-01", r) for r in range(3)] + [
        result("rsl-02", 0),
        result("rsl-02", 1, action="no_action"),
        result("rsl-02", 2),
    ]
    text = render(config=CONFIG | {"repeats": 3}, scenarios=SCENARIOS, results=results)
    assert "| Decision accuracy | 83.3% ± 28.9% |" in text  # 1.0, 0.5, 1.0
    assert "## Consistency across repeats" in text
    assert "| rsl-02 | 66.7% |" in text
    assert "**rsl-02** (reliable_slightly_late, repeat 2): expected reminder, got no_action" in text

    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text(text)
    b.write_text(render(config=CONFIG, scenarios=SCENARIOS, results=[result("rsl-01")]))
    assert load_summary(a)["metrics"]["decision_accuracy"]["mean"] == pytest.approx(5 / 6)
    diff = compare(a, b)
    assert "| Decision accuracy | 83.3% ± 28.9% | 100.0% | +16.7 pp |" in diff


def test_load_summary_requires_block(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("# nothing here")
    with pytest.raises(ValueError, match="no eval-summary"):
        load_summary(p)
