"""与 Telegram webhook 共用监听器的插件 GET 路由注册表。"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiohttp.web import AppKey
from telegram import Bot

WEBHOOK_BOT_KEY = AppKey("telegram_bot", Bot)
WebhookHandler = Callable[[Any], Awaitable[Any]]
_ROUTES: dict[str, WebhookHandler] = {}


def webhook_route(path: str) -> Callable[[WebhookHandler], WebhookHandler]:
    """注册共享 webhook 服务上的 GET 路由。"""
    normalized = "/" + path.strip("/")
    if normalized == "/":
        raise ValueError("插件 webhook 路径不应为根路径 '/'")

    def decorate(func: WebhookHandler) -> WebhookHandler:
        if normalized in _ROUTES:
            raise ValueError(f"插件 webhook 路径重复：{normalized}")
        _ROUTES[normalized] = func
        return func

    return decorate


def webhook_routes() -> dict[str, WebhookHandler]:
    """返回当前插件路由快照。"""
    return dict(_ROUTES)


def clear_webhook_routes() -> None:
    """在重新加载插件前清空路由。"""
    _ROUTES.clear()
