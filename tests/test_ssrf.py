"""SSRF protection for user-supplied URLs (spec §8.1, §20). No real network: names are resolved by
injected fakes and HTTP by respx. Numeric-IP cases use the system resolver, which needs no network."""

import time

import httpx
import pytest
import respx

from apps.core import ssrf
from apps.core.ssrf import (
    FetchFailed,
    UnsafeURL,
    is_public_address,
    safe_fetch,
    validate_public_url,
)

PUBLIC = "93.184.216.34"
PUBLIC2 = "151.101.1.69"


def resolves_to(*addresses: str):  # type: ignore[no-untyped-def]
    return lambda host: list(addresses)


def by_host(mapping: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    def resolver(host: str) -> list[str]:
        try:
            return mapping[host]
        except KeyError:
            raise UnsafeURL("dns_failure") from None

    return resolver


# --- address classification ---------------------------------------------------------------------------------


@pytest.mark.parametrize("address", [PUBLIC, PUBLIC2, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_public(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.1.2.3", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255",
        "192.168.0.1", "169.254.169.254", "169.254.0.1", "0.0.0.0", "100.64.0.1", "100.127.255.255",
        "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255", "192.0.2.1", "198.51.100.7",
        "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
        "not-an-ip", "", "999.1.1.1",
    ],
)  # fmt: skip
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public_address(address)


# --- URL validation ----------------------------------------------------------------------------------------------


def test_a_normal_url_passes_and_returns_the_pinned_address() -> None:
    target = validate_public_url("https://Jobs.Example.com/careers/1?a=1", resolves_to(PUBLIC))
    assert (target.hostname, target.port, target.address) == ("jobs.example.com", 443, PUBLIC)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("gopher://example.com/", "scheme"),
        ("data:text/html,hi", "scheme"),
        ("//example.com/x", "scheme"),
        ("http://", "no_host"),
        ("https:///path", "no_host"),
        ("https://user:pw@example.com/", "credentials"),
        ("https://user@example.com/", "credentials"),
        ("https://example.com@evil.example/", "credentials"),
        ("https://example.com:22/", "port"),
        ("http://example.com:6379/", "port"),
        ("http://example.com:8080/", "port"),
        ("https://example.com:99999/", "bad_port"),
        ("http://localhost/admin", "local_name"),
        ("http://LOCALHOST./admin", "local_name"),
        ("http://api.localhost/", "local_name"),
        ("http://printer.local/", "local_name"),
        ("http://db.internal/", "local_name"),
    ],
)
def test_structurally_unsafe_urls_are_refused(url: str, reason: str) -> None:
    with pytest.raises(UnsafeURL) as info:
        validate_public_url(url, resolves_to(PUBLIC))
    assert info.value.reason == reason


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:80/admin",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://224.0.0.1/",
        "http://2130706433/",  # 127.0.0.1 as one decimal number
        "http://0x7f000001/",  # hexadecimal
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",  # octal
        "http://127.1/",  # shortened
    ],
)
def test_private_and_loopback_addresses_are_refused_with_the_real_resolver(url: str) -> None:
    with pytest.raises(UnsafeURL) as info:
        validate_public_url(url)  # the system resolver, which resolves literal IPs offline
    assert info.value.reason in {"non_public_address", "dns_failure"}


def test_a_hostname_that_resolves_to_a_private_address_is_refused() -> None:
    with pytest.raises(UnsafeURL) as info:
        validate_public_url("https://rebind.example/job", resolves_to("127.0.0.1"))
    assert info.value.reason == "non_public_address"


def test_a_name_with_any_private_answer_is_refused() -> None:
    with pytest.raises(UnsafeURL):
        validate_public_url("https://mixed.example/", resolves_to(PUBLIC, "10.0.0.9"))
    with pytest.raises(UnsafeURL):
        validate_public_url("https://mixed.example/", resolves_to("::1", PUBLIC))


def test_unresolvable_names_are_refused() -> None:
    with pytest.raises(UnsafeURL, match="dns_failure"):
        validate_public_url("https://nowhere.example/", resolves_to())
    with pytest.raises(UnsafeURL):
        validate_public_url("https://nowhere.example/", by_host({}))


