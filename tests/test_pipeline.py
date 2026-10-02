from decimal import Decimal
from pathlib import Path

import pytest

from stations.normalize import (
    in_conus,
    normalize_address,
    normalize_city,
    normalize_state,
    repair_mojibake,
)
from stations.pipeline import (
    PipelineStats,
    RawStation,
    clean_stations,
    dedupe_by_address,
    dedupe_by_id,
    filter_us,
    read_fuel_csv,
    unique_city_states,
)


def station(**overrides) -> RawStation:
    base = {
        "station_id": 1,
        "name": "PILOT #1",
        "address": "I-80, EXIT 1",
        "city": "Somewhere",
        "state": "OH",
        "price": Decimal("3.5"),
    }
    base.update(overrides)
    return RawStation(**base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Villa Park      ", "villa park"),
        ("St. Louis", "saint louis"),
        ("Saint Louis", "saint louis"),
        ("Ft. Smith", "fort smith"),
        ("Mt. Vernon", "mount vernon"),
        ("Winston-Salem", "winston salem"),
        ("Mc Lean", "mclean"),
        ("McLean", "mclean"),
        ("La Fayette", "lafayette"),
        ("Du Bois", "dubois"),
        ("Bois D'Arc", "bois darc"),
        ("Hot Springs National Park", "hot springs"),
        ("O'Fallon", "ofallon"),
    ],
)
def test_normalize_city(raw, expected):
    assert normalize_city(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Stuckeyâ€™s Travel Center", "Stuckey\u2019s Travel Center"),  # as found in the source CSV
        ("Stuckey\u2019s Travel Center", "Stuckey\u2019s Travel Center"),  # already correct
        ("PILOT #1", "PILOT #1"),
        ("Café Olé", "Café Olé"),  # genuine Latin-1 text that is not double-encoded
    ],
)
def test_repair_mojibake(raw, expected):
    assert repair_mojibake(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NY", "NY"),
        (" ny ", "NY"),
        ("New York", "NY"),
        ("illinois", "IL"),
        ("Washington DC", "DC"),
        ("ON", None),
        ("", None),
    ],
)
def test_normalize_state(raw, expected):
    assert normalize_state(raw) == expected


def test_normalize_address_ignores_punctuation_and_case():
    assert normalize_address("I-80, EXIT 100 & US-6") == normalize_address("i80 exit 100 & us6")


def test_in_conus():
    assert in_conus(40.7, -74.0)
    assert not in_conus(61.2, -149.9)  # Anchorage
    assert not in_conus(21.3, -157.8)  # Honolulu
    assert not in_conus(51.05, -114.07)  # Calgary


def test_filter_us_drops_canadian_rows():
    rows = [station(state="OH"), station(station_id=2, state="ON"), station(station_id=3, state="DC")]
    stats = PipelineStats()
    kept = filter_us(rows, stats)
    assert [r.station_id for r in kept] == [1, 3]
    assert stats.rows_in == 3 and stats.rows_us == 2 and stats.rows_non_us == 1
    assert stats.non_us_states == {"ON": 1}


def test_dedupe_by_id_min_policy_keeps_lowest_price_and_first_name():
    rows = [
        station(station_id=20, name="PILOT TRAVEL CENTER #1243", price=Decimal("3.899")),
        station(station_id=20, name="PILOT #1243", price=Decimal("3.799")),
        station(station_id=7, name="WOODSHED", price=Decimal("3.007")),
    ]
    result = dedupe_by_id(rows, "min")
    assert [r.station_id for r in result] == [7, 20]
    assert result[1].name == "PILOT TRAVEL CENTER #1243"
    assert result[1].price == Decimal("3.799")


def test_dedupe_by_id_other_policies():
    rows = [station(price=Decimal("3.1")), station(price=Decimal("3.9"))]
    assert dedupe_by_id(rows, "max")[0].price == Decimal("3.9")
    assert dedupe_by_id(rows, "first")[0].price == Decimal("3.1")
    with pytest.raises(ValueError, match="STATION_PRICE_POLICY"):
        dedupe_by_id(rows, "median")  # type: ignore[arg-type]


def test_dedupe_by_address_keeps_cheapest_of_same_location():
    rows = [
        station(station_id=1, address="I-80, EXIT 1", price=Decimal("3.5")),
        station(station_id=2, address="I-80 EXIT 1", price=Decimal("3.2")),
        station(station_id=3, address="I-80, EXIT 2", price=Decimal("3.0")),
    ]
    result = dedupe_by_address(rows)
    assert [r.station_id for r in result] == [2, 3]


def test_unique_city_states_uses_normalized_key():
    rows = [station(city="St. Louis", state="MO"), station(station_id=2, city="Saint Louis ", state="MO")]
    assert unique_city_states(rows) == [("St. Louis", "MO")]


def test_full_pipeline_on_real_csv():
    """Numbers from Section 2 of the assignment, verified against the shipped CSV."""
    path = Path(__file__).resolve().parents[1] / "data" / "fuel-prices-for-be-assessment.csv"
    stats = PipelineStats()
    result = clean_stations(read_fuel_csv(path), "min", stats)
    assert stats.rows_in == 8151
    assert stats.rows_us == 7531
    assert stats.rows_non_us == 620
    assert stats.after_id_dedupe == 6626
    assert len(result) == stats.after_address_dedupe
    assert len({r.station_id for r in result}) == len(result)
    assert all(r.state != "ON" for r in result)
    assert len(unique_city_states(result)) == 3808
