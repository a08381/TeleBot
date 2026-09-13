"""日志初始化。

输出分三路：

===============  ==================================================
日志文件          全量（DEBUG 及以上），按天切分保留 10 天
stdout           DEBUG / INFO  —— 正常输出，对应面板的「标准日志」
stderr           WARNING 及以上 —— 对应面板的「错误日志」
===============  ==================================================

为什么要拆：Docker / 1Panel 这类面板按 stdout、stderr 分栏展示，
而 logging.StreamHandler() 默认写 stderr —— 不拆的话 INFO 也会全跑到「错误日志」里。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# 低于这个级别的走 stdout，达到或高于它的走 stderr
ERROR_THRESHOLD = logging.WARNING


class _MaxLevelFilter(logging.Filter):
    """只放行低于 max_level 的记录（给 stdout 用）。"""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self._max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self._max_level


class _MinLevelFilter(logging.Filter):
    """只放行不低于 min_level 的记录（给 stderr 用）。"""

    def __init__(self, min_level: int) -> None:
        super().__init__()
        self._min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self._min_level


def _unbuffered(stream) -> None:
    """容器里行缓冲，避免日志攒着不显示。"""
    try:
        stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path = "logs",
    *,
    split_streams: bool = True,
    fmt: Optional[str] = None,
) -> None:
    """初始化根 logger。

    :param level:         根 logger 级别
    :param log_dir:       日志目录（相对路径基于项目根）
    :param split_streams: True 时 INFO 及以下走 stdout、WARNING 及以上走 stderr；
                          False 时维持旧行为（全部走 stderr）
    :param fmt:           自定义格式
    """
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    log_path.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(fmt or DEFAULT_FORMAT)
    max_level = getattr(logging, str(level).upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    # 1) 文件：全量
    file_handler = TimedRotatingFileHandler(
        log_path / "telebot.log", when="D", backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    # 2) 控制台：按级别分流
    if split_streams:
        _unbuffered(sys.stdout)
        _unbuffered(sys.stderr)

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(_MaxLevelFilter(ERROR_THRESHOLD))
        handlers.append(stdout_handler)

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.addFilter(_MinLevelFilter(ERROR_THRESHOLD))
        handlers.append(stderr_handler)
    else:
        console = logging.StreamHandler()  # 默认 stderr
        console.setFormatter(formatter)
        handlers.append(console)

    logging.basicConfig(
        level=max_level,
        format=fmt or DEFAULT_FORMAT,
        handlers=handlers,
        force=True,  # 允许重复调用（reload 场景）时覆盖旧配置
    )
