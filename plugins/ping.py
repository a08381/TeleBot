import time

from telegram import Update, Bot

from plugin import listener


@listener("ping")
async def ping(bot: Bot, update: Update, *args, **kwargs):
    times = time.time() - update.effective_message.date.timestamp()

    await update.message.reply_text(f"{round(times, 2)}s")
