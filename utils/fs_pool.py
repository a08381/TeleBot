"""FlareSolverr 客户端的模块级单例 + 后台保活。

关键约束：
1. 必须放在 **顶层模块**（不放在 plugins/ 下）。
   reload_all_plugins() 会 pop 掉所有 "plugins." 开头的 sys.modules，
   单例若写在插件里，热重载一次就丢了。
2. 单例只能在 **事件循环内** 创建/关闭，因此挂到 PTB 的 post_init / post_shutdown。
3. 挑战只解一次，cookie 由后台任务在过期前自动续，用户请求永远不等待浏览器。

配置分两处：
- **站点相关**（entry_url / session_name / user_agent）由使用它的插件提供：
  插件 import 时调用 configure_site(...)，例见 plugins/yiff.py。
  这三个值描述的是「要访问哪个站」，属于业务，不是 FlareSolverr 本身的参数。
- **客户端行为**（地址、超时、重试、并发等）在主配置 config.json 的 flaresolverr 段，
  见 utils.config.FlareSolverrSettings。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from .async_flaresolverr import AsyncFlareSolverrClient
from .config import get_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteProfile:
    """目标站的身份信息 —— 决定「用哪个浏览器 session 解哪个站的挑战」。"""

    site_url: str                       # 站点根地址，如 https://e926.net
    entry_url: str                      # 触发挑战的入口页，默认就是站点根
    session_name: str = "default"       # FlareSolverr session，复用同一个浏览器实例
    user_agent: Optional[str] = None    # 强制无头浏览器使用的 UA（有些站禁浏览器 UA）
    source: str = ""                    # 谁注册的（插件名），仅用于日志

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
    return urlparse(entry_url).hostname or "default"


# ----------------------------------------------------------------- 站点注册
_current_site: Optional[SiteProfile] = None


def configure_site(
    site_url: str,
    user_agent: Optional[str] = None,
    *,
    entry_url: Optional[str] = None,
    session_name: Optional[str] = None,
    source: str = "",
) -> SiteProfile:
    """由插件声明「我要访问哪个站」。

    只需要站点地址：entry_url 默认就是站点根，session_name 默认取站点域名。
    少数站点的根地址不触发挑战、必须用特定 URL 解时，才需要单独指定 entry_url。

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
    )
    return _current_site


def get_site() -> Optional[SiteProfile]:
    return _current_site


def clear_site() -> None:
    global _current_site
    _current_site = None


# ----------------------------------------------------------------- 单例状态
_client: Optional[AsyncFlareSolverrClient] = None
_client_site: Optional[SiteProfile] = None   # 建 _client 时用的站点信息
_lock: Optional[asyncio.Lock] = None
_refresh_task: Optional[asyncio.Task] = None
_stop: Optional[asyncio.Event] = None


def _get_lock() -> asyncio.Lock:
    """惰性创建，避免在 import 阶段绑定事件循环。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _drop_client() -> None:
    """关掉当前客户端（换站点时先清场）。"""
    global _client, _client_site
    if _client is None:
        return
    old = _client
    _client, _client_site = None, None
    try:
        await old.aclose()
    except Exception as e:
        logger.warning("关闭旧 FlareSolverr 客户端失败: %s", e)


async def fs() -> AsyncFlareSolverrClient:
    """获取全局唯一的 FlareSolverr 客户端。不存在则创建并立即预热。"""
    global _client, _client_site

    site = _current_site
    if site is None:
        raise RuntimeError(
            "还没有插件调用 configure_site() 声明目标站点，无法创建 FlareSolverr 客户端"
        )

    if _client is not None and _client.started:
        if site == _client_site:
            return _client
        # 站点变了（改了插件配置 + /reload）：旧 session 属于另一个站，必须重建
        logger.info("站点信息已变更为 %s，重建 FlareSolverr 客户端", site.entry_url)
        await _drop_client()

    cfg = get_config().flaresolverr
    async with _get_lock():
        if _client is not None and _client.started and site == _client_site:
            return _client

        client = AsyncFlareSolverrClient(
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
        await client.start()
        _client = client
        _client_site = site
        logger.info(
            "FlareSolverr 客户端已启动（session=%s, 来源=%s）",
            site.session_name, site.source or "未知",
        )

        # 预热必须放在锁内：否则并发调用者会拿到还没解出 cookie 的客户端
        try:
            await client.solve()
            logger.info("FlareSolverr 预热完成")
        except Exception as e:            # 预热失败不致命，请求时会重试
            logger.warning("FlareSolverr 预热失败，将在首次请求时重试: %s", e)

    return _client


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

        if _client is None:
            continue
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
        await fs()
    _stop = asyncio.Event()
    _refresh_task = asyncio.create_task(_refresh_loop())
    logger.info("FlareSolverr 保活任务已启动")


async def fs_stop(application: Any = None) -> None:
    """挂到 post_shutdown()。"""
    global _stop, _refresh_task, _client, _client_site
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
    """给 /status 之类的诊断指令用。"""
    if _client is None:
        return {"started": False, "site": _current_site.entry_url if _current_site else None}
    cl = _client.clearance
    return {
        "started": _client.started,
        "site": _client_site.entry_url if _client_site else None,
        "session": _client_site.session_name if _client_site else None,
        "has_clearance": cl is not None,
        "alive": bool(cl and cl.alive),
        "expires_in": round(cl.expires_at - time.time(), 1) if cl else None,
        "user_agent": cl.user_agent if cl else None,
    }
