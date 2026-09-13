"""TeleBot 入口。

用法::

    cp config.example.json config.json   # 填好 t_token
    pip install -r requirements.txt
    python main.py
    python main.py --init-config    # 只生成插件配置，不启动机器人
"""

import sys
from pathlib import Path

# 保证以 `python main.py` 方式运行时，项目根目录在 sys.path 上，
# 这样 plugins / core / utils 三个包都能被正常 import。
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import init_plugin_configs, run  # noqa: E402  必须在 sys.path 处理之后导入


def main() -> None:
    if "--init-config" in sys.argv[1:]:
        # 只生成 config/<插件名>.json，不连 Telegram
        init_plugin_configs()
        return
    run()


if __name__ == "__main__":
    main()
