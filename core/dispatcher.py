"""指令 / 按钮的注册与分发。

插件通过 @listener("cmd")、@button("prefix:") 声明自己，
本模块负责解析 /cmd a=1 b  这样的输入并把调用派发到插件函数。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import Application, CallbackContext, CallbackQueryHandler, MessageHandler, filters

from .context import Context

logger = logging.getLogger(__name__)

Listeners: Dict[str, List[Callable]] = {}
Buttons: Dict[str, List[Callable]] = {}

# /cmd@botname k=v arg1 arg2
CMD_RE = re.compile(r"^/(?P<command>[^@\s]+)(?:@(?P<bot_name>\S+))?(?:(?:\s+)(?P<args>.+))?$")
KEY_VALUE_RE = re.compile(r"^(?P<key>[^=]+)=(?P<value>.*)$")


# --------------------------------------------------------------------- 注册
def listener(command: str) -> Callable[[Callable], Callable]:
    """注册指令处理函数：async def f(ctx: Context, *args, **kwargs)。"""

    def decoration(func: Callable) -> Callable:
        key = command.lower()
        Listeners.setdefault(key, []).append(func)
        return func  # 返回原函数，不包装

    return decoration


def button(pattern: str) -> Callable[[Callable], Callable]:
    """注册 callback 按钮处理函数，pattern 为 callback_data 前缀或正则。"""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("button 的 pattern 必须是非空字符串")

    def decoration(func: Callable) -> Callable:
        Buttons.setdefault(pattern, []).append(func)
        return func

    return decoration


# --------------------------------------------------------------------- 解析
def parse_command(text: str) -> Optional[Tuple[str, Optional[str], List[str], Dict[str, str]]]:
    """把 /cmd a=1 b 解析成 (command, bot_name, args, kwargs)，不匹配返回 None。"""
    matched = CMD_RE.match(text or "")
    if not matched:
        return None

    args: List[str] = []
    kwargs: Dict[str, str] = {}
    raw_args = matched.group("args")
    if raw_args:
        for token in raw_args.split():
            kv = KEY_VALUE_RE.match(token)
            if kv:
                kwargs[kv.group("key")] = kv.group("value")
            else:
                args.append(token)
    return matched.group("command"), matched.group("bot_name"), args, kwargs


def match_button(data: str) -> Tuple[List[Callable], Tuple[str, ...]]:
    """给定 callback_data，返回 (处理函数列表, 位置参数元组)。"""
    if not data:
        return [], ()

    # 1) 字面前缀，最长优先
    best: Optional[str] = None
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
            continue  # 非法正则跳过，不让它炸掉整个分发
        if matched:
            return list(Buttons[pattern]), matched.groups()

    return [], ()


# --------------------------------------------------------------------- 分发
async def _run_all(funcs: List[Callable], ctx: Context, *args: Any, **kwargs: Any) -> None:
    """并发调用所有处理函数；单个插件出错不影响其它插件，也不向上抛。"""
    if not funcs:
        return
    results = await asyncio.gather(
        *(func(ctx, *args, **kwargs) for func in funcs), return_exceptions=True
    )
    for func, result in zip(funcs, results):
        if isinstance(result, BaseException):
            logger.exception(
                "插件 %s 执行失败", getattr(func, "__name__", func), exc_info=result
            )


async def dispatch_command(update: Update, context: CallbackContext) -> None:
    """MessageHandler 的回调：解析指令并派发。"""
    message = update.effective_message
    if message is None or not message.text:
        return

    parsed = parse_command(message.text)
    if parsed is None:
        return
    command, bot_name, args, kwargs = parsed
    if bot_name and bot_name != context.bot.username:
        return

    funcs = Listeners.get(command.lower())
    if not funcs:
        return
    logger.debug("command %s has been called.", command)
    await _run_all(funcs, Context(context.bot, update), *args, **kwargs)


async def dispatch_callback(update: Update, context: CallbackContext) -> None:
    """CallbackQueryHandler 的回调：按 callback_data 派发。"""
    query = update.callback_query
    if query is None:
        return

    funcs, args = match_button(query.data or "")
    if not funcs:
        await query.answer("未知按钮")  # 必须应答，否则客户端一直转圈
        return
    await _run_all(funcs, Context(context.bot, update), *args)


# --------------------------------------------------------------------- 装配
def register_handlers(application: Application) -> None:
    """把分发器挂到 Application 上（只需要在启动时执行一次）。"""
    application.add_handler(MessageHandler(filters.COMMAND, dispatch_command))
    application.add_handler(CallbackQueryHandler(dispatch_callback))
