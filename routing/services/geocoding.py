"""Resolve a free-text start/finish into coordinates.

Resolution order (cheapest first):

1. ``"lat,lon"`` literal            → parse, zero network.
2. ``"City, ST"`` / ``"City, State"`` → local gazetteer, zero network.
3. Anything else                    → Nominatim (cached 30 days). Counts as an
                                      external call.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

import requests
from django.conf import settings
from django.core.cache import cache

from stations.gazetteer import get_gazetteer
from stations.normalize import in_conus, normalize_city, normalize_state

from ..errors import GeocodingUnavailable, InvalidRequest, LocationNotFound, OutsideUSA
from .http import get_session

log = logging.getLogger(__name__)

Source = Literal["coords", "gazetteer", "nominatim"]

_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_TRAILING_COUNTRY = re.compile(r",?\s*(usa|u\.s\.a\.|us|united states(?: of america)?)\s*$", re.IGNORECASE)
_NEGATIVE_CACHE_TTL = 3600
_NOT_FOUND = "__not_found__"

# Nominatim is queried with ``countrycodes=us``, which makes it return *some* US match for anything
# ("Toronto, ON" → a street in Indianapolis). Inputs that name a Canadian province or a non-US
# country are therefore rejected locally before any network call.
_CANADIAN_PROVINCES: dict[str, str] = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}  # fmt: skip
_PROVINCE_BY_NAME: dict[str, str] = {name.casefold(): code for code, name in _CANADIAN_PROVINCES.items()}
_PROVINCE_BY_NAME["québec"] = "QC"
_PROVINCE_BY_NAME["newfoundland"] = "NL"
_NON_US_COUNTRY = re.compile(r",?\s*(canada|mexico|m[ée]xico)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    query: str
    lat: float
    lon: float
    source: Source
    external_calls: int


class NominatimClient:
    """Thin wrapper over Nominatim's search endpoint (usage policy: real UA, ≤1 req/s)."""

    def __init__(self, base_url: str, user_agent: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.timeout = timeout

    def _get(self, params: dict[str, str]) -> tuple[float, float] | None:
        params = {"format": "jsonv2", "limit": "1", "countrycodes": "us", **params}
        response = get_session().get(
            f"{self.base_url}/search", params=params, headers=self.headers, timeout=self.timeout
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])

    def search_city(self, city: str, state: str) -> tuple[float, float] | None:
        return self._get({"city": city, "state": state})

    def search_free_text(self, text: str) -> tuple[float, float] | None:
        return self._get({"q": text})


def default_nominatim_client() -> NominatimClient:
    return NominatimClient(
        settings.NOMINATIM_BASE_URL, settings.NOMINATIM_USER_AGENT, settings.NOMINATIM_TIMEOUT_SECONDS
    )


def parse_coordinates(text: str) -> tuple[float, float] | None:
    match = _COORDS.match(text)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidRequest(f"Coordinates out of range: {text!r}")
    return lat, lon


def parse_city_state(text: str) -> tuple[str, str] | None:
    """``"New York, NY"`` → ``("New York", "NY")``; returns None if not in that shape."""
    cleaned = _TRAILING_COUNTRY.sub("", text.strip())
    if "," not in cleaned:
        return None
    city, _, state = cleaned.rpartition(",")
    code = normalize_state(state)
    city = city.strip()
    if code is None or not city:
        return None
    return city, code


def non_us_region(text: str) -> str | None:
    """``"Toronto, ON"`` → ``"Ontario, Canada"``; ``"Tijuana, Mexico"`` → ``"Mexico"``; else None.

    Only explicit region/country suffixes are recognised; a bare city name is left to the
    geocoder, whose result is still checked against the contiguous-US box.
    """
    cleaned = text.strip()
    country = _NON_US_COUNTRY.search(cleaned)
    if country:
        return country.group(1).capitalize()
    if "," not in cleaned:
        return None
    region = cleaned.rpartition(",")[2].strip()
    upper = region.upper()
    code = upper if upper in _CANADIAN_PROVINCES else _PROVINCE_BY_NAME.get(region.casefold())
    if code is None:
        return None
    return f"{_CANADIAN_PROVINCES[code]}, Canada"


