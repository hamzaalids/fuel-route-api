"""Local US places gazetteer: (city, state) → (lat, lon), zero network.

The CSV is built once by ``manage.py build_gazetteer`` from the Census
Gazetteer "Places" file and GeoNames, and committed to the repo.
"""

from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .normalize import normalize_city, normalize_state


@dataclass(frozen=True, slots=True)
class Place:
    city: str
    state: str
    lat: float
    lon: float
    source: str


class Gazetteer:
    def __init__(self, places: dict[tuple[str, str], Place]) -> None:
        self._places = places

    @classmethod
    def from_csv(cls, path: Path) -> Gazetteer:
        if not path.exists():
            raise FileNotFoundError(
                f"Gazetteer file {path} is missing. Run `python manage.py build_gazetteer` "
                "(or restore data/us_places_gazetteer.csv from the repository)."
            )
        places: dict[tuple[str, str], Place] = {}
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                key = (normalize_city(row["city"]), row["state"])
                # First occurrence wins; the build command writes Census rows first.
                places.setdefault(
                    key,
                    Place(row["city"], row["state"], float(row["lat"]), float(row["lon"]), row["source"]),
                )
        return cls(places)

    def __len__(self) -> int:
        return len(self._places)

    def lookup(self, city: str, state: str) -> Place | None:
        code = normalize_state(state)
        if code is None:
            return None
        return self._places.get((normalize_city(city), code))

    def state_bounding_boxes(self, pad_degrees: float = 0.5) -> dict[str, tuple[float, float, float, float]]:
        """Rough per-state (min_lat, max_lat, min_lon, max_lon) derived from Census places."""
        boxes: dict[str, list[float]] = {}
        for place in self._places.values():
            if place.source != "census":
                continue
            box = boxes.setdefault(place.state, [place.lat, place.lat, place.lon, place.lon])
            box[0] = min(box[0], place.lat)
            box[1] = max(box[1], place.lat)
            box[2] = min(box[2], place.lon)
            box[3] = max(box[3], place.lon)
        return {
            st: (b[0] - pad_degrees, b[1] + pad_degrees, b[2] - pad_degrees, b[3] + pad_degrees)
            for st, b in boxes.items()
        }


_instance: Gazetteer | None = None
_lock = threading.Lock()


def get_gazetteer() -> Gazetteer:
    """Process-wide lazily loaded gazetteer."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = Gazetteer.from_csv(Path(settings.GAZETTEER_CSV))
    return _instance
