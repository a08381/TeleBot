from telegram import Update, Bot

from plugin import listener, reload_all_plugins


@listener("reload")
async def reload(bot: Bot, update: Update, *args, **kwargs):
    user = update.effective_user

    if user.id == 535840409:
        reload_all_plugins()
        await update.effective_message.reply_text("Reload Complete.")