def test_the_system_resolver_resolves_localhost_to_loopback_which_is_refused() -> None:
    assert is_public_address("127.0.0.1") is False
    with pytest.raises(UnsafeURL):
        validate_public_url("https://localhost/")


def test_a_public_literal_ip_is_allowed() -> None:
    target = validate_public_url(f"http://{PUBLIC}/job")
    assert target.address == PUBLIC and target.port == 80


def test_the_user_facing_message_never_names_the_reason() -> None:
    assert "paste the job description" in UnsafeURL.message
    assert "127" not in UnsafeURL.message and "private" not in UnsafeURL.message.lower()


# --- fetching ------------------------------------------------------------------------------------------------------


HTML = "<html><body><h1>Support Engineer</h1></body></html>"


def test_fetch_pins_the_connection_to_the_validated_address(router: respx.MockRouter) -> None:
    route = router.get(f"https://{PUBLIC}/careers/1", params={"x": "1"}).mock(
        return_value=httpx.Response(
            200, text=HTML, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    page = safe_fetch("https://jobs.example.com/careers/1?x=1", resolver=resolves_to(PUBLIC))

    request = route.calls.last.request
    assert request.headers["host"] == "jobs.example.com"  # the name, for the virtual host
    assert request.extensions["sni_hostname"] == "jobs.example.com"  # and for TLS verification
    assert str(request.url).startswith(f"https://{PUBLIC}/")
    assert (page.text, page.final_url, page.content_type) == (
        HTML,
        "https://jobs.example.com/careers/1?x=1",
        "text/html",
    )


def test_http_urls_use_port_80_and_no_sni(router: respx.MockRouter) -> None:
    route = router.get(f"http://{PUBLIC}/x").mock(return_value=httpx.Response(200, text=HTML))
    safe_fetch("http://plain.example/x", resolver=resolves_to(PUBLIC))
    request = route.calls.last.request
    assert request.headers["host"] == "plain.example" and "sni_hostname" not in request.extensions


def test_ipv6_addresses_are_bracketed(router: respx.MockRouter) -> None:
    route = router.get("https://[2606:4700:4700::1111]/x").mock(
        return_value=httpx.Response(200, text=HTML)
    )
    safe_fetch("https://v6.example/x", resolver=resolves_to("2606:4700:4700::1111"))
    assert route.called


def test_non_default_allowed_port_is_kept_in_the_host_header(router: respx.MockRouter) -> None:
    route = router.get(f"https://{PUBLIC}:80/x").mock(return_value=httpx.Response(200, text=HTML))
    safe_fetch("https://odd.example:80/x", resolver=resolves_to(PUBLIC))
    assert route.calls.last.request.headers["host"] == "odd.example:80"


def test_a_private_target_makes_no_request_at_all(router: respx.MockRouter) -> None:
    route = router.get(url__startswith="http").mock(return_value=httpx.Response(200, text=HTML))
    with pytest.raises(UnsafeURL):
        safe_fetch("https://rebind.example/job", resolver=resolves_to("127.0.0.1"))
    assert not route.called


def test_request_carries_no_credentials_and_identifies_us(
    router: respx.MockRouter, settings
) -> None:  # type: ignore[no-untyped-def]
    route = router.get(f"https://{PUBLIC}/x").mock(return_value=httpx.Response(200, text=HTML))
    safe_fetch("https://a.example/x", resolver=resolves_to(PUBLIC))
    headers = route.calls.last.request.headers
    assert headers["user-agent"] == settings.SCRAPER_USER_AGENT
    assert "authorization" not in headers and "cookie" not in headers


# --- redirects ---------------------------------------------------------------------------------------------------------


def test_redirects_are_followed_and_each_hop_is_pinned(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/old").mock(
        return_value=httpx.Response(301, headers={"location": "https://new.example/job"})
    )
    final = router.get(f"https://{PUBLIC2}/job").mock(return_value=httpx.Response(200, text=HTML))
    resolver = by_host({"old.example": [PUBLIC], "new.example": [PUBLIC2]})
    page = safe_fetch("https://old.example/old", resolver=resolver)
    assert page.final_url == "https://new.example/job" and final.called


def test_relative_redirects_work(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/a").mock(
        return_value=httpx.Response(302, headers={"location": "/b?x=1"})
    )
    router.get(f"https://{PUBLIC}/b").mock(return_value=httpx.Response(200, text=HTML))
    assert (
        safe_fetch("https://h.example/a", resolver=resolves_to(PUBLIC)).final_url
        == "https://h.example/b?x=1"
    )


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:80/",
        "http://10.0.0.1/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "https://user:pw@elsewhere.example/",
        "https://elsewhere.example:22/",
    ],
)
def test_a_redirect_to_an_unsafe_target_is_refused(router: respx.MockRouter, location: str) -> None:
    router.get(f"https://{PUBLIC}/start").mock(
        return_value=httpx.Response(302, headers={"location": location})
    )
    private = router.get(url__regex=r"http://(127|10|169|\[::1)").mock(
        return_value=httpx.Response(200, text="SECRET")
    )
    resolver = by_host({"start.example": [PUBLIC], "elsewhere.example": [PUBLIC2]})
    with pytest.raises(UnsafeURL):
        safe_fetch("https://start.example/start", resolver=resolver)
    assert not private.called


