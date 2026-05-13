"""Tests for the geo-distance module."""

from unittest.mock import MagicMock, patch

import src.geo as geo_module
from src.geo import _haversine, get_distance


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine((37.8, -122.27), (37.8, -122.27)) == 0.0

    def test_known_distance(self):
        # Oakland to SF is roughly 10-12 miles
        dist = _haversine((37.8044, -122.2712), (37.7749, -122.4194))
        assert 7 < dist < 15

    def test_long_distance(self):
        # Oakland to NYC is roughly 2500 miles
        dist = _haversine((37.8044, -122.2712), (40.7128, -74.0060))
        assert 2400 < dist < 2700


class TestGetDistance:
    def test_remote_returns_immediately(self):
        assert get_distance("Remote (US)") == "Remote"
        assert get_distance("remote") == "Remote"
        assert get_distance("San Francisco, CA (Remote)") == "Remote"

    def test_empty_string(self):
        assert get_distance("") == "Distance unknown"

    @patch.object(geo_module, "_home_coords", (37.8044, -122.2712))
    @patch.object(geo_module, "_home_initialized", True)
    @patch("src.geo._geocode_location")
    def test_known_location(self, mock_geocode):
        mock_geocode.return_value = (37.7749, -122.4194)
        result = get_distance("San Francisco, CA")
        assert "miles" in result
        miles = int(result.split()[0])
        assert 7 < miles < 15

    @patch.object(geo_module, "_home_coords", (37.8044, -122.2712))
    @patch.object(geo_module, "_home_initialized", True)
    @patch("src.geo._geocode_location")
    def test_geocode_failure_returns_unknown(self, mock_geocode):
        mock_geocode.return_value = None
        assert get_distance("Nowhere, XX") == "Distance unknown"

    @patch.object(geo_module, "_home_coords", None)
    @patch.object(geo_module, "_home_initialized", True)
    def test_no_home_coords_returns_unknown(self):
        assert get_distance("San Francisco, CA") == "Distance unknown"


class TestInitHome:
    @patch("src.geo._geolocator")
    @patch("src.geo.load_profile")
    def test_successful_init(self, mock_load, mock_geolocator):
        geo_module._home_initialized = False
        geo_module._home_coords = None

        mock_load.return_value = {
            "settings": {"home_location": "Oakland, CA"},
            "name": "Test",
            "llm_context": "",
        }
        mock_result = MagicMock()
        mock_result.latitude = 37.8044
        mock_result.longitude = -122.2712
        mock_geolocator.geocode.return_value = mock_result

        geo_module._init_home()

        assert geo_module._home_coords == (37.8044, -122.2712)
        assert geo_module._home_initialized is True

        # Reset for other tests
        geo_module._home_initialized = False
        geo_module._home_coords = None

    @patch("src.geo._geolocator")
    @patch("src.geo.load_profile")
    def test_geocode_failure_defaults_to_none(self, mock_load, mock_geolocator):
        geo_module._home_initialized = False
        geo_module._home_coords = None

        mock_load.return_value = {
            "settings": {"home_location": "Nonexistent Place"},
            "name": "Test",
            "llm_context": "",
        }
        mock_geolocator.geocode.return_value = None

        geo_module._init_home()

        assert geo_module._home_coords is None
        assert geo_module._home_initialized is True

        # Reset
        geo_module._home_initialized = False
        geo_module._home_coords = None

    @patch("src.geo.load_profile")
    def test_missing_home_location(self, mock_load):
        geo_module._home_initialized = False
        geo_module._home_coords = None

        mock_load.return_value = {
            "settings": {},
            "name": "Test",
            "llm_context": "",
        }

        geo_module._init_home()

        assert geo_module._home_coords is None

        # Reset
        geo_module._home_initialized = False
        geo_module._home_coords = None
