"""
FlareSolverr 接入封装（目标站客户端可选带浏览器指纹）。

设计要点：
1. 两个客户端分工
   - _fs    : 调 FlareSolverr /v1（超时必须长，挑战可能耗时 30~60s），固定 httpx
   - _target: 携带 cf_clearance + User-Agent（+ 浏览器指纹）直连目标站（快）
2. 挑战只解一次，cookie 过期前自动续期（提前 renew_margin 秒）
3. asyncio.Lock 防止并发请求同时触发求解（惊群）
4. 遇到 403/503 自动失效旧凭证、重解一次再重试
5. 浏览器 session 用 async with 自动 create / destroy
6. 过 Cloudflare 要"Cookie + 指纹"同时成立：
   - Cookie  : cf_clearance（本客户端解出）+ browser.cookies（用户自填）
   - 指纹    : browser.impersonate -> curl_cffi（TLS/JA3 + HTTP/2 + 头顺序）
   两者不一致（比如指纹是 Chrome、UA 却写着脚本 UA）会立刻被打回 403。
   开启指纹且 sync_ua=True 时，本客户端会把指纹 UA 同步给无头浏览器，
   保证「解挑战的 UA == 请求的 UA == 指纹的 UA」。

依赖: pip install httpx（必需）；pip install curl_cffi（可选，浏览器指纹）
"""

from __future__ import annotations

import asyncio
import logging
import time

from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlparse

import httpx

from .browser_profile import (
    BrowserProfile,
    aclose_client,
    cookie_to_header,
    create_client,
    host_of,
    inject_cookies,
)

logger = logging.getLogger(__name__)

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

    def merged_with(self, extra: Mapping[str, str]) -> dict[str, str]:
        """用户自填 Cookie 打底，cf_clearance 覆盖同名项（解出来的最新）。"""
        merged = dict(extra or {})
        merged.update(self.cookies)
        return merged


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
        browser: BrowserProfile | None = None,  # 浏览器指纹 + 自定义 Cookie（见 utils.browser_profile）
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
        self.browser = browser or BrowserProfile()

        self._entry_host = host_of(entry_url)
        # Cookie 绑定到哪些域名：站点主域 + 用户在 browser.cookie_domains 里补的
        # （图片常放在 CDN 子域，默认不给它们发 cf_clearance）
        self._cookie_domains = self.browser.domains_for(entry_url)
        self._fs: httpx.AsyncClient | None = None
        self._target: Any | None = None
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
        self._target = create_client(
            timeout=self.request_timeout,
            proxy=self.proxy,            # 必须与 FlareSolverr 出网 IP 一致
            verify=self.verify,
            follow_redirects=self.follow_redirects,
            headers=self.default_headers,
            cookies=dict(self.browser.cookies),
            cookie_domains=self._cookie_domains,
            browser=self.browser,
            transport=self._target_transport,
        )

        # 指纹开启时，无头浏览器的 UA 必须换成指纹 UA —— cf_clearance 跟 UA 绑定，
        # 拿脚本 UA 去解、再用 Chrome 指纹去请求，cookie 当场失效。
        if self.browser.enabled and self.browser.sync_ua and not self.fs_user_agent:
            ua = self.browser.effective_ua()
            if ua:
                self.fs_user_agent = ua
                logger.info(
                    "已把浏览器指纹 %s 的 UA 同步给 FlareSolverr: %s",
                    self.browser.impersonate, ua,
                )
        elif (self.browser.wants_fingerprint and self.browser.sync_ua
              and self.fs_user_agent and self.fs_user_agent != self.browser.effective_ua()):
            logger.warning(
                "flaresolverr.user_agent 与浏览器指纹 %s 的 UA 不一致，cf_clearance 可能失效；"
                "要么清空 user_agent 走自动同步，要么把 browser.sync_ua 设为 false",
                self.browser.impersonate,
            )

        self._apply_cookies()
        if self.session_name:
            await self._session_cmd("sessions.create")

    async def aclose(self) -> None:
        try:
            if self.session_name and self._fs is not None:
                await self._session_cmd("sessions.destroy")
        finally:
            for client in (self._target, self._fs):
                await aclose_client(client)
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

        # FlareSolverr 可能给出带子域的 domain（.e621.net），一并记进绑定列表
        extra_domains = [host_of(c.get("domain") or "") for c in raw_cookies]
        if extra_domains:
            merged_domains = list(self._cookie_domains) + [d for d in extra_domains if d]
            self._cookie_domains = tuple(dict.fromkeys(merged_domains))

        self._clearance = Clearance(
            cookies=cookies,
            user_agent=sol.get("userAgent") or "",
            expires_at=expires_at,
        )
        self._apply_cookies()
        return self._clearance

    # ------------------------------------------------------------------ Cookie
    def _cookie_payload(self) -> dict[str, str]:
        """最终要带上的 Cookie：用户自填的打底，cf_clearance 覆盖同名项。"""
        if self._clearance is None:
            return dict(self.browser.cookies)
        return self._clearance.merged_with(self.browser.cookies)

    def _apply_cookies(self) -> None:
        """把 Cookie 写进目标客户端的 jar。

        必须走 jar：curl_cffi 会用 jar 覆盖手动传的 Cookie 头，塞 header 会被丢掉。
        """
        if self._target is None:
            return
        cookies = self._cookie_payload()
        try:
            self._target.cookies.clear()      # 清掉过期的 cf_clearance 与临时 cookie
        except Exception as e:
            logger.warning("清空 cookie jar 失败: %s", e)
        inject_cookies(self._target, cookies, self._cookie_domains)

    def current_cookie_header(self) -> str:
        """当前 Cookie 的字符串形式（诊断用，如 /fs 状态）。"""
        return cookie_to_header(self._cookie_payload())

    # ------------------------------------------------------------------ 请求
    def _build_headers(self, user_headers: Mapping[str, str] | None) -> dict[str, str]:
        assert self._clearance is not None
        # UA 必须与求解时逐字符一致；开启指纹时由 browser.effective_ua 统一成指纹 UA
        ua = self.browser.effective_ua(self._clearance.user_agent)
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Mode": "navigate",
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(self.browser.headers)
        headers.update(self.default_headers)
        if user_headers:
            headers.update(user_headers)
        return headers

    def current_user_agent(self) -> str:
        """当前请求实际使用的 UA（诊断用）。"""
        base = self._clearance.user_agent if self._clearance else ""
        return self.browser.effective_ua(base)

    def _looks_like_challenge(self, resp: Any) -> bool:
        if resp.status_code not in self.challenge_codes:
            return False
        if not self.challenge_markers:
            return True
        head = resp.text[:4000]
        return any(m in head for m in self.challenge_markers) or not head

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """
        与 httpx.AsyncClient.request 基本一致，额外自动处理 Cloudflare 挑战。
        注意：会自动带上 cf_clearance、自定义 Cookie 与固定 UA，headers 里别再覆盖 User-Agent；
        响应对象可能是 httpx.Response 或 curl_cffi.Response（都支持 status_code / text / json / content）。
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

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[Any]:
        """流式下载/大响应，需自己再走一次挑战校验。"""
        await self._ensure_clearance()
        headers = self._build_headers(kwargs.pop("headers", None))
        assert self._target is not None
        async with self._target.stream(method, url, headers=headers, **kwargs) as resp:
            yield resp
