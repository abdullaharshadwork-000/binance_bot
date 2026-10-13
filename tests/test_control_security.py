import pytest

from app.security import trading_control_denial_reason


def check(
    *,
    client_host="127.0.0.1",
    host="127.0.0.1:8000",
    origin="http://127.0.0.1:8000",
    sec_fetch_site="same-origin",
    scheme="http",
):
    return trading_control_denial_reason(
        client_host=client_host,
        host_header=host,
        origin=origin,
        sec_fetch_site=sec_fetch_site,
        request_scheme=scheme,
    )


def test_same_origin_loopback_request_is_allowed():
    assert check() is None


def test_loopback_command_line_request_without_browser_headers_is_allowed():
    assert check(origin=None, sec_fetch_site=None) is None


def test_local_client_with_cross_origin_is_blocked():
    assert "Cross-origin" in check(origin="https://attacker.example")


def test_cross_site_fetch_is_blocked_even_if_origin_is_missing():
    assert "Cross-site" in check(origin=None, sec_fetch_site="cross-site")


def test_non_loopback_client_is_blocked_even_with_local_origin():
    assert "localhost-only" in check(client_host="203.0.113.8")


def test_non_local_host_is_blocked_to_prevent_dns_rebinding():
    assert "Host header" in check(host="attacker.example:8000", origin="http://attacker.example:8000")


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://localhost:8001",
        "https://127.0.0.1:8000",
        "http://localhost:invalid",
        "http://user@127.0.0.1:8000",
    ],
)
def test_mismatched_or_invalid_origin_is_blocked(origin):
    assert trading_control_denial_reason(
        client_host="127.0.0.1",
        host_header="127.0.0.1:8000",
        origin=origin,
        sec_fetch_site="same-origin",
        request_scheme="http",
    ) is not None


def test_same_hostname_and_default_port_origin_is_allowed():
    assert trading_control_denial_reason(
        client_host="::1",
        host_header="[::1]",
        origin="http://[::1]",
        sec_fetch_site="same-origin",
        request_scheme="http",
    ) is None
