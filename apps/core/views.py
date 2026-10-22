import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from apps.core.logging import log_event

logger = logging.getLogger(__name__)


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """Shallow check: process is up and the database answers. `?deep=1` is staff-only."""
    try:
        get_user_model().objects.order_by().values("pk")[:1].exists()
    except Exception:  # noqa: BLE001 - any DB failure means "not healthy"
        log_event(
            logger, logging.CRITICAL, "healthz_db_unreachable", "Database check failed",
            exc_info=True,
        )  # fmt: skip
        return JsonResponse({"status": "error", "detail": "database"}, status=503)

    if request.GET.get("deep"):
        user = getattr(request, "user", None)
        if user is None or not user.is_staff:
            return JsonResponse({"status": "forbidden"}, status=403)
        return _deep_check()
    return JsonResponse({"status": "ok"})


def _deep_check() -> JsonResponse:
    from huey.contrib.djhuey import HUEY

    checks: dict[str, Any] = {}
    try:
        checks["huey_pending"] = HUEY.pending_count()
    except Exception:  # noqa: BLE001
        log_event(logger, logging.ERROR, "healthz_huey_failed", "Huey check failed", exc_info=True)
        return JsonResponse({"status": "error", "detail": "huey"}, status=503)
    # TODO(phase 12): also verify the heartbeat is recent.
    return JsonResponse({"status": "ok", **checks})
