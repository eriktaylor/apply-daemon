# Contributing to apply-daemon

Thanks for your interest. This is a small project with a narrow scope —
local-first job-search automation — so contributions are most valuable
when they fit that scope.

## Before you open a PR

1. **Read [`SECURITY.md`](SECURITY.md).** It contains the contributor
   security mantra. Every PR is reviewed against those rules.
2. **Open an issue first** for anything beyond a small fix. It saves
   both of us time if the change isn't a fit.
3. **Check the [Code of Conduct](CODE_OF_CONDUCT.md).**

## Development setup

```bash
# Python 3.11+ required
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Copy the templates and fill them in (these stay local; .gitignore protects them)
cp .env.example .env
cp -r my_profile_example my_profile
```

See the README for end-to-end pipeline setup (Gmail App Password,
OpenRouter key, Slack tokens, etc.).

## Running checks locally

```bash
ruff check .
pytest tests/
```

CI runs the same two commands. Both must pass before a PR can merge.

## Branch and PR conventions

- Branch from `main`. Use a short descriptive name (`fix/dedup-edge-case`,
  `feat/cohort-trends`).
- Keep PRs focused — one concern per PR.
- Write a PR description that explains **why**, not just what.
- Squash on merge. Commit messages on `main` should read like a
  changelog entry.

## What we will probably accept

- Bug fixes with a regression test.
- Performance improvements with measurements.
- Test coverage for areas flagged in `docs/test_coverage_summary.md`.
- Documentation improvements.
- New parsers for additional job-alert email sources, with sanitized
  test fixtures (see `SECURITY.md` for the sanitization rules).

## What we will probably not accept

- New LLM providers beyond OpenRouter unless there's a clear reason.
- Speculative abstractions for hypothetical future use cases.
- Features that require committing personal data, real listings, or
  un-sanitized email fixtures.
- Changes that weaken `.gitignore`, disable TLS verification, or
  introduce raw-content logging.

## Reporting security issues

Do not file public issues for security reports. See [`SECURITY.md`](SECURITY.md).
