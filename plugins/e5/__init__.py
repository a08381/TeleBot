"""E5 插件包；私有服务可独立导入，加载插件时再注册处理器。"""


def setup() -> None:
    from . import e5  # noqa: F401
