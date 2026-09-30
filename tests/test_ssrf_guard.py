"""Unit tests for media_bot_v2.engines.ssrf_guard (finding B).

These test the guard directly, with no network and no local HTTP server:
resolution is either a literal IP (no DNS involved) or a monkeypatched
`socket.getaddrinfo`, so the assertions are deterministic and cannot be
affected by real-world DNS or connectivity.
"""

from __future__ import annotations

import socket

import pytest

from media_bot_v2.engines.ssrf_guard import (
    SSRFBlockedError,
    _is_blocked_ip,
    assert_safe_url,
    safe_request,
)

_BLOCKED_URLS = [
    "http://127.0.0.1/",  # loopback
    "http://127.0.0.1:8080/admin",
    "http://[::1]/",  # loopback (IPv6)
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata / link-local
    "http://[fe80::1]/",  # link-local (IPv6)
    "http://10.0.0.5/",  # private (RFC1918)
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://[fc00::1]/",  # unique-local (IPv6)
    "http://224.0.0.1/",  # multicast
    "http://0.0.0.0/",  # unspecified
    "http://[::]/",  # unspecified (IPv6)
]


@pytest.mark.parametrize("url", _BLOCKED_URLS)
def test_blocked_ranges_are_rejected(url):
    with pytest.raises(SSRFBlockedError):
        assert_safe_url(url)


@pytest.mark.parametrize(
    "scheme_url",
    ["file:///etc/passwd", "ftp://93.184.216.34/", "gopher://93.184.216.34/", "data:text/plain,hi"],
)
def test_disallowed_schemes_are_rejected(scheme_url):
    with pytest.raises(SSRFBlockedError):
        assert_safe_url(scheme_url)


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/",  # example.com's public IP, used literally (no DNS)
        "https://8.8.8.8/",
        "http://1.1.1.1:443/",
    ],
)
def test_public_addresses_are_allowed(url):
    assert_safe_url(url)  # must not raise


def test_unresolvable_host_fails_closed(monkeypatch):
    def _boom(host, port):
        raise OSError("simulated DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(SSRFBlockedError):
        assert_safe_url("http://this-host-does-not-resolve.invalid/")


def test_hostname_resolving_to_blocked_ip_is_rejected(monkeypatch):
    """A public-looking hostname whose DNS answer is an internal address
    must be blocked - the guard checks the resolved IP, not the hostname
    string, so a name alone can't be used to bypass it."""

    def _fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(SSRFBlockedError):
        assert_safe_url("http://looks-public.example.test/")


def test_ipv4_mapped_ipv6_loopback_is_blocked():
    assert _is_blocked_ip(__import__("ipaddress").ip_address("::ffff:127.0.0.1"))


def test_safe_request_blocks_redirect_to_internal_address(monkeypatch):
    """A URL that itself resolves to a public address but 302s to an
    internal one must be blocked on the redirect hop, not just the first
    request - a one-time check on the original URL is not enough."""
    calls = []

    class _FakeResponse:
        def __init__(self, status_code, location=None):
            self.status_code = status_code
            self.headers = {"Location": location} if location else {}
            self.is_redirect = 300 <= status_code < 400 and location is not None
            self.is_permanent_redirect = False

        def close(self):
            pass

    def _fake_request(method, url, *, allow_redirects, **kwargs):
        calls.append(url)
        if url == "http://93.184.216.34/start":
            return _FakeResponse(302, "http://169.254.169.254/latest/meta-data/")
        raise AssertionError(f"should never reach {url}")

    monkeypatch.setattr("media_bot_v2.engines.ssrf_guard.requests.request", _fake_request)

    with pytest.raises(SSRFBlockedError):
        safe_request("GET", "http://93.184.216.34/start")

    # The redirect target must never have been requested.
    assert calls == ["http://93.184.216.34/start"]


def test_safe_request_stops_after_max_redirects(monkeypatch):
    calls = []

    class _FakeResponse:
        def __init__(self):
            self.status_code = 302
            self.headers = {"Location": "http://93.184.216.34/next"}
            self.is_redirect = True
            self.is_permanent_redirect = False

        def close(self):
            pass

    def _fake_request(method, url, *, allow_redirects, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr("media_bot_v2.engines.ssrf_guard.requests.request", _fake_request)

    with pytest.raises(SSRFBlockedError):
        safe_request("GET", "http://93.184.216.34/start", max_redirects=2)

    assert len(calls) == 3  # initial + 2 redirects, then give up
