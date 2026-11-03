"""协调插件调用与整包重载，避免旧请求使用已经关闭的资源。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

_active_calls: dict[asyncio.Task, int] = {}
_reloading = False
_generation = 0


class PluginsReloading(RuntimeError):
    """插件正在切换，调用方应稍后重试。"""


def plugin_generation() -> int:
    return _generation


@asynccontextmanager
async def plugin_execution(generation: int | None = None) -> AsyncIterator[None]:
    """登记一次命令、按钮或 HTTP 回调；切换期间不进入插件代码。"""
    if _reloading or (generation is not None and generation != _generation):
        raise PluginsReloading("插件正在重载，请稍后重试。")
    task = asyncio.current_task()
    assert task is not None
    _active_calls[task] = _active_calls.get(task, 0) + 1
    try:
        yield
    finally:
        count = _active_calls[task] - 1
        if count:
            _active_calls[task] = count
        else:
            del _active_calls[task]


@asynccontextmanager
async def exclusive_plugin_update() -> AsyncIterator[None]:
    """暂停新调用，等待已有调用结束；排除触发 /reload 的当前任务。"""
    global _reloading, _generation
    if _reloading:
        raise PluginsReloading("已有插件重载或卸载正在执行，请稍后重试。")
    _reloading = True
    _generation += 1
    try:
        current = asyncio.current_task()
        pending = [task for task in _active_calls if task is not current]
        if pending:
            # 重载任务取消时，不连带取消仍在处理的用户请求。
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        yield
    finally:
        _reloading = False
