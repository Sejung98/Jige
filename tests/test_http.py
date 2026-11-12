"""Shared HTTP client: per-host request spacing and Retry-After handling."""

import httpx
import pytest
import respx

from dataops_mcp import http

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(http, "MIN_INTERVAL", {"eutils.ncbi.nlm.nih.gov": (0.34, 0.11)})
    monkeypatch.setattr(http, "_last_call", {})
    return FakeClock()


def test_throttle_spaces_ncbi_requests_without_key(clock):
    http.throttle(EUTILS, {"db": "sra"}, clock.sleep, clock.clock)
    clock.now += 0.1
    http.throttle(EUTILS, {"db": "sra"}, clock.sleep, clock.clock)
    assert clock.sleeps == [0.24]


def test_throttle_shorter_with_api_key(clock):
    http.throttle(EUTILS, {"api_key": "k"}, clock.sleep, clock.clock)
    http.throttle(EUTILS, {"api_key": "k"}, clock.sleep, clock.clock)
    assert clock.sleeps == [0.11]


def test_throttle_ignores_other_hosts(clock):
    for _ in range(3):
        http.throttle("https://api.gdc.cancer.gov/files", None, clock.sleep, clock.clock)
    assert clock.sleeps == []


@respx.mock
def test_retry_after_is_honoured_on_429(monkeypatch):
    monkeypatch.setattr(http, "MIN_INTERVAL", {})
    route = respx.get("https://example.org/x").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(200, json={"ok": True}),
    ])
    sleeps = []
    with httpx.Client() as c:
        resp = http.request(c, http.ApiLog(), "GET", "https://example.org/x", retries=2, sleep=sleeps.append)
    assert resp.status_code == 200 and route.call_count == 2
    assert sleeps == [5.0]  # Retry-After (5 s) is longer than the first backoff (1 s)


@respx.mock
def test_extra_ok_status_is_accepted(monkeypatch):
    monkeypatch.setattr(http, "MIN_INTERVAL", {})
    respx.get("https://example.org/list").mock(return_value=httpx.Response(206, json=[1, 2]))
    with httpx.Client() as c:
        resp = http.request(c, http.ApiLog(), "GET", "https://example.org/list", retries=0,
                            ok_status=(200, 206))
    assert resp.json() == [1, 2]
    with httpx.Client() as c, pytest.raises(http.ApiError, match="HTTP 206"):
        http.request(c, http.ApiLog(), "GET", "https://example.org/list", retries=0)
