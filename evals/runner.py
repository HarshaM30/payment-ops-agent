"""Eval runner (``make eval``): run every scenario through the real agent graph.

Each scenario runs alone in a dedicated eval database (wiped before every scenario),
sequentially, to respect free-tier rate limits.

Resumable: every finished scenario run is appended to
``evals/runs/<dataset>_<provider>_<model>_<prompt>_r<repeats>/results.jsonl`` as soon as it
finishes. A rerun with the same settings skips what is already there. If the provider
rate-limits a run (for example the daily token quota runs out), that run is discarded,
the runner stops, and it prints the command to continue later. The markdown report is
written only once every scenario run is done.

A run that fails because the eval database itself went away is treated the same way:
discarded, not scored. Exit codes: 0 done (or stopped by --limit / --token-budget),
75 stopped by a rate limit or a database outage (try again later).
"""

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session

from evals import fixtures
from evals.collect import collect, is_infrastructure_error, is_rate_limited
from evals.dataset import DATASETS_DIR, Dataset, Scenario, load_dataset
from evals.report import render
from payops import clock
from payops.agents.runner import execute_run
from payops.config import Settings, get_settings
from payops.db.admin import ensure_database, upgrade_head
from payops.llm.models import daily_limit, resolve_model
from payops.schemas.api import RunCreate
from payops.services.runs import create_run

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = ROOT / "evals" / "runs"
EXIT_RATE_LIMITED = 75  # EX_TEMPFAIL: try again later
# Used until this eval has finished scenarios to measure. Measured on the manual Groq run
# in Milestone 3: ~6.6K tokens and ~5 LLM calls per invoice.
DEFAULT_TOKENS_PER_RUN = 7_000
DEFAULT_CALLS_PER_RUN = 6

log = logging.getLogger("evals.runner")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")


def git_sha() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "evals/*.py", "evals/datasets"],  # noqa: S607
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def eval_database_url(settings: Settings) -> str:
    if settings.eval_database_url:
        url = settings.eval_database_url
    else:
        base = make_url(settings.database_url)
        url = base.set(database=f"{base.database}_eval").render_as_string(hide_password=False)
    if make_url(url).database == make_url(settings.database_url).database:
        raise SystemExit("Refusing to run: the eval database must not be DATABASE_URL's database")
    return url


# ------------------------------------------------------------------------- state


@dataclass
class EvalState:
    """The results file and metadata of one eval configuration."""

    directory: Path

    @property
    def results_path(self) -> Path:
        return self.directory / "results.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.directory / "meta.json"

    def load_meta(self) -> dict[str, Any] | None:
        if not self.meta_path.exists():
            return None
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def save_meta(self, meta: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.meta_path)

    def load_results(self) -> list[dict[str, Any]]:
        if not self.results_path.exists():
            return []
        results = []
        for line in self.results_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skipping a truncated line in %s", self.results_path)
        return results

    def append(self, result: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.results_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, sort_keys=True, default=str) + "\n")
            f.flush()

    def clear(self) -> None:
        for path in (self.results_path, self.meta_path):
            path.unlink(missing_ok=True)


# ------------------------------------------------------------------------- estimate


def estimate(
    remaining: int, done: list[dict[str, Any]], provider: str, model: str
) -> tuple[list[str], float]:
    """Lines describing the expected cost of the remaining runs, and tokens per run."""
    measured = [r for r in done if r.get("total_tokens")]
    if measured:
        per_run = sum(r["total_tokens"] for r in measured) / len(measured)
        calls = sum(r["llm_calls"] for r in measured) / len(measured)
        source = f"measured over {len(measured)} finished runs"
    else:
        per_run, calls = DEFAULT_TOKENS_PER_RUN, DEFAULT_CALLS_PER_RUN
        source = "default estimate from the Milestone 3 Groq run"
    tokens = per_run * remaining
    lines = [
        f"Estimate for {remaining} remaining runs: ~{calls * remaining:,.0f} LLM calls, "
        f"~{tokens:,.0f} tokens ({per_run:,.0f} tokens/run, {source})."
    ]
    tpd, rpd = daily_limit(provider, model)
    if provider == "fake":
        lines.append("Fake provider: no API calls, no quota used.")
    elif tpd:
        days = max(tokens / tpd, (calls * remaining) / rpd if rpd else 0)
        verdict = "fits in one day" if days <= 1 else f"needs ~{math.ceil(days)} days of quota"
        lines.append(
            f"{provider}/{model} free tier: {tpd:,} tokens/day, {rpd:,} requests/day: {verdict} "
            "(minus anything already used today)."
        )
    else:
        lines.append(f"No known daily quota for {provider}/{model}.")
    return lines, per_run


