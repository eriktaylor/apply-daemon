"""M-4 experiment — does listwise Stage 5 scoring beat pointwise?

Answers three questions on real listings, before any live code changes:

1. **Cost.** Pointwise re-sends the candidate profile on every listing;
   listwise amortizes it across a batch. Stage 5 is 61% of run spend and ~96%
   input tokens, so this is measurable and should be decisive on its own.
2. **Agreement.** Do the two methods reach the same verdict? Disagreement is
   the interesting case, not a failure — pointwise is the incumbent, not the
   ground truth.
3. **Which one is right, where they differ.** The labeled eval set is far too
   small to answer this (8 emails / 11 listings — one flip moves accuracy 9
   points), so this uses a better signal that already exists: autopilot's
   post-research verdict from a larger model that read a research dossier
   (``ranking_upgrade.md`` O-3's cascade). Where a listing has one, it acts
   as a referee.

**This spends real tokens** — it re-scores listings through OpenRouter. Every
call is metered and logged, so `report --spend` shows the bill. Start with
``--limit 20``; scoring 20 listings both ways costs well under $0.10.

Usage:
    python -m eval.listwise_compare --limit 20 --batch 10 --dry-run
    python -m eval.listwise_compare --limit 20 --batch 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

from src.db import Database
from src.listing_card import parse_skill_list
from src.model_usage import log_response_usage
from src.triage import get_confidence_threshold, get_openrouter_config

logger = logging.getLogger(__name__)

# One shared preamble, N listings, N verdicts. The saving comes from sending
# the profile once instead of once per listing; the accuracy claim comes from
# the model seeing candidates side by side.
_LISTWISE_PROMPT = """\
You are a recruiting assistant evaluating job listings against a candidate's profile.

## Candidate profile
{profile}

## Job listings
{listings_block}

## Instructions
Evaluate EVERY listing above against the candidate profile, independently but with
full awareness of the others — use the comparison to calibrate, so that a listing you
rate 90 is genuinely a better match than one you rate 70.

For each listing return:
- `id`: the listing id exactly as given.
- `verdict`: YES, MAYBE, or NO.
- `confidence`: integer 0-100, your confidence that this is a strong match.
- `matching_skills`: up to 3 requirements from the listing the candidate clearly has.
- `missing_skills`: up to 3 requirements stated in the listing that the candidate lacks.

Return ONLY a JSON object of the form:
{{"listings": [{{"id": "...", "verdict": "...", "confidence": 0, "matching_skills": [], "missing_skills": []}}]}}

Every id given must appear exactly once in your response.
"""


@dataclass
class Comparison:
    """Per-listing pointwise vs listwise outcome."""

    id: str
    title: str
    pointwise_verdict: str
    pointwise_confidence: int
    listwise_verdict: str | None = None
    listwise_confidence: int | None = None
    post_research_verdict: str | None = None

    @property
    def agree(self) -> bool | None:
        if self.listwise_verdict is None:
            return None
        return self.pointwise_verdict == self.listwise_verdict

    @property
    def referee(self) -> str | None:
        """Which method the post-research verdict backs, where it exists."""
        if not self.post_research_verdict or self.agree is not False:
            return None
        if self.post_research_verdict == self.pointwise_verdict:
            return "pointwise"
        if self.post_research_verdict == self.listwise_verdict:
            return "listwise"
        return "neither"


@dataclass
class Totals:
    pointwise_prompt: int = 0
    pointwise_completion: int = 0
    listwise_prompt: int = 0
    listwise_completion: int = 0
    pointwise_calls: int = 0
    listwise_calls: int = 0
    rows: list[Comparison] = field(default_factory=list)


def _post_research_verdict(job_id: str) -> str | None:
    """Autopilot's large-model verdict for this listing, if it ran."""
    from src.cli import AUTO_ASSETS_FILE, OUTPUT_DIR, _read_text
    from src.file_utils import find_output_folder

    folder = find_output_folder(job_id, OUTPUT_DIR)
    if not folder:
        return None
    raw = _read_text(folder / AUTO_ASSETS_FILE)
    if not raw:
        return None
    try:
        return (json.loads(raw) or {}).get("post_research_verdict")
    except json.JSONDecodeError:
        return None


