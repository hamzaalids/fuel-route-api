"""Pure data-cleaning steps for the fuel-price CSV. No Django, no network.

Input rows are the original CSV columns; output is a list of ``RawStation``
records with one row per physical station, US-only.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from .normalize import US_STATES, normalize_address, normalize_city, repair_mojibake

PricePolicy = Literal["min", "max", "first"]

# Fuel is quoted to a tenth of a cent; the CSV carries 3-way averages like 3.00733333.
PRICE_QUANTUM = Decimal("0.001")

CSV_COLUMNS = {
    "OPIS Truckstop ID": "station_id",
    "Truckstop Name": "name",
    "Address": "address",
    "City": "city",
    "State": "state",
    "Rack ID": "rack_id",
    "Retail Price": "price",
}


@dataclass(frozen=True, slots=True)
class RawStation:
    station_id: int
    name: str
    address: str
    city: str
    state: str
    price: Decimal


@dataclass(slots=True)
class PipelineStats:
    rows_in: int = 0
    rows_us: int = 0
    rows_non_us: int = 0
    after_id_dedupe: int = 0
    after_address_dedupe: int = 0
    non_us_states: Counter = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.non_us_states is None:
            self.non_us_states = Counter()


def read_fuel_csv(path: Path) -> list[RawStation]:
    """Load and lightly clean every row (strip whitespace, parse numbers)."""
    rows: list[RawStation] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing expected columns: {sorted(missing)}")
        for raw in reader:
            rows.append(
                RawStation(
                    station_id=int(raw["OPIS Truckstop ID"].strip()),
                    name=repair_mojibake(raw["Truckstop Name"].strip()),
                    address=repair_mojibake(raw["Address"].strip()),
                    city=repair_mojibake(raw["City"].strip()),
                    state=raw["State"].strip().upper(),
                    price=Decimal(raw["Retail Price"].strip()).quantize(
                        PRICE_QUANTUM, rounding=ROUND_HALF_UP
                    ),
                )
            )
    return rows


def filter_us(rows: list[RawStation], stats: PipelineStats) -> list[RawStation]:
    """Keep the 50 states + DC. Canadian provinces etc. are dropped."""
    kept: list[RawStation] = []
    for row in rows:
        if row.state in US_STATES:
            kept.append(row)
        else:
            stats.non_us_states[row.state] += 1
    stats.rows_in = len(rows)
    stats.rows_us = len(kept)
    stats.rows_non_us = len(rows) - len(kept)
    return kept


def _pick_price(prices: list[Decimal], policy: PricePolicy) -> Decimal:
    if policy == "min":
        return min(prices)
    if policy == "max":
        return max(prices)
    if policy == "first":
        return prices[0]
    raise ValueError(f"Unknown STATION_PRICE_POLICY {policy!r}; expected min|max|first")


def dedupe_by_id(rows: list[RawStation], policy: PricePolicy = "min") -> list[RawStation]:
    """One record per OPIS Truckstop ID.

    Duplicate IDs are the same station listed under name variants (and sometimes
    a different price). Price is collapsed per ``policy``; the first non-empty
    name is kept.
    """
    grouped: dict[int, list[RawStation]] = {}
    for row in rows:
        grouped.setdefault(row.station_id, []).append(row)

    result: list[RawStation] = []
    for group in grouped.values():
        first = group[0]
        name = next((g.name for g in group if g.name), first.name)
        price = _pick_price([g.price for g in group], policy)
        result.append(replace(first, name=name, price=price))
    result.sort(key=lambda r: r.station_id)
    return result


def dedupe_by_address(rows: list[RawStation]) -> list[RawStation]:
    """Collapse different IDs that share (normalized address, city, state), keeping the cheapest."""
    best: dict[tuple[str, str, str], RawStation] = {}
    for row in rows:
        key = (normalize_address(row.address), normalize_city(row.city), row.state)
        current = best.get(key)
        if current is None or (row.price, row.station_id) < (current.price, current.station_id):
            best[key] = row
    return sorted(best.values(), key=lambda r: r.station_id)


def clean_stations(rows: list[RawStation], policy: PricePolicy, stats: PipelineStats) -> list[RawStation]:
    """Full cleaning chain: US-only → dedupe by ID → dedupe by address."""
    us_rows = filter_us(rows, stats)
    by_id = dedupe_by_id(us_rows, policy)
    stats.after_id_dedupe = len(by_id)
    by_address = dedupe_by_address(by_id)
    stats.after_address_dedupe = len(by_address)
    return by_address


def unique_city_states(rows: list[RawStation]) -> list[tuple[str, str]]:
    """Distinct (city, state) pairs, using the first-seen spelling of each city."""
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        seen.setdefault((normalize_city(row.city), row.state), (row.city, row.state))
    return sorted(seen.values(), key=lambda p: (p[1], p[0]))
