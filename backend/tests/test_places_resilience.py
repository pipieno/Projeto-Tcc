from __future__ import annotations

import asyncio
import time
from functools import partial

import httpx
import pytest

from backend import places
from backend.places import OverpassPlaceFinder, PlaceSearchUnavailable, detect_place_category


ASYNC_CLIENT = httpx.AsyncClient
ASYNC_SLEEP = asyncio.sleep
PAYLOAD = {
    "elements": [{
        "lat": -26.4701,
        "lon": -49.1265,
        "tags": {"name": "Farmácia do Bairro", "addr:suburb": "Estrada Nova"},
    }],
}


@pytest.fixture
def finder(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERPASS_URL", "https://primary.example/api/interpreter")
    monkeypatch.setenv("OVERPASS_FALLBACK_URL", "https://backup.example/api/interpreter")
    monkeypatch.setenv("PLACE_SEARCH_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("PLACE_SEARCH_TOTAL_TIMEOUT_SECONDS", "18")
    monkeypatch.setenv("PLACE_SEARCH_CACHE_TTL_SECONDS", "600")
    monkeypatch.setattr(places.logger, "propagate", True)
    finder = OverpassPlaceFinder()
    finder.snapshot_path = tmp_path / "snapshot.json"
    return finder


@pytest.fixture
def delays(monkeypatch):
    recorded = []

    async def sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(places.asyncio, "sleep", sleep)
    return recorded


def mock_network(monkeypatch, handler):
    monkeypatch.setattr(
        places.httpx, "AsyncClient",
        partial(ASYNC_CLIENT, transport=httpx.MockTransport(handler)),
    )


def pharmacy():
    return detect_place_category("onde fica a farmacia mais perto do centro do bairro")


def test_overloaded_primary_uses_backup_and_caches_result(finder, delays, monkeypatch, caplog):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        assert request.method == "POST"
        if request.url.host == "primary.example":
            return httpx.Response(502)
        return httpx.Response(200, json=PAYLOAD)

    mock_network(monkeypatch, handler)
    result = finder.search(pharmacy())

    assert result[0].name == "Farmácia do Bairro"
    assert finder.search(pharmacy()) == result
    assert calls == ["primary.example", "backup.example"]
    assert delays == [0.5]
    failures = [record for record in caplog.records if hasattr(record, "http_status")]
    assert failures[0].http_status == 502
    assert failures[0].endpoint == "primary.example"
    assert failures[0].error_type == "HTTPStatusError"


def test_connection_failure_retries_with_progressive_delays(finder, delays, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        raise httpx.ConnectError("Connection refused", request=request)

    mock_network(monkeypatch, handler)
    with pytest.raises(PlaceSearchUnavailable) as error:
        finder.search(pharmacy())

    assert isinstance(error.value.__cause__, httpx.ConnectError)
    assert calls == ["primary.example", "backup.example", "primary.example"]
    assert delays == [0.5, 1.0]
    assert not finder._cache


def test_query_error_is_not_retried(finder, delays, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(400)

    mock_network(monkeypatch, handler)
    with pytest.raises(PlaceSearchUnavailable):
        finder.search(pharmacy())
    assert calls == ["primary.example"]
    assert delays == []


@pytest.mark.parametrize("status", [406, 429, 503])
def test_retry_after_prevents_reusing_busy_server(finder, delays, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.example":
            return httpx.Response(status, headers={"Retry-After": "120"})
        if len(calls) == 2:
            return httpx.Response(502)
        return httpx.Response(200, json=PAYLOAD)

    mock_network(monkeypatch, handler)
    assert finder.search(pharmacy())
    assert calls == ["primary.example", "backup.example", "backup.example"]
    assert delays == [0.5, 1.0]


def test_total_deadline_includes_retry_delays(finder, monkeypatch):
    finder.total_timeout_seconds = 0.08
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(502)

    mock_network(monkeypatch, handler)
    started = time.monotonic()
    with pytest.raises(PlaceSearchUnavailable, match="Tempo total"):
        finder.search(pharmacy())
    assert time.monotonic() - started < 0.5
    assert calls == ["primary.example"]


def test_slow_primary_leaves_time_for_backup(finder, delays, monkeypatch):
    finder.total_timeout_seconds = 0.3
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.example":
            await ASYNC_SLEEP(1)
        return httpx.Response(200, json=PAYLOAD)

    mock_network(monkeypatch, handler)
    assert finder.search(pharmacy())
    assert calls == ["primary.example", "backup.example"]


@pytest.mark.parametrize("payload", [
    {"elements": [], "remark": "runtime error: Query timed out"},
    {"error": "unavailable"},
])
def test_incomplete_response_uses_backup(finder, delays, monkeypatch, payload):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=payload if len(calls) == 1 else PAYLOAD)

    mock_network(monkeypatch, handler)
    assert finder.search(pharmacy())
    assert calls == ["primary.example", "backup.example"]


def test_all_rate_limited_servers_stop_without_long_wait(finder, delays, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(429, headers={"Retry-After": "120"})

    mock_network(monkeypatch, handler)
    with pytest.raises(PlaceSearchUnavailable):
        finder.search(pharmacy())
    assert calls == ["primary.example", "backup.example"]
    assert sum(delays) < 2


def test_empty_result_is_successful_and_cached(finder, delays, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"elements": []})

    mock_network(monkeypatch, handler)
    assert finder.search(pharmacy()) == []
    assert finder.search(pharmacy()) == []
    assert calls == ["primary.example"]
