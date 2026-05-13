# Migration Guide

This document tracks the Day 2 operations for the `apply-daemon` repository after its v0.1.0 initial public commit. It is divided into three phases that can be executed independently.

  - **Phase 1** — finish the internal rename from `apply-pilot` to `apply-daemon`.
  - **Phase 2** — keep the lint surface clean going forward.
  - **Phase 3** — the security baseline a future audit agent should start from.

---

## Phase 1 — The Re-Naming Plan (`apply-pilot` → `apply-daemon`)

### Naming conventions

| Variant | Old | New |
|---|---|---|
| Brand / prose | `Apply Pilot` | `Apply Daemon` |
| Package + CLI | `apply-pilot` (kebab) | `apply-daemon` |
| Module / file identifier | `apply_pilot` (snake) | `apply_daemon` |
| Slack wire-format tag | `apply_pilot_listing` | `apply_daemon_listing` |
| User-Agent token | `apply-pilot/1.0` | `apply-daemon/1.0` |

`APPLY_PILOT_*` environment variables: **none in the codebase** (confirmed by `git grep`). No env-var renames required.

Application log files: **none hardcoded** (the project uses the stdlib `logging` module; output paths are determined by the user's runtime configuration, not by source). If your deployment writes to a path like `apply-pilot.log`, update it operationally — no source change required.

### 1.1 Hard renames (will change install behavior)

These are the "must change first" items because they affect how the CLI installs and how the pipeline reads its database.

#### `pyproject.toml`

```toml
# Top of the [project] block
name = "apply-pilot"           # → "apply-daemon"

# All seven console_scripts entry points under [project.scripts]
apply-pilot          = "src.pipeline:main"        # → apply-daemon
apply-pilot-ingest   = "src.jobspy_ingest:main"   # → apply-daemon-ingest
apply-pilot-digest   = "src.digest:main"          # → apply-daemon-digest
apply-pilot-tailor   = "src.tailor:main"          # → apply-daemon-tailor
apply-pilot-sweeper  = "src.sweeper:main"         # → apply-daemon-sweeper
apply-pilot-batch    = "src.batch_process:main"   # → apply-daemon-batch
apply-pilot-test-proxy = "src.proxy_test:main"    # → apply-daemon-test-proxy
```

Target callables (`src.pipeline:main` etc.) stay unchanged — there is no top-level `apply_pilot` Python package to rename.

After this edit, run `pip install -e ".[dev]"` to refresh the installed entry points and `uv pip compile pyproject.toml --extra dev -o requirements.lock` to regenerate the lockfile (its auto-generated header references the old package name in ~17 places).

#### `src/db.py` — SQLite filename

```python
DEFAULT_DB_PATH = Path("apply_pilot.db")   # → Path("apply_daemon.db")
```

**One-time data migration step for any deployment carrying live data:**

```bash
mv apply_pilot.db apply_daemon.db
```

The schema is unchanged; only the default filename moves. Users supplying an explicit DB path via CLI flag or env override are unaffected.

### 1.2 Slack wire-format (`event_type` field) — dual-read pattern

Every digest card the tool has ever posted carries metadata `event_type: apply_pilot_listing`. The sweeper reads this back to recognize cards for ChatOps (`!triage`, `!tailor`, `!pass`, etc.). A hard rename would orphan the existing card history.

**Recommended pattern** — write the new tag, accept either tag when reading:

```python
# src/sweeper.py — near the existing module constants
_LISTING_EVENT_TYPES = frozenset({"apply_daemon_listing", "apply_pilot_listing"})
```

Then:

- `src/digest.py` (1 write site) — emit `"apply_daemon_listing"`.
- `src/sweeper.py` (1 write site, 4 read sites) — write the new value; replace the 4 `== "apply_pilot_listing"` checks with `in _LISTING_EVENT_TYPES`; update the module docstring to mention both legacy and new tags.
- Add one regression test (`tests/test_sweeper.py`) asserting that a mocked Slack message with the legacy tag is still detected.

The dual-read can be retired any time after the oldest still-actionable Slack card has aged out (typically 30–90 days depending on `dedup_window_days` / `pass_window_days` settings).

### 1.3 Outbound User-Agent strings

These are sent to third-party hosts (LinkedIn, Indeed, ATS pages, Nominatim, IPRoyal). Rename for brand consistency; downstream services do not key behavior off the literal value.

| File | Line | Old value |
|---|---|---|
| `src/triage.py` | scrape session | `"Mozilla/5.0 (compatible; apply-pilot/1.0)"` |
| `src/sweeper.py` | scrape session | `"Mozilla/5.0 (compatible; apply-pilot/1.0)"` |
| `src/geo.py` | Nominatim init | `user_agent="apply-pilot/0.1"` |
| `src/proxy_test.py` | smoke-test fetch | `"apply-pilot-proxy-test/1.0"` |

Replace `apply-pilot` with `apply-daemon` in each.

### 1.4 Test fixtures

- `tests/test_proxy_manager.py` — replace the `"apply_pilot_user"` username fixture with `"apply_daemon_user"`; update the corresponding regex assertion.
- `tests/test_sweeper.py`, `tests/test_idempotency.py` — fixtures use the legacy `apply_pilot_listing` Slack tag. Keep at least one fixture using the legacy value to exercise the dual-read; mirror new-tag fixtures alongside.

### 1.5 Source-code prose (comments, docstrings, argparse descriptions)

These do not affect runtime behavior; rename for brand consistency. Bulk find-and-replace `apply-pilot` → `apply-daemon` in:

- `src/models.py` — module docstring.
- `src/pipeline.py` — argparse description.
- `src/jobspy_ingest.py` — crontab-recipe comment.
- `src/digest.py` — crontab-recipe comment.
- `src/sweeper.py` — crontab-recipe comment + sweeper module docstring.
- `src/report.py` — argparse description.
- `src/proxy_test.py` — module docstring, comments, sticky-session log line.
- `src/batch_process.py` — any incidental references.

### 1.6 Documentation prose

| File | Notes |
|---|---|
| `README.md` | H1 title, `cd apply-pilot` setup line, ~5 crontab path examples, directory tree, all CLI examples (`apply-pilot-ingest`, etc.). Heaviest single file at ~14 hits. |
| `CONTRIBUTING.md` | H1. |
| `SECURITY.md` | Prose mentions in threat model and third-party services section. |
| `CHANGELOG.md` | Header line. |
| `conduct.md` | Verify with `git grep` (likely no hits). |
| `docs/PROJECT_BRIEFING.md` | H1. |
| `docs/PROXY.md` | Multiple prose references. |
| `docs/MODELS.md`, `docs/CHATOPS.md`, `docs/EVAL_GUIDE.md` | Sweep for stray references. |
| `docs/test_coverage_summary.md` | Single reference. |
| `.env.example` | Comment block introducing the proxy section. |
| `my_profile_example/search_config.yaml` | CLI-command comment. |

### 1.7 Auto-generated artifacts

- `requirements.lock` — regenerate after the `pyproject.toml` rename:

  ```bash
  uv pip compile pyproject.toml --extra dev -o requirements.lock
  ```

  The header comment will pick up the new package name automatically.

### 1.8 Recommended commit sequence

Land each commit independently green:

1. **Slack wire format (dual-read).** Add `_LISTING_EVENT_TYPES`, flip read sites to set membership, flip write sites to the new value, update docstring, add the dual-read regression test.
2. **Package, CLI, DB filename, User-Agents.** `pyproject.toml`, `src/db.py`, the 4 outbound UA strings, regenerated `requirements.lock`.
3. **Source-code prose.** Module docstrings, argparse, crontab comments.
4. **Docs and prose.** README, CONTRIBUTING, SECURITY, CHANGELOG, `docs/`, `.env.example`, `my_profile_example/search_config.yaml`.
5. **Test fixtures.** Rename `apply_pilot_user` and any other purely-textual references.

### 1.9 Verification

After each commit:

```bash
.venv/bin/ruff check .          # exits 0
.venv/bin/pytest tests/ -q      # all tests pass
```

After the final commit:

```bash
git grep -i "apply[-_]pilot"
# Should return ONLY:
#   - the legacy entry inside _LISTING_EVENT_TYPES (intentional)
#   - the legacy-tag regression test fixture (intentional)
# All other hits should be gone.

pip install -e ".[dev]" && apply-daemon --help
# New entry point installs and runs.
```

---

## Phase 2 — The Lint Clean-up Plan

### Current state

Repo is clean (`ruff check .` exits 0). The CI lint step is **blocking** — any new violation will fail the workflow. The configuration lives in `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I"]

[tool.ruff.lint.per-file-ignores]
# E402: load_dotenv() must run before importing project modules that
#       call os.getenv at import time.
# E501: triage.py and tailor.py hold the LLM prompt templates as
#       multi-line strings; one prose sentence per source line keeps
#       the wire format readable. Test files in this list inline long
#       mock-response fixtures.
"src/batch_process.py"   = ["E402"]
"src/digest.py"          = ["E402"]
"src/jobspy_ingest.py"   = ["E402"]
"src/pipeline.py"        = ["E402"]
"src/proxy_test.py"      = ["E402"]
"src/sweeper.py"         = ["E402"]
"src/triage.py"          = ["E501"]
"src/tailor.py"          = ["E402", "E501"]
"tests/test_compile.py"  = ["E501"]
"tests/test_tailor.py"   = ["E501"]
"tests/test_triage.py"   = ["E501"]
```

These ignores are deliberate — do not weaken them without a written justification in the PR.

### Strategy for future violations

1. **Pre-commit hook.** Add a `.pre-commit-config.yaml` running `ruff check .` (and optionally `ruff format`) on every commit. Catches violations before they reach CI. Lightweight bootstrap: `pre-commit install`.

2. **Branch protection on `main`.** Require the `test` check to pass before merge. Without this, a PR with a failing lint step can still be merged and leave `main` red — exactly the failure mode that produced `tests/test_sweeper.py` import-order regressions.

3. **Triage policy when a violation appears.**

    - **Auto-fixable categories** (`I001` unsorted-imports, `F401` unused-imports, `F541` empty f-strings): run `ruff check . --fix`. Zero risk.
    - **`E402` module-import-not-at-top:** intentional only if it follows a `load_dotenv()` call. If so, add the file to the `per-file-ignores` block. Otherwise reorder the imports.
    - **`E741` ambiguous variable name (`l`, `I`, `O`):** rename. `listing` / `item` / `link` are the conventions already used in the codebase.
    - **`F841` unused local:** delete the assignment if dead; if it's an intentional patch context (`as mock_x`), drop the alias.
    - **`E501` line-too-long:** mechanical line-wrap first. Per-file-ignore is reserved for files that hold LLM prompt templates or inlined fixture data where wrapping would harm readability — every new addition to the ignore block needs a one-line justification in the comment.

4. **Optional next steps (not required to ship).**

    - `ruff format` for auto-formatting. Adopt as a second CI step alongside `ruff check`.
    - `pytest-xdist` for parallel test runs.
    - CI caching of `~/.cache/uv`.
    - Python 3.12 matrix entry once the codebase has been validated against it.
    - One-off dead-code sweep with `vulture src/` after a major refactor.

5. **When tempted to bump `line-length`.** Don't. Keep it at 100. The per-file-ignores already cover the legitimately-long cases (prompt strings, fixture data). Bumping the global limit hides future regressions.

---

## Phase 3 — The Post-Publish Security Plan

This phase documents the security baseline so a future security agent can audit the repository without re-deriving it from scratch.

### 3.1 Canonical documents

- `SECURITY.md` — the public security policy. Includes reporting channels, supported versions, threat model, third-party services and outbound data flow, the contributor security mantra, and the forbidden-files list.
- `CONTRIBUTING.md` — references `SECURITY.md` and lists the eight contributor rules every PR is reviewed against.
- `.gitignore` — enforces the forbidden-files list at the tooling level. Patterns of note: `my_profile/`, `/my_profile_*` (with `!/my_profile_example/`), `*.db`, `.env`, `.cache/`, `*:Zone.Identifier`.

### 3.2 Threat model (summary)

`apply-daemon` runs locally on a user's machine. It reads a candidate profile, ingests email from a dedicated alerts inbox, calls third-party LLM APIs, scrapes job-board content, and writes to a local SQLite database. The only trust boundary that matters is the user running it. The pipeline must:

  - never exfiltrate the candidate profile or email contents beyond the configured LLM endpoint;
  - never log secrets, raw email bodies, full LLM prompts, or LLM responses;
  - treat all scraped HTML and inbound email as untrusted input.

### 3.3 Third-party services the pipeline talks to

| Service | Required? | What it sees |
|---|---|---|
| OpenRouter | yes | Full prompts: candidate profile context + each job listing. Stage 1 / Stage 5 / Tailor / Research / Trend calls. |
| Gmail (IMAP) | yes | App-password credential; reads from a dedicated alerts inbox only. |
| Slack | yes | Bot token + channel ID. Outbound notifications carry listing IDs, scores, summaries, and tailored asset previews. |
| Job boards | yes | Outbound HTTP(S). IP, User-Agent, and query terms visible to each board. |
| DuckDuckGo (DDGS) | yes | Search queries used by the Stage-3 healing path. |
| Rotating residential proxy (optional) | optional | When configured, sees destination hostnames, request timing, and (for plain HTTP only) request bodies. Cannot decrypt HTTPS payloads. |
| OpenStreetMap Nominatim | yes | Geocoding queries — the user's `home_location` and each listing's location string. |

### 3.4 Files that must never be committed

```
.env
*.db
my_profile/                (user's real profile)
my_profile_*/              (except my_profile_example/, the synthetic template)
eval/eval_data/
eval/*.csv                 (except eval_example.csv)
.cache/                    (proxy session state — IDs/timestamps only, never credentials)
*:Zone.Identifier
*.Zone.Identifier
```

### 3.5 Credential handling rules

- Secrets live in `.env` only; the `.env.example` template ships with placeholder values per variable.
- Outbound logs use the public-grade redaction surface — e.g. `ProxyManager.describe()` prints `username[:3] + "***"` and never the password.
- The proxy sticky-session state file (`.cache/iproyal_session.json` or equivalent) contains only `session_id`, `born_at_wall`, and `lifetime_minutes` — never the credential pair.
- No SQL string interpolation: all queries use `?` placeholders. No `eval`/`exec`/`pickle.load`. YAML reads use `yaml.safe_load`. No `subprocess(..., shell=True)` with external input.
- TLS verification is on (`verify=True`) for every outbound HTTPS call.

### 3.6 Required example files (must contain only synthetic data)

  - `.env.example` — placeholder values, one comment per variable.
  - `my_profile_example/profile.example.md` — fictional candidate (currently "Jane Doe / Seattle").
  - `my_profile_example/cover_letter.md` — fictional cover letter.
  - `my_profile_example/search_config.yaml` — generic ML/AI engineer template.
  - `eval/eval_example.csv` — synthetic listings.

If the schema of any of the above changes, update the example in the same PR. The CONTRIBUTING document calls this out under the "What we will probably not accept" list.

### 3.7 GitHub repository settings (one-time, after initial push)

Enable in the repository's web UI:

  - **Code security:** Dependabot alerts, Dependabot security updates, Secret scanning, Push protection.
  - **Branch protection on `main`:** require the `test` CI check to pass before merge; require at least one approving review for external contributions; restrict force-push.
  - **Disable wiki.** The `docs/` directory is the canonical location.
  - **Topics:** `python`, `llm`, `openrouter`, `automation`, `job-search`, `scraping`, `proxy`.

### 3.8 Recurring hygiene

  - **Dependabot first 48 hours.** Expect the dependency-graph scan to fire on first push because of the pinned `requirements.lock`. Triage and merge as needed.
  - **Quarterly dependency review.** `uv pip compile` against the latest constraints, sanity-check changelogs of any major-version bumps, run the full test suite.
  - **Annual prompt-leak audit.** Spot-check `logger.*` call sites in `src/triage.py`, `src/tailor.py`, `src/research.py` to confirm no path logs raw email bodies, full prompts, or full LLM responses. The contributor mantra (`SECURITY.md`, rule 4) is the canonical reference.
  - **Watch for new outbound endpoints.** Any PR that adds a new third-party service (new SDK, new HTTP host, new credential env var) must update the §3.3 table in `SECURITY.md` in the same commit.

### 3.9 Known follow-ups left over from the publish prep

  - `pyproject.toml` is missing an `authors` / `urls` block. Required if/when the package is published to PyPI; nice-to-have for repository discoverability now.
  - `SECURITY.md` currently lists two disclosure channels (GitHub Security Advisories and email). Pick one as the canonical channel to keep the path obvious to reporters.
  - `docs/test_coverage_summary.md` flags a small number of pre-existing coverage gaps (e.g. `get_trend_skills`). Track each as its own issue.

---

*End of migration plan.*
