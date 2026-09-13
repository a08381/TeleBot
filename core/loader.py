"""插件加载与热重载。

reload 时会清掉注册表和 sys.modules 里的 plugins.*，再重新 import，
所以插件里的模块级状态（比如缓存字典）会一起重置 —— 这是有意为之。
需要跨 reload 保留的单例请放 utils/ 下（例如 fs_pool / posts_pool）。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from pathlib import Path
from typing import List

from .dispatcher import Buttons, Listeners
from .lifecycle import clear_hooks
from utils.plugin_config import clear_plugin_configs

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
PLUGINS_DIR = BASE_DIR / "plugins"
PLUGINS_PACKAGE = "plugins"


def iter_plugin_modules() -> List[str]:
    """扫描 plugins/ 下所有模块名（含包内的子模块）。"""
    names: List[str] = []
    if not PLUGINS_DIR.exists():
        return names
    for module_info in pkgutil.walk_packages([str(PLUGINS_DIR)], f"{PLUGINS_PACKAGE}."):
        names.append(module_info.name)
    return names


def unload_all_plugins() -> None:
    """清空注册表并卸载 plugins.* 模块。"""
    Listeners.clear()
    Buttons.clear()
    clear_plugin_configs()  # 让插件下次 import 时重新读 config/<name>.json
    clear_hooks()           # 生命周期钩子由插件重新注册
    for name in list(sys.modules):
        if name == PLUGINS_PACKAGE or name.startswith(f"{PLUGINS_PACKAGE}."):
            sys.modules.pop(name, None)


def load_all_plugins() -> List[str]:
    """导入 plugins/ 下所有模块，返回已加载的模块名。"""
    loaded: List[str] = []
    for name in iter_plugin_modules():
        try:
            importlib.import_module(name)
        except Exception:
            logger.exception("加载插件 %s 失败", name)
            continue
        loaded.append(name)
    if loaded:
        logger.info("已加载 %d 个插件: %s", len(loaded), ", ".join(loaded))
    return loaded


def reload_all_plugins() -> List[str]:
    """热重载：先卸载，再重新加载。"""
    unload_all_plugins()
    return load_all_plugins()
