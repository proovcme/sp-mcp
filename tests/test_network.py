from urllib.robotparser import RobotFileParser

import pytest

from sp_mcp.errors import NormsError
from sp_mcp.network import Network


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
