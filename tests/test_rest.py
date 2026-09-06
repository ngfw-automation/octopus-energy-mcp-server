"""The REST client's cache: entries must survive callers that edit what they
are handed (the gas m3 -> kWh conversion used to rewrite rows in place)."""

from octopus_mcp.rest import TTLCache


def test_cache_returns_a_copy_not_the_stored_object():
    cache = TTLCache()
    payload = {"results": [{"consumption": 1.0}, {"consumption": 2.0}]}
    cache.set("k", payload, ttl=60)

    first = cache.get("k")
    for row in first["results"]:
        row["consumption"] *= 11.0  # what the gas conversion used to do

    second = cache.get("k")
    assert [r["consumption"] for r in second["results"]] == [1.0, 2.0]
    assert [r["consumption"] for r in payload["results"]] == [1.0, 2.0]


def test_cache_expires():
    import time

    cache = TTLCache()
    cache.set("k", {"a": 1}, ttl=0)
    time.sleep(0.01)
    assert cache.get("k") is None


def test_cache_miss_is_none():
    assert TTLCache().get("nope") is None


# --- S-04 / S-05 / S-06: cache bounds, single flight, retry budget --------

import asyncio  # noqa: E402
import time  # noqa: E402

from octopus_mcp.rest import OctopusAPIError, OctopusAuthError, OctopusREST  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"results": [{"v": 1}]}
        self.headers = headers or {}
        self.text = "fake"
        self.content = b"fake"

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient, counting calls and scripting statuses."""

    def __init__(self, responses=None, delay=0.0):
        self.responses = list(responses or [])
        self.calls = 0
        self.delay = delay
        self.is_closed = False

    async def get(self, url, params=None, headers=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()

    async def aclose(self):
        self.is_closed = True


def _client(rest_client, fake):
    async def _get_client():
        return fake

    rest_client._get_client = _get_client
    return rest_client


def test_cache_is_lru_bounded():
    cache = TTLCache(maxsize=3)
    for i in range(5):
        cache.set(f"k{i}", {"i": i}, ttl=60)
    assert len(cache) == 3
    assert cache.get("k0") is None  # evicted, oldest first
    assert cache.get("k4") == {"i": 4}
    assert cache.stats()["evictions"] == 2


def test_concurrent_identical_requests_make_one_call():
    """Single flight: five callers asking the same question at once used to
    make five upstream requests."""
    rest_client = _client(OctopusREST("key"), FakeClient(delay=0.02))
    fake = asyncio.run(rest_client._get_client())

    async def run():
        return await asyncio.gather(*[rest_client.get("/products/") for _ in range(5)])

    results = asyncio.run(run())
    assert fake.calls == 1
    assert all(r == results[0] for r in results)


def test_cache_key_separates_credentials():
    """Two accounts in one process must not read each other's data."""
    shared = TTLCache()
    a = _client(OctopusREST("key-aaaa-1111", cache=shared), FakeClient([FakeResponse(payload={"who": "a"})]))
    b = _client(OctopusREST("key-bbbb-2222", cache=shared), FakeClient([FakeResponse(payload={"who": "b"})]))
    assert asyncio.run(a.get("/accounts/x/", auth=True)) == {"who": "a"}
    assert asyncio.run(b.get("/accounts/x/", auth=True)) == {"who": "b"}
    assert a.fingerprint != b.fingerprint


def test_retries_are_bounded_by_a_deadline():
    """A 429 storm used to sleep up to ~46s per page with no ceiling."""
    rest_client = OctopusREST("key", max_attempts=5, deadline=0.5)
    fake = FakeClient([FakeResponse(status=429) for _ in range(5)])
    _client(rest_client, fake)
    started = time.monotonic()
    try:
        asyncio.run(rest_client.get("/products/"))
        raise AssertionError("expected the request to give up")
    except OctopusAPIError as exc:
        assert "retries" in str(exc)
    assert time.monotonic() - started < 3.0
    assert fake.calls < 5  # gave up inside the budget rather than trying them all


def test_retry_then_success_is_returned():
    rest_client = OctopusREST("key", deadline=10.0)
    fake = FakeClient([FakeResponse(status=503, headers={"Retry-After": "0"}), FakeResponse()])
    _client(rest_client, fake)
    assert asyncio.run(rest_client.get("/products/")) == {"results": [{"v": 1}]}
    assert fake.calls == 2


def test_auth_errors_are_not_retried():
    rest_client = OctopusREST("key")
    fake = FakeClient([FakeResponse(status=401)])
    _client(rest_client, fake)
    try:
        asyncio.run(rest_client.get("/accounts/x/", auth=True))
        raise AssertionError("expected an auth error")
    except OctopusAuthError:
        pass
    assert fake.calls == 1
