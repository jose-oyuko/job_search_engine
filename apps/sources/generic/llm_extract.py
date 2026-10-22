"""`llm_extract` source: messy, low-volume sites with no structure (spec §6.4).

Config: `list_url`, `link_selector`, `link_pattern`, `max_new_per_run` (default 10).
Listing and link handling are the same as `jsonld`; each detail page is stripped to text and the
LLM returns the job fields (task `extract_page`, system-paid, cached per page content).
"""

import hashlib
from typing import Any
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from apps.core.sanitize import html_to_text
from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError
from apps.llm.models import LLMCall
from apps.llm.schemas import ExtractedPage
from apps.llm.untrusted import MAX_JOB_CHARS, wrap_untrusted

from ..base import RawJob
from ..exceptions import SourceError
from ..registry import register
from .jsonld import JsonLdSource


def page_text(html: str) -> str:
    """The visible text of a page, without scripts, styles, navigation or footers."""
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript, nav, footer, header, svg"):
        node.decompose()
    body = tree.body
    return html_to_text(body.html or "") if body is not None else ""


@register("llm_extract")
class LlmExtractSource(JsonLdSource):
    @property
    def max_new(self) -> int:
        configured = int(self.config.get("max_new_per_run", 10))
        return min(configured, self.limit) if self.limit is not None else configured

    def parse_detail(self, html: str, external_id: str, url: str) -> RawJob:
        text = page_text(html)
        path = urlsplit(url).path
        if len(text) < 40:
            raise SourceError(f"empty_page:{path}")
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        try:
            page = call_structured(
                task=LLMCall.Task.EXTRACT_PAGE,
                prompt_name="extract_page",
                variables={"page": wrap_untrusted("page", text, MAX_JOB_CHARS)},
                schema=ExtractedPage,
                user=None,
                cache_key=f"page:{self.source.name}:{external_id}:{digest}",
            )
        except LLMError as exc:
            raise SourceError(f"llm_error:{type(exc).__name__}:{path}") from exc
        if not page.is_job_posting or not page.title:
            raise SourceError(f"not_a_job_posting:{path}")
        apply_url: Any = page.apply_url or ""
        return RawJob(
            source=self.source.name,
            external_id=external_id,
            url=url,
            title=page.title,
            company=page.company or str(self.config.get("company", "")),
            apply_url=str(apply_url),
            description_text=page.description,
            pay_text=page.pay_text or "",
            locations=page.locations,
            work_type="" if page.work_type == "unknown" else page.work_type,
            employment_type=page.employment_type or "",
            raw={"extracted_by": "llm", "text_sha": digest},
        )
