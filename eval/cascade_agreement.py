"""Cascade agreement report (ranking_upgrade.md item O-3).

A free, model-vs-model calibration signal mined from data every autopilot
run already writes — no new LLM calls, no prompt change.

Every autopilot-queued listing is scored twice by different model tiers:

  * ``original_triage.json`` — the pre-research Stage 5 snapshot from the
    small ``OPENROUTER_MODEL`` slot (verdict, confidence, matching/missing
    skills), written at ``process_queue.py:550``.
  * ``auto_assets.json`` — the post-research pass from the larger
    ``OPENROUTER_TAILOR_MODEL`` (verdict, confidence, and the structured
    ``updated_skills_match`` the ``_AUTO_PROMPT`` already emits), written at
    ``process_queue.py:649``.

This walks ``output/*/`` folders that have both files and diffs the two on
verdict, confidence, and skills — never touching the prose ``match_analysis``
(invariant 2). Read-only.

Two honesty caveats, printed with every run:
  (a) Censored sample — only listings that already cleared
      ``CONFIDENCE_THRESHOLD`` get the second pass, so this measures
      calibration *above* the gate and is structurally blind to small-model
      false negatives. E-3's human labels stay the primary ground truth.
  (b) Model-vs-model — this is agreement between two models, not human
      preference; weaker than E-3 but free and available for every autopilot
      run to date.

Usage:
    python -m eval.cascade_agreement
    python -m eval.cascade_agreement --output-root output --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from rapidfuzz.fuzz import token_set_ratio

DEFAULT_OUTPUT_ROOT = Path("output")

# Two model tiers describe the same skill in different vocabularies — the small
# Stage 5 model emits terse tags ("AI Evaluation"), the large post-research
# model emits descriptive phrases ("AI evaluation and model evaluation …"). So
# skills agreement is measured with fuzzy token-set matching, not exact set
# overlap (which is structurally ~0 on real data).
_SKILL_MATCH_THRESHOLD = 70


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _parse_skill_list(raw) -> set[str]:
    """Coerce a skills field to a lowercase set.

    Handles both storage shapes: original_triage's JSON-list *string* and
    auto_assets' already-parsed list.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return set()
    if isinstance(raw, list):
        return {str(s).strip().lower() for s in raw if str(s).strip()}
    return set()


def _coerce_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _fuzzy_overlap(a: set[str], b: set[str]) -> float | None:
    """Symmetric fuzzy overlap in [0, 1]; None when both sides are empty.

    A skill counts as "shared" if it token-set-matches any skill on the other
    side at or above ``_SKILL_MATCH_THRESHOLD``. The score is the fraction of
    both sides' skills that find such a match — 1.0 when every skill is
    covered both ways, 0.0 when none are. This tolerates the tag-vs-phrase
    vocabulary gap between the two model tiers that makes exact overlap ~0.
    """
    if not a and not b:
        return None
    if not a or not b:
        return 0.0

    def _covered(src: set[str], dst: set[str]) -> int:
        return sum(
            1 for s in src if any(token_set_ratio(s, d) >= _SKILL_MATCH_THRESHOLD for d in dst)
        )

    matched = _covered(a, b) + _covered(b, a)
    return matched / (len(a) + len(b))


def analyze_pair(orig: dict, auto: dict) -> dict | None:
    """Diff one (original_triage, auto_assets) pair. None if verdicts missing."""
    orig_verdict = (orig.get("verdict") or "").upper()
    auto_verdict = (auto.get("post_research_verdict") or "").upper()
    if not orig_verdict or not auto_verdict:
        return None

    orig_conf = _coerce_int(orig.get("confidence"))
    auto_conf = _coerce_int(auto.get("post_research_confidence"))
    conf_gap = (
        auto_conf - orig_conf
        if orig_conf is not None and auto_conf is not None
        else None
    )

    updated = auto.get("updated_skills_match")
    if not isinstance(updated, dict):
        updated = {}
    orig_matching = _parse_skill_list(orig.get("matching_skills"))
    auto_matching = _parse_skill_list(updated.get("matching"))
    orig_missing = _parse_skill_list(orig.get("missing_skills"))
    auto_missing = _parse_skill_list(updated.get("missing"))

    return {
        "orig_verdict": orig_verdict,
        "auto_verdict": auto_verdict,
        "verdict_agree": orig_verdict == auto_verdict,
        "orig_confidence": orig_conf,
        "auto_confidence": auto_conf,
        "confidence_gap": conf_gap,
        "matching_overlap": _fuzzy_overlap(orig_matching, auto_matching),
        "missing_overlap": _fuzzy_overlap(orig_missing, auto_missing),
    }


