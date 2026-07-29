"""Pareto-frontier model report (ranking_upgrade.md item E-2).

Reads E-1's accumulated ``eval/runs.csv`` and answers the question this whole
thread started from — per stage, which models are Pareto-*dominated* (strictly
worse accuracy *and* no cheaper than some other model) and which sit on the
frontier. Adding a new slug (``deepseek/deepseek-v4-flash``, …) shows up here
automatically once E-1 has logged a run for it.

Two per-stage views, because the two stages have different accuracy metrics
and different cost bases:

  * **Stage 5 (scoring)** — accuracy = verdict accuracy; cost = ``$/1k
    listings`` (already priced against the Stage 5 slug in E-1). Clean.
  * **Stage 1 (extraction)** — accuracy = extraction accuracy; cost = avg
    total tokens, used as a *proxy* because ``runs.csv`` prices only the
    Stage 5 slug (per-stage token pricing is a documented E-1 limitation).
    Labeled as a proxy so it's never mistaken for dollars.

A frontier comparison holds the *other* stage fixed to be clean; with sparse
data the other slug may vary across the points being compared, so this prints
a confound warning when it does.

Offline / read-only.

Usage:
    python -m eval.frontier
    python -m eval.frontier --runs eval/runs.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RUNS_PATH = Path("eval/runs.csv")


@dataclass
class RunRow:
    stage1_model: str
    stage5_model: str
    extraction_accuracy: float
    verdict_accuracy: float
    avg_tokens: float
    cost_per_1k: float | None
    pricing_verified: str
    pricing_last_updated: str


@dataclass
class Point:
    model: str
    accuracy: float
    cost: float | None
    n_runs: int
    other_models: set[str]  # the other stage's slugs seen for this model


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def load_runs(path: Path = DEFAULT_RUNS_PATH) -> list[RunRow]:
    if not path.exists():
        return []
    rows: list[RunRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(RunRow(
                stage1_model=r.get("stage1_model", ""),
                stage5_model=r.get("stage5_model", ""),
                extraction_accuracy=_to_float(r.get("extraction_accuracy")) or 0.0,
                verdict_accuracy=_to_float(r.get("verdict_accuracy")) or 0.0,
                avg_tokens=_to_float(r.get("avg_tokens")) or 0.0,
                cost_per_1k=_to_float(r.get("cost_per_1k_listings")),
                pricing_verified=r.get("pricing_verified", ""),
                pricing_last_updated=r.get("pricing_last_updated", ""),
            ))
    return rows


def build_points(
    rows: list[RunRow],
    model_of,
    acc_of,
    cost_of,
    other_of,
) -> list[Point]:
    """Aggregate runs into one Point per model (mean accuracy + mean cost)."""
    accs: dict[str, list[float]] = defaultdict(list)
    costs: dict[str, list[float]] = defaultdict(list)
    others: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        model = model_of(r)
        if not model:
            continue
        accs[model].append(acc_of(r))
        c = cost_of(r)
        if c is not None:
            costs[model].append(c)
        others[model].add(other_of(r))
    points: list[Point] = []
    for model, acc_list in accs.items():
        cost_list = costs[model]
        points.append(Point(
            model=model,
            accuracy=sum(acc_list) / len(acc_list),
            cost=(sum(cost_list) / len(cost_list)) if cost_list else None,
            n_runs=len(acc_list),
            other_models=others[model],
        ))
    return points


def pareto_frontier(points: list[Point]) -> set[str]:
    """Return the set of non-dominated model names (higher acc, lower cost).

    Points with no cost are excluded — they can't be placed on a cost/accuracy
    frontier. A point is dominated if another has accuracy ≥ and cost ≤ it,
    with at least one strict inequality.
    """
    priced = [p for p in points if p.cost is not None]
    frontier: set[str] = set()
    for p in priced:
        dominated = False
        for q in priced:
            if q.model == p.model:
                continue
            if (
                q.accuracy >= p.accuracy
                and q.cost <= p.cost
                and (q.accuracy > p.accuracy or q.cost < p.cost)
            ):
                dominated = True
                break
        if not dominated:
            frontier.add(p.model)
    return frontier


def _print_stage(title: str, points: list[Point], cost_label: str) -> None:
    print(f"\n  {title}")
    print(f"  {'-' * len(title)}")
    if not points:
        print("  (no runs recorded for this stage)")
        return
    frontier = pareto_frontier(points)
    # Highest accuracy first, then cheapest.
    ranked = sorted(points, key=lambda p: (-p.accuracy, p.cost if p.cost is not None else 1e18))
    print(f"  {'Model':<34} {'Accuracy':>9} {cost_label:>16} {'Runs':>5}  Status")
    print(f"  {'-' * 34} {'-' * 9} {'-' * 16} {'-' * 5}  {'-' * 12}")
    for p in ranked:
        cost_str = "n/a" if p.cost is None else f"{p.cost:,.4f}"
        if p.cost is None:
            status = "unpriced"
        elif p.model in frontier:
            status = "◆ frontier"
        else:
            status = "dominated"
        print(f"  {p.model:<34} {p.accuracy:>8.1%} {cost_str:>16} {p.n_runs:>5}  {status}")
    # Confound warning: a clean frontier holds the other stage fixed.
    varying = [p.model for p in points if len(p.other_models) > 1]
    if varying:
        print(f"  ⚠ other-stage slug varies across runs for: {', '.join(sorted(varying))}")
        print("    — frontier may be confounded; pin the other stage to compare cleanly.")


def report(runs_path: Path = DEFAULT_RUNS_PATH) -> None:
    rows = load_runs(runs_path)
    print()
    print("  " + "═" * 56)
    print("    MODEL PARETO FRONTIER (from eval/runs.csv)")
    print("  " + "═" * 56)
    if not rows:
        print(f"\n  No runs found at {runs_path}. Run `python -m eval.eval ...` first.")
        print()
        return

    last = rows[-1]
    verified = str(last.pricing_verified).strip().lower() in ("true", "1", "yes")
    banner = "verified" if verified else "UNVERIFIED placeholders — verify before trusting $"
    print(f"  Pricing: {banner} (table dated {last.pricing_last_updated})")

    _print_stage(
        "Stage 5 (scoring): verdict accuracy vs. $/1k listings",
        build_points(
            rows,
            model_of=lambda r: r.stage5_model,
            acc_of=lambda r: r.verdict_accuracy,
            cost_of=lambda r: r.cost_per_1k,
            other_of=lambda r: r.stage1_model,
        ),
        cost_label="$/1k listings",
    )
    _print_stage(
        "Stage 1 (extraction): extraction accuracy vs. avg tokens (proxy)",
        build_points(
            rows,
            model_of=lambda r: r.stage1_model,
            acc_of=lambda r: r.extraction_accuracy,
            cost_of=lambda r: r.avg_tokens,
            other_of=lambda r: r.stage5_model,
        ),
        cost_label="avg tokens",
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pareto-frontier model report (E-2)")
    parser.add_argument(
        "--runs", default=str(DEFAULT_RUNS_PATH),
        help="Path to eval/runs.csv (default: eval/runs.csv)",
    )
    args = parser.parse_args()
    report(Path(args.runs))


if __name__ == "__main__":
    main()
