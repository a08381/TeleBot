import time

from core import Context, listener, plugin_config

# 首次运行自动生成 config/ping.json
cfg = plugin_config({
    "precision": 2,      # 延迟保留几位小数
    "suffix": "s",       # 单位后缀
})


@listener("ping")
async def ping(ctx: Context, *args, **kwargs):
    message = ctx.message
    if message is None or message.date is None:
        return
    delay = time.time() - message.date.timestamp()
    await ctx.reply_text(f"{round(delay, cfg.get('precision'))}{cfg.get('suffix')}")
