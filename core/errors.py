"""统一异常。

插件只捕获这里定义的异常，不需要知道底层用的是哪个 Telegram SDK。
"""


class BotError(Exception):
    """机器人 API 调用失败（对 python-telegram-bot 异常的包装）。"""


class ConfigError(Exception):
    """配置缺失或非法。"""
