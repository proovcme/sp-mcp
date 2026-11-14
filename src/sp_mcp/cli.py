import argparse
import hmac
import json
import os

from . import __version__
from .config import Config
from .errors import NormsError
from .service import Service
from .web import make_http_app


class BearerMiddleware:
    """Constant-time bearer guard, following the fgis-mcp HTTP deployment pattern."""

    def __init__(self, app, token):
        self.app = app
        self.expected = ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("method") in {"GET", "HEAD"} and (
                scope.get("path") == "/" or scope.get("path", "").startswith("/assets/")
            ):
                return await self.app(scope, receive, send)
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


def main():
    parser = argparse.ArgumentParser(description="Search and verify Russian construction normative documents")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "web", "sources", "search", "open", "check", "read"],
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
        if args.command in {"serve", "web"}:
            from .server import create_server

            server, service = create_server(config)
            if args.transport == "stdio" and args.command != "web":
                server.run()
                return
            token = os.getenv("SP_MCP_HTTP_TOKEN", "")
            if args.host not in {"127.0.0.1", "::1", "localhost"} and len(token) < 32:
                parser.error("Non-loopback HTTP requires SP_MCP_HTTP_TOKEN (at least 32 characters)")
            import uvicorn

            app = make_http_app(server, service, args.host)
            if os.getenv("SP_MCP_PUBLIC_LIMITS") == "1":
                from .public_limits import PublicLimits

                app = PublicLimits(app, config.root)
            if token:
                app = BearerMiddleware(app, token)
            if args.command == "web":
                print(f"SP MCP: http://{args.host}:{args.port}", flush=True)
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
