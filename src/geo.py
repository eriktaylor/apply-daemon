"""Geo-distance calculator using OpenStreetMap Nominatim.

Resolves the user's home_location from profile settings on first use,
then calculates distances from home for job listing locations.

Uses functools.lru_cache to avoid repeated geocode API calls for
common location strings (Nominatim limits to 1 request/second).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from math import atan2, cos, radians, sin, sqrt

from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim

from src.profile_loader import load_profile

logger = logging.getLogger(__name__)

_geolocator = Nominatim(user_agent="apply-pilot/0.1", timeout=5)

# Home coordinates — resolved lazily on first get_distance() call
_home_coords: tuple[float, float] | None = None
_home_initialized = False


def _init_home() -> None:
    """Resolve home coordinates from profile settings. Called once lazily."""
    global _home_coords, _home_initialized
    if _home_initialized:
        return
    _home_initialized = True

    try:
        profile = load_profile()
        home_location = profile["settings"].get("home_location", "")
    except Exception:
        logger.warning("Could not load profile for home_location", exc_info=True)
        return

    if not home_location:
        logger.warning("home_location not set in profile settings")
        return

    try:
        result = _geolocator.geocode(home_location)
        if result:
            _home_coords = (result.latitude, result.longitude)
            logger.info("Home location resolved: %s → (%.4f, %.4f)", home_location, *_home_coords)
        else:
            logger.warning("Could not geocode home_location: %s", home_location)
    except (GeocoderTimedOut, GeocoderServiceError, Exception):
        logger.warning("Geocoding failed for home_location: %s", home_location, exc_info=True)


def _haversine(coord1: tuple[float, float], coord2: tuple[float, float]) -> float:
    """Calculate distance in miles between two (lat, lon) coordinates."""
    lat1, lon1 = radians(coord1[0]), radians(coord1[1])
    lat2, lon2 = radians(coord2[0]), radians(coord2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    earth_radius_miles = 3959
    return earth_radius_miles * c


@lru_cache(maxsize=256)
def _geocode_location(location_string: str) -> tuple[float, float] | None:
    """Geocode a location string. Cached to avoid repeated API calls."""
    try:
        result = _geolocator.geocode(location_string)
        if result:
            return (result.latitude, result.longitude)
    except (GeocoderTimedOut, GeocoderServiceError, Exception):
        logger.debug("Geocoding failed for: %s", location_string, exc_info=True)
    return None


def get_distance(location_string: str) -> str:
    """Calculate distance from home to a job location.

    Returns:
        "Remote" if location contains "remote".
        "14 miles" if geocoding succeeds for both home and job.
        "Distance unknown" if geocoding fails.
    """
    if not location_string:
        return "Distance unknown"

    if "remote" in location_string.lower():
        return "Remote"

    _init_home()

    if _home_coords is None:
        return "Distance unknown"

    job_coords = _geocode_location(location_string.strip())
    if job_coords is None:
        return "Distance unknown"

    miles = _haversine(_home_coords, job_coords)
    return f"{int(round(miles))} miles"
