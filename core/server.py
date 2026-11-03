"""Telegram webhook 与插件 HTTP 回调共用的 aiohttp 服务。

路由构建不启动网络连接，Application 生命周期与 HTTP 监听分别管理，
便于独立测试请求校验、热重载路由和异常退出时的资源回收。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from utils.config import WebhookSettings

from .runtime import PluginsReloading, plugin_execution
from .webhook import WEBHOOK_BOT_KEY, webhook_routes

logger = logging.getLogger(__name__)


def create_webhook_app(application: Application, settings: WebhookSettings) -> web.Application:
    """构建 HTTP 路由；只接受通过校验的 Telegram 更新。"""
    public_url = urlsplit(settings.url)
    if public_url.scheme != "https" or not public_url.netloc:
        raise ValueError("webhook.url 必须是外部可访问的 HTTPS URL")
    if public_url.query or public_url.fragment:
        raise ValueError("webhook.url 不应包含查询参数或片段")
    if not 1 <= settings.port <= 65535:
        raise ValueError("webhook.port 必须在 1 到 65535 之间")

    telegram_path = "/" + settings.path.strip("/")
    routes = webhook_routes()
    if telegram_path in routes:
        raise ValueError(f"Telegram webhook 路径与插件路由冲突：{telegram_path}")

    app = web.Application(client_max_size=20 * 1024 * 1024)
    app[WEBHOOK_BOT_KEY] = application.bot

    async def receive_update(request: web.Request) -> web.Response:
        if settings.secret_token and request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        ) != settings.secret_token:
            raise web.HTTPUnauthorized(text="Invalid webhook secret")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")
        try:
            data = await request.json()
        except (ValueError, web.HTTPBadRequest) as exc:
            raise web.HTTPBadRequest(text="Invalid Telegram update JSON") from exc
        if not isinstance(data, dict) or type(data.get("update_id")) is not int:
            raise web.HTTPBadRequest(text="Telegram update must contain an integer update_id")
        try:
            update = Update.de_json(data, application.bot)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise web.HTTPBadRequest(text="Invalid Telegram update") from exc
        await application.update_queue.put(update)
        return web.Response(status=200)

    app.router.add_post(telegram_path, receive_update)
    for path in routes:
        async def dispatch_plugin_route(
            request: web.Request, route_path: str = path
        ) -> web.StreamResponse:
            try:
                async with plugin_execution():
                    handler = webhook_routes().get(route_path)
                    if handler is None:
                        raise web.HTTPNotFound()
                    return await handler(request)
            except PluginsReloading as exc:
                raise web.HTTPServiceUnavailable(
                    text=str(exc), headers={"Retry-After": "1"}
                ) from exc

        app.router.add_get(path, dispatch_plugin_route)
    return app


@asynccontextmanager
async def _application_lifespan(application: Application) -> AsyncIterator[None]:
    """对齐 PTB 的启停顺序，启动钩子部分失败时也执行清理。"""
    await application.initialize()
    try:
        if application.post_init:
            await application.post_init(application)
        await application.start()
        yield
    finally:
        try:
            if application.running:
                await application.stop()
                if application.post_stop:
                    await application.post_stop(application)
        finally:
            try:
                await application.shutdown()
            finally:
                if application.post_shutdown:
                    await application.post_shutdown(application)


async def run_webhook(application: Application, settings: WebhookSettings) -> None:
    """启动共享 HTTP 监听，取消任务或中断时先关闭监听再关闭 Application。"""
    app = create_webhook_app(application, settings)
    runner = web.AppRunner(app, access_log=None)
    telegram_url = f"{settings.url.rstrip('/')}/{settings.path.strip('/')}"

    async with _application_lifespan(application):
        try:
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
            await site.start()
            await application.bot.set_webhook(
                url=telegram_url,
                allowed_updates=Update.ALL_TYPES,
                secret_token=settings.secret_token or None,
            )
            logger.info(
                "自定义 webhook 已启动: Telegram=%s，插件路由=%s，监听端口=%s",
                telegram_url,
                ", ".join(webhook_routes()) or "无",
                settings.port,
            )
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
