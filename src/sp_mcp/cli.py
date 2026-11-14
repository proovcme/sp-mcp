import argparse
import hmac
import inspect
import json
import os

from . import __version__
from .config import Config
from .errors import NormsError
from .service import Service


class BearerMiddleware:
    """Constant-time bearer guard, following the fgis-mcp HTTP deployment pattern."""

    def __init__(self, app, token):
        self.app = app
        self.expected = ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def make_http_app(server, service, host):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Route

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
        )
    }

    async def api(request):
        method = methods.get(request.path_params["method"])
        if not method:
            return JSONResponse({"error_code": "METHOD_NOT_FOUND"}, status_code=404)
        if int(request.headers.get("content-length", "0")) > 65536:
            return JSONResponse({"error_code": "REQUEST_TOO_LARGE"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                return JSONResponse({"error_code": "REQUEST_TOO_LARGE"}, status_code=413)
        try:
            args = json.loads(body or b"{}")
            if not isinstance(args, dict):
                raise ValueError("Body must be a JSON object")
            inspect.signature(method).bind(**args)
            result = await run_in_threadpool(method, **args)
            return JSONResponse(result)
        except NormsError as exc:
            return JSONResponse(exc.payload(), status_code=400)
        except (ValueError, TypeError) as exc:
            return JSONResponse(NormsError("INVALID_ARGUMENT", str(exc)).payload(), status_code=400)

    async def health(request):
        return JSONResponse({"name": "sp-mcp", "version": __version__, "status": "ok"})

    app = server.streamable_http_app(host=host, stateless_http=True, json_response=True)
    app.routes.extend([Route("/api/{method}", api, methods=["POST"]), Route("/health", health)])
    return app


def main():
    parser = argparse.ArgumentParser(description="Search and verify Russian construction normative documents")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "sources", "search", "open", "check", "read"]
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--query")
    parser.add_argument("--url")
    parser.add_argument("--code")
    parser.add_argument("--snapshot")
    parser.add_argument("--as-of")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--latest-revision", action="store_true")
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if args.command == "serve":
            from .server import create_server

            server, service = create_server(config)
            if args.transport == "stdio":
                server.run()
                return
            token = os.getenv("SP_MCP_HTTP_TOKEN", "")
            if args.host not in {"127.0.0.1", "::1", "localhost"} and len(token) < 32:
                parser.error("Non-loopback HTTP requires SP_MCP_HTTP_TOKEN (at least 32 characters)")
            import uvicorn

            app = make_http_app(server, service, args.host)
            if token:
                app = BearerMiddleware(app, token)
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
            return
        service = Service(config)
        if args.command == "sources":
            result = service.sources()
        elif args.command == "search":
            if not args.query:
                parser.error("search requires --query")
            result = service.search(args.query, online=not args.offline, refresh=args.refresh)
        elif args.command == "open":
            if not args.url:
                parser.error("open requires --url")
            result = service.open(args.url, refresh=args.refresh, latest_revision=args.latest_revision)
        elif args.command == "check":
            if not args.code:
                parser.error("check requires --code")
            result = service.check(args.code, args.snapshot, args.as_of, refresh=True)
        else:
            if not args.snapshot:
                parser.error("read requires --snapshot")
            result = service.read(args.snapshot)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except NormsError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
