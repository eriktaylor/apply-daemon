# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately. Open a GitHub Security
Advisory on this repository ("Security" tab → "Report a vulnerability") or
email the maintainer listed in `pyproject.toml`. Do not file a public issue
for security reports.

You can expect an acknowledgement within 7 days. Coordinated disclosure is
appreciated; we will credit reporters in the release notes for the fix
unless you prefer to remain anonymous.

## Supported versions

Only `main` is actively maintained. Pin to a tagged release if you need
stability. Security fixes are applied to `main` only.

## Threat model

`apply-daemon` runs locally. It reads a candidate profile, ingests email
from a dedicated inbox, calls third-party LLM APIs, scrapes job-board
content, and writes to a local SQLite database. The only trust boundary
that matters is the user running it. The pipeline must:

  - never exfiltrate the candidate profile or email contents beyond the
    configured LLM endpoint;
  - never log secrets, raw email bodies, full LLM prompts, or LLM responses;
  - treat all scraped HTML and inbound email as untrusted input.

## Third-party services and outbound data flow

`apply-daemon` talks to the following external services. Operators of any
of these can in principle observe the data the pipeline sends them.
Decide whether you trust each one before pointing the tool at real data.

| Service | Required? | What it sees |
|---|---|---|
| **OpenRouter** | yes | Full prompts (your candidate profile context + each job listing). All Stage 1 / Stage 5 / Tailor / Research / Trend calls go here. Your account API key authenticates. |
| **Gmail (IMAP)** | yes | Gmail credentials (App Password). The pipeline only reads from a dedicated alerts inbox — never your personal mail. |
| **Slack** | yes | Bot token and channel ID. Outbound notifications include listing IDs, scores, summaries, and (for `!polish` etc.) tailored asset previews. |
| **Job boards** (Indeed, LinkedIn, Glassdoor, Google Jobs, etc.) | yes | Outbound HTTP(S) fetches via JobSpy and the Track C deep-research scraper. These boards see your IP, User-Agent, and query terms. |
| **DuckDuckGo (DDGS)** | yes | Search queries used by the Stage-3 healing path when a primary scrape fails. |
| **IPRoyal residential proxy** | optional | If `IPROYAL_USERNAME` / `IPROYAL_PASSWORD` are set, all Track A and Track C outbound traffic is routed through IPRoyal's residential network. The proxy operator can see destination hostnames, request timing, and (for plain HTTP, not HTTPS) full request bodies. They cannot decrypt HTTPS payloads. See `src/proxy_manager.py` and the README "Anti-bot evasion" section for the integration details. |
| **OpenStreetMap Nominatim** | yes | Geocoding queries — your `home_location` and each listing's location string. No auth, but rate-limited. |

Notes on the proxy integration:

  - Credentials live in `.env` only; `proxy_manager.describe()` is the
    public-grade log surface and never prints the password.
  - Sticky session state is persisted to `.cache/iproyal_session.json`,
    which contains only the random session id, wall-clock timestamp,
    and lifetime — never the credentials. `.cache/` is gitignored.
  - The default scheme is `http` (CONNECT tunneling for HTTPS targets).
    HTTPS sites are still end-to-end encrypted between client and
    target; the proxy only sees the destination hostname via the
    CONNECT handshake.

## Security mantra (for contributors)

These are the rules every change is reviewed against. They exist because
this is a personal-data tool that ships in public.

1. **Treat the repo as if it ships tomorrow.** Every commit lands in
   public history. There is no "we'll clean it up later."
2. **No secrets, no PII, no usernames in code or commits.** `.env`,
   profile data, eval datasets, resumes, and `*.db` files stay
   gitignored. Ship `*.example` templates with fictional data instead.
3. **Sanitize test fixtures.** Strip real email addresses, tracking
   links, company names, and listing URLs from any captured HTML before
   committing. Prefer synthetic fixtures built from scratch.
4. **Logs are public-grade.** Log IDs, decisions, and scores — never
   raw email bodies, credentials, tokens, prompts, or LLM responses.
5. **Verify TLS.** No `verify=False` on outbound HTTPS. If a host
   genuinely needs a custom CA, use `certifi` with a per-host bundle.
6. **Parameterize everything.** SQL via `?` placeholders — never
   f-strings. No `eval` / `exec` / `pickle.load`. YAML loads via
   `yaml.safe_load`. No `subprocess.run(..., shell=True)` with external
   input.
7. **Pin and review dependencies.** The lockfile is the source of truth
   for installs. New dependencies require a brief justification in the PR.
8. **Boundary checks live at I/O.** Validate on the way in (email
   parser, web fetch, user-supplied config). Trust internal calls.

## Files that must never be committed

```
.env
*.db
my_profile/          (includes your customized search_config.yaml)
my_profile_*/        (except my_profile_example/, the synthetic template)
eval/eval_data/
eval/*.csv           (except eval_example.csv)
.cache/              (proxy session state; never contains credentials but
                      should not ship)
*:Zone.Identifier
*.Zone.Identifier
```

The `.gitignore` enforces these patterns. Do not weaken them.

## Required example files

These files MUST exist and MUST contain only synthetic data:

  - `.env.example` — placeholder values, comments per variable.
  - `my_profile_example/profile.md` — fictional candidate.
  - `my_profile_example/cover_letter.md` — fictional cover letter.
  - `my_profile_example/search_config.yaml` — generic ML/AI engineer template.
  - `eval/eval_example.csv` — synthetic listings.

If you change the schema of any of the above, update the example in the
same PR.
