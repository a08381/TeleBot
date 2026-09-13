"""TeleBot 框架核心。

分层约定：
    core/    —— 框架层，**唯一**允许 import python-telegram-bot 的地方
    utils/   —— 通用工具层，只依赖标准库 + httpx / curl_cffi
    plugins/ —— 业务插件，只 import core（以及需要时的 utils），不碰 telegram

插件作者能拿到的东西都在 core 的导出里：
    listener / button          注册指令与按钮回调
    Context / Keyboard         与 Telegram 交互的门面（无 telegram 类型）
    BotError                   统一的发送失败异常
    plugin_config              插件私有配置（config/<插件名>.json，插件自己声明默认值）
    startup / shutdown         插件生命周期钩子（随进程启停的资源，如连接池）
    load_all_plugins / reload_all_plugins
    get_config
"""

from .bot import build_application, init_plugin_configs, run
from .context import CallbackQuery, Chat, Context, Message, User
from .dispatcher import (
    Buttons,
    Listeners,
    button,
    dispatch_callback,
    dispatch_command,
    listener,
    register_handlers,
)
from .errors import BotError
from .keyboard import Keyboard
from .lifecycle import shutdown, startup
from .loader import load_all_plugins, reload_all_plugins, unload_all_plugins
from .logging_setup import setup_logging
from utils.plugin_config import PluginConfig, plugin_config
from utils.config import BotSettings, get_config, load_config

__all__ = [
    "build_application",
    "run",
    "init_plugin_configs",
    "Context",
    "CallbackQuery",
    "Chat",
    "Message",
    "User",
    "Keyboard",
    "listener",
    "button",
    "Listeners",
    "Buttons",
    "register_handlers",
    "dispatch_command",
    "dispatch_callback",
    "BotError",
    "plugin_config",
    "PluginConfig",
    "startup",
    "shutdown",
    "load_all_plugins",
    "reload_all_plugins",
    "unload_all_plugins",
    "setup_logging",
    "get_config",
    "load_config",
    "BotSettings",
]
