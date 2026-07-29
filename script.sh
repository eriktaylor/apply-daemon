#!/usr/bin/env bash
# Daily Ingestion Trigger — the no-session entry point to the pipeline.
#
# A thin wrapper over `python -m src.cli refresh`, which owns the stage
# sequence. The chain used to live here as well; duplicating it meant the
# budget gate applied to only one of the two paths. Any argument you pass is
# forwarded, so:
#
#     ./script.sh                 # budget-gated run, then review with `next`
#     ./script.sh --dry-run       # show the stages and budget verdict
#     ./script.sh --top-n 5       # raise autopilot enrichment for this run
#     ./script.sh --force         # run even if the budget check refuses
#
# Exits non-zero if a stage fails or the budget refuses the run.

set -euo pipefail

cd "$(dirname "$0")"

# Prefer the project venv so the script works from a plain shell — the
# previous version assumed an already-activated venv and died with
# "exec: python: not found".
if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

exec "$PY" -m src.cli refresh "$@"
