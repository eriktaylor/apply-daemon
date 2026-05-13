"""Tests for the tailor module (response parsing and asset saving)."""

import json

import pytest

from src.tailor import _parse_tailor_response, _save_assets


class TestParseTailorResponse:
    def test_valid_json(self):
        text = json.dumps({
            "match_analysis": "Great fit for the role.",
            "custom_cover_letter": "Dear Hiring Manager...",
            "resume_bullet_edits": ["Edit 1", "Edit 2", "Edit 3"],
        })
        result = _parse_tailor_response(text)
        assert result["match_analysis"] == "Great fit for the role."
        assert result["custom_cover_letter"] == "Dear Hiring Manager..."
        assert len(result["resume_bullet_edits"]) == 3

    def test_new_diff_schema_parses(self):
        """Validate the nested diff-based schema parses correctly."""
        text = json.dumps({
            "match_analysis": "Strong backend match.",
            "clean_cover_letter_text": "Dear Hiring Manager,\n\nI am excited to apply.",
            "cover_letter_diff_summary": "- Added paragraph about their Series B\n- Mentioned Kubernetes migration",
            "resume_bullet_edits": [
                {
                    "original_bullet": "Developed REST APIs for user management",
                    "slack_diff": "~~Developed REST APIs~~ **Architected high-throughput REST APIs serving 50K rps**",
                    "clean_bullet": "Architected high-throughput REST APIs serving 50K rps for user management",
                },
                {
                    "original_bullet": "Built ETL pipelines",
                    "slack_diff": "~~Built ETL pipelines~~ **Designed real-time event processing pipelines with Kafka**",
                    "clean_bullet": "Designed real-time event processing pipelines with Kafka",
                },
            ],
        })
        result = _parse_tailor_response(text)
        assert result["match_analysis"] == "Strong backend match."
        assert result["clean_cover_letter_text"].startswith("Dear Hiring Manager")
        assert "Series B" in result["cover_letter_diff_summary"]
        assert len(result["resume_bullet_edits"]) == 2
        assert result["resume_bullet_edits"][0]["clean_bullet"].startswith("Architected")
        assert "~~Developed" in result["resume_bullet_edits"][0]["slack_diff"]

    def test_json_in_markdown_fences(self):
        inner = json.dumps({
            "match_analysis": "Good match.",
            "custom_cover_letter": "Dear team...",
            "resume_bullet_edits": ["Edit 1"],
        })
        text = f"```json\n{inner}\n```"
        result = _parse_tailor_response(text)
        assert result["match_analysis"] == "Good match."

    def test_missing_match_analysis_raises(self):
        text = json.dumps({
            "custom_cover_letter": "Letter",
            "resume_bullet_edits": ["edit 1"],
        })
        with pytest.raises(RuntimeError, match="match_analysis"):
            _parse_tailor_response(text)

    def test_bullet_edits_not_list_raises(self):
        text = json.dumps({
            "match_analysis": "Good",
            "custom_cover_letter": "Letter",
            "resume_bullet_edits": "not a list",
        })
        with pytest.raises(RuntimeError, match="resume_bullet_edits must be an array"):
            _parse_tailor_response(text)

    def test_invalid_json_raises(self):
        with pytest.raises(RuntimeError, match="Failed to parse"):
            _parse_tailor_response("this is not json")

    def test_new_resume_fields_parse(self):
        """executive_summary_rewrite and other_suggestions are optional and pass through."""
        text = json.dumps({
            "match_analysis": "Strong fit.",
            "executive_summary_rewrite": "Experienced AI engineer with proven track record.",
            "resume_bullet_edits": [
                {
                    "original_bullet": "Built APIs for user management",
                    "slack_diff": "~~Built~~ **Architected** APIs for user management",
                    "clean_bullet": "Architected APIs for user management",
                },
            ],
            "other_suggestions": "- Add a Skills section\n- Highlight ML projects",
        })
        result = _parse_tailor_response(text)
        assert result["executive_summary_rewrite"].startswith("Experienced")
        assert "Skills section" in result["other_suggestions"]
        assert len(result["resume_bullet_edits"]) == 1


class TestSaveAssets:
    def test_creates_output_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tailor.OUTPUT_DIR", tmp_path)
        listing = {"title": "ML Engineer", "company": "DataCo"}
        assets = {
            "match_analysis": "Strong match.",
            "custom_cover_letter": "Dear team...",
            "resume_bullet_edits": ["Edit section A", "Edit section B"],
        }
        output = _save_assets("abc12345-def", listing, assets)
        assert (output / "match_analysis.md").exists()
        assert (output / "cover_letter.md").exists()
        assert (output / "resume_edits.md").exists()
        assert (output / "assets.json").exists()

    def test_assets_json_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tailor.OUTPUT_DIR", tmp_path)
        listing = {"title": "Engineer", "company": "Co"}
        assets = {
            "match_analysis": "Good.",
            "custom_cover_letter": "Letter.",
            "resume_bullet_edits": ["Edit 1"],
        }
        output = _save_assets("xyz99999-ghi", listing, assets)
        saved = json.loads((output / "assets.json").read_text())
        assert saved["match_analysis"] == "Good."
        assert saved["resume_bullet_edits"] == ["Edit 1"]

    def test_directory_name_contains_slugs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tailor.OUTPUT_DIR", tmp_path)
        listing = {"title": "Staff Engineer", "company": "Big Corp"}
        assets = {
            "match_analysis": "x",
            "custom_cover_letter": "x",
            "resume_bullet_edits": [],
        }
        output = _save_assets("job123456-abc", listing, assets)
        assert "big_corp" in output.name
        assert "staff_engineer" in output.name
        assert "job12345" in output.name
