"""OFFLINE, run once: reduce public gazetteers to ``data/us_places_gazetteer.csv``.

Sources (download manually into ``data/raw/``; both are free):

* US Census Bureau Gazetteer, Places, 2024 (primary, authoritative centroids)
  https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip
* GeoNames US dump (fills New England towns, townships, consolidated cities)
  https://download.geonames.org/export/dump/US.zip

Census rows are written first so they win on lookup. GeoNames populated places
are appended only when the (city, state) key is not already covered AND the
place is either populated, an administrative seat, or needed by a station in
the fuel-price CSV. That keeps the committed file small.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from stations.normalize import US_STATES, normalize_city
from stations.pipeline import PipelineStats, clean_stations, read_fuel_csv, unique_city_states

# Census "NAME" carries the legal/statistical area description as a suffix.
_CENSUS_DESCRIPTORS = (
    "city and borough",
    "consolidated government",
    "metropolitan government",
    "metro government",
    "unified government",
    "urban county",
    "municipality",
    "comunidad",
    "zona urbana",
    "plantation",
    "township",
    "borough",
    "village",
    "purchase",
    "location",
    "grant",
    "gore",
    "city",
    "town",
    "CDP",
)
_BALANCE = re.compile(r"\s*\(balance\)\s*$")

_GEONAMES_CODES = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5, "PPLX": 6}
_GEONAMES_ADMIN_CODES = {"PPLC", "PPLA", "PPLA2", "PPLA3", "PPLA4"}


def census_display_name(name: str, lsad: str) -> str:
    """Strip the Census area descriptor.

    ``"Abbeville city"`` → ``"Abbeville"``;
    ``"Louisville/Jefferson County metro government (balance)"`` → ``"Louisville"``.
    """
    text = _BALANCE.sub("", name.strip())
    if lsad != "00":
        lowered = text.casefold()
        for descriptor in _CENSUS_DESCRIPTORS:
            if lowered.endswith(" " + descriptor.casefold()):
                text = text[: -(len(descriptor) + 1)].rstrip()
                break
    # Consolidated city-county names: "Athens-Clarke County" → "Athens"
    if "county" in text.casefold():
        head = re.split(r"[-/]", text, maxsplit=1)[0].strip()
        if head and "county" not in head.casefold():
            text = head
    return text


class Command(BaseCommand):
    help = "Reduce Census + GeoNames downloads into data/us_places_gazetteer.csv (offline, run once)."

    def add_arguments(self, parser) -> None:
        raw = settings.DATA_DIR / "raw"
        parser.add_argument("--census", type=Path, default=raw / "2024_Gaz_place_national.txt")
        parser.add_argument("--geonames", type=Path, default=raw / "US.txt")
        parser.add_argument("--output", type=Path, default=settings.GAZETTEER_CSV)

    def handle(self, *args, **options) -> None:
        census_path: Path = options["census"]
        geonames_path: Path = options["geonames"]
        output: Path = options["output"]
        if not census_path.exists():
            raise CommandError(f"Census file not found: {census_path} (see module docstring for the URL)")
        if not geonames_path.exists():
            raise CommandError(f"GeoNames file not found: {geonames_path} (see module docstring for the URL)")

        needed = self._station_keys()
        rows: dict[tuple[str, str], tuple[str, str, float, float, str]] = {}

        census_count = self._load_census(census_path, rows)
        geonames_count = self._load_geonames(geonames_path, rows, needed)

        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(["city", "state", "lat", "lon", "source"])
            for city, state, lat, lon, source in sorted(
                rows.values(), key=lambda r: (r[4] != "census", r[1], r[0])
            ):
                writer.writerow([city, state, f"{lat:.5f}", f"{lon:.5f}", source])

        covered = sum(1 for key in needed if key in rows)
        self.stdout.write(
            f"Wrote {len(rows)} places to {output} (census={census_count}, geonames={geonames_count}). "
            f"Station (city,state) pairs covered: {covered}/{len(needed)}."
        )

    def _station_keys(self) -> set[tuple[str, str]]:
        fuel_csv = Path(settings.FUEL_PRICES_CSV)
        if not fuel_csv.exists():
            self.stderr.write(
                f"Fuel CSV {fuel_csv} not found; GeoNames fill-in will rely on population only."
            )
            return set()
        stations = clean_stations(read_fuel_csv(fuel_csv), "min", PipelineStats())
        return {(normalize_city(city), state) for city, state in unique_city_states(stations)}

    def _load_census(self, path: Path, rows: dict) -> int:
        best_area: dict[tuple[str, str], float] = {}
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh, delimiter="\t"):
                row = {k.strip(): v.strip() for k, v in raw.items() if k}
                state = row["USPS"]
                if state not in US_STATES:
                    continue
                city = census_display_name(row["NAME"], row["LSAD"])
                key = (normalize_city(city), state)
                area = float(row["ALAND"] or 0)
                # Same name twice in one state (e.g. a CDP and a city): keep the larger one.
                if key in best_area and best_area[key] >= area:
                    continue
                best_area[key] = area
                rows[key] = (city, state, float(row["INTPTLAT"]), float(row["INTPTLONG"]), "census")
        return len(best_area)

    def _load_geonames(self, path: Path, rows: dict, needed: set[tuple[str, str]]) -> int:
        candidates: dict[tuple[str, str], tuple[tuple[int, int], tuple]] = {}
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 15 or parts[6] != "P" or parts[7] not in _GEONAMES_CODES:
                    continue
                state = parts[10]
                if state not in US_STATES:
                    continue
                population = int(parts[14] or 0)
                for name in {parts[1], parts[2]}:
                    key = (normalize_city(name), state)
                    if not key[0] or key in rows:
                        continue
                    wanted = population > 0 or parts[7] in _GEONAMES_ADMIN_CODES or key in needed
                    if not wanted:
                        continue
                    rank = (-population, _GEONAMES_CODES[parts[7]])
                    current = candidates.get(key)
                    if current is None or rank < current[0]:
                        candidates[key] = (rank, (name, state, float(parts[4]), float(parts[5]), "geonames"))
        for key, (_rank, record) in candidates.items():
            rows[key] = record
        return len(candidates)