def walk(output_root: Path = DEFAULT_OUTPUT_ROOT) -> list[dict]:
    """Collect one analysis record per output folder holding both files."""
    records: list[dict] = []
    if not output_root.exists():
        return records
    for folder in sorted(p for p in output_root.iterdir() if p.is_dir()):
        orig = _load_json(folder / "original_triage.json")
        auto = _load_json(folder / "auto_assets.json")
        if orig is None or auto is None:
            continue
        rec = analyze_pair(orig, auto)
        if rec is not None:
            rec["folder"] = folder.name
            records.append(rec)
    return records


def summarize(records: list[dict]) -> dict:
    """Aggregate agreement metrics across records."""
    n = len(records)
    if n == 0:
        return {"n": 0}
    verdict_agree = sum(1 for r in records if r["verdict_agree"])
    conf_gaps = [r["confidence_gap"] for r in records if r["confidence_gap"] is not None]
    matching_j = [r["matching_overlap"] for r in records if r["matching_overlap"] is not None]
    missing_j = [r["missing_overlap"] for r in records if r["missing_overlap"] is not None]
    # Directional confidence drift: does the large post-research model tend to
    # raise or lower confidence vs. the small pre-research model?
    conf_up = sum(1 for g in conf_gaps if g > 0)
    conf_down = sum(1 for g in conf_gaps if g < 0)

    return {
        "n": n,
        "verdict_agreement_rate": verdict_agree / n,
        "verdict_agree": verdict_agree,
        "mean_confidence_gap": statistics.mean(conf_gaps) if conf_gaps else None,
        "median_confidence_gap": statistics.median(conf_gaps) if conf_gaps else None,
        "confidence_raised": conf_up,
        "confidence_lowered": conf_down,
        "mean_matching_overlap": statistics.mean(matching_j) if matching_j else None,
        "mean_missing_overlap": statistics.mean(missing_j) if missing_j else None,
    }


def _fmt(value, pct: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1%}" if pct else f"{value:+.1f}"


def print_summary(summary: dict) -> None:
    print(f"\n{'=' * 60}")
    print("  Cascade Agreement — small Stage 5 vs. large post-research")
    print(f"{'=' * 60}")
    n = summary.get("n", 0)
    if n == 0:
        print("  No autopilot output folders with both triage + assets found.")
        print(f"{'=' * 60}")
        return
    print(f"  Pairs analyzed:            {n}")
    rate = summary["verdict_agreement_rate"]
    print(f"  Verdict agreement:         {rate:.1%}  ({summary['verdict_agree']}/{n})")
    print(f"  Mean confidence gap:       {_fmt(summary['mean_confidence_gap'])}  "
          f"(large − small)")
    print(f"  Median confidence gap:     {_fmt(summary['median_confidence_gap'])}")
    print(f"  Confidence raised/lowered: {summary['confidence_raised']} up / "
          f"{summary['confidence_lowered']} down")
    mj = summary["mean_matching_overlap"]
    mm = summary["mean_missing_overlap"]
    print(f"  Matching-skills overlap:   {mj:.2f} (fuzzy)" if mj is not None else
          "  Matching-skills overlap:   n/a")
    print(f"  Missing-skills overlap:    {mm:.2f} (fuzzy)" if mm is not None else
          "  Missing-skills overlap:    n/a")
    print(f"{'─' * 60}")
    print("  Caveats: (a) censored — only above-threshold listings get the")
    print("  second pass, so false negatives are invisible here. (b) this is")
    print("  model-vs-model, not human preference. E-3's labels stay primary.")
    print(f"{'=' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cascade agreement report (O-3)")
    parser.add_argument(
        "--output-root", default=str(DEFAULT_OUTPUT_ROOT),
        help="Root dir of per-job output folders (default: output)",
    )
    parser.add_argument("--json", action="store_true", help="Emit summary as JSON")
    args = parser.parse_args()

    records = walk(Path(args.output_root))
    summary = summarize(records)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_summary(summary)


if __name__ == "__main__":
    main()
