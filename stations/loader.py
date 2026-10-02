"""In-memory station index: loaded once per process from ``stations_geocoded.csv``.

Runtime requests never touch the DB or re-read the CSV; they filter these numpy
arrays. ~6k stations → a few hundred KB.
"""

from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from django.conf import settings


@dataclass(frozen=True, slots=True)
class StationIndex:
    ids: np.ndarray  # int64
    lats: np.ndarray  # float64, degrees
    lons: np.ndarray  # float64, degrees
    prices: np.ndarray  # float64, $/gallon
    states: np.ndarray  # str
    names: list[str]
    addresses: list[str]
    cities: list[str]

    def __len__(self) -> int:
        return int(self.ids.shape[0])

    @classmethod
    def from_csv(cls, path: Path) -> StationIndex:
        if not path.exists():
            raise FileNotFoundError(
                f"Station data file {path} is missing. Run `python manage.py build_station_geodata` "
                "to generate it (or restore data/stations_geocoded.csv from the repository)."
            )
        ids, lats, lons, prices, states, names, addresses, cities = [], [], [], [], [], [], [], []
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ids.append(int(row["station_id"]))
                lats.append(float(row["lat"]))
                lons.append(float(row["lon"]))
                prices.append(float(row["price"]))
                states.append(row["state"])
                names.append(row["name"])
                addresses.append(row["address"])
                cities.append(row["city"])
        if not ids:
            raise ValueError(f"Station data file {path} contains no rows.")
        return cls(
            ids=np.asarray(ids, dtype=np.int64),
            lats=np.asarray(lats, dtype=np.float64),
            lons=np.asarray(lons, dtype=np.float64),
            prices=np.asarray(prices, dtype=np.float64),
            states=np.asarray(states),
            names=names,
            addresses=addresses,
            cities=cities,
        )

    def record(self, index: int) -> dict:
        """Plain dict for one station, used to build API responses."""
        return {
            "station_id": int(self.ids[index]),
            "name": self.names[index],
            "address": self.addresses[index],
            "city": self.cities[index],
            "state": str(self.states[index]),
            "lat": float(self.lats[index]),
            "lon": float(self.lons[index]),
            "price": float(self.prices[index]),
        }


_index: StationIndex | None = None
_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """Lazily load the index once per process (double-checked locking)."""
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = StationIndex.from_csv(Path(settings.STATIONS_GEOCODED_CSV))
    return _index


def reset_station_index() -> None:
    """Testing hook."""
    global _index
    with _lock:
        _index = None
