"""Tests for the simplified profile loader."""

from pathlib import Path

import pytest

from src.profile_loader import load_profile

EXAMPLE_PROFILE = Path("my_profile_example/profile.md")


@pytest.fixture
def profile():
    return load_profile(EXAMPLE_PROFILE)


def test_extracts_name(profile):
    assert profile["name"] == "Jane Doe"


def test_llm_context_has_content(profile):
    assert len(profile["llm_context"]) > 100


def test_llm_context_includes_who_i_am(profile):
    assert "Who I am" in profile["llm_context"]


def test_llm_context_includes_skills(profile):
    assert "My skills" in profile["llm_context"] or "skills" in profile["llm_context"].lower()


def test_llm_context_includes_what_looking_for(profile):
    assert (
        "What I'm looking for" in profile["llm_context"]
        or "looking for" in profile["llm_context"].lower()
    )


def test_llm_context_excludes_pipeline_settings(profile):
    # The heading and its content should be stripped; the phrase may appear
    # in the introductory blockquote but not as a ## heading or table content
    assert "## Pipeline Settings" not in profile["llm_context"]
    assert "max_listings_per_run | 200" not in profile["llm_context"]


def test_llm_context_excludes_job_alert_config(profile):
    assert "## Job Alert Configuration" not in profile["llm_context"]


def test_settings_parsed(profile):
    settings = profile["settings"]
    assert settings.get("max_listings_per_run") == 200
    assert settings.get("dedup_window_days") == 30


def test_home_location_parsed(profile):
    settings = profile["settings"]
    assert settings.get("home_location") == "Oakland, CA"


def test_missing_profile_raises():
    with pytest.raises(FileNotFoundError):
        load_profile(Path("nonexistent/profile.md"))
