"""Unit tests for src/proxy_manager.py — IPRoyal sticky-session manager.

Uses pytest-mock to drive a fake clock and verify:
- TTL-based rotation (30-minute lifecycle)
- Forced rotation on HTTP 403 / 429 / 999 (DataDome / Cloudflare / LinkedIn)
- Magic-string username format matches the IPRoyal documentation
- Disabled-state behaviour when credentials are absent
"""

from __future__ import annotations

import json
import re

import pytest

from src.proxy_manager import (
    ROTATE_ON_STATUS,
    ProxyManager,
    get_default_proxy_manager,
    reset_default_proxy_manager,
)


class _FakeClock:
    """Deterministic monotonic-clock substitute for pytest-mock time-travel."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def manager(clock: _FakeClock) -> ProxyManager:
    return ProxyManager(
        username="apply_pilot_user",
        password="hunter2",
        host="geo.iproyal.com",
        port=12321,
        lifetime_minutes=30,
        scheme="http",
        clock=clock,
        disable_persistence=True,
    )


@pytest.fixture(autouse=True)
def _clear_singleton(monkeypatch, tmp_path):
    """Ensure the module-level singleton and on-disk cache never leak."""
    # Send the default state path into a per-test tmp dir so a stray default
    # ProxyManager() does not write to the real .cache/.
    monkeypatch.chdir(tmp_path)
    reset_default_proxy_manager()
    yield
    reset_default_proxy_manager()


# ---------------------------------------------------------------------------
# Capability / disabled-state
# ---------------------------------------------------------------------------

class TestEnabled:
    def test_enabled_when_credentials_present(self, manager: ProxyManager):
        assert manager.enabled is True

    def test_disabled_when_username_missing(self, monkeypatch):
        monkeypatch.delenv("IPROYAL_USERNAME", raising=False)
        monkeypatch.delenv("IPROYAL_PASSWORD", raising=False)
        mgr = ProxyManager()
        assert mgr.enabled is False

    def test_disabled_manager_returns_none_proxy(self):
        mgr = ProxyManager(username="", password="")
        assert mgr.current_proxy() is None
        assert mgr.proxies_dict() == {}
        assert mgr.proxies_list() == []

    def test_disabled_manager_swallows_status_reports(self):
        mgr = ProxyManager(username="", password="")
        assert mgr.report_status(429) is False
        assert mgr.force_rotate() is None


# ---------------------------------------------------------------------------
# Magic-string format
# ---------------------------------------------------------------------------

class TestMagicString:
    def test_url_includes_session_and_lifetime(self, manager: ProxyManager):
        url = manager.current_proxy()
        # IPRoyal magic-string lives in the PASSWORD field per their docs:
        # http://USER:PASS_session-XXXXXXXX_lifetime-30m@HOST:PORT
        match = re.match(
            r"^http://apply_pilot_user:hunter2_session-([a-z0-9]{8})_lifetime-30m"
            r"@geo\.iproyal\.com:12321$",
            url,
        )
        assert match is not None, f"URL did not match magic-string format: {url}"

    def test_session_id_is_8_lowercase_alphanumeric(self, manager: ProxyManager):
        url = manager.current_proxy()
        session_id = re.search(r"_session-([^_]+)_lifetime", url).group(1)
        assert len(session_id) == 8
        assert re.fullmatch(r"[a-z0-9]+", session_id)

    def test_lifetime_minutes_reflected_in_url(self, clock: _FakeClock):
        mgr = ProxyManager(
            username="u", password="p", lifetime_minutes=15, clock=clock,
            disable_persistence=True,
        )
        assert "_lifetime-15m" in mgr.current_proxy()

    def test_invalid_lifetime_rejected(self):
        with pytest.raises(ValueError):
            ProxyManager(
                username="u", password="p", lifetime_minutes=0,
                disable_persistence=True,
            )

    def test_unsupported_scheme_rejected(self):
        with pytest.raises(ValueError):
            ProxyManager(
                username="u", password="p", scheme="ftp",
                disable_persistence=True,
            )

    def test_socks5_scheme_supported(self, clock: _FakeClock):
        mgr = ProxyManager(
            username="u", password="p", scheme="socks5", clock=clock,
            disable_persistence=True,
        )
        assert mgr.current_proxy().startswith("socks5://")


# ---------------------------------------------------------------------------
# TTL-based rotation
# ---------------------------------------------------------------------------

class TestTTLRotation:
    def test_same_session_within_lifetime(self, manager: ProxyManager, clock: _FakeClock):
        first = manager.current_proxy()
        clock.advance(60 * 29)  # 29 minutes — still inside the 30m TTL
        second = manager.current_proxy()
        assert first == second

    def test_session_rotates_after_ttl(self, manager: ProxyManager, clock: _FakeClock):
        first = manager.current_proxy()
        clock.advance(60 * 30)  # exactly 30 minutes — TTL elapsed
        second = manager.current_proxy()
        assert first != second

    def test_session_rotates_well_after_ttl(self, manager: ProxyManager, clock: _FakeClock):
        first = manager.current_proxy()
        clock.advance(60 * 90)  # 1.5 hours later
        second = manager.current_proxy()
        assert first != second

    def test_first_call_creates_session(self, manager: ProxyManager):
        # Sanity: no session at construction; lazy-init on first request.
        url = manager.current_proxy()
        assert url is not None
        assert "_session-" in url


# ---------------------------------------------------------------------------
# Forced rotation on bot-block status codes
# ---------------------------------------------------------------------------

class TestForceRotateOnStatus:
    @pytest.mark.parametrize("status", sorted(ROTATE_ON_STATUS))
    def test_block_codes_trigger_rotation(
        self, manager: ProxyManager, status: int, clock: _FakeClock
    ):
        first = manager.current_proxy()
        rotated = manager.report_status(status)
        assert rotated is True
        second = manager.current_proxy()
        assert second != first

    @pytest.mark.parametrize("status", [200, 301, 404, 500, 502, 503])
    def test_non_block_codes_do_not_rotate(
        self, manager: ProxyManager, status: int
    ):
        first = manager.current_proxy()
        rotated = manager.report_status(status)
        assert rotated is False
        assert manager.current_proxy() == first

    def test_force_rotate_resets_ttl(self, manager: ProxyManager, clock: _FakeClock):
        manager.current_proxy()
        clock.advance(60 * 25)
        rotated_url = manager.force_rotate(reason="test")
        # Right after rotation we should be well within the new TTL.
        clock.advance(60 * 4)
        assert manager.current_proxy() == rotated_url


# ---------------------------------------------------------------------------
# Adapter helpers
# ---------------------------------------------------------------------------

class TestAdapterHelpers:
    def test_proxies_dict_has_http_and_https(self, manager: ProxyManager):
        d = manager.proxies_dict()
        assert set(d.keys()) == {"http", "https"}
        assert d["http"] == d["https"]

    def test_proxies_list_single_entry(self, manager: ProxyManager):
        lst = manager.proxies_list()
        assert isinstance(lst, list)
        assert len(lst) == 1
        assert lst[0].startswith("http://")


# ---------------------------------------------------------------------------
# Mocked HTTP integration with _scrape_url
# ---------------------------------------------------------------------------

class TestScraperIntegration:
    """Verify that triage._scrape_url rotates the proxy on bot-block codes
    without making any real network calls. Uses pytest-mock to stub
    requests.Session.get and freeze the singleton."""

    def test_scrape_rotates_on_429(self, mocker, monkeypatch):
        monkeypatch.setenv("IPROYAL_USERNAME", "u")
        monkeypatch.setenv("IPROYAL_PASSWORD", "p")
        reset_default_proxy_manager()

        mgr = get_default_proxy_manager()
        first_url = mgr.current_proxy()

        fake_response = mocker.Mock()
        fake_response.status_code = 429
        fake_response.text = ""
        mocker.patch(
            "requests.Session.get", return_value=fake_response,
        )

        from src.triage import _scrape_url
        result = _scrape_url("https://example.com/job")

        assert result is None  # 429 returns None
        assert mgr.current_proxy() != first_url  # rotated

    def test_scrape_does_not_rotate_on_200(self, mocker, monkeypatch):
        monkeypatch.setenv("IPROYAL_USERNAME", "u")
        monkeypatch.setenv("IPROYAL_PASSWORD", "p")
        reset_default_proxy_manager()

        mgr = get_default_proxy_manager()
        first_url = mgr.current_proxy()

        fake_response = mocker.Mock()
        fake_response.status_code = 200
        fake_response.text = "<html><body>" + ("real job content " * 50) + "</body></html>"
        mocker.patch(
            "requests.Session.get", return_value=fake_response,
        )
        # Ignore trafilatura; we only care about rotation behaviour.
        mocker.patch("trafilatura.extract", return_value="x" * 200)

        from src.triage import _scrape_url
        _scrape_url("https://example.com/job")

        assert mgr.current_proxy() == first_url

    def test_scrape_passes_proxies_to_requests(self, mocker, monkeypatch):
        monkeypatch.setenv("IPROYAL_USERNAME", "u")
        monkeypatch.setenv("IPROYAL_PASSWORD", "p")
        reset_default_proxy_manager()

        fake_response = mocker.Mock(status_code=200, text="")
        get_spy = mocker.patch("requests.Session.get", return_value=fake_response)

        from src.triage import _scrape_url
        _scrape_url("https://example.com/job")

        kwargs = get_spy.call_args.kwargs
        assert kwargs["proxies"] is not None
        assert "http" in kwargs["proxies"]
        # Magic string lives in the password field per IPRoyal docs.
        assert kwargs["proxies"]["http"].startswith("http://u:p_session-")

    def test_scrape_with_no_proxy_credentials_passes_none(self, mocker, monkeypatch):
        monkeypatch.delenv("IPROYAL_USERNAME", raising=False)
        monkeypatch.delenv("IPROYAL_PASSWORD", raising=False)
        reset_default_proxy_manager()

        fake_response = mocker.Mock(status_code=200, text="")
        get_spy = mocker.patch("requests.Session.get", return_value=fake_response)

        from src.triage import _scrape_url
        _scrape_url("https://example.com/job")

        assert get_spy.call_args.kwargs["proxies"] is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_default_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("IPROYAL_USERNAME", "u")
        monkeypatch.setenv("IPROYAL_PASSWORD", "p")
        reset_default_proxy_manager()
        a = get_default_proxy_manager()
        b = get_default_proxy_manager()
        assert a is b

    def test_reset_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("IPROYAL_USERNAME", "u")
        monkeypatch.setenv("IPROYAL_PASSWORD", "p")
        a = get_default_proxy_manager()
        reset_default_proxy_manager()
        b = get_default_proxy_manager()
        assert a is not b


# ---------------------------------------------------------------------------
# Cross-process state persistence
# ---------------------------------------------------------------------------

class _FakeWallClock:
    """Wall-clock substitute for the persisted born_at timestamp."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestStatePersistence:
    def test_second_manager_rehydrates_session(self, tmp_path, clock: _FakeClock):
        path = tmp_path / "iproyal_session.json"
        wall = _FakeWallClock()

        first = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=clock, wall_clock=wall, state_path=path,
        )
        url1 = first.current_proxy()
        session_id_1 = re.search(r"_session-([^_]+)_lifetime", url1).group(1)

        # Simulate a fresh process: independent monotonic clock, advanced wall
        # clock (5 minutes later — well inside the 30m lifetime).
        wall.advance(60 * 5)
        second_clock = _FakeClock()
        second = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=second_clock, wall_clock=wall, state_path=path,
        )
        url2 = second.current_proxy()
        session_id_2 = re.search(r"_session-([^_]+)_lifetime", url2).group(1)

        assert session_id_2 == session_id_1, (
            "Second ProxyManager should rehydrate the persisted session id"
        )

    def test_expired_state_yields_fresh_session(self, tmp_path, clock: _FakeClock):
        path = tmp_path / "iproyal_session.json"
        wall = _FakeWallClock()

        first = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=clock, wall_clock=wall, state_path=path,
        )
        first.current_proxy()  # writes state at wall=now

        # Advance wall-clock past the 30-minute lifetime.
        wall.advance(60 * 31)
        second = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=_FakeClock(), wall_clock=wall, state_path=path,
        )
        # Internal state should be None — _load_state discards expired records.
        assert second._state is None

    def test_corrupt_json_is_ignored(self, tmp_path, clock: _FakeClock):
        path = tmp_path / "iproyal_session.json"
        path.write_text("{not valid json")

        # Construction must not raise.
        mgr = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=clock, state_path=path,
        )
        assert mgr._state is None
        # And current_proxy() should successfully mint a fresh session.
        assert mgr.current_proxy() is not None

    def test_lifetime_mismatch_discards_state(self, tmp_path, clock: _FakeClock):
        path = tmp_path / "iproyal_session.json"
        path.write_text(json.dumps({
            "session_id": "deadbeef",
            "born_at_wall": 1_700_000_000.0,
            "lifetime_minutes": 60,  # configured manager uses 30m
        }))
        wall = _FakeWallClock(start=1_700_000_000.0 + 60)
        mgr = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=clock, wall_clock=wall, state_path=path,
        )
        assert mgr._state is None  # mismatched lifetime → discard

    def test_force_rotate_persists_new_session(self, tmp_path, clock: _FakeClock):
        path = tmp_path / "iproyal_session.json"
        mgr = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=clock, state_path=path,
        )
        mgr.current_proxy()
        original_id = mgr._state.session_id

        mgr.force_rotate("test")
        new_id = mgr._state.session_id
        assert new_id != original_id

        on_disk = json.loads(path.read_text())
        assert on_disk["session_id"] == new_id

    def test_disable_persistence_skips_disk_writes(self, tmp_path, clock: _FakeClock):
        path = tmp_path / "iproyal_session.json"
        mgr = ProxyManager(
            username="u", password="p", lifetime_minutes=30,
            clock=clock, state_path=path, disable_persistence=True,
        )
        mgr.current_proxy()
        assert not path.exists()
