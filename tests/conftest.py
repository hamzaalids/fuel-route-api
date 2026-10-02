import json
from pathlib import Path

import pytest
from django.core.cache import cache

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def osrm_body(distance_m: float, duration_s: float, coordinates: list[list[float]]) -> dict:
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_m,
                "duration": duration_s,
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [],
            }
        ],
        "waypoints": [],
    }


@pytest.fixture
def osrm_nyc_chicago() -> dict:
    """Recorded OSRM response for New York → Chicago (geometry simplified)."""
    return json.loads((FIXTURES / "osrm_nyc_chicago.json").read_text(encoding="utf-8"))
