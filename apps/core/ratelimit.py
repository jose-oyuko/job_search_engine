"""Client-IP lookup for django-ratelimit when running behind the Caddy reverse proxy."""

from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str:
    """The address Caddy saw. Caddy overwrites `X-Forwarded-For` with the real client address, and
    we read the *last* entry (the one our own proxy appended) so a client cannot spoof it by
    sending its own header. Only configured in prod; locally REMOTE_ADDR is already the client."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")
