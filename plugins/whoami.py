from telegram import Update, Bot

from plugin import listener


@listener("whoami")
async def whoami(bot: Bot, update: Update, *args, **kwargs):
    userid = update.effective_user.id
    username = update.effective_user.username
    await bot.sendMessage(userid, f"your user id is: {userid}\nyour username is: {username}")
