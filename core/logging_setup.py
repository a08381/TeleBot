"""日志初始化。

控制台 + 按天切分的日志文件（保留 10 天）。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> None:
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    log_path.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=DEFAULT_FORMAT,
        handlers=[
            TimedRotatingFileHandler(log_path / "telebot.log", when="D", backupCount=10, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,  # 允许重复调用（reload 场景）时覆盖旧配置
    )
