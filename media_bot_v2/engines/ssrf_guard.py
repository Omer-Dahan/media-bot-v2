"""SSRF protection shared by the direct-link engine and the provider
downloader (finding B).

Both routes fetch a URL supplied by an untrusted party - the end user for a
direct link, or a (possibly compromised/malicious) provider for the media
URL it hands back - and stream the response to the user as a "downloaded
file". Without this guard, either route can be pointed at loopback,
RFC1918/link-local (including the `169.254.169.254` cloud metadata address)
or other reserved ranges, turning the bot into a way to read internal
service responses.

Two things must both be validated, and re-validated on every redirect hop:
the scheme (only http/https) and every IP the host resolves to. Resolution
failure is treated as unsafe (fail-closed) rather than allowed through.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_REDIRECTS = 5


class SSRFBlockedError(Exception):
    """Raised when a URL's scheme or resolved address is disallowed."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _is_blocked_ip(mapped):
        return True
    sixtofour = getattr(ip, "sixtofour", None)
    return sixtofour is not None and _is_blocked_ip(sixtofour)


def _resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [literal]
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SSRFBlockedError(f"Could not resolve host {host!r}: {exc}") from exc
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = info[4][0]
        try:
            ips.append(ipaddress.ip_address(addr.split("%")[0]))
        except ValueError:
            continue
    if not ips:
        raise SSRFBlockedError(f"Could not resolve host {host!r} to any address")
    return ips


def assert_safe_url(url: str) -> None:
    """Raise SSRFBlockedError unless url's scheme is http(s) and every
    address its host resolves to is outside the blocked ranges."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"URL scheme {parsed.scheme!r} is not allowed")
    host = parsed.hostname
    if not host:
        raise SSRFBlockedError("URL has no host")
    for ip in _resolve_ips(host):
        if _is_blocked_ip(ip):
            raise SSRFBlockedError(f"URL host {host!r} resolves to disallowed address {ip}")


def safe_request(method: str, url: str, *, max_redirects: int = _MAX_REDIRECTS, **kwargs):
    """requests.request, but every hop (including redirect targets) is
    validated with assert_safe_url before it is connected to - a public URL
    that 302s to an internal address must not slip through a one-time check
    on the original URL alone."""
    kwargs.pop("allow_redirects", None)
    current_url = url
    for _ in range(max_redirects + 1):
        assert_safe_url(current_url)
        response = requests.request(method, current_url, allow_redirects=False, **kwargs)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise SSRFBlockedError(f"Redirect from {current_url} had no Location header")
            current_url = urljoin(current_url, location)
            continue
        return response
    raise SSRFBlockedError(f"Too many redirects (> {max_redirects}) starting from {url}")
