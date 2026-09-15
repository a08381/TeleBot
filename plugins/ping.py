import time

from core import Context, listener


@listener("ping")
async def ping(ctx: Context, *args, **kwargs):
    message = ctx.message
    if message is None or message.date is None:
        return
    delay = time.time() - message.date.timestamp()
    await ctx.reply_text(f"{round(delay, 2)}s")
