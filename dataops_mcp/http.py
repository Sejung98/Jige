"""Shared HTTP client: retries and API call logging (provenance).

Discovery tools record every API call in an `ApiLog` and return that log with their results,
so the agent can report "the API queries the answer is based on".
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from . import __version__
from .config import Config

RETRY_STATUS = {429, 500, 502, 503, 504}

# Minimum spacing between requests to hosts with published rate limits: (without API key, with API key).
# NCBI E-utilities allow 3 requests/s without a key and 10/s with one
# (https://www.ncbi.nlm.nih.gov/books/NBK25497/). Back-to-back esearch/esummary/efetch calls
# otherwise get HTTP 429.
MIN_INTERVAL: dict[str, tuple[float, float]] = {"eutils.ncbi.nlm.nih.gov": (0.34, 0.11)}
_last_call: dict[str, float] = {}


def throttle(url: str, params: dict | None, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> None:
    """Wait if the previous request to the same rate-limited host was too recent."""
    host = httpx.URL(url).host
    rule = MIN_INTERVAL.get(host)
    if rule is None:
        return
    interval = rule[1] if params and params.get("api_key") else rule[0]
    last = _last_call.get(host)
    if last is not None:
        wait = interval - (clock() - last)
        if wait > 0:
            sleep(wait)
    _last_call[host] = clock()


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After", "")
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class ApiError(RuntimeError):
    pass


class ApiLog:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url: str, params: dict | None, body: Any, status: int | None) -> None:
        entry: dict[str, Any] = {"method": method, "url": url, "status": status}
        if params:
            entry["params"] = params
        if body is not None:
            entry["json"] = body
        self.calls.append(entry)


def make_client(cfg: Config) -> httpx.Client:
    http = cfg.get("http", {})
    ua = f"Jige/{__version__} (+https://github.com/Sejung98/Jige)"
    if http.get("contact_email"):
        ua += f" ({http['contact_email']})"
    return httpx.Client(
        timeout=float(http.get("timeout", 60)),
        headers={"User-Agent": ua},
        follow_redirects=True,
    )


def request(
    client: httpx.Client,
    log: ApiLog,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: Any = None,
    headers: dict | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    ok_status: tuple[int, ...] = (200,),
) -> httpx.Response:
    """Request with per-host rate limiting and retries (429/5xx/network errors; exponential backoff,
    or the server's Retry-After if longer, capped at 30 s). Raises ApiError on failure."""
    last_exc: Exception | None = None
    wait_hint: float | None = None
    for attempt in range(retries + 1):
        throttle(url, params, sleep)
        try:
            resp = client.request(method, url, params=params, json=json, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            log.add(method, url, params, json, None)
        else:
            log.add(method, url, params, json, resp.status_code)
            if resp.status_code in ok_status:
                return resp
            if resp.status_code not in RETRY_STATUS:
                raise ApiError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
            last_exc = ApiError(f"{method} {url} -> HTTP {resp.status_code}")
            wait_hint = _retry_after(resp)
        if attempt < retries:
            backoff = float(2 ** attempt)
            sleep(min(max(backoff, wait_hint or 0.0), 30.0))
            wait_hint = None
    raise ApiError(f"{method} {url} failed after {retries + 1} attempts: {last_exc}")
