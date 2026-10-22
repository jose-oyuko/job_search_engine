"""HTTP access for fetchers (spec §6.3): one polite client, bounded retries, 429 awareness."""

import functools
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx
from django.conf import settings

from apps.core.logging import log_event

from .exceptions import HTTPStatusFailure, RateLimited, SourceError

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
MAX_BODY_BYTES = 5 * 1024 * 1024
_RETRYABLE = {500, 502, 503, 504}


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Loading the CA bundle takes about half a second, so do it once per process."""
    return ssl.create_default_context(cafile=certifi.where())


def build_client() -> httpx.Client:
    """One shared client per run, identifying itself with `SCRAPER_USER_AGENT`."""
    return httpx.Client(
        verify=_ssl_context(),
        headers={"User-Agent": settings.SCRAPER_USER_AGENT, "Accept-Language": "en"},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def fetch(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET (or `method`) `url`, retrying 5xx and timeouts up to twice with backoff.

    HTTP 429 raises `RateLimited` immediately (we never hammer a site that asked us to slow down).
    Other 4xx raise `HTTPStatusFailure`. Only the URL *path* is ever logged.
    """
    for attempt in range(MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        try:
            response = client.request(method, url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if last:
                log_event(
                    logger, logging.WARNING, "source_fetch_error", "Fetch failed",
                    path=_path(url), error=type(exc).__name__,
                )  # fmt: skip
                raise SourceError(f"network_error:{type(exc).__name__}:{_path(url)}") from exc
            sleep(float(2**attempt))
            continue
        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after", "")
            log_event(
                logger, logging.WARNING, "source_fetch_error", "Rate limited by site",
                path=_path(url), status=status,
            )  # fmt: skip
            raise RateLimited(
                float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None
            )
        if status in _RETRYABLE and not last:
            sleep(float(2**attempt))
            continue
        if status >= 400:
            log_event(
                logger, logging.WARNING, "source_fetch_error", "Fetch failed",
                path=_path(url), status=status,
            )  # fmt: skip
            raise HTTPStatusFailure(status, _path(url))
        if len(response.content) > MAX_BODY_BYTES:
            raise SourceError(f"response_too_large:{_path(url)}")
        return response
    raise AssertionError("unreachable")  # pragma: no cover
