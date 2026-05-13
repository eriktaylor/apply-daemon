"""Sticky-session rotating-proxy manager for IPRoyal residential endpoints.

Wraps the IPRoyal "magic string" username convention so callers get a single
proxy URL that holds the same exit IP for a configurable lifetime (default
30 minutes), then rotate via either:

1. **TTL expiry** — the wall-clock lifetime elapses, so the next caller of
   ``current_proxy()`` regenerates the session id.
2. **Block signal** — the scraper observes HTTP 403 (Cloudflare),
   429 (rate-limited / DataDome), or 999 (LinkedIn auth wall) and calls
   ``report_status()`` to force an immediate rotation.

The manager stays a no-op when ``IPROYAL_USERNAME`` / ``IPROYAL_PASSWORD``
are not set, so the pipeline degrades gracefully to direct local-IP scraping
when a residential proxy block has not been provisioned.

IPRoyal "magic string" reference:
    https://docs.iproyal.com/proxies/residential/proxy/rotation

Magic-string lives in the PASSWORD field (not the username):
    {PASSWORD}_session-{8charID}_lifetime-{N}m
Endpoint:
    geo.iproyal.com:12321 (HTTP/SOCKS5; HTTP used by default for jobspy)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Status codes that mean "this exit IP is burned — rotate now".
#   403 — Cloudflare / DataDome bot challenge
#   429 — generic rate limiting
#   999 — LinkedIn auth wall / non-standard block
ROTATE_ON_STATUS = frozenset({403, 429, 999})

_SESSION_ID_ALPHABET = string.ascii_lowercase + string.digits

# Default location for the cross-process state file. Lives at the repo root
# so consecutive CLI invocations (proxy_test → jobspy_ingest) rehydrate the
# same sticky session while it is still inside its lifetime.
DEFAULT_STATE_PATH = Path(".cache/iproyal_session.json")


def _new_session_id(length: int = 8) -> str:
    """Generate a cryptographically random lowercase-alphanumeric session id."""
    return "".join(secrets.choice(_SESSION_ID_ALPHABET) for _ in range(length))


@dataclass
class _SessionState:
    session_id: str
    born_at_monotonic: float  # in-process expiry checks
    born_at_wall: float  # wall-clock for cross-process persistence


class ProxyManager:
    """Generate and rotate IPRoyal sticky-session proxy URLs.

    The class is thread-safe; callers in concurrent fetchers may share a
    single instance. ``current_proxy()`` returns the active proxy URL,
    auto-rotating when the TTL has expired. ``report_status()`` forces
    immediate rotation when the caller observes an anti-bot status code.

    When the IPRoyal credentials are missing from the environment the
    manager reports ``enabled is False`` and all accessors return ``None`` /
    empty containers, so call sites can wire it in unconditionally.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        host: str | None = None,
        port: int | None = None,
        lifetime_minutes: int | None = None,
        scheme: str | None = None,
        clock: callable = time.monotonic,
        wall_clock: callable = time.time,
        state_path: Path | str | None = None,
        disable_persistence: bool = False,
    ) -> None:
        self._username = username if username is not None else os.getenv("IPROYAL_USERNAME", "")
        self._password = password if password is not None else os.getenv("IPROYAL_PASSWORD", "")
        self._host = host or os.getenv("IPROYAL_HOST", "geo.iproyal.com")
        self._port = int(port if port is not None else os.getenv("IPROYAL_PORT", "12321"))
        self._lifetime_minutes = int(
            lifetime_minutes
            if lifetime_minutes is not None
            else os.getenv("IPROYAL_SESSION_TTL_MINUTES", "30")
        )
        if self._lifetime_minutes <= 0:
            raise ValueError("lifetime_minutes must be > 0")
        scheme_raw = (scheme or os.getenv("IPROYAL_SCHEME", "http")).lower()
        if scheme_raw not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"Unsupported proxy scheme: {scheme_raw}")
        self._scheme = scheme_raw

        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._state: _SessionState | None = None

        self._disable_persistence = disable_persistence
        self._state_path: Path | None = (
            None if disable_persistence
            else Path(state_path if state_path is not None else DEFAULT_STATE_PATH)
        )

        if self.enabled and self._state_path is not None:
            self._state = self._load_state()

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when both IPRoyal credentials are configured."""
        return bool(self._username) and bool(self._password)

    @property
    def lifetime_seconds(self) -> int:
        return self._lifetime_minutes * 60

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _build_url(self, session_id: str) -> str:
        password = (
            f"{self._password}_session-{session_id}"
            f"_lifetime-{self._lifetime_minutes}m"
        )
        return f"{self._scheme}://{self._username}:{password}@{self._host}:{self._port}"

    def _is_expired(self, state: _SessionState) -> bool:
        return (self._clock() - state.born_at_monotonic) >= self.lifetime_seconds

    def _new_state(self) -> _SessionState:
        return _SessionState(
            session_id=_new_session_id(),
            born_at_monotonic=self._clock(),
            born_at_wall=self._wall_clock(),
        )

    def current_proxy(self) -> str | None:
        """Return the active proxy URL, regenerating if TTL has expired.

        Returns ``None`` when the manager is disabled (no credentials).
        """
        if not self.enabled:
            return None
        with self._lock:
            if self._state is None or self._is_expired(self._state):
                self._state = self._new_state()
                logger.info(
                    "ProxyManager: opened sticky session %s (lifetime=%dm)",
                    self._state.session_id, self._lifetime_minutes,
                )
                self._save_state()
            return self._build_url(self._state.session_id)

    def force_rotate(self, reason: str = "manual") -> str | None:
        """Discard the current sticky session and mint a fresh one immediately.

        Returns the new proxy URL, or ``None`` when disabled.
        """
        if not self.enabled:
            return None
        with self._lock:
            old_id = self._state.session_id if self._state else None
            self._state = self._new_state()
            logger.info(
                "ProxyManager: rotated session %s -> %s (reason=%s)",
                old_id, self._state.session_id, reason,
            )
            self._save_state()
            return self._build_url(self._state.session_id)

    def report_status(self, status_code: int) -> bool:
        """Notify the manager of an HTTP response status from a downstream fetch.

        Triggers an immediate rotation when the status indicates a bot block
        (see ``ROTATE_ON_STATUS``). Returns True if a rotation happened.
        """
        if not self.enabled:
            return False
        if status_code not in ROTATE_ON_STATUS:
            return False
        self.force_rotate(reason=f"http_{status_code}")
        return True

    # ------------------------------------------------------------------
    # Adapter helpers
    # ------------------------------------------------------------------

    def proxies_dict(self) -> dict[str, str]:
        """Return a ``{"http": url, "https": url}`` dict for ``requests``.

        Empty dict when disabled, so it can be splatted into ``requests.get``
        unconditionally.
        """
        url = self.current_proxy()
        if url is None:
            return {}
        return {"http": url, "https": url}

    def proxies_list(self) -> list[str]:
        """Return a single-element list of the active proxy URL for jobspy.

        Empty list when disabled. ``scrape_jobs(proxies=[...])`` accepts this
        shape directly.
        """
        url = self.current_proxy()
        return [url] if url else []

    # ------------------------------------------------------------------
    # Safe diagnostics (never includes the password)
    # ------------------------------------------------------------------

    def describe(self) -> str:
        """Return a human-readable, password-free summary for log output."""
        if not self.enabled:
            return "ProxyManager(disabled — no IPRoyal credentials)"
        username_preview = self._username[:3] + "***" if self._username else "<empty>"
        session_id = self._state.session_id if self._state else "<not-yet-opened>"
        return (
            f"ProxyManager(user={username_preview}, host={self._host}:{self._port}, "
            f"scheme={self._scheme}, session={session_id}, "
            f"lifetime={self._lifetime_minutes}m)"
        )

    # ------------------------------------------------------------------
    # Cross-process persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> _SessionState | None:
        """Rehydrate a session from disk if the wall-clock TTL still applies.

        Returns ``None`` on any error (missing file, corrupt JSON, mismatched
        lifetime, expired TTL) — the caller will lazy-init a fresh session.
        """
        path = self._state_path
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session_id = data["session_id"]
            born_at_wall = float(data["born_at_wall"])
            persisted_lifetime = int(data["lifetime_minutes"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.debug("ProxyManager: state file unreadable, ignoring", exc_info=True)
            return None

        if persisted_lifetime != self._lifetime_minutes:
            logger.debug(
                "ProxyManager: persisted lifetime %dm != configured %dm — discarding",
                persisted_lifetime, self._lifetime_minutes,
            )
            return None

        age_seconds = self._wall_clock() - born_at_wall
        if age_seconds >= self.lifetime_seconds or age_seconds < 0:
            logger.debug(
                "ProxyManager: persisted session aged %.0fs (lifetime=%ds) — discarding",
                age_seconds, self.lifetime_seconds,
            )
            return None

        # Map remaining wall-clock TTL onto the in-process monotonic clock so
        # _is_expired() retires the rehydrated session at the correct moment.
        born_at_monotonic = self._clock() - age_seconds
        logger.info(
            "ProxyManager: rehydrated sticky session %s (%.0fs remaining)",
            session_id, self.lifetime_seconds - age_seconds,
        )
        return _SessionState(
            session_id=session_id,
            born_at_monotonic=born_at_monotonic,
            born_at_wall=born_at_wall,
        )

    def _save_state(self) -> None:
        """Atomically write the current session to disk."""
        if self._disable_persistence or self._state_path is None or self._state is None:
            return
        path = self._state_path
        payload = {
            "session_id": self._state.session_id,
            "born_at_wall": self._state.born_at_wall,
            "lifetime_minutes": self._lifetime_minutes,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            # Persistence is best-effort — proxy operation must continue
            # even when the cache directory is unwritable.
            logger.debug("ProxyManager: failed to persist session state", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default: ProxyManager | None = None
_default_lock = threading.Lock()


def get_default_proxy_manager() -> ProxyManager:
    """Lazy-init a process-wide ProxyManager from environment variables."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = ProxyManager()
    return _default


def reset_default_proxy_manager() -> None:
    """Drop the cached singleton — exposed for tests."""
    global _default
    with _default_lock:
        _default = None
