"""One shared TLS context for outbound HTTP clients."""

import functools
import ssl

import certifi


@functools.lru_cache(maxsize=1)
def shared_ssl_context() -> ssl.SSLContext:
    """Loading the CA bundle takes about half a second, so do it once per process."""
    return ssl.create_default_context(cafile=certifi.where())
