"""从运行时命令注册表生成帮助，不维护另一份静态命令清单。"""

from __future__ import annotations

import inspect
from collections import defaultdict
from typing import Iterable

from core import Context, Listeners, listener

# 留出余量，并按 UTF-16 单元计算，避免表情导致消息超长。
MESSAGE_LIMIT = 3500


def _group(handler) -> str:
    module = (getattr(handler, "__module__", "") or "").split(".")
    if module[0] == "plugins":
        return module[1] if len(module) > 2 else "基础工具"
    return "其他"


def _summary(handler) -> str:
    doc = inspect.getdoc(handler) or ""
    first_line = next((line.strip() for line in doc.splitlines() if line.strip()), "")
    return first_line if len(first_line) <= 120 else first_line[:119] + "…"


def _split_messages(lines: Iterable[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """优先在行间分段，超长单行也拆开，保留所有指令。"""
    messages = []
    current = ""
    used = 0
    for line in lines:
        width = sum(2 if ord(char) > 0xFFFF else 1 for char in line)
        if current and used + 1 + width > limit:
            messages.append(current)
            current, used = "", 0
        if current:
            current += "\n"
            used += 1
        for char in line:
            width = 2 if ord(char) > 0xFFFF else 1
            if used + width > limit:
                messages.append(current)
                current, used = "", 0
            current += char
            used += width
    if current:
        messages.append(current)
    return messages


def command_help() -> list[str]:
    """每次取最新注册表；同名指令只列一次，别名保留为独立指令。"""
    groups = defaultdict(list)
    count = 0
    for command, handlers in sorted(Listeners.items()):
        if not handlers:
            continue
        count += 1
        group = " / ".join(sorted({_group(handler) for handler in handlers}))
        descriptions = list(dict.fromkeys(filter(None, (_summary(handler) for handler in handlers))))
        line = f"/{command}"
        if descriptions:
            line += " — " + "；".join(descriptions)
        groups[group].append(line)
    if not groups:
        return ["当前没有已注册的指令。"]

    lines = [f"📚 指令帮助（共 {count} 条，包含别名）"]
    for group in sorted(groups, key=lambda name: (name != "基础工具", name)):
        lines.extend(("", f"【{group}】", *groups[group]))
    lines.extend(("", "以上为当前已加载指令；部分指令需要管理员权限或已绑定账号。"))
    return _split_messages(lines)


@listener("help")
async def help_command(ctx: Context, *args, **kwargs):
    """自动列出已加载插件的全部指令，包含别名及已有说明。"""
    # 在发送前生成完整快照，不调用任何业务处理函数。
    for message in command_help():
        await ctx.reply_text(message, parse_mode=None)