def unknown_state_code(text: str) -> str | None:
    """``"Nowhere, ZZ"`` → ``"ZZ"`` when the trailing token looks like a state code but is not one.

    Nominatim (with ``countrycodes=us``) would otherwise force-match such input to some US place.
    Longer trailing words ("Times Square, Manhattan") are left for the geocoder.
    """
    cleaned = _TRAILING_COUNTRY.sub("", text.strip())
    if "," not in cleaned:
        return None
    region = cleaned.rpartition(",")[2].strip()
    if re.fullmatch(r"[A-Za-z]{2}", region) and normalize_state(region) is None:
        return region.upper()
    return None


def _nominatim_cache_key(text: str) -> str:
    digest = hashlib.sha1(text.casefold().encode("utf-8")).hexdigest()
    return f"nominatim:{digest}"


def _resolve_via_nominatim(text: str, client: NominatimClient) -> tuple[tuple[float, float] | None, int]:
    """Returns ((lat, lon) or None, external_calls_made)."""
    key = _nominatim_cache_key(text)
    cached = cache.get(key)
    if cached == _NOT_FOUND:
        return None, 0
    if cached is not None:
        return (cached[0], cached[1]), 0

    started = time.perf_counter()
    try:
        result = client.search_free_text(text)
    except requests.Timeout as exc:
        log.warning("nominatim timeout for %r", text)
        raise GeocodingUnavailable("Geocoding service timed out.") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log.warning("nominatim HTTP %s for %r (check NOMINATIM_USER_AGENT if 403)", status, text)
        raise GeocodingUnavailable(f"Geocoding service rejected the request (HTTP {status}).") from exc
    except requests.RequestException as exc:
        log.warning("nominatim request failed for %r: %s", text, exc)
        raise GeocodingUnavailable("Geocoding service is unavailable.") from exc
    log.info(
        "nominatim query=%r hit=%s ms=%.0f", text, result is not None, (time.perf_counter() - started) * 1000
    )

    if result is None:
        cache.set(key, _NOT_FOUND, _NEGATIVE_CACHE_TTL)
    else:
        cache.set(key, list(result), settings.NOMINATIM_CACHE_TTL)
    return result, 1


def resolve_location(text: str, client: NominatimClient | None = None) -> ResolvedLocation:
    """Resolve user input to a point inside the contiguous USA.

    Raises ``InvalidRequest``, ``LocationNotFound``, ``OutsideUSA`` or
    ``GeocodingUnavailable``.
    """
    query = text.strip()
    if not query:
        raise InvalidRequest("Location must not be empty.")

    coords = parse_coordinates(query)
    if coords is not None:
        lat, lon = coords
        source: Source = "coords"
        calls = 0
    else:
        city_state = parse_city_state(query)
        place = get_gazetteer().lookup(*city_state) if city_state else None
        if place is not None:
            lat, lon, source, calls = place.lat, place.lon, "gazetteer", 0
        else:
            region = non_us_region(query)
            if region is not None:
                raise OutsideUSA(f"{query!r} is in {region}, outside the contiguous USA.")
            code = unknown_state_code(query)
            if code is not None:
                raise LocationNotFound(f"{code!r} is not a US state code; use 'City, ST' or 'City, State'.")
            result, calls = _resolve_via_nominatim(query, client or default_nominatim_client())
            if result is None:
                raise LocationNotFound(f"Could not find a US location for {query!r}.")
            lat, lon = result
            source = "nominatim"

    if not in_conus(lat, lon):
        raise OutsideUSA(f"{query!r} resolves to ({lat:.4f}, {lon:.4f}), outside the contiguous USA.")
    return ResolvedLocation(query=query, lat=lat, lon=lon, source=source, external_calls=calls)


def normalize_query(text: str) -> str:
    """Canonical form used for cache keys: casefold, collapse whitespace, strip noise."""
    city_state = parse_city_state(text)
    if city_state:
        return f"{normalize_city(city_state[0])}, {city_state[1].lower()}"
    text = re.sub(r"[^\w,.\-\s]", "", text.strip().casefold())
    return re.sub(r"\s+", " ", text)
