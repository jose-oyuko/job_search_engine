"""The seam between manual job input and matching (spec §8.1, Phase 5 scope).

Matching arrives in Phase 6. Until then this is a no-op; once `apps.matching.services` exists and
provides `match_job_for_user(user, job)`, manual input starts using it without any change here.
"""

import importlib
import importlib.util
import logging
from typing import Any

from apps.core.logging import log_event

logger = logging.getLogger(__name__)

_MODULE = "apps.matching.services"
_FUNCTION = "match_job_for_user"


def job_added_for_user(user: Any, job: Any) -> bool:
    """Create and score a match for `user` and `job` if the matching service exists. Returns True
    if matching ran. TODO(phase 6): matching creates the Match row, scores it (counting towards the
    user's quota) and the job detail page shows the result."""
    try:
        if importlib.util.find_spec(_MODULE) is None:
            return False
    except ModuleNotFoundError:  # the parent package (apps.matching) does not exist yet
        return False
    function = getattr(importlib.import_module(_MODULE), _FUNCTION, None)
    if function is None:
        return False
    function(user, job)
    log_event(logger, logging.INFO, "manual_job_matched", "Manual job matched", job_id=job.pk)
    return True
