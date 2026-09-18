"""Uniform API error envelope.

Mobile clients parse errors programmatically, so every failure -- validation,
permission, domain rule or unhandled crash -- leaves the API in the same
shape::

    {"error": {"code": "...", "message": "...", "details": {...},
               "request_id": "..."}}
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class DomainError(APIException):
    """A business rule was violated.

    Distinct from a validation error: the payload was well formed, but the
    action is not permissible given the user's financial state (for example,
    a payoff budget below the sum of minimum payments).
    """

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "The request is valid but violates a business rule."
    default_code = "domain_error"

    def __init__(
        self,
        detail: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail=detail, code=code)
        self.details = details or {}


class InsufficientBudgetError(DomainError):
    default_detail = "Monthly budget does not cover the sum of minimum payments."
    default_code = "insufficient_budget"


class NoEligibleProductsError(DomainError):
    status_code = status.HTTP_200_OK
    default_detail = "No partner products match this profile."
    default_code = "no_eligible_products"


def nuvix_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """DRF exception hook that normalises every error into one envelope."""

    if isinstance(exc, DjangoValidationError):
        exc = APIException(detail=exc.messages, code="validation_error")
        exc.status_code = status.HTTP_400_BAD_REQUEST

    response = drf_exception_handler(exc, context)

    request = context.get("request")
    request_id = getattr(request, "request_id", None)

    if response is None:
        logger.exception("unhandled_exception", extra={"request_id": request_id})
        return Response(
            {
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred.",
                    "details": {},
                    "request_id": request_id,
                }
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # The per-raise code, not the class default. DRF stores the ``code=``
    # argument on the ErrorDetail rather than on the exception, so reading
    # ``default_code`` would collapse every DomainError subclass down to
    # "domain_error" and make the specific codes clients switch on useless.
    detail = response.data
    code = getattr(getattr(exc, "detail", None), "code", None) or getattr(
        exc, "default_code", "error"
    )

    if isinstance(exc, Http404):
        code, message, details = "not_found", "Resource not found.", {}
    elif isinstance(exc, PermissionDenied):
        code, message, details = "permission_denied", str(exc.detail), {}
    elif isinstance(detail, dict) and "detail" in detail:
        message, details = str(detail["detail"]), getattr(exc, "details", {})
    elif isinstance(detail, dict):
        message, details = "Request validation failed.", detail
        code = "validation_error"
    else:
        message, details = str(detail), {}

    response.data = {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id,
        }
    }
    return response
