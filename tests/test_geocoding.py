import pytest
import requests

from routing.errors import GeocodingUnavailable, InvalidRequest, LocationNotFound, OutsideUSA
from routing.services.geocoding import (
    NominatimClient,
    non_us_region,
    normalize_query,
    parse_city_state,
    parse_coordinates,
    resolve_location,
)

NOMINATIM = "https://nominatim.test"


@pytest.fixture
def client() -> NominatimClient:
    return NominatimClient(NOMINATIM, user_agent="tests/1.0", timeout=1.0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("40.7128,-74.0060", (40.7128, -74.006)),
        (" 40.7128 , -74.0060 ", (40.7128, -74.006)),
        ("41,-87", (41.0, -87.0)),
        ("New York, NY", None),
        ("", None),
    ],
)
def test_parse_coordinates(text, expected):
    assert parse_coordinates(text) == expected


def test_parse_coordinates_out_of_range():
    with pytest.raises(InvalidRequest):
        parse_coordinates("95,-74")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("New York, NY", ("New York", "NY")),
        ("new york,ny", ("new york", "NY")),
        ("Chicago, Illinois", ("Chicago", "IL")),
        ("Chicago, IL, USA", ("Chicago", "IL")),
        ("Washington, DC", ("Washington", "DC")),
        ("Toronto, ON", None),
        ("123 Main St", None),
        ("Springfield", None),
    ],
)
def test_parse_city_state(text, expected):
    assert parse_city_state(text) == expected


def test_resolve_coordinates_makes_no_calls(client, requests_mock):
    loc = resolve_location("40.7128,-74.0060", client)
    assert (loc.lat, loc.lon, loc.source, loc.external_calls) == (40.7128, -74.006, "coords", 0)
    assert requests_mock.call_count == 0


def test_resolve_city_state_uses_gazetteer_with_no_network(client, requests_mock):
    loc = resolve_location("Chicago, IL", client)
    assert loc.source == "gazetteer" and loc.external_calls == 0
    assert abs(loc.lat - 41.84) < 0.1 and abs(loc.lon + 87.68) < 0.1
    assert requests_mock.call_count == 0


def test_resolve_falls_back_to_nominatim_and_caches(client, requests_mock):
    m = requests_mock.get(
        f"{NOMINATIM}/search", json=[{"lat": "40.7580", "lon": "-73.9855", "display_name": "Times Square"}]
    )
    first = resolve_location("Times Square, Manhattan", client)
    assert (first.source, first.external_calls) == ("nominatim", 1)
    assert first.lat == pytest.approx(40.758)
    assert m.last_request.qs["countrycodes"] == ["us"]
    assert m.last_request.qs["q"] == ["times square, manhattan"]
    assert m.last_request.headers["User-Agent"] == "tests/1.0"

    second = resolve_location("Times Square, Manhattan", client)
    assert (second.source, second.external_calls) == ("nominatim", 0)
    assert m.call_count == 1


def test_resolve_unknown_location_is_404_and_negative_cached(client, requests_mock):
    m = requests_mock.get(f"{NOMINATIM}/search", json=[])
    with pytest.raises(LocationNotFound):
        resolve_location("Nowhere Imaginary Plaza", client)
    with pytest.raises(LocationNotFound):
        resolve_location("Nowhere Imaginary Plaza", client)
    assert m.call_count == 1


def test_resolve_outside_usa_is_rejected(client, requests_mock):
    with pytest.raises(OutsideUSA):
        resolve_location("61.2181,-149.9003", client)  # Anchorage
    requests_mock.get(f"{NOMINATIM}/search", json=[{"lat": "21.3069", "lon": "-157.8583"}])
    with pytest.raises(OutsideUSA):
        resolve_location("Honolulu Harbor", client)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Toronto, ON", "Ontario, Canada"),
        ("Vancouver, British Columbia", "British Columbia, Canada"),
        ("Montréal, Québec", "Quebec, Canada"),
        ("Montreal, Canada", "Canada"),
        ("Tijuana, Mexico", "Mexico"),
        ("Ontario, CA", None),  # Ontario, California is a US city
        ("Chicago, IL", None),
        ("Times Square, Manhattan", None),
        ("Toronto", None),
    ],
)
def test_non_us_region(text, expected):
    assert non_us_region(text) == expected


def test_unknown_two_letter_state_is_404_without_network(client, requests_mock):
    """Nominatim would force-match "Nowhere, ZZ" to some US place; reject it locally instead."""
    requests_mock.get(f"{NOMINATIM}/search", json=[{"lat": "35.0", "lon": "-90.0"}])
    with pytest.raises(LocationNotFound, match="'ZZ' is not a US state code"):
        resolve_location("Nowhere, ZZ", client)
    assert requests_mock.call_count == 0
    # Longer trailing words are still free text for the geocoder.
    assert resolve_location("Times Square, Manhattan", client).source == "nominatim"
    assert requests_mock.call_count == 1


def test_canadian_province_is_rejected_without_network(client, requests_mock):
    """With ``countrycodes=us`` Nominatim would force-match "Toronto, ON" to some US street, so the
    province suffix must be rejected locally and never reach the geocoder."""
    requests_mock.get(f"{NOMINATIM}/search", json=[{"lat": "39.9062", "lon": "-86.2237"}])
    with pytest.raises(OutsideUSA, match="Ontario, Canada"):
        resolve_location("Toronto, ON", client)
    with pytest.raises(OutsideUSA, match="Mexico"):
        resolve_location("Tijuana, Mexico", client)
    assert requests_mock.call_count == 0


def test_resolve_empty_is_invalid(client):
    with pytest.raises(InvalidRequest):
        resolve_location("   ", client)


def test_nominatim_failure_maps_to_502(client, requests_mock):
    requests_mock.get(f"{NOMINATIM}/search", exc=requests.exceptions.ConnectTimeout)
    with pytest.raises(GeocodingUnavailable):
        resolve_location("Some Street Address", client)


def test_normalize_query():
    assert normalize_query("  New   York ,  ny ") == "new york, ny"
    assert normalize_query("St. Louis, Missouri") == "saint louis, mo"
    assert normalize_query("40.7128,-74.0060") == "40.7128,-74.0060"
    assert normalize_query("Times  Square!!") == "times square"