def test_dns_rebinding_between_hops_is_caught(router: respx.MockRouter) -> None:
    """The first hop is public; the redirect names a host whose DNS now answers with a private IP."""
    answers = {"first.example": [PUBLIC], "second.example": ["127.0.0.1"]}
    router.get(f"https://{PUBLIC}/go").mock(
        return_value=httpx.Response(302, headers={"location": "https://second.example/x"})
    )
    with pytest.raises(UnsafeURL) as info:
        safe_fetch("https://first.example/go", resolver=by_host(answers))
    assert info.value.reason == "non_public_address"


def test_the_same_name_is_validated_again_on_every_hop(router: respx.MockRouter) -> None:
    """A name that is public at first and private later (rebinding) is refused on the second look."""
    calls = {"n": 0}

    def flipping(host: str) -> list[str]:
        calls["n"] += 1
        return [PUBLIC] if calls["n"] == 1 else ["10.0.0.1"]

    router.get(f"https://{PUBLIC}/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/again"})
    )
    with pytest.raises(UnsafeURL):
        safe_fetch("https://flip.example/loop", resolver=flipping)
    assert calls["n"] == 2


def test_too_many_redirects(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/loop"})
    )
    with pytest.raises(FetchFailed) as info:
        safe_fetch("https://loop.example/loop", resolver=resolves_to(PUBLIC))
    assert info.value.reason == "too_many_redirects"


def test_a_redirect_without_a_location_is_an_error(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/x").mock(return_value=httpx.Response(302))
    with pytest.raises(FetchFailed, match="bad_redirect"):
        safe_fetch("https://h.example/x", resolver=resolves_to(PUBLIC))


# --- size, type, status, time --------------------------------------------------------------------------------------------


def test_a_declared_oversize_body_is_refused_before_reading(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/big").mock(
        return_value=httpx.Response(
            200, content=b"x", headers={"content-length": str(ssrf.MAX_BYTES + 1)}
        )
    )
    with pytest.raises(FetchFailed) as info:
        safe_fetch("https://big.example/big", resolver=resolves_to(PUBLIC))
    assert info.value.reason == "too_large"


def test_an_oversize_body_without_a_length_is_cut_off(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/big").mock(
        return_value=httpx.Response(200, content=b"a" * (ssrf.MAX_BYTES + 10))
    )
    with pytest.raises(FetchFailed, match="too_large"):
        safe_fetch("https://big.example/big", resolver=resolves_to(PUBLIC))


def test_a_body_exactly_at_the_limit_is_accepted(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/ok").mock(
        return_value=httpx.Response(200, content=b"a" * ssrf.MAX_BYTES)
    )
    assert (
        len(safe_fetch("https://ok.example/ok", resolver=resolves_to(PUBLIC)).text)
        == ssrf.MAX_BYTES
    )


@pytest.mark.parametrize(
    "content_type",
    ["image/png", "application/json", "application/pdf", "application/octet-stream", "video/mp4"],
)
def test_non_web_content_types_are_refused(router: respx.MockRouter, content_type: str) -> None:
    router.get(f"https://{PUBLIC}/x").mock(
        return_value=httpx.Response(200, content=b"data", headers={"content-type": content_type})
    )
    with pytest.raises(FetchFailed) as info:
        safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC))
    assert info.value.reason == "content_type"


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "text/html; charset=iso-8859-1",
        "application/xhtml+xml",
        "text/plain",
        "TEXT/HTML",
    ],
)
def test_web_content_types_are_accepted(router: respx.MockRouter, content_type: str) -> None:
    router.get(f"https://{PUBLIC}/x").mock(
        return_value=httpx.Response(
            200, content=b"<p>ok</p>", headers={"content-type": content_type}
        )
    )
    assert safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC)).text == "<p>ok</p>"


