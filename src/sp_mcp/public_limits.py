"""Fail-fast budgets for an anonymous public normative service; no waiting request queue."""

import math
import os
import shutil
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from starlette.responses import JSONResponse

HEAVY = {"search", "open", "browse", "check", "batch_check", "compare", "prepare_search"}


class PublicLimits:
    def __init__(
        self, app, root: Path, *, clock=time.monotonic, disk_reserve=1024**3, data_limit=512 * 1024**2
    ):
        self.app, self.root, self.clock = app, root, clock
        self.disk_reserve, self.data_limit = disk_reserve, data_limit
        self.lock = threading.Lock()
        self.active = self.heavy_active = 0
        self.global_requests, self.global_heavy = deque(), deque()
        self.clients = OrderedDict()
        self.storage_checked, self.storage_full = -float("inf"), False

    @staticmethod
    def trim(queue, now):
        while queue and queue[0] <= now - 60:
            queue.popleft()

    def storage_available(self, now):
        if now - self.storage_checked < 30:
            return not self.storage_full
        total, count = 0, 0
        full = shutil.disk_usage(self.root).free < self.disk_reserve
        for parent, dirs, files in os.walk(self.root, followlinks=False):
            if full:
                break
            for name in files:
                total += os.stat(Path(parent) / name, follow_symlinks=False).st_size
                count += 1
                if total >= self.data_limit or count >= 10000:
                    full = True
                    break
        self.storage_checked, self.storage_full = now, full
        return not full

    def acquire(self, method, client):
        heavy, now = method in HEAVY, self.clock()
        with self.lock:
            self.clients.move_to_end(client) if client in self.clients else None
            if client not in self.clients:
                if len(self.clients) >= 2048:
                    self.clients.popitem(last=False)
                self.clients[client] = (deque(), deque())
            requests, operations = self.clients[client]
            budgets = [(self.global_requests, 240), (requests, 90)]
            if heavy:
                budgets += [(self.global_heavy, 12), (operations, 6)]
            for queue, limit in budgets:
                self.trim(queue, now)
                if len(queue) >= limit:
                    return 429, "RATE_LIMIT", max(1, math.ceil(queue[0] + 60 - now))
            if self.active >= 4 or heavy and self.heavy_active >= 1:
                return 503, "SERVER_BUSY", 5
            if heavy and not self.storage_available(now):
                return 503, "STORAGE_LIMIT", 60
            for queue, _ in budgets:
                queue.append(now)
            self.active += 1
            self.heavy_active += heavy
        return None

    def release(self, method):
        with self.lock:
            self.active -= 1
            self.heavy_active -= method in HEAVY
            if method in HEAVY:
                self.storage_checked = -float("inf")

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/"):
            return await self.app(scope, receive, send)
        method = path.removeprefix("/api/")
        client = (scope.get("client") or ("unknown",))[0]
        refusal = self.acquire(method, client)
        if refusal:
            status, code, retry = refusal
            messages = {
                "RATE_LIMIT": "Слишком частые запросы. Подождите немного и повторите поиск.",
                "SERVER_BUSY": "Сервер занят другим запросом. Повторите через несколько секунд.",
                "STORAGE_LIMIT": "Достигнут лимит загрузок сервера. Чтение сохранённых документов доступно.",
            }
            response = JSONResponse(
                {"error_code": code, "message": messages[code], "retryable": code != "STORAGE_LIMIT"},
                status_code=status,
                headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        try:
            return await self.app(scope, receive, send)
        finally:
            self.release(method)
