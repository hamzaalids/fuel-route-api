"""Thin views: validate, delegate to the planner, respond."""

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import ApiError
from .serializers import ErrorResponseSerializer, RouteRequestSerializer, RouteResponseSerializer
from .services.planner import plan_route

EXAMPLES = [
    ("New York, NY", "Chicago, IL"),
    ("Los Angeles, CA", "Dallas, TX"),
    ("Miami, FL", "Seattle, WA"),
    ("Denver, CO", "Colorado Springs, CO"),
]

_ERROR_RESPONSES = {
    400: ErrorResponseSerializer,
    404: ErrorResponseSerializer,
    422: ErrorResponseSerializer,
    502: ErrorResponseSerializer,
    504: ErrorResponseSerializer,
}


class HealthView(APIView):
    """Liveness probe. Also reports the vehicle configuration in effect."""

    @extend_schema(responses={200: dict}, summary="Health check")
    def get(self, request: Request) -> Response:
        return Response(
            {
                "status": "ok",
                "vehicle": {"max_range_miles": settings.MAX_RANGE_MILES, "mpg": settings.MPG},
            }
        )


class RouteView(APIView):
    """Route between two US locations with cost-optimal fuel stops."""

    @extend_schema(
        request=RouteRequestSerializer,
        responses={200: RouteResponseSerializer, **_ERROR_RESPONSES},
        examples=[OpenApiExample("NYC to Chicago", value={"start": "New York, NY", "finish": "Chicago, IL"})],
        summary="Plan a route with optimal fuel stops",
    )
    def post(self, request: Request) -> Response:
        return self._plan(request.data)

    @extend_schema(
        parameters=[
            OpenApiParameter("start", str, required=True, description="e.g. New York, NY"),
            OpenApiParameter("finish", str, required=True, description="e.g. Chicago, IL"),
        ],
        responses={200: RouteResponseSerializer, **_ERROR_RESPONSES},
        summary="Same as POST, with query parameters (browser-friendly)",
    )
    def get(self, request: Request) -> Response:
        return self._plan({"start": request.GET.get("start", ""), "finish": request.GET.get("finish", "")})

    @staticmethod
    def _plan(data: dict) -> Response:
        serializer = RouteRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        payload = plan_route(serializer.validated_data["start"], serializer.validated_data["finish"])
        return Response(payload)


def home(request: HttpRequest) -> HttpResponse:
    """Landing page: a start/finish form plus example links to the map, JSON and Swagger docs."""
    vehicle = {"max_range_miles": settings.MAX_RANGE_MILES, "mpg": settings.MPG}
    return render(request, "routing/home.html", {"examples": EXAMPLES, "vehicle": vehicle})


def route_map(request: HttpRequest) -> HttpResponse:
    """Leaflet page for the same plan (served from cache when the POST was just made)."""
    serializer = RouteRequestSerializer(
        data={"start": request.GET.get("start", ""), "finish": request.GET.get("finish", "")}
    )
    if not serializer.is_valid():
        message = "; ".join(f"{k}: {' '.join(map(str, v))}" for k, v in serializer.errors.items())
        return render(request, "routing/map.html", {"error": message, "status": 400}, status=400)
    try:
        payload = plan_route(serializer.validated_data["start"], serializer.validated_data["finish"])
    except ApiError as exc:
        return render(
            request,
            "routing/map.html",
            {"error": f"{exc.code}: {exc.message}", "status": exc.status_code},
            status=exc.status_code,
        )
    return render(request, "routing/map.html", {"plan": payload})
