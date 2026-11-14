import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

import truststore

from .errors import NormsError
from .storage import now

HOSTS = {"meganorm.ru", "new.gostinfo.ru", "www.gostinfo.ru", "gostinfo.ru", "protect.gost.ru"}


def validate_url(url):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname not in HOSTS
        or p.username
        or p.password
        or p.port not in (None, 443)
    ):
        raise NormsError(
            "URL_NOT_ALLOWED", "Only HTTPS URLs on configured normative source hosts are allowed"
        )
    if re.search(r"(?:%2f|%5c|%2e)", p.path, re.I) or ".." in p.path.split("/"):
        raise NormsError("URL_NOT_ALLOWED", "Encoded or parent path segments are not accepted")
    return urldefrag(url)[0]


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Network:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(
            HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)), Redirects()
        )
        self.robots = {}
        self.last = 0.0
        self.lock = threading.RLock()

    def _request(self, url):
        with self.lock:
            time.sleep(max(0, self.config.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            req = Request(
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/pdf,text/plain,application/xml",
                },
            )
            try:
                with self.opener.open(req, timeout=self.config.timeout) as response:
                    final = validate_url(response.url)
                    raw = response.read(self.config.max_bytes + 1)
                    if len(raw) > self.config.max_bytes:
                        raise NormsError("RESPONSE_TOO_LARGE", "Source exceeds bounded download limit")
                    return {
                        "url": final,
                        "raw": raw,
                        "checked_at": now(),
                        "content_type": response.headers.get_content_type(),
                        "charset": response.headers.get_content_charset(),
                    }
            except HTTPError as exc:
                raise NormsError(
                    "HTTP_ERROR",
                    f"Source returned HTTP {exc.code}",
                    http_status=exc.code,
                    retryable=exc.code == 429 or exc.code >= 500,
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                raise NormsError(
                    "NETWORK_ERROR", f"Source unavailable ({type(exc).__name__})", retryable=True
                ) from exc

    def fetch(self, url):
        url = validate_url(url)
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self.lock:
            if origin not in self.robots:
                robot = RobotFileParser(origin + "/robots.txt")
                try:
                    result = self._request(origin + "/robots.txt")
                    robot.parse(result["raw"].decode("utf-8", errors="replace").splitlines())
                except NormsError as exc:
                    if exc.http_status == 404:
                        robot.parse([])
                    else:
                        raise NormsError(
                            "ROBOTS_UNAVAILABLE", "Cannot verify source crawl policy", retryable=True
                        ) from exc
                self.robots[origin] = robot
            if not self.robots[origin].can_fetch(self.config.user_agent, url):
                raise NormsError("ROBOTS_DISALLOWED", "Source robots.txt excludes this URL")
            result = self._request(url)
            if result["url"] != url:
                final_p = urlsplit(result["url"])
                final_origin = f"{final_p.scheme}://{final_p.netloc}"
                if final_origin != origin:
                    # Do not consume cross-origin content before checking that source's policy.
                    return self.fetch(result["url"])
                if not self.robots[origin].can_fetch(self.config.user_agent, result["url"]):
                    raise NormsError("ROBOTS_DISALLOWED", "Redirect destination excluded by robots.txt")
            return result


def decode(fetched):
    raw = fetched["raw"]
    match = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:4000], re.I)
    encoding = fetched.get("charset") or (match[1].decode() if match else "utf-8")
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
