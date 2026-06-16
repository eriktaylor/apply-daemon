## Rotating residential proxy (recommended for heavy scraping)

Apply Daemon is fully usable from your home IP for casual polling — a single Indeed tier and a few `!triage` commands per day will not draw attention. However, if you raise `results_wanted` on LinkedIn, run multiple proactive cycles per day, or aim Track C deep-research scrapes at hardened ATS pages, your home IP **will** eventually trip Cloudflare / DataDome / LinkedIn's auth wall and the pipeline will silently start returning empty descriptions to the LLM.

**Recommendation:**

- **Light usage (≤20 scrapes/day):** No proxy needed. A desktop VPN is fine if you want a layer of privacy, but it does *not* prevent bans (one static exit IP just gets banned slightly later).
- **Aggressive scraping (LinkedIn, ATS deep research, batch runs):** Use a rotating residential proxy. We integrate first-class with [IPRoyal](https://iproyal.com/) because their non-expiring traffic plan fits the stop-and-start nature of job hunting (a few hundred requests/day rarely exceeds 50 MB) and they support the 30-minute "sticky sessions" that LinkedIn's pagination requires.

**How the integration works** (see `src/proxy_manager.py`):

The `ProxyManager` builds an IPRoyal "magic-string" password on every fresh session — `{PASS}_session-{8-char-id}_lifetime-30m` (per [IPRoyal's rotation docs](https://docs.iproyal.com/proxies/residential/proxy/rotation), the magic string lives in the password field, not the username) — and reuses that exit IP for 30 minutes before rotating. It also force-rotates immediately when:

- the lifetime elapses,
- jobspy raises (most block exceptions surface this way), or
- a downstream `_scrape_url` HTTP fetch returns **403** (Cloudflare), **429** (DataDome / rate-limit), or **999** (LinkedIn auth wall).

**Setup:** Add the following to your `.env` (all values come from the IPRoyal dashboard after purchasing a residential block):

```bash
IPROYAL_USERNAME=your-iproyal-user
IPROYAL_PASSWORD=your-iproyal-pass
# Defaults — usually fine to leave as-is:
IPROYAL_HOST=geo.iproyal.com
IPROYAL_PORT=12321
IPROYAL_SESSION_TTL_MINUTES=30
IPROYAL_SCHEME=http   # or socks5 / socks5h
```

When both credentials are set, every `scrape_jobs()` call configured in `my_profile/search_config.yaml` and every Track-C web fetch (deep research, `!triage <URL>`) routes through the sticky session automatically. Leave them blank to keep all traffic on your local IP.

**Verify the proxy is working — run this before firing the pipeline:**

```bash
python -m src.proxy_test
```

The smoke test runs five sequential checks against the live IPRoyal endpoint and exits non-zero on the first failure:

1. Credentials present in the environment.
2. Local egress IP fetched (baseline, no proxy).
3. Exit IP fetched **through** the proxy — confirms it differs from the local IP.
4. Forced session rotation, exit IP fetched again — confirms the rotation path.
5. The mocked unit suite (`tests/test_proxy_manager.py`, ~38 tests, no IPRoyal traffic) runs as a regression check.

A successful run uses ≈200 bytes of IPRoyal data and leaves a sticky session warm in `.cache/iproyal_session.json`. The next `python -m src.jobspy_ingest`, `python -m src.pipeline`, or `!triage` reuses the **same** exit IP for the rest of the 30-minute lifetime — no second handshake, no second `session-{id}` allocation. If the lifetime has expired by the time you run them, a fresh session is opened automatically.

| Failure message | What to check |
|---|---|
| `Residential Proxy credentials missing.` | `IPROYAL_USERNAME` and `IPROYAL_PASSWORD` are set in `.env`. |
| `Residential Proxy connection failed. Make sure the username and password are correct.` | Copy the credentials again from the IPRoyal dashboard — most often a username typo. |
| `Residential Proxy unreachable.` | `IPROYAL_HOST` / `IPROYAL_PORT` are correct (defaults: `geo.iproyal.com:12321`); also check your local network and any firewall blocking port 12321. |
| `Proxy unit tests failed.` | Run `pytest tests/test_proxy_manager.py -v` for the full traceback. |

> **Pytest isolation:** the smoke test is the *only* surface that consumes IPRoyal data. The `tests/test_proxy_manager.py` unit suite is fully mocked, so a normal `pytest` run never touches the network or burns proxy traffic.

> **Why not a desktop VPN?** A desktop VPN gives every request the same exit IP. After ~50 LinkedIn page loads the VPN's exit IP gets fingerprinted as "the apply-daemon machine" and you're back to manual reCAPTCHAs. Rotating residential proxies put each request behind a different real consumer Wi-Fi network, which is what fooled the anti-bot algorithm in the first place.
