"""Tests for the triage module (response parsing, extraction, evaluation, consensus)."""

import json
from unittest.mock import MagicMock, patch

from src.triage import (
    _clean_source_board,
    _consensus_label,
    _is_aggregator_url,
    _is_tracking_url,
    _parse_block_fields,
    _parse_evaluation_json,
    _parse_extraction_response,
    _parse_triage_response,
    auto_match_cutoff,
    evaluate_scrape_validity,
    get_confidence_threshold,
)


class TestParseBlockFields:
    def test_standard_fields(self):
        block = """LISTING:
title: Senior Backend Engineer
company: Acme Corp
location: Remote (US)
salary: $150k-$190k
verdict: YES
reason: Strong match for backend-focused role
links: https://example.com/job/123"""
        fields = _parse_block_fields(block)
        assert fields["title"] == "Senior Backend Engineer"
        assert fields["company"] == "Acme Corp"
        assert fields["location"] == "Remote (US)"
        assert fields["salary"] == "$150k-$190k"
        assert fields["verdict"] == "YES"
        assert fields["reason"] == "Strong match for backend-focused role"

    def test_recruiter_fields(self):
        block = """LISTING:
title: ML Engineer
company: StartupCo
location: San Francisco
salary: not listed
verdict: MAYBE
reason: Partial match
recruiter_name: Sarah Chen
recruiter_title: Engineering Manager"""
        fields = _parse_block_fields(block)
        assert fields["recruiter_name"] == "Sarah Chen"
        assert fields["recruiter_title"] == "Engineering Manager"

    def test_empty_block(self):
        fields = _parse_block_fields("")
        assert fields == {}

    def test_multiline_job_summary_joined(self):
        """Second sentence on its own line must not be silently dropped."""
        block = (
            "LISTING:\ntitle: SWE\ncompany: Acme\n"
            "job_summary: Acme Corp builds cloud infrastructure for fintech.\n"
            "This role leads backend API development and owns the payments service.\n"
            "verdict: YES"
        )
        fields = _parse_block_fields(block)
        assert "Acme Corp builds cloud infrastructure" in fields["job_summary"]
        assert "This role leads backend API" in fields["job_summary"]
        assert fields["verdict"] == "YES"

    def test_continuation_resets_on_blank_line(self):
        """A blank line must end continuation so the next key parses correctly."""
        block = "LISTING:\njob_summary: First sentence.\nSecond sentence.\n\nverdict: MAYBE"
        fields = _parse_block_fields(block)
        assert "Second sentence" in fields["job_summary"]
        assert fields.get("verdict") == "MAYBE"