# ------------------------------------------------------------------------- one scenario


def run_scenario(
    engine: Engine, scenario: Scenario, repeat: int, settings: Settings
) -> dict[str, Any]:
    started = clock.now()
    with Session(engine, expire_on_commit=False) as session:
        fixtures.reset(session)
        invoice_ids = fixtures.load(session, scenario, clock.today())
        run, _ = create_run(session, RunCreate(invoice_ids=invoice_ids), actor="eval")
        t0 = time.perf_counter()
        execute_run(session, run.id, settings=settings)
        latency = round((time.perf_counter() - t0) * 1000)
        result = collect(
            session, run.id, threshold=settings.approval_threshold_amount, latency_ms=latency
        )
    return {
        "scenario_id": scenario.id,
        "scenario_type": scenario.scenario_type.value,
        "repeat": repeat,
        "started_at": started.isoformat(),
        "finished_at": clock.now().isoformat(),
        **result,
    }


# ------------------------------------------------------------------------- main


def resume_command(args: argparse.Namespace) -> str:
    parts = ["make eval", f"REPEATS={args.repeats}"]
    if args.dataset != "v1":
        parts.append(f"DATASET={args.dataset}")
    return " ".join(parts)


def build_report(
    state: EvalState,
    meta: dict[str, Any],
    dataset: Dataset,
    results: list[dict[str, Any]],
    reports_dir: Path,
) -> Path:
    now = datetime.now(UTC)
    config = {
        **{k: meta[k] for k in ("llm_provider", "llm_model", "prompt_version", "repeats")},
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256[:12],
        "scenarios": len(dataset.scenarios),
        "approval_threshold": meta["approval_threshold"],
        "git_sha": sorted({s["git_sha"] for s in meta["sessions"]}),
        "sessions": len(meta["sessions"]),
        "started_at": min(r["started_at"] for r in results),
        "finished_at": max(r["finished_at"] for r in results),
        "generated_at": now.isoformat(timespec="seconds"),
        "results_file": str(state.results_path.relative_to(ROOT))
        if state.results_path.is_relative_to(ROOT)
        else str(state.results_path),
    }
    text = render(config=config, scenarios=dataset.by_id(), results=results)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{now:%Y-%m-%dT%H-%M-%S}_{slug(meta['llm_model'])}_{dataset.version}.md"
    path.write_text(text, encoding="utf-8")
    return path


