"""Per-Profile fixed-network configuration for the local browser manager.

The first product slice deliberately supports only direct connections and a
single fixed, credential-free proxy endpoint.  Chromium does not consume
credentials embedded in a manual ``--proxy-server`` setting, so accepting
username/password fields here would create a misleading and unsafe UI.
"""
from __future__ import annotations

import re
from typing import Any


PROXY_SCHEMES = {"http", "https", "socks5"}
PROXY_AUTHENTICATIONS = {"none", "basic"}
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PROXY_ARGUMENT_PREFIXES = (
    "--proxy-server",
    "--no-proxy-server",
    "--proxy-auto-detect",
    "--proxy-pac-url",
    "--proxy-bypass-list",
)


def normalize_network_config(value: Any) -> dict[str, Any]:
    """Return a strict, credential-free network configuration.

    ``None`` is accepted only for records created before this product module;
    it becomes the safe explicit default, ``direct``.
    """
    if value is None:
        return {"mode": "direct"}
    if not isinstance(value, dict):
        raise ValueError("network_config_must_be_an_object")
    mode = value.get("mode")
    if mode == "direct":
        if set(value) != {"mode"}:
            raise ValueError("direct_network_config_has_unknown_fields")
        return {"mode": "direct"}
    if mode != "fixed":
        raise ValueError("network_mode_must_be_direct_or_fixed")
    if set(value) - {"mode", "scheme", "host", "port", "authentication"}:
        raise ValueError("fixed_network_config_has_unknown_fields")
    scheme = value.get("scheme")
    host = value.get("host")
    port = value.get("port")
    authentication = value.get("authentication", "none")
    if not isinstance(scheme, str) or scheme not in PROXY_SCHEMES:
        raise ValueError("proxy_scheme_must_be_http_https_or_socks5")
    if not isinstance(host, str) or not HOST_RE.fullmatch(host) or ".." in host:
        raise ValueError("proxy_host_invalid")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("proxy_port_must_be_between_1_and_65535")
    if not isinstance(authentication, str) or authentication not in PROXY_AUTHENTICATIONS:
        raise ValueError("proxy_authentication_must_be_none_or_basic")
    if authentication == "basic" and scheme not in {"http", "https"}:
        raise ValueError("proxy_basic_authentication_requires_http_or_https")
    return {"mode": "fixed", "scheme": scheme, "host": host.lower(), "port": port, "authentication": authentication}


def proxy_launch_args(config: dict[str, Any]) -> list[str]:
    """Build the only proxy-related Chromium argument for one Profile.

    A fixed server list intentionally contains no ``direct://`` fallback.  If
    the configured endpoint is unavailable, Chromium must surface an error
    instead of silently changing the Profile's intended network route.
    """
    normalized = normalize_network_config(config)
    if normalized["mode"] == "direct":
        return ["--no-proxy-server"]
    return [f"--proxy-server={normalized['scheme']}://{normalized['host']}:{normalized['port']}"]


def without_proxy_arguments(arguments: list[str]) -> list[str]:
    """Remove global proxy flags so a Profile's network setting is authoritative."""
    return [item for item in arguments if not item.startswith(PROXY_ARGUMENT_PREFIXES)]
