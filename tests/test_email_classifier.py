"""Tests for the email classifier module."""

import email

from src.email_classifier import (
    GOOGLE_ALERT,
    JOB_DIGEST,
    RECRUITER_OUTREACH,
    SKIP_JUNK,
    SKIP_UNCLASSIFIED,
    classify_email,
)


def _make_msg(sender: str, subject: str, body: str = "") -> email.message.Message:
    """Build a minimal email.message.Message for classification testing."""
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    if body:
        msg.set_content(body, subtype="plain")
    return msg


class TestGoogleAlerts:
    def test_google_alerts_sender(self):
        msg = _make_msg("googlealerts-noreply@google.com", "Google Alert - staff engineer")
        assert classify_email(msg) == GOOGLE_ALERT

    def test_google_alerts_any_subject(self):
        msg = _make_msg("googlealerts-noreply@google.com", "random subject line")
        assert classify_email(msg) == GOOGLE_ALERT


class TestJobDigest:
    def test_linkedin_job_alert_sender(self):
        msg = _make_msg("jobs-noreply@linkedin.com", "Your job alert for Python")
        assert classify_email(msg) == JOB_DIGEST

    def test_linkedin_jobalerts_sender(self):
        msg = _make_msg("jobalerts-noreply@linkedin.com", "5 new jobs for you")
        assert classify_email(msg) == JOB_DIGEST

    def test_indeed_sender(self):
        msg = _make_msg("jobalert@indeed.com", "New jobs matching your search")
        assert classify_email(msg) == JOB_DIGEST

    def test_glassdoor_sender(self):
        msg = _make_msg("noreply@glassdoor.com", "Jobs that match your profile")
        assert classify_email(msg) == JOB_DIGEST

    def test_subject_pattern_job_alert(self):
        msg = _make_msg("unknown@someboardsite.com", "Job Alert: Backend Engineer")
        assert classify_email(msg) == JOB_DIGEST

    def test_subject_pattern_new_jobs(self):
        msg = _make_msg("alerts@randomboard.com", "12 new jobs for you this week")
        assert classify_email(msg) == JOB_DIGEST


class TestRecruiterOutreach:
    def test_linkedin_inmail_sender_with_outreach_subject(self):
        msg = _make_msg(
            "inmail-hit-reply@linkedin.com",
            "Sarah sent you a message",
        )
        assert classify_email(msg) == RECRUITER_OUTREACH

    def test_linkedin_notification_with_outreach_subject(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "New opportunity at Acme Corp",
        )
        assert classify_email(msg) == RECRUITER_OUTREACH

    def test_corporate_sender_with_outreach_subject(self):
        msg = _make_msg(
            "recruiter@techstartup.com",
            "Senior Engineer role at TechStartup",
        )
        assert classify_email(msg) == RECRUITER_OUTREACH

    def test_linkedin_notification_body_match(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "You have a new message",
            body="Hi Jane, I noticed your background in Python and distributed systems.",
        )
        assert classify_email(msg) == RECRUITER_OUTREACH


class TestSkip:
    def test_skip_sender(self):
        msg = _make_msg("noreply@google.com", "Security alert for your account")
        assert classify_email(msg) == SKIP_JUNK

    def test_linkedin_social_viewed_profile(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "5 people viewed your profile this week",
        )
        assert classify_email(msg) == SKIP_JUNK

    def test_linkedin_social_connection_request(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "You have a new connection request",
        )
        assert classify_email(msg) == SKIP_JUNK

    def test_linkedin_social_endorsed(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "Someone endorsed you for Python",
        )
        assert classify_email(msg) == SKIP_JUNK

    def test_linkedin_social_birthday(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "Wish John a happy birthday!",
        )
        assert classify_email(msg) == SKIP_JUNK

    def test_unclassified_defaults_to_skip(self):
        msg = _make_msg("random@example.com", "Meeting tomorrow at 3pm")
        assert classify_email(msg) == SKIP_UNCLASSIFIED

    def test_gmail_sender_no_outreach_subject(self):
        msg = _make_msg("friend@gmail.com", "Hey, how are you?")
        assert classify_email(msg) == SKIP_UNCLASSIFIED


