"""Routing provider: exactly ONE HTTP call per route.

``OsrmProvider`` talks to any OSRM-compatible server (the public demo by
default). ``RoutingProvider`` is the minimal protocol another provider would
implement to be swapped in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import requests
from django.conf import settings

from ..errors import NoRouteError, RoutingTimeout, RoutingUnavailable
from .http import get_session

log = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
_NO_ROUTE_CODES = {"NoRoute", "NoSegment"}


@dataclass(frozen=True, slots=True)
class RouteResult:
    distance_miles: float
    duration_minutes: float
    coordinates: np.ndarray  # (N, 2) GeoJSON order lon/lat
    external_calls: int


class RoutingProvider(Protocol):
    def get_route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        """``start``/``finish`` are ``(lat, lon)``. Raises ``NoRouteError`` / ``RoutingUnavailable``."""


class OsrmProvider:
    def __init__(self, base_url: str, timeout: float, session: requests.Session | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session

    def route_url(self, start: tuple[float, float], finish: tuple[float, float]) -> str:
        (lat1, lon1), (lat2, lon2) = start, finish
        return f"{self.base_url}/route/v1/driving/{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"

    def get_route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        params = {"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"}
        session = self._session or get_session()
        url = self.route_url(start, finish)

        attempts = 0
        response: requests.Response | None = None
        last_error: Exception | None = None
        while attempts < 2 and response is None:  # one retry, only on timeout / 5xx / connection error
            attempts += 1
            try:
                candidate = session.get(url, params=params, timeout=self.timeout)
            except requests.Timeout as exc:
                last_error = exc
                log.warning("OSRM timeout (attempt %d): %s", attempts, url)
                continue
            except requests.RequestException as exc:
                last_error = exc
                log.warning("OSRM connection error (attempt %d): %s", attempts, exc)
                continue
            if candidate.status_code >= 500:
                last_error = requests.HTTPError(f"OSRM returned {candidate.status_code}")
                log.warning("OSRM %d (attempt %d)", candidate.status_code, attempts)
                continue
            response = candidate

        if response is None:
            if isinstance(last_error, requests.Timeout):
                raise RoutingTimeout("Routing service timed out.") from last_error
            raise RoutingUnavailable("Routing service is unavailable.") from last_error

        return self._parse(response, attempts)

    @staticmethod
    def _parse(response: requests.Response, attempts: int) -> RouteResult:
        try:
            body = response.json()
        except ValueError as exc:
            raise RoutingUnavailable("Routing service returned an invalid response.") from exc

        code = body.get("code")
        if code in _NO_ROUTE_CODES or (code != "Ok" and response.status_code == 400 and code is not None):
            raise NoRouteError(body.get("message") or "No drivable route found between these points.")
        if code != "Ok" or not body.get("routes"):
            raise RoutingUnavailable(f"Routing service error: {code or response.status_code}")

        route = body["routes"][0]
        coords = np.asarray(route["geometry"]["coordinates"], dtype=np.float64)
        if coords.ndim != 2 or coords.shape[0] < 2:
            raise RoutingUnavailable("Routing service returned an empty geometry.")
        return RouteResult(
            distance_miles=float(route["distance"]) / METERS_PER_MILE,
            duration_minutes=float(route["duration"]) / 60.0,
            coordinates=coords,
            external_calls=attempts,
        )


def default_routing_provider() -> OsrmProvider:
    return OsrmProvider(settings.OSRM_BASE_URL, settings.OSRM_TIMEOUT_SECONDS)
