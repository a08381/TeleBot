from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot

from plugin import listener


@listener("wm")
async def wm(bot: Bot, update: Update, *args, **kwargs):
    keyboard = [
        [
            InlineKeyboardButton("Option 1", callback_data="1"),
            InlineKeyboardButton("Option 2", callback_data="2"),
        ],
        [InlineKeyboardButton("Option 3", callback_data="3")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Please choose:", reply_markup=reply_markup)
