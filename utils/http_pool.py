"""取图客户端的模块级单例（带浏览器指纹）。

过 Cloudflare 靠两件事，都由本模块与 utils.browser_profile 提供：
1. **浏览器指纹**：TLS/JA3 + HTTP/2 + 头顺序跟真实浏览器一致（curl_cffi 的 impersonate）
2. **Cookie**：站点 Cookie（登录态、手填的 cf_clearance…），按域名注入

关键约束：
1. 必须放在 **顶层模块**（不放在 plugins/ 下）。
   reload_all_plugins() 会 pop 掉所有 "plugins." 开头的 sys.modules，
   单例若写在插件里，热重载一次就丢了。
2. 单例只能在 **事件循环内** 创建/关闭，因此挂到 PTB 的 post_init / post_shutdown。
3. 客户端是惰性的：没人取图就不建；站点/UA/指纹变了会自动重建。

配置分两处：
- **站点相关**（site / user_agent）由使用它的插件提供：插件 import 时调用
  configure_site(...)，例见 plugins/yiff.py。这描述的是「访问哪个站」，属于业务。
- **浏览器身份**（impersonate / cookies / headers / proxy / timeout）在插件配置的
  browser 段，见 utils.browser_profile.BrowserProfile。

没装 curl_cffi 时自动退回 httpx：功能不受影响，只是没有 TLS 指纹，
启动日志与 /fp 都会提示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union
from urllib.parse import urlparse

from .browser_profile import (
    BrowserProfile,
    aclose_client,
    create_client,
    host_of,
)

logger = logging.getLogger(__name__)

# 插件没给 user_agent 时的兜底 UA（e621/e926 这类站要求 UA 非空且不能是浏览器 UA）
DEFAULT_UA = "TeleBot/1.0"


@dataclass(frozen=True)
class SiteProfile:
    """目标站的身份信息 —— 决定「用什么浏览器身份访问哪个站」。"""

    site_url: str                                  # 站点根地址，如 https://e926.net
    user_agent: Optional[str] = None               # 站点要求的合规 UA（e621 要求带用户名）
    source: str = ""                               # 谁注册的（插件名），仅用于日志
    browser: BrowserProfile = field(default_factory=BrowserProfile)  # 指纹 + Cookie

    def __post_init__(self) -> None:
        if not self.site_url:
            raise ValueError("site_url 不能为空")

    @property
    def cookie_hosts(self) -> tuple[str, ...]:
        """Cookie 绑定域名：站点主域 + browser.cookie_domains（CDN 子域要自己加）。"""
        return self.browser.domains_for(self.site_url)

    def describe_browser(self) -> str:
        """给日志/状态用的一句话描述。"""
        if not self.browser.wants_fingerprint:
            return "无指纹（httpx）"
        if not self.browser.enabled:
            return f"{self.browser.impersonate}（未生效：缺 curl_cffi）"
        return self.browser.impersonate


def _normalize_site(url: str) -> str:
    """补全协议、去掉结尾斜杠：e926.net -> https://e926.net"""
    url = (url or "").strip().rstrip("/")
    if url and not urlparse(url).scheme:
        url = f"https://{url}"
    return url


# ----------------------------------------------------------------- 站点注册
_current_site: Optional[SiteProfile] = None


def _as_browser_profile(browser: Optional[Union[BrowserProfile, Mapping[str, Any]]]) -> BrowserProfile:
    """把插件传进来的 browser 配置统一成 BrowserProfile（支持对象或 config 里的 dict）。"""
    if browser is None:
        return BrowserProfile()
    if isinstance(browser, BrowserProfile):
        return browser
    return BrowserProfile.from_mapping(browser)


def configure_site(
    site_url: str,
    user_agent: Optional[str] = None,
    *,
    source: str = "",
    browser: Optional[Union[BrowserProfile, Mapping[str, Any]]] = None,
) -> SiteProfile:
    """由插件声明「我要访问哪个站、用什么浏览器身份」。

    browser 描述浏览器身份（见 utils.browser_profile.BrowserProfile）：
    - impersonate  填 chrome124 / firefox135 之类即启用 TLS/HTTP2 指纹（需装 curl_cffi）
    - cookies      站点 Cookie（登录态、手填的 cf_clearance…）
    - headers      附加请求头
    - timeout      单次请求超时（秒）

    一般在插件模块顶层调用（import 时执行），早于任何 http() 调用。
    重复调用会覆盖；换了一个来源来覆盖时会打 warning，便于发现两个插件抢同一个单例。
    """
    global _current_site
    previous = _current_site
    if previous is not None and source and previous.source and source != previous.source:
        logger.warning(
            "站点信息被 %s 覆盖（原为 %s）；单例只有一个，两个插件访问不同站点会互相抢占",
            source, previous.source,
        )

    _current_site = SiteProfile(
        site_url=_normalize_site(site_url),
        user_agent=user_agent,
        source=source,
        browser=_as_browser_profile(browser),
    )
    return _current_site


def get_site() -> Optional[SiteProfile]:
    return _current_site


def clear_site() -> None:
    global _current_site
    _current_site = None


