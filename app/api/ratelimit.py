"""应用层速率限制中间件：按来访 IP 滑动窗口计数，超限返回 429。

Why（设计决策）：
- Nginx 已在边缘限流（60r/m），应用层再做一层是因为 Nginx 限流是粗粒度
  （所有路径同一配额），而创建会话（POST /api/sessions）会真实消耗上游
  LLM 额度，应该按 IP 收得更紧：默认 10 次/分钟。
- 只对写操作（POST/PUT/DELETE）计数：GET 是查询/SSE 流，不受业务限流。
- 单进程内存实现即可：uvicorn 单 worker 部署，无需 Redis。
- TRUST_PROXY_HEADERS=true 时优先取 X-Forwarded-For 首个地址（真实访客
  IP），否则取 scope["client"]；保证 nginx 反代后限的是访客而不是代理。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.types import ASGIApp, Receive, Scope, Send

_JSON_429: bytes = (
    '{"error":{"code":"rate_limited","message":"请求太频繁，请稍后再试"}}'
).encode()


class SlidingWindowRateLimiter:
    """按 key（IP）的滑动窗口计数器。线程安全（uvicorn 单线程事件循环足够）。"""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        window = self._hits[key]
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True


class RateLimitMiddleware:
    """FastAPI/Starlette ASGI 中间件：写请求按 IP 限流。"""

    def __init__(
        self,
        app: ASGIApp,
        limit: int,
        window_seconds: float = 60.0,
        trust_proxy: bool = False,
    ) -> None:
        self.app = app
        self.limiter = SlidingWindowRateLimiter(limit, window_seconds)
        self.trust_proxy = trust_proxy

    def _client_ip(self, scope: Scope) -> str:
        if self.trust_proxy:
            for name, value in scope.get("headers") or ():
                if name == b"x-forwarded-for":
                    first = value.decode("latin-1").split(",")[0].strip()
                    if first:
                        return first
        return (scope.get("client") or ("unknown", 0))[0]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if method in ("POST", "PUT", "DELETE"):
            key = self._client_ip(scope)
            if not self.limiter.allow(key):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(_JSON_429)).encode()),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": _JSON_429,
                        "more_body": False,
                    }
                )
                return
        await self.app(scope, receive, send)