class TestAllowlistGate:
    """With an EmailConfig, only allowlisted senders may classify as alerts."""

    @staticmethod
    def _config():
        from src.email_config import EmailConfig, SenderGroup
        return EmailConfig(
            senders=[
                SenderGroup(
                    source="linkedin",
                    addresses=["jobs-noreply@linkedin.com"],
                    tier="friendly",
                ),
                SenderGroup(
                    source="google_alerts",
                    addresses=["googlealerts-noreply@google.com"],
                ),
            ],
            subject_hints=["job alert"],
        )

    def test_allowlisted_digest_sender_passes(self):
        msg = _make_msg("jobs-noreply@linkedin.com", "Your job alert for Python")
        assert classify_email(msg, config=self._config()) == JOB_DIGEST

    def test_allowlisted_google_alert_passes(self):
        msg = _make_msg("googlealerts-noreply@google.com", "Google Alert - ML engineer")
        assert classify_email(msg, config=self._config()) == GOOGLE_ALERT

    def test_digest_shaped_unknown_sender_is_unclassified(self):
        """Subject looks like a digest, but sender not allowlisted → candidate,
        not processed."""
        msg = _make_msg("alerts@newjobboard.com", "12 new jobs for you this week")
        assert classify_email(msg, config=self._config()) == SKIP_UNCLASSIFIED

    def test_non_allowlisted_indeed_sender_is_unclassified(self):
        """Sender in built-in constants but NOT in this config's allowlist."""
        msg = _make_msg("jobalert@indeed.com", "New jobs matching your search")
        assert classify_email(msg, config=self._config()) == SKIP_UNCLASSIFIED

    def test_recruiter_outreach_unaffected_by_config(self):
        msg = _make_msg("recruiter@techstartup.com", "Senior Engineer role at TechStartup")
        assert classify_email(msg, config=self._config()) == RECRUITER_OUTREACH

    def test_junk_unaffected_by_config(self):
        msg = _make_msg("noreply@google.com", "Security alert for your account")
        assert classify_email(msg, config=self._config()) == SKIP_JUNK

    def test_without_config_subject_pattern_still_promotes(self):
        """Legacy behavior preserved when no config is passed."""
        msg = _make_msg("alerts@newjobboard.com", "12 new jobs for you this week")
        assert classify_email(msg) == JOB_DIGEST

    def test_yaml_subject_hint_flags_allowlist_candidate(self, caplog):
        """A custom subject_hints entry marks unknown-sender mail as
        digest-shaped (allowlist candidate) without processing it."""
        import logging

        config = self._config()
        config.subject_hints = ["roles roundup"]
        msg = _make_msg("digest@nichejobsite.com", "Your weekly Roles Roundup")
        with caplog.at_level(logging.INFO, logger="src.email_classifier"):
            assert classify_email(msg, config=config) == SKIP_UNCLASSIFIED
        assert "allowlist candidate" in caplog.text


class TestRealWorldAlertFormats:
    """The four live alert formats observed in-inbox (2026-07), classified
    against the committed template allowlist — synthetic subjects/names."""

    @staticmethod
    def _template_config():
        from src.email_config import EXAMPLE_EMAIL_CONFIG_PATH, load_email_config

        return load_email_config(EXAMPLE_EMAIL_CONFIG_PATH)

    def test_linkedin_job_picks_sender(self):
        msg = _make_msg("LinkedIn <jobs-listings@linkedin.com>", "Top job picks for you")
        assert classify_email(msg, config=self._template_config()) == JOB_DIGEST

    def test_google_jobs_alert_query_subject(self):
        msg = _make_msg(
            "Job Alerts from Google <notify-noreply@google.com>",
            '("Solutions Architect") AND ("AI") near Springfield',
        )
        assert classify_email(msg, config=self._template_config()) == GOOGLE_ALERT

    def test_glassdoor_jobs_for_name(self):
        msg = _make_msg("Glassdoor Jobs <noreply@glassdoor.com>", "Jobs for Alex")
        assert classify_email(msg, config=self._template_config()) == JOB_DIGEST

    def test_indeed_jobalert_subdomain_sender(self):
        msg = _make_msg(
            "Indeed <donotreply@jobalert.indeed.com>",
            '13 new title:("AI") jobs in Springfield, IL',
        )
        assert classify_email(msg, config=self._template_config()) == JOB_DIGEST
