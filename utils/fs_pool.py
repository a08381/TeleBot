"""取图客户端的模块级单例 + 后台保活（FlareSolverr / 直连 两种通道）。

关键约束：
1. 必须放在 **顶层模块**（不放在 plugins/ 下）。
   reload_all_plugins() 会 pop 掉所有 "plugins." 开头的 sys.modules，
   单例若写在插件里，热重载一次就丢了。
2. 单例只能在 **事件循环内** 创建/关闭，因此挂到 PTB 的 post_init / post_shutdown。
3. 挑战只解一次，cookie 由后台任务在过期前自动续，用户请求永远不等待浏览器。

两种通道（同一个 fs() 入口，调用方无感）：
- ``use_fs=True``  AsyncFlareSolverrClient：先解 Cloudflare 挑战，再带 cf_clearance 直连
- ``use_fs=False`` DirectClient：普通 httpx 直连目标站，只带 UA，不碰 FlareSolverr
  适合站点没上挑战、或 FlareSolverr 挂了/太慢时临时切走

切换方式（插件侧开关见 plugins/yiff.py 的 /fs 指令）：
- 改 config/<插件>.json 的 flaresolverr.enabled，然后 /reload
- 或运行时 await set_mode(False/True)，立即生效并自动重建客户端

配置分两处：
- **站点相关**（entry_url / session_name / user_agent / enabled）由使用它的插件提供：
  插件 import 时调用 configure_site(...)，例见 plugins/yiff.py。
  这些值描述的是「要访问哪个站、走不走 FlareSolverr」，属于业务，
  不是 FlareSolverr 本身的参数。
- **客户端行为**（地址、超时、重试、并发等）在主配置 config.json 的 flaresolverr 段，
  见 utils.config.FlareSolverrSettings。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Optional, Union
from urllib.parse import urlparse

import httpx

from .async_flaresolverr import AsyncFlareSolverrClient
from .config import get_config

logger = logging.getLogger(__name__)

# 插件没给 user_agent 时直连用的兜底 UA（e621/e926 这类站要求 UA 非空且不能是浏览器 UA）
DEFAULT_DIRECT_UA = "TeleBot/1.0 (direct)"


@dataclass(frozen=True)
class SiteProfile:
    """目标站的身份信息 —— 决定「用哪个浏览器 session 解哪个站的挑战」。"""

    site_url: str                       # 站点根地址，如 https://e926.net
    entry_url: str                      # 触发挑战的入口页，默认就是站点根
    session_name: str = "default"       # FlareSolverr session，复用同一个浏览器实例
    user_agent: Optional[str] = None    # 强制无头浏览器使用的 UA（有些站禁浏览器 UA）
    source: str = ""                    # 谁注册的（插件名），仅用于日志
    use_fs: bool = True                 # True=走 FlareSolverr 解挑战；False=纯直连

    def __post_init__(self) -> None:
        if not self.site_url and not self.entry_url:
            raise ValueError("site_url 与 entry_url 至少要有一个")


def _normalize_entry(url: str) -> str:
    """站点根地址 -> 入口页。只补路径，不动查询串。"""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:                # 用户只写了 e926.net
        url, parsed = f"https://{url}", urlparse(f"https://{url}")
    if not parsed.path:
        url = url.rstrip("/") + "/"      # https://e926.net -> https://e926.net/
    return url


def _derive_session(entry_url: str) -> str:
    """入口页 -> session 名。取域名（e926.net），没有域名就退回 default。"""
    return urlparse(entry_url).hostname.replace(".", "_") or "default"


# ----------------------------------------------------------------- 站点注册
_current_site: Optional[SiteProfile] = None


def configure_site(
    site_url: str,
    user_agent: Optional[str] = None,
    *,
    entry_url: Optional[str] = None,
    session_name: Optional[str] = None,
    source: str = "",
    enabled: bool = True,
) -> SiteProfile:
    """由插件声明「我要访问哪个站、走不走 FlareSolverr」。

    只需要站点地址：entry_url 默认就是站点根，session_name 默认取站点域名。
    少数站点的根地址不触发挑战、必须用特定 URL 解时，才需要单独指定 entry_url。

    enabled=False 时 fs() 返回直连客户端（DirectClient），不启动浏览器、不连
    FlareSolverr；站点没上挑战或 FlareSolverr 不可用时用它。

    一般在插件模块顶层调用（import 时执行），早于任何 fs() 调用。
    重复调用会覆盖；换了一个来源来覆盖时会打 warning，便于发现两个插件抢同一个单例。
    """
    global _current_site
    previous = _current_site
    if previous is not None and source and previous.source and source != previous.source:
        logger.warning(
            "FlareSolverr 站点信息被 %s 覆盖（原为 %s）；单例只有一个，"
            "两个插件访问不同站点会互相抢占",
            source, previous.source,
        )

    resolved_entry = _normalize_entry(entry_url or site_url or "")
    if not resolved_entry:
        raise ValueError("site_url 不能为空，无法确定要访问哪个站")

    _current_site = SiteProfile(
        site_url=(site_url or "").strip().rstrip("/"),
        entry_url=resolved_entry,
        session_name=session_name or _derive_session(resolved_entry),
        user_agent=user_agent,
        source=source,
        use_fs=bool(enabled),
    )
    return _current_site


def get_site() -> Optional[SiteProfile]:
    return _current_site


def clear_site() -> None:
    global _current_site
    _current_site = None


# ----------------------------------------------------------------- 直连客户端
class DirectClient:
    """不走 FlareSolverr 的直连客户端。

    接口对齐 AsyncFlareSolverrClient 的常用部分（get / request / start / aclose /
    started），这样 fs() 的调用方（图池、插件）不用关心当前是哪条通道。

    与 FlareSolverr 通道的区别：
    - 没有 cf_clearance，遇到 Cloudflare 挑战页会直接拿到 403/503 的 HTML
    - 请求头自己带 UA（站点通常要求 UA 非空），不带 cookie
    """

    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        timeout: float = 20.0,
        headers: Optional[dict[str, str]] = None,
        proxy: Optional[str] = None,
        follow_redirects: bool = True,
        verify: bool = True,
        transport: Optional[httpx.AsyncBaseTransport] = None,  # 测试钩子：注入 MockTransport
    ) -> None:
        self.user_agent = user_agent or DEFAULT_DIRECT_UA
        self.timeout = timeout
        self.extra_headers = dict(headers or {})
        self.proxy = proxy
        self.follow_redirects = follow_redirects
        self.verify = verify
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._client is not None:
            return
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        headers.update(self.extra_headers)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            headers=headers,
            proxy=self.proxy,
            follow_redirects=self.follow_redirects,
            verify=self.verify,
            transport=self._transport,
        )

    @property
    def started(self) -> bool:
        return self._client is not None

    @property
    def clearance(self) -> None:
        """直连没有 cf_clearance，这里返回 None 以对齐 FlareSolverr 客户端。"""
        return None

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            await self.start()
        assert self._client is not None
        headers = dict(self.extra_headers)          # 单请求的头覆盖客户端默认头
        user_headers = kwargs.pop("headers", None)
        if user_headers:
            headers.update(user_headers)
        return await self._client.request(method, url, headers=headers, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


# fs() 的返回类型：两种通道都实现 get / request / aclose / started
AnyClient = Union[AsyncFlareSolverrClient, DirectClient]


# ----------------------------------------------------------------- 单例状态
_client: Optional[AnyClient] = None
_client_site: Optional[SiteProfile] = None   # 建 _client 时用的站点信息
_lock: Optional[asyncio.Lock] = None
_refresh_task: Optional[asyncio.Task] = None
_warm_task: Optional[asyncio.Task] = None
_stop: Optional[asyncio.Event] = None


def _get_lock() -> asyncio.Lock:
    """惰性创建，避免在 import 阶段绑定事件循环。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _is_fs_client(client: Optional[AnyClient]) -> bool:
    """当前客户端是不是 FlareSolverr 通道。"""
    return isinstance(client, AsyncFlareSolverrClient)


