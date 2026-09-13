"""插件生命周期钩子。

插件用 @startup / @shutdown 声明自己需要随进程启动/停止的资源，
框架在 PTB 的 post_init / post_shutdown 阶段统一调用：

    from core import shutdown, startup
    from utils.posts_pool import PoolSettings, pool_start, pool_stop

    @startup
    async def _start_pool(application=None):
        await pool_start(PoolSettings.from_mapping(cfg.as_dict()))

    @shutdown
    async def _stop_pool(application=None):
        await pool_stop()

调用顺序（重要）：
    启动: FlareSolverr 就绪 -> 插件 startup
    停止: 插件 shutdown     -> FlareSolverr 关闭
即池子依赖的浏览器要后停，所以插件钩子在 FS 之前结束。

钩子只在进程启动时执行一次；/reload 会重新注册，但不会重复触发。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

Hook = Callable[..., Awaitable[Any]]

_startups: List[Hook] = []
_shutdowns: List[Hook] = []


def startup(func: Hook) -> Hook:
    """注册启动钩子（post_init 阶段，FlareSolverr 之后）。"""
    if func not in _startups:
        _startups.append(func)
    return func


def shutdown(func: Hook) -> Hook:
    """注册停止钩子（post_shutdown 阶段，FlareSolverr 之前）。"""
    if func not in _shutdowns:
        _shutdowns.append(func)
    return func


def clear_hooks() -> None:
    """清空钩子表；reload 时由 core.loader 调用，插件重新 import 会再注册。"""
    _startups.clear()
    _shutdowns.clear()


def hook_names() -> dict[str, List[str]]:
    """诊断用：当前注册的钩子名。"""
    return {
        "startup": [f.__name__ for f in _startups],
        "shutdown": [f.__name__ for f in _shutdowns],
    }


async def _call(hook: Hook, application: Optional[Any]) -> None:
    """兼容 `async def f()` 与 `async def f(application)` 两种签名。"""
    try:
        params = [
            p for p in inspect.signature(hook).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        accepts_arg = bool(params) or any(
            p.kind == p.VAR_POSITIONAL for p in inspect.signature(hook).parameters.values()
        )
    except (TypeError, ValueError):  # pragma: no cover - 内置对象
        accepts_arg = True

    try:
        await hook(application) if accepts_arg else await hook()
    except Exception:
        logger.exception("生命周期钩子 %s 执行失败", getattr(hook, "__name__", hook))


async def run_startup(application: Optional[Any] = None) -> None:
    """按顺序执行所有启动钩子；单个失败不影响其它钩子。"""
    for hook in list(_startups):
        logger.debug("startup: %s", getattr(hook, "__name__", hook))
        await _call(hook, application)


async def run_shutdown(application: Optional[Any] = None) -> None:
    """按注册顺序执行所有停止钩子。"""
    for hook in list(_shutdowns):
        logger.debug("shutdown: %s", getattr(hook, "__name__", hook))
        await _call(hook, application)
