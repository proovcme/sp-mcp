import threading
from concurrent.futures import ThreadPoolExecutor

import anyio
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from sp_mcp.public_limits import PublicLimits


def test_busy_public_server_rejects_without_queue_and_keeps_health_available(tmp_path):
    started, release = threading.Event(), threading.Event()

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                await send({"type": event["type"] + ".complete"})
                if event["type"] == "lifespan.shutdown":
                    return
        if scope["path"] == "/api/search":
            started.set()
            await anyio.to_thread.run_sync(lambda: release.wait(5))
        await JSONResponse({"ok": True})(scope, receive, send)

    guard = PublicLimits(app, tmp_path, disk_reserve=0)
    with TestClient(guard) as client, ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.post, "/api/search", json={})
        try:
            assert started.wait(3)
            denied = client.post("/api/search", json={})
            assert denied.status_code == 503
            assert denied.json()["error_code"] == "SERVER_BUSY"
            assert denied.headers["Retry-After"] == "5"
            assert client.get("/health").status_code == 200
            assert guard.heavy_active == 1
        finally:
            release.set()
        assert pending.result().status_code == 200
        assert guard.active == guard.heavy_active == 0


def test_per_client_heavy_rate_recovers_after_minute_and_cache_is_bounded(tmp_path):
    clock = [0]
    guard = PublicLimits(None, tmp_path, disk_reserve=0, clock=lambda: clock[0])
    for _ in range(6):
        assert guard.acquire("search", "client") is None
        guard.release("search")
    assert guard.acquire("search", "client")[:2] == (429, "RATE_LIMIT")
    assert guard.acquire("library", "client") is None
    guard.release("library")
    clock[0] = 61
    assert guard.acquire("search", "client") is None
    guard.release("search")
    for i in range(2100):
        clock[0] += 1
        result = guard.acquire("library", str(i))
        if result is None:
            guard.release("library")
    assert len(guard.clients) <= 2048


def test_storage_budget_stops_new_heavy_work_without_disabling_saved_reads(tmp_path):
    (tmp_path / "synthetic.bin").write_bytes(b"x" * 100)
    guard = PublicLimits(None, tmp_path, disk_reserve=0, data_limit=50)
    assert guard.acquire("open", "client")[:2] == (503, "STORAGE_LIMIT")
    assert guard.acquire("read", "client") is None
    guard.release("read")
