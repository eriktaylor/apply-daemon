#!/usr/bin/env bash
# Daily Ingestion Trigger — chains the full intake batch and, when
# AUTOPILOT_ENABLED=true in .env, fires the Speculative Agent at the end.
#
# After this script returns, the only command needed to triage the batch is:
#     python -m src.sweeper
#
# Re-run this script to start the next batch.

set -euo pipefail

cd "$(dirname "$0")"

python -m src.jobspy_ingest
python -m src.digest
python -m src.pipeline
python -m src.digest

# src.process_queue is a no-op when AUTOPILOT_ENABLED is unset/false,
# so it is always safe to call here.
python -m src.process_queue
