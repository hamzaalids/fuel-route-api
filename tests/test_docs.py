import yaml
from rest_framework.test import APIClient


def test_openapi_schema_generates_and_documents_route_endpoint():
    response = APIClient().get("/api/schema/")
    assert response.status_code == 200
    schema = yaml.safe_load(response.content)
    route = schema["paths"]["/api/v1/route/"]["post"]
    assert set(route["responses"]) >= {"200", "400", "404", "422", "502", "504"}
    assert "RouteRequest" in schema["components"]["schemas"]
    assert "RouteResponse" in schema["components"]["schemas"]
    assert "/api/v1/health/" in schema["paths"]


def test_swagger_ui_page_renders():
    response = APIClient().get("/api/docs/")
    assert response.status_code == 200
    assert b"swagger" in response.content.lower()


def test_home_page_links_to_map_json_and_docs():
    response = APIClient().get("/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "/api/v1/route/map/?start=New%20York%2C%20NY&amp;finish=Chicago%2C%20IL" in body
    assert "/api/v1/route/?start=New%20York%2C%20NY&amp;finish=Chicago%2C%20IL" in body
    assert 'href="/api/docs/"' in body
    assert "500-mile-range, 10&nbsp;MPG" in body


def test_schema_documents_get_variant_of_route():
    schema = yaml.safe_load(APIClient().get("/api/schema/").content)
    get = schema["paths"]["/api/v1/route/"]["get"]
    assert {p["name"] for p in get["parameters"]} == {"start", "finish"}
