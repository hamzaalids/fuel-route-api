"""End-to-end API tests with OSRM mocked (recorded NYC → Chicago response)."""

from __future__ import annotations

import itertools
import re
from decimal import Decimal
from html import escape

import pytest
import requests
from requests_mock import ANY
from rest_framework.test import APIClient

from routing.services import http
from tests.conftest import osrm_body

OSRM_ROUTE = re.compile(r"https://router\.project-osrm\.org/route/v1/driving/.*")
NOMINATIM = re.compile(r"https://nominatim\.openstreetmap\.org/search.*")


@pytest.fixture(autouse=True)
def _fresh_session():
    """Make sure the shared requests.Session is created inside requests_mock's patch."""
    http._session = None
    yield
    http._session = None


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def mock_osrm(requests_mock, osrm_nyc_chicago):
    return requests_mock.get(OSRM_ROUTE, json=osrm_nyc_chicago)


def post_route(client: APIClient, start="New York, NY", finish="Chicago, IL"):
    return client.post("/api/v1/route/", {"start": start, "finish": finish}, format="json")


# --- happy path ---------------------------------------------------------------


def test_nyc_to_chicago_happy_path(client, mock_osrm):
    response = post_route(client)
    assert response.status_code == 200, response.content
    body = response.json()

    assert body["start"]["query"] == "New York, NY" and abs(body["start"]["lat"] - 40.66) < 0.1
    assert body["finish"]["query"] == "Chicago, IL"
    assert 780 < body["route"]["distance_miles"] < 810
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) > 1000  # ≈0.25-mile spacing
    assert body["vehicle"] == {"max_range_miles": 500, "mpg": 10, "tank_capacity_gallons": 50}

    stops = body["fuel_stops"]
    assert 1 <= len(stops) <= 4
    assert [s["order"] for s in stops] == list(range(1, len(stops) + 1))
    for stop in stops:
        assert stop["offset_from_route_miles"] <= 10
        assert stop["gallons_purchased"] > 0 and stop["cost"] > 0
        assert {"station_id", "name", "address", "city", "state", "lat", "lon", "price_per_gallon"} <= set(
            stop
        )

    summary = body["summary"]
    assert summary["number_of_stops"] == len(stops)
    assert summary["total_gallons"] == pytest.approx(body["route"]["distance_miles"] / 10, abs=0.002)
    total = sum(Decimal(str(s["cost"])) for s in stops) + Decimal(str(summary["origin_fill"]["cost"]))
    assert Decimal(str(summary["total_fuel_cost"])) == total
    assert 2.5 < summary["average_price_per_gallon"] < 5.0
    assert summary["origin_fill"]["price_basis"] in {"nearby", "state", "global"}

    # No leg between consecutive fuel points may exceed the range.
    points = [0.0, *[s["miles_from_start"] for s in stops], body["route"]["distance_miles"]]
    assert all(b - a <= 500.0 for a, b in itertools.pairwise(points))

    meta = body["meta"]
    assert meta["external_api_calls"] == 1
    assert meta["cache_hit"] is False
    assert meta["candidate_stations_in_corridor"] > 20
    assert set(meta["timings_ms"]) >= {"geocode", "routing", "projection", "optimize", "total"}
    assert meta["geocode_sources"] == {"start": "gazetteer", "finish": "gazetteer"}

    assert body["map"]["url"].startswith("/api/v1/route/map/?start=New+York")
    kinds = [f["properties"]["kind"] for f in body["map"]["geojson"]["features"]]
    assert kinds[:3] == ["route", "start", "finish"] and kinds.count("fuel_stop") == len(stops)
    assert mock_osrm.call_count == 1


def test_lat_lon_inputs_skip_geocoding(client, mock_osrm):
    response = post_route(client, "40.66271,-73.93868", "41.83705,-87.68494")
    assert response.status_code == 200
    assert response.json()["meta"]["geocode_sources"] == {"start": "coords", "finish": "coords"}
    assert response.json()["meta"]["external_api_calls"] == 1


def test_cache_hit_makes_zero_external_calls(client, mock_osrm):
    first = post_route(client).json()
    second = post_route(client, "new york , ny", "CHICAGO, Illinois").json()  # same after normalization
    assert second["meta"]["cache_hit"] is True
    assert second["meta"]["external_api_calls"] == 0
    assert second["fuel_stops"] == first["fuel_stops"]
    assert second["summary"] == first["summary"]
    assert mock_osrm.call_count == 1


def test_nominatim_fallback_counts_as_external_call(client, mock_osrm, requests_mock):
    requests_mock.get(NOMINATIM, json=[{"lat": "40.7580", "lon": "-73.9855"}])
    response = post_route(client, "Times Square Manhattan", "Chicago, IL")
    assert response.status_code == 200
    assert response.json()["meta"]["external_api_calls"] == 2
    assert response.json()["meta"]["geocode_sources"]["start"] == "nominatim"


