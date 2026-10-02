import pytest
import requests
from requests_mock import ANY

from routing.errors import NoRouteError, RoutingTimeout, RoutingUnavailable
from routing.services.osrm import OsrmProvider, default_routing_provider
from tests.conftest import osrm_body

BASE = "https://osrm.test"
START = (40.7128, -74.0060)  # lat, lon
FINISH = (41.8781, -87.6298)


@pytest.fixture
def provider() -> OsrmProvider:
    return OsrmProvider(BASE, timeout=1.0, session=requests.Session())


def test_route_url_uses_lon_lat_order(provider):
    assert (
        provider.route_url(START, FINISH)
        == f"{BASE}/route/v1/driving/-74.006000,40.712800;-87.629800,41.878100"
    )


def test_get_route_parses_and_converts_units(provider, requests_mock):
    m = requests_mock.get(
        f"{BASE}/route/v1/driving/-74.006000,40.712800;-87.629800,41.878100",
        json=osrm_body(1609.344 * 790.4, 748 * 60, [[-74.0, 40.7], [-80.0, 41.0], [-87.6, 41.9]]),
    )
    result = provider.get_route(START, FINISH)
    assert result.distance_miles == pytest.approx(790.4)
    assert result.duration_minutes == pytest.approx(748.0)
    assert result.coordinates.shape == (3, 2)
    assert result.external_calls == 1
    assert m.call_count == 1
    assert m.last_request.qs == {
        "overview": ["full"],
        "geometries": ["geojson"],
        "steps": ["false"],
        "alternatives": ["false"],
    }


def test_no_route_maps_to_error_and_is_not_retried(provider, requests_mock):
    m = requests_mock.get(
        ANY, status_code=400, json={"code": "NoRoute", "message": "Impossible route between points"}
    )
    with pytest.raises(NoRouteError, match="Impossible route"):
        provider.get_route(START, FINISH)
    assert m.call_count == 1


def test_ok_without_routes_is_unavailable(provider, requests_mock):
    requests_mock.get(ANY, json={"code": "Ok", "routes": []})
    with pytest.raises(RoutingUnavailable):
        provider.get_route(START, FINISH)


def test_timeout_is_retried_once_then_succeeds(provider, requests_mock):
    m = requests_mock.get(
        ANY,
        [
            {"exc": requests.exceptions.ConnectTimeout},
            {"json": osrm_body(1609.344, 60, [[-74.0, 40.7], [-74.1, 40.8]])},
        ],
    )
    result = provider.get_route(START, FINISH)
    assert result.external_calls == 2
    assert m.call_count == 2


def test_timeout_twice_maps_to_504(provider, requests_mock):
    m = requests_mock.get(ANY, exc=requests.exceptions.ReadTimeout)
    with pytest.raises(RoutingTimeout):
        provider.get_route(START, FINISH)
    assert m.call_count == 2


def test_server_error_is_retried_once_then_maps_to_502(provider, requests_mock):
    m = requests_mock.get(ANY, status_code=503, text="bad gateway")
    with pytest.raises(RoutingUnavailable):
        provider.get_route(START, FINISH)
    assert m.call_count == 2


def test_client_error_is_not_retried(provider, requests_mock):
    m = requests_mock.get(ANY, status_code=400, json={"code": "InvalidQuery", "message": "bad"})
    with pytest.raises(NoRouteError):
        provider.get_route(START, FINISH)
    assert m.call_count == 1


def test_invalid_json_is_unavailable(provider, requests_mock):
    requests_mock.get(ANY, text="<html>not json</html>")
    with pytest.raises(RoutingUnavailable):
        provider.get_route(START, FINISH)


def test_default_provider_reads_settings(settings):
    settings.OSRM_BASE_URL = "https://my-osrm.example/"
    settings.OSRM_TIMEOUT_SECONDS = 3.0
    provider = default_routing_provider()
    assert provider.base_url == "https://my-osrm.example"
    assert provider.timeout == 3.0
