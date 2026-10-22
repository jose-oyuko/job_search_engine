"""Shared helpers for source tests: saved ExpertWoka pages and an HTTP mock for the site."""

from pathlib import Path

import httpx
import respx

FIXTURES = Path(__file__).parent / "fixtures" / "html" / "expertwoka"
BASE = "https://expertwoka.com"
LISTING_URL = f"{BASE}/opportunities"

# Which saved page belongs to which listing ID.
DETAIL_FIXTURES = {
    "analytical-chemist-mercor": "detail_mercor_global_no_jsonld.html",
    "sourcing/823": "detail_sourcing_823.html",
    "sms-data-collector-english-nigeria-productive-playhouse": "detail_nigeria_sms.html",
    "ai-benchmark-red-teaming-specialist-turing": "detail_turing_no_pay.html",
    "finance-expert-afterquery": "detail_afterquery_us_jsonld.html",
}
# The twelve IDs on the saved listing page, newest first.
LISTING_IDS = [
    "sourcing/823",
    "sourcing/822",
    "sourcing/821",
    "sourcing/820",
    "sms-data-collector-english-nigeria-productive-playhouse",
    "analytical-chemist-mercor",
    "litigation-associate-attorney-biglaw-firms-micro1",
    "ai-benchmark-red-teaming-specialist-turing",
    "computational-chemistry-researcher-task-author-afterquery",
    "finance-expert-afterquery",
    "finance-expert-talent-pool-vetto",
    "senior-software-engineer-task-ideation-vetto",
]


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def mock_listing(
    router: respx.MockRouter, html: str | None = None, status: int = 200
) -> respx.Route:
    body = page("listing_newest_page1.html") if html is None else html
    return router.get(LISTING_URL, params={"sort": "newest", "page": "1"}).mock(
        return_value=httpx.Response(status, text=body)
    )


def mock_details(
    router: respx.MockRouter, only: list[str] | None = None, fallback_status: int = 404
) -> dict[str, respx.Route]:
    """Mock detail pages: the saved fixtures, plus `fallback_status` for every other listing ID."""
    routes: dict[str, respx.Route] = {}
    for external_id in LISTING_IDS:
        if only is not None and external_id not in only:
            continue
        fixture = DETAIL_FIXTURES.get(external_id)
        response = (
            httpx.Response(200, text=page(fixture))
            if fixture
            else httpx.Response(fallback_status, text="not found")
        )
        routes[external_id] = router.get(f"{BASE}/opportunities/{external_id}").mock(
            return_value=response
        )
    return routes
