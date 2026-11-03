"""TeleBot 命令行入口：启动机器人，或仅初始化插件配置。"""

from __future__ import annotations

import argparse

from core import init_plugin_configs, run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="插件式 Telegram Bot")
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="加载插件并生成配置，不启动机器人或连接 Telegram",
    )
    args = parser.parse_args(argv)
    if args.init_config:
        init_plugin_configs()
    else:
        run()


if __name__ == "__main__":
    main()
