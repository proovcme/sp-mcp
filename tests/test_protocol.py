import asyncio
import json
import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from sp_mcp.cli import make_http_app
from sp_mcp.server import create_server


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_discovery_and_structured_errors(config, mode):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-X", "utf8", "-m", "sp_mcp"],
            env={**os.environ, "SP_MCP_DATA_DIR": str(config.root)},
        )
        async with Client(params, mode=mode) as client:
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            assert {
                "sp_search_documents",
                "sp_open_document",
                "sp_read_clause",
                "sp_check_status",
                "sp_prepare_uvyazka_link",
                "sp_attach_uvyazka_link",
                "sp_batch_check_status",
            } <= names
            sources = await client.call_tool("sp_sources", {})
            assert not sources.is_error
            assert json.loads(sources.content[0].text)["local"]["snapshots"] == 0
            failure = await client.call_tool("sp_open_document", {"url": "https://evil.invalid/"})
            assert not failure.is_error
            assert json.loads(failure.content[0].text)["error_code"] == "URL_NOT_ALLOWED"
            resource = await client.read_resource("sp://help")
            assert resource.contents

    asyncio.run(run())


def test_same_service_available_as_http_api(config):
    from starlette.testclient import TestClient

    from sp_mcp.cli import BearerMiddleware

    server, service = create_server(config)
    app = BearerMiddleware(make_http_app(server, service, "127.0.0.1"), "synthetic-token")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 401
        headers = {"Authorization": "Bearer synthetic-token"}
        assert client.get("/health", headers=headers).json()["name"] == "sp-mcp"
        assert client.post("/api/sources", json={}, headers=headers).status_code == 200
        result = client.post("/api/open", json={"url": "https://evil.invalid/"}, headers=headers)
        assert result.status_code == 400
        assert result.json()["error_code"] == "URL_NOT_ALLOWED"
        assert client.post("/api/no_such_method", json={}, headers=headers).status_code == 404
        assert client.post("/api/search", json=[], headers=headers).status_code == 400
