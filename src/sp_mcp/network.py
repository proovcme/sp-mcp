import asyncio
import re
import ssl
import threading
import time
import zlib
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import truststore

from .errors import NormsError
from .faufcc import ASSET, ENTRIES, ENTRY
from .faufcc import HOST as FCS_HOST
from .storage import now

HOSTS = {"meganorm.ru", "new.gostinfo.ru", "www.gostinfo.ru", "gostinfo.ru", "protect.gost.ru", FCS_HOST}
GZIP_HOSTS = {"protect.gost.ru", FCS_HOST}


def validate_url(url):
    try:
        p = urlsplit(url)
        port = p.port
    except (TypeError, ValueError) as exc:
        raise NormsError("URL_NOT_ALLOWED", "Invalid source URL") from exc
    if p.scheme != "https" or p.hostname not in HOSTS or p.username or p.password or port not in (None, 443):
        raise NormsError(
            "URL_NOT_ALLOWED", "Only HTTPS URLs on configured normative source hosts are allowed"
        )
    if re.search(r"(?:%2f|%5c|%2e)", p.path, re.I) or ".." in p.path.split("/"):
        raise NormsError("URL_NOT_ALLOWED", "Encoded or parent path segments are not accepted")
    if p.hostname == FCS_HOST and not (
        p.path in {ENTRIES, "/robots.txt"} or ENTRY.fullmatch(p.path) or ASSET.fullmatch(p.path)
    ):
        raise NormsError(
            "URL_NOT_ALLOWED", "Only public FCS registry entries and original assets are supported"
        )
    return urldefrag(url)[0]


@dataclass
class NetworkBudget:
    deadline: float
    requests_left: int

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise NormsError("NETWORK_BUDGET_EXCEEDED", "Source operation time limit reached", retryable=True)
        return remaining


_budget = ContextVar("source_budget", default=None)


def bounded_network(method):
    """Search and batch checks share their budget with nested opens and catalogue reads."""

    @wraps(method)
    def run(service, *args, **kwargs):
        if _budget.get() is not None:
            return method(service, *args, **kwargs)
        token = _budget.set(NetworkBudget(time.monotonic() + service.config.operation_timeout, 36))
        try:
            return method(service, *args, **kwargs)
        finally:
            _budget.reset(token)

    return run


class Network:
    def __init__(self, config):
        self.config = config
        self.ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.robots = {}
        self.last = 0.0
        self.lock = threading.RLock()

    async def _download(self, url, timeout):
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/pdf,application/json,text/plain,application/xml",
            "Accept-Encoding": "gzip" if urlsplit(url).hostname in GZIP_HOSTS else "identity",
        }
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                verify=self.ssl_context, follow_redirects=False, timeout=timeout
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect and "location" in response.headers:
                        return {"redirect": urljoin(url, response.headers["location"])}
                    if not response.is_success:
                        code = response.status_code
                        raise NormsError(
                            "HTTP_ERROR",
                            f"Source returned HTTP {code}",
                            http_status=code,
                            retryable=code == 429 or code >= 500,
                        )
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding not in {"identity", headers["Accept-Encoding"]}:
                        raise NormsError("UNEXPECTED_ENCODING", "Source returned an unsupported encoding")
                    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
                    raw = bytearray()
                    wire_length = 0
                    async with aclosing(response.aiter_raw(chunk_size=65536)) as raw_stream:
                        async for chunk in raw_stream:
                            wire_length += len(chunk)
                            if wire_length > self.config.max_bytes:
                                raise NormsError(
                                    "RESPONSE_TOO_LARGE", "Source exceeds bounded download limit"
                                )
                            if inflater:
                                try:
                                    chunk = inflater.decompress(chunk, self.config.max_bytes - len(raw) + 1)
                                except zlib.error as exc:
                                    raise NormsError(
                                        "SOURCE_ENCODING_ERROR", "Malformed gzip transfer"
                                    ) from exc
                            if len(raw) + len(chunk) > self.config.max_bytes or (
                                inflater and inflater.unconsumed_tail
                            ):
                                raise NormsError(
                                    "RESPONSE_TOO_LARGE", "Source exceeds bounded download limit"
                                )
                            raw.extend(chunk)
                    if inflater and (not inflater.eof or inflater.unused_data):
                        raise NormsError("SOURCE_ENCODING_ERROR", "Incomplete or unexpected gzip transfer")
                    return {
                        "url": url,
                        "raw": bytes(raw),
                        "checked_at": now(),
                        "content_type": response.headers.get("content-type", "").split(";")[0],
                        "charset": response.charset_encoding,
                    }

    def _request(self, url):
        budget = _budget.get()
        wait = max(0, self.config.interval - (time.monotonic() - self.last))
        if budget and (budget.requests_left <= 0 or budget.remaining() <= wait):
            raise NormsError(
                "NETWORK_BUDGET_EXCEEDED", "Source operation request limit reached", retryable=True
            )
        if wait:
            time.sleep(wait)
        remaining = budget.remaining() if budget else self.config.timeout
        if budget:
            budget.requests_left -= 1
        self.last = time.monotonic()
        loop = asyncio.new_event_loop()
        try:
            source_timeout = 8 if urlsplit(url).hostname in GZIP_HOSTS else self.config.timeout
            return loop.run_until_complete(
                self._download(url, min(source_timeout, self.config.timeout, remaining))
            )
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            if budget:
                budget.remaining()
            raise NormsError(
                "NETWORK_ERROR", f"Source unavailable ({type(exc).__name__})", retryable=True
            ) from exc
        finally:
            # Do not wait for an OS resolver thread after cancelling a timed-out request.
            loop.close()

    def _check_robots(self, url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.hostname}"
        if origin not in self.robots:
            robot = RobotFileParser(origin + "/robots.txt")
            try:
                result = self._request(robot.url)
                if "redirect" in result:
                    raise NormsError(
                        "ROBOTS_UNAVAILABLE", "Cannot verify source crawl policy after robots redirect"
                    )
                robot.parse(result["raw"].decode("utf-8", errors="replace").splitlines())
            except NormsError as exc:
                if exc.http_status == 404:
                    robot.parse([])
                elif exc.code == "NETWORK_BUDGET_EXCEEDED":
                    raise
                else:
                    raise NormsError(
                        "ROBOTS_UNAVAILABLE", "Cannot verify source crawl policy", retryable=True
                    ) from exc
            self.robots[origin] = robot
        if not self.robots[origin].can_fetch(self.config.user_agent, url):
            raise NormsError("ROBOTS_DISALLOWED", "Source robots.txt excludes this URL")

    def fetch(self, url):
        budget = _budget.get()
        timeout = budget.remaining() if budget else self.config.timeout
        if not self.lock.acquire(timeout=timeout):
            raise NormsError(
                "NETWORK_BUDGET_EXCEEDED", "Source connection wait limit reached", retryable=True
            )
        try:
            seen = set()
            for _ in range(6):
                url = validate_url(url)
                if url in seen:
                    break
                seen.add(url)
                self._check_robots(url)
                result = self._request(url)
                if "redirect" not in result:
                    return result
                url = result["redirect"]
            raise NormsError("REDIRECT_LIMIT", "Source redirect limit reached")
        finally:
            self.lock.release()


def decode(fetched):
    raw = fetched["raw"]
    match = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:4000], re.I)
    encoding = fetched.get("charset") or (match[1].decode() if match else "utf-8")
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
