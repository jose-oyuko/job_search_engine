"""Request correlation and access logging (spec §21.1)."""

import logging
import re
import time
import uuid
from collections.abc import Callable

import sentry_sdk
from django.http import HttpRequest, HttpResponse

from apps.core.logging import log_event, request_id_var, user_id_var

logger = logging.getLogger("apps.core.access")

REQUEST_ID_HEADER = "X-Request-ID"
# Only accept sane inbound IDs so a client cannot inject junk into our logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PREFIXES = ("/healthz", "/static/")


class RequestIDMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = inbound if _VALID_REQUEST_ID.match(inbound) else uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        uid_token = user_id_var.set(None)
        sentry_sdk.set_tag("request_id", request_id)
        started = time.monotonic()
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            if not request.path.startswith(_QUIET_PREFIXES):
                match = request.resolver_match
                log_event(
                    logger,
                    logging.INFO,
                    "http_request",
                    "Request handled",
                    method=request.method,
                    # Route template, not the raw path: paths can carry invite codes.
                    route=match.route if match else "unmatched",
                    status=response.status_code,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            return response
        finally:
            request_id_var.reset(rid_token)
            user_id_var.reset(uid_token)

    def process_view(
        self, request: HttpRequest, view_func: Callable[..., HttpResponse], *args: object
    ) -> None:
        """Runs after AuthenticationMiddleware, so the user is known from here on."""
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            user_id_var.set(user.pk)
