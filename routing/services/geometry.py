"""Route geometry with numpy only. No Django, no network.

Coordinates follow GeoJSON order: ``(lon, lat)`` in degrees. Distances are miles.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
MILES_PER_DEGREE_LAT = 2 * np.pi * EARTH_RADIUS_MILES / 360.0  # ≈ 69.09


def haversine_miles(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance, broadcasting over numpy inputs in degrees."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=np.float64)) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_miles(coords: np.ndarray) -> np.ndarray:
    """Cumulative great-circle distance along a polyline of ``(lon, lat)`` vertices."""
    coords = np.asarray(coords, dtype=np.float64)
    if coords.ndim != 2 or coords.shape[1] != 2 or coords.shape[0] == 0:
        raise ValueError("coords must be a non-empty (N, 2) array of lon/lat pairs")
    if coords.shape[0] == 1:
        return np.zeros(1)
    seg = haversine_miles(coords[:-1, 1], coords[:-1, 0], coords[1:, 1], coords[1:, 0])
    return np.concatenate(([0.0], np.cumsum(seg)))


@dataclass(frozen=True, slots=True)
class SampledRoute:
    points: np.ndarray  # (M, 2) lon/lat
    miles: np.ndarray  # (M,) cumulative miles, miles[0] == 0, miles[-1] == total

    @property
    def total_miles(self) -> float:
        return float(self.miles[-1])


def resample_route(coords: np.ndarray, spacing_miles: float = 1.0) -> SampledRoute:
    """Resample a polyline to (approximately) uniform spacing along its length.

    Start and end vertices are kept exactly. Intermediate points are linearly
    interpolated on the chords, which is accurate for road geometry at mile scale.
    """
    coords = np.asarray(coords, dtype=np.float64)
    cum = cumulative_miles(coords)
    total = float(cum[-1])
    if total <= 0.0 or coords.shape[0] < 2:
        return SampledRoute(points=coords[:1].copy(), miles=np.zeros(1))

    # Drop zero-length segments so np.interp sees a strictly increasing x.
    keep = np.concatenate(([True], np.diff(cum) > 0))
    coords, cum = coords[keep], cum[keep]

    n = max(2, int(np.ceil(total / spacing_miles)) + 1)
    target = np.linspace(0.0, total, n)
    lon = np.interp(target, cum, coords[:, 0])
    lat = np.interp(target, cum, coords[:, 1])
    points = np.column_stack((lon, lat))
    points[0], points[-1] = coords[0], coords[-1]
    return SampledRoute(points=points, miles=target)


@dataclass(frozen=True, slots=True)
class Projection:
    """Stations found inside the route corridor."""

    station_indices: np.ndarray  # int, indices into the caller's station arrays
    miles_along: np.ndarray  # float, distance from route start to the nearest route point
    offset_miles: np.ndarray  # float, distance from station to that route point
    bbox_candidates: int  # how many stations passed the bounding-box pre-filter


def project_stations(
    route: SampledRoute,
    station_lats: np.ndarray,
    station_lons: np.ndarray,
    corridor_miles: float,
    coarse_stride: int = 10,
    refine_k: int = 4,
    bbox_chunk_points: int = 200,
) -> Projection:
    """Locate stations within ``corridor_miles`` of the route and their along-route position.

    Two-stage nearest-point search so a coast-to-coast route (≈3,000 sample
    points) against ~6,000 stations stays well under 50 ms:

    1. bounding-box pre-filter per chunk of the route (degrees, padded by the corridor);
    2. coarse: distance to every ``coarse_stride``-th route point in a local
       equirectangular plane; keep stations plausibly inside the corridor and
       their ``refine_k`` nearest coarse points;
    3. fine: exact haversine to the route points around those coarse points.
    """
    station_lats = np.asarray(station_lats, dtype=np.float64)
    station_lons = np.asarray(station_lons, dtype=np.float64)
    pts, cum = route.points, route.miles
    m = pts.shape[0]
    empty = Projection(np.empty(0, dtype=np.int64), np.empty(0), np.empty(0), 0)
    if m == 0 or station_lats.size == 0:
        return empty

    # 1. bounding-box pre-filter, one box per ~200 route points. A single box around a long
    #    diagonal route would admit most of the country; per-chunk boxes hug the route.
    pad_lat = corridor_miles / MILES_PER_DEGREE_LAT
    cos_mid = max(np.cos(np.radians(pts[:, 1].mean())), 0.2)
    pad_lon = pad_lat / cos_mid
    bounds = np.array(
        [
            (
                chunk[:, 1].min() - pad_lat,
                chunk[:, 1].max() + pad_lat,
                chunk[:, 0].min() - pad_lon,
                chunk[:, 0].max() + pad_lon,
            )
            for chunk in np.array_split(pts, max(1, m // bbox_chunk_points))
        ]
    )  # (chunks, 4)
    inside = (
        (station_lats[None, :] >= bounds[:, 0:1])
        & (station_lats[None, :] <= bounds[:, 1:2])
        & (station_lons[None, :] >= bounds[:, 2:3])
        & (station_lons[None, :] <= bounds[:, 3:4])
    ).any(axis=0)
    cand = np.flatnonzero(inside)
    if cand.size == 0:
        return empty
    s_lat, s_lon = station_lats[cand], station_lons[cand]

    # 2. coarse stage in a local flat plane (miles), centred on the route so magnitudes stay small.
    #    |a-b|^2 = |a|^2 + |b|^2 - 2 a.b lets BLAS do the (S x C) work instead of large broadcast temporaries.
    coarse_idx = np.unique(np.concatenate((np.arange(0, m, coarse_stride), [m - 1])))
    to_x = MILES_PER_DEGREE_LAT * cos_mid
    lon_c, lat_c = pts[:, 0].mean(), pts[:, 1].mean()
    route_xy = np.column_stack(
        ((pts[coarse_idx, 0] - lon_c) * to_x, (pts[coarse_idx, 1] - lat_c) * MILES_PER_DEGREE_LAT)
    )
    station_xy = np.column_stack(((s_lon - lon_c) * to_x, (s_lat - lat_c) * MILES_PER_DEGREE_LAT))
    d2 = (station_xy * station_xy).sum(axis=1)[:, None] + (route_xy * route_xy).sum(axis=1)[None, :]
    d2 -= 2.0 * (station_xy @ route_xy.T)
    np.maximum(d2, 0.0, out=d2)  # (S, C); clamp tiny negative round-off

    # A fine point within the corridor has a coarse neighbour (≤ stride/2 fine points away
    # along the route) no farther than corridor + half the coarse spacing. Widen for the
    # flat-plane approximation over long routes.
    coarse_spacing = float(cum[min(coarse_stride, m - 1)] - cum[0]) if m > 1 else 0.0
    margin = (corridor_miles + coarse_spacing / 2.0) * 1.5 + 1.0
    plausible = d2.min(axis=1) <= margin * margin
    if not plausible.any():
        return Projection(np.empty(0, dtype=np.int64), np.empty(0), np.empty(0), int(cand.size))
    cand, d2, s_lat, s_lon = cand[plausible], d2[plausible], s_lat[plausible], s_lon[plausible]

    k = min(refine_k, coarse_idx.size)
    nearest_coarse = (
        np.argpartition(d2, k - 1, axis=1)[:, :k]
        if k < coarse_idx.size
        else np.tile(np.arange(coarse_idx.size), (cand.size, 1))
    )

    # 3. fine stage: exact distances to fine points around each chosen coarse point
    half = coarse_stride  # window covers the full gap to the neighbouring coarse points
    offsets = np.arange(-half, half + 1)
    fine_idx = coarse_idx[nearest_coarse][:, :, None] + offsets[None, None, :]  # (S, k, W)
    fine_idx = np.clip(fine_idx.reshape(cand.size, -1), 0, m - 1)
    dist = haversine_miles(s_lat[:, None], s_lon[:, None], pts[fine_idx, 1], pts[fine_idx, 0])
    best = dist.argmin(axis=1)
    best_dist = dist[np.arange(cand.size), best]
    best_route_idx = fine_idx[np.arange(cand.size), best]

    keep = best_dist <= corridor_miles
    order = np.argsort(cum[best_route_idx[keep]], kind="stable")
    return Projection(
        station_indices=cand[keep][order],
        miles_along=cum[best_route_idx[keep]][order],
        offset_miles=best_dist[keep][order],
        bbox_candidates=int(inside.sum()),
    )


def downsample_geometry(coords: np.ndarray, spacing_miles: float = 0.25) -> list[list[float]]:
    """Route geometry for the response: ≈``spacing_miles`` apart, endpoints exact, 5-decimal rounding."""
    sampled = resample_route(np.asarray(coords, dtype=np.float64), spacing_miles)
    return np.round(sampled.points, 5).tolist()
