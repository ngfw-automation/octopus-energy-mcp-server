"""Thin async REST client for the Octopus Energy public API.

Handles Basic-auth (API key as username, blank password), an in-memory TTL
cache, bounded retry/backoff on 429/5xx, and ``next``-link pagination.
Deliberately small and read-only: it only issues ``GET`` requests.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import logging
import random
import time
from collections import OrderedDict
from typing import Any, Optional

import httpx

from .observability import event, mask_identifiers

BASE_URL = "https://api.octopus.energy/v1"
_RETRYABLE = (429, 500, 502, 503, 504)


class OctopusError(Exception):
    """Base error for Octopus API problems."""


class OctopusAuthError(OctopusError):
    """The API key was rejected (HTTP 401)."""


class OctopusNotFoundError(OctopusError):
    """The meter / account / tariff was not found (HTTP 404)."""


class OctopusAPIError(OctopusError):
    """A non-retryable API error (other 4xx/5xx)."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"Octopus API error {status}: {detail}")
        self.status = status


class TTLCache:
    """A monotonic-clock TTL cache with an LRU bound.

    ``get`` returns a deep copy: callers walk the rows they are handed (the
    gas m3->kWh conversion used to rewrite them in place), and a cached
    payload a caller can mutate would compound that edit on every later hit.
    Entries are capped and swept on write, so a long-running process querying
    wide ranges cannot grow without limit.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._store: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def __len__(self) -> int:
        return len(self._store)

    def get(self, key: Any) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires, value = entry
        if time.monotonic() > expires:
            self._store.pop(key, None)
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return copy.deepcopy(value)

    def set(self, key: Any, value: Any, ttl: int) -> None:
        now = time.monotonic()
        for stale in [k for k, (expires, _v) in self._store.items() if expires < now]:
            self._store.pop(stale, None)
        self._store[key] = (now + ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)
            self.evictions += 1

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }


class OctopusREST:
    """Async GET-only client with caching, single-flight and bounded retries."""

    def __init__(
        self,
        api_key: str,
        cache: Optional[TTLCache] = None,
        timeout: float = 30.0,
        deadline: float = 90.0,
        max_attempts: int = 5,
    ) -> None:
        self.api_key = api_key
        # Identifies the credential in cache keys without holding the key
        # itself, so one process could serve two accounts without leaking
        # one's data into the other's cache.
        self.fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:12]
        self.cache = cache or TTLCache()
        self.timeout = timeout
        self.deadline = deadline
        self.max_attempts = max_attempts
        self._client: Optional[httpx.AsyncClient] = None
        self._locks: dict[Any, asyncio.Lock] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def get(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        auth: bool = False,
        ttl: int = 3600,
    ) -> Any:
        """Perform a single GET (one page), with caching and retry/backoff.

        Concurrent callers asking for the same thing wait on one request
        rather than each starting their own.
        """
        params = dict(params or {})
        full_url = url if url.startswith("http") else BASE_URL + url
        cache_key = (
            self.fingerprint if auth else "public",
            full_url,
            tuple(sorted(params.items())),
        )
        hit = self.cache.get(cache_key)
        if hit is not None:
            return hit

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        try:
            async with lock:
                hit = self.cache.get(cache_key)  # filled while we waited
                if hit is not None:
                    return hit
                data = await self._fetch(full_url, params, auth)
                self.cache.set(cache_key, data, ttl)
                return data
        finally:
            if not lock.locked():
                self._locks.pop(cache_key, None)

    async def _backoff(
        self, attempt: int, deadline: float, retry_after: Optional[str]
    ) -> bool:
        """Sleep before the next attempt; False when the budget is spent."""
        delay = min(2**attempt, 16) + random.random()
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 60.0))
            except ValueError:
                pass  # HTTP-date form; the computed backoff will do
        if time.monotonic() + delay > deadline:
            return False
        await asyncio.sleep(delay)
        return True

    async def _fetch(self, full_url: str, params: dict[str, Any], auth: bool) -> Any:
        client = await self._get_client()
        headers = self._auth_header() if auth else {}
        # One budget for the whole request, however many attempts fit in it:
        # five attempts of unbounded backoff could hang a tool call for a
        # minute per page with nothing to show for it.
        deadline = time.monotonic() + self.deadline
        started = time.monotonic()
        safe_url = mask_identifiers(full_url)
        last_error: Optional[Exception] = None

        for attempt in range(self.max_attempts):
            final = attempt == self.max_attempts - 1
            try:
                resp = await client.get(full_url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                event(
                    "upstream.error",
                    level=logging.WARNING,
                    url=safe_url,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                if final or not await self._backoff(attempt, deadline, None):
                    break
                continue

            status = resp.status_code
            if status in _RETRYABLE:
                last_error = OctopusAPIError(status, resp.text[:200])
                event(
                    "upstream.retry",
                    level=logging.WARNING,
                    url=safe_url,
                    status=status,
                    attempt=attempt + 1,
                )
                if final or not await self._backoff(
                    attempt, deadline, resp.headers.get("Retry-After")
                ):
                    break
                continue

            ms = round((time.monotonic() - started) * 1000)
            if status == 401:
                event("upstream.request", level=logging.WARNING, url=safe_url, status=401, ms=ms)
                raise OctopusAuthError(
                    "Octopus rejected the API key (401). Check OCTOPUS_API_KEY."
                )
            if status == 404:
                event("upstream.request", level=logging.WARNING, url=safe_url, status=404, ms=ms)
                raise OctopusNotFoundError(f"Not found (404): {safe_url}")
            if status >= 400:
                event("upstream.request", level=logging.WARNING, url=safe_url, status=status, ms=ms)
                raise OctopusAPIError(status, resp.text[:200])

            event(
                "upstream.request",
                url=safe_url,
                status=status,
                ms=ms,
                attempts=attempt + 1,
                bytes=len(resp.content),
            )
            return resp.json()

        spent = round((time.monotonic() - started) * 1000)
        event(
            "upstream.exhausted",
            level=logging.ERROR,
            url=safe_url,
            ms=spent,
            error=str(last_error),
        )
        raise OctopusAPIError(
            0, f"request failed after {spent} ms of retries: {last_error}"
        )

    async def get_all(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        auth: bool = False,
        ttl: int = 3600,
        max_pages: int = 20,
        row_cap: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Follow ``next`` links and return a flat list of result rows."""
        rows: list[dict[str, Any]] = []
        current_url = url
        current_params = params
        for page in range(max_pages):
            data = await self.get(current_url, params=current_params, auth=auth, ttl=ttl)
            if isinstance(data, list):
                rows.extend(data)
                break
            rows.extend(data.get("results", []))
            if row_cap is not None and len(rows) >= row_cap:
                return rows[:row_cap]
            nxt = data.get("next")
            if not nxt:
                break
            if page == max_pages - 1:
                event(
                    "upstream.page_cap",
                    level=logging.WARNING,
                    url=mask_identifiers(url),
                    max_pages=max_pages,
                    rows=len(rows),
                )
            current_url = nxt
            current_params = None  # the next URL already carries the query string
        return rows
