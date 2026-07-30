"""Shared pytest configuration.

Two jobs, both about isolating the suite from the developer's machine:

1. Disable the O-1 model-usage telemetry sink so the suite never writes a
   stray logs/ directory. Tests that exercise the channel opt back in
   explicitly with a tmp path (see test_model_usage.py).

2. Pin runtime knobs to their in-code defaults so a developer's real .env
   cannot change test outcomes. Several modules call ``load_dotenv()`` at
   call time (e.g. ``src/triage.py::_get_openrouter_config``), which loads
   the real .env into os.environ mid-test. python-dotenv defaults to
   ``override=False``, so seeding os.environ here — at collection, before
   any module imports — wins over the file.

   Without this, CONFIDENCE_THRESHOLD=0.85 in a local .env silently fails
   fixtures written against the 0.5 code default, and the suite is green on
   CI but red locally. Values below MUST match the in-code fallbacks.
"""

import os

os.environ.setdefault("MODEL_USAGE_LOG_ENABLED", "false")

# Runtime knobs → in-code defaults. Keep in sync with the reading module.
_ENV_DEFAULTS = {
    "CONFIDENCE_THRESHOLD": "0.5",   # src/triage.py::get_confidence_threshold
    "AUTOPILOT_ENABLED": "false",    # src/process_queue.py::_autopilot_enabled
    "AUTOPILOT_POST_STAGE_5": "true",  # src/cli.py::high_signal_only
}

for _key, _value in _ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
