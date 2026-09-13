import asyncio
import functools
import importlib
import json
import logging
import pkgutil
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List, Callable, Dict, Tuple

from telegram import Update, Bot
from telegram.ext import CallbackContext, ApplicationBuilder

from fs_pool import fs_start, fs_stop
from posts_pool import pool_start, pool_stop

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
PLUGINS_DIR = BASE_DIR / "plugins"
if not LOG_DIR.exists():
    LOG_DIR.mkdir()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        TimedRotatingFileHandler(
            LOG_DIR / "tele_test.log",
            when="D",
            backupCount=10
        ),
        logging.StreamHandler()
    ]
)

Listeners: Dict[str, List[Callable]] = {}
Buttons: Dict[str, List[Callable]] = {}
cmd = re.compile(r"^/(?P<command>[^@\s]+)(?:@(?P<bot_name>\S+))?(?:(?:\s+)(?P<args>.+))?$")
key_value = re.compile(r"^(?P<key>[^=]+)=(?P<value>.*)$")
logger = logging.getLogger()
config = json.loads((BASE_DIR / "config.json").read_text(encoding="UTF-8"))
application = (
    ApplicationBuilder()
    .token(config.get("t_token"))
    .base_url(f"{config.get('t_host')}/bot")
    .base_file_url(f"{config.get('t_host')}/file/bot")
    .post_init(fs_start)        # 启动浏览器 + 预热挑战
    .post_init(pool_start)      # 预热图池
    .post_shutdown(pool_stop)   # 注意顺序：先停池，再关 FS
    .post_shutdown(fs_stop)
    .build()
)


async def callback(update: Update, context: CallbackContext):
    message = update.effective_message

    if message.text:
        ma = cmd.match(message.text)
        if ma:
            command = ma.group("command")
            bot_name = ma.group("bot_name")
            s_args = ma.group("args")
            if bot_name and bot_name != context.bot.username:
                return

            args = []
            kwargs = {}
            if s_args:
                for s in s_args.split():
                    if s:
                        ma2 = key_value.match(s)
                        if ma2:
                            key = ma2.group("key")
                            value = ma2.group("value")
                            kwargs[key] = value
                        else:
                            args.append(s)

            await post(command, context.bot, update, *args, **kwargs)


async def post(command: str, bot: Bot, update: Update, *args, **kwargs):
    funcs = Listeners.get(command, [])
    if len(funcs) > 0:
        logger.debug(f"command {command} has been called.")
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(func(bot, update, *args, **kwargs)) for func in funcs]


async def on_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    funcs, args = match_button(query.data or "")
    if not funcs:
        await query.answer("未知按钮")     # 必须应答，否则客户端一直转圈
        return
    async with asyncio.TaskGroup() as tg:
        for func in funcs:
            tg.create_task(func(context.bot, update, *args))


def listener(command: str):
    def decoration(func: Callable):
        lower_name = command.lower()
        if lower_name not in Listeners:
            Listeners[lower_name] = []
        Listeners[lower_name].append(func)  # 直接存储原始函数
        return func  # 返回原始函数，不包装
    return decoration


def button(pattern: str) -> Callable[[Callable], Callable]:
    """注册一个 callback 按钮处理函数。用法同 listener。"""

    if not isinstance(pattern, str) or not pattern:
        raise ValueError("button 的 pattern 必须是非空字符串")

    def decoration(func: Callable) -> Callable:
        Buttons.setdefault(pattern, []).append(func)
        return func          # 返回原函数，不包装

    return decoration


def match_button(data: str) -> Tuple[List[Callable], Tuple[str, ...]]:
    """给定 callback_data，返回 (处理函数列表, 位置参数元组)。"""
    if not data:
        return [], ()

    # 1) 字面前缀，最长优先
    best: str | None = None
    for pattern in Buttons:
        if data.startswith(pattern) and (best is None or len(pattern) > len(best)):
            best = pattern
    if best is not None:
        return list(Buttons[best]), (data[len(best):],)

    # 2) 退化为正则
    for pattern in Buttons:
        try:
            matched = re.match(pattern, data)
        except re.error:
            continue          # 非法正则跳过，不让它炸掉整个分发
        if matched:
            return list(Buttons[pattern]), matched.groups()

    return [], ()


def load_all_plugins():
    for module_info in pkgutil.iter_modules([str(PLUGINS_DIR)], "plugins."):
        importlib.import_module(module_info.name)


def reload_all_plugins():
    Listeners.clear()
    Buttons.clear()
    modules_name = []
    modules_name.extend(sys.modules.keys())
    for name in modules_name:
        if name.startswith("plugins."):
            sys.modules.pop(name)
    load_all_plugins()
