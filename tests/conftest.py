"""Shared pytest configuration.

Disable the O-1 model-usage telemetry sink by default so the suite never
writes a stray logs/ directory. Tests that exercise the channel opt back in
explicitly with a tmp path (see test_model_usage.py).
"""

import os

os.environ.setdefault("MODEL_USAGE_LOG_ENABLED", "false")