async def _drop_client() -> None:
    """关掉当前客户端（换站点 / 换通道时先清场）。"""
    global _client, _client_site
    if _client is None:
        return
    old = _client
    _client, _client_site = None, None
    try:
        await old.aclose()
    except Exception as e:
        logger.warning("关闭旧客户端失败: %s", e)


async def fs() -> AnyClient:
    """获取全局唯一的取图客户端（FlareSolverr 或直连，取决于站点配置的 enabled）。

    不存在则创建；FlareSolverr 通道还会立即预热（解一次挑战）。
    """
    global _client, _client_site

    site = _current_site
    if site is None:
        raise RuntimeError(
            "还没有插件调用 configure_site() 声明目标站点，无法创建取图客户端"
        )

    if _client is not None and _client.started:
        if site == _client_site:
            return _client
        # 站点或通道变了（改了插件配置 + /reload，或 set_mode 切换）：
        # 旧 session 属于另一个站/另一条通道，必须重建
        logger.info(
            "站点信息已变更（%s，%s），重建客户端",
            site.entry_url, "FlareSolverr" if site.use_fs else "直连",
        )
        await _drop_client()

    cfg = get_config().flaresolverr
    async with _get_lock():
        if _client is not None and _client.started and site == _client_site:
            return _client

        if site.use_fs:
            client: AnyClient = AsyncFlareSolverrClient(
                entry_url=site.entry_url,
                fs_url=cfg.url,
                session_name=site.session_name,
                fs_user_agent=site.user_agent,   # 让无头浏览器带上合规 UA
                fs_timeout=cfg.fs_timeout,
                request_timeout=cfg.request_timeout,
                renew_margin=cfg.renew_margin,   # 过期前 N 秒就换
                max_retries=cfg.max_retries,
                concurrency=cfg.concurrency,
            )
        else:
            client = DirectClient(
                user_agent=site.user_agent,      # 站点要求的合规 UA
                timeout=cfg.request_timeout,
            )

        await client.start()
        _client = client
        _client_site = site
        logger.info(
            "取图客户端已启动（%s，站点=%s，来源=%s）",
            "FlareSolverr" if site.use_fs else "直连",
            site.entry_url, site.source or "未知",
        )

        # 预热必须放在锁内：否则并发调用者会拿到还没解出 cookie 的客户端
        if site.use_fs and isinstance(client, AsyncFlareSolverrClient):
            try:
                await client.solve()
                logger.info("FlareSolverr 预热完成")
            except Exception as e:            # 预热失败不致命，请求时会重试
                logger.warning("FlareSolverr 预热失败，将在首次请求时重试: %s", e)

    return _client


