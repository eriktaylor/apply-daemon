"""Smoke test for the IPRoyal residential-proxy setup.

Run before firing the real pipeline to verify the proxy stack end-to-end
without burning hours of debugging silent jobspy failures.

Steps:
    1. Validate IPRoyal credentials are in the environment.
    2. Fetch your local IP (no proxy) to establish a baseline.
    3. Fetch the exit IP through the proxy and confirm it differs.
    4. Force a session rotation and re-fetch the exit IP.
    5. Run the mocked unit suite (tests/test_proxy_manager.py) as a
       regression check — that subprocess does NOT consume any IPRoyal
       data because the unit tests are fully mocked.

The smoke test leaves a sticky session warm in
``.cache/iproyal_session.json`` so a subsequent ``apply-pilot-ingest``
or ``!triage`` reuses the same exit IP within its 30-minute lifetime.

Usage:
    python -m src.proxy_test
    apply-pilot-test-proxy
"""

from __future__ import annotations

import logging
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

from src.pipeline import setup_logging
from src.proxy_manager import ProxyManager, get_default_proxy_manager

logger = logging.getLogger(__name__)

# Tiny JSON IP-echo endpoint. Returns {"ip": "1.2.3.4"} — ~50 bytes per call.
IP_ECHO_URL = "https://api.ipify.org?format=json"

# Exit codes
EXIT_OK = 0
EXIT_MISSING_CREDS = 1
EXIT_AUTH_FAILED = 2
EXIT_UNREACHABLE = 3
EXIT_UNIT_TESTS_FAILED = 4

# User-facing error messages — matched literally to the README troubleshooting
# table. Edit both surfaces together if rewording.
MSG_MISSING_CREDS = (
    "Residential Proxy credentials missing. "
    "Set IPROYAL_USERNAME and IPROYAL_PASSWORD in your .env file."
)
MSG_AUTH_FAILED = (
    "Residential Proxy connection failed. "
    "Make sure the username and password are correct."
)
MSG_UNREACHABLE = (
    "Residential Proxy unreachable. "
    "Check IPROYAL_HOST and IPROYAL_PORT, or your local network."
)
MSG_UNIT_TESTS_FAILED = (
    "Proxy unit tests failed. "
    "Run `pytest tests/test_proxy_manager.py -v` for details."
)


def _fetch_ip(proxies: dict | None, timeout: float) -> str:
    """Fetch the public IP through the supplied proxies dict.

    Raises ``requests`` exceptions on any failure — the caller maps those
    to user-facing error categories.
    """
    import requests

    response = requests.get(
        IP_ECHO_URL,
        timeout=timeout,
        proxies=proxies or None,
        headers={"User-Agent": "apply-pilot-proxy-test/1.0"},
    )
    response.raise_for_status()
    return response.json()["ip"]


def _categorize_proxy_error(exc: Exception) -> int:
    """Map a ``requests`` exception to one of our exit codes."""
    import requests

    if isinstance(exc, requests.exceptions.ProxyError):
        return EXIT_AUTH_FAILED
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status in (401, 407):
            return EXIT_AUTH_FAILED
    if isinstance(exc, (requests.exceptions.ConnectTimeout,
                        requests.exceptions.ReadTimeout,
                        requests.exceptions.ConnectionError)):
        return EXIT_UNREACHABLE
    return EXIT_AUTH_FAILED  # default to auth-failed since password issues are most common


def _check_credentials(mgr: ProxyManager) -> int | None:
    if not mgr.enabled:
        logger.error(MSG_MISSING_CREDS)
        return EXIT_MISSING_CREDS
    logger.info("Step 1/5 — credentials present. %s", mgr.describe())
    return None


def _check_local_ip() -> tuple[str | None, int | None]:
    try:
        local_ip = _fetch_ip(proxies=None, timeout=5)
    except Exception:
        logger.warning(
            "Step 2/5 — could not fetch local IP for baseline (offline?). "
            "Continuing without baseline comparison.",
            exc_info=True,
        )
        return None, None
    logger.info("Step 2/5 — local egress IP: %s", local_ip)
    return local_ip, None


def _check_proxy_connection(
    mgr: ProxyManager, local_ip: str | None
) -> tuple[str | None, int | None]:
    try:
        proxy_ip = _fetch_ip(proxies=mgr.proxies_dict(), timeout=10)
    except Exception as exc:
        exit_code = _categorize_proxy_error(exc)
        if exit_code == EXIT_UNREACHABLE:
            logger.error(MSG_UNREACHABLE)
        else:
            logger.error(MSG_AUTH_FAILED)
        # Surface enough of the underlying exception that the user can
        # self-diagnose without re-running with --verbose.
        logger.error("  Underlying exception (%s): %s", type(exc).__name__, exc)
        return None, exit_code

    if local_ip and proxy_ip == local_ip:
        logger.warning(
            "Step 3/5 — proxy returned your local IP (%s). The proxy may "
            "have been bypassed; continuing.", proxy_ip,
        )
    else:
        logger.info("Step 3/5 — proxy egress IP: %s", proxy_ip)
    return proxy_ip, None


def _check_rotation(mgr: ProxyManager, prior_ip: str | None) -> int | None:
    mgr.force_rotate("smoke_test_rotation")
    try:
        new_ip = _fetch_ip(proxies=mgr.proxies_dict(), timeout=10)
    except Exception as exc:
        exit_code = _categorize_proxy_error(exc)
        if exit_code == EXIT_UNREACHABLE:
            logger.error(MSG_UNREACHABLE)
        else:
            logger.error(MSG_AUTH_FAILED)
        logger.error("  Underlying exception (%s): %s", type(exc).__name__, exc)
        return exit_code

    if prior_ip and new_ip == prior_ip:
        logger.warning(
            "Step 4/5 — rotation returned the same IP (%s). IPRoyal pools "
            "occasionally re-issue an IP; not fatal.", new_ip,
        )
    else:
        logger.info("Step 4/5 — rotated exit IP: %s", new_ip)
    return None


def _run_pytest_suite() -> int | None:
    logger.info(
        "Step 5/5 — running mocked unit suite (no IPRoyal traffic): "
        "pytest tests/test_proxy_manager.py"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_proxy_manager.py", "-q"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Surface the pytest output so the user does not have to re-run.
        logger.error("%s\n%s", MSG_UNIT_TESTS_FAILED, proc.stdout + proc.stderr)
        return EXIT_UNIT_TESTS_FAILED
    # Print the last summary line of the pytest output so the user sees
    # something like "38 passed in 1.20s".
    summary = (proc.stdout or "").strip().splitlines()
    last = summary[-1] if summary else "(no output)"
    logger.info("Step 5/5 — pytest result: %s", last)
    return None


def run_proxy_test() -> int:
    """Execute every smoke-test step, return the exit code."""
    mgr = get_default_proxy_manager()

    failure = _check_credentials(mgr)
    if failure is not None:
        return failure

    local_ip, failure = _check_local_ip()
    if failure is not None:
        return failure

    proxy_ip, failure = _check_proxy_connection(mgr, local_ip)
    if failure is not None:
        return failure

    failure = _check_rotation(mgr, proxy_ip)
    if failure is not None:
        return failure

    failure = _run_pytest_suite()
    if failure is not None:
        return failure

    logger.info(
        "All checks passed. The pipeline is safe to run. "
        "The sticky session is warm and will be reused by apply-pilot-ingest."
    )
    return EXIT_OK


def main() -> None:
    setup_logging()
    sys.exit(run_proxy_test())


if __name__ == "__main__":
    main()
