# Audit Log — Mismatch Drops and Expired Listings

A subset of pipeline decisions silently drop listings before they reach Slack
or before autopilot enriches them. Without an audit trail those drops are
invisible: we'd never know whether the gate is calibrated too aggressively
(losing real opportunities) or too leniently (still letting bad rows
through). This document defines the schema, where the logger writes, and
how to audit later.

## Logger

All audit entries route through the Python logger:

```
apply_daemon.audit.mismatch_drops
```

It writes to its own file, **`logs/audit.log`** by default, via
`src/file_logger.py` — the same dedicated-logger-with-a-file-sink helper
`src/model_usage.py` uses for `logs/model_usage.log`. Override the path
with `AUDIT_LOG_PATH`, or disable the file sink entirely with
`AUDIT_LOG_ENABLED=false` (default `true`).

This is additive, not a replacement: the logger still propagates to the
root logger, so a `script.sh`/cron setup that redirects stdout/stderr keeps
capturing these lines exactly as before — nothing to change there.

**Why this changed.** The original design ("no separate sink, no file")
assumed every run went through cron, where stderr redirection was a given.
That assumption broke when the CLI's `refresh` verb became the normal way
to run the pipeline by hand (`plans/cli_skill_interface.md` A-8): the CLI
streams stderr to the terminal and captures nothing, so under it every drop
reason was lost with no way to recover it after the fact. The file sink
gives the CLI path — and any other caller that doesn't own its own log
capture — a durable trail without taking anything away from cron's.

## Log line schema

One pipe-delimited line per drop. Stable column order so a downstream
`awk -F'|'` or `cut` works without surprises:

```
audit.mismatch_drops | <iso_timestamp> | <listing_id> | <source> | <gate> | <anchor_company> | <observed_company> | <links_host> | <reason>
```

| Column | Description | Example |
|--------|-------------|---------|
| `iso_timestamp` | UTC ISO-8601 | `2026-06-15T18:00:00+00:00` |
| `listing_id` | UUID from `listings.id` | `9ad4143b-3617-…` |
| `source` | Track-A site (`linkedin`, `indeed`, `jobspy`) or Track-B classification | `linkedin` |
| `gate` | Which check fired the drop | `stage5`, `substring`, `llm`, `probe` |
| `anchor_company` | What the row metadata claimed | `Handshake` |
| `observed_company` | What the body or URL actually points at; `""` when N/A | `OpenAI` |
| `links_host` | Resolved host of `links[0]`, stripped of `www.` | `thehomebase.ai` |
| `reason` | One short clause, no newlines, no commas inside | `body about a different company` |

Empty fields are written as the empty string between pipes, never `null`
or `none`. The line is single-pipe-delimited so a value containing a pipe
must be normalized away first (collapsed to a space) — only the `reason`
field is at any practical risk of this, and the helper strips it before
emitting.

## Gate values

| Gate | When it fires | Fix |
|------|---------------|-----|
| `stage5` | Stage 5 LLM marked verdict=NO with `reason` starting "listing expired:" | Fix 4a |
| `substring` | Hybrid mismatch gate: token check failed in both `job_summary` and URL host, fallback LLM was bypassed (e.g. `MISMATCH_GATE_MODE=substring_only`) | Fix 2a Stage 1 |
| `llm` | Hybrid mismatch gate: token check missed and the LLM fallback returned `matches=false` | Fix 2a Stage 2 |
| `probe` | HTTP probe returned 404/410 or matched an expired-page stop-phrase | Fix 4b |

## How to audit

Tail the file (works for both the CLI path and cron, since both now write
here):

```bash
grep "audit.mismatch_drops" logs/audit.log | tail -n 200
```

Bucket by gate to see where drops are concentrated:

```bash
grep "audit.mismatch_drops" logs/audit.log \
  | awk -F'|' '{ gsub(/ /, "", $5); print $5 }' \
  | sort | uniq -c | sort -rn
```

Find the worst-offending hosts (likely candidates for the
`_AGGREGATOR_DOMAINS` blocklist):

```bash
grep "audit.mismatch_drops" logs/audit.log \
  | awk -F'|' '{ gsub(/ /, "", $8); print $8 }' \
  | sort | uniq -c | sort -rn | head
```

A host that shows up repeatedly with `gate=llm` is a strong signal we
should add it to the blocklist, eliminating the LLM call entirely for
that domain on future runs.

If `AUDIT_LOG_ENABLED=false` or the path was overridden, substitute
`$AUDIT_LOG_PATH` (or a cron-redirected stdout file, if that's still the
only capture in place) for `logs/audit.log` above.

## Retention

No code-level retention policy — `logs/audit.log` is gitignored (see
`.gitignore`'s `logs/` entry) and grows unbounded like
`logs/model_usage.log`. If a cron setup also redirects stdout to a rotated
file, that copy rotates on its own schedule; the file sink does not. For
long-term trend analysis, copy the matching lines out before rotating or
truncating either one.

## Security note

The schema deliberately excludes raw description text, LLM prompts/
responses, and credentials, per `SECURITY.md`. The `reason` field is a
short human-readable clause produced by the gate code itself — never a
verbatim slice of the source.