# ------------------------------------------------------- 运行时切换通道
async def set_mode(use_fs: bool, *, warm: bool = True) -> SiteProfile:
    """运行时切换 直连 / FlareSolverr，立即生效。

    做三件事：丢掉旧客户端（FlareSolverr 会 destroy 浏览器 session，
    省得后台一直挂着浏览器）-> 更新站点信息 -> 按需后台预热。
    注意这只改内存；想让重启后也生效，调用方要把开关写回配置文件
    （plugins/yiff.py 的 /fs 指令就是这么做的）。
    """
    global _current_site, _warm_task

    site = _current_site
    if site is None:
        raise RuntimeError("还没有插件调用 configure_site()，无法切换通道")

    if site.use_fs == use_fs and _client is not None and _is_fs_client(_client) == use_fs:
        return site                                # 已经是目标模式，且客户端对得上

    await _drop_client()
    _current_site = replace(site, use_fs=use_fs)
    logger.info("取图通道已切换为 %s", "FlareSolverr" if use_fs else "直连")

    if warm:                                       # 解挑战可能要几十秒，放后台
        _warm_task = asyncio.create_task(_warm_up())
    return _current_site


async def _warm_up() -> None:
    """切回 FlareSolverr 后后台预热；失败只在日志里留痕，请求时会惰性重试。"""
    try:
        await fs()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("切换通道后的预热失败: %s", e)


def current_mode() -> str:
    """当前通道：'flaresolverr' / 'direct' / 'unknown'（还没插件注册站点）。"""
    site = _current_site
    if site is None:
        return "unknown"
    return "flaresolverr" if site.use_fs else "direct"


async def _refresh_loop() -> None:
    """后台续期：cookie 快过期时提前重解，用户侧零感知。"""
    assert _stop is not None
    interval = get_config().flaresolverr.refresh_interval
    backoff = interval
    while not _stop.is_set():
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
            return                       # 收到停止信号
        except asyncio.TimeoutError:
            pass

        if _client is None or not _is_fs_client(_client):
            continue                     # 直连模式没有 cookie 要续
        try:
            refreshed = await _client.maybe_refresh()
            if refreshed:
                logger.info("cf_clearance 已在后台续期")
            backoff = interval
        except asyncio.CancelledError:
            raise
        except Exception as e:
            backoff = min(backoff * 2, 600.0)
            logger.warning("后台续期失败，%.0fs 后重试: %s", backoff, e)


async def fs_start(application: Any = None) -> None:
    """挂到 post_init()。没有插件声明站点时跳过（不白起一个浏览器）。"""
    global _stop, _refresh_task
    if _current_site is None:
        logger.info("没有插件声明 FlareSolverr 目标站点，跳过浏览器启动")
    else:
        logger.info(
            "启动取图客户端（%s）",
            "FlareSolverr" if _current_site.use_fs else "直连（已关闭 FlareSolverr 通道）",
        )
        await fs()
    _stop = asyncio.Event()
    _refresh_task = asyncio.create_task(_refresh_loop())
    logger.info("保活任务已启动")


async def fs_stop(application: Any = None) -> None:
    """挂到 post_shutdown()。"""
    global _stop, _refresh_task, _warm_task, _client, _client_site
    if _warm_task is not None and not _warm_task.done():
        _warm_task.cancel()
        _warm_task = None
    if _stop is not None:
        _stop.set()
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except (asyncio.CancelledError, Exception):
            pass
    _refresh_task = None
    _stop = None

    if _client is not None:
        try:
            await _client.aclose()       # 内部会 sessions.destroy
            logger.info("FlareSolverr 客户端已关闭")
        except Exception as e:
            logger.warning("关闭 FlareSolverr 客户端失败: %s", e)
        _client = None
        _client_site = None


async def fs_health() -> dict[str, Any]:
    """给 /status、/fs 之类的诊断指令用。"""
    mode = current_mode()
    site = _client_site or _current_site
    base: dict[str, Any] = {
        "mode": mode,
        "started": _client.started if _client is not None else False,
        "site": site.entry_url if site else None,
        "session": (site.session_name if site else None) if mode == "flaresolverr" else None,
    }
    if mode != "flaresolverr" or _client is None or not _is_fs_client(_client):
        return base                       # 直连：没有 cookie 可报

    cl = _client.clearance
    base.update({
        "has_clearance": cl is not None,
        "alive": bool(cl and cl.alive),
        "expires_in": round(cl.expires_at - time.time(), 1) if cl else None,
        "user_agent": cl.user_agent if cl else None,
    })
    return base
