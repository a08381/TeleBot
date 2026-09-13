"""插件私有配置。

每个插件一份 JSON，放在 ``config/<插件名>.json``，由插件自己声明默认值后
自动生成，不写进根目录的 config.json（那里只放 Bot 主配置）。

用法（在 plugins/xxx.py 里）::

    from core import plugin_config

    cfg = plugin_config({              # 插件名自动取模块名 -> config/xxx.json
        "timeout": 10,
        "tags": ["male"],
    })

    cfg.get("timeout")        # -> 10
    cfg["tags"]               # -> ["male"]
    cfg.timeout               # -> 10（属性访问）
    cfg.set("timeout", 30)    # 改值并立即写回文件
    cfg.update(timeout=30)    # 批量改值并写回
    cfg.reload()              # 重新读盘（/reload 时会自动做）

行为约定：
- 文件不存在      -> 用默认值生成一份
- 文件缺字段      -> 补上默认值并写回
- 文件多出字段    -> 保留（不会被删）
- 类型对不上      -> 按默认值的类型强转，转不过来就回退默认值
- 调用 /reload    -> 插件重新 import，配置文件被重新读取
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
_COMMENT_KEY = "_comment"
_MISSING = object()

# name -> PluginConfig，reload 时由 core.loader 清空
_CACHE: Dict[str, "PluginConfig"] = {}


# --------------------------------------------------------------------- 工具
def _sanitize(name: str) -> str:
    """防止插件名里出现 ../ 之类的路径穿越。"""
    safe = _UNSAFE_NAME.sub("_", (name or "").strip()) or "plugin"
    return safe.lstrip(".")


def _caller_plugin_name() -> str:
    """从调用者模块的 __name__ 推导插件名：plugins.yiff -> yiff。"""
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back if frame and frame.f_back else None  # 跳过本函数与 plugin_config
        module = (caller.f_globals.get("__name__", "") if caller else "") or ""
    finally:
        del frame  # 避免引用循环
    if not module:
        return "plugin"
    if module == "__main__":
        return Path(sys.argv[0] if sys.argv and sys.argv[0] else "main").stem
    return module.rpartition(".")[2] or module


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="UTF-8"))
    except Exception as e:
        logger.warning("插件配置 %s 解析失败，改用默认值: %s", path.name, e)
        return {}
    return raw if isinstance(raw, dict) else {}


def _coerce(value: Any, default: Any) -> Tuple[Any, bool]:
    """按默认值的类型强转；失败返回 (默认值, True)。"""
    if default is None:
        return value, False
    if isinstance(value, type(default)) and not (isinstance(default, bool) ^ isinstance(value, bool)):
        return value, False

    # 布尔：字符串 / 数字都尽量认
    if isinstance(default, bool):
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True, True
            if text in ("false", "0", "no", "off"):
                return False, True
        elif isinstance(value, (int, float)):
            return bool(value), True
        return default, True

    try:
        if isinstance(default, (int, float, str)) and not isinstance(value, (list, dict, tuple)):
            return type(default)(value), True
        if isinstance(default, list):
            return (list(value), True) if isinstance(value, (list, tuple)) else (default, True)
        if isinstance(default, dict):
            return (dict(value), True) if isinstance(value, dict) else (default, True)
    except (TypeError, ValueError):
        return default, True
    return value, False


def _merge(defaults: Mapping[str, Any], loaded: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """以 defaults 为基底合并磁盘数据，返回 (结果, 是否有改动需要写回)。"""
    merged: Dict[str, Any] = {}
    changed = False

    for key, default in defaults.items():
        raw = loaded.get(key, _MISSING)
        if raw is _MISSING:
            merged[key] = default
            changed = True                      # 新字段，补进去
            continue
        # dict 逐层合并，用户只填部分子字段也不会把其它默认值冲掉
        if isinstance(default, dict) and isinstance(raw, dict):
            sub, sub_changed = _merge(default, raw)
            merged[key] = sub
            changed = changed or sub_changed
            continue
        value, coerced = _coerce(raw, default)
        merged[key] = value
        changed = changed or coerced

    for key, value in loaded.items():           # 文件里多出来的键原样保留
        if key not in merged:
            merged[key] = value
    return merged, changed


# --------------------------------------------------------------------- 对象
class PluginConfig(Mapping):
    """只读视图 + 显式写接口，避免插件随手改内存里的值却忘了落盘。"""

    __slots__ = ("_name", "_path", "_defaults", "_data")

    def __init__(self, name: str, path: Path, defaults: Dict[str, Any], data: Dict[str, Any]) -> None:
        self._name = name
        self._path = path
        self._defaults = defaults
        self._data = data

    # ------------------------------------------------------------ 元信息
    @property
    def name(self) -> str:
        return self._name

    @property
    def path(self) -> Path:
        return self._path

    @property
    def defaults(self) -> Dict[str, Any]:
        return dict(self._defaults)

    def as_dict(self) -> Dict[str, Any]:
        """返回副本（不含 _comment 之类的元信息请用 keys() 自行过滤）。"""
        return dict(self._data)

    # ------------------------------------------------------------ 读
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getattr__(self, item: str) -> Any:
        """允许 cfg.timeout 这种写法；内部字段以下划线开头，不会被遮蔽。"""
        try:
            data = object.__getattribute__(self, "_data")
        except AttributeError:
            raise AttributeError(item)
        if item in data:
            return data[item]
        raise AttributeError(f"插件 {self._name} 的配置里没有 {item!r}")

    # ------------------------------------------------------------ 写
    def set(self, key: str, value: Any, *, save: bool = True) -> "PluginConfig":
        """改一个键；save=True 时立即落盘。"""
        self._data[key] = value
        if save:
            self.save()
        return self

    def update(self, values: Optional[Mapping[str, Any]] = None, *, save: bool = True, **kwargs: Any) -> "PluginConfig":
        """批量改键；save=True 时立即落盘。"""
        if values:
            self._data.update(values)
        self._data.update(kwargs)
        if save:
            self.save()
        return self

    def reset(self, *, save: bool = True) -> "PluginConfig":
        """恢复成默认值。"""
        self._data = dict(self._defaults)
        if save:
            self.save()
        return self

    def save(self) -> "PluginConfig":
        """原子写回（临时文件 + replace，避免写一半被打断）。"""
        payload = dict(self._data)
        payload.setdefault(_COMMENT_KEY, f"{self._name} 插件配置，由插件自动生成；改完重启或 /reload 生效")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="UTF-8"
        )
        tmp.replace(self._path)
        return self

    def reload(self) -> "PluginConfig":
        """重新读盘并与默认值合并（缺的补、多的留）。"""
        merged, _ = _merge(self._defaults, _read_json(self._path))
        self._data = merged
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"PluginConfig(name={self._name!r}, path={self._path.name!r}, keys={list(self._data)})"


# --------------------------------------------------------------------- 入口
def plugin_config(
    defaults: Optional[Mapping[str, Any]] = None,
    *,
    name: Optional[str] = None,
    auto_create: bool = True,
) -> PluginConfig:
    """获取（必要时生成）当前插件的配置对象。

    :param defaults:   插件的默认值；文件里缺的字段用它补
    :param name:       配置名，默认取调用者模块名（plugins.yiff -> yiff）
    :param auto_create: 文件缺失/有新增字段时自动写回
    """
    config_name = _sanitize(name or _caller_plugin_name())
    path = CONFIG_DIR / f"{config_name}.json"

    cached = _CACHE.get(config_name)
    if cached is not None:
        return cached

    merged, changed = _merge(dict(defaults or {}), _read_json(path))
    config = PluginConfig(config_name, path, dict(defaults or {}), merged)

    if auto_create and (changed or not path.exists()):
        config.save()
        logger.info("插件配置已就绪: %s", path.relative_to(BASE_DIR))

    _CACHE[config_name] = config
    return config


def get_plugin_config(name: str) -> Optional[PluginConfig]:
    """按名字取已加载的插件配置（诊断用）。"""
    return _CACHE.get(_sanitize(name))


def all_plugin_configs() -> Dict[str, PluginConfig]:
    return dict(_CACHE)


def clear_plugin_configs() -> None:
    """清空缓存；配合 reload，让插件下次 import 时重新读盘。"""
    _CACHE.clear()
