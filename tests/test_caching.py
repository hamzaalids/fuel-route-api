"""Caching, timings and meta behaviour of the planner."""

from __future__ import annotations

import logging
import re

import pytest
from django.core.cache import cache

from routing.services import http, planner
from routing.services.planner import cache_key, plan_route

OSRM_ROUTE = re.compile(r"https://router\.project-osrm\.org/route/v1/driving/.*")


@pytest.fixture(autouse=True)
def _fresh_session():
    http._session = None
    yield
    http._session = None


def test_cache_key_is_normalized_and_backend_safe():
    key = cache_key("  New   York , NY ", "chicago, ILLINOIS")
    assert key == cache_key("new york, ny", "Chicago, IL")
    assert key != cache_key("Chicago, IL", "New York, NY")  # direction matters
    assert re.fullmatch(r"route:[0-9a-f]{40}", key), "key must be memcached-safe (no spaces/control chars)"


def test_plan_is_cached_with_configured_ttl(requests_mock, osrm_nyc_chicago, settings, monkeypatch):
    settings.ROUTE_CACHE_TTL = 1234
    requests_mock.get(OSRM_ROUTE, json=osrm_nyc_chicago)
    recorded: dict = {}
    real_set = cache.set

    def spy_set(key, value, timeout=None, **kw):
        recorded[key] = timeout
        return real_set(key, value, timeout, **kw)

    monkeypatch.setattr(planner.cache, "set", spy_set)
    plan_route("New York, NY", "Chicago, IL")
    assert recorded == {cache_key("New York, NY", "Chicago, IL"): 1234}


def test_cache_hit_returns_same_plan_with_fresh_meta(requests_mock, osrm_nyc_chicago):
    m = requests_mock.get(OSRM_ROUTE, json=osrm_nyc_chicago)
    first = plan_route("New York, NY", "Chicago, IL")
    second = plan_route("New York, NY", "Chicago, IL")
    assert m.call_count == 1
    assert first["meta"]["cache_hit"] is False and second["meta"]["cache_hit"] is True
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["candidate_stations_in_corridor"] == first["meta"]["candidate_stations_in_corridor"]
    assert set(second["meta"]["timings_ms"]) == {"total"}
    assert second["fuel_stops"] == first["fuel_stops"]


def test_use_cache_false_bypasses_cache(requests_mock, osrm_nyc_chicago):
    m = requests_mock.get(OSRM_ROUTE, json=osrm_nyc_chicago)
    plan_route("New York, NY", "Chicago, IL", use_cache=False)
    plan_route("New York, NY", "Chicago, IL", use_cache=False)
    assert m.call_count == 2
    assert cache.get(cache_key("New York, NY", "Chicago, IL")) is None


def test_timings_cover_every_step(requests_mock, osrm_nyc_chicago):
    requests_mock.get(OSRM_ROUTE, json=osrm_nyc_chicago)
    timings = plan_route("New York, NY", "Chicago, IL")["meta"]["timings_ms"]
    assert set(timings) == {"geocode", "routing", "projection", "optimize", "build", "total"}
    assert all(isinstance(v, int) and v >= 0 for v in timings.values())
    assert timings["total"] >= max(v for k, v in timings.items() if k != "total")


def test_one_log_line_per_request_without_secrets(requests_mock, osrm_nyc_chicago, caplog, settings):
    settings.SECRET_KEY = "super-secret-value"
    requests_mock.get(OSRM_ROUTE, json=osrm_nyc_chicago)
    with caplog.at_level(logging.INFO, logger="routing.services.planner"):
        plan_route("New York, NY", "Chicago, IL")
        plan_route("New York, NY", "Chicago, IL")
    lines = [r.getMessage() for r in caplog.records if r.name == "routing.services.planner"]
    assert len(lines) == 2
    assert "cache_hit=False external_calls=1" in lines[0]
    assert "cache_hit=True external_calls=0" in lines[1]
    assert "super-secret-value" not in caplog.text


def test_default_cache_backend_is_locmem(settings):
    assert settings.CACHES["default"]["BACKEND"].endswith("LocMemCache")
    assert "LOCATION" not in settings.CACHES["default"]
