from __future__ import annotations

import time

import numpy as np
import pytest

from routing.services.geometry import (
    MILES_PER_DEGREE_LAT,
    cumulative_miles,
    downsample_geometry,
    haversine_miles,
    project_stations,
    resample_route,
)

NYC = (40.7128, -74.0060)
CHI = (41.8781, -87.6298)


def test_haversine_known_distance():
    # Great-circle NYC → Chicago is ≈ 711 miles.
    assert float(haversine_miles(NYC[0], NYC[1], CHI[0], CHI[1])) == pytest.approx(711.5, abs=2.0)
    assert float(haversine_miles(0, 0, 0, 0)) == 0.0


def test_haversine_broadcasts():
    lats = np.array([40.0, 41.0, 42.0])
    d = haversine_miles(lats, -74.0, 40.0, -74.0)
    assert d.shape == (3,)
    assert d[0] == 0.0 and d[1] == pytest.approx(MILES_PER_DEGREE_LAT, rel=1e-6)


def straight_route(miles: float, lat: float = 40.0, lon0: float = -100.0, vertices: int = 50) -> np.ndarray:
    """Polyline heading due east along a parallel, ``miles`` long."""
    deg_per_mile = 1.0 / (MILES_PER_DEGREE_LAT * np.cos(np.radians(lat)))
    lons = lon0 + np.linspace(0.0, miles * deg_per_mile, vertices)
    return np.column_stack((lons, np.full(vertices, lat)))


def test_cumulative_miles_monotonic_and_total():
    coords = straight_route(300.0)
    cum = cumulative_miles(coords)
    assert cum[0] == 0.0
    assert np.all(np.diff(cum) > 0)
    assert cum[-1] == pytest.approx(300.0, rel=1e-4)


def test_cumulative_miles_rejects_bad_shape():
    with pytest.raises(ValueError):
        cumulative_miles(np.zeros((0, 2)))
    with pytest.raises(ValueError):
        cumulative_miles(np.zeros((3, 3)))


def test_resample_keeps_endpoints_and_spacing():
    coords = straight_route(100.0, vertices=7)
    sampled = resample_route(coords, spacing_miles=1.0)
    assert sampled.points.shape[0] == 101
    np.testing.assert_allclose(sampled.points[0], coords[0])
    np.testing.assert_allclose(sampled.points[-1], coords[-1])
    assert sampled.total_miles == pytest.approx(100.0, rel=1e-4)
    spacing = np.diff(sampled.miles)
    assert spacing.min() == pytest.approx(spacing.max(), rel=1e-6)
    assert spacing.mean() == pytest.approx(1.0, rel=1e-3)


def test_resample_handles_duplicate_vertices_and_degenerate_input():
    coords = np.array([[-100.0, 40.0], [-100.0, 40.0], [-99.0, 40.0], [-99.0, 40.0]])
    sampled = resample_route(coords, 1.0)
    assert sampled.total_miles > 50
    single = resample_route(np.array([[-100.0, 40.0]]), 1.0)
    assert single.points.shape == (1, 2) and single.total_miles == 0.0


def test_project_stations_positions_and_offsets():
    route = resample_route(straight_route(500.0), spacing_miles=1.0)
    deg_lat_per_mile = 1.0 / MILES_PER_DEGREE_LAT
    lon_at = lambda m: float(np.interp(m, route.miles, route.points[:, 0]))  # noqa: E731
    # station A: 3 miles north of mile 120; B: 25 miles south of mile 300 (outside corridor);
    # C: right on mile 410; D: far away.
    lats = np.array([40.0 + 3 * deg_lat_per_mile, 40.0 - 25 * deg_lat_per_mile, 40.0, 30.0])
    lons = np.array([lon_at(120), lon_at(300), lon_at(410), -80.0])

    proj = project_stations(route, lats, lons, corridor_miles=10.0)
    assert proj.station_indices.tolist() == [0, 2]
    assert proj.miles_along[0] == pytest.approx(120.0, abs=0.6)
    assert proj.offset_miles[0] == pytest.approx(3.0, abs=0.1)
    assert proj.miles_along[1] == pytest.approx(410.0, abs=0.6)
    assert proj.offset_miles[1] == pytest.approx(0.0, abs=0.05)
    assert proj.bbox_candidates == 2  # B (25 mi off) and D fail the 10-mile padded bbox


def test_project_stations_sorted_by_miles_along_and_handles_empty():
    route = resample_route(straight_route(200.0), 1.0)
    lons = np.interp([150.0, 20.0, 90.0], route.miles, route.points[:, 0])
    proj = project_stations(route, np.full(3, 40.0), lons, 10.0)
    assert np.all(np.diff(proj.miles_along) > 0)
    empty = project_stations(route, np.array([]), np.array([]), 10.0)
    assert empty.station_indices.size == 0


def test_project_stations_on_a_winding_route_finds_true_nearest():
    # A route that bends back on itself: east 200 mi, north 30 mi, west 200 mi.
    east = straight_route(200.0, lat=40.0, vertices=40)
    north_lat = 40.0 + 30.0 / MILES_PER_DEGREE_LAT
    north = np.column_stack((np.full(10, east[-1, 0]), np.linspace(40.0, north_lat, 10)))
    west = straight_route(200.0, lat=north_lat, vertices=40)[::-1]
    west[:, 0] = west[:, 0] - (west[0, 0] - east[-1, 0])  # start the westbound leg where the northbound ended
    route = resample_route(np.vstack((east, north[1:], west[1:])), 1.0)

    # Station 2 miles north of the eastbound leg at mile 100: nearest is mile 100,
    # not the westbound leg (28 miles away).
    station_lat = 40.0 + 2.0 / MILES_PER_DEGREE_LAT
    station_lon = float(np.interp(100.0, route.miles, route.points[:, 0]))
    proj = project_stations(route, np.array([station_lat]), np.array([station_lon]), 10.0)
    assert proj.miles_along[0] == pytest.approx(100.0, abs=1.0)
    assert proj.offset_miles[0] == pytest.approx(2.0, abs=0.1)


def test_projection_is_fast_for_coast_to_coast():
    """Whole projection step for a ~3,000-mile diagonal route against 6,000 stations must be quick.

    A diagonal (San Diego → Maine) has a bounding box covering most of the US,
    so the pre-filter removes little and the two-stage search does the work.
    """
    rng = np.random.default_rng(42)
    diagonal = np.column_stack((np.linspace(-117.2, -68.8, 400), np.linspace(32.7, 46.9, 400)))
    route = resample_route(diagonal, 1.0)
    assert 2600 < route.total_miles < 3200
    lats = rng.uniform(25.0, 49.0, 6000)
    lons = rng.uniform(-124.0, -67.0, 6000)

    project_stations(route, lats, lons, 10.0)  # warm-up
    started = time.perf_counter()
    proj = project_stations(route, lats, lons, 10.0)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert proj.station_indices.size > 0
    assert np.all(proj.offset_miles <= 10.0)
    assert elapsed_ms < 250, f"projection took {elapsed_ms:.1f} ms"


def test_downsample_geometry_keeps_endpoints_and_rounds():
    coords = straight_route(10.0, vertices=200)
    out = downsample_geometry(coords, spacing_miles=0.25)
    assert 39 <= len(out) <= 43
    assert out[0] == [round(coords[0, 0], 5), round(coords[0, 1], 5)]
    assert out[-1] == [round(coords[-1, 0], 5), round(coords[-1, 1], 5)]
