import json
from pathlib import Path
from unittest.mock import patch

from src.cli import _ASSET_VERB_NAMES, main
from src.tailor import ASSET_SPECS


class TestAssetVerbWiring:
    """Every asset verb must exist as a spec and vice versa. A verb with no
    spec KeyErrors at dispatch; a spec with no verb is reachable only from
    Slack, which is the metered path this tranche exists to avoid."""

    def test_every_verb_maps_to_a_spec(self):
        for asset in _ASSET_VERB_NAMES.values():
            assert asset in ASSET_SPECS

    def test_every_spec_has_a_verb(self):
        assert set(_ASSET_VERB_NAMES.values()) == set(ASSET_SPECS)

    def test_wire_names_use_hyphens_keys_use_underscores(self):
        """The wire vocabulary is hyphenated; the asset keys match
        generate_assets (profile.md / GENERATE_ASSETS), not a second set."""
        assert _ASSET_VERB_NAMES["cover-letter"] == "cover_letter"
        assert _ASSET_VERB_NAMES["interview-prep"] == "interview_prep"


class TestAssetRouting:
    """The default route must never reach the network. That is the whole
    point: these four assets are the expensive half of a listing."""

    def _row(self):
        return {"id": "abc123", "title": "SWE", "company": "Acme"}

    def test_default_route_emits_a_prompt_without_calling_openrouter(self, capsys):
        with patch("src.cli.Database") as db, \
             patch("src.tailor.build_asset_prompt", return_value=("PROMPT", {})), \
             patch("src.tailor._call_openrouter") as call:
            db.return_value.get_listing_by_id.return_value = self._row()
            rc = main(["polish", "abc123", "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["route"] == "in_session" and out["prompt"] == "PROMPT"
        call.assert_not_called()

    def test_via_api_is_opt_in(self, capsys):
        with patch("src.cli.Database") as db, \
             patch("src.tailor.generate_asset_via_api",
                   return_value=(Path("output/x"), {})) as gen:
            db.return_value.get_listing_by_id.return_value = self._row()
            rc = main(["polish", "abc123", "--via", "api", "--json"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["route"] == "api"
        gen.assert_called_once()

    def test_apply_writes_without_calling_openrouter(self, capsys, tmp_path):
        payload = tmp_path / "resp.json"
        payload.write_text(json.dumps({"clean_cover_letter_text": "Dear ..."}))
        with patch("src.cli.Database") as db, \
             patch("src.tailor.parse_asset_response", return_value={"k": "v"}), \
             patch("src.tailor.write_asset", return_value=Path("output/x")) as w, \
             patch("src.tailor._call_openrouter") as call:
            db.return_value.get_listing_by_id.return_value = self._row()
            rc = main(["cover-letter", "abc123", "--apply", str(payload), "--json"])
        assert rc == 0
        call.assert_not_called()
        w.assert_called_once()

    def test_answers_requires_questions(self, capsys):
        with patch("src.cli.Database") as db:
            db.return_value.get_listing_by_id.return_value = self._row()
            rc = main(["answers", "abc123", "--json"])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["error"] == "questions_required"

    def test_unknown_listing_is_reported_not_raised(self, capsys):
        with patch("src.cli.Database") as db:
            db.return_value.get_listing_by_id.return_value = None
            rc = main(["polish", "nope", "--json"])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["error"] == "not_found"

    def test_polish_without_a_prior_tailor_is_a_stated_error(self, capsys):
        with patch("src.cli.Database") as db, \
             patch("src.tailor.build_asset_prompt",
                   side_effect=RuntimeError("No tailor assets found for abc123.")):
            db.return_value.get_listing_by_id.return_value = self._row()
            rc = main(["polish", "abc123", "--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "unavailable" and "tailor assets" in out["detail"]
