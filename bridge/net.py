"""Outbound HTTP with timeouts and bounded retries."""

import logging
import random
import time

import httpx

log = logging.getLogger("hermes.net")

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_S = 8.0
_sleep = time.sleep   # swapped out in tests


class UpstreamError(RuntimeError):
    """A provider call failed after retries. `status` is the HTTP code, if there was one."""

    def __init__(self, service: str, message: str, status: int | None = None):
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


def make_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)))


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers.get("retry-after", ""))
    except ValueError:
        return None


def request(client: httpx.Client, method: str, url: str, *, service: str, retries: int = 2,
            retry_status: frozenset[int] = RETRY_STATUS, backoff_s: float = 0.5, **kw) -> httpx.Response:
    """Send a request, retrying network errors and `retry_status` codes with jittered backoff."""
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            r = client.request(method, url, **kw)
        except httpx.HTTPError as e:   # timeouts, resets, DNS
            if last:
                raise UpstreamError(service, f"{type(e).__name__}: {e}") from e
            wait = backoff_s * 2 ** attempt
            log.warning("upstream network error, retrying", extra={"service": service, "error": str(e)[:200],
                                                                   "attempt": attempt + 1})
        else:
            if r.status_code < 400:
                return r
            if last or r.status_code not in retry_status:
                raise UpstreamError(service, f"HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
            wait = _retry_after(r) or backoff_s * 2 ** attempt
            log.warning("upstream returned retryable status", extra={"service": service,
                                                                     "status": r.status_code,
                                                                     "attempt": attempt + 1})
        _sleep(min(MAX_BACKOFF_S, wait) + random.uniform(0, 0.1))
    raise AssertionError("unreachable")
