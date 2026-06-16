"""Unit tests for src/expired_probe.py — Fix 4b HTTP backstop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.expired_probe import probe


def _mock_response(status: int = 200, body: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raw.read.return_value = body
    resp.close.return_value = None
    return resp


class TestExpiredProbe:
    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "false")
        is_exp, _ = probe("https://example.com/job/1")
        assert is_exp is False

    def test_empty_url_no_op(self):
        is_exp, _ = probe("")
        assert is_exp is False

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_404_marks_expired(self, mock_pm, mock_get, monkeypatch):
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        mock_get.return_value = _mock_response(status=404)
        is_exp, reason = probe("https://example.com/job/1")
        assert is_exp is True
        assert "404" in reason

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_410_marks_expired(self, mock_pm, mock_get, monkeypatch):
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        mock_get.return_value = _mock_response(status=410)
        is_exp, _ = probe("https://example.com/job/1")
        assert is_exp is True

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_stop_phrase_marks_expired(self, mock_pm, mock_get, monkeypatch):
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        body = b"<html><body>No longer accepting applications.</body></html>"
        mock_get.return_value = _mock_response(status=200, body=body)
        is_exp, reason = probe("https://example.com/job/1")
        assert is_exp is True
        assert "no longer accepting applications" in reason.lower()

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_position_filled_marks_expired(self, mock_pm, mock_get, monkeypatch):
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        body = b"<html>This position has been filled.</html>"
        mock_get.return_value = _mock_response(status=200, body=body)
        is_exp, _ = probe("https://example.com/job/1")
        assert is_exp is True

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_healthy_page_passes(self, mock_pm, mock_get, monkeypatch):
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        body = b"<html>We are hiring a Senior FDE. Apply now!</html>"
        mock_get.return_value = _mock_response(status=200, body=body)
        is_exp, _ = probe("https://example.com/job/1")
        assert is_exp is False

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_request_exception_fails_open(self, mock_pm, mock_get, monkeypatch):
        """Probe failure (timeout, connection error) must never mark a row expired."""
        import requests
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        mock_get.side_effect = requests.Timeout("slow")
        is_exp, _ = probe("https://example.com/job/1")
        assert is_exp is False

    @patch("src.expired_probe.requests.get")
    @patch("src.expired_probe.get_default_proxy_manager")
    def test_whitespace_resilient_phrase_match(self, mock_pm, mock_get, monkeypatch):
        """HTML formatting (newlines, tabs) between words must not block the match."""
        monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "true")
        mgr = MagicMock()
        mgr.enabled = False
        mock_pm.return_value = mgr
        body = b"<html>\n  No\tlonger\n accepting   applications.\n</html>"
        mock_get.return_value = _mock_response(status=200, body=body)
        is_exp, _ = probe("https://example.com/job/1")
        assert is_exp is True