# --- error rows from the API contract -----------------------------------------


def test_missing_field_is_400(client):
    response = client.post("/api/v1/route/", {"start": "New York, NY"}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "finish" in response.json()["error"]["message"]


def test_blank_and_too_long_fields_are_400(client):
    assert post_route(client, "", "Chicago, IL").status_code == 400
    assert post_route(client, "x" * 201, "Chicago, IL").status_code == 400


def test_same_start_and_finish_is_400(client):
    response = post_route(client, "Chicago, IL", "chicago,  il")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_get_variant_matches_post_and_shares_cache(client, mock_osrm):
    posted = post_route(client).json()
    got = client.get("/api/v1/route/", {"start": "New York, NY", "finish": "Chicago, IL"})
    assert got.status_code == 200
    body = got.json()
    assert body["summary"] == posted["summary"] and body["fuel_stops"] == posted["fuel_stops"]
    assert body["meta"]["cache_hit"] is True and mock_osrm.call_count == 1

    missing = client.get("/api/v1/route/", {"start": "New York, NY"})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "invalid_request"


def test_malformed_json_is_400(client):
    response = client.post("/api/v1/route/", "{not json", content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_location_is_404(client, requests_mock):
    nominatim = requests_mock.get(NOMINATIM, json=[])
    # Bogus state code: rejected locally, no geocoder call.
    response = post_route(client, "Nowhere Imaginary, ZZ", "Chicago, IL")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "location_not_found"
    assert nominatim.call_count == 0
    # Free text the geocoder cannot find.
    response = post_route(client, "Nowhere Imaginary Plaza", "Chicago, IL")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "location_not_found"
    assert nominatim.call_count == 1


def test_outside_usa_is_400(client):
    response = post_route(client, "61.2181,-149.9003", "Chicago, IL")  # Anchorage
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "outside_usa"


def test_canadian_city_is_400_without_external_calls(client, requests_mock):
    requests_mock.get(
        NOMINATIM, json=[{"lat": "39.9062", "lon": "-86.2237"}]
    )  # what Nominatim really returns
    response = post_route(client, "Toronto, ON", "Chicago, IL")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "outside_usa"
    assert "Ontario, Canada" in response.json()["error"]["message"]
    assert requests_mock.call_count == 0


def test_no_route_is_422(client, requests_mock):
    requests_mock.get(OSRM_ROUTE, status_code=400, json={"code": "NoRoute", "message": "Impossible route"})
    response = post_route(client)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_route"


def test_no_feasible_fuel_plan_is_422(client, requests_mock, settings):
    # A 900-mile straight line across the Pacific coast far offshore: no stations within the corridor.
    coords = [[-130.0, 40.0], [-130.0, 45.0], [-130.0, 49.0]]
    requests_mock.get(OSRM_ROUTE, json=osrm_body(900 * 1609.344, 3600 * 15, coords))
    response = post_route(client, "40.0,-125.0", "49.0,-125.0")
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "no_feasible_fuel_plan"
    assert "mile 0.0" in body["message"]


def test_osrm_server_error_is_502(client, requests_mock):
    requests_mock.get(OSRM_ROUTE, status_code=503)
    response = post_route(client)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "routing_unavailable"


def test_osrm_timeout_is_504(client, requests_mock):
    requests_mock.get(OSRM_ROUTE, exc=requests.exceptions.ReadTimeout)
    response = post_route(client)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "routing_unavailable"


def test_errors_are_not_cached(client, requests_mock, osrm_nyc_chicago):
    m = requests_mock.get(
        OSRM_ROUTE, [{"status_code": 503}, {"status_code": 503}, {"json": osrm_nyc_chicago}]
    )
    assert post_route(client).status_code == 502
    assert post_route(client).status_code == 200
    assert m.call_count == 3


# --- map page -------------------------------------------------------------------


def test_map_page_renders_route_and_stops(client, mock_osrm):
    plan = post_route(client).json()
    response = client.get(plan["map"]["url"])
    assert response.status_code == 200
    html = response.content.decode()
    assert "leaflet" in html and 'id="map"' in html
    assert "New York, NY" in html and "Chicago, IL" in html
    assert 'id="route-geojson"' in html
    for stop in plan["fuel_stops"]:
        assert escape(stop["name"]) in html
    assert mock_osrm.call_count == 1  # served from cache


def test_map_page_shows_errors(client):
    response = client.get("/api/v1/route/map/?start=Chicago, IL")
    assert response.status_code == 400 and "finish" in response.content.decode()
    response = client.get("/api/v1/route/map/?start=61.2,-149.9&finish=Chicago, IL")
    assert response.status_code == 400 and "outside_usa" in response.content.decode()


def test_unmocked_external_calls_are_impossible_in_tests(client, requests_mock):
    """Guard: the suite must never reach the real network."""
    requests_mock.get(ANY, exc=requests.exceptions.ConnectionError("blocked"))
    assert post_route(client).status_code == 502