# ----------------------------------------------------------------- 客户端
class SiteClient:
    """带浏览器身份的取图客户端。

    接口对齐 httpx.AsyncClient 的常用部分（get / request / start / aclose / started），
    调用方（图池、插件）不需要知道底层是 curl_cffi 还是 httpx。
    """

    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        timeout: float = 20.0,
        headers: Optional[dict[str, str]] = None,
        browser: Optional[BrowserProfile] = None,
        cookie_hosts: tuple[str, ...] = (),     # Cookie 绑定域名，空则发给所有域名
        transport: Any = None,                  # 测试钩子：注入 httpx MockTransport
    ) -> None:
        self.browser = browser or BrowserProfile()
        self.user_agent = user_agent or DEFAULT_UA
        self.timeout = timeout
        self.extra_headers = dict(headers or {})
        self.cookie_hosts = tuple(h for h in cookie_hosts if h)
        self._transport = transport
        self._client: Optional[Any] = None

    async def start(self) -> None:
        if self._client is not None:
            return
        headers = {
            "User-Agent": self.browser.effective_ua(self.user_agent),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        headers.update(self.browser.headers)
        headers.update(self.extra_headers)
        self._client = create_client(
            timeout=self.timeout,
            proxy=self.browser.proxy or None,
            verify=self.browser.verify,
            follow_redirects=True,
            headers=headers,
            cookies=dict(self.browser.cookies),
            cookie_domains=self.cookie_hosts,   # 按域名绑定，别发给无关的 CDN / 第三方域
            browser=self.browser,
            transport=self._transport,
        )

    @property
    def started(self) -> bool:
        return self._client is not None

    @property
    def user_agent_in_use(self) -> str:
        """当前实际发出的 UA（开了指纹时会被替换成浏览器 UA）。"""
        return self.browser.effective_ua(self.user_agent)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        await aclose_client(client)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if self._client is None:
            await self.start()
        assert self._client is not None
        headers = dict(self.extra_headers)          # 单请求的头覆盖客户端默认头
        user_headers = kwargs.pop("headers", None)
        if user_headers:
            headers.update(user_headers)
        return await self._client.request(method, url, headers=headers, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)


# ----------------------------------------------------------------- 单例状态
_client: Optional[SiteClient] = None
_client_site: Optional[SiteProfile] = None


async def _drop_client() -> None:
    """关掉当前客户端（换站点 / 换指纹时先清场）。"""
    global _client, _client_site
    if _client is None:
        return
    old = _client
    _client, _client_site = None, None
    try:
        await old.aclose()
    except Exception as e:
        logger.warning("关闭旧客户端失败: %s", e)


async def http() -> SiteClient:
    """获取全局唯一的取图客户端；不存在则创建，站点或浏览器身份变了则重建。"""
    global _client, _client_site

    site = _current_site
    if site is None:
        raise RuntimeError(
            "还没有插件调用 configure_site() 声明目标站点，无法创建取图客户端"
        )

    if _client is not None and _client.started:
        if site == _client_site:
            return _client
        logger.info("站点信息已变更（%s），重建客户端", site.site_url)
        await _drop_client()

    _client = SiteClient(
        user_agent=site.user_agent,
        timeout=site.browser.timeout,
        browser=site.browser,
        cookie_hosts=site.cookie_hosts,
    )
    await _client.start()
    _client_site = site
    logger.info(
        "取图客户端已启动（站点=%s，来源=%s，指纹=%s，自定义 Cookie %d 个）",
        site.site_url, site.source or "未知",
        site.describe_browser(), len(site.browser.cookies),
    )
    return _client


async def http_start(application: Any = None) -> None:
    """挂到 post_init()。没有插件声明站点时跳过（不白建一个客户端）。"""
    if _current_site is None:
        logger.info("没有插件声明目标站点，跳过取图客户端启动")
        return
    await http()


async def http_stop(application: Any = None) -> None:
    """挂到 post_shutdown()。"""
    await _drop_client()


def http_health() -> dict[str, Any]:
    """给 /fp、/status 之类的诊断指令用。

    站点与浏览器身份报的是**当前配置**（_current_site），不是上次建客户端时的快照，
    免得改完配置还没重建客户端时显示旧值；调用方先 await http() 就能让两者对齐。
    """
    site = _current_site or _client_site
    if site is None:
        return {"started": False}
    health: dict[str, Any] = {
        "started": _client.started if _client is not None else False,
        "site": site.site_url,
        "impersonate": site.browser.impersonate,
        "fingerprint_active": site.browser.enabled,
        "user_agent": _client.user_agent_in_use if _client is not None else "",
    }
    if site.browser.cookies:
        health["cookies"] = sorted(site.browser.cookies)      # 只报名字，不报值
    return health


def cookie_domains_for(site: Optional[SiteProfile] = None) -> tuple[str, ...]:
    """当前 Cookie 会发给哪些域名（诊断用）。"""
    target = site or _current_site
    return target.cookie_hosts if target else ()


def site_host(site: Optional[SiteProfile] = None) -> str:
    """当前站点主域（诊断用）。"""
    target = site or _current_site
    return host_of(target.site_url) if target else ""
