"""OFFLINE, run once: fuel-price CSV → cleaned, deduplicated, geocoded stations CSV.

    python manage.py build_station_geodata

Output ``data/stations_geocoded.csv`` is committed so reviewers need zero setup.
Geocoding is city-level: the local gazetteer first, Nominatim (≤1 req/s,
resumable via ``data/nominatim_cache.json``) only for misses.
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services.geocoding import NominatimClient, default_nominatim_client
from stations.gazetteer import Gazetteer
from stations.normalize import in_conus, normalize_city
from stations.pipeline import PipelineStats, RawStation, clean_stations, read_fuel_csv, unique_city_states

OUTPUT_COLUMNS = ["station_id", "name", "address", "city", "state", "price", "lat", "lon", "geocode_source"]


class Command(BaseCommand):
    help = "Clean, dedupe and geocode the fuel-price CSV into data/stations_geocoded.csv (offline)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--input", type=Path, default=Path(settings.FUEL_PRICES_CSV))
        parser.add_argument("--output", type=Path, default=Path(settings.STATIONS_GEOCODED_CSV))
        parser.add_argument("--gazetteer", type=Path, default=Path(settings.GAZETTEER_CSV))
        parser.add_argument(
            "--nominatim-cache", type=Path, default=settings.DATA_DIR / "nominatim_cache.json"
        )
        parser.add_argument(
            "--no-network", action="store_true", help="Skip Nominatim; drop unresolved stations."
        )

    def handle(self, *args, **options) -> None:
        input_path: Path = options["input"]
        if not input_path.exists():
            raise CommandError(f"Input CSV not found: {input_path}")
        gazetteer = Gazetteer.from_csv(options["gazetteer"])
        policy = settings.STATION_PRICE_POLICY

        stats = PipelineStats()
        stations = clean_stations(read_fuel_csv(input_path), policy, stats)
        pairs = unique_city_states(stations)

        coords, sources, failures = self._geocode_pairs(
            pairs, gazetteer, options["nominatim_cache"], use_network=not options["no_network"]
        )

        kept, dropped = self._attach_coordinates(stations, coords, sources)
        outliers = self._state_outliers(kept, gazetteer)
        self._write(options["output"], kept)
        self._print_summary(
            stats, pairs, sources, failures, kept, dropped, outliers, policy, options["output"]
        )

    # -- geocoding -----------------------------------------------------------

    def _geocode_pairs(
        self,
        pairs: list[tuple[str, str]],
        gazetteer: Gazetteer,
        cache_path: Path,
        use_network: bool,
    ) -> tuple[dict[tuple[str, str], tuple[float, float]], dict[tuple[str, str], str], list[tuple[str, str]]]:
        coords: dict[tuple[str, str], tuple[float, float]] = {}
        sources: dict[tuple[str, str], str] = {}
        misses: list[tuple[str, str]] = []
        state_boxes = gazetteer.state_bounding_boxes()

        for city, state in pairs:
            key = (normalize_city(city), state)
            place = gazetteer.lookup(city, state)
            if place is not None and in_conus(place.lat, place.lon):
                coords[key] = (place.lat, place.lon)
                sources[key] = "gazetteer"
            else:
                misses.append((city, state))

        failures: list[tuple[str, str]] = []
        if misses:
            cache = self._load_cache(cache_path)
            client = default_nominatim_client() if use_network else None
            for index, (city, state) in enumerate(misses, start=1):
                key = (normalize_city(city), state)
                cache_key = f"{key[0]}|{state}"
                if cache_key in cache:
                    result = cache[cache_key]
                elif client is None:
                    result = None
                else:
                    result = self._query_nominatim(client, city, state, state_boxes.get(state))
                    cache[cache_key] = result
                    self._save_cache(cache_path, cache)
                    self.stdout.write(f"  nominatim [{index}/{len(misses)}] {city}, {state} -> {result}")

                if result and in_conus(result[0], result[1]):
                    coords[key] = (result[0], result[1])
                    sources[key] = "nominatim"
                else:
                    failures.append((city, state))
        return coords, sources, failures

    @staticmethod
    def _query_nominatim(
        client: NominatimClient, city: str, state: str, state_box: tuple[float, float, float, float] | None
    ) -> list[float] | None:
        """Structured search first, free-text second; reject hits outside the state's rough box."""
        attempts = (
            lambda: client.search_city(city, state),
            lambda: client.search_free_text(f"{city}, {state}"),
        )
        for attempt in attempts:
            try:
                result = attempt()
            except requests.RequestException as exc:
                raise CommandError(f"Nominatim request failed for {city}, {state}: {exc}") from exc
            finally:
                time.sleep(1.05)  # Nominatim usage policy: max 1 request/second
            if result is None:
                continue
            lat, lon = result
            if state_box and not (
                state_box[0] <= lat <= state_box[1] and state_box[2] <= lon <= state_box[3]
            ):
                continue  # wrong state (e.g. "La Place, LA" resolved into Alabama)
            return [lat, lon]
        return None

    @staticmethod
    def _load_cache(path: Path) -> dict[str, list[float] | None]:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    @staticmethod
    def _save_cache(path: Path, cache: dict) -> None:
        path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")

    # -- assembling output ---------------------------------------------------

    @staticmethod
    def _attach_coordinates(
        stations: list[RawStation],
        coords: dict[tuple[str, str], tuple[float, float]],
        sources: dict[tuple[str, str], str],
    ) -> tuple[list[dict], list[RawStation]]:
        kept: list[dict] = []
        dropped: list[RawStation] = []
        for station in stations:
            key = (normalize_city(station.city), station.state)
            if key not in coords:
                dropped.append(station)
                continue
            lat, lon = coords[key]
            kept.append(
                {
                    "station_id": station.station_id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "price": f"{station.price.normalize():f}",
                    "lat": f"{lat:.5f}",
                    "lon": f"{lon:.5f}",
                    "geocode_source": sources[key],
                }
            )
        return kept, dropped

    @staticmethod
    def _state_outliers(kept: list[dict], gazetteer: Gazetteer) -> list[dict]:
        boxes = gazetteer.state_bounding_boxes()
        outliers = []
        for row in kept:
            box = boxes.get(row["state"])
            if box is None:
                continue
            lat, lon = float(row["lat"]), float(row["lon"])
            if not (box[0] <= lat <= box[1] and box[2] <= lon <= box[3]):
                outliers.append(row)
        return outliers

    @staticmethod
    def _write(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def _print_summary(
        self,
        stats: PipelineStats,
        pairs: list,
        sources: dict,
        failures: list,
        kept: list,
        dropped: list,
        outliers: list,
        policy: str,
        output: Path,
    ) -> None:
        by_source = Counter(sources.values())
        lines = [
            "",
            "=== build_station_geodata summary ===",
            f"rows in CSV:                 {stats.rows_in}",
            f"US rows kept:                {stats.rows_us}  (dropped {stats.rows_non_us} non-US: "
            f"{dict(stats.non_us_states)})",
            f"after dedupe by ID ({policy}):   {stats.after_id_dedupe}",
            f"after dedupe by address:     {stats.after_address_dedupe}",
            f"unique (city, state) pairs:  {len(pairs)}",
            f"  geocoded via gazetteer:    {by_source.get('gazetteer', 0)}",
            f"  geocoded via nominatim:    {by_source.get('nominatim', 0)}",
            f"  failed:                    {len(failures)}",
            f"stations written:            {len(kept)}  -> {output}",
            f"stations dropped (no geocode): {len(dropped)}",
        ]
        if failures:
            lines.append("failed pairs: " + ", ".join(f"{c}, {s}" for c, s in failures))
        if outliers:
            lines.append(f"WARNING {len(outliers)} stations fall outside their state's rough bounding box:")
            lines.extend(
                f"  {o['station_id']} {o['city']}, {o['state']} ({o['lat']}, {o['lon']})" for o in outliers
            )
        else:
            lines.append("state bounding-box sanity check: all stations inside their state's box")
        self.stdout.write("\n".join(lines))
