"""Request correlation."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"


class RequestIDMiddleware:
    """Attach a request id to every request/response pair.

    Accepts an upstream ``X-Request-ID`` when the load balancer supplies one so
    that a single identifier follows a request across the gateway, Django and
    the Celery task it enqueues.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.request_id = request.META.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response
