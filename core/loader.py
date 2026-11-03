"""按插件入口加载，按包完整卸载。

plugins/name.py 与 plugins/name/__init__.py 都是入口，包内工具不作为独立
插件扫描。入口可提供同步 setup()，由它显式导入处理器和私有工具。
重载顺序：等待当前调用 -> 停止旧资源 -> 清表/卸载整包 -> 导入 -> 启动新资源。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import sys
from pathlib import Path
from typing import Any, List

from utils.plugin_config import clear_plugin_configs

from .dispatcher import Buttons, Listeners
from .lifecycle import clear_hooks, run_shutdown, run_startup
from .runtime import exclusive_plugin_update
from .webhook import clear_webhook_routes

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
PLUGINS_DIR = BASE_DIR / "plugins"
PLUGINS_PACKAGE = "plugins"
_loaded: set[str] = set()
_application: Any = None
_started = False


def iter_plugin_modules() -> List[str]:
    """只扫描顶层入口，不递归导入包中的工具、测试或其它辅助模块。"""
    if not PLUGINS_DIR.exists():
        return []
    return sorted(
        info.name
        for info in pkgutil.iter_modules([str(PLUGINS_DIR)], f"{PLUGINS_PACKAGE}.")
        if not info.name.rpartition(".")[2].startswith("_")
    )


def load_all_plugins() -> List[str]:
    """导入全部入口并执行可选 setup()；此阶段不启动异步资源。"""
    loaded = []
    importlib.invalidate_caches()
    for name in iter_plugin_modules():
        try:
            if name not in _loaded:
                module = importlib.import_module(name)
                setup = getattr(module, "setup", None)
                if setup is not None:
                    if not callable(setup) or inspect.iscoroutinefunction(setup):
                        raise TypeError("插件 setup 必须是同步函数；异步初始化请使用 @startup。")
                    result = setup()
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise TypeError("插件 setup 不应返回协程；异步初始化请使用 @startup。")
                _loaded.add(name)
        except Exception:
            logger.exception("加载插件 %s 失败", name)
            continue
        loaded.append(name)
    if loaded:
        logger.info("已加载 %d 个插件: %s", len(loaded), ", ".join(loaded))
    return loaded


def _unload_modules() -> None:
    """资源停止后，清空注册表并移除插件包及其所有已导入的子模块。"""
    Listeners.clear()
    Buttons.clear()
    clear_plugin_configs()
    clear_hooks()
    clear_webhook_routes()
    names = [
        name for name in sys.modules
        if name == PLUGINS_PACKAGE or name.startswith(f"{PLUGINS_PACKAGE}.")
    ]
    for name in sorted(names, key=len, reverse=True):
        module = sys.modules.pop(name, None)
        cached = getattr(module, "__cached__", None)
        if cached:
            path = Path(cached).resolve()
            # 避免同一秒内等长编辑仍命中旧字节码，只清理插件目录内的生成缓存。
            if path.suffix == ".pyc" and path.is_relative_to(PLUGINS_DIR.resolve()):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("清理插件字节码缓存失败: %s", path, exc_info=True)
    _loaded.clear()
    importlib.invalidate_caches()


async def start_plugins(application: Any = None) -> None:
    """启动已加载的插件，并记住重载时需要传递的 Application。"""
    global _application, _started
    async with exclusive_plugin_update():
        if _started:
            return
        _application = application
        _started = True
        await run_startup(application)


async def stop_plugins(application: Any = None) -> None:
    """进程退出时清理插件资源，不重新导入模块。"""
    global _application, _started
    async with exclusive_plugin_update():
        if not _started:
            return
        try:
            await run_shutdown(_application)
        finally:
            _started = False
            _application = None


async def unload_all_plugins() -> None:
    """停止全部插件后卸载；停止失败时保留模块，不丢弃资源引用。"""
    global _application, _started
    async with exclusive_plugin_update():
        if _started:
            await run_shutdown(_application, strict=True)
        _started = False
        _application = None
        _unload_modules()


async def reload_all_plugins() -> List[str]:
    """异步整包重载；只有之前已经启动的运行实例才重启资源。"""
    global _started
    async with exclusive_plugin_update():
        was_started = _started
        if was_started:
            await run_shutdown(_application, strict=True)
        _started = False
        _unload_modules()
        loaded = load_all_plugins()
        if was_started:
            _started = True
            await run_startup(_application)
        return loaded
