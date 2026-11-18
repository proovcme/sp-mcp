"""Buildless, model-free browser client over the shared service and optional Uvyazka bridge."""

import asyncio
import hmac
import inspect
import json
import os
import re
import secrets
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import __version__
from .errors import NormsError
from .uvyazka import bridge_call, prepare_link
from .verification import today_moscow

UI = Path(__file__).parent / "ui"
ASSETS = {"app.js": "text/javascript", "style.css": "text/css", "mark.svg": "image/svg+xml"}
WRITE_METHODS = {"uvyazka_attach"}


def reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def methods_for(service):
    methods = {
        name: getattr(service, name)
        for name in (
            "sources",
            "search",
            "browse",
            "open",
            "read",
            "outline",
            "read_clause",
            "search_document",
            "table",
            "check",
            "batch_check",
            "compare",
            "library",
            "document",
            "contents",
            "verifications",
            "search_status",
            "prepare_search",
        )
    }

    def uvyazka_projects():
        return bridge_call("list_projects", {})

    def uvyazka_documents(project_id: str, query="", offset=0):
        return bridge_call(
            "find_documents", {"project_id": project_id, "query": query, "limit": 30, "offset": offset}
        )

    def uvyazka_link_state(project_id: str, analysis_id: str):
        result = bridge_call(
            "get_project_structure",
            {"project_id": project_id, "kind": "analyses", "query": analysis_id, "limit": 100},
        )
        found = [
            item
            for item in result.get("items", [])
            if item.get("analysis_id") == analysis_id and item.get("reader") == "model"
        ]
        return {"revision": max((item["revision"] for item in found), default=0)}

    def uvyazka_prepare(
        project_id: str,
        project_source: dict,
        snapshot_id: str,
        verification_id: str,
        clause=None,
        expected_revision=0,
        reason="",
    ):
        return prepare_link(
            service,
            project_id,
            project_source,
            snapshot_id,
            verification_id,
            clause,
            expected_revision,
            reason,
        )

    def uvyazka_attach(
        project_id: str,
        project_source: dict,
        snapshot_id: str,
        verification_id: str,
        clause=None,
        expected_revision=0,
        reason="",
    ):
        payload = uvyazka_prepare(
            project_id, project_source, snapshot_id, verification_id, clause, expected_revision, reason
        )
        return {
            "published": True,
            "project_id": project_id,
            "uvyazka_result": bridge_call(payload["tool"], payload["arguments"]),
        }

    methods.update(
        uvyazka_projects=uvyazka_projects,
        uvyazka_documents=uvyazka_documents,
        uvyazka_link_state=uvyazka_link_state,
        uvyazka_prepare=uvyazka_prepare,
        uvyazka_attach=uvyazka_attach,
    )
    return methods


def make_http_app(server, service, host):
    methods = methods_for(service)
    csrf_token = secrets.token_urlsafe(32)

    async def api(request):
        name = request.path_params["method"]
        method = methods.get(name)
        if not method:
            return JSONResponse({"error_code": "METHOD_NOT_FOUND"}, status_code=404)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error_code": "JSON_REQUIRED"}, status_code=415)
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin != expected_origin:
            return JSONResponse({"error_code": "ORIGIN_REJECTED"}, status_code=403)
        if name in WRITE_METHODS and not hmac.compare_digest(
            request.headers.get("x-sp-token", "").encode(), csrf_token.encode()
        ):
            return JSONResponse({"error_code": "WRITE_TOKEN_REQUIRED"}, status_code=403)
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        return JSONResponse({"error_code": "REQUEST_TOO_LARGE"}, status_code=413)
        except TimeoutError:
            return JSONResponse({"error_code": "REQUEST_TIMEOUT"}, status_code=408)
        try:
            args = json.loads(body or b"{}", parse_constant=reject_json_constant)
            if not isinstance(args, dict):
                raise ValueError("Body must be a JSON object")
            signature = inspect.signature(method)
            signature.bind(**args)
            for key, value in args.items():
                parameter = signature.parameters[key]
                if value is None and parameter.default is None:
                    continue
                expected = parameter.annotation
                if expected is inspect.Parameter.empty:
                    expected = str if parameter.default is None else type(parameter.default)
                if type(value) is not expected:
                    raise NormsError("INVALID_ARGUMENT", f"{key} must be {expected.__name__}")
            if (
                os.getenv("SP_MCP_PUBLIC_LIMITS") == "1"
                and name == "batch_check"
                and len(args.get("items", [])) > 2
            ):
                raise NormsError(
                    "INVALID_ARGUMENT", "Публичный сервер принимает не более двух нормативов за пакет."
                )
            result = await run_in_threadpool(method, **args)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except NormsError as exc:
            return JSONResponse(exc.payload(), status_code=400)
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse(NormsError("INVALID_ARGUMENT", str(exc)).payload(), status_code=400)

    async def ui_config(request):
        return JSONResponse(
            {
                "version": __version__,
                "write_token": csrf_token,
                "today": today_moscow().isoformat(),
                "uvyazka_configured": bool(os.getenv("SP_MCP_UVYAZKA_COMMAND")),
                "search_requires_ai": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def health(request):
        return JSONResponse({"name": "sp-mcp", "version": __version__, "status": "ok"})

    async def index(request):
        return FileResponse(
            UI / "index.html",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'",
            },
        )

    async def asset(request):
        name = request.path_params["name"]
        if name not in ASSETS:
            return JSONResponse({"error_code": "ASSET_NOT_FOUND"}, status_code=404)
        return FileResponse(UI / name, media_type=ASSETS[name], headers={"Cache-Control": "no-cache"})

    app = server.streamable_http_app(host=host, stateless_http=True, json_response=True)
    app.routes.extend(
        [
            Route("/", index),
            Route("/assets/{name}", asset),
            Route("/api/ui-config", ui_config),
            Route("/api/{method}", api, methods=["POST"]),
            Route("/health", health),
        ]
    )
    allowed_hosts = {"localhost", "127.0.0.1", "::1", host}
    allowed_hosts.update(
        value.strip() for value in os.getenv("SP_MCP_ALLOWED_HOSTS", "").split(",") if value.strip()
    )
    app.add_middleware(HostMiddleware, allowed_hosts=allowed_hosts)
    return app


class HostMiddleware:
    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed_hosts = {host.lower() for host in allowed_hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [value for key, value in scope["headers"] if key == b"host"]
            match = (
                re.fullmatch(rb"(\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?", values[0])
                if len(values) == 1
                else None
            )
            if not match or match[1].decode("ascii").strip("[]").lower() not in self.allowed_hosts:
                return await JSONResponse({"error_code": "HOST_REJECTED"}, status_code=400)(
                    scope, receive, send
                )
        return await self.app(scope, receive, send)