def _format_listing(row) -> str:
    desc = (row["job_summary"] or row["reason"] or "")[:1200]
    return (
        f"### id: {row['id']}\n"
        f"Title: {row['title']}\n"
        f"Company: {row['company']}\n"
        f"Location: {row['location'] or 'not specified'}\n"
        f"Salary: {row['salary'] or 'not listed'}\n"
        f"Description: {desc}\n"
    )


def score_via_claude_cli(model: str, profile: str, rows: list,
                         timeout_s: int = 300) -> tuple[dict, int, int, float]:
    """Score a batch through the Claude Code CLI (`claude -p --model ...`).

    This is the membership path made measurable: a subprocess cannot reach the
    *calling* session's model, but it can start its own, so Haiku and Sonnet
    are callable head-to-head against the OpenRouter arms. Returns
    (by_id, input_tokens, output_tokens, reported_cost_usd) — the CLI reports
    real usage, including cache hits, which OpenRouter's list pricing cannot
    show us.
    """
    import subprocess

    block = "\n".join(_format_listing(r) for r in rows)
    prompt = _LISTWISE_PROMPT.format(profile=profile, listings_block=block)
    prompt += "\n\nRespond with ONLY the JSON object, no prose, no code fence."

    # Prompt goes on stdin, not argv: passing it as an argument fails without
    # a TTY (backgrounded runs error with "Input must be provided either
    # through stdin or as a prompt argument"), and these prompts are long
    # enough to risk ARG_MAX.
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=timeout_s,
    )
    by_id: dict[str, dict] = {}
    if proc.returncode != 0:
        logger.warning("claude CLI failed (rc=%d): %s", proc.returncode,
                       (proc.stderr or "")[-300:])
        return by_id, 0, 0, 0.0
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("claude CLI returned non-JSON envelope")
        return by_id, 0, 0, 0.0

    usage = envelope.get("usage", {}) or {}
    # Cache reads/creations are real input the model processed; counting only
    # `input_tokens` would understate the batch by orders of magnitude.
    in_tok = (int(usage.get("input_tokens", 0) or 0)
              + int(usage.get("cache_read_input_tokens", 0) or 0)
              + int(usage.get("cache_creation_input_tokens", 0) or 0))
    out_tok = int(usage.get("output_tokens", 0) or 0)
    cost = float(envelope.get("total_cost_usd", 0.0) or 0.0)

    text = _strip_fence(envelope.get("result", "") or "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("claude CLI result was not parseable JSON")
        return by_id, in_tok, out_tok, cost
    for item in data.get("listings", []) or []:
        if isinstance(item, dict) and item.get("id"):
            try:
                by_id[str(item["id"])] = {
                    "verdict": str(item.get("verdict", "")).upper(),
                    "confidence": int(item.get("confidence", 0) or 0),
                }
            except (TypeError, ValueError):
                continue
    return by_id, in_tok, out_tok, cost


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        for part in parts:
            cleaned = part.removeprefix("json").strip()
            if cleaned.startswith("{"):
                return cleaned
    return text


def score_listwise(client, model: str, profile: str, rows: list) -> tuple[dict, int, int]:
    """Score a batch in one call. Returns (by_id, prompt_tokens, completion_tokens).

    Parses per item: a malformed entry costs that listing only, and the caller
    can retry it pointwise. All-or-nothing batching would make one bad
    response lose the whole batch — the blast-radius risk the plan flagged.
    """
    block = "\n".join(_format_listing(r) for r in rows)
    prompt = _LISTWISE_PROMPT.format(profile=profile, listings_block=block)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200 * len(rows) + 500,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    log_response_usage(resp, model, "eval_listwise")
    usage = getattr(resp, "usage", None)
    p_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
    c_tok = int(getattr(usage, "completion_tokens", 0) or 0)

    by_id: dict[str, dict] = {}
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        logger.warning("Listwise batch returned unparseable JSON")
        return by_id, p_tok, c_tok
    for item in data.get("listings", []) or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            by_id[str(item["id"])] = {
                "verdict": str(item.get("verdict", "")).upper(),
                "confidence": int(item.get("confidence", 0) or 0),
                "matching_skills": parse_skill_list(item.get("matching_skills")),
                "missing_skills": parse_skill_list(item.get("missing_skills")),
            }
        except (TypeError, ValueError):
            continue
    return by_id, p_tok, c_tok


def load_gold() -> dict[str, str]:
    """job_id[:8] -> Sonnet's post-research verdict, from autopilot output.

    The best referee available: a larger model that additionally read a
    research dossier. Not a pure same-input comparison — it knows more than
    either scorer — but it is the standard the pipeline already trusts enough
    to auto-pass NO verdicts on.
    """
    import glob
    import os
    gold: dict[str, str] = {}
    for path in glob.glob("output/*/auto_assets.json"):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        verdict = (data or {}).get("post_research_verdict")
        if verdict:
            gold[os.path.basename(os.path.dirname(path))[-8:]] = str(verdict).upper()
    return gold


def run(limit: int, batch: int, dry_run: bool, gold_only: bool = False,
        emit: str | None = None, apply_dir: str | None = None,
        shuffle: bool = False, seed: int = 0,
        model_override: str | None = None,
        via_claude: str | None = None,
        dump: str | None = None) -> int:
    api_key, model = get_openrouter_config()
    if model_override:
        model = model_override
    gold = load_gold() if (gold_only or emit or apply_dir) else {}

    with Database() as db:
        rows = db.conn.execute(
            "SELECT * FROM listings WHERE verdict IS NOT NULL "
            "AND job_summary IS NOT NULL AND job_summary != '' "
            "ORDER BY date_ingested DESC",
        ).fetchall()
    if gold_only or emit or apply_dir:
        rows = [r for r in rows if r["id"][:8] in gold]
    rows = rows[:limit]

    if shuffle:
        # Listwise models over-rank early items. A fixed seed keeps arms
        # comparable while breaking the date_ingested ordering that every
        # previous run shared — position bias was the largest unmeasured
        # confound in the first three arms.
        import random
        random.Random(seed).shuffle(rows)

    if not rows:
        print("No scored listings with summaries available.")
        return 1

    n_batches = (len(rows) + batch - 1) // batch
    print(f"\n  {len(rows)} listings · batch size {batch} → "
          f"{n_batches} listwise call(s) vs {len(rows)} pointwise")
    print(f"  model: {model}" + ("  · shuffled" if shuffle else "  · date order"))
    if gold:
        print(f"  gold standard: {len([r for r in rows if r['id'][:8] in gold])} "
              "listings carry a Sonnet post-research verdict")

    # --emit writes the batch prompts for an in-session model to answer; the
    # answers come back via --apply. Same emit/apply handshake as `cli tailor`,
    # because a subprocess cannot reach the calling session's model.
    if emit:
        from pathlib import Path

        from src.profile_loader import load_profile
        outdir = Path(emit)
        outdir.mkdir(parents=True, exist_ok=True)
        profile = load_profile()["llm_context"]
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            block = "\n".join(_format_listing(r) for r in chunk)
            (outdir / f"batch_{i // batch + 1}.txt").write_text(
                _LISTWISE_PROMPT.format(profile=profile, listings_block=block),
                encoding="utf-8",
            )
        print(f"  wrote {n_batches} prompt(s) to {outdir}/ — answer each as JSON,")
        print(f"  save as {outdir}/batch_N.json, then re-run with --apply {outdir}")
        return 0

    if dry_run:
        est_pointwise = sum(len(_format_listing(r)) for r in rows)
        print(f"  Pointwise re-sends the profile {len(rows)}x; "
              f"listwise sends it {n_batches}x.")
        print(f"  Listing text total: ~{est_pointwise // 4:,} tokens "
              "(sent once either way)\n")
        print("  Dry run — nothing scored, nothing spent.")
        return 0

    if apply_dir:
        from pathlib import Path
        by_id: dict[str, dict] = {}
        for f in sorted(Path(apply_dir).glob("batch_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"  skipping unparseable {f.name}")
                continue
            for item in data.get("listings", []) or []:
                if isinstance(item, dict) and item.get("id"):
                    by_id[str(item["id"])] = {
                        "verdict": str(item.get("verdict", "")).upper(),
                        "confidence": int(item.get("confidence", 0) or 0),
                    }
        totals = Totals()
        for r in rows:
            totals.rows.append(Comparison(
                id=r["id"], title=r["title"],
                pointwise_verdict=(r["verdict"] or "").upper(),
                pointwise_confidence=int(r["confidence"] or 0),
                post_research_verdict=gold.get(r["id"][:8]),
            ))
            totals.pointwise_prompt += int(r["tokens_used"] or 0)
            totals.pointwise_calls += 1
        for row in totals.rows:
            got = by_id.get(row.id)
            if got:
                row.listwise_verdict = got["verdict"]
                row.listwise_confidence = got["confidence"]
        totals.listwise_calls = n_batches
        _report(totals, model, label="in-session")
        return 0

    if via_claude:
        from src.profile_loader import load_profile
        profile = load_profile()["llm_context"]
        totals = Totals()
        for r in rows:
            totals.rows.append(Comparison(
                id=r["id"], title=r["title"],
                pointwise_verdict=(r["verdict"] or "").upper(),
                pointwise_confidence=int(r["confidence"] or 0),
                post_research_verdict=gold.get(r["id"][:8]),
            ))
            totals.pointwise_prompt += int(r["tokens_used"] or 0)
            totals.pointwise_calls += 1
        by_id: dict[str, dict] = {}
        cli_cost = 0.0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            print(f"  {via_claude} batch {i // batch + 1}/{n_batches} "
                  f"({len(chunk)} listings)…")
            scored, in_tok, out_tok, cost = score_via_claude_cli(
                via_claude, profile, chunk)
            by_id.update(scored)
            totals.listwise_prompt += in_tok
            totals.listwise_completion += out_tok
            totals.listwise_calls += 1
            cli_cost += cost
        for row in totals.rows:
            got = by_id.get(row.id)
            if got:
                row.listwise_verdict = got["verdict"]
                row.listwise_confidence = got["confidence"]
        if dump:
            from pathlib import Path
            Path(dump).write_text(json.dumps({
                r.id: {"title": r.title, "verdict": r.listwise_verdict,
                       "confidence": r.listwise_confidence,
                       "pointwise_confidence": r.pointwise_confidence}
                for r in totals.rows if r.listwise_verdict
            }, indent=1), encoding="utf-8")
            print(f"  per-listing scores → {dump}")
        _report(totals, model, label=f"claude/{via_claude}",
                override_cost=cli_cost)
        return 0

    if not api_key:
        print("OPENROUTER_API_KEY not set.")
        return 1

    import openai

    from src.profile_loader import load_profile
    profile = load_profile()["llm_context"]
    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    totals = Totals()
    # Pointwise numbers come from what the pipeline already recorded — those
    # calls happened, and re-running them would spend twice to learn nothing.
    for r in rows:
        totals.rows.append(Comparison(
            id=r["id"], title=r["title"],
            pointwise_verdict=(r["verdict"] or "").upper(),
            pointwise_confidence=int(r["confidence"] or 0),
            post_research_verdict=gold.get(r["id"][:8]) or _post_research_verdict(r["id"]),
        ))
        totals.pointwise_prompt += int(r["tokens_used"] or 0)
        totals.pointwise_calls += 1

    by_id: dict[str, dict] = {}
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        print(f"  listwise batch {i // batch + 1}/{n_batches} ({len(chunk)} listings)…")
        scored, p_tok, c_tok = score_listwise(client, model, profile, chunk)
        by_id.update(scored)
        totals.listwise_prompt += p_tok
        totals.listwise_completion += c_tok
        totals.listwise_calls += 1

    for row in totals.rows:
        got = by_id.get(row.id)
        if got:
            row.listwise_verdict = got["verdict"]
            row.listwise_confidence = got["confidence"]

    if dump:
        from pathlib import Path
        Path(dump).write_text(json.dumps({
            r.id: {"title": r.title, "verdict": r.listwise_verdict,
                   "confidence": r.listwise_confidence,
                   "pointwise_confidence": r.pointwise_confidence}
            for r in totals.rows if r.listwise_verdict
        }, indent=1), encoding="utf-8")
        print(f"  per-listing scores → {dump}")

    _report(totals, model)
    return 0


def _report(t: Totals, model: str, label: str = "listwise",
            override_cost: float | None = None) -> None:
    from eval.model_pricing import cost_for_usage

    scored = [r for r in t.rows if r.listwise_verdict]
    agreed = [r for r in scored if r.agree]
    disagreed = [r for r in scored if r.agree is False]

    print("\n  " + "=" * 58)
    print(f"    M-4 — {label.upper()} vs POINTWISE")
    print("  " + "=" * 58)

    # Accuracy against the Sonnet gold standard, where available.
    refereed_all = [r for r in t.rows if r.post_research_verdict]
    if refereed_all:
        pw = sum(1 for r in refereed_all
                 if r.pointwise_verdict == r.post_research_verdict)
        lw_rows = [r for r in refereed_all if r.listwise_verdict]
        lw = sum(1 for r in lw_rows
                 if r.listwise_verdict == r.post_research_verdict)
        print(f"\n  Agreement with Sonnet gold standard "
              f"({len(refereed_all)} listings):")
        print(f"    pointwise  {pw}/{len(refereed_all)} = "
              f"{pw / len(refereed_all):.0%}")
        if lw_rows:
            print(f"    {label:<10} {lw}/{len(lw_rows)} = {lw / len(lw_rows):.0%}")

    print(f"\n  Coverage:   {len(scored)}/{len(t.rows)} listings returned by listwise")
    if scored:
        print(f"  Agreement:  {len(agreed)}/{len(scored)} "
              f"({len(agreed) / len(scored):.0%})")

    lw_cost = (override_cost if override_cost is not None
               else cost_for_usage(model, t.listwise_prompt,
                                   t.listwise_completion))
    print(f"\n  {'':<12} {'calls':>6} {'prompt tok':>12} {'est $':>9}")
    print(f"  {'pointwise':<12} {t.pointwise_calls:>6} "
          f"{t.pointwise_prompt:>12,} {'(recorded)':>9}")
    print(f"  {'listwise':<12} {t.listwise_calls:>6} "
          f"{t.listwise_prompt:>12,} "
          f"{(f'{lw_cost:.4f}' if lw_cost is not None else 'n/a'):>9}")
    if t.pointwise_prompt and t.listwise_prompt:
        ratio = t.pointwise_prompt / t.listwise_prompt
        print(f"\n  Prompt tokens: listwise uses {1 / ratio:.0%} of pointwise "
              f"({ratio:.1f}x reduction)")

    refereed = [r for r in disagreed if r.referee]
    if refereed:
        wins = {}
        for r in refereed:
            wins[r.referee] = wins.get(r.referee, 0) + 1
        print(f"\n  Where they disagree, post-research backs "
              f"({len(refereed)} refereed):")
        for who, n in sorted(wins.items(), key=lambda kv: -kv[1]):
            print(f"    {who:<12} {n}")
    elif disagreed:
        print(f"\n  {len(disagreed)} disagreement(s), none with a post-research "
              "verdict to referee.")

    if disagreed:
        print("\n  Disagreements:")
        for r in disagreed[:12]:
            ref = f"  → {r.referee}" if r.referee else ""
            print(f"    {r.title[:40]:<40} "
                  f"point={r.pointwise_verdict}({r.pointwise_confidence}) "
                  f"list={r.listwise_verdict}({r.listwise_confidence}){ref}")

    print(f"\n  Threshold in force: {get_confidence_threshold():.0%}")
    print("  Listwise is NOT wired into the pipeline — this is a read-only "
          "experiment.\n")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(
        prog="python -m eval.listwise_compare",
        description="M-4: compare listwise batch scoring against pointwise.",
    )
    p.add_argument("--limit", type=int, default=20,
                   help="Listings to compare (default: 20)")
    p.add_argument("--batch", type=int, default=10,
                   help="Listings per listwise call (default: 10)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the call-count comparison, spend nothing")
    p.add_argument("--gold", action="store_true",
                   help="Only listings carrying a Sonnet post-research verdict")
    p.add_argument("--emit", metavar="DIR",
                   help="Write batch prompts for an in-session model to answer")
    p.add_argument("--apply", metavar="DIR", dest="apply_dir",
                   help="Score from in-session answers written to DIR")
    p.add_argument("--shuffle", action="store_true",
                   help="Shuffle input order (tests listwise position bias)")
    p.add_argument("--seed", type=int, default=0,
                   help="Shuffle seed, so arms stay comparable (default: 0)")
    p.add_argument("--model", dest="model_override",
                   help="Override the scoring model slug for this run")
    p.add_argument("--dump", metavar="PATH",
                   help="Write per-listing listwise scores to PATH (JSON)")
    p.add_argument("--via-claude", dest="via_claude", metavar="MODEL",
                   help="Score through `claude -p --model MODEL` "
                        "(e.g. haiku, sonnet) instead of OpenRouter")
    args = p.parse_args()
    return run(args.limit, args.batch, args.dry_run, gold_only=args.gold,
               emit=args.emit, apply_dir=args.apply_dir,
               shuffle=args.shuffle, seed=args.seed,
               model_override=args.model_override,
               via_claude=args.via_claude, dump=args.dump)


if __name__ == "__main__":
    sys.exit(main())
