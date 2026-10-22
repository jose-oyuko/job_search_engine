"""Domain exceptions for sources. Raised by fetchers and the HTTP helper, handled in
`run_source`."""


class SourceError(Exception):
    """A source could not be read. The message goes to `SourceRun.errors`, so it must not contain
    secrets or page content: error codes and URL paths only."""


class RateLimited(SourceError):
    """The site answered HTTP 429. The run is marked degraded and backs off until the next tick."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class HTTPStatusFailure(SourceError):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"http_{status}:{path}")
        self.status = status
        self.path = path


class UnknownSourceKind(SourceError):
    """No fetcher is registered for this source's kind (or custom plugin name)."""


class SourceConfigError(SourceError):
    """The source's `config` is missing a key or has a bad value."""
