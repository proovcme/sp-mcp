from conftest import fetched
from starlette.testclient import TestClient

from sp_mcp.cli import BearerMiddleware
from sp_mcp.parsers import parse_document
from sp_mcp.server import create_server
from sp_mcp.web import make_http_app


def test_browser_and_mcp_service_use_the_same_snapshots(config, synthetic_html):
    server, service = create_server(config)
    raw = fetched(synthetic_html)
    saved = service.store.save(raw, parse_document(raw))
    with TestClient(make_http_app(server, service, "127.0.0.1"), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/not-a-file").status_code == 404
        config_result = client.get("/api/ui-config").json()
        assert config_result["search_requires_ai"] is False
        library = client.post("/api/library", json={}).json()
        assert library["items"][0]["snapshot_id"] == saved["snapshot_id"]
        document = client.post("/api/document", json={"snapshot_id": saved["snapshot_id"]}).json()
        assert document["provenance"]["sha256"] == saved["provenance"]["sha256"]
        text = client.post(
            "/api/read_clause", json={"snapshot_id": saved["snapshot_id"], "number": "7.1.1"}
        ).json()
        assert text["status"] == "found"


def test_project_write_requires_same_origin_and_session_token(config, monkeypatch):
    server, service = create_server(config)
    monkeypatch.setattr(
        "sp_mcp.web.bridge_call", lambda *args: (_ for _ in ()).throw(AssertionError("Unexpected write"))
    )
    with TestClient(make_http_app(server, service, "127.0.0.1"), base_url="http://127.0.0.1") as client:
        assert client.post("/api/uvyazka_attach", json={}).status_code == 403
        assert (
            client.post(
                "/api/search", json={"query": "test"}, headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )


def test_web_rejects_unknown_hosts_and_invalid_batch_values(config, monkeypatch):
    monkeypatch.setenv("SP_MCP_ALLOWED_HOSTS", "standards.example")
    server, service = create_server(config)
    with TestClient(make_http_app(server, service, "127.0.0.1"), base_url="http://127.0.0.1") as client:
        assert client.get("/health", headers={"Host": "standards.example"}).status_code == 200
        assert (
            client.get("/api/ui-config", headers={"Host": "other.example"}).json()["error_code"]
            == "HOST_REJECTED"
        )
        assert client.post("/api/batch_check", json={"items": {}}).status_code == 400
        assert client.post("/api/open", json={"url": {}}).json()["error_code"] == "INVALID_ARGUMENT"
        assert client.post("/api/search", json={"query": "demo", "online": "false"}).status_code == 400
        batch = client.post(
            "/api/batch_check", json={"items": [1, {"input_id": "bad-code", "code": 3}]}
        ).json()
        assert [item["input_id"] for item in batch["items"]] == [None, "bad-code"]
        assert all(item["result"]["error_code"] == "INVALID_ARGUMENT" for item in batch["items"])
        assert (
            client.post("/api/search", content="{}", headers={"Content-Type": "text/plain"}).status_code
            == 415
        )


def test_public_shell_does_not_bypass_data_authentication(config):
    server, service = create_server(config)
    with TestClient(
        BearerMiddleware(make_http_app(server, service, "127.0.0.1"), "test-token"),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/ui-config").status_code == 401
        assert client.post("/api/library", json={}).status_code == 401
