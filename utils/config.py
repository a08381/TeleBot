"""主配置加载。

这里只管 Bot 本体与基础设施（Token、API 地址、日志、webhook、FlareSolverr）。
图站相关的业务参数（站点地址、标签、图池大小、限流）属于使用它的插件，
放 config/<插件名>.json，见 utils.plugin_config。

config.json 里没写的字段用代码默认值，保证「不写配置 = 行为不变」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# 项目根目录（utils/ 的上一级）
BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = BASE_DIR / "config.json"
EXAMPLE_FILE = BASE_DIR / "config.example.json"

# 机器人主人（/reload 等管理指令的白名单），改成你自己的 Telegram user id
DEFAULT_OWNER_IDS = (535840409,)


def _as_tuple_int(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)):
        return (int(value),)
    return tuple(int(v) for v in value)


@dataclass(frozen=True)
class WebhookSettings:
    """webhook 模式配置，enable=false 时走长轮询。"""

    enable: bool = False
    url: str = ""
    port: int = 7755
    path: str = "/"

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "WebhookSettings":
        data = data or {}
        return cls(
            enable=bool(data.get("enable", False)),
            url=str(data.get("url", "")).rstrip("/"),
            port=int(data.get("port", 7755)),
            path=str(data.get("path", "/")),
        )


@dataclass(frozen=True)
class TelegramSettings:
    """Telegram 相关配置：Bot Token、API 地址、收更新方式（webhook）。

    写在 config.json 的 telegram 段::

        "telegram": {
          "token": "123456:ABC...",
          "host": "https://api.telegram.org",
          "webhook": {"enable": false, "url": "", "port": 7755, "path": "/"}
        }

    旧格式（顶层 t_token / t_host / webhook）仍然兼容，但建议迁移到 telegram 段。
    """

    token: str
    host: str = "https://api.telegram.org"   # 自建/反代 API 时改这里
    webhook: WebhookSettings = field(default_factory=WebhookSettings)

    @classmethod
    def from_mapping(
        cls, data: Optional[Mapping[str, Any]], root: Optional[Mapping[str, Any]] = None
    ) -> "TelegramSettings":
        """data 是 telegram 段；root 是顶层，用于兜底读旧格式的 t_token / t_host。"""
        data = data or {}
        root = root or {}

        token = (
            data.get("token")
            or data.get("t_token")
            or root.get("t_token")
            or root.get("token")
        )
        if not token or token == "你的Telegram Bot Token":
            raise ValueError(
                "config.json 缺少 telegram.token，"
                "请先复制 config.example.json 为 config.json 并填入 Bot Token"
            )

        host = data.get("host") or data.get("t_host") or root.get("t_host") or cls.host
        return cls(
            token=str(token),
            host=str(host).rstrip("/"),
            webhook=WebhookSettings.from_mapping(data.get("webhook") or root.get("webhook")),
        )


@dataclass(frozen=True)
class FlareSolverrSettings:
    """FlareSolverr **客户端本身**的行为参数。

    这里只放「怎么连 FlareSolverr、超时重试怎么定」这类通用参数，
    与访问哪个网站无关，因此**不建议轻易改动**。

    站点相关的三项（entry_url / session_name / user_agent）描述的是
    「要访问哪个站」，属于业务，由使用它的插件在自己的配置里声明，
    例如 config/yiff.json 的 flaresolverr 段。
    """

    url: str = "http://localhost:8191/v1"  # FlareSolverr 服务地址（本地 docker 一般是这个）
    refresh_interval: float = 60.0         # 后台保活多久检查一次 cookie 是否快过期
    fs_timeout: float = 120.0              # 调 FlareSolverr 的超时；解挑战可能要 30~60s，别调小
    request_timeout: float = 20.0          # 拿到 cookie 后直连目标站的超时
    renew_margin: float = 300.0            # cookie 还剩多少秒过期时提前续期
    max_retries: int = 1                   # 遇到 403/503 判定为挑战时，重解并重试的次数
    concurrency: int = 4                   # 直连目标站的并发上限

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "FlareSolverrSettings":
        data = data or {}
        defaults = cls()
        return cls(
            url=str(data.get("url", defaults.url)),
            refresh_interval=float(data.get("refresh_interval", defaults.refresh_interval)),
            fs_timeout=float(data.get("fs_timeout", defaults.fs_timeout)),
            request_timeout=float(data.get("request_timeout", defaults.request_timeout)),
            renew_margin=float(data.get("renew_margin", defaults.renew_margin)),
            max_retries=int(data.get("max_retries", defaults.max_retries)),
            concurrency=int(data.get("concurrency", defaults.concurrency)),
        )


@dataclass(frozen=True)
class BotSettings:
    """顶层配置。

    这里只放「Bot 本体 + 基础设施」参数。
    具体图站（e621/e926）相关的配置属于使用它的插件，
    放在 config/<插件名>.json 里（例如 config/yiff.json）。
    """

    telegram: TelegramSettings
    owner_ids: tuple[int, ...] = DEFAULT_OWNER_IDS
    log_level: str = "INFO"
    log_dir: str = "logs"
    # 控制台日志是否按级别分流：INFO 及以下走 stdout，WARNING 及以上走 stderr。
    # Docker / 1Panel 这类面板按 stdout、stderr 分栏，开着才能把「错误日志」留给真错误。
    log_split_streams: bool = True
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    flaresolverr: FlareSolverrSettings = field(default_factory=FlareSolverrSettings)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BotSettings":
        owner_ids = data.get("owner_ids", data.get("owner_id", DEFAULT_OWNER_IDS))
        return cls(
            telegram=TelegramSettings.from_mapping(data.get("telegram"), data),
            owner_ids=_as_tuple_int(owner_ids) or DEFAULT_OWNER_IDS,
            log_level=str(data.get("log_level", cls.log_level)).upper(),
            log_dir=str(data.get("log_dir", cls.log_dir)),
            log_split_streams=bool(data.get("log_split_streams", cls.log_split_streams)),
            log_format=str(data.get("log_format", cls.log_format)),
            flaresolverr=FlareSolverrSettings.from_mapping(data.get("flaresolverr")),
        )

    # ------------------------------------------------------------ 便捷访问
    @property
    def token(self) -> str:
        return self.telegram.token

    @property
    def host(self) -> str:
        return self.telegram.host

    @property
    def webhook(self) -> WebhookSettings:
        return self.telegram.webhook


_config: Optional[BotSettings] = None


def load_config(path: Optional[Path | str] = None, *, force_reload: bool = False) -> BotSettings:
    """读取配置；path 为空时用项目根的 config.json。"""
    global _config
    if _config is not None and not force_reload and path is None:
        return _config

    target = Path(path) if path else CONFIG_FILE
    if not target.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {target}，请复制 {EXAMPLE_FILE.name} 为 config.json 并修改"
        )
    raw = json.loads(target.read_text(encoding="UTF-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{target} 的内容必须是 JSON 对象")
    _config = BotSettings.from_mapping(raw)
    return _config


def get_config() -> BotSettings:
    """懒加载：首次调用时读取配置，之后复用。"""
    if _config is None:
        return load_config()
    return _config


def set_config(config: BotSettings) -> None:
    """测试或直接内嵌配置时用。"""
    global _config
    _config = config
