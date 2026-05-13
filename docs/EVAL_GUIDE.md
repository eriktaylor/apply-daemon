# Eval guide

How to build and run the extraction + matching eval set.

## Overview

The eval harness (`eval/eval.py`) tests the full unified pipeline: given raw email text, does the LLM correctly extract listings AND score them against the candidate profile? It reports extraction accuracy, verdict accuracy, tokens, and latency.

## Eval data format

The eval CSV has two columns:

| Column | Description |
|--------|-------------|
| `email_text` | Raw text content of the email (as the LLM would see it) |
| `expected_listings` | JSON array of `{"title", "company", "verdict"}` objects |

See `eval/eval_example.csv` for the expected format with synthetic examples.

## Building a real eval set

1. Run the pipeline to process real emails.
2. Review the `debug/` folder for saved email texts and the database for LLM outputs.
3. For each email, record the expected listings with your labels (YES/NO/MAYBE).
4. Export to CSV format matching the example.
5. The eval CSV is gitignored — it contains real job data.

## Recommended models

| Model | Size | VRAM | Quality | Notes |
|-------|------|------|---------|-------|
| `gemma3:4b` | 4B | ~3 GB | Best | Default. ~800ms per email. |
| `mistral` | 7B | ~6 GB | Good | Solid alternative. |

Pull models before running eval:

```bash
ollama pull gemma3:4b
ollama pull mistral
```

## Running the eval

```bash
# Quick smoke test with bundled example data
python -m eval.eval --input eval/eval_example.csv --model gemma3:4b

# Compare models
python -m eval.eval --input eval/my_eval.csv --model gemma3:4b
python -m eval.eval --input eval/my_eval.csv --model mistral
```

## Metrics

| Metric | Description | Priority |
|--------|-------------|----------|
| **Extraction accuracy** | Did the LLM find all expected listings? | **Highest** |
| **Verdict accuracy** | Of found listings, are verdicts correct? | High |
| **Tokens/email** | Total tokens consumed per email | Medium |
| **Latency/email** | Wall-clock ms per email | Medium |

Extraction errors are the most costly — if the LLM misses a listing entirely, it can't be scored. Optimize for high extraction accuracy first.

## Output

- Summary table printed to stdout
- Detailed per-email results saved to `eval/results_<model>.csv`
