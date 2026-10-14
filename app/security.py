"""Request-origin checks for localhost trading controls.

These checks reduce browser-based CSRF and DNS-rebinding exposure. They are not
an authentication system and do not make the dashboard safe to expose publicly.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}


def _authority(value: str | None) -> tuple[str, int | None] | None:
    if not value:
        return None
    try:
        parsed = urlsplit("//" + value)
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path or parsed.query or parsed.fragment:
            return None
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not hostname:
            return None
        return hostname, parsed.port
    except ValueError:
        return None


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(scheme.lower())


def trading_control_denial_reason(
    *,
    client_host: str | None,
    host_header: str | None,
    origin: str | None,
    sec_fetch_site: str | None,
    request_scheme: str,
) -> str | None:
    """Return a fail-closed reason for an unsafe/non-local control request."""
    if (client_host or "").lower().rstrip(".") not in _LOCAL_CLIENTS:
        return "Trading-control endpoints are localhost-only."

    request_authority = _authority(host_header)
    if request_authority is None or request_authority[0] not in _LOCAL_HOSTS:
        return "Trading-control requests require a localhost Host header."

    if (sec_fetch_site or "").strip().lower() == "cross-site":
        return "Cross-site trading-control requests are blocked."

    if origin is not None:
        try:
            parsed_origin = urlsplit(origin.strip())
            origin_host = (parsed_origin.hostname or "").lower().rstrip(".")
            origin_port = parsed_origin.port
        except ValueError:
            return "Invalid Origin header for a trading-control request."

        if (
            parsed_origin.scheme.lower() not in {"http", "https"}
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or origin_host not in _LOCAL_HOSTS
        ):
            return "Cross-origin trading-control requests are blocked."

        if (
            parsed_origin.scheme.lower() != request_scheme.lower()
            or origin_host != request_authority[0]
            or _effective_port(parsed_origin.scheme, origin_port)
            != _effective_port(request_scheme, request_authority[1])
        ):
            return "Cross-origin trading-control requests are blocked."

    return None
