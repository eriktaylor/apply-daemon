"""Eval harness for testing the unified LLM extraction + matching pipeline.

Usage:
    python -m eval.eval --input eval/eval_example.csv
    python -m eval.eval --input eval/eval_example.csv --model openai/gpt-4o-mini --runs 3
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.model_pricing import (
    LAST_UPDATED as PRICING_LAST_UPDATED,
)
from eval.model_pricing import (
    PRICING_VERIFIED,
    cost_per_1k_listings,
)
from src.profile_loader import load_profile
from src.triage import TriageSession

RUNS_CSV_PATH = Path("eval/runs.csv")


@dataclass
class EvalRow:
    email_text: str
    expected_listings: list[dict]  # [{title, company, verdict}, ...]


@dataclass
class EvalResult:
    email_text_preview: str
    expected: list[dict]
    extracted: list[dict]  # What the LLM returned
    runs: int
    tokens: list[int]
    latencies_ms: list[int]
    json_parse_ok: bool = True  # Whether LLM output parsed without fallback

    @property
    def extraction_accuracy(self) -> float:
        """How many expected listings were found (by title match)."""
        if not self.expected:
            return 1.0
        expected_titles = {e["title"].lower() for e in self.expected}
        found_titles = {e.get("title", "").lower() for e in self.extracted}
        matched = expected_titles & found_titles
        return len(matched) / len(expected_titles)

    @property
    def verdict_accuracy(self) -> float:
        """Of matched listings, how many verdicts are correct."""
        expected_map = {e["title"].lower(): e["verdict"].upper() for e in self.expected}
        correct = 0
        matched = 0
        for ext in self.extracted:
            title = ext.get("title", "").lower()
            if title in expected_map:
                matched += 1
                if ext.get("verdict", "").upper() == expected_map[title]:
                    correct += 1
        return correct / matched if matched else 0.0

    @property
    def avg_tokens(self) -> float:
        return statistics.mean(self.tokens) if self.tokens else 0

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0


def load_eval_data(path: Path) -> list[EvalRow]:
    """Load eval data from CSV with email_text and expected_listings columns."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            expected = json.loads(row["expected_listings"])
            rows.append(
                EvalRow(
                    email_text=row["email_text"],
                    expected_listings=expected,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Main eval loop
# ---------------------------------------------------------------------------

def run_eval(
    eval_data: list[EvalRow],
    model: str | None,
    runs_per_email: int = 1,
    stage1_model: str | None = None,
) -> tuple[list[EvalResult], int]:
    """Run each email through the unified triage and collect results.

    ``model`` overrides the Stage 5 scoring slot; ``stage1_model`` overrides
    the Stage 1 extraction slot (both default to their .env slots when None).

    Returns (results, first_call_latency_ms).
    """
    profile = load_profile()

    results = []
    first_call_ms = 0

    with TriageSession(
        profile["llm_context"], model=model, stage1_model=stage1_model,
    ) as session:
        for i, row in enumerate(eval_data):
            print(f"  [{i + 1}/{len(eval_data)}] Evaluating email...", flush=True)
            all_extracted = []
            tokens = []
            latencies = []
            json_parse_ok = True

            for _ in range(runs_per_email):
                listings = session.triage_email(
                    row.email_text, [], "JOB_DIGEST", "eval",
                )
                extracted = [
                    {"title": listing.title, "company": listing.company, "verdict": listing.verdict}
                    for listing in listings
                ]
                all_extracted = extracted  # Use last run
                tokens.append(sum(listing.tokens_used for listing in listings) if listings else 0)
                latencies.append(sum(listing.latency_ms for listing in listings) if listings else 0)

                # Check for JSON parse failures (fallback verdicts indicate parse failure)
                for listing in listings:
                    if listing.reason and "Unable to parse" in listing.reason:
                        json_parse_ok = False

            results.append(
                EvalResult(
                    email_text_preview=row.email_text[:80],
                    expected=row.expected_listings,
                    extracted=all_extracted,
                    runs=runs_per_email,
                    tokens=tokens,
                    latencies_ms=latencies,
                    json_parse_ok=json_parse_ok,
                )
            )

    return results, first_call_ms


# ---------------------------------------------------------------------------
# Advanced metrics
# ---------------------------------------------------------------------------

def compute_advanced_metrics(results: list[EvalResult]) -> dict:
    """Compute FPR, FNR, throughput, and JSON parse success rate."""
    false_positives = 0  # Model said YES, expected NO/SKIP
    false_positive_opportunities = 0  # Total expected NO/SKIP
    false_negatives = 0  # Model said NO, expected YES
    false_negative_opportunities = 0  # Total expected YES
    total_tokens = 0
    total_warm_ms = 0
    json_ok_count = sum(1 for r in results if r.json_parse_ok)

    for r in results:
        expected_map = {e["title"].lower(): e["verdict"].upper() for e in r.expected}

        for ext in r.extracted:
            title = ext.get("title", "").lower()
            model_verdict = ext.get("verdict", "").upper()
            if title not in expected_map:
                continue
            expected_verdict = expected_map[title]

            # FP: model YES, expected NO or SKIP
            if expected_verdict in ("NO", "SKIP"):
                false_positive_opportunities += 1
                if model_verdict == "YES":
                    false_positives += 1

            # FN: model NO, expected YES
            if expected_verdict == "YES":
                false_negative_opportunities += 1
                if model_verdict == "NO":
                    false_negatives += 1

        total_tokens += r.avg_tokens
        total_warm_ms += r.avg_latency_ms

    fpr = false_positives / false_positive_opportunities if false_positive_opportunities else 0.0
    fnr = false_negatives / false_negative_opportunities if false_negative_opportunities else 0.0
    throughput = (total_tokens / (total_warm_ms / 1000)) if total_warm_ms > 0 else 0.0
    json_parse_rate = json_ok_count / len(results) if results else 0.0

    return {
        "false_positive_rate": fpr,
        "false_negative_rate": fnr,
        "throughput_tok_per_sec": throughput,
        "json_parse_success_rate": json_parse_rate,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(
    results: list[EvalResult],
    model: str,
    first_call_ms: int = 0,
    stage1_model: str | None = None,
) -> None:
    """Print a summary report to stdout."""
    total = len(results)
    avg_extraction = statistics.mean(r.extraction_accuracy for r in results) if results else 0
    avg_verdict = statistics.mean(r.verdict_accuracy for r in results) if results else 0
    avg_tokens = statistics.mean(r.avg_tokens for r in results) if results else 0
    avg_latency = statistics.mean(r.avg_latency_ms for r in results) if results else 0

    metrics = compute_advanced_metrics(results)

    # Cost is priced against the Stage 5 scoring slug (the model under test);
    # None when the slug isn't in the pricing table.
    cost_1k = cost_per_1k_listings(model, avg_tokens)
    verified_note = "" if PRICING_VERIFIED else "  (UNVERIFIED)"

    print(f"\n{'=' * 60}")
    print(f"  Eval Report — stage5: {model}")
    if stage1_model:
        print(f"                stage1: {stage1_model}")
    print(f"{'=' * 60}")
    print(f"  Emails evaluated:      {total}")
    print(f"  Extraction accuracy:   {avg_extraction:.1%}")
    print(f"  Verdict accuracy:      {avg_verdict:.1%}")
    print(f"  JSON parse success:    {metrics['json_parse_success_rate']:.1%}")
    print(f"{'─' * 60}")
    print(f"  Avg latency:           {avg_latency:.0f}ms")
    print(f"  Avg tokens:            {avg_tokens:.0f}")
    print(f"  Throughput:            {metrics['throughput_tok_per_sec']:.0f} tok/s")
    if cost_1k is not None:
        print(f"  Est. $/1k listings:    ${cost_1k:.2f}{verified_note}")
    else:
        print(f"  Est. $/1k listings:    n/a (no pricing for {model})")
    print(f"  Pricing table dated:   {PRICING_LAST_UPDATED}"
          f"{'' if PRICING_VERIFIED else ' — placeholders, verify before trusting'}")
    print(f"{'─' * 60}")
    print(f"  False Positive Rate:   {metrics['false_positive_rate']:.1%}"
          f"  ({metrics['false_positives']} YES when expected NO)")
    print(f"  False Negative Rate:   {metrics['false_negative_rate']:.1%}"
          f"  ({metrics['false_negatives']} NO when expected YES)")
    print(f"{'=' * 60}")

    print(
        f"\n{'Email preview':<50} {'Extract':>8} {'Verdict':>8} "
        f"{'Tokens':>7} {'ms':>6} {'JSON':>5}"
    )
    print("-" * 90)
    for r in results:
        json_flag = "OK" if r.json_parse_ok else "FAIL"
        print(
            f"{r.email_text_preview:<50} {r.extraction_accuracy:>7.0%} "
            f"{r.verdict_accuracy:>7.0%} {r.avg_tokens:>7.0f} {r.avg_latency_ms:>6.0f} "
            f"{json_flag:>5}"
        )


def save_results(results: list[EvalResult], output_path: Path, model: str) -> None:
    """Save detailed results to CSV."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "email_preview", "expected_count", "extracted_count",
            "extraction_accuracy", "verdict_accuracy",
            "avg_tokens", "avg_latency_ms", "json_parse_ok", "model",
        ])
        for r in results:
            writer.writerow([
                r.email_text_preview,
                len(r.expected),
                len(r.extracted),
                f"{r.extraction_accuracy:.2f}",
                f"{r.verdict_accuracy:.2f}",
                f"{r.avg_tokens:.0f}",
                f"{r.avg_latency_ms:.0f}",
                r.json_parse_ok,
                model,
            ])
    print(f"\nDetailed results saved to {output_path}")


def append_run_summary(
    results: list[EvalResult],
    stage5_model: str,
    stage1_model: str,
    dataset: str,
    runs_per_email: int,
    path: Path = RUNS_CSV_PATH,
) -> None:
    """Append a one-row-per-run summary to eval/runs.csv (E-2 reads this).

    Both model slugs are recorded so Stage 1 and Stage 5 comparisons are
    each attributable, not conflated under the Stage 5 slug alone. The header
    is written once when the file is created.
    """
    total = len(results)
    avg_extraction = statistics.mean(r.extraction_accuracy for r in results) if results else 0
    avg_verdict = statistics.mean(r.verdict_accuracy for r in results) if results else 0
    avg_tokens = statistics.mean(r.avg_tokens for r in results) if results else 0
    cost_1k = cost_per_1k_listings(stage5_model, avg_tokens)

    header = [
        "timestamp", "dataset", "stage1_model", "stage5_model",
        "n_emails", "runs_per_email", "extraction_accuracy", "verdict_accuracy",
        "avg_tokens", "cost_per_1k_listings", "pricing_verified", "pricing_last_updated",
    ]
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(header)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            dataset,
            stage1_model,
            stage5_model,
            total,
            runs_per_email,
            f"{avg_extraction:.3f}",
            f"{avg_verdict:.3f}",
            f"{avg_tokens:.0f}",
            f"{cost_1k:.4f}" if cost_1k is not None else "",
            PRICING_VERIFIED,
            PRICING_LAST_UPDATED,
        ])
    print(f"Run summary appended to {path}")


def main():
    parser = argparse.ArgumentParser(description="Eval harness for unified LLM pipeline")
    parser.add_argument("--input", required=True, help="Path to eval CSV file")
    parser.add_argument(
        "--model", default=None,
        help="OpenRouter Stage 5 (scoring) model override (e.g. openai/gpt-4o-mini)",
    )
    parser.add_argument(
        "--stage1-model", default=None, dest="stage1_model",
        help="OpenRouter Stage 1 (extraction) model override; defaults to "
             "OPENROUTER_STAGE1_MODEL. Lets a nano-vs-flash-lite Stage 1 run be isolated.",
    )
    parser.add_argument("--runs", type=int, default=1, help="Runs per email")
    parser.add_argument("--output", default=None, help="Output CSV path")
    args = parser.parse_args()

    eval_data = load_eval_data(Path(args.input))
    print(f"Loaded {len(eval_data)} eval emails from {args.input}")

    model = args.model  # None means use OPENROUTER_MODEL from .env
    display_model = (
        model
        if model
        else os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite")
    )
    display_stage1 = (
        args.stage1_model
        if args.stage1_model
        else os.getenv("OPENROUTER_STAGE1_MODEL", "openai/gpt-5.4-nano")
    )
    print(
        f"Running eval with stage5={display_model}, stage1={display_stage1}, "
        f"runs_per_email={args.runs}"
    )

    results, _ = run_eval(
        eval_data, model=model, runs_per_email=args.runs,
        stage1_model=args.stage1_model,
    )
    print_report(results, display_model, stage1_model=display_stage1)

    output_path = Path(args.output) if args.output else Path(f"eval/results_{display_model}.csv")
    save_results(results, output_path, display_model)
    append_run_summary(
        results, display_model, display_stage1, args.input, args.runs,
    )


if __name__ == "__main__":
    main()
