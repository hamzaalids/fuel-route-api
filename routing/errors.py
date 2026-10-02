"""Domain exceptions and their mapping to HTTP responses.

Services raise the typed exceptions below; the DRF exception handler turns them
into ``{"error": {"code": ..., "message": ...}}`` with the right status code.
"""

from __future__ import annotations

import logging

from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

log = logging.getLogger(__name__)


class ApiError(Exception):
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidRequest(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_request"


class LocationNotFound(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "location_not_found"


class OutsideUSA(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "outside_usa"


class NoRouteError(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "no_route"


class NoFeasiblePlan(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "no_feasible_fuel_plan"


class RoutingUnavailable(ApiError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "routing_unavailable"


class RoutingTimeout(RoutingUnavailable):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT


class GeocodingUnavailable(ApiError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "geocoding_unavailable"


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _flatten_validation_errors(detail) -> str:
    """Turn DRF's nested validation detail into one readable sentence."""
    if isinstance(detail, dict):
        parts = []
        for field, msgs in detail.items():
            text = _flatten_validation_errors(msgs)
            parts.append(text if field == "non_field_errors" else f"{field}: {text}")
        return "; ".join(parts)
    if isinstance(detail, list):
        return " ".join(_flatten_validation_errors(m) for m in detail)
    return str(detail)


def exception_handler(exc: Exception, context: dict) -> Response | None:
    if isinstance(exc, ApiError):
        return Response(_error_body(exc.code, exc.message), status=exc.status_code)

    if isinstance(exc, drf_exceptions.ValidationError):
        return Response(
            _error_body("invalid_request", _flatten_validation_errors(exc.detail)),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, drf_exceptions.ParseError):
        return Response(_error_body("invalid_request", str(exc.detail)), status=status.HTTP_400_BAD_REQUEST)

    response = drf_exception_handler(exc, context)
    if response is not None:
        code = getattr(exc, "default_code", "error")
        message = exc.detail if isinstance(getattr(exc, "detail", None), str) else str(exc)
        response.data = _error_body(str(code), str(message))
        return response

    log.exception("Unhandled error in %s", context.get("view"))
    return Response(
        _error_body("internal_error", "An unexpected error occurred."),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