class TestParseTriageResponse:
    def _parse(self, text, **kwargs):
        defaults = {
            "email_text": "test email",
            "email_links": [],
            "classification": "JOB_DIGEST",
            "source": "linkedin",
            "model": "gemma3:4b",
            "tokens": 100,
            "latency_ms": 500,
        }
        defaults.update(kwargs)
        return _parse_triage_response(text, **defaults)

    def test_single_listing(self):
        text = """LISTING:
title: Senior Backend Engineer
company: Acme Corp
location: Remote
salary: $150k
verdict: YES
confidence: 85
reason: Good match
links: none
---"""
        listings = self._parse(text)
        assert len(listings) == 1
        assert listings[0].title == "Senior Backend Engineer"
        assert listings[0].verdict == "YES"
        assert listings[0].confidence == 85
        assert listings[0].company == "Acme Corp"
        assert listings[0].model_used == "gemma3:4b"

    def test_multiple_listings(self):
        text = """LISTING:
title: Engineer A
company: Co A
location: Remote
salary: not listed
verdict: YES
confidence: 90
reason: Great match
links: none
---
LISTING:
title: Engineer B
company: Co B
location: NYC
salary: $200k
verdict: NO
confidence: 20
reason: Wrong location
links: none
---"""
        listings = self._parse(text)
        assert len(listings) == 2
        assert listings[0].verdict == "YES"
        assert listings[0].confidence == 90
        assert listings[1].verdict == "NO"
        assert listings[1].confidence == 20

    def test_invalid_verdict_defaults_to_maybe(self):
        text = """LISTING:
title: Some Role
company: Some Co
location: Remote
salary: not listed
verdict: UNSURE
reason: Not clear
links: none
---"""
        listings = self._parse(text)
        assert len(listings) == 1
        assert listings[0].verdict == "MAYBE"

    def test_no_listings_in_garbage_text(self):
        listings = self._parse("This is just random text with no structure")
        assert len(listings) == 0

    def test_links_from_response(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
reason: Match
links: https://example.com/job1, https://example.com/job2
---"""
        listings = self._parse(text)
        assert len(listings[0].links) == 2
        assert "https://example.com/job1" in listings[0].links

    def test_links_fallback_to_email_links(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
reason: Match
links: none
---"""
        listings = self._parse(text, email_links=["https://email-link.com/job"])
        assert "https://email-link.com/job" in listings[0].links

    def test_source_and_classification_propagated(self):
        text = """LISTING:
title: Role
company: Co
location: Remote
salary: not listed
verdict: MAYBE
reason: Partial
links: none
---"""
        listings = self._parse(
            text, source="recruiter", classification="RECRUITER_OUTREACH"
        )
        assert listings[0].source == "recruiter"
        assert listings[0].email_classification == "RECRUITER_OUTREACH"

    def test_confidence_defaults_to_50_if_missing(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
reason: Match
links: none
---"""
        listings = self._parse(text)
        assert listings[0].confidence == 50

    def test_model_scores_populated(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
confidence: 80
reason: Match
links: none
---"""
        listings = self._parse(text)
        scores = json.loads(listings[0].model_scores)
        assert len(scores) == 1
        assert scores[0]["model"] == "gemma3:4b"
        assert scores[0]["verdict"] == "YES"
        assert scores[0]["confidence"] == 80

    def test_single_model_defaults_to_triaged(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
confidence: 85
reason: Match
links: none
---"""
        listings = self._parse(text)
        assert listings[0].final_status == "triaged"

    def test_job_summary_parsed(self):
        text = """LISTING:
title: Senior Engineer
company: Acme Corp
location: Remote
salary: $180k
job_summary: Acme Corp builds cloud infrastructure. This role leads backend API development.
verdict: YES
confidence: 85
reason: Strong match
links: none
---"""
        listings = self._parse(text)
        assert "Acme Corp builds cloud infrastructure" in listings[0].job_summary

    def test_job_summary_empty_when_missing(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
confidence: 80
reason: Match
links: none
---"""
        listings = self._parse(text)
        assert listings[0].job_summary == ""


class TestIsTrackingUrl:
    def test_notifications_googleapis(self):
        assert _is_tracking_url("https://notifications.googleapis.com/email/redirect?...") is True

    def test_googleapis_subdomain(self):
        assert _is_tracking_url("https://foo.googleapis.com/track?url=abc") is True

    def test_google_url_redirect(self):
        assert _is_tracking_url("https://google.com/url?q=https://example.com") is True

    def test_www_google_url_redirect(self):
        assert _is_tracking_url("https://www.google.com/url?q=https://example.com") is True

    def test_regular_google_search_not_tracking(self):
        assert _is_tracking_url("https://google.com/search?q=ml+engineer") is False

    def test_linkedin_not_tracking(self):
        assert _is_tracking_url("https://linkedin.com/jobs/view/12345") is False

    def test_empty_string(self):
        assert _is_tracking_url("") is False

    def test_regular_ats_url(self):
        assert _is_tracking_url("https://boards.greenhouse.io/acme/jobs/123") is False


class TestCleanSourceBoard:
    def test_already_clean_domain(self):
        assert _clean_source_board("talent.com") == "talent.com"

    def test_strips_via_prefix(self):
        assert _clean_source_board("via Talent.com") == "talent.com"

    def test_via_without_tld(self):
        # "via JobLeads" → strip via, no dot → add .com
        assert _clean_source_board("via JobLeads") == "jobleads.com"

    def test_no_tld_adds_com(self):
        assert _clean_source_board("jobleads") == "jobleads.com"

    def test_spaces_collapsed(self):
        # Multi-word board name with no dot → collapse spaces then add .com
        assert _clean_source_board("job leads") == "jobleads.com"

    def test_none_returns_empty(self):
        assert _clean_source_board("none") == ""

    def test_empty_returns_empty(self):
        assert _clean_source_board("") == ""

    def test_lowercased(self):
        assert _clean_source_board("via LinkedIn.com") == "linkedin.com"

    def test_via_with_mixed_case_no_tld(self):
        assert _clean_source_board("via JobLeads") == "jobleads.com"


class TestParseExtractionResponse:
    def test_single_extraction(self):
        text = """LISTING:
title: Senior Engineer
company: BigCo
location: Remote
salary: $200k
description: A senior role building distributed systems
links: https://example.com/job
---"""
        results = _parse_extraction_response(text, [])
        assert len(results) == 1
        assert results[0].title == "Senior Engineer"
        assert results[0].company == "BigCo"
        assert results[0].description == "A senior role building distributed systems"

    def test_multiple_extractions(self):
        text = """LISTING:
title: Engineer A
company: Co A
location: NYC
salary: not listed
description: Role A description
links: none
---
LISTING:
title: Engineer B
company: Co B
location: Remote
salary: $150k
description: Role B description
links: none
---"""
        results = _parse_extraction_response(text, [])
        assert len(results) == 2
        assert results[0].title == "Engineer A"
        assert results[1].title == "Engineer B"

    def test_extraction_with_recruiter_fields(self):
        text = """LISTING:
title: Staff Engineer
company: StartupCo
location: SF
salary: not listed
description: A staff role
recruiter_name: Jane Doe
recruiter_title: Talent Lead
links: none
---"""
        results = _parse_extraction_response(text, [])
        assert results[0].recruiter_name == "Jane Doe"
        assert results[0].recruiter_title == "Talent Lead"

    def test_extraction_links_fallback(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
description: Some role
links: none
---"""
        results = _parse_extraction_response(text, ["https://fallback.com/job"])
        assert "https://fallback.com/job" in results[0].links

    def test_no_listings_returns_empty(self):
        results = _parse_extraction_response("random garbage text", [])
        assert results == []

    def test_extraction_job_summary(self):
        text = """LISTING:
title: ML Engineer
company: DataCo
location: NYC
salary: $200k
job_summary: DataCo is an AI-first analytics platform. This role owns the model training pipeline.
description: Build and maintain ML infrastructure
links: none
---"""
        results = _parse_extraction_response(text, [])
        assert "DataCo is an AI-first analytics platform" in results[0].job_summary

    def test_source_board_extracted(self):
        """'source_board: talent.com' in LLM output → stored on ExtractedListing."""
        text = (
            "LISTING:\ntitle: AI Engineer\ncompany: Harnham\n"
            "location: Oakland, CA\nsalary: not listed\n"
            "job_summary: Harnham is a data science recruiter. Core is ML.\n"
            "description: AI engineering role.\n"
            "source_board: talent.com\nlinks: none\n---\n"
        )
        results = _parse_extraction_response(text, [])
        assert len(results) == 1
        assert results[0].source_board == "talent.com"

    def test_source_board_none_stored_as_empty(self):
        """'source_board: none' (no attribution) → stored as empty string."""
        text = (
            "LISTING:\ntitle: Engineer\ncompany: Acme\n"
            "location: Remote\nsalary: not listed\n"
            "description: Some role.\nsource_board: none\nlinks: none\n---\n"
        )
        results = _parse_extraction_response(text, [])
        assert results[0].source_board == ""

    def test_source_board_absent_defaults_to_empty(self):
        """No source_board field in LLM output → defaults to empty string."""
        text = (
            "LISTING:\ntitle: Engineer\ncompany: Acme\n"
            "location: Remote\nsalary: not listed\n"
            "description: Some role.\nlinks: none\n---\n"
        )
        results = _parse_extraction_response(text, [])
        assert results[0].source_board == ""


class TestParseEvaluationJson:
    def test_valid_json(self):
        text = '{"verdict": "YES", "confidence": 85, "reasoning": "Strong match"}'
        result = _parse_evaluation_json(text)
        assert result["verdict"] == "YES"
        assert result["confidence"] == 85
        assert result["reasoning"] == "Strong match"

    def test_json_in_markdown_fences(self):
        text = '```json\n{"verdict": "NO", "confidence": 30, "reasoning": "Poor match"}\n```'
        result = _parse_evaluation_json(text)
        assert result["verdict"] == "NO"
        assert result["confidence"] == 30

    def test_invalid_verdict_normalized(self):
        text = '{"verdict": "STRONG_YES", "confidence": 90, "reasoning": "Great"}'
        result = _parse_evaluation_json(text)
        assert result["verdict"] == "MAYBE"

    def test_confidence_clamped(self):
        text = '{"verdict": "YES", "confidence": 150, "reasoning": "Over limit"}'
        result = _parse_evaluation_json(text)
        assert result["confidence"] == 100

    def test_confidence_clamped_negative(self):
        text = '{"verdict": "NO", "confidence": -10, "reasoning": "Under limit"}'
        result = _parse_evaluation_json(text)
        assert result["confidence"] == 0

    def test_garbage_falls_back(self):
        result = _parse_evaluation_json("This is not JSON at all")
        assert result["verdict"] == "MAYBE"
        assert result["confidence"] == 50

    def test_empty_string_falls_back(self):
        result = _parse_evaluation_json("")
        assert result["verdict"] == "MAYBE"
        assert result["confidence"] == 50

    def test_job_summary_extracted(self):
        import json as _json
        text = _json.dumps({
            "verdict": "YES", "confidence": 80, "reasoning": "Good match",
            "job_summary": "Acme is a seed-stage fintech. This role owns the payments API.",
            "skills_extracted": True, "matching_skills": ["Python"], "missing_skills": [],
        })
        result = _parse_evaluation_json(text)
        assert result["job_summary"] == "Acme is a seed-stage fintech. This role owns the payments API."

    def test_job_summary_empty_when_missing(self):
        import json as _json
        text = _json.dumps({
            "verdict": "MAYBE", "confidence": 50, "reasoning": "ok",
            "skills_extracted": False, "matching_skills": [], "missing_skills": [],
        })
        result = _parse_evaluation_json(text)
        assert result["job_summary"] == ""

    def test_job_summary_empty_on_fallback(self):
        result = _parse_evaluation_json("not json")
        assert result["job_summary"] == ""


class TestConfidenceThreshold:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
        assert get_confidence_threshold() == 0.5

    def test_parses_float(self, monkeypatch):
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.75")
        assert get_confidence_threshold() == 0.75

    def test_clamps_below_zero(self, monkeypatch):
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "-0.5")
        assert get_confidence_threshold() == 0.0

    def test_clamps_above_one(self, monkeypatch):
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "1.5")
        assert get_confidence_threshold() == 1.0

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "not-a-number")
        assert get_confidence_threshold() == 0.5


class TestAutoMatchCutoff:
    def test_floor_at_80(self):
        assert auto_match_cutoff(0.5) == 80

    def test_threshold_above_floor_wins(self):
        assert auto_match_cutoff(0.9) == 90

    def test_zero_threshold(self):
        assert auto_match_cutoff(0.0) == 80


class TestConsensusLabel:
    def test_yes_above_cutoff_is_auto_match(self):
        assert _consensus_label("YES", 85, 0.5) == "AUTO_MATCH"

    def test_yes_below_cutoff_needs_review(self):
        assert _consensus_label("YES", 60, 0.5) == "NEEDS_REVIEW"

    def test_no_is_standard(self):
        assert _consensus_label("NO", 90, 0.5) == "STANDARD"

    def test_maybe_is_standard(self):
        assert _consensus_label("MAYBE", 70, 0.5) == "STANDARD"

    def test_yes_at_strict_threshold(self):
        # threshold 0.95 -> cutoff = 95; conf 90 -> NEEDS_REVIEW
        assert _consensus_label("YES", 90, 0.95) == "NEEDS_REVIEW"
        assert _consensus_label("YES", 95, 0.95) == "AUTO_MATCH"


class TestSkillsMatrix:
    """Tests for skills extraction in evaluation and triage parsing."""

    def test_evaluation_json_with_skills(self):
        text = json.dumps({
            "verdict": "YES",
            "confidence": 85,
            "reasoning": "Strong match",
            "skills_extracted": True,
            "matching_skills": ["Python", "AWS", "Docker"],
            "missing_skills": ["Kubernetes"],
        })
        result = _parse_evaluation_json(text)
        assert result["skills_extracted"] is True
        assert result["matching_skills"] == ["Python", "AWS", "Docker"]
        assert result["missing_skills"] == ["Kubernetes"]

    def test_evaluation_json_no_skills(self):
        text = json.dumps({
            "verdict": "MAYBE",
            "confidence": 60,
            "reasoning": "Brief outreach",
            "skills_extracted": False,
            "matching_skills": [],
            "missing_skills": [],
        })
        result = _parse_evaluation_json(text)
        assert result["skills_extracted"] is False
        assert result["matching_skills"] == []
        assert result["missing_skills"] == []

    def test_evaluation_json_missing_skills_fields_default(self):
        text = json.dumps({
            "verdict": "YES",
            "confidence": 80,
            "reasoning": "Good",
        })
        result = _parse_evaluation_json(text)
        assert result["skills_extracted"] is False
        assert result["matching_skills"] == []
        assert result["missing_skills"] == []

    def test_evaluation_json_invalid_skills_types_default(self):
        text = json.dumps({
            "verdict": "YES",
            "confidence": 80,
            "reasoning": "Good",
            "skills_extracted": True,
            "matching_skills": "not a list",
            "missing_skills": 42,
        })
        result = _parse_evaluation_json(text)
        assert result["matching_skills"] == []
        assert result["missing_skills"] == []

    def test_fallback_has_skills_fields(self):
        result = _parse_evaluation_json("garbage")
        assert "skills_extracted" in result
        assert "matching_skills" in result
        assert "missing_skills" in result

    def _parse(self, text):
        return _parse_triage_response(
            text,
            email_text="raw",
            email_links=[],
            classification="JOB_DIGEST",
            source="linkedin",
            model="gemma3:4b",
            tokens=100,
            latency_ms=500,
        )

    def test_single_model_skills_parsed(self):
        text = """LISTING:
title: Backend Engineer
company: CloudCo
location: Remote
salary: $180k
verdict: YES
confidence: 85
reason: Strong match
skills_extracted: true
matching_skills: Python, AWS, Docker
missing_skills: Kubernetes
links: none
---"""
        listings = self._parse(text)
        assert listings[0].skills_extracted is True
        assert json.loads(listings[0].matching_skills) == ["Python", "AWS", "Docker"]
        assert json.loads(listings[0].missing_skills) == ["Kubernetes"]

    def test_single_model_no_skills(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: MAYBE
confidence: 60
reason: Brief outreach
skills_extracted: false
matching_skills: none
missing_skills: none
links: none
---"""
        listings = self._parse(text)
        assert listings[0].skills_extracted is False
        assert listings[0].matching_skills == ""
        assert listings[0].missing_skills == ""

    def test_single_model_skills_default_when_missing(self):
        text = """LISTING:
title: Engineer
company: Co
location: Remote
salary: not listed
verdict: YES
confidence: 80
reason: Match
links: none
---"""
        listings = self._parse(text)
        assert listings[0].skills_extracted is False
        assert listings[0].matching_skills == ""
        assert listings[0].missing_skills == ""


class TestRecruiterOverride:
    """Tests for the recruiter outreach scoring floor."""

    def _parse_recruiter(self, text):
        return _parse_triage_response(
            text,
            email_text="raw",
            email_links=[],
            classification="RECRUITER_OUTREACH",
            source="recruiter",
            model="gemma3:4b",
            tokens=100,
            latency_ms=500,
        )

    def _parse_digest(self, text):
        return _parse_triage_response(
            text,
            email_text="raw",
            email_links=[],
            classification="JOB_DIGEST",
            source="linkedin",
            model="gemma3:4b",
            tokens=100,
            latency_ms=500,
        )

    def test_recruiter_no_upgraded_to_maybe(self):
        text = """LISTING:
title: Staff Engineer
company: Startup Inc
location: Austin
salary: not listed
verdict: NO
confidence: 30
reason: Location mismatch
links: none
---"""
        listings = self._parse_recruiter(text)
        assert listings[0].verdict == "MAYBE"
        assert "[System Override:" in listings[0].reason
        assert "direct recruiter outreach" in listings[0].reason

    def test_recruiter_yes_not_downgraded(self):
        text = """LISTING:
title: Backend Engineer
company: BigCo
location: Remote
salary: $200k
verdict: YES
confidence: 90
reason: Perfect match
links: none
---"""
        listings = self._parse_recruiter(text)
        assert listings[0].verdict == "YES"
        assert "[System Override:" not in listings[0].reason

    def test_recruiter_maybe_unchanged(self):
        text = """LISTING:
title: Engineer
company: Co
location: NYC
salary: not listed
verdict: MAYBE
confidence: 50
reason: Partial match
links: none
---"""
        listings = self._parse_recruiter(text)
        assert listings[0].verdict == "MAYBE"
        assert "[System Override:" not in listings[0].reason

    def test_digest_no_stays_no(self):
        text = """LISTING:
title: Junior Dev
company: SmallCo
location: Austin
salary: not listed
verdict: NO
confidence: 20
reason: Wrong level
links: none
---"""
        listings = self._parse_digest(text)
        assert listings[0].verdict == "NO"
        assert "[System Override:" not in listings[0].reason

    def test_recruiter_override_saves_as_triaged(self):
        text = """LISTING:
title: Engineer
company: Co
location: Austin
salary: not listed
verdict: NO
confidence: 25
reason: Location mismatch
links: none
---"""
        listings = self._parse_recruiter(text)
        assert listings[0].final_status == "triaged"

    def test_recruiter_override_in_model_scores(self):
        text = """LISTING:
title: Engineer
company: Co
location: Austin
salary: not listed
verdict: NO
confidence: 30
reason: Location mismatch
links: none
---"""
        listings = self._parse_recruiter(text)
        scores = json.loads(listings[0].model_scores)
        assert scores[0]["verdict"] == "MAYBE"


# ---------------------------------------------------------------------------
# Scrape validity judge
# ---------------------------------------------------------------------------


class TestEvaluateScrapeValidity:
    """Unit tests for the Judge LLM function."""

    @patch("src.triage._call_openrouter")
    def test_valid_job_description(self, mock_ollama):
        mock_ollama.return_value = {
            "text": '{"is_valid": true, "company_name": "Acme Corp", '
                    '"job_title": "Backend Engineer", "reason": "Complete job description"}',
            "tokens": 50,
        }
        result = evaluate_scrape_validity(
            "We are hiring a Backend Engineer at Acme Corp...",
            "http://localhost:11434", "gemma3:4b",
        )
        assert result["is_valid"] is True
        assert result["company_name"] == "Acme Corp"
        assert result["job_title"] == "Backend Engineer"

    @patch("src.triage._call_openrouter")
    def test_cloudflare_page_rejected(self, mock_ollama):
        mock_ollama.return_value = {
            "text": '{"is_valid": false, "company_name": "", '
                    '"job_title": "", "reason": "Cloudflare challenge page"}',
            "tokens": 30,
        }
        result = evaluate_scrape_validity(
            "Checking your browser before accessing... Enable JavaScript",
            "http://localhost:11434", "gemma3:4b",
        )
        assert result["is_valid"] is False

    @patch("src.triage._call_openrouter")
    def test_unparseable_response_returns_invalid(self, mock_ollama):
        mock_ollama.return_value = {"text": "not json at all", "tokens": 10}
        result = evaluate_scrape_validity(
            "some text", "http://localhost:11434", "gemma3:4b",
        )
        assert result["is_valid"] is False
        assert "unparseable" in result["reason"]

    @patch("src.triage._call_openrouter")
    def test_partial_extraction_from_wrapper(self, mock_ollama):
        mock_ollama.return_value = {
            "text": '{"is_valid": false, "company_name": "Google", '
                    '"job_title": "Software Engineer", '
                    '"reason": "Share link wrapper, no actual description"}',
            "tokens": 40,
        }
        result = evaluate_scrape_validity(
            "Share this job: Google Software Engineer - Click to view",
            "http://localhost:11434", "gemma3:4b",
        )
        assert result["is_valid"] is False
        assert result["company_name"] == "Google"
        assert result["job_title"] == "Software Engineer"


class TestIsAggregatorUrl:
    def test_indeed_blocked(self):
        assert _is_aggregator_url("https://www.indeed.com/viewjob?jk=abc") is True

    def test_glassdoor_blocked(self):
        assert _is_aggregator_url("https://glassdoor.com/job/123") is True

    def test_linkedin_blocked(self):
        assert _is_aggregator_url("https://www.linkedin.com/jobs/view/123") is True

    def test_direct_ats_allowed(self):
        assert _is_aggregator_url("https://jobs.lever.co/acme/abc-123") is False

    def test_company_careers_allowed(self):
        assert _is_aggregator_url("https://careers.google.com/jobs/123") is False

    def test_subdomain_aggregator_blocked(self):
        assert _is_aggregator_url("https://uk.indeed.com/viewjob?jk=abc") is True


class TestValidateAndHealIntegration:
    """Integration tests for the Stage 3 Scrape + Heal loop.

    Stage 3 runs per-anchor after Stage 1 extraction. It uses ANCHORED
    company/title/location for DDGS searches — never judge guesses from
    invalid content (the anti-hallucination guarantee).
    """

    # Reusable extraction response blocks for mocking Stage 1 _call_ollama
    _ACME_EXTRACTION = {
        "text": (
            "LISTING:\ntitle: Backend Engineer\ncompany: Acme Corp\n"
            "location: Remote\nsalary: $150k-$200k\n"
            "job_summary: Acme builds infrastructure. Core responsibility is backend.\n"
            "description: Backend engineering role requiring Python skills.\n"
            "links: https://jobs.lever.co/acme/be-123\n---\n"
        ),
        "tokens": 100,
    }
    _EVAL_YES = {
        "text": (
            '{"verdict":"YES","confidence":85,"reasoning":"Strong match",'
            '"skills_extracted":false,"matching_skills":[],"missing_skills":[]}'
        ),
        "tokens": 50,
    }

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._scrape_url")
    @patch("src.triage._search_duckduckgo_results")
    @patch("src.triage.evaluate_scrape_validity")
    def test_judge_rejects_then_heals_via_search(
        self, mock_judge, mock_search, mock_scrape, mock_sleep, mock_escalate,
    ):
        """Stage 3 direct scrape invalid → DDGS with ANCHORED query → batch judge picks winner."""
        healed_text = (
            "Acme Corp — Backend Engineer\n"
            "We are looking for a backend engineer. Requirements: 3+ years Python. Remote. $150k."
        )
        # Direct scrape fails the judge; DDGS path uses batch _call_openrouter instead
        mock_judge.side_effect = [
            {"is_valid": False, "company_name": "wrong", "job_title": "wrong", "reason": "Auth wall"},
        ]
        mock_search.return_value = [{"href": "https://jobs.lever.co/acme/be-456", "title": "Backend Eng", "body": "Python role"}]
        mock_scrape.return_value = healed_text

        from src.triage import TriageSession
        with patch("src.triage._call_openrouter") as mock_ollama:
            # Stage 1 extraction, batch judge selects DDGS winner, Stage 5 evaluation
            mock_ollama.side_effect = [
                self._ACME_EXTRACTION,
                {"text": json.dumps({"winner_url": "https://jobs.lever.co/acme/be-456", "reason": "Full JD"}), "tokens": 40},
                self._EVAL_YES,
            ]

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="Click here to share this job at Acme Corp",
                email_links=["https://share.google.com/acme-be"],
                classification="JOB_DIGEST",
                source="google_alerts",
            )

        assert len(listings) == 1
        assert listings[0].title == "Backend Engineer"
        assert listings[0].company == "Acme Corp"

        # DDGS query built from ANCHORED data (company + title + location)
        # — never from judge's extracted values, which were "wrong" above
        mock_search.assert_called_once()
        assert mock_search.call_args[0][0] == "Acme Corp Backend Engineer Remote"

        # No escalation needed
        mock_escalate.assert_not_called()

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._scrape_url")
    @patch("src.triage._search_duckduckgo_results")
    def test_all_scrapes_fail_listing_dropped(
        self, mock_search, mock_scrape, mock_sleep, mock_escalate,
    ):
        """All DDGS scrapes return empty → batch judge never called → listing silently dropped."""
        mock_search.return_value = [
            {"href": "https://mystco.com/careers/ds-1"},
            {"href": "https://mystco.com/careers/ds-2"},
        ]
        mock_scrape.return_value = ""  # every scrape returns no usable content

        stage1 = {
            "text": (
                "LISTING:\ntitle: Data Scientist\ncompany: MystCo\n"
                "location: Remote\nsalary: not listed\n"
                "job_summary: MystCo is a data company. Core role is ML.\n"
                "description: Data science role building ML pipelines.\n"
                "links: https://redirect.example.com/mystco\n---\n"
            ),
            "tokens": 80,
        }

        from src.triage import TriageSession
        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.side_effect = [stage1]

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="Visit MystCo to view this job",
                email_links=["https://redirect.example.com/mystco"],
                classification="JOB_DIGEST",
                source="linkedin",
            )

        # No synthesis fallback — listing is dropped when all scrapes fail
        assert len(listings) == 0
        mock_escalate.assert_not_called()

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._scrape_url")
    @patch("src.triage._search_duckduckgo_results")
    def test_no_search_results_listing_dropped(
        self, mock_search, mock_scrape, mock_sleep, mock_escalate,
    ):
        """DDGS returns empty → no candidate URLs → listing silently dropped (no synthesis)."""
        mock_search.return_value = []
        mock_scrape.return_value = ""

        stage1 = {
            "text": (
                "LISTING:\ntitle: Engineer\ncompany: Stealth Co\n"
                "location: Remote\nsalary: not listed\n"
                "job_summary: Stealth Co builds software. Core role is engineering.\n"
                "description: Engineering role at a stealth startup.\n"
                "links: https://stealth.co/apply\n---\n"
            ),
            "tokens": 60,
        }

        from src.triage import TriageSession
        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.side_effect = [stage1]

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="Verify you are human",
                email_links=["https://stealth.co/apply"],
                classification="JOB_DIGEST",
                source="google_alerts",
            )

        assert len(listings) == 0
        mock_escalate.assert_not_called()

    def test_recruiter_no_url_uses_source_text_without_judge(self):
        """RECRUITER_OUTREACH anchor with no URL → source text used directly, no judge/DDGS."""
        stage1 = {
            "text": (
                "LISTING:\ntitle: Engineer\ncompany: GoodCo\n"
                "location: Remote\nsalary: not listed\n"
                "job_summary: GoodCo builds software. Core role is engineering.\n"
                "description: Engineering role with no job URL.\n"
                "links: none\n---\n"
            ),
            "tokens": 80,
        }
        eval_resp = {
            "text": (
                '{"verdict":"YES","confidence":90,"reasoning":"Great match",'
                '"skills_extracted":false,"matching_skills":[],"missing_skills":[]}'
            ),
            "tokens": 40,
        }

        from src.triage import TriageSession

        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.side_effect = [stage1, eval_resp]

            with patch("src.triage.evaluate_scrape_validity") as mock_judge:
                with patch("src.triage._search_duckduckgo_results") as mock_search:
                    session = TriageSession(profile_llm_context="test profile")
                    session._client = MagicMock()

                    listings = session.triage_email(
                        email_text="GoodCo is hiring an Engineer...",
                        email_links=[],
                        classification="RECRUITER_OUTREACH",
                        source="recruiter",
                    )

        # Recruiter outreach + no URL → source text used directly; judge and DDGS never called
        mock_judge.assert_not_called()
        mock_search.assert_not_called()

        assert len(listings) == 1
        assert listings[0].company == "GoodCo"

    def test_stage2_hard_stop_on_garbage_input(self):
        """Stage 1 finds no listings in garbage input → return [] with failure reason, no escalation."""
        from src.triage import TriageSession

        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.return_value = {"text": "NO_LISTINGS_FOUND", "tokens": 10}

            with patch("src.triage._post_escalation") as mock_escalate:
                session = TriageSession(profile_llm_context="test profile")
                session._client = MagicMock()

                listings = session.triage_email(
                    email_text="???!!!@@@",
                    email_links=["https://broken.example.com"],
                    classification="JOB_DIGEST",
                    source="google_alerts",
                )

        # Garbage input → Stage 1 finds nothing → return [] without escalation
        assert listings == []
        mock_escalate.assert_not_called()

    def test_stage2_hard_stop_missing_location(self):
        """Stage 2 drops anchors missing location → last_failure_reason set, no escalation."""
        stage1 = {
            "text": (
                "LISTING:\ntitle: Engineer\ncompany: SomeCo\n"
                "location: not specified\nsalary: not listed\n"
                "job_summary: SomeCo builds things.\n"
                "description: Engineering role.\nlinks: none\n---\n"
            ),
            "tokens": 50,
        }

        from src.triage import TriageSession

        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.return_value = stage1

            with patch("src.triage._post_escalation") as mock_escalate:
                session = TriageSession(profile_llm_context="test profile")
                session._client = MagicMock()

                listings = session.triage_email(
                    email_text="SomeCo is hiring",
                    email_links=[],
                    classification="JOB_DIGEST",
                    source="linkedin",
                )

        assert listings == []
        assert session.last_failure_reason == "stage2_missing_required_fields"
        mock_escalate.assert_not_called()

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._search_duckduckgo_results")
    def test_no_url_digest_triggers_ddgs(self, mock_search, mock_sleep, mock_escalate):
        """JOB_DIGEST with only aggregator URLs → DDGS called with anchored query, not source text."""
        stage1_response = {
            "text": (
                "LISTING:\ntitle: ML Engineer\ncompany: Stripe\n"
                "location: Remote\nsalary: not listed\n"
                "job_summary: Stripe builds payments. Core is ML.\n"
                "description: ML engineering role at Stripe.\n"
                "links: https://www.linkedin.com/comm/feed/?lipi=tracker123\n---\n"
            ),
            "tokens": 80,
        }
        # DDGS finds a result but scraping fails → listing dropped (no synthesis fallback)
        mock_search.return_value = [{"href": "https://stripe.com/jobs/ml-engineer"}]

        from src.triage import TriageSession

        with patch("src.triage._call_openrouter") as mock_ollama, \
             patch("src.triage._scrape_url") as mock_scrape:
            mock_ollama.side_effect = [stage1_response]
            mock_scrape.return_value = ""

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="Stripe is hiring ML engineers",
                email_links=["https://www.linkedin.com/comm/feed/?lipi=tracker123"],
                classification="JOB_DIGEST",
                source="linkedin",
            )

        # DDGS was called (not source-text fallback) — anchored query is the key check
        mock_search.assert_called()
        first_query = mock_search.call_args_list[0][0][0]
        assert "Stripe" in first_query
        assert "ML Engineer" in first_query
        # Scrape failed → listing dropped (no synthesis fallback)
        assert len(listings) == 0

    @patch("src.triage._post_escalation")
    @patch("src.triage._search_duckduckgo_results")
    def test_no_url_recruiter_uses_source_text(self, mock_search, mock_escalate):
        """RECRUITER_OUTREACH anchor with no URL → Stage 3 uses source text, DDGS NOT called."""
        stage1_response = {
            "text": (
                "LISTING:\ntitle: Backend Engineer\ncompany: Acme Corp\n"
                "location: Remote\nsalary: $150k\n"
                "job_summary: Acme builds infrastructure. Core is backend.\n"
                "description: Backend engineering role.\nlinks: none\n---\n"
            ),
            "tokens": 80,
        }
        eval_response = {
            "text": (
                '{"verdict":"MAYBE","confidence":65,"reasoning":"Recruiter outreach match",'
                '"skills_extracted":false,"matching_skills":[],"missing_skills":[]}'
            ),
            "tokens": 40,
        }

        from src.triage import TriageSession

        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.side_effect = [stage1_response, eval_response]

            session = TriageSession(
                profile_llm_context="test profile",
                confidence_threshold=0.5,
            )
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="Hi, I'm reaching out about a role at Acme Corp.",
                email_links=[],
                classification="RECRUITER_OUTREACH",
                source="recruiter",
            )

        # DDGS was NOT called
        mock_search.assert_not_called()
        # Got one listing (source text was used as job description)
        assert len(listings) == 1
        assert listings[0].verdict == "MAYBE"

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._scrape_url")
    @patch("src.triage._search_duckduckgo_results")
    def test_google_alert_source_board_uses_site_query(
        self, mock_search, mock_scrape, mock_sleep, mock_escalate,
    ):
        """Google Jobs email with 'via Talent.com' → DDGS query uses site:talent.com."""
        healed_text = (
            "Harnham — AI Engineer\n"
            "Looking for an AI engineer. Requirements: Python, ML. Oakland, CA."
        )
        # Use a non-aggregator URL so _ddgs_heal candidate_urls filter passes
        mock_search.return_value = [{"href": "https://www.harnham.com/jobs/ai-engineer", "title": "AI Eng", "body": "Python ML role"}]
        mock_scrape.return_value = healed_text

        stage1_response = {
            "text": (
                "LISTING:\ntitle: AI Engineer\ncompany: Harnham\n"
                "location: Oakland, CA\nsalary: not listed\n"
                "job_summary: Harnham is a data recruiter. Core is AI engineering.\n"
                "description: AI engineering role.\n"
                "source_board: talent.com\nlinks: none\n---\n"
            ),
            "tokens": 80,
        }
        eval_response = {
            "text": (
                '{"verdict":"YES","confidence":82,"reasoning":"Strong AI match",'
                '"skills_extracted":true,"matching_skills":["Python","ML"],"missing_skills":[]}'
            ),
            "tokens": 50,
        }

        from src.triage import TriageSession
        with patch("src.triage._call_openrouter") as mock_ollama:
            # Stage 1 extraction, batch judge picks winner, Stage 5 evaluation
            mock_ollama.side_effect = [
                stage1_response,
                {"text": json.dumps({"winner_url": "https://www.harnham.com/jobs/ai-engineer", "reason": "Valid JD"}), "tokens": 40},
                eval_response,
            ]

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text=(
                    "AI Engineer\nHarnham\nOakland, CA, United States\nvia Talent.com"
                ),
                email_links=[],
                classification="GOOGLE_ALERT",
                source="google_alerts",
            )

        assert len(listings) == 1
        # DDGS query must use site: operator, not bare location
        mock_search.assert_called_once()
        assert mock_search.call_args[0][0] == "Harnham AI Engineer (site:talent.com)"
        mock_escalate.assert_not_called()

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._scrape_url")
    @patch("src.triage._search_duckduckgo_results")
    def test_ddgs_heal_drops_listing_when_all_scrapes_empty(
        self, mock_ddgs, mock_scrape, mock_sleep, mock_escalate,
    ):
        """DDGS finds URLs but all scrapes return empty → batch judge never called → dropped."""
        mock_ddgs.return_value = [
            {"href": "https://www.lever.co/acme/job1", "title": "ML Eng at Acme",
             "body": "Acme builds ML infrastructure."},
            {"href": "https://www.greenhouse.io/acme/job2", "title": "Acme Corp",
             "body": "Senior ML engineers wanted."},
        ]
        mock_scrape.return_value = ""

        stage1_response = {
            "text": (
                "LISTING:\ntitle: ML Engineer\ncompany: Acme Corp\n"
                "location: Remote\nsalary: not listed\n"
                "job_summary: Acme builds ML infra.\n"
                "description: ML engineering role.\n"
                "source_board: none\nlinks: none\n---\n"
            ),
            "tokens": 80,
        }

        from src.triage import TriageSession
        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.side_effect = [stage1_response]

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="ML Engineer job at Acme Corp.",
                email_links=[],
                classification="JOB_DIGEST",
                source="google_alerts",
            )

        assert len(listings) == 0
        mock_escalate.assert_not_called()
        mock_sleep.assert_called_once()  # rate-limit guard fires before search

    @patch("src.triage._post_escalation")
    @patch("src.triage.time.sleep")
    @patch("src.triage._scrape_url")
    @patch("src.triage._search_duckduckgo_results")
    def test_no_ddgs_results_listing_dropped(
        self, mock_ddgs, mock_scrape, mock_sleep, mock_escalate,
    ):
        """DDGS returns nothing → no candidate URLs → listing silently dropped."""
        mock_ddgs.return_value = []
        mock_scrape.return_value = ""

        stage1_response = {
            "text": (
                "LISTING:\ntitle: Staff Engineer\ncompany: NoData Corp\n"
                "location: Remote\nsalary: not listed\n"
                "job_summary: NoData Corp.\ndescription: Engineering role.\n"
                "source_board: none\nlinks: none\n---\n"
            ),
            "tokens": 80,
        }

        from src.triage import TriageSession
        with patch("src.triage._call_openrouter") as mock_ollama:
            mock_ollama.side_effect = [stage1_response]

            session = TriageSession(profile_llm_context="test profile")
            session._client = MagicMock()

            listings = session.triage_email(
                email_text="Staff Engineer at NoData Corp.",
                email_links=[],
                classification="JOB_DIGEST",
                source="google_alerts",
            )

        assert len(listings) == 0
        mock_escalate.assert_not_called()
