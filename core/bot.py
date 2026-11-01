"""Application 的构建与启动。

这里是整个项目唯一 touch python-telegram-bot 入口逻辑的地方。

生命周期顺序：
    启动：取图客户端就绪（http_start） -> 插件 startup 钩子（如启动图池）
    停止：插件 shutdown 钩子           -> 取图客户端关闭（http_stop）
图池依赖取图客户端，所以必须「后启动、先停止」。
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from utils.config import get_config, load_config
from utils.http_pool import http_start, http_stop
from utils.plugin_config import all_plugin_configs

from .dispatcher import register_handlers
from .lifecycle import hook_names, run_shutdown, run_startup
from .loader import load_all_plugins
from .logging_setup import setup_logging
from .webhook import webhook_routes

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- 生命周期
async def _on_startup(application: Application) -> None:
    await http_start()               # 1) 取图客户端（带浏览器指纹）
    await run_startup(application)   # 2) 插件自己的资源（图池等）


async def _on_shutdown(application: Application) -> None:
    await run_shutdown(application)  # 1) 先停插件资源
    await http_stop()                # 2) 再关取图客户端


# --------------------------------------------------------------------- 构建
def build_application() -> Application:
    """按主配置构建 Application，并挂上生命周期钩子。"""
    config = get_config()
    telegram = config.telegram

    application = (
        ApplicationBuilder()
        .token(telegram.token)
        .base_url(f"{telegram.host}/bot")
        .base_file_url(f"{telegram.host}/file/bot")
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )
    return application


async def _run_custom_webhook(application: Application) -> None:
    """Serve Telegram updates and plugin HTTP callbacks on the same listener."""
    from aiohttp import web

    webhook = get_config().telegram.webhook
    base_url = webhook.url.rstrip("/")
    telegram_path = "/" + webhook.path.strip("/")
    if not base_url.startswith("https://"):
        raise ValueError("webhook.url 必须是外部可访问的 HTTPS URL")
    if not webhook.port:
        raise ValueError("webhook.port 必须是有效端口")

    app = web.Application(client_max_size=20 * 1024 * 1024)
    app["e5_telegram_bot"] = application.bot
    routes = webhook_routes()
    if telegram_path in routes:
        raise ValueError(f"Telegram webhook 路径与插件路由冲突：{telegram_path}")

    async def receive_update(request: web.Request) -> web.Response:
        expected_secret = webhook.secret_token
        if expected_secret and request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        ) != expected_secret:
            raise web.HTTPUnauthorized(text="Invalid webhook secret")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")
        try:
            data = await request.json()
        except (ValueError, web.HTTPBadRequest) as exc:
            raise web.HTTPBadRequest(text="Invalid Telegram update JSON") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Telegram update must be a JSON object")
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return web.Response(status=200)

    app.router.add_post(telegram_path, receive_update)
    for path in routes:
        async def dispatch_plugin_route(
            request: web.Request, route_path: str = path
        ) -> web.StreamResponse:
            handler = webhook_routes().get(route_path)
            if handler is None:
                raise web.HTTPNotFound()
            return await handler(request)

        app.router.add_get(path, dispatch_plugin_route)

    runner = web.AppRunner(app, access_log=None)
    initialized = False
    startup_hook_ran = False
    try:
        await application.initialize()
        initialized = True
        if application.post_init:
            await application.post_init(application)
        startup_hook_ran = True
        await application.start()
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=webhook.port)
        await site.start()
        await application.bot.set_webhook(
            url=f"{base_url}{telegram_path}",
            allowed_updates=Update.ALL_TYPES,
            secret_token=webhook.secret_token or None,
        )
        logger.info(
            "自定义 webhook 已启动: Telegram=%s%s，插件路由=%s，监听端口=%s",
            base_url,
            telegram_path,
            ", ".join(routes) or "无",
            webhook.port,
        )
        await asyncio.Event().wait()
    finally:
        try:
            await runner.cleanup()
        finally:
            try:
                if application.running:
                    await application.stop()
            finally:
                if initialized:
                    try:
                        if startup_hook_ran and application.post_shutdown:
                            await application.post_shutdown(application)
                    finally:
                        await application.shutdown()


def init_plugin_configs() -> int:
    """只加载插件、生成 config/<插件名>.json，不连 Telegram。

    适合在填主配置之前先把插件配置生成出来：python main.py --init-config
    """
    setup_logging("INFO", "logs")
    load_all_plugins()
    names = sorted(cfg.name for cfg in all_plugin_configs().values())
    logger.info("插件配置已就绪（%d 个）: %s", len(names), ", ".join(names) or "无")
    return len(names)


def run() -> None:
    """一键启动：初始化日志 -> 构建应用 -> 加载插件 -> 注册分发 -> 跑起来。"""
    config = load_config()  # 已加载过就直接复用缓存
    setup_logging(
        config.log_level,
        config.log_dir,
        split_streams=config.log_split_streams,
        fmt=config.log_format,
    )

    application = build_application()
    # 插件必须先于 Application 启动流程加载，startup 钩子才注册得上
    loaded = load_all_plugins()
    register_handlers(application)
    logger.info("插件加载完毕，共 %d 个；生命周期钩子: %s", len(loaded), hook_names())

    if config.webhook.enable:
        webhook = config.telegram.webhook
        logger.info("以 webhook 模式启动: %s%s", webhook.url, webhook.path)
        try:
            asyncio.run(_run_custom_webhook(application))
        except KeyboardInterrupt:
            logger.info("webhook 服务已停止")
    else:
        logger.info("以长轮询模式启动")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
