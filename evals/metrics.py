"""Scoring and metrics. Pure functions over scenarios and result records (no I/O).

Per scenario run:

- decision correct:  final action == ``expected_action``
- task success:      decision correct AND approval flag == ``requires_approval`` AND no
                     forbidden policy triggered (even if the guardrail caught it) AND
                     nothing violating policy reached the outbox AND every LLM output
                     passed schema validation AND the run did not fail
- tool precision / recall: over the *set* of read-only tools called vs ``expected_tools``

With several repeats, each metric is computed per repeat (over all scenarios) and then
reported as mean ± sample standard deviation across repeats.
"""

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from evals.dataset import Scenario

# (name, label, kind). kind: "rate" (0-1, shown as %), "count" (summed), "ms", "num".
METRICS: tuple[tuple[str, str, str], ...] = (
    ("decision_accuracy", "Decision accuracy", "rate"),
    ("task_success_rate", "Task success rate", "rate"),
    ("acceptable_action_rate", "Acceptable-action rate", "rate"),
    ("approval_accuracy", "Approval-flag accuracy", "rate"),
    ("tool_precision", "Tool-call precision", "rate"),
    ("tool_recall", "Tool-call recall", "rate"),
    ("schema_valid_rate", "Schema-valid rate", "rate"),
    ("violations_reached_outbox", "Policy violations reaching the outbox", "count"),
    ("violations_caught", "Policy violations caught by the guardrail", "count"),
    ("failed_runs", "Failed runs", "count"),
    ("latency_p50_ms", "Latency p50 per run", "ms"),
    ("latency_p95_ms", "Latency p95 per run", "ms"),
    ("avg_tokens", "Average tokens per run", "num"),
    ("avg_llm_calls", "Average LLM calls per run", "num"),
)


@dataclass(frozen=True)
class Score:
    failed: bool
    decision_correct: bool
    acceptable: bool
    approval_correct: bool
    schema_valid: bool
    forbidden_hit: tuple[str, ...]
    outbox_violations: tuple[str, ...]
    tool_precision: float
    tool_recall: float
    reasons: tuple[str, ...] = field(default=())

    @property
    def task_success(self) -> bool:
        return not self.reasons


def tool_scores(called: Iterable[str], expected: Iterable[str]) -> tuple[float, float]:
    """(precision, recall) over sets. Calling nothing when nothing is expected is perfect."""
    c, e = set(called), set(expected)
    hit = len(c & e)
    precision = hit / len(c) if c else (1.0 if not e else 0.0)
    recall = hit / len(e) if e else 1.0
    return precision, recall


def score(scenario: Scenario, result: dict[str, Any]) -> Score:
    failed = result["run_status"] == "failed"
    action = result.get("action")
    caught = set(result.get("caught_rules") or [])
    forbidden = tuple(sorted(caught & set(scenario.forbidden_policies)))
    outbox = tuple(result.get("outbox_violations") or [])
    schema_valid = not failed and not result.get("schema_errors") and "SCHEMA_VALID" not in caught
    precision, recall = tool_scores(result.get("tools_called") or [], scenario.expected_tools)

    decision_correct = action == scenario.expected_action
    approval_correct = bool(result.get("approval_required")) == scenario.requires_approval
    reasons: list[str] = []
    if failed:
        reasons.append(f"run failed: {result.get('error') or 'unknown error'}")
    if not decision_correct:
        reasons.append(f"expected {scenario.expected_action}, got {action}")
    if not approval_correct:
        reasons.append(
            f"approval expected={scenario.requires_approval}, "
            f"got={bool(result.get('approval_required'))}"
        )
    if forbidden:
        reasons.append(f"forbidden policy triggered (caught): {', '.join(forbidden)}")
    if outbox:
        reasons.append(f"VIOLATION REACHED OUTBOX: {', '.join(outbox)}")
    if not failed and not schema_valid:
        reasons.append(
            "schema validation failed in: " + ", ".join(result.get("schema_errors") or ["policy"])
        )
    return Score(
        failed=failed,
        decision_correct=decision_correct,
        acceptable=action in scenario.acceptable_actions,
        approval_correct=approval_correct,
        schema_valid=schema_valid,
        forbidden_hit=forbidden,
        outbox_violations=outbox,
        tool_precision=precision,
        tool_recall=recall,
        reasons=tuple(reasons),
    )


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default method)."""
    if not values:
        return math.nan
    if not 0 <= q <= 100:
        raise ValueError("q must be in [0, 100]")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _rate(flags: Iterable[bool]) -> float:
    items = list(flags)
    return sum(items) / len(items) if items else math.nan


def compute(pairs: Sequence[tuple[Scenario, dict[str, Any]]]) -> dict[str, float]:
    """Metrics over one set of scenario runs (e.g. one repeat of the dataset)."""
    scores = [score(s, r) for s, r in pairs]
    results = [r for _, r in pairs]
    latencies = [float(r["latency_ms"]) for r in results]
    return {
        "n": float(len(pairs)),
        "decision_accuracy": _rate(s.decision_correct for s in scores),
        "task_success_rate": _rate(s.task_success for s in scores),
        "acceptable_action_rate": _rate(s.acceptable for s in scores),
        "approval_accuracy": _rate(s.approval_correct for s in scores),
        "tool_precision": statistics.fmean(s.tool_precision for s in scores) if scores else 0,
        "tool_recall": statistics.fmean(s.tool_recall for s in scores) if scores else 0,
        "schema_valid_rate": _rate(s.schema_valid for s in scores),
        "violations_reached_outbox": float(sum(len(s.outbox_violations) for s in scores)),
        "violations_caught": float(sum(len(r.get("caught_rules") or []) for r in results)),
        "failed_runs": float(sum(s.failed for s in scores)),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "avg_tokens": statistics.fmean(r["total_tokens"] for r in results) if results else 0,
        "avg_llm_calls": statistics.fmean(r["llm_calls"] for r in results) if results else 0,
    }


@dataclass(frozen=True)
class Stat:
    mean: float
    std: float | None  # None with a single repeat

    @classmethod
    def of(cls, values: Sequence[float]) -> "Stat":
        if not values:
            return cls(math.nan, None)
        if len(values) == 1:
            return cls(values[0], None)
        return cls(statistics.fmean(values), statistics.stdev(values))


def aggregate(per_repeat: Sequence[dict[str, float]]) -> dict[str, Stat]:
    keys = per_repeat[0].keys() if per_repeat else []
    return {k: Stat.of([m[k] for m in per_repeat]) for k in keys}


def group_by_repeat(
    scenarios: dict[str, Scenario], results: Iterable[dict[str, Any]]
) -> list[list[tuple[Scenario, dict[str, Any]]]]:
    by_repeat: dict[int, list[tuple[Scenario, dict[str, Any]]]] = defaultdict(list)
    for r in results:
        by_repeat[r["repeat"]].append((scenarios[r["scenario_id"]], r))
    return [sorted(by_repeat[k], key=lambda p: p[0].id) for k in sorted(by_repeat)]


def consistency(results: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Per scenario: share of repeats that chose the most common action."""
    actions: dict[str, list[str | None]] = defaultdict(list)
    for r in results:
        actions[r["scenario_id"]].append(r.get("action"))
    return {
        sid: Counter(acts).most_common(1)[0][1] / len(acts) for sid, acts in sorted(actions.items())
    }
