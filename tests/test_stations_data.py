"""Tests for the committed data files, the gazetteer and the in-memory station index."""

import csv
from pathlib import Path

import numpy as np
import pytest
from django.core.management import call_command

from stations.gazetteer import Gazetteer, get_gazetteer
from stations.loader import StationIndex, get_station_index
from stations.models import Station
from stations.normalize import in_conus

DATA = Path(__file__).resolve().parents[1] / "data"


def test_gazetteer_resolves_common_cities():
    gaz = get_gazetteer()
    assert len(gaz) > 30_000
    nyc = gaz.lookup("New York", "NY")
    assert nyc is not None and abs(nyc.lat - 40.7) < 0.2 and abs(nyc.lon + 73.9) < 0.2
    assert gaz.lookup("chicago", "Illinois") is not None
    assert gaz.lookup("St. Louis", "MO") is not None
    assert gaz.lookup("Saint Louis", "MO") is not None
    assert gaz.lookup("Boise", "ID") is not None  # GeoNames fill-in
    assert gaz.lookup("Nowhere Imaginary", "NY") is None
    assert gaz.lookup("Toronto", "ON") is None


def test_gazetteer_state_bounding_boxes_cover_capitals():
    boxes = get_gazetteer().state_bounding_boxes()
    lat_min, lat_max, lon_min, lon_max = boxes["CO"]
    assert lat_min <= 39.74 <= lat_max and lon_min <= -104.99 <= lon_max


def test_gazetteer_missing_file_has_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_gazetteer"):
        Gazetteer.from_csv(tmp_path / "nope.csv")


def test_stations_geocoded_csv_is_clean():
    with (DATA / "stations_geocoded.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) > 6000
    ids = [int(r["station_id"]) for r in rows]
    assert len(set(ids)) == len(ids), "station_id must be unique"
    for row in rows:
        assert in_conus(float(row["lat"]), float(row["lon"])), row
        assert 2.0 < float(row["price"]) < 7.0, row
        assert row["geocode_source"] in {"gazetteer", "nominatim"}
        assert row["city"] == row["city"].strip()
        assert "â€" not in row["name"], f"mojibake in station name: {row['name']!r}"
    assert any("Stuckey\u2019s" in r["name"] for r in rows), "the repaired apostrophe should be present"


def test_station_index_loads_arrays():
    index = get_station_index()
    assert len(index) > 6000
    assert index.lats.dtype == np.float64
    assert index.ids.shape == index.prices.shape == index.lats.shape == index.lons.shape
    assert 2.5 < float(np.median(index.prices)) < 4.0
    record = index.record(0)
    assert set(record) == {"station_id", "name", "address", "city", "state", "lat", "lon", "price"}


def test_station_index_missing_file_names_the_command(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_station_geodata"):
        StationIndex.from_csv(tmp_path / "missing.csv")


@pytest.mark.django_db
def test_load_stations_is_idempotent(tmp_path):
    sample = tmp_path / "sample.csv"
    sample.write_text(
        "station_id,name,address,city,state,price,lat,lon,geocode_source\n"
        "1,PILOT #1,I-80 EXIT 1,Somewhere,OH,3.199,41.0,-82.0,gazetteer\n"
        "2,LOVES #2,I-80 EXIT 2,Elsewhere,OH,3.099,41.1,-82.1,gazetteer\n",
        encoding="utf-8",
    )
    call_command("load_stations", input=sample)
    call_command("load_stations", input=sample)
    assert Station.objects.count() == 2
    assert str(Station.objects.get(pk=1).price) == "3.199"
