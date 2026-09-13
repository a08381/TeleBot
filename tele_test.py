from logging.handlers import TimedRotatingFileHandler

from telegram.ext import MessageHandler, CallbackQueryHandler, filters

from plugin import *
from plugins.yiff import yiff_next


def main():
    load_all_plugins()

    application.add_handler(MessageHandler(filters.COMMAND, callback))
    application.add_handler(CallbackQueryHandler(on_callback))

    if config.get("webhook", {}).get("enable"):
        webhook_url = config.get("webhook", {}).get("url")
        webhook_port = config.get("webhook", {}).get("port")
        webhook_path = config.get("webhook", {}).get("path")
        application.run_webhook(
            port=webhook_port,
            webhook_url=f"{webhook_url}{webhook_path}"
        )
    else:
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
