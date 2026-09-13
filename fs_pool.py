"""
FlareSolverr 客户端的模块级单例 + 后台保活。

关键约束（针对你这份 TGBot 入口）：
1. 必须放在 **顶层模块**（不放在 plugins/ 下）。
   你的 reload_all_plugins() 会 pop 掉所有 "plugins." 开头的 sys.modules，
   单例若写在插件里，热重载一次就丢了。
2. 单例只能在 **事件循环内** 创建/关闭，因此挂到 PTB 的 post_init / post_shutdown。
3. 挑战只解一次，cookie 由后台任务在过期前自动续，用户请求永远不等待浏览器。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from async_flaresolverr import AsyncFlareSolverrClient, FlareSolverrError

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- 配置
FS_URL = "http://localhost:8191/v1"
ENTRY_URL = "https://e926.net/"          # 会触发挑战的入口页
SESSION_NAME = "e926"                    # 固定 session，复用同一个浏览器实例
# e621/e926 明令禁止浏览器 UA，但挑战又必须由浏览器过 —— 让无头浏览器用这个 UA
SITE_UA = "MyTgBot/1.0 (by a08381 on e621)"

# ----------------------------------------------------------------- 单例状态
_client: Optional[AsyncFlareSolverrClient] = None
_lock: Optional[asyncio.Lock] = None
_refresh_task: Optional[asyncio.Task] = None
_stop: Optional[asyncio.Event] = None
REFRESH_INTERVAL = 60.0                  # 保活检查间隔（秒）


def _get_lock() -> asyncio.Lock:
    """惰性创建，避免在 import 阶段绑定事件循环。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def fs() -> AsyncFlareSolverrClient:
    """获取全局唯一的 FlareSolverr 客户端。不存在则创建并立即预热。"""
    global _client
    if _client is not None and _client.started:
        return _client

    async with _get_lock():
        if _client is not None and _client.started:
            return _client
        client = AsyncFlareSolverrClient(
            entry_url=ENTRY_URL,
            fs_url=FS_URL,
            session_name=SESSION_NAME,
            fs_user_agent=SITE_UA,       # 让无头浏览器带上合规 UA
            fs_timeout=120.0,
            request_timeout=20.0,
            renew_margin=300.0,          # 过期前 5 分钟就换
            max_retries=1,
            concurrency=4,
        )
        await client.start()
        _client = client
        logger.info("FlareSolverr 客户端已启动（session=%s）", SESSION_NAME)

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
    backoff = REFRESH_INTERVAL
    while not _stop.is_set():
        try:
            await asyncio.wait_for(_stop.wait(), timeout=REFRESH_INTERVAL)
            return                       # 收到停止信号
        except asyncio.TimeoutError:
            pass

        if _client is None:
            continue
        try:
            refreshed = await _client.maybe_refresh()
            if refreshed:
                logger.info("cf_clearance 已在后台续期")
            backoff = REFRESH_INTERVAL
        except asyncio.CancelledError:
            raise
        except Exception as e:
            backoff = min(backoff * 2, 600.0)
            logger.warning("后台续期失败，%.0fs 后重试: %s", backoff, e)


async def fs_start(application: Any = None) -> None:
    """挂到 ApplicationBuilder().post_init()。"""
    global _stop, _refresh_task
    await fs()
    _stop = asyncio.Event()
    _refresh_task = asyncio.create_task(_refresh_loop())
    logger.info("FlareSolverr 保活任务已启动")


async def fs_stop(application: Any = None) -> None:
    """挂到 ApplicationBuilder().post_shutdown()。"""
    global _stop, _refresh_task, _client
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


async def fs_health() -> dict[str, Any]:
    """给 /status 之类的诊断指令用。"""
    if _client is None:
        return {"started": False}
    cl = _client.clearance
    return {
        "started": _client.started,
        "has_clearance": cl is not None,
        "alive": bool(cl and cl.alive),
        "expires_in": round(cl.expires_at - __import__("time").time(), 1) if cl else None,
        "user_agent": cl.user_agent if cl else None,
    }
