"""Application 构建与启动入口。

这里只负责配置、插件和分发器的装配；HTTP 监听由 core.server 管理。
插件自行拥有并管理客户端与后台任务，框架统一调用启停钩子。
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from utils.config import BotSettings, get_config, load_config
from utils.plugin_config import all_plugin_configs

from .dispatcher import register_handlers
from .lifecycle import hook_names
from .loader import load_all_plugins, start_plugins, stop_plugins
from .logging_setup import setup_logging
from .server import run_webhook

logger = logging.getLogger(__name__)


async def _on_startup(application: Application) -> None:
    await start_plugins(application)


async def _on_shutdown(application: Application) -> None:
    await stop_plugins(application)


def build_application(config: BotSettings | None = None) -> Application:
    """按配置构建 Application；不传配置时使用主配置缓存。"""
    telegram = (config or get_config()).telegram
    return (
        ApplicationBuilder()
        .token(telegram.token)
        .base_url(f"{telegram.host}/bot")
        .base_file_url(f"{telegram.host}/file/bot")
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )


def init_plugin_configs() -> int:
    """加载插件并生成配置，不连接 Telegram，也不执行启动钩子。"""
    setup_logging("INFO", "logs")
    load_all_plugins()
    names = sorted(cfg.name for cfg in all_plugin_configs().values())
    logger.info("插件配置已就绪（%d 个）: %s", len(names), ", ".join(names) or "无")
    return len(names)


def run() -> None:
    """读取配置，装配应用，再启动长轮询或 webhook 服务。"""
    config = load_config()
    setup_logging(
        config.log_level,
        config.log_dir,
        split_streams=config.log_split_streams,
        fmt=config.log_format,
    )

    application = build_application(config)
    loaded = load_all_plugins()
    register_handlers(application)
    logger.info("插件加载完毕，共 %d 个；生命周期钩子: %s", len(loaded), hook_names())

    if config.webhook.enable:
        logger.info("以 webhook 模式启动: %s%s", config.webhook.url, config.webhook.path)
        try:
            asyncio.run(run_webhook(application, config.webhook))
        except KeyboardInterrupt:
            logger.info("webhook 服务已停止")
    else:
        logger.info("以长轮询模式启动")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
