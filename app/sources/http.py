"""Shared async HTTP client: retries, per-host rate limiting, and caching.

One source failing must never take down a run, so every helper here converts
transport problems into ``FetchError`` and lets the caller decide.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from typing import Any

import httpx

from app.config import CACHE_DIR, get_settings
from app.logging_setup import get_logger

log = get_logger("http")


class FetchError(Exception):
    """A source-level fetch failure that should be logged, not raised further."""


class RateLimiter:
    """Minimum delay between requests to the same host."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, host: str) -> None:
        if self._delay <= 0:
            return
        async with self._locks[host]:
            elapsed = time.monotonic() - self._last[host]
            wait = self._delay - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


class ResponseCache:
    """Small on-disk cache, keyed by method+url+body.

    Keeps repeated runs cheap and polite during development, and lets a source
    survive a transient upstream outage within the TTL window.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return CACHE_DIR / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        path = self._path(key)
        try:
            if not path.exists():
                return None
            if time.time() - path.stat().st_mtime > self.ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        try:
            self._path(key).write_text(json.dumps(value), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass


class HttpClient:
    """Async HTTP client wrapper used by every source."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        concurrency: int | None = None,
        rate_limit_delay: float | None = None,
        cache_ttl: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        settings = get_settings()
        self.timeout = timeout if timeout is not None else settings.http_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.http_max_retries
        self.user_agent = user_agent or settings.user_agent
        self._sem = asyncio.Semaphore(
            concurrency if concurrency is not None else settings.http_max_concurrency
        )
        self._limiter = RateLimiter(
            rate_limit_delay if rate_limit_delay is not None else settings.http_rate_limit_delay
        )
        self._cache = ResponseCache(
            cache_ttl if cache_ttl is not None else settings.http_cache_ttl_seconds
        )
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient must be used as an async context manager")
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        use_cache: bool = True,
        expect_json: bool = True,
    ) -> Any:
        """Perform a request with retry and exponential backoff.

        Raises :class:`FetchError` on final failure.
        """
        cache_key = json.dumps(
            {"m": method, "u": url, "p": params, "b": json_body}, sort_keys=True, default=str
        )
        if use_cache and method.upper() == "GET":
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
        if use_cache and method.upper() == "POST" and json_body is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        host = httpx.URL(url).host or "unknown"
        last_error: str = "unknown error"

        for attempt in range(self.max_retries + 1):
            try:
                async with self._sem:
                    await self._limiter.acquire(host)
                    response = await self.client.request(
                        method, url, params=params, json=json_body, headers=headers
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if status == 429 or 500 <= status < 600:
                    last_error = f"HTTP {status}"
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(float(retry_after), 30.0))
                        continue
                elif 400 <= status < 500:
                    # Client errors will not improve on retry.
                    raise FetchError(f"HTTP {status} for {url}: {response.text[:200]}")
                else:
                    try:
                        payload = response.json() if expect_json else response.text
                    except ValueError as exc:
                        raise FetchError(f"invalid JSON from {url}: {exc}") from exc
                    if use_cache:
                        self._cache.set(cache_key, payload)
                    return payload

            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt * 0.6, 10.0))

        raise FetchError(f"{last_error} after {self.max_retries + 1} attempts for {url}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post_json(self, url: str, json_body: dict, **kwargs: Any) -> Any:
        return await self.request("POST", url, json_body=json_body, **kwargs)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        kwargs["expect_json"] = False
        return await self.request("GET", url, **kwargs)

    async def gather(self, coros: list, *, return_exceptions: bool = True) -> list:
        """Run tasks concurrently, keeping failures as values."""
        return await asyncio.gather(*coros, return_exceptions=return_exceptions)
