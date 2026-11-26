"""Shared HTTP layer for all providers.

Standard library only (no extra dependency). Features:
  * configurable timeout (HTTP_TIMEOUT, seconds) and retry count (HTTP_RETRIES)
  * exponential backoff with jitter for network errors and HTTP 5xx
  * HTTP 429 handling that honours the Retry-After header
  * no retry for errors that cannot heal themselves (4xx except 408/429)
  * secrets are scrubbed from every error message: request headers are never
    included and any registered secret value is replaced by "***"
"""
from __future__ import annotations

import gzip
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "tryunhedgedcarrytrade-data-bot/1.0 (+https://github.com/CemKarakutuk/tryunhedgedcarrytrade)"
DEFAULT_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "60"))
DEFAULT_RETRIES = int(os.environ.get("HTTP_RETRIES", "4"))
BACKOFF_BASE = float(os.environ.get("HTTP_BACKOFF", "2"))
MAX_SLEEP = 120.0

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Values registered here are masked in every error message."""
    if value:
        _SECRETS.add(value)


def scrub(text: str) -> str:
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class ProviderError(Exception):
    """Raised when a provider cannot deliver valid data. Message is already scrubbed."""

    def __init__(self, msg: str):
        super().__init__(scrub(str(msg)))


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str
    url: str

    def text(self) -> str:
        return self.body.decode("utf-8-sig", errors="replace")


# Tests replace this with a fake to simulate failures without network access.
def _default_opener(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)


opener = _default_opener
sleeper = time.sleep


def get(url: str, headers: dict | None = None, *, timeout: float | None = None,
        retries: int | None = None, label: str = "") -> Response:
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    retries = DEFAULT_RETRIES if retries is None else retries
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    last = "unknown error"
    for attempt in range(retries + 1):
        wait = None
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with opener(req, timeout) as r:
                body = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    body = gzip.decompress(body)
                return Response(getattr(r, "status", 200), body,
                                (r.headers.get("Content-Type") or "").lower(), url)
        except urllib.error.HTTPError as e:
            code = e.code
            last = f"HTTP {code}"
            if code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra) if ra else None
                except ValueError:
                    wait = None
                last += " (rate limited)"
            elif code not in (408, 500, 502, 503, 504):
                raise ProviderError(f"{label}: {last}; not retried")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
        if attempt < retries:
            if wait is None:
                wait = BACKOFF_BASE ** attempt + random.uniform(0, 1)
            wait = min(wait, MAX_SLEEP)
            print(f"  {label}: {scrub(last)}; retry {attempt + 1}/{retries} in {wait:.1f}s")
            sleeper(wait)
    raise ProviderError(f"{label}: {last} after {retries + 1} attempts")
