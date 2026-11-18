import asyncio
import gzip
import time
from dataclasses import replace
from urllib.robotparser import RobotFileParser

import httpx
import pytest
from conftest import ros_empty_catalogue

from sp_mcp.errors import NormsError
from sp_mcp.network import Network
from sp_mcp.service import Service


def test_robots_exclusion_prevents_search_request(config, monkeypatch):
    network = Network(config)
    robots = RobotFileParser()
    robots.parse(["User-agent: *", "Disallow: /search.htm"])
    network.robots["https://meganorm.ru"] = robots
    monkeypatch.setattr(network, "_request", lambda url: pytest.fail("Disallowed page was requested"))
    with pytest.raises(NormsError, match="robots"):
        network.fetch("https://meganorm.ru/search.htm")


def test_unavailable_robots_is_not_silently_ignored(config, monkeypatch):
    network = Network(config)

    def unavailable(url):
        raise NormsError("NETWORK_ERROR", "Unavailable", retryable=True)

    monkeypatch.setattr(network, "_request", unavailable)
    with pytest.raises(NormsError, match="crawl policy"):
        network.fetch("https://meganorm.ru/")


@pytest.mark.parametrize("host", ["meganorm.ru", "new.gostinfo.ru"])
def test_redirect_destination_is_checked_before_request(config, monkeypatch, host):
    network = Network(config)
    start = "https://meganorm.ru/catalog.html"
    blocked = f"https://{host}/blocked.html"
    requested = []

    def request(url):
        requested.append(url)
        if url.endswith("robots.txt"):
            return {"raw": b"User-agent: *\nDisallow: /blocked.html"}
        assert url == start
        return {"redirect": blocked}

    monkeypatch.setattr(network, "_request", request)
    with pytest.raises(NormsError, match="robots"):
        network.fetch(start)
    assert blocked not in requested
    assert f"https://{host}/robots.txt" in requested


def test_search_uses_one_wall_clock_budget_and_reports_partial_coverage(config, monkeypatch):
    service = Service(replace(config, operation_timeout=1))
    timeouts = []

    async def download(url, timeout):
        if url.endswith("robots.txt"):
            return {"raw": b""}
        timeouts.append(timeout)
        if len(timeouts) == 1:
            await asyncio.sleep(0.2)
            return ros_empty_catalogue(url)
        await asyncio.sleep(timeout + 0.02)
        raise TimeoutError

    monkeypatch.setattr(service.network, "_download", download)
    start = time.monotonic()
    result = service.search("канализация")
    assert time.monotonic() - start < 2.5
    assert len(timeouts) == 2 and 0 < timeouts[1] < timeouts[0] <= 1
    assert result["status"] == "partial"
    assert result["match_status"] == "search_incomplete"
    assert result["search_provenance"]
    assert any(error["error_code"] == "NETWORK_BUDGET_EXCEEDED" for error in result["source_errors"])


@pytest.mark.parametrize("size", [32, 1024])
def test_rosstandard_gzip_obeys_decoded_size_limit(config, monkeypatch, size):
    original_client = httpx.AsyncClient
    body = b"a" * size

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(body)

    def send(request):
        assert request.headers["accept-encoding"] == "gzip"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Body())

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original_client(**kw, transport=httpx.MockTransport(send))
    )
    network = Network(replace(config, max_bytes=128))
    url = "https://protect.gost.ru/sp/"
    if size < 128:
        assert network._request(url)["raw"] == body
    else:
        with pytest.raises(NormsError, match="bounded download"):
            network._request(url)
