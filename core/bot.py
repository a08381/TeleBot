"""Application 的构建与启动。

这里是整个项目唯一 touch python-telegram-bot 入口逻辑的地方。

生命周期顺序：
    启动：FlareSolverr 就绪（fs_start） -> 插件 startup 钩子（如启动图池）
    停止：插件 shutdown 钩子          -> FlareSolverr 关闭（fs_stop）
图池依赖 FlareSolverr，所以必须「后启动、先停止」。
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from utils.config import get_config, load_config
from utils.fs_pool import fs_start, fs_stop
from utils.plugin_config import all_plugin_configs

from .dispatcher import register_handlers
from .lifecycle import hook_names, run_shutdown, run_startup
from .loader import load_all_plugins
from .logging_setup import setup_logging

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- 生命周期
async def _on_startup(application: Application) -> None:
    await fs_start()        # 1) 浏览器 + 挑战预热
    await run_startup(application)   # 2) 插件自己的资源（图池等）


async def _on_shutdown(application: Application) -> None:
    await run_shutdown(application)  # 1) 先停插件资源
    await fs_stop()                  # 2) 再关浏览器


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
    setup_logging(config.log_level, config.log_dir)

    application = build_application()
    # 插件必须先于 Application 启动流程加载，startup 钩子才注册得上
    loaded = load_all_plugins()
    register_handlers(application)
    logger.info("插件加载完毕，共 %d 个；生命周期钩子: %s", len(loaded), hook_names())

    if config.webhook.enable:
        webhook = config.telegram.webhook
        logger.info("以 webhook 模式启动: %s%s", webhook.url, webhook.path)
        application.run_webhook(
            port=webhook.port,
            webhook_url=f"{webhook.url.rstrip('/')}{webhook.path}",
        )
    else:
        logger.info("以长轮询模式启动")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
