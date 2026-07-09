"""Tests for src/email_aggregator.py — candidate parsing + staged selection."""

from __future__ import annotations

from src.email_aggregator import (
    ListingCandidate,
    _unwrap_google_redirect,
    parse_candidates,
    score_candidates,
    select_top_n,
)

DIGEST_HTML = """
<html><body>
  <table>
    <tr><td>
      <a href="https://www.linkedin.com/comm/jobs/view/4001?tracking=x">
        Senior Machine Learning Engineer
      </a>
      <span>Acme Robotics &middot; Remote &middot; $180k</span>
    </td></tr>
    <tr><td>
      <a href="https://www.linkedin.com/comm/jobs/view/4002">
        Staff AI Platform Engineer
      </a>
      <span>Beta Health &middot; San Francisco, CA</span>
    </td></tr>
    <tr><td><a href="https://www.linkedin.com/e/unsubscribe?x=1">Unsubscribe</a></td></tr>
    <tr><td><a href="https://www.linkedin.com/jobs/search">See all jobs</a></td></tr>
    <tr><td><a href="https://www.linkedin.com/help/x">Help Center</a></td></tr>
  </table>
</body></html>
"""


def _parse(html: str = DIGEST_HTML) -> list[ListingCandidate]:
    return parse_candidates(
        html,
        source="linkedin",
        tier="friendly",
        email_uid=b"7",
        gmail_message_id="<d@x>",
        email_age_days=1.0,
    )


class TestParseCandidates:
    def test_extracts_job_links_only(self):
        candidates = _parse()
        titles = [c.title for c in candidates]
        assert "Senior Machine Learning Engineer" in titles
        assert "Staff AI Platform Engineer" in titles
        assert len(candidates) == 2  # chrome links filtered

    def test_snippet_captures_parent_block(self):
        candidates = _parse()
        ml = next(c for c in candidates if "Machine Learning" in c.title)
        assert "Acme Robotics" in ml.snippet

    def test_carries_email_context(self):
        candidates = _parse()
        assert all(c.email_uid == b"7" for c in candidates)
        assert all(c.tier == "friendly" for c in candidates)
        assert all(c.source == "linkedin" for c in candidates)

    def test_dedupes_repeated_title_host(self):
        html = DIGEST_HTML + DIGEST_HTML
        assert len(_parse(html)) == 2

    def test_empty_html_yields_nothing(self):
        assert _parse("<html><body><p>plain text digest</p></body></html>") == []

    def test_short_anchor_text_rejected(self):
        html = '<a href="https://x.com/jobs/1">Job</a>'
        assert _parse(html) == []


class TestGoogleRedirectUnwrap:
    def test_unwraps_url_param(self):
        wrapped = "https://www.google.com/url?rct=j&url=https://boards.example.com/j/1&ct=ga"
        assert _unwrap_google_redirect(wrapped) == "https://boards.example.com/j/1"

    def test_unwraps_q_param(self):
        wrapped = "https://www.google.com/url?q=https://boards.example.com/j/2"
        assert _unwrap_google_redirect(wrapped) == "https://boards.example.com/j/2"

    def test_passthrough_non_google(self):
        url = "https://www.linkedin.com/comm/jobs/view/1"
        assert _unwrap_google_redirect(url) == url

    def test_candidate_urls_are_unwrapped(self):
        html = (
            '<a href="https://www.google.com/url?url=https://startup.jobs/ml-eng-99">'
            "Machine Learning Engineer at Startup</a>"
        )
        candidates = _parse(html)
        assert candidates[0].url == "https://startup.jobs/ml-eng-99"


def _cand(title: str, tier: str = "ok", age: float = 0.0) -> ListingCandidate:
    return ListingCandidate(
        title=title, url=f"https://x.com/{title[:8]}", tier=tier, email_age_days=age
    )


class TestScoreCandidates:
    def test_friendly_tier_outranks_ok(self):
        friendly = _cand("AI Engineer One", tier="friendly")
        ok = _cand("AI Engineer Two", tier="ok")
        score_candidates([friendly, ok], recent_titles=[])
        assert friendly.score > ok.score

    def test_fresh_email_outranks_old(self):
        fresh = _cand("AI Engineer One", age=0.0)
        old = _cand("AI Engineer Two", age=10.0)
        score_candidates([fresh, old], recent_titles=[])
        assert fresh.score > old.score

    def test_novel_title_outranks_known(self):
        novel = _cand("Quantum Compiler Engineer")
        known = _cand("Senior Machine Learning Engineer")
        score_candidates(
            [novel, known], recent_titles=["Senior Machine Learning Engineer"]
        )
        assert novel.score > known.score


class TestSelectTopN:
    def test_no_cap_returns_all_unprobed(self):
        cands = [_cand(f"Engineer Number {i} Role") for i in range(5)]
        probe_calls = []

        def spy_probe(url):
            probe_calls.append(url)
            return False, ""

        selected, dropped = select_top_n(cands, top_n=None, probe_fn=spy_probe)
        assert len(selected) == 5
        assert dropped == 0
        assert probe_calls == []

    def test_caps_at_top_n(self):
        cands = [_cand(f"Engineer Number {i} Role") for i in range(6)]
        score_candidates(cands, recent_titles=[])
        selected, _ = select_top_n(cands, top_n=2, probe_fn=lambda u: (False, ""))
        assert len(selected) == 2

    def test_expired_candidates_dropped_and_replaced(self):
        high = _cand("Best Role Here", tier="friendly")
        mid = _cand("Second Role Here", tier="ok")
        low = _cand("Third Role Here", tier="hostile")
        score_candidates([high, mid, low], recent_titles=[])

        def probe_fn(url):
            return (url == high.url), "expired"

        selected, dropped = select_top_n([high, mid, low], top_n=2, probe_fn=probe_fn)
        assert dropped == 1
        assert high not in selected
        assert mid in selected and low in selected

    def test_probe_budget_is_two_x_top_n(self):
        cands = [_cand(f"Engineer Number {i} Role") for i in range(10)]
        score_candidates(cands, recent_titles=[])
        probe_calls = []

        def all_expired(url):
            probe_calls.append(url)
            return True, "expired"

        selected, dropped = select_top_n(cands, top_n=3, probe_fn=all_expired)
        assert selected == []
        assert len(probe_calls) == 6  # 2 × top_n, not the whole pool
        assert dropped == 6

    def test_selection_is_score_ordered(self):
        low = _cand("Alpha Role Position", tier="hostile")
        high = _cand("Beta Role Position", tier="friendly")
        score_candidates([low, high], recent_titles=[])
        selected, _ = select_top_n([low, high], top_n=1, probe_fn=lambda u: (False, ""))
        assert selected == [high]
