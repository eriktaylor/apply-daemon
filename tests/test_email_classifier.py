"""Tests for the email classifier module."""

import email

from src.email_classifier import (
    GOOGLE_ALERT,
    JOB_DIGEST,
    RECRUITER_OUTREACH,
    SKIP,
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
        assert classify_email(msg) == SKIP

    def test_linkedin_social_viewed_profile(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "5 people viewed your profile this week",
        )
        assert classify_email(msg) == SKIP

    def test_linkedin_social_connection_request(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "You have a new connection request",
        )
        assert classify_email(msg) == SKIP

    def test_linkedin_social_endorsed(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "Someone endorsed you for Python",
        )
        assert classify_email(msg) == SKIP

    def test_linkedin_social_birthday(self):
        msg = _make_msg(
            "notifications-noreply@linkedin.com",
            "Wish John a happy birthday!",
        )
        assert classify_email(msg) == SKIP

    def test_unclassified_defaults_to_skip(self):
        msg = _make_msg("random@example.com", "Meeting tomorrow at 3pm")
        assert classify_email(msg) == SKIP

    def test_gmail_sender_no_outreach_subject(self):
        msg = _make_msg("friend@gmail.com", "Hey, how are you?")
        assert classify_email(msg) == SKIP