def test_a_missing_content_type_is_tolerated(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/x").mock(return_value=httpx.Response(200, content=b"<p>ok</p>"))
    assert safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC)).text == "<p>ok</p>"


def test_the_declared_charset_is_used_and_bad_bytes_are_replaced(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/x").mock(
        return_value=httpx.Response(
            200,
            content="café".encode("latin-1"),
            headers={"content-type": "text/html; charset=iso-8859-1"},
        )
    )
    assert safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC)).text == "café"
    router.get(f"https://{PUBLIC}/y").mock(
        return_value=httpx.Response(
            200, content=b"ok \xff\xfe", headers={"content-type": "text/html"}
        )
    )
    assert safe_fetch("https://t.example/y", resolver=resolves_to(PUBLIC)).text.startswith("ok ")


@pytest.mark.parametrize("status", [401, 403, 404, 410, 500, 503])
def test_error_statuses_are_friendly_failures(router: respx.MockRouter, status: int) -> None:
    router.get(f"https://{PUBLIC}/x").mock(return_value=httpx.Response(status, text="boom"))
    with pytest.raises(FetchFailed) as info:
        safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC))
    assert info.value.reason == f"http_{status}" and "boom" not in info.value.message


def test_network_errors_are_friendly_failures(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/x").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FetchFailed) as info:
        safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC))
    assert info.value.reason == "network"
    router.get(f"https://{PUBLIC}/y").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchFailed, match="network"):
        safe_fetch("https://t.example/y", resolver=resolves_to(PUBLIC))


def test_the_whole_request_has_a_time_limit(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    router.get(f"https://{PUBLIC}/slow").mock(return_value=httpx.Response(200, content=b"a" * 1000))
    start = time.monotonic()
    ticks = iter(
        [start, start + 1000, start + 1000, start + 1000]
    )  # the deadline, then far past it
    monkeypatch.setattr(ssrf.time, "monotonic", lambda: next(ticks))
    with pytest.raises(FetchFailed) as info:
        safe_fetch("https://slow.example/slow", resolver=resolves_to(PUBLIC))
    assert info.value.reason == "timeout"


def test_an_injected_client_is_not_closed(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/x").mock(return_value=httpx.Response(200, text=HTML))
    with httpx.Client() as client:
        safe_fetch("https://t.example/x", resolver=resolves_to(PUBLIC), client=client)
        assert not client.is_closed


# --- the per-hop hook -------------------------------------------------------------------------------------------------


def test_before_request_runs_for_every_hop_and_can_stop_the_fetch(router: respx.MockRouter) -> None:
    router.get(f"https://{PUBLIC}/a").mock(
        return_value=httpx.Response(302, headers={"location": "https://other.example/b"})
    )
    final = router.get(f"https://{PUBLIC}/b").mock(return_value=httpx.Response(200, text=HTML))
    seen: list[str] = []

    class Stop(Exception):
        pass

    def hook(url: str) -> None:
        seen.append(url)
        if "other.example" in url:
            raise Stop

    with pytest.raises(Stop):
        safe_fetch("https://first.example/a", resolver=resolves_to(PUBLIC), before_request=hook)
    assert seen == ["https://first.example/a", "https://other.example/b"] and not final.called
