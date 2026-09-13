"""
基于 httpx.AsyncClient 的 FlareSolverr 接入封装。

设计要点：
1. 两个 AsyncClient 分工
   - _fs    : 调 FlareSolverr /v1（超时必须长，挑战可能耗时 30~60s）
   - _target: 携带 cf_clearance + User-Agent 直连目标站（快）
2. 挑战只解一次，cookie 过期前自动续期（提前 renew_margin 秒）
3. asyncio.Lock 防止并发请求同时触发求解（惊群）
4. 遇到 403/503 自动失效旧凭证、重解一次再重试
5. 浏览器 session 用 async with 自动 create / destroy

依赖: pip install httpx
"""

from __future__ import annotations

import asyncio
import time

from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlparse

import httpx

__all__ = ["AsyncFlareSolverrClient", "Clearance", "FlareSolverrError"]


class FlareSolverrError(RuntimeError):
    """FlareSolverr 返回 status != ok，或通信失败。"""


@dataclass
class Clearance:
    """一次挑战求解的结果。"""

    cookies: dict[str, str]
    user_agent: str
    expires_at: float

    @property
    def alive(self) -> bool:
        return time.time() < self.expires_at

    def as_header(self) -> str:
        """给 curl_cffi / 其它客户端复用的 Cookie 头字符串。"""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class AsyncFlareSolverrClient:
    def __init__(
        self,
        entry_url: str,
        *,
        fs_url: str = "http://localhost:8191/v1",
        session_name: str | None = "default",
        proxy: str | None = None,
        fs_proxy: str | None = None,
        max_timeout: int = 60_000,
        fs_timeout: float = 120.0,
        request_timeout: float = 30.0,
        renew_margin: float = 300.0,
        max_retries: int = 1,
        challenge_codes: tuple[int, ...] = (403, 503),
        challenge_markers: tuple[str, ...] = ("Just a moment", "cf-chl", "challenge-platform"),
        concurrency: int | None = None,
        follow_redirects: bool = True,
        verify: bool | str = True,
        default_headers: Mapping[str, str] | None = None,
        fs_user_agent: str | None = None,   # 强制无头浏览器使用的 UA（绕开站点对浏览器 UA 的封禁）
        # 测试钩子：注入 MockTransport
        fs_transport: httpx.AsyncBaseTransport | None = None,
        target_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not entry_url:
            raise ValueError("entry_url 不能为空，它是触发挑战的入口 URL")
        self.entry_url = entry_url
        self.fs_url = fs_url
        self.session_name = session_name
        self.proxy = proxy
        self.fs_proxy = fs_proxy
        self.max_timeout = max_timeout
        self.fs_timeout = fs_timeout
        self.request_timeout = request_timeout
        self.renew_margin = renew_margin
        self.max_retries = max_retries
        self.challenge_codes = challenge_codes
        self.challenge_markers = challenge_markers
        self.follow_redirects = follow_redirects
        self.verify = verify
        self.default_headers = dict(default_headers or {})
        self.fs_user_agent = fs_user_agent

        self._entry_host = urlparse(entry_url).hostname or ""
        self._fs: httpx.AsyncClient | None = None
        self._target: httpx.AsyncClient | None = None
        self._clearance: Clearance | None = None
        self._lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(concurrency) if concurrency else None
        )
        self._fs_transport = fs_transport
        self._target_transport = target_transport

    # ------------------------------------------------------------------ 生命周期
    async def __aenter__(self) -> "AsyncFlareSolverrClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._fs is not None:
            return
        self._lock = asyncio.Lock()
        self._fs = httpx.AsyncClient(
            timeout=httpx.Timeout(self.fs_timeout, connect=10.0),
            proxy=self.fs_proxy,          # 本地 FlareSolverr 一般留空
            transport=self._fs_transport,
        )
        self._target = httpx.AsyncClient(
            timeout=httpx.Timeout(self.request_timeout, connect=10.0),
            proxy=self.proxy,            # 必须与 FlareSolverr 出网 IP 一致
            follow_redirects=self.follow_redirects,
            verify=self.verify,
            transport=self._target_transport,
        )
        if self.session_name:
            await self._session_cmd("sessions.create")

    async def aclose(self) -> None:
        try:
            if self.session_name and self._fs is not None:
                await self._session_cmd("sessions.destroy")
        finally:
            for client in (self._target, self._fs):
                if client is not None:
                    await client.aclose()
            self._fs = self._target = None
            self._clearance = None

    async def _session_cmd(self, cmd: str) -> None:
        """sessions.create / sessions.destroy / sessions.list"""
        assert self._fs is not None
        payload: dict[str, Any] = {"cmd": cmd}
        if self.session_name:
            payload["session"] = self.session_name
        if cmd == "sessions.create" and self.proxy:
            payload["proxy"] = self._proxy_payload()
        r = await self._fs.post(self.fs_url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise FlareSolverrError(f"{cmd} 失败: {data.get('message')}")

    def _proxy_payload(self) -> dict[str, str]:
        assert self.proxy
        parsed = urlparse(self.proxy)
        payload = {"url": self.proxy}
        if parsed.username:
            payload["username"] = parsed.username
        if parsed.password:
            payload["password"] = parsed.password
        return payload

    # ------------------------------------------------------------------ 求解
    def invalidate(self) -> None:
        """主动丢弃当前 cf_clearance（换 IP、换 UA、被识别后调用）。"""
        self._clearance = None

    @property
    def clearance(self) -> Clearance | None:
        return self._clearance

    @property
    def started(self) -> bool:
        """客户端是否已 start（未关闭）。"""
        return self._fs is not None

    async def maybe_refresh(self) -> bool:
        """
        后台保活用：仅在凭证即将过期时才求解。
        返回 True 表示本次真的刷新了。
        """
        if self._fs is None:
            await self.start()
        if self._clearance and self._clearance.alive:
            return False
        await self.solve(force=True)
        return True

    async def _ensure_clearance(self) -> Clearance:
        if self._clearance and self._clearance.alive:
            return self._clearance
        assert self._lock is not None
        async with self._lock:                       # 并发下只解一次
            if self._clearance and self._clearance.alive:
                return self._clearance
            return await self._solve()

    async def solve(self, url: str | None = None, *, force: bool = False) -> Clearance:
        """手动触发一次求解。force=True 时忽略当前有效凭证。"""
        if self._fs is None:
            await self.start()
        assert self._lock is not None
        async with self._lock:
            if not force and self._clearance and self._clearance.alive:
                return self._clearance
            return await self._solve(url)

    async def _solve(self, url: str | None = None) -> Clearance:
        assert self._fs is not None and self._target is not None
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url or self.entry_url,
            "maxTimeout": self.max_timeout,
        }
        if self.session_name:
            payload["session"] = self.session_name
        if self.proxy:
            payload["proxy"] = self._proxy_payload()
        if self.fs_user_agent:
            payload["userAgent"] = self.fs_user_agent

        r = await self._fs.post(self.fs_url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise FlareSolverrError(
                f"求解失败: {data.get('message')} (code={data.get('code')}, url={payload['url']})"
            )

        sol = data["solution"]
        raw_cookies = sol.get("cookies") or []
        cookies = {c["name"]: c["value"] for c in raw_cookies}

        # 取最早的过期时间；没有 expiry 字段就按 30 分钟算
        expiries = [c.get("expiry") for c in raw_cookies if c.get("expiry")]
        expires_at = (min(expiries) - self.renew_margin) if expiries else time.time() + 1800

        # 写进 cookie jar，按域绑定，避免把 cf_clearance 发给无关站点
        self._target.cookies.clear()
        for c in raw_cookies:
            self._target.cookies.set(
                c["name"],
                c["value"],
                domain=(c.get("domain") or self._entry_host).lstrip("."),
                path=c.get("path") or "/",
            )

        self._clearance = Clearance(
            cookies=cookies,
            user_agent=sol.get("userAgent") or "",
            expires_at=expires_at,
        )
        return self._clearance

    # ------------------------------------------------------------------ 请求
    def _build_headers(self, user_headers: Mapping[str, str] | None) -> dict[str, str]:
        assert self._clearance is not None
        headers = {
            "User-Agent": self._clearance.user_agent,   # 必须与求解时逐字符一致
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Mode": "navigate",
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(self.default_headers)
        if user_headers:
            headers.update(user_headers)
        return headers

    def _looks_like_challenge(self, resp: httpx.Response) -> bool:
        if resp.status_code not in self.challenge_codes:
            return False
        if not self.challenge_markers:
            return True
        head = resp.text[:4000]
        return any(m in head for m in self.challenge_markers) or not head

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """
        与 httpx.AsyncClient.request 基本一致，额外自动处理 Cloudflare 挑战。
        注意：会自动带上 cf_clearance 与固定 UA，headers 里别再覆盖 User-Agent。
        """
        if self._target is None:
            await self.start()

        for attempt in range(self.max_retries + 1):
            await self._ensure_clearance()
            headers = self._build_headers(kwargs.pop("headers", None))

            if self._sem is None:
                resp = await self._target.request(method, url, headers=headers, **kwargs)
            else:
                async with self._sem:
                    resp = await self._target.request(method, url, headers=headers, **kwargs)

            if attempt < self.max_retries and self._looks_like_challenge(resp):
                self.invalidate()          # 凭证失效，下一轮重解
                continue
            return resp
        return resp  # pragma: no cover

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[httpx.Response]:
        """流式下载/大响应，需自己再走一次挑战校验。"""
        await self._ensure_clearance()
        headers = self._build_headers(kwargs.pop("headers", None))
        assert self._target is not None
        async with self._target.stream(method, url, headers=headers, **kwargs) as resp:
            yield resp
