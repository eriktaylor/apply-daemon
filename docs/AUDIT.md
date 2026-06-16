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

It uses the standard `logging.INFO` stream — no separate sink, no file
rotation of its own. In production the lines land in whatever stream
captures the rest of `script.sh`'s output (typically the cron stdout
redirect).

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

Tail the live stream (cron-redirected log file):

```bash
grep "audit.mismatch_drops" /var/log/apply-daemon.log | tail -n 200
```

Bucket by gate to see where drops are concentrated:

```bash
grep "audit.mismatch_drops" /var/log/apply-daemon.log \
  | awk -F'|' '{ gsub(/ /, "", $5); print $5 }' \
  | sort | uniq -c | sort -rn
```

Find the worst-offending hosts (likely candidates for the
`_AGGREGATOR_DOMAINS` blocklist):

```bash
grep "audit.mismatch_drops" /var/log/apply-daemon.log \
  | awk -F'|' '{ gsub(/ /, "", $8); print $8 }' \
  | sort | uniq -c | sort -rn | head
```

A host that shows up repeatedly with `gate=llm` is a strong signal we
should add it to the blocklist, eliminating the LLM call entirely for
that domain on future runs.

## Retention

No code-level retention policy — the log line is treated like any other
INFO record. If the stdout cron log is rotated nightly, audit history
matches that rotation. For long-term trend analysis, copy the matching
lines into a dedicated file before rotation.

## Security note

The schema deliberately excludes raw description text, LLM prompts/
responses, and credentials, per `SECURITY.md`. The `reason` field is a
short human-readable clause produced by the gate code itself — never a
verbatim slice of the source.
