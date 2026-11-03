"""插件启动与停止钩子。

插件在启动钩子中创建客户端和后台任务，在停止钩子中关闭并等待它们结束。
整包重载会先执行旧插件的停止钩子，再卸载模块并执行新插件的启动钩子。
钩子支持 async def f() 与 async def f(application) 两种签名。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

Hook = Callable[..., Awaitable[Any]]
_startups: List[Hook] = []
_shutdowns: List[Hook] = []


class PluginShutdownError(RuntimeError):
    """旧插件资源未完全停止，此时应保留模块供诊断或重试。"""


def startup(func: Hook) -> Hook:
    """注册资源启动钩子。"""
    if func not in _startups:
        _startups.append(func)
    return func


def shutdown(func: Hook) -> Hook:
    """注册资源停止钩子，函数返回前应完成任务和连接的清理。"""
    if func not in _shutdowns:
        _shutdowns.append(func)
    return func


def clear_hooks() -> None:
    _startups.clear()
    _shutdowns.clear()


def hook_names() -> dict[str, List[str]]:
    return {
        "startup": [f.__name__ for f in _startups],
        "shutdown": [f.__name__ for f in _shutdowns],
    }


async def _call(hook: Hook, application: Optional[Any]) -> Optional[Exception]:
    try:
        accepts_arg = any(
            p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
            for p in inspect.signature(hook).parameters.values()
        )
    except (TypeError, ValueError):
        accepts_arg = True
    try:
        await hook(application) if accepts_arg else await hook()
    except Exception as exc:
        logger.exception("生命周期钩子 %s 执行失败", getattr(hook, "__name__", hook))
        return exc
    return None


async def run_startup(application: Optional[Any] = None) -> None:
    """按注册顺序启动；单个失败仍继续其它插件。"""
    for hook in list(_startups):
        await _call(hook, application)


async def run_shutdown(application: Optional[Any] = None, *, strict: bool = False) -> None:
    """执行全部清理钩子；整包卸载时使用 strict，失败则中止卸载。"""
    errors = []
    for hook in list(_shutdowns):
        error = await _call(hook, application)
        if error is not None:
            errors.append(error)
    if strict and errors:
        raise PluginShutdownError(
            f"{len(errors)} 个插件停止钩子执行失败，已保留旧模块，请检查日志后重试或重启。"
        ) from errors[0]
