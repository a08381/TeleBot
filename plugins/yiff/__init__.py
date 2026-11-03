"""图站插件包，独立拥有 HTTP 客户端与帖子池。"""


def setup() -> None:
    from . import yiff  # noqa: F401
