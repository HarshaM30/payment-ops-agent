"""Markdown eval report, and a diff of two reports (``make eval-compare``).

The report ends with an HTML comment holding the metric summary as JSON, so two
reports can be compared without parsing the tables.
"""

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.dataset import Scenario
from evals.metrics import METRICS, Stat, aggregate, compute, consistency, group_by_repeat, score

SUMMARY_RE = re.compile(r"<!-- eval-summary\n(.*?)\n-->", re.DOTALL)
TYPE_COLUMNS = (
    ("decision_accuracy", "Decision acc."),
    ("task_success_rate", "Task success"),
    ("approval_accuracy", "Approval acc."),
    ("violations_caught", "Caught"),
    ("violations_reached_outbox", "Reached outbox"),
    ("avg_tokens", "Avg tokens"),
)
_KINDS = {name: kind for name, _, kind in METRICS}


def fmt(value: float, kind: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if kind == "rate":
        return f"{value * 100:.1f}%"
    if kind == "ms":
        return f"{value:,.0f} ms"
    if kind == "count":
        return f"{value:g}" if float(value).is_integer() else f"{value:.1f}"
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.1f}"


def fmt_stat(stat: Stat, kind: str) -> str:
    if stat.std is None:
        return fmt(stat.mean, kind)
    return f"{fmt(stat.mean, kind)} ± {fmt(stat.std, kind)}"


def _summary(metrics: dict[str, Stat]) -> dict[str, dict[str, float | None]]:
    def clean(v: float | None) -> float | None:
        return None if v is None or math.isnan(v) else round(v, 6)

    return {k: {"mean": clean(s.mean), "std": clean(s.std)} for k, s in metrics.items()}


def render(
    *,
    config: dict[str, Any],
    scenarios: dict[str, Scenario],
    results: list[dict[str, Any]],
) -> str:
    repeats = group_by_repeat(scenarios, results)
    overall = aggregate([compute(r) for r in repeats])

    lines = [
        f"# Eval report: {config['llm_provider']}/{config['llm_model']} on dataset "
        f"{config['dataset_version']}",
        "",
        "## Configuration",
        "",
        "| Setting | Value |",
        "| --- | --- |",
    ]
    for key, label in (
        ("llm_provider", "LLM provider"),
        ("llm_model", "LLM model"),
        ("prompt_version", "Prompt version"),
        ("dataset_version", "Dataset version"),
        ("dataset_sha256", "Dataset sha256"),
        ("scenarios", "Scenarios"),
        ("repeats", "Repeats"),
        ("approval_threshold", "Approval threshold"),
        ("git_sha", "Git SHA"),
        ("sessions", "Runner sessions"),
        ("started_at", "First scenario started"),
        ("finished_at", "Last scenario finished"),
        ("generated_at", "Report generated"),
    ):
        value = config.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines.append(f"| {label} | {value} |")

    lines += ["", "## Metrics", "", "| Metric | Value |", "| --- | --- |"]
    for name, label, kind in METRICS:
        lines.append(f"| {label} | {fmt_stat(overall[name], kind)} |")
    if len(repeats) > 1:
        lines.append("")
        lines.append(f"Values are mean ± sample std across {len(repeats)} repeats.")

    # Per scenario type: metrics per repeat, then aggregated.
    by_type: dict[str, list[dict[str, float]]] = defaultdict(list)
    for rep in repeats:
        groups: dict[str, list[tuple[Scenario, dict[str, Any]]]] = defaultdict(list)
        for pair in rep:
            groups[pair[0].scenario_type.value].append(pair)
        for stype, pairs in groups.items():
            by_type[stype].append(compute(pairs))
    lines += [
        "",
        "## By scenario type",
        "",
        "| Scenario type | n | " + " | ".join(label for _, label in TYPE_COLUMNS) + " |",
        "| --- | ---: | " + " | ".join("---:" for _ in TYPE_COLUMNS) + " |",
    ]
    for stype in sorted(by_type):
        stats = aggregate(by_type[stype])
        cells = [fmt_stat(stats[k], _KINDS[k]) for k, _ in TYPE_COLUMNS]
        lines.append(f"| {stype} | {stats['n'].mean:g} | " + " | ".join(cells) + " |")

    if len(repeats) > 1:
        cons = consistency(results)
        mean_cons = sum(cons.values()) / len(cons) if cons else math.nan
        unstable = {k: v for k, v in cons.items() if v < 1}
        lines += [
            "",
            "## Consistency across repeats",
            "",
            f"Mean consistency (share of repeats choosing the most common action): "
            f"{fmt(mean_cons, 'rate')}.",
        ]
        if unstable:
            lines += ["", "| Scenario | Consistency |", "| --- | ---: |"]
            lines += [f"| {k} | {fmt(v, 'rate')} |" for k, v in unstable.items()]

    failures = []
    for r in sorted(results, key=lambda r: (r["scenario_id"], r["repeat"])):
        s = scenarios[r["scenario_id"]]
        sc = score(s, r)
        if not sc.task_success:
            rep = f", repeat {r['repeat'] + 1}" if len(repeats) > 1 else ""
            failures.append(f"- **{s.id}** ({s.scenario_type.value}{rep}): {'; '.join(sc.reasons)}")
    lines += ["", f"## Failures ({len(failures)} of {len(results)} scenario runs)", ""]
    lines += failures or ["None."]

    lines += [
        "",
        "## Definitions",
        "",
        "- **Decision accuracy**: final action equals the scenario's `expected_action`.",
        "- **Task success**: correct action, correct approval flag, no forbidden policy "
        "triggered (even if caught), no violation in the outbox, all LLM outputs valid.",
        "- **Acceptable-action rate**: final action is one of `acceptable_actions`.",
        "- **Caught**: blocking policy results during the run (the guardrail stopped a draft).",
        "- **Reached outbox**: outbox rows that fail an independent re-check of the policy "
        "rules (including sends that needed approval but had none). Must be 0.",
        "- **Latency**: wall-clock time of one run, including client-side throttling waits.",
        "",
        "<!-- eval-summary",
        json.dumps({"config": config, "metrics": _summary(overall)}, sort_keys=True),
        "-->",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------- compare


def load_summary(path: Path) -> dict[str, Any]:
    match = SUMMARY_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{path} has no eval-summary block")
    return json.loads(match.group(1))


def compare(a: Path, b: Path) -> str:
    sa, sb = load_summary(a), load_summary(b)
    lines = [
        f"A: {a.name} ({sa['config']['llm_model']}, prompt {sa['config']['prompt_version']})",
        f"B: {b.name} ({sb['config']['llm_model']}, prompt {sb['config']['prompt_version']})",
        "",
        "| Metric | A | B | B - A |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, label, kind in METRICS:
        ma, mb = sa["metrics"].get(name, {}), sb["metrics"].get(name, {})
        va, vb = ma.get("mean"), mb.get("mean")
        delta = "n/a"
        if va is not None and vb is not None:
            d = vb - va
            delta = ("+" if d > 0 else "") + (
                f"{d * 100:.1f} pp" if kind == "rate" else fmt(d, kind)
            )
        sta = Stat(va if va is not None else math.nan, ma.get("std"))
        stb = Stat(vb if vb is not None else math.nan, mb.get("std"))
        lines.append(f"| {label} | {fmt_stat(sta, kind)} | {fmt_stat(stb, kind)} | {delta} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two eval reports.")
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    args = parser.parse_args(argv)
    sys.stdout.write(compare(args.a, args.b) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
