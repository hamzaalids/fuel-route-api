from rest_framework.test import APIClient


def test_health_ok():
    response = APIClient().get("/api/v1/health/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["vehicle"]["max_range_miles"] == 500
    assert body["vehicle"]["mpg"] == 10
