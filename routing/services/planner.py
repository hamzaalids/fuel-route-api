"""Orchestrates one route request end to end.

    1. validate input                      (serializer + here)
    2. cache lookup                        → hit: return immediately
    3. resolve start/finish → lat/lon      (local gazetteer; Nominatim fallback)
    4. ONE OSRM call                       → distance, duration, geometry
    5. resample route to ~1-mile spacing   (numpy, local)
    6. project stations onto the route     (numpy, local)
    7. run the fuel optimizer              (pure Python, local)
    8. build response + GeoJSON, cache it

Money is computed with ``Decimal`` and rounded to cents once, at output, so
``sum(stop.cost) + origin_fill.cost == total_fuel_cost`` holds exactly.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from stations.loader import StationIndex, get_station_index

from .. import errors
from .geocoding import NominatimClient, ResolvedLocation, normalize_query, resolve_location
from .geometry import SampledRoute, downsample_geometry, haversine_miles, project_stations, resample_route
from .optimizer import Candidate, FuelPlan, NoFeasiblePlan, estimate_origin_price, plan_fuel_stops
from .osrm import RouteResult, RoutingProvider, default_routing_provider

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
ORIGIN_PRICE_RADIUS_MILES = 50.0
RESAMPLE_SPACING_MILES = 1.0
GEOMETRY_OUTPUT_SPACING_MILES = 0.25


@dataclass
class _Timer:
    marks: dict[str, float] = field(default_factory=dict)
    _started: float = field(default_factory=time.perf_counter)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.marks[name] = self.marks.get(name, 0.0) + (time.perf_counter() - t0) * 1000

    def as_ms(self) -> dict[str, int]:
        out = {k: round(v) for k, v in self.marks.items()}
        out["total"] = round((time.perf_counter() - self._started) * 1000)
        return out


def cache_key(start: str, finish: str) -> str:
    """``route:<sha1 of "normalized_start|normalized_finish">`` (hashed so any cache backend accepts it)."""
    normalized = f"{normalize_query(start)}|{normalize_query(finish)}"
    return "route:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def plan_route(
    start: str,
    finish: str,
    *,
    routing: RoutingProvider | None = None,
    nominatim: NominatimClient | None = None,
    use_cache: bool = True,
) -> dict:
    """Return the full API payload for ``start`` → ``finish``. Raises ``routing.errors.ApiError``."""
    timer = _Timer()
    if normalize_query(start) == normalize_query(finish):
        raise errors.InvalidRequest("Start and finish must be different locations.")

    key = cache_key(start, finish)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            cached["meta"] = {
                **cached["meta"],
                "cache_hit": True,
                "external_api_calls": 0,
                "timings_ms": timer.as_ms(),
            }
            _log_request(start, finish, cached["meta"])
            return cached

    with timer.measure("geocode"):
        origin = resolve_location(start, nominatim)
        destination = resolve_location(finish, nominatim)
    if abs(origin.lat - destination.lat) < 1e-6 and abs(origin.lon - destination.lon) < 1e-6:
        raise errors.InvalidRequest("Start and finish resolve to the same location.")

    with timer.measure("routing"):
        route = (routing or default_routing_provider()).get_route(
            (origin.lat, origin.lon), (destination.lat, destination.lon)
        )

    index = get_station_index()
    with timer.measure("projection"):
        sampled = _resample_in_road_miles(route)
        projection = project_stations(sampled, index.lats, index.lons, settings.CORRIDOR_MILES)
        candidates = [
            Candidate(int(index.ids[i]), float(miles), float(index.prices[i]))
            for i, miles in zip(projection.station_indices, projection.miles_along, strict=True)
        ]
        origin_price, origin_basis = _origin_price(index, origin)

    with timer.measure("optimize"):
        try:
            plan = plan_fuel_stops(
                sampled.total_miles, candidates, origin_price, settings.MAX_RANGE_MILES, settings.MPG
            )
        except NoFeasiblePlan as exc:
            raise errors.NoFeasiblePlan(str(exc)) from exc

    with timer.measure("build"):
        payload = _build_payload(
            start,
            finish,
            origin,
            destination,
            route,
            sampled.total_miles,
            plan,
            origin_price,
            origin_basis,
            index,
            projection,
        )
    payload["meta"] = {
        "external_api_calls": origin.external_calls + destination.external_calls + route.external_calls,
        "cache_hit": False,
        "candidate_stations_in_corridor": len(candidates),
        "geocode_sources": {"start": origin.source, "finish": destination.source},
    }
    if use_cache:
        cache.set(key, payload, settings.ROUTE_CACHE_TTL)
    payload["meta"] = {**payload["meta"], "timings_ms": timer.as_ms()}
    _log_request(start, finish, payload["meta"])
    return payload


def _resample_in_road_miles(route: RouteResult) -> SampledRoute:
    """Resample to ~1-mile spacing, with cumulative miles scaled to OSRM's reported road distance.

    The polyline's chord length is slightly shorter than the road distance OSRM
    reports; scaling keeps station positions, legs and total gallons consistent
    with ``distance_miles``.
    """
    sampled = resample_route(route.coordinates, RESAMPLE_SPACING_MILES)
    if sampled.total_miles <= 0:
        raise errors.NoRouteError("Route geometry has zero length.")
    scale = route.distance_miles / sampled.total_miles
    return SampledRoute(points=sampled.points, miles=sampled.miles * scale)


def _origin_price(index: StationIndex, origin: ResolvedLocation) -> tuple[float, str]:
    """The start point is not a station: price it from the stations around it."""
    distances = haversine_miles(origin.lat, origin.lon, index.lats, index.lons)
    nearby = index.prices[distances <= ORIGIN_PRICE_RADIUS_MILES]
    nearest_state = str(index.states[int(np.argmin(distances))])
    state_prices = index.prices[index.states == nearest_state]
    return estimate_origin_price(nearby.tolist(), state_prices.tolist(), index.prices.tolist())


def _money(gallons: float, price: float) -> Decimal:
    return (Decimal(repr(gallons)) * Decimal(repr(price))).quantize(CENT, rounding=ROUND_HALF_UP)


def _build_payload(
    start: str,
    finish: str,
    origin: ResolvedLocation,
    destination: ResolvedLocation,
    route: RouteResult,
    total_miles: float,
    plan: FuelPlan,
    origin_price: float,
    origin_basis: str,
    index: StationIndex,
    projection,
) -> dict:
    offsets = dict(
        zip(index.ids[projection.station_indices].tolist(), projection.offset_miles.tolist(), strict=True)
    )
    row_by_id = {int(index.ids[i]): int(i) for i in projection.station_indices}

    stops = []
    stops_cost = Decimal("0")
    for order, purchase in enumerate(plan.stops, start=1):
        station = index.record(row_by_id[purchase.station_id])
        cost = _money(purchase.gallons, purchase.price)
        stops_cost += cost
        stops.append(
            {
                "order": order,
                "station_id": station["station_id"],
                "name": station["name"],
                "address": station["address"],
                "city": station["city"],
                "state": station["state"],
                "lat": station["lat"],
                "lon": station["lon"],
                "miles_from_start": round(purchase.miles, 1),
                "offset_from_route_miles": round(offsets[purchase.station_id], 1),
                "price_per_gallon": purchase.price,
                "gallons_purchased": round(purchase.gallons, 3),
                "cost": float(cost),
                "tank_gallons_on_arrival": round(purchase.tank_on_arrival, 3),
            }
        )

    origin_fill = plan.origin_fill
    origin_gallons = origin_fill.gallons if origin_fill else 0.0
    origin_cost = _money(origin_gallons, origin_price) if origin_fill else Decimal("0.00")
    total_cost = stops_cost + origin_cost
    total_gallons = plan.total_gallons
    distance_miles = round(total_miles, 2)

    geometry = {
        "type": "LineString",
        "coordinates": downsample_geometry(route.coordinates, GEOMETRY_OUTPUT_SPACING_MILES),
    }
    start_point = {"query": origin.query, "lat": origin.lat, "lon": origin.lon}
    finish_point = {"query": destination.query, "lat": destination.lat, "lon": destination.lon}
    summary = {
        "total_gallons": round(total_gallons, 3),
        "total_fuel_cost": float(total_cost),
        "average_price_per_gallon": round(float(total_cost) / total_gallons, 3) if total_gallons else 0.0,
        "number_of_stops": len(stops),
        "origin_fill": {
            "price_per_gallon": round(origin_price, 3),
            "gallons": round(origin_gallons, 3),
            "cost": float(origin_cost),
            "price_basis": origin_basis,
            "note": (
                "The start point is not a station in the dataset, so it is priced from nearby stations "
                f"({_BASIS_NOTES[origin_basis]}). The tank starts empty; this is the fuel bought before "
                "departure."
                + (
                    " A cheaper station sits at the start itself, so nothing is bought at this price."
                    if not origin_gallons
                    else ""
                )
            ),
        },
    }
    map_url = reverse("route-map") + "?" + urlencode({"start": start, "finish": finish})

    return {
        "start": start_point,
        "finish": finish_point,
        "route": {
            "distance_miles": distance_miles,
            "duration_minutes": round(route.duration_minutes),
            "geometry": geometry,
        },
        "vehicle": {
            "max_range_miles": settings.MAX_RANGE_MILES,
            "mpg": settings.MPG,
            "tank_capacity_gallons": settings.MAX_RANGE_MILES / settings.MPG,
        },
        "fuel_stops": stops,
        "summary": summary,
        "map": {"url": map_url, "geojson": _geojson(geometry, start_point, finish_point, stops)},
    }


_BASIS_NOTES = {
    "nearby": f"average of the 5 cheapest within {ORIGIN_PRICE_RADIUS_MILES:g} miles",
    "state": "average price in the start state",
    "global": "median price across all stations",
}


def _geojson(geometry: dict, start: dict, finish: dict, stops: list[dict]) -> dict:
    def point(lon: float, lat: float, props: dict) -> dict:
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        }

    features = [
        {"type": "Feature", "geometry": geometry, "properties": {"kind": "route"}},
        point(start["lon"], start["lat"], {"kind": "start", "label": start["query"]}),
        point(finish["lon"], finish["lat"], {"kind": "finish", "label": finish["query"]}),
    ]
    for stop in stops:
        features.append(
            point(
                stop["lon"],
                stop["lat"],
                {
                    "kind": "fuel_stop",
                    "order": stop["order"],
                    "name": stop["name"],
                    "city": stop["city"],
                    "state": stop["state"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "gallons_purchased": stop["gallons_purchased"],
                    "cost": stop["cost"],
                    "miles_from_start": stop["miles_from_start"],
                },
            )
        )
    return {"type": "FeatureCollection", "features": features}


def _log_request(start: str, finish: str, meta: dict) -> None:
    log.info(
        "route start=%r finish=%r cache_hit=%s external_calls=%d candidates=%s timings_ms=%s",
        start,
        finish,
        meta.get("cache_hit"),
        meta.get("external_api_calls", 0),
        meta.get("candidate_stations_in_corridor"),
        meta.get("timings_ms"),
    )
