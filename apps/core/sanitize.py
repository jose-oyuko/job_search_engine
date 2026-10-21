"""nh3 wrappers for third-party HTML (spec §20). Never use `|safe` on untrusted content."""

import nh3

ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "a",
    "h1", "h2", "h3", "h4", "blockquote", "code", "pre",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {"a": {"href"}}
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def clean_html(html: str) -> str:
    """Keep a small set of formatting tags; drop scripts, styles, handlers and unsafe URLs."""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )


def strip_html(html: str) -> str:
    """Remove all markup, returning plain text."""
    return nh3.clean(html, tags=set())
