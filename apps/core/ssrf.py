"""SSRF-safe fetching of user-supplied URLs (spec §8.1, §20).

Fetching a URL the user typed is dangerous: it could point at our own database, the cloud metadata
service or an internal admin page. This module is the only way user URLs are fetched, and it:

- allows only http(s) on ports 80 and 443, with no credentials in the URL;
- resolves the host and requires **every** address to be public (no private, loopback, link-local,
  carrier-grade NAT, multicast, reserved or IPv4-mapped-private addresses; odd IP spellings such as
  `2130706433` or `0x7f.1` resolve to the same thing and are caught by the same check);
- **connects to the address it validated** (the request goes to the IP, with the original host name
  in the `Host` header and the TLS server name), so a DNS answer that changes between the check and
  the connection (DNS rebinding) cannot redirect us;
- follows redirects itself, at most 5, and validates every hop the same way;
- caps the response at 2 MB, the whole request at 20 seconds, and accepts only HTML-like content.

Nothing here logs the URL query string or page content.
"""

import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx
from django.conf import settings

from apps.core.logging import log_event
from apps.core.tls import shared_ssl_context

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 5
ALLOWED_PORTS = {80, 443}
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

Resolver = Callable[[str], list[str]]


class UnsafeURL(Exception):
    """The URL is not allowed. `reason` is a stable code; `message` is safe to show the user."""

    message = "That address cannot be used. Please paste the job description instead."

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FetchFailed(Exception):
    """The page could not be fetched (network error, bad status, too large, wrong type)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class FetchedPage:
    final_url: str
    text: str
    content_type: str


def _literal_address(host: str) -> str | None:
    """The IP address if `host` is a numeric literal, including the legacy IPv4 spellings
    (`2130706433`, `0x7f.1`, `0177.0.0.1`, `127.1`) that a resolver would silently accept. These are
    normalised here, without any DNS query."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


def system_resolver(host: str) -> list[str]:
    """All addresses the system resolver returns for `host` (IPv4 and IPv6)."""
    literal = _literal_address(host)
    if literal is not None:
        return [literal]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURL("dns_failure") from exc
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # ::ffff:127.0.0.1 is loopback in disguise
    return address.is_global and not address.is_multicast


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    parts: SplitResult
    hostname: str
    port: int
    address: str  # the validated IP we will connect to


def validate_public_url(url: str, resolver: Resolver | None = None) -> ValidatedURL:
    """Check a URL and return it with the validated address to connect to, or raise `UnsafeURL`."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("scheme")
    if not parts.hostname:
        raise UnsafeURL("no_host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURL("credentials")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("bad_port") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeURL("port")
    hostname = parts.hostname.rstrip(".").lower()
    if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise UnsafeURL("local_name")

    # Looked up at call time (not bound as a default) so tests can substitute the resolver.
    addresses = (resolver or system_resolver)(hostname)
    if not addresses:
        raise UnsafeURL("dns_failure")
    # Every answer must be public: a name that also resolves to a private address is refused,
    # because we cannot know which one the connection would use.
    if not all(is_public_address(a) for a in addresses):
        raise UnsafeURL("non_public_address")
    return ValidatedURL(
        url=url.strip(), parts=parts, hostname=hostname, port=port, address=addresses[0]
    )


def _pinned_request(
    client: httpx.Client, target: ValidatedURL, deadline: float
) -> tuple[httpx.Response, bytes]:
    """GET `target` by connecting to its validated IP, reading at most MAX_BYTES."""
    ip_host = f"[{target.address}]" if ":" in target.address else target.address
    default_port = 443 if target.parts.scheme == "https" else 80
    netloc = ip_host if target.port == default_port else f"{ip_host}:{target.port}"
    path = urlunsplit(("", "", target.parts.path or "/", target.parts.query, ""))
    pinned_url = f"{target.parts.scheme}://{netloc}{path}"
    host_header = (
        target.hostname if target.port == default_port else f"{target.hostname}:{target.port}"
    )
    extensions = {"sni_hostname": target.hostname} if target.parts.scheme == "https" else {}

    with client.stream(
        "GET", pinned_url, headers={"Host": host_header}, extensions=extensions
    ) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            raise FetchFailed("too_large", "That page is too large to read.")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise FetchFailed("too_large", "That page is too large to read.")
            if time.monotonic() > deadline:
                raise FetchFailed("timeout", "That page took too long to load.")
        return response, bytes(body)


def safe_fetch(
    url: str,
    *,
    resolver: Resolver | None = None,
    client: httpx.Client | None = None,
    before_request: Callable[[str], None] | None = None,
) -> FetchedPage:
    """Fetch a public web page. Raises `UnsafeURL` or `FetchFailed`; never returns unsafe pages."""
    owns_client = client is None
    client = client or httpx.Client(
        verify=shared_ssl_context(),
        headers={"User-Agent": settings.SCRAPER_USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
        timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS),
        follow_redirects=False,  # we follow redirects ourselves so every hop is validated
    )
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if before_request is not None:
                before_request(current)  # e.g. refuse blocked sites before any request is made
            target = validate_public_url(current, resolver)
            try:
                response, body = _pinned_request(client, target, deadline)
            except FetchFailed:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log_event(
                    logger, logging.WARNING, "manual_fetch_failed", "Manual URL fetch failed",
                    host=target.hostname, error=type(exc).__name__,
                )  # fmt: skip
                raise FetchFailed("network", "We could not reach that page.") from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise FetchFailed(
                        "bad_redirect", "That page redirected somewhere we cannot follow."
                    )
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                raise FetchFailed(
                    f"http_{response.status_code}",
                    "That page could not be loaded (it may be gone or need a login).",
                )
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith(ALLOWED_CONTENT_TYPES):
                raise FetchFailed("content_type", "That link is not a web page we can read.")
            encoding = response.charset_encoding or "utf-8"
            return FetchedPage(
                final_url=current,
                text=body.decode(encoding, errors="replace"),
                content_type=content_type,
            )
        raise FetchFailed("too_many_redirects", "That link redirects too many times.")
    finally:
        if owns_client:
            client.close()