def parse_args(argv: list[str] | None, settings: Settings) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--dataset", default=settings.eval_dataset_version)
    p.add_argument("--repeats", type=int, default=settings.eval_repeats)
    p.add_argument("--limit", type=int, help="Run at most N more scenario runs, then stop")
    p.add_argument(
        "--token-budget", type=int, help="Stop before a run that would exceed N tokens this session"
    )
    p.add_argument("--fresh", action="store_true", help="Discard saved results and start over")
    p.add_argument("--rebuild-report", action="store_true", help="Write a new report if complete")
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    p.add_argument("--datasets-dir", type=Path, default=DATASETS_DIR, help=argparse.SUPPRESS)
    p.add_argument("--reports-dir", type=Path, default=ROOT / settings.eval_reports_dir)
    args = p.parse_args(argv)
    if args.repeats < 1:
        p.error("--repeats must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # A failed run's error is already in its result line; skip the agent's traceback.
    logging.getLogger("payops.runner").setLevel(logging.CRITICAL)
    settings = get_settings()
    args = parse_args(argv, settings)
    out = sys.stdout

    provider = settings.llm_provider
    model = resolve_model(provider, settings.llm_model)
    dataset = load_dataset(args.dataset, args.datasets_dir)
    key = f"{dataset.version}_{provider}_{slug(model)}_{settings.prompt_version}_r{args.repeats}"
    state = EvalState(args.state_dir / key)
    if args.fresh:
        state.clear()

    meta = state.load_meta()
    if meta and meta["dataset_sha256"] != dataset.sha256:
        out.write(
            f"Saved results in {state.directory} were made with a different version of the "
            "dataset file. Rerun with --fresh to start over.\n"
        )
        return 2
    meta = meta or {
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256,
        "llm_provider": provider,
        "llm_model": model,
        "prompt_version": settings.prompt_version,
        "repeats": args.repeats,
        "approval_threshold": str(settings.approval_threshold_amount),
        "sessions": [],
        "report": None,
    }

    results = state.load_results()
    done = {(r["scenario_id"], r["repeat"]) for r in results}
    todo = [
        (rep, s)
        for rep in range(args.repeats)
        for s in dataset.scenarios
        if (s.id, rep) not in done
    ]
    total = len(dataset.scenarios) * args.repeats
    out.write(
        f"Eval {dataset.version} with {provider}/{model}, prompt {settings.prompt_version}: "
        f"{len(dataset.scenarios)} scenarios x {args.repeats} repeat(s) = {total} runs; "
        f"{len(done)} done, {len(todo)} to go.\nResults: {state.results_path}\n"
    )
    lines, per_run = estimate(len(todo), results, provider, model)
    out.write("\n".join(lines) + "\n\n")

    stop_reason: str | None = None
    if todo:
        url = eval_database_url(settings)
        ensure_database(url)
        engine = create_engine(url, pool_pre_ping=True)
        upgrade_head(engine)
        meta["sessions"].append({"started_at": clock.now().isoformat(), "git_sha": git_sha()})
        state.save_meta(meta)

        used = 0
        ran = 0
        try:
            for rep, scenario in todo:
                if args.limit is not None and ran >= args.limit:
                    stop_reason = "limit"
                    break
                if args.token_budget is not None and used + per_run > args.token_budget:
                    stop_reason = "budget"
                    break
                label = f"[{len(done) + 1}/{total}] {scenario.id}" + (
                    f" (repeat {rep + 1})" if args.repeats > 1 else ""
                )
                out.write(f"{label} ... ")
                out.flush()
                result = run_scenario(engine, scenario, rep, settings)
                result["git_sha"] = meta["sessions"][-1]["git_sha"]
                used += result["total_tokens"]
                if result["run_status"] == "failed" and is_rate_limited(result["error"]):
                    out.write(f"rate-limited, not saved\n  {result['error'][:300]}\n")
                    stop_reason = "rate_limit"
                    break
                if result["run_status"] == "failed" and is_infrastructure_error(result["error"]):
                    out.write(f"database error, not saved\n  {result['error'][:300]}\n")
                    stop_reason = "infrastructure"
                    break
                state.append(result)
                results.append(result)
                done.add((scenario.id, rep))
                ran += 1
                out.write(
                    f"{result['action'] or result['run_status']}"
                    f" (expected {scenario.expected_action}),"
                    f" {result['total_tokens']:,} tokens, {result['latency_ms'] / 1000:.1f}s\n"
                )
        except KeyboardInterrupt:
            stop_reason = "interrupted"
            out.write("\ninterrupted; the unfinished run was not saved\n")
        finally:
            engine.dispose()
        out.write(f"\nThis session: {ran} runs, {used:,} tokens.\n")

    remaining = total - len(done)
    if remaining:
        why = {
            "rate_limit": "The provider rate limit was reached (likely the daily quota).",
            "infrastructure": "The eval database failed mid-run.",
            "limit": f"Stopped after --limit {args.limit} runs.",
            "budget": f"Stopped before exceeding --token-budget {args.token_budget:,}."
            if args.token_budget
            else "",
            "interrupted": "Interrupted.",
        }.get(stop_reason or "", "")
        out.write(
            f"{why} {len(done)}/{total} runs done, {remaining} left. No report yet.\n"
            f"Continue with:  {resume_command(args)}\n"
        )
        return EXIT_RATE_LIMITED if stop_reason in {"rate_limit", "infrastructure"} else 0

    if meta.get("report") and not args.rebuild_report and (ROOT / meta["report"]).exists():
        out.write(f"All runs done. Report: {meta['report']} (use --rebuild-report for a new one)\n")
        return 0
    path = build_report(state, meta, dataset, results, args.reports_dir)
    meta["report"] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    state.save_meta(meta)
    out.write(f"All {total} runs done. Report: {meta['report']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
